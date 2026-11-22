"""Phone calls made for the user: the audio on the line, the conversation, the provider binding and a whole call end to end."""
import asyncio
import base64
import json
import math
from array import array
from datetime import date, timedelta
from urllib.parse import parse_qs, urlencode

import httpx
import pytest
from fastapi.testclient import TestClient

from engine import call as callmod
from engine.app import create_app
from engine.call import RATE, Listener, Talk, from_mulaw, line_to_wav, to_mulaw, tones, wav_to_line
from engine.config import Settings
from engine.phone import PhoneError, Telnyx, Twilio
from engine.runtime import Completion
from engine.speech import to_wav


SOON = (date.today() + timedelta(days=5)).isoformat()   # a day a table could be booked for


def tone(ms, amplitude=9000, hz=300):
    return array("h", (int(amplitude * math.sin(2 * math.pi * hz * n / RATE)) for n in range(RATE * ms // 1000)))


def quiet(ms):
    return array("h", [0] * (RATE * ms // 1000))


def said(ms=600):
    """Someone saying something, then pausing: what the listener takes as one thing said."""
    return to_mulaw(quiet(100) + tone(ms) + quiet(900))


class FakeSpeech:
    """Recognises whatever is scripted, and "speaks" a tenth of a second of sound."""
    def __init__(self, hears=()):
        self.hears, self.spoken = list(hears), []

    def status(self):
        return {"stt": True, "tts": True}

    async def warm(self):
        pass

    async def transcribe(self, data, language="auto"):
        return {"text": self.hears.pop(0) if self.hears else "", "language": language}

    async def speak(self, text, speaker=""):
        self.spoken.append(text)
        return to_wav([0.2] * 2400, 24000)


class FakeModel:
    def __init__(self, replies):
        self.replies, self.calls, self.thinks = list(replies), [], 0

    async def stream(self, messages, **options):
        self.calls.append([dict(m) for m in messages])
        await asyncio.sleep(self.thinks)
        return Completion(self.replies.pop(0), [], "stop")


class FakeLine:
    def __init__(self):
        self.sent, self.cleared, self.pressed = [], 0, []

    async def send(self, audio):
        self.sent.append(audio)

    async def clear(self):
        self.cleared += 1

    async def press(self, digits):
        self.pressed.append(digits)

    async def hangup(self):
        pass


# ---- audio ----
def test_mulaw_matches_the_standard_and_round_trips():
    assert to_mulaw([0, -1, 32767, -32768]).hex() == "ff7f8000"
    wave = tone(200, 12000)
    back = from_mulaw(to_mulaw(wave))
    assert len(back) == len(wave) and max(abs(a - b) for a, b in zip(wave, back)) < 500


def test_synthesised_speech_is_brought_down_to_the_line_rate():
    line = wav_to_line(to_wav([0.5] * 24000, 24000))  # one second at 24 kHz
    assert len(line) == RATE
    assert abs(from_mulaw(line)[100] - 16383) < 600
    assert len(wav_to_line(to_wav([0.1] * 22050, 22050))) == RATE  # a rate 8 kHz does not divide


def test_listener_finds_where_something_said_ends_and_ignores_clicks():
    listener = Listener()
    assert listener.feed(quiet(300) + tone(100) + quiet(900)) == []          # a click is not speech
    heard = listener.feed(tone(600) + quiet(400))
    assert heard == [] and listener.voiced == 600                               # still within the pause that ends a sentence
    heard = listener.feed(quiet(400))
    assert len(heard) == 1 and 0.6 * RATE <= len(heard[0]) <= 1.6 * RATE
    assert line_to_wav(heard[0])[:4] == b"RIFF"


def test_keys_are_sent_as_tones():
    audio = tones("1#x")
    assert len(audio) == 2 * RATE * 280 // 1000 and max(abs(s) for s in from_mulaw(audio)) > 12000


# ---- the conversation ----
async def talk_through(talk, audio_turns, gap=0.05):
    runner = asyncio.ensure_future(talk.run())
    for audio in audio_turns:
        await asyncio.sleep(gap)
        while talk.playing_until and callmod.time.monotonic() < talk.playing_until:
            await asyncio.sleep(0.02)
        talk.feed(audio)
    return await asyncio.wait_for(runner, 5)


@pytest.mark.asyncio
async def test_a_call_says_what_it_is_books_and_hangs_up():
    speech = FakeSpeech(["喂你好，小南国", "周六七点四位可以的，贵姓", "好的李先生，周六晚上七点四位，订好了"])
    model = FakeModel(["想订周六晚上七点四位。", "姓李，李先生。", "好的，谢谢，再见。[[hangup]]",
                       '{"outcome": "done", "summary": "已订好周六 19:00 四位，李先生。"}'])
    line = FakeLine()
    seen = []
    talk = Talk(line, speech, model, purpose="订本周六晚上 7 点 4 位，靠窗优先", name="李先生", on_update=lambda t: seen.append(len(t)))
    reason = await talk_through(talk, [said(), said(), said()])
    assert reason == "助理结束了通话"
    # The model forgot to say what it is: the first thing said says so anyway.
    assert speech.spoken[0].startswith("您好，我是李先生的 AI 助理") and "想订周六晚上七点四位" in speech.spoken[0]
    assert speech.spoken[-1] == "好的，谢谢，再见。"   # the control is never spoken
    assert [t["who"] for t in talk.transcript] == ["them", "us", "them", "us", "them", "us"]
    assert len(line.sent) == 3 and seen[-1] == 6
    system = model.calls[0][0]["content"]
    assert "订本周六晚上 7 点 4 位" in system and "AI assistant" in system and "Mandarin" in system
    assert await talk.report() == {"outcome": "done", "summary": "已订好周六 19:00 四位，李先生。", "booking": None}


@pytest.mark.asyncio
async def test_a_sentence_said_in_two_halves_is_answered_once():
    speech, line = FakeSpeech(["喂，你好，", "小南国淮海店。"]), FakeLine()
    model = FakeModel(["（只听到半句时想说的话）", "您好，我是李先生的 AI 助理，想订个位。[[hangup]]"])
    model.thinks = 0.3
    talk = Talk(line, speech, model, purpose="订位", name="李先生")
    runner = asyncio.ensure_future(talk.run())
    talk.feed(said(300))
    for _ in range(100):
        if model.calls:
            break
        await asyncio.sleep(0.01)
    talk.feed(said(300))            # they go on while the model is still answering the first half
    await asyncio.wait_for(runner, 5)
    assert speech.spoken == ["您好，我是李先生的 AI 助理，想订个位。"]
    assert model.calls[1][-1] == {"role": "user", "content": "喂，你好， 小南国淮海店。"}
    assert [t["who"] for t in talk.transcript] == ["them", "them", "us"]


@pytest.mark.asyncio
async def test_a_silent_line_is_asked_once_then_ended():
    speech, line = FakeSpeech(), FakeLine()
    talk = Talk(line, speech, FakeModel(["您好，我是王女士的 AI 助理，想订个位。"]), purpose="订位", name="王女士")
    talk.greeting_wait, talk.silence_wait = 0.05, 0.1
    assert await asyncio.wait_for(talk.run(), 5) == "对方一直没有声音"
    assert speech.spoken == ["您好，我是王女士的 AI 助理，想订个位。", "喂，您好，还在吗？"]
    assert (await talk.report())["outcome"] == "unreached"


@pytest.mark.asyncio
async def test_menu_keys_talking_over_and_the_other_side_hanging_up():
    speech, line = FakeSpeech(["订位请按一"]), FakeLine()
    talk = Talk(line, speech, FakeModel(["[[press:1]]"]), purpose="book a table for two tonight at 7", name="Lee", language="en")
    runner = asyncio.ensure_future(talk.run())
    talk.feed(said())
    for _ in range(100):
        if line.pressed:
            break
        await asyncio.sleep(0.02)
    assert line.pressed == ["1"] and speech.spoken == []
    talk.playing_until = callmod.time.monotonic() + 5     # we are in the middle of saying something
    talk.feed(to_mulaw(tone(500)))                        # and they start talking
    await asyncio.sleep(0)
    assert line.cleared == 1 and talk.playing_until == 0
    talk.hung_up()
    assert await asyncio.wait_for(runner, 5) == "对方挂断了"


@pytest.mark.asyncio
async def test_only_a_confirmed_day_and_time_becomes_a_booking():
    talk = Talk(FakeLine(), FakeSpeech(), FakeModel([]), purpose="土曜の19時に4名で予約", name="李", language="ja", zone="Asia/Tokyo")
    talk.transcript = [{"who": "them", "text": "はい、土曜日の19時、4名様で承りました", "at": 0}]
    assert talk.language == "ja" and "Asia/Tokyo" not in talk.messages[0]["content"] and "at the place called" in talk.messages[0]["content"]

    async def reported(answer):
        talk.llm = FakeModel([json.dumps(answer, ensure_ascii=False)])
        report = await talk.report()
        assert "Japanese" in talk.llm.calls[0][0]["content"]   # the user wrote in Japanese, so that is what they read
        return report["booking"]
    good = {"date": SOON, "time": "19:00", "people": 4, "note": ""}
    assert await reported({"outcome": "done", "summary": "予約できました", "booking": good}) == good
    assert await reported({"outcome": "partly", "summary": "…", "booking": good}) is None                       # not settled: nothing for the calendar
    assert await reported({"outcome": "done", "summary": "…", "booking": {**good, "date": "2020-01-04"}}) is None   # a day that has passed
    assert await reported({"outcome": "done", "summary": "…", "booking": {**good, "time": "夜"}}) is None
    assert await reported({"outcome": "done", "summary": "…", "booking": None}) is None


def test_a_bookings_clock_is_the_one_where_the_place_is():
    from engine.phone import zone_of
    assert zone_of("+6562345678") == "Asia/Singapore" and zone_of("+81312345678") == "Asia/Tokyo" and zone_of("+85221234567") == "Asia/Hong_Kong"
    assert zone_of("+14155550123") == ""   # a country with several clocks: the user's own is used


# ---- the provider binding ----
@pytest.fixture
def app(tmp_path):
    return create_app(Settings(data=tmp_path, public_url="https://jurvis.example.ts.net"), run_worker=False)


def twilio_api(log):
    def handle(request):
        log.append(request)
        if request.headers.get("authorization") != "Basic " + base64.b64encode(b"AC123:secret").decode():
            return httpx.Response(401, json={"message": "Authenticate"})
        if request.url.path.endswith("/IncomingPhoneNumbers.json"):
            return httpx.Response(200, json={"incoming_phone_numbers": [{"phone_number": "+14155550123"}]})
        if request.url.path.endswith("/Calls.json"):
            return httpx.Response(201, json={"sid": "CA1", "status": "queued"})
        return httpx.Response(200, json={"sid": "CA1", "status": "completed"})
    return httpx.MockTransport(handle)


def test_binding_checks_the_credentials_and_never_returns_them(app):
    log, phone = [], app.state.phone
    phone.transport = twilio_api(log)
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + app.state.security.local_token
        state = client.get("/api/phone").json()
        assert state["binding"] is None and {c["kind"] for c in state["catalog"]} == {"twilio", "telnyx"}
        assert state["public_url"] == "https://jurvis.example.ts.net"
        bad = client.put("/api/phone", json={"kind": "twilio", "settings": {"account_sid": "AC123", "auth_token": "wrong"}})
        assert bad.status_code == 400 and "拒绝" in bad.json()["detail"] and not phone.bound()
        saved = client.put("/api/phone", json={"kind": "twilio", "settings": {"account_sid": "AC123", "auth_token": "secret"}, "country": "86"}).json()["binding"]
        assert saved["caller"] == "+14155550123" and saved["country"] == "+86" and saved["numbers"] == ["+14155550123"]
        assert "secret" not in json.dumps(client.get("/api/phone").json()["binding"]) and "auth_token" not in saved["settings"]
        assert (phone.path.stat().st_mode & 0o777) == 0o600
        assert "phone_call" in app.state.worker.tools.available()
        assert client.delete("/api/phone").json() == {"binding": None}
        assert "phone_call" not in app.state.worker.tools.available()


def test_numbers_that_should_not_be_dialled_are_refused(app):
    phone = app.state.phone
    assert phone.number("021-6123 4567", "+86") == "+862161234567"
    assert phone.number("+1 (415) 555-0123") == "+14155550123"
    assert phone.number("0044 20 7946 0958") == "+442079460958"
    assert phone.number("6234 5678", "+65") == "+6562345678" and phone.number("03-1234-5678", "+81") == "+81312345678"
    for bad in ("110", "911", "+86 120", "+1 900 555 0100", "12345678901234567890"):
        with pytest.raises(PhoneError):
            phone.number(bad, "+86")
    with pytest.raises(PhoneError, match="国家区号"):
        phone.number("13800138000", "")


def test_status_reports_are_checked_against_the_provider():
    twilio = Twilio({"account_sid": "AC1", "auth_token": "12345"})
    body = urlencode({"CallSid": "CA1234567890ABCDE", "Caller": "+12349013030", "Digits": "1234", "From": "+12349013030", "To": "+18005551212"}).encode()
    url = "https://mycompany.com/myapp.php?foo=1&bar=2"   # the example in Twilio's own documentation
    assert twilio.verified(url, {"x-twilio-signature": "0/KCTR6DLpKmkAf8muzZqo1nDgQ="}, body)
    assert not twilio.verified(url, {"x-twilio-signature": "0/KCTR6DLpKmkAf8muzZqo1nDgQ="}, body + b"&To=%2B19005550100")
    assert twilio.progress({}, b"CallStatus=no-answer") == ("ended", "无人接听")
    assert twilio.progress({}, b"CallStatus=in-progress") == ("answered", "")

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
    key = Ed25519PrivateKey.generate()
    telnyx = Telnyx({"api_key": "KEY", "connection_id": "1", "public_key": base64.b64encode(key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)).decode()})
    event = json.dumps({"data": {"event_type": "call.hangup", "payload": {"hangup_cause": "user_busy"}}}).encode()
    stamp = str(int(callmod.time.time()))
    signed = {"telnyx-timestamp": stamp, "telnyx-signature-ed25519": base64.b64encode(key.sign(stamp.encode() + b"|" + event)).decode()}
    assert telnyx.verified("", signed, event) and not telnyx.verified("", signed, event + b" ")
    assert not telnyx.verified("", {**signed, "telnyx-timestamp": "1"}, event)
    assert telnyx.progress({}, event) == ("ended", "对方占线")


def test_telnyx_finds_its_application_and_presses_keys_through_the_api(app):
    seen = []

    def handle(request):
        seen.append((request.method, request.url.path, request.content))
        if request.headers.get("authorization") != "Bearer KEY1":
            return httpx.Response(401, json={"errors": [{"detail": "bad key"}]})
        if request.url.path == "/v2/phone_numbers":
            return httpx.Response(200, json={"data": [{"phone_number": "+13125550100", "connection_id": "1293384261075731499"}]})
        if request.url.path == "/v2/calls":
            return httpx.Response(200, json={"data": {"call_control_id": "v3:abc", "is_alive": False}})
        return httpx.Response(200, json={"data": {"result": "ok"}})
    phone = app.state.phone
    phone.transport = httpx.MockTransport(handle)

    async def scenario():
        saved = await phone.bind("telnyx", {"api_key": "KEY1"})
        assert saved["caller"] == "+13125550100" and saved["settings"]["connection_id"] == "1293384261075731499" and "api_key" not in saved["settings"]
        provider = phone.provider()
        assert await provider.dial("+862161234567", "+13125550100", "wss://x/phone/stream/t", "https://x/phone/status/t", 300) == "v3:abc"
        dial = json.loads(seen[-1][2])
        assert dial["stream_bidirectional_mode"] == "rtp" and dial["stream_bidirectional_codec"] == "PCMU" and dial["stream_track"] == "inbound_track"
        assert dial["connection_id"] == "1293384261075731499" and dial["webhook_url"] == "https://x/phone/status/t"
        assert await provider.press("v3:abc", "1#") and seen[-1][1] == "/v2/calls/v3:abc/actions/send_dtmf"
        await provider.hangup("v3:abc")
        assert seen[-1][:2] == ("POST", "/v2/calls/v3:abc/actions/hangup")
        assert provider.read({"event": "start", "stream_id": "S1", "start": {}}) == ("start", "S1")
        assert provider.read({"event": "media", "media": {"track": "inbound", "payload": "/38="}}) == ("audio", b"\xff\x7f")
        assert provider.read({"event": "media", "media": {"track": "outbound", "payload": "/38="}}) == (None, None)
        assert json.loads(provider.audio("S1", b"\xff")) == {"event": "media", "media": {"payload": "/w=="}} and json.loads(provider.clear("S1")) == {"event": "clear"}
    asyncio.run(scenario())


# ---- a whole call ----
class FakeSocket:
    """The provider's end of the audio WebSocket."""
    def __init__(self):
        self.inbox, self.sent = asyncio.Queue(), []

    async def receive_text(self):
        message = await self.inbox.get()
        if message is None:
            raise RuntimeError("closed")
        return message

    async def send_text(self, text):
        self.sent.append(json.loads(text))

    def media(self, mulaw):
        for at in range(0, len(mulaw), 160):
            self.inbox.put_nowait(json.dumps({"event": "media", "media": {"track": "inbound", "payload": base64.b64encode(mulaw[at:at + 160]).decode()}}))


async def bound(app, log):
    phone = app.state.phone
    phone.transport = twilio_api(log)
    await phone.bind("twilio", {"account_sid": "AC123", "auth_token": "secret"}, "+86")
    return phone


@pytest.mark.asyncio
async def test_a_whole_call_from_dialling_to_the_record(app):
    log = []
    phone = await bound(app, log)
    phone.speech = FakeSpeech(["你好小南国", "可以，周六七点四位，李先生，订好了"])
    phone.llm = FakeModel(["您好，我是李先生的 AI 助理，想订周六晚上七点四位。", "好的，谢谢，再见。[[hangup]]",
                           json.dumps({"outcome": "done", "summary": "周六 19:00 四位已订好。",
                                       "booking": {"date": SOON, "time": "19:00", "people": 4, "note": "座位保留 15 分钟"}})])
    calling = asyncio.ensure_future(phone.call("021 6123 4567", "订周六晚 7 点 4 位", "李先生", place="小南国"))
    for _ in range(100):
        if phone.live and next(iter(phone.live.values())).call_id:
            break
        await asyncio.sleep(0.01)
    token, live = next(iter(phone.live.items()))
    dial = parse_qs(log[-1].content.decode())
    assert dial["To"] == ["+862161234567"] and dial["From"] == ["+14155550123"]
    assert f'<Stream url="wss://jurvis.example.ts.net/phone/stream/{token}"/>' in dial["Twiml"][0]
    assert dial["StatusCallback"] == [f"https://jurvis.example.ts.net/phone/status/{token}"]
    assert phone.record(live.id)["status"] == "dialing"

    # A report nobody signed is refused; a signed one is taken.
    url, ringing = dial["StatusCallback"][0], b"CallStatus=ringing"
    assert not phone.report(token, {"x-twilio-signature": "bogus"}, ringing)
    signature = base64.b64encode(callmod_hmac("secret", url + "CallStatusringing")).decode()
    assert phone.report(token, {"x-twilio-signature": signature}, ringing) and phone.record(live.id)["status"] == "ringing"

    socket = FakeSocket()
    streaming = asyncio.ensure_future(phone.stream(token, socket))
    assert not await phone.stream("not-a-call", FakeSocket())
    socket.inbox.put_nowait(json.dumps({"event": "connected"}))
    socket.inbox.put_nowait(json.dumps({"event": "start", "streamSid": "MZ1", "start": {"streamSid": "MZ1", "callSid": "CA1"}}))
    socket.media(said())
    for _ in range(200):
        if len(phone.speech.spoken) == 1 and socket.sent:
            break
        await asyncio.sleep(0.01)
    assert phone.record(live.id)["status"] == "talking"
    assert socket.sent[0]["event"] == "media" and socket.sent[0]["streamSid"] == "MZ1"
    await asyncio.sleep(0.5)
    socket.media(said())
    result = await asyncio.wait_for(calling, 10)
    socket.inbox.put_nowait(None)   # the provider closes the stream once the call is hung up
    assert await asyncio.wait_for(streaming, 5)

    assert result["outcome"] == "done" and result["outcome_label"] == "办妥了" and result["summary"] == "周六 19:00 四位已订好。"
    assert [t["who"] for t in result["transcript"]] == ["对方", "助理", "对方", "助理"]
    assert log[-1].url.path.endswith("/Calls/CA1.json") and parse_qs(log[-1].content.decode()) == {"Status": ["completed"]}   # hung up at the provider
    record = phone.record(result["call_id"])
    assert record["status"] == "ended" and record["place"] == "小南国" and len(record["transcript"]) == 4 and not phone.live

    # What was booked waits for one of the user's devices to put it in their calendar, on the restaurant's clock.
    assert result["calendar"]["start"] == SOON + "T19:00" and result["calendar"]["timezone"] == "Asia/Shanghai"
    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer " + app.state.security.local_token
        waiting = client.get("/api/state").json()["calendar"]
        assert len(waiting) == 1 and waiting[0]["title"] == "小南国 · 4 位" and waiting[0]["minutes"] == 120
        assert "座位保留 15 分钟" in waiting[0]["notes"] and "+862161234567" in waiting[0]["notes"]
        ident = waiting[0]["id"]
        assert client.post(f"/api/calendar/{ident}/claim").json()["status"] == "claimed"
        assert client.post(f"/api/calendar/{ident}/claim").status_code == 409        # a second device does not add it again
        assert client.get("/api/state").json()["calendar"] == []
        client.post(f"/api/calendar/{ident}/done", json={"ok": False})               # that device could not: it is offered again
        assert len(client.get("/api/state").json()["calendar"]) == 1
        client.post(f"/api/calendar/{ident}/claim")
        client.post(f"/api/calendar/{ident}/done", json={"ok": True})
        assert client.get("/api/state").json()["calendar"] == []
        assert client.get(f"/api/phone/calls/{result['call_id']}").json()["booking"]["status"] == "added"


def callmod_hmac(token, text):
    import hashlib
    import hmac
    return hmac.new(token.encode(), text.encode(), hashlib.sha1).digest()


@pytest.mark.asyncio
async def test_a_call_nobody_answers(app):
    phone = await bound(app, [])
    phone.speech = FakeSpeech()
    calling = asyncio.ensure_future(phone.call("+862161234567", "订位", "李先生", place="小南国"))
    for _ in range(100):
        if phone.live and next(iter(phone.live.values())).call_id:
            break
        await asyncio.sleep(0.01)
    token = next(iter(phone.live))
    url = f"https://jurvis.example.ts.net/phone/status/{token}"
    signature = base64.b64encode(callmod_hmac("secret", url + "CallStatusno-answer")).decode()
    assert phone.report(token, {"x-twilio-signature": signature}, b"CallStatus=no-answer")
    result = await asyncio.wait_for(calling, 5)
    with TestClient(app) as client:   # the provider's door: open without pairing, and shut once the call is over
        assert client.post(f"/phone/status/{token}", content=b"CallStatus=completed").status_code == 403
        assert client.get("/api/phone").status_code == 401
    assert result["outcome"] == "unreached" and result["summary"] == "无人接听" and result["transcript"] == []


@pytest.mark.asyncio
async def test_what_stops_a_call_before_it_is_placed(app):
    phone = app.state.phone
    with pytest.raises(PhoneError, match="绑定"):
        await phone.call("+862161234567", "订位", "李先生")
    log = []
    await bound(app, log)
    phone.speech = FakeSpeech()
    phone.speech.status = lambda: {"stt": True, "tts": False}
    with pytest.raises(PhoneError, match="语音"):
        await phone.call("+862161234567", "订位", "李先生")
    phone.speech = FakeSpeech()
    phone.settings.public_url, phone.public_url = "", None
    import engine.phone as module
    original, module.funnel_url = module.funnel_url, lambda port: None
    try:
        with pytest.raises(PhoneError, match="公网"):
            await phone.call("+862161234567", "订位", "李先生")
    finally:
        module.funnel_url = original
    assert not any(r.url.path.endswith("/Calls.json") for r in log)


@pytest.mark.asyncio
async def test_the_assistant_must_ask_before_every_call(app):
    from tests.test_agent import ScriptedModel, chat_job
    phone = await bound(app, [])
    worker = app.state.worker
    worker.agent.runtime = ScriptedModel([
        Completion("我来打电话订位。", [{"id": "call_1", "name": "phone_call", "arguments": json.dumps(
            {"number": "021 6123 4567", "place": "小南国", "purpose": "订周六晚 7 点 4 位", "name": "李先生"})}], "tool_calls"),
        Completion("你没有同意，我就不打了。", [], "stop")])
    job, mid = chat_job(app, "帮我给小南国打电话订周六晚上 7 点 4 位")
    running = asyncio.ensure_future(worker.agent.run(job))
    entry = None
    for _ in range(200):
        steps = (app.state.store.one("SELECT metadata FROM messages WHERE id=?", (mid,))["metadata"] or {}).get("steps") or []
        entry = next((c for s in steps for c in s["calls"] if c.get("status") == "awaiting"), None)
        if entry:
            break
        await asyncio.sleep(0.01)
    assert entry and "小南国（+862161234567）" in entry["command"] and "李先生" in entry["command"]
    worker.shell.decide(mid, entry["id"], "deny")
    await asyncio.wait_for(running, 5)
    assert not phone.calls()   # nothing was dialled
    first = worker.agent.runtime.calls[0]
    assert "phone_call" in first["messages"][0]["content"] and any(t["function"]["name"] == "phone_call" for t in first["tools"])
