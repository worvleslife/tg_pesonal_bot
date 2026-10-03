"""File boundaries, review-before-index, privacy, restart and async integration."""
import asyncio
from dataclasses import replace
from io import BytesIO
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from PIL import Image
from pypdf import PdfWriter
from pypdf.generic import DictionaryObject, NameObject, DecodedStreamObject

import test_work_bot as fixtures
from assistant_bot import bot, recognition, extraction
from assistant_bot.extract_worker import parse
from assistant_bot.storage import Store
from assistant_bot.planner import source as planner_source
from assistant_bot.material_ai import parse_brief, summarize
from assistant_bot.ai import AIError


def pdf_bytes(count=1, blank=False):
    writer = PdfWriter()
    font = DictionaryObject({NameObject('/Type'): NameObject('/Font'), NameObject('/Subtype'): NameObject('/Type1'), NameObject('/BaseFont'): NameObject('/Helvetica')})
    for _ in range(count):
        page = writer.add_blank_page(200, 200)
        if not blank:
            page[NameObject('/Resources')] = DictionaryObject({NameObject('/Font'): DictionaryObject({NameObject('/F1'): writer._add_object(font)})})
            stream = DecodedStreamObject()
            stream.set_data(b'BT /F1 12 Tf 10 100 Td (Camera manual report) Tj ET')
            page[NameObject('/Contents')] = writer._add_object(stream)
    result = BytesIO()
    writer.write(result)
    return result.getvalue()


def ogg_page(body, *, serial=1, sequence=0, granule=0, flags=2):
    header = b'OggS\x00' + bytes([flags]) + granule.to_bytes(8,'little') + serial.to_bytes(4,'little') + sequence.to_bytes(4,'little') + b'\0'*4 + bytes([1,len(body)])
    return header + body


def voice_bytes(seconds=1, channels=1):
    head = b'OpusHead' + bytes([1,channels]) + b'\0\0' + (48000).to_bytes(4,'little') + b'\0\0\0'
    return ogg_page(head) + ogg_page(b'abc', sequence=1, granule=seconds*48000, flags=4)


class ParserTests(unittest.IsolatedAsyncioTestCase):
    async def test_ai_brief_requires_source_quotes_and_marks_interpretation(self):
        data={'title':'Оплата курса','summary':'Нужно оплатить курс','details':[{'text':'Сумма 3500 рублей','quote':'Сумма: 3500 рублей'}],
              'actions':[{'text':'Оплатить до 5 октября','quote':'Оплатить до 05.10.2026'}],'uncertainty':''}
        raw='Сумма: 3500 рублей. Оплатить до 05.10.2026.'
        result=parse_brief(json.dumps(data),raw)
        self.assertIn('Выжимка ИИ',result['text'])
        self.assertIn('Опора на текст OCR',result['text'])
        data['details'][0]['quote']='Несуществующие данные'
        with self.assertRaises(AIError):
            parse_brief(json.dumps(data),raw)
        with self.assertRaises(AIError):
            parse_brief('not json',raw)

    async def test_ai_request_has_only_selected_text_no_history_or_tools(self):
        cfg=SimpleNamespace(ai_model_uri='gpt://folder/deepseek',yandex_api_key='key',yandex_folder_id='folder')
        answer={'title':'Счёт','summary':'Оплата','details':[],'actions':[],'uncertainty':'Проверить сумму'}
        client=AsyncMock()
        client.__aenter__.return_value=client
        client.post.return_value=httpx.Response(200,json={'status':'completed','output':[{'type':'message','role':'assistant','content':[{'type':'output_text','text':json.dumps(answer)}]}]})
        with patch('assistant_bot.material_ai.httpx.AsyncClient',return_value=client):
            result=await summarize(cfg,'Счёт 123','Для учёбы')
        self.assertEqual(result['title'],'Счёт')
        payload=client.post.call_args.kwargs['json']
        self.assertFalse(payload['store'])
        self.assertNotIn('tools',payload)
        self.assertEqual(len(payload['input']),1)
        self.assertEqual(json.loads(payload['input'][0]['content'])['ocr_text'],'Счёт 123')
    async def test_timed_out_worker_is_killed(self):
        worker=SimpleNamespace(returncode=None,communicate=AsyncMock(side_effect=TimeoutError),kill=lambda: setattr(worker,'returncode',1),wait=AsyncMock())
        with patch('assistant_bot.extraction.asyncio.create_subprocess_exec',AsyncMock(return_value=worker)):
            with self.assertRaises(TimeoutError):
                await extraction.parse_local(b'pdf','pdf')
        self.assertEqual(worker.returncode,1)
        worker.wait.assert_awaited_once()

    async def test_download_stream_stops_at_hard_limit(self):
        cfg=SimpleNamespace(token='123:token')
        telegram=SimpleNamespace(get_file=AsyncMock(return_value=SimpleNamespace(file_path='https://api.telegram.org/file/bot123:token/a',file_size=None)))
        async def chunks(*args):
            yield b'x'*8_000_000
            yield b'x'
            raise AssertionError('Must stop reading oversized body')
        response=SimpleNamespace(status_code=200,aiter_bytes=chunks)
        stream=AsyncMock()
        stream.__aenter__.return_value=response
        client=AsyncMock()
        client.__aenter__.return_value=client
        from unittest.mock import Mock
        client.stream=Mock(return_value=stream)
        with patch('assistant_bot.extraction.httpx.AsyncClient',return_value=client):
            with self.assertRaises(extraction.ExtractionError):
                await extraction.download(telegram,cfg,{'file_id':'x'})

    async def test_actual_pdf_subprocess_and_limits(self):
        result = await extraction.parse_local(pdf_bytes(21), 'pdf')
        self.assertIn('Camera manual report', result['text'])
        self.assertIn('первые 20 из 21', result['notice'])
        self.assertNotIn('[Страница 21]', result['text'])
        with self.assertRaises(extraction.ExtractionError):
            await extraction.parse_local(pdf_bytes(blank=True), 'pdf')

    async def test_text_worker_unicode_and_bad_binary(self):
        result = await extraction.parse_local(('я'*13000).encode(), 'text')
        self.assertEqual(len(result['text']),12000)
        self.assertIn('ограничен',result['notice'])
        for content in (b'\xff', b'a\0b', b''):
            with self.assertRaises(extraction.ExtractionError):
                await extraction.parse_local(content,'text')

    async def test_image_and_audio_magic_size_duration(self):
        image = BytesIO()
        Image.new('RGB',(50,50),'white').save(image,'PNG')
        self.assertEqual(parse(image.getvalue(),'image')['mime'],'image/png')
        self.assertEqual(parse(voice_bytes(),'voice')['mime'],'audio/ogg')
        for bad in (voice_bytes(31), voice_bytes(channels=2), voice_bytes()[:-1], voice_bytes()+voice_bytes(), b'not audio'):
            with self.assertRaises(ValueError):
                parse(bad,'voice')
        with self.assertRaises(Exception):
            parse(b'not an image','image')
        with self.assertRaises(ValueError):
            parse(b'x'*8_000_001,'text')

    async def test_cloud_requests_use_expected_service_no_raw_errors(self):
        cfg = SimpleNamespace(yandex_api_key='test', yandex_folder_id='folder')
        client = AsyncMock()
        client.__aenter__.return_value = client
        client.post.return_value = httpx.Response(200,json={'textAnnotation':{'fullText':'Проверить питание'}})
        with patch('assistant_bot.extraction.httpx.AsyncClient', return_value=client):
            result = await extraction.recognize_cloud(cfg,b'img','image','image/png')
            self.assertEqual(result['text'],'Проверить питание')
            self.assertEqual(client.post.call_args.args[0],extraction.OCR_ENDPOINT)
            self.assertEqual(client.post.call_args.kwargs['json']['mimeType'],'image/png')
            client.post.return_value = httpx.Response(200,json={'result':{'textAnnotation':{'fullText':'Вложенный ответ'}}})
            wrapped = await extraction.recognize_cloud(cfg,b'img','image','image/png')
            self.assertEqual(wrapped['text'],'Вложенный ответ')
            client.post.return_value = httpx.Response(200,json={'result':'Позвонить завтра'})
            await extraction.recognize_cloud(cfg,b'audio','voice','audio/ogg')
            self.assertEqual(client.post.call_args.kwargs['params']['format'],'oggopus')
            for status in (403,429,500,302):
                client.post.return_value = httpx.Response(status,text='PRIVATE_SECRET')
                with self.assertRaises(extraction.ExtractionError) as error:
                    await extraction.recognize_cloud(cfg,b'img','image','image/png')
                self.assertNotIn('PRIVATE_SECRET',str(error.exception))

    async def test_download_rejects_untrusted_address_and_oversize_before_http(self):
        cfg = SimpleNamespace(token='123:token')
        telegram = SimpleNamespace(get_file=AsyncMock())
        for url, size in [('http://127.0.0.1/secret',10), ('https://api.telegram.org.evil/file',10),
                          ('https://api.telegram.org/file/bot123:token/a',8_000_001)]:
            telegram.get_file.return_value = SimpleNamespace(file_path=url,file_size=size)
            with patch('assistant_bot.extraction.httpx.AsyncClient') as http:
                with self.assertRaises(extraction.ExtractionError):
                    await extraction.download(telegram,cfg,{'file_id':'x'})
                http.assert_not_called()

    async def test_invalid_cloud_file_is_not_sent(self):
        with patch('assistant_bot.extraction.download',AsyncMock(return_value=b'invalid')), patch('assistant_bot.extraction.recognize_cloud',AsyncMock()) as cloud:
            with self.assertRaises(extraction.ExtractionError):
                await extraction.extract(None,None,{'file_id':'x','kind':'voice'})
            cloud.assert_not_awaited()


class RecognitionTests(unittest.IsolatedAsyncioTestCase):
    asyncSetUp=fixtures.WorkBotTests.asyncSetUp
    asyncTearDown=fixtures.WorkBotTests.asyncTearDown
    update=fixtures.WorkBotTests.update
    texts=staticmethod(fixtures.WorkBotTests.texts)
    buttons=staticmethod(fixtures.WorkBotTests.buttons)
    callbacks=fixtures.WorkBotTests.callbacks
    callback_with_prefix=fixtures.WorkBotTests.callback_with_prefix
    click=fixtures.WorkBotTests.click
    say=fixtures.WorkBotTests.say

    def item(self, owner=None, kind='document', file_name='manual.pdf'):
        return self.db.add_item(owner or self.owner,'Моя подпись','Инструкция','Работа',[],kind=kind,file_name=file_name,file_id='original')

    def draft(self, item, kind='library', text='Секретный протокол проверки камеры'):
        owner=item['owner_id']
        job=self.db.start_extraction(owner,kind,item['id'])
        return self.db.finish_extraction(owner,kind,item['id'],job['revision'],text=text,method='PDF',notice='Проверь текст')

    async def test_draft_not_searchable_until_confirm_original_unchanged(self):
        item=self.item()
        row=self.draft(item)
        self.assertEqual(self.db.memory_search(self.owner,'протокол'),[])
        self.assertEqual(self.db.count_items(self.owner,query='протокол'),0)
        view=await self.click(f'rec:view:library:{item["id"]}:0')
        accept=self.callback_with_prefix(view,'rec:accept:')
        await self.click(accept)
        await self.click(accept)
        self.assertEqual(len(self.db.memory_search(self.owner,'протокол')),1)
        self.assertEqual(self.db.count_items(self.owner,query='протокол'),1)
        self.assertEqual(self.db.get_item(self.owner,item['id'])['text'],'Моя подпись')
        self.assertEqual(self.db.get_item(self.owner,item['id'])['file_id'],'original')
        self.assertIn('протокол',planner_source(self.ctx,self.owner,'library',item['id'])['text'])
        self.assertEqual(len(self.db.extraction_export(self.owner)['extraction_versions']),1)

    async def test_edit_revision_history_and_discard_preserves_accepted(self):
        item=self.item()
        self.draft(item,text='Было 10'*400)
        self.db.accept_extraction(self.owner,'library',item['id'],0)
        view=await self.click(f'rec:view:library:{item["id"]}:1')
        await self.click(self.callback_with_prefix(view,'rec:edit:'))
        updated=await self.say('Теперь 20')
        self.assertIn('Было 10',self.db.material_text(self.owner,'library',item['id']))
        await self.click(self.callback_with_prefix(updated,'rec:accept:'))
        current=self.db.extraction(self.owner,'library',item['id'])
        self.assertIn('Теперь 20',current['accepted'])
        history=await self.click(f'rec:version:library:{item["id"]}:1:1')
        self.assertIn('Было 10',self.texts(history))
        self.assertNotIn('Теперь 20',self.texts(history))
        old_edit=f'rec:edit:library:{item["id"]}:1:0'
        self.assertIn('изменился',self.texts(await self.click(old_edit)))
        self.db.edit_extraction(self.owner,'library',item['id'],current['revision'],'UNCONFIRMED')
        revised=self.db.extraction(self.owner,'library',item['id'])
        await self.click(f'rec:discard:library:{item["id"]}:{revised["revision"]}')
        self.assertNotIn('UNCONFIRMED',self.db.material_text(self.owner,'library',item['id']))

    async def test_other_user_and_group_cannot_read_edit_or_download(self):
        item=self.item()
        self.draft(item)
        for action in ('view','accept','discard','download','history','edit','version','remind'):
            update=await self.click(f'rec:{action}:library:{item["id"]}:0:0', user=self.other,ctx=self.other_ctx)
            self.assertNotIn('Секретный',self.texts(update))
            update.effective_message.reply_document.assert_not_awaited()
        update=self.update(callback=f'rec:view:library:{item["id"]}:0',chat_type='group')
        await bot.callback(update,self.ctx)
        update.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(self.db.extraction(self.owner,'library',item['id'])['status'],'draft')
        self.assertEqual(self.db.extraction_export(self.other)['extractions'],[])

    async def test_work_and_library_delete_cascade_and_migration_recovery(self):
        item=self.item()
        self.draft(item)
        self.db.accept_extraction(self.owner,'library',item['id'],0)
        self.db.delete_item(self.owner,item['id'])
        self.assertEqual(self.db.extraction_export(self.owner)['extraction_versions'],[])
        section=self.db.create_work_section(self.owner,'Раздел')
        work=self.db.add_work_material(self.owner,section['id'],title='Фото',kind='photo',file_id='photo')
        self.draft(work,'work')
        self.db.accept_extraction(self.owner,'work',work['id'],0)
        self.assertEqual(self.db.memory_search(self.owner,'протокол')[0]['source'],'work')
        self.db.delete_work_material(self.owner,work['id'])
        self.assertEqual(self.db.extraction_export(self.owner)['extractions'],[])
        with tempfile.TemporaryDirectory() as directory:
            path=Path(directory)/'db.sqlite'
            store=Store(path)
            source=store.add_item(self.owner,'','File','Входящие',[],file_id='x')
            store.start_extraction(self.owner,'library',source['id'])
            store.close()
            store=Store(path)
            store.recover_extractions()
            self.assertEqual(store.extraction(self.owner,'library',source['id'])['status'],'failed')
            self.assertIsNotNone(store.start_extraction(self.owner,'library',source['id']))
            self.assertEqual(store._conn.execute('PRAGMA foreign_key_check').fetchall(),[])
            store.close()

    async def test_background_job_releases_lock_and_survives_navigation(self):
        item=self.item()
        started,finish=asyncio.Event(),asyncio.Event()
        async def extract(*args):
            self.assertFalse(self.app.bot_data['lock'].locked())
            started.set()
            await finish.wait()
            return dict(text='Готовый протокол',method='PDF',notice='')
        self.app.create_task=asyncio.create_task
        with patch('assistant_bot.recognition.extract',side_effect=extract) as extract_mock:
            view=await self.click(f'rec:view:library:{item["id"]}:0')
            start=self.callback_with_prefix(view,'rec:start:')
            await self.click(start)
            await started.wait()
            task=self.app.bot_data['extraction_tasks'][self.owner]
            await self.click(start)
            await self.say('☰ Ещё')
            self.assertFalse(task.done())
            finish.set()
            await task
            extract_mock.assert_awaited_once()
        self.assertEqual(self.db.extraction(self.owner,'library',item['id'])['status'],'draft')
        self.assertEqual(self.app.bot_data['extraction_tasks'],{})

    async def test_delete_during_processing_does_not_resurrect_or_deliver(self):
        item=self.item()
        row=self.db.start_extraction(self.owner,'library',item['id'])
        pending=SimpleNamespace(edit_text=AsyncMock())
        async def extract(*args):
            self.db.delete_item(self.owner,item['id'])
            return dict(text='Deleted secret',method='OCR',notice='')
        with patch('assistant_bot.recognition.extract',side_effect=extract):
            await recognition.run(self.ctx,self.owner,'library',item['id'],item,row['revision'],pending)
        pending.edit_text.assert_not_awaited()
        self.assertIsNone(self.db.extraction(self.owner,'library',item['id']))

    async def test_failed_job_retry_and_cloud_quota(self):
        item=self.item(kind='photo',file_name=None)
        self.app.create_task=asyncio.create_task
        self.app.bot_data['config']=replace(self.app.bot_data['config'],yandex_api_key='test',ai_daily_limit=1)
        view=await self.click(f'rec:view:library:{item["id"]}:0')
        self.assertIn('Передам',self.texts(view))
        with patch('assistant_bot.recognition.extract',side_effect=extraction.ExtractionError('Нет прав OCR')):
            await self.click(self.callback_with_prefix(view,'rec:start:'))
            await self.app.bot_data['extraction_tasks'][self.owner]
        self.assertEqual(self.db.extraction(self.owner,'library',item['id'])['status'],'failed')
        view=await self.click(f'rec:view:library:{item["id"]}:0')
        denied=await self.click(self.callback_with_prefix(view,'rec:start:'))
        self.assertIn('исчерпан',self.texts(denied))

    async def test_pagination_download_cancel_and_utf16_bound(self):
        item=self.item()
        self.draft(item,text='😀<&'*3000)
        view=await self.click(f'rec:view:library:{item["id"]}:0')
        from html import unescape
        import re
        text=unescape(re.sub('<[^>]+>','',self.texts(view)))
        self.assertLess(len(text.encode('utf-16-le'))//2,4096)
        download=await self.click(self.callback_with_prefix(view,'rec:download:'))
        download.effective_message.reply_document.assert_awaited_once()
        await self.click(self.callback_with_prefix(view,'rec:edit:'))
        await bot.command(self.update('/cancel'),self.ctx)
        self.assertNotIn('rec_edit',self.ctx.user_data)
        self.assertEqual(self.db.extraction(self.owner,'library',item['id'])['candidate'],'😀<&'*3000)

    async def test_link_to_project_uses_only_confirmed_text(self):
        item=self.item()
        self.draft(item)
        self.db.accept_extraction(self.owner,'library',item['id'],0)
        project=self.db.create_project(self.owner,'Камера','Исправить сбой')
        await self.click(f'mem:attach:{project["id"]}:library:{item["id"]}')
        token=self.ctx.user_data['mem_choice']
        preview=await self.click(f'mem:kind:{token}:instruction')
        await self.click(self.callback_with_prefix(preview,'mem:confirm:'))
        self.assertIn('протокол',self.db.memory_entries(self.owner,project['id'])[0]['body'])

    async def test_project_hints_are_personal_and_not_automatic_links(self):
        project=self.db.create_project(self.owner,'Камера','Проверить питание')
        self.db.create_project(self.other,'Камера PRIVATE','Проверить питание')
        item=self.item()
        self.draft(item,text='Камера: проверить питание')
        view=await self.click(f'rec:accept:library:{item["id"]}:0')
        self.assertIn('совпадению слов',self.texts(view))
        self.assertNotIn('PRIVATE',self.texts(view))
        self.assertIn(f'mem:attach:{project["id"]}:library:{item["id"]}',self.callbacks(view))
        self.assertEqual(self.db.memory_count(self.owner,project['id']),0)

    async def test_job_capacity_prevents_launch_and_delivery_failure_keeps_draft(self):
        item=self.item()
        self.app.bot_data['extraction_tasks']={100:None,101:None}
        view=await self.click(f'rec:view:library:{item["id"]}:0')
        denied=await self.click(self.callback_with_prefix(view,'rec:start:'))
        self.assertIn('другие файлы',self.texts(denied))
        self.assertIsNone(self.db.extraction(self.owner,'library',item['id']))
        self.app.bot_data['extraction_tasks']={}
        row=self.db.start_extraction(self.owner,'library',item['id'])
        from telegram.error import TelegramError
        pending=SimpleNamespace(edit_text=AsyncMock(side_effect=TelegramError('deleted message')))
        with patch('assistant_bot.recognition.extract',AsyncMock(return_value=dict(text='Result',method='PDF',notice=''))):
            await recognition.run(self.ctx,self.owner,'library',item['id'],item,row['revision'],pending)
        self.assertEqual(self.db.extraction(self.owner,'library',item['id'])['candidate'],'Result')

    async def test_direct_project_file_keeps_original_after_library_delete(self):
        project=self.db.create_project(self.owner,'Учёба','Курс')
        await self.click(f'mem:project:{project["id"]}')
        view=await self.say(document=SimpleNamespace(file_id='actual_pdf',file_name='lecture_01.pdf'))
        entry=self.db.memory_entries(self.owner,project['id'])[0]
        self.assertEqual(entry['title'],'lecture 01')
        self.assertIn(f'mem:original:{entry["id"]}',self.callbacks(view))
        self.db.delete_item(self.owner,entry['source_id'])
        await self.click(f'mem:original:{entry["id"]}')
        self.telegram.send_document.assert_awaited_once_with(self.owner,'actual_pdf')
        await self.click(f'mem:original:{entry["id"]}',user=self.other,ctx=self.other_ctx)
        self.assertEqual(self.telegram.send_document.await_count,1)
        self.assertIn('memory_attachments',self.db.export_data(self.owner))
        self.assertEqual(self.db.export_data(self.other)['memory_attachments'],[])
        self.db.delete_memory(self.owner,entry['id'])
        self.assertIsNone(self.db.memory_attachment(self.owner,entry['id']))

    async def test_image_pipeline_retains_raw_and_updates_only_untouched_project_card(self):
        self.app.bot_data['config']=replace(self.app.bot_data['config'],yandex_api_key='test',yandex_folder_id='folder')
        project=self.db.create_project(self.owner,'Учёба','Курс')
        await self.click(f'mem:file:{project["id"]}')
        await self.say(photo=[SimpleNamespace(file_id='actual_photo')])
        entry=self.db.memory_entries(self.owner,project['id'])[0]
        source=self.db.get_item(self.owner,entry['source_id'])
        job=self.db.start_extraction(self.owner,'library',source['id'])
        pending=SimpleNamespace(edit_text=AsyncMock())
        with patch('assistant_bot.recognition.extract',AsyncMock(return_value=dict(text='RAW FULL TEXT',method='OCR',notice=''))),patch('assistant_bot.recognition.summarize',AsyncMock(return_value=dict(title='Оплата курса',text='Выжимка ИИ: оплатить курс'))):
            await recognition.run(self.ctx,self.owner,'library',source['id'],source,job['revision'],pending)
        draft=self.db.extraction(self.owner,'library',source['id'])
        self.assertEqual(draft['source_text'],'RAW FULL TEXT')
        self.assertEqual(draft['candidate_title'],'Оплата курса')
        self.assertNotEqual(self.db.get_memory_entry(self.owner,entry['id'])['title'],'Оплата курса')
        self.db.accept_extraction(self.owner,'library',source['id'],draft['revision'])
        updated=self.db.get_memory_entry(self.owner,entry['id'])
        self.assertEqual(updated['title'],'Оплата курса')
        self.assertEqual(updated['revision'],2)
        self.assertEqual(self.db.memory_attachment(self.owner,entry['id'])['file_id'],'actual_photo')
        self.assertEqual(self.db.get_item(self.owner,source['id'])['file_id'],'actual_photo')
        self.assertEqual(self.db.get_item(self.owner,source['id'])['title'],'Оплата курса')
        raw=await self.click(f'rec:raw:library:{source["id"]}:0')
        self.assertIn('RAW FULL TEXT',self.texts(raw))

    async def test_ai_failure_preserves_ocr_and_manual_project_edits(self):
        self.app.bot_data['config']=replace(self.app.bot_data['config'],yandex_api_key='test',yandex_folder_id='folder')
        project=self.db.create_project(self.owner,'Учёба','Курс')
        await self.click(f'mem:file:{project["id"]}')
        await self.say(photo=[SimpleNamespace(file_id='actual_photo')])
        entry=self.db.memory_entries(self.owner,project['id'])[0]
        self.db.revise_memory(self.owner,entry['id'],1,'Моя личная заметка')
        source=self.db.get_item(self.owner,entry['source_id'])
        job=self.db.start_extraction(self.owner,'library',source['id'])
        pending=SimpleNamespace(edit_text=AsyncMock())
        with patch('assistant_bot.recognition.extract',AsyncMock(return_value=dict(text='RAW FULL TEXT',method='OCR',notice=''))),patch('assistant_bot.recognition.summarize',AsyncMock(side_effect=AIError('error'))):
            await recognition.run(self.ctx,self.owner,'library',source['id'],source,job['revision'],pending)
        draft=self.db.extraction(self.owner,'library',source['id'])
        self.assertEqual(draft['candidate'],'RAW FULL TEXT')
        self.assertIn('не удался',draft['notice'])
        self.db.accept_extraction(self.owner,'library',source['id'],draft['revision'])
        self.assertEqual(self.db.get_memory_entry(self.owner,entry['id'])['body'],'Моя личная заметка')

    async def test_legacy_attachment_backfill_and_refresh_history(self):
        source=self.item()
        project=self.db.create_project(self.owner,'Учёба','Курс')
        entry=self.db.add_memory_entry(self.owner,project['id'],'material','Старое','Старое описание',source_kind='library',source_id=source['id'])
        self.db._conn.execute('DELETE FROM memory_attachments')
        self.db.backfill_memory_attachments()
        self.assertEqual(self.db.memory_attachment(self.owner,entry['id'])['file_id'],'original')
        self.draft(source,text='Новая выжимка')
        self.db.accept_extraction(self.owner,'library',source['id'],0)
        preview=await self.click(f'mem:refresh:{entry["id"]}')
        await self.click(self.callback_with_prefix(preview,'mem:confirm:'))
        self.assertEqual(self.db.get_memory_entry(self.owner,entry['id'])['body'],'Новая выжимка')
        self.assertEqual(self.db.memory_version(self.owner,entry['id'],1)['body'],'Старое описание')
