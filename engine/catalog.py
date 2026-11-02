"""Pinned, opt-in media components. Installing weights never executes remote code."""
import asyncio
import hashlib
import fcntl
import json
import shutil
import time
from pathlib import Path

import httpx


class MediaCatalog:
    def __init__(self, settings):
        self.settings = settings
        self.manifest = json.loads((settings.root / "config/media-catalog.json").read_text())

    def entry(self, ident):
        for item in self.manifest["components"]:
            if item["id"] == ident:
                return item
        raise ValueError("未知创作组件")

    def path(self, item):
        return self.settings.root / item["directory"] / item["filename"]

    def receipt(self, item):
        return self.settings.data / "components" / (item["id"] + ".json")

    def installed(self, item):
        path = self.path(item)
        try:
            receipt = json.loads(self.receipt(item).read_text())
            stat = path.stat()
            return (receipt["sha256"] == item["sha256"] and stat.st_size == item["bytes"]
                    and stat.st_mtime_ns == receipt["mtime_ns"])
        except (OSError, ValueError, KeyError):
            return False

    def require(self, ident):
        item = self.entry(ident)
        if not self.installed(item):
            raise ValueError(f"请先在设置中安装并校验 {item['title']}")
        return item

    def public(self):
        return [{**{k: v for k, v in x.items() if k != "download_url"},
                 "installed": self.installed(x)} for x in self.manifest["components"]]

    async def install(self, ident, progress=None):
        self.entry(ident)
        locks=self.settings.data/'component-locks';locks.mkdir(exist_ok=True)
        with (locks/(ident+'.lock')).open('a') as lock:
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:raise ValueError('该组件已经在安装，请等待完成')
            try:return await self._install(ident,progress)
            finally:fcntl.flock(lock,fcntl.LOCK_UN)

    async def _install(self, ident, progress=None):
        item = self.entry(ident)
        path = self.path(item)
        if self.installed(item):
            return {"component_id": ident, "sha256": item["sha256"], "reused": True}
        path.parent.mkdir(parents=True, exist_ok=True)
        partial = path.with_suffix(path.suffix + ".part")
        if not path.exists():
            offset = partial.stat().st_size if partial.exists() else 0
            if offset > item["bytes"]:
                raise ValueError("临时下载大小异常，请移走 .part 文件后重试")
            if shutil.disk_usage(path.parent).free < item["bytes"] - offset + 2 * 1024**3:
                raise ValueError("磁盘空间不足，需为下载预留额外 2GB")
            if offset != item["bytes"]:
                if item['bytes']>2*1024**3 and item['download_url'].startswith('https://huggingface.co/'):
                    await self._download_ranges(item,partial,offset,progress)
                else:
                    await self._download_stream(item,partial,offset,progress)
        target = path if path.exists() else partial
        if progress:
            await progress(.94, "正在校验 SHA-256")
        def digest():
            with target.open("rb") as f:
                return hashlib.file_digest(f, "sha256").hexdigest()
        actual = await asyncio.to_thread(digest)
        if target.stat().st_size != item["bytes"] or actual != item["sha256"]:
            raise ValueError("组件校验失败；原文件保留，未启用。请移走错误文件后重新下载")
        if target == partial:
            partial.replace(path)
        receipt = self.receipt(item)
        receipt.parent.mkdir(parents=True, exist_ok=True)
        receipt.write_text(json.dumps({"sha256": actual, "mtime_ns": path.stat().st_mtime_ns,
                                       "installed_at": time.time(), "source": item["source"]}))
        return {"component_id": ident, "sha256": actual}

    async def _download_stream(self,item,partial,offset,progress):
        async with httpx.AsyncClient(follow_redirects=True, timeout=httpx.Timeout(90, connect=30)) as client:
            headers = {"Range": f"bytes={offset}-"} if offset else {}
            async with client.stream("GET", item["download_url"], headers=headers) as response:
                if response.status_code in (401, 403):
                    raise ValueError("来源要求登录。请在组件来源页下载对应文件到显示的目录，再点击安装校验")
                response.raise_for_status()
                if not str(response.url).startswith("https://"):
                    raise ValueError("下载来源未使用 HTTPS")
                if response.status_code == 206:
                    if not response.headers.get("content-range", "").startswith(f"bytes {offset}-"):
                        raise ValueError("下载来源返回了错误的续传范围")
                else:
                    offset = 0
                count, last = offset, 0.0
                with partial.open("ab" if offset else "wb") as output:
                    async for chunk in response.aiter_bytes(1024 * 1024):
                        count += len(chunk)
                        if count > item["bytes"]:
                            raise ValueError("下载文件超过已核验的大小")
                        output.write(chunk)
                        if progress and time.monotonic() - last > 1:
                            await progress(count / item["bytes"] * .9, "正在下载 " + item["title"])
                            last = time.monotonic()

    async def _download_ranges(self,item,partial,offset,progress):
        # Four bounded 8MiB ranges, committed in order, keep cancellation/resume simple.
        chunk_size=8*1024**2
        async with httpx.AsyncClient(follow_redirects=True,timeout=httpx.Timeout(120,connect=30)) as client:
            async def segment(start,end):
                for attempt in range(3):
                    try:
                        async with client.stream('GET',item['download_url'],headers={'Range':f'bytes={start}-{end}'}) as response:
                            response.raise_for_status()
                            if response.url.scheme!='https':raise ValueError('下载来源未使用 HTTPS')
                            expected=f'bytes {start}-{end}/{item["bytes"]}'
                            if response.status_code!=206 or response.headers.get('content-range')!=expected:
                                raise ValueError('下载来源未正确响应分段请求，未写入数据')
                            data=bytearray()
                            async for part in response.aiter_bytes(1024*1024):
                                data.extend(part)
                                if len(data)>end-start+1:raise ValueError('分段下载超出预期大小')
                            if len(data)!=end-start+1:raise ValueError('分段下载不完整，请重试')
                            return data
                    except httpx.TransportError:
                        if attempt==2:raise
                        await asyncio.sleep(attempt+1)
            with partial.open('ab') as output:
                while offset<item['bytes']:
                    ranges=[(n,min(n+chunk_size-1,item['bytes']-1)) for n in range(offset,min(offset+4*chunk_size,item['bytes']),chunk_size)]
                    chunks=await asyncio.gather(*(segment(a,b) for a,b in ranges))
                    for chunk in chunks:output.write(chunk);offset+=len(chunk)
                    output.flush()
                    if progress:await progress(offset/item['bytes']*.9,'正在下载 '+item['title'])

    def validate_video(self, payload):
        if payload.get("model_profile") == "convrot_int8":
            self.require("h3_convrot_int8")
        if payload.get("sampling_profile") == "turbo_v2":
            self.require("h3_turbo_v2")
        if payload.get("sampling_profile") == "dasiwa_v33":
            self.require("dasiwa_v33")
        if payload.get("motion_strength", 0):
            self.require("h3_motion_fos")

