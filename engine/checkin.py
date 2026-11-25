"""Reaching out: the moment the user's body data turns (an elevated stress signal, short nights, low recovery, low mood), the assistant
checks in on its own. The local model looks at their recent email and conversations for a plausible reason, then writes one short,
caring message in a quiet conversation.

It watches as data arrives (an iPhone sync, a WHOOP or Google sync, a mood the user notes) and acts on each change:
- a sign that newly appears is reached out about right away; a sign that is still there is not mentioned again,
  but one that went away and came back is new again;
- quiet hours hold a message until they end; so does the user being in the middle of talking to Jurvis;
- messages are at least SPACING apart, so a flapping signal never turns into a stream of texts;
- when the user already talked in a quiet conversation in the last hours, what showed up meanwhile counts as known;
- a check-in the user did not answer makes the next one wait longer (four times as long for each one ignored in a row, up to a day),
  and one they marked as unwelcome silences the signs it was about for a week.
"""
import json
import time
from datetime import date, datetime, timedelta

from .store import new_id

EVERY = 300           # looked at this often even without new data (quiet hours end, a held message is due)
SPACING = 3600        # at least this long between two check-ins
TALKING = 600         # the user wrote to Jurvis this recently: wait until they are done
ANSWERED = 12 * 3600  # a check-in with no word from the user in its conversation for this long was ignored
LONGEST = 86400       # however many were ignored, the wait between two check-ins is never longer than this
MUTE_DAYS = 7         # how long the signs of an unwelcome check-in stay silent
DEFAULTS = {"enabled": True, "quiet_start": "22:30", "quiet_end": "08:30"}


class Checkin:
    def __init__(self, settings, store, health):
        self.store, self.health = store, health
        self.path = settings.data / "checkin.json"
        self.looked = 0.0
        self.seen = None  # the newest health event looked at

    def load(self):
        try:
            return {**DEFAULTS, **json.loads(self.path.read_text())}
        except (OSError, ValueError):
            return dict(DEFAULTS)

    def save(self, state):
        self.path.write_text(json.dumps(state, ensure_ascii=False))

    def settings(self):
        state = self.load()
        return {k: state[k] for k in DEFAULTS} | {"last_at": state.get("last_at"), "last_signals": state.get("last_signals", []),
                                                   "watching": list(state.get("told", {}).values()), "spacing": self.spacing(),
                                                   "muted_until": max((state.get("muted") or {}).values(), default=None)}

    def spacing(self, now=None):
        """How long to wait between two check-ins: longer for each of the latest ones the user let pass without a word."""
        now = now or time.time()
        sent = self.store.all("""SELECT m.session_id,m.created_at FROM messages m WHERE json_extract(m.metadata,'$.checkin') AND m.status='complete' AND m.content!=''
                                 ORDER BY m.created_at DESC LIMIT 4""")
        ignored = 0
        for row in sent:
            if now - row["created_at"] < ANSWERED:
                continue  # still fresh: not answered yet is not ignored
            if self.store.one("SELECT 1 AS x FROM messages WHERE session_id=? AND role='user' AND created_at>? AND created_at<? LIMIT 1",
                              (row["session_id"], row["created_at"], row["created_at"] + ANSWERED)):
                break
            ignored += 1
        return min(LONGEST, SPACING * 4 ** ignored)

    def less(self, now=None):
        """The user said the last check-in was not welcome: what it was about is not raised again for a week."""
        now = now or time.time()
        state = self.load()
        kinds = list(state.get("told", {})) or ["stress", "sleep", "recovery", "mood"]
        state["muted"] = {**(state.get("muted") or {}), **{k: now + MUTE_DAYS * 86400 for k in kinds}}
        self.save(state)
        return self.settings()

    def update(self, enabled=None, quiet_start=None, quiet_end=None):
        state = self.load()
        for key, value in (("enabled", enabled), ("quiet_start", quiet_start), ("quiet_end", quiet_end)):
            if value is not None:
                if key != "enabled":
                    datetime.strptime(value, "%H:%M")  # a bad time is refused, not stored
                state[key] = value
        self.save(state)
        self.looked = 0  # looked at again right away: a message held by the old settings may be due now
        return self.settings()

    @staticmethod
    def quiet(now, start, end):
        t = now.strftime("%H:%M")
        return (start <= t or t < end) if start > end else (start <= t < end)

    def found(self, today=None):
        """{kind: what it is, in words the model can use}. Empty when all is well or nothing recent was synced."""
        today = today or date.today()
        days = self.health.daily(today - timedelta(3), today)
        out = {}
        moods = self.store.all("SELECT score FROM moods WHERE at>=?", (time.time() - 3 * 86400,))
        if len(moods) >= 2 and sum(m["score"] for m in moods) / len(moods) <= 2:
            out["mood"] = "the moods they noted lately were low"
        if not any(d >= (today - timedelta(1)).isoformat() for d in days):
            return out  # no recent body data: no guessing from old figures
        stress = self.health.stress(days, today)
        if stress and stress["level"] == "偏高":
            out["stress"] = "estimated body stress is elevated: " + "; ".join(stress["signs"])
        nights = [days.get((today - timedelta(i)).isoformat(), {}).get("sleep_min") for i in (0, 1)]
        if all(n is not None and n < 360 for n in nights) and not any("睡" in s for s in out.values()):
            out["sleep"] = f"the last two nights were short ({round(nights[0])} and {round(nights[1])} minutes asleep)"
        recovery = [days[d]["recovery"] for d in sorted(days) if "recovery" in days[d]]
        if recovery and recovery[-1] < 34:
            out["recovery"] = f"WHOOP recovery is low today ({recovery[-1]:.0f}%)"
        return out

    def signals(self, today=None):
        return list(self.found(today).values())

    def changed(self):
        row = self.store.one("SELECT MAX(id) AS id FROM events WHERE entity='health'")
        newest = row["id"] if row else None
        if newest != self.seen:
            self.seen = newest
            return True
        return False

    def talking(self, since):
        """When the user last wrote to Jurvis, in any conversation, and in a quiet one."""
        row = self.store.one("""SELECT MAX(m.created_at) AS anywhere, MAX(CASE WHEN s.persona='companion' THEN m.created_at END) AS quiet
                                FROM messages m JOIN sessions s ON s.id=m.session_id WHERE m.role='user' AND m.created_at>=?""", (since,))
        return (row["anywhere"] or 0, row["quiet"] or 0) if row else (0, 0)

    def tick(self, now=None, force=False):
        """Called by the worker every few seconds. Returns the job that will write the check-in, or None."""
        now = now or datetime.now()
        fresh = self.changed()
        if not force and not fresh and time.time() - self.looked < EVERY:
            return None
        self.looked = time.time()
        state = self.load()
        muted = state.get("muted") or {}
        found = {k: v for k, v in self.found(now.date()).items() if force or muted.get(k, 0) < time.time()}
        told = {k: v for k, v in state.get("told", {}).items() if k in found}  # a sign that went away may be new again later
        new = [k for k in found if k not in told]
        if not force and not new:
            if told != state.get("told", {}):
                state["told"] = told
                self.save(state)
            return None
        if not found:
            return None
        anywhere, quiet_talk = self.talking(time.time() - 3 * 3600)
        if not force and quiet_talk:
            state["told"] = {**told, **{k: found[k] for k in new}}  # they are already talking it through with Jurvis
            self.save(state)
            return None
        held = (not state["enabled"] or self.quiet(now, state["quiet_start"], state["quiet_end"])
                or time.time() - anywhere < TALKING or time.time() - (state.get("last_at") or 0) < self.spacing())
        if (held and not force) or self.store.one(
                "SELECT id FROM jobs WHERE kind='chat' AND status IN ('queued','running') AND json_extract(payload,'$.checkin') IS NOT NULL"):
            return None  # looked at again in a few minutes; nothing is marked as told
        session = self.conversation(list(found.values()))
        mid = self.store.add_message(session, "assistant", "", "queued", {"checkin": True})
        news = [found[k] for k in new] or list(found.values())
        job = self.store.create_job("chat", {"session_id": session, "message_id": mid, "background": True,
                                             "checkin": {"signals": news, "also": [v for k, v in found.items() if k not in new]}})
        state.update(last_at=time.time(), last_signals=news, told={**told, **{k: found[k] for k in found}})
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
