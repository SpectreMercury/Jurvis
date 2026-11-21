import hashlib
import hmac
import ipaddress
import json
import os
import secrets
import shutil
import socket
import subprocess
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode, urlparse
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from .store import new_id


class Security:
    def __init__(self, settings, store):
        self.settings, self.store = settings, store
        self.folder = settings.data / "security"
        self.folder.mkdir(parents=True, exist_ok=True)
        self.folder.chmod(0o700)
        self.cert = self.folder / "server.pem"
        self.key = self.folder / "server-key.pem"
        self.local_config = self.folder / "local.json"
        if not self.cert.exists() or not self.key.exists():
            key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
            name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "Jurvis Local Engine")])
            now = datetime.now(timezone.utc)
            cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
                    .serial_number(x509.random_serial_number()).not_valid_before(now-timedelta(minutes=5))
                    .not_valid_after(now+timedelta(days=3650))
                    .add_extension(x509.SubjectAlternativeName([x509.DNSName("localhost"), x509.IPAddress(ipaddress.ip_address("127.0.0.1"))]), critical=False)
                    .sign(key, hashes.SHA256()))
            self.key.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
            self.key.chmod(0o600)
            self.cert.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
        cert = x509.load_pem_x509_certificate(self.cert.read_bytes())
        self.fingerprint = cert.fingerprint(hashes.SHA256()).hex()
        if self.local_config.exists():
            self.local_token = json.loads(self.local_config.read_text())["token"]
        else:
            self.local_token = secrets.token_urlsafe(32)
        fd = os.open(self.local_config, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd,"w") as file:
            json.dump({"url":f"https://127.0.0.1:{settings.port}", "token":self.local_token, "fingerprint":self.fingerprint}, file)
        self.local_config.chmod(0o600)
        self.codes = {}
        self.attempts = {}

    def authorize(self, token):
        if token and hmac.compare_digest(token, self.local_token):
            return {"id":"local", "name":"本机 Mac", "local":True}
        if not token:
            return None
        row = self.store.one("SELECT id,name FROM devices WHERE token_hash=? AND revoked=0", (hashlib.sha256(token.encode()).hexdigest(),))
        if row:
            self.store.execute("UPDATE devices SET last_seen=? WHERE id=?", (time.time(),row["id"]))
        return row

    def pairing(self, urls):
        # An invitation is single-use, time-limited, and contains a pinned TLS fingerprint.
        # The first URL is the one to use; the rest (Tailscale, a public address) are addresses the device may fall back to.
        now=time.time()
        self.codes={k:v for k,v in self.codes.items() if v>now}
        code=secrets.token_urlsafe(24)
        self.codes[code]=now+300
        urls=list(dict.fromkeys(urls))
        url=urls[0]
        link="jurvis://pair?"+urlencode([("url",url),("code",code),("fingerprint",self.fingerprint)]+[("alt",other) for other in urls[1:]])
        # The same invitation for a browser: the web app reads the code from the fragment, which is never sent to a server.
        return {"url":url,"urls":urls,"code":code,"fingerprint":self.fingerprint,"link":link,"web_link":f"{url}/app/#pair={code}","expires_at":now+300}

    def pair(self, code, name, client):
        now=time.time()
        # Global and per-source limits; never log invitation codes or bearer tokens.
        self.attempts={k:[t for t in v if now-t<60] for k,v in self.attempts.items() if any(now-t<60 for t in v)}
        recent=self.attempts.setdefault(client,[])
        if len(recent)>=8 or sum(map(len,self.attempts.values()))>=40:
            raise ValueError("配对尝试过于频繁，请一分钟后重试")
        recent.append(now)
        expires=self.codes.pop(code,None)
        if expires is None or expires<now:
            raise ValueError("邀请无效或已过期，请在 Mac 上重新生成")
        token,ident=secrets.token_urlsafe(32),new_id()
        self.store.execute("INSERT INTO devices(id,name,token_hash,created_at,last_seen) VALUES(?,?,?,?,?)",
                           (ident,name,hashlib.sha256(token.encode()).hexdigest(),now,now))
        return {"token":token,"device_id":ident,"fingerprint":self.fingerprint}


def lan_addresses():
    import psutil
    return list(dict.fromkeys(a.address for name,items in psutil.net_if_addrs().items()
                            if not name.startswith(("lo","utun","awdl","llw"))
                            for a in items if a.family==socket.AF_INET and not a.address.startswith("127.")))


TAILNET = ipaddress.ip_network("100.64.0.0/10")   # the range Tailscale hands out addresses from


def tailscale_addresses():
    """This Mac's addresses on a Tailscale network, which reach it from outside the home."""
    import psutil
    return list(dict.fromkeys(a.address for name,items in psutil.net_if_addrs().items() if not name.startswith("lo")
                            for a in items if a.family==socket.AF_INET and ipaddress.ip_address(a.address) in TAILNET))


def reach_addresses():
    """Every address a paired device may use: the local network first, then Tailscale."""
    return list(dict.fromkeys(lan_addresses()+tailscale_addresses()))


TAILSCALE_BINARIES = ("/opt/homebrew/bin/tailscale", "/usr/local/bin/tailscale", "/Applications/Tailscale.app/Contents/MacOS/Tailscale")


def funnel_url(port):
    """The public address of a Tailscale Funnel that forwards to this engine, or None.

    Funnel publishes the engine at https://<machine>.<tailnet>.ts.net with a certificate every phone trusts,
    so a device reaches it from anywhere without Tailscale installed."""
    binary = shutil.which("tailscale") or next((path for path in TAILSCALE_BINARIES if os.path.exists(path)), None)
    if not binary:
        return None
    try:
        out = subprocess.run([binary, "serve", "status", "--json"], capture_output=True, text=True, timeout=3)
        config = json.loads(out.stdout or "{}") if out.returncode == 0 else {}
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    return funnel_from_config(config, port)


def funnel_from_config(config, port):
    """Reads `tailscale serve status --json`: a host:port open to the internet whose root forwards to the engine."""
    public = config.get("AllowFunnel") or {}
    for hostport, web in (config.get("Web") or {}).items():
        if not public.get(hostport):
            continue
        proxy = (((web or {}).get("Handlers") or {}).get("/") or {}).get("Proxy") or ""
        target = urlparse(proxy if "://" in proxy else "http://" + proxy)
        try:
            if target.hostname not in ("127.0.0.1", "localhost", "::1") or target.port != port:
                continue
        except ValueError:
            continue
        host, _, outer = hostport.rpartition(":")
        return f"https://{host}" if outer == "443" else f"https://{host}:{outer}"
    return None
