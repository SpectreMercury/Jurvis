"""Reaching out: when the user's body data turns (an elevated stress signal, short nights, low recovery, low mood), the assistant
checks in on its own. The local model looks at their recent email and conversations for a plausible reason, then writes one
short, caring message in a quiet conversation. Never during quiet hours, at most once every 20 hours, never twice for the same signs.
"""
import json
import time
from datetime import date, datetime, timedelta

from .store import new_id

EVERY = 1800          # how often the signals are looked at
GAP = 20 * 3600       # at least this long between two check-ins
DEFAULTS = {"enabled": True, "quiet_start": "22:30", "quiet_end": "08:30"}


class Checkin:
    def __init__(self, settings, store, health):
        self.store, self.health = store, health
        self.path = settings.data / "checkin.json"
        self.looked = 0.0

    def load(self):
        try:
            return {**DEFAULTS, **json.loads(self.path.read_text())}
        except (OSError, ValueError):
            return dict(DEFAULTS)

    def save(self, state):
        self.path.write_text(json.dumps(state, ensure_ascii=False))

    def settings(self):
        state = self.load()
        return {k: state[k] for k in DEFAULTS} | {"last_at": state.get("last_at"), "last_signals": state.get("last_signals", [])}

    def update(self, enabled=None, quiet_start=None, quiet_end=None):
        state = self.load()
        for key, value in (("enabled", enabled), ("quiet_start", quiet_start), ("quiet_end", quiet_end)):
            if value is not None:
                if key != "enabled":
                    datetime.strptime(value, "%H:%M")  # a bad time is refused, not stored
                state[key] = value
        self.save(state)
        return self.settings()

    @staticmethod
    def quiet(now, start, end):
        t = now.strftime("%H:%M")
        return (start <= t or t < end) if start > end else (start <= t < end)

    def signals(self, today=None):
        """What in the last few days is worth checking in about, in words the model can use. Empty when all is well or the data is stale."""
        today = today or date.today()
        days = self.health.daily(today - timedelta(3), today)
        if not any(d >= (today - timedelta(1)).isoformat() for d in days):
            return []  # nothing recent synced: no guessing from old data
        out = []
        stress = self.health.stress(days, today)
        if stress and stress["level"] == "偏高":
            out.append("estimated body stress is elevated: " + "; ".join(stress["signs"]))
        nights = [days.get((today - timedelta(i)).isoformat(), {}).get("sleep_min") for i in (0, 1)]
        if all(n is not None and n < 360 for n in nights) and not any("睡" in s for s in out):
            out.append(f"the last two nights were short ({round(nights[0])} and {round(nights[1])} minutes asleep)")
        recovery = [days[d]["recovery"] for d in sorted(days) if "recovery" in days[d]]
        if recovery and recovery[-1] < 34:
            out.append(f"WHOOP recovery is low today ({recovery[-1]:.0f}%)")
        moods = self.store.all("SELECT score FROM moods WHERE at>=?", (time.time() - 3 * 86400,))
        if len(moods) >= 2 and sum(m["score"] for m in moods) / len(moods) <= 2:
            out.append("the moods they noted lately were low")
        return out

    def tick(self, now=None, force=False):
        """Called by the worker. Returns the job that will write the check-in, or None."""
        now = now or datetime.now()
        if not force and time.time() - self.looked < EVERY:
            return None
        self.looked = time.time()
        state = self.load()
        if not force and (not state["enabled"] or self.quiet(now, state["quiet_start"], state["quiet_end"])
                          or time.time() - (state.get("last_at") or 0) < GAP):
            return None
        signals = self.signals(now.date())
        if not signals or (not force and sorted(signals) == sorted(state.get("last_signals", []))):
            return None
        if self.store.one("SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.checkin') IS NOT NULL"):
            return None
        session = self.conversation(signals)
        mid = self.store.add_message(session, "assistant", "", "queued", {"checkin": True})
        job = self.store.create_job("chat", {"session_id": session, "message_id": mid, "checkin": {"signals": signals}, "background": True})
        state.update(last_at=time.time(), last_signals=signals)
        self.save(state)
        return job

    def conversation(self, signals):
        """The quiet conversation from the last few days, or a new one."""
        recent = self.store.one("SELECT id FROM sessions WHERE persona='companion' AND incognito=0 AND updated_at>=? ORDER BY updated_at DESC LIMIT 1",
                                (time.time() - 3 * 86400,))
        if recent:
            return recent["id"]
        ident, now = new_id(), time.time()
        self.store.execute("INSERT INTO sessions(id,title,created_at,updated_at,persona,brief) VALUES(?,?,?,?,?,?)",
                           (ident, "最近还好吗", now, now, "companion",
                            "Jurvis opened this conversation on its own to check in, because the user's body data showed: " + "; ".join(signals)))
        self.store.event("sessions", ident)
        return ident
