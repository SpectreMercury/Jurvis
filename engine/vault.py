"""The vault: what Jurvis knows, as Markdown the user can read and correct.

data/vault/ is a folder of plain Markdown pages, laid out so that it opens as an Obsidian vault:

    我.md        the portrait kept in mind in every conversation (engine/memory.py)
    人物/        one page per person in the user's life
    主题/        one page per subject; a long-term goal's page is here too
    习惯/        how the user wants things done
    技能/        the skills folder (data/skills), linked in
    日记/        one page per day: what was talked about, stretch by stretch

A page under 人物/, 主题/ or 习惯/ is a list of facts, and each fact is one row in the memories table. The two are
kept the same in both directions: a fact Jurvis learns appears in its page, and a line the user adds, changes or
removes in a page is a memory added, changed or switched off. The diary is written by Jurvis and not read back.
"""
import hashlib
import re
import time
from datetime import datetime

from .store import new_id

FOLDERS = ("人物", "主题", "习惯")
DEFAULT = "主题/杂记"
KINDS = {"人物": "人物", "主题": "主题", "习惯": "习惯"}
SOURCES = "来源："
DIARY_DAYS = 3           # days whose diary page is brought up to date on each pass
LINK = re.compile(r"\[\[([^\]|]+)(?:\|[^\]]*)?\]\]")
BULLET = re.compile(r"^\s*(?:[-*+•]|\d+[.)、])\s+")
UNSAFE = re.compile(r'[\\/:*?"<>|#^\[\]\x00-\x1f]')


def clean(name):
    """A page name that is safe as a file name."""
    return UNSAFE.sub(" ", str(name or "")).strip(" .")[:40].strip()


def page_of(text):
    """ "人物/妈妈" or "主题/健身" from what a model or a tag gave; None when it names no page."""
    folder, _, name = str(text or "").replace("：", "/").replace(":", "/").partition("/")
    folder, name = folder.strip(), clean(name.removesuffix(".md"))
    return f"{folder}/{name}" if folder in FOLDERS and name else None


def plain(text):
    """A fact as it is compared: without link brackets and with its spaces evened out."""
    return " ".join(LINK.sub(r"\1", text).split())


def digest(text):
    return hashlib.sha1(text.encode("utf-8")).hexdigest()


def named(record):
    """A record as the user reads it. A short stretch is its own record, word for word: there the two speakers get their names."""
    return re.sub(r"(?m)^User: ", "我：", re.sub(r"(?m)^Assistant: ", "Jurvis：", record))


class Vault:
    def __init__(self, settings, store):
        self.root, self.skills, self.store = settings.data / "vault", settings.data / "skills", store
        with store.connection() as db:
            # What each page looked like when Jurvis last wrote or read it: a file that differs was edited by the user.
            db.execute("CREATE TABLE IF NOT EXISTS pages(path TEXT PRIMARY KEY,hash TEXT NOT NULL,tidied_at REAL NOT NULL DEFAULT 0)")
            if "page" not in [r[1] for r in db.execute("PRAGMA table_info(memories)")]:
                db.execute("ALTER TABLE memories ADD COLUMN page TEXT")
            if "page" not in [r[1] for r in db.execute("PRAGMA table_info(goals)")]:
                db.execute("ALTER TABLE goals ADD COLUMN page TEXT")

    def file(self, page):
        return self.root / (page + ".md")

    def pages(self):
        """Every page that has facts, as "folder/name"."""
        return [r["page"] for r in self.store.all("SELECT DISTINCT page FROM memories WHERE enabled=1 AND page IS NOT NULL ORDER BY page")]

    # ---- reading a page the user edited ----
    @staticmethod
    def parse(text):
        """The facts in a page: every line that is not front matter, a heading or the sources line."""
        lines = text.replace("\r\n", "\n").split("\n")
        if lines and lines[0].strip() == "---":
            end = next((n for n, line in enumerate(lines[1:], 1) if line.strip() == "---"), 0)
            lines = lines[end + 1:]
        facts = []
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or line.startswith(SOURCES):
                continue
            fact = plain(BULLET.sub("", line))
            if fact and fact not in facts:
                facts.append(fact[:4000])
        return facts

    def take(self, page, text):
        """Makes the memories of a page match what the user left in its file. Returns whether anything changed."""
        facts, now, changed = self.parse(text), time.time(), False
        rows = self.store.all("SELECT id,content FROM memories WHERE page=? AND enabled=1", (page,))
        kept = {plain(r["content"]): r["id"] for r in rows}
        for key, ident in kept.items():
            if key not in facts:
                # Taken out of the page: switched off rather than deleted, so it can be brought back from the 记忆 screen.
                self.store.execute("UPDATE memories SET enabled=0,updated_at=? WHERE id=?", (now, ident))
                self.store.event("memories", ident); changed = True
        name = page.split("/", 1)[1]
        for fact in facts:
            if fact not in kept:
                ident = new_id()
                self.store.execute("INSERT INTO memories(id,title,content,created_at,updated_at,importance,durable,tags,source,page) VALUES(?,?,?,?,?,0.7,1,'[]','vault',?)",
                                   (ident, name[:100], fact, now, now, page))
                self.store.event("memories", ident); changed = True
        return changed

    # ---- writing a page ----
    def render(self, page, rows, people):
        folder, name = page.split("/", 1)
        def linked(text):
            for person in people:
                if person != name and len(person) >= 2 and person in text and f"[[{person}]]" not in text:
                    text = text.replace(person, f"[[{person}]]", 1)
            return text
        days = sorted({datetime.fromtimestamp(r["created_at"]).strftime("%Y-%m-%d") for r in rows})[-6:]
        updated = datetime.fromtimestamp(max(r["updated_at"] for r in rows)).strftime("%Y-%m-%d")
        body = "\n".join("- " + linked(" ".join(r["content"].split())) for r in rows)
        return f"---\n类型: {KINDS[folder]}\n更新: {updated}\n---\n# {name}\n\n{body}\n\n{SOURCES}" + "、".join(f"[[{d}]]" for d in days) + "\n"

    def sync(self):
        """One pass in both directions: the user's edits are taken in, then every page is written as the memories now are.
        Returns {"taken": pages read from the user, "written": pages written}."""
        self.root.mkdir(parents=True, exist_ok=True)
        if self.skills.exists() and not (self.root / "技能").exists():
            try:
                (self.root / "技能").symlink_to(self.skills, target_is_directory=True)
            except OSError:
                pass
        seen = {r["path"]: r["hash"] for r in self.store.all("SELECT path,hash FROM pages")}
        taken = written = 0
        for folder in FOLDERS:
            for path in sorted((self.root / folder).glob("*.md")) if (self.root / folder).is_dir() else []:
                page = f"{folder}/{path.stem}"
                try:
                    text = path.read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    continue
                if seen.get(page) != digest(text):
                    taken += self.take(page, text)
                    self.store.execute("INSERT INTO pages(path,hash) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET hash=excluded.hash", (page, digest(text)))
                    seen[page] = digest(text)
        # A page the user deleted: its facts are switched off with it.
        for page in seen:
            if page.split("/", 1)[0] in FOLDERS and not self.file(page).exists():
                gone = self.store.execute("UPDATE memories SET enabled=0,updated_at=? WHERE page=? AND enabled=1", (time.time(), page))
                self.store.execute("DELETE FROM pages WHERE path=?", (page,))
                taken += bool(gone)
        # Memories that have no page yet get one: their first tag when it names one, else the catch-all.
        for row in self.store.all("SELECT id,tags FROM memories WHERE page IS NULL"):
            page = next((p for p in (page_of(t) or page_of("主题/" + clean(t)) for t in row["tags"]) if p), DEFAULT)
            self.store.execute("UPDATE memories SET page=? WHERE id=?", (page, row["id"]))
        people = [p.split("/", 1)[1] for p in self.pages() if p.startswith("人物/")]
        for page in self.pages():
            rows = self.store.all("SELECT content,created_at,updated_at FROM memories WHERE page=? AND enabled=1 ORDER BY importance DESC,created_at", (page,))
            text, path = self.render(page, rows, people), self.file(page)
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                written += 1
            if seen.get(page) != digest(text):
                self.store.execute("INSERT INTO pages(path,hash) VALUES(?,?) ON CONFLICT(path) DO UPDATE SET hash=excluded.hash", (page, digest(text)))
        # A page whose facts are all gone goes too, unless the user has written in it since.
        live = set(self.pages())
        for page, _ in list(seen.items()):
            path = self.file(page)
            if page.split("/", 1)[0] in FOLDERS and page not in live and path.exists():
                row = self.store.one("SELECT hash FROM pages WHERE path=?", (page,))
                if row and row["hash"] == digest(path.read_text(encoding="utf-8")):
                    path.unlink()
                    self.store.execute("DELETE FROM pages WHERE path=?", (page,))
        written += self.diary()
        return {"taken": taken, "written": written}

    # ---- the diary ----
    def diary(self, now=None):
        """Writes the page of each recent day from the records of that day's stretches. Returns how many pages changed."""
        now = now or time.time()
        rows = self.store.all("""SELECT s.id,s.title,d.text,d.upto FROM digests d JOIN sessions s ON s.id=d.session_id
                                 WHERE s.incognito=0 AND d.upto>=? ORDER BY d.upto""", (now - DIARY_DAYS * 86400,))
        days, changed = {}, 0
        for row in rows:
            days.setdefault(datetime.fromtimestamp(row["upto"]).strftime("%Y-%m-%d"), []).append(row)
        for day, stretches in days.items():
            parts = [f"---\n类型: 日记\n---\n# {day}\n"]
            for row in stretches:
                touched = [r["page"].split("/", 1)[1] for r in self.store.all(
                    "SELECT DISTINCT page FROM memories WHERE session_id=? AND enabled=1 AND page IS NOT NULL ORDER BY page", (row["id"],))]
                said = named(row["text"].strip())
                parts.append(f"## {datetime.fromtimestamp(row['upto']).strftime('%H:%M')} {row['title']}\n\n{said}\n"
                             + ("\n涉及：" + "、".join(f"[[{name}]]" for name in touched) + "\n" if touched else ""))
            text, path = "\n".join(parts), self.root / "日记" / (day + ".md")
            if not path.exists() or path.read_text(encoding="utf-8") != text:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
                changed += 1
        return changed
