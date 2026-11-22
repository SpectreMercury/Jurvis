"""Phone calls the assistant makes for the user, carried by the user's own telephone provider.

The user binds one provider account (Twilio or Telnyx) in 设置. Jurvis has no account of its own: the number shown to the
person called, and the bill, are the user's. Credentials live in data/security/phone.json, owner-only; the API never returns them.

How a call runs:
    1. phone_call (engine/tools.py) is approved by the user, then call() asks the provider to dial.
    2. When the call is answered, the provider opens a WebSocket to /phone/stream/<token> on the engine's public address
       and audio flows both ways over it. engine/call.py holds the conversation.
    3. The provider reports progress to /phone/status/<token> (ringing, answered, ended, and why).
    4. The transcript and what came of the call are kept in the calls table and returned to the conversation.

The provider has to reach this Mac from the internet, so calls need the engine's public address (a Tailscale Funnel, or
JURVIS_PUBLIC_URL). The two /phone/ paths are the only ones open to it without pairing: each carries a token made for one
call, is good for that call only, and the status reports are checked against the provider's signature.
"""
import asyncio
import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from urllib.parse import parse_qsl
from xml.sax.saxutils import quoteattr

import httpx

from .call import FRAME, LIMIT, Talk, tones
from .llm import Prefs
from .security import funnel_url
from .store import dumps, new_id

RING_SECONDS = 40        # how long the other side is given to pick up
OUTCOMES = {"done": "办妥了", "partly": "没有完全办妥", "failed": "没办成", "unreached": "没联系上"}
# Numbers that bill the caller by the minute at special rates: never dialled.
PREMIUM = ("+1900", "+1976", "+449", "+4470", "+3389", "+49900", "+81990", "+61190", "+85290", "+651900")
# The clock a booking's time is on, for countries with one time zone. Elsewhere the event is put at that time on the user's own clock.
ZONES = {"+65": "Asia/Singapore", "+81": "Asia/Tokyo", "+82": "Asia/Seoul", "+86": "Asia/Shanghai", "+852": "Asia/Hong_Kong", "+853": "Asia/Macau",
         "+886": "Asia/Taipei", "+66": "Asia/Bangkok", "+60": "Asia/Kuala_Lumpur", "+84": "Asia/Ho_Chi_Minh", "+63": "Asia/Manila",
         "+44": "Europe/London", "+33": "Europe/Paris", "+49": "Europe/Berlin", "+39": "Europe/Rome", "+34": "Europe/Madrid", "+31": "Europe/Amsterdam",
         "+41": "Europe/Zurich", "+971": "Asia/Dubai", "+64": "Pacific/Auckland"}
BOOKING_MINUTES = 120    # how long a booked table is put in the calendar for
CLAIM_SECONDS = 120      # a device that took a calendar entry and never said it was added: another may take it after this


def zone_of(number):
    return next((ZONES[number[:n]] for n in (4, 3, 2) if number[:n] in ZONES), "")


class PhoneError(ValueError):
    """A failure with a message the user can act on."""


class Provider:
    kind = title = description = setup = console = ""
    fields = []   # [{"name", "label", "secret": bool, "placeholder", "optional": bool}] for the settings form

    def __init__(self, config, transport=None):
        self.config, self.transport = config, transport

    def client(self, **options):
        return httpx.AsyncClient(transport=self.transport, timeout=20, **options)

    def failure(self, response):
        try:
            data = response.json()
            errors = data.get("errors")
            detail = data.get("message") or (errors[0].get("detail") or errors[0].get("title") if errors else "")
        except (ValueError, AttributeError, IndexError, TypeError):
            detail = ""
        if response.status_code in (401, 403):
            return PhoneError(f"{self.title} 拒绝了这组凭据（{response.status_code}），请检查后重新填写")
        return PhoneError(f"{self.title} 返回错误 {response.status_code}" + (f"：{str(detail)[:300]}" if detail else ""))

    async def request(self, method, url, **options):
        try:
            async with self.client(**self.auth()) as client:
                response = await client.request(method, url, **options)
        except httpx.HTTPError as exc:
            raise PhoneError(f"连不上 {self.title}：{type(exc).__name__}") from exc
        if response.status_code >= 300:
            raise self.failure(response)
        return response.json() if response.content else {}

    # what each provider implements
    def auth(self):
        raise NotImplementedError

    async def numbers(self):
        """The account's own numbers a call can be shown as coming from. Also proves the credentials work."""
        raise NotImplementedError

    async def dial(self, to, caller, stream_url, status_url, limit):
        """Starts the call and returns the provider's id for it."""
        raise NotImplementedError

    async def hangup(self, call_id):
        raise NotImplementedError

    async def press(self, call_id, digits):
        """Presses phone keys on the live call. False when the provider has no way to: the keys are then sent as tones in the audio."""
        return False

    def complete(self, entry, owned):
        """Fills in what can be read from the account instead of asked for. Returns the entry to save."""
        return entry

    def verified(self, url, headers, body):
        """True when a status report really came from the provider."""
        raise NotImplementedError

    def progress(self, headers, body):
        """A status report as ("ringing" | "answered" | "ended" | None, why it ended)."""
        raise NotImplementedError

    def read(self, message):
        """One message from the audio WebSocket as ("start", stream id), ("audio", μ-law bytes), ("stop", None) or (None, None)."""
        raise NotImplementedError

    def audio(self, stream_id, chunk):
        """The WebSocket message that plays one chunk of μ-law audio to the other side."""
        raise NotImplementedError

    def clear(self, stream_id):
        """The WebSocket message that drops audio sent but not yet played."""
        raise NotImplementedError


class Twilio(Provider):
    kind, title = "twilio", "Twilio"
    description = "全球覆盖最广的云电话服务，按分钟计费。"
    console = "https://console.twilio.com"
    setup = ("在 Twilio 控制台首页的 Account Info 里复制 Account SID 和 Auth Token，填在这里。"
             "主叫号码是你在 Twilio 买的号码，或在 Verified Caller IDs 里验证过的自己的号码；留空就用账户里的第一个号码。"
             "打往哪些国家要先在 Voice → Settings → Geo permissions 里勾选。")
    fields = [{"name": "account_sid", "label": "Account SID", "placeholder": "AC…"},
              {"name": "auth_token", "label": "Auth Token", "secret": True},
              {"name": "caller", "label": "主叫号码", "placeholder": "+14155550123", "optional": True}]
    ENDED = {"completed": "", "busy": "对方占线", "no-answer": "无人接听", "failed": "电话没有打通", "canceled": "呼叫已取消"}

    @property
    def base(self):
        return f"https://api.twilio.com/2010-04-01/Accounts/{self.config['account_sid']}"

    def auth(self):
        return {"auth": (self.config["account_sid"], self.config["auth_token"])}

    async def numbers(self):
        data = await self.request("GET", self.base + "/IncomingPhoneNumbers.json", params={"PageSize": 50})
        return [n["phone_number"] for n in data.get("incoming_phone_numbers", []) if n.get("phone_number")]

    async def dial(self, to, caller, stream_url, status_url, limit):
        twiml = f'<?xml version="1.0" encoding="UTF-8"?><Response><Connect><Stream url={quoteattr(stream_url)}/></Connect></Response>'
        data = await self.request("POST", self.base + "/Calls.json", data={
            "To": to, "From": caller, "Twiml": twiml, "Timeout": str(RING_SECONDS), "TimeLimit": str(limit + 30),
            "StatusCallback": status_url, "StatusCallbackMethod": "POST",
            "StatusCallbackEvent": ["initiated", "ringing", "answered", "completed"]})   # sent once per event
        return data["sid"]

    async def hangup(self, call_id):
        await self.request("POST", f"{self.base}/Calls/{call_id}.json", data={"Status": "completed"})

    def verified(self, url, headers, body):
        # https://www.twilio.com/docs/usage/security#validating-requests: the URL, then each POST field name and value, sorted by name
        fields = parse_qsl(body.decode(errors="replace"), keep_blank_values=True)
        text = url + "".join(name + value for name, value in sorted(fields))
        expected = base64.b64encode(hmac.new(self.config["auth_token"].encode(), text.encode(), hashlib.sha1).digest()).decode()
        return hmac.compare_digest(expected, headers.get("x-twilio-signature", ""))

    def progress(self, headers, body):
        status = dict(parse_qsl(body.decode(errors="replace"))).get("CallStatus", "")
        if status in self.ENDED:
            return "ended", self.ENDED[status]
        return {"ringing": "ringing", "in-progress": "answered"}.get(status), ""

    def read(self, message):
        event = message.get("event")
        if event == "start":
            return "start", (message.get("start") or {}).get("streamSid") or message.get("streamSid") or ""
        if event == "media":
            return "audio", base64.b64decode((message.get("media") or {}).get("payload") or "")
        return ("stop", None) if event == "stop" else (None, None)

    def audio(self, stream_id, chunk):
        return json.dumps({"event": "media", "streamSid": stream_id, "media": {"payload": base64.b64encode(chunk).decode()}})

    def clear(self, stream_id):
        return json.dumps({"event": "clear", "streamSid": stream_id})


class Telnyx(Provider):
    kind, title = "telnyx", "Telnyx"
    description = "自有运营商网络的云电话服务，资费通常更低。"
    console = "https://portal.telnyx.com"
    setup = ("在 Telnyx 后台：① API Keys 里新建一个 API Key；② Voice → Programmable Voice 里新建一个 Voice API Application，"
             "Webhook 地址随便填一个 https 地址即可（每通电话会用自己的地址），把它的 Application ID 填在这里；"
             "Webhook API Version 选 API v2；③ 买一个号码并分配给这个 Application，再给 Application 配一个 Outbound Voice Profile，勾选要拨打的国家。"
             "Application ID 留空时，会用主叫号码所分配的那个 Application。④ 可选：把 Account → Public Key 填上，Jurvis 就会核对每条通话状态确实来自 Telnyx。")
    fields = [{"name": "api_key", "label": "API Key", "secret": True, "placeholder": "KEY…"},
              {"name": "connection_id", "label": "Application ID", "placeholder": "Voice API Application 的 ID", "optional": True},
              {"name": "caller", "label": "主叫号码", "placeholder": "+14155550123", "optional": True},
              {"name": "public_key", "label": "Public Key", "optional": True, "placeholder": "用来核对通话状态的来源"}]
    CAUSES = {"normal_clearing": "", "user_busy": "对方占线", "no_answer": "无人接听", "timeout": "无人接听", "originator_cancel": "呼叫已取消",
              "call_rejected": "对方拒接", "not_found": "号码不存在", "time_limit": "通话到了时长上限"}
    connections = {}   # number -> the application it is assigned to, as last read from the account

    def auth(self):
        return {"headers": {"Authorization": "Bearer " + self.config["api_key"]}}

    async def numbers(self):
        data = await self.request("GET", "https://api.telnyx.com/v2/phone_numbers", params={"page[size]": 50})
        found = [n for n in data.get("data", []) if n.get("phone_number")]
        self.connections = {n["phone_number"]: str(n["connection_id"]) for n in found if n.get("connection_id")}
        return [n["phone_number"] for n in found]

    def complete(self, entry, owned):
        if not entry.get("connection_id"):
            assigned = self.connections.get(entry["caller"])
            if not assigned:
                raise PhoneError("请填写 Application ID，或先在 Telnyx 后台把主叫号码分配给一个 Voice API Application")
            entry = {**entry, "connection_id": assigned}
        return entry

    async def press(self, call_id, digits):
        await self.request("POST", f"https://api.telnyx.com/v2/calls/{call_id}/actions/send_dtmf", json={"digits": digits})
        return True

    async def dial(self, to, caller, stream_url, status_url, limit):
        data = await self.request("POST", "https://api.telnyx.com/v2/calls", json={
            "connection_id": self.config["connection_id"], "to": to, "from": caller, "webhook_url": status_url, "webhook_url_method": "POST",
            "stream_url": stream_url, "stream_track": "inbound_track", "stream_bidirectional_mode": "rtp", "stream_bidirectional_codec": "PCMU",
            "timeout_secs": RING_SECONDS, "time_limit_secs": limit + 30})
        return data["data"]["call_control_id"]

    async def hangup(self, call_id):
        await self.request("POST", f"https://api.telnyx.com/v2/calls/{call_id}/actions/hangup", json={})

    def verified(self, url, headers, body):
        key = (self.config.get("public_key") or "").strip()
        if not key:
            return True  # without the key, the address made for this one call is what vouches for the report
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        try:
            timestamp = headers.get("telnyx-timestamp", "")
            if abs(time.time() - int(timestamp)) > 300:
                return False
            Ed25519PublicKey.from_public_bytes(base64.b64decode(key)).verify(
                base64.b64decode(headers.get("telnyx-signature-ed25519", "")), timestamp.encode() + b"|" + body)
            return True
        except (InvalidSignature, ValueError):
            return False

    def progress(self, headers, body):
        try:
            data = json.loads(body).get("data") or {}
        except ValueError:
            return None, ""
        event, payload = data.get("event_type"), data.get("payload") or {}
        if event == "call.hangup":
            cause = payload.get("hangup_cause") or ""
            return "ended", self.CAUSES.get(cause, "电话没有打通" if cause else "")
        return {"call.initiated": "ringing", "call.answered": "answered"}.get(event), ""

    def read(self, message):
        event = message.get("event")
        if event == "start":
            return "start", message.get("stream_id") or ""
        if event == "media":
            media = message.get("media") or {}
            if media.get("track") not in (None, "inbound"):
                return None, None
            return "audio", base64.b64decode(media.get("payload") or "")
        return ("stop", None) if event == "stop" else (None, None)

    def audio(self, stream_id, chunk):
        return json.dumps({"event": "media", "media": {"payload": base64.b64encode(chunk).decode()}})

    def clear(self, stream_id):
        return json.dumps({"event": "clear"})


KINDS = {c.kind: c for c in (Twilio, Telnyx)}


class Line:
    """The audio side of one live call, as engine/call.py sees it."""
    CHUNK = FRAME * 10   # 200 ms of audio per message

    def __init__(self, socket, provider, live, hangup):
        self.socket, self.provider, self.live, self.stream_id, self._hangup = socket, provider, live, "", hangup

    async def send(self, mulaw):
        for at in range(0, len(mulaw), self.CHUNK):
            await self.socket.send_text(self.provider.audio(self.stream_id, mulaw[at:at + self.CHUNK]))

    async def clear(self):
        try:
            await self.socket.send_text(self.provider.clear(self.stream_id))
        except Exception:
            pass  # the call ended under us

    async def press(self, digits):
        try:
            pressed = await self.provider.press(self.live.call_id, digits)
        except PhoneError:
            pressed = False
        if not pressed:
            await self.send(tones(digits))

    async def hangup(self):
        await self._hangup()


class Live:
    """A call in progress."""
    def __init__(self, ident, provider, brief):
        self.id, self.provider, self.brief = ident, provider, brief
        self.call_id = ""
        self.status_url = ""   # where the provider was told to report: what its signature is checked against
        self.talk = None
        self.done = asyncio.get_running_loop().create_future()
        self.started = time.time()
        self.answered_at = None


class Phone:
    def __init__(self, settings, store, llm, speech, prefs, transport=None):
        self.settings, self.store, self.llm, self.speech, self.prefs, self.transport = settings, store, llm, speech, prefs, transport
        self.path = settings.data / "security" / "phone.json"
        self.live = {}        # token -> Live
        self.public_url = None   # for tests: an address to use instead of looking one up
        with store.connection() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS calls(
                id TEXT PRIMARY KEY,provider TEXT NOT NULL,number TEXT NOT NULL,place TEXT NOT NULL DEFAULT '',name TEXT NOT NULL DEFAULT '',
                purpose TEXT NOT NULL,status TEXT NOT NULL,outcome TEXT,summary TEXT,ended TEXT,transcript TEXT NOT NULL DEFAULT '[]',
                created_at REAL NOT NULL,answered_at REAL,ended_at REAL)""")
            # What a call booked, waiting to be put in the user's calendar by one of their devices (see calendar()).
            db.execute("""CREATE TABLE IF NOT EXISTS bookings(
                id TEXT PRIMARY KEY,call_id TEXT NOT NULL,title TEXT NOT NULL,place TEXT NOT NULL,number TEXT NOT NULL,start TEXT NOT NULL,
                timezone TEXT NOT NULL DEFAULT '',minutes INTEGER NOT NULL,people INTEGER NOT NULL DEFAULT 0,notes TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'pending',device TEXT,claimed_at REAL,created_at REAL NOT NULL)""")
            # A call that was live when the engine stopped is over.
            db.execute("UPDATE calls SET status='ended',outcome=COALESCE(outcome,'unreached'),ended=COALESCE(ended,'引擎重启，通话中断'),ended_at=COALESCE(ended_at,?) WHERE status!='ended'",
                       (time.time(),))

    # ---- the bound provider ----
    def config(self):
        data = Prefs.load(self.path)
        return data if data.get("kind") in KINDS else {}

    def bound(self):
        return bool(self.config())

    def provider(self):
        config = self.config()
        if not config:
            raise PhoneError("还没有绑定电话服务商，请到 设置 → 电话 里绑定")
        return KINDS[config["kind"]](config, self.transport)

    def catalog(self):
        return [{"kind": c.kind, "title": c.title, "description": c.description, "setup": c.setup, "console": c.console, "fields": c.fields} for c in KINDS.values()]

    def public(self):
        """The binding without its secrets, or None."""
        config = self.config()
        if not config:
            return None
        cls = KINDS[config["kind"]]
        secret = {f["name"] for f in cls.fields if f.get("secret")}
        return {"kind": cls.kind, "title": cls.title, "caller": config.get("caller", ""), "country": config.get("country", ""),
                "settings": {f["name"]: config.get(f["name"], "") for f in cls.fields if f["name"] not in secret}}

    async def bind(self, kind, values, country=""):
        """Saves the account after its credentials were accepted by the provider. Returns the binding as public() gives it."""
        if kind not in KINDS:
            raise PhoneError("不支持的电话服务商")
        cls, current = KINDS[kind], self.config()
        entry = {f["name"]: current[f["name"]] for f in cls.fields if current.get("kind") == kind and current.get(f["name"])}
        for field in cls.fields:
            value = str(values.get(field["name"]) or "").strip()
            if value:
                entry[field["name"]] = value
            elif field["name"] not in entry and not field.get("optional"):
                raise PhoneError(f"请填写 {field['label']}")
        country = "+" + re.sub(r"\D", "", country) if re.sub(r"\D", "", country or "") else ""
        provider = cls(entry, self.transport)
        owned = await provider.numbers()   # refused credentials stop here, before anything is saved
        if entry.get("caller"):
            entry["caller"] = self.number(entry["caller"], country)
        elif owned:
            entry["caller"] = owned[0]
        else:
            raise PhoneError(f"这个 {cls.title} 账户里还没有号码。请先买一个号码，或填写一个已验证的主叫号码")
        entry = provider.complete(entry, owned)
        Prefs.save(self.path, {**entry, "kind": kind, "country": country}, private=True)
        self.store.event("phone", kind)
        return {**self.public(), "numbers": owned}

    def unbind(self):
        self.path.unlink(missing_ok=True)
        self.store.event("phone", "")

    # ---- numbers ----
    def number(self, text, country=None):
        """A number as the network wants it (+country and digits). Refuses what should never be dialled."""
        country = self.config().get("country", "") if country is None else country
        raw = str(text or "").strip()
        digits = re.sub(r"\D", "", raw)
        if raw.startswith("+"):
            number = "+" + digits
        elif raw.startswith("00"):
            number = "+" + digits[2:]
        elif country:
            number = country + digits.lstrip("0")
        else:
            raise PhoneError(f"号码 {raw} 没有国家区号。请写成 +86… 这样，或在 设置 → 电话 里填默认国家区号")
        if not 9 <= len(number) <= 16:  # shorter is an emergency or service number, longer is not a phone number
            raise PhoneError(f"{raw} 不是一个可以拨打的电话号码")
        if number.startswith(PREMIUM):
            raise PhoneError(f"{raw} 是按分钟收费的声讯号码，不能拨打")
        return number

    async def address(self):
        """The https address the provider reaches this engine on."""
        if self.public_url:
            return self.public_url
        public = self.settings.public_url.strip().rstrip("/") or await asyncio.to_thread(funnel_url, self.settings.port)
        if not public or not public.startswith("https://"):
            raise PhoneError("电话服务商需要从公网连到这台 Mac，但还没有公网地址。请打开 Tailscale Funnel（tailscale funnel --bg https+insecure://localhost:"
                             f"{self.settings.port}），或在 .env 里设置 JURVIS_PUBLIC_URL")
        return public

    async def reach(self):
        try:
            return {"public_url": await self.address()}
        except PhoneError as exc:
            return {"public_url": "", "problem": str(exc)}

    # ---- records ----
    def row(self, row):
        if row and isinstance(row.get("transcript"), str):
            row["transcript"] = json.loads(row["transcript"])
        if row:
            row["outcome_label"] = OUTCOMES.get(row.get("outcome") or "", "")
            row["booking"] = self.store.one("SELECT id,title,start,timezone,minutes,people,status,device FROM bookings WHERE call_id=?", (row["id"],))
        return row

    def calls(self, limit=30):
        return [self.row(r) for r in self.store.all("SELECT * FROM calls ORDER BY created_at DESC LIMIT ?", (limit,))]

    def record(self, ident):
        return self.row(self.store.one("SELECT * FROM calls WHERE id=?", (ident,)))

    def update(self, ident, **changes):
        if "transcript" in changes:
            changes["transcript"] = dumps(changes["transcript"])
        self.store.execute("UPDATE calls SET " + ",".join(f"{k}=?" for k in changes) + " WHERE id=?", (*changes.values(), ident))
        self.store.event("calls", ident)

    # ---- the calendar ----
    def book(self, call_id, brief, booking, summary):
        """Queues a confirmed booking for the user's calendar. Returns it as the devices will see it."""
        people = booking["people"]
        ident = new_id()
        notes = "\n".join(part for part in (summary, booking["note"], f"电话 {brief['number']}", "由 Jurvis 电话预订") if part)
        self.store.execute("INSERT INTO bookings(id,call_id,title,place,number,start,timezone,minutes,people,notes,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                           (ident, call_id, brief["place"] + (f" · {people} 位" if people else ""), brief["place"], brief["number"],
                            f"{booking['date']}T{booking['time']}", zone_of(brief["number"]), BOOKING_MINUTES, people, notes, time.time()))
        self.store.event("calendar", ident)
        return self.store.one("SELECT * FROM bookings WHERE id=?", (ident,))

    def calendar(self):
        """Bookings no device has put in a calendar yet. The apps on the Mac, iPhone and iPad see these in /api/state; the first to
        claim one adds it, and the user's calendar account carries it to their other devices."""
        return self.store.all("SELECT * FROM bookings WHERE status='pending' OR (status='claimed' AND claimed_at<?) ORDER BY created_at",
                              (time.time() - CLAIM_SECONDS,))

    def claim(self, ident, device):
        """One device takes a booking to add it. None when another already has."""
        with self.store.connection() as db:
            taken = db.execute("UPDATE bookings SET status='claimed',device=?,claimed_at=? WHERE id=? AND (status='pending' OR (status='claimed' AND claimed_at<?))",
                               (device, time.time(), ident, time.time() - CLAIM_SECONDS)).rowcount
        return self.store.one("SELECT * FROM bookings WHERE id=?", (ident,)) if taken else None

    def added(self, ident, device, ok):
        """The device that claimed a booking says whether it is in the calendar now. If not, it goes back for another device."""
        self.store.execute("UPDATE bookings SET status=?,claimed_at=NULL WHERE id=? AND status='claimed' AND device=?", ("added" if ok else "pending", ident, device))
        row = self.store.one("SELECT call_id FROM bookings WHERE id=?", (ident,))
        if row:
            self.store.event("calls", row["call_id"])

    # ---- a call ----
    async def call(self, number, purpose, name, place="", callback="", language=""):
        """Places the call, holds the conversation and returns what came of it. Takes as long as the call does."""
        provider, config = self.provider(), self.config()
        number = self.number(number)
        callback = self.number(callback) if callback.strip() else ""
        if self.live:
            raise PhoneError("已经有一通电话在进行，等它结束再打下一通")
        status = self.speech.status()
        if not (status["stt"] and status["tts"]):
            raise PhoneError("打电话需要本机的语音识别和语音合成模型，请先在 设置 → 语音 里装好")
        public = await self.address()
        token, ident = secrets.token_urlsafe(24), new_id()
        brief = {"number": number, "purpose": purpose.strip(), "name": name.strip() or "用户", "place": place.strip() or number,
                 "callback": callback, "language": language}
        self.store.execute("INSERT INTO calls(id,provider,number,place,name,purpose,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
                           (ident, provider.kind, number, brief["place"], brief["name"], brief["purpose"], "dialing", time.time()))
        self.store.event("calls", ident)
        live = self.live[token] = Live(ident, provider, brief)
        try:
            await self.speech.warm()   # models loaded while the phone rings, not while someone waits on the line
            live.status_url = public + "/phone/status/" + token
            live.call_id = await provider.dial(number, config["caller"], "wss://" + public[8:] + "/phone/stream/" + token, live.status_url, LIMIT)
            try:
                reason = await asyncio.wait_for(asyncio.shield(live.done), RING_SECONDS + LIMIT + 120)
            except asyncio.TimeoutError:
                reason = "通话超时，已结束"
        except BaseException as exc:
            # refused by the provider, or the conversation that asked for the call was cancelled: nothing is left ringing
            if live.call_id:
                await self.end(live)
            self.update(ident, status="ended", outcome="unreached", ended=str(exc)[:300] or "已取消", ended_at=time.time())
            raise
        finally:
            self.live.pop(token, None)
        talk = live.talk
        report = await talk.report() if talk else {"outcome": "unreached", "summary": reason or "电话没有接通。", "booking": None}
        transcript = talk.transcript if talk else []
        self.update(ident, status="ended", outcome=report["outcome"], summary=report["summary"], ended=reason, transcript=transcript, ended_at=time.time())
        entry = self.book(ident, brief, report["booking"], report["summary"]) if report["booking"] else None
        return {"call_id": ident, "place": brief["place"], "number": number, "outcome": report["outcome"], "outcome_label": OUTCOMES[report["outcome"]],
                "summary": report["summary"], "ended": reason,
                **({"calendar": {"title": entry["title"], "start": entry["start"], "timezone": entry["timezone"] or "the user's own clock",
                                 "note": "Queued for the user's calendar: their Mac, iPhone or iPad adds it as soon as the Jurvis app is open there."}} if entry else {}),
                "duration_seconds": round(time.time() - live.answered_at) if live.answered_at else 0,
                "transcript": [{"who": "对方" if line["who"] == "them" else "助理", "text": line["text"]} for line in transcript]}

    async def end(self, live):
        try:
            await live.provider.hangup(live.call_id)
        except (PhoneError, KeyError):
            pass  # already over

    def finish(self, live, reason):
        if not live.done.done():
            live.done.set_result(reason)

    def stop(self, ident):
        """The user ends a live call from the app."""
        for live in self.live.values():
            if live.id == ident:
                if live.talk:
                    live.talk.hung_up("你结束了通话")
                else:
                    self.finish(live, "你结束了通话")
                asyncio.ensure_future(self.end(live))
                return True
        return False

    # ---- what the provider calls ----
    def report(self, token, headers, body):
        """A status report from the provider. Returns False when it should be refused."""
        live = self.live.get(token)
        if not live or not live.provider.verified(live.status_url, headers, body):
            return False
        state, why = live.provider.progress(headers, body)
        if state == "ringing" and not live.answered_at:
            self.update(live.id, status="ringing")
        elif state == "ended":
            if live.talk:
                live.talk.hung_up(why or "对方挂断了")
            else:
                self.finish(live, why or "电话没有接通")
        return True

    async def stream(self, token, socket):
        """The audio of one call, from the moment the provider connects until the call is over."""
        live = self.live.get(token)
        if not live or live.talk:
            return False  # not a call of ours, or its audio is already connected
        voice = self.prefs.get()["voice"]["speaker"]
        line = Line(socket, live.provider, live, lambda: self.end(live))
        talk = live.talk = Talk(line, self.speech, self.llm, purpose=live.brief["purpose"], name=live.brief["name"], callback=live.brief["callback"],
                                language=live.brief["language"], speaker=voice, zone=zone_of(live.brief["number"]), on_update=lambda transcript: self.update(live.id, transcript=transcript))
        runner = None
        try:
            while True:
                kind, value = live.provider.read(json.loads(await socket.receive_text()))
                if kind == "start" and not runner:
                    line.stream_id = value
                    live.answered_at = time.time()
                    self.update(live.id, status="talking", answered_at=live.answered_at)
                    runner = asyncio.ensure_future(self.converse(live, talk))
                elif kind == "audio" and runner:
                    talk.feed(value)
                elif kind == "stop":
                    break
        except Exception:
            pass  # the provider closed the connection: the call is over
        finally:
            talk.hung_up()
            if runner:
                await runner
            else:
                self.finish(live, "电话没有接通")
        return True

    async def converse(self, live, talk):
        try:
            reason = await talk.run()
        except Exception as exc:  # the model or the speech models failed mid-call: end it rather than leave someone on a silent line
            reason = talk.ended = f"通话中出了问题：{str(exc)[:200] or type(exc).__name__}"
        await self.end(live)
        self.finish(live, reason)
