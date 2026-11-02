import json
import sqlite3
import threading
import time
import uuid
from contextlib import contextmanager


def new_id():
    return uuid.uuid4().hex


def dumps(value):
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class Store:
    """Small transactional store; SQLite WAL permits concurrent native clients."""

    def __init__(self, path):
        self.path = str(path)
        self.lock = threading.RLock()
        with self.connection() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS schema_version(version INTEGER NOT NULL);
                INSERT INTO schema_version SELECT 1 WHERE NOT EXISTS(SELECT 1 FROM schema_version);
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,created_at REAL NOT NULL,updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS messages(
                    id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
                    role TEXT NOT NULL,content TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'complete',
                    created_at REAL NOT NULL,metadata TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS jobs(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,status TEXT NOT NULL,progress REAL NOT NULL DEFAULT 0,
                    phase TEXT NOT NULL DEFAULT '',payload TEXT NOT NULL,result TEXT NOT NULL DEFAULT '{}',
                    error TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL,
                    idempotency_key TEXT UNIQUE,cancel_requested INTEGER NOT NULL DEFAULT 0);
                CREATE INDEX IF NOT EXISTS jobs_status ON jobs(status,created_at);
                CREATE INDEX IF NOT EXISTS messages_session ON messages(session_id,created_at);
                CREATE TABLE IF NOT EXISTS assets(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,path TEXT NOT NULL,mime TEXT NOT NULL,
                    size INTEGER NOT NULL,kind TEXT NOT NULL,created_at REAL NOT NULL,
                    job_id TEXT,metadata TEXT NOT NULL DEFAULT '{}');
                CREATE TABLE IF NOT EXISTS projects(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,brief TEXT NOT NULL,document TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL,updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS memories(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,content TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL,updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS devices(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,token_hash TEXT UNIQUE NOT NULL,created_at REAL NOT NULL,
                    last_seen REAL NOT NULL,revoked INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,entity TEXT NOT NULL,entity_id TEXT NOT NULL,
                    created_at REAL NOT NULL);
            """)
            # Coding conversations are ordinary sessions bound to a connected code project.
            if "code_project_id" not in [r[1] for r in db.execute("PRAGMA table_info(sessions)")]:
                db.execute("ALTER TABLE sessions ADD COLUMN code_project_id TEXT")

    @contextmanager
    def connection(self):
        with self.lock:
            db = sqlite3.connect(self.path, timeout=15)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                yield db
                db.commit()
            except BaseException:
                db.rollback()
                raise
            finally:
                db.close()

    @staticmethod
    def decode(row):
        if row is None:
            return None
        obj = dict(row)
        for key in ("payload", "result", "document", "metadata"):
            if key in obj:
                obj[key] = json.loads(obj[key])
        return obj

    def all(self, query, params=()):
        with self.connection() as db:
            return [self.decode(r) for r in db.execute(query, params).fetchall()]

    def one(self, query, params=()):
        with self.connection() as db:
            return self.decode(db.execute(query, params).fetchone())

    def execute(self, query, params=()):
        with self.connection() as db:
            return db.execute(query, params).rowcount

    def event(self, entity, entity_id):
        self.execute("INSERT INTO events(entity,entity_id,created_at) VALUES(?,?,?)", (entity, entity_id, time.time()))

    def get_job(self, job_id):
        return self.one("SELECT * FROM jobs WHERE id=?", (job_id,))

    def record_job(self, kind, payload, result):
        """A job that is already finished when created, e.g. a change set the assistant wrote inline."""
        ident, now = new_id(), time.time()
        self.execute("INSERT INTO jobs(id,kind,status,progress,phase,payload,result,created_at,updated_at) VALUES(?,?,'succeeded',1,'已完成',?,?,?,?)",
                     (ident, kind, dumps(payload), dumps(result), now, now))
        self.event("jobs", ident)
        return self.get_job(ident)

    def create_job(self, kind, payload, key=None):
        ident, now = new_id(), time.time()
        with self.connection() as db:
            if key:
                prior = self.decode(db.execute("SELECT * FROM jobs WHERE idempotency_key=?", (key,)).fetchone())
                if prior:
                    if prior["kind"] != kind or prior["payload"] != payload:
                        raise ValueError("重复请求标识对应了不同内容")
                    return prior
            db.execute("INSERT INTO jobs(id,kind,status,payload,created_at,updated_at,idempotency_key) VALUES(?,?,'queued',?,?,?,?)",
                       (ident, kind, dumps(payload), now, now, key))
        self.event("jobs", ident)
        return self.get_job(ident)

    def update_job(self, ident, **fields):
        allowed = {"status", "progress", "phase", "result", "error", "cancel_requested"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Invalid job update")
        fields["updated_at"] = time.time()
        if "result" in fields:
            fields["result"] = dumps(fields["result"])
        self.execute("UPDATE jobs SET " + ",".join(f"{k}=?" for k in fields) + " WHERE id=?", (*fields.values(), ident))
        self.event("jobs", ident)

    def claim_next(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',phase='正在准备',updated_at=? WHERE id=?", (time.time(), row["id"]))
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def recover(self):
        with self.connection() as db:
            db.execute("UPDATE jobs SET status='interrupted',error='引擎重启中断了此任务，可重新尝试',updated_at=? WHERE status='running'", (time.time(),))
            db.execute("UPDATE messages SET status='interrupted' WHERE status IN ('queued','streaming') AND id IN (SELECT json_extract(payload,'$.message_id') FROM jobs WHERE status='interrupted')")

    def add_message(self, session, role, content, status="complete", metadata=None):
        ident, now = new_id(), time.time()
        self.execute("INSERT INTO messages(id,session_id,role,content,status,created_at,metadata) VALUES(?,?,?,?,?,?,?)",
                     (ident, session, role, content, status, now, dumps(metadata or {})))
        self.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, session))
        self.event("messages", session)
        return ident

    def update_message(self, ident, content, status, metadata=None):
        self.execute("UPDATE messages SET content=?,status=?,metadata=? WHERE id=?", (content, status, dumps(metadata or {}), ident))
        self.event("messages", ident)

    def set_message_status(self, ident, status, **metadata):
        """Change status while keeping streamed content and the tool trace."""
        with self.connection() as db:
            row = db.execute("SELECT metadata FROM messages WHERE id=?", (ident,)).fetchone()
            if row is None:
                return
            merged = {**json.loads(row["metadata"]), **metadata}
            for step in merged.get("steps", []):
                for call in step.get("calls", []):
                    if call.get("status") == "running":
                        call["status"] = "error"; call["summary"] = "未完成"
            db.execute("UPDATE messages SET status=?,metadata=? WHERE id=?", (status, dumps(merged), ident))
        self.event("messages", ident)
