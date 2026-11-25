"""Email: read by default, sent only from accounts the user opened for sending, one approved message at a time.

- Gmail and Outlook are connected by signing in (OAuth, read-only scopes: gmail.readonly, Mail.Read) and read through their APIs.
  No password is ever typed into Jurvis.
- iCloud, QQ, 163 and 126 offer no sign-in for other apps: they are read over IMAP with an app-specific password or
  authorization code the user generates on the provider's site (never their account password).
- Accounts and their tokens or codes live in data/security/mail.json, owner-only; the API never returns them.
- Nothing is marked read or moved (EXAMINE and BODY.PEEK over IMAP; read-only scopes over the APIs).
- Sending is off for every account until the user switches it on for that account. Gmail and Outlook then ask for one more
  permission at sign-in (gmail.send, Mail.Send); the others send over SMTP with the same app password. Every message is shown
  to the user in full and goes out only after they approve it (engine/tools.py: mail_send); a sent message is not kept here.
- What a model may read is decided per account: by default only the local model; an account can allow cloud models too.
"""
import base64
import email
import imaplib
import smtplib
import json
import re
import ssl
import time
from datetime import datetime, timedelta
from email.header import decode_header, make_header
from email.message import EmailMessage
from email.utils import formataddr, formatdate, getaddresses, make_msgid, parseaddr, parsedate_to_datetime
from html import unescape

import httpx

# kind -> how it connects. "oauth" kinds sign in with the user's own Google or Microsoft app; the others use an app password over IMAP.
PRESETS = {
    "gmail": {"name": "Gmail", "auth": "oauth", "app": "google", "host": "imap.gmail.com",
              "hint": "用 Google 账号登录授权，只读，不会看到你的密码。"},
    "outlook": {"name": "Outlook", "auth": "oauth", "app": "microsoft", "host": "outlook.office365.com",
                "hint": "用 Microsoft 账号登录授权（Outlook、Hotmail、Live、Microsoft 365），只读。"},
    "icloud": {"name": "iCloud 邮件", "auth": "password", "host": "imap.mail.me.com", "secret": "App 专用密码",
               "hint": "Apple 不提供给其他 App 的登录授权，需要一个 App 专用密码：登录 account.apple.com →「登录与安全」→「App 专用密码」，生成一个填在这里。它只能收邮件，不是你的 Apple 账户密码。",
               "link": "https://account.apple.com/account/manage"},
    "qq": {"name": "QQ 邮箱", "auth": "password", "host": "imap.qq.com", "secret": "授权码",
           "hint": "QQ 邮箱不提供登录授权，需要一个授权码：网页版 QQ 邮箱 → 设置 → 账号与安全 → 开启 IMAP 服务，按提示生成授权码。它不是 QQ 密码。",
           "link": "https://wx.mail.qq.com/"},
    "163": {"name": "网易 163 邮箱", "auth": "password", "host": "imap.163.com", "secret": "授权码",
            "hint": "网易邮箱不提供登录授权，需要一个客户端授权码：网页版 163 邮箱 → 设置 → POP3/SMTP/IMAP → 开启 IMAP，按提示新增授权码。",
            "link": "https://mail.163.com/"},
    "126": {"name": "网易 126 邮箱", "auth": "password", "host": "imap.126.com", "secret": "授权码",
            "hint": "同 163：网页版 126 邮箱 → 设置 → POP3/SMTP/IMAP → 开启 IMAP，新增一个客户端授权码。", "link": "https://mail.126.com/"},
    "imap": {"name": "其他邮箱", "auth": "password", "host": "", "secret": "专用密码",
             "hint": "任何支持 IMAP 的邮箱：填 IMAP 服务器地址，以及邮箱服务商给第三方客户端用的专用密码。"},
}
SCOPES = {"gmail": "https://www.googleapis.com/auth/gmail.readonly",
          "outlook": "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/User.Read"}
GMAIL = "https://gmail.googleapis.com/gmail/v1/users/me"
GRAPH = "https://graph.microsoft.com/v1.0/me"
NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
SEND_SCOPES = {"gmail": "https://www.googleapis.com/auth/gmail.send", "outlook": "https://graph.microsoft.com/Mail.Send"}
# Where the app-password accounts hand over outgoing mail: (host, port). 465 is TLS from the start, 587 upgrades.
SMTP = {"icloud": ("smtp.mail.me.com", 587), "qq": ("smtp.qq.com", 465), "163": ("smtp.163.com", 465), "126": ("smtp.126.com", 465)}
ADDRESS = re.compile(r"^[^@\s<>\"',;]+@[^@\s<>\"',;]+\.[^@\s<>\"',;]+$")
RECIPIENTS = 10          # addresses one message may go to


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
    def __init__(self, settings, apps=None):
        self.path = settings.data / "security" / "mail.json"
        self.apps = apps
        self.connect = self._connect  # replaced in tests
        self.deliver = self._deliver  # the SMTP hand-over; replaced in tests
        self.http = lambda: httpx.Client(timeout=20)  # replaced in tests
        self.ahttp = lambda: httpx.AsyncClient(timeout=20)

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
        return [{"id": k, "kind": v.get("kind", "imap"), "name": PRESETS.get(v.get("kind"), PRESETS["imap"])["name"], "address": v["address"],
                 "host": v.get("host", ""), "auth": "oauth" if v.get("oauth") else "password", "cloud": bool(v.get("cloud")), "send": bool(v.get("send")),
                 "checked_at": v.get("checked_at"), "error": v.get("error")} for k, v in self.load().items()]

    def presets(self):
        out = []
        for kind, p in PRESETS.items():
            item = {"kind": kind, **{k: v for k, v in p.items() if k != "app"}}
            if p["auth"] == "oauth" and self.apps:
                item["app"] = self.apps.describe(p["app"])
            out.append(item)
        return out

    def share(self, ident, cloud=None, send=None):
        """Opens an account to cloud models or keeps it for the local one; lets Jurvis send from it, or not."""
        accounts = self.load()
        if ident not in accounts:
            raise ValueError("没有这个邮箱")
        account = accounts[ident]
        if cloud is not None:
            account["cloud"] = bool(cloud)
        if send is not None:
            if send and account.get("oauth") and not account.get("send_granted"):
                raise ValueError("发送需要多一项授权：请在这个邮箱上点「允许发送」，重新登录一次")
            account["send"] = bool(send)
        self.write(accounts)
        return self.public()

    # ---- signing in (Gmail, Outlook) ----
    def start(self, kind, cloud=False, send=False):
        if PRESETS.get(kind, {}).get("auth") != "oauth" or not self.apps:
            raise ValueError("这个邮箱不支持登录授权")
        extra = {"access_type": "offline", "prompt": "consent"} if kind == "gmail" else {"prompt": "select_account"}
        scope = SCOPES[kind] + (" " + SEND_SCOPES[kind] if send else "")
        return {"url": self.apps.start(PRESETS[kind]["app"], "mail", scope, {"kind": kind, "cloud": bool(cloud), "send": bool(send)}, **extra)}

    async def finish(self, provider, code, state):
        """The browser came back from Google or Microsoft: keep the tokens under the address they belong to."""
        extra = self.apps.claim(provider, state)
        kind = extra["kind"]
        token = await self.apps.exchange(provider, code)
        async with self.ahttp() as http:
            r = await http.get(GMAIL + "/profile" if kind == "gmail" else GRAPH + "?$select=mail,userPrincipalName",
                               headers={"Authorization": "Bearer " + token["access_token"]})
        if r.status_code >= 400:
            raise ValueError(f"授权成功，但读不到邮箱地址：{r.text[:200]}")
        me = r.json()
        address = me.get("emailAddress") or me.get("mail") or me.get("userPrincipalName") or ""
        ident = re.sub(r"[^a-z0-9_-]", "", address.split("@")[0].lower())[:24] or kind
        accounts = self.load()
        if ident in accounts and accounts[ident]["address"] != address:
            ident = f"{ident}-{kind}"[:32]
        accounts[ident] = {"kind": kind, "address": address, "host": "", "oauth": {"provider": provider, **token},
                           "cloud": bool(extra.get("cloud")), "send": bool(extra.get("send")), "send_granted": bool(extra.get("send")), "checked_at": time.time()}
        self.write(accounts)
        return address

    def bearer(self, ident, account, http):
        """A current access token for a signed-in account, refreshed and saved when it has run out."""
        token = account["oauth"]
        if token.get("expires_at", 0) <= time.time():
            try:
                token = self.apps.refresh(token["provider"], token, http)
            except ValueError as exc:
                accounts = self.load()
                if ident in accounts:
                    accounts[ident]["error"] = str(exc)
                    self.write(accounts)
                raise ValueError(f"{account['address']}：{exc}")
            accounts = self.load()
            if ident in accounts:
                accounts[ident]["oauth"], accounts[ident]["error"] = token, None
                self.write(accounts)
            account["oauth"] = token
        return {"Authorization": "Bearer " + token["access_token"]}

    def api_recent(self, ident, account, days, query, unread, limit):
        with self.http() as http:
            auth = self.bearer(ident, account, http)
            if account["kind"] == "gmail":
                q = f"in:inbox newer_than:{days}d" + (" is:unread" if unread else "") + (f" {query}" if query else "")
                r = http.get(GMAIL + "/messages", params={"q": q, "maxResults": limit}, headers=auth)
                r.raise_for_status()
                out = []
                for ref in r.json().get("messages", [])[:limit]:
                    m = http.get(f"{GMAIL}/messages/{ref['id']}", params={"format": "raw"}, headers=auth)
                    m.raise_for_status()
                    body = m.json()
                    out.append(self.entry(ident, ref["id"], base64.urlsafe_b64decode(body["raw"] + "=="), "UNREAD" in body.get("labelIds", [])))
                return out
            since = datetime.now().astimezone() - timedelta(days=days)
            params = {"$top": limit, "$select": "id,subject,from,receivedDateTime,isRead,bodyPreview"}
            if query:
                params["$search"] = f'"{query}"'  # Graph allows no date filter together with a search: dates are checked below
            else:
                params["$filter"] = f"receivedDateTime ge {since.astimezone().isoformat(timespec='seconds').replace('+00:00', 'Z')}"
                params["$orderby"] = "receivedDateTime desc"
            r = http.get(GRAPH + "/mailFolders/inbox/messages", params=params, headers=auth)
            r.raise_for_status()
            out = []
            for m in r.json().get("value", []):
                at = datetime.fromisoformat(m["receivedDateTime"].replace("Z", "+00:00")).astimezone()
                if at < since or (unread and m.get("isRead")):
                    continue
                sender = (m.get("from") or {}).get("emailAddress") or {}
                out.append({"id": f"{ident}:{m['id']}", "from": sender.get("name") or sender.get("address", ""), "from_address": sender.get("address", ""),
                            "subject": m.get("subject") or "(无主题)", "time": at.strftime("%Y-%m-%d %H:%M"), "unread": not m.get("isRead"),
                            "preview": " ".join((m.get("bodyPreview") or "").split())[:200], "at": at.timestamp()})
            return out

    def api_read(self, ident, account, ref):
        with self.http() as http:
            auth = self.bearer(ident, account, http)
            if account["kind"] == "gmail":
                r = http.get(f"{GMAIL}/messages/{ref}", params={"format": "raw"}, headers=auth)
                r.raise_for_status()
                return base64.urlsafe_b64decode(r.json()["raw"] + "==")
            r = http.get(f"{GRAPH}/messages/{ref}/$value", headers=auth)  # the message as MIME
            r.raise_for_status()
            return r.content

    # ---- app passwords (iCloud, QQ, 163, any IMAP) ----
    def save(self, ident, kind, address, password, host="", cloud=False):
        if not NAME.match(ident or ""):
            raise ValueError("名称只能用小写字母、数字、- 和 _")
        if PRESETS.get(kind, {}).get("auth") == "oauth":
            raise ValueError(f"{PRESETS[kind]['name']} 用登录授权连接，不需要密码")
        accounts = self.load()
        old = accounts.get(ident, {})
        if old.get("oauth"):
            return self.share(ident, cloud)
        host = (host or PRESETS.get(kind, PRESETS["imap"])["host"]).strip()
        if not host:
            raise ValueError("请填写 IMAP 服务器地址")
        account = {"kind": kind, "address": address.strip(), "host": host, "password": password or old.get("password", ""), "cloud": bool(cloud),
                   "send": bool(old.get("send"))}
        if not account["password"]:
            raise ValueError(f"请填写{PRESETS.get(kind, PRESETS['imap']).get('secret', '专用密码')}")
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
                if account.get("oauth"):
                    found += self.api_recent(ident, account, max(1, min(days, 30)), query, unread, limit)
                    continue
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
            except (OSError, imaplib.IMAP4.error, ValueError, httpx.HTTPError) as exc:
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
        if account.get("oauth"):
            try:
                raw = self.api_read(account_id, account, ref)
            except httpx.HTTPError as exc:
                raise ValueError(f"读不到这封邮件：{exc}")
        else:
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

    # ---- sending ----
    def senders(self, cloud):
        """Accounts this model may send from: the user switched sending on, and the model may see the account at all."""
        return {k: v for k, v in self.accounts(cloud).items() if v.get("send")}

    def compose(self, ident, to, subject, body, reply_to="", cloud=False):
        """The message as it will go out, and the account it goes from. Nothing is sent. Raises ValueError for what cannot be sent."""
        senders = self.senders(cloud)
        if not senders:
            raise ValueError("没有可以发信的邮箱：需要你在 设置 → 邮件 里给一个邮箱打开「允许发送」")
        original = None
        if reply_to.strip():
            account_id, _, ref = reply_to.strip().partition(":")
            if account_id not in senders:
                raise ValueError("这封邮件所在的邮箱没有打开「允许发送」，或不对当前模型开放")
            ident = ident or account_id
            original = email.message_from_bytes(self.raw(account_id, senders[account_id], ref))
        ident = ident or (next(iter(senders)) if len(senders) == 1 else "")
        if ident not in senders:
            raise ValueError("要从哪个邮箱发？可以发信的有：" + "、".join(f"{k}（{v['address']}）" for k, v in senders.items()))
        account = senders[ident]
        to = [a for _, a in getaddresses([to if isinstance(to, str) else ",".join(to)]) if a]
        if not to and original is not None:
            to = [parseaddr(text_of(original.get("Reply-To") or original.get("From")))[1]]
        to = list(dict.fromkeys(a.strip() for a in to if a.strip()))
        if not to or len(to) > RECIPIENTS or not all(ADDRESS.match(a) for a in to):
            raise ValueError(f"收件人需要是 1 到 {RECIPIENTS} 个完整的邮箱地址")
        subject = " ".join(subject.split())[:300]
        if not subject and original is not None:
            subject = text_of(original.get("Subject"))
            subject = subject if subject.lower().startswith("re:") or subject.startswith("回复") else "Re: " + subject
        if not subject or not body.strip():
            raise ValueError("主题和正文都不能为空")
        message = EmailMessage()
        message["From"] = formataddr((account.get("display") or "", account["address"]))
        message["To"] = ", ".join(to)
        message["Subject"] = subject
        message["Date"] = formatdate(localtime=True)
        message["Message-ID"] = make_msgid(domain=account["address"].rpartition("@")[2] or "localhost")
        if original is not None and original.get("Message-ID"):
            message["In-Reply-To"] = original["Message-ID"]
            message["References"] = " ".join(filter(None, [" ".join((original.get("References") or "").split()), original["Message-ID"]]))[:2000]
        message.set_content(body.strip() + "\n")
        return ident, account, message, original is not None

    def preview(self, ident, to, subject, body, reply_to="", cloud=False):
        """What the user is shown before a message goes out: all of it."""
        _, account, message, answering = self.compose(ident, to, subject, body, reply_to, cloud)
        return (f"发邮件 · 从 {account['address']}\n收件人：{message['To']}\n主题：{message['Subject']}"
                + ("\n（回复一封来信）" if answering else "") + "\n\n" + body.strip())

    def send(self, ident, to, subject, body, reply_to="", cloud=False):
        ident, account, message, _ = self.compose(ident, to, subject, body, reply_to, cloud)
        raw = message.as_bytes()
        try:
            if account.get("oauth"):
                with self.http() as http:
                    auth = self.bearer(ident, account, http)
                    if account["kind"] == "gmail":
                        r = http.post(GMAIL + "/messages/send", json={"raw": base64.urlsafe_b64encode(raw).decode()}, headers=auth)
                    else:  # Graph takes the message as MIME, base64 in a text body
                        r = http.post(GRAPH + "/sendMail", content=base64.b64encode(raw), headers={**auth, "Content-Type": "text/plain"})
                    if r.status_code in (401, 403):
                        raise ValueError(f"{account['address']} 没有发送权限：请在 设置 → 邮件 里重新点一次「允许发送」")
                    r.raise_for_status()
            else:
                self.deliver(account, message)
        except (OSError, smtplib.SMTPException, httpx.HTTPError) as exc:
            raise ValueError(f"没有发出去：{text_of(str(exc))[:200]}")
        return {"sent": True, "from": account["address"], "to": message["To"], "subject": message["Subject"]}

    def _deliver(self, account, message):
        host, port = SMTP.get(account["kind"]) or (account.get("smtp") or account["host"].replace("imap.", "smtp.", 1), 465)
        context = ssl.create_default_context()
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, context=context, timeout=30)
        else:
            server = smtplib.SMTP(host, port, timeout=30)
            server.starttls(context=context)
        with server:
            server.login(account["address"], account["password"])
            server.send_message(message)

    def raw(self, ident, account, ref):
        """One message as it arrived, to answer it."""
        if account.get("oauth"):
            try:
                return self.api_read(ident, account, ref)
            except httpx.HTTPError as exc:
                raise ValueError(f"读不到要回复的邮件：{exc}")
        with self.connect(account) as box:
            box.select("INBOX", readonly=True)
            _, parts = box.uid("FETCH", ref, "(BODY.PEEK[HEADER])")
        found = next((p[1] for p in parts if isinstance(p, tuple)), b"")
        if not found:
            raise ValueError("要回复的邮件已经不在收件箱里")
        return found
