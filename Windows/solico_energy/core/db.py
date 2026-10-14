from __future__ import annotations
import sqlite3
from contextlib import contextmanager
from .paths import db_path

SCHEMA = '''
PRAGMA journal_mode=WAL;
CREATE TABLE IF NOT EXISTS sources(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 name TEXT NOT NULL,
 path TEXT NOT NULL,
 adapter TEXT NOT NULL DEFAULT 'wincc_csv',
 enabled INTEGER NOT NULL DEFAULT 1,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE TABLE IF NOT EXISTS tag_mappings(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_id INTEGER NOT NULL,
 tag_name TEXT NOT NULL,
 enabled INTEGER NOT NULL DEFAULT 1,
 entity_type TEXT NOT NULL DEFAULT 'other',
 entity_name TEXT NOT NULL DEFAULT '',
 role TEXT NOT NULL DEFAULT 'ignore',
 display_name TEXT NOT NULL DEFAULT '',
 unit TEXT NOT NULL DEFAULT '',
 UNIQUE(source_id, tag_name)
);
CREATE TABLE IF NOT EXISTS records(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 source_id INTEGER NOT NULL,
 tag_name TEXT NOT NULL,
 ts TEXT NOT NULL,
 ts_epoch REAL NOT NULL,
 value_num REAL,
 value_text TEXT,
 validity TEXT,
 time_ms TEXT,
 imported_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(source_id, tag_name, ts_epoch)
);
CREATE INDEX IF NOT EXISTS idx_records_tag_time ON records(source_id, tag_name, ts_epoch);
CREATE TABLE IF NOT EXISTS generated_reports(
 id INTEGER PRIMARY KEY AUTOINCREMENT,
 report_type TEXT NOT NULL,
 period_key TEXT NOT NULL,
 path TEXT NOT NULL,
 created_at TEXT DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(report_type, period_key)
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
'''

@contextmanager
def connect():
    con=sqlite3.connect(db_path()); con.row_factory=sqlite3.Row
    try:
        con.executescript(SCHEMA); yield con; con.commit()
    finally: con.close()

def init_db():
    with connect() as con: con.executescript(SCHEMA)

def list_sources():
    with connect() as con: return [dict(r) for r in con.execute('SELECT * FROM sources ORDER BY id')]

def add_source(name,path,adapter='wincc_csv'):
    with connect() as con:
        cur=con.execute('INSERT INTO sources(name,path,adapter) VALUES(?,?,?)',(name,path,adapter)); return cur.lastrowid

def update_source(source_id,name,path,enabled=1):
    with connect() as con: con.execute('UPDATE sources SET name=?, path=?, enabled=? WHERE id=?',(name,path,int(enabled),source_id))

def delete_source(source_id):
    with connect() as con:
        con.execute('DELETE FROM tag_mappings WHERE source_id=?',(source_id,)); con.execute('DELETE FROM sources WHERE id=?',(source_id,))

def mappings(source_id=None, enabled_only=False):
    sql='SELECT * FROM tag_mappings'; args=[]; clauses=[]
    if source_id is not None: clauses.append('source_id=?'); args.append(source_id)
    if enabled_only: clauses.append('enabled=1')
    if clauses: sql+=' WHERE '+' AND '.join(clauses)
    sql+=' ORDER BY entity_type, entity_name, tag_name'
    with connect() as con: return [dict(r) for r in con.execute(sql,args)]

def upsert_mapping(m):
    with connect() as con:
        con.execute('''INSERT INTO tag_mappings(source_id,tag_name,enabled,entity_type,entity_name,role,display_name,unit)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_id,tag_name) DO UPDATE SET enabled=excluded.enabled,entity_type=excluded.entity_type,entity_name=excluded.entity_name,role=excluded.role,display_name=excluded.display_name,unit=excluded.unit''',
        (m['source_id'],m['tag_name'],int(m.get('enabled',1)),m.get('entity_type','other'),m.get('entity_name',''),m.get('role','ignore'),m.get('display_name',''),m.get('unit','')))

def insert_record(row):
    with connect() as con:
        cur=con.execute('INSERT OR IGNORE INTO records(source_id,tag_name,ts,ts_epoch,value_num,value_text,validity,time_ms) VALUES(?,?,?,?,?,?,?,?)',
            (row['source_id'],row['tag_name'],row['ts'],row['ts_epoch'],row.get('value_num'),row.get('value_text'),row.get('validity'),row.get('time_ms')))
        return cur.rowcount

def insert_records(rows):
    vals=[(r['source_id'],r['tag_name'],r['ts'],r['ts_epoch'],r.get('value_num'),r.get('value_text'),r.get('validity'),r.get('time_ms')) for r in rows]
    if not vals:return 0
    with connect() as con:
        before=con.total_changes
        con.executemany('INSERT OR IGNORE INTO records(source_id,tag_name,ts,ts_epoch,value_num,value_text,validity,time_ms) VALUES(?,?,?,?,?,?,?,?)',vals)
        return con.total_changes-before

def mark_report(report_type,period_key,path,broadcast=True):
    with connect() as con:
        con.execute('INSERT INTO generated_reports(report_type,period_key,path) VALUES(?,?,?) ON CONFLICT(report_type,period_key) DO UPDATE SET path=excluded.path, created_at=CURRENT_TIMESTAMP',(report_type,period_key,path))
    # Every newly generated Windows report is announced to the other SolicoEnergy
    # Windows installations. Synced downloads call this with broadcast=False to
    # avoid report loops.
    if broadcast:
        try:
            from .mobile_push import broadcast_windows_report_async
            broadcast_windows_report_async(report_type,period_key,path)
        except Exception:
            pass
def report_exists(report_type,period_key):
    with connect() as con:
        r=con.execute('SELECT path FROM generated_reports WHERE report_type=? AND period_key=?',(report_type,period_key)).fetchone(); return dict(r) if r else None

def last_report_key(report_type):
    with connect() as con:
        r=con.execute('SELECT period_key FROM generated_reports WHERE report_type=? ORDER BY period_key DESC LIMIT 1',(report_type,)).fetchone()
        return r['period_key'] if r else None

def first_record_date():
    with connect() as con:
        r=con.execute('SELECT MIN(ts_epoch) AS mn FROM records').fetchone()
        if not r or r['mn'] is None: return None
        from datetime import datetime
        return datetime.fromtimestamp(r['mn']).date()


def delete_generated_report(report_type,period_key):
    """Remove only one generated report; keep its PDF in recoverable storage."""
    import shutil, uuid, json
    from pathlib import Path
    from .paths import data_dir
    with connect() as con:
        row=con.execute('SELECT * FROM generated_reports WHERE report_type=? AND period_key=?',(report_type,period_key)).fetchone()
        if row is None: return False
        record=dict(row)
        folder=data_dir()/'deleted_reports'/uuid.uuid4().hex
        folder.mkdir(parents=True)
        (folder/'report.json').write_text(json.dumps(record,ensure_ascii=False,indent=2),encoding='utf-8')
        source=Path(record['path']) if record.get('path') else None
        shared=con.execute('SELECT COUNT(*) FROM generated_reports WHERE path=?',(record.get('path'),)).fetchone()[0]>1
        moved=False
        try:
            if source and source.is_file():
                if shared: shutil.copy2(source,folder/source.name)
                else: shutil.move(str(source),str(folder/source.name)); moved=True
            con.execute('DELETE FROM generated_reports WHERE report_type=? AND period_key=?',(report_type,period_key))
            con.commit()
        except Exception:
            con.rollback()
            if moved: shutil.move(str(folder/source.name),str(source))
            raise
    return True
