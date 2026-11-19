"""File search, ranged reads, background commands, plan mode, tool lookup and skills from several places."""
import asyncio
import json
from types import SimpleNamespace

import pytest

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
