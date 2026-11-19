"""Skills: instructions the assistant loads only when a task needs them.

A skill is a folder with a SKILL.md whose front matter gives a name and a
one-line description. Only the descriptions go into every prompt; the body is
read with the load_skill tool, which keeps the context small for local models.
The format is the SKILL.md convention other agents use, so existing skills work
as they are.

Skills come from three places, the first one winning when names clash:
data/skills/ (yours, edited in Jurvis), the folder shared with Claude Code
(~/.claude/skills), and <project>/.jurvis/skills of a connected code project.
"""
import json
import re
import shutil
from pathlib import Path

NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
FRONT = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
MAX_BODY = 24000


def parse(text):
    """(metadata, body) of a SKILL.md. The front matter is simple "key: value" lines."""
    match = FRONT.match(text)
    if not match:
        return {}, text.strip()
    meta = {}
    for line in match.group(1).splitlines():
        if ":" in line and not line.startswith((" ", "\t", "#")):
            key, value = line.split(":", 1)
            meta[key.strip().lower()] = value.strip().strip("\"'")
    return meta, match.group(2).strip()


class Skills:
    def __init__(self, settings):
        self.folder = settings.data / "skills"
        self.folder.mkdir(parents=True, exist_ok=True)
        shared = getattr(settings, "shared_skills", "")
        self.shared = Path(shared).expanduser() if shared else None
        self.projects = []                       # project skill folders seen in conversations, newest last
        self.switches = self.folder / ".off.json"  # skills Jurvis does not own cannot carry "enabled: false", so their switch lives here

    def path(self, name):
        if not NAME.match(name or ""):
            raise ValueError("技能名只能用小写字母、数字、- 和 _")
        return self.folder / name / "SKILL.md"

    def roots(self):
        places = [("personal", self.folder)]
        if self.shared and self.shared.is_dir():
            places.append(("shared", self.shared))
        return places + [("project", folder) for folder in self.projects[-3:] if folder.is_dir()]

    def use_project(self, root):
        """A conversation in a code project also gets that project's own skills."""
        folder = Path(root) / ".jurvis" / "skills"
        if folder in self.projects:
            self.projects.remove(folder)
        self.projects.append(folder)

    def off(self):
        try:
            return set(json.loads(self.switches.read_text()))
        except (OSError, ValueError):
            return set()

    def find(self, name):
        """(source, path of SKILL.md) of the skill that answers to this name."""
        if not NAME.match(name or ""):
            raise ValueError("技能名只能用小写字母、数字、- 和 _")
        for source, folder in self.roots():
            path = folder / name / "SKILL.md"
            if path.is_file():
                return source, path
        raise ValueError(f"没有名为 {name} 的技能")

    def list(self):
        out, seen, off = [], set(), self.off()
        for source, folder in self.roots():
            for path in sorted(folder.glob("*/SKILL.md")):
                name = path.parent.name
                if not NAME.match(name) or name in seen:
                    continue
                try:
                    meta, body = parse(path.read_text())
                except (OSError, UnicodeDecodeError):
                    continue
                seen.add(name)
                enabled = meta.get("enabled", "true").lower() != "false" if source == "personal" else name not in off
                out.append({"name": name, "title": meta.get("name") or name, "source": source,
                            "description": (meta.get("description") or body.split("\n", 1)[0])[:300], "enabled": enabled})
        return out

    def enabled(self):
        return [s for s in self.list() if s["enabled"]]

    def switch(self, name, enabled):
        """Turns a shared or project skill on or off without touching its files."""
        source, _ = self.find(name)
        if source == "personal":
            raise ValueError("自己的技能在 SKILL.md 里用 enabled: false 关闭，或直接删除")
        off = self.off() - {name} if enabled else self.off() | {name}
        self.switches.write_text(json.dumps(sorted(off)))
        return next(s for s in self.list() if s["name"] == name)

    def read(self, name, file=""):
        source, path = self.find(name)
        folder = path.parent
        if file:
            # A skill may keep reference material beside SKILL.md; it is read only when the instructions point to it.
            target = (folder / file).resolve()
            if not target.is_relative_to(folder.resolve()) or not target.is_file() or target.is_symlink():
                raise ValueError(f"技能 {name} 里没有文件 {file}")
            try:
                return {"name": name, "file": file, "content": target.read_text()[:MAX_BODY]}
            except UnicodeDecodeError:
                raise ValueError(f"{file} 不是文本文件")
        meta, body = parse(path.read_text())
        files = sorted(str(p.relative_to(folder)) for p in folder.rglob("*") if p.is_file() and p.name != "SKILL.md" and not p.name.startswith("."))[:50]
        result = {"name": name, "instructions": body[:MAX_BODY], "folder": str(folder)}
        if files:
            result["files"] = files
        return result

    def save(self, name, content):
        path = self.path(name)
        if len(content.encode()) > 200000:
            raise ValueError("技能文件超过 200 KB")
        meta, body = parse(content)
        if not body:
            raise ValueError("技能内容是空的")
        if not meta.get("description"):
            raise ValueError("SKILL.md 开头需要写 description，用一句话说明什么时候用它")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        return next(s for s in self.list() if s["name"] == name)

    def delete(self, name):
        folder = self.path(name).parent
        if folder.is_dir():
            shutil.rmtree(folder)

    def prompt(self):
        skills = self.enabled()
        if not skills:
            return ""
        lines = "\n".join(f"- {s['name']}: {s['description']}" for s in skills[:40])
        return ("\nSkills (instructions for specific kinds of tasks). Only when the user's request clearly is the kind of task a skill describes, call load_skill with its name first and follow what it says (it lists the skill's other files; read one by passing file); otherwise ignore them:\n" + lines + "\n")
