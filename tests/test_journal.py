"""The work record the harness writes, what the model says to itself, and the character that says it."""
import json
import pytest
from fastapi.testclient import TestClient
from PIL import Image, ImageDraw
from engine.app import create_app
from engine.avatar import Avatar, cut
from engine.config import Settings
from engine.runtime import Completion
from test_agent import ScriptedModel, run


def call(tool, **args):
    return {"id": "call_" + tool, "name": tool, "arguments": json.dumps(args)}


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path), run_worker=False)


def rows(app, where="1"):
    return app.state.store.all(f"SELECT * FROM journal WHERE {where} ORDER BY id")


@pytest.mark.asyncio
async def test_every_tool_call_and_what_was_said_before_it_is_recorded_by_the_harness(app):
    model = ScriptedModel([
        Completion("我先算一下。", [call("calculate", expression="2+3")], "tool_calls"),
        Completion("", [call("read_file", name="没有这个.md")], "tool_calls"),
        Completion("是 5。", [], "stop"),
    ])
    _, message = await run(app, model, "算一下 2+3")
    said, ok, failed = rows(app)
    assert (said["stream"], said["kind"], said["text"]) == ("mind", "say", "我先算一下。")
    assert (ok["stream"], ok["kind"], ok["name"], ok["status"], ok["effect"], ok["owner"]) == ("work", "tool", "calculate", "ok", "read", "")
    assert ok["message_id"] == message["id"] and ok["session_id"] == "s1" and ok["origin"] == "chat"
    assert ok["extras"]["arguments"] == {"expression": "2+3"}
    # a file that is not there was the model's guess, not a fault of the tool
    assert (failed["status"], failed["owner"]) == ("error", "model")
    assert "是 5" not in json.dumps([r["text"] for r in rows(app)], ensure_ascii=False)  # the answer itself is not self-talk


@pytest.mark.asyncio
async def test_a_correction_by_the_harness_is_recorded_as_the_models_to_fix(app):
    model = ScriptedModel([Completion("", [], "stop"), Completion("你好。", [], "stop")])
    await run(app, model, "你好")
    (row,) = rows(app)
    assert (row["stream"], row["kind"], row["name"], row["owner"]) == ("work", "harness", "empty", "model")
    improve = app.state.worker.tools.journal.improvements()
    assert [i["name"] for i in improve["model"]] == ["empty"] and improve["user"] == []


@pytest.mark.asyncio
async def test_the_same_call_sent_twice_is_one_tool_line_and_one_correction(app):
    model = ScriptedModel([
        Completion("", [call("calculate", expression="1+1")], "tool_calls"),
        Completion("", [call("calculate", expression="1+1")], "tool_calls"),
        Completion("2。", [], "stop"),
    ])
    await run(app, model, "1+1")
    assert [(r["kind"], r["name"]) for r in rows(app)] == [("tool", "calculate"), ("harness", "repeat")]


@pytest.mark.asyncio
async def test_a_jot_in_goal_mode_is_kept_with_its_owner_and_not_as_a_tool_line(app):
    model = ScriptedModel([
        Completion("", [call("jot", text="他没说预算，我先按人均两百猜了", kind="friction", about="user")], "tool_calls"),
        Completion("", [call("finish_goal", status="blocked", summary="需要预算")], "tool_calls"),
    ])
    await run(app, model, "订个餐厅", mode="goal")
    assert "jot" in {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "jot" in model.calls[0]["messages"][0]["content"]
    (note,) = rows(app, "stream='mind'")
    assert (note["kind"], note["owner"], note["text"], note["origin"]) == ("friction", "user", "他没说预算，我先按人均两百猜了", "goal")
    assert not rows(app, "name='jot'")
    assert app.state.worker.tools.journal.mind()[0]["mood"] == "troubled"


@pytest.mark.asyncio
async def test_the_local_model_is_not_offered_jot_in_ordinary_conversation(app):
    model = ScriptedModel([Completion("你好。", [], "stop")])
    await run(app, model, "你好")
    assert "jot" not in {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert "with jot" not in model.calls[0]["messages"][0]["content"]


@pytest.mark.asyncio
async def test_nothing_from_a_private_conversation_is_written(app):
    store = app.state.store
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at,incognito) VALUES('s1','新对话',0,0,1)")
    model = ScriptedModel([Completion("我先算一下。", [call("calculate", expression="2+3")], "tool_calls"), Completion("5。", [], "stop")])
    await run(app, model, "算一下 2+3")
    assert rows(app) == []


@pytest.mark.asyncio
async def test_figures_that_stay_on_the_mac_are_not_copied_into_the_journal(app):
    model = ScriptedModel([Completion("", [call("log_expense", amount=35, category="餐饮", note="午饭")], "tool_calls"), Completion("记了。", [], "stop")])
    await run(app, model, "午饭 35")
    (row,) = rows(app)
    assert row["text"] == "记一笔" and row["extras"] == {"duration": row["extras"]["duration"]}  # no amount, no arguments
    assert "35" not in json.dumps(app.state.worker.tools.journal.digest(), ensure_ascii=False)


@pytest.mark.asyncio
async def test_a_finished_goal_is_looked_back_on_once(app):
    model = ScriptedModel([
        Completion("", [call("calculate", expression="1+1")], "tool_calls"),
        Completion("", [call("calculate", expression="2+2")], "tool_calls"),
        Completion("", [call("calculate", expression="3+3")], "tool_calls"),
        Completion("", [call("finish_goal", status="blocked", summary="缺数据")], "tool_calls"),
        Completion('[{"owner":"user","text":"他没给数据来源，我只能自己算"},{"owner":"nobody","text":"x"}]', [], "stop"),
    ])
    await run(app, model, "把这些加起来", mode="goal")
    assert model.calls[-1]["tools"] is None
    (note,) = rows(app, "stream='mind'")
    assert (note["kind"], note["owner"], note["text"]) == ("friction", "user", "他没给数据来源，我只能自己算")


@pytest.mark.asyncio
async def test_asked_about_its_work_it_answers_from_the_journal_and_does_no_other_work(app):
    store, client = app.state.store, TestClient(app)
    headers = {"Authorization": "Bearer " + app.state.security.local_token}
    await run(app, ScriptedModel([Completion("", [], "stop"), Completion("你好。", [], "stop")]), "你好")
    session = client.post("/api/journal/talk", headers=headers).json()
    assert session["persona"] == "inner" and client.post("/api/journal/talk", headers=headers).json()["id"] == session["id"]
    opening = store.one("SELECT content FROM messages WHERE session_id=?", (session["id"],))["content"]
    assert opening.startswith("我是小维")
    assert app.state.worker.stream.current()["id"] == session["id"]  # what is said next is said to it
    store.add_message(session["id"], "user", "你最近有什么困扰？")
    mid = store.add_message(session["id"], "assistant", "", "queued")
    job = store.create_job("chat", {"session_id": session["id"], "message_id": mid})
    model = ScriptedModel([Completion("有一次我交了白卷 [#1]。", [], "stop")])
    app.state.worker.agent.runtime = model
    await app.state.worker.agent.run(job)
    system = model.calls[0]["messages"][0]["content"]
    assert "#1 [" in system and "交了一份空白回复" in system and "小维" in system
    assert {t["function"]["name"] for t in model.calls[0]["tools"]} == {"read_journal", "remember"}
    assert len(rows(app)) == 1  # talking about the record adds nothing to it


@pytest.mark.asyncio
async def test_the_day_is_read_back_as_tasks_with_the_request_that_started_each(app):
    client = TestClient(app)
    headers = {"Authorization": "Bearer " + app.state.security.local_token}
    model = ScriptedModel([Completion("我先算一下。", [call("save_file", name="a.md", content="hi")], "tool_calls"), Completion("好了。", [], "stop")])
    await run(app, model, "写个文件")
    page = client.get("/api/journal", headers=headers).json()
    (task,) = page["tasks"]
    assert task["title"] == "写个文件" and task["acted"] == 1 and len(task["work"]) == 1 and len(task["mind"]) == 1
    assert page["totals"] == {"tasks": 1, "calls": 1, "acted": 1, "failed": 0, "corrected": 0} and page["days"] == [page["day"]]
    assert client.get("/api/journal?day=nope", headers=headers).status_code == 400
    notes = client.get("/api/journal/mind", headers=headers).json()["notes"]
    assert [n["text"] for n in notes] == ["我先算一下。"]
    assert client.get(f"/api/journal/mind?after={notes[0]['id']}", headers=headers).json()["notes"] == []


def picture(background, body):
    image = Image.new("RGB", (256, 256), background)
    ImageDraw.Draw(image).ellipse((70, 60, 190, 200), fill=body, outline=(40, 30, 20), width=4)
    return image


def test_the_background_is_cut_away_and_a_character_lost_in_it_is_noticed():
    out, kept, box = cut(picture((12, 236, 10), (255, 255, 255)))
    assert out.mode == "RGBA" and out.size == (512, 512) and .15 < kept < .35
    assert out.getpixel((2, 2))[3] == 0 and out.getpixel((256, 256)) == (255, 255, 255, 255)
    assert box[2] - box[0] == box[3] - box[1]
    _, lost, _ = cut(picture((255, 255, 255), (255, 255, 255)))
    assert lost < .04  # white on white: only the outline is left, which the avatar job treats as a failed drawing


class FakeMedia:
    """Stands in for the image model: every drawing is a white figure on the key colour."""
    def __init__(self, real):
        self.real, self.requests = real, []
        self.asset, self.record = real.asset, real.record

    async def run(self, kind, p, job_id, prompt=None, phase=None):
        self.requests.append(p)
        path = self.real.settings.data / "assets" / f"raw{len(self.requests)}.png"
        picture((255, 0, 255) if "magenta" in p["prompt"] else (10, 240, 12), (250, 250, 250)).save(path)
        return {"asset_ids": [self.real.record(path, "raw", "image/png", "image", job_id)]}


@pytest.mark.asyncio
async def test_a_character_is_drafted_picked_and_given_faces_from_the_pick(app):
    store, worker, client = app.state.store, app.state.worker, TestClient(app)
    headers = {"Authorization": "Bearer " + app.state.security.local_token}
    avatar = worker.avatar
    avatar.media = media = FakeMedia(worker.media)
    assert client.get("/api/avatar", headers=headers).json()["status"] == "none"
    assert client.post("/api/avatar/draft", json={}, headers=headers).status_code == 400
    state = client.post("/api/avatar/draft", json={"description": "一只戴眼镜的灰猫"}, headers=headers).json()
    assert state["status"] == "drafting"
    assert client.post("/api/avatar/draft", json={"description": "狗"}, headers=headers).status_code == 400  # one at a time
    job = store.get_job(state["job_id"])
    store.update_job(job["id"], status="running")
    await worker.handle(job)
    store.update_job(job["id"], status="succeeded")
    state = client.get("/api/avatar", headers=headers).json()
    assert state["status"] == "choosing" and len(state["candidates"]) == 3
    assert "green background" in media.requests[0]["prompt"] and "一只戴眼镜的灰猫" in media.requests[0]["prompt"]
    pick, others = state["candidates"][1], [state["candidates"][0], state["candidates"][2]]
    assert client.post("/api/avatar/choose", json={"asset_id": "nope"}, headers=headers).status_code == 400
    state = client.post("/api/avatar/choose", json={"asset_id": pick}, headers=headers).json()
    assert state["status"] == "making" and state["faces"] == {"calm": pick}  # usable at once, the faces follow
    assert not store.one("SELECT id FROM assets WHERE id=?", (others[0],))    # what was not picked is gone, with its uncut picture
    job = store.get_job(state["job_id"])
    store.update_job(job["id"], status="running")
    await worker.handle(job)
    store.update_job(job["id"], status="succeeded")
    state = client.get("/api/avatar", headers=headers).json()
    assert state["status"] == "ready" and set(state["faces"]) == {"calm", "thinking", "troubled", "happy", "guilty", "sleepy"}
    raw = store.one("SELECT metadata FROM assets WHERE id=?", (pick,))["metadata"]["raw"]
    assert all(r["reference_assets"] == [raw] for r in media.requests[3:])   # every face is drawn from the pick
    assert {a["kind"] for a in store.all("SELECT kind FROM assets")} == {"avatar", "avatar-raw"}
    assert client.put("/api/avatar", json={"name": "团子"}, headers=headers).json()["name"] == "团子"
    assert client.delete("/api/avatar", headers=headers).json()["status"] == "none"
    assert store.all("SELECT id FROM assets") == []


def test_a_green_character_is_drawn_on_another_key_colour():
    assert Avatar.key("一只绿色的小青蛙") == "magenta" and Avatar.key("一只橘猫") == "green"
