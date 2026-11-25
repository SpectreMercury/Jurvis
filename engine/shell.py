"""Shell commands for the assistant: the user approves each one in the chat, then it runs on the Mac.

A command waits in the reply's trace as "awaiting" until the user taps 运行 or 拒绝
(or allows every command in that conversation). Risky commands always ask, even then.
"""
import asyncio
import os
import re
import signal
import time
from pathlib import Path


APPROVAL_SECONDS = 900
OUTPUT_CHARS = 40000
BACKGROUND_LIMIT = 6       # commands left running at once

# Always ask for these, even when the conversation allows commands.
RISKY = re.compile(r"""(^|[;&|(`]\s*|\s)(sudo|su|rm\s+-\w*[rf]|rm\s+.*\s-\w*[rf]|git\s+(push|reset\s+--hard|clean|checkout\s+--|branch\s+-D)|mkfs|diskutil|dd\s|shutdown|reboot|halt|launchctl|kill|killall|pkill|chmod\s+-R|chown|csrutil|defaults\s+write|security\s|osascript)\b""")


def risky(command):
    return bool(RISKY.search(command))


def excerpt(text, limit=8000):
    """Head and tail of long output: install logs put the error at the end."""
    if len(text) <= limit:
        return text
    head = limit // 4
    return text[:head] + f"\n…({len(text) - limit} chars omitted)…\n" + text[-(limit - head):]


class Shell:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.pending = {}  # (message_id, call_id) -> Future[str]: "run" | "deny" | "always" | "forever"
        self.allowed = set()  # (session_id, tool name): external tools the user allowed for the rest of a conversation, until the engine restarts
        self.background = {}  # command id -> a command left running (a dev server, a watcher) and what it has printed
        self.started = 0

    def decide(self, message_id, call_id, decision):
        future = self.pending.get((message_id, call_id))
        if not future or future.done():
            raise ValueError("这条命令已经不在等待确认")
        future.set_result(decision)

    def needs_approval(self, session_id, command):
        row = self.store.one("SELECT allow_commands FROM sessions WHERE id=?", (session_id,))
        return risky(command) or not (row and row["allow_commands"])

    async def wait(self, session_id, message_id, call_id):
        """Waits for the user's answer; returns "run", "forever" (run, and this kind of command from now on), "deny" or "timeout"."""
        future = asyncio.get_running_loop().create_future()
        self.pending[(message_id, call_id)] = future
        try:
            decision = await asyncio.wait_for(future, APPROVAL_SECONDS)
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            self.pending.pop((message_id, call_id), None)
        if decision == "always":
            self.store.execute("UPDATE sessions SET allow_commands=1 WHERE id=?", (session_id,))
            self.store.event("sessions", session_id)
            return "run"
        return decision

    async def ask(self, message_id, call_id):
        """Waits for the user's answer to any approval card; returns "run", "always", "forever", "deny" or "timeout"."""
        future = asyncio.get_running_loop().create_future()
        self.pending[(message_id, call_id)] = future
        try:
            return await asyncio.wait_for(future, APPROVAL_SECONDS)
        except asyncio.TimeoutError:
            return "timeout"
        finally:
            self.pending.pop((message_id, call_id), None)

    def directory(self, cwd, default):
        path = Path(os.path.expanduser(cwd)) if cwd else Path(default)
        if not path.is_absolute():
            path = Path(default) / path
        if not path.is_dir():
            raise ValueError(f"目录不存在：{path}")
        return path

    @staticmethod
    async def spawn(command, cwd):
        env = dict(os.environ, CI="1", PYTHONUNBUFFERED="1", TERM="dumb", NONINTERACTIVE="1", HOMEBREW_NO_AUTO_UPDATE="1",
                   GIT_TERMINAL_PROMPT="0", PIP_NO_INPUT="1")
        return await asyncio.create_subprocess_exec("/bin/zsh", "-lc", command, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
                                                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)

    async def start(self, command, cwd):
        """Starts a command that keeps running (a server, a watcher) and returns once it has had a moment to fail or print."""
        if sum(1 for r in self.background.values() if r["process"].returncode is None) >= BACKGROUND_LIMIT:
            raise ValueError(f"已有 {BACKGROUND_LIMIT} 条后台命令在运行，请先用 stop_command 停掉不用的")
        process = await self.spawn(command, cwd)
        self.started += 1
        # Short ids in order (c1, c2): a small model copies those back correctly, which it does not always manage with random hex.
        record = {"id": f"c{self.started}", "command": command, "cwd": str(cwd), "process": process, "output": "", "started": time.monotonic()}

        async def collect():
            while chunk := await process.stdout.read(4096):
                record["output"] = (record["output"] + chunk.decode(errors="replace"))[-OUTPUT_CHARS:]
            await process.wait()
        record["task"] = asyncio.create_task(collect())
        self.background[record["id"]] = record
        for stale in [k for k, r in self.background.items() if r["process"].returncode is not None][:-20]:
            del self.background[stale]
        await asyncio.wait([record["task"]], timeout=1.5)
        return self.output(record["id"])

    def find(self, ident):
        """The background command with this id; a wrong id is answered with the ones that exist, so the next call can be right."""
        record = self.background.get(str(ident).strip().lower())
        if record:
            return record
        known = "；".join(f"{r['id']}（{'运行中' if r['process'].returncode is None else '已结束'}）{r['command'][:60]}" for r in self.background.values())
        raise ValueError(f"没有编号为 {ident} 的后台命令。" + (f"现有的后台命令：{known}" if known else "现在没有后台命令"))

    def output(self, ident):
        """What a background command has printed so far and whether it is still running."""
        record = self.find(ident)
        ident, code = record["id"], record["process"].returncode
        return {"command_id": ident, "command": record["command"], "cwd": record["cwd"], "running": code is None, "exit_code": code,
                "seconds": round(time.monotonic() - record["started"], 1), "output": excerpt(record["output"])}

    async def kill(self, ident):
        record = self.find(ident)
        await self.stop(record["process"])
        await asyncio.wait([record["task"]], timeout=3)
        return self.output(record["id"])

    async def close(self):
        """Nothing started here outlives the engine."""
        for record in self.background.values():
            if record["process"].returncode is None:
                await self.stop(record["process"])

    async def run(self, command, cwd, timeout, on_output):
        started = time.monotonic()
        process = await self.spawn(command, cwd)
        output, timed_out = "", False

        async def collect():
            nonlocal output
            while chunk := await process.stdout.read(4096):
                output = (output + chunk.decode(errors="replace"))[-OUTPUT_CHARS:]
                await on_output(output)
            return await process.wait()
        try:
            code = await asyncio.wait_for(collect(), timeout)
        except asyncio.TimeoutError:
            timed_out, code = True, None
            await self.stop(process)
        except BaseException:
            await self.stop(process)
            raise
        return {"command": command, "cwd": str(cwd), "exit_code": code, "timed_out": timed_out,
                "duration": round(time.monotonic() - started, 1), "output": output}

    @staticmethod
    async def stop(process):
        for sig, wait in ((signal.SIGTERM, 3), (signal.SIGKILL, 3)):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                return
            try:
                await asyncio.wait_for(process.wait(), wait)
                return
            except asyncio.TimeoutError:
                continue
