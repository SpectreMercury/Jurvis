"""A copy of what cannot be made again, taken every night: the database (conversations, memories, ledger, health, schedules),
the settings and keys, the notes and the workspace documents.

- Each copy is one folder named by its date, in data/backups or wherever the user points it (another disk, a synced folder).
  The newest few are kept. Generated media in data/assets is large and is copied only when the user asks for it.
- The database is copied with SQLite's own backup, so a copy taken while the engine is working is still whole.
- A copy holds the same keys and tokens as data/security: it is written owner-only, and a folder that syncs to a cloud
  sends them there. That is the user's decision to make, so the default stays on this Mac.

Restoring is done by hand with the engine stopped (docs/OPERATIONS.md): nothing here ever writes into data/.
"""
import json
import shutil
import sqlite3
import time
from datetime import datetime
from pathlib import Path

DEFAULTS = {"enabled": True, "folder": "", "keep": 7, "assets": False}
HOUR = 3                 # the nightly copy is taken at the first look after this hour
COPIED = ("security", "vault", "workspace", "skills")   # folders of data/ that go into every copy


class Backup:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.path = settings.data / "backup.json"

    def load(self):
        try:
            return {**DEFAULTS, **json.loads(self.path.read_text())}
        except (OSError, ValueError):
            return dict(DEFAULTS)

    def folder(self, state=None):
        chosen = (state or self.load())["folder"].strip()
        return Path(chosen).expanduser() if chosen else self.settings.data / "backups"

    def update(self, enabled=None, folder=None, keep=None, assets=None):
        state = self.load()
        if folder is not None:
            target = Path(folder.strip()).expanduser() if folder.strip() else None
            if target is not None:
                if not target.is_absolute():
                    raise ValueError("备份位置需要是完整路径，例如 /Volumes/Backup/Jurvis")
                data = self.settings.data.resolve()
                if target.resolve() == data or (data in target.resolve().parents and target.resolve() != data / "backups"):
                    raise ValueError("备份不能放进数据目录里别的文件夹")
                try:
                    target.mkdir(parents=True, exist_ok=True)
                except OSError as exc:
                    raise ValueError(f"这个位置写不进去：{exc}")
            state["folder"] = str(target) if target else ""
        if enabled is not None:
            state["enabled"] = bool(enabled)
        if keep is not None:
            if not 1 <= int(keep) <= 60:
                raise ValueError("保留份数需在 1 到 60 之间")
            state["keep"] = int(keep)
        if assets is not None:
            state["assets"] = bool(assets)
        self.remember(**{k: state[k] for k in DEFAULTS})
        return self.status()

    def copies(self, state=None):
        folder = self.folder(state)
        found = []
        for item in sorted(folder.glob("jurvis-*"), reverse=True) if folder.is_dir() else []:
            if item.is_dir() and (item / "jurvis.sqlite3").exists():
                found.append({"name": item.name, "path": str(item), "at": item.stat().st_mtime,
                              "bytes": sum(f.stat().st_size for f in item.rglob("*") if f.is_file())})
        return found

    def status(self):
        state = self.load()
        return {**{k: state[k] for k in DEFAULTS}, "location": str(self.folder(state)), "last_at": state.get("last_at"), "last_error": state.get("last_error"),
                "copies": self.copies(state)[:10]}

    def due(self, now=None):
        """True once a day, from HOUR on, until a copy has been taken."""
        state, now = self.load(), datetime.fromtimestamp(now or time.time())
        last = datetime.fromtimestamp(state.get("last_at") or 0)
        tried = datetime.fromtimestamp(state.get("tried_at") or 0)
        return state["enabled"] and now.hour >= HOUR and last.date() != now.date() and (now - tried).total_seconds() > 3600

    def run(self, now=None):
        """Takes a copy now. Returns it as status() lists it. Blocking: call it off the event loop."""
        state, now = self.load(), now or time.time()
        target = self.folder(state) / f"jurvis-{datetime.fromtimestamp(now):%Y%m%d-%H%M%S}"
        partial = target.with_name(target.name + ".partial")
        try:
            partial.mkdir(parents=True, exist_ok=False)
            partial.chmod(0o700)
            source = sqlite3.connect(self.store.path, timeout=30)
            copy = sqlite3.connect(partial / "jurvis.sqlite3")
            try:
                source.backup(copy)
            finally:
                copy.close()
                source.close()
            data = self.settings.data
            for item in data.glob("*.json"):
                shutil.copy2(item, partial / item.name)
            for name in COPIED + (("assets",) if state["assets"] else ()):
                if (data / name).is_dir():
                    shutil.copytree(data / name, partial / name, symlinks=True, ignore=shutil.ignore_patterns(".history", "*.tmp"))
            (partial / "jurvis.sqlite3").chmod(0o600)
            partial.rename(target)
        except (OSError, sqlite3.Error) as exc:
            shutil.rmtree(partial, ignore_errors=True)
            self.remember(tried_at=now, last_error=str(exc)[:300])
            raise ValueError(f"备份没有完成：{exc}")
        for old in self.copies(state)[state["keep"]:]:
            shutil.rmtree(old["path"], ignore_errors=True)
        self.remember(last_at=now, tried_at=now, last_error=None)
        self.store.event("backup", target.name)
        return next(c for c in self.copies(state) if c["name"] == target.name)

    def remember(self, **changes):
        try:
            state = json.loads(self.path.read_text())
        except (OSError, ValueError):
            state = {}
        self.path.write_text(json.dumps({**state, **changes}, ensure_ascii=False))
