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
ONLINE_WINDOW = 15
DELIVERY_TTL = 7 * 24 * 3600
MAX_DELIVERY_ATTEMPTS = 5
MAX_PENDING_DELIVERIES = 50
# A claim older than this belongs to a worker that died without reporting.
DELIVERY_STALE = 300
DELIVERY_BACKOFF = 60
MAX_DELIVERY_BACKOFF = 1800


class OfflineError(ValueError):
    """The paired folder is not currently reachable."""


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
                CREATE TABLE IF NOT EXISTS delivery (
                    id TEXT PRIMARY KEY, owner TEXT, workspace TEXT, cloud_path TEXT, sha256 TEXT,
                    dest TEXT, name TEXT, status TEXT, attempts INTEGER DEFAULT 0,
                    created REAL, expires REAL, detail TEXT DEFAULT '',
                    claimed REAL DEFAULT 0, next_attempt REAL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS delivery_owner ON delivery(owner, status);
                CREATE INDEX IF NOT EXISTS delivery_workspace ON delivery(workspace, status);
            """)
            # Existing installations predate the local path metadata. Keep the
            # migration additive so connected folders and session bindings stay
            # intact when the server updates.
            columns = {row[1] for row in db.execute("PRAGMA table_info(workspace)")}
            if "path" not in columns:
                db.execute("ALTER TABLE workspace ADD COLUMN path TEXT")
            delivery_columns = {row[1] for row in db.execute("PRAGMA table_info(delivery)")}
            for column in ("claimed", "next_attempt"):
                if column not in delivery_columns:
                    db.execute(f"ALTER TABLE delivery ADD COLUMN {column} REAL DEFAULT 0")
            # A restart while a delivery was in flight must not strand it. The
            # write itself is idempotent (hash compared before sending).
            db.execute("UPDATE delivery SET status='pending' WHERE status='running'")
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
        # One grouped query, not one per folder: this list is polled every few
        # seconds by every open tab.
        with self.db() as db:
            rows = db.execute("""
                SELECT w.id, w.name, w.path, w.writable, w.seen,
                       COALESCE(SUM(d.status='pending'), 0) AS pending,
                       COALESCE(SUM(d.status='failed'), 0) AS failed
                FROM workspace w LEFT JOIN delivery d ON d.workspace=w.id AND d.status IN ('pending','failed')
                WHERE w.owner=? AND w.revoked=0 GROUP BY w.id""", (owner,)).fetchall()
        return [{"id": r['id'], "name": r['name'], "path": r['path'], "writable": bool(r['writable']),
                 "online": r['seen'] > time.time()-ONLINE_WINDOW,
                 "pending": r['pending'], "failed": r['failed']} for r in rows]

    def revoke(self, owner, wid):
        self.owned(owner, wid)
        with self.db() as db:
            db.execute("DELETE FROM binding WHERE owner=? AND workspace=?", (owner, wid))
            db.execute("UPDATE workspace SET revoked=1 WHERE id=?", (wid,))
            db.execute("UPDATE job SET status='cancelled',payload='{}',result=NULL WHERE workspace=?", (wid,))
            # Disconnecting must stop queued files from reaching the folder
            # later; a revoked pairing is an explicit withdrawal of access.
            db.execute("DELETE FROM delivery WHERE workspace=?", (wid,))

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
        if row['seen'] < time.time()-ONLINE_WINDOW:
            raise OfflineError("Local folder is offline. Open the Local Agent and retry when connected.")
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

    # --- Deferred cloud → local deliveries -------------------------------
    # Only outbound writes are queued. A queued read would answer a question
    # nobody is waiting for any more, so reads still fail fast when offline.

    def enqueue(self, owner, wid, cloud_path, sha256, dest, name=''):
        row = self.owned(owner, wid)
        if not row['writable']:
            raise ValueError("Folder is read-only; enable Read & Write on the Local Agent and pair again")
        now = time.time()
        with self.db() as db:
            db.execute("DELETE FROM delivery WHERE status='pending' AND expires<?", (now,))
            existing = db.execute("SELECT id FROM delivery WHERE workspace=? AND dest=? AND sha256=? AND status IN ('pending','running')",
                                  (wid, dest, sha256)).fetchone()
            if existing:
                return existing['id']
            waiting = db.execute("SELECT count(*) FROM delivery WHERE workspace=? AND status IN ('pending','running')", (wid,)).fetchone()[0]
            if waiting >= MAX_PENDING_DELIVERIES:
                raise ValueError(f"{MAX_PENDING_DELIVERIES} files are already waiting for this folder; reconnect it or cancel some before adding more")
            did = secrets.token_hex(16)
            db.execute("INSERT INTO delivery(id,owner,workspace,cloud_path,sha256,dest,name,status,attempts,created,expires,detail)"
                       " VALUES (?,?,?,?,?,?,?,'pending',0,?,?,'')",
                       (did, owner, wid, cloud_path, sha256, dest, name or dest, now, now+DELIVERY_TTL))
        return did

    def deliveries(self, owner, limit=100):
        with self.db() as db:
            rows = db.execute("SELECT id,workspace,name,dest,status,attempts,created,expires,detail FROM delivery"
                              " WHERE owner=? AND status IN ('pending','running','failed') ORDER BY created LIMIT ?",
                              (owner, limit)).fetchall()
        return [dict(r) for r in rows]

    def cancel_delivery(self, owner, did):
        with self.db() as db:
            row = db.execute("DELETE FROM delivery WHERE id=? AND owner=? AND status IN ('pending','failed') RETURNING id",
                             (did, owner)).fetchone()
        if not row:
            raise ValueError("Pending delivery not found")

    def claim_delivery(self):
        """Claim one delivery whose folder is back online, idle, and still writable."""
        now = time.time()
        with self.db() as db:
            # A worker that died before reporting would otherwise strand the row
            # in 'running', where nothing can claim, cancel or re-queue it.
            db.execute("UPDATE delivery SET status='pending' WHERE status='running' AND claimed<?",
                       (now-DELIVERY_STALE,))
            db.execute("UPDATE delivery SET status='failed', detail='The folder did not reconnect before this delivery expired'"
                       " WHERE status='pending' AND expires<?", (now,))
            db.execute("DELETE FROM delivery WHERE status='failed' AND expires<?", (now-DELIVERY_TTL,))
            # A queued or running interactive job means someone is waiting on a
            # turn; background deliveries never jump that queue.
            row = db.execute("""
                UPDATE delivery SET status='running', attempts=attempts+1, claimed=? WHERE id=(
                    SELECT d.id FROM delivery d JOIN workspace w ON w.id=d.workspace
                    WHERE d.status='pending' AND d.next_attempt<=? AND w.revoked=0 AND w.writable=1 AND w.seen>?
                      AND NOT EXISTS (SELECT 1 FROM job j WHERE j.workspace=d.workspace
                                      AND j.status IN ('queued','running') AND j.expires>?)
                    ORDER BY d.created LIMIT 1)
                RETURNING id,owner,workspace,cloud_path,sha256,dest,name,attempts""",
                (now, now, now-ONLINE_WINDOW, now)).fetchone()
        return dict(row) if row else None

    def finish_delivery(self, did, delivered, detail='', final=False, transient=False):
        """Retries back off exponentially so one bad minute cannot burn the budget."""
        now = time.time()
        with self.db() as db:
            if delivered:
                db.execute("DELETE FROM delivery WHERE id=?", (did,))
            elif transient:
                # The folder, not the file, was unavailable. Hand the attempt
                # back so a flapping connection cannot exhaust the budget.
                db.execute("UPDATE delivery SET status='pending', attempts=max(attempts-1,0),"
                           " next_attempt=?, detail=? WHERE id=?",
                           (now+DELIVERY_BACKOFF, detail[:300], did))
            else:
                db.execute("UPDATE delivery SET status=CASE WHEN ? OR attempts>=? THEN 'failed' ELSE 'pending' END,"
                           " next_attempt=? + min(? * (1 << (max(attempts,1)-1)), ?), detail=? WHERE id=?",
                           (final, MAX_DELIVERY_ATTEMPTS, now, DELIVERY_BACKOFF, MAX_DELIVERY_BACKOFF, detail[:300], did))
