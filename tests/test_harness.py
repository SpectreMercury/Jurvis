"""File search, ranged reads, background commands, plan mode, tool lookup and skills from several places."""
import asyncio
import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.runtime import Completion
from engine.shell import Shell
from engine.skills import Skills
from engine.tools import DEFER_TOOLS, Tool, glob_regex
from tests.test_agent import ScriptedModel, call, run

SKILL = "---\nname: {title}\ndescription: {title}的做法\n---\n按步骤来。\n"


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path / "data"), run_worker=False)


def test_glob_patterns_follow_folders_the_way_people_expect():
    assert glob_regex("*.py").match("engine/agent.py") and glob_regex("*.py").match("setup.py")
    assert glob_regex("engine/*.py").match("engine/agent.py") and not glob_regex("engine/*.py").match("engine/home/hue.py")
    assert glob_regex("engine/**/*.py").match("engine/home/hue.py") and glob_regex("engine/**/*.py").match("engine/agent.py")
    assert glob_regex("docs/{intro,usage}.md").match("docs/usage.md") and not glob_regex("docs/{intro,usage}.md").match("docs/api.md")
    assert not glob_regex("*.py").match("notes.pyc")


def test_reading_a_range_returns_numbered_lines(app):
    tools = app.state.worker.tools
    tools.save_file("long.md", "\n".join(f"第 {n} 行" for n in range(1, 51)))
    assert tools.invoke("read_file", {"name": "long.md"}).result["content"].startswith("第 1 行\n第 2 行")
    part = tools.invoke("read_file", {"name": "long.md", "offset": 10, "limit": 3}).result
    assert part["content"] == "10\t第 10 行\n11\t第 11 行\n12\t第 12 行"
    assert (part["lines"], part["from"], part["to"]) == (50, 10, 12)


def test_finding_and_searching_files_in_the_workspace_and_a_project(app, tmp_path):
    tools = app.state.worker.tools
    tools.save_file("plan.md", "# 计划\n周一：采购\n周二：布置")
    tools.save_file("notes.txt", "采购清单：气球")
    assert [f["path"] for f in tools.invoke("find_files", {"pattern": "*.md"}).result["files"]] == ["plan.md"]
    found = tools.invoke("search_files", {"pattern": "采购"}).result
    assert {(m["path"], m["line"]) for m in found["matches"]} == {("plan.md", 2), ("notes.txt", 1)} and found["files"] == 2
    assert tools.invoke("search_files", {"pattern": "采购", "glob": "*.txt"}).result["files"] == 1
    assert "周一" in tools.invoke("search_files", {"pattern": "周二", "context": 1}).result["matches"][0]["around"]
    assert not tools.invoke("search_files", {"pattern": "("}).ok

    root = tmp_path / "repo"
    (root / "src" / "api").mkdir(parents=True)
    (root / "src" / "api" / "users.py").write_text("def list_users():\n    return []\n")
    (root / "src" / "main.py").write_text("from api.users import list_users\n")
    (root / ".env").write_text("SECRET=1\n")
    project = tools.coding.connect(str(root))
    paths = [f["path"] for f in tools.invoke("find_files", {"pattern": "src/**/*.py", "project_id": project["id"]}).result["files"]]
    assert sorted(paths) == ["src/api/users.py", "src/main.py"]
    hits = tools.invoke("search_files", {"pattern": r"def \w+_users", "project_id": project["id"]}).result["matches"]
    assert [(m["path"], m["line"]) for m in hits] == [("src/api/users.py", 1)]
    # credentials stay out of reach, as with the other project tools
    assert tools.invoke("search_files", {"pattern": "SECRET", "project_id": project["id"]}).result["matches"] == []


@pytest.mark.asyncio
async def test_a_background_command_keeps_running_until_it_is_stopped(tmp_path):
    shell = Shell(SimpleNamespace(data=tmp_path), None)
    started = await shell.start("echo ready; sleep 30", tmp_path)
    assert started["running"] and "ready" in started["output"]
    assert shell.output(started["command_id"])["running"]
    stopped = await shell.kill(started["command_id"])
    assert not stopped["running"] and stopped["exit_code"] != 0
    quick = await shell.start("echo done", tmp_path)
    assert not quick["running"] and quick["exit_code"] == 0 and "done" in quick["output"]
    assert started["command_id"] == "c1" and quick["command_id"] == "c2"
    with pytest.raises(ValueError, match="c1.*已结束.*c2"):
        shell.output("f2403b5")
    survivor = await shell.start("sleep 30", tmp_path)
    await shell.close()
    assert not shell.output(survivor["command_id"])["running"]


@pytest.mark.asyncio
async def test_plan_mode_offers_only_tools_that_look_and_refuses_the_rest(app):
    model = ScriptedModel([
        Completion("", [{"id": "call_save", "name": "save_file", "arguments": json.dumps({"name": "a.md", "content": "不该写入"})}], "tool_calls"),
        Completion("", [call("update_plan", steps="读现有方案\n改预算表\n发给店长确认", done="1,2")], "tool_calls"),
        Completion("计划如上，确认后我开始。", [], "stop"),
    ])
    _, message = await run(app, model, "帮我把店庆方案改一版", mode="plan")
    offered = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert {"read_file", "find_files", "search_files", "update_plan"} <= offered
    assert not offered & {"save_file", "edit_file", "run_command", "create_image", "remember", "home_control", "delegate"}
    assert "Plan mode" in model.calls[0]["messages"][0]["content"]
    refused = message["metadata"]["steps"][0]["calls"][0]
    assert refused["status"] == "error" and "plan mode" in refused["output"]
    assert app.state.worker.tools.list_files()["files"] == []
    assert [(i["step"], i["status"]) for i in message["metadata"]["plan"]] == [("读现有方案", "pending"), ("改预算表", "pending"), ("发给店长确认", "pending")]


@pytest.mark.asyncio
async def test_many_mcp_tools_are_looked_up_instead_of_all_listed(app):
    tools = app.state.worker.tools

    async def ping(arguments):
        return {"output": "pong"}
    for n in range(DEFER_TOOLS + 6):
        name = "github__create_issue" if n == 0 else f"srv__tool_{n}"
        tools.tools[name] = Tool(name, name, "Create an issue in a GitHub repository" if n == 0 else f"Does thing {n}", None, ping, "external",
                                 parameters={"type": "object", "properties": {}})
        tools.dynamic.add(name)
    tools.refresh = lambda: None   # keep the stand-in tools for this turn
    model = ScriptedModel([
        Completion("", [call("find_tools", query="github issue")], "tool_calls"),
        Completion("", [call("github__create_issue")], "tool_calls"),
        Completion("已创建。", [], "stop"),
    ])
    _, message = await run(app, model, "在 GitHub 上提一个 issue")
    first = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "find_tools" in first and "github__create_issue" not in first and not any(n.startswith("srv__") for n in first)
    found = json.loads(message["metadata"]["steps"][0]["calls"][0]["output"])["tools"]
    assert found[0]["name"] == "github__create_issue"
    assert "github__create_issue" in {t["function"]["name"] for t in model.calls[1]["tools"]}
    assert message["metadata"]["steps"][1]["calls"][0]["status"] == "ok"


def test_skills_come_from_three_places_and_your_own_wins(tmp_path):
    shared, repo = tmp_path / "shared", tmp_path / "repo"
    for folder, name, title in ((shared, "weekly-report", "共享周报"), (shared, "pdf", "PDF"), (repo / ".jurvis" / "skills", "release", "发布")):
        (folder / name).mkdir(parents=True)
        (folder / name / "SKILL.md").write_text(SKILL.format(title=title))
    (shared / "pdf" / "REFERENCE.md").write_text("表单字段说明")
    skills = Skills(Settings(data=tmp_path / "data", shared_skills=str(shared)))
    skills.save("weekly-report", SKILL.format(title="我的周报"))
    skills.use_project(repo)
    listed = {s["name"]: (s["source"], s["title"]) for s in skills.list()}
    assert listed == {"weekly-report": ("personal", "我的周报"), "pdf": ("shared", "PDF"), "release": ("project", "发布")}
    loaded = skills.read("pdf")
    assert loaded["files"] == ["REFERENCE.md"] and skills.read("pdf", "REFERENCE.md")["content"] == "表单字段说明"
    with pytest.raises(ValueError):
        skills.read("pdf", "../weekly-report/SKILL.md")
    assert not skills.switch("pdf", False)["enabled"] and "pdf" not in [s["name"] for s in skills.enabled()]
    assert skills.switch("pdf", True)["enabled"]
    with pytest.raises(ValueError):
        skills.switch("weekly-report", False)
    # with the shared folder switched off, only your own are listed
    assert [s["name"] for s in Skills(Settings(data=tmp_path / "data", shared_skills="")).list()] == ["weekly-report"]


def test_local_model_folder_can_be_replaced(tmp_path):
    from engine.config import Settings, ROOT
    assert Settings(data=tmp_path).llm_model == ROOT / "models/qwen/aeon-mxfp4-mtplx"
    named = Settings(data=tmp_path, local_model="mine/gemma-4b")
    assert named.llm_model == ROOT / "models/mine/gemma-4b" and named.llm_title == "gemma-4b（本机）"
    assert Settings(data=tmp_path, local_model=str(tmp_path / "m")).llm_model == tmp_path / "m"


# ---- speech ----
def test_speech_text_is_cleaned_and_split_for_reading():
    from engine.speech import plain, sentences, language_of
    spoken = plain("## 结果\n\n- **第一**：见 [文档](https://a.b/c)\n- 第二 `code`\n\n```py\nprint(1)\n```\n| a | b |\n|---|---|\nhttps://x.y/z 完。")
    assert spoken == "结果 第一 ：见 文档 第二 code a b 完。"
    parts = sentences("今天天气不错。" * 30 + "A long English sentence without any Chinese in it at all. " * 12, limit=120)
    assert all(0 < len(p) <= 120 for p in parts) and "".join(parts).replace(" ", "") == ("今天天气不错。" * 30 + "A long English sentence without any Chinese in it at all. " * 12).replace(" ", "")
    assert sentences("没有标点的很长一句话" * 40, limit=100) and max(map(len, sentences("没有标点的很长一句话" * 40, limit=100))) <= 100
    assert [language_of(t) for t in ("你好", "hello", "こんにちは", "안녕", "123")] == ["chinese", "english", "japanese", "korean", "auto"]


def test_speech_api_reports_missing_models_and_wraps_audio(tmp_path):
    import io, wave
    from fastapi.testclient import TestClient
    from engine.app import create_app
    from engine.config import Settings
    from engine.speech import to_wav
    with wave.open(io.BytesIO(to_wav([0.0, 0.5, -0.5, 2.0], 24000))) as audio:
        assert (audio.getframerate(), audio.getnchannels(), audio.getnframes()) == (24000, 1, 4)
    app = create_app(Settings(data=tmp_path / "data", speech_stt=str(tmp_path / "no-stt"), speech_tts=str(tmp_path / "no-tts")), run_worker=False)
    with TestClient(app, headers={"Authorization": f"Bearer {app.state.security.local_token}"}) as client:
        status = client.get("/api/speech").json()
        assert status["stt"] is False and status["tts"] is False and status["speakers"] == [] and status["speaker"] == ""
        answer = client.post("/api/speech/speak", json={"text": "你好"})
        assert answer.status_code == 409 and "语音合成模型还没有安装" in answer.json()["detail"]
        assert client.post("/api/speech/speak", json={"text": "```\ncode only\n```"}).status_code == 409
        silent = client.post("/api/speech/transcribe", files={"file": ("a.wav", to_wav([0.0] * 800, 16000), "audio/wav")})
        assert silent.status_code == 200 and silent.json()["text"] == ""
        assert client.post("/api/speech/transcribe", files={"file": ("a.wav", b"not audio", "audio/wav")}).status_code == 409
        assert client.patch("/api/settings", json={"voice": {"speaker": "Vivian"}}).json()["settings"]["voice"] == {"speaker": "Vivian", "auto_speak": False}
        assert client.get("/api/speech").json()["speaker"] == "Vivian"


@pytest.mark.asyncio
async def test_a_question_with_options_ends_the_reply_and_is_kept_for_the_buttons(app):
    model = ScriptedModel([
        Completion("集中流动性的核心是取舍：区间越窄，手续费越高，无常损失也越大。",
                   [call("ask_user", question="你想先从哪一步开始？", options="1. 先搭个通用分析框架\n- 帮我分析我现在做的池子\n先算几个典型区间的对照表\n先算几个典型区间的对照表")], "tool_calls"),
    ])
    result, message = await run(app, model, "我一直在做集中流动性 LP，想系统分析区间选择")
    assert "ask_user" in {t["function"]["name"] for t in model.calls[0]["tools"]} and len(model.calls) == 1  # asking costs no further model call
    meta = message["metadata"]
    assert meta["ask"] == {"question": "你想先从哪一步开始？", "options": ["先搭个通用分析框架", "帮我分析我现在做的池子", "先算几个典型区间的对照表"]}
    assert [s["text"] for s in meta["steps"]] == ["集中流动性的核心是取舍：区间越窄，手续费越高，无常损失也越大。", "你想先从哪一步开始？"]
    assert message["status"] == "complete" and message["content"].endswith("你想先从哪一步开始？")
    # the answer arrives as an ordinary message, and the question is replayed as the call it was
    model.turns.append(Completion("好，按通用框架来。", [], "stop"))
    await run(app, model, "先搭个通用分析框架")
    replayed = [m for m in model.calls[1]["messages"] if m.get("tool_calls")]
    assert replayed and replayed[0]["tool_calls"][0]["function"]["name"] == "ask_user"


@pytest.mark.asyncio
async def test_a_question_already_written_in_the_reply_is_not_shown_twice_and_one_option_is_refused(app):
    model = ScriptedModel([
        Completion("按周还是按月更新？", [call("ask_user", question="按周还是按月更新？", options="按周\n按月")], "tool_calls"),
    ])
    _, message = await run(app, model, "帮我长期跟踪 L2")
    assert [s["text"] for s in message["metadata"]["steps"] if s["text"]] == ["按周还是按月更新？"] and message["content"] == "按周还是按月更新？"
    # reworded at the end of the answer, it is still one question: the answer keeps its body and the buttons get the question
    model = ScriptedModel([
        Completion("记下了，存到 Goals 里了。\n\n要不要设一个每周的简报？频率你想怎么定？", [call("ask_user", question="要不要设一个每周简报？频率怎么定？", options="每周\n每月")], "tool_calls"),
    ])
    _, message = await run(app, model, "我每周都看质押项目")
    assert [s["text"] for s in message["metadata"]["steps"] if s["text"]] == ["记下了，存到 Goals 里了。", "要不要设一个每周简报？频率怎么定？"]
    model = ScriptedModel([
        Completion("", [call("ask_user", question="要继续吗？", options="好")], "tool_calls"),
        Completion("要继续吗？", [], "stop"),
    ])
    _, message = await run(app, model, "继续")
    assert "ask" not in message["metadata"] and message["metadata"]["steps"][0]["calls"][0]["status"] == "error"


@pytest.mark.asyncio
async def test_a_goal_is_not_offered_option_questions(app):
    model = ScriptedModel([Completion("", [call("finish_goal", status="blocked", summary="需要你确认预算")], "tool_calls")])
    await run(app, model, "把预算表做完", mode="goal")
    assert "ask_user" not in {t["function"]["name"] for t in model.calls[0]["tools"]}


def task_jobs(app):
    return [j for j in app.state.store.all("SELECT * FROM jobs WHERE kind='chat' ORDER BY created_at") if j["payload"].get("task")]


@pytest.mark.asyncio
async def test_longer_work_is_started_in_the_background_and_delivered_below_what_was_said_meanwhile(app):
    store = app.state.store
    brief = "算三个典型区间（±1%、±10%、全区间）在价格 ±20% 时的无常损失，保存为 il.md。入场价居中，不计手续费。"
    model = ScriptedModel([
        Completion("", [call("start_task", title="IL 对照表", brief=brief), {**call("start_task", title="IL 对照表", brief=brief + "再来一次"), "id": "call_again"}], "tool_calls"),
        Completion("表在做了，入场价居中、不计手续费，算好发你。", [], "stop"),
    ])
    _, reply = await run(app, model, "先算几个典型区间的 IL 对照表")
    assert {"ask_user", "start_task"} <= {t["function"]["name"] for t in model.calls[0]["tools"]}
    first, again = reply["metadata"]["steps"][0]["calls"]
    assert first["status"] == "ok" and again["status"] == "error"  # the same task is not started twice
    (job,) = task_jobs(app)
    assert first["job_id"] == job["id"] and job["status"] == "queued" and job["payload"]["background"] is True
    card = store.one("SELECT * FROM messages WHERE id=?", (job["payload"]["message_id"],))
    assert card["status"] == "queued" and card["metadata"]["task"] == {"title": "IL 对照表"}

    # the conversation goes on while the task is queued (the reply that started it has finished)
    for done in store.all("SELECT * FROM jobs WHERE kind='chat'"):
        if not done["payload"].get("task"):
            store.update_job(done["id"], status="succeeded")
    client = TestClient(app)
    token = {"Authorization": "Bearer " + app.state.security.local_token}
    sent = client.post("/api/sessions/s1/messages", json={"content": "顺便问下，今天周几", "request_id": "request-while-task-runs"}, headers=token)
    assert sent.status_code == 200
    store.update_job(sent.json()["id"], status="succeeded")
    store.update_message(sent.json()["payload"]["message_id"], "周五。", "complete")

    # the task runs as a goal on its brief, without the tools for starting tasks or asking with buttons
    worker = app.state.worker
    worker.agent.runtime = task_model = ScriptedModel([
        Completion("", [{"id": "call_save", "name": "save_file", "arguments": json.dumps({"name": "il.md", "content": "| 区间 | +20% | -20% |\n| ±10% | -6.6% | -9.1% |"})}], "tool_calls"),
        Completion("", [call("finish_goal", status="done", summary="表做好了，存在 il.md：±10% 区间涨 20% 时无常损失约 -6.6%。", evidence="save_file ok")], "tool_calls"),
        Completion('{"achieved": true, "missing": ""}', [], "stop"),
    ])
    await worker.agent.run(store.get_job(job["id"]))
    offered = {t["function"]["name"] for t in task_model.calls[0]["tools"]}
    assert "finish_goal" in offered and not offered & {"start_task", "ask_user"}
    assert task_model.calls[0]["messages"][-1]["content"].startswith("IL 对照表\n\n" + brief)
    assert "Background task" in task_model.calls[0]["messages"][0]["content"]
    rows = store.all("SELECT id,role,content,status FROM messages WHERE session_id='s1' ORDER BY created_at")
    assert rows[-1]["id"] == card["id"] and rows[-1]["status"] == "complete" and "il.md" in rows[-1]["content"]
    assert [r["content"] for r in rows[-3:-1]] == ["顺便问下，今天周几", "周五。"]


@pytest.mark.asyncio
async def test_a_reply_that_keeps_working_hands_the_conversation_back(app):
    store = app.state.store
    turns = [Completion("", [{**call("calculate", expression=f"{n}+1"), "id": f"c{n}"}], "tool_calls") for n in range(8)]
    model = ScriptedModel([*turns, Completion("都算完了。", [], "stop")])
    job_seen = {}

    async def stream(messages, **options):
        (job,) = store.all("SELECT * FROM jobs WHERE kind='chat'")
        job_seen[len(model.calls)] = bool(job["payload"].get("background"))
        return await ScriptedModel.stream(model, messages, **options)
    watcher = SimpleNamespace(stream=stream)
    worker = app.state.worker
    worker.agent.runtime = watcher
    from tests.test_agent import chat_job
    job, mid = chat_job(app, "把这几个数都加一")
    await worker.agent.run(job)
    message = store.one("SELECT * FROM messages WHERE id=?", (mid,))
    assert job_seen[0] is False and job_seen[5] is False and job_seen[6] is True  # after six rounds of tools the conversation is free
    assert message["metadata"]["background"] is True and message["content"] == "都算完了。"


def test_schedule_rules_fall_on_the_next_matching_time():
    from datetime import datetime
    from engine.agenda import describe, make_rule, next_run
    friday = datetime(2026, 11, 20, 12, 0)
    monthly = make_rule("month", "9:00", day=1)
    assert next_run(monthly, friday) == datetime(2026, 12, 1, 9, 0) and describe(monthly) == "每月 1 号 09:00"
    assert next_run(make_rule("month", "09:00", day=31), datetime(2027, 1, 31, 10, 0)) == datetime(2027, 2, 28, 9, 0)  # a month without a 31st
    assert next_run(make_rule("week", "08:30", weekday="fri"), friday) == datetime(2026, 11, 27, 8, 30)  # this Friday's time has passed
    assert next_run(make_rule("week", "18:00", weekday="fri"), friday) == datetime(2026, 11, 20, 18, 0)
    assert next_run(make_rule("day", "07:00"), friday) == datetime(2026, 11, 21, 7, 0)
    assert next_run(make_rule("once", "09:00", date="2026-11-19"), friday) is None
    for bad in (dict(every="week"), dict(every="month"), dict(every="once", date="明天"), dict(every="day", at="25:00")):
        with pytest.raises(ValueError):
            make_rule(**bad)


@pytest.mark.asyncio
async def test_a_goal_is_saved_once_and_a_schedule_delivers_its_runs_into_the_conversation(app):
    from datetime import datetime, timedelta
    store, agenda = app.state.store, app.state.worker.tools.agenda
    brief = "复盘本月 L2 的技术路线和生态变化：TVL、Stage 进展、重要升级，对比上月。"
    model = ScriptedModel([
        Completion("", [call("agenda")], "tool_calls"),
        Completion("", [call("track_goal", title="L2 长期跟踪", detail="技术路线和生态进展"),
                        call("schedule_task", title="L2 每月深度复盘", brief=brief, every="month", day=1, time="09:00")], "tool_calls"),
        Completion("记下了，每月 1 号上午复盘。", [], "stop"),
    ])
    _, reply = await run(app, model, "我想长期跟踪 L2，按月给我复盘")
    assert {"agenda", "track_goal", "schedule_task", "cancel_schedule"} <= {t["function"]["name"] for t in model.calls[0]["tools"]}
    goal_call, schedule_call = reply["metadata"]["steps"][1]["calls"]
    assert goal_call["card"] == {"kind": "goal", "title": "L2 长期跟踪", "subtitle": "长期目标"}
    assert schedule_call["card"] == {"kind": "schedule", "title": "L2 每月深度复盘", "subtitle": "定时任务 · 每月 1 号 09:00"}
    assert "first_run" in json.loads(schedule_call["output"])
    assert agenda.track("s1", "L2长期跟踪")["existing"] and len(agenda.goals()) == 1  # nearly the same title is the same goal
    (schedule,) = agenda.schedules()
    assert schedule["when"] == "每月 1 号 09:00" and schedule["next_run"] > datetime.now().timestamp()

    # nothing runs before its time; when it is due it starts once as a background task and moves to the next month
    assert agenda.fire() == []
    due = datetime.fromtimestamp(schedule["next_run"]) + timedelta(minutes=1)
    (job,) = agenda.fire(due)
    assert job["payload"]["background"] is True and job["payload"]["schedule_id"] == schedule["id"] and job["payload"]["session_id"] == "s1"
    assert agenda.fire(due + timedelta(minutes=1)) == []
    assert agenda.schedules()[0]["next_run"] > due.timestamp()
    worker = app.state.worker
    worker.agent.runtime = task_model = ScriptedModel([
        Completion("", [call("finish_goal", status="done", summary="本月复盘：总锁定价值小幅上升。", evidence="无需工具")], "tool_calls"),
        Completion('{"achieved": true, "missing": ""}', [], "stop"),
    ])
    await worker.agent.run(job)
    system = task_model.calls[0]["messages"][0]["content"]
    assert "Scheduled run" in system and "每月 1 号 09:00" in system and task_model.calls[0]["messages"][-1]["content"].startswith("L2 每月深度复盘\n\n" + brief)
    last = store.all("SELECT * FROM messages WHERE session_id='s1' ORDER BY created_at")[-1]
    assert last["content"] == "本月复盘：总锁定价值小幅上升。" and last["metadata"]["task"] == {"title": "L2 每月深度复盘", "scheduled": "每月 1 号 09:00"}

    # a one-time schedule runs once and is then off; a schedule whose conversation is gone gets a conversation of its own
    once = agenda.schedule(None, "提醒交房租", "提醒我今天交房租，金额见上月记录。", {"every": "once", "at": "09:00", "date": "2030-01-01"}, now=datetime(2029, 12, 31, 8, 0))
    (reminder,) = agenda.fire(datetime(2030, 1, 1, 9, 1))
    assert store.one("SELECT title FROM sessions WHERE id=?", (reminder["payload"]["session_id"],))["title"] == "提醒交房租"
    assert once["id"] not in {s["id"] for s in agenda.schedules()}

    client = TestClient(app)
    token = {"Authorization": "Bearer " + app.state.security.local_token}
    listed = client.get("/api/agenda", headers=token).json()
    assert [g["title"] for g in listed["goals"]] == ["L2 长期跟踪"] and [s["title"] for s in listed["schedules"]] == ["L2 每月深度复盘"]
    assert client.delete("/api/schedules/" + schedule["id"], headers=token).status_code == 200 and agenda.schedules() == []
    assert client.delete("/api/schedules/nope", headers=token).status_code == 404


@pytest.mark.asyncio
async def test_options_sent_as_a_json_list_are_read_and_a_silent_reply_is_told_to_speak_first(app):
    model = ScriptedModel([
        Completion("", [call("track_goal", title="质押收益来源")], "tool_calls"),
        Completion("", [call("ask_user", question="多久看一次？", options='["每周一次", "每月一次"]')], "tool_calls"),
        Completion("记下了，存到 Goals 里了。", [{**call("ask_user", question="多久看一次？", options=["每周一次", "每月一次"]), "id": "call_again"}], "tool_calls"),
    ])
    _, message = await run(app, model, "我每周都会看质押项目")
    steps = message["metadata"]["steps"]
    assert steps[1]["calls"][0]["status"] == "error" and "written anything" in steps[1]["calls"][0]["output"]
    assert message["metadata"]["ask"]["options"] == ["每周一次", "每月一次"]
    assert [s["text"] for s in steps if s["text"]] == ["记下了，存到 Goals 里了。", "多久看一次？"]
