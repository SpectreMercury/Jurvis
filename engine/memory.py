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

from .runtime import strip_markup
from .store import dumps, new_id

EXCERPTS = 3             # earlier exchanges of the same conversation brought back verbatim for one reply
EXCERPT_FLOOR = 0.5      # share (by rarity) of the request's known terms an earlier exchange must contain to be brought back
LEXICAL_FLOOR = 0.08
DUPLICATE = 0.82         # lexical similarity above which a new memory updates an old one
TEMPORARY_DAYS = 30

EXTRACT = """You maintain the long-term memory of a personal assistant. From the conversation below, extract facts worth remembering in FUTURE conversations:
stable preferences, facts about the user and people or things in their life, ongoing projects, decisions and their reasons, and corrections of how the assistant should work.
Do NOT extract: passwords, keys, tokens or other secrets; one-off requests; anything that is only relevant to this conversation; things the assistant said that the user did not confirm; facts already in the existing memories.
Write each memory in the language the user used, as one self-contained statement that makes sense without the conversation.
Reply with JSON only, an array (empty if nothing qualifies) of:
{"title": "short label", "content": "the fact", "importance": 0.0-1.0, "durable": true for lasting facts or false for things that expire within weeks, "tags": ["..."]}"""

SUMMARIZE = """Summarize the earlier part of a conversation between a user and their assistant so the assistant can continue it later without the original messages.
Keep, with the exact names, numbers, dates and wording that later questions may refer to: who the user is and what they want, every decision and agreed figure,
each thing the assistant produced (lists with their numbered items in one line each, drafts with their title and opening line, files with their names), and what is still open.
When a "Summary so far" is given, keep everything in it that still matters and add the new messages; never drop an earlier topic just because the conversation moved on.
Drop small talk. Write in the language of the conversation, at most 600 words, as short labelled lines grouped by topic."""

REWRITE = """Rewrite the user's latest message as a short standalone search query for a memory store about this user (their preferences, people, projects, decisions).
Use the conversation only to resolve references like "it" or "那个". Reply with the query only, in the user's language."""

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

    def prefs(self):
        return self.llm.prefs.get()["memory"]

    # ---- writing ----
    def save(self, title, content, *, importance=0.5, durable=True, tags=(), source="manual", session_id=None):
        """Adds a memory, or updates the one that already says nearly the same thing. Vectors are added later by index()."""
        title, content = title.strip()[:100], content.strip()[:4000]
        importance = min(1.0, max(0.0, float(importance)))
        tags = [str(t)[:40] for t in tags if str(t).strip()][:8]
        wanted, now = features(title + " " + content), time.time()
        for row in self.store.all("SELECT id,title,content FROM memories"):
            if lexical(wanted, features(row["title"] + " " + row["content"])) >= DUPLICATE:
                self.store.execute("UPDATE memories SET title=?,content=?,importance=MAX(importance,?),durable=MAX(durable,?),tags=?,updated_at=?,embedding=NULL,embed_model=NULL WHERE id=?",
                                   (title, content, importance, int(durable), dumps(tags), now, row["id"]))
                self.store.event("memories", row["id"])
                return {"saved": row["id"], "title": title, "updated": True}
        ident = new_id()
        self.store.execute("INSERT INTO memories(id,title,content,created_at,updated_at,importance,durable,tags,source,session_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
                           (ident, title, content, now, now, importance, int(durable), dumps(tags), source, session_id))
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
            return {"memories": 0, "summarized": 0}
        model = self.llm.tool_model(session.get("model"))
        saved = await self.extract(session, model) if self.prefs()["auto_extract"] else 0
        summarized = 0  # the conversation itself folds old messages into its summary when they stop fitting (Agent.context)
        await self.index()
        self.prune()
        return {"memories": saved, "summarized": summarized}

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
        reply = await self.llm.stream([{"role": "system", "content": EXTRACT},
                                       {"role": "user", "content": f"Existing memories:\n{existing}\n\nConversation:\n{self.transcript(rows)}"}],
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
            self.save(title, content, importance=min(importance, 0.85), durable=bool(item.get("durable", True)), tags=tags, source="chat", session_id=session["id"])
            saved += 1
        self.store.execute("UPDATE sessions SET memory_upto=? WHERE id=?", (rows[-1]["created_at"], session["id"]))
        return saved

    async def fold(self, session, rows, ask):
        """Folds messages that no longer fit the context into the conversation summary. ask is the model call of the conversation."""
        if not rows:
            return False
        prior = session.get("summary") or ""
        content = (f"Summary so far:\n{prior}\n\n" if prior else "") + "Messages to fold in:\n" + self.transcript(rows, 24000)
        reply = await ask([{"role": "system", "content": SUMMARIZE}, {"role": "user", "content": content}], max_tokens=1400, temperature=0.2, search=False)
        text = reply.text.strip()
        if not text:
            return False
        self.store.execute("UPDATE sessions SET summary=?,summary_upto=? WHERE id=?", (text[:6000], rows[-1]["created_at"], session["id"]))
        return True

    def excerpts(self, session_id, upto, request):
        """Earlier exchanges of this conversation, now folded into the summary, that the request seems to refer to: (question, answer) pairs, verbatim."""
        wanted = {k: v for k, v in features(request).items() if len(k) > 1}  # single characters match everything
        if len(wanted) < 2 or not upto:
            return []
        rows = self.store.all("SELECT role,content FROM messages WHERE session_id=? AND status='complete' AND role IN ('user','assistant') AND created_at<=? ORDER BY created_at",
                              (session_id, upto))
        pairs = []
        for row in rows:
            if row["role"] == "user" or not pairs:
                pairs.append(["", ""])
            pairs[-1][0 if row["role"] == "user" else 1] += row["content"]
        bags = []
        for question, answer in pairs:
            bag = features(answer[:6000])
            bag.update(features(question)); bag.update(features(question))  # the question names the topic ("写一条朋友圈文案"), so it counts more
            bags.append(bag)
        # Only terms this conversation actually used can tell exchanges apart, and the rarer a term the more it tells.
        weight = {k: math.log(1 + len(bags) / n) for k in wanted if (n := sum(1 for bag in bags if k in bag))}
        total, scored = sum(weight.values()), []
        for index, bag in enumerate(bags):
            hits = [k for k in weight if k in bag]
            if len(hits) >= 2 and sum(weight[k] for k in hits) / total >= EXCERPT_FLOOR:
                scored.append((sum(weight[k] for k in hits) / total, index))
        best = sorted(index for _, index in sorted(scored, reverse=True)[:EXCERPTS])
        return [{"user": pairs[i][0][:600], "assistant": pairs[i][1][:2400]} for i in best]

    def prune(self):
        cutoff = time.time() - TEMPORARY_DAYS * 86400
        self.store.execute("DELETE FROM memories WHERE durable=0 AND source!='manual' AND updated_at<? AND COALESCE(used_at,0)<?", (cutoff, cutoff))
