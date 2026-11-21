"""Questions Jurvis has put to the user, and what the user chose.

A reply may end on a question with a few options (ask_user). The conversation moves on, so the question is also
kept here: until it is answered it stays in front of the user wherever they are in the conversation, and once it
is answered the choice is kept, so that the next time a similar question comes up Jurvis already knows the answer.
"""
import json
import re
import time

from .memory import features, lexical
from .store import dumps, new_id

KEEP_DAYS = 7            # an unanswered question stops waiting after this long
SHOWN = 5                # unanswered questions kept in front of the user
SIMILAR = 0.5            # lexical similarity above which an earlier question counts as the same kind
HABIT = 2                # times the same answer was given before it is taken without asking
OWN_WORDS = 40           # a typed reply this short, that is not itself a question, is taken as the answer
THANKS = re.compile(r"^\W*(谢谢|多谢|感谢|谢了|辛苦了|thanks|thank you|thx|ty)[\W你您啦了呀哦a-z]*$", re.I)   # a word of thanks chooses nothing
ASKING = re.compile(r"[?？]|吗\s*$|呢\s*$|什么|怎么|为什么|多少|哪|是不是|能不能|可不可以|\b(what|why|how|when|where|which|who)\b", re.I)


class Asks:
    def __init__(self, store):
        self.store = store
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS questions(
                id TEXT PRIMARY KEY,session_id TEXT NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,message_id TEXT UNIQUE NOT NULL,
                question TEXT NOT NULL,options TEXT NOT NULL,status TEXT NOT NULL DEFAULT 'pending',answer TEXT,
                asked_at REAL NOT NULL,answered_at REAL)""")

    def rows(self, where, params=()):
        rows = self.store.all("SELECT * FROM questions WHERE " + where, params)
        for row in rows:
            if isinstance(row["options"], str):
                row["options"] = json.loads(row["options"])
        return rows

    # ---- keeping ----
    def sync(self, session_id, now=None):
        """Records the question a finished reply ended on. A newer question in the same stretch takes the place of an older unanswered one."""
        now = now or time.time()
        self.store.execute("UPDATE questions SET status='passed' WHERE status='pending' AND asked_at<?", (now - KEEP_DAYS * 86400,))
        session = self.store.one("SELECT incognito FROM sessions WHERE id=?", (session_id,))
        if not session or session["incognito"]:
            return None  # what is off the record is not kept, a question included
        last = self.store.one("SELECT id,metadata,created_at FROM messages WHERE session_id=? AND role='assistant' AND status='complete' ORDER BY created_at DESC LIMIT 1", (session_id,))
        ask = last and last["metadata"].get("ask")
        if not ask or not ask.get("options") or self.store.one("SELECT id FROM questions WHERE message_id=?", (last["id"],)):
            return None
        if self.store.one("SELECT id FROM messages WHERE session_id=? AND role='user' AND created_at>? LIMIT 1", (session_id, last["created_at"])):
            return None  # already answered while the reply was being finished
        self.store.execute("UPDATE questions SET status='passed' WHERE status='pending' AND session_id=?", (session_id,))
        ident = new_id()
        self.store.execute("INSERT INTO questions(id,session_id,message_id,question,options,asked_at) VALUES(?,?,?,?,?,?)",
                           (ident, session_id, last["id"], str(ask.get("question") or "")[:300], dumps([str(o) for o in ask["options"]][:4]), now))
        self.store.event("questions", ident)
        return ident

    def heard(self, session_id, content, answer_to=None, now=None):
        """What the user just said in a stretch, checked against the questions waiting there. Returns the question it answers, or None.

        Tapping an option answers. So does a short reply in their own words. A question of their own does not: they went on to
        something else, and the question keeps waiting."""
        now = now or time.time()
        said = content.strip()
        waiting = self.rows("status='pending' AND " + ("id=?" if answer_to else "session_id=? ORDER BY asked_at DESC"), (answer_to or session_id,))
        if not waiting:
            return None
        chosen = next((q for q in waiting if said in q["options"]), None)
        if not chosen and (answer_to or (len(said) <= OWN_WORDS and not ASKING.search(said) and not THANKS.match(said))):
            chosen = waiting[0]
        if not chosen:
            return None
        self.store.execute("UPDATE questions SET status='answered',answer=?,answered_at=? WHERE id=?", (said[:300], now, chosen["id"]))
        self.store.event("questions", chosen["id"])
        return chosen

    def dismiss(self, ident):
        """The user does not want to decide this: it stops waiting."""
        changed = self.store.execute("UPDATE questions SET status='dismissed',answered_at=? WHERE id=? AND status='pending'", (time.time(), ident))
        if changed:
            self.store.event("questions", ident)
        return bool(changed)

    # ---- reading ----
    def pending(self):
        """What is waiting for the user to decide, newest first, for the apps."""
        rows = self.rows("status='pending' ORDER BY asked_at DESC LIMIT ?", (SHOWN,))
        titles = {s["id"]: s["title"] for s in self.store.all("SELECT id,title FROM sessions")}
        return [{"id": q["id"], "session_id": q["session_id"], "message_id": q["message_id"], "question": q["question"], "options": q["options"],
                 "asked_at": q["asked_at"], "title": titles.get(q["session_id"], "")} for q in rows]

    def elsewhere(self, session_id):
        """Questions still waiting in other stretches of the conversation, for the model to keep in mind."""
        return [{"question": q["question"], "options": q["options"]} for q in self.rows("status='pending' AND session_id!=? ORDER BY asked_at DESC LIMIT 3", (session_id,))]

    def habit(self, question, options):
        """The answer the user has given to this kind of question before, when they gave the same one each time and it is on offer again:
        (answer, times), or None. Twice is a habit; once could be chance."""
        wanted = features(question + " " + " ".join(options))
        answers = []
        for q in self.rows("status='answered' AND answer!='' ORDER BY answered_at DESC LIMIT 300"):
            if lexical(wanted, features(q["question"] + " " + " ".join(q["options"]))) >= SIMILAR:
                answers.append(q["answer"])
        if len(answers) >= HABIT and len(set(answers)) == 1 and answers[0] in options:
            return answers[0], len(answers)
        return None
