"""What the assistant did and what it said to itself while doing it.

Two streams in one table, read side by side on a timeline:
- "work" is written by the harness: every tool call and every time the harness had to step in. The model has no say in it.
- "mind" is written by the model: the sentence it says before reaching for a tool, and notes it jots down when something is off.

A row may name an owner: who would have to change for this not to happen again. Those rows are the improvement list.
Nothing from a private (incognito) conversation is written.
"""
import time
from datetime import datetime, timedelta

from .store import dumps

KEEP_DAYS = 90
OWNERS = ("user", "model", "jurvis", "external")
# What a tool reported, in the model's words, when the fault is not the tool's.
_MODEL_FAULT = ("参数无效", "参数格式错误", "未知工具", "没有名为", "not available in plan mode", "Same call already", "is only for goal mode",
                "不存在", "找不到", "没有找到", "not found", "No such file", "不读取本机")
_EXTERNAL = {"web_fetch", "web_search", "mail_recent", "mail_read", "mail_send", "phone_call", "weather", "crypto_price", "convert_currency",
             "home_devices", "home_control"}
# Figures that stay on this Mac: the journal keeps that the call happened, not what it returned.
PRIVATE = {"ledger_transactions", "spending_summary", "log_expense", "set_budget", "health_summary", "cycle_status", "log_period",
           "end_period", "log_mood", "mail_recent", "mail_read", "recent_conversations", "mail_send", "calendar_events", "reminders_list", "find_contact",
           "add_event", "add_reminder"}
MOODS = {"say": "thinking", "thought": "calm", "doubt": "troubled", "friction": "troubled", "slip": "guilty", "glad": "happy"}


def day_bounds(day=None):
    start = datetime.strptime(day, "%Y-%m-%d") if day else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
    return start.timestamp(), (start + timedelta(days=1)).timestamp()


class Journal:
    def __init__(self, store):
        self.store = store
        self.trimmed = 0.0

    def write(self, stream, kind, text, *, owner="", name="", status="", effect="", origin="chat", session_id=None, message_id=None,
              job_id=None, detail=None):
        text = " ".join(str(text or "").split())[:600]
        if not text:
            return None
        with self.store.connection() as db:
            ident = db.execute("INSERT INTO journal(at,stream,kind,owner,text,name,status,effect,origin,session_id,message_id,job_id,extras) "
                               "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                               (time.time(), stream, kind, owner if owner in OWNERS else "", text, name, status, effect, origin,
                                session_id, message_id, job_id, dumps(detail or {}))).lastrowid
        self.store.event("journal", str(ident))
        if time.time() - self.trimmed > 3600:
            self.trimmed = time.time()
            self.store.execute("DELETE FROM journal WHERE at<?", (time.time() - KEEP_DAYS * 86400,))
        return ident

    @staticmethod
    def blame(name, effect, result, dynamic=False):
        """Who a failed tool call points at: the model (it called the tool wrongly), the outside world, or Jurvis itself."""
        error = str((result or {}).get("error") or "")
        if any(mark in error for mark in _MODEL_FAULT):
            return "model"
        if dynamic or name in _EXTERNAL or name.startswith("browser_") or effect in ("external", "mail"):
            return "external"
        return "jurvis"

    # ---- reading ----
    def day(self, day=None):
        """One day as a work record: the tasks (one per reply), what was said in passing, and what should be better."""
        start, end = day_bounds(day)
        rows = self.store.all("SELECT * FROM journal WHERE at>=? AND at<? ORDER BY id", (start, end))
        tasks, order = {}, []
        for row in rows:
            key = row["message_id"] or f"job:{row['job_id']}" if row["message_id"] or row["job_id"] else f"row:{row['id']}"
            if key not in tasks:
                tasks[key] = {"key": key, "session_id": row["session_id"], "message_id": row["message_id"], "origin": row["origin"],
                              "started_at": row["at"], "ended_at": row["at"], "work": [], "mind": [], "acted": 0, "failed": 0, "corrected": 0}
                order.append(key)
            task = tasks[key]
            task["ended_at"] = row["at"]
            task[row["stream"]].append(row)
            if row["stream"] == "work" and row["kind"] == "tool":
                task["acted"] += row["status"] == "ok" and row["effect"] not in ("read", "")
                task["failed"] += row["status"] == "error"
            task["corrected"] += row["kind"] == "harness" and bool(row["owner"])
        asked = {}
        for task in tasks.values():
            if task["message_id"] and task["session_id"]:
                # the request the reply answers: the user's message just before it
                row = self.store.one("SELECT content FROM messages WHERE session_id=? AND role='user' AND created_at<=(SELECT created_at FROM messages WHERE id=?) "
                                     "ORDER BY created_at DESC LIMIT 1", (task["session_id"], task["message_id"]))
                asked[task["key"]] = (row or {}).get("content", "")[:120]
        listed = [{**tasks[k], "title": asked.get(k, "")} for k in order]
        work = [r for r in rows if r["stream"] == "work" and r["kind"] == "tool"]
        return {"day": datetime.fromtimestamp(start).strftime("%Y-%m-%d"), "tasks": listed,
                "totals": {"tasks": len(listed), "calls": len(work), "acted": sum(t["acted"] for t in listed),
                           "failed": sum(t["failed"] for t in listed), "corrected": sum(t["corrected"] for t in listed)},
                "days": [r["day"] for r in self.store.all(
                    "SELECT DISTINCT date(at,'unixepoch','localtime') AS day FROM journal ORDER BY day DESC LIMIT 60")]}

    def mind(self, after=0, limit=30):
        """The newest things it said to itself, oldest first: what the bubbles show."""
        rows = self.store.all("SELECT * FROM journal WHERE stream='mind' AND id>? ORDER BY id DESC LIMIT ?", (after, limit))
        return [{**row, "mood": MOODS.get(row["kind"], "calm")} for row in rows[::-1]]

    def improvements(self, days=30):
        """What keeps going wrong, grouped by who would have to change. The same thing many times is one line with a count."""
        rows = self.store.all("SELECT * FROM journal WHERE owner!='' AND at>=? ORDER BY id DESC", (time.time() - days * 86400,))
        groups = {}
        for row in rows:
            what = row["name"] or row["text"][:40]
            key = (row["owner"], row["kind"], what) if row["stream"] == "work" else (row["owner"], "note", row["id"])
            item = groups.setdefault(key, {"owner": row["owner"], "kind": row["kind"], "stream": row["stream"], "text": row["text"], "name": row["name"],
                                           "count": 0, "last_at": row["at"], "id": row["id"], "session_id": row["session_id"], "message_id": row["message_id"]})
            item["count"] += 1
        out = {owner: [] for owner in OWNERS}
        for item in groups.values():
            out[item["owner"]].append(item)
        for items in out.values():
            items.sort(key=lambda i: (-i["count"], -i["last_at"]))
        return out

    def digest(self, days=7, limit=60):
        """The journal as the assistant reads it back when the user asks what troubles it: its own notes in full, the work as
        counts and labels only. What a tool returned is never in here, so figures that stay on this Mac do not travel with it."""
        since = time.time() - days * 86400
        when = lambda at: datetime.fromtimestamp(at).strftime("%m-%d %H:%M")
        notes = self.store.all("SELECT * FROM journal WHERE at>=? AND (owner!='' OR (stream='mind' AND kind!='say')) ORDER BY id DESC LIMIT ?", (since, limit))
        lines = [f"#{r['id']} [{when(r['at'])}] {r['kind']}" + (f" (owner: {r['owner']})" if r["owner"] else "") + f": {r['text']}" for r in notes[::-1]]
        counts = self.store.all("SELECT name,status,COUNT(*) AS n FROM journal WHERE stream='work' AND kind='tool' AND at>=? GROUP BY name,status ORDER BY n DESC LIMIT 40", (since,))
        work = ", ".join(f"{c['name']} {c['status']}×{c['n']}" for c in counts)
        return {"notes": "\n".join(lines) or "(nothing noted in this period)", "work": work or "(no tool calls in this period)"}
