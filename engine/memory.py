"""Long-term memory and conversation summaries.

Memories are extracted from finished conversations in the background, stored with
an importance, a durability and tags, and the relevant ones are retrieved before
each reply. Search is semantic when an embedding model is configured and falls
back to built-in lexical matching (character bigrams, which also works for
Chinese) so that it keeps working fully offline.
"""
import array
import json
import math
import re
import time
from collections import Counter
from datetime import datetime

from .agenda import describe
from .runtime import strip_markup
from .store import dumps, new_id
from .vault import Vault, page_of, plain, named as said

EXCERPTS = 3             # earlier exchanges of the same conversation brought back verbatim for one reply
EXCERPT_FLOOR = 0.5      # share (by rarity) of the request's known terms an earlier exchange must contain to be brought back
LEXICAL_FLOOR = 0.08
DUPLICATE = 0.82         # lexical similarity above which a new memory updates an old one
TEMPORARY_DAYS = 30
QUIET = 30 * 60          # seconds without talk after which a stretch of the conversation is finished and gets its record
RECENT_HOURS = 36        # how far back "what we talked about just before" reaches
RELATED_FLOOR = 0.12     # lexical similarity an older stretch needs to be brought back for a request
SHIFT_TURNS = 3          # exchanges a stretch needs before a message can be read as a change of subject
SHIFT_OVERLAP = 0.05     # lexical similarity with the recent talk above which a message plainly continues it
PORTRAIT_FACTS = 3       # memories needed before a portrait of the user is worth writing
PORTRAIT_HOURS = 20      # the portrait is rewritten at most this often
NIGHT_HOUR = 3           # the nightly tidy-up runs at the first quiet moment after this hour
TIDY_PAGES = 3           # pages of notes rewritten in one night

EXTRACT = """You maintain the long-term memory of a personal assistant. From the conversation below, extract facts worth remembering in FUTURE conversations:
stable preferences, facts about the user and people or things in their life, ongoing projects, decisions and their reasons, and corrections of how the assistant should work.
Do NOT extract: passwords, keys, tokens or other secrets; one-off requests; that the user asked for something or where this conversation stands (a request is not a fact about them, though what it reveals may be: "has started running", not "asked for a running plan"); anything that is only relevant to this conversation; things the assistant said that the user did not confirm; facts already in the existing memories.
Write each memory in the language the user used, as one self-contained statement that makes sense without the conversation.
Reply with JSON only, an array (empty if nothing qualifies) of:
{"title": "short label", "content": "the fact", "importance": 0.0-1.0, "durable": true for lasting facts or false for things that expire within weeks, "tags": ["..."],
 "page": the page of the user's notes it belongs on: "人物/<name>" for a fact about a person in the user's life, "习惯/<kind of work>" for how the user wants something done, "主题/<subject>" for anything else. Use a page from the list of existing pages when one fits; otherwise a short new name}"""

TIDY = """You keep one page of a personal assistant's notes about its user. Below are the facts on the page, oldest first, each with the date it was learned.
Rewrite them as a clean list. Merge facts that say the same thing. When a newer fact replaces an older one (a move, a new job, a changed preference),
keep the newer one and add what it was before in brackets at its end, like （之前是…）. Drop nothing else and add nothing that is not there.
Lines under "Kept as they are" stay on the page untouched: do not return them, and leave out a fact that one of them already says in full.
Reply with JSON only: an array of strings, one fact each, in the language of the facts."""

SUMMARIZE = """Summarize the earlier part of a conversation between a user and their assistant so the assistant can continue it later without the original messages.
Keep, with the exact names, numbers, dates and wording that later questions may refer to: who the user is and what they want, every decision and agreed figure,
each thing the assistant produced (lists with their numbered items in one line each, drafts with their title and opening line, files with their names), and what is still open.
Give each thing produced the file it was saved in or the date it was made, so that its full text can be looked up again later.
When a "Summary so far" is given, keep everything in it that still matters and add the new messages; never drop an earlier topic just because the conversation moved on.
Drop small talk. Write in the language of the conversation, at most 600 words, as short labelled lines grouped by topic."""

HANDOFF = """Write the handoff notes that let an assistant carry on a conversation with its user after the earlier messages are gone.
This is not a summary of what was said: pick what the rest of the conversation will need. The messages themselves are kept and can be looked up by date or by their words.
Write, as short labelled lines:
- Now: what the user is working on or wants at this point, and what was about to happen next.
- Open: questions not yet answered, decisions not yet made, things promised but not done.
- Standing instructions: how the user said they want things done in this conversation, in their own words.
- Settled: decisions and agreed figures that what is open depends on, with the exact names, numbers and dates.
- Produced: each thing the assistant made, one line each: what it is, its file name or the date it was made, and its title or opening words, so it can be looked up.
- Earlier subjects: one line each for subjects that are finished, with their date, as pointers only.
When "Notes so far" are given, carry over what still matters and bring it up to date; drop what the new messages have settled or replaced.
Write in the language of the conversation, at most 400 words. No preamble."""

REWRITE = """Rewrite the user's latest message as a short standalone search query for a memory store about this user (their preferences, people, projects, decisions).
Use the conversation only to resolve references like "it" or "那个". Reply with the query only, in the user's language."""

DIGEST = """Write the assistant's own record of one finished conversation with its user, so that it can be found again months later.
First line: a title of at most 12 words, no quotes. Then a blank line, then at most 120 words: what the user wanted, what was decided or produced
(names, numbers, dates and file names exactly as they were), and what was left open. Drop small talk.
Write in the language of the conversation. No preamble."""

SHIFT = """A user is talking with their assistant. Decide where the latest message belongs.
SAME: it continues what they were just talking about (a follow-up, a reaction, a correction, a question about what was just said).
A number: it goes back to that earlier subject from the list, when a list is given.
NEW: it starts a subject that is neither.
A short message that only makes sense as a follow-up to something ("and the second one?", "那…呢", "那个改成…") is never NEW: it belongs to whichever of them it fits.
Reply with one word: SAME, NEW or the number."""

PORTRAIT = """You keep a short portrait of the user for their personal assistant: the few things it should have in mind in every conversation.
From the portrait so far and the remembered facts (each with the date it was learned), write the portrait anew in at most 300 words, as short labelled lines:
who they are, the people in their life, where they live and work, what they are working toward now, and how they like things done.
Write the labels and the lines in the language most of the facts use. Leave a label out when the facts say nothing for it: never write that something is unknown.
Put in what is true of the person and their life, not what they once asked the assistant to do. When two facts disagree, the newer one holds.
Keep what is in the portrait so far unless a fact contradicts it: the user may have written it themselves. State only what the facts say; no guesses, no secrets.
No preamble."""

WORD = re.compile(r"[a-z0-9_]+")
CJK = re.compile(r"[㐀-鿿぀-ヿ가-힯]")


def features(text):
    """Bag of latin words and CJK character bigrams."""
    text = (text or "").lower()
    bag = Counter(WORD.findall(text))
    chars = [c for c in text if CJK.match(c)]
    bag.update(chars)
    bag.update(a + b for a, b in zip(chars, chars[1:]))
    return bag


def lexical(a, b):
    if not a or not b:
        return 0.0
    dot = sum(count * b[key] for key, count in a.items() if key in b)
    return dot / math.sqrt(sum(v * v for v in a.values()) * sum(v * v for v in b.values())) if dot else 0.0


def cosine(a, b):
    if len(a) != len(b) or not a:
        return 0.0
    norm = math.sqrt(math.sumprod(a, a) * math.sumprod(b, b))
    return math.sumprod(a, b) / norm if norm else 0.0


def pack(vector):
    return array.array("f", vector).tobytes()


def unpack(blob):
    values = array.array("f")
    values.frombytes(blob)
    return values


def parse_list(text):
    """The JSON objects in a model reply, whether it sent an array, a single object, or objects with prose around them."""
    text, decoder, found, index = strip_markup(text), json.JSONDecoder(), [], 0
    while (start := min((i for i in (text.find("[", index), text.find("{", index)) if i >= 0), default=-1)) >= 0:
        try:
            value, end = decoder.raw_decode(text, start)
        except json.JSONDecodeError:
            index = start + 1
            continue
        found.extend(v for v in (value if isinstance(value, list) else [value]) if isinstance(v, dict))
        index = end
    return found


class Memory:
    def __init__(self, settings, store, llm):
        self.settings, self.store, self.llm = settings, store, llm
        self.vault = Vault(settings, store)

    def prefs(self):
        return self.llm.prefs.get()["memory"]

    # ---- writing ----
    def save(self, title, content, *, importance=0.5, durable=True, tags=(), source="manual", session_id=None, page=None):
        """Adds a memory, or updates the one that already says nearly the same thing. Vectors are added later by index()."""
        title, content = title.strip()[:100], content.strip()[:4000]
        importance = min(1.0, max(0.0, float(importance)))
        tags = [str(t)[:40] for t in tags if str(t).strip()][:8]
        wanted, now = features(title + " " + content), time.time()
        for row in self.store.all("SELECT id,title,content FROM memories"):
            if lexical(wanted, features(row["title"] + " " + row["content"])) >= DUPLICATE:
                # A fact the user took out of their notes stays out when it is only overheard again; said on purpose, it comes back.
                self.store.execute("UPDATE memories SET title=?,content=?,importance=MAX(importance,?),durable=MAX(durable,?),tags=?,updated_at=?,embedding=NULL,embed_model=NULL,"
                                   "page=COALESCE(?,page),enabled=CASE WHEN ?='chat' THEN enabled ELSE 1 END WHERE id=?",
                                   (title, content, importance, int(durable), dumps(tags), now, page, source, row["id"]))
                self.store.event("memories", row["id"])
                return {"saved": row["id"], "title": title, "updated": True}
        ident = new_id()
        self.store.execute("INSERT INTO memories(id,title,content,created_at,updated_at,importance,durable,tags,source,session_id,page) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (ident, title, content, now, now, importance, int(durable), dumps(tags), source, session_id, page))
        self.store.event("memories", ident)
        return {"saved": ident, "title": title}

    async def index(self, ids=None):
        """Embeds memories that have no vector for the current embedding model. Returns how many were embedded."""
        model = self.llm.prefs.get()["embed_model"]
        if not model:
            return 0
        rows = self.store.all("SELECT id,title,content,embed_model FROM memories")
        rows = [r for r in rows if r["embed_model"] != model and (ids is None or r["id"] in ids)]
        done = 0
        for start in range(0, len(rows), 32):
            batch = rows[start:start + 32]
            vectors = await self.llm.embed([r["title"] + "\n" + r["content"] for r in batch])
            if not vectors:
                break
            for row, vector in zip(batch, vectors):
                self.store.execute("UPDATE memories SET embedding=?,embed_model=? WHERE id=?", (pack(vector), model, row["id"]))
            done += len(batch)
        if ids is None:  # the records of finished stretches are searched the same way
            rows = [r for r in self.store.all("SELECT d.session_id,d.text,d.embed_model,s.title FROM digests d JOIN sessions s ON s.id=d.session_id") if r["embed_model"] != model]
            for start in range(0, len(rows), 32):
                batch = rows[start:start + 32]
                vectors = await self.llm.embed([r["title"] + "\n" + r["text"] for r in batch])
                if not vectors:
                    break
                for row, vector in zip(batch, vectors):
                    self.store.execute("UPDATE digests SET embedding=?,embed_model=? WHERE session_id=?", (pack(vector), model, row["session_id"]))
        return done

    # ---- reading ----
    async def retrieve(self, query, limit=None, threshold=None):
        """The memories most relevant to the query, best first, each with a score."""
        prefs = self.prefs()
        limit = limit or prefs["max_retrieved"]
        threshold = prefs["threshold"] if threshold is None else threshold
        rows = self.store.all("SELECT id,title,content,importance,durable,tags,embedding,embed_model FROM memories WHERE enabled=1")
        if not rows:
            return []
        model = self.llm.prefs.get()["embed_model"]
        vector = None
        if model and query.strip() and any(r["embed_model"] == model for r in rows):
            vectors = await self.llm.embed([query])
            vector = vectors[0] if vectors else None
        wanted, scored = features(query), []
        for row in rows:
            if vector is not None and row["embed_model"] == model and row["embedding"]:
                score, floor = cosine(vector, unpack(row["embedding"])), threshold
            else:
                score, floor = lexical(wanted, features(row["title"] + " " + row["content"])), LEXICAL_FLOOR
            pinned = row["importance"] >= 0.9
            if score >= floor or pinned:
                scored.append((score * (0.75 + 0.5 * row["importance"]) + (0.05 if pinned else 0), row))
        scored.sort(key=lambda item: -item[0])
        if vector is None:
            # Word matching cannot tell that "孩子" means "女儿": without embeddings, the important memories ride along as well.
            chosen = {row["id"] for _, row in scored}
            core = sorted((r for r in rows if r["importance"] >= 0.6 and r["id"] not in chosen), key=lambda r: -r["importance"])
            scored.extend((0.0, row) for row in core[:max(0, limit - len(scored))])
        out = [{"id": r["id"], "title": r["title"], "content": r["content"][:1200], "score": round(s, 3)} for s, r in scored[:limit]]
        if out:
            marks = ",".join("?" for _ in out)
            self.store.execute(f"UPDATE memories SET used_at=? WHERE id IN ({marks})", (time.time(), *[m["id"] for m in out]))
        return out

    async def for_turn(self, session, request, history):
        """Memories to put in front of the model for this reply."""
        prefs = self.prefs()
        if not prefs["enabled"] or not prefs["auto_retrieve"] or (session and session.get("incognito")):
            return []
        query = request
        # A follow-up like "那个呢" only makes sense with the conversation; a cheap cloud model rewrites it.
        tool_model = self.llm.prefs.get()["tool_model"]
        if history and len(request) < 40 and tool_model and not self.llm.is_local(tool_model) and self.store.one("SELECT 1 AS x FROM memories WHERE enabled=1 LIMIT 1"):
            recent = "\n".join(f"{m['role']}: {m['content'][:300]}" for m in history[-4:] if isinstance(m.get("content"), str) and m["role"] in ("user", "assistant"))
            try:
                reply = await self.llm.stream([{"role": "system", "content": REWRITE}, {"role": "user", "content": f"Conversation:\n{recent}\n\nLatest message:\n{request}"}],
                                              max_tokens=80, temperature=0, model=tool_model, search=False)
                query = (reply.text.strip().splitlines() or [request])[0][:300] + " " + request
            except RuntimeError:
                pass
        return await self.retrieve(query)

    # ---- background work after a reply ----
    def search(self, query, limit=20):
        """Keyword lookup for the recall tool: any term may match; an empty query lists the newest."""
        rows = self.store.all("SELECT id,title,content FROM memories WHERE enabled=1 ORDER BY updated_at DESC LIMIT 500")
        if not query.strip():
            return rows[:limit]
        wanted = features(query)
        scored = [(lexical(wanted, features(r["title"] + " " + r["content"])), r) for r in rows]
        return [r for s, r in sorted(scored, key=lambda item: -item[0]) if s > 0][:limit]

    def pending(self, session_id):
        """True when a finished conversation has something to extract or summarize."""
        session = self.store.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not session or session.get("incognito") or not self.prefs()["enabled"]:
            return False
        newest = self.store.one("SELECT MAX(created_at) AS at FROM messages WHERE session_id=? AND status='complete'", (session_id,))["at"] or 0
        return newest > (session.get("memory_upto") or 0)

    async def process(self, session_id):
        session = self.store.one("SELECT * FROM sessions WHERE id=?", (session_id,))
        if not session or session.get("incognito"):
            return {"memories": 0, "summarized": 0, "recorded": 0}
        model = self.llm.tool_model(session.get("model"))
        saved = await self.extract(session, model) if self.prefs()["auto_extract"] else 0
        summarized = 0  # the conversation itself folds old messages into its summary when they stop fitting (Agent.context)
        recorded = await self.digest(model)
        await self.index()
        self.prune()
        if saved or not self.portrait_file.exists():
            await self.draw(model)
        self.vault.sync()  # what was just learned appears in the user's notes
        return {"memories": saved, "summarized": summarized, "recorded": recorded}

    @staticmethod
    def transcript(rows, limit=12000):
        lines = []
        for row in rows:
            if row["role"] in ("user", "assistant") and row["content"].strip():
                lines.append(("User: " if row["role"] == "user" else "Assistant: ") + row["content"].strip()[:2000])
        text = "\n".join(lines)
        return text[-limit:]

    async def extract(self, session, model):
        rows = self.store.all("SELECT role,content,created_at FROM messages WHERE session_id=? AND status='complete' AND created_at>? ORDER BY created_at",
                              (session["id"], session.get("memory_upto") or 0))
        if not any(r["role"] == "user" for r in rows):
            return 0
        known = self.store.all("SELECT title,content FROM memories ORDER BY updated_at DESC LIMIT 40")
        existing = "\n".join(f"- {m['title']}: {m['content'][:200]}" for m in known) or "(none)"
        goal = self.store.one("SELECT page FROM goals WHERE session_id=? AND page IS NOT NULL ORDER BY created_at DESC LIMIT 1", (session["id"],))
        about = f"\nThis conversation set up the long-term goal whose page is {goal['page']}: facts about that goal belong on it." if goal else ""
        reply = await self.llm.stream([{"role": "system", "content": EXTRACT},
                                       {"role": "user", "content": f"Existing pages: {', '.join(self.vault.pages()) or '(none)'}{about}\n\nExisting memories:\n{existing}\n\nConversation:\n{self.transcript(rows)}"}],
                                      max_tokens=1200, temperature=0.2, model=model, search=False)
        saved = 0
        for item in parse_list(reply.text)[:8]:
            title, content = str(item.get("title") or "").strip(), str(item.get("content") or "").strip()
            if not title or not content or re.search(r"(password|passwd|api[_ -]?key|secret|token|密码|口令|密钥)\s*[:：=是为]", content, re.I):
                continue
            try:
                importance = float(item.get("importance", 0.5))
            except (TypeError, ValueError):
                importance = 0.5
            tags = item.get("tags") if isinstance(item.get("tags"), list) else []
            self.save(title, content, importance=min(importance, 0.85), durable=bool(item.get("durable", True)), tags=tags, source="chat", session_id=session["id"], page=page_of(item.get("page")))
            saved += 1
        self.store.execute("UPDATE sessions SET memory_upto=? WHERE id=?", (rows[-1]["created_at"], session["id"]))
        return saved

    async def fold(self, session, rows, ask, brief=False):
        """Folds messages that no longer fit the context into the conversation summary. ask is the model call of the conversation.
        brief=True writes notes for what is still open instead of a summary of everything (a cloud model only, when the user turned it on)."""
        if not rows:
            return False
        prior = session.get("summary") or ""
        day = lambda row: datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d")
        dates = day(rows[0]) if day(rows[0]) == day(rows[-1]) else f"{day(rows[0])} to {day(rows[-1])}"
        content = (f"{'Notes' if brief else 'Summary'} so far:\n{prior}\n\n" if prior else "") + f"Messages to fold in ({dates}):\n" + self.transcript(rows, 24000)
        reply = await ask([{"role": "system", "content": HANDOFF if brief else SUMMARIZE}, {"role": "user", "content": content}], max_tokens=1400, temperature=0.2, search=False)
        text = reply.text.strip()
        if not text:
            return False
        self.store.execute("UPDATE sessions SET summary=?,summary_upto=? WHERE id=?", (text[:6000], rows[-1]["created_at"], session["id"]))
        return True

    def excerpts(self, session_id, upto, request, others=()):
        """Earlier exchanges the request seems to refer to, verbatim: (question, answer) pairs from the part of this stretch
        that is folded into its summary, and from the other stretches given. Those from other stretches carry their date."""
        wanted = {k: v for k, v in features(request).items() if len(k) > 1}  # single characters match everything
        if len(wanted) < 2 or not (upto or others):
            return []
        pairs = []
        def collect(rows, dated):
            fresh = True
            for row in rows:
                if row["role"] == "user" or fresh:
                    pairs.append(["", "", datetime.fromtimestamp(row["created_at"]).strftime("%Y-%m-%d") if dated else None])
                    fresh = False
                pairs[-1][0 if row["role"] == "user" else 1] += row["content"]
        if upto:
            collect(self.store.all("SELECT role,content,created_at FROM messages WHERE session_id=? AND status='complete' AND role IN ('user','assistant') AND created_at<=? ORDER BY created_at",
                                   (session_id, upto)), False)
        for other in others:
            collect(self.store.all("SELECT role,content,created_at FROM messages WHERE session_id=? AND status='complete' AND role IN ('user','assistant') ORDER BY created_at LIMIT 400",
                                   (other,)), True)
        bags = []
        for question, answer, _ in pairs:
            bag = features(answer[:6000])
            bag.update(features(question)); bag.update(features(question))  # the question names the topic ("写一条朋友圈文案"), so it counts more
            bags.append(bag)
        # Only terms these conversations actually used can tell exchanges apart, and the rarer a term the more it tells.
        weight = {k: math.log(1 + len(bags) / n) for k in wanted if (n := sum(1 for bag in bags if k in bag))}
        total, scored = sum(weight.values()), []
        for index, bag in enumerate(bags):
            hits = [k for k in weight if k in bag]
            if len(hits) >= 2 and sum(weight[k] for k in hits) / total >= EXCERPT_FLOOR:
                scored.append((sum(weight[k] for k in hits) / total, index))
        best = sorted(index for _, index in sorted(scored, reverse=True)[:EXCERPTS])
        return [{"user": pairs[i][0][:600], "assistant": pairs[i][1][:2400], **({"when": pairs[i][2]} if pairs[i][2] else {})} for i in best]

    # ---- the one conversation: records of its finished stretches, and what comes back from them ----
    def stale(self, now=None, limit=5):
        """Stretches that have gone quiet and have no up-to-date record yet, newest first."""
        now = now or time.time()
        return self.store.all("""SELECT s.id,s.title,s.model,MAX(m.created_at) AS last,COALESCE(d.upto,0) AS recorded FROM sessions s
                                 JOIN messages m ON m.session_id=s.id AND m.status='complete' AND m.role IN ('user','assistant') AND m.content!=''
                                 LEFT JOIN digests d ON d.session_id=s.id WHERE s.incognito=0
                                 GROUP BY s.id HAVING last>recorded AND last<? ORDER BY last DESC LIMIT ?""", (now - QUIET, limit))

    async def digest(self, model, now=None, limit=5):
        """Writes the record of stretches that have finished. Returns how many were written."""
        done = 0
        for row in self.stale(now, limit):
            rows = self.store.all("SELECT role,content,created_at FROM messages WHERE session_id=? AND status='complete' ORDER BY created_at", (row["id"],))
            text = self.transcript(rows)
            if len(text) > 500:  # a short exchange is its own record
                reply = await self.llm.stream([{"role": "system", "content": DIGEST}, {"role": "user", "content": text}],
                                              max_tokens=500, temperature=0.2, model=model, search=False)
                title, _, body = strip_markup(reply.text).strip().partition("\n")
                asked = next((r["content"] for r in rows if r["role"] == "user"), "")
                if body.strip():
                    text = body.strip()[:1500]
                    if title.strip() and row["title"] in ("新对话", asked[:40]):  # a name the user gave stays
                        self.store.execute("UPDATE sessions SET title=? WHERE id=?", (title.strip(" #\"“”")[:40], row["id"]))
                        self.store.event("sessions", row["id"])
                else:  # the model gave one line: it is kept, with how the stretch began and ended under it
                    text = (title.strip() + "\n" + f"User: {' '.join(asked.split())[:200]}\n…\n" + self.tail(row["id"]))[:1500]
            self.store.execute("INSERT INTO digests(session_id,text,upto) VALUES(?,?,?) ON CONFLICT(session_id) DO UPDATE SET text=excluded.text,upto=excluded.upto,embedding=NULL,embed_model=NULL",
                               (row["id"], text, row["last"]))
            done += 1
        return done

    def tail(self, session_id):
        """How a stretch ended, for one that has no record yet."""
        rows = self.store.all("SELECT role,content FROM messages WHERE session_id=? AND status='complete' AND role IN ('user','assistant') AND content!='' ORDER BY created_at DESC LIMIT 4",
                              (session_id,))
        return "\n".join(("User: " if r["role"] == "user" else "Assistant: ") + " ".join(r["content"].split())[:240] for r in rows[::-1])

    async def background(self, session, request, wide=True):
        """Other stretches of the conversation worth having in mind for this reply: the latest ones, for continuity,
        and older ones the request seems to be about. A local model gets fewer (wide=False)."""
        prefs = self.prefs()
        if not prefs["enabled"] or not prefs["auto_retrieve"] or not session or session.get("incognito"):
            return {"recent": [], "related": []}
        rows = self.store.all("""SELECT s.id,s.title,s.updated_at,d.text,d.embedding,d.embed_model FROM sessions s LEFT JOIN digests d ON d.session_id=s.id
                                 WHERE s.incognito=0 AND s.id!=? ORDER BY s.updated_at DESC LIMIT 2000""", (session["id"],))
        now = time.time()
        def shown(row, text):
            return {"id": row["id"], "when": datetime.fromtimestamp(row["updated_at"]).strftime("%Y-%m-%d %H:%M"), "title": row["title"], "text": text[:600]}
        recent = []
        for row in rows[:4]:
            if now - row["updated_at"] > RECENT_HOURS * 3600 or len(recent) >= (2 if wide else 1):
                break
            text = row["text"] or self.tail(row["id"])
            if text:
                recent.append(shown(row, text))
        taken = {r["id"] for r in recent}
        rows = [r for r in rows if r["text"] and r["id"] not in taken]
        model = self.llm.prefs.get()["embed_model"]
        vector = None
        if model and request.strip() and any(r["embed_model"] == model for r in rows):
            vectors = await self.llm.embed([request])
            vector = vectors[0] if vectors else None
        wanted, scored = features(request), []
        for row in rows:
            if vector is not None and row["embed_model"] == model and row["embedding"]:
                score, floor = cosine(vector, unpack(row["embedding"])), prefs["threshold"]
            else:
                score, floor = lexical(wanted, features(row["title"] + " " + row["text"])), RELATED_FLOOR
            if score >= floor:
                scored.append((score * (0.75 + 0.25 * math.exp(-(now - row["updated_at"]) / (30 * 86400))), row))
        scored.sort(key=lambda item: -item[0])
        return {"recent": recent, "related": [shown(row, row["text"]) for _, row in scored[:3 if wide else 2]]}

    async def shifted(self, session_id, request):
        """Where the user's latest message belongs: False when it continues this stretch, True when it starts a different subject,
        or the id of an earlier stretch it goes back to."""
        if len(request) < 6:
            return False
        rows = self.store.all("SELECT role,content FROM messages WHERE session_id=? AND status='complete' AND role IN ('user','assistant') AND content!='' ORDER BY created_at DESC LIMIT 9",
                              (session_id,))[::-1]
        if rows and rows[-1]["role"] == "user":
            rows = rows[:-1]  # the message in question
        settled = sum(1 for r in rows if r["role"] == "user") >= SHIFT_TURNS  # a stretch this long has a subject a message can leave
        earlier = self.store.all("""SELECT s.id,s.title,COALESCE(d.text,'') AS text FROM sessions s LEFT JOIN digests d ON d.session_id=s.id
                                    WHERE s.id!=? AND s.incognito=0 AND s.persona IS NULL AND s.code_project_id IS NULL AND s.updated_at>=?
                                    AND EXISTS(SELECT 1 FROM messages m WHERE m.session_id=s.id AND m.role='user')
                                    ORDER BY s.updated_at DESC LIMIT 4""", (session_id, time.time() - RECENT_HOURS * 3600))
        if not rows or not (settled or earlier):
            return False
        # Single characters are in every Chinese sentence: only words and character pairs say that two texts share a subject.
        words = lambda text: Counter({k: v for k, v in features(text).items() if len(k) > 1})
        if lexical(words(request), words(" ".join(r["content"][:1000] for r in rows))) >= SHIFT_OVERLAP:
            return False
        recent = "\n".join(f"{r['role']}: {r['content'][:300]}" for r in rows[-6:])
        def last(ident):
            said = self.store.all("SELECT role,content FROM messages WHERE session_id=? AND status='complete' AND role IN ('user','assistant') AND content!='' ORDER BY created_at DESC LIMIT 2", (ident,))[::-1]
            return " / ".join(f"{r['role']}: {' '.join(r['content'].split())[:200]}" for r in said)
        listed = "".join(f"\n{n}. {s['title']}" + (f" — {' '.join(s['text'].split())[:160]}" if s["text"] else "") + f" (last said there: {last(s['id'])})" for n, s in enumerate(earlier, 1))
        try:
            reply = await self.llm.stream([{"role": "system", "content": SHIFT}, {"role": "user", "content": f"Conversation:\n{recent}\n\n"
                                           + (f"Earlier subjects:{listed}\n\n" if earlier else "") + f"Latest message:\n{request[:600]}"}],
                                          max_tokens=8, temperature=0, model=self.llm.tool_model(), search=False)
        except RuntimeError:
            return False
        word = reply.text.strip().upper()
        number = re.match(r"\D{0,3}(\d)", word)
        if number and 1 <= int(number.group(1)) <= len(earlier):
            return earlier[int(number.group(1)) - 1]["id"]
        return settled and word.startswith("NEW")

    def returning(self, session_id, request):
        """The stretch the user is going back to: they stepped aside for something else a moment ago, and this message is about
        what they were on before. Returns that stretch's id, or None."""
        session = self.store.one("SELECT parent_id FROM sessions WHERE id=?", (session_id,)) or {}
        parent = session.get("parent_id")
        if not parent or not self.store.one("SELECT id FROM sessions WHERE id=? AND incognito=0", (parent,)):
            return None
        def talk(ident, limit):
            return self.store.all("SELECT role,content,created_at FROM messages WHERE session_id=? AND status='complete' AND role IN ('user','assistant') AND content!='' "
                                  "ORDER BY created_at DESC LIMIT ?", (ident, limit))
        here, there = talk(session_id, 9), talk(parent, 8)
        if here and here[0]["role"] == "user":
            here = here[1:]  # the message in question
        if not there or time.time() - there[0]["created_at"] > 2 * QUIET or sum(1 for r in here if r["role"] == "user") >= SHIFT_TURNS:
            return None  # too long ago, or the aside has become a conversation of its own
        wanted = features(request)
        back = lexical(wanted, features(" ".join(r["content"][:1000] for r in there)))
        stay = lexical(wanted, features(" ".join(r["content"][:1000] for r in here)))
        return parent if back >= SHIFT_OVERLAP and back > stay else None

    # ---- spaces: a long-term goal, its page of notes, and its rhythm, as one thing ----
    def space(self, goal):
        """Gives a goal its page under 主题/. The page opens with what the goal is; facts learned about it later are filed there."""
        page = goal.get("page") or page_of("主题/" + goal["title"])
        if not page:
            return None
        self.store.execute("DELETE FROM memories WHERE source='goal' AND page=? AND title=?", (page, goal["title"][:100]))  # what the goal was before
        self.store.execute("UPDATE goals SET page=? WHERE id=?", (page, goal["id"]))
        if goal.get("session_id"):
            # What was already noted in the stretch that set the goal up is about the goal: a page that holds nothing else joins the goal's page.
            for row in self.store.all("SELECT DISTINCT page FROM memories WHERE session_id=? AND source='chat' AND page LIKE '主题/%' AND page!=?", (goal["session_id"], page)):
                if not self.store.one("SELECT 1 AS x FROM memories WHERE page=? AND (session_id IS NULL OR session_id!=?) LIMIT 1", (row["page"], goal["session_id"])):
                    self.store.execute("UPDATE memories SET page=?,updated_at=? WHERE page=?", (page, time.time(), row["page"]))
        self.save(goal["title"], "长期关注：" + goal["title"] + ("。" + goal["detail"] if goal.get("detail") else ""),
                  importance=0.7, source="goal", session_id=goal.get("session_id"), page=page)
        return page

    def space_view(self, goal_id):
        """A goal as the user opens it: what is known about it, how often it is looked at, and where it was talked about."""
        goal = self.store.one("SELECT * FROM goals WHERE id=?", (goal_id,))
        if not goal:
            return None
        facts = self.store.all("SELECT id,content,source,created_at FROM memories WHERE page=? AND enabled=1 ORDER BY importance DESC,created_at", (goal["page"],)) if goal.get("page") else []
        schedules = self.store.all("SELECT id,title,brief,rule,next_run,last_run FROM schedules WHERE enabled=1 AND session_id=? ORDER BY created_at", (goal["session_id"],)) if goal.get("session_id") else []
        for item in schedules:
            item["when"] = describe(item["rule"])
        # Where it was talked about: the stretch it was set up in, the ones its facts came from, and the ones whose record names it.
        named, wanted = set(), features(goal["title"])
        for row in self.store.all("SELECT session_id FROM memories WHERE page=? AND session_id IS NOT NULL", (goal.get("page"),)):
            named.add(row["session_id"])
        if goal.get("session_id"):
            named.add(goal["session_id"])
        stretches = []
        for row in self.store.all("""SELECT s.id,s.title,s.updated_at,d.text FROM sessions s LEFT JOIN digests d ON d.session_id=s.id
                                     WHERE s.incognito=0 ORDER BY s.updated_at DESC LIMIT 500"""):
            if row["id"] in named or (row["text"] and lexical(wanted, features(row["title"] + " " + row["text"])) >= 0.2):
                stretches.append({"id": row["id"], "title": row["title"], "updated_at": row["updated_at"], "text": said(row["text"] or "")[:300]})
        return {"goal": goal, "page": goal.get("page"), "file": str(self.vault.file(goal["page"])) if goal.get("page") else None,
                "facts": facts, "schedules": schedules, "stretches": stretches[:12]}

    # ---- the nightly tidy-up ----
    def due(self, now=None):
        """True once a night, from NIGHT_HOUR on, until the tidy-up has run."""
        now = datetime.fromtimestamp(now or time.time())
        last = self.store.one("SELECT hash FROM pages WHERE path='.night'")
        return self.prefs()["enabled"] and now.hour >= NIGHT_HOUR and (not last or last["hash"] != now.strftime("%Y-%m-%d"))

    async def consolidate(self, model, now=None):
        """What sleep does: the day's talk gets its records, the notes are put in order, and the portrait is brought up to date."""
        self.vault.sync()  # the user's own corrections first: they are what the tidy-up starts from
        recorded = await self.digest(model, now, limit=20)
        tidied = await self.tidy(model)
        await self.index()
        self.prune()
        drawn = await self.draw(model, now)
        self.vault.sync()
        self.store.execute("INSERT INTO pages(path,hash) VALUES('.night',?) ON CONFLICT(path) DO UPDATE SET hash=excluded.hash",
                           (datetime.fromtimestamp(now or time.time()).strftime("%Y-%m-%d"),))
        return {"recorded": recorded, "tidied": tidied, "portrait": drawn}

    async def tidy(self, model, limit=TIDY_PAGES):
        """Rewrites the pages that learned something new since they were last put in order: duplicates merged, a fact that replaced
        an older one says what it was before. Lines the user wrote are left word for word. Returns how many pages changed."""
        pages = self.store.all("""SELECT m.page,MAX(m.updated_at) AS changed,COALESCE(p.tidied_at,0) AS tidied FROM memories m LEFT JOIN pages p ON p.path=m.page
                                  WHERE m.enabled=1 AND m.source='chat' AND m.page IS NOT NULL GROUP BY m.page HAVING changed>tidied ORDER BY changed DESC LIMIT ?""", (limit,))
        done = 0
        for page in (p["page"] for p in pages):
            rows = self.store.all("SELECT id,content,source,importance,created_at,session_id FROM memories WHERE page=? AND enabled=1 ORDER BY created_at", (page,))
            loose, kept = [r for r in rows if r["source"] == "chat"], [r for r in rows if r["source"] != "chat"]
            fixed = {plain(r["content"]) for r in kept}
            if len(rows) >= 2:
                listing = "\n".join(f"- [{datetime.fromtimestamp(r['created_at']).strftime('%Y-%m-%d')}] {r['content']}" for r in loose)
                if kept:
                    listing += "\n\nKept as they are:\n" + "\n".join("- " + r["content"] for r in kept)
                reply = await self.llm.stream([{"role": "system", "content": TIDY}, {"role": "user", "content": f"Page: {page}\n\n{listing}"}],
                                              max_tokens=1500, temperature=0, model=model, search=False)
                text = strip_markup(reply.text)
                try:
                    facts = json.loads(text[text.index("["):text.rindex("]") + 1])
                except ValueError:
                    facts = None
                facts = [plain(f)[:4000] for f in facts if isinstance(f, str) and f.strip()] if isinstance(facts, list) else []
                fresh = [f for f in dict.fromkeys(facts) if f not in fixed]
                # Only a rewrite that did not grow the page is taken. The user's own lines are not the model's to touch: they stay as they are.
                if fresh and len(fresh) <= len(loose) and set(fresh) != {plain(r["content"]) for r in loose}:
                    now, weight = time.time(), max((r["importance"] for r in loose), default=0.5)
                    self.store.execute(f"DELETE FROM memories WHERE id IN ({','.join('?' for _ in loose)})", tuple(r["id"] for r in loose))
                    for n, fact in enumerate(fresh):
                        self.store.execute("INSERT INTO memories(id,title,content,created_at,updated_at,importance,durable,tags,source,session_id,page) VALUES(?,?,?,?,?,?,1,'[]','chat',?,?)",
                                           (new_id(), page.split("/", 1)[1][:100], fact, loose[min(n, len(loose) - 1)]["created_at"], now, weight, loose[-1]["session_id"], page))
                    self.store.event("memories", page)
                    done += 1
            self.store.execute("INSERT INTO pages(path,hash,tidied_at) VALUES(?,'',?) ON CONFLICT(path) DO UPDATE SET tidied_at=excluded.tidied_at", (page, time.time() + 1))
        return done

    # ---- the portrait: what the assistant has in mind about the user in every conversation ----
    @property
    def portrait_file(self):
        return self.settings.data / "vault" / "我.md"

    def portrait(self):
        try:
            return self.portrait_file.read_text(encoding="utf-8").strip()[:4000]
        except OSError:
            return ""

    async def draw(self, model, now=None):
        """Rewrites the portrait when there are new memories, at most once a day. The user may edit the file; their text is the starting point."""
        now = now or time.time()
        rows = self.store.all("SELECT title,content,created_at,updated_at FROM memories WHERE enabled=1 ORDER BY importance DESC,updated_at DESC LIMIT 60")
        if len(rows) < PORTRAIT_FACTS:
            return False
        path = self.portrait_file
        written = path.stat().st_mtime if path.exists() else 0
        if written and (now - written < PORTRAIT_HOURS * 3600 or max(r["updated_at"] for r in rows) <= written):
            return False
        facts = "\n".join(f"- [{datetime.fromtimestamp(r['created_at']).strftime('%Y-%m-%d')}] {r['title']}: {r['content'][:300]}" for r in rows)
        reply = await self.llm.stream([{"role": "system", "content": PORTRAIT},
                                       {"role": "user", "content": f"Portrait so far:\n{self.portrait() or '(none yet)'}\n\nRemembered facts:\n{facts}"}],
                                      max_tokens=900, temperature=0.2, model=model, search=False)
        text = strip_markup(reply.text).strip()
        if not text:
            return False
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text[:4000] + "\n", encoding="utf-8")
        return True

    def prune(self):
        cutoff = time.time() - TEMPORARY_DAYS * 86400
        self.store.execute("DELETE FROM memories WHERE durable=0 AND source!='manual' AND updated_at<? AND COALESCE(used_at,0)<?", (cutoff, cutoff))
