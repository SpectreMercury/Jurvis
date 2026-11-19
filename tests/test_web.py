"""The web client (PWA) under web/: served without auth at /app/, and every file it references exists."""
import json
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from engine.app import create_app
from engine.config import Settings

WEB = Path(__file__).resolve().parent.parent / "web"


@pytest.fixture
def client(tmp_path):
    app = create_app(Settings(data=tmp_path), run_worker=False)
    with TestClient(app) as c:
        yield c


def local(reference):
    """A relative reference -> its path under web/, or None for anchors and data URIs."""
    reference = reference.split("#", 1)[0].split("?", 1)[0]
    if not reference or reference.startswith("data:"):
        return None
    assert not re.match(r"^([a-z][a-z0-9+.-]*:)?//", reference, re.I) and not reference.startswith("/"), f"not a relative path: {reference}"
    return WEB / ("index.html" if reference in (".", "./") else reference)


def test_app_is_served_without_auth(client):
    response = client.get("/app/")
    assert response.status_code == 200
    assert "text/html" in response.headers["content-type"]
    assert response.text == (WEB / "index.html").read_text()
    assert '<div id="app">' in response.text
    assert client.get("/app/app.js").status_code == 200
    assert client.get("/app/sw.js").status_code == 200
    assert client.get("/app/manifest.webmanifest").status_code == 200
    # the API itself stays behind pairing
    assert client.get("/api/state").status_code == 401


def test_index_references_exist():
    html = (WEB / "index.html").read_text()
    references = re.findall(r'(?:href|src)="([^"]+)"', html)
    assert "./app.js" in references and "./app.css" in references and "./manifest.webmanifest" in references
    for reference in references:
        path = local(reference)
        assert path is None or path.is_file(), f"index.html references a missing file: {reference}"


def test_module_imports_exist():
    for script in WEB.glob("*.js"):
        for reference in re.findall(r"""(?:from|import)\s+['"](\.[^'"]+)['"]""", script.read_text()):
            assert (WEB / reference).is_file(), f"{script.name} imports a missing file: {reference}"


def test_manifest():
    manifest = json.loads((WEB / "manifest.webmanifest").read_text())
    for key in ("name", "start_url", "display", "icons"):
        assert manifest.get(key), f"manifest is missing {key}"
    assert manifest["display"] == "standalone"
    assert manifest["icons"]
    for icon in manifest["icons"]:
        assert icon.get("sizes") and icon.get("type")
        assert local(icon["src"]).is_file(), f"manifest icon is missing: {icon['src']}"
    assert local(manifest["start_url"]).is_file()


def test_service_worker_shell():
    source = (WEB / "sw.js").read_text()
    block = re.search(r"const SHELL = \[(.*?)\];", source, re.S)
    assert block, "sw.js has no SHELL list"
    shell = re.findall(r"'([^']+)'", block.group(1))
    assert "./index.html" in shell and "./app.js" in shell
    for reference in shell:
        assert "/api/" not in reference
        assert local(reference).is_file(), f"sw.js caches a missing file: {reference}"
    # every script and stylesheet of the app is part of the shell, so it works offline once cached
    for path in list(WEB.glob("*.js")) + list(WEB.glob("*.css")):
        if path.name != "sw.js":
            assert f"./{path.name}" in shell, f"{path.name} is not in the sw.js cache list"


def test_no_external_requests():
    for path in list(WEB.glob("*.html")) + list(WEB.glob("*.css")):
        text = path.read_text()
        assert not re.search(r"""(?:src|href)=["'](?:https?:)?//""", text), f"{path.name} loads something from another host"
        assert "@import" not in text and not re.search(r"url\(\s*['\"]?https?:", text), f"{path.name} loads something from another host"


@pytest.mark.asyncio
async def test_names_a_fake_ip_proxy_answers_for_are_still_public(monkeypatch):
    import asyncio, socket
    from engine import web

    def answers(address):
        async def getaddrinfo(host, port, **kwargs):
            return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, port))]
        return getaddrinfo
    loop = asyncio.get_running_loop()
    monkeypatch.setattr(loop, "getaddrinfo", answers("198.18.0.136"))
    await web.check_public("https://example.com/")                # the proxy's placeholder for a real site
    with pytest.raises(ValueError):
        await web.check_public("http://198.18.0.136/")             # the same address typed directly
    monkeypatch.setattr(loop, "getaddrinfo", answers("192.168.1.10"))
    with pytest.raises(ValueError):
        await web.check_public("https://router.example/")          # a name that points into the home network
