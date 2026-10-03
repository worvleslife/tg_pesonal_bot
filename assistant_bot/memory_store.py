"""Owner-scoped project memory, provenance, and immutable revision snapshots."""
import json
import re
import time
import unicodedata

KINDS={'observation':'Наблюдение','instruction':'Из инструкции','decision':'Решение',
       'idea':'Идея','question':'Открытый вопрос','case':'Случай','result':'Результат',
       'assumption':'Предположение','material':'Материал'}

SCHEMA='''
CREATE TABLE IF NOT EXISTS memory_projects (
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,title TEXT NOT NULL,
 goal TEXT NOT NULL,created_at INTEGER NOT NULL,UNIQUE(id,owner_id));
CREATE INDEX IF NOT EXISTS memory_projects_owner ON memory_projects(owner_id,id);
CREATE TABLE IF NOT EXISTS memory_entries (
 id INTEGER PRIMARY KEY AUTOINCREMENT,owner_id INTEGER NOT NULL,project_id INTEGER NOT NULL,
 kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,payload TEXT NOT NULL DEFAULT '{}',
 source_kind TEXT,source_id INTEGER,revision INTEGER NOT NULL DEFAULT 1,
 created_at INTEGER NOT NULL,updated_at INTEGER NOT NULL,UNIQUE(id,owner_id),
 FOREIGN KEY(project_id,owner_id) REFERENCES memory_projects(id,owner_id) ON DELETE CASCADE);
CREATE INDEX IF NOT EXISTS memory_entries_project ON memory_entries(owner_id,project_id,id);
CREATE UNIQUE INDEX IF NOT EXISTS memory_source_unique
 ON memory_entries(owner_id,project_id,source_kind,source_id) WHERE source_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS memory_versions (
 entry_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,revision INTEGER NOT NULL,
 snapshot TEXT NOT NULL,recorded_at INTEGER NOT NULL,PRIMARY KEY(entry_id,owner_id,revision),
 FOREIGN KEY(entry_id,owner_id) REFERENCES memory_entries(id,owner_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS memory_task_links (
 owner_id INTEGER NOT NULL,project_id INTEGER NOT NULL,task_id INTEGER NOT NULL,
 PRIMARY KEY(owner_id,project_id,task_id),
 FOREIGN KEY(project_id,owner_id) REFERENCES memory_projects(id,owner_id) ON DELETE CASCADE,
 FOREIGN KEY(task_id,owner_id) REFERENCES tasks(id,owner_id) ON DELETE CASCADE);
CREATE TABLE IF NOT EXISTS memory_attachments (
 entry_id INTEGER NOT NULL,owner_id INTEGER NOT NULL,kind TEXT NOT NULL,file_id TEXT NOT NULL,file_name TEXT,
 PRIMARY KEY(entry_id,owner_id),
 FOREIGN KEY(entry_id,owner_id) REFERENCES memory_entries(id,owner_id) ON DELETE CASCADE);
'''


def normalized(text):
    return unicodedata.normalize('NFKC',text).casefold().replace('ё','е')


def query_terms(query):
    stop=set('где когда как что мы я ты это та тот те уже мне меня мои мой моя было был была которые которую присылала присылал найти найди покажи пожалуйста про для или при перед после снова'.split())
    terms=[]
    for word in re.findall(r'[\w]+',normalized(query)):
        if word in stop or len(word)<3:
            continue
        if re.fullmatch('[а-я]+',word) and len(word)>5:
            word=re.sub(r'(иями|ями|ами|ого|ему|ому|ах|ях|ой|ей|ую|юю|ая|яя|ые|ие|ов|ев|а|я|ы|и|у|ю|е|о)$','',word)
        if word not in terms:
            terms.append(word)
    return terms[:10]


class MemoryStore:
    def get_project(self,owner,ident):
        with self._lock:
            row=self._conn.execute('SELECT * FROM memory_projects WHERE owner_id=? AND id=?',(owner,ident)).fetchone()
        return dict(row) if row else None

    def project_count(self,owner):
        with self._lock:
            return self._conn.execute('SELECT COUNT(*) FROM memory_projects WHERE owner_id=?',(owner,)).fetchone()[0]

    def list_projects(self,owner,offset=0,limit=8):
        self._page(limit,offset)
        with self._lock:
            rows=self._conn.execute('SELECT * FROM memory_projects WHERE owner_id=? ORDER BY id DESC LIMIT ? OFFSET ?',
                                    (owner,limit,offset)).fetchall()
        return [dict(r) for r in rows]

    def create_project(self,owner,title,goal):
        self._chat_owner(owner)
        title=self._work_title(title,80)
        goal=self._work_title(goal,500)
        with self._lock,self._conn:
            cursor=self._conn.execute('INSERT INTO memory_projects(owner_id,title,goal,created_at) VALUES(?,?,?,?)',
                                      (owner,title,goal,int(time.time())))
            return self.get_project(owner,cursor.lastrowid)

    def memory_source(self,owner,kind,ident):
        source = self.extraction_source(owner,kind,ident)
        return dict(source,text=self.material_text(owner,kind,ident)) if source else None

    def get_memory_entry(self,owner,ident):
        with self._lock:
            row=self._conn.execute('SELECT * FROM memory_entries WHERE owner_id=? AND id=?',(owner,ident)).fetchone()
        if not row:
            return None
        entry=dict(row)
        entry['payload']=json.loads(entry['payload'])
        return entry

    def memory_entries(self,owner,project_id,offset=0,limit=8):
        self._page(limit,offset)
        with self._lock:
            rows=self._conn.execute('SELECT id FROM memory_entries WHERE owner_id=? AND project_id=? ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?',
                                    (owner,project_id,limit,offset)).fetchall()
        return [self.get_memory_entry(owner,row['id']) for row in rows]

    def memory_count(self,owner,project_id):
        with self._lock:
            return self._conn.execute('SELECT COUNT(*) FROM memory_entries WHERE owner_id=? AND project_id=?',(owner,project_id)).fetchone()[0]

    def add_memory_entry(self,owner,project_id,kind,title,body,*,payload=None,source_kind=None,source_id=None):
        if kind not in KINDS:
            raise ValueError('Неизвестный тип записи.')
        title=self._work_title(title,100)
        if not isinstance(body,str) or not body.strip() or len(body)>12000:
            raise ValueError('Текст записи должен быть от 1 до 12 000 символов.')
        with self._lock,self._conn:
            if not self.get_project(owner,project_id):
                return None
            if source_kind is not None:
                if not self.memory_source(owner,source_kind,source_id):
                    return None
                existing=self._conn.execute('SELECT id FROM memory_entries WHERE owner_id=? AND project_id=? AND source_kind=? AND source_id=?',
                                             (owner,project_id,source_kind,source_id)).fetchone()
                if existing:
                    return self.get_memory_entry(owner,existing['id'])
            elif source_id is not None:
                return None
            now=int(time.time())
            cursor=self._conn.execute('''INSERT INTO memory_entries
                (owner_id,project_id,kind,title,body,payload,source_kind,source_id,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?)''',(owner,project_id,kind,title,body,json.dumps(payload or {},ensure_ascii=False),source_kind,source_id,now,now))
            entry=self.get_memory_entry(owner,cursor.lastrowid)
            self.preserve_memory_attachment(owner,entry['id'])
            self._memory_snapshot(entry)
            return entry

    def _memory_snapshot(self,entry):
        self._conn.execute('INSERT INTO memory_versions(entry_id,owner_id,revision,snapshot,recorded_at) VALUES (?,?,?,?,?)',
            (entry['id'],entry['owner_id'],entry['revision'],json.dumps(entry,ensure_ascii=False),int(time.time())))

    def revise_memory(self,owner,ident,revision,body,*,payload=None,title=None):
        if not body.strip() or len(body)>12000:
            raise ValueError('Напиши текст до 12 000 символов.')
        with self._lock,self._conn:
            current=self.get_memory_entry(owner,ident)
            if not current or current['revision']!=revision:
                return None
            title=self._work_title(title,100) if title is not None else current['title']
            changed=self._conn.execute('UPDATE memory_entries SET body=?,payload=?,title=?,revision=revision+1,updated_at=? WHERE owner_id=? AND id=? AND revision=?',
                (body,json.dumps(current['payload'] if payload is None else payload,ensure_ascii=False),title,int(time.time()),owner,ident,revision))
            if not changed.rowcount:
                return None
            entry=self.get_memory_entry(owner,ident)
            self._memory_snapshot(entry)
            return entry

    def memory_version(self,owner,ident,revision):
        with self._lock:
            row=self._conn.execute('SELECT snapshot FROM memory_versions WHERE owner_id=? AND entry_id=? AND revision=?',(owner,ident,revision)).fetchone()
        return json.loads(row['snapshot']) if row else None

    def link_project_task(self,owner,project_id,task_id):
        with self._lock,self._conn:
            if not self.get_project(owner,project_id) or not self.get_task(owner,task_id):
                return False
            self._conn.execute('INSERT OR IGNORE INTO memory_task_links(owner_id,project_id,task_id) VALUES (?,?,?)',(owner,project_id,task_id))
            return True

    def project_tasks(self,owner,project_id,limit=8):
        with self._lock:
            rows=self._conn.execute('''SELECT t.* FROM tasks t JOIN memory_task_links l ON t.id=l.task_id AND t.owner_id=l.owner_id
                WHERE l.owner_id=? AND l.project_id=? ORDER BY t.status, t.priority DESC,t.id DESC LIMIT ?''',(owner,project_id,limit)).fetchall()
        return [dict(row) for row in rows]

    def memory_search(self,owner,query,limit=8,*,only_cases=False,exclude_id=0):
        """Rank literal stem matches. Never claim semantic or temporal understanding."""
        terms=query_terms(query)
        if not terms:
            return []
        score='+'.join('CASE WHEN instr(memory_normalize(title || char(10) || body),?)>0 THEN 1 ELSE 0 END' for _ in terms)
        corpus='''SELECT 'library' AS source,id,title,text || COALESCE((SELECT char(10) || '[Текст вложения, проверен пользователем]' || char(10) || accepted FROM extractions e WHERE e.owner_id=items.owner_id AND e.source_kind='library' AND e.source_id=items.id AND accepted!=''),'') AS body,created_at,'material' AS kind FROM items WHERE owner_id=?
            UNION ALL SELECT 'work',id,title,text || COALESCE((SELECT char(10) || '[Текст вложения, проверен пользователем]' || char(10) || accepted FROM extractions e WHERE e.owner_id=work_materials.owner_id AND e.source_kind='work' AND e.source_id=work_materials.id AND accepted!=''),''),created_at,'material' FROM work_materials WHERE owner_id=?
            UNION ALL SELECT 'memory',id,title,body,created_at,kind FROM memory_entries WHERE owner_id=?'''
        with self._lock:
            self._conn.create_function('memory_normalize',1,normalized,deterministic=True)
            rows=self._conn.execute(f"SELECT *,({score}) AS score FROM ({corpus}) WHERE score>0 AND (?=0 OR (source='memory' AND kind='case' AND id!=?)) ORDER BY score DESC,created_at DESC,id DESC LIMIT ?",
                                    (*terms,owner,owner,owner,int(only_cases),exclude_id,limit)).fetchall()
        return [dict(row) for row in rows]

    def delete_memory(self,owner,ident,*,project=False):
        table='memory_projects' if project else 'memory_entries'
        with self._lock,self._conn:
            return self._conn.execute(f'DELETE FROM {table} WHERE owner_id=? AND id=?',(owner,ident)).rowcount==1

    def memory_export(self,owner):
        with self._lock:
            return {table:[dict(row) for row in self._conn.execute(f'SELECT * FROM {table} WHERE owner_id=?',(owner,)).fetchall()]
                    for table in ('memory_projects','memory_entries','memory_versions','memory_task_links','memory_attachments')}

    def preserve_memory_attachment(self,owner,ident):
        entry=self.get_memory_entry(owner,ident)
        if not entry:
            return
        source=self.extraction_source(owner,entry['source_kind'],entry['source_id'])
        if source and source.get('file_id'):
            self._conn.execute('INSERT OR IGNORE INTO memory_attachments VALUES(?,?,?,?,?)',
                (ident,owner,source['kind'],source['file_id'],source.get('file_name')))

    def backfill_memory_attachments(self):
        with self._lock,self._conn:
            for row in self._conn.execute('SELECT owner_id,id FROM memory_entries WHERE source_id IS NOT NULL').fetchall():
                self.preserve_memory_attachment(row['owner_id'],row['id'])

    def memory_attachment(self,owner,ident):
        with self._lock:
            row=self._conn.execute('SELECT * FROM memory_attachments WHERE owner_id=? AND entry_id=?',(owner,ident)).fetchone()
        return dict(row) if row else None
