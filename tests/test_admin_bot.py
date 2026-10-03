"""Offline checks for the administrator directory and user-data isolation."""

import asyncio
import json
import re
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

from telegram import BotCommandScopeChat

from assistant_bot import bot
from assistant_bot.config import Config
from assistant_bot.storage import Store


class AdminBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        # Telegram identifiers must not be truncated to a signed 32-bit integer.
        self.admin = 7267009888
        self.other = 8123456789
        self.db = Store(":memory:")
        self.telegram = SimpleNamespace(
            send_document=AsyncMock(), send_photo=AsyncMock(), send_voice=AsyncMock(),
            send_audio=AsyncMock(), send_video=AsyncMock(), send_animation=AsyncMock(),
            send_video_note=AsyncMock(), send_message=AsyncMock(),
        )
        self.application = SimpleNamespace(bot_data={
            "config": Config("123456:fake_token_for_offline_tests_only", admin_id=self.admin),
            "store": self.db, "lock": asyncio.Lock(),
        })
        self.ctx = SimpleNamespace(application=self.application, user_data={}, bot=self.telegram)
        self.other_ctx = SimpleNamespace(application=self.application, user_data={}, bot=self.telegram)

    async def asyncTearDown(self):
        self.db.close()

    def update(self, text=None, *, user=None, username=None, name="Пользователь",
               chat_type="private", chat_id=None, callback=None, is_bot=False):
        user_id = self.admin if user is None else user
        recipient = user_id if chat_id is None else chat_id
        message = SimpleNamespace(
            text=text, caption=None, document=None, photo=None, voice=None,
            audio=None, video=None, animation=None, video_note=None,
            chat_id=recipient, message_id=100,
            reply_text=AsyncMock(), reply_document=AsyncMock(),
            parse_entities=lambda: {}, parse_caption_entities=lambda: {},
        )
        query = (SimpleNamespace(data=callback, answer=AsyncMock(), message=message)
                 if callback is not None else None)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, is_bot=is_bot, username=username,
                                           full_name=name, first_name=name, last_name=None),
            effective_chat=SimpleNamespace(type=chat_type, id=recipient),
            effective_message=message, callback_query=query,
        )

    @staticmethod
    def response(update):
        return "\n".join(call.args[0] for call in update.effective_message.reply_text.await_args_list)

    @staticmethod
    def buttons(update):
        result = []
        for call in update.effective_message.reply_text.await_args_list:
            markup = call.kwargs.get("reply_markup")
            for row in getattr(markup, "inline_keyboard", ()):
                result.extend(button.callback_data for button in row)
        return result

    async def test_admin_directory_shows_ids_names_and_material_counts(self):
        await bot.command(self.update("/start", user=self.other, username="reader",
                                      name="Читатель <b> & друг"), self.other_ctx)
        for index in range(3):
            self.db.add_item(self.other, f"Содержимое {index}", f"Заголовок {index}", "Личное", [])
        update = self.update("/users")
        await bot.command(update, self.ctx)
        text = self.response(update)
        self.assertIn(f"<code>{self.admin}</code>", text)
        self.assertIn(f"<code>{self.other}</code>", text)
        self.assertIn("reader", text)
        self.assertIn("Читатель &lt;b&gt; &amp; друг", text)
        self.assertNotIn("Читатель <b>", text)
        self.assertRegex(text.lower(), r"материалов\s*:\s*(?:</?[^>]+>\s*)*3")
        self.assertEqual(self.db.count_users(), 2)

    async def test_other_user_is_denied_before_directory_queries(self):
        self.db.record_user(self.admin, username="PRIVATE_ADMIN_USERNAME", display_name="ADMIN_DIRECTORY_NAME")
        for command in ("/users", "/users@Poshmenik_bot"):
            with self.subTest(command=command):
                update = self.update(command, user=self.other)
                with patch.object(self.db, "admin_users", wraps=self.db.admin_users) as listing, \
                        patch.object(self.db, "count_users", wraps=self.db.count_users) as count:
                    await bot.command(update, self.other_ctx)
                listing.assert_not_called()
                count.assert_not_called()
                self.assertTrue(self.response(update))
                for secret in (str(self.admin), "PRIVATE_ADMIN_USERNAME", "ADMIN_DIRECTORY_NAME"):
                    self.assertNotIn(secret, self.response(update))
                self.assertFalse(any(value.startswith("users:") for value in self.buttons(update)))

    async def test_forged_pagination_callbacks_do_not_query_or_reveal_users(self):
        self.db.record_user(self.admin, username="PRIVATE_ADMIN_USERNAME", display_name="ADMIN_DIRECTORY_NAME")
        for callback in ("users:0", "users:8", "users:-8", "users:9999999999999999999999999999", "users:not-a-number"):
            with self.subTest(callback=callback):
                update = self.update(user=self.other, callback=callback)
                with patch.object(self.db, "admin_users", wraps=self.db.admin_users) as listing, \
                        patch.object(self.db, "count_users", wraps=self.db.count_users) as count:
                    await bot.callback(update, self.other_ctx)
                listing.assert_not_called()
                count.assert_not_called()
                for secret in (str(self.admin), "PRIVATE_ADMIN_USERNAME", "ADMIN_DIRECTORY_NAME"):
                    self.assertNotIn(secret, self.response(update))
                update.callback_query.answer.assert_awaited()

    async def test_missing_admin_id_denies_everyone(self):
        self.application.bot_data["config"] = Config("123456:fake_token_for_offline_tests_only")
        self.db.record_user(self.other, display_name="PRIVATE_DIRECTORY_NAME")
        for update, handler in ((self.update("/users"), bot.command),
                                (self.update(callback="users:0"), bot.callback)):
            with patch.object(self.db, "admin_users", wraps=self.db.admin_users) as listing, \
                    patch.object(self.db, "count_users", wraps=self.db.count_users) as count:
                await handler(update, self.ctx)
            listing.assert_not_called()
            count.assert_not_called()
            self.assertNotIn("PRIVATE_DIRECTORY_NAME", self.response(update))

    async def test_admin_directory_rejects_groups_and_mismatched_chats(self):
        invalid = ({"chat_type": "group"}, {"chat_type": "supergroup"},
                   {"chat_type": "channel"}, {"chat_id": self.other}, {"is_bot": True})
        for params in invalid:
            with self.subTest(params=params):
                for update, handler in ((self.update("/users", **params), bot.command),
                                        (self.update(callback="users:0", **params), bot.callback)):
                    with patch.object(self.db, "admin_users", wraps=self.db.admin_users) as listing, \
                            patch.object(self.db, "count_users", wraps=self.db.count_users) as count:
                        await handler(update, self.ctx)
                    listing.assert_not_called()
                    count.assert_not_called()
                    update.effective_message.reply_text.assert_not_awaited()
                    update.effective_message.reply_document.assert_not_awaited()
        self.assertEqual(self.db.count_users(), 0)

    async def test_directory_contains_no_material_reminder_or_private_setting_content(self):
        self.db.record_user(self.other, display_name="Читатель")
        secrets = ("PRIVATE_ITEM_BODY", "PRIVATE_ITEM_TITLE", "PRIVATE_CATEGORY", "PRIVATE_TAG",
                   "PRIVATE_FILE_ID", "PRIVATE_REMINDER", "PRIVATE_SETTING_VALUE")
        self.db.add_item(self.other, secrets[0], secrets[1], secrets[2], [secrets[3]],
                         kind="document", file_id=secrets[4])
        self.db.add_reminder(self.other, secrets[5], int(time.time()) + 3600, "Europe/Moscow")
        self.db.set_setting(self.other, "private_setting", secrets[6])
        for update, handler in ((self.update("/users"), bot.command),
                                (self.update(callback="users:0"), bot.callback)):
            await handler(update, self.ctx)
            for secret in secrets:
                self.assertNotIn(secret, self.response(update))
            self.assertFalse(any(value.startswith(("item:", "open:", "rview:"))
                                 for value in self.buttons(update)))
            update.effective_message.reply_document.assert_not_awaited()

    async def test_admin_cannot_open_modify_or_export_other_users_materials(self):
        self.db.record_user(self.other, display_name="Другой пользователь")
        item = self.db.add_item(self.other, "PRIVATE_BODY", "PRIVATE_TITLE", "Личное", [],
                                kind="document", file_id="PRIVATE_DOCUMENT")
        for action in ("item", "open", "fav", "edit", "later", "delete", "erase"):
            update = self.update(callback=f"{action}:{item['id']}")
            await bot.callback(update, self.ctx)
            self.assertNotIn("PRIVATE_", self.response(update))
            self.assertEqual(self.db.get_item(self.other, item["id"]), item)
        self.telegram.send_document.assert_not_awaited()
        update = self.update("/export")
        await bot.command(update, self.ctx)
        raw = update.effective_message.reply_document.await_args.args[0].getvalue()
        payload = json.loads(raw)
        self.assertEqual(payload["owner_id"], self.admin)
        self.assertEqual(payload["items"], [])
        self.assertNotIn("PRIVATE_", raw.decode())

    async def test_pagination_covers_directory_once_and_remains_stable(self):
        for user in [self.admin] + list(range(8000000100, 8000000116)):
            self.db.record_user(user, username=f"user{user}", display_name=f"Имя {user}")
        seen = []
        for offset in (0, 8, 16):
            update = self.update("/users") if not offset else self.update(callback=f"users:{offset}")
            await (bot.command(update, self.ctx) if not offset else bot.callback(update, self.ctx))
            ids = [int(value) for value in re.findall(r"<code>(\d+)</code>", self.response(update))]
            self.assertEqual(len(ids), min(8, 17 - offset))
            self.assertEqual(len(ids), len(set(ids)))
            seen.extend(ids)
            buttons = self.buttons(update)
            if offset < 16:
                self.assertIn(f"users:{offset + 8}", buttons)
            if offset:
                self.assertIn(f"users:{offset - 8}", buttons)
        self.assertEqual(len(seen), len(set(seen)))
        self.assertEqual(set(seen), {self.admin, *range(8000000100, 8000000116)})
        repeated = self.update(callback="users:0")
        await bot.callback(repeated, self.ctx)
        self.assertEqual([int(value) for value in re.findall(r"<code>(\d+)</code>", self.response(repeated))], seen[:8])

    async def test_protected_handlers_record_latest_public_profile_without_duplicates(self):
        await bot.command(self.update("/start", user=self.other, username="old_name", name="Старое имя"), self.other_ctx)
        await bot.identity(self.update("/id", user=self.other, username="new_name", name="Новое имя"), self.other_ctx)
        self.assertEqual(self.db.count_users(), 1)
        update = self.update("/users")
        await bot.command(update, self.ctx)
        self.assertIn("new_name", self.response(update))
        self.assertIn("Новое имя", self.response(update))
        self.assertNotIn("old_name", self.response(update))
        self.assertNotIn("Старое имя", self.response(update))

    async def test_telegram_admin_command_menu_is_scoped_to_admin_chat_only(self):
        app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()),
                              bot_data=self.application.bot_data,
                              job_queue=SimpleNamespace(run_repeating=Mock()))
        await bot.post_init(app)
        defaults = []
        scoped = []
        for call in app.bot.set_my_commands.await_args_list:
            commands = call.args[0] if call.args else call.kwargs["commands"]
            names = [command.command for command in commands]
            scope = call.kwargs.get("scope")
            if scope is None or getattr(scope, "type", None) == "default":
                defaults.append(names)
                self.assertNotIn("users", names)
            else:
                self.assertIsInstance(scope, BotCommandScopeChat)
                self.assertEqual(scope.chat_id, self.admin)
                scoped.append(names)
        self.assertEqual(len(defaults), 1)
        self.assertEqual(len(scoped), 1)
        self.assertIn("users", scoped[0])
        self.assertTrue(set(defaults[0]).issubset(scoped[0]))

    async def test_without_admin_no_admin_menu_is_registered(self):
        app = SimpleNamespace(bot=SimpleNamespace(set_my_commands=AsyncMock()),
                              bot_data={"config": Config("123456:fake_token_for_offline_tests_only")},
                              job_queue=SimpleNamespace(run_repeating=Mock()))
        await bot.post_init(app)
        for call in app.bot.set_my_commands.await_args_list:
            commands = call.args[0] if call.args else call.kwargs["commands"]
            self.assertNotIn("users", [command.command for command in commands])


if __name__ == "__main__":
    unittest.main()
