"""Questions that wait for the user: kept until answered, answered from anywhere, learned from, and the way back to what one was doing."""
import json
import time

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.asks import KEEP_DAYS
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


def ask(question, *options):
    return [{"id": "c1", "name": "ask_user", "arguments": json.dumps({"question": question, "options": list(options)}, ensure_ascii=False)}]


def say(client, text, n=[0], **more):
    n[0] += 1
    response = client.post("/api/stream/messages", json={"content": text, "request_id": f"ask-request-{n[0]:04d}", **more})
    assert response.status_code == 200, response.text
    return response.json()


async def answer(app, job, *turns):
    """Runs the reply to a queued message with a scripted model, the way the worker would."""
    worker, store = app.state.worker, app.state.store
    model = Scripted(turns)
    worker.agent.runtime = model
    worker.llm.stream = model.stream
    await worker.agent.run(job)
    store.update_job(job["id"], status="succeeded")
    worker.after_chat(job["payload"]["session_id"])
    return model


def waiting(client):
    return client.get("/api/state").json()["questions"]


def quiet(app, seconds=GAP + 60):
    """Lets time pass: everything said so far moves into the past."""
    store = app.state.store
    store.execute("UPDATE messages SET created_at=created_at-?", (seconds,))
    store.execute("UPDATE sessions SET created_at=created_at-?,updated_at=updated_at-?,focused_at=focused_at-?", (seconds, seconds, seconds))
    store.execute("UPDATE jobs SET updated_at=updated_at-?,created_at=created_at-?", (seconds, seconds))
    store.execute("UPDATE questions SET asked_at=asked_at-?", (seconds,))


BRIEF = ("好，我帮你长期盯着 L2，记下了。", ask("你想我按什么节奏跟？", "每周发我一份简报", "每月做一次深度复盘", "先不做，帮我留意着"))


@pytest.mark.asyncio
async def test_a_question_waits_until_it_is_answered_by_a_tap_or_in_the_users_own_words(client, app):
    first = say(client, "想长期跟踪 L2 的进展")
    await answer(app, first, Completion(*BRIEF, "tool_calls"))
    [q] = waiting(client)
    assert q["question"] == "你想我按什么节奏跟？" and q["options"][0] == "每周发我一份简报" and q["message_id"] == first["payload"]["message_id"]
    # A question of their own is not an answer: the first one keeps waiting.
    aside = say(client, "Optimistic rollup 是什么意思？")
    await answer(app, aside, Completion("先假定交易有效，有人质疑时再验证。我们刚才在定跟踪的节奏。", [], "stop"))
    assert [w["id"] for w in waiting(client)] == [q["id"]]
    thanks = say(client, "谢谢你")  # nor is a word of thanks
    await answer(app, thanks, Completion("不客气。", [], "stop"))
    assert [w["id"] for w in waiting(client)] == [q["id"]]
    tapped = say(client, "每周发我一份简报")
    assert waiting(client) == []
    row = app.state.store.one("SELECT status,answer FROM questions WHERE id=?", (q["id"],))
    assert row == {"status": "answered", "answer": "每周发我一份简报"}
    # Other things were said in between, so the model is told which question this answers.
    model = await answer(app, tapped, Completion("好，每周一早上发你。", [], "stop"))
    assert "my answer to a question you asked me earlier: 你想我按什么节奏跟？" in model.calls[0]["messages"][-1]["content"]


@pytest.mark.asyncio
async def test_a_short_reply_in_ones_own_words_answers_and_a_newer_question_takes_the_place_of_an_older_one(client, app):
    first = say(client, "想长期跟踪 L2 的进展")
    await answer(app, first, Completion(*BRIEF, "tool_calls"))
    typed = say(client, "两周一次吧")
    model = await answer(app, typed, Completion("好，两周一次。", ask("周几发你？", "周一早上", "周五傍晚"), "tool_calls"))
    assert "my answer to a question" not in model.calls[0]["messages"][-1]["content"]  # it follows the question directly: no need to say so
    rows = {r["question"]: r for r in app.state.store.all("SELECT question,status,answer FROM questions")}
    assert rows["你想我按什么节奏跟？"]["answer"] == "两周一次吧" and rows["周几发你？"]["status"] == "pending"
    again = say(client, "等等，为什么不是每周？")
    await answer(app, again, Completion("你刚才说两周一次。", ask("要改成每周吗？", "改成每周", "保持两周"), "tool_calls"))
    assert [w["question"] for w in waiting(client)] == ["要改成每周吗？"]
    assert app.state.store.one("SELECT status FROM questions WHERE question='周几发你？'")["status"] == "passed"


@pytest.mark.asyncio
async def test_an_answer_picked_later_goes_back_to_where_the_question_was_asked(client, app):
    first = say(client, "想长期跟踪 L2 的进展")
    await answer(app, first, Completion(*BRIEF, "tool_calls"))
    asked_in = first["payload"]["session_id"]
    quiet(app)
    other = say(client, "明天的会议几点")
    model = await answer(app, other, Completion("上午十点。", [], "stop"))
    assert other["payload"]["session_id"] != asked_in
    note = model.calls[0]["messages"][-1]["content"]
    assert "Questions you asked me earlier that I have not answered" in note and "每月做一次深度复盘" in note  # it knows the question is still open
    [q] = waiting(client)
    picked = say(client, "每月做一次深度复盘", answer_to=q["id"])
    assert picked["payload"]["session_id"] == asked_in and waiting(client) == []
    await answer(app, picked, Completion("好，每月一次。", [], "stop"))
    assert say(client, "第一期什么时候出")["payload"]["session_id"] == asked_in  # and that is where they are now


@pytest.mark.asyncio
async def test_a_question_can_be_dropped_stops_waiting_after_a_week_and_is_not_kept_off_the_record(client, app):
    first = say(client, "想长期跟踪 L2 的进展")
    await answer(app, first, Completion(*BRIEF, "tool_calls"))
    [q] = waiting(client)
    assert client.delete("/api/questions/" + q["id"]).status_code == 200 and waiting(client) == []
    assert client.delete("/api/questions/" + q["id"]).status_code == 404
    second = say(client, "再问我一次", fresh=True)
    await answer(app, second, Completion(*BRIEF, "tool_calls"))
    assert len(waiting(client)) == 1
    quiet(app, KEEP_DAYS * 86400 + 60)
    hidden = say(client, "这段别记", incognito=True)
    await answer(app, hidden, Completion("好。", ask("要我现在就忘掉吗？", "忘掉", "先留着"), "tool_calls"))
    assert waiting(client) == []  # the old one ran out; the one asked off the record was never kept
    assert {r["status"] for r in app.state.store.all("SELECT status FROM questions")} == {"dismissed", "passed"}


# ---- learning from what was chosen ----

@pytest.mark.asyncio
async def test_a_choice_made_the_same_way_twice_is_taken_the_third_time_without_asking(client, app):
    turn = ("周报草稿写好了。", ask("周报发给谁？", "只发给我", "发给全组"))
    for _ in range(2):
        job = say(client, "帮我写这周的周报", fresh=True)
        await answer(app, job, Completion(*turn, "tool_calls"))
        picked = say(client, "只发给我")
        await answer(app, picked, Completion("已发给你。", [], "stop"))
    job = say(client, "帮我写这周的周报", fresh=True)
    model = await answer(app, job, Completion(*turn, "tool_calls"), Completion("周报写好了，按以往的做法只发给了你。", [], "stop"))
    message = app.state.store.one("SELECT content,metadata FROM messages WHERE id=?", (job["payload"]["message_id"],))
    assert "ask" not in message["metadata"] and "按以往的做法" in message["content"] and waiting(client) == []
    refusal = json.dumps(model.calls[1]["messages"], ensure_ascii=False)
    assert "2 times before" in refusal and "只发给我" in refusal


@pytest.mark.asyncio
async def test_a_choice_that_went_both_ways_or_is_not_on_offer_is_asked_again(client, app):
    turn = ("周报草稿写好了。", ask("周报发给谁？", "只发给我", "发给全组"))
    for choice in ("只发给我", "发给全组"):
        job = say(client, "帮我写这周的周报", fresh=True)
        await answer(app, job, Completion(*turn, "tool_calls"))
        await answer(app, say(client, choice), Completion("好。", [], "stop"))
    job = say(client, "帮我写这周的周报", fresh=True)
    await answer(app, job, Completion(*turn, "tool_calls"))
    assert [w["question"] for w in waiting(client)] == ["周报发给谁？"]


# ---- stepping aside and coming back ----

def past(app, ident, exchanges, ago, **columns):
    store, at = app.state.store, time.time() - ago
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at,focused_at) VALUES(?,?,?,?,?)", (ident, exchanges[0][0][:40], at, at + len(exchanges), at + len(exchanges)))
    for key, value in columns.items():
        store.execute(f"UPDATE sessions SET {key}=? WHERE id=?", (value, ident))
    for n, (asked, answered) in enumerate(exchanges):
        store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES(?,?,'user',?,'complete',?)", (f"{ident}u{n}", ident, asked, at + n))
        store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES(?,?,'assistant',?,'complete',?)", (f"{ident}a{n}", ident, answered, at + n + .5))


TRIP = [("帮我规划十月去京都的行程", "住四条河原町。"), ("第二天去哪", "岚山。"), ("清水寺呢", "最后一天傍晚。")]


@pytest.mark.asyncio
async def test_after_stepping_aside_a_message_about_the_main_subject_goes_back_to_it(client, app):
    worker = app.state.worker
    worker.prefs.set_provider("miavo", api_key="sk-maas-test")
    worker.prefs.update({"chat_model": "miavo:claude-haiku-4-5", "tool_model": "miavo:claude-haiku-4-5"})
    past(app, "trip", TRIP, ago=120)
    aside = say(client, "Python 怎么读取 Excel 文件")
    assert aside["payload"]["session_id"] == "trip"  # it arrives in the stretch the user is in
    await answer(app, aside, Completion("NEW", [], "stop"), Completion("用 pandas 的 read_excel。", [], "stop"))
    moved = app.state.store.get_job(aside["id"])["payload"]["session_id"]
    assert moved != "trip" and app.state.store.one("SELECT parent_id FROM sessions WHERE id=?", (moved,))["parent_id"] == "trip"
    back = say(client, "岚山那天要不要早点出发")
    assert back["payload"]["session_id"] == moved
    model = await answer(app, back, Completion("建议八点前到竹林。", [], "stop"))
    assert app.state.store.get_job(back["id"])["payload"]["session_id"] == "trip" and len(model.calls) == 1  # no model was asked: the words say it
    sent = [m["content"] for m in model.calls[0]["messages"] if m["role"] in ("user", "assistant") and isinstance(m["content"], str)]
    assert "岚山。" in sent and not any("pandas" in text for text in sent[:-1])  # the trip is replayed again, the aside is not
    assert say(client, "那清水寺几点关门")["payload"]["session_id"] == "trip"


@pytest.mark.asyncio
async def test_a_message_that_goes_on_with_the_aside_stays_in_it(client, app):
    worker = app.state.worker
    worker.prefs.set_provider("miavo", api_key="sk-maas-test")
    worker.prefs.update({"chat_model": "miavo:claude-haiku-4-5", "tool_model": "miavo:claude-haiku-4-5"})
    past(app, "trip", TRIP, ago=120)
    aside = say(client, "Python 怎么读取 Excel 文件")
    await answer(app, aside, Completion("NEW", [], "stop"), Completion("用 pandas 的 read_excel。", [], "stop"))
    moved = app.state.store.get_job(aside["id"])["payload"]["session_id"]
    more = say(client, "pandas 读取 Excel 需要装 openpyxl 吗")
    await answer(app, more, Completion("需要。", [], "stop"))
    assert app.state.store.get_job(more["id"])["payload"]["session_id"] == moved


# ---- the pace ----

@pytest.mark.asyncio
async def test_the_model_is_told_how_to_pace_and_when_to_ask(client, app):
    job = say(client, "想长期跟踪 L2 的进展")
    model = await answer(app, job, Completion("好。", [], "stop"))
    system = model.calls[0]["messages"][0]["content"]
    for rule in ("goes one step at a time", "Ask only when all three hold", "Put the option you would pick first", "a way out in their voice", "say in one line where you two were"):
        assert rule in system
