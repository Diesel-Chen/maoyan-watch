"""Local SQLite history and persistent per-channel notification outbox."""
import json
import sqlite3
from pathlib import Path


def connect(path):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA journal_mode=WAL')
    db.executescript('''
        CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS movies (
            scope TEXT NOT NULL, id TEXT NOT NULL, name TEXT NOT NULL,
            first_seen TEXT NOT NULL, release_date TEXT, release_kind TEXT,
            release_source TEXT, checked_at TEXT,
            PRIMARY KEY(scope,id));
        CREATE TABLE IF NOT EXISTS shows (
            scope TEXT NOT NULL, movie_id TEXT NOT NULL, day TEXT NOT NULL,
            time TEXT NOT NULL, hall TEXT NOT NULL, version TEXT NOT NULL,
            first_seen TEXT NOT NULL, baseline INTEGER NOT NULL,
            PRIMARY KEY(scope,movie_id,day,time,hall,version));
        CREATE TABLE IF NOT EXISTS outbox (
            id INTEGER PRIMARY KEY, scope TEXT NOT NULL, movie_id TEXT NOT NULL,
            detected_at TEXT NOT NULL, text TEXT NOT NULL, channel TEXT NOT NULL,
            sent_at TEXT, attempts INTEGER NOT NULL DEFAULT 0);
    ''')
    return db


def migrate_legacy(db, path):
    """Archive old history once; never replay old notifications on upgrade."""
    marker = 'legacy_imported'
    if db.execute('SELECT 1 FROM meta WHERE key=?', (marker,)).fetchone():
        return
    source = Path(path)
    if not source.exists():
        return
    data = json.loads(source.read_text())
    if data.get('version') != 1 or not isinstance(data.get('seen'), dict):
        raise ValueError('旧状态文件格式无效，停止迁移')
    cid, name, old_hall, mid = data['scope']
    scope = json.dumps([str(cid), old_hall], ensure_ascii=False)
    with db:
        for key, record in data['seen'].items():
            day, tm, th, tp = json.loads(key)
            found = record['first_seen']
            db.execute('INSERT OR IGNORE INTO movies(scope,id,name,first_seen) VALUES(?,?,?,?)',
                       (scope, str(mid), name, found))
            db.execute('INSERT OR IGNORE INTO shows VALUES(?,?,?,?,?,?,?,?)',
                       (scope, str(mid), day, tm, th, tp, found, int(record.get('baseline', False))))
        db.execute('INSERT INTO meta VALUES(?,?)', (marker, '1'))
