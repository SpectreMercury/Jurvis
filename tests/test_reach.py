"""What lets the assistant work while nobody is looking: push, standing permissions, the calendar and contacts, watches,
sending mail, what stays with the local model, the nightly copy, and the user's verdict on a reply."""
import asyncio
import json
import sqlite3
import time
from datetime import datetime, timedelta
from email import message_from_bytes
from types import SimpleNamespace

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.backup import Backup
from engine.config import Settings
from engine.permits import command_scope, page_scope
from engine.push import b64, point, sealed, unb64
from engine.runtime import Completion
from tests.test_agent import ScriptedModel, answer_when_awaiting, call, run
from tests.test_mail_checkin import FakeBox, letter


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path / "data"), run_worker=False)


def client_of(app):
    return TestClient(app, headers={"Authorization": "Bearer " + app.state.security.local_token})


def device(app, ident, seen_ago):
    app.state.store.execute("INSERT INTO devices(id,name,token_hash,created_at,last_seen) VALUES(?,?,?,?,?)", (ident, "手机 " + ident, "hash-" + ident, 0, time.time() - seen_ago))


class FakeHttp:
    """Stands in for the push services: records each request and answers with the status it was told to."""
    def __init__(self, log, status=201, **options):
        self.log, self.status, self.options = log, status, options

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **request):
        self.log.append({"url": url, **request, **self.options})
        return SimpleNamespace(status_code=self.status(url) if callable(self.status) else self.status, text="BadDeviceToken" if "gone" in url else "")


def browser():
    """A browser's side of a Web Push subscription: its key pair and secret, and what it would send the engine."""
    key, auth = ec.generate_private_key(ec.SECP256R1()), b"0123456789abcdef"
    return key, auth, {"endpoint": "https://push.example/send/abc", "keys": {"p256dh": b64(point(key)), "auth": b64(auth)}}


def opened(body, key, auth):
    """Decrypts an aes128gcm push body the way the browser does (RFC 8291)."""
    salt, size, theirs = body[:16], body[20], body[21:86]
    assert size == 65
    shared = key.exchange(ec.ECDH(), ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), theirs))
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + point(key) + theirs).derive(shared)
    cek = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    plain = AESGCM(cek).decrypt(nonce, body[86:], None)
    assert plain.endswith(b"\x02")
    return plain[:-1]


# ---- push ----

def test_a_push_body_opens_only_with_the_key_of_the_browser_it_was_sealed_for():
    key, auth, sub = browser()
    body = sealed("你好，推送".encode(), unb64(sub["keys"]["p256dh"]), auth)
    assert opened(body, key, auth).decode() == "你好，推送"
    with pytest.raises(Exception):
        opened(body, ec.generate_private_key(ec.SECP256R1()), auth)


@pytest.mark.asyncio
async def test_a_notice_is_shown_by_open_apps_and_pushed_only_to_closed_ones(app):
    push, sent = app.state.worker.push, []
    push.http = lambda **options: FakeHttp(sent, **options)
    client = client_of(app)
    device(app, "away", 3600)
    device(app, "here", 1)
    key, auth, sub = browser()
    assert push.subscribe("away", "web", sub) == {"ok": True}
    push.subscribe("here", "web", {**sub, "endpoint": "https://push.example/send/open"})
    with pytest.raises(ValueError):
        push.subscribe("away", "web", {"endpoint": "http://plain.example/x", "keys": sub["keys"]})

    assert push.notify("result", "L2 每月复盘", "本月复盘：总锁定价值小幅上升。", ident="m1", session_id="s1")
    assert push.notify("result", "L2 每月复盘", "again", ident="m1") is None  # told once
    assert [n["id"] for n in client.get("/api/state").json()["notices"]] == ["m1"]
    assert await push.flush() == 1 and await push.flush() == 0
    (request,) = sent
    assert request["url"] == sub["endpoint"] and request["headers"]["Content-Encoding"] == "aes128gcm"
    assert request["headers"]["Authorization"].startswith("vapid t=") and f"k={push.status()['vapid_key']}" in request["headers"]["Authorization"]
    told = json.loads(opened(request["content"], key, auth))
    assert told == {"id": "m1", "kind": "result", "title": "L2 每月复盘", "body": "本月复盘：总锁定价值小幅上升。", "session_id": "s1"}

    # a check-in is pushed too, but the open apps already get it as a message; a settled notice is gone
    push.notify("checkin", "Jurvis", "这几天好像挺累的？", ident="m2")
    push.notify("approval", "等你确认", "rm -rf build", ident="c1", message_id="m3", call_id="c1")
    push.settle("c1")
    assert [n["id"] for n in push.recent()] == ["m1"] and await push.flush() == 1

    # an address the push service says is gone is forgotten
    push.http = lambda **options: FakeHttp(sent, status=410, **options)
    push.notify("result", "x", "y", ident="m4")
    assert await push.flush() == 0 and [t["device_id"] for t in push.status()["targets"]] == ["here"]


@pytest.mark.asyncio
async def test_the_iphone_app_is_reached_through_the_users_own_apple_key(app):
    push, sent = app.state.worker.push, []
    push.http = lambda **options: FakeHttp(sent, status=lambda url: 400 if "gone" in url else 200, **options)
    client = client_of(app)
    device(app, "phone", 3600)
    token = "ab" * 32
    assert client.post("/api/push/subscribe", json={"kind": "apns", "token": token, "topic": "app.jurvis.mobile", "sandbox": True}).status_code == 200
    push.store.execute("UPDATE push_targets SET device_id='phone'")
    push.subscribe("phone", "apns", {"token": "cd" * 32 + "gone"[:0], "topic": "app.jurvis.mobile"})
    push.store.execute("UPDATE push_targets SET address=? WHERE address=?", ("gone" + "cd" * 30, "cd" * 32))

    push.notify("approval", "等你确认 · 运行命令", "brew install ffmpeg", ident="call9", session_id="s1", message_id="m9", call_id="call9")
    assert await push.flush() == 0  # no key yet: nothing can be sent, and the reason is kept
    assert "Apple 推送密钥" in push.store.one("SELECT error FROM push_targets WHERE address=?", (token,))["error"]

    assert client.put("/api/push/apns", json={"key": "not a key", "key_id": "ABC123", "team_id": "TEAM01"}).status_code == 400
    pem = ec.generate_private_key(ec.SECP256R1()).private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
    status = client.put("/api/push/apns", json={"key": pem, "key_id": "abc123", "team_id": "team01"}).json()
    assert status["apns"] == {"key_id": "ABC123", "team_id": "TEAM01"} and pem not in json.dumps(status)
    push.notify("approval", "等你确认 · 运行命令", "brew install ffmpeg", ident="call10", session_id="s1", message_id="m9", call_id="call10")
    assert await push.flush() == 1
    ok = next(r for r in sent if token in r["url"])
    assert ok["url"] == f"https://api.sandbox.push.apple.com/3/device/{token}" and ok["http2"] is True
    assert ok["headers"]["apns-topic"] == "app.jurvis.mobile" and ok["headers"]["authorization"].startswith("bearer ")
    assert ok["json"]["aps"]["alert"] == {"title": "等你确认 · 运行命令", "body": "brew install ffmpeg"} and ok["json"]["aps"]["category"] == "approval"
    assert ok["json"]["message_id"] == "m9" and ok["json"]["call_id"] == "call10"
    assert [t["kind"] for t in push.status()["targets"]] == ["apns"]  # the token Apple refused is forgotten
    assert client.post("/api/push/test").status_code == 400  # this Mac itself has nowhere to be pushed to


# ---- standing permissions ----

def test_only_a_plain_command_is_a_kind_that_can_be_allowed_for_good():
    assert command_scope("git status --short")[0] == "cmd:git status"
    assert command_scope("npm test -- --watch=false")[0] == "cmd:npm test"
    assert command_scope("ls -la /tmp")[0] == "cmd:ls" and command_scope("CI=1 pytest -q tests")[0] == "cmd:pytest"
    for script in ("cat notes.md | sh", "echo a; echo b", "python run.py", "./deploy.sh", "curl https://x.example", "echo $(whoami)", "echo 'unclosed"):
        assert command_scope(script) is None
    assert page_scope("https://www.tabelog.com/tokyo/A1301/")[0] == "site:www.tabelog.com" and page_scope("") is None


@pytest.mark.asyncio
async def test_a_kind_of_command_allowed_for_good_is_not_asked_about_again_until_taken_back(app, tmp_path):
    client, push = client_of(app), app.state.worker.push
    script = lambda text: ScriptedModel([Completion("", [call("run_command", command=text, reason="测试", cwd=str(tmp_path))], "tool_calls"), Completion("好了。", [], "stop")])
    (_, message), shown = await asyncio.gather(run(app, script("echo one"), "跑一下"), answer_when_awaiting(app, "forever"))
    assert shown["permit"] == "以后运行 echo … 都不用问"
    entry = message["metadata"]["steps"][0]["calls"][0]
    assert entry["status"] == "ok" and "one" in entry["console"] and "permit" not in entry
    assert push.recent() == []  # the notice that asked for a yes went away with the answer
    (permit,) = client.get("/api/permits").json()["permits"]
    assert (permit["tool"], permit["scope"]) == ("run_command", "cmd:echo")

    _, message = await asyncio.wait_for(run(app, script("echo two"), "再跑一下"), 5)  # nobody answers: it did not ask
    assert "two" in message["metadata"]["steps"][0]["calls"][0]["console"]
    assert client.get("/api/permits").json()["permits"][0]["uses"] == 1

    # a risky or chained command asks whatever was allowed, and offers no "from now on"
    (_, message), shown = await asyncio.gather(run(app, script("echo x; rm -rf nothing-here"), "清理"), answer_when_awaiting(app, "deny"))
    assert "permit" not in shown and message["metadata"]["steps"][0]["calls"][0]["status"] == "error"

    assert client.delete("/api/permits/" + permit["id"]).json()["permits"] == [] and client.delete("/api/permits/" + permit["id"]).status_code == 404
    (_, _), shown = await asyncio.gather(run(app, script("echo three"), "又跑一下"), answer_when_awaiting(app, "run"))
    assert shown["permit"]


@pytest.mark.asyncio
async def test_waiting_for_a_yes_is_announced_and_one_website_can_be_allowed_for_good(app):
    worker = app.state.worker
    clicked = []

    class Browser:
        def available(self):
            return True

        async def click(self, target):
            clicked.append(target)
            return {"url": "https://www.tablecheck.com/shops/x/reserve", "clicked": target}
    worker.tools.browser = Browser()
    worker.tools.page = {"url": "https://www.tablecheck.com/shops/x/reserve", "elements": {"e3": {"text": "次へ", "role": "button"}}}
    script = lambda: ScriptedModel([Completion("", [call("browser_click", target="e3")], "tool_calls"), Completion("点了。", [], "stop")])

    async def answer(decision):
        entry = await answer_when_awaiting(app, decision)
        return entry, worker.push.store.all("SELECT * FROM notices")
    (_, message), (shown, notices) = await asyncio.gather(run(app, script(), "帮我点下一步"), answer("forever"))
    assert shown["permit"] == "以后在 www.tablecheck.com 上点击和输入都不用问" and "次へ" in shown["command"]
    assert [(n["kind"], n["extras"]["call_id"]) for n in notices] == [("approval", "call_browser_click")] and "次へ" in notices[0]["body"]
    await asyncio.wait_for(run(app, script(), "再点一次"), 5)
    assert clicked == ["e3", "e3"]
    worker.tools.page["url"] = "https://other.example/pay"  # another site is another kind
    (_, _), (shown, _) = await asyncio.gather(run(app, script(), "这个也点"), answer("deny"))
    assert shown["permit"] == "以后在 other.example 上点击和输入都不用问" and clicked == ["e3", "e3"]


# ---- calendar, reminders, contacts ----

def at(day_offset, hour, minute=0):
    return (datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0) + timedelta(days=day_offset)).timestamp()


def share_everything(app):
    soon = time.time() + 20 * 60
    return client_of(app).post("/api/personal/sync", json={
        "events": [{"id": "e1", "title": "和田中吃饭", "start": at(1, 19), "end": at(1, 21), "place": "銀座 すし匠", "calendar": "个人"},
                   {"id": "e2", "title": "季度复盘", "start": soon, "end": soon + 3600, "calendar": "工作", "notes": "带上预算表"},
                   {"id": "e3", "title": "妈妈生日", "start": at(3, 0), "end": at(4, 0), "all_day": True},
                   {"id": "", "title": "没有 id 的不要"}],
        "reminders": [{"id": "r1", "title": "交房租", "due": at(-1, 9), "list": "家里"}, {"id": "r2", "title": "买牛奶", "done": True}],
        "contacts": [{"id": "c1", "name": "田中 健", "org": "Sushi Sho", "phones": ["+81 3-1234-5678"], "emails": ["tanaka@example.jp"]},
                     {"id": "c2", "name": "李雷", "phones": ["138 0000 1111"]}, {"id": "c3", "name": "", "org": ""}]}).json()


def test_a_copy_from_the_users_devices_can_be_read_by_day_and_one_person_at_a_time(app):
    personal = app.state.worker.tools.personal
    assert not personal.open("calendar", False)
    result = share_everything(app)
    assert result["kept"] == {"calendar": 3, "reminders": 2, "contacts": 2}
    assert personal.open("calendar", False) and not personal.open("calendar", True)  # the local model only, until the user says otherwise

    week = personal.events(days=7)
    assert [e["title"] for e in week["events"]] == ["季度复盘", "和田中吃饭", "妈妈生日"] and "全天" in week["events"][2]["when"] and "note" not in week
    assert [e["title"] for e in personal.events(days=7, query="田中")["events"]] == ["和田中吃饭"]
    assert [e["title"] for e in personal.upcoming()] == ["季度复盘"] or [e["title"] for e in personal.upcoming()] == ["季度复盘", "和田中吃饭"]
    (rent,) = personal.reminders()["reminders"]
    assert rent["title"] == "交房租" and rent["overdue"] is True and len(personal.reminders(include_done=True)["reminders"]) == 2

    assert personal.contact("田中")["matches"] == [{"name": "田中 健", "org": "Sushi Sho", "phones": ["+81 3-1234-5678"], "emails": ["tanaka@example.jp"]}]
    assert personal.contact("sushi sho")["matches"][0]["name"] == "田中 健" and personal.contact("0000 1111")["matches"][0]["name"] == "李雷"
    assert personal.contact("王")["matches"] == []
    with pytest.raises(ValueError):
        personal.contact("a")  # never the whole book

    # a later copy replaces the earlier one; a kind the device did not send is left as it was
    client_of(app).post("/api/personal/sync", json={"events": []})
    assert personal.events(days=30)["events"] == [] and len(personal.reminders()["reminders"]) == 1
    assert client_of(app).patch("/api/personal/contacts", json={"cloud": True}).json()["kinds"][2]["cloud"] is True and personal.open("contacts", True)
    assert client_of(app).delete("/api/personal/contacts").json()["kinds"][2]["count"] == 0 and not personal.open("contacts", False)


def test_new_events_wait_in_an_outbox_for_the_first_device_that_claims_them(app):
    personal, client = app.state.worker.tools.personal, client_of(app)
    share_everything(app)
    day = datetime.fromtimestamp(at(1, 19))
    made = app.state.worker.tools.execute("add_event", {"title": "牙医", "start": f"{day:%Y-%m-%d}T19:30", "minutes": 45, "place": "诊所"})
    assert made["overlaps"][0]["title"] == "和田中吃饭" and "overlaps" in made["note"] and made["card"]["subtitle"].startswith("日程 · ")
    app.state.worker.tools.execute("add_reminder", {"title": "带医保卡"})
    with pytest.raises(ValueError):
        personal.add_event("x", "明天晚上")
    waiting = client.get("/api/state").json()["outbox"]
    assert [(w["kind"], w["title"]) for w in waiting] == [("event", "牙医"), ("reminder", "带医保卡")] and waiting[0]["minutes"] == 45
    assert client.post(f"/api/personal/outbox/{waiting[0]['id']}/claim").json()["title"] == "牙医"
    assert client.post(f"/api/personal/outbox/{waiting[0]['id']}/claim").status_code == 409  # another device was first
    assert [w["title"] for w in client.get("/api/state").json()["outbox"]] == ["带医保卡"]
    client.post(f"/api/personal/outbox/{waiting[0]['id']}/done", json={"ok": False})  # it had no calendar access: back for another device
    assert len(client.get("/api/state").json()["outbox"]) == 2
    client.post(f"/api/personal/outbox/{waiting[0]['id']}/claim")
    client.post(f"/api/personal/outbox/{waiting[0]['id']}/done", json={"ok": True})
    assert [w["title"] for w in client.get("/api/state").json()["outbox"]] == ["带医保卡"]


def cloud_model(app, turns):
    """The conversation is answered by a cloud model that plays back these turns; returns what it was sent."""
    worker, sent = app.state.worker, []
    turns = list(turns)

    async def stream(messages, *, model=None, tools=None, on_text=None, **options):
        sent.append({"messages": messages, "tools": [t["function"]["name"] for t in tools or []]})
        turn = turns.pop(0) if turns else Completion("好的。", [], "stop")
        if on_text and turn.text:
            await on_text(turn.text)
        return turn
    worker.agent.llm = SimpleNamespace(chat_model=lambda m=None: "miavo:x", is_local=lambda m=None: m in (None, "local"), searches=lambda m=None: False,
                                       stream=stream, prefs=worker.prefs)
    if not app.state.store.one("SELECT id FROM sessions WHERE id='s1'"):
        app.state.store.execute("INSERT INTO sessions(id,title,created_at,updated_at,model) VALUES('s1','新对话',0,0,'miavo:x')")
    return sent


@pytest.mark.asyncio
async def test_the_local_model_knows_the_day_and_a_cloud_model_only_what_was_opened_to_it(app):
    share_everything(app)
    model = ScriptedModel([Completion("", [call("calendar_events", days=2), {"id": "call_find_contact", "name": "find_contact", "arguments": json.dumps({"name": "田中"})}], "tool_calls"), Completion("明晚和田中吃饭。", [], "stop")])
    _, message = await run(app, model, "我明天有什么安排")
    first = model.calls[0]
    assert {"calendar_events", "reminders_list", "find_contact", "add_event", "add_reminder"} <= {t["function"]["name"] for t in first["tools"]}
    assert "On my calendar from now through the next day" in first["messages"][-1]["content"] and "季度复盘" in first["messages"][-1]["content"]
    assert "calendar_events for what is on" in first["messages"][0]["content"]
    events, contact = message["metadata"]["steps"][0]["calls"]
    assert "和田中吃饭" in events["output"] and "+81 3-1234-5678" in contact["output"]
    journal = app.state.store.all("SELECT text FROM journal WHERE name IN ('calendar_events','find_contact')")
    assert journal and not any("日程" in j["text"] or "位" in j["text"] for j in journal)  # that it looked is recorded, not what it saw

    app.state.store.execute("DELETE FROM sessions")
    sent = cloud_model(app, [Completion("", [call("calendar_events", days=2)], "tool_calls"), Completion("我看不到你的日历。", [], "stop")])
    _, message = await run(app, ScriptedModel([]), "我明天有什么安排")
    assert not {"calendar_events", "reminders_list", "find_contact"} & set(sent[0]["tools"]) and "add_event" in sent[0]["tools"]
    assert "季度复盘" not in json.dumps(sent[0]["messages"], ensure_ascii=False)
    refused = message["metadata"]["steps"][0]["calls"][0]
    assert refused["status"] == "error" and "not open to you" in refused["output"] and "和田中吃饭" not in refused["output"]
    app.state.worker.tools.personal.share("calendar", True)
    await run(app, ScriptedModel([]), "那现在呢")
    assert "calendar_events" in sent[-1]["tools"] and "find_contact" not in sent[-1]["tools"] and "季度复盘" in sent[-1]["messages"][-1]["content"]


# ---- watches ----

@pytest.mark.asyncio
async def test_mail_that_arrives_starts_the_work_once_and_what_was_already_there_does_not(app):
    worker, store = app.state.worker, app.state.store
    mail, watches = worker.tools.mail, worker.tools.watches
    letters = [letter("7", "房东 <landlord@example.com>", "下月房租", "下个月起房租涨到 8000。")]
    mail.connect = lambda account: FakeBox(letters)
    mail.save("work", "qq", "me@example.com", "app-password")
    brief = "房东来信时，读完整封信，告诉我他要什么、有没有涨价、要不要我回复。"
    model = ScriptedModel([Completion("", [call("agenda"), call("watch_for", title="房东来信", brief=brief, when="mail", sender="landlord")], "tool_calls"),
                           Completion("好，房东来信我会告诉你。", [], "stop")])
    _, reply = await run(app, model, "房东来邮件的时候告诉我")
    assert "watch_for" in {t["function"]["name"] for t in model.calls[0]["tools"]} and "when something happens" in model.calls[0]["messages"][0]["content"]
    made = reply["metadata"]["steps"][0]["calls"][1]
    assert made["card"] == {"kind": "schedule", "title": "房东来信", "subtitle": "事件触发 · 收到来自 landlord 的邮件时"}
    assert json.loads(model.calls[1]["messages"][-2]["content"])["watches"] == []  # agenda was asked before the watch existed

    assert await watches.tick() == []  # the first look: the letter already in the inbox is not news
    letters.append(letter("8", "Newsletter <n@x.com>", "本周精选", "Hello"))
    watches.looked.clear()
    assert await watches.tick() == []  # new, but not from the landlord
    letters.append(letter("9", "房东 <landlord@example.com>", "水费", "这个月水费 120，请转给我。"))
    letters.append(letter("10", "房东 <landlord@example.com>", "还有电费", "电费 300。"))
    watches.looked.clear()
    (job,) = await watches.tick()  # two letters at once are one run
    payload = job["payload"]
    assert payload["background"] is True and payload["session_id"] == "s1" and payload["task"]["watched"] == "收到来自 landlord 的邮件时"
    assert payload["task"]["brief"].startswith(brief) and "水费" in payload["task"]["brief"] and "电费" in payload["task"]["brief"] and "本周精选" not in payload["task"]["brief"]
    watches.looked.clear()
    assert await watches.tick() == []

    worker.agent.runtime = task = ScriptedModel([Completion("", [call("finish_goal", status="done", summary="房东来信：水费 120、电费 300，等你转账。", evidence="信里写的")], "tool_calls"),
                                                 Completion('{"achieved": true, "missing": ""}', [], "stop")])
    await worker.agent.run(job)
    assert "Triggered run" in task.calls[0]["messages"][0]["content"]
    last = store.all("SELECT * FROM messages WHERE session_id='s1' ORDER BY created_at")[-1]
    assert last["content"].startswith("房东来信：水费 120") and last["metadata"]["task"]["watched"]
    (notice,) = worker.push.recent()  # the result is announced: nobody was waiting for it
    assert (notice["kind"], notice["title"], notice["id"]) == ("result", "房东来信", last["id"])

    client = client_of(app)
    (listed,) = client.get("/api/agenda").json()["watches"]
    assert listed["when"] == "收到来自 landlord 的邮件时" and listed["fired"] == 1
    assert client.delete("/api/watches/" + listed["id"]).status_code == 200 and client.delete("/api/watches/" + listed["id"]).status_code == 404


@pytest.mark.asyncio
async def test_an_event_about_to_begin_a_budget_used_up_and_a_door_that_opens_each_fire_once(app):
    worker = app.state.worker
    watches, tools = worker.tools.watches, worker.tools
    app.state.store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('s1','新对话',0,0)")
    with pytest.raises(ValueError, match="not shared"):
        await watches.create("s1", "会前提醒", "开会前把相关的邮件和上次的结论整理给我。", "calendar")
    with pytest.raises(ValueError, match="no monthly budget"):
        await watches.create("s1", "预算", "告诉我还剩多少、哪一类花得最多。", "budget", category="餐饮")
    share_everything(app)
    await watches.create("s1", "会前提醒", "开会前把相关的邮件和上次的结论整理给我。", "calendar", minutes=30, words="复盘")
    (job,) = await watches.tick()
    assert "季度复盘" in job["payload"]["task"]["brief"] and "带上预算表" in job["payload"]["task"]["brief"]
    watches.looked.clear()
    assert await watches.tick() == []  # told once per event
    with pytest.raises(ValueError, match="already exists"):
        await watches.create("s1", "会前提醒", "开会前把相关的邮件和上次的结论整理给我。", "calendar")
    watches.cancel(watches.list()[0]["id"])

    tools.ledger.set_budget("餐饮", 100)
    await watches.create("s1", "餐饮预算", "告诉我还剩多少、哪一类花得最多。", "budget", category="餐饮", percent=80)
    assert await watches.tick() == []
    tools.ledger.add(85, "餐饮", "面馆")
    watches.looked.clear()
    (job,) = await watches.tick()
    assert "85% of the ¥100.00 budget" in job["payload"]["task"]["brief"]
    app.state.store.execute("UPDATE jobs SET status='succeeded'")
    tools.ledger.add(50, "餐饮", "火锅")
    watches.looked.clear()
    assert await watches.tick() == []  # once a month
    watches.cancel(watches.list()[0]["id"])

    door = {"id": "ha:door", "name": "大门", "room": "玄关", "type": "sensor", "online": True, "state": {"contact": "closed"}, "actions": []}

    class Home:
        async def devices(self, refresh=False):
            return [door]

        async def find(self, ident):
            return door
    watches.home = Home()
    with pytest.raises(ValueError, match="no state field"):
        await watches.create("s1", "门开了", "大门打开时告诉我，并说现在几点。", "device", device_id="ha:door", key="lock", value="open")
    made = await watches.create("s1", "门开了", "大门打开时告诉我，并说现在几点。", "device", device_id="ha:door", key="contact", value="open")
    assert made["when"] == "大门 的 contact 变为 open 时"
    assert await watches.tick() == []
    door["state"]["contact"] = "open"
    watches.looked.clear()
    (job,) = await watches.tick()
    assert "大门 (玄关): contact is now open" in job["payload"]["task"]["brief"]
    watches.looked.clear()
    assert await watches.tick() == []  # still open: it changed once


# ---- sending mail ----

@pytest.mark.asyncio
async def test_an_email_goes_out_only_from_a_mailbox_opened_for_sending_and_only_on_a_yes(app):
    worker = app.state.worker
    mail, delivered = worker.tools.mail, []
    letters = [("7", b"Message-ID: <rent-7@example.com>\n" + letter("7", "房东 <landlord@example.com>", "下月房租", "下个月起房租涨到 8000，可以吗？")[1])]
    mail.connect = lambda account: FakeBox(letters)
    mail.deliver = lambda account, message: delivered.append((account["address"], message))
    mail.save("work", "qq", "me@example.com", "app-password")
    body = "王先生您好，\n\n8000 可以，从下个月开始。\n\n李雷"
    send = lambda **more: ScriptedModel([Completion("", [call("mail_send", body=body, **more)], "tool_calls"), Completion("发了。", [], "stop")])

    model = send(reply_to="work:7")
    _, message = await run(app, model, "回复房东说可以")
    assert "mail_send" not in {t["function"]["name"] for t in model.calls[0]["tools"]} and "you cannot send" in model.calls[0]["messages"][0]["content"]
    refused = message["metadata"]["steps"][0]["calls"][0]
    assert refused["status"] == "error" and "允许发送" in refused["output"] and delivered == []

    client = client_of(app)
    assert client.patch("/api/mail/work", json={"send": True}).json()["accounts"][0] == {**mail.public()[0], "send": True, "cloud": False}
    model = send(reply_to="work:7")
    (_, message), shown = await asyncio.gather(run(app, model, "回复房东说可以"), answer_when_awaiting(app, "run"))
    assert "send email for them with mail_send" in model.calls[0]["messages"][0]["content"]
    assert shown["command"].startswith("发邮件 · 从 me@example.com\n收件人：landlord@example.com\n主题：Re: 下月房租\n（回复一封来信）") and body in shown["command"]
    assert "permit" not in shown  # an email is asked about every time
    (sender, sent), = delivered
    assert sender == "me@example.com" and sent["To"] == "landlord@example.com" and sent["Subject"] == "Re: 下月房租" and sent["In-Reply-To"] == "<rent-7@example.com>"
    assert message_from_bytes(sent.as_bytes()).get_payload(decode=True).decode().strip() == body
    assert json.loads(message["metadata"]["steps"][0]["calls"][0]["output"])["sent"] is True
    assert "8000" not in app.state.store.one("SELECT text FROM journal WHERE name='mail_send'")["text"]

    (_, message), _ = await asyncio.gather(run(app, send(to="a@example.com", subject="你好"), "给 a 发一封"), answer_when_awaiting(app, "always"))
    assert len(delivered) == 2
    (_, message), _ = await asyncio.gather(run(app, send(to="b@example.com", subject="你好"), "给 b 也发一封"), answer_when_awaiting(app, "deny"))  # "always" does not cover email
    assert len(delivered) == 2 and message["metadata"]["steps"][0]["calls"][0]["summary"] == "你拒绝了这个操作"

    _, message = await run(app, send(to="不是地址", subject="你好"), "发给他")
    assert "完整的邮箱地址" in message["metadata"]["steps"][0]["calls"][0]["output"] and len(delivered) == 2
    assert mail.start("gmail", send=True)["url"].count("gmail.send") == 1 if mail.apps.configured("google") else True
    with pytest.raises(ValueError, match="允许发送"):
        mail.write({"g": {"kind": "gmail", "address": "me@gmail.com", "host": "", "oauth": {"provider": "google"}, "cloud": False}}) or mail.share("g", send=True)


# ---- what stays with the local model ----

@pytest.mark.asyncio
async def test_private_memories_and_quiet_conversations_are_not_shown_to_a_cloud_model(app):
    worker, store = app.state.worker, app.state.store
    memory = worker.memory
    memory.save("住处", "用户住在杭州西湖区")
    health = memory.save("血压", "用户上个月确诊了高血压，正在服药")["saved"]
    marked = memory.save("家里的事", "用户和父亲最近在为老房子的事争执", private=True)["saved"]
    private = {r["id"] for r in store.all("SELECT id FROM memories WHERE private=1")}
    assert private == {health, marked}  # the wording gave the first away; the second was said to be

    everything = {m["title"] for m in await memory.retrieve("用户 最近 身体 家里 住在哪")}
    for_cloud = {m["title"] for m in await memory.retrieve("用户 最近 身体 家里 住在哪", cloud=True)}
    assert everything == {"住处", "血压", "家里的事"} and for_cloud == {"住处"}
    assert {m["title"] for m in memory.search("血压 住处", cloud=True)} == {"住处"}

    now = time.time()
    for ident, title, persona in (("quiet", "最近的压力", "companion"), ("work", "搬家计划", None), ("doctor", "看医生", None)):
        store.execute("INSERT INTO sessions(id,title,created_at,updated_at,persona) VALUES(?,?,?,?,?)", (ident, title, now - 600, now - 600, persona))
        store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES(?,?,'user',?,'complete',?)", ("m-" + ident, ident, "搬家 压力 医生 都聊了", now - 600))
        store.execute("INSERT INTO digests(session_id,text,upto) VALUES(?,?,?)", (ident, "搬家 压力 医生：聊了很多", now - 600))
    store.execute("UPDATE memories SET session_id='doctor' WHERE id=?", (health,))
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('now','现在',?,?)", (now, now))
    here = {"id": "now"}
    seen = lambda found: {item["id"] for item in found["recent"] + found["related"]}
    assert seen(await memory.background(here, "搬家 压力 医生")) == {"quiet", "work", "doctor"}
    assert seen(await memory.background(here, "搬家 压力 医生", cloud=True)) == {"work"}
    assert len(worker.stream.find("搬家")) == 3 and [m["conversation"] for m in worker.stream.find("搬家", guarded=True)] == ["搬家计划"]
    assert worker.stream.around("m-quiet", guarded=True) is None and worker.stream.around("m-quiet")

    # the portrait a cloud model gets is written from the open facts only; until it exists it gets none
    memory.portrait_file.parent.mkdir(parents=True, exist_ok=True)
    memory.portrait_file.write_text("住在杭州。有高血压。")
    assert "高血压" in memory.portrait() and memory.portrait(cloud=True) == ""
    for n in range(3):
        memory.save(f"事实{n}", f"用户喜欢的第 {n} 种茶是龙井 {n} 号")
    asked = []

    async def stream(messages, **options):
        asked.append(messages[-1]["content"])
        return Completion("住在杭州，爱喝龙井。", [], "stop")
    worker.llm.stream = stream
    memory.portrait_file.unlink()
    assert await memory.draw(None) is True
    assert "高血压" in asked[0] and "高血压" not in asked[1] and "争执" not in asked[1] and memory.portrait(cloud=True) == "住在杭州，爱喝龙井。"

    worker.prefs.update({"memory": {"private_local": False}})  # the user says a cloud model may see everything
    assert {m["title"] for m in await memory.retrieve("血压 家里", cloud=True)} >= {"血压", "家里的事"}

    client = client_of(app)
    listed = {m["id"]: m for m in client.get("/api/state").json()["memories"]}
    assert listed[health]["private"] == 1
    client.put("/api/memories/" + health, json={"title": "血压", "content": "用户上个月确诊了高血压，正在服药", "private": False})
    assert store.one("SELECT private FROM memories WHERE id=?", (health,))["private"] == 0


# ---- the nightly copy ----

def test_a_copy_is_whole_and_only_the_newest_few_are_kept(app, tmp_path):
    worker, store = app.state.worker, app.state.store
    backup, client = worker.backup, client_of(app)
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('s1','要留下的对话',0,0)")
    (worker.settings.data / "workspace" / "plan.md").write_text("计划")
    assert backup.due(datetime(2026, 11, 26, 4, 0).timestamp()) and not backup.due(datetime(2026, 11, 26, 1, 0).timestamp())

    first = backup.run(now=datetime(2026, 11, 26, 4, 0).timestamp())
    assert first["name"] == "jurvis-20261126-040000" and not backup.due(datetime(2026, 11, 26, 5, 0).timestamp()) and backup.due(datetime(2026, 11, 27, 4, 0).timestamp())
    copy = sqlite3.connect(first["path"] + "/jurvis.sqlite3")
    assert copy.execute("SELECT title FROM sessions").fetchall() == [("要留下的对话",)]
    copy.close()
    folder = worker.settings.data / "backups" / first["name"]
    assert (folder / "workspace" / "plan.md").read_text() == "计划" and (folder / "security" / "local.json").exists() and not (folder / "assets").exists()

    assert client.put("/api/backup", json={"keep": 2}).json()["keep"] == 2
    for day in (27, 28):
        backup.run(now=datetime(2026, 11, day, 4, 0).timestamp())
    assert [c["name"] for c in backup.status()["copies"]] == ["jurvis-20261128-040000", "jurvis-20261127-040000"]

    assert client.put("/api/backup", json={"folder": str(worker.settings.data / "workspace")}).status_code == 400  # not inside the data it copies
    elsewhere = tmp_path / "disk" / "Jurvis"
    assert client.put("/api/backup", json={"folder": str(elsewhere)}).json()["location"] == str(elsewhere)
    assert [c["name"][:6] for c in client.post("/api/backup/run").json()["copies"]] == ["jurvis"] and len(list(elsewhere.iterdir())) == 1
    assert client.put("/api/backup", json={"enabled": False}).json()["enabled"] is False and not Backup(worker.settings, store).due(datetime(2027, 1, 1, 4, 0).timestamp())
    assert client.patch("/api/settings", json={"power": {"keep_awake": True}}).json()["settings"]["power"] == {"keep_awake": True}
    assert client.patch("/api/settings", json={"power": {"keep_awake": "yes"}}).status_code == 400


# ---- the user's verdict ----

def test_a_reply_marked_down_goes_on_the_improvement_list_and_an_ignored_check_in_makes_the_next_one_wait(app):
    worker, store, client = app.state.worker, app.state.store, client_of(app)
    checkin = worker.checkin
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('s1','新对话',0,0)")
    reply = store.add_message("s1", "assistant", "这是我整理的八周跑步计划……", "complete")
    assert client.post(f"/api/messages/{reply}/feedback", json={"rating": "down", "note": "太长了，我只要这周的"}).json()["feedback"]["rating"] == "down"
    assert store.one("SELECT metadata FROM messages WHERE id=?", (reply,))["metadata"]["feedback"]["note"] == "太长了，我只要这周的"
    (item,) = client.get("/api/journal/improvements").json()["model"]
    assert item["name"] == "feedback" and "太长了" in item["text"] and item["message_id"] == reply
    assert client.post(f"/api/messages/{reply}/feedback", json={"rating": ""}).json()["feedback"] is None
    assert client.post(f"/api/messages/{reply}/feedback", json={"rating": "meh"}).status_code == 400 and client.post("/api/messages/nope/feedback", json={"rating": "up"}).status_code == 404

    assert checkin.spacing() == 3600
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at,persona) VALUES('q','最近还好吗',0,0,'companion')")
    now = time.time()
    def reached_out(ident, hours_ago):
        store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at,metadata) VALUES(?,'q','assistant','这几天好像挺累的？','complete',?,?)",
                      (ident, now - hours_ago * 3600, json.dumps({"checkin": True})))
    reached_out("c1", 80)
    store.execute("INSERT INTO messages(id,session_id,role,content,status,created_at) VALUES('u1','q','user','是有点',  'complete',?)", (now - 79 * 3600,))
    assert checkin.spacing() == 3600  # answered
    reached_out("c2", 50)
    assert checkin.spacing() == 4 * 3600
    reached_out("c3", 20)
    assert checkin.spacing() == 16 * 3600
    reached_out("c4", 1)
    assert checkin.spacing() == 16 * 3600  # an hour old is not ignored yet
    reached_out("c5", 13)
    assert checkin.spacing() == 86400  # never longer than a day

    state = checkin.load()
    state["told"] = {"sleep": "the last two nights were short"}
    checkin.save(state)
    client.post("/api/messages/c5/feedback", json={"rating": "down"})
    muted = checkin.load()["muted"]
    assert set(muted) == {"sleep"} and muted["sleep"] > now + 6 * 86400 and client.get("/api/checkin").json()["muted_until"] == muted["sleep"]
