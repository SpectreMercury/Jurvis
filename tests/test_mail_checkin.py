"""Email over IMAP (read-only, per-account model access) and the assistant checking in on its own when body data turns."""
import json
import time
from datetime import date, datetime, timedelta
from email.message import EmailMessage
from email.utils import format_datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.checkin import Checkin
from engine.config import Settings
from email import message_from_bytes

from engine.mail import Mail, plain, text_of
from engine.runtime import Completion
from tests.test_agent import ScriptedModel, call


def letter(uid, sender, subject, body, hours_ago=2, html=False):
    m = EmailMessage()
    m["From"], m["To"], m["Subject"] = sender, "me@example.com", subject
    m["Date"] = format_datetime(datetime.now().astimezone() - timedelta(hours=hours_ago))
    if html:
        m.set_content(f"<html><body><p>{body}</p><script>x()</script></body></html>", subtype="html")
    else:
        m.set_content(body)
    return uid, m.as_bytes()


class FakeBox:
    """An IMAP inbox that records what the client asked."""
    def __init__(self, letters, seen=(), log=None):
        self.letters, self.seen, self.log = dict(letters), set(seen), log if log is not None else []
        self.capabilities = ("IMAP4REV1",)
        self.literal = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def select(self, box, readonly=False):
        self.log.append(("select", box, readonly))
        return "OK", [b"1"]

    def uid(self, command, *args):
        self.log.append((command, args, self.literal))
        if command == "SEARCH":
            words = lambda raw: (lambda m: text_of(m["Subject"]) + plain(m))(message_from_bytes(raw))
            hits = [u for u, raw in self.letters.items() if not self.literal or self.literal.decode() in words(raw)]
            self.literal = None
            return "OK", [" ".join(hits).encode()]
        uid = args[0].decode() if isinstance(args[0], bytes) else args[0]
        assert "PEEK" in args[1]  # never marks anything read
        flags = b"(\\Seen)" if uid in self.seen else b"()"
        return "OK", [(b"1 (UID " + uid.encode() + b" FLAGS " + flags + b" BODY[] {1}", self.letters[uid]), b")"]


@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path / "data"), run_worker=False)


def connect_mail(mail, letters, seen=(), cloud=False):
    log = []
    mail.connect = lambda account: FakeBox(letters, seen, log)
    mail.save("work", "qq", "me@example.com", "app-password", cloud=cloud)
    return log


def test_mail_is_read_only_and_never_shows_the_password(tmp_path):
    mail = Mail(Settings(data=tmp_path))
    log = connect_mail(mail, [letter("7", "老板 <boss@co.com>", "方案还要再改", "周五前给我新版本，这版不行。"),
                              letter("8", "Newsletter <n@x.com>", "本周精选", "Hello", hours_ago=30, html=True)], seen={"8"})
    assert mail.public() == [{"id": "work", "kind": "qq", "name": "QQ 邮箱", "address": "me@example.com", "host": "imap.qq.com", "auth": "password",
                              "cloud": False, "checked_at": mail.public()[0]["checked_at"], "error": None}]
    assert "app-password" not in json.dumps(mail.public()) and (tmp_path / "security/mail.json").stat().st_mode & 0o077 == 0
    got = mail.recent(days=3)
    assert [m["subject"] for m in got["messages"]] == ["方案还要再改", "本周精选"]
    assert got["messages"][0]["from"] == "老板" and got["messages"][0]["unread"] and not got["messages"][1]["unread"]
    assert "这版不行" in got["messages"][0]["preview"] and "x()" not in got["messages"][1]["preview"]
    assert all(entry[2] is True for entry in log if entry[0] == "select")  # opened read-only
    assert mail.recent(query="方案")["messages"][0]["subject"] == "方案还要再改"
    full = mail.read("work:7")
    assert "周五前" in full["text"] and full["to"] == "me@example.com"
    # an account not opened to cloud models is invisible to them
    assert mail.recent(cloud=True)["messages"] == [] and "local model only" in mail.recent(cloud=True)["note"]
    with pytest.raises(ValueError):
        mail.read("work:7", cloud=True)


def test_a_wrong_password_is_said_when_saving(tmp_path):
    mail = Mail(Settings(data=tmp_path))

    def refuse(account):
        raise ValueError("me@example.com 登录失败")
    mail.connect = refuse
    with pytest.raises(ValueError, match="登录失败"):
        mail.save("work", "qq", "me@example.com", "nope")
    assert mail.public() == []
    with pytest.raises(ValueError, match="IMAP"):
        mail.save("x", "imap", "me@example.com", "p")


def test_html_mail_becomes_text():
    from email import message_from_bytes
    _, raw = letter("1", "a@b.c", "s", "第一段<br>第二段 &amp; 更多", html=True)
    assert plain(message_from_bytes(raw)) == "第一段\n第二段 & 更多"


def tired(health, today):
    for i in range(35):
        d = today - timedelta(i)
        bad = i < 3
        health.put("apple", d, {"sleep_min": 320 if bad else 450, "hrv_ms": 40 if bad else 62, "resting_hr": 64 if bad else 57})


def test_check_in_signals_quiet_hours_and_limits(app):
    worker = app.state.worker
    checkin = worker.checkin
    today = date.today()
    assert checkin.signals(today) == []  # no data, nothing to say
    tired(worker.tools.health, today)
    signals = checkin.signals(today)
    assert any("stress is elevated" in s for s in signals)
    night = datetime.combine(today, datetime.min.time()).replace(hour=23)
    assert checkin.tick(now=night) is None  # quiet hours hold it...
    checkin.looked = 0
    noon = night.replace(hour=12)
    job = checkin.tick(now=noon)  # ...and it goes out as soon as they end
    assert job and job["payload"]["checkin"]["signals"] == signals and job["payload"]["background"]
    session = app.state.store.one("SELECT * FROM sessions WHERE id=?", (job["payload"]["session_id"],))
    assert session["persona"] == "companion" and session["title"] == "最近还好吗"
    checkin.looked = 0
    assert checkin.tick(now=noon) is None  # a sign already reached out about is not said again
    app.state.store.update_job(job["id"], status="succeeded")
    assert checkin.update(enabled=False)["enabled"] is False
    with pytest.raises(ValueError):
        checkin.update(quiet_start="半夜")


def test_a_new_sign_is_reached_out_about_as_the_data_arrives(app):
    worker, store = app.state.worker, app.state.store
    checkin, health = worker.checkin, worker.tools.health
    noon = datetime.combine(date.today(), datetime.min.time()).replace(hour=12)
    tired(health, date.today())
    first = checkin.tick(now=noon)
    store.update_job(first["id"], status="succeeded")
    checkin.looked = time.time()  # just looked: without new data nothing happens before the next look
    assert checkin.tick(now=noon) is None
    health.mood(1, "很烦")
    health.mood(2)
    assert checkin.tick(now=noon) is None  # new data is looked at at once, but the last message was just now: held
    state = checkin.load()
    checkin.save({**state, "last_at": time.time() - 2 * 3600})
    checkin.looked = 0
    job = checkin.tick(now=noon)  # the next look (a few minutes later) sends it, about the new sign only
    assert job["payload"]["checkin"]["signals"] == ["the moods they noted lately were low"]
    assert any("stress is elevated" in s for s in job["payload"]["checkin"]["also"])
    assert job["payload"]["session_id"] == first["payload"]["session_id"]  # the same quiet conversation
    store.update_job(job["id"], status="succeeded")
    # the sign goes away (a good night and calm heart today), then comes back: it is new again
    health.put("apple", date.today(), {"sleep_min": 480, "hrv_ms": 63, "resting_hr": 56})
    health.put("apple", date.today() - timedelta(1), {"sleep_min": 470, "hrv_ms": 62, "resting_hr": 57})
    health.put("apple", date.today() - timedelta(2), {"sleep_min": 470, "hrv_ms": 62, "resting_hr": 57})
    checkin.looked = 0
    assert checkin.tick(now=noon) is None and "stress" not in checkin.load()["told"]
    tired(health, date.today())
    checkin.save({**checkin.load(), "last_at": time.time() - 2 * 3600})
    checkin.looked = 0
    again = checkin.tick(now=noon)
    assert again and any("stress is elevated" in s for s in again["payload"]["checkin"]["signals"])


def test_no_check_in_while_the_user_is_talking_it_through(app):
    worker, store = app.state.worker, app.state.store
    noon = datetime.combine(date.today(), datetime.min.time()).replace(hour=12)
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at,persona) VALUES('c','聊聊',?,?,'companion')", (time.time(), time.time()))
    store.add_message("c", "user", "这几天真的很累")
    tired(worker.tools.health, date.today())
    assert worker.checkin.tick(now=noon) is None
    assert "stress" in worker.checkin.load()["told"]  # known from the conversation, not brought up again later
    worker.checkin.looked = 0
    assert worker.checkin.tick(now=noon) is None


@pytest.mark.asyncio
async def test_checking_in_reads_mail_and_conversations_on_the_local_model(app):
    worker, store = app.state.worker, app.state.store
    tired(worker.tools.health, date.today())
    connect_mail(worker.tools.mail, [letter("7", "老板 <boss@co.com>", "方案还要再改", "周五前给我新版本，这版不行。")])
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at) VALUES('w','周报',?,?)", (time.time(), time.time()))
    store.add_message("w", "user", "帮我把周报改得再专业一点，老板又不满意")
    worker.agent.llm = SimpleNamespace(chat_model=lambda m=None: "miavo:x", is_local=lambda m=None: m in (None, "local"), searches=lambda m=None: False,
                                       stream=None)  # a cloud model is never asked
    store.execute("UPDATE sessions SET model='miavo:x' WHERE id='w'")
    job = worker.checkin.tick(force=True)
    model = ScriptedModel([
        Completion("I'll take a quiet look first.", [call("health_summary", period="last_7_days"), call("mail_recent", days=3), call("recent_conversations", days=3)], "tool_calls"),
        Completion("这几天好像一直在为那个方案收尾？睡得也比平时少一些。想聊聊的话我在。", [], "stop"),
    ])
    worker.agent.runtime = model
    await worker.agent.run(store.get_job(job["id"]))
    offered = {t["function"]["name"] for t in model.calls[0]["tools"]}
    assert offered == {"health_summary", "cycle_status", "mail_recent", "mail_read", "recent_conversations"}
    system = model.calls[0]["messages"][0]["content"]
    assert "Reaching out" in system and "stress is elevated" in system and "Companion conversation" in system
    message = store.one("SELECT * FROM messages WHERE id=?", (job["payload"]["message_id"],))
    assert message["status"] == "complete" and message["content"].startswith("这几天好像")  # only the message, not the look-around
    assert message["metadata"]["checkin"] is True  # still marked after the run, so the apps announce it
    client = TestClient(app, headers={"Authorization": "Bearer " + app.state.security.local_token})
    assert [o["id"] for o in client.get("/api/state").json()["outreach"]] == [message["id"]]
    outputs = {c["name"]: c["output"] for c in message["metadata"]["steps"][0]["calls"]}
    assert "方案还要再改" in outputs["mail_recent"] and "老板又不满意" in outputs["recent_conversations"]


@pytest.mark.asyncio
async def test_a_cloud_model_reads_mail_only_from_accounts_opened_to_it(app):
    worker, store = app.state.worker, app.state.store
    connect_mail(worker.tools.mail, [letter("7", "Bank <b@bank.com>", "对账单", "余额 12,000")])
    sent = []

    async def stream(messages, *, model=None, tools=None, **options):
        sent.append(tools)
        return Completion("好的。", [], "stop")
    worker.agent.llm = SimpleNamespace(chat_model=lambda m=None: "miavo:x", is_local=lambda m=None: m in (None, "local"), searches=lambda m=None: False, stream=stream)
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at,model) VALUES('s1','新对话',0,0,'miavo:x')")
    from tests.test_agent import run
    await run(app, ScriptedModel([]), "我最近有什么邮件")
    assert not {"mail_recent", "mail_read"} & {t["function"]["name"] for t in sent[0]}
    worker.tools.mail.share("work", True)  # the user opens it to cloud models
    await run(app, ScriptedModel([]), "那现在呢")
    assert {"mail_recent", "mail_read"} <= {t["function"]["name"] for t in sent[1]} and "recent_conversations" not in {t["function"]["name"] for t in sent[1]}


def test_settings_endpoints(app):
    client, token = TestClient(app), {"Authorization": "Bearer " + app.state.security.local_token}
    assert client.get("/api/checkin", headers=token).json()["enabled"] is True
    assert client.put("/api/checkin", json={"quiet_start": "23:00"}, headers=token).json()["quiet_start"] == "23:00"
    assert client.post("/api/checkin/now", headers=token).status_code == 409
    store = app.state.store
    store.execute("INSERT INTO sessions(id,title,created_at,updated_at,persona) VALUES('c','最近还好吗',?,?,'companion')", (time.time(), time.time()))
    store.add_message("c", "assistant", "这两天睡得有点少？", "complete", {"checkin": True})
    store.add_message("c", "assistant", "", "queued", {"checkin": True})
    store.add_message("c", "assistant", "普通回复", "complete")
    assert [o["content"] for o in client.get("/api/state", headers=token).json()["outreach"]] == ["这两天睡得有点少？"]  # the apps notify these
    mail = client.get("/api/mail", headers=token).json()
    assert mail["accounts"] == [] and {"gmail", "outlook", "qq", "163", "icloud"} <= {p["kind"] for p in mail["presets"]}
    gmail = next(p for p in mail["presets"] if p["kind"] == "gmail")
    assert gmail["auth"] == "oauth" and gmail["app"]["app"] == "google" and not gmail["app"]["configured"]
    assert client.post("/api/mail/connect/gmail", headers=token).status_code == 400  # no Google app yet: said plainly


# ---------------------------------------------------------------- signing in: Gmail and Outlook
import base64 as _b64

import httpx


def mock(handler, log):
    def wrapped(request):
        log.append(request)
        return handler(request)
    return lambda: httpx.Client(transport=httpx.MockTransport(wrapped))


def amock(handler):
    return lambda: httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_gmail_is_connected_by_signing_in_and_read_through_its_api(app):
    client = TestClient(app, headers={"Authorization": "Bearer " + app.state.security.local_token})
    mail, apps = app.state.worker.tools.mail, app.state.worker.tools.oauth
    assert client.put("/api/oauth/google", json={"client_id": "cid.apps.googleusercontent.com", "client_secret": "sec"}).json()["configured"]
    # the same Google app serves Google Health too
    assert next(c for c in client.get("/api/health/connectors").json()["connectors"] if c["provider"] == "google")["configured"]
    url = client.post("/api/mail/connect/gmail", json={}).json()["url"]
    assert url.startswith("https://accounts.google.com/") and "gmail.readonly" in url and "access_type=offline" in url
    state = dict(p.split("=", 1) for p in url.split("?", 1)[1].split("&"))["state"]

    async def exchange(provider, code):
        assert (provider, code) == ("google", "the-code")
        return {"access_token": "access-tok-xyz", "refresh_token": "refresh-tok-xyz", "expires_at": time.time() + 3600}
    apps.exchange = exchange
    mail.ahttp = amock(lambda r: httpx.Response(200, json={"emailAddress": "me@gmail.com"}))
    page = client.get(f"/oauth/google?code=the-code&state={state}")
    assert page.status_code == 200 and "me@gmail.com" in page.text
    assert client.get(f"/oauth/google?code=the-code&state={state}").status_code == 400  # a state works once
    [account] = client.get("/api/mail").json()["accounts"]
    assert account["kind"] == "gmail" and account["auth"] == "oauth" and account["address"] == "me@gmail.com"
    assert "tok-xyz" not in client.get("/api/mail").text

    _, raw = letter("x", "老板 <boss@co.com>", "方案还要再改", "周五前给我新版本。")
    encoded = _b64.urlsafe_b64encode(raw).decode().rstrip("=")
    log = []

    def gmail(request):
        assert request.headers["authorization"] == "Bearer access-tok-xyz"
        if request.url.path.endswith("/messages"):
            return httpx.Response(200, json={"messages": [{"id": "m1"}]})
        return httpx.Response(200, json={"id": "m1", "labelIds": ["INBOX", "UNREAD"], "raw": encoded})
    mail.http = mock(gmail, log)
    got = mail.recent(days=3, query="方案")
    assert got["messages"][0]["subject"] == "方案还要再改" and got["messages"][0]["unread"] and got["messages"][0]["id"] == "me:m1"
    assert "newer_than:3d" in log[0].url.params["q"] and "方案" in log[0].url.params["q"]
    assert "周五前" in mail.read("me:m1")["text"]
    assert all(r.method == "GET" for r in log)  # read-only: nothing is modified
    with pytest.raises(ValueError, match="不需要密码"):
        mail.save("x", "gmail", "me@gmail.com", "password")


def test_an_expired_outlook_token_is_refreshed_and_kept(app):
    mail, apps = app.state.worker.tools.mail, app.state.worker.tools.oauth
    apps.set("microsoft", "ms-id", "ms-secret")
    mail.write({"me": {"kind": "outlook", "address": "me@outlook.com", "host": "", "cloud": False,
                       "oauth": {"provider": "microsoft", "access_token": "old", "refresh_token": "rt", "expires_at": 0}}})
    log = []

    def graph(request):
        if request.url.host == "login.microsoftonline.com":
            assert b"grant_type=refresh_token" in request.content and b"client_secret=ms-secret" in request.content
            return httpx.Response(200, json={"access_token": "new", "expires_in": 3600})
        assert request.headers["authorization"] == "Bearer new"
        now = datetime.now().astimezone().isoformat()
        return httpx.Response(200, json={"value": [{"id": "AAk=", "subject": "季度复盘", "from": {"emailAddress": {"name": "Lee", "address": "lee@x.com"}},
                                                    "receivedDateTime": now, "isRead": False, "bodyPreview": "周一开会"}]})
    mail.http = mock(graph, log)
    got = mail.recent(days=2)
    assert got["messages"][0]["subject"] == "季度复盘" and got["messages"][0]["from"] == "Lee"
    saved = mail.load()["me"]["oauth"]
    assert saved["access_token"] == "new" and saved["refresh_token"] == "rt"  # the refresh token is kept
    assert "receivedDateTime ge" in log[1].url.params["$filter"]
