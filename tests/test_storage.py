"""Storage behavior tests: isolation, persistence, search and delivery state."""

import tempfile
import unittest
from pathlib import Path

from assistant_bot.storage import Store


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "nested" / "assistant.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def item(self, owner=101, **kwargs):
        data = dict(text="Полезная статья о Python", title="Статья", category="Обучение",
                    tags=["Python", "python", " Разработка "], urls=["https://example.com/guide"])
        data.update(kwargs)
        return self.store.add_item(owner, **data)

    def test_all_material_actions_are_owner_scoped(self):
        item = self.item()
        other = self.item(202, title="Другая статья")
        self.assertIsNone(self.store.get_item(202, item["id"]))
        self.assertIsNone(self.store.toggle_favorite(202, item["id"]))
        self.assertIsNone(self.store.update_item(202, item["id"], "x", "y", []))
        self.assertFalse(self.store.delete_item(202, item["id"]))
        self.assertEqual([other["id"]], [i["id"] for i in self.store.list_items(202)])
        self.assertEqual(other["id"], self.store.random_item(202)["id"])
        self.assertEqual([("Обучение", 1)], self.store.categories(202))
        self.assertEqual(1, self.store.stats(101)["items"])
        exported = self.store.export_data(202)
        self.assertEqual([other["id"]], [i["id"] for i in exported["items"]])
        self.assertTrue(self.store.delete_item(101, item["id"]))
        self.assertIsNone(self.store.get_item(101, item["id"]))

    def test_unicode_search_filters_literal_terms_and_metadata_update(self):
        item = self.item(file_name="Конспект.PDF")
        self.assertEqual(["Python", "Разработка"], item["tags"])
        self.assertEqual(1, self.store.count_items(101, query="ПОЛЕЗНАЯ PYTHON"))
        self.assertEqual(1, self.store.count_items(101, query="конспект.pdf"))
        self.assertEqual(1, self.store.count_items(101, query="example.com"))
        self.assertEqual(0, self.store.count_items(101, query="%"))
        self.assertEqual(0, self.store.count_items(101, query="' OR 1=1 --"))
        self.assertEqual(0, self.store.count_items(101, favorites=True))
        self.store.toggle_favorite(101, item["id"])
        self.assertEqual(1, self.store.count_items(101, category="Обучение", favorites=True))
        edited = self.store.update_item(101, item["id"], "Новый заголовок", "Работа", ["Проект"])
        self.assertEqual("Работа", edited["category"])
        self.assertEqual(1, self.store.count_items(101, query="НОВЫЙ ПРОЕКТ"))
        self.assertEqual(0, self.store.count_items(101, query="разработка"))
        self.assertEqual(0, self.store.count_items(101, category="Обучение"))

    def test_stable_pagination_and_empty_counts(self):
        self.assertEqual({"items": 0, "favorites": 0, "pending": 0, "completed": 0}, self.store.stats(101))
        ids = [self.item(title=str(i))["id"] for i in range(7)]
        self.assertEqual(ids[::-1][:3], [i["id"] for i in self.store.list_items(101, limit=3)])
        self.assertEqual(ids[::-1][3:6], [i["id"] for i in self.store.list_items(101, offset=3, limit=3)])
        with self.assertRaises(ValueError):
            self.store.list_items(101, limit=-1)

    def test_urls_keep_case_sensitive_paths(self):
        item = self.item(urls=["https://example.com/a", "https://example.com/A", "https://example.com/a"])
        self.assertEqual(["https://example.com/a", "https://example.com/A"], item["urls"])

    def test_restart_preserves_materials_settings_and_reminder_backoff(self):
        item = self.item(kind="document", file_id="tg-file", source_chat_id=101, source_message_id=9)
        reminder = self.store.add_reminder(101, "Повторить", 100, "Europe/Moscow", "daily")
        self.store.retry_reminder(101, reminder["id"], 150)
        self.store.set_setting(101, "timezone", "Europe/Moscow")
        self.store.set_setting(101, "timezone", "Europe/Berlin")
        self.store.set_setting(202, "timezone", "UTC")
        self.store.close()
        self.store = Store(self.path)
        restored = self.store.get_item(101, item["id"])
        self.assertEqual("tg-file", restored["file_id"])
        self.assertEqual(["https://example.com/guide"], restored["urls"])
        self.assertEqual("Europe/Berlin", self.store.get_setting(101, "timezone"))
        self.assertEqual("UTC", self.store.get_setting(202, "timezone"))
        self.assertEqual("fallback", self.store.get_setting(303, "timezone", "fallback"))
        self.assertEqual([], self.store.due_reminders(149))
        self.assertEqual(100, self.store.due_reminders(150)[0]["due_at"])
        self.assertEqual("daily", self.store.due_reminders(150)[0]["repeat"])

    def test_one_off_delivery_snooze_and_completion(self):
        reminder = self.store.add_reminder(101, "Позвонить", 100, "UTC")
        rid = reminder["id"]
        self.assertEqual([], self.store.due_reminders(99))
        self.assertEqual(1, len(self.store.due_reminders(100)))
        self.store.mark_delivered(101, rid, None, 101)
        self.assertEqual([], self.store.list_reminders(101))
        self.assertEqual("sent", self.store.get_reminder(101, rid)["status"])
        self.assertEqual(101, self.store.get_reminder(101, rid)["last_delivered_at"])
        self.assertTrue(self.store.snooze_reminder(101, rid, 200))
        self.assertEqual([], self.store.due_reminders(199))
        self.assertEqual(1, len(self.store.due_reminders(200)))
        self.assertTrue(self.store.complete_reminder(101, rid))
        self.assertFalse(self.store.snooze_reminder(101, rid, 300))
        self.store.retry_reminder(101, rid, 400)
        self.store.mark_delivered(101, rid, 500, 200)
        self.assertEqual("done", self.store.get_reminder(101, rid)["status"])
        self.assertEqual([], self.store.due_reminders(999))
        self.assertEqual(1, self.store.stats(101)["completed"])

    def test_recurring_delivery_and_cancel_clear_retry(self):
        reminder = self.store.add_reminder(101, "Разминка", 100, "UTC", "daily")
        rid = reminder["id"]
        self.store.retry_reminder(101, rid, 120)
        self.store.mark_delivered(101, rid, 86400, 125)
        current = self.store.get_reminder(101, rid)
        self.assertEqual(("pending", 86400, None, 125),
                         (current["status"], current["due_at"], current["retry_at"], current["last_delivered_at"]))
        self.assertEqual([], self.store.due_reminders(86399))
        self.assertEqual(1, self.store.stats(101)["pending"])
        self.assertTrue(self.store.cancel_reminder(101, rid))
        self.assertFalse(self.store.cancel_reminder(101, rid))
        self.assertFalse(self.store.snooze_reminder(101, rid, 90000))
        self.assertEqual([], self.store.due_reminders(999999))

    def test_reminder_operations_and_export_are_owner_scoped(self):
        reminder = self.store.add_reminder(101, "Личное", 100, "UTC")
        rid = reminder["id"]
        self.assertIsNone(self.store.get_reminder(202, rid))
        self.assertFalse(self.store.cancel_reminder(202, rid))
        self.assertFalse(self.store.complete_reminder(202, rid))
        self.assertFalse(self.store.snooze_reminder(202, rid, 500))
        self.store.retry_reminder(202, rid, 600)
        self.store.mark_delivered(202, rid, None, 700)
        current = self.store.get_reminder(101, rid)
        self.assertEqual(("pending", 100, None), (current["status"], current["due_at"], current["retry_at"]))
        self.assertEqual([], self.store.list_reminders(202))
        self.assertEqual([], self.store.export_data(202)["reminders"])
        self.assertEqual(1, len(self.store.export_data(101)["reminders"]))

    def test_due_owner_filter_precedes_sql_limit(self):
        for index in range(3):
            self.store.add_reminder(101, str(index), 10, "UTC")
        other = self.store.add_reminder(202, "second user", 20, "UTC")
        self.assertEqual(101, self.store.due_reminders(30, limit=1)[0]["owner_id"])
        scoped = self.store.due_reminders(30, limit=1, owner_id=202)
        self.assertEqual([other["id"]], [row["id"] for row in scoped])
        self.assertEqual([], self.store.due_reminders(30, owner_id=303))

    def test_digest_subscribers_join_only_own_enabled_started_settings(self):
        self.store.set_setting(101, "started", "1")
        self.store.set_setting(101, "digest_time", "09:00")
        self.store.set_setting(101, "timezone", "Europe/Moscow")
        self.store.set_setting(202, "started", "1")
        self.store.set_setting(202, "digest_time", "10:30")
        self.store.set_setting(303, "digest_time", "11:00")
        self.store.set_setting(303, "timezone", "Asia/Tokyo")
        self.store.set_setting(404, "started", "1")
        self.store.set_setting(404, "digest_time", "off")
        self.store.set_setting(505, "started", "1")
        expected = [{"owner_id": 101, "timezone": "Europe/Moscow"},
                    {"owner_id": 202, "timezone": None}]
        self.assertEqual(expected, self.store.digest_subscribers())
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(expected, self.store.digest_subscribers())
        self.store.set_setting(101, "digest_time", "off")
        self.assertEqual(expected[1:], self.store.digest_subscribers())

    def test_restart_preserves_all_owners_without_transferring_records(self):
        for owner in (101, 202):
            self.item(owner, text=f"private material {owner}")
            self.store.add_reminder(owner, f"private reminder {owner}", 100, "UTC")
            self.store.set_setting(owner, "started", "1")
        self.store.close()
        self.store = Store(self.path)
        for owner in (101, 202):
            exported = self.store.export_data(owner)
            self.assertEqual([owner], [row["owner_id"] for row in exported["items"]])
            self.assertEqual([owner], [row["owner_id"] for row in exported["reminders"]])
            self.assertEqual(f"private material {owner}", exported["items"][0]["text"])
            self.assertEqual(f"private reminder {owner}", exported["reminders"][0]["text"])
            self.assertEqual("1", exported["settings"]["started"])


if __name__ == "__main__":
    unittest.main()
