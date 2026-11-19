"""WLED controllers over their local JSON API (http://<address>/json). No account or token; WLED has no API authentication by default."""
import asyncio
import re

import httpx

from . import Connector, ConnectorError, number, parse_color

RESERVED = ("RSVD", "-")  # placeholder names WLED keeps for removed effects


def addresses(text):
    return [a.strip().removeprefix("http://").rstrip("/") for a in re.split(r"[,，;\s]+", str(text or "")) if a.strip()]


class Wled(Connector):
    kind = "wled"
    title = "WLED"
    description = "直接连接局域网里刷了 WLED 固件的灯带控制器（JSON API），不经过云端，也不需要账号。"
    fields = [{"name": "hosts", "label": "设备地址", "placeholder": "192.168.1.40, 192.168.1.41（多个用逗号分隔）"}]

    def client(self, host):
        return httpx.AsyncClient(base_url=f"http://{host}", timeout=10, transport=self.transport)

    @staticmethod
    def unreachable(host, exc):
        return ConnectorError(f"连不上 WLED 设备 {host}：{type(exc).__name__}，请确认它已通电并和这台电脑在同一网络")

    @staticmethod
    def check(response, host):
        if response.status_code >= 400:
            raise ConnectorError(f"WLED 设备 {host} 返回 {response.status_code}，请确认地址正确、固件支持 JSON API")
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError(f"{host} 返回的不是 WLED 的数据，请确认这个地址是 WLED 设备") from exc

    async def read(self, host):
        try:
            async with self.client(host) as client:
                return self.check(await client.get("/json"), host)
        except httpx.HTTPError as exc:
            raise self.unreachable(host, exc) from exc

    async def devices(self):
        hosts = addresses(self.config.get("hosts"))
        if not hosts:
            raise ConnectorError("请填写至少一个 WLED 设备地址")
        results = await asyncio.gather(*(self.read(h) for h in hosts), return_exceptions=True)
        if all(isinstance(r, BaseException) for r in results):
            raise results[0] if isinstance(results[0], ConnectorError) else ConnectorError(f"读取 WLED 设备失败：{type(results[0]).__name__}")
        out = []
        for host, data in zip(hosts, results):
            if isinstance(data, BaseException):
                out.append(self.device(host, host, "light", online=False))
                continue
            raw, info, effects = data.get("state") or {}, data.get("info") or {}, data.get("effects") or []
            segment = next((s for s in raw.get("seg") or [] if isinstance(s, dict)), {})
            state = {"on": raw.get("on"), "brightness": max(1, round(raw["bri"] / 255 * 100)) if isinstance(raw.get("bri"), (int, float)) else None,
                     "modes": [e for e in effects if e not in RESERVED]}
            color = (segment.get("col") or [None])[0]
            if isinstance(color, list) and len(color) >= 3:
                state["color"] = "#%02x%02x%02x" % tuple(color[:3])
            if isinstance(segment.get("fx"), int) and segment["fx"] < len(effects):
                state["mode"] = effects[segment["fx"]]
            actions = ["turn_on", "turn_off", "toggle", "set_brightness", "set_color"] + (["set_mode"] if state["modes"] else [])
            out.append(self.device(host, info.get("name") or host, "light", state=state, actions=actions))
        return out

    async def control(self, native_id, action, value=None):
        host = native_id
        if action in ("turn_on", "turn_off"):
            body = {"on": action == "turn_on"}
        elif action == "toggle":
            body = {"on": "t"}
        elif action == "set_brightness":
            level = number(value, 0, 100, "亮度")
            body = {"on": True, "bri": max(1, round(level * 255 / 100))} if level > 0 else {"on": False}
        elif action == "set_color":
            body = {"on": True, "seg": {"col": [list(parse_color(value))]}}
        elif action == "set_mode":
            body = None
        else:
            raise ConnectorError("WLED 不支持这个操作")
        try:
            async with self.client(host) as client:
                if action in ("set_color", "set_mode"):
                    # Apply to every segment, not just the first, so the whole strip changes together.
                    current = self.check(await client.get("/json"), host)
                    ids = [s["id"] for s in (current.get("state") or {}).get("seg") or [] if isinstance(s, dict) and "id" in s] or [0]
                    if action == "set_mode":
                        effects, wanted = current.get("effects") or [], str(value or "").strip().lower()
                        index = next((i for i, name in enumerate(effects) if name.lower() == wanted and name not in RESERVED), None)
                        if index is None:
                            raise ConnectorError(f"WLED 没有名为“{value}”的灯效，可用名称见设备状态里的 modes")
                        body = {"on": True, "seg": [{"id": i, "fx": index} for i in ids]}
                    else:
                        body = {"on": True, "seg": [{"id": i, "col": [list(parse_color(value))]} for i in ids]}
                self.check(await client.post("/json/state", json=body), host)
        except httpx.HTTPError as exc:
            raise self.unreachable(host, exc) from exc
