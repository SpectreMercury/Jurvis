"""The vault: memories as Markdown the user can correct, the diary, the nightly tidy-up, and a goal's space."""
import json
import time
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.runtime import Completion
from engine.vault import Vault, page_of


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path), run_worker=False)


@pytest.fixture
def client(app):
    token = app.state.security.local_token
    with TestClient(app, headers={"Authorization": f"Bearer {token}"}) as c:
        yield c


class Scripted:
    def __init__(self, turns):
        self.turns, self.calls = list(turns), []

    async def stream(self, messages, *, tools=None, tool_choice=None, max_tokens=0, temperature=0, on_text=None, model=None, search=None):
        self.calls.append({"messages": [dict(m) for m in messages], "tools": tools, "model": model})
        turn = self.turns.pop(0)
        if on_text and turn.text:
            await on_text(turn.text)
        return turn


def facts(app, page):
    return [r["content"] for r in app.state.store.all("SELECT content FROM memories WHERE page=? AND enabled=1 ORDER BY created_at", (page,))]


def test_page_names_are_kept_to_the_three_folders_and_safe_file_names():
    assert page_of("人物/妈妈") == "人物/妈妈" and page_of("主题：健身") == "主题/健身" and page_of("习惯/写邮件.md") == "习惯/写邮件"
    assert page_of("../etc/passwd") is None and page_of("秘密/x") is None and page_of("主题/") is None
    assert page_of("主题/a/b:c?") == "主题/a b c"


def test_memories_appear_as_pages_and_the_users_edits_come_back(app):
    memory, vault = app.state.worker.memory, app.state.worker.memory.vault
    memory.save("教练", "健身教练是小李", page="人物/小李")
    memory.save("健身", "每周三、周六晚上去健身，教练是小李", page="主题/健身", importance=0.8)
    memory.save("膝盖", "膝盖有旧伤，避免深蹲", page="主题/健身")
    memory.save("闲聊", "喜欢喝手冲咖啡", tags=["饮食"])
    memory.save("别的", "周末常去爬山")
    assert vault.sync() == {"taken": 0, "written": 4}
    page = vault.file("主题/健身").read_text(encoding="utf-8")
    assert page.startswith("---\n类型: 主题\n更新: " + datetime.now().strftime("%Y-%m-%d") + "\n---\n# 健身\n\n- 每周三、周六晚上去健身，教练是[[小李]]\n- 膝盖有旧伤，避免深蹲\n")
    assert "来源：[[" + datetime.now().strftime("%Y-%m-%d") + "]]" in page
    assert vault.file("主题/饮食").exists() and "周末常去爬山" in vault.file("主题/杂记").read_text(encoding="utf-8")
    assert vault.sync() == {"taken": 0, "written": 0}  # nothing changed: nothing is rewritten
    # The user corrects the page in their editor: one line changed, one removed, one added in their own way of writing.
    vault.file("主题/健身").write_text("# 健身\n\n每周二、周四晚上去健身，教练是[[小李]]\n* 最近在练硬拉\n", encoding="utf-8")
    assert vault.sync()["taken"] == 1
    assert facts(app, "主题/健身") == ["每周二、周四晚上去健身，教练是小李", "最近在练硬拉"]
    off = app.state.store.all("SELECT content,enabled FROM memories WHERE enabled=0")
    assert sorted(r["content"] for r in off) == ["每周三、周六晚上去健身，教练是小李", "膝盖有旧伤，避免深蹲"]  # switched off, not lost
    assert "- 最近在练硬拉" in vault.file("主题/健身").read_text(encoding="utf-8")  # written back in the usual form
    # What they took out stays out when it is only overheard again; said on purpose, it comes back.
    memory.save("膝盖", "膝盖有旧伤，避免深蹲", source="chat", page="主题/健身")
    assert "膝盖" not in "".join(facts(app, "主题/健身"))
    memory.save("膝盖", "膝盖有旧伤，避免深蹲", page="主题/健身")
    assert "膝盖有旧伤，避免深蹲" in facts(app, "主题/健身")


def test_a_page_the_user_writes_or_deletes_is_a_set_of_memories_made_or_switched_off(app):
    memory, vault = app.state.worker.memory, app.state.worker.memory.vault
    memory.save("猫", "养了一只猫叫年糕", page="主题/宠物")
    vault.sync()
    (vault.root / "人物").mkdir(parents=True, exist_ok=True)
    vault.file("人物/妈妈").write_text("- 生日是 3 月 12 日\n- 住在苏州\n", encoding="utf-8")
    vault.file("主题/宠物").unlink()
    vault.sync()
    assert facts(app, "人物/妈妈") == ["生日是 3 月 12 日", "住在苏州"] and facts(app, "主题/宠物") == []
    row = app.state.store.one("SELECT source,importance,title FROM memories WHERE content='住在苏州'")
    assert row == {"source": "vault", "importance": 0.7, "title": "妈妈"}
    assert not vault.file("主题/宠物").exists() and vault.file("人物/妈妈").read_text(encoding="utf-8").startswith("---\n类型: 人物")
    # A memory switched off on the 记忆 screen leaves its page; the last one takes the page with it.
    app.state.store.execute("UPDATE memories SET enabled=0 WHERE page='人物/妈妈'")
    vault.sync()
    assert not vault.file("人物/妈妈").exists()


@pytest.mark.asyncio
async def test_extraction_files_facts_on_pages_and_the_diary_links_to_them(app):
    worker, store = app.state.worker, app.state.store
    at = time.time() - 7200
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('s1','搬家的事',?,?)", (at, at + 2))
    store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES('u','s1','user','我下个月搬去杭州，妈妈也一起',?,?)", ("complete", at))
    store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES('a','s1','assistant','好的，记下了。',?,?)", ("complete", at + 1))
    worker.memory.save("旧的", "住在上海", page="主题/住处")
    extracted = [{"title": "城市", "content": "下个月搬去杭州", "importance": 0.8, "page": "主题/住处"},
                 {"title": "妈妈", "content": "妈妈会一起搬去杭州", "page": "人物/妈妈"},
                 {"title": "乱写的", "content": "喜欢安静的小区", "page": "别处/乱放"}]
    model = Scripted([Completion(json.dumps(extracted, ensure_ascii=False), [], "stop"), Completion("- 下个月搬去杭州，妈妈一起", [], "stop")])
    worker.runtime.stream = model.stream
    result = await worker.memory.process("s1")
    assert result["memories"] == 3 and result["recorded"] == 1
    assert "Existing pages: 主题/住处" in model.calls[0]["messages"][-1]["content"]
    vault = worker.memory.vault
    assert facts(app, "主题/住处") == ["住在上海", "下个月搬去杭州"] and facts(app, "人物/妈妈") == ["妈妈会一起搬去杭州"]
    assert facts(app, "主题/杂记") == ["喜欢安静的小区"]  # a page outside the three folders is not made
    day = vault.root / "日记" / (datetime.fromtimestamp(at + 1).strftime("%Y-%m-%d") + ".md")
    diary = day.read_text(encoding="utf-8")
    assert "## " + datetime.fromtimestamp(at + 1).strftime("%H:%M") + " 搬家的事" in diary and "我下个月搬去杭州" in diary
    assert "涉及：[[住处]]、[[杂记]]、[[妈妈]]" in diary
    assert (vault.root / "技能").is_symlink()


@pytest.mark.asyncio
async def test_the_nightly_tidy_up_merges_what_changed_and_leaves_the_users_lines_alone(app):
    worker, store = app.state.worker, app.state.store
    memory = worker.memory
    memory.save("城市", "住在上海", source="chat", page="主题/住处")
    memory.save("城市", "2026 年 12 月搬到了杭州", source="chat", page="主题/住处")
    memory.save("小区", "喜欢安静的小区", page="主题/住处")  # the user's own
    memory.save("猫", "养了一只猫叫年糕", source="chat", page="主题/宠物")
    night = datetime.now().replace(hour=4, minute=0).timestamp()
    assert memory.due(night) and not memory.due(datetime.now().replace(hour=1).timestamp())
    model = Scripted([Completion('```json\n["2026 年 12 月搬到了杭州（之前是住在上海）"]\n```', [], "stop"), Completion("- 住在杭州，养猫", [], "stop")])
    worker.llm.stream = model.stream
    result = await memory.consolidate("miavo:x", now=night)
    assert result == {"recorded": 0, "tidied": 1, "portrait": True} and len(model.calls) == 2  # one page rewritten (a page with one fact needs none), then the portrait
    assert memory.portrait() == "- 住在杭州，养猫"
    asked = model.calls[0]["messages"][-1]["content"]
    assert "Page: 主题/住处" in asked and "Kept as they are:\n- 喜欢安静的小区" in asked
    assert sorted(facts(app, "主题/住处")) == ["2026 年 12 月搬到了杭州（之前是住在上海）", "喜欢安静的小区"]
    assert "（之前是住在上海）" in memory.vault.file("主题/住处").read_text(encoding="utf-8")
    assert not memory.due(night)  # once a night
    assert await memory.tidy("miavo:x") == 0 and len(model.calls) == 2  # nothing new since: no page is rewritten again


@pytest.mark.asyncio
async def test_a_rewrite_that_grows_the_page_or_is_no_list_is_not_taken_and_the_users_line_is_never_touched(app):
    worker = app.state.worker
    memory = worker.memory
    memory.save("城市", "住在上海", source="chat", page="主题/住处")
    memory.save("城市", "搬到了杭州", source="chat", page="主题/住处")
    memory.save("小区", "喜欢安静的小区", page="主题/住处")
    for reply in ('["住在上海", "搬到了杭州", "可能喜欢西湖"]', '["喜欢安静的小区"]', "抱歉，我做不到"):
        app.state.store.execute("UPDATE pages SET tidied_at=0")
        worker.llm.stream = Scripted([Completion(reply, [], "stop")]).stream
        assert await memory.tidy("miavo:x") == 0
        assert sorted(facts(app, "主题/住处")) == ["住在上海", "喜欢安静的小区", "搬到了杭州"]
    app.state.store.execute("UPDATE pages SET tidied_at=0")
    worker.llm.stream = Scripted([Completion('["搬到了杭州（之前是住在上海）"]', [], "stop")]).stream  # the user's line is not in the reply, and stays
    assert await memory.tidy("miavo:x") == 1
    assert sorted(facts(app, "主题/住处")) == ["喜欢安静的小区", "搬到了杭州（之前是住在上海）"]


# ---- spaces ----

@pytest.mark.asyncio
async def test_a_goal_gets_a_space_with_its_notes_its_rhythm_and_where_it_was_talked_about(client, app):
    worker, store = app.state.worker, app.state.store
    job = client.post("/api/stream/messages", json={"content": "想长期跟踪以太坊 L2 的进展", "request_id": "space-request-1"}).json()
    sid = job["payload"]["session_id"]
    calls = [{"id": "c1", "name": "track_goal", "arguments": json.dumps({"title": "以太坊 L2 长期跟踪", "detail": "技术路线和生态数据"}, ensure_ascii=False)},
             {"id": "c2", "name": "schedule_task", "arguments": json.dumps({"title": "L2 周报", "brief": "汇总本周以太坊 L2 的技术路线和生态数据进展，列出三件最值得看的事", "every": "week", "weekday": "mon", "time": "09:00"}, ensure_ascii=False)}]
    model = Scripted([Completion("", calls, "tool_calls"), Completion("好，记下了，每周一早上发你简报。", [], "stop")])
    worker.agent.runtime = model
    # What this stretch had already noted on a page of its own moves to the goal's page; a page other talk also wrote on stays.
    worker.memory.save("兴趣", "在研究以太坊扩容", source="chat", page="主题/L2 研究", session_id=sid)
    worker.memory.save("工作", "在一家交易所做研究员", source="chat", page="主题/工作", session_id=sid)
    worker.memory.save("工作", "办公室在深圳湾", source="chat", page="主题/工作")
    await worker.agent.run(job)
    goal = client.get("/api/agenda").json()["goals"][0]
    assert goal["page"] == "主题/以太坊 L2 长期跟踪"
    assert {m["content"]: m["page"] for m in store.all("SELECT content,page FROM memories WHERE source='chat'")} == {
        "在研究以太坊扩容": goal["page"], "在一家交易所做研究员": "主题/工作", "办公室在深圳湾": "主题/工作"}
    store.execute("DELETE FROM memories WHERE source='chat'")
    worker.memory.save("格局", "Base 的 TVL 约 148 亿美元", source="chat", page=goal["page"], session_id=sid)
    worker.memory.vault.sync()
    space = client.get("/api/spaces/" + goal["id"]).json()
    assert [f["content"] for f in space["facts"]] == ["长期关注：以太坊 L2 长期跟踪。技术路线和生态数据", "Base 的 TVL 约 148 亿美元"]
    # When the goal changes, its line on the page changes with it.
    worker.memory.space(worker.tools.agenda.track(sid, "以太坊 L2 长期跟踪", "只看生态数据，每月一次"))
    assert [f["content"] for f in client.get("/api/spaces/" + goal["id"]).json()["facts"] if f["source"] == "goal"] == ["长期关注：以太坊 L2 长期跟踪。只看生态数据，每月一次"]
    worker.memory.space(worker.tools.agenda.track(sid, "以太坊 L2 长期跟踪", "技术路线和生态数据"))
    assert [s["title"] for s in space["schedules"]] == ["L2 周报"] and space["schedules"][0]["rule"]["every"] == "week"
    assert [s["id"] for s in space["stretches"]] == [sid] and space["file"].endswith("vault/主题/以太坊 L2 长期跟踪.md")
    assert "- Base 的 TVL 约 148 亿美元" in open(space["file"], encoding="utf-8").read()
    assert client.get("/api/spaces/nope").status_code == 404
    assert client.get("/api/state").json()["vault"].endswith("/vault")
