"""Durable inbox jobs and atomic, reversible task creation."""
import json
import time

SCHEMA = '''
CREATE TABLE IF NOT EXISTS inbox_jobs (
 owner_id INTEGER NOT NULL,item_id INTEGER NOT NULL,status TEXT NOT NULL DEFAULT 'pending',
 destination TEXT NOT NULL DEFAULT 'library',summary TEXT NOT NULL DEFAULT '',
 raw_text TEXT NOT NULL DEFAULT '',error TEXT NOT NULL DEFAULT '',
 snapshot TEXT NOT NULL,created_at INTEGER NOT NULL,PRIMARY KEY(owner_id,item_id));
CREATE TABLE IF NOT EXISTS task_effort (
 owner_id INTEGER NOT NULL,task_id INTEGER NOT NULL,minutes INTEGER NOT NULL CHECK(minutes BETWEEN 1 AND 1440),
 reason TEXT NOT NULL DEFAULT '',ai INTEGER NOT NULL DEFAULT 0,PRIMARY KEY(owner_id,task_id),
 FOREIGN KEY(task_id,owner_id) REFERENCES tasks(id,owner_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS inbox_tasks (
 owner_id INTEGER NOT NULL,item_id INTEGER NOT NULL,task_id INTEGER NOT NULL,snapshot TEXT NOT NULL,
 quote TEXT NOT NULL,PRIMARY KEY(owner_id,item_id,task_id),
 FOREIGN KEY(owner_id,item_id) REFERENCES inbox_jobs(owner_id,item_id) ON DELETE CASCADE);
CREATE TRIGGER IF NOT EXISTS inbox_library_delete AFTER DELETE ON items BEGIN
 DELETE FROM inbox_jobs WHERE owner_id=OLD.owner_id AND item_id=OLD.id; END;
CREATE INDEX IF NOT EXISTS inbox_pending ON inbox_jobs(status,created_at);
'''


class InboxStore:
    def inbox_job(self, owner, ident):
        with self._lock:
            row = self._conn.execute('SELECT * FROM inbox_jobs WHERE owner_id=? AND item_id=?', (owner, ident)).fetchone()
            return dict(row) if row else None

    def queue_inbox(self, owner, ident):
        with self._lock, self._conn:
            item = self.get_item(owner, ident)
            if not item:
                return None
            snapshot = json.dumps([item['title'], item['category'], item['tags']], ensure_ascii=False)
            self._conn.execute('INSERT OR IGNORE INTO inbox_jobs(owner_id,item_id,snapshot,created_at) VALUES(?,?,?,?)',
                               (owner, ident, snapshot, int(time.time())))
            return self.inbox_job(owner, ident)

    def recover_inbox(self):
        # Unknown request outcome must not trigger another paid request on boot.
        with self._lock, self._conn:
            self._conn.execute("UPDATE inbox_jobs SET status='failed',error='Разбор прерван перезапуском. Исходник сохранён; можно повторить.' WHERE status='running'")

    def pending_inbox(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM inbox_jobs WHERE status='pending' ORDER BY created_at,item_id LIMIT 100")]

    def inbox_state(self, owner, ident, status, *, error='', raw_text=None):
        with self._lock, self._conn:
            self._conn.execute('UPDATE inbox_jobs SET status=?,error=?,raw_text=COALESCE(?,raw_text) WHERE owner_id=? AND item_id=?',
                               (status, error[:600], raw_text, owner, ident))

    def with_effort(self, row):
        if row is None:
            return None
        value = dict(row)
        effort = self._conn.execute('SELECT minutes,reason,ai FROM task_effort WHERE owner_id=? AND task_id=?',
                                    (value['owner_id'], value['id'])).fetchone()
        if effort:
            value.update(minutes=effort['minutes'], estimate_reason=effort['reason'], estimate_ai=effort['ai'])
        return value

    def finish_inbox(self, owner, ident, result):
        from .storage import _search_text
        # No nested write helpers: tasks + result commit together, including on failure.
        with self._lock, self._conn:
            item, job = self.get_item(owner, ident), self.inbox_job(owner, ident)
            if not item or not job or job['status'] != 'running':
                return False
            for task in result['tasks']:
                cursor = self._conn.execute('''INSERT INTO tasks(owner_id,title,source_kind,source_id,created_at)
                    VALUES(?,?,'library',?,?)''', (owner, task['title'], ident, int(time.time())))
                task_id = cursor.lastrowid
                self._conn.execute('INSERT INTO task_effort VALUES(?,?,?,?,1)', (owner, task_id, task['minutes'], task['reason']))
                self._conn.execute('INSERT INTO inbox_tasks VALUES(?,?,?,?,?)',
                    (owner, ident, task_id, json.dumps(self.get_task(owner, task_id), ensure_ascii=False), task['quote']))
            # A user rename during processing takes precedence over the model.
            if [item['title'], item['category'], item['tags']] == json.loads(job['snapshot']):
                self._conn.execute('UPDATE items SET title=?,search_text=?,updated_at=? WHERE owner_id=? AND id=?',
                    (result['title'], _search_text(item['text'], result['title'], item['category'], item['tags'], item['urls'], item['file_name']), int(time.time()), owner, ident))
            self._conn.execute("UPDATE inbox_jobs SET status='done',destination=?,summary=?,error='' WHERE owner_id=? AND item_id=?",
                               (result['destination'], result['summary'], owner, ident))
            return True

    def inbox_tasks(self, owner, ident):
        with self._lock:
            return [dict(r) for r in self._conn.execute('SELECT * FROM inbox_tasks WHERE owner_id=? AND item_id=?', (owner, ident))]

    def undo_inbox(self, owner, ident):
        with self._lock, self._conn:
            job = self.inbox_job(owner, ident)
            if not job or job['status'] != 'done':
                return None
            removed, kept = 0, 0
            for link in self.inbox_tasks(owner, ident):
                task = self.get_task(owner, link['task_id'])
                if not task:
                    continue
                if task == json.loads(link['snapshot']) and not self.get_checkpoint(owner, task['id']):
                    self._conn.execute('DELETE FROM tasks WHERE owner_id=? AND id=?', (owner, task['id']))
                    removed += 1
                else:
                    kept += 1
            self._conn.execute("UPDATE inbox_jobs SET destination='library',status='undone' WHERE owner_id=? AND item_id=?", (owner, ident))
            return removed, kept

    def inbox_export(self, owner):
        with self._lock:
            return {name: [dict(r) for r in self._conn.execute(f'SELECT * FROM {name} WHERE owner_id=?', (owner,))]
                    for name in ('inbox_jobs', 'inbox_tasks', 'task_effort')}
