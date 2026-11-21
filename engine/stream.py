"""The one conversation.

Jurvis talks with its user in a single stream, the way two people do, not in a list of chats. Underneath, the stream
is a run of stretches: a session row is one stretch of talk about one thing, with its own context, summary and
settings. A message continues the stretch the user is in; after a pause, or when the subject changes, the next
stretch begins by itself. What was said in other stretches comes back through memory (engine/memory.py), not by
replaying them.
"""
import time

from .store import new_id

GAP = 30 * 60            # seconds of quiet after which the next message begins a new stretch
PAGE = 120               # messages in one page of the stream
LEAD = 4                 # messages shown before the one a search result opens at


class Stream:
    def __init__(self, store):
        self.store = store

    # ---- which stretch a message belongs to ----
    def active(self, session_id):
        """When this stretch last saw talk: its newest message, or a reply to the user that is still being written."""
        said = self.store.one("SELECT MAX(created_at) AS at FROM messages WHERE session_id=?", (session_id,))["at"] or 0
        reply = self.store.one("SELECT MAX(updated_at) AS at FROM jobs WHERE kind='chat' AND json_extract(payload,'$.session_id')=? "
                               "AND json_extract(payload,'$.background') IS NULL", (session_id,))["at"] or 0
        return max(said, reply)

    def current(self, now=None):
        """The stretch the user is in, or None once it has gone quiet."""
        now = now or time.time()
        row = self.store.one("SELECT * FROM sessions WHERE focused_at IS NOT NULL ORDER BY focused_at DESC LIMIT 1")
        if row and now - max(row["focused_at"], self.active(row["id"])) <= GAP:
            return row
        return None

    def answered(self, now=None):
        """The stretch Jurvis just wrote in by itself (a check-in, a finished task, a scheduled run): a reply goes there."""
        now = now or time.time()
        last = self.store.one("SELECT session_id,role,created_at FROM messages WHERE status='complete' AND content!='' ORDER BY created_at DESC LIMIT 1")
        if last and last["role"] == "assistant" and now - last["created_at"] <= GAP:
            return self.store.one("SELECT * FROM sessions WHERE id=?", (last["session_id"],))
        return None

    def focus(self, session_id, now=None):
        self.store.execute("UPDATE sessions SET focused_at=? WHERE id=?", (now or time.time(), session_id))

    def open(self, *, title="新对话", model=None, incognito=False, code_project_id=None, parent=None, now=None):
        """Begins a stretch and puts the user in it. parent is the stretch it was split off from."""
        ident, now = new_id(), now or time.time()
        self.store.execute("INSERT INTO sessions(id,title,created_at,updated_at,model,incognito,code_project_id,focused_at,parent_id) VALUES(?,?,?,?,?,?,?,?,?)",
                           (ident, title, now, now, model, int(bool(incognito)), code_project_id or None, now, parent))
        self.store.event("sessions", ident)
        return ident

    def route(self, *, code_project_id=None, incognito=None, fresh=False, now=None):
        """The stretch a new message from the user belongs to; a new one after a pause or when its settings differ.

        code_project_id: None keeps what the stretch has, "" means no code project. incognito: None keeps what it has.
        """
        now = now or time.time()
        here = None if fresh else self.current(now) or self.answered(now)
        if here and code_project_id is not None and (code_project_id or None) != here["code_project_id"]:
            here = None
        if here and incognito is not None and int(incognito) != here["incognito"]:
            here = None
        if here:
            self.focus(here["id"], now)
            return here["id"]
        last = self.store.one("SELECT model FROM sessions ORDER BY updated_at DESC LIMIT 1") or {}
        return self.open(model=last.get("model"), incognito=bool(incognito), code_project_id=code_project_id, now=now)

    def split(self, p):
        """Moves the message being answered (job payload p) into a new stretch: the user changed the subject."""
        old = self.store.one("SELECT model,incognito FROM sessions WHERE id=?", (p["session_id"],)) or {}
        asked = self.store.one("SELECT id,content FROM messages WHERE session_id=? AND role='user' ORDER BY created_at DESC LIMIT 1", (p["session_id"],))
        if not asked:
            return p["session_id"]
        ident = self.open(title=asked["content"][:40], model=old.get("model"), incognito=old.get("incognito"), parent=p["session_id"])
        return self.move(p, ident)

    def move(self, p, target):
        """Moves the message being answered (job payload p) into another stretch, which the user is in from now on."""
        asked = self.store.one("SELECT id FROM messages WHERE session_id=? AND role='user' ORDER BY created_at DESC LIMIT 1", (p["session_id"],))
        if not asked or target == p["session_id"]:
            return p["session_id"]
        self.store.execute("UPDATE messages SET session_id=? WHERE id IN (?,?)", (target, asked["id"], p["message_id"]))
        self.store.execute("UPDATE jobs SET payload=json_set(payload,'$.session_id',?) WHERE json_extract(payload,'$.message_id')=?", (target, p["message_id"]))
        self.store.execute("UPDATE sessions SET updated_at=? WHERE id=?", (time.time(), target))
        self.focus(target)
        self.store.event("messages", target)
        p["session_id"] = target
        return target

    # ---- reading the stream ----
    def page(self, *, before=None, after=None, start=None, at=None, limit=PAGE):
        """One page of the stream, oldest first. With nothing given: the newest page.

        before/after are message times; start is a stretch to begin at; at is a message to open at, with a few before it.
        """
        limit = max(1, min(int(limit), 400))
        if start:
            first = self.store.one("SELECT MIN(created_at) AS at FROM messages WHERE session_id=?", (start,))["at"]
            after = (first or 0) - 1e-6
        elif at:
            found = self.store.one("SELECT created_at FROM messages WHERE id=?", (at,))
            if found:
                lead = self.store.all("SELECT created_at FROM messages WHERE created_at<? ORDER BY created_at DESC LIMIT ?", (found["created_at"], LEAD))
                after = (lead[-1]["created_at"] if lead else found["created_at"]) - 1e-6
        if after is not None:
            rows = self.store.all("SELECT * FROM messages WHERE created_at>? ORDER BY created_at LIMIT ?", (after, limit + 1))
            later, rows = len(rows) > limit, rows[:limit]
            earlier = bool(self.store.one("SELECT 1 AS x FROM messages WHERE created_at<=? LIMIT 1", (after,)))
        else:
            rows = self.store.all("SELECT * FROM messages WHERE created_at<? ORDER BY created_at DESC LIMIT ?", (before if before is not None else 1e18, limit + 1))
            earlier, rows = len(rows) > limit, rows[:limit][::-1]
            later = before is not None and bool(self.store.one("SELECT 1 AS x FROM messages WHERE created_at>=? LIMIT 1", (before,)))
        here = self.current() or self.answered()
        return {"messages": rows, "earlier": earlier, "later": later, "current": here["id"] if here else None}

    def search(self, query, limit=30):
        """Messages that contain the words, newest first, each with a little of what surrounds the match."""
        query = query.strip()
        if not query:
            return []
        like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
        rows = self.store.all("SELECT m.id,m.session_id,m.role,m.content,m.created_at,s.title FROM messages m JOIN sessions s ON s.id=m.session_id "
                              "WHERE m.status='complete' AND m.role IN ('user','assistant') AND m.content LIKE ? ESCAPE '\\' ORDER BY m.created_at DESC LIMIT ?",
                              (like, max(1, min(int(limit), 100))))
        found = []
        for row in rows:
            text = " ".join(row["content"].split())
            spot = max(0, text.lower().find(query.lower()))
            lead = max(0, spot - 30)
            found.append({"id": row["id"], "session_id": row["session_id"], "role": row["role"], "title": row["title"], "created_at": row["created_at"],
                          "snippet": ("…" if lead else "") + text[lead:spot + len(query) + 60] + ("…" if spot + len(query) + 60 < len(text) else "")})
        return found
