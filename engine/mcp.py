"""Model Context Protocol client: tools from MCP servers become assistant tools.

Servers are listed in data/security/mcp.json (it may hold tokens, so it stays
private):

    {"servers": {"files": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-filesystem", "/Users/me/Documents"]},
                 "notes": {"url": "https://example.com/mcp", "headers": {"Authorization": "Bearer ..."}, "trusted": true}}}

Both transports are supported: a local process speaking JSON-RPC over stdio, and
streamable HTTP. A tool that is not marked read-only by its server asks the user
before it runs, unless the server is marked "trusted".
"""
import asyncio
import itertools
import json
import os
import re

import httpx

from . import __version__
from .llm import Prefs

PROTOCOL = "2025-06-18"
CLIENT = {"name": "jurvis", "version": __version__}
SAFE = re.compile(r"[^a-zA-Z0-9_-]")
CALL_SECONDS = 120


class McpError(Exception):
    pass


class Server:
    def __init__(self, name, config):
        self.name, self.config = name, config
        self.tools, self.error, self.process, self.reader = [], None, None, None
        self.waiting, self.ids = {}, itertools.count(1)
        self.session, self.version, self.lock = None, PROTOCOL, asyncio.Lock()

    # ---- transports ----
    async def start(self):
        if self.config.get("command"):
            env = {**os.environ, **{str(k): str(v) for k, v in (self.config.get("env") or {}).items()}}
            self.process = await asyncio.create_subprocess_exec(self.config["command"], *map(str, self.config.get("args") or []), env=env, limit=8 * 1024 * 1024,
                                                                stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
            self.reader = asyncio.create_task(self.read())
        elif not str(self.config.get("url", "")).startswith(("http://", "https://")):
            raise McpError("需要 command 或 url")
        result = await self.request("initialize", {"protocolVersion": PROTOCOL, "capabilities": {}, "clientInfo": CLIENT})
        self.version = result.get("protocolVersion") or PROTOCOL
        await self.notify("notifications/initialized")
        self.tools, cursor = [], None
        for _ in range(20):
            page = await self.request("tools/list", {"cursor": cursor} if cursor else {})
            self.tools.extend(t for t in page.get("tools") or [] if isinstance(t, dict) and t.get("name"))
            cursor = page.get("nextCursor")
            if not cursor:
                break

    async def read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except json.JSONDecodeError:
                    continue
                future = self.waiting.pop(message.get("id"), None) if isinstance(message, dict) else None
                if future and not future.done():
                    future.set_result(message)
        finally:
            for future in self.waiting.values():
                if not future.done():
                    future.set_exception(McpError("服务已退出"))
            self.waiting.clear()

    async def notify(self, method):
        await self.send({"jsonrpc": "2.0", "method": method}, expect=False)

    async def request(self, method, params, timeout=30):
        ident = next(self.ids)
        message = await asyncio.wait_for(self.send({"jsonrpc": "2.0", "id": ident, "method": method, "params": params}, expect=True, ident=ident), timeout)
        if message.get("error"):
            raise McpError(str(message["error"].get("message") or message["error"])[:500])
        return message.get("result") or {}

    async def send(self, payload, expect, ident=None):
        if self.process:
            if self.process.returncode is not None:
                raise McpError("服务已退出")
            future = None
            if expect:
                future = asyncio.get_running_loop().create_future()
                self.waiting[ident] = future
            self.process.stdin.write((json.dumps(payload) + "\n").encode())
            await self.process.stdin.drain()
            return await future if future else None
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json", "MCP-Protocol-Version": self.version,
                   **{str(k): str(v) for k, v in (self.config.get("headers") or {}).items()}}
        if self.session:
            headers["Mcp-Session-Id"] = self.session
        async with self.lock:
            async with httpx.AsyncClient(timeout=httpx.Timeout(CALL_SECONDS, connect=15)) as client:
                async with client.stream("POST", self.config["url"], json=payload, headers=headers) as response:
                    if response.status_code >= 400:
                        raise McpError(f"服务返回 {response.status_code}")
                    self.session = response.headers.get("mcp-session-id") or self.session
                    if not expect:
                        return None
                    if "text/event-stream" in response.headers.get("content-type", ""):
                        async for line in response.aiter_lines():
                            if line.startswith("data:"):
                                try:
                                    message = json.loads(line[5:].strip())
                                except json.JSONDecodeError:
                                    continue
                                if isinstance(message, dict) and message.get("id") == ident:
                                    return message
                        raise McpError("服务没有返回结果")
                    return json.loads(await response.aread())

    async def call(self, tool, arguments):
        result = await self.request("tools/call", {"name": tool, "arguments": arguments}, timeout=CALL_SECONDS)
        parts = []
        for block in result.get("content") or []:
            if block.get("type") == "text":
                parts.append(str(block.get("text", "")))
            elif block.get("type") == "resource" and isinstance(block.get("resource"), dict):
                parts.append(str(block["resource"].get("text") or block["resource"].get("uri") or ""))
            else:
                parts.append(f"[{block.get('type', 'content')} omitted]")
        text = "\n".join(p for p in parts if p)
        if not text and result.get("structuredContent") is not None:
            text = json.dumps(result["structuredContent"], ensure_ascii=False)
        if result.get("isError"):
            raise McpError(text[:1000] or "工具报告失败")
        return text

    async def close(self):
        if self.reader:
            self.reader.cancel()
        if self.process and self.process.returncode is None:
            self.process.terminate()
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except asyncio.TimeoutError:
                self.process.kill()
        self.process = None


class Mcp:
    def __init__(self, settings):
        self.path = settings.data / "security" / "mcp.json"
        self.servers = {}

    def config(self):
        servers = Prefs.load(self.path).get("servers")
        return {str(k): v for k, v in servers.items() if isinstance(v, dict)} if isinstance(servers, dict) else {}

    def save(self, name, config):
        if SAFE.search(name) or not name or len(name) > 32:
            raise ValueError("服务名只能用字母、数字、- 和 _")
        if not config.get("command") and not str(config.get("url", "")).startswith(("http://", "https://")):
            raise ValueError("请填写启动命令或 http(s) 地址")
        servers = self.config()
        servers[name] = config
        Prefs.save(self.path, {"servers": servers}, private=True)

    def remove(self, name):
        servers = self.config()
        if servers.pop(name, None) is not None:
            Prefs.save(self.path, {"servers": servers}, private=True)

    async def connect(self):
        """(Re)connects every enabled server. A server that fails keeps its error for the settings page."""
        await self.close()
        for name, config in self.config().items():
            if config.get("enabled") is False:
                continue
            server = Server(name, config)
            self.servers[name] = server
            try:
                await asyncio.wait_for(server.start(), 45)
            except (McpError, OSError, asyncio.TimeoutError, httpx.HTTPError, json.JSONDecodeError) as exc:
                server.error = (str(exc) or type(exc).__name__)[:300]
                await server.close()

    async def close(self):
        for server in self.servers.values():
            await server.close()
        self.servers = {}

    def status(self):
        out = []
        for name, config in self.config().items():
            server = self.servers.get(name)
            out.append({"name": name, "transport": "stdio" if config.get("command") else "http", "target": config.get("command") or config.get("url"),
                        "enabled": config.get("enabled") is not False, "trusted": bool(config.get("trusted")),
                        "connected": bool(server and not server.error), "error": server.error if server else None,
                        "tools": [{"name": t["name"], "description": str(t.get("description") or "")[:200]} for t in (server.tools if server else [])]})
        return out

    def tools(self):
        """(tool name for the model, server, MCP tool definition, needs approval)."""
        out = []
        for name, server in self.servers.items():
            if server.error:
                continue
            for tool in server.tools:
                exposed = ("mcp__" + SAFE.sub("_", name) + "__" + SAFE.sub("_", tool["name"]))[:64]
                read_only = bool((tool.get("annotations") or {}).get("readOnlyHint"))
                out.append((exposed, server, tool, not (read_only or server.config.get("trusted"))))
        return out
