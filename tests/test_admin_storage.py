"""Admin directory migration, profile registration and data minimization."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from assistant_bot.storage import Store


class AdminStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "test.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def item(self, owner_id):
        return self.store.add_item(
            owner_id, "private-text", "private-title", "private-category",
            ["private-tag"], file_id="private-file-id", file_name="private-filename",
            urls=["https://private.example/material"])

    def activity(self, user_id):
        # Read only our temporary fixture: activity timestamps are intentionally
        # not part of the admin directory's public return shape.
        row = self.store._conn.execute(
            "SELECT first_seen, last_seen FROM users WHERE user_id = ?", (user_id,)
        ).fetchone()
        return tuple(row)

    def test_legacy_backfill_is_idempotent_and_preserves_unknown_dates(self):
        self.item(101)
        self.store.add_reminder(202, "private-reminder", 123, "UTC")
        self.store.set_setting(303, "started", "1")
        self.store.set_setting(101, "timezone", "UTC")
        self.store.add_reminder(101, "duplicate-owner", 123, "UTC")
        self.store.set_setting(0, "invalid-owner", "1")
        self.store.set_setting(-99, "group-chat", "1")
        # Reproduce a pre-migration database without touching any real data.
        with self.store._conn:
            self.store._conn.execute("DROP TABLE users")
        self.reopen()
        expected = [
            {"user_id": 101, "username": None, "display_name": None, "item_count": 1},
            {"user_id": 202, "username": None, "display_name": None, "item_count": 0},
            {"user_id": 303, "username": None, "display_name": None, "item_count": 0},
        ]
        self.assertEqual(expected, self.store.admin_users())
        self.assertEqual(3, self.store.count_users())
        self.assertEqual((None, None), self.activity(101))
        self.assertEqual((None, None), self.activity(202))
        self.assertEqual((None, None), self.activity(303))
        self.reopen()
        self.assertEqual(expected, self.store.admin_users())
        self.assertEqual(3, self.store.count_users())
        self.assertEqual((None, None), self.activity(101))

    def test_registration_lists_users_without_materials_and_survives_restart(self):
        self.assertEqual(0, self.store.count_users())
        self.assertEqual([], self.store.admin_users())
        with patch("assistant_bot.storage.time.time", return_value=100):
            self.store.record_user(101, "sample", "Example User")
        expected = [{"user_id": 101, "username": "sample", "display_name": "Example User",
                     "item_count": 0}]
        self.assertEqual(expected, self.store.admin_users())
        self.assertEqual((100, 100), self.activity(101))
        self.reopen()
        self.assertEqual(expected, self.store.admin_users())
        self.assertEqual(1, self.store.count_users())

    def test_counts_do_not_multiply_or_return_private_fields(self):
        for user_id in (101, 202, 303):
            self.store.record_user(user_id)
        for _ in range(3):
            self.item(101)
        self.item(202)
        for due_at in (100, 200, 300):
            self.store.add_reminder(101, "private-reminder", due_at, "UTC")
        self.store.set_setting(101, "started", "1")
        self.store.set_setting(101, "timezone", "UTC")
        rows = self.store.admin_users()
        self.assertEqual([3, 1, 0], [row["item_count"] for row in rows])
        for row in rows:
            self.assertEqual({"user_id", "username", "display_name", "item_count"}, set(row))
        self.assertNotIn("private", json.dumps(rows))

    def test_pagination_is_stable_disjoint_and_includes_zero_material_users(self):
        for user_id in (90, 10, 50, 30, 70):
            self.store.record_user(user_id)
        pages = [self.store.admin_users(limit=2, offset=offset) for offset in (0, 2, 4)]
        self.assertEqual([[10, 30], [50, 70], [90]],
                         [[row["user_id"] for row in page] for page in pages])
        self.assertEqual([], self.store.admin_users(limit=2, offset=6))
        self.assertEqual(5, self.store.count_users())
        with self.assertRaises(ValueError):
            self.store.admin_users(limit=0)
        with self.assertRaises(ValueError):
            self.store.admin_users(offset=-1)

    def test_profile_update_clears_removed_username_and_keeps_first_seen(self):
        with patch("assistant_bot.storage.time.time", return_value=100):
            self.store.record_user(101, "old_name", "Old Name")
        with patch("assistant_bot.storage.time.time", return_value=200):
            self.store.record_user(101, "new_name", "New Name")
        self.assertEqual("new_name", self.store.admin_users()[0]["username"])
        self.assertEqual("New Name", self.store.admin_users()[0]["display_name"])
        self.assertEqual((100, 200), self.activity(101))
        with patch("assistant_bot.storage.time.time", return_value=300):
            self.store.record_user(101, None, "No Username")
        self.assertIsNone(self.store.admin_users()[0]["username"])
        self.assertEqual("No Username", self.store.admin_users()[0]["display_name"])
        self.assertEqual((100, 300), self.activity(101))
        self.reopen()
        self.assertEqual((100, 300), self.activity(101))
        self.assertEqual(1, self.store.count_users())

    def test_legacy_users_get_actual_first_interaction_date_on_return(self):
        self.item(101)
        self.reopen()
        self.assertEqual((None, None), self.activity(101))
        with patch("assistant_bot.storage.time.time", return_value=400):
            self.store.record_user(101, "returning", "Returning User")
        self.assertEqual((400, 400), self.activity(101))
        self.reopen()
        self.assertEqual("returning", self.store.admin_users()[0]["username"])
        self.assertEqual((400, 400), self.activity(101))

    def test_registration_rejects_nonpositive_and_noninteger_ids(self):
        for invalid in (0, -1, True, False, "101", 101.5, None):
            with self.subTest(user_id=invalid), self.assertRaises(ValueError):
                self.store.record_user(invalid)
        self.assertEqual(0, self.store.count_users())


if __name__ == "__main__":
    unittest.main()
