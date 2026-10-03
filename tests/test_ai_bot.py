"""Offline AI-mode integration tests: isolation, cancellation, and scheduler access."""

import asyncio
from dataclasses import replace
from html import unescape
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from assistant_bot import bot, chat
from assistant_bot.ai import AIError
from assistant_bot.config import Config
from assistant_bot.storage import Store


class AIBotTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.owner = 7267009888
        self.other = 8123456789
        self.db = Store(":memory:")
        self.db.set_setting(self.owner, 'auto_inbox', 'off')
        self.db.set_setting(self.other, 'auto_inbox', 'off')
        self.created_tasks = []
        self.telegram = SimpleNamespace(send_message=AsyncMock())
        self.app = SimpleNamespace(bot_data={
            "config": Config("123456:fake_token_for_offline_tests_only",
                             admin_id=self.owner, yandex_api_key="offline-key",
                             yandex_folder_id="b1gtestfolder12345678"),
            "store": self.db, "lock": asyncio.Lock(),
        }, create_task=self.create_task)
        self.ctx = SimpleNamespace(application=self.app, user_data={}, bot=self.telegram)
        self.other_ctx = SimpleNamespace(application=self.app, user_data={}, bot=self.telegram)

    def create_task(self, coroutine, **kwargs):
        task = asyncio.create_task(coroutine)
        self.created_tasks.append(task)
        return task

    async def asyncTearDown(self):
        for task in self.created_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.created_tasks, return_exceptions=True)
        self.db.close()

    def update(self, text=None, *, user=None, callback=None, chat_type="private",
               chat_id=None, caption=None, document=None, is_bot=False):
        user_id = self.owner if user is None else user
        recipient = user_id if chat_id is None else chat_id
        sent_message = SimpleNamespace(message_id=100, edit_text=AsyncMock())
        message = SimpleNamespace(
            text=text, caption=caption, document=document, photo=None, voice=None,
            audio=None, video=None, animation=None, video_note=None,
            chat_id=recipient, message_id=100,
            reply_text=AsyncMock(return_value=sent_message), reply_document=AsyncMock(),
            edit_text=AsyncMock(),
            parse_entities=lambda: {}, parse_caption_entities=lambda: {},
        )
        query = (SimpleNamespace(data=callback, answer=AsyncMock(), message=message)
                 if callback is not None else None)
        return SimpleNamespace(
            effective_user=SimpleNamespace(id=user_id, is_bot=is_bot, username="tester",
                                           full_name="Тестовый пользователь"),
            effective_chat=SimpleNamespace(type=chat_type, id=recipient),
            effective_message=message, callback_query=query,
        )

    @staticmethod
    def responses(update):
        message = update.effective_message
        edits = message.reply_text.return_value.edit_text.await_args_list
        return ([call.args[0] for call in message.reply_text.await_args_list]
                + [call.args[0] for call in edits])

    async def finish_tasks(self):
        await asyncio.wait_for(asyncio.gather(*self.created_tasks), timeout=2)

    async def enter(self, *, user=None, ctx=None):
        await bot.command(self.update("/chat", user=user), ctx or self.ctx)

    async def test_button_command_and_inline_callback_enter_ai_without_saving_materials(self):
        for value, handler in ((self.update(chat.CHAT_LABEL), bot.message),
                               (self.update("/chat"), bot.command),
                               (self.update(callback="ai:chat"), bot.callback)):
            with self.subTest(handler=handler.__name__):
                self.ctx.user_data.clear()
                with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock) as generate:
                    await handler(value, self.ctx)
                self.assertEqual(self.ctx.user_data.get("state"), "ai")
                greeting = "\n".join(self.responses(value))
                self.assertIn("GPT 6 Astra", greeting)
                self.assertLess(len(greeting), 100)
                self.assertEqual(self.db.count_items(self.owner), 0)
                generate.assert_not_awaited()

    async def test_missing_key_or_folder_is_explained_without_request_or_accidental_note(self):
        complete_config = self.app.bot_data["config"]
        for missing_field in ("yandex_api_key", "yandex_folder_id"):
            with self.subTest(missing_field=missing_field):
                self.app.bot_data["config"] = replace(complete_config, **{missing_field: ""})
                for update, handler in ((self.update(chat.CHAT_LABEL), bot.message),
                                        (self.update("/chat"), bot.command),
                                        (self.update(callback="ai:chat"), bot.callback)):
                    with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock) as generate:
                        await handler(update, self.ctx)
                    self.assertNotEqual(self.ctx.user_data.get("state"), "ai")
                    self.assertIn("не подключён", "\n".join(self.responses(update)))
                    self.assertEqual(self.db.count_items(self.owner), 0)
                    generate.assert_not_awaited()

    async def test_private_chat_guard_rejects_ai_requests_before_processing(self):
        self.ctx.user_data["state"] = "ai"
        for invalid in ({"chat_type": "group"}, {"chat_type": "supergroup"},
                        {"chat_id": self.other}, {"is_bot": True}):
            for update, handler in ((self.update("/chat", **invalid), bot.command),
                                    (self.update("PRIVATE_PROMPT", **invalid), bot.message),
                                    (self.update(callback="ai:clear", **invalid), bot.callback)):
                with self.subTest(invalid=invalid, handler=handler.__name__), \
                        patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock) as generate:
                    await handler(update, self.ctx)
                    generate.assert_not_awaited()
                    update.effective_message.reply_text.assert_not_awaited()
        self.assertEqual(self.db.chat_history(self.owner), [])
        self.assertEqual(self.db.count_users(), 0)

    async def test_ai_answers_use_only_callers_conversation_without_library_content(self):
        self.db.append_chat_turn(self.owner, "FIRST_PRIVATE_QUESTION", "FIRST_PRIVATE_ANSWER")
        self.db.append_chat_turn(self.other, "OTHER_PRIVATE_QUESTION", "OTHER_PRIVATE_ANSWER")
        self.db.add_item(self.owner, "OWN_LIBRARY_SECRET", "PRIVATE_TITLE", "Личное", [])
        self.db.add_item(self.other, "OTHER_LIBRARY_SECRET", "PRIVATE_TITLE", "Личное", [])
        for owner, ctx, expected, forbidden in (
            (self.owner, self.ctx, "FIRST_PRIVATE", "OTHER_PRIVATE"),
            (self.other, self.other_ctx, "OTHER_PRIVATE", "FIRST_PRIVATE"),
        ):
            await self.enter(user=owner, ctx=ctx)
            prompt = self.update("CURRENT_QUESTION", user=owner)
            with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock,
                       return_value="CURRENT_ANSWER") as generate:
                await bot.message(prompt, ctx)
                await self.finish_tasks()
            generate.assert_awaited_once()
            payload = generate.await_args.kwargs
            self.assertEqual(payload["text"], "CURRENT_QUESTION")
            history = str(payload["history"])
            self.assertIn(expected, history)
            for secret in (forbidden, "OWN_LIBRARY_SECRET", "OTHER_LIBRARY_SECRET", "PRIVATE_TITLE"):
                self.assertNotIn(secret, str(payload))
            self.assertEqual(self.db.chat_history(owner)[-2:], [
                {"role": "user", "content": "CURRENT_QUESTION"},
                {"role": "assistant", "content": "CURRENT_ANSWER"},
            ])
            self.assertEqual(self.db.count_items(owner), 1)

    async def test_slow_ai_does_not_block_other_users_or_reminders_and_duplicate_is_rejected(self):
        await self.enter()
        started = asyncio.Event()
        release = asyncio.Event()

        async def slow_reply(**kwargs):
            started.set()
            await release.wait()
            return "Готово"

        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock,
                   side_effect=slow_reply) as generate:
            original = self.update("Долгий вопрос")
            await asyncio.wait_for(bot.message(original, self.ctx), timeout=1)
            await asyncio.wait_for(started.wait(), timeout=1)
            self.assertFalse(self.app.bot_data["lock"].locked())
            duplicate = self.update("Второй вопрос")
            await asyncio.wait_for(bot.message(duplicate, self.ctx), timeout=1)
            self.assertIn("предыдущий", "\n".join(self.responses(original)))
            duplicate.effective_message.reply_text.assert_not_awaited()
            generate.assert_awaited_once()
            await asyncio.wait_for(bot.message(self.update("Независимая заметка", user=self.other),
                                              self.other_ctx), timeout=1)
            self.assertEqual(self.db.count_items(self.other), 1)
            with patch("assistant_bot.bot.dispatch_due", new_callable=AsyncMock) as dispatch, \
                    patch("assistant_bot.bot.send_digests", new_callable=AsyncMock) as digests:
                await asyncio.wait_for(bot.tick(self.other_ctx), timeout=1)
                dispatch.assert_awaited_once()
                digests.assert_awaited_once()
            release.set()
            await self.finish_tasks()
        self.assertNotIn(self.owner, self.app.bot_data["ai_tasks"])

    async def test_two_users_can_request_ai_concurrently_and_receive_only_their_answers(self):
        await self.enter()
        await self.enter(user=self.other, ctx=self.other_ctx)
        release = asyncio.Event()
        both_started = asyncio.Event()
        running = []

        async def slow_reply(**kwargs):
            running.append(kwargs["text"])
            if len(running) == 2:
                both_started.set()
            await release.wait()
            return "Ответ: " + kwargs["text"]

        first = self.update("FIRST_SECRET")
        second = self.update("SECOND_SECRET", user=self.other)
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock, side_effect=slow_reply):
            await bot.message(first, self.ctx)
            await bot.message(second, self.other_ctx)
            await asyncio.wait_for(both_started.wait(), timeout=1)
            release.set()
            await self.finish_tasks()
        self.assertIn("FIRST_SECRET", self.responses(first)[-1])
        self.assertNotIn("SECOND_SECRET", "\n".join(self.responses(first)))
        self.assertIn("SECOND_SECRET", self.responses(second)[-1])
        self.assertNotIn("FIRST_SECRET", "\n".join(self.responses(second)))

    async def test_slow_telegram_delivery_allows_tick_other_user_and_cancellation_without_save(self):
        self.db.append_chat_turn(self.owner, "OLD_QUESTION", "OLD_ANSWER")
        previous = self.db.chat_history(self.owner)
        await self.enter()
        delivery_started = asyncio.Event()
        delivery_cancelled = asyncio.Event()
        release_delivery = asyncio.Event()
        original = self.update("QUESTION_WITH_SLOW_DELIVERY")

        async def slow_delivery(text, **kwargs):
            if text == "Запрос отменён.":
                return
            delivery_started.set()
            try:
                await release_delivery.wait()
            except asyncio.CancelledError:
                delivery_cancelled.set()
                raise

        original.effective_message.reply_text.return_value.edit_text.side_effect = slow_delivery
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock,
                   return_value="Готовый ответ " * 300) as generate:
            await bot.message(original, self.ctx)
            await asyncio.wait_for(delivery_started.wait(), timeout=1)
            generate.assert_awaited_once()
            self.assertFalse(self.app.bot_data["lock"].locked())
            self.assertEqual(self.db.chat_history(self.owner), previous)
            await asyncio.wait_for(bot.message(self.update("Заметка другого пользователя", user=self.other),
                                              self.other_ctx), timeout=1)
            self.assertEqual(self.db.count_items(self.other), 1)
            with patch("assistant_bot.bot.dispatch_due", new_callable=AsyncMock) as dispatch, \
                    patch("assistant_bot.bot.send_digests", new_callable=AsyncMock) as digests:
                await asyncio.wait_for(bot.tick(self.other_ctx), timeout=1)
                dispatch.assert_awaited_once()
                digests.assert_awaited_once()
            await asyncio.wait_for(bot.command(self.update("/cancel"), self.ctx), timeout=1)
            await asyncio.wait_for(delivery_cancelled.wait(), timeout=1)
            await asyncio.wait_for(asyncio.gather(*self.created_tasks, return_exceptions=True), timeout=1)
            release_delivery.set()
        self.assertEqual(self.db.chat_history(self.owner), previous)
        self.assertNotIn(self.owner, self.app.bot_data["ai_tasks"])
        self.assertNotIn("state", self.ctx.user_data)
        # A single processing message is edited in place. Cancellation prevents
        # committing a turn that was not delivered to Telegram.
        self.assertEqual(original.effective_message.reply_text.await_count, 1)
        self.assertEqual(self.responses(original)[-1], "Запрос отменён.")

    async def test_cancel_stops_pending_request_and_does_not_save_unanswered_prompt(self):
        await self.enter()
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def waiting(**kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cancelled.set()
                raise

        original = self.update("Не должен попасть в историю")
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock, side_effect=waiting):
            await bot.message(original, self.ctx)
            await asyncio.wait_for(started.wait(), timeout=1)
            await bot.command(self.update("/cancel"), self.ctx)
            await asyncio.wait_for(cancelled.wait(), timeout=1)
            await asyncio.gather(*self.created_tasks, return_exceptions=True)
        self.assertNotIn("state", self.ctx.user_data)
        self.assertNotIn(self.owner, self.app.bot_data["ai_tasks"])
        self.assertEqual(self.db.chat_history(self.owner), [])
        self.assertEqual(original.effective_message.reply_text.await_count, 1)
        self.assertEqual(self.responses(original)[-1], "Запрос отменён.")
        self.assertEqual(self.db.count_items(self.owner), 0)

    async def test_reset_requires_confirmation_and_clears_only_own_history(self):
        self.db.append_chat_turn(self.owner, "FIRST_QUESTION", "FIRST_ANSWER")
        self.db.append_chat_turn(self.other, "SECOND_QUESTION", "SECOND_ANSWER")
        first_history = self.db.chat_history(self.owner)
        other_history = self.db.chat_history(self.other)
        await self.enter()
        for update, handler in ((self.update("/newchat"), bot.command),
                                (self.update(callback="ai:new"), bot.callback)):
            await handler(update, self.ctx)
            self.assertEqual(self.db.chat_history(self.owner), first_history)
            buttons = update.effective_message.reply_text.await_args.kwargs["reply_markup"]
            self.assertIn("ai:clear", [button.callback_data for row in buttons.inline_keyboard for button in row])
        await bot.callback(self.update(callback="ai:clear"), self.ctx)
        self.assertEqual(self.db.chat_history(self.owner), [])
        self.assertEqual(self.db.chat_history(self.other), other_history)
        self.assertEqual(self.ctx.user_data.get("state"), "ai")

    async def test_confirmed_reset_cancels_pending_response_without_resurrecting_history(self):
        self.db.append_chat_turn(self.owner, "OLD_QUESTION", "OLD_ANSWER")
        await self.enter()
        started = asyncio.Event()

        async def waiting(**kwargs):
            started.set()
            await asyncio.Event().wait()

        original = self.update("UNANSWERED_QUESTION")
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock, side_effect=waiting):
            await bot.message(original, self.ctx)
            await asyncio.wait_for(started.wait(), timeout=1)
            await bot.callback(self.update(callback="ai:clear"), self.ctx)
            await asyncio.gather(*self.created_tasks, return_exceptions=True)
        self.assertEqual(self.db.chat_history(self.owner), [])
        self.assertNotIn(self.owner, self.app.bot_data["ai_tasks"])
        self.assertEqual(self.ctx.user_data.get("state"), "ai")
        self.assertEqual(original.effective_message.reply_text.await_count, 1)
        self.assertEqual(self.responses(original)[-1], "Запрос отменён.")

    async def test_api_error_preserves_completed_history_and_does_not_save_failed_turn(self):
        self.db.append_chat_turn(self.owner, "OLD_QUESTION", "OLD_ANSWER")
        previous = self.db.chat_history(self.owner)
        await self.enter()
        prompt = self.update("FAILED_QUESTION")
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock,
                   side_effect=AIError("Сервис временно недоступен")):
            await bot.message(prompt, self.ctx)
            await self.finish_tasks()
        self.assertIn("временно недоступен", self.responses(prompt)[-1])
        self.assertEqual(self.db.chat_history(self.owner), previous)
        self.assertNotIn(self.owner, self.app.bot_data["ai_tasks"])

    async def test_answer_under_telegram_limit_edits_one_message_in_place(self):
        await self.enter()
        answer = "А" * 3500
        prompt = self.update("Напиши подробный ответ")
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock, return_value=answer):
            await bot.message(prompt, self.ctx)
            await self.finish_tasks()
        prompt.effective_message.reply_text.assert_awaited_once()
        edit = prompt.effective_message.reply_text.return_value.edit_text
        edit.assert_awaited_once()
        self.assertEqual(unescape(edit.await_args.args[0]), answer)
        self.assertEqual(edit.await_args.kwargs["parse_mode"], "HTML")

    async def test_long_model_output_is_navigated_losslessly_in_one_message(self):
        await self.enter()
        answer = ('<b>Текст & данные</b> "цитата" 😀\n' * 330)
        prompt = self.update("Напиши длинный ответ")
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock, return_value=answer):
            await bot.message(prompt, self.ctx)
            await self.finish_tasks()
        prompt.effective_message.reply_text.assert_awaited_once()
        edit = prompt.effective_message.reply_text.return_value.edit_text
        edit.assert_awaited_once()
        first = edit.await_args
        self.assertEqual(first.kwargs["parse_mode"], "HTML")
        pages = [first.args[0]]
        keyboard = first.kwargs["reply_markup"]
        while True:
            next_callbacks = [button.callback_data
                              for row in keyboard.inline_keyboard for button in row
                              if button.callback_data and button.callback_data.startswith("ai:page:")
                              and int(button.callback_data.rsplit(":", 1)[1]) == len(pages)]
            if not next_callbacks:
                break
            page = self.update(callback=next_callbacks[0])
            await bot.callback(page, self.ctx)
            page.effective_message.reply_text.assert_not_awaited()
            page.effective_message.edit_text.assert_awaited_once()
            edit = page.effective_message.edit_text.await_args
            self.assertEqual(edit.kwargs["parse_mode"], "HTML")
            pages.append(edit.args[0])
            keyboard = edit.kwargs["reply_markup"]
        self.assertGreater(len(pages), 1)
        self.assertEqual("".join(unescape(page) for page in pages), answer)
        self.assertTrue(all("<b>" not in page for page in pages))
        self.assertTrue(all(len(unescape(page).encode("utf-16-le")) // 2 <= 4096 for page in pages))
        self.assertEqual(self.ctx.user_data.get("state"), "ai")
        self.assertEqual(self.db.chat_history(self.owner)[-1]["content"], answer)

    async def test_provider_and_privacy_details_are_available_on_request(self):
        greeting = self.update("/chat")
        await bot.command(greeting, self.ctx)
        text = "\n".join(self.responses(greeting))
        self.assertLess(len(text), 100)
        self.assertIn("GPT 6 Astra", text)
        keyboard = greeting.effective_message.reply_text.await_args.kwargs["reply_markup"]
        callbacks = [button.callback_data for row in keyboard.inline_keyboard for button in row]
        self.assertIn("ai:info", callbacks)
        details = self.update(callback="ai:info")
        await bot.callback(details, self.ctx)
        text = "\n".join(self.responses(details))
        self.assertIn("DeepSeek", text)
        self.assertIn("Yandex AI Studio", text)
        self.assertIn("истори", text.lower())
        self.assertIn("библиотек", text.lower())
        self.assertEqual(self.ctx.user_data.get("state"), "ai")

    async def test_cached_pages_reject_other_user_wrong_message_and_invalid_index(self):
        await self.enter()
        answer = "PRIVATE_OWNER_ANSWER " * 500
        prompt = self.update("Подробный ответ")
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock, return_value=answer):
            await bot.message(prompt, self.ctx)
            await self.finish_tasks()
        token, cached = next(iter(self.app.bot_data["ai_answers"].items()))
        self.assertEqual(cached["owner"], self.owner)
        self.assertEqual(cached["message_id"], 100)
        self.other_ctx.user_data["state"] = "ai"
        unauthorized = self.update(user=self.other, callback=f"ai:page:{token}:1")
        wrong_message = self.update(callback=f"ai:page:{token}:1")
        wrong_message.effective_message.message_id = 101
        invalid = [self.update(callback=f"ai:page:{token}:{index}")
                   for index in ("-1", "999", "oops", "")]
        expired = self.update(callback="ai:page:expired:1")
        for update, ctx in [(unauthorized, self.other_ctx), (wrong_message, self.ctx),
                            (expired, self.ctx)] + [(update, self.ctx) for update in invalid]:
            with self.subTest(callback=update.callback_query.data,
                              owner=update.effective_user.id,
                              message=update.effective_message.message_id):
                await bot.callback(update, ctx)
                update.effective_message.edit_text.assert_not_awaited()
                self.assertNotIn("PRIVATE_OWNER_ANSWER", "\n".join(self.responses(update)))
                self.assertNotIn("PRIVATE_OWNER_ANSWER", str(update.callback_query.answer.await_args_list))
                self.assertEqual(ctx.user_data.get("state"), "ai")
        self.assertEqual(cached["page"], 0)

    async def test_navigation_noop_and_info_preserve_pending_request(self):
        await self.enter()
        completed = self.update("Предыдущий вопрос")
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock,
                   return_value="Старый длинный ответ " * 500):
            await bot.message(completed, self.ctx)
            await self.finish_tasks()
        token = next(iter(self.app.bot_data["ai_answers"]))
        started = asyncio.Event()
        release = asyncio.Event()

        async def waiting(**kwargs):
            started.set()
            await release.wait()
            return "Новый ответ"

        pending = self.update("Новый вопрос")
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock, side_effect=waiting):
            await bot.message(pending, self.ctx)
            await asyncio.wait_for(started.wait(), timeout=1)
            task = self.app.bot_data["ai_tasks"][self.owner]
            for callback in (f"ai:page:{token}:1", "ai:noop", "ai:info"):
                page = self.update(callback=callback)
                await bot.callback(page, self.ctx)
                self.assertIs(self.app.bot_data["ai_tasks"].get(self.owner), task)
                self.assertFalse(task.cancelled())
                self.assertEqual(self.ctx.user_data.get("state"), "ai")
                if callback == "ai:noop":
                    page.effective_message.edit_text.assert_not_awaited()
                    page.effective_message.reply_text.assert_not_awaited()
            release.set()
            await self.finish_tasks()
        self.assertEqual(self.db.chat_history(self.owner)[-1]["content"], "Новый ответ")

    async def test_confirmed_new_chat_clears_only_owners_answer_cache(self):
        for owner, ctx in ((self.owner, self.ctx), (self.other, self.other_ctx)):
            await self.enter(user=owner, ctx=ctx)
            prompt = self.update("Подробный вопрос", user=owner)
            with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock,
                       return_value=f"Ответ {owner} " * 500):
                await bot.message(prompt, ctx)
                await self.finish_tasks()
        before = dict(self.app.bot_data["ai_answers"])
        self.assertEqual({entry["owner"] for entry in before.values()}, {self.owner, self.other})
        await bot.command(self.update("/newchat"), self.ctx)
        self.assertEqual(self.app.bot_data["ai_answers"], before)
        await bot.callback(self.update(callback="ai:clear"), self.ctx)
        remaining = self.app.bot_data["ai_answers"]
        self.assertEqual({entry["owner"] for entry in remaining.values()}, {self.other})
        for token, entry in remaining.items():
            self.assertEqual(entry, before[token])

    async def test_quota_blocks_generation_and_keeps_library_available(self):
        self.app.bot_data["config"] = replace(self.app.bot_data["config"], ai_daily_limit=1,
                                              ai_global_daily_limit=2)
        await self.enter()
        with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock, return_value="Ответ") as generate:
            await bot.message(self.update("Первый запрос"), self.ctx)
            await self.finish_tasks()
            denied = self.update("Второй запрос")
            await bot.message(denied, self.ctx)
            self.assertIn("лимит", "\n".join(self.responses(denied)))
            generate.assert_awaited_once()
            await bot.command(self.update("/cancel"), self.ctx)
            await bot.message(self.update("Заметка после лимита"), self.ctx)
        self.assertEqual(self.db.count_items(self.owner), 1)
        self.assertEqual(len(self.db.chat_history(self.owner)), 2)

    async def test_file_and_oversized_text_in_ai_mode_are_not_sent_or_saved(self):
        await self.enter()
        document = SimpleNamespace(file_id="PRIVATE_FILE_ID", file_name="private.pdf")
        for update in (self.update(caption="Секретная подпись", document=document),
                       self.update("А" * 4001)):
            with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock) as generate:
                await bot.message(update, self.ctx)
                generate.assert_not_awaited()
            self.assertTrue(self.responses(update))
            self.assertEqual(self.db.count_items(self.owner), 0)
            self.assertEqual(self.db.chat_history(self.owner), [])
            self.assertEqual(self.ctx.user_data.get("state"), "ai")

    async def test_leaving_ai_by_menu_or_command_restores_library_save(self):
        for update, handler in ((self.update("/cancel"), bot.command),
                                (self.update("/library"), bot.command),
                                (self.update("/id"), bot.identity),
                                (self.update(callback="home"), bot.callback),
                                (self.update("📚 База знаний"), bot.message)):
            with self.subTest(handler=handler.__name__, text=update.effective_message.text):
                await self.enter()
                await handler(update, self.ctx)
                self.assertNotEqual(self.ctx.user_data.get("state"), "ai")
                previous = self.db.count_items(self.owner)
                with patch("assistant_bot.chat.generate_reply", new_callable=AsyncMock) as generate:
                    await bot.message(self.update("Обычная новая заметка"), self.ctx)
                    generate.assert_not_awaited()
                self.assertEqual(self.db.count_items(self.owner), previous + 1)


if __name__ == "__main__":
    unittest.main()
