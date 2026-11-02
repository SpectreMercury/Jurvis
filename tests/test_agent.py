import json
import pytest
from engine.app import create_app
from engine.config import Settings
from engine.runtime import Completion, extract_text_tool_calls


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path), run_worker=False)


class ScriptedModel:
    """Plays back assistant turns and records what the harness sent."""
    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    async def stream(self, messages, *, tools=None, tool_choice=None, max_tokens=0, temperature=0, on_text=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools})
        turn = self.turns.pop(0)
        if on_text and turn.text:
            await on_text(turn.text)
        return turn


def call(name, **args):
    return {"id": "call_" + name, "name": name, "arguments": json.dumps(args)}


def chat_job(app, text):
    store = app.state.store
    if not store.one("SELECT id FROM sessions"):
        store.execute("INSERT INTO sessions VALUES('s1','新对话',0,0)")
    store.add_message("s1", "user", text)
    mid = store.add_message("s1", "assistant", "", "queued")
    return store.create_job("chat", {"session_id": "s1", "message_id": mid}), mid


async def run(app, model, text):
    worker = app.state.worker
    worker.agent.runtime = model
    job, mid = chat_job(app, text)
    result = await worker.agent.run(job)
    return result, app.state.store.one("SELECT * FROM messages WHERE id=?", (mid,))


@pytest.mark.asyncio
async def test_tool_loop_records_trace_and_feeds_results_back(app):
    model = ScriptedModel([
        Completion("我先算一下。", [call("calculate", expression="(128+64)*3")], "tool_calls"),
        Completion("结果是 576。", [], "stop"),
    ])
    result, message = await run(app, model, "算一下 (128+64)*3")
    assert message["status"] == "complete"
    assert message["content"] == "我先算一下。\n\n结果是 576。"
    step = message["metadata"]["steps"][0]["calls"][0]
    assert step["status"] == "ok" and step["summary"] == "= 576" and step["label"] == "计算"
    second = model.calls[1]["messages"]
    assert second[-2]["tool_calls"][0]["function"]["name"] == "calculate"
    assert second[-1] == {"role": "tool", "tool_call_id": "call_calculate", "content": '{"value":576}'}
    assert {t["function"]["name"] for t in model.calls[0]["tools"]} >= {"calculate", "create_image", "code_propose"}
    assert result["tools"] == 1


@pytest.mark.asyncio
async def test_invalid_arguments_are_reported_to_the_model_not_raised(app):
    model = ScriptedModel([
        Completion("", [call("create_image")], "tool_calls"),
        Completion("缺少描述，请告诉我想画什么。", [], "stop"),
    ])
    _, message = await run(app, model, "画一张图")
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "error" and "prompt" in entry["output"]
    assert app.state.store.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='image'")["n"] == 0


@pytest.mark.asyncio
async def test_same_job_is_not_queued_twice_in_one_turn(app):
    image = call("create_image", prompt="雨后的街道")
    model = ScriptedModel([
        Completion("", [image], "tool_calls"),
        Completion("", [image], "tool_calls"),
        Completion("图片已排队。", [], "stop"),
    ])
    _, message = await run(app, model, "画雨后的街道")
    assert app.state.store.one("SELECT COUNT(*) AS n FROM jobs WHERE kind='image'")["n"] == 1
    first, second = (s["calls"][0] for s in message["metadata"]["steps"][:2])
    assert first["job_id"] == second["job_id"] and "not repeated" in second["output"]


@pytest.mark.asyncio
async def test_step_budget_ends_with_an_answer_instead_of_an_error(app):
    loop = [Completion("", [call("list_files")], "tool_calls") for _ in range(8)]
    model = ScriptedModel([*loop, Completion("工作区目前是空的。", [], "stop")])
    _, message = await run(app, model, "看看我的文件")
    assert message["status"] == "complete" and message["content"] == "工作区目前是空的。"
    assert model.calls[-1]["tools"] is None


@pytest.mark.asyncio
async def test_later_turns_replay_tool_calls_and_attachments(app):
    first = ScriptedModel([Completion("", [call("calculate", expression="2+2")], "tool_calls"), Completion("是 4。", [], "stop")])
    await run(app, first, "2+2?")
    second = ScriptedModel([Completion("刚才算的是 4。", [], "stop")])
    await run(app, second, "刚才结果是多少？")
    history = second.calls[0]["messages"]
    roles = [m["role"] for m in history]
    assert roles == ["system", "user", "assistant", "tool", "assistant", "user"]
    assert history[3]["content"] == '{"value":4}'


@pytest.mark.asyncio
async def test_failure_keeps_partial_trace(app):
    class Broken(ScriptedModel):
        async def stream(self, messages, **kw):
            if self.calls:
                raise RuntimeError("模型崩溃")
            return await super().stream(messages, **kw)
    model = Broken([Completion("先查一下。", [call("list_files")], "tool_calls")])
    worker = app.state.worker
    worker.agent.runtime = model
    job, mid = chat_job(app, "我的文件")
    with pytest.raises(RuntimeError):
        await worker.agent.run(job)
    app.state.store.set_message_status(mid, "failed", error="模型崩溃")
    message = app.state.store.one("SELECT * FROM messages WHERE id=?", (mid,))
    assert message["content"] == "先查一下。" and message["metadata"]["error"] == "模型崩溃"
    assert message["metadata"]["steps"][0]["calls"][0]["status"] == "ok"


def test_text_tool_call_fallback_parses_qwen_xml_and_json():
    text, calls = extract_text_tool_calls("好的\n<tool_call>\n<function=calculate>\n<parameter=expression>\n1+1\n</parameter>\n</function>\n</tool_call>")
    assert text == "好的" and calls[0]["name"] == "calculate" and json.loads(calls[0]["arguments"]) == {"expression": "1+1"}
    _, calls = extract_text_tool_calls('<tool_call>{"name":"list_files","arguments":{}}</tool_call>')
    assert calls[0]["name"] == "list_files"


def test_tool_schemas_are_self_contained(app):
    for schema in app.state.worker.tools.schemas():
        text = json.dumps(schema)
        assert "$ref" not in text and "$defs" not in text, schema["function"]["name"]
