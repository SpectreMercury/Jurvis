"""Reading web pages for the assistant.

Search itself is done by the cloud provider (Miavo serves it with the model).
This module fetches one public page and turns it into text. Private and
loopback addresses are refused so a page cannot steer the assistant into the
user's own network.
"""
import asyncio
import ipaddress
import re
import socket
from html.parser import HTMLParser
from urllib.parse import urljoin, urlsplit

import httpx

MAX_BYTES = 2_000_000
SKIP = {"script", "style", "noscript", "svg", "template", "iframe", "head"}
BLOCK = {"p", "div", "br", "li", "tr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "article", "blockquote", "pre", "table"}


class Text(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts, self.title, self.depth, self.in_title = [], "", 0, False

    def handle_starttag(self, tag, attrs):
        if tag in SKIP and tag != "head":
            self.depth += 1
        elif tag == "title":
            self.in_title = True
        elif tag in BLOCK:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if tag in SKIP and tag != "head":
            self.depth = max(0, self.depth - 1)
        elif tag == "title":
            self.in_title = False
        elif tag in BLOCK:
            self.parts.append("\n")

    def handle_data(self, data):
        if self.in_title:
            self.title += data
        elif not self.depth:
            self.parts.append(data)


def html_text(html):
    parser = Text()
    parser.feed(html)
    text = re.sub(r"[ \t\r\f\v]+", " ", "".join(parser.parts))
    text = re.sub(r" ?\n ?", "\n", text)
    return parser.title.strip()[:200], re.sub(r"\n{3,}", "\n\n", text).strip()


PLACEHOLDER = ipaddress.ip_network("198.18.0.0/15")


async def check_public(url):
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("只能读取 http 或 https 网址")
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parts.hostname, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
    except socket.gaierror as exc:
        raise ValueError(f"找不到网站 {parts.hostname}") from exc
    try:
        ipaddress.ip_address(parts.hostname)
        named = False
    except ValueError:
        named = True
    for info in infos:
        address = ipaddress.ip_address(info[4][0])
        # A proxy in fake-IP mode (Clash, Surge) answers every name with a placeholder from 198.18.0.0/15 and resolves the real name itself.
        # That is still a public site, so a name may resolve there; the address typed directly may not.
        if not address.is_global and not (named and address in PLACEHOLDER):
            raise ValueError("不读取本机或内网地址")


async def fetch(url, limit=20000):
    """{"url", "title", "content"} for a public page; raises ValueError with a reason the model can relay."""
    headers = {"User-Agent": "Mozilla/5.0 (compatible; assistant-fetch)", "Accept": "text/html,text/plain,application/json;q=0.9,*/*;q=0.5"}
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(25, connect=10), follow_redirects=False, trust_env=False) as client:
            for _ in range(6):
                await check_public(url)
                async with client.stream("GET", url, headers=headers) as response:
                    if response.status_code in (301, 302, 303, 307, 308) and response.headers.get("location"):
                        url = urljoin(url, response.headers["location"])
                        continue
                    if response.status_code != 200:
                        raise ValueError(f"网站返回 {response.status_code}")
                    kind = response.headers.get("content-type", "").split(";")[0].strip().lower()
                    if not (kind.startswith("text/") or kind in ("application/json", "application/xml", "application/xhtml+xml", "")):
                        raise ValueError(f"不是可读的文本页面（{kind}）")
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        body.extend(chunk)
                        if len(body) > MAX_BYTES:
                            break
                    text = bytes(body[:MAX_BYTES]).decode(response.encoding or "utf-8", errors="replace")
                    break
            else:
                raise ValueError("跳转次数过多")
    except httpx.HTTPError as exc:
        raise ValueError(f"读取失败：{type(exc).__name__}") from exc
    title, content = html_text(text) if "html" in kind or text.lstrip()[:15].lower().startswith(("<!doctype", "<html")) else ("", text.strip())
    result = {"url": url, "title": title, "content": content[:limit]}
    if len(content) > limit:
        result["note"] = f"Page truncated: showing {limit} of {len(content)} characters."
    return result
