import asyncio
import json
import pytest
from engine.app import create_app
from engine.config import Settings
from engine.agent import MAX_STEPS
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


def chat_job(app, text, mode=None):
    store = app.state.store
    if not store.one("SELECT id FROM sessions"):
        store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('s1','新对话',0,0)")
    store.add_message("s1", "user", text, metadata={"mode": mode} if mode else None)
    mid = store.add_message("s1", "assistant", "", "queued")
    return store.create_job("chat", {"session_id": "s1", "message_id": mid}), mid


async def run(app, model, text, mode=None):
    worker = app.state.worker
    worker.agent.runtime = model
    job, mid = chat_job(app, text, mode)
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
    assert {t["function"]["name"] for t in model.calls[0]["tools"]} >= {"calculate", "create_image", "edit_code"}
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
    assert first["job_id"] == second["job_id"] and "not re-run" in second["output"]


@pytest.mark.asyncio
async def test_step_budget_ends_with_an_answer_instead_of_an_error(app):
    loop = [Completion("", [call("calculate", expression=f"{n}+1")], "tool_calls") for n in range(MAX_STEPS)]
    model = ScriptedModel([*loop, Completion("工作区目前是空的。", [], "stop")])
    _, message = await run(app, model, "看看我的文件")
    assert message["status"] == "complete" and message["content"] == "工作区目前是空的。"
    assert model.calls[-1]["tools"] is None


@pytest.mark.asyncio
async def test_repeating_the_same_read_call_forces_an_answer(app):
    again = Completion("", [call("calculate", expression="399/7")], "tool_calls")
    stray = Completion("周报共 399 条里程碑。", [call("calculate", expression="1")], "stop")
    model = ScriptedModel([again, again, again, stray])
    _, message = await run(app, model, "总结周报")
    assert message["content"] == "周报共 399 条里程碑。" and len(message["metadata"]["steps"]) == 4
    nudge = model.calls[-1]["messages"][-1]
    assert model.calls[-1]["tools"] is None and nudge["role"] == "user" and "Tool budget reached" in nudge["content"]
    assert "not re-run" in message["metadata"]["steps"][1]["calls"][0]["output"]


def test_recall_matches_any_keyword(app):
    store, tools = app.state.store, app.state.worker.tools
    store.execute("INSERT INTO assets(id,name,path,mime,kind,size,created_at,metadata) VALUES('a1','红色圆形.png','x','image/png','upload',1,0,'{}')")
    assert [a["id"] for a in tools.invoke("recall", {"query": "红色圆形 图片"}).result["assets"]] == ["a1"]
    assert tools.invoke("recall", {"query": ""}).result["assets"][0]["id"] == "a1"
    assert tools.invoke("recall", {"query": "蓝色"}).result["assets"] == []


def test_language_rule_follows_latest_message():
    from engine.agent import language_rule
    assert "Simplified Chinese" in language_rule("帮我画一只猫")
    assert "Chinese" not in language_rule("Draw a cat")


def test_markup_is_stripped_from_text():
    text, calls = extract_text_tool_calls("好的。<tool_call>\n<function=calculate>\n<parameter=expression>\n1\n</parameter>\n</function>\n</tool_call>")
    assert text == "好的。" and calls[0]["name"] == "calculate"
    from engine.runtime import strip_markup
    assert strip_markup("先算一下。<tool_call>\n<function=calculate>\n<parameter=expression>\n1\n</parameter>\n</function") == "先算一下。"


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


@pytest.mark.asyncio
async def test_coding_session_edits_become_an_applicable_change_set(app, tmp_path):
    root = tmp_path / "proj"; root.mkdir()
    (root / "calc.py").write_text("def total(a, b):\n    return a * b\n")
    store, worker = app.state.store, app.state.worker
    pid = worker.coding.connect(str(root))["id"]
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at,code_project_id) VALUES('s1','修复',0,0,?)", (pid,))
    edit = call("edit_code", project_id=pid, summary="改成相加",
                edits=[{"path": "calc.py", "old_text": "return a * b", "new_text": "return a + b"},
                       {"path": "test_calc.py", "new_text": "from calc import total\n"}])
    model = ScriptedModel([Completion("", [edit], "tool_calls"), Completion("已准备好修改，请审阅后应用。", [], "stop")])
    _, message = await run(app, model, "total 应该相加")
    first = model.calls[0]
    assert "calc.py" in first["messages"][0]["content"] and pid in first["messages"][0]["content"]
    assert {t["function"]["name"] for t in first["tools"]} == {"calculate", "recall", "remember", "code_files", "code_read", "code_search", "edit_code", "run_command"}
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "ok" and "calc.py +1 −1" in entry["summary"]
    assert (root / "calc.py").read_text().endswith("a * b\n"), "nothing is written before the user applies"
    worker.coding.apply(entry["job_id"])
    assert "a + b" in (root / "calc.py").read_text() and (root / "test_calc.py").exists()
    worker.coding.apply(entry["job_id"], revert=True)
    assert "a * b" in (root / "calc.py").read_text() and not (root / "test_calc.py").exists()


def test_edit_code_rejects_inexact_or_ambiguous_text(app, tmp_path):
    root = tmp_path / "p"; root.mkdir(); (root / "a.py").write_text("x = 1\nx = 1\n")
    tools = app.state.worker.tools
    pid = tools.coding.connect(str(root))["id"]
    def edit(**e):
        return tools.invoke("edit_code", {"project_id": pid, "summary": "s", "edits": [e]})
    assert "找不到" in edit(path="a.py", old_text="y = 2", new_text="z").result["error"]
    assert "2 次" in edit(path="a.py", old_text="x = 1", new_text="z").result["error"]
    assert "已存在" in edit(path="a.py", new_text="z").result["error"]
    assert not edit(path="../a.py", old_text="x", new_text="z").ok
    assert app.state.store.one("SELECT COUNT(*) AS n FROM jobs")["n"] == 0


@pytest.mark.asyncio
async def test_wrong_language_narration_before_a_tool_is_dropped(app):
    model = ScriptedModel([
        Completion("Let me calculate that.", [call("calculate", expression="2+2")], "tool_calls"),
        Completion("结果是 4。", [], "stop"),
    ])
    _, message = await run(app, model, "2+2 等于几")
    assert message["content"] == "结果是 4。" and message["metadata"]["steps"][0]["text"] == ""


@pytest.mark.asyncio
async def test_repeated_failing_call_is_stopped_and_reported(app):
    bad = Completion("", [{"id": "c1", "name": "read_file", "arguments": '{"name": "../x.md"}'}], "tool_calls")
    model = ScriptedModel([bad, bad, bad, Completion("", [], "stop"), Completion("", [], "stop")])
    _, message = await run(app, model, "读 ../x.md")
    assert len(model.calls) == 5 and model.calls[3]["tools"] is None
    assert message["status"] == "complete" and message["content"].startswith("没能完成这个请求") and "工作区" in message["content"]


@pytest.mark.asyncio
async def test_composer_mode_is_passed_to_the_model(app):
    store = app.state.store
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('s1','新对话',0,0)")
    store.add_message("s1", "user", "海边的灯塔", metadata={"mode": "image"})
    mid = store.add_message("s1", "assistant", "", "queued")
    job = store.create_job("chat", {"session_id": "s1", "message_id": mid})
    model = ScriptedModel([Completion("好的。", [], "stop")])
    app.state.worker.agent.runtime = model
    await app.state.worker.agent.run(job)
    assert "Image mode" in model.calls[0]["messages"][0]["content"]


# ---- commands and goal mode ----

def command(cmd, **extra):
    return call("run_command", command=cmd, reason="测试", **extra)


async def answer_when_awaiting(app, decision):
    """Plays the user: waits for a command to need approval, then answers it."""
    store, shell = app.state.store, app.state.worker.shell
    for _ in range(200):
        await asyncio.sleep(.02)
        if shell.pending:
            (mid, call_id), = shell.pending
            entry = store.one("SELECT metadata FROM messages WHERE id=?", (mid,))["metadata"]["steps"][-1]["calls"][-1]
            assert entry["status"] == "awaiting" and entry["command"]
            shell.decide(mid, call_id, decision)
            return entry
    raise AssertionError("no command waited for approval")


@pytest.mark.asyncio
async def test_command_waits_for_approval_then_runs_and_reports_output(app, tmp_path):
    model = ScriptedModel([Completion("我来安装。", [command("echo installed-ok", cwd=str(tmp_path))], "tool_calls"),
                           Completion("已安装。", [], "stop")])
    (_, message), shown = await asyncio.gather(run(app, model, "帮我装一下插件"), answer_when_awaiting(app, "run"))
    assert shown["reason"] == "测试" and shown["cwd"] == str(tmp_path)
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "ok" and entry["exit_code"] == 0 and "installed-ok" in entry["console"]
    assert "installed-ok" in model.calls[1]["messages"][-1]["content"]
    assert "run_command" in {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "finish_goal" not in {t["function"]["name"] for t in model.calls[0]["tools"]}


@pytest.mark.asyncio
async def test_declined_command_does_not_run(app, tmp_path):
    marker = tmp_path / "ran"
    model = ScriptedModel([Completion("", [command(f"touch {marker}")], "tool_calls"), Completion("好的，不运行。", [], "stop")])
    (_, message), _ = await asyncio.gather(run(app, model, "运行脚本"), answer_when_awaiting(app, "deny"))
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "error" and not marker.exists()
    assert "declined" in model.calls[1]["messages"][-2]["content"] and "stop here" in model.calls[1]["messages"][-1]["content"]
    assert model.calls[1]["tools"] is None, "after a refusal the turn goes back to the user"


@pytest.mark.asyncio
async def test_declining_a_command_in_goal_mode_stops_the_goal(app, tmp_path):
    model = ScriptedModel([Completion("", [command("ls", cwd=str(tmp_path)), command("pwd", cwd=str(tmp_path))], "tool_calls"),
                           Completion("命令没有运行。你想装哪个插件？", [], "stop")])
    (_, message), _ = await asyncio.gather(run(app, model, "帮我把插件装好", mode="goal"), answer_when_awaiting(app, "deny"))
    calls = message["metadata"]["steps"][0]["calls"]
    assert [c["status"] for c in calls] == ["error", "skipped"] and len(model.calls) == 2
    assert message["metadata"]["goal"]["status"] == "blocked" and model.calls[1]["tools"] is None


@pytest.mark.asyncio
async def test_goal_mode_waits_when_the_model_asks_a_question(app):
    model = ScriptedModel([Completion("你指的是哪个插件？告诉我名字我就装。", [], "stop")])
    _, message = await run(app, model, "帮我把那个插件装好", mode="goal")
    assert message["metadata"]["goal"]["status"] == "blocked" and len(model.calls) == 1


@pytest.mark.asyncio
async def test_same_command_with_a_new_reason_is_a_repeat(app, tmp_path):
    app.state.store.execute("INSERT INTO sessions(id,title,created_at,updated_at,allow_commands) VALUES('s1','x',0,0,1)")
    model = ScriptedModel([Completion("", [call("run_command", command="echo hi", reason="看看", cwd=str(tmp_path))], "tool_calls"),
                           Completion("", [call("run_command", command="echo hi", reason="再看看", cwd=str(tmp_path))], "tool_calls"),
                           Completion("好了。", [], "stop")])
    _, message = await run(app, model, "看一下")
    assert "not re-run" in message["metadata"]["steps"][1]["calls"][0]["output"]


@pytest.mark.asyncio
async def test_allowing_a_conversation_skips_approval_except_for_risky_commands(app, tmp_path):
    model = ScriptedModel([Completion("", [command("echo one", cwd=str(tmp_path))], "tool_calls"),
                           Completion("", [command("echo two", cwd=str(tmp_path))], "tool_calls"),
                           Completion("", [command(f"rm -rf {tmp_path}/nothing", cwd=str(tmp_path))], "tool_calls"),
                           Completion("完成。", [], "stop")])
    async def user():
        await answer_when_awaiting(app, "always")   # first command: allow the whole conversation
        await answer_when_awaiting(app, "run")      # rm -rf still asks
    (_, message), _ = await asyncio.gather(run(app, model, "跑几个命令"), user())
    calls = [c for s in message["metadata"]["steps"] for c in s["calls"]]
    assert [c["status"] for c in calls] == ["ok", "ok", "ok"]
    assert app.state.store.one("SELECT allow_commands FROM sessions WHERE id='s1'")["allow_commands"] == 1


@pytest.mark.asyncio
async def test_failed_command_output_reaches_the_model(app, tmp_path):
    app.state.store.execute("INSERT INTO sessions(id,title,created_at,updated_at,allow_commands) VALUES('s1','x',0,0,1)")
    model = ScriptedModel([Completion("", [command("echo 'No module named foo' >&2; exit 3", cwd=str(tmp_path))], "tool_calls"),
                           Completion("缺少 foo 模块。", [], "stop")])
    _, message = await run(app, model, "运行脚本")
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "error" and entry["exit_code"] == 3 and "退出码 3" in entry["summary"]
    fed = json.loads(model.calls[1]["messages"][-1]["content"])
    assert fed["exit_code"] == 3 and "No module named foo" in fed["output"]


@pytest.mark.asyncio
async def test_a_command_reruns_after_the_system_changed(app, tmp_path):
    app.state.store.execute("INSERT INTO sessions(id,title,created_at,updated_at,allow_commands) VALUES('s1','x',0,0,1)")
    check = command("ls", cwd=str(tmp_path))
    model = ScriptedModel([Completion("", [check], "tool_calls"),
                           Completion("", [command("touch new.txt", cwd=str(tmp_path))], "tool_calls"),
                           Completion("", [check], "tool_calls"),
                           Completion("好了。", [], "stop")])
    _, message = await run(app, model, "建个文件")
    last = message["metadata"]["steps"][2]["calls"][0]
    assert "new.txt" in last["console"] and "not re-run" not in last["output"]


@pytest.mark.asyncio
async def test_goal_mode_keeps_working_until_review_confirms(app, tmp_path):
    app.state.store.execute("INSERT INTO sessions(id,title,created_at,updated_at,allow_commands) VALUES('s1','x',0,0,1)")
    done = call("finish_goal", status="done", summary="插件已装好。", evidence="import 成功")
    model = ScriptedModel([
        Completion("我先看看目录。", [], "stop"),                                     # pauses to talk: nudged on
        Completion("", [done], "tool_calls"),                                       # claims done with no proof
        Completion('{"achieved": false, "missing": "还没有安装也没有验证"}', [], "stop"),  # reviewer
        Completion("", [command("echo ok", cwd=str(tmp_path))], "tool_calls"),
        Completion("", [done], "tool_calls"),
        Completion('{"achieved": true, "missing": ""}', [], "stop"),
    ])
    _, message = await run(app, model, "把插件装好并确认能用", mode="goal")
    goal = message["metadata"]["goal"]
    assert goal["status"] == "done" and goal["reviews"] == 1
    assert message["content"].endswith("插件已装好。") and message["status"] == "complete"
    assert "finish_goal" in {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "Goal mode" in model.calls[0]["messages"][0]["content"]
    assert "stopped without calling finish_goal" in model.calls[1]["messages"][-1]["content"]
    assert "not proven done" in model.calls[3]["messages"][-1]["content"]
    review = model.calls[2]
    assert review["tools"] is None and "Goal:\n把插件装好并确认能用" in review["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_goal_mode_can_stop_to_ask_the_user(app):
    blocked = call("finish_goal", status="blocked", summary="需要你的 GitHub 访问令牌才能继续。")
    model = ScriptedModel([Completion("", [blocked], "tool_calls")])
    _, message = await run(app, model, "把私有仓库的插件装上", mode="goal")
    assert message["metadata"]["goal"]["status"] == "blocked" and message["content"] == "需要你的 GitHub 访问令牌才能继续。"


@pytest.mark.asyncio
async def test_goal_question_is_not_repeated_when_the_model_already_asked(app):
    blocked = call("finish_goal", status="blocked", summary="要装哪个插件？")
    model = ScriptedModel([Completion("请问你想装哪个插件？", [blocked], "tool_calls")])
    _, message = await run(app, model, "帮我把那个插件装好", mode="goal")
    assert message["content"] == "请问你想装哪个插件？"


@pytest.mark.asyncio
async def test_goal_mode_reports_progress_when_it_never_finishes(app):
    talk = [Completion("还在想。", [], "stop")] * 3
    model = ScriptedModel(talk)
    _, message = await run(app, model, "搞定它", mode="goal")
    assert message["metadata"]["goal"]["status"] == "incomplete" and len(model.calls) == 3


@pytest.mark.asyncio
async def test_goal_mode_applies_code_edits_so_they_can_be_tested(app, tmp_path):
    root = tmp_path / "proj"; root.mkdir()
    (root / "calc.py").write_text("def total(a, b):\n    return a * b\n")
    store, worker = app.state.store, app.state.worker
    pid = worker.coding.connect(str(root))["id"]
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at,code_project_id,allow_commands) VALUES('s1','修复',0,0,?,1)", (pid,))
    edit = call("edit_code", project_id=pid, summary="改成相加", edits=[{"path": "calc.py", "old_text": "return a * b", "new_text": "return a + b"}])
    test = command("python3 -c 'from calc import total; assert total(2, 3) == 5; print(\"passed\")'")
    model = ScriptedModel([Completion("", [edit], "tool_calls"), Completion("", [test], "tool_calls"),
                           Completion("", [call("finish_goal", status="done", summary="已修复并通过测试。", evidence="passed")], "tool_calls"),
                           Completion('{"achieved": true}', [], "stop")])
    _, message = await run(app, model, "修好 total 并测试", mode="goal")
    calls = [c for s in message["metadata"]["steps"] for c in s["calls"]]
    assert calls[0]["summary"].startswith("已应用") and "a + b" in (root / "calc.py").read_text()
    assert calls[1]["status"] == "ok" and "passed" in calls[1]["console"]
    assert message["metadata"]["goal"]["status"] == "done"
    worker.coding.apply(calls[0]["job_id"], revert=True)
    assert "a * b" in (root / "calc.py").read_text()


def test_risky_commands_are_recognised():
    from engine.shell import risky
    assert all(risky(c) for c in ["sudo pip install x", "rm -rf build", "cd a && git push", "killall Finder", "git reset --hard"])
    assert not any(risky(c) for c in ["pip install -r requirements.txt", "git clone https://x/y.git", "brew install ffmpeg", "ls -la", "python3 skill.py"])


def test_tool_arguments_tolerate_what_local_models_stream():
    from engine.agent import parse_arguments
    assert parse_arguments("") == {} and parse_arguments("null") == {} and parse_arguments(" {} ") == {}
    assert parse_arguments("{}{}") == {} and parse_arguments('{"name": "a.md"} trailing') == {"name": "a.md"}
    assert parse_arguments('{"name": "a.md"') == '{"name": "a.md"'
