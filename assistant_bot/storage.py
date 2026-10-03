"""Persistent, owner-scoped storage for notes, settings and reminders.

All times are Unix seconds in UTC. ``due_at`` is the intended schedule;
``retry_at`` is only a delivery backoff and never changes that schedule.
SQLite writes are transactional; a lock also permits safe use from worker threads.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import unicodedata
from datetime import date
from pathlib import Path
from typing import Any
from .memory_store import MemoryStore, SCHEMA as MEMORY_SCHEMA
from .extraction_store import ExtractionStore, SCHEMA as EXTRACTION_SCHEMA
from .cleanup_store import CleanupStore, SCHEMA as CLEANUP_SCHEMA


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _unique(values: list[str] | None, *, case_sensitive: bool = False) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values or []:
        value = value.strip()
        key = value if case_sensitive else _normalize(value)
        if value and key not in seen:
            seen.add(key)
            result.append(value)
    return result


def _search_text(text: str, title: str, category: str, tags: list[str],
                 urls: list[str], file_name: str | None) -> str:
    return _normalize("\n".join([text, title, category, *tags, *urls, file_name or ""]))


class Store(MemoryStore, ExtractionStore, CleanupStore):
    """A SQLite repository. Always supply the authenticated user's owner_id."""

    def __init__(self, path: str | Path) -> None:
        path = str(path)
        if path != ":memory:":
            Path(path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)
            path = str(Path(path).expanduser())
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(path, timeout=10, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.create_function('extraction_normalize', 1, _normalize, deterministic=True)
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        self._conn.execute("PRAGMA journal_mode = WAL")
        self._conn.execute("PRAGMA synchronous = FULL")
        with self._conn:
            self._conn.executescript("""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    title TEXT NOT NULL,
                    category TEXT NOT NULL,
                    tags TEXT NOT NULL DEFAULT '[]',
                    kind TEXT NOT NULL DEFAULT 'text',
                    file_id TEXT,
                    file_name TEXT,
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    urls TEXT NOT NULL DEFAULT '[]',
                    favorite INTEGER NOT NULL DEFAULT 0 CHECK (favorite IN (0, 1)),
                    search_text TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS items_owner_recent
                    ON items(owner_id, created_at DESC, id DESC);
                CREATE INDEX IF NOT EXISTS items_owner_category
                    ON items(owner_id, category);
                CREATE INDEX IF NOT EXISTS items_owner_favorite
                    ON items(owner_id, favorite);
                CREATE TABLE IF NOT EXISTS reminders (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    due_at INTEGER NOT NULL CHECK (due_at >= 0),
                    timezone TEXT NOT NULL,
                    repeat TEXT,
                    status TEXT NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending', 'sent', 'done', 'cancelled')),
                    retry_at INTEGER CHECK (retry_at IS NULL OR retry_at >= 0),
                    last_delivered_at INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS reminders_owner_status
                    ON reminders(owner_id, status, due_at, id);
                CREATE INDEX IF NOT EXISTS reminders_due
                    ON reminders(due_at, retry_at) WHERE status = 'pending';
                CREATE TABLE IF NOT EXISTS settings (
                    owner_id INTEGER NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    PRIMARY KEY (owner_id, key)
                );
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    display_name TEXT,
                    first_seen INTEGER,
                    last_seen INTEGER
                );
                CREATE TABLE IF NOT EXISTS ai_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
                    role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                    content TEXT NOT NULL,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ai_messages_owner_recent
                    ON ai_messages(owner_id, id DESC);
                CREATE TABLE IF NOT EXISTS ai_usage (
                    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
                    day TEXT NOT NULL,
                    count INTEGER NOT NULL CHECK (count >= 0),
                    PRIMARY KEY (owner_id, day)
                );
                CREATE INDEX IF NOT EXISTS ai_usage_day ON ai_usage(day);
                CREATE TABLE IF NOT EXISTS work_sections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
                    title TEXT NOT NULL,
                    title_key TEXT NOT NULL,
                    position INTEGER NOT NULL CHECK (position >= 0),
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    UNIQUE (id, owner_id),
                    UNIQUE (owner_id, title_key)
                );
                CREATE INDEX IF NOT EXISTS work_sections_owner_order
                    ON work_sections(owner_id, position, id);
                CREATE TABLE IF NOT EXISTS work_materials (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
                    section_id INTEGER NOT NULL,
                    title TEXT NOT NULL,
                    text TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL DEFAULT 'text',
                    file_id TEXT,
                    file_name TEXT,
                    urls TEXT NOT NULL DEFAULT '[]',
                    source_chat_id INTEGER,
                    source_message_id INTEGER,
                    created_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    FOREIGN KEY (section_id, owner_id)
                        REFERENCES work_sections(id, owner_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS work_materials_owner_section
                    ON work_materials(owner_id, section_id, created_at DESC, id DESC);
                CREATE TABLE IF NOT EXISTS tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL CHECK (owner_id > 0),
                    title TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'active' CHECK (status IN ('active','done')),
                    minutes INTEGER NOT NULL DEFAULT 25 CHECK (minutes IN (5,15,25,50)),
                    priority INTEGER NOT NULL DEFAULT 0 CHECK (priority IN (0,1)),
                    source_kind TEXT,
                    source_id INTEGER,
                    focus_reminder_id INTEGER,
                    created_at INTEGER NOT NULL,
                    completed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS tasks_owner_state
                    ON tasks(owner_id,status,priority DESC,id);
                CREATE UNIQUE INDEX IF NOT EXISTS tasks_identity_owner ON tasks(id,owner_id);
                CREATE TABLE IF NOT EXISTS task_checkpoints (
                    task_id INTEGER NOT NULL,
                    owner_id INTEGER NOT NULL,
                    progress TEXT NOT NULL,
                    next_step TEXT NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY(task_id,owner_id),
                    FOREIGN KEY(task_id,owner_id) REFERENCES tasks(id,owner_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS study_cards (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    owner_id INTEGER NOT NULL CHECK(owner_id>0),
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL,
                    source_kind TEXT,
                    source_id INTEGER,
                    stage INTEGER NOT NULL DEFAULT 0,
                    due_at INTEGER NOT NULL,
                    revision INTEGER NOT NULL DEFAULT 0,
                    created_at INTEGER NOT NULL,
                    reviewed_at INTEGER
                );
                CREATE INDEX IF NOT EXISTS study_owner_due ON study_cards(owner_id,due_at,id);
                INSERT OR IGNORE INTO users(user_id)
                    SELECT owner_id FROM items WHERE owner_id > 0
                    UNION
                    SELECT owner_id FROM reminders WHERE owner_id > 0
                    UNION
                    SELECT owner_id FROM settings WHERE owner_id > 0
                    UNION
                    SELECT owner_id FROM work_sections WHERE owner_id > 0;
            """)

        with self._conn:
            self._conn.executescript(MEMORY_SCHEMA)
            self._conn.executescript(EXTRACTION_SCHEMA)
            self._conn.executescript(CLEANUP_SCHEMA)
            self.migrate_extractions()
            self.backfill_memory_attachments()

    def close(self) -> None:
        """Close the connection after the bot and scheduler have stopped."""
        with self._lock:
            self._conn.close()

    @staticmethod
    def _chat_owner(owner_id: int) -> None:
        if (isinstance(owner_id, bool) or not isinstance(owner_id, int)
                or not 1 <= owner_id <= 2**63 - 1):
            raise ValueError("owner_id must be a positive 64-bit integer")

    def chat_history(self, owner_id: int) -> list[dict[str, str]]:
        """Return only this user's latest six conversation turns, oldest first."""
        self._chat_owner(owner_id)
        with self._lock:
            rows = self._conn.execute("""
                SELECT role, content FROM (
                    SELECT id, role, content FROM ai_messages
                    WHERE owner_id = ? ORDER BY id DESC LIMIT 12
                ) ORDER BY id ASC
            """, (owner_id,)).fetchall()
        return [dict(row) for row in rows]

    def append_chat_turn(self, owner_id: int, user_text: str,
                         assistant_text: str) -> None:
        """Save a completed pair and retain six turns for this owner atomically."""
        self._chat_owner(owner_id)
        if not isinstance(user_text, str) or not isinstance(assistant_text, str):
            raise ValueError("chat messages must be text")
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.executemany("""
                INSERT INTO ai_messages(owner_id, role, content, created_at)
                VALUES (?, ?, ?, ?)
            """, ((owner_id, "user", user_text, now),
                  (owner_id, "assistant", assistant_text, now)))
            self._conn.execute("""
                DELETE FROM ai_messages WHERE owner_id = ? AND id NOT IN (
                    SELECT id FROM ai_messages WHERE owner_id = ?
                    ORDER BY id DESC LIMIT 12
                )
            """, (owner_id, owner_id))

    def clear_chat(self, owner_id: int) -> None:
        """Forget this user's conversation; daily request limits remain intact."""
        self._chat_owner(owner_id)
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM ai_messages WHERE owner_id = ?", (owner_id,))

    def reserve_ai_request(self, owner_id: int, day: str, per_user_limit: int,
                           global_limit: int) -> bool:
        """Reserve one request before networking, with persistent daily caps.

        A reservation also counts failed API calls, so errors or restarts cannot
        bypass the budget. BEGIN IMMEDIATE serializes reservations even when
        separate Store connections share this database.
        """
        self._chat_owner(owner_id)
        if not isinstance(day, str):
            raise ValueError("day must be an ISO date (YYYY-MM-DD)")
        try:
            if date.fromisoformat(day).isoformat() != day:
                raise ValueError
        except ValueError:
            raise ValueError("day must be an ISO date (YYYY-MM-DD)") from None
        for limit in (per_user_limit, global_limit):
            if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 10000:
                raise ValueError("daily limits must be integers between 1 and 10000")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            own_count = self._conn.execute(
                "SELECT count FROM ai_usage WHERE owner_id = ? AND day = ?",
                (owner_id, day)).fetchone()
            if own_count is not None and own_count[0] >= per_user_limit:
                return False
            total = self._conn.execute(
                "SELECT COALESCE(SUM(count), 0) FROM ai_usage WHERE day = ?",
                (day,)).fetchone()[0]
            if total >= global_limit:
                return False
            self._conn.execute("""
                INSERT INTO ai_usage(owner_id, day, count) VALUES (?, ?, 1)
                ON CONFLICT(owner_id, day) DO UPDATE SET count = ai_usage.count + 1
            """, (owner_id, day))
            return True

    def record_user(self, user_id: int, username: str | None = None,
                    display_name: str | None = None) -> None:
        """Record an authenticated private-chat interaction and current profile.

        Legacy users keep unknown activity dates until their next interaction.
        A missing username clears its old value if Telegram removed it.
        """
        if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
            raise ValueError("user_id must be a positive integer")
        now = int(time.time())
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO users(user_id, username, display_name, first_seen, last_seen)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = excluded.username,
                    display_name = excluded.display_name,
                    first_seen = COALESCE(users.first_seen, excluded.first_seen),
                    last_seen = excluded.last_seen
            """, (user_id, username, display_name, now, now))

    def count_users(self) -> int:
        """Admin-only registered-user count; callers must authorize the admin."""
        with self._lock:
            return self._conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]

    def admin_users(self, limit: int = 8, offset: int = 0) -> list[dict[str, Any]]:
        """Admin-only profile page and material counts, never private contents.

        Callers must authorize the administrator before querying this method.
        The count subquery cannot multiply items by reminders or settings.
        """
        limit, offset = self._page(limit, offset)
        with self._lock:
            rows = self._conn.execute("""
                SELECT users.user_id, users.username, users.display_name,
                    ((SELECT COUNT(*) FROM items WHERE owner_id = users.user_id)
                    + (SELECT COUNT(*) FROM work_materials WHERE owner_id = users.user_id)) AS item_count
                FROM users ORDER BY users.user_id ASC LIMIT ? OFFSET ?
            """, (limit, offset)).fetchall()
        return [dict(row) for row in rows]

    @staticmethod
    def _item(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        item = dict(row)
        item["tags"] = json.loads(item["tags"])
        item["urls"] = json.loads(item["urls"])
        item["favorite"] = bool(item["favorite"])
        item.pop("search_text", None)
        return item

    @staticmethod
    def _page(limit: int, offset: int) -> tuple[int, int]:
        if not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise ValueError("limit must be an integer between 1 and 1000")
        if not isinstance(offset, int) or offset < 0:
            raise ValueError("offset must be a non-negative integer")
        return limit, offset

    @staticmethod
    def _work_title(title: str, limit: int) -> str:
        if not isinstance(title, str):
            raise ValueError("Название должно быть текстом.")
        title = title.strip()
        if not title or len(title) > limit:
            raise ValueError(f"Название должно содержать от 1 до {limit} символов.")
        if any(unicodedata.category(char).startswith("C") for char in title):
            # Keep labels single-line; allow ZWJ and variation selectors in emoji.
            if any(unicodedata.category(char).startswith("C") and char != "\u200d"
                   for char in title):
                raise ValueError("Название должно быть в одну строку, без скрытых символов.")
        return title

    @staticmethod
    def _work_section(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result.pop("title_key", None)
        return result

    @staticmethod
    def _work_material(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if row is None:
            return None
        result = dict(row)
        result["urls"] = json.loads(result["urls"])
        return result

    def count_work_sections(self, owner_id: int) -> int:
        self._chat_owner(owner_id)
        with self._lock:
            return self._conn.execute(
                "SELECT COUNT(*) FROM work_sections WHERE owner_id = ?",
                (owner_id,)).fetchone()[0]

    def list_work_sections(self, owner_id: int, limit: int = 8,
                           offset: int = 0) -> list[dict[str, Any]]:
        self._chat_owner(owner_id)
        limit, offset = self._page(limit, offset)
        with self._lock:
            rows = self._conn.execute("""
                SELECT sections.*, (SELECT COUNT(*) FROM work_materials
                    WHERE owner_id = sections.owner_id AND section_id = sections.id) AS material_count
                FROM work_sections AS sections WHERE sections.owner_id = ?
                ORDER BY position, id LIMIT ? OFFSET ?
            """, (owner_id, limit, offset)).fetchall()
        return [self._work_section(row) for row in rows]

    def get_work_section(self, owner_id: int, section_id: int) -> dict[str, Any] | None:
        self._chat_owner(owner_id)
        with self._lock:
            row = self._conn.execute("""
                SELECT sections.*, (SELECT COUNT(*) FROM work_materials
                    WHERE owner_id = sections.owner_id AND section_id = sections.id) AS material_count
                FROM work_sections AS sections WHERE sections.owner_id = ? AND sections.id = ?
            """, (owner_id, section_id)).fetchone()
        return self._work_section(row)

    def create_work_section(self, owner_id: int, title: str) -> dict[str, Any]:
        self._chat_owner(owner_id)
        title = self._work_title(title, 60)
        now = int(time.time())
        try:
            with self._lock, self._conn:
                cursor = self._conn.execute("""
                    INSERT INTO work_sections(owner_id, title, title_key, position, created_at, updated_at)
                    SELECT ?, ?, ?, COALESCE(MAX(position), -1) + 1, ?, ?
                    FROM work_sections WHERE owner_id = ?
                """, (owner_id, title, _normalize(title), now, now, owner_id))
                result = self.get_work_section(owner_id, cursor.lastrowid)
        except sqlite3.IntegrityError:
            raise ValueError("У тебя уже есть раздел с таким названием.") from None
        assert result is not None
        return result

    def rename_work_section(self, owner_id: int, section_id: int,
                            title: str) -> dict[str, Any] | None:
        self._chat_owner(owner_id)
        title = self._work_title(title, 60)
        try:
            with self._lock, self._conn:
                self._conn.execute("""
                    UPDATE work_sections SET title = ?, title_key = ?, updated_at = ?
                    WHERE owner_id = ? AND id = ?
                """, (title, _normalize(title), int(time.time()), owner_id, section_id))
                return self.get_work_section(owner_id, section_id)
        except sqlite3.IntegrityError:
            raise ValueError("У тебя уже есть раздел с таким названием.") from None

    def shift_work_section(self, owner_id: int, section_id: int, direction: int) -> bool:
        """Swap adjacent owned sections atomically; at a boundary return False."""
        self._chat_owner(owner_id)
        if isinstance(direction, bool) or direction not in (-1, 1):
            raise ValueError("Направление должно быть -1 или 1.")
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            rows = self._conn.execute("""
                SELECT id, position FROM work_sections WHERE owner_id = ? ORDER BY position, id
            """, (owner_id,)).fetchall()
            index = next((i for i, row in enumerate(rows) if row["id"] == section_id), None)
            if index is None or not 0 <= index + direction < len(rows):
                return False
            current, adjacent = rows[index], rows[index + direction]
            now = int(time.time())
            self._conn.executemany("""
                UPDATE work_sections SET position = ?, updated_at = ? WHERE owner_id = ? AND id = ?
            """, ((adjacent["position"], now, owner_id, current["id"]),
                  (current["position"], now, owner_id, adjacent["id"])))
            return True

    def delete_work_section(self, owner_id: int, section_id: int) -> bool:
        self._chat_owner(owner_id)
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM work_sections WHERE owner_id = ? AND id = ?",
                (owner_id, section_id)).rowcount == 1

    def count_work_materials(self, owner_id: int, section_id: int | None = None) -> int:
        self._chat_owner(owner_id)
        where = "owner_id = ?" + (" AND section_id = ?" if section_id is not None else "")
        params = (owner_id, section_id) if section_id is not None else (owner_id,)
        with self._lock:
            return self._conn.execute(
                f"SELECT COUNT(*) FROM work_materials WHERE {where}", params).fetchone()[0]

    def list_work_materials(self, owner_id: int, section_id: int, limit: int = 8,
                            offset: int = 0) -> list[dict[str, Any]]:
        self._chat_owner(owner_id)
        limit, offset = self._page(limit, offset)
        with self._lock:
            rows = self._conn.execute("""
                SELECT * FROM work_materials WHERE owner_id = ? AND section_id = ?
                ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?
            """, (owner_id, section_id, limit, offset)).fetchall()
        return [self._work_material(row) for row in rows]

    def add_work_material(self, owner_id: int, section_id: int, *, title: str,
                          text: str = "", kind: str = "text", file_id: str | None = None,
                          file_name: str | None = None, urls: list[str] | None = None,
                          source_chat_id: int | None = None,
                          source_message_id: int | None = None) -> dict[str, Any] | None:
        self._chat_owner(owner_id)
        title = self._work_title(title, 100)
        if kind not in ("text", "document", "photo", "voice", "audio", "video", "animation", "video_note"):
            raise ValueError("Этот тип материала не поддерживается.")
        if not isinstance(text, str):
            raise ValueError("Текст материала должен быть строкой.")
        if kind != "text" and (not isinstance(file_id, str) or not file_id.strip()):
            raise ValueError("Пришли файл, который нужно сохранить.")
        urls = _unique(urls, case_sensitive=True)
        if not text.strip() and not file_id and not urls:
            raise ValueError("Пришли текст, ссылку или файл.")
        now = int(time.time())
        with self._lock, self._conn:
            # INSERT...SELECT checks ownership in the same statement as creation;
            # the composite foreign key also protects moves and direct SQL writes.
            cursor = self._conn.execute("""
                INSERT INTO work_materials(owner_id, section_id, title, text, kind, file_id,
                    file_name, urls, source_chat_id, source_message_id, created_at, updated_at)
                SELECT ?, id, ?, ?, ?, ?, ?, ?, ?, ?, ?, ? FROM work_sections
                WHERE owner_id = ? AND id = ?
            """, (owner_id, title, text, kind, file_id, file_name,
                  json.dumps(urls, ensure_ascii=False), source_chat_id, source_message_id,
                  now, now, owner_id, section_id))
            return self.get_work_material(owner_id, cursor.lastrowid) if cursor.rowcount else None

    def get_work_material(self, owner_id: int, material_id: int) -> dict[str, Any] | None:
        self._chat_owner(owner_id)
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM work_materials WHERE owner_id = ? AND id = ?",
                (owner_id, material_id)).fetchone()
        return self._work_material(row)

    def rename_work_material(self, owner_id: int, material_id: int,
                             title: str) -> dict[str, Any] | None:
        self._chat_owner(owner_id)
        title = self._work_title(title, 100)
        with self._lock, self._conn:
            self._conn.execute("""
                UPDATE work_materials SET title = ?, updated_at = ? WHERE owner_id = ? AND id = ?
            """, (title, int(time.time()), owner_id, material_id))
            return self.get_work_material(owner_id, material_id)

    def move_work_material(self, owner_id: int, material_id: int,
                           target_section_id: int) -> dict[str, Any] | None:
        self._chat_owner(owner_id)
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                UPDATE work_materials SET section_id = ?, updated_at = ?
                WHERE owner_id = ? AND id = ? AND EXISTS (
                    SELECT 1 FROM work_sections WHERE owner_id = ? AND id = ?)
            """, (target_section_id, int(time.time()), owner_id, material_id, owner_id, target_section_id))
            return self.get_work_material(owner_id, material_id) if cursor.rowcount else None

    def delete_work_material(self, owner_id: int, material_id: int) -> bool:
        self._chat_owner(owner_id)
        with self._lock, self._conn:
            return self._conn.execute(
                "DELETE FROM work_materials WHERE owner_id = ? AND id = ?",
                (owner_id, material_id)).rowcount == 1

    @staticmethod
    def _filters(owner_id: int, query: str = "", category: str | None = None,
                 favorites: bool = False) -> tuple[str, list[Any]]:
        clauses = ["owner_id = ?"]
        params: list[Any] = [owner_id]
        if category is not None:
            clauses.append("category = ?")
            params.append(category)
        if favorites:
            clauses.append("favorite = 1")
        # Literal substring terms, not SQL wildcards. NFKC + casefold supports
        # Cyrillic and Unicode far beyond SQLite's ASCII-only default LIKE.
        for term in _normalize(query).split():
            clauses.append("instr(search_text || COALESCE((SELECT extraction_normalize(accepted) FROM extractions e WHERE e.owner_id=items.owner_id AND e.source_kind='library' AND e.source_id=items.id), ''), ?) > 0")
            params.append(term)
        return " AND ".join(clauses), params

    def add_item(self, owner_id: int, text: str, title: str, category: str,
                 tags: list[str], kind: str = "text", file_id: str | None = None,
                 file_name: str | None = None, source_chat_id: int | None = None,
                 source_message_id: int | None = None,
                 urls: list[str] | None = None) -> dict[str, Any]:
        """Save a material and its Telegram source/file references."""
        # URL paths and query strings can be case-sensitive.
        tags, urls = _unique(tags), _unique(urls, case_sensitive=True)
        title, category = title.strip(), category.strip()
        now = int(time.time())
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                INSERT INTO items
                    (owner_id, text, title, category, tags, kind, file_id,
                     file_name, source_chat_id, source_message_id, urls,
                     search_text, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (owner_id, text, title, category, json.dumps(tags, ensure_ascii=False),
                  kind, file_id, file_name, source_chat_id, source_message_id,
                  json.dumps(urls, ensure_ascii=False),
                  _search_text(text, title, category, tags, urls, file_name), now, now))
            item = self.get_item(owner_id, cursor.lastrowid)
        assert item is not None
        return item

    def get_item(self, owner_id: int, item_id: int) -> dict[str, Any] | None:
        """Return one material, or None when absent or owned by another user."""
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM items WHERE owner_id = ? AND id = ?",
                (owner_id, item_id)).fetchone()
        return self._item(row)

    def list_items(self, owner_id: int, query: str = "", category: str | None = None,
                   favorites: bool = False, offset: int = 0,
                   limit: int = 6) -> list[dict[str, Any]]:
        """Search all material fields and return a stable page, newest first."""
        limit, offset = self._page(limit, offset)
        where, params = self._filters(owner_id, query, category, favorites)
        with self._lock:
            rows = self._conn.execute(
                f"SELECT * FROM items WHERE {where} ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?",
                [*params, limit, offset]).fetchall()
        return [self._item(row) for row in rows]

    def count_items(self, owner_id: int, query: str = "", category: str | None = None,
                    favorites: bool = False) -> int:
        """Count the exact same owner-scoped filters as list_items."""
        where, params = self._filters(owner_id, query, category, favorites)
        with self._lock:
            return self._conn.execute(f"SELECT COUNT(*) FROM items WHERE {where}", params).fetchone()[0]

    def toggle_favorite(self, owner_id: int, item_id: int) -> dict[str, Any] | None:
        """Atomically toggle the star on an existing owned material."""
        with self._lock, self._conn:
            self._conn.execute(
                "UPDATE items SET favorite = 1 - favorite, updated_at = ? WHERE owner_id = ? AND id = ?",
                (int(time.time()), owner_id, item_id))
            return self.get_item(owner_id, item_id)

    def delete_item(self, owner_id: int, item_id: int) -> bool:
        """Delete only an owned material; report whether it existed."""
        with self._lock, self._conn:
            return self._conn.execute("DELETE FROM items WHERE owner_id = ? AND id = ?",
                                      (owner_id, item_id)).rowcount == 1

    def update_item(self, owner_id: int, item_id: int, title: str,
                    category: str, tags: list[str]) -> dict[str, Any] | None:
        """Edit classification and refresh the searchable representation."""
        with self._lock, self._conn:
            item = self.get_item(owner_id, item_id)
            if item is None:
                return None
            title, category, tags = title.strip(), category.strip(), _unique(tags)
            self._conn.execute("""
                UPDATE items SET title = ?, category = ?, tags = ?, search_text = ?, updated_at = ?
                WHERE owner_id = ? AND id = ?
            """, (title, category, json.dumps(tags, ensure_ascii=False),
                  _search_text(item["text"], title, category, tags, item["urls"], item["file_name"]),
                  int(time.time()), owner_id, item_id))
            return self.get_item(owner_id, item_id)

    def random_item(self, owner_id: int) -> dict[str, Any] | None:
        """Select a random saved material for rediscovery."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM items WHERE owner_id = ? ORDER BY RANDOM() LIMIT 1",
                                     (owner_id,)).fetchone()
        return self._item(row)

    def categories(self, owner_id: int) -> list[tuple[str, int]]:
        """Return categories and material counts, most populated first."""
        with self._lock:
            rows = self._conn.execute("""
                SELECT category, COUNT(*) AS n FROM items WHERE owner_id = ?
                GROUP BY category ORDER BY n DESC, category
            """, (owner_id,)).fetchall()
        return [(row["category"], row["n"]) for row in rows]

    def add_reminder(self, owner_id: int, text: str, due_at: int, timezone: str,
                     repeat: str | None = None) -> dict[str, Any]:
        """Create a scheduled reminder; recurrence names are scheduler-defined."""
        now = int(time.time())
        with self._lock, self._conn:
            cursor = self._conn.execute("""
                INSERT INTO reminders(owner_id, text, due_at, timezone, repeat, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
            """, (owner_id, text, due_at, timezone, repeat or None, now, now))
            reminder = self.get_reminder(owner_id, cursor.lastrowid)
        assert reminder is not None
        return reminder

    def get_reminder(self, owner_id: int, id: int) -> dict[str, Any] | None:
        """Read one reminder with an explicit ownership check."""
        with self._lock:
            row = self._conn.execute("SELECT * FROM reminders WHERE owner_id = ? AND id = ?",
                                     (owner_id, id)).fetchone()
        return dict(row) if row else None

    def list_reminders(self, owner_id: int, limit: int = 20,
                       offset: int = 0) -> list[dict[str, Any]]:
        """List only pending reminders, by intended due time."""
        limit, offset = self._page(limit, offset)
        with self._lock:
            rows = self._conn.execute("""
                SELECT * FROM reminders WHERE owner_id = ? AND status = 'pending'
                ORDER BY due_at, id LIMIT ? OFFSET ?
            """, (owner_id, limit, offset)).fetchall()
        return [dict(row) for row in rows]

    def due_reminders(self, now: int, limit: int = 20,
                      owner_id: int | None = None) -> list[dict[str, Any]]:
        """Scheduler-only due queue, optionally owner-filtered before pagination.

        Public handlers must use owner-scoped list_reminders instead. Delivery
        backoff remains independent from the original due_at schedule.
        """
        self._page(limit, 0)
        owner_clause = " AND owner_id = ?" if owner_id is not None else ""
        params = [now, now]
        if owner_id is not None:
            params.append(owner_id)
        params.append(limit)
        with self._lock:
            rows = self._conn.execute(f"""
                SELECT * FROM reminders WHERE status = 'pending' AND due_at <= ?
                    AND (retry_at IS NULL OR retry_at <= ?)
                    {owner_clause}
                ORDER BY due_at, id LIMIT ?
            """, params).fetchall()
        return [dict(row) for row in rows]

    def digest_subscribers(self) -> list[dict[str, Any]]:
        """Internal scheduler discovery; never expose this cross-user list in chat.

        Return only users who started the bot and enabled their daily digest.
        A missing personal timezone stays None so the scheduler can use its
        configured default. Every settings join is scoped by the same owner.
        """
        with self._lock:
            rows = self._conn.execute("""
                SELECT digest.owner_id AS owner_id, zone.value AS timezone
                FROM settings AS digest
                JOIN settings AS started ON started.owner_id = digest.owner_id
                    AND started.key = 'started' AND started.value = '1'
                LEFT JOIN settings AS zone ON zone.owner_id = digest.owner_id
                    AND zone.key = 'timezone'
                WHERE digest.key = 'digest_time'
                    AND digest.value NOT IN ('off', '')
                ORDER BY digest.owner_id
            """).fetchall()
        return [dict(row) for row in rows]

    def mark_delivered(self, owner_id: int, id: int, next_due: int | None,
                       now: int) -> None:
        """Mark successful delivery, or advance a recurring reminder atomically."""
        with self._lock, self._conn:
            self._conn.execute("""
                UPDATE reminders SET status = ?, due_at = COALESCE(?, due_at),
                    retry_at = NULL, last_delivered_at = ?, updated_at = ?
                WHERE owner_id = ? AND id = ? AND status = 'pending'
            """, ("pending" if next_due is not None else "sent", next_due, now, now, owner_id, id))

    def retry_reminder(self, owner_id: int, id: int, retry_at: int) -> None:
        """Back off a failed delivery while keeping its original schedule."""
        with self._lock, self._conn:
            self._conn.execute("""
                UPDATE reminders SET retry_at = ?, updated_at = ?
                WHERE owner_id = ? AND id = ? AND status = 'pending'
            """, (retry_at, int(time.time()), owner_id, id))

    def complete_reminder(self, owner_id: int, id: int) -> bool:
        """Mark a pending or delivered reminder done, stopping its recurrence."""
        with self._lock, self._conn:
            return self._conn.execute("""
                UPDATE reminders SET status = 'done', retry_at = NULL, updated_at = ?
                WHERE owner_id = ? AND id = ? AND status IN ('pending', 'sent')
            """, (int(time.time()), owner_id, id)).rowcount == 1

    def snooze_reminder(self, owner_id: int, id: int, due_at: int) -> bool:
        """Reschedule pending/sent reminders; completed/cancelled ones stay closed."""
        with self._lock, self._conn:
            return self._conn.execute("""
                UPDATE reminders SET status = 'pending', due_at = ?, retry_at = NULL, updated_at = ?
                WHERE owner_id = ? AND id = ? AND status IN ('pending', 'sent')
            """, (due_at, int(time.time()), owner_id, id)).rowcount == 1

    def cancel_reminder(self, owner_id: int, id: int) -> bool:
        """Cancel an active/delivered reminder without removing its history."""
        with self._lock, self._conn:
            return self._conn.execute("""
                UPDATE reminders SET status = 'cancelled', retry_at = NULL, updated_at = ?
                WHERE owner_id = ? AND id = ? AND status IN ('pending', 'sent')
            """, (int(time.time()), owner_id, id)).rowcount == 1

    def stats(self, owner_id: int) -> dict[str, int]:
        """Counts for the owner's dashboard; completed means delivered or done."""
        with self._lock:
            items = self._conn.execute("""
                SELECT COUNT(*) AS items, COALESCE(SUM(favorite), 0) AS favorites
                FROM items WHERE owner_id = ?
            """, (owner_id,)).fetchone()
            reminders = self._conn.execute("""
                SELECT COALESCE(SUM(status = 'pending'), 0) AS pending,
                    COALESCE(SUM(status IN ('sent', 'done')), 0) AS completed
                FROM reminders WHERE owner_id = ?
            """, (owner_id,)).fetchone()
        return {**dict(items), **dict(reminders)}

    def add_tasks(self, owner_id: int, titles: list[str], *, source_kind=None, source_id=None):
        """Save one confirmed batch atomically; source references must be owned."""
        self._chat_owner(owner_id)
        if not 1 <= len(titles) <= 12:
            raise ValueError("Добавь от 1 до 12 дел за раз.")
        titles = [self._work_title(title, 300) for title in titles]
        with self._lock, self._conn:
            if source_kind is not None:
                source = (self.get_memory_entry(owner_id,source_id) if source_kind=='memory' else self.get_item(owner_id, source_id) if source_kind == 'library'
                          else self.get_work_material(owner_id, source_id) if source_kind == 'work' else None)
                if not source:
                    raise ValueError("Исходный материал недоступен.")
            elif source_id is not None:
                raise ValueError("Не указан источник материала.")
            ids = []
            for title in titles:
                cursor = self._conn.execute('''INSERT INTO tasks
                    (owner_id,title,source_kind,source_id,created_at) VALUES (?,?,?,?,?)''',
                    (owner_id,title,source_kind,source_id,int(time.time())))
                ids.append(cursor.lastrowid)
        return [self.get_task(owner_id, ident) for ident in ids]

    def get_task(self, owner_id, task_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE owner_id=? AND id=?", (owner_id,task_id)).fetchone()
        return dict(row) if row else None

    def list_tasks(self, owner_id, *, status='active', limit=8, offset=0, minutes=None):
        limit, offset = self._page(limit, offset)
        with self._lock:
            rows = self._conn.execute('''SELECT * FROM tasks WHERE owner_id=? AND status=?
                AND (? IS NULL OR minutes<=?) ORDER BY priority DESC,id LIMIT ? OFFSET ?''',
                (owner_id,status,minutes,minutes,limit,offset)).fetchall()
        return [dict(row) for row in rows]

    def task_counts(self, owner_id):
        with self._lock:
            row = self._conn.execute('''SELECT COALESCE(SUM(status='active'),0) AS active,
                COALESCE(SUM(status='done'),0) AS done FROM tasks WHERE owner_id=?''', (owner_id,)).fetchone()
        return dict(row)

    def edit_task(self, owner_id, task_id, *, title=None, minutes=None, priority=None, done=None):
        if title is not None:
            title = self._work_title(title, 300)
        if minutes is not None and minutes not in (5,15,25,50):
            raise ValueError("Выбери 5, 15, 25 или 50 минут.")
        with self._lock, self._conn:
            task = self.get_task(owner_id, task_id)
            if not task:
                return None
            if priority and task['status'] == 'active':
                self._conn.execute("UPDATE tasks SET priority=0 WHERE owner_id=?", (owner_id,))
            changes = {}
            if title is not None:
                changes['title'] = title
            if minutes is not None:
                changes['minutes'] = minutes
            if priority is not None:
                changes['priority'] = int(bool(priority) and task['status'] == 'active')
            if done is not None:
                changes.update(status='done' if done else 'active',
                               completed_at=int(time.time()) if done else None, priority=0)
                if done and task['focus_reminder_id']:
                    self._conn.execute("UPDATE reminders SET status='cancelled', retry_at=NULL WHERE owner_id=? AND id=? AND status='pending'",
                                       (owner_id, task['focus_reminder_id']))
            if changes:
                assignments = ','.join(f'{key}=?' for key in changes)
                self._conn.execute(f'UPDATE tasks SET {assignments} WHERE owner_id=? AND id=?',
                                   (*changes.values(), owner_id,task_id))
            return self.get_task(owner_id,task_id)

    def start_task_focus(self, owner_id, task_id, minutes, timezone):
        """Only one running focus per owner; timer creation and linking are atomic."""
        if minutes not in (5,15,25,50):
            raise ValueError("Неверная длительность фокуса.")
        now = int(time.time())
        with self._lock, self._conn:
            task = self.get_task(owner_id,task_id)
            if not task or task['status'] != 'active':
                return None
            running = self.active_focus(owner_id, now)
            if running:
                raise ValueError("У тебя уже идёт фокус-сессия. Заверши её или дождись таймера.")
            # Expired but undelivered timers must not interrupt the new session.
            self._conn.execute('''UPDATE reminders SET status='cancelled',retry_at=NULL
                WHERE owner_id=? AND status='pending' AND id IN
                (SELECT focus_reminder_id FROM tasks WHERE owner_id=?)''', (owner_id,owner_id))
            cursor = self._conn.execute('''INSERT INTO reminders
                (owner_id,text,due_at,timezone,created_at,updated_at) VALUES (?,?,?,?,?,?)''',
                (owner_id, f"Фокус завершён: {task['title']}. Отметь результат в /plan.",
                 now + minutes * 60, timezone, now, now))
            self._conn.execute('UPDATE tasks SET focus_reminder_id=? WHERE owner_id=? AND id=?',
                               (cursor.lastrowid,owner_id,task_id))
            return self.get_task(owner_id,task_id)

    def active_focus(self, owner_id, now=None):
        with self._lock:
            row = self._conn.execute('''SELECT t.*,r.due_at FROM tasks t JOIN reminders r
                ON r.id=t.focus_reminder_id AND r.owner_id=t.owner_id
                WHERE t.owner_id=? AND t.status='active' AND r.status='pending' AND r.due_at>?
                ORDER BY r.due_at LIMIT 1''', (owner_id,int(time.time()) if now is None else now)).fetchone()
        return dict(row) if row else None

    def task_for_reminder(self, owner_id, reminder_id):
        with self._lock:
            row = self._conn.execute('SELECT * FROM tasks WHERE owner_id=? AND focus_reminder_id=?',
                                     (owner_id,reminder_id)).fetchone()
        return dict(row) if row else None

    def get_checkpoint(self, owner_id, task_id):
        with self._lock:
            row = self._conn.execute('SELECT * FROM task_checkpoints WHERE owner_id=? AND task_id=?',
                                     (owner_id,task_id)).fetchone()
        return dict(row) if row else None

    def save_checkpoint(self, owner_id, task_id, progress, next_step):
        progress = self._work_title(progress, 800)
        next_step = self._work_title(next_step, 300)
        with self._lock, self._conn:
            task = self.get_task(owner_id,task_id)
            if not task or task['status'] != 'active':
                return None
            self._conn.execute('''INSERT INTO task_checkpoints(task_id,owner_id,progress,next_step,updated_at)
                VALUES (?,?,?,?,?) ON CONFLICT(task_id,owner_id) DO UPDATE SET
                progress=excluded.progress,next_step=excluded.next_step,updated_at=excluded.updated_at''',
                (task_id,owner_id,progress,next_step,int(time.time())))
            if task['focus_reminder_id']:
                self._conn.execute("UPDATE reminders SET status='cancelled',retry_at=NULL WHERE owner_id=? AND id=? AND status='pending'",
                                   (owner_id,task['focus_reminder_id']))
            return self.get_checkpoint(owner_id,task_id)

    def recent_checkpoint(self, owner_id):
        with self._lock:
            row = self._conn.execute('''SELECT c.*,t.title FROM task_checkpoints c JOIN tasks t
                ON t.id=c.task_id AND t.owner_id=c.owner_id WHERE c.owner_id=? AND t.status='active'
                ORDER BY c.updated_at DESC,c.task_id DESC LIMIT 1''', (owner_id,)).fetchone()
        return dict(row) if row else None

    def create_study_card(self, owner_id, question, answer, source_kind=None, source_id=None):
        self._chat_owner(owner_id)
        question = self._work_title(question,300)
        answer = self._work_title(answer,1000)
        with self._lock,self._conn:
            if source_kind is not None:
                source = (self.get_item(owner_id,source_id) if source_kind=='library' else
                          self.get_work_material(owner_id,source_id) if source_kind=='work' else None)
                if not source:
                    raise ValueError('Материал недоступен.')
            elif source_id is not None:
                raise ValueError('Не указан тип материала.')
            now=int(time.time())
            cursor=self._conn.execute('''INSERT INTO study_cards
                (owner_id,question,answer,source_kind,source_id,due_at,created_at) VALUES (?,?,?,?,?,?,?)''',
                (owner_id,question,answer,source_kind,source_id,now,now))
            return self.get_study_card(owner_id,cursor.lastrowid)

    def get_study_card(self, owner_id, ident):
        with self._lock:
            row=self._conn.execute('SELECT * FROM study_cards WHERE owner_id=? AND id=?',(owner_id,ident)).fetchone()
        return dict(row) if row else None

    def study_counts(self, owner_id, now=None):
        with self._lock:
            row=self._conn.execute('''SELECT COUNT(*) AS total,COALESCE(SUM(due_at<=?),0) AS due
                FROM study_cards WHERE owner_id=?''',(int(time.time()) if now is None else now,owner_id)).fetchone()
        return dict(row)

    def list_study_cards(self, owner_id, *, offset=0, limit=8, due_only=False, now=None):
        limit,offset=self._page(limit,offset)
        now=int(time.time()) if now is None else now
        with self._lock:
            rows=self._conn.execute('''SELECT * FROM study_cards WHERE owner_id=? AND (?=0 OR due_at<=?)
                ORDER BY due_at,id LIMIT ? OFFSET ?''',(owner_id,int(due_only),now,limit,offset)).fetchall()
        return [dict(row) for row in rows]

    def review_study_card(self, owner_id, ident, revision, remembered, now=None):
        """Compare-and-swap prevents duplicate ratings and stale-card scheduling."""
        now=int(time.time()) if now is None else now
        with self._lock,self._conn:
            card=self.get_study_card(owner_id,ident)
            if not card or card['revision']!=revision or card['due_at']>now:
                return None
            days=(1,3,7,14,30)
            delay=days[min(card['stage'],len(days)-1)]*86400 if remembered else 600
            cursor=self._conn.execute('''UPDATE study_cards SET stage=?,due_at=?,reviewed_at=?,revision=revision+1
                WHERE owner_id=? AND id=? AND revision=?''',
                (min(card['stage']+1,4) if remembered else 0,now+delay,now,owner_id,ident,revision))
            return self.get_study_card(owner_id,ident) if cursor.rowcount else None

    def delete_study_card(self, owner_id, ident):
        with self._lock,self._conn:
            return self._conn.execute('DELETE FROM study_cards WHERE owner_id=? AND id=?',(owner_id,ident)).rowcount==1

    def edit_study_card(self, owner_id, ident, revision, question, answer):
        question=self._work_title(question,300)
        answer=self._work_title(answer,1000)
        with self._lock,self._conn:
            cursor=self._conn.execute('''UPDATE study_cards SET question=?,answer=?,stage=0,due_at=?,
                reviewed_at=NULL,revision=revision+1 WHERE owner_id=? AND id=? AND revision=?''',
                (question,answer,int(time.time()),owner_id,ident,revision))
            return self.get_study_card(owner_id,ident) if cursor.rowcount else None

    def export_data(self, owner_id: int) -> dict[str, Any]:
        """Export all owned records, including reminder history, as JSON-ready data."""
        with self._lock:
            items = self._conn.execute("SELECT * FROM items WHERE owner_id = ? ORDER BY id",
                                       (owner_id,)).fetchall()
            reminders = self._conn.execute("SELECT * FROM reminders WHERE owner_id = ? ORDER BY id",
                                           (owner_id,)).fetchall()
            settings = self._conn.execute("SELECT key, value FROM settings WHERE owner_id = ? ORDER BY key",
                                          (owner_id,)).fetchall()
            work_sections = self._conn.execute(
                "SELECT * FROM work_sections WHERE owner_id = ? ORDER BY position, id",
                (owner_id,)).fetchall()
            work_materials = self._conn.execute(
                "SELECT * FROM work_materials WHERE owner_id = ? ORDER BY id",
                (owner_id,)).fetchall()
            tasks = self._conn.execute('SELECT * FROM tasks WHERE owner_id=? ORDER BY id', (owner_id,)).fetchall()
            checkpoints=self._conn.execute('SELECT * FROM task_checkpoints WHERE owner_id=? ORDER BY task_id',(owner_id,)).fetchall()
            study=self._conn.execute('SELECT * FROM study_cards WHERE owner_id=? ORDER BY id',(owner_id,)).fetchall()
        return {"version": 7, "owner_id": owner_id, "exported_at": int(time.time()),
                **self.cleanup_export(owner_id),
                **self.extraction_export(owner_id),
                **self.memory_export(owner_id),
                "task_checkpoints": [dict(row) for row in checkpoints],
                "study_cards": [dict(row) for row in study],
                "tasks": [dict(row) for row in tasks],
                "items": [self._item(row) for row in items],
                "reminders": [dict(row) for row in reminders],
                "settings": {row["key"]: row["value"] for row in settings},
                "work_sections": [self._work_section(row) for row in work_sections],
                "work_materials": [self._work_material(row) for row in work_materials]}

    def set_setting(self, owner_id: int, key: str, value: str) -> None:
        """Create or replace an owner-specific text setting."""
        with self._lock, self._conn:
            self._conn.execute("""
                INSERT INTO settings(owner_id, key, value) VALUES (?, ?, ?)
                ON CONFLICT(owner_id, key) DO UPDATE SET value = excluded.value
            """, (owner_id, key, value))

    def get_setting(self, owner_id: int, key: str,
                    default: str | None = None) -> str | None:
        """Return a text setting or the supplied default."""
        with self._lock:
            row = self._conn.execute("SELECT value FROM settings WHERE owner_id = ? AND key = ?",
                                     (owner_id, key)).fetchone()
        return row["value"] if row else default
