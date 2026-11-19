"""A real browser the assistant can drive: open, read, list elements, click, type, screenshot, run scripts.

It launches an installed Chromium-family browser with its own profile (never the
user's) and talks to it over the Chrome DevTools Protocol. Private and loopback
addresses are refused the same way web_fetch refuses them, for every document the
page loads, so a page cannot walk the assistant into the user's own network.
"""
import asyncio
import base64
import itertools
import json
import re
import struct
import subprocess
import time
from pathlib import Path
from urllib.parse import urlsplit

from websockets.asyncio.client import connect
from websockets.exceptions import WebSocketException

from .web import check_public

APPS = ("Google Chrome.app/Contents/MacOS/Google Chrome", "Chromium.app/Contents/MacOS/Chromium", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "Brave Browser.app/Contents/MacOS/Brave Browser")
REF = re.compile(r"e\d+")
LOAD_SECONDS, STEP_SECONDS, SETTLE_SECONDS = 20, 8, 6
OPEN_TEXT, READ_TEXT, VALUE_CHARS, ELEMENTS = 3000, 20000, 8000, 150
# Where a click or script may leave the page; anything else (file:, chrome:) is undone.
LANDING = {"http", "https", "about", "data", "blob", "chrome-error"}

# Runs inside the page. Refs are stamped on the elements so the next click or type finds them again.
PAGE = r"""(() => {
  const SKIP = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEMPLATE", "SVG", "IFRAME", "HEAD", "CANVAS", "VIDEO", "AUDIO", "OBJECT"]);
  const BLOCK = new Set(["P", "DIV", "SECTION", "ARTICLE", "MAIN", "HEADER", "FOOTER", "NAV", "ASIDE", "UL", "OL", "TABLE", "TR", "BLOCKQUOTE", "PRE", "FORM", "FIGURE", "DL", "DT", "DD", "DETAILS", "SUMMARY", "FIELDSET", "HR"]);
  const INTERACTIVE = 'a[href],button,input:not([type=hidden]),select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=option],[role=combobox],[role=textbox],[contenteditable=""],[contenteditable=true],[onclick]';
  const FIELD = ["input", "select", "textarea"];
  const clean = (text, limit) => (text || "").replace(/\s+/g, " ").trim().slice(0, limit);
  const tagOf = el => el.tagName.toLowerCase();
  const kind = el => (el.getAttribute("type") || "").toLowerCase();
  let room = 0;
  const walk = node => {
    if (room <= 0) return "";
    if (node.nodeType === 3) { room -= node.data.length; return node.data.replace(/\s+/g, " "); }
    if (node.nodeType !== 1) return "";
    const tag = node.tagName.toUpperCase();
    if (SKIP.has(tag) || node.getAttribute("aria-hidden") === "true") return "";
    const style = getComputedStyle(node);
    if (style.display === "none" || style.visibility === "hidden") return "";
    if (tag === "BR") return "\n";
    if (tag === "IMG") return clean(node.alt, 80);
    let inner = "";
    for (const child of node.childNodes) inner += walk(child);
    const text = inner.trim();
    if (/^H[1-6]$/.test(tag)) return text ? `\n\n${"#".repeat(+tag[1])} ${text}\n\n` : "";
    if (tag === "A") return text && /^https?:/.test(node.href) ? `[${text.replace(/\s*\n\s*/g, " ")}](${node.href})` : inner;
    if (tag === "LI") return `\n- ${text}`;
    if (tag === "TD" || tag === "TH") return `${text} | `;
    return BLOCK.has(tag) ? `\n\n${text}\n\n` : inner;
  };
  const role = el => {
    const tag = tagOf(el);
    if (tag === "a") return "link";
    if (tag === "input") return ["button", "submit", "reset", "image"].includes(kind(el)) ? "button" : ["checkbox", "radio"].includes(kind(el)) ? kind(el) : "input";
    if (tag === "button" || FIELD.includes(tag)) return tag;
    return el.getAttribute("role") || (el.isContentEditable ? "textbox" : tag);
  };
  const label = el => {
    if (role(el) === "button" && tagOf(el) === "input") return clean(el.value || el.getAttribute("aria-label") || el.alt || kind(el), 80);
    if (FIELD.includes(tagOf(el))) return clean(el.getAttribute("aria-label") || (el.labels && el.labels[0] && el.labels[0].innerText) || el.title, 80);
    return clean(el.innerText || el.getAttribute("aria-label") || el.title || (el.querySelector("img[alt]") || {}).alt, 80);
  };
  const describe = el => {
    const item = {role: role(el), text: label(el)};
    for (const name of ["name", "placeholder"]) if (el.getAttribute(name)) item[name] = clean(el.getAttribute(name), 80);
    if (tagOf(el) === "a") item.href = el.href.slice(0, 300);
    if (FIELD.includes(tagOf(el)) && item.role !== "button") {
      if (kind(el) && !["text", "checkbox", "radio"].includes(kind(el))) item.type = kind(el);
      if (el.checked) item.checked = true;
      if (el.value && kind(el) !== "password") item.value = clean(el.value, 120);
    }
    return item;
  };
  const what = el => clean(`${role(el)} ${label(el) || el.getAttribute("placeholder") || el.getAttribute("name") || ""}`, 100);
  const find = target => {
    try { return {el: document.querySelector(/^e\d+$/.test(target) ? `[data-jurvis-ref="${target}"]` : target)}; }
    catch (error) { return {error: "selector"}; }
  };
  const located = target => {
    const found = find(target);
    if (found.el) found.el.scrollIntoView({block: "center", inline: "center", behavior: "instant"});
    return found.el ? found : {error: found.error || "missing"};
  };
  const fire = el => ["input", "change"].forEach(name => el.dispatchEvent(new Event(name, {bubbles: true})));
  return {
    state: () => ({url: location.href, title: document.title, ready: document.readyState}),
    read: () => { room = 200000; return walk(document.body || document.documentElement); },
    list: (start, limit) => {
      document.querySelectorAll("[data-jurvis-ref]").forEach(el => el.removeAttribute("data-jurvis-ref"));
      const elements = [];
      let more = false;
      for (const el of document.querySelectorAll(INTERACTIVE)) {
        const box = el.getBoundingClientRect();
        if (el.disabled || box.width < 1 || box.height < 1 || getComputedStyle(el).visibility === "hidden") continue;
        if (elements.length >= limit) { more = true; break; }
        const ref = `e${start + elements.length + 1}`;
        el.setAttribute("data-jurvis-ref", ref);
        elements.push({ref, ...describe(el)});
      }
      return {elements, more};
    },
    point: target => {
      const {el, error} = located(target);
      if (error) return {error};
      const box = el.getClientRects()[0] || el.getBoundingClientRect();
      if (box.width < 1 || box.height < 1) return {error: "hidden"};
      const x = Math.min(Math.max(box.left + box.width / 2, 1), innerWidth - 1), y = Math.min(Math.max(box.top + box.height / 2, 1), innerHeight - 1);
      const top = document.elementFromPoint(x, y);
      const covered = top && top !== el && !el.contains(top) && !top.contains(el);
      return {x, y, what: what(el), select: tagOf(el) === "select", ...(covered ? {covered: what(top)} : {})};
    },
    focus: target => {
      const {el, error} = located(target);
      if (error) return {error};
      if (tagOf(el) === "select") return {what: what(el), select: true};
      const typed = tagOf(el) === "textarea" || (tagOf(el) === "input" && !["button", "submit", "reset", "image", "checkbox", "radio", "file", "range", "color"].includes(kind(el)));
      if (!typed && !el.isContentEditable) return {error: "field"};
      el.focus();
      if (typed) el.select(); else getSelection().selectAllChildren(el);
      return {what: what(el), filled: !!(typed ? el.value : el.innerText.trim())};
    },
    // Date and similar inputs ignore inserted text; set the value the way a framework would notice.
    confirm: (target, text) => {
      const el = find(target).el;
      if (!el || !("value" in el) || el.value === text) return;
      Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), "value").set.call(el, text);
      fire(el);
    },
    choose: (target, text) => {
      const el = find(target).el, wanted = text.trim().toLowerCase();
      const options = [...el.options], name = option => option.text.trim();
      const option = options.find(o => o.value === text || name(o).toLowerCase() === wanted) || options.find(o => wanted && name(o).toLowerCase().includes(wanted));
      if (!option) return {error: "option", options: options.slice(0, 30).map(name)};
      el.value = option.value;
      fire(el);
      return {chosen: name(option)};
    },
    viewport: () => ({x: scrollX, y: scrollY, width: innerWidth, height: innerHeight, scale: 1 / (devicePixelRatio || 1)}),
  };
})()"""


def installed():
    """Path of the first Chromium-family browser found on this Mac, or None."""
    for base in (Path("/Applications"), Path.home() / "Applications"):
        for app in APPS:
            if (base / app).is_file():
                return base / app
    return None


def tidy(text):
    text = re.sub(r"[ \t\r\f\v ]+", " ", text)
    text = re.sub(r" ?\n ?", "\n", text)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class Browser:
    def __init__(self, settings, headless=False, allow_local=False):
        self.root = Path(settings.data) / "browser"
        self.headless, self.allow_local = headless, allow_local
        self.proc = self.ws = self.session = None
        self.pages = {}  # CDP session id -> {"target", "ready", "loading", "fresh"} for every open tab
        self.waiting = {}  # command id -> Future of its reply
        self.ids = itertools.count(1)
        self.refs = 0  # refs never repeat, so one from an earlier page cannot hit a different element
        self.notes = {}  # what happened behind the current call: a blocked address, a dialog
        self.tasks = set()
        self.lock = asyncio.Lock()

    def available(self):
        return installed() is not None

    # The connection

    async def send(self, method, params=None, session=None, timeout=STEP_SECONDS):
        if not self.ws:
            raise RuntimeError("浏览器连接已断开，请重试")
        number = next(self.ids)
        self.waiting[number] = future = asyncio.get_running_loop().create_future()
        try:
            await self.ws.send(json.dumps({"id": number, "method": method, "params": params or {}, **({"sessionId": session} if session else {})}))
            return await asyncio.wait_for(future, timeout)
        finally:
            self.waiting.pop(number, None)

    async def _listen(self, ws):
        try:
            async for raw in ws:
                message = json.loads(raw)
                future = self.waiting.get(message.get("id"))
                if future and not future.done():
                    if "error" in message:
                        future.set_exception(RuntimeError(message["error"].get("message", "浏览器拒绝了这个操作")))
                    else:
                        future.set_result(message.get("result", {}))
                elif "method" in message:
                    self._event(message["method"], message.get("params", {}), message.get("sessionId"))
        except (WebSocketException, OSError):
            pass
        finally:
            if self.ws is ws:
                self.ws = self.session = None
                self.pages.clear()
            for future in self.waiting.values():
                if not future.done():
                    future.set_exception(RuntimeError("浏览器已经关闭，请重试"))

    def _spawn(self, work):
        async def quiet():
            try:
                await work
            except (RuntimeError, asyncio.TimeoutError, WebSocketException, OSError):
                pass  # the tab went away while we were answering it
        task = asyncio.get_running_loop().create_task(quiet())
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    def _event(self, method, params, session):
        page = self.pages.get(session)
        if method == "Target.attachedToTarget":
            self._spawn(self._attached(params["sessionId"], params["targetInfo"]))
        elif method == "Target.detachedFromTarget":
            self.pages.pop(params["sessionId"], None)
            if self.session == params["sessionId"]:
                self.session = next(reversed(self.pages), None)
        elif method in ("Page.frameStartedLoading", "Page.frameStoppedLoading") and page and params.get("frameId") == page["target"]:
            page["loading"], page["fresh"] = method == "Page.frameStartedLoading", False
        elif method == "Page.javascriptDialogOpening":
            self.notes["dialog"] = params.get("message", "")[:300]
            self._spawn(self.send("Page.handleJavaScriptDialog", {"accept": params.get("type") in ("alert", "beforeunload")}, session))
        elif method == "Fetch.requestPaused":
            self._spawn(self._gate(session, params))

    async def _attached(self, session, info):
        """Every new tab starts paused; it gets the address gate before it loads anything, then becomes the driven page."""
        if info.get("type") != "page" or info.get("url", "").startswith("devtools://"):
            await self.send("Runtime.runIfWaitingForDebugger", session=session)
            return await self.send("Target.detachFromTarget", {"sessionId": session})
        # A tab the page opened has its first address still to load, and its loading events may be missed.
        self.pages[session] = page = {"target": info["targetId"], "ready": False, "loading": False, "fresh": bool(info.get("openerId"))}
        self.session = session
        try:
            # In this order: a paused tab stalls if Page is enabled before it is resumed.
            await self.send("Fetch.enable", {"patterns": [{"resourceType": "Document"}]}, session)
            await self.send("Runtime.runIfWaitingForDebugger", session=session)
            await self.send("Page.enable", session=session)
        finally:
            page["ready"] = True

    async def _check(self, url):
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.hostname:
            raise ValueError("只能打开 http 或 https 网址")
        if not self.allow_local:
            await check_public(url)

    async def _gate(self, session, params):
        """Decides each document request (navigations, redirects, frames) before it leaves the browser."""
        url = params["request"]["url"]
        try:
            if urlsplit(url).scheme in ("http", "https"):
                await self._check(url)
        except ValueError:
            self.notes["blocked"] = url
            return await self.send("Fetch.failRequest", {"requestId": params["requestId"], "errorReason": "BlockedByClient"}, session)
        await self.send("Fetch.continueRequest", {"requestId": params["requestId"]}, session)

    # The process

    def _endpoint(self):
        try:
            port, path = (self.root / "profile" / "DevToolsActivePort").read_text().split()[:2]
            return f"ws://127.0.0.1:{int(port)}{path}"
        except (OSError, ValueError):
            return None

    async def _connect(self):
        endpoint = self._endpoint()
        if not endpoint:
            return False
        try:
            ws = await connect(endpoint, max_size=None, open_timeout=3, close_timeout=2, ping_interval=None, proxy=None)
        except (WebSocketException, OSError, asyncio.TimeoutError):
            return False
        self.ws = ws
        self._spawn(self._listen(ws))
        await self.send("Target.setAutoAttach", {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        # Nothing the assistant clicks should land a file on the Mac unasked.
        await self.send("Browser.setDownloadBehavior", {"behavior": "deny"})
        return True

    async def _start(self):
        """Joins the browser a previous engine run left open on this profile, or launches one."""
        if await self._connect():
            return
        binary, profile = installed(), self.root / "profile"
        if not binary:
            raise RuntimeError("没有找到可用的浏览器，请先安装 Google Chrome")
        profile.mkdir(parents=True, exist_ok=True)
        (profile / "DevToolsActivePort").unlink(missing_ok=True)
        flags = ["--remote-debugging-port=0", "--no-first-run", "--no-default-browser-check", "--window-size=1280,900", "--disable-backgrounding-occluded-windows", "--disable-renderer-backgrounding"]
        self.proc = subprocess.Popen([str(binary), f"--user-data-dir={profile}", *flags, *(["--headless=new"] if self.headless else []), "about:blank"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(150):
            await asyncio.sleep(.1)
            if await self._connect():
                return
            if self.proc.poll() is not None:
                break
        self._kill()
        raise RuntimeError("浏览器启动失败；如果有一个 Jurvis 浏览器窗口卡住了，请先把它退出再试")

    def _kill(self):
        if self.proc and self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait()
        self.proc = None

    async def _drop(self):
        ws, self.ws, self.session = self.ws, None, None
        self.pages.clear()
        if ws:
            await ws.close()
        self._kill()

    async def _ensure(self):
        try:
            await self.send("Browser.getVersion", timeout=3)
        except (RuntimeError, asyncio.TimeoutError, WebSocketException, OSError):
            await self._drop()  # never started, quit by the user, or hung: start over
            await self._start()
        if self.session not in self.pages:
            self.session = next(reversed(self.pages), None)
        if not self.session:
            await self.send("Target.createTarget", {"url": "about:blank"})
        while not (self.session and self.pages.get(self.session, {}).get("ready")):
            await asyncio.sleep(.05)

    async def _do(self, work, timeout=STEP_SECONDS + SETTLE_SECONDS):
        """One call at a time, never longer than its timeout: a hung page must not hang the agent."""
        async with self.lock:
            try:
                await asyncio.wait_for(self._ensure(), LOAD_SECONDS)
                self.notes = {}
                return await asyncio.wait_for(work(), timeout)
            except asyncio.TimeoutError:
                raise RuntimeError("浏览器操作超时，页面可能卡住了；可以重新 open 这个网址再试") from None
            except (WebSocketException, OSError) as exc:
                raise RuntimeError(f"浏览器连接出错：{type(exc).__name__}，请重试") from exc

    async def close(self):
        async with self.lock:
            if self.ws:
                try:
                    await asyncio.wait_for(self.ws.send(json.dumps({"id": next(self.ids), "method": "Browser.close"})), 3)
                    if self.proc:
                        await asyncio.wait_for(asyncio.to_thread(self.proc.wait), 5)
                except (WebSocketException, OSError, asyncio.TimeoutError):
                    pass
            await self._drop()
            for task in list(self.tasks):
                task.cancel()

    # The page

    async def _page(self, name, *args):
        """Calls one of the PAGE helpers in the driven tab."""
        result = await self.send("Runtime.evaluate", {"expression": f"{PAGE}.{name}({', '.join(json.dumps(arg) for arg in args)})", "returnByValue": True}, self.session)
        if "exceptionDetails" in result:
            raise RuntimeError("页面脚本出错：" + self._thrown(result))
        return result["result"].get("value")

    async def _script(self, script, **more):
        return await self.send("Runtime.evaluate", {"expression": script, "returnByValue": True, "awaitPromise": True, "userGesture": True, "timeout": STEP_SECONDS * 1000, **more}, self.session, timeout=STEP_SECONDS + 2)

    @staticmethod
    def _thrown(result):
        details = result.get("exceptionDetails", {})
        return str(details.get("exception", {}).get("description") or details.get("text", ""))[:500]

    async def _state(self):
        if not self.session:
            return {"url": "", "title": "", "ready": "complete"}
        return await self._page("state")

    async def _settle(self, seconds):
        """Waits out a navigation the last action may have started; False when the page is still loading."""
        await asyncio.sleep(.3)
        start = time.monotonic()
        while time.monotonic() - start < seconds:
            page = self.pages.get(self.session)
            if not self.pages:
                return True
            if page and page["ready"] and not page["loading"]:
                try:
                    state = await self._state()
                except RuntimeError:
                    state = {"ready": ""}  # the old document is being replaced
                blank = page["fresh"] and state.get("url") in ("", "about:blank") and time.monotonic() - start < 2
                if state["ready"] == "complete" and not blank:
                    return True
            await asyncio.sleep(.1)
        return False

    async def _receipt(self, before, result):
        """Adds where the page ended up, so the model sees a navigation its action caused."""
        loaded = await self._settle(SETTLE_SECONDS)
        state = await self._state()
        if state["url"] != before["url"] and urlsplit(state["url"]).scheme not in LANDING:
            await self.send("Page.navigate", {"url": "about:blank"}, self.session)
            raise ValueError("页面跳到了不允许的地址（只能访问 http 或 https 网页），已退回空白页")
        result.update(url=state["url"], title=state["title"], navigated=state["url"] != before["url"] or self.session != before["tab"])
        if self.session != before["tab"]:
            result["new_tab"] = True
        if not loaded:
            result["loading"] = True
        if "blocked" in self.notes:
            result["blocked"] = f"已拦截对本机或内网地址的访问：{self.notes['blocked']}"
        if "dialog" in self.notes:
            result["dialog"] = self.notes["dialog"]
        return result

    async def _before(self):
        return {"url": (await self._state())["url"], "tab": self.session}

    def _missing(self, target, error):
        if error == "selector":
            return ValueError(f"选择器写法有误：{target}")
        if error == "hidden":
            return ValueError(f"元素 {target} 现在不可见，无法操作")
        if error == "field":
            return ValueError(f"元素 {target} 不是可以输入文字的输入框")
        if REF.fullmatch(target):
            return ValueError(f"没有找到元素 {target}，页面可能已经变化，请先调用 dom 重新列出元素")
        return ValueError(f"没有找到匹配 {target} 的元素，请先调用 dom 查看页面上有哪些元素")

    async def _key(self, key, code, text=None):
        event = {"key": key, "code": key, "windowsVirtualKeyCode": code, "nativeVirtualKeyCode": code}
        await self.send("Input.dispatchKeyEvent", {"type": "keyDown" if text else "rawKeyDown", **event, **({"text": text} if text else {})}, self.session)
        await self.send("Input.dispatchKeyEvent", {"type": "keyUp", **event}, self.session)

    async def _text(self):
        return tidy(await self._page("read") or "")

    async def open(self, url):
        url = str(url).strip()

        async def work():
            await self._check(url)
            started = time.monotonic()
            result = await self.send("Page.navigate", {"url": url}, self.session, timeout=LOAD_SECONDS)
            if "blocked" in self.notes:
                raise ValueError("这个网址跳转到了本机或内网地址，已拦截")
            if result.get("errorText"):
                raise RuntimeError(f"打不开 {url}：{result['errorText']}")
            if not await self._settle(max(1, LOAD_SECONDS - (time.monotonic() - started))):
                await self.send("Page.stopLoading", session=self.session)
            state, text = await self._state(), await self._text()
            return {"url": state["url"], "title": state["title"], "text": text[:OPEN_TEXT], **({"truncated": True} if len(text) > OPEN_TEXT else {})}
        return await self._do(work, LOAD_SECONDS + STEP_SECONDS)

    async def read(self):
        async def work():
            state, text = await self._state(), await self._text()
            return {"url": state["url"], "title": state["title"], "text": text[:READ_TEXT], **({"truncated": True} if len(text) > READ_TEXT else {})}
        return await self._do(work)

    async def dom(self):
        async def work():
            state, found = await self._state(), await self._page("list", self.refs, ELEMENTS)
            self.refs += len(found["elements"])
            return {"url": state["url"], "title": state["title"], "elements": found["elements"], **({"truncated": True} if found["more"] else {})}
        return await self._do(work)

    async def click(self, target):
        target = str(target).strip()

        async def work():
            before, spot = await self._before(), await self._page("point", target)
            if spot.get("error"):
                raise self._missing(target, spot["error"])
            if spot["select"]:
                raise ValueError("下拉框不能用 click 选择，请用 type 并把 text 设为要选的选项")
            mouse = {"x": spot["x"], "y": spot["y"], "button": "left", "clickCount": 1}
            await self.send("Input.dispatchMouseEvent", {"type": "mouseMoved", "x": spot["x"], "y": spot["y"]}, self.session)
            await self.send("Input.dispatchMouseEvent", {"type": "mousePressed", **mouse}, self.session)
            await self.send("Input.dispatchMouseEvent", {"type": "mouseReleased", **mouse}, self.session)
            return await self._receipt(before, {"clicked": spot["what"], **({"covered_by": spot["covered"]} if "covered" in spot else {})})
        return await self._do(work)

    async def type(self, target, text, submit=False):
        target, text = str(target).strip(), str(text)

        async def work():
            before, field = await self._before(), await self._page("focus", target)
            if field.get("error"):
                raise self._missing(target, field["error"])
            if field.get("select"):
                chosen = await self._page("choose", target, text)
                if chosen.get("error"):
                    raise ValueError(f"下拉框里没有“{text}”，可选：{'、'.join(chosen['options'])}")
            else:
                if field["filled"]:
                    await self._key("Backspace", 8)
                if text:
                    await self.send("Input.insertText", {"text": text}, self.session)
                await self._page("confirm", target, text)
            if submit and not field.get("select"):
                await self._key("Enter", 13, "\r")
                return await self._receipt(before, {"typed": len(text), "into": field["what"]})
            state = await self._state()
            return {"typed": len(text), "into": field["what"], "url": state["url"], "title": state["title"], "navigated": state["url"] != before["url"]}
        return await self._do(work)

    async def screenshot(self):
        async def work():
            state, clip = await self._state(), await self._page("viewport")
            shot = await self.send("Page.captureScreenshot", {"format": "png", "clip": clip}, self.session)
            data = base64.b64decode(shot["data"])
            path = self.root / "shots" / f"{time.strftime('%Y%m%d-%H%M%S')}-{int(time.time() * 1000) % 1000:03d}.png"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(data)
            width, height = struct.unpack(">II", data[16:24])
            return {"path": str(path), "width": width, "height": height, "url": state["url"], "title": state["title"]}
        return await self._do(work)

    async def evaluate(self, script):
        async def work():
            before, value = await self._before(), None
            try:
                result = await self._script(str(script))
                if "await is only valid" in self._thrown(result):
                    result = await self._script(str(script), replMode=True)
            except RuntimeError as exc:
                if not re.search(r"context was destroyed|navigated or closed|Cannot find context", str(exc)):
                    raise RuntimeError(f"脚本没有执行成功：{exc}") from exc
                result = {"result": {}}  # the script itself navigated away before it could return
            if "exceptionDetails" in result:
                raise RuntimeError("脚本出错：" + self._thrown(result))
            value = result["result"].get("value")
            shown = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            cut = {"value": shown[:VALUE_CHARS], "truncated": True} if len(shown) > VALUE_CHARS else {"value": value}
            return await self._receipt(before, cut)
        return await self._do(work, 2 * STEP_SECONDS + SETTLE_SECONDS)
