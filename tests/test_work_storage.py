"""Work folders: durable ordering, additive migration and ownership boundaries."""

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from assistant_bot.storage import Store


class WorkStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / "work.sqlite3"
        self.store = Store(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def reopen(self):
        self.store.close()
        self.store = Store(self.path)

    def section(self, owner=101, title="Инструкции"):
        return self.store.create_work_section(owner, title)

    def material(self, owner, section, **changes):
        fields = {"title": "Инструкция", "text": "Личный рабочий текст"}
        fields.update(changes)
        return self.store.add_work_material(owner, section["id"], **fields)

    def test_sections_are_owner_scoped_in_all_operations(self):
        first = self.section()
        other = self.section(202)
        self.assertEqual(1, self.store.count_work_sections(101))
        self.assertEqual(0, self.store.count_work_sections(303))
        self.assertEqual([other["id"]], [row["id"] for row in self.store.list_work_sections(202)])
        self.assertIsNone(self.store.get_work_section(202, first["id"]))
        self.assertIsNone(self.store.rename_work_section(202, first["id"], "Взлом"))
        self.assertFalse(self.store.shift_work_section(202, first["id"], 1))
        self.assertFalse(self.store.delete_work_section(202, first["id"]))
        self.assertEqual("Инструкции", self.store.get_work_section(101, first["id"])["title"])

    def test_unicode_duplicates_and_atomic_rename(self):
        first = self.section(title="  Инструкции  ")
        second = self.section(title="Документы")
        self.assertEqual("Инструкции", first["title"])
        with self.assertRaisesRegex(ValueError, "уже есть"):
            self.section(title="ИНСТРУКЦИИ")
        with self.assertRaisesRegex(ValueError, "уже есть"):
            self.store.rename_work_section(101, second["id"], "инструкции")
        self.assertEqual("Документы", self.store.get_work_section(101, second["id"])["title"])
        self.assertEqual("Инструкции", self.store.rename_work_section(101, first["id"], "Инструкции")["title"])
        self.section(title="ＦＯＬＤＥＲ")
        with self.assertRaisesRegex(ValueError, "уже есть"):
            self.section(title="folder")
        self.section(202, "ИНСТРУКЦИИ")
        self.assertEqual(3, self.store.count_work_sections(101))

    def test_title_and_owner_validation(self):
        for title in ("", " \n ", "x" * 61, None, "two\nlines", "zero\x00"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.section(title=title)
        self.assertEqual("x" * 60, self.section(title="x" * 60)["title"])
        self.section(title="👩‍💻 Разработка")
        for owner in (0, -1, True, "101", 2**63):
            with self.subTest(owner=owner), self.assertRaises(ValueError):
                self.store.create_work_section(owner, "Название")
        for direction in (0, 2, True, "1"):
            with self.subTest(direction=direction), self.assertRaises(ValueError):
                self.store.shift_work_section(101, 1, direction)

    def test_ordering_moves_boundaries_and_persistence(self):
        sections = [self.section(title=str(i)) for i in range(4)]
        other = self.section(202)
        ids = [row["id"] for row in sections]
        self.assertFalse(self.store.shift_work_section(101, ids[0], -1))
        self.assertFalse(self.store.shift_work_section(101, ids[-1], 1))
        self.assertTrue(self.store.shift_work_section(101, ids[1], 1))
        self.assertTrue(self.store.shift_work_section(101, ids[-1], -1))
        expected = [ids[0], ids[2], ids[3], ids[1]]
        self.assertEqual(expected, [row["id"] for row in self.store.list_work_sections(101)])
        self.assertEqual(0, self.store.get_work_section(202, other["id"])["position"])
        self.reopen()
        self.assertEqual(expected, [row["id"] for row in self.store.list_work_sections(101)])
        self.assertEqual(expected[:2], [row["id"] for row in self.store.list_work_sections(101, limit=2)])
        self.assertEqual(expected[2:], [row["id"] for row in self.store.list_work_sections(101, limit=2, offset=2)])
        self.assertEqual([], self.store.list_work_sections(101, offset=4))
        self.store.delete_work_section(101, ids[2])
        added = self.section(title="after deletion")
        self.assertEqual(added["id"], self.store.list_work_sections(101)[-1]["id"])

    def test_materials_cannot_be_read_added_or_mutated_by_another_owner(self):
        first, other = self.section(), self.section(202)
        material = self.material(101, first)
        foreign_material = self.material(202, other)
        self.assertIsNone(self.material(202, first))
        self.assertIsNone(self.store.get_work_material(202, material["id"]))
        self.assertIsNone(self.store.rename_work_material(202, material["id"], "Взлом"))
        self.assertFalse(self.store.delete_work_material(202, material["id"]))
        self.assertIsNone(self.store.move_work_material(202, material["id"], other["id"]))
        self.assertIsNone(self.store.move_work_material(101, material["id"], other["id"]))
        self.assertEqual([], self.store.list_work_materials(202, first["id"]))
        self.assertEqual(0, self.store.count_work_materials(202, first["id"]))
        self.assertEqual(1, self.store.count_work_materials(202))
        self.assertEqual(first["id"], self.store.get_work_material(101, material["id"])["section_id"])
        self.assertEqual([foreign_material["id"]], [row["id"] for row in self.store.list_work_materials(202, other["id"])])

    def test_material_title_validation_and_rename_preserve_original_contents(self):
        section = self.section()
        original = self.material(101, section)
        for title in ("", " ", None, "x" * 101, "line\nbreak"):
            with self.subTest(title=title), self.assertRaises(ValueError):
                self.store.rename_work_material(101, original["id"], title)
        renamed = self.store.rename_work_material(101, original["id"], " x " )
        self.assertEqual("x", renamed["title"])
        self.assertEqual(original["text"], renamed["text"])
        self.assertEqual("x" * 100, self.material(101, section, title="x" * 100)["title"])

    def test_media_roundtrip_with_urls_source_and_caption(self):
        section = self.section()
        expected = []
        for kind in ("text", "document", "photo", "voice", "audio", "video", "animation", "video_note"):
            file_id = None if kind == "text" else f"telegram-{kind}"
            material = self.material(101, section, kind=kind, file_id=file_id,
                                     file_name=f"name-{kind}.bin", text="Подпись <текст>",
                                     urls=["https://example.com/A", "https://example.com/a", "https://example.com/A"],
                                     source_chat_id=101, source_message_id=99)
            self.assertEqual(["https://example.com/A", "https://example.com/a"], material["urls"])
            expected.append(material)
        self.reopen()
        for saved in expected:
            self.assertEqual(saved, self.store.get_work_material(101, saved["id"]))
        self.assertEqual(8, self.store.get_work_section(101, section["id"])["material_count"])
        self.assertEqual([row["id"] for row in expected][::-1],
                         [row["id"] for row in self.store.list_work_materials(101, section["id"])])

    def test_invalid_materials_are_rejected_before_saving(self):
        section = self.section()
        for fields in ({"kind": "unknown"}, {"kind": "photo", "file_id": None},
                       {"kind": "voice", "file_id": " "}, {"text": None}, {"text": " "}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.material(101, section, **fields)
        self.assertEqual(0, self.store.count_work_materials(101))
        self.assertIsNotNone(self.material(101, section, text="", urls=["https://example.com"]))
        self.assertIsNotNone(self.material(101, section, text="", kind="photo", file_id="file"))

    def test_move_material_updates_counts_and_delete_is_idempotent(self):
        source = self.section()
        target = self.section(title="Документы")
        material = self.material(101, source)
        moved = self.store.move_work_material(101, material["id"], target["id"])
        self.assertEqual(target["id"], moved["section_id"])
        self.assertEqual(0, self.store.count_work_materials(101, source["id"]))
        self.assertEqual(1, self.store.count_work_materials(101, target["id"]))
        self.assertEqual([0, 1], [row["material_count"] for row in self.store.list_work_sections(101)])
        self.assertIsNone(self.store.move_work_material(101, material["id"], 999999))
        self.assertEqual(target["id"], self.store.get_work_material(101, material["id"])["section_id"])
        self.assertTrue(self.store.delete_work_material(101, material["id"]))
        self.assertFalse(self.store.delete_work_material(101, material["id"]))

    def test_section_cascade_keeps_other_sections_and_users(self):
        first, second, other = self.section(), self.section(title="Второй"), self.section(202)
        removed = self.material(101, first)
        kept = self.material(101, second)
        foreign = self.material(202, other)
        self.assertFalse(self.store.delete_work_section(202, first["id"]))
        self.assertTrue(self.store.delete_work_section(101, first["id"]))
        self.assertFalse(self.store.delete_work_section(101, first["id"]))
        self.assertIsNone(self.store.get_work_material(101, removed["id"]))
        self.assertEqual(kept, self.store.get_work_material(101, kept["id"]))
        self.assertEqual(foreign, self.store.get_work_material(202, foreign["id"]))
        self.assertIsNone(self.material(101, first))

    def test_composite_foreign_key_rejects_direct_cross_owner_write(self):
        section = self.section()
        material = self.material(101, section)
        foreign = self.section(202)
        with self.assertRaises(sqlite3.IntegrityError), self.store._conn:
            self.store._conn.execute("UPDATE work_materials SET section_id = ? WHERE id = ?",
                                     (foreign["id"], material["id"]))
        with self.assertRaises(sqlite3.IntegrityError), self.store._conn:
            self.store._conn.execute("UPDATE work_materials SET owner_id = ? WHERE id = ?",
                                     (202, material["id"]))
        self.assertEqual(material, self.store.get_work_material(101, material["id"]))

    def test_export_has_only_owned_work_content_and_admin_has_only_counts(self):
        for owner in (101, 202):
            self.store.record_user(owner)
            section = self.section(owner, f"private-folder-{owner}")
            self.material(owner, section, text=f"private-content-{owner}")
        self.store.add_item(101, "text", "title", "category", [])
        exported = self.store.export_data(101)
        self.assertEqual(7, exported["version"])
        self.assertEqual([101], [row["owner_id"] for row in exported["work_sections"]])
        self.assertEqual([101], [row["owner_id"] for row in exported["work_materials"]])
        self.assertNotIn("private-content-202", json.dumps(exported))
        self.assertNotIn("private-folder-202", json.dumps(exported))
        rows = self.store.admin_users()
        self.assertEqual([2, 1], [row["item_count"] for row in rows])
        self.assertNotIn("private", json.dumps(rows))
        self.assertEqual({"user_id", "username", "display_name", "item_count"}, set(rows[0]))

    def test_additive_migration_preserves_existing_materials_and_is_idempotent(self):
        item = self.store.add_item(101, "original", "Title", "Category", [])
        reminder = self.store.add_reminder(101, "Reminder", 12345, "Europe/Moscow")
        self.store.set_setting(101, "timezone", "Europe/Moscow")
        with self.store._conn:
            self.store._conn.execute("DROP TABLE work_materials")
            self.store._conn.execute("DROP TABLE work_sections")
        self.reopen()
        self.assertEqual(item, self.store.get_item(101, item["id"]))
        self.assertEqual(reminder, self.store.get_reminder(101, reminder["id"]))
        self.assertEqual("Europe/Moscow", self.store.get_setting(101, "timezone"))
        section = self.section()
        material = self.material(101, section)
        self.reopen()
        self.assertEqual(1, self.store.count_work_sections(101))
        self.assertEqual(material, self.store.get_work_material(101, material["id"]))

    def test_material_pagination_and_missing_ids(self):
        section = self.section()
        rows = [self.material(101, section, title=str(index)) for index in range(5)]
        ids = [row["id"] for row in rows][::-1]
        self.assertEqual(ids[2:4], [row["id"] for row in self.store.list_work_materials(101, section["id"], limit=2, offset=2)])
        self.assertEqual([], self.store.list_work_materials(101, section["id"], offset=5))
        self.assertIsNone(self.store.get_work_material(101, 99999))
        self.assertIsNone(self.store.rename_work_material(101, 99999, "New title"))
        self.assertIsNone(self.store.move_work_material(101, 99999, section["id"]))
        self.assertFalse(self.store.delete_work_material(101, 99999))
        for operation in (lambda: self.store.list_work_sections(101, limit=0),
                          lambda: self.store.list_work_sections(101, offset=-1),
                          lambda: self.store.list_work_materials(101, section["id"], limit=0),
                          lambda: self.store.list_work_materials(101, section["id"], offset=-1)):
            with self.assertRaises(ValueError):
                operation()


if __name__ == "__main__":
    unittest.main()
