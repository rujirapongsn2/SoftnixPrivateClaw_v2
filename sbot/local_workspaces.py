"""Durable, owner-scoped local folder broker. No shell execution or automatic retries."""
import asyncio
import hashlib
import json
import secrets
import sqlite3
import time
from pathlib import Path
from contextlib import contextmanager

MAX_BYTES = 8 * 1024 * 1024


class LocalWorkspaces:
    def __init__(self, root: Path):
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / "registry.sqlite3"
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS pairing_result (code TEXT PRIMARY KEY, owner TEXT, workspace TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS pairing (code TEXT PRIMARY KEY, owner TEXT, expires REAL);
                CREATE TABLE IF NOT EXISTS workspace (
                    id TEXT PRIMARY KEY, owner TEXT, token TEXT UNIQUE, name TEXT,
                    path TEXT, writable INTEGER, seen REAL DEFAULT 0, revoked INTEGER DEFAULT 0);
                CREATE TABLE IF NOT EXISTS job (
                    id TEXT PRIMARY KEY, workspace TEXT, payload TEXT,
                    status TEXT, expires REAL, result TEXT);
                CREATE TABLE IF NOT EXISTS binding (session TEXT PRIMARY KEY, owner TEXT, workspace TEXT);
            """)
            # Existing installations predate the local path metadata. Keep the
            # migration additive so connected folders and session bindings stay
            # intact when the server updates.
            columns = {row[1] for row in db.execute("PRAGMA table_info(workspace)")}
            if "path" not in columns:
                db.execute("ALTER TABLE workspace ADD COLUMN path TEXT")
        self.path.chmod(0o600)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def select(self, owner, session, wid):
        if wid:
            self.owned(owner, wid)
        with self.db() as db:
            db.execute('INSERT OR REPLACE INTO binding VALUES (?,?,?)', (session, owner, wid))

    def selected(self, owner, session):
        with self.db() as db:
            row = db.execute('SELECT workspace FROM binding WHERE session=? AND owner=?', (session, owner)).fetchone()
        return row['workspace'] if row else ''

    @staticmethod
    def digest(value):
        return hashlib.sha256(value.encode()).hexdigest()

    def pair(self, owner):
        code = secrets.token_urlsafe(24)
        with self.db() as db:
            db.execute("DELETE FROM pairing WHERE expires < ?", (time.time(),))
            db.execute("DELETE FROM pairing_result WHERE expires < ?", (time.time(),))
            db.execute("INSERT INTO pairing VALUES (?, ?, ?)", (self.digest(code), owner, time.time()+600))
        return {"code": code, "expires_in": 600}

    def pairing_status(self, owner, code):
        with self.db() as db:
            row = db.execute("SELECT workspace FROM pairing_result WHERE code=? AND owner=? AND expires>?",
                             (self.digest(code), owner, time.time())).fetchone()
        return {"workspace_id": row["workspace"] if row else ""}

    def redeem(self, code, name, writable, previous_token=None, path=None):
        token, wid = secrets.token_urlsafe(32), secrets.token_hex(16)
        with self.db() as db:
            row = db.execute("DELETE FROM pairing WHERE code=? AND expires>? RETURNING owner",
                             (self.digest(code), time.time())).fetchone()
            if not row:
                raise ValueError("Pairing code expired or already used")
            if previous_token:
                previous = db.execute('SELECT id FROM workspace WHERE token=? AND owner=? AND revoked=0',
                                      (self.digest(previous_token), row['owner'])).fetchone()
                if not previous:
                    raise ValueError('Existing folder belongs to another account or was disconnected')
                db.execute('UPDATE workspace SET name=?,path=?,writable=? WHERE id=?',
                           (name, path, writable, previous['id']))
                db.execute('INSERT INTO pairing_result VALUES (?,?,?,?)', (self.digest(code), row['owner'], previous['id'], time.time()+600))
                return {'id': previous['id'], 'token': previous_token}
            db.execute("INSERT INTO workspace(id,owner,token,name,path,writable) VALUES (?,?,?,?,?,?)",
                       (wid, row['owner'], self.digest(token), name, path, writable))
            db.execute('INSERT INTO pairing_result VALUES (?,?,?,?)', (self.digest(code), row['owner'], wid, time.time()+600))
        return {"id": wid, "token": token}

    def owned(self, owner, wid):
        with self.db() as db:
            row = db.execute("SELECT * FROM workspace WHERE id=? AND owner=? AND revoked=0", (wid, owner)).fetchone()
        if not row:
            raise ValueError("Workspace not found")
        return row

    def authenticate(self, token):
        with self.db() as db:
            row = db.execute("SELECT * FROM workspace WHERE token=? AND revoked=0", (self.digest(token),)).fetchone()
        if not row:
            raise ValueError("Device disconnected or revoked")
        return row

    def list(self, owner):
        with self.db() as db:
            rows = db.execute("SELECT id,name,path,writable,seen FROM workspace WHERE owner=? AND revoked=0", (owner,)).fetchall()
        return [{"id": r['id'], "name": r['name'], "path": r['path'], "writable": bool(r['writable']),
                 "online": r['seen'] > time.time()-15} for r in rows]

    def revoke(self, owner, wid):
        self.owned(owner, wid)
        with self.db() as db:
            db.execute("DELETE FROM binding WHERE owner=? AND workspace=?", (owner, wid))
            db.execute("UPDATE workspace SET revoked=1 WHERE id=?", (wid,))
            db.execute("UPDATE job SET status='cancelled',payload='{}',result=NULL WHERE workspace=?", (wid,))

    def poll(self, wid, path=None):
        with self.db() as db:
            if isinstance(path, str) and path and len(path) <= 4096:
                db.execute("UPDATE workspace SET seen=?,path=? WHERE id=?", (time.time(), path, wid))
            else:
                db.execute("UPDATE workspace SET seen=? WHERE id=?", (time.time(), wid))
            db.execute("DELETE FROM job WHERE expires < ?", (time.time()-3600,))
            row = db.execute("UPDATE job SET status='running' WHERE id=(SELECT id FROM job WHERE workspace=? AND status='queued' AND expires>? ORDER BY rowid LIMIT 1) RETURNING id,payload",
                             (wid, time.time())).fetchone()
        return {"id": row['id'], **json.loads(row['payload'])} if row else None

    def result(self, wid, jid, result):
        with self.db() as db:
            row = db.execute("UPDATE job SET status='done',result=?,payload='{}' WHERE id=? AND workspace=? AND status='running' AND expires>? RETURNING id",
                             (json.dumps(result), jid, wid, time.time())).fetchone()
        return bool(row)

    async def call(self, owner, wid, payload):
        row = self.owned(owner, wid)
        if row['seen'] < time.time()-15:
            raise ValueError("Local folder is offline. Open the Local Agent and retry when connected.")
        if payload['action'] == 'write' and not row['writable']:
            raise ValueError("Folder is read-only; enable --write on the Local Agent and pair again")
        jid = secrets.token_hex(16)
        with self.db() as db:
            db.execute("INSERT INTO job VALUES (?,?,?,'queued',?,NULL)", (jid, wid, json.dumps(payload), time.time()+45))
        try:
            for _ in range(180):
                await asyncio.sleep(.25)
                self.owned(owner, wid)
                with self.db() as db:
                    job = db.execute("SELECT status,result FROM job WHERE id=?", (jid,)).fetchone()
                if job and job['status'] == 'done':
                    return json.loads(job['result'])
            raise ValueError("Local operation timed out. Its outcome may be unknown; inspect the file before retrying a write.")
        finally:
            with self.db() as db:
                db.execute("DELETE FROM job WHERE id=?", (jid,))
