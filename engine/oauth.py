"""The user's own OAuth apps (Google, WHOOP, Microsoft) and the sign-in round trip they are used for.

Jurvis has no app of its own registered with these services: each user makes one in the service's developer console, once,
and enters its ID and secret in 设置 (or in .env, which wins). One Google app serves both Google Health and Gmail.

Apps live in data/security/oauth_apps.json, owner-only; the secret is never sent back to a device. A sign-in started here
carries a single-use state that expires after 15 minutes; the browser comes back to /oauth/{provider}.
"""
import json
import secrets
import time
from urllib.parse import urlencode, urlparse

import httpx

PROVIDERS = {
    "google": {
        "name": "Google",
        "auth": "https://accounts.google.com/o/oauth2/v2/auth",
        "token": "https://oauth2.googleapis.com/token",
        "console": "https://console.cloud.google.com/apis/credentials",
        "setup": "在 Google Cloud 控制台新建一个 OAuth 客户端（类型选 Web 应用），「已获授权的重定向 URI」填下面的回调地址。"
                 "用 Google Health 就启用 Google Health API，用 Gmail 就启用 Gmail API，并在「OAuth 同意屏幕」里把自己加为测试用户。"
                 "然后把客户端 ID 和客户端密钥填在这里。Google Health 和 Gmail 共用这一个应用。",
    },
    "whoop": {
        "name": "WHOOP",
        "auth": "https://api.prod.whoop.com/oauth/oauth2/auth",
        "token": "https://api.prod.whoop.com/oauth/oauth2/token",
        "console": "https://developer-dashboard.whoop.com",
        "setup": "在 WHOOP 开发者后台新建一个 App：Redirect URL 填下面的回调地址，权限勾选 read:recovery、read:cycles、read:sleep、"
                 "read:workout、read:body_measurement 和 offline。然后把 Client ID 和 Client Secret 填在这里。",
    },
    "microsoft": {
        "name": "Microsoft",
        "auth": "https://login.microsoftonline.com/common/oauth2/v2.0/authorize",
        "token": "https://login.microsoftonline.com/common/oauth2/v2.0/token",
        "console": "https://entra.microsoft.com/#view/Microsoft_AAD_RegisteredApps/ApplicationsListBlade",
        "setup": "在 Microsoft Entra 管理中心「应用注册」里新建一个应用，账户类型选「任何组织目录中的帐户和个人 Microsoft 帐户」，"
                 "重定向 URI 选 Web，填下面的回调地址。在「API 权限」里添加 Microsoft Graph 的 Mail.Read 和 offline_access（要让 Jurvis 替你发信，再加 Mail.Send），"
                 "在「证书和密码」里新建一个客户端密码。然后把应用程序（客户端）ID 和密码的值填在这里。",
    },
}


class OAuthApps:
    def __init__(self, settings):
        self.settings = settings
        self.path = settings.data / "security" / "oauth_apps.json"
        self.pending = {}  # state -> (provider, purpose, extra, created)
        self.migrate()

    def migrate(self):
        """Apps entered before this file existed were kept next to the health tokens."""
        old = self.settings.data / "security" / "health.json"
        if self.path.exists() or not old.exists():
            return
        try:
            data = json.loads(old.read_text())
        except (OSError, ValueError):
            return
        if data.get("clients"):
            self.write({k: v for k, v in data["clients"].items() if k in PROVIDERS})

    def load(self):
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def write(self, data):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.chmod(0o600)
        tmp.replace(self.path)

    def env(self, provider):
        cid = getattr(self.settings, f"{provider}_client_id", "")
        secret = getattr(self.settings, f"{provider}_client_secret", "")
        return (cid, secret) if cid and secret else None

    def get(self, provider):
        """(client id, secret): from .env when set there, otherwise the one the user entered in the app."""
        found = self.env(provider)
        if found:
            return found
        saved = self.load().get(provider, {})
        return saved.get("id", ""), saved.get("secret", "")

    def configured(self, provider):
        return all(self.get(provider))

    def set(self, provider, cid, secret):
        """A blank secret keeps the saved one; both blank removes the app."""
        if provider not in PROVIDERS:
            raise ValueError("unknown provider")
        data = self.load()
        cid, secret = (cid or "").strip(), (secret or "").strip()
        if not cid and not secret:
            data.pop(provider, None)
        else:
            secret = secret or data.get(provider, {}).get("secret", "")
            if not cid or not secret:
                raise ValueError("请同时填写 Client ID 和 Client Secret")
            data[provider] = {"id": cid, "secret": secret}
        self.write(data)

    def redirect(self, provider):
        base = (self.settings.oauth_base or getattr(self.settings, "public_url", "") or f"https://localhost:{self.settings.port}").rstrip("/")
        return f"{base}/oauth/{provider}"

    def describe(self, provider):
        """What a 设置 sheet shows for this app. Never the secret."""
        p = PROVIDERS[provider]
        cid, _ = self.get(provider)
        return {"app": provider, "app_name": p["name"], "configured": self.configured(provider), "client_id": cid,
                "client_from": "env" if self.env(provider) else "app" if cid else None, "redirect_uri": self.redirect(provider),
                "console": p["console"], "setup": p["setup"], "on_this_mac": urlparse(self.redirect(provider)).hostname in ("localhost", "127.0.0.1")}

    # ---- the round trip ----
    def start(self, provider, purpose, scope, extra=None, **params):
        """The sign-in address. `purpose` says who finishes it (health, mail)."""
        cid, secret = self.get(provider)
        if not (cid and secret):
            raise ValueError(f"先填一次你自己的 {PROVIDERS[provider]['name']} 开发者应用，回调地址填 {self.redirect(provider)}")
        state, now = secrets.token_urlsafe(24), time.time()
        self.pending = {s: v for s, v in self.pending.items() if now - v[3] < 900}
        self.pending[state] = (provider, purpose, extra or {}, now)
        query = {"response_type": "code", "client_id": cid, "redirect_uri": self.redirect(provider), "scope": scope, "state": state, **params}
        return PROVIDERS[provider]["auth"] + "?" + urlencode(query)

    def purpose(self, state):
        held = self.pending.get(state)
        return held[1] if held else None

    def claim(self, provider, state):
        held = self.pending.pop(state, None)
        if not held or held[0] != provider or time.time() - held[3] > 900:
            raise ValueError("这个连接链接已失效，请回到 Jurvis 重新点连接")
        return held[2]

    @staticmethod
    def stamp(body):
        return {"access_token": body["access_token"], "refresh_token": body.get("refresh_token"), "expires_at": time.time() + float(body.get("expires_in", 3600)) - 60}

    def form(self, provider, **data):
        cid, secret = self.get(provider)
        return {**data, "client_id": cid, "client_secret": secret}

    async def exchange(self, provider, code):
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(PROVIDERS[provider]["token"], data=self.form(provider, grant_type="authorization_code", code=code, redirect_uri=self.redirect(provider)))
        if r.status_code >= 400:
            raise ValueError(f"{PROVIDERS[provider]['name']} 拒绝了授权：{r.text[:200]}")
        return self.stamp(r.json())

    def refresh(self, provider, token, http, **extra):
        """A fresh access token for a saved one (sync, for code that runs in a thread). Returns the updated token or raises."""
        r = http.post(PROVIDERS[provider]["token"], data=self.form(provider, grant_type="refresh_token", refresh_token=token["refresh_token"], **extra))
        if r.status_code >= 400:
            raise ValueError("授权已失效，请重新连接")
        fresh = self.stamp(r.json())
        fresh["refresh_token"] = fresh["refresh_token"] or token["refresh_token"]  # Google keeps the old refresh token
        return {**token, **fresh}
