"""Durable, owner-scoped message cleanup and local-file receipts."""
import time

SCHEMA='''
CREATE TABLE IF NOT EXISTS chat_receipts (
 owner_id INTEGER NOT NULL,message_id INTEGER NOT NULL,kind TEXT NOT NULL,file_id TEXT,file_name TEXT,
 received_at INTEGER NOT NULL,PRIMARY KEY(owner_id,message_id));
CREATE TABLE IF NOT EXISTS local_files (
 owner_id INTEGER NOT NULL,file_id TEXT NOT NULL,relative_path TEXT NOT NULL DEFAULT '',
 size INTEGER NOT NULL DEFAULT 0,sha256 TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'pending',
 attempts INTEGER NOT NULL DEFAULT 0,retry_at INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(owner_id,file_id));
CREATE TABLE IF NOT EXISTS message_cleanup (
 owner_id INTEGER NOT NULL,message_id INTEGER NOT NULL,reason TEXT NOT NULL,due_at INTEGER NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending',attempts INTEGER NOT NULL DEFAULT 0,error TEXT NOT NULL DEFAULT '',
 PRIMARY KEY(owner_id,message_id));
CREATE INDEX IF NOT EXISTS cleanup_due ON message_cleanup(status,due_at);
'''


class CleanupStore:
    def queue_delete(self,owner,message_id,reason,due_at=None):
        self._chat_owner(owner)
        if not isinstance(message_id,int) or isinstance(message_id,bool) or message_id<=0:
            return
        with self._lock,self._conn:
            self._conn.execute('''INSERT OR IGNORE INTO message_cleanup(owner_id,message_id,reason,due_at)
                VALUES(?,?,?,?)''',(owner,message_id,reason,int(time.time()) if due_at is None else due_at))

    def record_chat_input(self,owner,message_id,kind='text',file_id=None,file_name=None):
        with self._lock,self._conn:
            previous=self.get_setting(owner,'ui_last_user_message','')
            self._conn.execute('INSERT OR IGNORE INTO chat_receipts VALUES(?,?,?,?,?,?)',
                (owner,message_id,kind,file_id,file_name,int(time.time())))
            # Telegram message IDs increase within a chat. Replayed updates must
            # not move the current-input pointer backwards or delete newer input.
            if previous.isdigit() and int(previous)>=message_id:
                return
            self.set_setting(owner,'ui_last_user_message',str(message_id))
            if previous.isdigit():
                receipt=self._conn.execute('SELECT kind FROM chat_receipts WHERE owner_id=? AND message_id=?',(owner,int(previous))).fetchone()
                if receipt and receipt['kind']=='text':
                    self.queue_delete(owner,int(previous),'previous_input')

    def has_file_reference(self,owner,file_id):
        with self._lock:
            return bool(self._conn.execute('''SELECT 1 FROM items WHERE owner_id=? AND file_id=?
                UNION ALL SELECT 1 FROM work_materials WHERE owner_id=? AND file_id=?
                UNION ALL SELECT 1 FROM memory_attachments WHERE owner_id=? AND file_id=? LIMIT 1''',
                (owner,file_id,owner,file_id,owner,file_id)).fetchone())

    def discover_archives(self):
        with self._lock,self._conn:
            self._conn.execute('''INSERT OR IGNORE INTO local_files(owner_id,file_id)
                SELECT r.owner_id,r.file_id FROM chat_receipts r WHERE r.file_id IS NOT NULL AND (
                EXISTS(SELECT 1 FROM items i WHERE i.owner_id=r.owner_id AND i.file_id=r.file_id)
                OR EXISTS(SELECT 1 FROM work_materials w WHERE w.owner_id=r.owner_id AND w.file_id=r.file_id)
                OR EXISTS(SELECT 1 FROM memory_attachments m WHERE m.owner_id=r.owner_id AND m.file_id=r.file_id))''')

    def local_file(self,owner,file_id):
        with self._lock:
            row=self._conn.execute('SELECT * FROM local_files WHERE owner_id=? AND file_id=?',(owner,file_id)).fetchone()
            return dict(row) if row else None

    def archive_jobs(self,now,limit=2):
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM local_files WHERE status IN ('pending','retry') AND retry_at<=? ORDER BY retry_at,attempts,owner_id LIMIT ?",(now,limit))]

    def archive_state(self,owner,file_id,status,*,relative_path='',size=0,sha256='',error=''):
        with self._lock,self._conn:
            self._conn.execute('''UPDATE local_files SET status=?,relative_path=?,size=?,sha256=?,error=?,
                attempts=attempts+CASE WHEN ?='running' THEN 1 ELSE 0 END,retry_at=? WHERE owner_id=? AND file_id=?''',
                (status,relative_path,size,sha256,error[:200],status,int(time.time())+60,owner,file_id))

    def recover_archives(self):
        with self._lock,self._conn:
            self._conn.execute("UPDATE local_files SET status='retry',retry_at=0 WHERE status='running'")

    def archived_receipts(self,limit=20):
        with self._lock:
            return [dict(r) for r in self._conn.execute('''SELECT r.*,l.relative_path,l.size,l.sha256 FROM chat_receipts r
                JOIN local_files l ON l.owner_id=r.owner_id AND l.file_id=r.file_id
                WHERE l.status='ready' AND NOT EXISTS(SELECT 1 FROM message_cleanup d WHERE d.owner_id=r.owner_id AND d.message_id=r.message_id)
                ORDER BY r.received_at LIMIT ?''',(limit,))]

    def cleanup_jobs(self,now,limit=10):
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM message_cleanup WHERE status='pending' AND due_at<=? ORDER BY due_at,owner_id,message_id LIMIT ?",(now,limit))]

    def cleanup_result(self,owner,message_id,status,error='',retry_at=None):
        with self._lock,self._conn:
            self._conn.execute('''UPDATE message_cleanup SET status=?,error=?,attempts=attempts+1,due_at=COALESCE(?,due_at)
                WHERE owner_id=? AND message_id=?''',(status,error[:120],retry_at,owner,message_id))

    def archive_usage(self,owner=None):
        with self._lock:
            return self._conn.execute("SELECT COALESCE(SUM(size),0) FROM local_files WHERE status='ready'" + (' AND owner_id=?' if owner else ''), (owner,) if owner else ()).fetchone()[0]

    def cleanup_export(self,owner):
        with self._lock:
            return {'local_files':[dict(r) for r in self._conn.execute('SELECT * FROM local_files WHERE owner_id=?',(owner,))]}
