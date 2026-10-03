"""Archive before delete. Never delete a user file based only on a Telegram ID."""
import asyncio
import hashlib
import os
from pathlib import Path
import shutil
import time
from urllib.parse import urlsplit
import uuid

import httpx
from telegram import InputFile
from telegram.error import BadRequest, Forbidden, RetryAfter, TelegramError

MAX_FILE=20_000_000
USER_QUOTA=512_000_000
GLOBAL_QUOTA=5_000_000_000
TTL=3600
MEDIA={'document':'send_document','photo':'send_photo','voice':'send_voice','audio':'send_audio',
       'video':'send_video','animation':'send_animation','video_note':'send_video_note'}


def incoming_media(message):
    for kind in MEDIA:
        obj=getattr(message,kind,None)
        if obj:
            return kind,obj[-1] if kind=='photo' else obj
    return None,None


def track_delivery(ctx,owner,message):
    manager=ctx.application.bot_data.get('cleanup')
    if manager:
        manager.store.queue_delete(owner,message.message_id,'attachment',int(time.time())+TTL)


async def send_original(ctx,owner,kind,file_id,file_name=None):
    manager=ctx.application.bot_data.get('cleanup')
    payload=file_id
    if manager:
        row=manager.store.local_file(owner,file_id)
        if row and row['status']=='ready' and manager.store.has_file_reference(owner,file_id):
            path=manager.path(row['relative_path'])
            if path.is_file() and path.stat().st_size==row['size']:
                content=await asyncio.to_thread(path.read_bytes)
                if hashlib.sha256(content).hexdigest()==row['sha256']:
                    payload=InputFile(content,filename=file_name or manager.default_name(kind))
    sent=await getattr(ctx.bot,MEDIA[kind])(owner,payload)
    track_delivery(ctx,owner,sent)
    return sent


async def send_document(update,ctx,document,**kwargs):
    if ctx.application.bot_data.get('cleanup'):
        caption=kwargs.get('caption','')
        kwargs['caption']=(caption+'\nФайл исчезнет из чата через час; данные останутся в боте.').strip()[:1000]
    sent=await update.effective_message.reply_document(document=document,**kwargs)
    track_delivery(ctx,update.effective_user.id,sent)
    return sent


class ArchiveError(Exception):
    pass


class Cleanup:
    def __init__(self,bot,store,cfg):
        self.bot,self.store,self.cfg=bot,store,cfg
        self.root=(cfg.database.parent/'files').resolve()
        self.root.mkdir(parents=True,exist_ok=True)
        self.tasks={}
        self.lock=asyncio.Lock()

    @staticmethod
    def default_name(kind):
        return {'photo':'photo.jpg','voice':'voice.ogg','audio':'audio.mp3','video':'video.mp4',
                'animation':'animation.mp4','video_note':'video.mp4'}.get(kind,'original.bin')

    def path(self,relative):
        result=(self.root/relative).resolve()
        if not relative or not result.is_relative_to(self.root) or result==self.root:
            raise ValueError('Invalid archive path')
        return result

    def received(self,update):
        if update.callback_query:
            return
        msg=update.effective_message
        kind,obj=incoming_media(msg)
        # Unsupported media is left untouched, never mistaken for plain text.
        if not obj and not msg.text:
            return
        self.store.record_chat_input(update.effective_user.id,msg.message_id,kind or 'text',
            obj.file_id if obj else None,getattr(obj,'file_name',None) if obj else None)

    async def download(self,owner,file_id):
        remote=await self.bot.get_file(file_id)
        if remote.file_size and remote.file_size>MAX_FILE:
            raise ArchiveError('Файл больше 20 МБ: исходное сообщение сохранено.')
        url=str(remote.file_path or '')
        parsed=urlsplit(url)
        if parsed.scheme!='https' or parsed.netloc!='api.telegram.org' or not parsed.path.startswith('/file/bot'+self.cfg.token+'/') or parsed.query or parsed.fragment:
            raise ArchiveError('Не удалось получить безопасную ссылку Telegram.')
        if (self.store.archive_usage(owner)+MAX_FILE>USER_QUOTA or self.store.archive_usage()+MAX_FILE*2>GLOBAL_QUOTA
                or shutil.disk_usage(self.root).free<MAX_FILE*3):
            raise ArchiveError('Недостаточно места или достигнут лимит локального архива. Исходник сохранён.')
        relative=f'{owner}/{uuid.uuid4().hex}.bin'
        final=self.path(relative)
        final.parent.mkdir(parents=True,exist_ok=True)
        partial=final.with_suffix('.part')
        digest,size=hashlib.sha256(),0
        try:
            async with httpx.AsyncClient(timeout=45,trust_env=True,follow_redirects=False) as client:
                async with client.stream('GET',url) as response:
                    if response.status_code!=200:
                        raise OSError('Download unavailable')
                    with partial.open('xb') as target:
                        async for chunk in response.aiter_bytes(65536):
                            size+=len(chunk)
                            if size>MAX_FILE:
                                raise ArchiveError('Файл больше 20 МБ: исходник сохранён.')
                            target.write(chunk)
                            digest.update(chunk)
                        target.flush()
                        os.fsync(target.fileno())
            if not size or (remote.file_size and size!=remote.file_size):
                raise OSError('Incomplete download')
            os.replace(partial,final)
            return relative,size,digest.hexdigest()
        finally:
            partial.unlink(missing_ok=True)

    async def archive(self,job):
        owner,file_id=job['owner_id'],job['file_id']
        try:
            if not self.store.has_file_reference(owner,file_id):
                self.store.archive_state(owner,file_id,'unused')
                return
            relative,size,digest=await asyncio.wait_for(self.download(owner,file_id),timeout=120)
            self.store.archive_state(owner,file_id,'ready',relative_path=relative,size=size,sha256=digest)
        except ArchiveError as exc:
            self.store.archive_state(owner,file_id,'blocked',error=str(exc))
        except asyncio.CancelledError:
            self.store.archive_state(owner,file_id,'retry',error='Обработка прервана; исходник сохранён.')
            raise
        except Exception:
            self.store.archive_state(owner,file_id,'retry' if job['attempts']<3 else 'blocked',error='Не удалось сохранить копию. Исходное сообщение не удалено.')
        finally:
            self.tasks.pop((owner,file_id),None)

    async def verify(self,row):
        try:
            path=self.path(row['relative_path'])
            if not path.is_file() or path.stat().st_size!=row['size']:
                return False
            digest=await asyncio.to_thread(lambda:hashlib.sha256(path.read_bytes()).hexdigest())
            return digest==row['sha256']
        except (OSError,ValueError):
            return False

    async def tick(self):
        async with self.lock:
            now=int(time.time())
            self.store.discover_archives()
            if len(self.tasks)<2:
                for job in self.store.archive_jobs(now,2):
                    key=(job['owner_id'],job['file_id'])
                    if key not in self.tasks and len(self.tasks)<2:
                        self.store.archive_state(*key,'running')
                        self.tasks[key]=asyncio.create_task(self.archive(job))
            for row in self.store.archived_receipts():
                if self.store.has_file_reference(row['owner_id'],row['file_id']) and await self.verify(row):
                    self.store.queue_delete(row['owner_id'],row['message_id'],'archived_input')
            for row in self.store.cleanup_jobs(now):
                owner,ident=row['owner_id'],row['message_id']
                if self.store.get_setting(owner,'ui_panel_message','')==str(ident):
                    continue
                if row['reason']=='previous_input' and self.store.get_setting(owner,'ui_last_user_message','')==str(ident):
                    continue
                if row['reason']=='archived_input':
                    with self.store._lock:
                        receipt=self.store._conn.execute('SELECT file_id FROM chat_receipts WHERE owner_id=? AND message_id=?',(owner,ident)).fetchone()
                    local=self.store.local_file(owner,receipt['file_id']) if receipt else None
                    if not local or local['status']!='ready' or not self.store.has_file_reference(owner,receipt['file_id']) or not await self.verify(local):
                        self.store.cleanup_result(owner,ident,'blocked','Нет проверенной локальной копии')
                        continue
                try:
                    await asyncio.wait_for(self.bot.delete_message(chat_id=owner,message_id=ident),timeout=10)
                except RetryAfter as exc:
                    delay=exc.retry_after.total_seconds() if hasattr(exc.retry_after,'total_seconds') else exc.retry_after
                    self.store.cleanup_result(owner,ident,'pending','Telegram: ограничение частоты',now+int(delay)+1)
                    break
                except BadRequest as exc:
                    missing='message to delete not found' in str(exc).lower()
                    self.store.cleanup_result(owner,ident,'done' if missing else 'blocked','' if missing else 'Telegram запретил удаление')
                except Forbidden:
                    self.store.cleanup_result(owner,ident,'blocked','Удаление недоступно')
                except (TelegramError,TimeoutError):
                    self.store.cleanup_result(owner,ident,'pending','Временная ошибка удаления',now+60)
                else:
                    self.store.cleanup_result(owner,ident,'done')
