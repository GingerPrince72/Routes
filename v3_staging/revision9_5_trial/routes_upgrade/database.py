import os, sqlite3
from contextlib import contextmanager
from pathlib import Path
DB_PATH=Path(os.getenv('ROUTES_DB_PATH','routes.db'))
@contextmanager
def connect():
    conn=sqlite3.connect(DB_PATH,timeout=15); conn.row_factory=sqlite3.Row
    try: yield conn; conn.commit()
    except Exception: conn.rollback(); raise
    finally: conn.close()
def init_db():
    with connect() as db:
        db.execute('''CREATE TABLE IF NOT EXISTS locations (id INTEGER PRIMARY KEY AUTOINCREMENT,name TEXT NOT NULL,latitude REAL,longitude REAL,open_time TEXT,close_time TEXT,is_depot INTEGER DEFAULT 0)''')
        cols={r['name'] for r in db.execute('PRAGMA table_info(locations)')}
        additions={'service_minutes':'INTEGER NOT NULL DEFAULT 5','address':"TEXT DEFAULT ''",'notes':"TEXT DEFAULT ''",'weekly_hours':"TEXT NOT NULL DEFAULT '{}'",'place_id':"TEXT NOT NULL DEFAULT ''"}
        for name,definition in additions.items():
            if name not in cols: db.execute(f'ALTER TABLE locations ADD COLUMN {name} {definition}')
        db.execute('UPDATE locations SET service_minutes=5 WHERE service_minutes IS NULL')
        db.execute('CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY,value TEXT NOT NULL)')
def all_locations():
    with connect() as db: return [dict(r) for r in db.execute('SELECT * FROM locations WHERE COALESCE(is_depot,0)=0 ORDER BY name COLLATE NOCASE,id')]
def upsert_location(data,loc_id=None):
    fields=('name','address','latitude','longitude','open_time','close_time','service_minutes','notes','weekly_hours','place_id')
    vals=[data.get(k) for k in fields]
    with connect() as db:
        if loc_id is None:
            cursor=db.execute(f"INSERT INTO locations ({','.join(fields)}) VALUES ({','.join('?' for _ in fields)})",vals)
            return cursor.lastrowid
        else:
            db.execute('UPDATE locations SET '+','.join(f'{f}=?' for f in fields)+' WHERE id=?',vals+[loc_id])
            return loc_id
def delete_location(loc_id):
    with connect() as db: db.execute('DELETE FROM locations WHERE id=?',(loc_id,))
def get_setting(key):
    with connect() as db:
        row=db.execute('SELECT value FROM settings WHERE key=?',(key,)).fetchone(); return row['value'] if row else None
def save_setting(key,value):
    with connect() as db: db.execute('INSERT INTO settings(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',(key,value))
