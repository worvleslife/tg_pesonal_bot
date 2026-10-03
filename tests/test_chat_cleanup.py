import asyncio
from dataclasses import replace
import hashlib
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from telegram import InputFile
from telegram.error import BadRequest, TimedOut

from assistant_bot import chat_cleanup as cleanup, panel
from assistant_bot.config import Config
from assistant_bot.storage import Store


class CleanupTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.cfg = Config('12345:offline', database=Path(self.temp.name)/'db.sqlite3')
        self.db = Store(self.cfg.database)
        self.bot = SimpleNamespace(get_file=AsyncMock(), delete_message=AsyncMock(), send_document=AsyncMock(return_value=SimpleNamespace(message_id=500)))
        self.manager = cleanup.Cleanup(self.bot, self.db, self.cfg)
        self.ctx = SimpleNamespace(bot=self.bot, application=SimpleNamespace(bot_data={'cleanup': self.manager}))

    async def asyncTearDown(self):
        for task in list(self.manager.tasks.values()): task.cancel()
        await asyncio.gather(*list(self.manager.tasks.values()), return_exceptions=True)
        self.db.close()
        self.temp.cleanup()

    def source(self, owner=1, message=10, file='original'):
        item = self.db.add_item(owner, '', 'Файл', 'Материалы', [], kind='document', file_id=file, file_name='file.pdf')
        self.db.record_chat_input(owner, message, 'document', file, 'file.pdf')
        self.db.discover_archives()
        return item

    def local(self, owner=1, file='original', data=b'original bytes'):
        relative = f'{owner}/{file}.bin'
        path = self.manager.path(relative)
        path.parent.mkdir(exist_ok=True)
        path.write_bytes(data)
        self.db.archive_state(owner, file, 'ready', relative_path=relative, size=len(data), sha256=hashlib.sha256(data).hexdigest())
        return path

    async def test_download_hash_and_delete_only_after_verified_local_copy(self):
        self.source()
        self.bot.get_file.return_value = SimpleNamespace(file_size=8, file_path='https://api.telegram.org/file/bot12345:offline/file.pdf')
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'filedata')))
        with patch('assistant_bot.chat_cleanup.httpx.AsyncClient', return_value=client):
            await self.manager.tick()
            self.bot.delete_message.assert_not_awaited()
            await asyncio.gather(*list(self.manager.tasks.values()))
        self.assertEqual(self.db.local_file(1, 'original')['status'], 'ready')
        await self.manager.tick()
        self.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=10)
        self.assertTrue(self.db.get_item(1, 1))

    async def test_bad_size_and_unsafe_url_never_delete_input(self):
        self.source()
        self.bot.get_file.return_value = SimpleNamespace(file_size=99, file_path='https://api.telegram.org/file/bot12345:offline/file.pdf')
        client = httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(200, content=b'short')))
        with patch('assistant_bot.chat_cleanup.httpx.AsyncClient', return_value=client):
            await self.manager.tick()
            await asyncio.gather(*list(self.manager.tasks.values()))
        self.assertEqual(self.db.local_file(1, 'original')['status'], 'retry')
        self.assertEqual(list(self.manager.root.rglob('*.part')), [])
        self.bot.delete_message.assert_not_awaited()
        self.bot.get_file.return_value = SimpleNamespace(file_size=5, file_path='https://evil.example/file')
        with self.assertRaises(cleanup.ArchiveError): await self.manager.download(1, 'original')

    async def test_size_and_quota_refuse_before_download(self):
        self.bot.get_file.return_value = SimpleNamespace(file_size=cleanup.MAX_FILE+1)
        with self.assertRaises(cleanup.ArchiveError): await self.manager.download(1, 'large')
        self.bot.get_file.return_value = SimpleNamespace(file_size=4, file_path='https://api.telegram.org/file/bot12345:offline/test')
        with patch.object(self.db, 'archive_usage', return_value=cleanup.USER_QUOTA):
            with self.assertRaises(cleanup.ArchiveError): await self.manager.download(1, 'large')

    async def test_unsaved_draft_and_other_owner_reference_not_archived(self):
        self.db.record_chat_input(1, 10, 'document', 'secret')
        self.db.add_item(2, '', 'Чужой', 'Материалы', [], kind='document', file_id='secret')
        await self.manager.tick()
        self.assertIsNone(self.db.local_file(1, 'secret'))
        self.bot.delete_message.assert_not_awaited()

    async def test_corrupted_copy_and_deleted_source_preserve_incoming_file(self):
        item = self.source()
        path = self.local()
        path.write_bytes(b'corrupt')
        await self.manager.tick()
        self.assertEqual(self.db.local_file(1, 'original')['status'], 'blocked')
        self.bot.delete_message.assert_not_awaited()
        self.local()
        self.db.queue_delete(1, 10, 'archived_input', 0)
        self.db.delete_item(1, item['id'])
        await self.manager.tick()
        self.bot.delete_message.assert_not_awaited()

    async def test_only_previous_text_removed_and_replayed_pointer_not_reversed(self):
        self.db.record_chat_input(1, 10)
        self.db.record_chat_input(1, 11)
        self.db.record_chat_input(1, 10)
        await self.manager.tick()
        self.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=10)
        self.assertEqual(self.db.get_setting(1, 'ui_last_user_message'), '11')

    async def test_attachment_expiry_persists_across_restart(self):
        with patch('assistant_bot.chat_cleanup.time.time', return_value=10000):
            cleanup.track_delivery(self.ctx, 1, SimpleNamespace(message_id=55))
        self.db.close()
        self.db = Store(self.cfg.database)
        self.manager = cleanup.Cleanup(self.bot, self.db, self.cfg)
        with patch('assistant_bot.chat_cleanup.time.time', return_value=13599): await self.manager.tick()
        self.bot.delete_message.assert_not_awaited()
        with patch('assistant_bot.chat_cleanup.time.time', return_value=13600): await self.manager.tick()
        self.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=55)

    async def test_transient_failure_retries_and_old_unremovable_message_stops(self):
        self.db.queue_delete(1, 10, 'attachment', 0)
        self.bot.delete_message.side_effect = TimedOut()
        await self.manager.tick()
        self.assertEqual(self.db._conn.execute('SELECT status FROM message_cleanup').fetchone()[0], 'pending')
        self.db._conn.execute('UPDATE message_cleanup SET due_at=0')
        self.db._conn.commit()
        self.bot.delete_message.side_effect = BadRequest("Message can't be deleted")
        await self.manager.tick()
        self.assertEqual(self.db._conn.execute('SELECT status FROM message_cleanup').fetchone()[0], 'blocked')

    async def test_original_retrieved_from_disk_and_other_user_denied(self):
        self.source()
        self.local(data=b'PDF original')
        await cleanup.send_original(self.ctx, 1, 'document', 'original', 'nice.pdf')
        payload = self.bot.send_document.await_args.args[1]
        self.assertIsInstance(payload, InputFile)
        self.assertEqual(payload.input_file_content, b'PDF original')
        self.assertEqual(payload.filename, 'nice.pdf')
        self.assertEqual(self.db._conn.execute('SELECT reason FROM message_cleanup').fetchone()[0], 'attachment')
        with self.assertRaises(ValueError): await cleanup.send_original(self.ctx, 2, 'document', 'original')

    async def test_active_panel_is_never_deleted_by_cleanup(self):
        self.db.queue_delete(1, 10, 'previous_panel', 0)
        self.db.set_setting(1, 'ui_panel_message', '10')
        await self.manager.tick()
        self.bot.delete_message.assert_not_awaited()

    async def test_notification_moves_panel_without_invalidating_background_answer(self):
        self.bot.send_message = AsyncMock(side_effect=[SimpleNamespace(message_id=20), SimpleNamespace(message_id=21)])
        self.bot.edit_message_text = AsyncMock(return_value=True)
        manager = panel.Panel(self.bot, self.db)
        handle = await manager.render(1, 'Думаю над ответом')
        await manager.notify(1, 'Пора на встречу')
        self.assertEqual(handle.message_id, 21)
        self.assertTrue(await handle.edit_text('Готовый ответ'))
        self.assertIn('Пора на встречу', self.bot.edit_message_text.await_args.kwargs['text'])
        self.assertIn('Готовый ответ', self.bot.edit_message_text.await_args.kwargs['text'])
        self.bot.delete_message.assert_awaited_once_with(chat_id=1, message_id=20)

    async def test_archive_path_cannot_escape_root(self):
        for path in ('../private', '', str(Path(self.temp.name)/'other')):
            with self.assertRaises(ValueError): self.manager.path(path)
