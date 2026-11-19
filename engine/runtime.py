import asyncio
import base64
import json
import os
import re
import secrets
import signal
import time
from pathlib import Path
import httpx
from dataclasses import dataclass, field
import logging

log = logging.getLogger("jurvis.runtime")


@dataclass
class Completion:
    text: str
    tool_calls: list = field(default_factory=list)
    finish: str | None = None
    citations: list = field(default_factory=list)  # web sources a cloud model searched: {"url", "title"}
    cut: bool = False  # the output limit hit in the middle of a tool call, so the call is incomplete and was dropped
    bad: str = ""      # the model wrote a tool call the server could not read (its arguments were not valid); nothing ran


class IncompleteCall(RuntimeError):
    """The model ran out of output tokens in the middle of a tool call. Carries the prose written before it."""
    def __init__(self, text):
        super().__init__("模型的工具调用超出了单次输出上限")
        self.text = text


class BadCall(RuntimeError):
    """The model wrote a tool call whose arguments could not be parsed. Carries the prose before it and the parser's complaint."""
    def __init__(self, text, detail):
        super().__init__("模型的工具调用格式有误")
        self.text, self.detail = text, detail


# What the model server reports when it cannot turn the model's tool-call text into arguments.
_BAD_CALL = re.compile(r"<unknown>, line \d+|invalid (character|syntax|decimal|literal)|does not match opening|was never closed|unterminated|"
                       r"malformed node|unmatched '|Expecting .{1,40}(delimiter|value|property)|Tool arguments changed|Extra data|unexpected EOF", re.I)

_XML_CALL = re.compile(r"<tool_call>\s*<function=([^>\s]+)>(.*?)</function>\s*</tool_call>", re.S)
_XML_PARAM = re.compile(r"<parameter=([^>\s]+)>\n?(.*?)\n?</parameter>", re.S)
_JSON_CALL = re.compile(r"<tool_call>\s*(\{.*?\})\s*</tool_call>", re.S)
_UNCLOSED = re.compile(r"<tool_call>.*\Z", re.S)


def strip_markup(text):
    """Text safe to show: drops tool-call markup, including a cut-off trailing block."""
    return _UNCLOSED.sub("", _JSON_CALL.sub("", _XML_CALL.sub("", text))).strip()


def extract_text_tool_calls(text):
    """Fallback when the server leaves tool-call markup in the text stream."""
    calls = []
    for name, body in _XML_CALL.findall(text):
        args = {}
        for key, value in _XML_PARAM.findall(body):
            try:
                args[key] = json.loads(value)
            except json.JSONDecodeError:
                args[key] = value
        calls.append({"id": "call_" + secrets.token_hex(6), "name": name, "arguments": json.dumps(args, ensure_ascii=False)})
    for raw in _JSON_CALL.findall(text):
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            continue
        name = obj.get("name") or obj.get("tool")
        if isinstance(name, str):
            calls.append({"id": "call_" + secrets.token_hex(6), "name": name,
                          "arguments": json.dumps(obj.get("arguments") or {}, ensure_ascii=False)})
    return strip_markup(text), calls


async def read_stream(response, on_text=None):
    """Reads an OpenAI-style chat completion stream: (text, finish reason, tool calls by index, citations)."""
    text, finish, calls, citations = "", None, {}, {}
    async for line in response.aiter_lines():
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if data == "[DONE]":
            break
        try:
            item = json.loads(data)
        except json.JSONDecodeError:
            continue
        if item.get("error"):
            error = item["error"]
            if "incomplete tool call" in str(error).lower():
                raise IncompleteCall(text)  # the output limit hit mid tool call; the harness asks for smaller parts
            if _BAD_CALL.search(str(error)):
                raise BadCall(text, str(error.get("message") if isinstance(error, dict) else error)[:200])
            raise RuntimeError("模型返回错误：" + str(error.get("message") if isinstance(error, dict) else error)[:500])
        choice = (item.get("choices") or [{}])[0]
        delta = choice.get("delta") or {}
        for part in delta.get("tool_calls") or []:
            call = calls.setdefault(part.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
            call["id"] = part.get("id") or call["id"]
            function = part.get("function") or {}
            call["name"] += function.get("name") or ""
            args = function.get("arguments")
            call["arguments"] += args if isinstance(args, str) else json.dumps(args) if args else ""
        for note in delta.get("annotations") or []:
            cite = note.get("url_citation") if isinstance(note, dict) else None
            if isinstance(cite, dict) and str(cite.get("url", "")).startswith(("http://", "https://")):
                citations.setdefault(cite["url"], {"url": cite["url"], "title": str(cite.get("title") or "")[:200]})
        if delta.get("content"):
            text += delta["content"]
            if on_text:
                await on_text(text)
        if choice.get("finish_reason"):
            finish = choice["finish_reason"]
    return text, finish, calls, list(citations.values())


class Runtime:
    """Owns local inference processes. All calls are serialized by the job worker."""
    def __init__(self, settings):
        self.settings = settings
        self.processes = {}
        self.log_handles = {}
        self.active = None
        self.llm_key = secrets.token_urlsafe(32)
        self.lock = asyncio.Lock()

    async def stop(self, name):
        process = self.processes.pop(name, None)
        if process and process.returncode is None:
            process.terminate()
            try:
                await asyncio.wait_for(process.wait(), 12)
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
        handle = self.log_handles.pop(name, None)
        if handle:
            handle.close()
        if self.active == name:
            self.active = None

    async def shutdown(self):
        for name in list(self.processes):
            await self.stop(name)

    async def ensure(self, name):
        async with self.lock:
            if self.active == name and name in self.processes and self.processes[name].returncode is None:
                return
            for other in list(self.processes):
                await self.stop(other)
            s = self.settings
            env = dict(os.environ)
            env["TOKENIZERS_PARALLELISM"] = "false"
            if name == "llm":
                if not s.llm_model.is_dir():
                    raise RuntimeError(f"找不到本机模型，请检查 {s.llm_model}")
                env["MLX_VLM_SERVER_API_KEY"] = self.llm_key
                # The server's prefix cache (APC_ENABLED=1) makes follow-up calls several times faster, but with it on this model dropped tokens
                # in its output ("<=read_file>" for "<function=read_file>", "%" for "10%"), so it stays off unless the environment asks for it.
                env.setdefault("APC_ENABLED", "0")
                env.setdefault("APC_DISK_ENABLED", "0")
                command = [str(s.python), "-m", "mlx_vlm.server", "--host", "127.0.0.1", "--port", str(s.llm_port),
                           "--model", str(s.llm_model), "--max-num-seqs", "1", "--max-tokens", "16384",
                           "--prefill-step-size", "512", "--max-pixels", "524288", "--log-level", "WARNING"]
                cwd = s.root
                url = f"http://127.0.0.1:{s.llm_port}/v1/models"
            else:
                if not (s.comfy / "main.py").exists():
                    raise RuntimeError("创作引擎尚未安装，请运行 scripts/setup_media.py")
                env["ASFP8_INT8_EXT"] = "1"
                env["PYTORCH_ENABLE_MPS_FALLBACK"] = "1"
                command = [str(s.comfy / "venv/bin/python"), "main.py", "--listen", "127.0.0.1", "--port", str(s.comfy_port),
                           "--reserve-vram", "10", "--cache-none", "--disable-smart-memory",
                           "--extra-model-paths-config", str(s.root / "config/comfy-models.yaml")]
                cwd = s.comfy
                url = f"http://127.0.0.1:{s.comfy_port}/system_stats"
            log = open(s.data / "logs" / f"{name}.log", "a")
            self.log_handles[name] = log
            self.processes[name] = await asyncio.create_subprocess_exec(*command, cwd=cwd, env=env, stdout=log, stderr=log)
            headers = {"Authorization": f"Bearer {self.llm_key}"} if name == "llm" else {}
            try:
                async with httpx.AsyncClient(timeout=3, trust_env=False) as client:
                    for _ in range(240):
                        if self.processes[name].returncode is not None:
                            raise RuntimeError(f"{name} 启动失败，详情见 data/logs/{name}.log")
                        try:
                            result = await client.get(url, headers=headers)
                            if result.status_code == 200:
                                self.active = name
                                return
                        except httpx.HTTPError:
                            pass
                        await asyncio.sleep(1)
                raise RuntimeError(f"{name} 启动超时，请查看运行日志")
            except BaseException:
                await self.stop(name)
                raise

    async def complete(self, messages, *, max_tokens=2048, temperature=0.6, on_text=None):
        result = await self.stream(messages, max_tokens=max_tokens, temperature=temperature, on_text=on_text)
        if result.finish == "length":
            raise RuntimeError("模型输出达到长度限制，已保留部分内容，请缩小任务范围后重试")
        if not result.text:
            raise RuntimeError("模型没有返回可用内容")
        return result.text

    async def stream(self, messages, *, tools=None, tool_choice=None, max_tokens=2048, temperature=0.6, on_text=None):
        """Stream one assistant turn. Tool calls arrive as structured deltas and never mix into text."""
        await self.ensure("llm")
        payload = {"model": str(self.settings.llm_model), "messages": messages, "max_tokens": max_tokens,
                   "temperature": temperature, "stream": True, "enable_thinking": False,
                   # Without a cut-off the server samples from the whole distribution, and now and then picks a junk token inside a tool call
                   # (a dropped parameter name, "<=read_file>"). These are the values Qwen recommends.
                   "top_p": 0.8, "top_k": 20,
                   "chat_template_kwargs": {"enable_thinking": False}}
        if temperature > .65:
            # The server samples from a fixed seed unless told otherwise, so a retry would repeat the same text word for word.
            # Callers raise the temperature exactly when they need a different draw (a retry, a section of a long piece): those get a fresh seed.
            # Ordinary turns keep the fixed seed, which keeps tool use steady from run to run.
            payload.update(seed=secrets.randbelow(2**31), top_p=0.95)
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        started, first = time.monotonic(), []
        relay = on_text

        async def on_text(text):
            if not first:
                first.append(time.monotonic())
            if relay:
                await relay(text)
        size = sum(len(m["content"]) if isinstance(m["content"], str) else 2000 for m in messages)
        try:
            return await self.request(payload, tools, on_text)
        finally:
            # Time to the first text shows whether the server reused its cache of the earlier context.
            log.info("llm call: %d messages, %d chars in, first text after %.1fs, %.1fs in all", len(messages), size,
                     (first[0] if first else time.monotonic()) - started, time.monotonic() - started)

    async def request(self, payload, tools, on_text):
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.settings.llm_timeout, connect=10), trust_env=False) as client:
            async with client.stream("POST", f"http://127.0.0.1:{self.settings.llm_port}/v1/chat/completions",
                                     json=payload, headers={"Authorization":f"Bearer {self.llm_key}"}) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise RuntimeError(f"语言模型返回错误 {response.status_code}，请检查模型日志")
                try:
                    text, finish, calls, _ = await read_stream(response, on_text)
                except IncompleteCall as exc:
                    return Completion(strip_markup(exc.text), [], "length", cut=True)
                except BadCall as exc:
                    if not tools:
                        raise
                    return Completion(strip_markup(exc.text), [], "stop", bad=exc.detail)
        tool_calls = [{"id": c["id"] or "call_" + secrets.token_hex(6), "name": c["name"], "arguments": c["arguments"] or "{}"}
                      for _, c in sorted(calls.items()) if c["name"]]
        cut = False
        if not tool_calls:
            cut = finish == "length" and bool(tools) and bool(_UNCLOSED.search(_JSON_CALL.sub("", _XML_CALL.sub("", text))))
            text, tool_calls = extract_text_tool_calls(text)
            if not tools:
                tool_calls = []  # tools were withheld: drop the markup, keep any prose
        return Completion(text.strip(), tool_calls, finish, cut=cut)

    def image_block(self, path, mime="image/png"):
        return {"type":"image_url", "image_url":{"url":f"data:{mime};base64,"+base64.b64encode(Path(path).read_bytes()).decode()}}

