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
        self.pending = {}  # (message_id, call_id) -> Future[str]: "run" | "deny" | "always"

    def decide(self, message_id, call_id, decision):
        future = self.pending.get((message_id, call_id))
        if not future or future.done():
            raise ValueError("这条命令已经不在等待确认")
        future.set_result(decision)

    def needs_approval(self, session_id, command):
        row = self.store.one("SELECT allow_commands FROM sessions WHERE id=?", (session_id,))
        return risky(command) or not (row and row["allow_commands"])

    async def wait(self, session_id, message_id, call_id):
        """Waits for the user's answer; returns "run", "deny" or "timeout"."""
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

    def directory(self, cwd, default):
        path = Path(os.path.expanduser(cwd)) if cwd else Path(default)
        if not path.is_absolute():
            path = Path(default) / path
        if not path.is_dir():
            raise ValueError(f"目录不存在：{path}")
        return path

    async def run(self, command, cwd, timeout, on_output):
        env = dict(os.environ, CI="1", PYTHONUNBUFFERED="1", TERM="dumb", NONINTERACTIVE="1", HOMEBREW_NO_AUTO_UPDATE="1",
                   GIT_TERMINAL_PROMPT="0", PIP_NO_INPUT="1")
        started = time.monotonic()
        process = await asyncio.create_subprocess_exec("/bin/zsh", "-lc", command, cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL,
                                                       stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True)
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
