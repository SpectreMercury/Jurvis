"""Email: the user's mailboxes read over IMAP, read-only. Gmail, iCloud, Outlook, QQ, 163 and any IMAP server work.

- Accounts and their passwords (app passwords) live in data/security/mail.json, owner-only; the API never returns a password.
- Messages are only looked at (EXAMINE, BODY.PEEK): nothing is marked read, moved or sent.
- What a model may read is decided per account: by default only the local model; an account can allow cloud models too.
"""
import email
import imaplib
import json
import re
import ssl
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.utils import parseaddr, parsedate_to_datetime
from html import unescape

PRESETS = {
    "gmail": ("imap.gmail.com", "Gmail：在 Google 账号里开启两步验证，再生成一个「应用专用密码」"),
    "icloud": ("imap.mail.me.com", "iCloud：在 account.apple.com 生成一个 App 专用密码"),
    "outlook": ("outlook.office365.com", "Outlook：需要账号允许 IMAP；个人账号可用应用密码"),
    "qq": ("imap.qq.com", "QQ 邮箱：设置 → 账户 → 开启 IMAP，用生成的授权码"),
    "163": ("imap.163.com", "163 邮箱：设置 → POP3/SMTP/IMAP → 开启 IMAP，用客户端授权码"),
    "126": ("imap.126.com", "126 邮箱：同 163，用客户端授权码"),
    "imap": ("", "其他邮箱：填 IMAP 服务器地址"),
}
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")


def text_of(value):
    try:
        return str(make_header(decode_header(value or ""))).strip()
    except Exception:
        return (value or "").strip()


def plain(message, limit=4000):
    """The readable text of a message: its text/plain part, or its HTML with the tags taken out."""
    parts = message.walk() if message.is_multipart() else [message]
    html = ""
    for part in parts:
        if part.get_content_maintype() == "multipart" or part.get("Content-Disposition", "").startswith("attachment"):
            continue
        try:
            body = part.get_payload(decode=True).decode(part.get_content_charset() or "utf-8", "replace")
        except Exception:
            continue
        if part.get_content_type() == "text/plain" and body.strip():
            return re.sub(r"\n{3,}", "\n\n", body).strip()[:limit]
        if part.get_content_type() == "text/html" and not html:
            html = body
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", html)
    text = unescape(re.sub(r"<[^>]+>", " ", re.sub(r"(?i)<br\s*/?>|</p>", "\n", text)))
    return re.sub(r"[ \t]+", " ", re.sub(r"\n\s*\n+", "\n\n", text)).strip()[:limit]


class Mail:
    def __init__(self, settings):
        self.path = settings.data / "security" / "mail.json"
        self.connect = self._connect  # replaced in tests

    def load(self):
        try:
            return json.loads(self.path.read_text()).get("accounts", {})
        except (OSError, ValueError):
            return {}

    def write(self, accounts):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"accounts": accounts}, ensure_ascii=False))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def public(self):
        return [{"id": k, "kind": v.get("kind", "imap"), "address": v["address"], "host": v["host"], "cloud": bool(v.get("cloud")),
                 "checked_at": v.get("checked_at"), "error": v.get("error")} for k, v in self.load().items()]

    def presets(self):
        return [{"kind": k, "host": host, "hint": hint} for k, (host, hint) in PRESETS.items()]

    def save(self, ident, kind, address, password, host="", cloud=False):
        if not NAME.match(ident or ""):
            raise ValueError("名称只能用小写字母、数字、- 和 _")
        accounts = self.load()
        old = accounts.get(ident, {})
        host = (host or PRESETS.get(kind, ("",))[0]).strip()
        if not host:
            raise ValueError("请填写 IMAP 服务器地址")
        account = {"kind": kind, "address": address.strip(), "host": host, "password": password or old.get("password", ""), "cloud": bool(cloud)}
        if not account["password"]:
            raise ValueError("请填写密码或应用专用密码")
        with self.connect(account) as box:  # a wrong password is said now, not at the first question
            box.select("INBOX", readonly=True)
        account["checked_at"] = time.time()
        accounts[ident] = account
        self.write(accounts)
        return self.public()

    def remove(self, ident):
        accounts = self.load()
        accounts.pop(ident, None)
        self.write(accounts)

    def _connect(self, account):
        box = imaplib.IMAP4_SSL(account["host"], 993, ssl_context=ssl.create_default_context(), timeout=20)
        try:
            box.login(account["address"], account["password"])
        except imaplib.IMAP4.error as exc:
            raise ValueError(f"{account['address']} 登录失败：{text_of(str(exc))[:120]}。请确认用的是应用专用密码或授权码")
        if "ID" in box.capabilities:  # 163 and 126 refuse to open a mailbox until the client says who it is
            try:
                box.xatom("ID", '("name" "Jurvis" "version" "1.0")')
            except imaplib.IMAP4.error:
                pass
        return box

    def accounts(self, cloud):
        return {k: v for k, v in self.load().items() if not cloud or v.get("cloud")}

    def recent(self, days=3, query="", unread=False, limit=30, cloud=False):
        """Recent messages across the accounts this model may read: sender, subject, time and the first lines."""
        found, problems = [], []
        since = (datetime.now() - timedelta(days=max(1, min(days, 30)))).strftime("%d-%b-%Y")
        for ident, account in self.accounts(cloud).items():
            try:
                with self.connect(account) as box:
                    box.select("INBOX", readonly=True)
                    criteria = ["SINCE", since] + (["UNSEEN"] if unread else [])
                    if query:
                        box.literal = query.encode()  # searched as UTF-8, so Chinese works
                        _, data = box.uid("SEARCH", "CHARSET", "UTF-8", *criteria, "TEXT")
                    else:
                        _, data = box.uid("SEARCH", *criteria)
                    for uid in reversed((data[0] or b"").split()[-limit:]):
                        _, parts = box.uid("FETCH", uid, "(FLAGS BODY.PEEK[]<0.20000>)")
                        raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
                        flags = b" ".join(p[0] if isinstance(p, tuple) else p for p in parts)
                        found.append(self.entry(ident, uid.decode(), raw, b"\\Seen" not in flags))
            except (OSError, imaplib.IMAP4.error, ValueError) as exc:
                problems.append(f"{account['address']}: {str(exc)[:120]}")
        found.sort(key=lambda m: m["at"], reverse=True)
        out = {"days": days, "messages": [{k: v for k, v in m.items() if k != "at"} for m in found[:limit]]}
        if not self.accounts(cloud):
            out["note"] = ("No mailbox is connected for this model. The user connects one under 设置 → 邮件." if not self.load() else
                           "The connected mailboxes are for the local model only; the user can allow cloud models per account under 设置 → 邮件.")
        if problems:
            out["problems"] = problems
        return out

    def entry(self, ident, uid, raw, unread):
        message = email.message_from_bytes(raw)
        name, address = parseaddr(text_of(message.get("From")))
        try:
            at = parsedate_to_datetime(message.get("Date")).astimezone()
        except (TypeError, ValueError):
            at = datetime.now().astimezone()
        return {"id": f"{ident}:{uid}", "from": name or address, "from_address": address, "subject": text_of(message.get("Subject")) or "(无主题)",
                "time": at.strftime("%Y-%m-%d %H:%M"), "unread": unread, "preview": " ".join(plain(message, 400).split())[:200], "at": at.timestamp()}

    def read(self, ident, cloud=False):
        """One message in full (its text, without attachments)."""
        account_id, _, ref = ident.partition(":")
        account = self.accounts(cloud).get(account_id)
        if not account or not ref:
            raise ValueError("没有这封邮件，或这个邮箱不对你开放")
        with self.connect(account) as box:
            box.select("INBOX", readonly=True)
            _, parts = box.uid("FETCH", ref, "(BODY.PEEK[])")
        raw = next((p[1] for p in parts if isinstance(p, tuple)), b"")
        if not raw:
            raise ValueError("这封邮件已经不在收件箱里")
        message = email.message_from_bytes(raw)
        entry = self.entry(account_id, ref, raw, False)
        entry.pop("at"); entry.pop("unread")
        return {**entry, "to": text_of(message.get("To")), "text": plain(message, 8000),
                "attachments": [text_of(p.get_filename()) for p in message.walk() if p.get_filename()][:10]}
