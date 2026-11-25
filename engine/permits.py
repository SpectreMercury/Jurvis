"""Standing permissions: kinds of action the user said need not be asked about again.

An approval card has three answers besides no: this once, for the rest of this conversation, and from now on. The last one
is kept here, for one narrow kind of action (clicking on one website, one device doing one thing, one tool of one MCP server,
one program with one subcommand), and can be taken back in 设置.

Some things are asked about every time whatever is kept here: a phone call, an email, a lock, door, alarm or camera,
a script run in a web page, and any command that looks risky or strings several commands together.
"""
import re
import shlex
import time
from urllib.parse import urlparse

from .store import new_id

CHAINED = re.compile(r"[;&|<>`\n]|\$\(")   # more than one program, or output sent somewhere: not a kind of command, a script
ENV = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Programs whose first word says what they do; for the rest the second word (the subcommand) is part of the kind.
PLAIN = {"ls", "cat", "head", "tail", "wc", "pwd", "which", "file", "stat", "du", "df", "date", "echo", "grep", "rg", "find", "tree", "open", "pytest", "make", "swift", "xcodebuild"}
NEVER = {"python", "python3", "node", "ruby", "perl", "sh", "bash", "zsh", "env", "xargs", "eval", "exec", "curl", "wget", "ssh", "scp"}  # they run whatever they are handed


def command_scope(command):
    """(scope, label) for a command simple enough to allow as a kind, or None."""
    if CHAINED.search(command) or len(command) > 400:
        return None
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    while words and ENV.match(words[0]):
        words = words[1:]
    if not words or "/" in words[0] or words[0] in NEVER:
        return None
    kind = words[:1] if words[0] in PLAIN or len(words) < 2 or words[1].startswith("-") else words[:2]
    if len(kind) == 2 and not re.fullmatch(r"[A-Za-z0-9_.:-]+", kind[1]):
        return None
    text = " ".join(kind)
    return "cmd:" + text, f"以后运行 {text} … 都不用问"


def page_scope(url):
    host = urlparse(url or "").hostname
    return ("site:" + host, f"以后在 {host} 上点击和输入都不用问") if host else None


class Permits:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS permits(
                id TEXT PRIMARY KEY,tool TEXT NOT NULL,scope TEXT NOT NULL,label TEXT NOT NULL,created_at REAL NOT NULL,
                used_at REAL,uses INTEGER NOT NULL DEFAULT 0,UNIQUE(tool,scope))""")

    def list(self):
        return self.store.all("SELECT * FROM permits ORDER BY created_at DESC")

    def allow(self, tool, scope, label):
        self.store.execute("INSERT INTO permits(id,tool,scope,label,created_at) VALUES(?,?,?,?,?) ON CONFLICT(tool,scope) DO NOTHING",
                           (new_id(), tool, scope, label[:200], time.time()))
        self.store.event("permits", tool)

    def allowed(self, tool, scope):
        """True when the user allowed this kind of action for good. Counts the use, so 设置 can show what a permission is doing."""
        if not scope:
            return False
        used = self.store.execute("UPDATE permits SET used_at=?,uses=uses+1 WHERE tool=? AND scope=?", (time.time(), tool, scope))
        return bool(used)

    def revoke(self, ident):
        gone = self.store.execute("DELETE FROM permits WHERE id=?", (ident,))
        self.store.event("permits", ident)
        return bool(gone)
