"""No-network integration checks for Telegram handlers and real SQLite storage."""

import asyncio
import json
import time
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from telegram import MessageEntity

from assistant_bot import bot
from assistant_bot.config import Config
from assistant_bot.storage import Store


class BotIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = 123456
        self.db = Store(":memory:")
        self.telegram = SimpleNamespace(
            send_document=AsyncMock(), send_photo=AsyncMock(), send_voice=AsyncMock(),
            send_audio=AsyncMock(), send_video=AsyncMock(), send_animation=AsyncMock(),
            send_video_note=AsyncMock(), send_message=AsyncMock(),
        )
        self.ctx = SimpleNamespace(
            application=SimpleNamespace(bot_data={
                "config": Config("123456:fake_token_for_offline_tests_only"),
                "store": self.db,
                "lock": asyncio.Lock(),
            }),
            user_data={},
            bot=self.telegram,
        )
        self.other = 999
        self.other_ctx = SimpleNamespace(
            application=self.ctx.application, user_data={}, bot=self.telegram)

    async def asyncTearDown(self):
        self.db.close()

    def update(self, text=None, *, user=None, chat_type="private", callback=None,
               caption=None, document=None, entities=None, chat_id=None, is_bot=False):
        user_id = self.owner if user is None else user
        recipient = user_id if chat_id is None else chat_id
        message = SimpleNamespace(
            text=text, caption=caption, document=document, photo=None, voice=None,
            audio=None, video=None, animation=None, video_note=None,
            chat_id=recipient, message_id=100,
            reply_text=AsyncMock(), reply_document=AsyncMock(),
            parse_entities=lambda: entities or {}, parse_caption_entities=lambda: {},
        )
        query = (SimpleNamespace(data=callback, answer=AsyncMock(), message=message)
                 if callback is not None else None)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, is_bot=is_bot),
            effective_chat=SimpleNamespace(type=chat_type, id=recipient),
            effective_message=message, callback_query=query,
        )

    def last_text(self, update):
        return update.effective_message.reply_text.await_args.args[0]

    def callback_values(self, update):
        markup = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
        return [button.callback_data for row in markup.inline_keyboard for button in row]

    async def test_two_users_can_start_and_save_independent_materials(self):
        for user, ctx, text in ((self.owner, self.ctx, "Первый приватный материал"),
                                (self.other, self.other_ctx, "Второй приватный материал")):
            await bot.command(self.update("/start", user=user), ctx)
            self.assertEqual(self.db.get_setting(user, "started"), "1")
            await bot.message(self.update(text, user=user), ctx)
            self.assertEqual(self.db.count_items(user), 1)
            self.assertEqual(self.db.list_items(user)[0]["text"], text)
        self.assertIs(self.ctx.application, self.other_ctx.application)
        self.assertIsNot(self.ctx.user_data, self.other_ctx.user_data)

    async def test_identity_displays_callers_own_id_without_owner_setup(self):
        for user, ctx in ((self.owner, self.ctx), (self.other, self.other_ctx)):
            update = self.update("/id", user=user)
            await bot.identity(update, ctx)
            self.assertIn(f"<code>{user}</code>", self.last_text(update))
            self.assertNotIn("OWNER_ID", self.last_text(update))

    async def test_new_user_library_does_not_reveal_other_user_materials(self):
        self.db.add_item(self.owner, "Секрет первого", "Секрет первого", "Личное", [])
        update = self.update("/library", user=self.other)
        await bot.command(update, self.other_ctx)
        self.assertIn("Найдено: 0", self.last_text(update))
        self.assertNotIn("Секрет первого", self.last_text(update))

    async def test_owner_group_chat_is_rejected_without_reply_or_save(self):
        update = self.update("Групповая заметка", chat_type="group")
        await bot.message(update, self.ctx)
        update.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(self.db.count_items(self.owner), 0)

    async def test_save_hidden_link_and_search_via_callback_state(self):
        entity = MessageEntity("text_link", 0, 7, url="https://example.com/PythonGuide")
        update = self.update("Учебник #python", entities={entity: "Учебник"})
        await bot.message(update, self.ctx)
        item = self.db.list_items(self.owner)[0]
        self.assertIn("python", item["tags"])
        self.assertIn("https://example.com/PythonGuide", item["urls"])
        self.assertEqual(item["source_message_id"], 100)
        search = self.update(callback="search")
        await bot.callback(search, self.ctx)
        self.assertEqual(self.ctx.user_data["state"], "search")
        result = self.update("PYTHONGUIDE")
        await bot.message(result, self.ctx)
        self.assertIn("Найдено: 1", self.last_text(result))
        self.assertIn(f"item:{item['id']}", self.callback_values(result))
        self.assertNotIn("state", self.ctx.user_data)
        self.assertEqual(self.db.count_items(self.owner), 1)

    async def test_search_command_filters_and_paging_preserves_search(self):
        for index in range(8):
            self.db.add_item(self.owner, f"Python урок {index}", f"Урок {index}", "Учёба", [])
        self.db.add_item(self.owner, "Несовпадение", "Другая тема", "Личное", [])
        search = self.update("/search python")
        await bot.command(search, self.ctx)
        self.assertIn("Найдено: 8", self.last_text(search))
        self.assertIn("page:6", self.callback_values(search))
        second = self.update(callback="page:6")
        await bot.callback(second, self.ctx)
        cards = [value for value in self.callback_values(second) if value.startswith("item:")]
        self.assertEqual(len(cards), 2)
        self.assertEqual(self.ctx.user_data["library_filter"], {"query": "python"})

    async def test_document_is_saved_without_download_and_opened_by_file_id(self):
        update = self.update(caption="Документ #работа", document=SimpleNamespace(
            file_id="telegram-document-id", file_name="notes.pdf"))
        await bot.message(update, self.ctx)
        item = self.db.list_items(self.owner)[0]
        self.assertEqual(item["kind"], "document")
        self.assertEqual(item["file_id"], "telegram-document-id")
        opened = self.update(callback=f"open:{item['id']}")
        await bot.callback(opened, self.ctx)
        self.telegram.send_document.assert_awaited_once_with(self.owner, "telegram-document-id")
        self.assertIn("Документ", self.last_text(opened))

    async def test_favorite_toggle_and_favorite_library(self):
        item = self.db.add_item(self.owner, "Полезное", "Книга", "Обучение", [])
        self.db.add_item(self.owner, "Остальное", "Без звезды", "Личное", [])
        toggle = self.update(callback=f"fav:{item['id']}")
        await bot.callback(toggle, self.ctx)
        self.assertTrue(self.db.get_item(self.owner, item["id"])["favorite"])
        favorites = self.update("⭐ Избранное")
        await bot.message(favorites, self.ctx)
        self.assertIn("Найдено: 1", self.last_text(favorites))
        self.assertIn(f"item:{item['id']}", self.callback_values(favorites))
        await bot.callback(toggle, self.ctx)
        self.assertFalse(self.db.get_item(self.owner, item["id"])["favorite"])

    async def test_reminder_preview_requires_confirm_and_double_click_is_idempotent(self):
        update = self.update("/remind через 10 минут купить хлеб")
        await bot.command(update, self.ctx)
        self.assertEqual(self.db.list_reminders(self.owner), [])
        nonce = self.ctx.user_data["draft"]["nonce"]
        self.assertIn(f"confirm:{nonce}", self.callback_values(update))
        confirm = self.update(callback=f"confirm:{nonce}")
        await bot.callback(confirm, self.ctx)
        reminder = self.db.list_reminders(self.owner)[0]
        self.assertEqual(reminder["text"], "купить хлеб")
        self.assertEqual(reminder["timezone"], "Europe/Moscow")
        await bot.callback(confirm, self.ctx)
        self.assertEqual(len(self.db.list_reminders(self.owner)), 1)
        self.assertIn("уже использована", self.last_text(confirm))

    async def test_new_preview_invalidates_old_confirmation(self):
        await bot.command(self.update("/remind через 10 минут первое"), self.ctx)
        old_nonce = self.ctx.user_data["draft"]["nonce"]
        await bot.command(self.update("/remind через 20 минут второе"), self.ctx)
        current_nonce = self.ctx.user_data["draft"]["nonce"]
        await bot.callback(self.update(callback=f"confirm:{old_nonce}"), self.ctx)
        self.assertEqual(self.db.list_reminders(self.owner), [])
        self.assertEqual(self.ctx.user_data["draft"]["nonce"], current_nonce)
        await bot.callback(self.update(callback=f"confirm:{current_nonce}"), self.ctx)
        self.assertEqual(self.db.list_reminders(self.owner)[0]["text"], "второе")

    async def test_cancel_discards_draft_and_pending_input(self):
        await bot.command(self.update("/remind через 10 минут тест"), self.ctx)
        nonce = self.ctx.user_data["draft"]["nonce"]
        self.ctx.user_data["state"] = "search"
        await bot.command(self.update("/cancel"), self.ctx)
        self.assertEqual(self.ctx.user_data, {})
        await bot.callback(self.update(callback=f"confirm:{nonce}"), self.ctx)
        self.assertEqual(self.db.list_reminders(self.owner), [])

    async def test_all_cross_user_item_callbacks_do_not_disclose_or_mutate(self):
        item = self.db.add_item(self.owner, "ТЕКСТ_СЕКРЕТ", "НАЗВАНИЕ_СЕКРЕТ", "КАТЕГОРИЯ_СЕКРЕТ", [],
                                kind="document", file_id="PRIVATE_FILE_ID")
        for action in ("item", "open", "fav", "edit", "later", "delete", "erase"):
            with self.subTest(action=action):
                update = self.update(callback=f"{action}:{item['id']}", user=self.other)
                await bot.callback(update, self.other_ctx)
                text = self.last_text(update)
                for secret in ("ТЕКСТ_СЕКРЕТ", "НАЗВАНИЕ_СЕКРЕТ", "КАТЕГОРИЯ_СЕКРЕТ", "PRIVATE_FILE_ID"):
                    self.assertNotIn(secret, text)
                self.assertEqual(self.db.get_item(self.owner, item["id"]), item)
                self.assertEqual(self.other_ctx.user_data, {})
                self.telegram.send_document.assert_not_awaited()

    async def test_all_cross_user_reminder_callbacks_do_not_disclose_or_mutate(self):
        reminder = self.db.add_reminder(self.owner, "СЕКРЕТНОЕ_НАПОМИНАНИЕ", int(time.time()) + 3600,
                                        "Europe/Moscow", "daily")
        for action in ("rview", "rd", "rs", "rc", "ra"):
            with self.subTest(action=action):
                data = f"{action}:{reminder['id']}" + (":10" if action == "rs" else "")
                update = self.update(callback=data, user=self.other)
                await bot.callback(update, self.other_ctx)
                self.assertNotIn("СЕКРЕТНОЕ_НАПОМИНАНИЕ", self.last_text(update))
                self.assertEqual(self.db.get_reminder(self.owner, reminder["id"]), reminder)
                self.assertEqual(self.db.list_reminders(self.other), [])
                self.assertEqual(self.other_ctx.user_data, {})

    async def test_users_cannot_confirm_each_others_drafts(self):
        await bot.command(self.update("/remind через 10 минут ПЕРВЫЙ_СЕКРЕТ"), self.ctx)
        await bot.command(self.update("/remind через 20 минут ВТОРОЙ_СЕКРЕТ", user=self.other), self.other_ctx)
        first = self.ctx.user_data["draft"]["nonce"]
        second = self.other_ctx.user_data["draft"]["nonce"]
        for user, ctx, wrong in ((self.owner, self.ctx, second), (self.other, self.other_ctx, first)):
            update = self.update(callback=f"confirm:{wrong}", user=user)
            await bot.callback(update, ctx)
            self.assertIn("устарела", self.last_text(update))
            self.assertEqual(self.db.list_reminders(user), [])
        await bot.callback(self.update(callback=f"confirm:{first}"), self.ctx)
        await bot.callback(self.update(callback=f"confirm:{second}", user=self.other), self.other_ctx)
        self.assertEqual(self.db.list_reminders(self.owner)[0]["text"], "ПЕРВЫЙ_СЕКРЕТ")
        self.assertEqual(self.db.list_reminders(self.other)[0]["text"], "ВТОРОЙ_СЕКРЕТ")

    async def test_owner_cannot_open_other_owners_card(self):
        item = self.db.add_item(999, "Чужой секрет", "Чужой", "Личное", [])
        update = self.update(callback=f"open:{item['id']}")
        await bot.callback(update, self.ctx)
        self.assertIn("уже удалён", self.last_text(update))
        self.assertNotIn("Чужой секрет", self.last_text(update))

    async def test_search_favorites_categories_and_random_are_user_scoped(self):
        first = self.db.add_item(self.owner, "Общий запрос ПЕРВЫЙ_СЕКРЕТ", "Первая карточка", "Категория первого", [])
        second = self.db.add_item(self.other, "Общий запрос ВТОРОЙ_СЕКРЕТ", "Вторая карточка", "Категория второго", [])
        self.db.toggle_favorite(self.owner, first["id"])
        self.db.toggle_favorite(self.other, second["id"])
        for user, ctx, own, foreign in ((self.owner, self.ctx, first, second),
                                        (self.other, self.other_ctx, second, first)):
            with self.subTest(user=user):
                search = self.update("/search Общий", user=user)
                await bot.command(search, ctx)
                self.assertIn("Найдено: 1", self.last_text(search))
                self.assertIn(f"item:{own['id']}", self.callback_values(search))
                self.assertNotIn(f"item:{foreign['id']}", self.callback_values(search))
                favorite = self.update("⭐ Избранное", user=user)
                await bot.message(favorite, ctx)
                self.assertIn("Найдено: 1", self.last_text(favorite))
                self.assertIn(f"item:{own['id']}", self.callback_values(favorite))
                self.assertNotIn(f"item:{foreign['id']}", self.callback_values(favorite))
                categories = self.update(callback="categories", user=user)
                await bot.callback(categories, ctx)
                self.assertEqual(ctx.user_data["categories"], [own["category"]])
                filtered = self.update(callback="cat:0", user=user)
                await bot.callback(filtered, ctx)
                self.assertIn(f"item:{own['id']}", self.callback_values(filtered))
                self.assertNotIn(f"item:{foreign['id']}", self.callback_values(filtered))
                random = self.update("🎲 Вспомнить", user=user)
                await bot.message(random, ctx)
                self.assertIn(own["text"], self.last_text(random))
                self.assertNotIn(foreign["text"], self.last_text(random))
                exact_foreign_search = self.update("/search " + foreign["text"].split()[-1], user=user)
                await bot.command(exact_foreign_search, ctx)
                self.assertIn("Найдено: 0", self.last_text(exact_foreign_search))

    async def test_stats_and_export_contain_only_requesting_user_records(self):
        first = self.db.add_item(self.owner, "ПЕРВЫЙ_СЕКРЕТ", "Первый", "Личное", [])
        self.db.add_item(self.owner, "Ещё первый", "Первый ещё", "Личное", [])
        second = self.db.add_item(self.other, "ВТОРОЙ_СЕКРЕТ", "Второй", "Работа", [])
        self.db.toggle_favorite(self.other, second["id"])
        self.db.add_reminder(self.owner, "НАПОМИНАНИЕ_ПЕРВОГО", int(time.time()) + 1000, "Europe/Moscow")
        self.db.add_reminder(self.other, "НАПОМИНАНИЕ_ВТОРОГО", int(time.time()) + 2000, "Europe/Moscow")
        self.db.set_setting(self.owner, "custom_private_setting", "ЗНАЧЕНИЕ_ПЕРВОГО")
        self.db.set_setting(self.other, "custom_private_setting", "ЗНАЧЕНИЕ_ВТОРОГО")
        for user, ctx, count, favorite_count, secret, foreign_secret in (
            (self.owner, self.ctx, 2, 0, "ПЕРВОГО", "ВТОРОГО"),
            (self.other, self.other_ctx, 1, 1, "ВТОРОГО", "ПЕРВОГО"),
        ):
            with self.subTest(user=user):
                stats = self.update("/stats", user=user)
                await bot.command(stats, ctx)
                self.assertIn(f"Материалов: {count}", self.last_text(stats))
                self.assertIn(f"В избранном: {favorite_count}", self.last_text(stats))
                exported = self.update("/export", user=user)
                await bot.command(exported, ctx)
                payload_bytes = exported.effective_message.reply_document.await_args.args[0].getvalue()
                payload = json.loads(payload_bytes)
                self.assertEqual(payload["owner_id"], user)
                self.assertEqual(len(payload["items"]), count)
                self.assertTrue(all(item["owner_id"] == user for item in payload["items"]))
                self.assertTrue(all(reminder["owner_id"] == user for reminder in payload["reminders"]))
                self.assertEqual(payload["settings"]["custom_private_setting"], f"ЗНАЧЕНИЕ_{secret}")
                self.assertNotIn(f"НАПОМИНАНИЕ_{foreign_secret}", payload_bytes.decode())
                self.assertNotIn(f"ЗНАЧЕНИЕ_{foreign_secret}", payload_bytes.decode())

    async def test_interleaved_wizard_search_and_timezone_states_are_private(self):
        self.db.add_item(self.other, "Материал второго", "Материал второго", "Личное", [])
        await bot.callback(self.update(callback="rnew"), self.ctx)
        await bot.callback(self.update(callback="search", user=self.other), self.other_ctx)
        self.assertEqual(self.ctx.user_data["state"], "reminder_text")
        self.assertEqual(self.other_ctx.user_data["state"], "search")
        await bot.message(self.update("Личный звонок"), self.ctx)
        self.assertEqual(self.ctx.user_data["state"], "reminder_time")
        self.assertEqual(self.other_ctx.user_data["state"], "search")
        search = self.update("Материал", user=self.other)
        await bot.message(search, self.other_ctx)
        self.assertIn("Найдено: 1", self.last_text(search))
        await bot.message(self.update("через 15 минут"), self.ctx)
        self.assertEqual(self.ctx.user_data["draft"]["text"], "Личный звонок")
        self.assertNotIn("draft", self.other_ctx.user_data)
        await bot.callback(self.update(callback="timezone", user=self.other), self.other_ctx)
        await bot.message(self.update("Asia/Yekaterinburg", user=self.other), self.other_ctx)
        self.assertEqual(bot.tz(self.other_ctx, self.other), "Asia/Yekaterinburg")
        self.assertEqual(bot.tz(self.ctx, self.owner), "Europe/Moscow")
        self.assertEqual(self.ctx.user_data["draft"]["timezone"], "Europe/Moscow")

    async def test_group_channel_and_mismatched_private_updates_never_access_data(self):
        item = self.db.add_item(self.owner, "ЗАЩИЩЕННЫЙ_СЕКРЕТ", "Секрет", "Личное", [])
        invalid = (
            {"chat_type": "group"}, {"chat_type": "supergroup"}, {"chat_type": "channel"},
            {"chat_id": self.other}, {"user": 0}, {"user": -1}, {"is_bot": True},
        )
        for params in invalid:
            with self.subTest(params=params):
                for text, handler in (("Не сохранять", bot.message), ("/export", bot.command), ("/id", bot.identity)):
                    update = self.update(text, **params)
                    await handler(update, self.ctx)
                    update.effective_message.reply_document.assert_not_awaited()
                    for call in update.effective_message.reply_text.await_args_list:
                        self.assertNotIn("ЗАЩИЩЕННЫЙ_СЕКРЕТ", call.args[0])
                        self.assertNotIn(f"<code>{self.owner}</code>", call.args[0])
                callback = self.update(callback=f"erase:{item['id']}", **params)
                await bot.callback(callback, self.ctx)
                self.assertEqual(self.db.get_item(self.owner, item["id"]), item)
                self.assertEqual(self.db.count_items(self.owner), 1)
                self.assertEqual(self.db.count_items(self.other), 0)
                self.assertEqual(self.db.count_items(0), 0)
                self.assertEqual(self.db.count_items(-1), 0)

    async def test_real_scheduler_tick_delivers_each_users_reminders_and_digest(self):
        # Exercise the actual application job, not just separately scoped helpers.
        now = 1790668800  # 2026-09-29 08:00 UTC, daytime in the default zone.
        for user, secret in ((self.owner, "FIRST_PRIVATE"), (self.other, "SECOND_PRIVATE")):
            self.db.set_setting(user, "started", "1")
            self.db.set_setting(user, "digest_time", "00:00")
            self.db.add_reminder(user, secret + "_DUE", now - 10, "Europe/Moscow")
            self.db.add_reminder(user, secret + "_LATER", now + 60, "Europe/Moscow")
        with patch("assistant_bot.service.time.time", return_value=now):
            await bot.tick(self.ctx)
        calls = self.telegram.send_message.await_args_list
        self.assertEqual(len(calls), 4)
        for user, own, foreign in ((self.owner, "FIRST_PRIVATE", "SECOND_PRIVATE"),
                                   (self.other, "SECOND_PRIVATE", "FIRST_PRIVATE")):
            messages = [call.kwargs["text"] for call in calls if call.kwargs["chat_id"] == user]
            self.assertEqual(len(messages), 2)
            self.assertTrue(any(own + "_DUE" in text for text in messages))
            self.assertTrue(any(own + "_LATER" in text for text in messages))
            self.assertTrue(all(foreign not in text for text in messages))
        self.telegram.send_message.reset_mock()
        with patch("assistant_bot.service.time.time", return_value=now):
            await bot.tick(self.ctx)
        self.telegram.send_message.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
