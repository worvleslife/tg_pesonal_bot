"""Conversation isolation and durable daily AI request budgets."""

import sqlite3
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier

from assistant_bot.storage import Store


class AIStoreTests(unittest.TestCase):
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

    def test_history_is_owner_scoped_and_contains_only_role_and_content(self):
        self.assertEqual([], self.store.chat_history(101))
        self.store.append_chat_turn(101, "private question A", "private reply A")
        self.store.append_chat_turn(7267009888, "private question B", "private reply B")
        self.assertEqual([
            {"role": "user", "content": "private question A"},
            {"role": "assistant", "content": "private reply A"},
        ], self.store.chat_history(101))
        self.assertEqual([
            {"role": "user", "content": "private question B"},
            {"role": "assistant", "content": "private reply B"},
        ], self.store.chat_history(7267009888))
        self.assertEqual([], self.store.chat_history(303))

    def test_retention_keeps_six_complete_pairs_and_preserves_other_owner(self):
        self.store.append_chat_turn(202, "unchanged question", "unchanged answer")
        for index in range(9):
            self.store.append_chat_turn(101, f"question {index}", f"answer {index}")
        expected = []
        for index in range(3, 9):
            expected.extend([
                {"role": "user", "content": f"question {index}"},
                {"role": "assistant", "content": f"answer {index}"},
            ])
        self.assertEqual(expected, self.store.chat_history(101))
        self.assertEqual(2, len(self.store.chat_history(202)))
        self.assertEqual(12, self.store._conn.execute(
            "SELECT COUNT(*) FROM ai_messages WHERE owner_id = 101").fetchone()[0])
        self.reopen()
        self.assertEqual(expected, self.store.chat_history(101))

    def test_turn_pair_rolls_back_if_second_insert_fails(self):
        self.store.append_chat_turn(101, "old question", "old answer")
        previous = self.store.chat_history(101)
        with self.store._conn:
            self.store._conn.execute("""
                CREATE TRIGGER reject_reply BEFORE INSERT ON ai_messages
                WHEN NEW.role = 'assistant'
                BEGIN SELECT RAISE(ABORT, 'fixture failure'); END
            """)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.append_chat_turn(101, "failed question", "failed answer")
        self.assertEqual(previous, self.store.chat_history(101))
        self.reopen()
        self.assertEqual(previous, self.store.chat_history(101))

    def test_clear_is_owner_scoped_and_cannot_reset_quota(self):
        for owner_id in (101, 202):
            self.store.append_chat_turn(owner_id, "question", "answer")
        self.assertTrue(self.store.reserve_ai_request(101, "2026-09-29", 1, 10))
        self.store.clear_chat(101)
        self.assertEqual([], self.store.chat_history(101))
        self.assertEqual(2, len(self.store.chat_history(202)))
        self.assertFalse(self.store.reserve_ai_request(101, "2026-09-29", 1, 10))
        self.reopen()
        self.assertEqual([], self.store.chat_history(101))
        self.assertEqual(2, len(self.store.chat_history(202)))
        self.assertFalse(self.store.reserve_ai_request(101, "2026-09-29", 1, 10))

    def test_daily_per_user_and_global_caps_survive_restart(self):
        self.assertTrue(self.store.reserve_ai_request(101, "2026-09-29", 2, 3))
        self.assertTrue(self.store.reserve_ai_request(101, "2026-09-29", 2, 3))
        self.assertFalse(self.store.reserve_ai_request(101, "2026-09-29", 2, 3))
        self.reopen()
        self.assertFalse(self.store.reserve_ai_request(101, "2026-09-29", 2, 3))
        self.assertTrue(self.store.reserve_ai_request(202, "2026-09-29", 2, 3))
        self.assertFalse(self.store.reserve_ai_request(202, "2026-09-29", 2, 3))
        self.assertFalse(self.store.reserve_ai_request(303, "2026-09-29", 2, 3))
        self.assertTrue(self.store.reserve_ai_request(101, "2026-09-30", 2, 3))
        self.assertTrue(self.store.reserve_ai_request(303, "2026-09-30", 2, 3))
        self.assertEqual(3, self.store._conn.execute(
            "SELECT SUM(count) FROM ai_usage WHERE day = '2026-09-29'").fetchone()[0])

    def test_separate_connections_cannot_race_past_global_cap(self):
        connections = [Store(self.path) for _ in range(4)]
        barrier = Barrier(4)

        def reserve(index):
            barrier.wait(timeout=5)
            return sum(connections[index].reserve_ai_request(
                100 + index, "2026-09-29", 20, 7) for _ in range(10))

        try:
            with ThreadPoolExecutor(max_workers=4) as workers:
                outcomes = list(workers.map(reserve, range(4)))
            self.assertEqual(7, sum(outcomes))
            self.assertEqual(7, self.store._conn.execute(
                "SELECT SUM(count) FROM ai_usage").fetchone()[0])
        finally:
            for connection in connections:
                connection.close()

    def test_ai_records_do_not_change_material_or_admin_counts(self):
        self.store.record_user(101, "sample", "User")
        self.store.append_chat_turn(101, "private chat", "private answer")
        self.store.reserve_ai_request(101, "2026-09-29", 2, 3)
        self.assertEqual(0, self.store.count_items(101))
        self.assertEqual([
            {"user_id": 101, "username": "sample", "display_name": "User", "item_count": 0},
        ], self.store.admin_users())

    def test_rejects_invalid_owners_limits_days_and_nontext_messages(self):
        for owner_id in (0, -1, True, False, "101", 1.5, None, 2**63):
            with self.subTest(owner_id=owner_id):
                for method, args in (
                    (self.store.chat_history, ()),
                    (self.store.clear_chat, ()),
                    (self.store.append_chat_turn, ("question", "answer")),
                    (self.store.reserve_ai_request, ("2026-09-29", 2, 3)),
                ):
                    with self.assertRaises(ValueError):
                        method(owner_id, *args)
        for invalid in (0, -1, 10001, True, False, "10", 1.5, None):
            with self.subTest(limit=invalid):
                with self.assertRaises(ValueError):
                    self.store.reserve_ai_request(101, "2026-09-29", invalid, 10)
                with self.assertRaises(ValueError):
                    self.store.reserve_ai_request(101, "2026-09-29", 10, invalid)
        for invalid in ("20260929", "2026-9-29", "2026-02-30", "2026-W40-2",
                        "2026-09-29T00:00:00", "", None, 20260929):
            with self.subTest(day=invalid), self.assertRaises(ValueError):
                self.store.reserve_ai_request(101, invalid, 2, 3)
        with self.assertRaises(ValueError):
            self.store.append_chat_turn(101, None, "answer")
        with self.assertRaises(ValueError):
            self.store.append_chat_turn(101, "question", ["answer"])
        self.assertEqual([], self.store.chat_history(101))
        self.assertEqual(0, self.store._conn.execute("SELECT COUNT(*) FROM ai_usage").fetchone()[0])


if __name__ == "__main__":
    unittest.main()
