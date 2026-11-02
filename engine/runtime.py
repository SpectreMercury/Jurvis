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


@dataclass
class Completion:
    text: str
    tool_calls: list = field(default_factory=list)
    finish: str | None = None


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
                    raise RuntimeError("找不到 Qwen 模型，请检查 models/qwen/aeon-mxfp4-mtplx")
                env["MLX_VLM_SERVER_API_KEY"] = self.llm_key
                command = [str(s.python), "-m", "mlx_vlm.server", "--host", "127.0.0.1", "--port", str(s.llm_port),
                           "--model", str(s.llm_model), "--max-num-seqs", "1", "--max-tokens", "6144",
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
                   "chat_template_kwargs": {"enable_thinking": False}}
        if tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        text, finish, calls = "", None, {}
        async with httpx.AsyncClient(timeout=httpx.Timeout(self.settings.llm_timeout, connect=10), trust_env=False) as client:
            async with client.stream("POST", f"http://127.0.0.1:{self.settings.llm_port}/v1/chat/completions",
                                     json=payload, headers={"Authorization":f"Bearer {self.llm_key}"}) as response:
                if response.status_code != 200:
                    await response.aread()
                    raise RuntimeError(f"语言模型返回错误 {response.status_code}，请检查模型日志")
                async for line in response.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    if line[6:] == "[DONE]":
                        break
                    item = json.loads(line[6:])
                    choice = (item.get("choices") or [{}])[0]
                    delta = choice.get("delta") or {}
                    for part in delta.get("tool_calls") or []:
                        call = calls.setdefault(part.get("index", len(calls)), {"id": "", "name": "", "arguments": ""})
                        call["id"] = part.get("id") or call["id"]
                        function = part.get("function") or {}
                        call["name"] += function.get("name") or ""
                        args = function.get("arguments")
                        call["arguments"] += args if isinstance(args, str) else json.dumps(args) if args else ""
                    if delta.get("content"):
                        text += delta["content"]
                        if on_text:
                            await on_text(text)
                    if choice.get("finish_reason"):
                        finish = choice["finish_reason"]
        tool_calls = [{"id": c["id"] or "call_" + secrets.token_hex(6), "name": c["name"], "arguments": c["arguments"] or "{}"}
                      for _, c in sorted(calls.items()) if c["name"]]
        if not tool_calls:
            text, tool_calls = extract_text_tool_calls(text)
            if not tools:
                tool_calls = []  # tools were withheld: drop the markup, keep any prose
        return Completion(text.strip(), tool_calls, finish)

    def image_block(self, path, mime="image/png"):
        return {"type":"image_url", "image_url":{"url":f"data:{mime};base64,"+base64.b64encode(Path(path).read_bytes()).decode()}}

