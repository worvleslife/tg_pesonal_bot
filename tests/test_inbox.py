import asyncio
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import test_work_bot as fixtures
from assistant_bot import bot, inbox, panel
from assistant_bot.ai import AIError
from assistant_bot.inbox_ai import parse
from assistant_bot.storage import Store

TEXT = 'Нужно написать отчёт. Ещё позвонить в школу.'
RESULT = dict(destination='tasks', title='Отчёт и звонок', summary='Два личных дела.', tasks=[
    dict(title='Написать отчёт', minutes=120, reason='Черновик на несколько страниц, без сбора данных.', quote='Нужно написать отчёт'),
    dict(title='Позвонить в школу', minutes=10, reason='Один короткий разговор.', quote='позвонить в школу')])


class ParseTests(unittest.TestCase):
    def test_grounded_tasks_and_full_duration(self):
        self.assertEqual(parse(json.dumps(RESULT), TEXT)['tasks'][0]['minutes'], 120)

    def test_rejects_fabricated_quotes_bad_duration_duplicate_and_route(self):
        for field, value in [('minutes', True), ('minutes', -1), ('minutes', 1441), ('quote', 'купить билет')]:
            data = deepcopy(RESULT)
            data['tasks'][0][field] = value
            with self.assertRaises(AIError):
                parse(json.dumps(data), TEXT)
        for mutation in ('route', 'duplicate', 'empty'):
            data = deepcopy(RESULT)
            if mutation == 'route': data['destination'] = 'library'
            elif mutation == 'duplicate': data['tasks'].append(data['tasks'][0])
            else: data['tasks'] = []
            with self.assertRaises(AIError): parse(json.dumps(data), TEXT)

    def test_reference_material_does_not_require_tasks(self):
        value = dict(destination='library', title='Рецепт', summary='Инструкция для справки.', tasks=[])
        self.assertEqual(parse(json.dumps(value), 'Смешайте муку с водой.'), value)


class InboxTests(unittest.IsolatedAsyncioTestCase):
    update = fixtures.WorkBotTests.update

    async def asyncSetUp(self):
        await fixtures.WorkBotTests.asyncSetUp(self)
        self.db.set_setting(self.owner, 'auto_inbox', 'on')
        self.app.bot_data['config'] = replace(self.app.bot_data['config'], yandex_api_key='offline', yandex_folder_id='testfolder')
        self.telegram.send_message.return_value = SimpleNamespace(message_id=1001)
        self.telegram.edit_message_text = AsyncMock(return_value=True)
        self.telegram.delete_message = AsyncMock()
        self.app.bot_data['panel'] = panel.Panel(self.telegram, self.db)

    async def asyncTearDown(self):
        await bot.shutdown(self.app)
        self.db.close()

    async def wait_jobs(self):
        await asyncio.gather(*list(self.app.bot_data.get('inbox_tasks', {}).values()))

    def item(self, text=TEXT, **kw):
        return self.db.add_item(self.owner, text, 'Исходник', 'Входящие', [], **kw)

    async def auto(self):
        with patch('assistant_bot.inbox.classify', AsyncMock(return_value=deepcopy(RESULT))) as call:
            await bot.message(self.update(TEXT), self.ctx)
            await self.wait_jobs()
        return call

    async def test_handler_routes_tasks_estimates_and_source_in_one_panel(self):
        call = await self.auto()
        tasks = self.db.list_tasks(self.owner)
        self.assertEqual([t['minutes'] for t in tasks], [120, 10])
        self.assertEqual([t['minutes'] for t in self.db.list_tasks(self.owner, minutes=20)], [10])
        self.assertEqual(self.db.count_items(self.owner), 0)
        source = self.db.get_item(self.owner, tasks[0]['source_id'])
        self.assertEqual(source['text'], TEXT)
        self.assertEqual(self.db.count_items(self.owner, query='отчёт'), 1)
        self.assertEqual(self.telegram.send_message.await_count, 1)
        self.assertIn('≈ 120 мин', self.telegram.edit_message_text.await_args.kwargs['text'])
        call.assert_awaited_once()

    async def test_finish_is_idempotent_and_undo_keeps_user_edits(self):
        await self.auto()
        tasks = self.db.list_tasks(self.owner)
        ident = tasks[0]['source_id']
        self.assertFalse(self.db.finish_inbox(self.owner, ident, RESULT))
        self.db.edit_task(self.owner, tasks[0]['id'], minutes=90)
        self.assertEqual(self.db.undo_inbox(self.owner, ident), (1, 1))
        self.assertEqual(self.db.list_tasks(self.owner)[0]['minutes'], 90)
        self.assertEqual(self.db.count_items(self.owner), 1)
        self.assertIsNone(self.db.undo_inbox(self.owner, ident))

    async def test_other_user_cannot_read_undo_or_export_result(self):
        await self.auto()
        task = self.db.list_tasks(self.owner)[0]
        ident = task['source_id']
        self.assertIsNone(self.db.inbox_job(self.other, ident))
        self.assertIsNone(self.db.undo_inbox(self.other, ident))
        self.assertEqual(self.db.inbox_export(self.other)['inbox_tasks'], [])
        self.assertEqual(self.db.list_tasks(self.other), [])
        self.assertEqual(inbox.content(self.db, self.other, ident)[0], 'Материал недоступен.')

    async def test_provider_failure_and_quota_keep_original_without_tasks(self):
        with patch('assistant_bot.inbox.classify', AsyncMock(side_effect=AIError('Временно недоступно.'))):
            await bot.message(self.update(TEXT), self.ctx)
            await self.wait_jobs()
        item = self.db.list_items(self.owner)[0]
        self.assertEqual(self.db.inbox_job(self.owner, item['id'])['status'], 'failed')
        self.assertEqual(self.db.task_counts(self.owner)['active'], 0)
        self.app.bot_data['config'] = replace(self.app.bot_data['config'], ai_daily_limit=1)
        with patch('assistant_bot.inbox.classify', AsyncMock()) as call:
            await bot.callback(self.update(callback=f'inbox:retry:{item["id"]}'), self.ctx)
            await self.wait_jobs()
        call.assert_not_awaited()

    async def test_navigation_during_processing_does_not_get_overwritten(self):
        ready, release = asyncio.Event(), asyncio.Event()
        async def answer(*args):
            ready.set()
            await release.wait()
            return deepcopy(RESULT)
        with patch('assistant_bot.inbox.classify', answer):
            await bot.message(self.update(TEXT), self.ctx)
            await ready.wait()
            await bot.callback(self.update(callback='settings'), self.ctx)
            edits = self.telegram.edit_message_text.await_count
            release.set()
            await self.wait_jobs()
        self.assertEqual(self.telegram.edit_message_text.await_count, edits)
        self.assertEqual(self.db.task_counts(self.owner)['active'], 2)

    async def test_file_extraction_and_mixed_route_preserve_file_and_raw(self):
        result = deepcopy(RESULT)
        result['destination'] = 'mixed'
        with patch('assistant_bot.inbox.extract', AsyncMock(return_value=dict(text=TEXT))), patch('assistant_bot.inbox.classify', AsyncMock(return_value=result)):
            await bot.message(self.update(document=SimpleNamespace(file_id='file', file_name='list.pdf')), self.ctx)
            await self.wait_jobs()
        item = self.db.list_items(self.owner)[0]
        self.assertEqual(item['file_id'], 'file')
        self.assertEqual(self.db.inbox_job(self.owner, item['id'])['raw_text'], TEXT)
        self.assertIn('не проверен', self.db.material_text(self.owner, 'library', item['id']))

    async def test_deleted_source_and_switched_off_midflight_create_no_tasks(self):
        for delete in (True, False):
            item = self.item()
            self.db.set_setting(self.owner, 'auto_inbox', 'on')
            self.db.queue_inbox(self.owner, item['id'])
            self.db.inbox_state(self.owner, item['id'], 'running')
            async def answer(*args):
                if delete: self.db.delete_item(self.owner, item['id'])
                else: self.db.set_setting(self.owner, 'auto_inbox', 'off')
                return deepcopy(RESULT)
            with patch('assistant_bot.inbox.classify', answer):
                await inbox.run(self.ctx, self.owner, item['id'])
        self.assertEqual(self.db.task_counts(self.owner)['active'], 0)

    async def test_edited_title_not_overwritten_and_custom_duration_ui(self):
        item = self.item()
        self.db.queue_inbox(self.owner, item['id'])
        self.db.inbox_state(self.owner, item['id'], 'running')
        self.db.update_item(self.owner, item['id'], 'Моё название', 'Личное', [])
        self.db.finish_inbox(self.owner, item['id'], RESULT)
        self.assertEqual(self.db.get_item(self.owner, item['id'])['title'], 'Моё название')
        task = self.db.list_tasks(self.owner)[0]
        await bot.callback(self.update(callback=f'plan:duration:{task["id"]}'), self.ctx)
        await bot.message(self.update('135'), self.ctx)
        self.assertEqual(self.db.get_task(self.owner, task['id'])['minutes'], 135)
        self.assertEqual(self.db.get_task(self.owner, task['id'])['estimate_ai'], 0)

    async def test_result_transaction_rolls_back_partial_task_creation(self):
        item = self.item()
        self.db.queue_inbox(self.owner, item['id'])
        self.db.inbox_state(self.owner, item['id'], 'running')
        value = deepcopy(RESULT)
        value['tasks'][1]['minutes'] = 0
        with self.assertRaises(Exception): self.db.finish_inbox(self.owner, item['id'], value)
        self.assertEqual(self.db.task_counts(self.owner)['active'], 0)
        self.assertEqual(self.db.inbox_job(self.owner, item['id'])['status'], 'running')


class RestartTests(unittest.TestCase):
    def test_pending_and_completed_jobs_survive_without_duplicate_tasks(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)/'db.sqlite3'
            store = Store(path)
            items = [store.add_item(1, TEXT, 'Материал', 'Личное', []) for _ in range(3)]
            for item in items: store.queue_inbox(1, item['id'])
            store.inbox_state(1, items[1]['id'], 'running')
            store.inbox_state(1, items[2]['id'], 'running')
            store.finish_inbox(1, items[2]['id'], RESULT)
            store.close()
            store = Store(path)
            store.recover_inbox()
            self.assertEqual(len(store.pending_inbox()), 1)
            self.assertEqual(store.inbox_job(1, items[1]['id'])['status'], 'failed')
            self.assertEqual(store.task_counts(1)['active'], 2)
            self.assertFalse(store.finish_inbox(1, items[2]['id'], RESULT))
            self.assertIsNone(store._conn.execute('PRAGMA foreign_key_check').fetchone())
            store.close()
