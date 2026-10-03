"""One persistent navigation message per private user; background-safe edits."""
import asyncio
from contextvars import ContextVar
from dataclasses import dataclass
from html import escape
from html.parser import HTMLParser
import re
import secrets
import json

from telegram import InlineKeyboardButton as B, InlineKeyboardMarkup as K, LinkPreviewOptions, ReplyKeyboardRemove
from telegram.error import BadRequest, TelegramError

from .answer_pages import paginate_answer

_scope = ContextVar('navigation_scope', default=None)


@dataclass
class Scope:
    manager: object
    owner: int
    fresh: bool = False


def bind(ctx, update):
    manager=ctx.application.bot_data.get('panel')
    text=update.effective_message.text or ''
    fresh=text.split(' ',1)[0].split('@',1)[0].lower()=='/start'
    return _scope.set(Scope(manager,update.effective_user.id,fresh) if manager else None)


def unbind(token):
    _scope.reset(token)


async def say(update, text, keyboard=None):
    scope=_scope.get()
    if scope is None:
        # Also usable by isolated feature handlers without an application UI.
        return await update.effective_message.reply_text(text,parse_mode='HTML',reply_markup=keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True))
    fresh,scope.fresh=scope.fresh,False
    return await scope.manager.render(scope.owner,text,keyboard,fresh=fresh)


class PlainText(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts=[]

    def handle_data(self,data):
        self.parts.append(data)


class PanelHandle:
    """A background operation may update only the screen it started on."""
    def __init__(self, manager, owner, message_id, revision):
        self.manager,self.owner,self._message_id,self.revision=manager,owner,message_id,revision

    @property
    def message_id(self):
        return self.manager.message_id(self.owner) if self.manager.revisions.get(self.owner)==self.revision else self._message_id

    async def edit_text(self,text,*,reply_markup=None,**kwargs):
        return await self.manager.finish(self,text,reply_markup)


class Panel:
    def __init__(self,bot,store):
        self.bot,self.store=bot,store
        self.locks={}
        self.revisions={}
        self.page_cache={}

    def lock(self,owner):
        return self.locks.setdefault(owner,asyncio.Lock())

    def message_id(self,owner):
        value=self.store.get_setting(owner,'ui_panel_message','')
        return int(value) if re.fullmatch(r'[1-9][0-9]{0,18}',value or '') else None

    def prepare(self,owner,text,keyboard,revision):
        self.page_cache.pop(owner,None)
        plain=PlainText()
        plain.feed(text)
        plain=''.join(plain.parts)
        if len(plain.encode('utf-16-le'))//2<=3800:
            return text,keyboard
        token=secrets.token_hex(6)
        self.page_cache[owner]={'token':token,'pages':paginate_answer(plain,limit=3300),'keyboard':keyboard,'revision':revision}
        return self.page_content(owner,0)

    def page_content(self,owner,index):
        cached=self.page_cache[owner]
        parts=cached['pages']
        nav=[]
        if index:
            nav.append(B('← Назад',callback_data=f'panel:page:{cached["token"]}:{index-1}'))
        if index+1<len(parts):
            nav.append(B('Далее →',callback_data=f'panel:page:{cached["token"]}:{index+1}'))
        rows=([nav] if nav else [])+list(getattr(cached['keyboard'],'inline_keyboard',()) or ())
        if not rows:
            rows=[[B('🏠 Меню',callback_data='home')]]
        return f'<b>Страница {index+1}/{len(parts)}</b>\n\n'+escape(parts[index]),K(rows)

    async def edit(self,owner,ident,text,keyboard):
        try:
            await self.bot.edit_message_text(chat_id=owner,message_id=ident,text=text,parse_mode='HTML',reply_markup=keyboard,
                link_preview_options=LinkPreviewOptions(is_disabled=True))
            return True
        except BadRequest as exc:
            error=str(exc).lower()
            if 'message is not modified' in error:
                return True
            if any(value in error for value in ('message to edit not found',"message can't be edited",'message_id_invalid')):
                return False
            raise

    async def create(self,owner,text,keyboard,old):
        cleanup=self.store.get_setting(owner,'ui_inline_keyboard','')!='1'
        message=await self.bot.send_message(chat_id=owner,text=text,parse_mode='HTML',
            reply_markup=ReplyKeyboardRemove() if cleanup else keyboard,
            link_preview_options=LinkPreviewOptions(is_disabled=True))
        ident=message.message_id
        self.store.set_setting(owner,'ui_panel_message',str(ident))
        if cleanup:
            self.store.set_setting(owner,'ui_inline_keyboard','1')
            await self.edit(owner,ident,text,keyboard)
        if old and old!=ident:
            self.store.queue_delete(owner,old,'previous_panel')
            try:
                await self.bot.delete_message(chat_id=owner,message_id=old)
            except TelegramError:
                pass  # Durable queue retries only this known obsolete panel.
            else:
                self.store.cleanup_result(owner,old,'done')
        return ident

    def remember(self,owner,text,keyboard):
        self.store.set_setting(owner,'ui_panel_frame',json.dumps({'text':text,'keyboard':keyboard.to_dict() if keyboard else None},ensure_ascii=False))

    async def notify(self,owner,text,keyboard=None):
        """Push a replacement panel; keep the current input screen and job lease."""
        async with self.lock(owner):
            saved=self.store.get_setting(owner,'ui_panel_frame','')
            try:
                frame=json.loads(saved) if saved else {}
            except ValueError:
                frame={}
            body=frame.get('text','')
            old_keyboard=K.de_json(frame['keyboard'],None) if frame.get('keyboard') else None
            rows=list(getattr(keyboard,'inline_keyboard',()) or ())
            rows+=list(getattr(old_keyboard,'inline_keyboard',()) or ())
            if not rows:
                rows=[[B('🏠 Меню',callback_data='home')]]
            self.store.set_setting(owner,'ui_panel_notice',text)
            combined=text+('\n\n────────\n'+body if body else '')
            revision=self.revisions.setdefault(owner,1)
            combined,keys=self.prepare(owner,combined,K(rows),revision)
            ident=await self.create(owner,combined,keys,self.message_id(owner))
            return PanelHandle(self,owner,ident,revision)

    async def render(self,owner,text,keyboard=None,*,fresh=False):
        async with self.lock(owner):
            revision=self.revisions.get(owner,0)+1
            self.revisions[owner]=revision
            self.store.set_setting(owner,'ui_panel_notice','')
            self.remember(owner,text,keyboard)
            text,keyboard=self.prepare(owner,text,keyboard,revision)
            ident=self.message_id(owner)
            if fresh or not ident or not await self.edit(owner,ident,text,keyboard):
                ident=await self.create(owner,text,keyboard,ident)
            return PanelHandle(self,owner,ident,revision)

    async def finish(self,handle,text,keyboard):
        async with self.lock(handle.owner):
            if self.revisions.get(handle.owner)!=handle.revision or self.message_id(handle.owner)!=handle.message_id:
                return False
            self.remember(handle.owner,text,keyboard)
            notice=self.store.get_setting(handle.owner,'ui_panel_notice','')
            if notice:
                text=notice+'\n\n────────\n'+text
            text,keyboard=self.prepare(handle.owner,text,keyboard,handle.revision)
            # Missing progress panel does not cause an unsolicited replacement.
            return await self.edit(handle.owner,handle.message_id,text,keyboard)

    async def page(self,update):
        owner=update.effective_user.id
        match=re.fullmatch(r'panel:page:([a-f0-9]{12}):([0-9]{1,4})',update.callback_query.data or '')
        async with self.lock(owner):
            cache=self.page_cache.get(owner)
            if (not match or not cache or cache['token']!=match[1]
                or cache['revision']!=self.revisions.get(owner) or int(match[2])>=len(cache['pages'])
                or update.effective_message.message_id!=self.message_id(owner)):
                await update.callback_query.answer('Экран устарел. Открой материал заново или отправь /start.',show_alert=True)
                return
            await update.callback_query.answer()
            text,keyboard=self.page_content(owner,int(match[2]))
            await self.edit(owner,self.message_id(owner),text,keyboard)
