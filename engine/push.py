"""Reaching the user when no app is open: a check-in, the result of background or scheduled work, an action waiting for a yes.

Everything worth telling is first a notice (a row here). An app that is open shows notices itself, from /api/state. A device
whose app is closed gets a push instead:
- the web app, installed or in a browser that allows it, through Web Push (RFC 8030/8291/8292). The keys are made on this Mac the
  first time they are needed; nothing has to be set up.
- the iPhone and iPad app through Apple's push service, which needs a key from the user's own Apple developer account
  (a .p8 file with its key ID and team ID), entered in 设置. Jurvis has no server of its own that could send for them.

A push carries the first lines of the message through Google's, Mozilla's or Apple's push service. Web Push bodies are encrypted
for the browser; Apple can read what is sent through it. Keys live in data/security/push.json, owner-only.
"""
import base64
import json
import logging
import os
import time
from urllib.parse import urlparse

import httpx
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from .store import dumps, new_id

log = logging.getLogger("jurvis.push")

PRESENT = 15             # a device that asked the engine for something this recently has its app open: it shows the notice itself
FRESH = 600              # a notice older than this is not pushed any more (the engine was off, or sending kept failing)
SHOWN_HOURS = 6          # how long a notice is offered to the apps
CONTACT = "https://github.com/SpectreMercury/Jurvis"   # who a push service may write to about misuse: the project, never the user
KINDS = {"checkin": "问候", "result": "后台结果", "approval": "等你确认", "test": "测试"}
APNS = {False: "https://api.push.apple.com", True: "https://api.sandbox.push.apple.com"}


def b64(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def signed(key, header, claims):
    """A compact ES256 JWT."""
    head = b64(json.dumps(header, separators=(",", ":")).encode()) + "." + b64(json.dumps(claims, separators=(",", ":")).encode())
    r, s = decode_dss_signature(key.sign(head.encode(), ec.ECDSA(hashes.SHA256())))
    return head + "." + b64(r.to_bytes(32, "big") + s.to_bytes(32, "big"))


def point(key):
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)


def sealed(body, p256dh, auth, ours=None, salt=None):
    """The body encrypted for one browser (RFC 8291, aes128gcm). ours and salt are given only by the tests."""
    theirs = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), p256dh)
    ours = ours or ec.generate_private_key(ec.SECP256R1())
    salt = salt or os.urandom(16)
    shared = ours.exchange(ec.ECDH(), theirs)
    mine = point(ours)
    ikm = HKDF(hashes.SHA256(), 32, auth, b"WebPush: info\x00" + p256dh + mine).derive(shared)
    key = HKDF(hashes.SHA256(), 16, salt, b"Content-Encoding: aes128gcm\x00").derive(ikm)
    nonce = HKDF(hashes.SHA256(), 12, salt, b"Content-Encoding: nonce\x00").derive(ikm)
    return salt + (4096).to_bytes(4, "big") + bytes([len(mine)]) + mine + AESGCM(key).encrypt(nonce, body + b"\x02", None)


class Gone(Exception):
    """The push service says this address will never work again."""


class Push:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.path = settings.data / "security" / "push.json"
        self.http = lambda **options: httpx.AsyncClient(timeout=15, **options)  # replaced in tests
        self.bearer = None  # (token, made at): Apple wants one reused for a while, not a new one per push
        with store.connection() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS notices(
                    id TEXT PRIMARY KEY,kind TEXT NOT NULL,title TEXT NOT NULL,body TEXT NOT NULL,session_id TEXT NOT NULL DEFAULT '',
                    extras TEXT NOT NULL DEFAULT '{}',created_at REAL NOT NULL,pushed INTEGER NOT NULL DEFAULT 0);
                CREATE TABLE IF NOT EXISTS push_targets(
                    id TEXT PRIMARY KEY,device_id TEXT NOT NULL,kind TEXT NOT NULL,address TEXT NOT NULL UNIQUE,
                    extras TEXT NOT NULL DEFAULT '{}',created_at REAL NOT NULL,sent_at REAL,error TEXT);
            """)

    # ---- keys ----
    def load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as file:
            json.dump(data, file)
        tmp.replace(self.path)

    def vapid(self):
        """This Mac's own Web Push key pair, made the first time a browser asks for it."""
        data = self.load()
        if not data.get("vapid"):
            key = ec.generate_private_key(ec.SECP256R1())
            data["vapid"] = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
            self.write(data)
        return serialization.load_pem_private_key(data["vapid"].encode(), None)

    def set_apns(self, key, key_id, team_id):
        """The user's Apple push key. The .p8 text is checked here, so a wrong file is said now and not at the first push."""
        key, key_id, team_id = key.strip(), key_id.strip().upper(), team_id.strip().upper()
        data = self.load()
        if not key and data.get("apns"):
            key = data["apns"]["key"]  # changing the IDs keeps the key that is already here
        if not (key and key_id and team_id):
            raise ValueError("需要 .p8 密钥的内容、Key ID 和 Team ID")
        try:
            loaded = serialization.load_pem_private_key(key.encode(), None)
        except (ValueError, TypeError):
            raise ValueError("这不是一个 .p8 密钥：它应以 -----BEGIN PRIVATE KEY----- 开头")
        if not isinstance(loaded, ec.EllipticCurvePrivateKey):
            raise ValueError("Apple 的推送密钥应是 P-256 密钥")
        data["apns"] = {"key": key, "key_id": key_id, "team_id": team_id}
        self.write(data)
        self.bearer = None
        return self.status()

    def clear_apns(self):
        data = self.load()
        data.pop("apns", None)
        self.write(data)
        self.bearer = None
        return self.status()

    def status(self, device=None):
        apns = self.load().get("apns")
        targets = self.store.all("SELECT t.id,t.device_id,t.kind,t.created_at,t.sent_at,t.error,d.name FROM push_targets t LEFT JOIN devices d ON d.id=t.device_id ORDER BY t.created_at DESC")
        return {"vapid_key": b64(point(self.vapid())), "apns": {"key_id": apns["key_id"], "team_id": apns["team_id"]} if apns else None,
                "targets": [{**t, "name": t["name"] or ("本机 Mac" if t["device_id"] == "local" else "已移除的设备"), "mine": t["device_id"] == device} for t in targets]}

    # ---- devices ----
    def subscribe(self, device, kind, body):
        """A device says where it can be reached. Asking again with the same address only refreshes it."""
        if kind == "web":
            keys = body.get("keys") or {}
            address = str(body.get("endpoint") or "")
            host = urlparse(address).hostname or ""
            if not address.startswith("https://") or not host or len(address) > 2000:
                raise ValueError("推送地址无效")
            try:
                if len(unb64(str(keys.get("p256dh") or ""))) != 65 or len(unb64(str(keys.get("auth") or ""))) != 16:
                    raise ValueError
            except ValueError:
                raise ValueError("推送密钥无效")
            extras = {"p256dh": keys["p256dh"], "auth": keys["auth"]}
        elif kind == "apns":
            address = str(body.get("token") or "").lower()
            if not (32 <= len(address) <= 200) or any(c not in "0123456789abcdef" for c in address):
                raise ValueError("设备令牌无效")
            extras = {"topic": str(body.get("topic") or "")[:200], "sandbox": bool(body.get("sandbox"))}
            if not extras["topic"]:
                raise ValueError("缺少 App 的 bundle ID")
        else:
            raise ValueError("不支持的推送方式")
        self.store.execute("INSERT INTO push_targets(id,device_id,kind,address,extras,created_at) VALUES(?,?,?,?,?,?) "
                           "ON CONFLICT(address) DO UPDATE SET device_id=excluded.device_id,extras=excluded.extras,error=NULL",
                           (new_id(), device, kind, address, dumps(extras), time.time()))
        return {"ok": True}

    def unsubscribe(self, device, address=None):
        if address:
            self.store.execute("DELETE FROM push_targets WHERE device_id=? AND address=?", (device, address))
        else:
            self.store.execute("DELETE FROM push_targets WHERE device_id=?", (device,))

    def remove(self, ident):
        self.store.execute("DELETE FROM push_targets WHERE id=?", (ident,))

    def present(self):
        """Devices whose app is open right now."""
        return {r["id"] for r in self.store.all("SELECT id FROM devices WHERE last_seen>=? AND revoked=0", (time.time() - PRESENT,))}

    # ---- notices ----
    def notify(self, kind, title, body, *, ident=None, session_id="", **extras):
        """Something the user should hear about. Told once: the same ident again changes nothing."""
        body = " ".join(str(body or "").split())
        if not body:
            return None
        ident = ident or new_id()
        with self.store.connection() as db:
            made = db.execute("INSERT OR IGNORE INTO notices(id,kind,title,body,session_id,extras,created_at) VALUES(?,?,?,?,?,?,?)",
                              (ident, kind, str(title or "Jurvis")[:80], body[:400], session_id or "", dumps(extras), time.time())).rowcount
        if made:
            self.store.event("notices", ident)
        return ident if made else None

    def settle(self, ident):
        """The thing a notice was about is done (an action was allowed or refused): it is not shown or pushed any more."""
        if self.store.execute("DELETE FROM notices WHERE id=?", (ident,)):
            self.store.event("notices", ident)

    def recent(self):
        """What the open apps announce themselves. Check-ins are not here: they reach the apps as messages (outreach in /api/state)."""
        return self.store.all("SELECT id,kind,title,body,session_id,extras,created_at FROM notices WHERE kind!='checkin' AND created_at>=? ORDER BY created_at DESC LIMIT 10",
                              (time.time() - SHOWN_HOURS * 3600,))

    async def flush(self):
        """Pushes the notices not pushed yet to every device whose app is closed. Returns how many pushes went out."""
        rows = self.store.all("SELECT * FROM notices WHERE pushed=0 ORDER BY created_at LIMIT 20")
        if not rows:
            return 0
        self.store.execute(f"UPDATE notices SET pushed=1 WHERE id IN ({','.join('?' for _ in rows)})", tuple(r["id"] for r in rows))
        rows = [r for r in rows if time.time() - r["created_at"] < FRESH]
        present = self.present()
        targets = [t for t in self.store.all("SELECT * FROM push_targets") if t["device_id"] not in present]
        sent = 0
        for notice in rows:
            for target in targets:
                sent += await self.send(target, notice)
        self.store.execute("DELETE FROM notices WHERE created_at<?", (time.time() - 7 * 86400,))
        return sent

    async def send(self, target, notice):
        try:
            if target["kind"] == "web":
                await self.web(target, notice)
            else:
                await self.apns(target, notice)
        except Gone:
            self.store.execute("DELETE FROM push_targets WHERE id=?", (target["id"],))
            return 0
        except (httpx.HTTPError, ValueError, OSError) as exc:
            log.warning("push to %s failed: %s", target["kind"], exc)
            self.store.execute("UPDATE push_targets SET error=? WHERE id=?", (str(exc)[:200] or type(exc).__name__, target["id"]))
            return 0
        self.store.execute("UPDATE push_targets SET sent_at=?,error=NULL WHERE id=?", (time.time(), target["id"]))
        return 1

    @staticmethod
    def payload(notice):
        return {"id": notice["id"], "kind": notice["kind"], "title": notice["title"], "body": notice["body"][:180],
                "session_id": notice["session_id"], **{k: v for k, v in notice["extras"].items() if isinstance(v, str)}}

    async def web(self, target, notice):
        key, address = self.vapid(), target["address"]
        origin = urlparse(address)
        token = signed(key, {"typ": "JWT", "alg": "ES256"}, {"aud": f"{origin.scheme}://{origin.netloc}", "exp": int(time.time()) + 12 * 3600, "sub": CONTACT})
        body = sealed(json.dumps(self.payload(notice), ensure_ascii=False).encode(), unb64(target["extras"]["p256dh"]), unb64(target["extras"]["auth"]))
        async with self.http() as http:
            r = await http.post(address, content=body, headers={"Authorization": f"vapid t={token}, k={b64(point(key))}", "Content-Encoding": "aes128gcm",
                                                                "Content-Type": "application/octet-stream", "TTL": "3600",
                                                                "Urgency": "high" if notice["kind"] == "approval" else "normal", "Topic": notice["id"][:32]})
        if r.status_code in (404, 410):
            raise Gone
        if r.status_code >= 400:
            raise ValueError(f"推送服务返回 {r.status_code}：{r.text[:120]}")

    async def apns(self, target, notice):
        config = self.load().get("apns")
        if not config:
            raise ValueError("还没有填写 Apple 推送密钥")
        if not self.bearer or time.time() - self.bearer[1] > 40 * 60:
            key = serialization.load_pem_private_key(config["key"].encode(), None)
            self.bearer = (signed(key, {"alg": "ES256", "kid": config["key_id"]}, {"iss": config["team_id"], "iat": int(time.time())}), time.time())
        data = self.payload(notice)
        body = {"aps": {"alert": {"title": data["title"], "body": data["body"]}, "sound": "default", "thread-id": notice["kind"],
                        **({"category": "approval", "interruption-level": "time-sensitive"} if notice["kind"] == "approval" else {})},
                **{k: v for k, v in data.items() if k not in ("title", "body")}}
        async with self.http(http2=True) as http:
            r = await http.post(f"{APNS[bool(target['extras'].get('sandbox'))]}/3/device/{target['address']}", json=body,
                                headers={"authorization": "bearer " + self.bearer[0], "apns-topic": target["extras"]["topic"], "apns-push-type": "alert",
                                         "apns-priority": "10", "apns-collapse-id": notice["id"][:64], "apns-expiration": str(int(time.time()) + 3600)})
        if r.status_code == 410 or (r.status_code == 400 and "BadDeviceToken" in r.text):
            raise Gone
        if r.status_code >= 400:
            if r.status_code == 403:
                self.bearer = None
            raise ValueError(f"Apple 推送返回 {r.status_code}：{r.text[:120]}")

    async def test(self, device):
        """Sends one push to every address of the asking device, whether or not its app is open. Returns what happened."""
        targets = self.store.all("SELECT * FROM push_targets WHERE device_id=?", (device,))
        if not targets:
            raise ValueError("这台设备还没有开启推送")
        notice = {"id": new_id(), "kind": "test", "title": "Jurvis", "body": "推送已经通了：以后我有事找你，会出现在这里。", "session_id": "", "extras": {}}
        sent = sum([await self.send(t, notice) for t in targets])
        failed = self.store.all("SELECT error FROM push_targets WHERE device_id=? AND error IS NOT NULL", (device,))
        return {"sent": sent, "errors": [f["error"] for f in failed]}
