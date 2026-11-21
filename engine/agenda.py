"""What the assistant keeps doing for the user over time: goals it tracks and work it runs on a schedule.

A schedule is a brief plus a rule for when to run it. When it comes due, the worker starts it as a
background task in the conversation it was set up in, and the result is posted there.
"""
import calendar
import difflib
import time
from datetime import datetime, timedelta

from .store import dumps, new_id

WEEKDAYS = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]
ACTIVE_SCHEDULES = 20        # schedules a user may have running; each one costs model time on its own


def parse_time(text):
    try:
        hour, minute = (int(part) for part in str(text).strip().replace("：", ":").split(":"))
    except ValueError:
        raise ValueError("time must be HH:MM in 24-hour form, e.g. 09:00")
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise ValueError("time must be HH:MM in 24-hour form, e.g. 09:00")
    return hour, minute


def make_rule(every, at="09:00", weekday="", day=0, date=""):
    """A validated rule: {"every": once|day|week|month, "at": "HH:MM"} plus the day it falls on."""
    hour, minute = parse_time(at)
    rule = {"every": every, "at": f"{hour:02d}:{minute:02d}"}
    if every == "week":
        if weekday not in WEEKDAYS:
            raise ValueError("a weekly schedule needs weekday: one of " + ", ".join(WEEKDAYS))
        rule["weekday"] = weekday
    elif every == "month":
        if not 1 <= int(day or 0) <= 31:
            raise ValueError("a monthly schedule needs day: the day of the month, 1 to 31")
        rule["day"] = int(day)
    elif every == "once":
        try:
            rule["date"] = datetime.strptime(str(date).strip(), "%Y-%m-%d").strftime("%Y-%m-%d")
        except ValueError:
            raise ValueError("a one-time schedule needs date as YYYY-MM-DD")
    elif every != "day":
        raise ValueError("every must be once, day, week or month")
    return rule


def next_run(rule, after):
    """The first time the rule falls strictly after `after` (a local datetime), or None when a one-time rule has passed."""
    hour, minute = parse_time(rule["at"])
    start = after.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if rule["every"] == "once":
        when = datetime.strptime(rule["date"], "%Y-%m-%d").replace(hour=hour, minute=minute)
        return when if when > after else None
    if rule["every"] == "day":
        return start if start > after else start + timedelta(days=1)
    if rule["every"] == "week":
        for ahead in range(8):
            when = start + timedelta(days=ahead)
            if when.weekday() == WEEKDAYS.index(rule["weekday"]) and when > after:
                return when
    year, month = after.year, after.month
    for _ in range(14):
        # A day the month does not have (the 31st in April) falls on its last day.
        when = start.replace(year=year, month=month, day=min(rule["day"], calendar.monthrange(year, month)[1]))
        if when > after:
            return when
        year, month = (year + 1, 1) if month == 12 else (year, month + 1)
    return None


def describe(rule):
    """The rule in the user's words: 每月 1 号 09:00."""
    if rule["every"] == "once":
        return f"{rule['date']} {rule['at']}"
    if rule["every"] == "day":
        return f"每天 {rule['at']}"
    if rule["every"] == "week":
        return f"每{WEEKDAY_NAMES[WEEKDAYS.index(rule['weekday'])]} {rule['at']}"
    return f"每月 {rule['day']} 号 {rule['at']}"


def stamp(when):
    return f"{when:%Y-%m-%d %H:%M} {WEEKDAY_NAMES[when.weekday()]}"


class Agenda:
    def __init__(self, store):
        self.store = store

    # ---- goals ----
    def goals(self, active=True):
        return self.store.all("SELECT * FROM goals" + (" WHERE status='active'" if active else "") + " ORDER BY created_at DESC LIMIT 100")

    def track(self, session_id, title, detail=""):
        """Saves a goal, or updates the one that is already about the same thing."""
        title, detail = title.strip()[:80], detail.strip()[:1000]
        now = time.time()
        for goal in self.goals():
            if difflib.SequenceMatcher(None, goal["title"].casefold(), title.casefold()).ratio() >= .8:
                self.store.execute("UPDATE goals SET detail=?,updated_at=? WHERE id=?", (detail or goal["detail"], now, goal["id"]))
                self.store.event("goals", goal["id"])
                return {**goal, "detail": detail or goal["detail"], "existing": True}
        ident = new_id()
        self.store.execute("INSERT INTO goals(id,title,detail,status,session_id,created_at,updated_at) VALUES(?,?,?,'active',?,?,?)",
                           (ident, title, detail, session_id, now, now))
        self.store.event("goals", ident)
        return {**self.store.one("SELECT * FROM goals WHERE id=?", (ident,)), "existing": False}

    def remove_goal(self, ident):
        self.store.execute("DELETE FROM goals WHERE id=?", (ident,))
        self.store.event("goals", ident)

    # ---- schedules ----
    def schedules(self, active=True):
        rows = self.store.all("SELECT * FROM schedules" + (" WHERE enabled=1" if active else "") + " ORDER BY created_at DESC LIMIT 100")
        return [{**row, "when": describe(row["rule"])} for row in rows]

    def schedule(self, session_id, title, brief, rule, now=None):
        now = now or datetime.now()
        first = next_run(rule, now)
        if first is None:
            raise ValueError(f"that time has already passed (it is {stamp(now)} now)")
        title = title.strip()[:80]
        if any(s["title"] == title for s in self.schedules()):
            raise ValueError("a schedule with this title already exists; cancel it first to change it")
        if len(self.schedules()) >= ACTIVE_SCHEDULES:
            raise ValueError(f"there are already {ACTIVE_SCHEDULES} active schedules; cancel one first")
        ident, created = new_id(), time.time()
        self.store.execute("INSERT INTO schedules(id,title,brief,rule,next_run,session_id,enabled,created_at) VALUES(?,?,?,?,?,?,1,?)",
                           (ident, title, brief.strip(), dumps(rule), first.timestamp(), session_id, created))
        self.store.event("schedules", ident)
        return {"id": ident, "title": title, "when": describe(rule), "next": stamp(first)}

    def cancel(self, ident):
        row = self.store.one("SELECT title FROM schedules WHERE id=?", (ident,))
        if not row:
            raise ValueError("no schedule with this id; list them with agenda")
        self.store.execute("DELETE FROM schedules WHERE id=?", (ident,))
        self.store.event("schedules", ident)
        return {"cancelled": row["title"]}

    def overview(self):
        return {"goals": [{"id": g["id"], "title": g["title"], "detail": g["detail"]} for g in self.goals()],
                "schedules": [{"id": s["id"], "title": s["title"], "when": s["when"], "next": stamp(datetime.fromtimestamp(s["next_run"]))} for s in self.schedules()]}

    def fire(self, now=None):
        """Starts every schedule that has come due as a background task and moves it to its next time. Returns the jobs it queued.
        A schedule whose time passed while the engine was off runs once, not once per missed time."""
        now = now or datetime.now()
        started = []
        for row in self.store.all("SELECT * FROM schedules WHERE enabled=1 AND next_run<=? ORDER BY next_run", (now.timestamp(),)):
            upcoming = next_run(row["rule"], now)
            self.store.execute("UPDATE schedules SET last_run=?,next_run=?,enabled=? WHERE id=?",
                               (now.timestamp(), upcoming.timestamp() if upcoming else row["next_run"], 1 if upcoming else 0, row["id"]))
            self.store.event("schedules", row["id"])
            if self.store.one("SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.schedule_id')=?", (row["id"],)):
                continue  # the previous run is still going: this time is skipped rather than piled on top of it
            session = row["session_id"]
            if not session or not self.store.one("SELECT id FROM sessions WHERE id=?", (session,)):
                # The conversation it was set up in is gone: it gets one of its own.
                session, created = new_id(), time.time()
                self.store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES(?,?,?,?)", (session, row["title"][:40], created, created))
                self.store.execute("UPDATE schedules SET session_id=? WHERE id=?", (session, row["id"]))
                self.store.event("sessions", session)
            task = {"title": row["title"], "brief": row["brief"], "scheduled": describe(row["rule"])}
            message = self.store.add_message(session, "assistant", "", "queued", {"task": {"title": row["title"], "scheduled": task["scheduled"]}})
            started.append(self.store.create_job("chat", {"session_id": session, "message_id": message, "task": task, "background": True, "schedule_id": row["id"]}))
        return started
