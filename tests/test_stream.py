"""The one conversation: which stretch a message belongs to, what comes back from other stretches, and the portrait."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings
from engine.runtime import Completion
from engine.stream import GAP


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


def say(client, text, n=[0], **more):
    n[0] += 1
    return client.post("/api/stream/messages", json={"content": text, "request_id": f"request-{n[0]:04d}", **more})


def finish(app, job, reply="好的。"):
    """Stands in for the worker: the reply is written and the job done."""
    store = app.state.store
    store.update_message(job["payload"]["message_id"], reply, "complete")
    store.update_job(job["id"], status="succeeded")


def age(app, session_id, seconds):
    """Moves a stretch into the past."""
    store = app.state.store
    store.execute("UPDATE messages SET created_at=created_at-? WHERE session_id=?", (seconds, session_id))
    store.execute("UPDATE sessions SET created_at=created_at-?,updated_at=updated_at-?,focused_at=focused_at-? WHERE id=?", (seconds, seconds, seconds, session_id))
    store.execute("UPDATE jobs SET updated_at=updated_at-?,created_at=created_at-? WHERE json_extract(payload,'$.session_id')=?", (seconds, seconds, session_id))


def past(app, ident, exchanges, ago=86400, **columns):
    """A finished stretch from some time ago."""
    store, at = app.state.store, time.time() - ago
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES(?,?,?,?)", (ident, exchanges[0][0][:40], at, at + len(exchanges)))
    for key, value in columns.items():
        store.execute(f"UPDATE sessions SET {key}=? WHERE id=?", (value, ident))
    for n, (asked, answered) in enumerate(exchanges):
        store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES(?,?,'user',?,'complete',?)", (f"{ident}u{n}", ident, asked, at + n))
        store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES(?,?,'assistant',?,'complete',?)", (f"{ident}a{n}", ident, answered, at + n + .5))
    return ident


async def reply(app, model, text, ident, **payload):
    worker, store = app.state.worker, app.state.store
    worker.agent.runtime = model
    worker.llm.stream = model.stream
    store.add_message(ident, "user", text)
    mid = store.add_message(ident, "assistant", "", "queued")
    job = store.create_job("chat", {"session_id": ident, "message_id": mid, **payload})
    await worker.agent.run(job)
    return job


# ---- which stretch a message belongs to ----

def test_messages_continue_the_same_stretch_until_a_pause(client, app):
    first = say(client, "帮我想想周末去哪玩").json()
    assert say(client, "还在吗").status_code == 409  # one conversation, one reply at a time
    finish(app, first)
    second = say(client, "预算两千以内").json()
    finish(app, second)
    assert second["payload"]["session_id"] == first["payload"]["session_id"] and second["payload"]["stream"] is True
    age(app, first["payload"]["session_id"], GAP + 60)
    third = say(client, "明天的会议几点").json()
    assert third["payload"]["session_id"] != first["payload"]["session_id"]
    page = client.get("/api/stream").json()
    assert [m["content"] for m in page["messages"] if m["role"] == "user"] == ["帮我想想周末去哪玩", "预算两千以内", "明天的会议几点"]
    assert page["current"] == third["payload"]["session_id"] and not page["earlier"] and not page["later"]
    assert [s["title"] for s in client.get("/api/state").json()["sessions"]] == ["明天的会议几点", "帮我想想周末去哪玩"]


def test_a_long_reply_does_not_end_the_stretch(client, app):
    first = say(client, "写一份很长的报告").json()
    store, ident = app.state.store, first["payload"]["session_id"]
    age(app, ident, GAP + 600)
    finish(app, first)  # the reply took longer than the pause; it has only just ended
    assert say(client, "第二段再短一点").json()["payload"]["session_id"] == ident
    assert store.one("SELECT COUNT(*) AS n FROM sessions")["n"] == 1


def test_a_code_project_or_going_off_the_record_begins_a_stretch(client, app, tmp_path):
    first = say(client, "你好").json(); finish(app, first)
    same = say(client, "继续").json(); finish(app, same)
    quiet = say(client, "这段别记", incognito=True).json(); finish(app, quiet)
    still = say(client, "这句也别记").json(); finish(app, still)
    back = say(client, "可以记了", incognito=False).json(); finish(app, back)
    fresh = say(client, "换个话题", fresh=True).json(); finish(app, fresh)
    ids = [j["payload"]["session_id"] for j in (first, same, quiet, still, back, fresh)]
    assert ids[0] == ids[1] and ids[2] == ids[3] and len(set(ids)) == 4
    rows = {r["id"]: r for r in app.state.store.all("SELECT id,incognito FROM sessions")}
    assert rows[ids[2]]["incognito"] == 1 and rows[ids[4]]["incognito"] == 0
    assert say(client, "看下这个项目", code_project_id="missing").status_code == 400


def test_a_reply_to_something_jurvis_wrote_on_its_own_goes_where_it_was_written(client, app):
    store = app.state.store
    old = past(app, "old", [("每周给我一份简报", "好，每周一早上发你。")], ago=3 * 86400)
    store.add_message(old, "assistant", "本周简报：三件事值得看。", "complete", {"task": {"title": "每周简报"}})
    assert say(client, "第二件展开说说").json()["payload"]["session_id"] == old


def test_the_stream_pages_backwards_and_opens_at_a_search_result(client, app):
    past(app, "a", [(f"第{n}个问题", f"第{n}个回答") for n in range(6)], ago=5 * 86400)
    past(app, "b", [("蓝莓松饼怎么做", "面粉两百克，蓝莓一百克。")], ago=2 * 86400)
    newest = client.get("/api/stream", params={"limit": 4}).json()
    assert [m["content"] for m in newest["messages"]] == ["第5个问题", "第5个回答", "蓝莓松饼怎么做", "面粉两百克，蓝莓一百克。"] and newest["earlier"]
    before = client.get("/api/stream", params={"limit": 4, "before": newest["messages"][0]["created_at"]}).json()
    assert [m["content"] for m in before["messages"]] == ["第3个问题", "第3个回答", "第4个问题", "第4个回答"] and before["earlier"] and before["later"]
    found = client.get("/api/stream/search", params={"q": "蓝莓"}).json()["results"]
    assert [r["role"] for r in found] == ["assistant", "user"] and "蓝莓一百克" in found[0]["snippet"] and found[0]["title"] == "蓝莓松饼怎么做"
    opened = client.get("/api/stream", params={"at": found[1]["id"], "limit": 50}).json()
    assert opened["messages"][4]["id"] == found[1]["id"] and opened["messages"][0]["content"] == "第4个问题" and not opened["later"]
    start = client.get("/api/stream", params={"start": "b"}).json()
    assert start["messages"][0]["content"] == "蓝莓松饼怎么做" and start["earlier"]
    assert client.get("/api/stream/search", params={"q": "100%"}).json()["results"] == []


# ---- what comes back from other stretches ----

@pytest.mark.asyncio
async def test_the_stretch_before_and_older_ones_on_the_same_subject_reach_the_model(app):
    worker, store = app.state.worker, app.state.store
    past(app, "trip", [("帮我规划十月去京都的行程", "建议住四条河原町，第二天去岚山，清水寺放在最后一天傍晚。")], ago=40 * 86400)
    past(app, "car", [("沃尔沃保养多久一次", "一万公里或一年。")], ago=30 * 86400)
    past(app, "lunch", [("中午吃什么好", "附近那家牛肉面不错。")], ago=1200)
    past(app, "secret", [("京都行程里别告诉别人的那件事", "好的，不会记。")], ago=20 * 86400, incognito=1)
    assert {r["id"] for r in worker.memory.stale()} == {"trip", "car"}  # quiet long enough and on the record; lunch was minutes ago
    assert await worker.memory.digest("local") == 2
    now = store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('now','新对话',?,?)", (time.time(), time.time()))
    model = Scripted([Completion("清水寺安排在最后一天傍晚。", [], "stop")])
    await reply(app, model, "上次京都行程里清水寺放在哪天", "now")
    note = model.calls[0]["messages"][-1]["content"]
    assert "What we talked about just before" in note and "牛肉面" in note          # continuity with the stretch just before
    assert "Earlier conversations of ours" in note and "岚山" in note                # the older one on this subject
    assert "沃尔沃" not in note and "别告诉别人" not in note                          # another subject; off the record
    assert "Passages from those earlier conversations" in note and "User: 帮我规划十月去京都的行程" in note
    assert "岚山" not in model.calls[0]["messages"][0]["content"]                    # the system prompt stays the same from turn to turn


@pytest.mark.asyncio
async def test_an_off_the_record_stretch_is_given_nothing_from_the_others(app):
    past(app, "trip", [("帮我规划十月去京都的行程", "第二天去岚山。")], ago=600)
    past(app, "quiet", [("你好", "你好。")], ago=10, incognito=1)
    model = Scripted([Completion("好的。", [], "stop")])
    await reply(app, model, "京都行程第二天去哪", "quiet")
    assert "岚山" not in json.dumps(model.calls[0]["messages"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_a_finished_stretch_gets_a_record_and_a_name(app):
    worker, store = app.state.worker, app.state.store
    long = [("帮我给房东写一封续租的邮件，租金想谈到每月六千五", "好的。" + "邮件正文。" * 80), ("语气再客气一点", "已改得更客气。" + "新的正文。" * 60)]
    past(app, "mail", long, ago=7200)
    past(app, "named", long, ago=9000, title="我起的名字")
    past(app, "short", [("谢谢", "不客气。")], ago=8000)
    model = Scripted([Completion("续租邮件：把租金谈到六千五\n\n用户要给房东写续租邮件，目标租金每月 6500；已写好并改得更客气。", [], "stop")] * 2)
    worker.llm.stream = model.stream
    assert await worker.memory.digest("miavo:x") == 3
    rows = {r["session_id"]: r["text"] for r in store.all("SELECT session_id,text FROM digests")}
    assert rows["mail"].startswith("用户要给房东写续租邮件") and "谢谢" in rows["short"] and len(model.calls) == 2
    titles = {r["id"]: r["title"] for r in store.all("SELECT id,title FROM sessions")}
    assert titles["mail"] == "续租邮件：把租金谈到六千五" and titles["named"] == "我起的名字"
    assert worker.memory.stale() == []
    store.add_message("mail", "user", "房东回信了"); age(app, "mail", 3600)
    assert [r["id"] for r in worker.memory.stale()] == ["mail"]  # said more since: the record is out of date


# ---- a change of subject ----

def cloud_tools(app):
    worker = app.state.worker
    worker.prefs.set_provider("miavo", api_key="sk-maas-test")
    worker.prefs.update({"chat_model": "miavo:claude-haiku-4-5", "tool_model": "miavo:claude-haiku-4-5"})


TALK = [("帮我规划十月去京都的行程", "住四条河原町。"), ("第二天去哪", "岚山。"), ("清水寺呢", "最后一天傍晚。")]


@pytest.mark.asyncio
async def test_a_change_of_subject_begins_a_new_stretch(app):
    store = app.state.store
    cloud_tools(app)
    past(app, "trip", TALK, ago=300)
    model = Scripted([Completion("NEW", [], "stop"), Completion("可以用 pandas 读取。", [], "stop")])
    job = await reply(app, model, "Python 怎么读取 Excel 文件", "trip", stream=True)
    moved = job["payload"]["session_id"]
    assert moved != "trip" and store.get_job(job["id"])["payload"]["session_id"] == moved
    assert [m["content"] for m in store.all("SELECT content FROM messages WHERE session_id=? ORDER BY created_at", (moved,))] == ["Python 怎么读取 Excel 文件", "可以用 pandas 读取。"]
    assert store.one("SELECT title FROM sessions WHERE id=?", (moved,))["title"] == "Python 怎么读取 Excel 文件"
    sent = model.calls[1]["messages"]
    assert [m["content"] for m in sent if m["role"] == "assistant"] == []       # the trip is no longer replayed
    assert "What we talked about just before" in sent[-1]["content"] and "清水寺" in sent[-1]["content"]  # but it is still at hand


@pytest.mark.asyncio
async def test_a_follow_up_stays_and_a_message_on_the_subject_is_not_even_asked_about(app):
    cloud_tools(app)
    past(app, "trip", TALK, ago=300)
    model = Scripted([Completion("SAME", [], "stop"), Completion("带一件薄外套。", [], "stop"), Completion("两小时。", [], "stop")])
    job = await reply(app, model, "那边冷不冷，穿什么合适", "trip", stream=True)
    assert job["payload"]["session_id"] == "trip"
    job = await reply(app, model, "岚山要逛多久", "trip", stream=True)  # shares words with the talk: plainly the same subject
    assert job["payload"]["session_id"] == "trip" and len(model.calls) == 3


@pytest.mark.asyncio
async def test_the_local_model_is_asked_too(app):
    past(app, "trip", TALK, ago=300)
    model = Scripted([Completion("NEW", [], "stop"), Completion("可以用 pandas 读取。", [], "stop")])
    job = await reply(app, model, "Python 怎么读取 Excel 文件", "trip", stream=True)
    assert job["payload"]["session_id"] != "trip" and len(model.calls) == 2


@pytest.mark.asyncio
async def test_a_message_about_something_from_earlier_goes_back_there(app):
    store = app.state.store
    past(app, "run", [("帮我做个跑步计划", "第一周跑一分钟走两分钟，循环四组。")], ago=3600)
    past(app, "eggs", [("番茄炒蛋要注意什么", "蛋先炒，盛出来再炒番茄。")], ago=120)
    model = Scripted([Completion("1", [], "stop"), Completion("四组，一共十二分钟。", [], "stop")])
    job = await reply(app, model, "第一周一共要循环几组来着", "eggs", stream=True)
    assert "1. 帮我做个跑步计划" in model.calls[0]["messages"][1]["content"]
    assert job["payload"]["session_id"] == "run"
    assert [m["content"] for m in store.all("SELECT content FROM messages WHERE session_id='run' ORDER BY created_at")][-2:] == ["第一周一共要循环几组来着", "四组，一共十二分钟。"]
    assert [m["content"] for m in model.calls[1]["messages"] if m["role"] == "assistant"] == ["第一周跑一分钟走两分钟，循环四组。"]  # with what was said there
    # One exchange is too little to call a subject: something unrelated to everything stays where it is.
    past(app, "tea", [("绿茶用多少度的水泡", "八十度。")], ago=60)
    model = Scripted([Completion("NEW", [], "stop"), Completion("可以用 pandas 读取。", [], "stop")])
    job = await reply(app, model, "Python 怎么读取 Excel 文件", "tea", stream=True)
    assert job["payload"]["session_id"] == "tea"


# ---- the portrait ----

@pytest.mark.asyncio
async def test_the_portrait_is_written_from_memories_and_kept_in_mind(app):
    worker = app.state.worker
    memory = worker.memory
    memory.save("家人", "用户的女儿小满六岁，对花生过敏")
    memory.save("城市", "用户住在杭州")
    model = Scripted([Completion("- 住在杭州\n- 女儿小满六岁，对花生过敏\n- 在做一款个人助理", [], "stop"), Completion("好的。", [], "stop"), Completion("好的。", [], "stop")])
    worker.llm.stream = model.stream
    assert await memory.draw("miavo:x") is False and not model.calls  # too little to draw from
    memory.save("工作", "用户在做一款个人助理")
    assert await memory.draw("miavo:x") is True
    assert "Portrait so far:\n(none yet)" in model.calls[0]["messages"][-1]["content"] and "花生过敏" in model.calls[0]["messages"][-1]["content"]
    assert memory.portrait_file.read_text(encoding="utf-8").startswith("- 住在杭州") and memory.portrait_file.parent.name == "vault"
    assert await memory.draw("miavo:x") is False  # nothing new since
    app.state.store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('s1','新对话',0,0)")
    await reply(app, model, "今天天气不错", "s1")
    assert "女儿小满六岁" in model.calls[1]["messages"][0]["content"] and "What you know about me in general" in model.calls[1]["messages"][0]["content"]
    app.state.store.execute("INSERT INTO sessions(id,title,created_at,updated_at,incognito) VALUES('s2','新对话',0,0,1)")
    await reply(app, model, "今天天气不错", "s2")
    assert "小满" not in json.dumps(model.calls[2]["messages"], ensure_ascii=False)


@pytest.mark.asyncio
async def test_the_users_own_edit_of_the_portrait_is_what_the_next_one_starts_from(app):
    memory = app.state.worker.memory
    for n, fact in enumerate(["用户住在杭州", "用户养了一只猫叫年糕", "用户每周三晚上健身"]):
        memory.save(f"事实{n}", fact)
    memory.portrait_file.parent.mkdir(parents=True, exist_ok=True)
    memory.portrait_file.write_text("我自己写的：喜欢简短的回答。", encoding="utf-8")
    model = Scripted([Completion("- 喜欢简短的回答\n- 住在杭州", [], "stop")])
    app.state.worker.llm.stream = model.stream
    assert await memory.draw("miavo:x", now=time.time() + 86400 * 2) is False  # written after the newest memory: nothing to add
    memory.save("新的", "用户下个月搬去上海")
    assert await memory.draw("miavo:x", now=time.time() + 86400 * 2) is True
    assert "我自己写的：喜欢简短的回答。" in model.calls[0]["messages"][-1]["content"]


# ---- a quiet conversation opened by Jurvis ----

@pytest.mark.asyncio
async def test_a_quiet_conversation_opened_by_jurvis_is_where_the_next_message_goes(client, app):
    first = say(client, "最近压力很大，睡不着").json()
    args = {"kind": "companion", "title": "最近的压力", "brief": "用户说最近压力大、睡不着。", "opening": "听起来这段时间很不容易。想从哪儿说起？"}
    model = Scripted([Completion("", [{"id": "c1", "name": "open_session", "arguments": json.dumps(args, ensure_ascii=False)}], "tool_calls"),
                      Completion("我们慢慢聊。", [], "stop")])
    app.state.worker.agent.runtime = model
    await app.state.worker.agent.run(first)
    app.state.store.update_job(first["id"], status="succeeded")
    quiet = app.state.store.one("SELECT id FROM sessions WHERE persona='companion'")["id"]
    assert say(client, "从工作说起吧").json()["payload"]["session_id"] == quiet
    said = [m["content"] for m in client.get("/api/stream").json()["messages"] if m["content"]]
    assert said == ["最近压力很大，睡不着", "我们慢慢聊。", "听起来这段时间很不容易。想从哪儿说起？", "从工作说起吧"]


# ---- the record: what the context did not bring back, the assistant can look up word for word ----

def test_the_assistant_can_search_the_record_and_read_a_passage_word_for_word(app):
    tools = app.state.worker.tools
    long = "第一周每次三公里，" + "慢慢加量。" * 1200
    past(app, "run", [("帮我订一个八周的跑步计划", long), ("第三周呢", "第三周每次五公里，周日加一次长距离。"), ("谢谢", "不客气。")], ago=40 * 86400)
    past(app, "trip", [("京都行程怎么排", "清水寺放在最后一天傍晚。")], ago=20 * 86400)
    past(app, "secret", [("跑步计划里别告诉别人的那件事", "好的，不会记。")], ago=10 * 86400, incognito=1)
    found = tools.invoke("search_conversations", {"query": "跑步计划 八周"}).result["messages"]
    assert found[0]["message_id"] == "runu0" and found[0]["who"] == "user" and "八周的跑步计划" in found[0]["snippet"]
    assert not any(m["message_id"].startswith(("secret", "trip")) for m in found)  # a private stretch is never searched
    # A question in one piece, as Chinese is written: its character pairs still find the talk.
    assert tools.invoke("search_conversations", {"query": "上次说的清水寺怎么安排"}).result["messages"][0]["message_id"] == "tripa0"
    assert tools.invoke("search_conversations", {"query": "潜水"}).result["messages"] == []
    passage = tools.invoke("read_conversation", {"message_id": "runa1", "before": 1, "after": 1}).result
    assert [m["message_id"] for m in passage["messages"]] == ["runu1", "runa1", "runu2"]
    assert passage["messages"][1]["text"] == "第三周每次五公里，周日加一次长距离。" and passage["earlier"] and passage["later"]
    whole = tools.invoke("read_conversation", {"message_id": "runa0", "before": 0, "after": 0}).result
    assert whole["messages"][0]["text"].startswith("第一周每次三公里") and "more characters not shown" in whole["messages"][0]["text"]
    assert len(json.dumps(tools.invoke("read_conversation", {"message_id": "runa0", "before": 6, "after": 6}).result, ensure_ascii=False)) < 8000
    for missing in ("secretu0", "nope"):
        assert not tools.invoke("read_conversation", {"message_id": missing}).ok


@pytest.mark.asyncio
async def test_the_record_is_offered_where_memory_is_and_not_in_a_private_stretch(app):
    past(app, "now", [("你好", "你好。")], ago=60)
    past(app, "quiet", [("这段别记", "好的。")], ago=60, incognito=1)
    offered = {}
    for ident in ("now", "quiet"):
        model = Scripted([Completion("好的。", [], "stop")])
        await reply(app, model, "我们之前定的续租租金是多少来着", ident)
        offered[ident] = ({t["function"]["name"] for t in model.calls[-1]["tools"]}, model.calls[-1]["messages"][0]["content"])
    assert {"search_conversations", "read_conversation"} <= offered["now"][0] and "search_conversations with a few distinctive words" in offered["now"][1]
    assert not {"search_conversations", "read_conversation"} & offered["quiet"][0] and "search_conversations" not in offered["quiet"][1]
    app.state.worker.prefs.update({"memory": {"enabled": False}})  # with memory off it does not go through the past either
    model = Scripted([Completion("好的。", [], "stop")])
    await reply(app, model, "我们之前定的续租租金是多少来着", "now")
    assert "search_conversations" not in {t["function"]["name"] for t in model.calls[-1]["tools"]}
