"""The user's calendar, reminders and contacts, as their own devices see them.

The engine has no access to these itself. The Jurvis app on the iPhone, iPad or Mac reads them with the user's permission
(each kind is switched on separately in the app) and sends a copy here: events from a week back to two months ahead, open
reminders, and the names, numbers and addresses of contacts. Each copy replaces the one before it.

- Read by the user's devices and by the model that may see them: by default only the local model; each kind can be opened
  to cloud models in 设置, like a mailbox.
- Contacts are never listed: a model looks one person up by name and gets at most a few matches.
- New events and reminders the assistant makes wait in an outbox; the first app to claim one adds it to the system
  calendar or reminders, and the user's own account carries it to their other devices. Nothing is changed or deleted.
"""
import json
import time
from datetime import datetime, timedelta

from .store import dumps, new_id

KINDS = {"calendar": "日历", "reminders": "提醒事项", "contacts": "通讯录"}
CLAIM_SECONDS = 120      # a device that took an entry and never said it was added: another may take it after this
STALE_HOURS = 36         # a copy older than this is said to be old when a model reads it
MAX = {"calendar": 2000, "reminders": 1000, "contacts": 5000}
WEEKDAYS = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def text(value, limit):
    return " ".join(str(value or "").split())[:limit]


def moment(value):
    """A time from a device: seconds since 1970, or None."""
    try:
        return float(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def clock(at):
    when = datetime.fromtimestamp(at)
    return f"{when:%Y-%m-%d} {WEEKDAYS[when.weekday()]} {when:%H:%M}"


def local_time(value):
    """ "2026-11-28T19:00" on this Mac's clock as a moment in time."""
    try:
        return datetime.strptime(str(value).strip().replace(" ", "T")[:16], "%Y-%m-%dT%H:%M")
    except ValueError:
        raise ValueError("time must be YYYY-MM-DDTHH:MM, e.g. 2026-11-28T19:00")


class Personal:
    def __init__(self, settings, store):
        self.store = store
        self.path = settings.data / "personal.json"
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS cal_events(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,start REAL NOT NULL,finish REAL NOT NULL,all_day INTEGER NOT NULL DEFAULT 0,
                    place TEXT NOT NULL DEFAULT '',calendar TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '');
                CREATE INDEX IF NOT EXISTS cal_events_start ON cal_events(start);
                CREATE TABLE IF NOT EXISTS reminders(
                    id TEXT PRIMARY KEY,title TEXT NOT NULL,due REAL,done INTEGER NOT NULL DEFAULT 0,list TEXT NOT NULL DEFAULT '',notes TEXT NOT NULL DEFAULT '');
                CREATE TABLE IF NOT EXISTS contacts(
                    id TEXT PRIMARY KEY,name TEXT NOT NULL,org TEXT NOT NULL DEFAULT '',phones TEXT NOT NULL DEFAULT '[]',emails TEXT NOT NULL DEFAULT '[]');
                CREATE TABLE IF NOT EXISTS personal_outbox(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,entry TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',device TEXT,claimed_at REAL,created_at REAL NOT NULL);
            """)

    # ---- settings ----
    def load(self):
        try:
            data = json.loads(self.path.read_text())
        except (OSError, ValueError):
            data = {}
        return {"cloud": {k: bool((data.get("cloud") or {}).get(k)) for k in KINDS}, "synced": data.get("synced") or {}}

    def save(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def status(self):
        data = self.load()
        counts = {"calendar": "cal_events", "reminders": "reminders", "contacts": "contacts"}
        return {"kinds": [{"kind": k, "name": name, "cloud": data["cloud"][k], "synced_at": (data["synced"].get(k) or {}).get("at"),
                           "device": (data["synced"].get(k) or {}).get("device", ""),
                           "count": self.store.one(f"SELECT COUNT(*) AS n FROM {counts[k]}")["n"]} for k, name in KINDS.items()]}

    def share(self, kind, cloud):
        if kind not in KINDS:
            raise ValueError("没有这一类数据")
        data = self.load()
        data["cloud"][kind] = bool(cloud)
        self.save(data)
        return self.status()

    def forget(self, kind):
        """The copy of one kind is dropped (the user switched it off on their devices, or wants it gone from the Mac)."""
        if kind not in KINDS:
            raise ValueError("没有这一类数据")
        self.store.execute("DELETE FROM " + {"calendar": "cal_events", "reminders": "reminders", "contacts": "contacts"}[kind])
        data = self.load()
        data["synced"].pop(kind, None)
        data["cloud"][kind] = False
        self.save(data)
        self.store.event("personal", kind)
        return self.status()

    def open(self, kind, cloud):
        """True when there is a copy of this kind and the asking model may read it."""
        data = self.load()
        return kind in data["synced"] and (not cloud or data["cloud"][kind])

    # ---- a copy arrives from a device ----
    def sync(self, body, device=""):
        """Replaces the copy of every kind the device sent. Returns how many of each were kept."""
        data, kept = self.load(), {}
        with self.store.connection() as db:
            if isinstance(body.get("events"), list):
                db.execute("DELETE FROM cal_events")
                rows = []
                for item in body["events"][:MAX["calendar"]]:
                    start, finish = moment(item.get("start")), moment(item.get("end"))
                    if not item.get("id") or start is None:
                        continue
                    rows.append((str(item["id"])[:200], text(item.get("title"), 200) or "(无标题)", start, finish if finish and finish >= start else start, int(bool(item.get("all_day"))),
                                 text(item.get("place"), 200), text(item.get("calendar"), 80), text(item.get("notes"), 500)))
                db.executemany("INSERT OR REPLACE INTO cal_events(id,title,start,finish,all_day,place,calendar,notes) VALUES(?,?,?,?,?,?,?,?)", rows)
                kept["calendar"] = len(rows)
            if isinstance(body.get("reminders"), list):
                db.execute("DELETE FROM reminders")
                rows = [(str(item["id"])[:200], text(item.get("title"), 200) or "(无标题)", moment(item.get("due")), int(bool(item.get("done"))),
                         text(item.get("list"), 80), text(item.get("notes"), 500)) for item in body["reminders"][:MAX["reminders"]] if item.get("id")]
                db.executemany("INSERT OR REPLACE INTO reminders(id,title,due,done,list,notes) VALUES(?,?,?,?,?,?)", rows)
                kept["reminders"] = len(rows)
            if isinstance(body.get("contacts"), list):
                db.execute("DELETE FROM contacts")
                rows = []
                for item in body["contacts"][:MAX["contacts"]]:
                    name = text(item.get("name"), 120) or text(item.get("org"), 120)
                    if not item.get("id") or not name:
                        continue
                    rows.append((str(item["id"])[:200], name, text(item.get("org"), 120), dumps([text(p, 40) for p in (item.get("phones") or [])][:6]),
                                 dumps([text(e, 120) for e in (item.get("emails") or [])][:6])))
                db.executemany("INSERT OR REPLACE INTO contacts(id,name,org,phones,emails) VALUES(?,?,?,?,?)", rows)
                kept["contacts"] = len(rows)
        for kind, count in kept.items():
            data["synced"][kind] = {"at": time.time(), "device": device, "count": count}
        if kept:
            self.save(data)
            self.store.event("personal", "sync")
        return kept

    def note(self, kind):
        """What a model is told about how fresh the copy is."""
        at = (self.load()["synced"].get(kind) or {}).get("at") or 0
        if time.time() - at > STALE_HOURS * 3600:
            return f"This copy is from {clock(at)}; it is refreshed when the Jurvis app is opened on the user's phone or Mac. Say so if it matters."
        return ""

    # ---- reading ----
    @staticmethod
    def shown(row):
        start, finish = datetime.fromtimestamp(row["start"]), datetime.fromtimestamp(row["finish"])
        when = (f"{start:%Y-%m-%d} {WEEKDAYS[start.weekday()]} 全天" + (f" – {finish:%m-%d}" if (finish - start).days >= 1 and finish.date() != start.date() else "")
                if row["all_day"] else f"{clock(row['start'])}–{finish:%H:%M}" if finish.date() == start.date() else f"{clock(row['start'])} – {clock(row['finish'])}")
        return {"title": row["title"], "when": when, **({"place": row["place"]} if row["place"] else {}), **({"calendar": row["calendar"]} if row["calendar"] else {}),
                **({"notes": row["notes"][:200]} if row["notes"] else {})}

    def events(self, start="", days=7, query=""):
        """Events that overlap the days asked for, in order."""
        first = datetime.strptime(start.strip(), "%Y-%m-%d") if start.strip() else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        last = first + timedelta(days=max(1, min(int(days), 62)))
        rows = self.store.all("SELECT * FROM cal_events WHERE start<? AND finish>=? ORDER BY start LIMIT 200", (last.timestamp(), first.timestamp()))
        words = [w.casefold() for w in query.split() if w][:6]
        if words:
            rows = [r for r in rows if any(w in (r["title"] + " " + r["place"] + " " + r["notes"]).casefold() for w in words)]
        out = {"from": f"{first:%Y-%m-%d}", "to": f"{last - timedelta(days=1):%Y-%m-%d}", "events": [self.shown(r) for r in rows[:80]]}
        if len(rows) > 80:
            out["more"] = len(rows) - 80
        if self.note("calendar"):
            out["note"] = self.note("calendar")
        return out

    def upcoming(self, hours=24, limit=6, now=None):
        """What is on in the next hours, for the context of a reply: short, and nothing that has ended."""
        now = now or time.time()
        rows = self.store.all("SELECT * FROM cal_events WHERE finish>? AND start<? ORDER BY start LIMIT ?", (now, now + hours * 3600, limit))
        return [{k: v for k, v in self.shown(r).items() if k in ("title", "when", "place")} for r in rows]

    def between(self, first, last):
        """Raw events that start in a window (engine/watch.py)."""
        return self.store.all("SELECT * FROM cal_events WHERE start>=? AND start<? AND all_day=0 ORDER BY start", (first, last))

    def busy(self, start, minutes):
        """Events on the user's calendar that overlap a time, for a booking about to be made."""
        first = start.timestamp()
        rows = self.store.all("SELECT * FROM cal_events WHERE all_day=0 AND start<? AND finish>? ORDER BY start LIMIT 5", (first + minutes * 60, first))
        return [self.shown(r) for r in rows]

    def reminders(self, include_done=False, query=""):
        rows = self.store.all("SELECT * FROM reminders" + ("" if include_done else " WHERE done=0") + " ORDER BY due IS NULL,due LIMIT 300")
        words = [w.casefold() for w in query.split() if w][:6]
        if words:
            rows = [r for r in rows if any(w in (r["title"] + " " + r["notes"]).casefold() for w in words)]
        now = time.time()
        out = {"reminders": [{"title": r["title"], **({"due": clock(r["due"]), **({"overdue": True} if r["due"] < now and not r["done"] else {})} if r["due"] else {}),
                              **({"list": r["list"]} if r["list"] else {}), **({"done": True} if r["done"] else {}), **({"notes": r["notes"][:200]} if r["notes"] else {})}
                             for r in rows[:60]]}
        if self.note("reminders"):
            out["note"] = self.note("reminders")
        return out

    def contact(self, query):
        """The few people whose name, company, number or address matches. Never the whole book."""
        wanted = query.strip().casefold()
        if len(wanted) < 2 and not any("㐀" <= c <= "鿿" for c in wanted):
            raise ValueError("give at least two letters of the name")
        digits = "".join(c for c in wanted if c.isdigit())
        found = []
        for row in self.store.all("SELECT * FROM contacts"):
            phones, emails = json.loads(row["phones"]), json.loads(row["emails"])
            name = (row["name"] + " " + row["org"]).casefold()
            score = (3 if row["name"].casefold() == wanted else 2 if wanted in name or all(part in name for part in wanted.split()) else
                     1 if any(wanted in e.casefold() for e in emails) or (len(digits) >= 5 and any(digits in "".join(c for c in p if c.isdigit()) for p in phones)) else 0)
            if score:
                found.append((-score, row["name"], {"name": row["name"], **({"org": row["org"]} if row["org"] else {}), "phones": phones, "emails": emails}))
        found.sort(key=lambda item: item[:2])
        return {"query": query, "matches": [item[2] for item in found[:6]], **({"more": len(found) - 6} if len(found) > 6 else {})}

    # ---- writing: an outbox the devices empty ----
    def add_event(self, title, start, minutes=60, place="", notes=""):
        when = local_time(start)
        entry = {"title": text(title, 200), "start": f"{when:%Y-%m-%dT%H:%M}", "minutes": max(5, min(int(minutes), 24 * 60)), "place": text(place, 200), "notes": text(notes, 1000)}
        if not entry["title"]:
            raise ValueError("an event needs a title")
        clashes = self.busy(when, entry["minutes"]) if "calendar" in self.load()["synced"] else []
        ident = self.queue("event", entry)
        return {"queued": ident, **entry, "when": clock(when.timestamp()), **({"overlaps": clashes} if clashes else {}),
                "note": "Queued: the user's phone or Mac adds it to their calendar as soon as the Jurvis app is open there. Say it is being added, with its day and time"
                        + ("; it overlaps what is already on their calendar, so tell them." if clashes else ".")}

    def add_reminder(self, title, due="", notes=""):
        entry = {"title": text(title, 200), "due": f"{local_time(due):%Y-%m-%dT%H:%M}" if due.strip() else "", "notes": text(notes, 1000)}
        if not entry["title"]:
            raise ValueError("a reminder needs a title")
        ident = self.queue("reminder", entry)
        return {"queued": ident, **entry, "note": "Queued: the user's phone or Mac adds it to their reminders as soon as the Jurvis app is open there."}

    def queue(self, kind, entry):
        ident = new_id()
        self.store.execute("INSERT INTO personal_outbox(id,kind,entry,created_at) VALUES(?,?,?,?)", (ident, kind, dumps(entry), time.time()))
        self.store.event("personal", ident)
        return ident

    def outbox(self):
        """Entries no device has added yet. A week-old one is given up on: no device with access ever opened."""
        rows = self.store.all("SELECT id,kind,entry FROM personal_outbox WHERE created_at>? AND (status='pending' OR (status='claimed' AND claimed_at<?)) ORDER BY created_at",
                              (time.time() - 7 * 86400, time.time() - CLAIM_SECONDS))
        return [{"id": r["id"], "kind": r["kind"], **json.loads(r["entry"])} for r in rows]

    def claim(self, ident, device):
        with self.store.connection() as db:
            taken = db.execute("UPDATE personal_outbox SET status='claimed',device=?,claimed_at=? WHERE id=? AND (status='pending' OR (status='claimed' AND claimed_at<?))",
                               (device, time.time(), ident, time.time() - CLAIM_SECONDS)).rowcount
        row = self.store.one("SELECT id,kind,entry FROM personal_outbox WHERE id=?", (ident,)) if taken else None
        return {"id": row["id"], "kind": row["kind"], **json.loads(row["entry"])} if row else None

    def added(self, ident, device, ok):
        self.store.execute("UPDATE personal_outbox SET status=?,claimed_at=NULL WHERE id=? AND status='claimed' AND device=?", ("added" if ok else "pending", ident, device))
        self.store.event("personal", ident)
