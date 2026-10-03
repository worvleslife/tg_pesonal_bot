"""Owner-scoped recognition drafts, accepted text and immutable revisions."""
import json
import time

SCHEMA = '''
CREATE TABLE IF NOT EXISTS extractions (
 owner_id INTEGER NOT NULL, source_kind TEXT NOT NULL CHECK(source_kind IN ('library','work')),
 source_id INTEGER NOT NULL, revision INTEGER NOT NULL DEFAULT 0,
 status TEXT NOT NULL, candidate TEXT NOT NULL DEFAULT '', accepted TEXT NOT NULL DEFAULT '',
 method TEXT NOT NULL DEFAULT '', notice TEXT NOT NULL DEFAULT '', updated_at INTEGER NOT NULL,
 PRIMARY KEY(owner_id,source_kind,source_id));
CREATE TABLE IF NOT EXISTS extraction_versions (
 owner_id INTEGER NOT NULL, source_kind TEXT NOT NULL, source_id INTEGER NOT NULL,
 revision INTEGER NOT NULL, snapshot TEXT NOT NULL, created_at INTEGER NOT NULL,
 PRIMARY KEY(owner_id,source_kind,source_id,revision),
 FOREIGN KEY(owner_id,source_kind,source_id) REFERENCES extractions(owner_id,source_kind,source_id) ON DELETE CASCADE);
CREATE TRIGGER IF NOT EXISTS extraction_library_delete AFTER DELETE ON items BEGIN
 DELETE FROM extractions WHERE owner_id=OLD.owner_id AND source_kind='library' AND source_id=OLD.id; END;
CREATE TRIGGER IF NOT EXISTS extraction_work_delete AFTER DELETE ON work_materials BEGIN
 DELETE FROM extractions WHERE owner_id=OLD.owner_id AND source_kind='work' AND source_id=OLD.id; END;
'''


class ExtractionStore:
    def migrate_extractions(self):
        columns={r['name'] for r in self._conn.execute('PRAGMA table_info(extractions)')}
        for name in ('source_text','candidate_title','accepted_title'):
            if name not in columns:
                self._conn.execute(f"ALTER TABLE extractions ADD COLUMN {name} TEXT NOT NULL DEFAULT ''")

    def extraction_source(self, owner, kind, ident):
        return self.get_item(owner, ident) if kind == 'library' else self.get_work_material(owner, ident) if kind == 'work' else None

    def extraction(self, owner, kind, ident):
        with self._lock:
            if not self.extraction_source(owner, kind, ident):
                return None
            row = self._conn.execute('SELECT * FROM extractions WHERE owner_id=? AND source_kind=? AND source_id=?',
                                     (owner, kind, ident)).fetchone()
            return dict(row) if row else None

    def start_extraction(self, owner, kind, ident):
        with self._lock, self._conn:
            source = self.extraction_source(owner, kind, ident)
            current = self.extraction(owner, kind, ident)
            if not source or not source.get('file_id') or (current and current['status'] in ('running', 'draft')):
                return None
            self._conn.execute('''INSERT INTO extractions(owner_id,source_kind,source_id,status,updated_at)
                VALUES(?,?,?,'running',?) ON CONFLICT(owner_id,source_kind,source_id)
                DO UPDATE SET status='running',candidate='',notice='',revision=revision+1,updated_at=excluded.updated_at''',
                (owner, kind, ident, int(time.time())))
            return self.extraction(owner, kind, ident)

    def finish_extraction(self, owner, kind, ident, revision, *, text='', method='', notice='', failed=False, source_text='', title=''):
        text = text.strip()[:12000]
        with self._lock, self._conn:
            title=' '.join(title.split())[:100]
            changed = self._conn.execute('''UPDATE extractions SET status=?,candidate=?,method=?,notice=?,updated_at=?,source_text=?,candidate_title=?
                WHERE owner_id=? AND source_kind=? AND source_id=? AND revision=? AND status='running' ''',
                ('failed' if failed or not text else 'draft', text, method[:100], notice[:600], int(time.time()),source_text[:12000] or text,title, owner, kind, ident, revision))
            return self.extraction(owner, kind, ident) if changed.rowcount else None

    def edit_extraction(self, owner, kind, ident, revision, text):
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise ValueError('Текст должен содержать от 1 до 12 000 символов.')
        with self._lock, self._conn:
            changed = self._conn.execute('''UPDATE extractions SET candidate=?,status='draft',revision=revision+1,updated_at=?
                WHERE owner_id=? AND source_kind=? AND source_id=? AND revision=? AND status IN ('draft','accepted')''',
                (text.strip(), int(time.time()), owner, kind, ident, revision))
            return self.extraction(owner, kind, ident) if changed.rowcount else None

    def accept_extraction(self, owner, kind, ident, revision):
        with self._lock, self._conn:
            row = self.extraction(owner, kind, ident)
            if not row or row['revision'] != revision or row['status'] != 'draft' or not row['candidate']:
                return None
            self._conn.execute('''UPDATE extractions SET accepted=candidate,accepted_title=candidate_title,status='accepted',revision=revision+1,updated_at=?
                WHERE owner_id=? AND source_kind=? AND source_id=?''', (int(time.time()), owner, kind, ident))
            row = self.extraction(owner, kind, ident)
            original=self.extraction_source(owner,kind,ident)
            if row['accepted_title']:
                if kind=='library':
                    from .storage import _search_text
                    self._conn.execute('UPDATE items SET title=?,search_text=?,updated_at=? WHERE owner_id=? AND id=?',
                        (row['accepted_title'],_search_text(original['text'],row['accepted_title'],original['category'],original['tags'],original['urls'],original['file_name']),int(time.time()),owner,ident))
                else:
                    self._conn.execute('UPDATE work_materials SET title=?,updated_at=? WHERE owner_id=? AND id=?',
                        (row['accepted_title'],int(time.time()),owner,ident))
            # Only untouched auto-created attachment placeholders are refreshed.
            # User-edited or previously linked notes always keep their own content.
            entries=self._conn.execute('SELECT id FROM memory_entries WHERE owner_id=? AND source_kind=? AND source_id=? AND revision=1', (owner,kind,ident)).fetchall()
            for entry in entries:
                current=self.get_memory_entry(owner,entry['id'])
                if current['payload'].get('attachment_placeholder'):
                    self._conn.execute("UPDATE memory_entries SET body=?,title=?,payload='{}',revision=revision+1,updated_at=? WHERE owner_id=? AND id=? AND revision=1",
                        (row['accepted'],row['accepted_title'] or current['title'],int(time.time()),owner,current['id']))
                    self._memory_snapshot(self.get_memory_entry(owner,current['id']))
            self._conn.execute('INSERT INTO extraction_versions VALUES (?,?,?,?,?,?)',
                (owner, kind, ident, row['revision'], json.dumps(row, ensure_ascii=False), int(time.time())))
            return row

    def title_extraction(self,owner,kind,ident,revision,title):
        title=self._work_title(title,100)
        with self._lock,self._conn:
            return self._conn.execute('''UPDATE extractions SET candidate_title=?,candidate=CASE WHEN status='accepted' THEN accepted ELSE candidate END,
                status='draft',revision=revision+1 WHERE owner_id=? AND source_kind=? AND source_id=? AND revision=? AND status IN ('draft','accepted')''',
                (title,owner,kind,ident,revision)).rowcount==1

    def discard_extraction(self, owner, kind, ident, revision):
        with self._lock, self._conn:
            return self._conn.execute('''UPDATE extractions SET candidate='',candidate_title=accepted_title,status=CASE WHEN accepted='' THEN 'discarded' ELSE 'accepted' END,
                revision=revision+1 WHERE owner_id=? AND source_kind=? AND source_id=? AND revision=? AND status='draft' ''',
                (owner, kind, ident, revision)).rowcount == 1

    def recover_extractions(self):
        with self._lock, self._conn:
            self._conn.execute("UPDATE extractions SET status='failed',notice='Обработка прервана перезапуском. Можно повторить вручную.' WHERE status='running'")

    def material_text(self, owner, kind, ident):
        source = self.extraction_source(owner, kind, ident)
        if not source:
            return ''
        row = self.extraction(owner, kind, ident)
        text = source['text'] or ''
        if row and row['accepted']:
            text += '\n\n[Текст вложения, сохранён после подтверждения]\n' + row['accepted']
        if kind == 'library':
            job = self.inbox_job(owner, ident)
            if job and job['summary']:
                text += '\n\n[Авторазбор ИИ, не проверен пользователем]\n' + job['summary']
            if job and job['raw_text'] and source.get('file_id'):
                text += '\n\n[Извлечённый текст, возможны ошибки]\n' + job['raw_text']
        return text.strip()

    def extraction_export(self, owner):
        with self._lock:
            return {name: [dict(r) for r in self._conn.execute(f'SELECT * FROM {name} WHERE owner_id=?', (owner,))]
                    for name in ('extractions', 'extraction_versions')}
