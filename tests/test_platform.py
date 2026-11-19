"""Cloud models, memory, skills, MCP, smart-home connectors, delegation and the two-lane scheduler."""
import asyncio
import json
import sys
import time

import httpx
import pytest
from fastapi.testclient import TestClient

from engine import llm as llm_module
from engine import web
from engine.app import create_app
from engine.config import Settings
from engine.home import Home, kinds
from engine.memory import features, lexical
from engine.runtime import Completion
from engine.skills import parse


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path), run_worker=False)


@pytest.fixture
def client(app):
    token = app.state.security.local_token
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


class Scripted:
    """Stands in for LLM.stream (cloud) and Runtime.stream (local); records every call."""
    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    async def stream(self, messages, *, tools=None, tool_choice=None, max_tokens=0, temperature=0, on_text=None, model=None, search=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model, "search": search})
        turn = self.turns.pop(0)
        if on_text and turn.text:
            await on_text(turn.text)
        return turn


def call(tool, **args):
    return {"id": "call_" + tool, "name": tool, "arguments": json.dumps(args)}


def use_cloud(app, model="miavo:claude-haiku-4-5"):
    worker = app.state.worker
    worker.prefs.set_provider("miavo", api_key="sk-maas-test")
    worker.prefs.update({"chat_model": model})


def session(app, ident="s1", **columns):
    store = app.state.store
    if not store.one("SELECT id FROM sessions WHERE id=?", (ident,)):
        store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES(?,'新对话',0,0)", (ident,))
    for key, value in columns.items():
        store.execute(f"UPDATE sessions SET {key}=? WHERE id=?", (value, ident))
    return ident


async def chat(app, model, text, ident="s1", cloud=True):
    worker, store = app.state.worker, app.state.store
    session(app, ident)
    if cloud:
        worker.llm.stream = model.stream
    else:
        worker.agent.runtime = model
    store.add_message(ident, "user", text)
    mid = store.add_message(ident, "assistant", "", "queued")
    job = store.create_job("chat", {"session_id": ident, "message_id": mid})
    await worker.agent.run(job)
    return store.one("SELECT * FROM messages WHERE id=?", (mid,))


# ---- cloud models ----

def sse(*chunks):
    return "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"


def patch_http(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(llm_module.httpx, "AsyncClient", lambda **kw: real(transport=httpx.MockTransport(handler), **{k: v for k, v in kw.items() if k != "transport"}))


@pytest.mark.asyncio
async def test_miavo_request_carries_key_search_and_parses_tools_and_citations(app, monkeypatch):
    use_cloud(app)
    seen = {}

    def handler(request):
        seen.update(url=str(request.url), auth=request.headers["authorization"], body=json.loads(request.content))
        return httpx.Response(200, text=sse(
            {"choices": [{"delta": {"content": "北京今天晴。"}}]},
            {"choices": [{"delta": {"annotations": [{"type": "url_citation", "url_citation": {"url": "https://weather.example/bj", "title": "北京天气"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "id": "c1", "function": {"name": "calculate", "arguments": "{\"expression\":"}}]}}]},
            {"choices": [{"delta": {"tool_calls": [{"index": 0, "function": {"arguments": "\"1+1\"}"}}]}, "finish_reason": "tool_calls"}]}))
    patch_http(monkeypatch, handler)
    reply = await app.state.worker.llm.stream([{"role": "user", "content": "北京天气"}], tools=[{"type": "function", "function": {"name": "calculate"}}])
    assert seen["url"] == "https://api.miavo.xyz/v1/chat/completions" and seen["auth"] == "Bearer sk-maas-test"
    assert seen["body"]["model"] == "claude-haiku-4-5" and seen["body"]["web_search_options"] == {} and "enable_thinking" not in seen["body"]
    assert reply.text == "北京今天晴。" and reply.finish == "tool_calls"
    assert reply.tool_calls == [{"id": "c1", "name": "calculate", "arguments": '{"expression":"1+1"}'}]
    assert reply.citations == [{"url": "https://weather.example/bj", "title": "北京天气"}]


@pytest.mark.asyncio
async def test_search_is_left_out_when_turned_off_and_key_errors_are_explained(app, monkeypatch):
    use_cloud(app)
    app.state.worker.prefs.update({"web_search": False})
    bodies = []

    def handler(request):
        bodies.append(json.loads(request.content))
        return httpx.Response(401, json={"error": {"message": "invalid key"}})
    patch_http(monkeypatch, handler)
    with pytest.raises(RuntimeError, match="API Key"):
        await app.state.worker.llm.stream([{"role": "user", "content": "hi"}])
    assert "web_search_options" not in bodies[0]


@pytest.mark.asyncio
async def test_refused_optional_fields_are_dropped_and_the_request_retried(app, monkeypatch):
    worker = app.state.worker
    worker.prefs.set_provider("openai", base_url="https://api.openai.example/v1", api_key="k")
    bodies = []

    def handler(request):
        body = json.loads(request.content)
        bodies.append(body)
        if "max_tokens" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported parameter: 'max_tokens'. Use 'max_completion_tokens' instead."}})
        if "temperature" in body:
            return httpx.Response(400, json={"error": {"message": "Unsupported value: 'temperature' does not support 0.6"}})
        return httpx.Response(200, text=sse({"choices": [{"delta": {"content": "好"}, "finish_reason": "stop"}]}))
    patch_http(monkeypatch, handler)
    reply = await worker.llm.stream([{"role": "user", "content": "hi"}], model="openai:gpt-x", max_tokens=500)
    assert reply.text == "好" and len(bodies) == 3
    assert bodies[-1]["max_completion_tokens"] == 500 and "temperature" not in bodies[-1] and "max_tokens" not in bodies[-1]


@pytest.mark.asyncio
async def test_missing_miavo_key_says_where_to_add_it(app):
    app.state.worker.prefs.update({"chat_model": "miavo:claude-haiku-4-5"})
    with pytest.raises(RuntimeError, match="设置"):
        await app.state.worker.llm.stream([{"role": "user", "content": "hi"}])


@pytest.mark.asyncio
async def test_cloud_conversation_uses_its_model_keeps_sources_and_knows_it_can_search(app):
    use_cloud(app)
    model = Scripted([Completion("今天晴，12 度。", [], "stop", [{"url": "https://weather.example", "title": "天气"}])])
    message = await chat(app, model, "北京现在天气怎么样？")
    assert message["status"] == "complete" and message["metadata"]["model"] == "miavo:claude-haiku-4-5"
    assert message["metadata"]["sources"] == [{"url": "https://weather.example", "title": "天气"}]
    first = model.calls[0]
    assert first["model"] == "miavo:claude-haiku-4-5" and "live web search" in first["messages"][0]["content"]
    names = {t["function"]["name"] for t in first["tools"]}
    assert {"web_fetch", "delegate", "calculate"} <= names and "web_search" not in names


@pytest.mark.asyncio
async def test_conversation_model_overrides_the_default(app):
    app.state.worker.prefs.set_provider("miavo", api_key="sk-maas-test")
    session(app, model="miavo:gemini-2.5-flash")
    model = Scripted([Completion("好的。", [], "stop")])
    await chat(app, model, "你好")
    assert model.calls[0]["model"] == "miavo:gemini-2.5-flash"


@pytest.mark.asyncio
async def test_local_model_gets_a_search_tool_backed_by_the_cloud_provider(app):
    worker = app.state.worker
    worker.prefs.set_provider("miavo", api_key="sk-maas-test")
    worker.prefs.update({"tool_model": "miavo:claude-haiku-4-5"})
    cloud = Scripted([Completion("金价每克 980 元。", [], "stop", [{"url": "https://gold.example", "title": "金价"}])])
    worker.llm.stream = cloud.stream
    local = Scripted([Completion("", [call("web_search", query="今日金价")], "tool_calls"), Completion("今天金价每克 980 元。", [], "stop")])
    message = await chat(app, local, "今天金价多少", cloud=False)
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "ok" and entry["summary"] == "1 个来源" and "980" in entry["output"]
    assert cloud.calls[0]["search"] is True and cloud.calls[0]["model"] == "miavo:claude-haiku-4-5"


def test_settings_api_hides_keys_and_validates_models(client):
    view = client.get("/api/settings").json()
    assert view["settings"]["chat_model"] == "local" and view["providers"][0]["id"] == "miavo" and not view["providers"][0]["has_key"]
    assert client.patch("/api/settings", json={"chat_model": "nowhere:model"}).status_code == 400
    saved = client.put("/api/providers/miavo", json={"api_key": "sk-maas-secret"}).json()
    assert saved["providers"][0]["has_key"] and "sk-maas-secret" not in json.dumps(saved)
    assert client.patch("/api/settings", json={"chat_model": "miavo:claude-haiku-4-5"}).json()["settings"]["chat_model"] == "miavo:claude-haiku-4-5"
    client.put("/api/providers/ollama", json={"base_url": "http://127.0.0.1:11434/v1", "title": "Ollama"})
    assert [p["id"] for p in client.get("/api/settings").json()["providers"]] == ["miavo", "ollama"]
    made = client.post("/api/sessions", json={"model": "ollama:qwen3:8b", "incognito": True}).json()
    assert made["model"] == "ollama:qwen3:8b" and made["incognito"] == 1
    assert client.patch("/api/sessions/" + made["id"], json={"model": ""}).json()["model"] is None


# ---- memory ----

def test_lexical_matching_works_for_chinese_and_english():
    assert lexical(features("我女儿对花生过敏"), features("女儿花生过敏，做饭要避开")) > 0.3
    assert lexical(features("deploy to staging"), features("Staging deploys go through GitHub Actions")) > 0.2
    assert lexical(features("今天天气"), features("女儿花生过敏")) == 0


@pytest.mark.asyncio
async def test_relevant_memories_reach_the_model_and_unrelated_ones_do_not(app):
    memory = app.state.worker.memory
    memory.save("饮食", "用户的女儿对花生过敏")
    memory.save("车", "用户开一辆 2019 年的蓝色沃尔沃")
    model = Scripted([Completion("可以做番茄牛腩，避开花生。", [], "stop")])
    await chat(app, model, "给我女儿推荐一道晚饭", cloud=False)
    note = model.calls[0]["messages"][-1]["content"]
    assert "花生过敏" in note and "沃尔沃" not in note and note.startswith("给我女儿推荐一道晚饭\n\n<jurvis_context>")
    assert "花生过敏" not in model.calls[0]["messages"][0]["content"]  # the system prompt stays the same from turn to turn


@pytest.mark.asyncio
async def test_incognito_conversation_neither_reads_nor_writes_memory(app):
    worker = app.state.worker
    worker.memory.save("饮食", "用户的女儿对花生过敏")
    session(app, incognito=1)
    model = Scripted([Completion("好的。", [], "stop")])
    await chat(app, model, "给我女儿推荐一道晚饭", cloud=False)
    assert "花生过敏" not in json.dumps(model.calls[0]["messages"], ensure_ascii=False)
    assert "remember" not in {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert worker.memory.pending("s1") is False


@pytest.mark.asyncio
async def test_important_memories_come_along_when_wording_differs(app):
    memory = app.state.worker.memory
    memory.save("饮食", "用户的女儿小满对花生过敏", importance=0.8)
    memory.save("闲聊", "用户上周看了一部电影", importance=0.3)
    found = await memory.retrieve("今晚给孩子做点什么吃好")
    assert [m["title"] for m in found] == ["饮食"]


@pytest.mark.parametrize("reply", [
    '[{"title": "a", "content": "b"}]',
    '{"title": "a", "content": "b", "tags": ["x", "y"]}',
    '好的：\n```json\n[{"title": "a", "content": "b"}]\n```',
    '{"title": "a", "content": "b"}\n{"title": "c", "content": "d"}',
])
def test_extraction_reply_formats(reply):
    from engine.memory import parse_list
    items = parse_list(reply)
    assert items and items[0]["title"] == "a" and all(isinstance(i, dict) for i in items)


def test_saving_the_same_fact_again_updates_it(app):
    memory = app.state.worker.memory
    first = memory.save("饮食", "用户的女儿对花生过敏")
    second = memory.save("饮食", "用户的女儿对花生过敏。")
    assert second["saved"] == first["saved"] and second["updated"]
    assert app.state.store.one("SELECT COUNT(*) AS n FROM memories")["n"] == 1


@pytest.mark.asyncio
async def test_memories_are_extracted_after_a_conversation_and_secrets_are_skipped(app):
    worker, store = app.state.worker, app.state.store
    session(app)
    store.add_message("s1", "user", "我下个月搬去杭州，以后推荐餐厅都按杭州来")
    store.add_message("s1", "assistant", "好的，记下了。")
    assert worker.memory.pending("s1")
    extracted = [{"title": "所在城市", "content": "用户下个月搬去杭州，推荐餐厅按杭州", "importance": 0.8, "durable": True, "tags": ["生活"]},
                 {"title": "服务器", "content": "服务器密码是 hunter2", "importance": 0.9}]
    worker.agent.runtime = worker.runtime
    worker.runtime.stream = Scripted([Completion("```json\n" + json.dumps(extracted, ensure_ascii=False) + "\n```", [], "stop")]).stream
    result = await worker.memory.process("s1")
    rows = store.all("SELECT title,content,source,importance,tags FROM memories")
    assert result["memories"] == 1 and [r["title"] for r in rows] == ["所在城市"]
    assert rows[0]["source"] == "chat" and rows[0]["tags"] == ["生活"] and rows[0]["importance"] == 0.8
    assert worker.memory.pending("s1") is False


@pytest.mark.asyncio
async def test_messages_that_stop_fitting_are_folded_into_a_summary_and_found_again(app):
    worker, store = app.state.worker, app.state.store
    session(app)
    total = worker.settings.history_messages + 6
    for n in range(total):
        text = "帮我写一条店庆的朋友圈文案" if n == 2 else "山月咖啡三周年，老朋友回家喝一杯。" if n == 3 else f"第{n}条"
        store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES(?,?,?,?,'complete',?)",
                      (f"m{n}", "s1", "user" if n % 2 == 0 else "assistant", text, 100 + n))
    model = Scripted([Completion("店庆：已写朋友圈文案。", [], "stop"), Completion("第一句是“山月咖啡三周年”。", [], "stop")])
    await chat(app, model, "刚才那条朋友圈文案的第一句是什么", cloud=False)
    fold, reply = model.calls
    assert fold["tools"] is None and "Messages to fold in" in fold["messages"][-1]["content"] and "第0条" in fold["messages"][-1]["content"]
    sent = reply["messages"]
    assert "店庆：已写朋友圈文案。" in sent[0]["content"]
    replayed = [m["content"] for m in sent[1:] if isinstance(m["content"], str)]
    assert not any(text == "第0条" for text in replayed) and any(text == f"第{total - 1}条" for text in replayed)
    assert len(replayed) == 15 and "第15条" in fold["messages"][-1]["content"]  # the newest stay verbatim, opening with a user message
    # The exchange the question refers to comes back word for word, although it was folded away.
    assert "老朋友回家喝一杯" in sent[-1]["content"] and "Passages from earlier" in sent[-1]["content"]
    # The next turns reuse the summary without folding again.
    again = Scripted([Completion("好的。", [], "stop")])
    await chat(app, again, "谢谢", cloud=False)
    assert len(again.calls) == 1 and "店庆：已写朋友圈文案。" in again.calls[0]["messages"][0]["content"]


def test_state_lists_memories_without_vectors(client, app):
    made = client.post("/api/memories", json={"title": "饮食", "content": "女儿花生过敏", "tags": ["家人"], "importance": 0.9}).json()
    app.state.store.execute("UPDATE memories SET embedding=?,embed_model='x:y' WHERE id=?", (b"\x00\x00\x80?", made["id"]))
    listed = client.get("/api/state").json()["memories"]
    assert listed[0]["tags"] == ["家人"] and listed[0]["importance"] == 0.9 and "embedding" not in listed[0]


# ---- scheduler ----

@pytest.mark.asyncio
async def test_cloud_conversation_runs_while_the_mac_is_busy_rendering(app):
    worker, store = app.state.worker, app.state.store
    use_cloud(app)
    session(app)
    started, release = [], asyncio.Event()

    async def handle(job):
        started.append(job["kind"])
        if job["kind"] == "image":
            await release.wait()
        return {}
    worker.handle = handle
    store.create_job("image", {"prompt": "x"})
    store.create_job("video", {"prompt": "y"})
    mid = store.add_message("s1", "assistant", "", "queued")
    store.create_job("chat", {"session_id": "s1", "message_id": mid})
    await worker.start()
    for _ in range(100):
        if {"chat", "image"} <= set(started):
            break
        await asyncio.sleep(.02)
    assert {"chat", "image"} <= set(started) and "video" not in started
    release.set()
    for _ in range(100):
        if "video" in started:
            break
        await asyncio.sleep(.02)
    assert "video" in started
    await worker.close()


@pytest.mark.asyncio
async def test_local_conversation_waits_for_the_gpu(app):
    worker, store = app.state.worker, app.state.store
    session(app)
    started, release = [], asyncio.Event()

    async def handle(job):
        started.append(job["kind"])
        await release.wait()
        return {}
    worker.handle = handle
    store.create_job("image", {"prompt": "x"})
    mid = store.add_message("s1", "assistant", "", "queued")
    store.create_job("chat", {"session_id": "s1", "message_id": mid})
    await worker.start()
    await asyncio.sleep(.6)
    assert started == ["image"]
    release.set()
    await worker.close()


# ---- skills ----

SKILL = "---\nname: 周报\ndescription: 写周报时使用，按固定三段格式\n---\n# 周报格式\n先写进展，再写风险，最后写下周计划。\n"


def test_skill_front_matter():
    meta, body = parse(SKILL)
    assert meta == {"name": "周报", "description": "写周报时使用，按固定三段格式"} and body.startswith("# 周报格式")


@pytest.mark.asyncio
async def test_skill_is_listed_in_the_prompt_and_loaded_on_demand(app, client):
    assert client.put("/api/skills/weekly-report", json={"content": "没有说明"}).status_code == 400
    assert client.put("/api/skills/weekly-report", json={"content": SKILL}).json()["title"] == "周报"
    model = Scripted([Completion("", [call("load_skill", name="weekly-report")], "tool_calls"), Completion("周报如下。", [], "stop")])
    message = await chat(app, model, "帮我写这周的周报", cloud=False)
    assert "weekly-report: 写周报时使用" in model.calls[0]["messages"][0]["content"]
    assert "先写进展" not in model.calls[0]["messages"][0]["content"]
    assert "先写进展" in model.calls[1]["messages"][-1]["content"]
    assert message["metadata"]["steps"][0]["calls"][0]["summary"] == "已加载 weekly-report"
    client.delete("/api/skills/weekly-report")
    assert client.get("/api/skills").json()["skills"] == []


# ---- MCP ----

MCP_SERVER = r'''
import json, sys
TOOLS = [{"name": "add_note", "description": "Add a note", "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
         {"name": "list_notes", "description": "List notes", "inputSchema": {"type": "object"}, "annotations": {"readOnlyHint": True}}]
notes = []
for line in sys.stdin:
    m = json.loads(line)
    if "id" not in m:
        continue
    method, result = m["method"], {}
    if method == "initialize":
        result = {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}}, "serverInfo": {"name": "notes", "version": "1"}}
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        if m["params"]["name"] == "add_note":
            notes.append(m["params"]["arguments"]["text"]); result = {"content": [{"type": "text", "text": "saved"}]}
        else:
            result = {"content": [{"type": "text", "text": "\n".join(notes) or "(empty)"}]}
    print(json.dumps({"jsonrpc": "2.0", "id": m["id"], "result": result}), flush=True)
'''


@pytest.mark.asyncio
async def test_mcp_tools_join_the_conversation_and_writes_wait_for_approval(app, tmp_path):
    worker = app.state.worker
    script = tmp_path / "notes_server.py"
    script.write_text(MCP_SERVER)
    worker.mcp.save("notes", {"command": sys.executable, "args": [str(script)]})
    await worker.mcp.connect()
    try:
        status = worker.mcp.status()[0]
        assert status["connected"] and [t["name"] for t in status["tools"]] == ["add_note", "list_notes"]
        model = Scripted([Completion("", [call("mcp__notes__list_notes")], "tool_calls"),
                          Completion("", [call("mcp__notes__add_note", text="买牛奶")], "tool_calls"),
                          Completion("已记下。", [], "stop")])

        async def approve_when_asked():
            for _ in range(200):
                if worker.shell.pending:
                    (message_id, call_id), = worker.shell.pending
                    worker.shell.decide(message_id, call_id, "run")
                    return True
                await asyncio.sleep(.01)
            return False
        approver = asyncio.create_task(approve_when_asked())
        message = await chat(app, model, "记一下买牛奶", cloud=False)
        assert await approver
        read, write = message["metadata"]["steps"][0]["calls"][0], message["metadata"]["steps"][1]["calls"][0]
        assert read["status"] == "ok" and "(empty)" in read["output"] and "command" not in read
        assert write["status"] == "ok" and "saved" in write["output"] and "add_note" in write["command"]
        assert "mcp__notes__add_note" in {t["function"]["name"] for t in model.calls[0]["tools"]}
    finally:
        await worker.mcp.close()


@pytest.mark.asyncio
async def test_declined_mcp_call_does_not_run(app, tmp_path):
    worker = app.state.worker
    script = tmp_path / "notes_server.py"
    script.write_text(MCP_SERVER)
    worker.mcp.save("notes", {"command": sys.executable, "args": [str(script)]})
    await worker.mcp.connect()
    try:
        model = Scripted([Completion("", [call("mcp__notes__add_note", text="买牛奶")], "tool_calls"), Completion("没有记下，需要你同意。", [], "stop")])

        async def deny():
            for _ in range(200):
                if worker.shell.pending:
                    (message_id, call_id), = worker.shell.pending
                    worker.shell.decide(message_id, call_id, "deny")
                    return
                await asyncio.sleep(.01)
        task = asyncio.create_task(deny())
        message = await chat(app, model, "记一下买牛奶", cloud=False)
        await task
        entry = message["metadata"]["steps"][0]["calls"][0]
        assert entry["status"] == "error" and entry["summary"] == "你拒绝了这个操作"
        assert await worker.mcp.servers["notes"].call("list_notes", {}) == "(empty)"
    finally:
        await worker.mcp.close()


def test_remote_devices_cannot_add_a_command_server(app):
    paired = app.state.security.pair(app.state.security.pairing("192.168.1.2")["code"], "iPhone", "test")
    with TestClient(app, headers={"Authorization": f"Bearer {paired['token']}"}) as remote:
        assert remote.put("/api/mcp/x", json={"command": "/bin/sh"}).status_code == 403


# ---- smart home ----

HA_STATES = [
    {"entity_id": "light.living_room", "state": "on", "attributes": {"friendly_name": "客厅灯", "brightness": 128, "rgb_color": [255, 136, 0]}},
    {"entity_id": "lock.front_door", "state": "locked", "attributes": {"friendly_name": "大门锁"}},
    {"entity_id": "climate.bedroom", "state": "cool", "attributes": {"friendly_name": "卧室空调", "current_temperature": 27, "temperature": 24, "hvac_modes": ["off", "cool", "heat"]}},
    {"entity_id": "sensor.outdoor_temperature", "state": "18.5", "attributes": {"friendly_name": "室外温度", "unit_of_measurement": "°C", "device_class": "temperature"}},
    {"entity_id": "automation.night", "state": "on", "attributes": {}},
]


def home_assistant(calls):
    def handler(request):
        if request.headers.get("authorization") != "Bearer ha-token":
            return httpx.Response(401)
        if request.url.path == "/api/states":
            return httpx.Response(200, json=HA_STATES)
        if request.url.path == "/api/template":
            return httpx.Response(200, text="light.living_room\t客厅\nlock.front_door\t玄关\nclimate.bedroom\t卧室\n")
        calls.append((request.url.path, json.loads(request.content)))
        return httpx.Response(200, json=[])
    return httpx.MockTransport(handler)


def connect_home(app, calls):
    worker = app.state.worker
    worker.home.transport = home_assistant(calls)
    worker.home.save("ha", "homeassistant", {"url": "http://ha.local:8123", "token": "ha-token"})
    return worker.home


@pytest.mark.asyncio
async def test_home_assistant_devices_are_normalized(app):
    home = connect_home(app, [])
    devices = {d["id"]: d for d in await home.devices()}
    assert set(devices) == {"ha:light.living_room", "ha:lock.front_door", "ha:climate.bedroom", "ha:sensor.outdoor_temperature"}
    light = devices["ha:light.living_room"]
    assert light["name"] == "客厅灯" and light["room"] == "客厅" and light["type"] == "light"
    assert light["state"] == {"on": True, "brightness": 50, "color": "#ff8800"} and "set_brightness" in light["actions"]
    assert devices["ha:climate.bedroom"]["state"]["target_temperature"] == 24
    assert devices["ha:sensor.outdoor_temperature"]["state"] == {"value": "18.5", "unit": "°C", "class": "temperature"}
    found = await home.search("客厅 灯")
    assert [d["name"] for d in found["devices"]] == ["客厅灯"]


@pytest.mark.asyncio
async def test_home_control_maps_actions_and_rejects_unsupported_ones(app):
    calls = []
    home = connect_home(app, calls)
    await home.control("ha:light.living_room", "set_brightness", 30)
    await home.control("ha:light.living_room", "set_color", "#00ff00")
    await home.control("ha:climate.bedroom", "set_mode", "heat")
    assert calls == [("/api/services/light/turn_on", {"entity_id": "light.living_room", "brightness_pct": 30}),
                     ("/api/services/light/turn_on", {"entity_id": "light.living_room", "rgb_color": [0, 255, 0]}),
                     ("/api/services/climate/set_hvac_mode", {"entity_id": "climate.bedroom", "hvac_mode": "heat"})]
    with pytest.raises(ValueError, match="不支持"):
        await home.control("ha:light.living_room", "unlock")
    with pytest.raises(ValueError, match="亮度"):
        await home.control("ha:light.living_room", "set_brightness", 400)


@pytest.mark.asyncio
async def test_assistant_switches_a_light_directly_but_asks_before_unlocking(app):
    calls = []
    connect_home(app, calls)
    worker = app.state.worker
    model = Scripted([Completion("", [call("home_devices", query="客厅")], "tool_calls"),
                      Completion("", [call("home_control", device_id="ha:light.living_room", action="turn_off")], "tool_calls"),
                      Completion("", [call("home_control", device_id="ha:lock.front_door", action="unlock")], "tool_calls"),
                      Completion("灯关了；门锁没有打开。", [], "stop")])

    async def deny():
        for _ in range(300):
            if worker.shell.pending:
                (message_id, call_id), = worker.shell.pending
                worker.shell.decide(message_id, call_id, "deny")
                return
            await asyncio.sleep(.01)
    task = asyncio.create_task(deny())
    message = await chat(app, model, "把客厅灯关了，再把大门打开", cloud=False)
    await task
    steps = message["metadata"]["steps"]
    assert steps[1]["calls"][0]["status"] == "ok" and steps[1]["calls"][0]["summary"] == "客厅灯 · turn_off"
    assert steps[2]["calls"][0]["status"] == "error" and "大门锁" in steps[2]["calls"][0]["command"]
    assert calls == [("/api/services/light/turn_off", {"entity_id": "light.living_room"})]
    assert "smart-home devices" in model.calls[0]["messages"][0]["content"]


def test_home_api_keeps_tokens_private_and_reports_bad_credentials(client, app):
    app.state.worker.home.transport = home_assistant([])
    assert {c["kind"] for c in client.get("/api/home").json()["catalog"]} == {"homeassistant", "hue", "smartthings", "tuya", "hubitat", "openhab", "dirigera", "lgthinq", "switchbot", "sensibo", "lifx", "govee", "nanoleaf", "yeelight", "shelly", "wled"}
    assert client.put("/api/home/connectors/ha", json={"kind": "homeassistant", "settings": {"url": "http://ha.local:8123"}}).status_code == 400
    saved = client.put("/api/home/connectors/ha", json={"kind": "homeassistant", "settings": {"url": "http://ha.local:8123", "token": "wrong"}}).json()
    assert saved["devices"] == 0 and "令牌" in saved["connectors"][0]["error"] and "wrong" not in json.dumps(saved)
    saved = client.put("/api/home/connectors/ha", json={"kind": "homeassistant", "settings": {"token": "ha-token"}}).json()
    assert saved["devices"] == 4 and saved["connectors"][0]["settings"] == {"url": "http://ha.local:8123"}
    assert client.post("/api/home/devices/control", json={"device_id": "ha:light.living_room", "action": "toggle"}).json()["status"] == "sent"


@pytest.mark.asyncio
async def test_hue_lights_and_scenes(tmp_path):
    puts = []

    def handler(request):
        assert request.headers["hue-application-key"] == "key"
        kind = request.url.path.rsplit("/", 2)[-1] if request.method == "GET" else None
        if request.method == "PUT":
            puts.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"data": []})
        data = {"light": [{"id": "L1", "owner": {"rid": "D1"}, "metadata": {"name": "床头灯"}, "on": {"on": False}, "dimming": {"brightness": 40.0}, "color_temperature": {"mirek": 250}}],
                "room": [{"id": "R1", "metadata": {"name": "卧室"}, "children": [{"rid": "D1"}]}],
                "scene": [{"id": "S1", "metadata": {"name": "阅读"}, "group": {"rid": "R1"}}]}[kind]
        return httpx.Response(200, json={"data": data})
    hue = kinds()["hue"]("hue", {"host": "192.168.1.20", "key": "key"}, httpx.MockTransport(handler))
    light, scene = await hue.devices()
    assert light["room"] == "卧室" and light["state"] == {"on": False, "brightness": 40, "color_temperature": 4000} and scene["actions"] == ["activate"]
    await hue.control(light["id"], "set_brightness", 80)
    await hue.control(scene["id"], "activate")
    assert puts == [("/clip/v2/resource/light/L1", {"on": {"on": True}, "dimming": {"brightness": 80.0}}), ("/clip/v2/resource/scene/S1", {"recall": {"action": "active"}})]


@pytest.mark.asyncio
async def test_smartthings_devices_and_commands():
    posted = []

    def handler(request):
        assert request.headers["authorization"] == "Bearer pat"
        path = request.url.path
        if request.method == "POST":
            posted.append((path, json.loads(request.content)))
            return httpx.Response(200, json={})
        if path == "/v1/devices":
            return httpx.Response(200, json={"items": [{"deviceId": "d1", "label": "客厅灯", "locationId": "loc", "roomId": "r1",
                                                        "components": [{"id": "main", "capabilities": [{"id": "switch"}, {"id": "switchLevel"}]}]}]})
        if path == "/v1/locations/loc/rooms":
            return httpx.Response(200, json={"items": [{"roomId": "r1", "name": "客厅"}]})
        return httpx.Response(200, json={"components": {"main": {"switch": {"switch": {"value": "on"}}, "switchLevel": {"level": {"value": 70}}}}})
    st = kinds()["smartthings"]("st", {"token": "pat"}, httpx.MockTransport(handler))
    device, = await st.devices()
    assert device["type"] == "light" and device["room"] == "客厅" and device["state"] == {"on": True, "brightness": 70}
    await st.control("d1", "toggle")
    assert posted == [("/v1/devices/d1/commands", {"commands": [{"component": "main", "capability": "switch", "command": "off", "arguments": []}]})]


@pytest.mark.asyncio
async def test_tuya_requests_are_signed_and_switch_codes_are_found():
    import hashlib
    import hmac
    commands = []

    def handler(request):
        path = request.url.raw_path.decode()
        token = request.headers.get("access_token", "")
        body = request.content.decode()
        text = "id" + token + request.headers["t"] + request.headers["nonce"] + f"{request.method}\n{hashlib.sha256(body.encode()).hexdigest()}\n\n{path}"
        assert request.headers["sign"] == hmac.new(b"secret", text.encode(), hashlib.sha256).hexdigest().upper()
        if path.startswith("/v1.0/token"):
            return httpx.Response(200, json={"success": True, "result": {"access_token": "tok", "expire_time": 7200}})
        if path.startswith("/v2.0/cloud/thing/device"):
            return httpx.Response(200, json={"success": True, "result": [{"id": "dev1", "name": "台灯", "category": "dj", "is_online": True}]})
        if path.startswith("/v1.0/iot-03/devices/status"):
            return httpx.Response(200, json={"success": True, "result": [{"id": "dev1", "status": [{"code": "switch_led", "value": True}, {"code": "bright_value_v2", "value": 500}]}]})
        commands.append(json.loads(body))
        return httpx.Response(200, json={"success": True, "result": True})
    tuya = kinds()["tuya"]("tuya", {"region": "cn", "client_id": "id", "secret": "secret"}, httpx.MockTransport(handler))
    tuya.tokens.clear()
    device, = await tuya.devices()
    assert device["type"] == "light" and device["state"] == {"on": True, "brightness": 50} and "set_brightness" in device["actions"]
    await tuya.control("dev1", "turn_off")
    await tuya.control("dev1", "set_brightness", 20)
    assert commands == [{"commands": [{"code": "switch_led", "value": False}]}, {"commands": [{"code": "bright_value_v2", "value": 200}]}]


# ---- delegation ----

@pytest.mark.asyncio
async def test_delegated_helper_works_in_its_own_context_and_only_the_report_returns(app):
    use_cloud(app)
    model = Scripted([
        Completion("", [call("delegate", title="算折扣", task="计算 1280 打七五折后的价格并报告结果。")], "tool_calls"),
        Completion("", [call("calculate", expression="1280*0.75")], "tool_calls"),   # helper
        Completion("打七五折后是 960 元。", [], "stop"),                                 # helper's report
        Completion("折后价是 960 元。", [], "stop"),
    ])
    message = await chat(app, model, "1280 打七五折多少钱，让助手去算")
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "ok" and entry["title"] == "算折扣" and entry["steps"][0]["calls"][0]["summary"] == "= 960"
    helper = model.calls[1]
    assert len(helper["messages"]) == 2 and "让助手去算" not in json.dumps(helper["messages"], ensure_ascii=False)
    helper_tools = {t["function"]["name"] for t in helper["tools"]}
    assert "calculate" in helper_tools and not helper_tools & {"run_command", "delegate", "save_file", "create_image", "remember", "home_control"}
    assert "960" in model.calls[3]["messages"][-1]["content"] and message["content"].endswith("折后价是 960 元。")


# ---- web pages ----

def test_html_is_reduced_to_readable_text():
    title, text = web.html_text("<html><head><title>公告</title><style>p{}</style></head><body><script>x()</script><h1>停水通知</h1><p>周三 <b>9 点</b>到 12 点停水。</p></body></html>")
    assert title == "公告" and text == "停水通知\n\n周三 9 点到 12 点停水。"


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://127.0.0.1:18777/api/state", "http://192.168.1.1/", "http://localhost/", "file:///etc/passwd"])
async def test_private_addresses_are_not_fetched(url):
    with pytest.raises(ValueError):
        await web.fetch(url)


def test_event_feed_and_pwa_are_served(client, app):
    assert client.get("/app/", headers={"Authorization": ""}).status_code in (200, 404)
    app.state.store.event("sessions", "s9")
    assert client.get("/api/state").json()["last_event"] >= 1
