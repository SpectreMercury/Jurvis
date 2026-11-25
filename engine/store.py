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
            columns = [r[1] for r in db.execute("PRAGMA table_info(sessions)")]
            if "code_project_id" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN code_project_id TEXT")
            # The user allowed the assistant to run commands in this conversation without asking each time.
            if "allow_commands" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN allow_commands INTEGER NOT NULL DEFAULT 0")
            # Per-conversation model, memory bookkeeping and the rolling summary of older messages.
            for name, kind in (("model", "TEXT"), ("incognito", "INTEGER NOT NULL DEFAULT 0"), ("summary", "TEXT"),
                               ("summary_upto", "REAL NOT NULL DEFAULT 0"), ("memory_upto", "REAL NOT NULL DEFAULT 0"), ("tools", "TEXT"),
                               # A conversation opened by the assistant for one subject: what it is for, what led to it, and where it came from.
                               ("persona", "TEXT"), ("brief", "TEXT"), ("parent_id", "TEXT")):
                if name not in columns:
                    db.execute(f"ALTER TABLE sessions ADD COLUMN {name} {kind}")
            # The user sees one conversation. A session row is one stretch of it, about one thing (engine/stream.py);
            # focused_at is when the user's attention was last put on it: where their next message continues.
            if "focused_at" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN focused_at REAL")
            # The record of a finished stretch, by which it is found again later (engine/memory.py).
            db.execute("""CREATE TABLE IF NOT EXISTS digests(
                session_id TEXT PRIMARY KEY REFERENCES sessions(id) ON DELETE CASCADE,text TEXT NOT NULL,
                upto REAL NOT NULL,embedding BLOB,embed_model TEXT)""")
            db.execute("CREATE INDEX IF NOT EXISTS messages_time ON messages(created_at)")
            columns = [r[1] for r in db.execute("PRAGMA table_info(memories)")]
            for name, kind in (("importance", "REAL NOT NULL DEFAULT 0.5"), ("durable", "INTEGER NOT NULL DEFAULT 1"), ("tags", "TEXT NOT NULL DEFAULT '[]'"),
                               ("source", "TEXT NOT NULL DEFAULT 'manual'"), ("session_id", "TEXT"), ("embedding", "BLOB"), ("embed_model", "TEXT"), ("used_at", "REAL"),
                               # A fact about the user's health, money, identity or intimate life: kept for the local model (engine/memory.py).
                               ("private", "INTEGER NOT NULL DEFAULT 0")):
                if name not in columns:
                    db.execute(f"ALTER TABLE memories ADD COLUMN {name} {kind}")
            db.execute("UPDATE schema_version SET version=2 WHERE version<2")
            # What the assistant keeps doing over time: goals it tracks and work it runs on a schedule (engine/agenda.py).
            db.executescript("""
                CREATE TABLE IF NOT EXISTS goals(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,detail TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT 'active',
                    session_id TEXT,created_at REAL NOT NULL,updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS transactions(
                    id TEXT PRIMARY KEY,at REAL NOT NULL,amount_cents INTEGER NOT NULL,direction TEXT NOT NULL DEFAULT 'expense',
                    category TEXT NOT NULL,merchant TEXT NOT NULL DEFAULT '',note TEXT NOT NULL DEFAULT '',source TEXT NOT NULL DEFAULT 'chat',
                    ref TEXT UNIQUE,auto INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL);
                CREATE INDEX IF NOT EXISTS transactions_at ON transactions(at);
                CREATE TABLE IF NOT EXISTS budgets(category TEXT PRIMARY KEY,amount_cents INTEGER NOT NULL,updated_at REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS schedules(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,brief TEXT NOT NULL,rule TEXT NOT NULL,next_run REAL NOT NULL,
                    last_run REAL,session_id TEXT,enabled INTEGER NOT NULL DEFAULT 1,created_at REAL NOT NULL);
            """)
            # What the assistant did ("work", written by the harness) and what it said to itself ("mind") (engine/journal.py).
            db.executescript("""
                CREATE TABLE IF NOT EXISTS journal(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,at REAL NOT NULL,stream TEXT NOT NULL,kind TEXT NOT NULL,
                    owner TEXT NOT NULL DEFAULT '',text TEXT NOT NULL,name TEXT NOT NULL DEFAULT '',status TEXT NOT NULL DEFAULT '',
                    effect TEXT NOT NULL DEFAULT '',origin TEXT NOT NULL DEFAULT 'chat',session_id TEXT,message_id TEXT,job_id TEXT,
                    extras TEXT NOT NULL DEFAULT '{}');
                CREATE INDEX IF NOT EXISTS journal_at ON journal(at);
                CREATE INDEX IF NOT EXISTS journal_mind ON journal(stream,id);
            """)

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
        for key in ("payload", "result", "document", "metadata", "tags", "rule", "extras"):
            if isinstance(obj.get(key), str):
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

    def to_background(self, ident):
        """Marks a conversation job as background work: it no longer holds the conversation, and its result is delivered when done."""
        self.execute("UPDATE jobs SET payload=json_set(payload,'$.background',json('true')),updated_at=? WHERE id=?", (time.time(), ident))
        self.event("jobs", ident)

    def deliver(self, message_id):
        """A background result is posted when it is ready: it moves below whatever was said while it was being worked on."""
        row = self.one("SELECT session_id,created_at FROM messages WHERE id=?", (message_id,))
        if row and self.one("SELECT id FROM messages WHERE session_id=? AND created_at>? LIMIT 1", (row["session_id"], row["created_at"])):
            now = time.time()
            self.execute("UPDATE messages SET created_at=? WHERE id=?", (now, message_id))
            self.execute("UPDATE sessions SET updated_at=? WHERE id=?", (now, row["session_id"]))
            self.event("messages", row["session_id"])

    def claim_next(self):
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 1").fetchone()
            if row is None:
                return None
            db.execute("UPDATE jobs SET status='running',phase='正在准备',updated_at=? WHERE id=?", (time.time(), row["id"]))
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (row["id"],)).fetchone())

    def queued(self):
        return self.all("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at LIMIT 50")

    def claim(self, ident):
        """Marks one queued job as running; None if something else took or cancelled it."""
        with self.connection() as db:
            db.execute("BEGIN IMMEDIATE")
            changed = db.execute("UPDATE jobs SET status='running',phase='正在准备',updated_at=? WHERE id=? AND status='queued'", (time.time(), ident)).rowcount
            return self.decode(db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()) if changed else None

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
