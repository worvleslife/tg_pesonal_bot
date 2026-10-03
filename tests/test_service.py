"""Delivery tests with isolated SQLite data and Telegram mocks; no network."""

import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, patch

from telegram.error import Forbidden, RetryAfter, TelegramError

from assistant_bot.service import dispatch_due, send_digest, send_digests, today_text
from assistant_bot.storage import Store


class DeliveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "assistant.sqlite3"
        self.store = Store(self.path)
        self.bot = AsyncMock()
        self.now = int(datetime(2026, 9, 29, 8, 0, tzinfo=timezone.utc).timestamp())

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reminder(self, owner=101, **kwargs):
        data = dict(text="Купить <хлеб> & молоко", due_at=self.now, timezone="UTC")
        data.update(kwargs)
        return self.store.add_reminder(owner, **data)

    def digest(self, clock="09:00"):
        self.store.set_setting(101, "started", "1")
        self.store.set_setting(101, "digest_time", clock)

    async def test_only_configured_owner_is_sent_and_text_is_escaped(self):
        own = self.reminder()
        other = self.reminder(202)
        await dispatch_due(self.store, self.bot, 101, self.now)
        self.bot.send_message.assert_awaited_once()
        sent = self.bot.send_message.await_args.kwargs
        self.assertEqual(101, sent["chat_id"])
        self.assertEqual("HTML", sent["parse_mode"])
        self.assertTrue(sent["disable_web_page_preview"])
        self.assertIn("&lt;хлеб&gt; &amp;", sent["text"])
        buttons = sent["reply_markup"].inline_keyboard
        self.assertEqual(f"rd:{own['id']}", buttons[0][0].callback_data)
        self.assertEqual(f"rs:{own['id']}:10", buttons[1][0].callback_data)
        self.assertEqual("sent", self.store.get_reminder(101, own["id"])["status"])
        self.assertEqual("pending", self.store.get_reminder(202, other["id"])["status"])
        await dispatch_due(self.store, self.bot, 101, self.now)
        self.bot.send_message.assert_awaited_once()

    async def test_retry_after_seconds_and_timedelta_survive_restart(self):
        for delay in (120, timedelta(seconds=120)):
            with self.subTest(delay=delay), patch.dict(
                os.environ, {"PTB_TIMEDELTA": "1" if isinstance(delay, timedelta) else "0"}
            ):
                reminder = self.reminder()
                rid = reminder["id"]
                self.bot.send_message.side_effect = RetryAfter(delay)
                await dispatch_due(self.store, self.bot, 101, self.now)
                current = self.store.get_reminder(101, rid)
                self.assertEqual(self.now, current["due_at"])
                self.assertEqual(self.now + 120, current["retry_at"])
                self.store.close()
                self.store = Store(self.path)
                self.bot.reset_mock()
                self.bot.send_message.side_effect = None
                await dispatch_due(self.store, self.bot, 101, self.now + 119)
                self.bot.send_message.assert_not_awaited()
                await dispatch_due(self.store, self.bot, 101, self.now + 120)
                self.bot.send_message.assert_awaited_once()
                self.assertEqual("sent", self.store.get_reminder(101, rid)["status"])

    async def test_failed_message_does_not_block_others_and_logs_no_content(self):
        first, second = self.reminder(), self.reminder()
        self.bot.send_message.side_effect = [TelegramError("SECRET_TOKEN and private text"), None]
        with self.assertLogs("assistant_bot.service", level="WARNING") as logs:
            await dispatch_due(self.store, self.bot, 101, self.now)
        self.assertNotIn("SECRET_TOKEN", "".join(logs.output))
        self.assertEqual(self.now + 60, self.store.get_reminder(101, first["id"])["retry_at"])
        self.assertEqual("sent", self.store.get_reminder(101, second["id"])["status"])

    async def test_forbidden_backs_off_one_hour(self):
        reminder = self.reminder()
        self.bot.send_message.side_effect = Forbidden("blocked")
        await dispatch_due(self.store, self.bot, 101, self.now)
        self.assertEqual(self.now + 3600, self.store.get_reminder(101, reminder["id"])["retry_at"])

    async def test_recurring_advances_and_has_recurrence_controls(self):
        reminder = self.reminder(repeat="daily", text="<&>" * 2000)
        await dispatch_due(self.store, self.bot, 101, self.now)
        sent = self.bot.send_message.await_args.kwargs
        self.assertLessEqual(len(sent["text"]), 2000)
        self.assertIn("…", sent["text"])
        buttons = sent["reply_markup"].inline_keyboard
        self.assertEqual(f"ra:{reminder['id']}", buttons[0][0].callback_data)
        self.assertEqual(f"rc:{reminder['id']}", buttons[2][0].callback_data)
        current = self.store.get_reminder(101, reminder["id"])
        self.assertEqual("pending", current["status"])
        self.assertEqual(self.now + 86400, current["due_at"])
        self.assertEqual(self.now, current["last_delivered_at"])

    def test_today_includes_overdue_and_local_today_but_no_future_or_other_owner(self):
        self.reminder(text="old", due_at=self.now - 86400)
        self.reminder(text="today", due_at=self.now + 3600)
        self.reminder(text="tomorrow", due_at=self.now + 86400)
        self.reminder(202, text="private")
        result = today_text(self.store, 101, "Europe/Moscow", self.now)
        self.assertIn("old", result)
        self.assertIn("today", result)
        self.assertIn("просрочено", result)
        self.assertNotIn("tomorrow", result)
        self.assertNotIn("private", result)

    def test_today_truncates_on_whole_html_fragments(self):
        for _ in range(100):
            self.reminder(text="<&>" * 500)
        result = today_text(self.store, 101, "UTC", self.now)
        self.assertLessEqual(len(result), 3500)
        self.assertEqual(result.count("<b>"), result.count("</b>"))
        self.assertIn("Ещё", result)
        self.assertNotIn("<&>", result)

    async def test_digest_default_off_started_gate_and_local_time(self):
        await send_digest(self.store, self.bot, 101, "Europe/Moscow", self.now)
        self.store.set_setting(101, "digest_time", "11:00")
        await send_digest(self.store, self.bot, 101, "Europe/Moscow", self.now)
        self.bot.send_message.assert_not_awaited()
        self.store.set_setting(101, "started", "1")
        await send_digest(self.store, self.bot, 101, "Europe/Moscow", self.now - 1)
        self.bot.send_message.assert_not_awaited()
        await send_digest(self.store, self.bot, 101, "Europe/Moscow", self.now)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual("2026-09-29", self.store.get_setting(101, "digest_last"))
        await send_digest(self.store, self.bot, 101, "Europe/Moscow", self.now + 3600)
        self.bot.send_message.assert_awaited_once()
        await send_digest(self.store, self.bot, 101, "Europe/Moscow", self.now + 86400)
        self.assertEqual(2, self.bot.send_message.await_count)

    async def test_digest_retry_is_persistent_and_marks_only_success(self):
        self.digest("08:00")
        self.bot.send_message.side_effect = TelegramError("transient")
        await send_digest(self.store, self.bot, 101, "UTC", self.now)
        self.assertIsNone(self.store.get_setting(101, "digest_last"))
        self.assertEqual(str(self.now + 60), self.store.get_setting(101, "digest_retry_at"))
        self.store.close()
        self.store = Store(self.path)
        self.bot.reset_mock()
        self.bot.send_message.side_effect = None
        await send_digest(self.store, self.bot, 101, "UTC", self.now + 59)
        self.bot.send_message.assert_not_awaited()
        await send_digest(self.store, self.bot, 101, "UTC", self.now + 60)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual("2026-09-29", self.store.get_setting(101, "digest_last"))
        self.store.close()
        self.store = Store(self.path)
        await send_digest(self.store, self.bot, 101, "UTC", self.now + 120)
        self.bot.send_message.assert_awaited_once()

    async def test_multiuser_dispatch_after_restart_keeps_recipients_isolated(self):
        first = self.reminder(101, text="only_alice")
        second = self.reminder(202, text="only_bob")
        self.store.close()
        self.store = Store(self.path)
        await dispatch_due(self.store, self.bot, now=self.now)
        messages = {call.kwargs["chat_id"]: call.kwargs["text"]
                    for call in self.bot.send_message.await_args_list}
        self.assertEqual({101, 202}, set(messages))
        self.assertIn("only_alice", messages[101])
        self.assertNotIn("only_bob", messages[101])
        self.assertIn("only_bob", messages[202])
        self.assertNotIn("only_alice", messages[202])
        self.assertEqual("sent", self.store.get_reminder(101, first["id"])["status"])
        self.assertEqual("sent", self.store.get_reminder(202, second["id"])["status"])
        self.assertIsNone(self.store.get_reminder(202, first["id"]))

    async def test_multiuser_delivery_failure_does_not_block_another_user(self):
        first = self.reminder(101, text="first_user")
        second = self.reminder(202, text="second_user")
        self.bot.send_message.side_effect = [Forbidden("private data"), None]
        with self.assertLogs("assistant_bot.service", level="WARNING"):
            await dispatch_due(self.store, self.bot, now=self.now)
        self.assertEqual(self.now + 3600, self.store.get_reminder(101, first["id"])["retry_at"])
        self.assertEqual("sent", self.store.get_reminder(202, second["id"])["status"])
        self.assertEqual([101, 202], [call.kwargs["chat_id"] for call in self.bot.send_message.await_args_list])

    async def test_optional_dispatch_owner_filter_cannot_be_starved_by_batch_limit(self):
        for _ in range(101):
            self.reminder(101)
        other = self.reminder(202, text="second user")
        await dispatch_due(self.store, self.bot, owner_id=202, now=self.now)
        self.bot.send_message.assert_awaited_once()
        self.assertEqual(202, self.bot.send_message.await_args.kwargs["chat_id"])
        self.assertEqual("sent", self.store.get_reminder(202, other["id"])["status"])
        self.assertEqual(101, self.store.stats(101)["pending"])

    async def test_bounded_failed_batch_leaves_other_user_for_next_tick(self):
        for _ in range(100):
            self.reminder(101)
        self.reminder(202, text="next batch")

        async def send(**kwargs):
            if kwargs["chat_id"] == 101:
                raise Forbidden("blocked")

        self.bot.send_message.side_effect = send
        with self.assertLogs("assistant_bot.service", level="WARNING"):
            await dispatch_due(self.store, self.bot, now=self.now)
        self.assertEqual(100, self.bot.send_message.await_count)
        await dispatch_due(self.store, self.bot, now=self.now)
        self.assertEqual(101, self.bot.send_message.await_count)
        self.assertEqual(202, self.bot.send_message.await_args.kwargs["chat_id"])

    async def test_digests_use_each_users_timezone_plan_and_persisted_settings(self):
        self.reminder(101, text="alice_plan", due_at=self.now + 3600)
        self.reminder(202, text="bob_plan", due_at=self.now + 7200)
        for owner, zone in ((101, "Europe/Moscow"), (202, "America/New_York")):
            self.store.set_setting(owner, "started", "1")
            self.store.set_setting(owner, "digest_time", "09:00")
            self.store.set_setting(owner, "timezone", zone)
            self.store.add_item(owner, f"private_notes_{owner}", "secret", "Личное", [])
        self.store.close()
        self.store = Store(self.path)
        # 08:00 UTC is already 11:00 Moscow, but only 04:00 New York.
        await send_digests(self.store, self.bot, "UTC", now=self.now)
        self.bot.send_message.assert_awaited_once()
        alice = self.bot.send_message.await_args.kwargs
        self.assertEqual(101, alice["chat_id"])
        self.assertIn("Europe/Moscow", alice["text"])
        self.assertIn("alice_plan", alice["text"])
        self.assertNotIn("bob_plan", alice["text"])
        self.assertNotIn("private_notes", alice["text"])
        self.assertIsNone(self.store.get_setting(202, "digest_last"))
        await send_digests(self.store, self.bot, "UTC", now=self.now + 5 * 3600)
        self.assertEqual(2, self.bot.send_message.await_count)
        bob = self.bot.send_message.await_args.kwargs
        self.assertEqual(202, bob["chat_id"])
        self.assertIn("America/New_York", bob["text"])
        self.assertIn("bob_plan", bob["text"])
        self.assertNotIn("alice_plan", bob["text"])
        self.assertNotIn("private_notes", bob["text"])
        self.store.close()
        self.store = Store(self.path)
        await send_digests(self.store, self.bot, "UTC", now=self.now + 6 * 3600)
        self.assertEqual(2, self.bot.send_message.await_count)

    async def test_digest_bad_timezone_and_failed_user_do_not_block_other_users(self):
        for owner in (101, 202, 303):
            self.store.set_setting(owner, "started", "1")
            self.store.set_setting(owner, "digest_time", "08:00")
            self.reminder(owner, text=f"private_{owner}")
        self.store.set_setting(101, "timezone", "private_invalid_zone")
        # The other two have no timezone and must use the configured default.
        self.bot.send_message.side_effect = [TelegramError("private_failure"), None]
        with self.assertLogs("assistant_bot.service", level="WARNING") as logs:
            await send_digests(self.store, self.bot, "UTC", now=self.now)
        logged = "".join(logs.output)
        self.assertNotIn("private_invalid_zone", logged)
        self.assertNotIn("private_failure", logged)
        self.assertEqual([202, 303], [call.kwargs["chat_id"] for call in self.bot.send_message.await_args_list])
        self.assertIsNone(self.store.get_setting(101, "digest_last"))
        self.assertIsNone(self.store.get_setting(202, "digest_last"))
        self.assertEqual("2026-09-29", self.store.get_setting(303, "digest_last"))
        self.assertEqual(str(self.now + 3600), self.store.get_setting(101, "digest_retry_at"))
        self.assertEqual(str(self.now + 60), self.store.get_setting(202, "digest_retry_at"))
        final_message = self.bot.send_message.await_args.kwargs["text"]
        self.assertIn("private_303", final_message)
        self.assertNotIn("private_202", final_message)
        self.assertNotIn("private_101", final_message)


if __name__ == "__main__":
    unittest.main()
