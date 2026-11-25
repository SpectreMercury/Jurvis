"""Work that starts when something happens, not at a time: an email arrives, an event is about to begin, a budget is used up,
a device at home changes.

A watch is a brief plus what to wait for. The engine looks every minute or few (nothing here uses a model); when the thing
happens, the brief runs as a background task in the conversation the watch was set up in, with what happened added to it,
and the result is posted there and announced like any other background result.

- The first look only takes note of how things are: a watch never fires for mail that was already there or a door already open.
- A watch whose last run is still going does not start another; mail that arrives in a burst is one run, not one per message.
- Mail is read with the rights of the conversation's model: a mailbox kept for the local model does not feed a watch whose
  task a cloud model would do.
"""
import asyncio
import json
import logging
import time
from datetime import date, datetime

from .ledger import CATEGORIES, TOTAL
from .personal import clock
from .store import dumps, new_id

log = logging.getLogger("jurvis.watch")

ACTIVE_WATCHES = 20
EVERY = {"mail": 300, "calendar": 60, "budget": 600, "device": 120}   # seconds between looks
GAP = {"mail": 600, "calendar": 0, "budget": 0, "device": 300}        # at least this long between two runs of one watch
SEEN = 300               # mail ids remembered per watch


def describe(kind, params):
    """What a watch waits for, in the user's words."""
    if kind == "mail":
        who = f"来自 {params['sender']} " if params.get("sender") else ""
        about = f"提到「{params['words']}」" if params.get("words") else ""
        return f"收到{who}{about}的邮件时" if who or about else "收到新邮件时"
    if kind == "calendar":
        about = f"「{params['words']}」" if params.get("words") else ""
        return f"日程{about}开始前 {params['minutes']} 分钟"
    if kind == "budget":
        return f"本月{'' if params['category'] == TOTAL else params['category']}支出达到预算的 {params['percent']}% 时"
    return f"{params.get('name') or params['device_id']} 的 {params['key']} 变为 {params['value']} 时"


class Watches:
    def __init__(self, store, mail, personal, ledger, cloud_of):
        self.store, self.mail, self.personal, self.ledger, self.cloud_of = store, mail, personal, ledger, cloud_of
        self.home = None   # attached by the worker
        self.looked = {}   # watch id -> when it was last looked at
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS watches(
                id TEXT PRIMARY KEY,title TEXT NOT NULL,brief TEXT NOT NULL,kind TEXT NOT NULL,params TEXT NOT NULL,session_id TEXT,
                enabled INTEGER NOT NULL DEFAULT 1,state TEXT NOT NULL DEFAULT '{}',last_fired REAL,fired INTEGER NOT NULL DEFAULT 0,created_at REAL NOT NULL)""")

    def rows(self):
        rows = self.store.all("SELECT * FROM watches WHERE enabled=1 ORDER BY created_at DESC LIMIT 100")
        for row in rows:
            row["params"], row["state"] = json.loads(row["params"]), json.loads(row["state"])
            row["when"] = describe(row["kind"], row["params"])
        return rows

    def list(self):
        return [{k: row[k] for k in ("id", "title", "brief", "kind", "when", "last_fired", "fired", "created_at", "session_id")} for row in self.rows()]

    async def create(self, session_id, title, brief, kind, *, sender="", words="", minutes=30, category="", percent=100, device_id="", key="", value=""):
        title = title.strip()[:80]
        rows = self.rows()
        if any(r["title"] == title for r in rows):
            raise ValueError("a watch with this title already exists; cancel it first to change it")
        if len(rows) >= ACTIVE_WATCHES:
            raise ValueError(f"there are already {ACTIVE_WATCHES} watches; cancel one first")
        if kind == "mail":
            if not self.mail.accounts(self.cloud_of(session_id)):
                raise ValueError("no mailbox is connected for this conversation's model; the user connects one, or allows cloud models for it, under 设置 → 邮件")
            params = {"sender": sender.strip()[:120], "words": " ".join(words.split())[:120]}
        elif kind == "calendar":
            if not self.personal.open("calendar", False):
                raise ValueError("the user's calendar is not shared with Jurvis yet; they switch it on in the Jurvis app on their phone or Mac (设置 → 日历与通讯录)")
            params = {"minutes": max(5, min(int(minutes), 24 * 60)), "words": " ".join(words.split())[:120]}
        elif kind == "budget":
            category = category.strip() or TOTAL
            if category != TOTAL and category not in CATEGORIES:
                raise ValueError("category must be 总计 or one of " + ", ".join(CATEGORIES))
            if category not in self.ledger.budgets():
                raise ValueError(f"there is no monthly budget for {category} yet; set one with set_budget first")
            params = {"category": category, "percent": max(10, min(int(percent), 300))}
        elif kind == "device":
            if not self.home or not (device_id.strip() and key.strip() and str(value).strip()):
                raise ValueError("a device watch needs device_id (from home_devices), key (a field of its state, or online) and value")
            device = await self.home.find(device_id.strip())
            if key.strip() != "online" and key.strip() not in device["state"]:
                raise ValueError(f"{device['name']} has no state field {key}; it has: {', '.join(device['state']) or 'none'}, and online")
            params = {"device_id": device["id"], "name": device["name"], "key": key.strip(), "value": str(value).strip()[:60]}
        else:
            raise ValueError("when must be mail, calendar, budget or device")
        ident = new_id()
        self.store.execute("INSERT INTO watches(id,title,brief,kind,params,session_id,created_at) VALUES(?,?,?,?,?,?,?)",
                           (ident, title, brief.strip(), kind, dumps(params), session_id, time.time()))
        self.store.event("watches", ident)
        return {"id": ident, "title": title, "when": describe(kind, params)}

    def cancel(self, ident):
        row = self.store.one("SELECT title FROM watches WHERE id=?", (ident,))
        if not row:
            raise ValueError("no watch with this id; list them with agenda")
        self.store.execute("DELETE FROM watches WHERE id=?", (ident,))
        self.store.event("watches", ident)
        return {"cancelled": row["title"]}

    # ---- looking ----
    async def tick(self, now=None):
        """Looks at every watch that is due for a look. Returns the jobs it started."""
        now = now or time.time()
        started = []
        for row in self.rows():
            if now - self.looked.get(row["id"], 0) < EVERY[row["kind"]]:
                continue
            self.looked[row["id"]] = now
            try:
                state, happened = await getattr(self, "look_" + row["kind"])(row, now)
            except Exception as exc:  # one mailbox or hub that is down must not stop the others
                log.warning("watch %s could not be looked at: %s", row["title"], exc)
                continue
            if state != row["state"]:
                self.store.execute("UPDATE watches SET state=? WHERE id=?", (dumps(state), row["id"]))
            if happened and now - (row["last_fired"] or 0) >= GAP[row["kind"]]:
                job = self.fire(row, happened, now)
                if job:
                    started.append(job)
        return started

    def fire(self, row, happened, now):
        if self.store.one("SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.watch_id')=?", (row["id"],)):
            return None
        session = row["session_id"]
        if not session or not self.store.one("SELECT id FROM sessions WHERE id=?", (session,)):
            session, created = new_id(), time.time()
            self.store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES(?,?,?,?)", (session, row["title"][:40], created, created))
            self.store.execute("UPDATE watches SET session_id=? WHERE id=?", (session, row["id"]))
            self.store.event("sessions", session)
        self.store.execute("UPDATE watches SET last_fired=?,fired=fired+1 WHERE id=?", (now, row["id"]))
        self.store.event("watches", row["id"])
        task = {"title": row["title"], "brief": row["brief"] + "\n\nWhat just happened:\n" + happened, "watched": row["when"]}
        message = self.store.add_message(session, "assistant", "", "queued", {"task": {"title": row["title"], "watched": row["when"]}})
        return self.store.create_job("chat", {"session_id": session, "message_id": message, "task": task, "background": True, "watch_id": row["id"]})

    async def look_mail(self, row, now):
        params, state = row["params"], dict(row["state"])
        found = await asyncio.to_thread(self.mail.recent, 1, "", False, 30, self.cloud_of(row["session_id"]))
        messages = found["messages"]
        seen = state.get("seen")
        state["seen"] = (list(dict.fromkeys([m["id"] for m in messages] + (seen or []))))[:SEEN]
        if seen is None:
            return state, ""  # the first look: what is already in the inbox is not news
        sender, words = params["sender"].casefold(), [w.casefold() for w in params["words"].split()]
        fresh = [m for m in messages if m["id"] not in seen
                 and (not sender or sender in (m["from"] + " " + m.get("from_address", "")).casefold())
                 and (not words or any(w in (m["subject"] + " " + m["preview"]).casefold() for w in words))]
        if not fresh:
            return state, ""
        lines = [f"- {m['time']} from {m['from']} <{m.get('from_address', '')}>: {m['subject']} — {m['preview'][:160]} (id {m['id']})" for m in fresh[:5]]
        return state, "New email arrived:\n" + "\n".join(lines) + (f"\n(and {len(fresh) - 5} more)" if len(fresh) > 5 else "") + "\nRead one in full with mail_read when the brief needs it."

    async def look_calendar(self, row, now):
        params, state = row["params"], dict(row["state"])
        words = [w.casefold() for w in params["words"].split()]
        told = {k: v for k, v in (state.get("told") or {}).items() if v > now - 2 * 86400}
        due = []
        for event in self.personal.between(now, now + params["minutes"] * 60):
            key = f"{event['id']}@{int(event['start'])}"
            if key in told or (words and not any(w in (event["title"] + " " + event["place"] + " " + event["notes"]).casefold() for w in words)):
                continue
            told[key] = now
            due.append(event)
        state["told"] = told
        if not due:
            return state, ""
        return state, "On the user's calendar, about to begin:\n" + "\n".join(
            f"- {e['title']} at {clock(e['start'])}" + (f", {e['place']}" if e["place"] else "") + (f" — {e['notes'][:200]}" if e["notes"] else "") for e in due[:3])

    async def look_budget(self, row, now):
        params, state = row["params"], dict(row["state"])
        month = date.fromtimestamp(now).strftime("%Y-%m")
        summary = self.ledger.summary("this_month", "" if params["category"] == TOTAL else params["category"], today=date.fromtimestamp(now))
        budget = summary.get("budget")
        if not budget or state.get("month") == month or budget["used_percent"] < params["percent"]:
            return state, ""
        state["month"] = month  # once a month
        return state, (f"This month's {'total' if params['category'] == TOTAL else params['category']} spending is ¥{summary['spent']:,.2f}, "
                       f"{budget['used_percent']:g}% of the ¥{budget['limit']:,.2f} budget (¥{budget['left']:,.2f} left).")

    async def look_device(self, row, now):
        params, state = row["params"], dict(row["state"])
        devices = await self.home.devices(refresh=True)
        device = next((d for d in devices if d["id"] == params["device_id"]), None)
        if not device:
            return state, ""  # its hub did not answer this time: neither a change nor the absence of one
        current = device["online"] if params["key"] == "online" else device["state"].get(params["key"])
        text = ("true" if current else "false") if isinstance(current, bool) else str(current)
        matches = text.casefold() == params["value"].casefold() or (isinstance(current, bool) and params["value"].casefold() in (("on", "yes", "1") if current else ("off", "no", "0")))
        before = state.get("on")
        state["on"] = matches
        if before is None or before or not matches:
            return state, ""  # the first look, or no change into the watched state
        return state, f"{device['name']}" + (f" ({device['room']})" if device["room"] else "") + f": {params['key']} is now {text} (at {datetime.fromtimestamp(now):%H:%M})."
