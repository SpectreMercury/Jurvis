"""Model routing: the local MLX model and cloud models behind one interface.

A model is named "local" or "<provider>:<model>". Providers speak the OpenAI Chat
Completions protocol; Miavo (https://api.miavo.xyz/v1) is built in and is the
default cloud provider, with web search served by the gateway itself. Other
OpenAI-compatible endpoints (OpenAI, DeepSeek, OpenRouter, Ollama, LM Studio...) can
be added. Keys live in data/security/providers.json, readable only by the user.
"""
import json
import os
import re
import secrets
import httpx

from .runtime import Completion, extract_text_tool_calls, read_stream, strip_markup

LOCAL = "local"
MIAVO = {"title": "Miavo", "base_url": "https://api.miavo.xyz/v1", "api_key": "", "web_search": True, "builtin": True}
PROVIDER_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
DEFAULTS = {
    "chat_model": LOCAL,      # used when a conversation does not pick its own
    "tool_model": "",         # cheap model for summaries, memory extraction and query rewriting; empty = chat model
    "embed_model": "",        # "<provider>:<model>" for semantic memory search; empty = built-in lexical matching
    "web_search": True,
    "memory": {"enabled": True, "auto_extract": True, "auto_retrieve": True, "max_retrieved": 8, "threshold": 0.3},
    "home": {"confirm": "sensitive"},  # "sensitive": locks, doors, alarms and cameras ask first; "always": every action asks
}


def merged(base, extra):
    out = dict(base)
    for key, value in (extra or {}).items():
        out[key] = merged(base[key], value) if isinstance(value, dict) and isinstance(base.get(key), dict) else value
    return out


class Prefs:
    """User settings (data/settings.json) and provider credentials (data/security/providers.json)."""

    def __init__(self, settings):
        self.path = settings.data / "settings.json"
        self.secret = settings.data / "security" / "providers.json"
        self.secret.parent.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def load(path):
        try:
            value = json.loads(path.read_text())
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    @staticmethod
    def save(path, value, private=False):
        temp = path.with_suffix(".tmp")
        fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600 if private else 0o644)
        with os.fdopen(fd, "w") as file:
            json.dump(value, file, ensure_ascii=False, indent=1)
        os.replace(temp, path)

    def get(self):
        return merged(DEFAULTS, self.load(self.path))

    def update(self, changes):
        unknown = set(changes) - set(DEFAULTS)
        if unknown:
            raise ValueError("未知设置：" + ", ".join(sorted(unknown)))
        value = merged(self.get(), changes)
        self.save(self.path, value)
        return value

    def providers(self):
        stored = self.load(self.secret)
        out = {"miavo": merged(MIAVO, stored.get("miavo"))}
        out.update({name: p for name, p in stored.items() if name != "miavo" and isinstance(p, dict)})
        return out

    def set_provider(self, name, *, base_url=None, api_key=None, title=None, web_search=None):
        if not PROVIDER_NAME.match(name):
            raise ValueError("服务名只能用小写字母、数字、- 和 _")
        stored = self.load(self.secret)
        entry = dict(stored.get(name) or {})
        if base_url is not None:
            if not base_url.startswith(("https://", "http://")):
                raise ValueError("接口地址需以 https:// 或 http:// 开头")
            entry["base_url"] = base_url.rstrip("/")
        if api_key is not None:
            entry["api_key"] = api_key.strip()
        if title is not None:
            entry["title"] = title
        if web_search is not None:
            entry["web_search"] = bool(web_search)
        if name != "miavo" and not entry.get("base_url"):
            raise ValueError("请填写接口地址")
        stored[name] = entry
        self.save(self.secret, stored, private=True)

    def remove_provider(self, name):
        stored = self.load(self.secret)
        if stored.pop(name, None) is not None:
            self.save(self.secret, stored, private=True)

    def public_providers(self):
        """Providers without their keys, for the clients."""
        return [{"id": name, "title": p.get("title") or name, "base_url": p.get("base_url", ""), "configured": bool(p.get("api_key")) or not p.get("builtin"),
                 "has_key": bool(p.get("api_key")), "web_search": bool(p.get("web_search")), "builtin": bool(p.get("builtin"))}
                for name, p in self.providers().items()]


def split(model):
    """"miavo:claude-haiku-4-5" -> ("miavo", "claude-haiku-4-5"); "local" or empty -> (None, None)."""
    if not model or model == LOCAL or ":" not in model:
        return None, None
    provider, slug = model.split(":", 1)
    return provider, slug


class LLM:
    """What the agent and the worker call instead of the local runtime."""

    def __init__(self, settings, runtime, prefs):
        self.settings, self.runtime, self.prefs = settings, runtime, prefs

    # ---- which model ----
    def chat_model(self, session_model=None):
        return session_model or self.prefs.get()["chat_model"] or LOCAL

    def tool_model(self, fallback=None):
        return self.prefs.get()["tool_model"] or fallback or self.chat_model()

    def is_local(self, model=None):
        return split(model if model is not None else self.chat_model())[0] is None

    def provider(self, model):
        name, slug = split(model)
        entry = self.prefs.providers().get(name)
        if not entry:
            raise RuntimeError(f"没有配置名为 {name} 的模型服务，请在设置里添加")
        if not entry.get("api_key") and entry.get("builtin"):
            raise RuntimeError(f"还没有填写 {entry.get('title') or name} 的 API Key，请在设置 → 模型里填写")
        return entry, slug

    def searches(self, model=None):
        """True when this model answers with live web search done by its provider."""
        name, _ = split(model if model is not None else self.chat_model())
        if not name or not self.prefs.get()["web_search"]:
            return False
        return bool(self.prefs.providers().get(name, {}).get("web_search"))

    def search_model(self):
        """A configured cloud model that can search the web, for when the chat model is local."""
        prefs = self.prefs.get()
        if not prefs["web_search"]:
            return None
        for candidate in (prefs["tool_model"], prefs["chat_model"]):
            name, _ = split(candidate)
            entry = self.prefs.providers().get(name) if name else None
            if entry and entry.get("web_search") and entry.get("api_key"):
                return candidate
        return None

    def image_block(self, path, mime="image/png"):
        return self.runtime.image_block(path, mime)

    # ---- calls ----
    async def stream(self, messages, *, tools=None, tool_choice=None, max_tokens=2048, temperature=0.6, on_text=None, model=None, search=None):
        model = model if model is not None else self.chat_model()
        if self.is_local(model):
            return await self.runtime.stream(messages, tools=tools, tool_choice=tool_choice, max_tokens=max_tokens, temperature=temperature, on_text=on_text)
        entry, slug = self.provider(model)
        payload = {"model": slug, "messages": messages, "max_tokens": max_tokens, "temperature": temperature, "stream": True}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        if search is not False and self.searches(model):
            payload["web_search_options"] = {}
        headers = {"Authorization": f"Bearer {entry['api_key']}"} if entry.get("api_key") else {}
        timeout = httpx.Timeout(self.settings.llm_timeout, connect=20)
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                for _ in range(3):
                    async with client.stream("POST", entry["base_url"] + "/chat/completions", json=payload, headers=headers) as response:
                        if response.status_code == 200:
                            text, finish, calls, citations = await read_stream(response, on_text)
                            break
                        body = (await response.aread()).decode(errors="replace")
                    # Some models refuse optional fields (newer OpenAI models take max_completion_tokens and a fixed temperature): drop what was refused and retry.
                    refused = next((k for k in ("max_tokens", "temperature", "web_search_options") if response.status_code == 400 and k in body and k in payload), None)
                    if not refused:
                        raise RuntimeError(self.failure(entry, response.status_code, body))
                    value = payload.pop(refused)
                    if refused == "max_tokens":
                        payload["max_completion_tokens"] = value
                else:
                    raise RuntimeError(self.failure(entry, response.status_code, body))
        except httpx.HTTPError as exc:
            raise RuntimeError(f"连不上 {entry.get('title') or entry['base_url']}：{type(exc).__name__}。请检查网络后重试") from exc
        tool_calls = [{"id": c["id"] or "call_" + secrets.token_hex(6), "name": c["name"], "arguments": c["arguments"] or "{}"}
                      for _, c in sorted(calls.items()) if c["name"]]
        if not tool_calls and "<tool_call>" in text:
            text, tool_calls = extract_text_tool_calls(text)
            if not tools:
                tool_calls = []
        return Completion(strip_markup(text), tool_calls, finish, citations)

    @staticmethod
    def failure(entry, status, body):
        try:
            error = json.loads(body).get("error")
            detail = error.get("message") if isinstance(error, dict) else error
        except (json.JSONDecodeError, AttributeError):
            detail = None
        name = entry.get("title") or entry["base_url"]
        if status in (401, 403):
            return f"{name} 拒绝了 API Key（{status}），请在设置 → 模型里检查"
        if status == 402:
            return f"{name} 余额不足（402）"
        if status == 429:
            return f"{name} 请求过于频繁（429），请稍后重试"
        return f"{name} 返回错误 {status}" + (f"：{str(detail)[:300]}" if detail else "")

    async def complete(self, messages, *, max_tokens=2048, temperature=0.6, on_text=None, model=None):
        result = await self.stream(messages, max_tokens=max_tokens, temperature=temperature, on_text=on_text, model=model, search=False)
        if result.finish == "length":
            raise RuntimeError("模型输出达到长度限制，已保留部分内容，请缩小任务范围后重试")
        if not result.text:
            raise RuntimeError("模型没有返回可用内容")
        return result.text

    async def embed(self, texts):
        """Vectors from the configured embedding model, or None when none is set or it fails (callers fall back to lexical matching)."""
        model = self.prefs.get()["embed_model"]
        if not texts or self.is_local(model):
            return None
        try:
            entry, slug = self.provider(model)
            headers = {"Authorization": f"Bearer {entry['api_key']}"} if entry.get("api_key") else {}
            async with httpx.AsyncClient(timeout=60) as client:
                response = await client.post(entry["base_url"] + "/embeddings", json={"model": slug, "input": texts}, headers=headers)
            if response.status_code != 200:
                return None
            data = sorted(response.json()["data"], key=lambda d: d.get("index", 0))
            vectors = [d["embedding"] for d in data]
            return vectors if len(vectors) == len(texts) else None
        except (RuntimeError, httpx.HTTPError, KeyError, ValueError, TypeError):
            return None

    async def list_models(self, name):
        entry = self.prefs.providers().get(name)
        if not entry:
            raise ValueError("没有这个模型服务")
        headers = {"Authorization": f"Bearer {entry['api_key']}"} if entry.get("api_key") else {}
        try:
            async with httpx.AsyncClient(timeout=20) as client:
                response = await client.get(entry["base_url"] + "/models", headers=headers)
        except httpx.HTTPError as exc:
            raise ValueError(f"连不上 {entry.get('title') or name}：{type(exc).__name__}") from exc
        if response.status_code != 200:
            raise ValueError(self.failure(entry, response.status_code, response.text))
        items = response.json().get("data") or []
        return [{"id": f"{name}:{m['id']}", "name": m.get("name") or m["id"]} for m in items if isinstance(m, dict) and m.get("id")]
