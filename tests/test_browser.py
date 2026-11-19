"""The browser the assistant drives, against a real headless Chrome and a local test site."""
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import psutil
import pytest

from engine.browser import Browser, installed

pytestmark = pytest.mark.skipif(installed() is None, reason="no Chromium-family browser installed")

INDEX = """<!doctype html><html><head><title>Test Home</title><style>.gone{display:none}</style></head><body>
<h1>Welcome</h1>
<p>Intro paragraph.</p>
<ul><li>First item</li><li>Second item</li></ul>
<a id="next" href="second.html">Go to second</a>
<a id="tab" href="second.html" target="_blank">Second in a new tab</a>
<input id="name" name="who" placeholder="Your name" value="old">
<button id="greet" onclick="document.getElementById('out').textContent = 'Hello ' + document.getElementById('name').value">Greet</button>
<select id="colour" onchange="document.getElementById('out').textContent = this.value"><option value="r">Red</option><option value="g">Green</option></select>
<form action="second.html"><input id="q" name="q" aria-label="Search"></form>
<button class="gone">Hidden button</button>
<p id="out"></p>
<script>document.title = "Test Home"</script>
</body></html>"""
SECOND = "<!doctype html><html><head><title>Second Page</title></head><body><h2>Second</h2><p>You arrived.</p></body></html>"


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture(scope="module")
def site(tmp_path_factory):
    root = tmp_path_factory.mktemp("site")
    (root / "index.html").write_text(INDEX)
    (root / "second.html").write_text(SECOND)
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(root)))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def leftovers(data):
    """Browser processes still running on this test's profile."""
    found = []
    for process in psutil.process_iter(["cmdline"]):
        if any(str(data) in part for part in process.info["cmdline"] or []):
            found.append(process.pid)
    return found


async def closed(browser, data):
    await browser.close()
    end = time.monotonic() + 5
    while leftovers(data) and time.monotonic() < end:
        time.sleep(.1)
    assert browser.proc is None and not leftovers(data)


@pytest.fixture
async def browser(tmp_path):
    instance = Browser(SimpleNamespace(data=tmp_path), headless=True, allow_local=True)
    yield instance
    await closed(instance, tmp_path)


@pytest.fixture
async def strict(tmp_path):
    instance = Browser(SimpleNamespace(data=tmp_path), headless=True)
    yield instance
    await closed(instance, tmp_path)


def by(elements, **wanted):
    return next(e for e in elements if all(e.get(k) == v for k, v in wanted.items()))


async def test_open_and_read(browser, site, tmp_path):
    assert browser.available()
    page = await browser.open(site + "/index.html")
    assert page["url"] == site + "/index.html" and page["title"] == "Test Home"
    assert "Welcome" in page["text"] and len(page["text"]) <= 3000
    read = await browser.read()
    assert read["title"] == "Test Home" and "truncated" not in read
    assert "# Welcome" in read["text"]
    assert f"[Go to second]({site}/second.html)" in read["text"]
    assert "- First item\n- Second item" in read["text"]
    assert "Hidden button" not in read["text"] and "document.title" not in read["text"]
    assert (tmp_path / "browser" / "profile").is_dir()


async def test_dom_lists_interactive_elements(browser, site):
    await browser.open(site + "/index.html")
    listing = await browser.dom()
    elements = listing["elements"]
    assert listing["url"] == site + "/index.html" and all(e["ref"].startswith("e") for e in elements)
    assert len({e["ref"] for e in elements}) == len(elements)
    link = by(elements, role="link", text="Go to second")
    assert link["href"] == site + "/second.html"
    field = by(elements, role="input", name="who")
    assert field["placeholder"] == "Your name" and field["value"] == "old"
    assert by(elements, role="button", text="Greet")
    assert by(elements, role="select")["value"] == "r"
    assert by(elements, role="input", text="Search")
    assert not [e for e in elements if e["text"] == "Hidden button"]
    again = (await browser.dom())["elements"]
    assert not {e["ref"] for e in again} & {e["ref"] for e in elements}


async def test_type_and_click_change_the_page(browser, site):
    await browser.open(site + "/index.html")
    elements = (await browser.dom())["elements"]
    typed = await browser.type(by(elements, role="input", name="who")["ref"], "Jurvis 你好")
    assert typed["typed"] == 9 and typed["into"].startswith("input") and typed["navigated"] is False
    clicked = await browser.click(by(elements, role="button", text="Greet")["ref"])
    assert clicked["clicked"] == "button Greet" and clicked["navigated"] is False and clicked["url"] == site + "/index.html"
    assert (await browser.evaluate("document.getElementById('out').textContent"))["value"] == "Hello Jurvis 你好"
    await browser.type("#name", "")
    await browser.click("#greet")
    assert "Hello" in (await browser.read())["text"] and "Jurvis" not in (await browser.read())["text"]


async def test_type_chooses_a_select_option_and_submits_a_form(browser, site):
    await browser.open(site + "/index.html")
    assert (await browser.type("#colour", "green"))["typed"] == 5
    assert (await browser.evaluate("document.getElementById('out').textContent"))["value"] == "g"
    with pytest.raises(ValueError, match="Red、Green"):
        await browser.type("#colour", "purple")
    with pytest.raises(ValueError, match="type"):
        await browser.click("#colour")
    sent = await browser.type("#q", "cats", submit=True)
    assert sent["navigated"] is True and sent["url"] == site + "/second.html?q=cats" and sent["title"] == "Second Page"


async def test_click_on_a_link_navigates(browser, site):
    await browser.open(site + "/index.html")
    link = by((await browser.dom())["elements"], role="link", text="Go to second")
    clicked = await browser.click(link["ref"])
    assert clicked["navigated"] is True and clicked["url"] == site + "/second.html" and clicked["title"] == "Second Page"
    assert "## Second" in (await browser.read())["text"]
    with pytest.raises(ValueError, match=f"没有找到元素 {link['ref']}.*dom"):
        await browser.click(link["ref"])


async def test_click_follows_a_new_tab(browser, site):
    await browser.open(site + "/index.html")
    clicked = await browser.click("#tab")
    assert clicked["navigated"] is True and clicked["new_tab"] is True
    assert clicked["url"] == site + "/second.html" and clicked["title"] == "Second Page"
    assert (await browser.read())["title"] == "Second Page"


async def test_evaluate(browser, site):
    await browser.open(site + "/index.html")
    assert (await browser.evaluate("1 + 2")) == {"value": 3, "url": site + "/index.html", "title": "Test Home", "navigated": False}
    assert (await browser.evaluate("new Promise(done => setTimeout(() => done({a: [1, 'x']}), 50))"))["value"] == {"a": [1, "x"]}
    assert (await browser.evaluate("await Promise.resolve(document.querySelectorAll('a').length)"))["value"] == 2
    assert (await browser.evaluate("undefined"))["value"] is None
    long = await browser.evaluate("'x'.repeat(9000)")
    assert long["truncated"] is True and len(long["value"]) == 8000
    with pytest.raises(RuntimeError, match="脚本出错.*nope"):
        await browser.evaluate("throw new Error('nope')")
    moved = await browser.evaluate("location.href = 'second.html'")
    assert moved["navigated"] is True and moved["url"] == site + "/second.html"
    alerted = await browser.evaluate("alert('hi'); 7")
    assert alerted["value"] == 7 and alerted["dialog"] == "hi"


async def test_screenshot_writes_a_png(browser, site, tmp_path):
    await browser.open(site + "/index.html")
    shot = await browser.screenshot()
    path = Path(shot["path"])
    assert path.parent == tmp_path / "browser" / "shots" and path.suffix == ".png"
    assert path.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and path.stat().st_size > 1000
    assert shot["width"] > 300 and shot["height"] > 300 and shot["title"] == "Test Home"
    assert (await browser.screenshot())["path"] != shot["path"]


async def test_refuses_other_schemes_and_bad_targets(browser, site):
    for url in ("file:///etc/passwd", "javascript:alert(1)", "chrome://settings", "ftp://example.com/x", "not a url"):
        with pytest.raises(ValueError, match="http"):
            await browser.open(url)
    await browser.open(site + "/index.html")
    with pytest.raises(ValueError, match="没有找到元素 e999.*dom"):
        await browser.click("e999")
    with pytest.raises(ValueError, match="e999"):
        await browser.type("e999", "x")
    with pytest.raises(ValueError, match="没有找到匹配 #absent"):
        await browser.click("#absent")
    with pytest.raises(ValueError, match="选择器"):
        await browser.click("a[[")
    with pytest.raises(ValueError, match="不可见"):
        await browser.click(".gone")
    with pytest.raises(ValueError, match="输入框"):
        await browser.type("#greet", "x")
    with pytest.raises(RuntimeError, match="打不开"):
        await browser.open("http://127.0.0.1:9/")


async def test_refuses_local_addresses_by_default(strict, site):
    for url in (site + "/index.html", "http://localhost/", "http://192.168.1.1/", "http://169.254.169.254/latest", "http://[::1]/"):
        with pytest.raises(ValueError, match="本机或内网"):
            await strict.open(url)
    moved = await strict.evaluate(f"location.href = '{site}/second.html'; 1")
    assert site in moved["blocked"] and moved["title"] != "Second Page"
    assert "You arrived" not in (await strict.read())["text"]


async def test_relaunches_after_the_browser_dies(browser, site):
    await browser.open(site + "/index.html")
    browser.proc.kill()
    browser.proc.wait()
    page = await browser.open(site + "/second.html")
    assert page["title"] == "Second Page"


async def test_close_is_safe_to_repeat_and_reopen(browser, site):
    await browser.close()
    assert (await browser.open(site + "/index.html"))["title"] == "Test Home"
    await browser.close()
    await browser.close()
