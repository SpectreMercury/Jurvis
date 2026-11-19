"""Shelly Gen2+ devices (Plus, Pro, Gen3, Gen4) over their local RPC API (http://<address>/rpc/<Method>). No cloud account is involved.

Covers the Switch, Light and Cover components. Devices with a password set use HTTP digest authentication, which this connector
does not do; first-generation Shelly devices speak a different API and are not supported either.
"""
import asyncio
import re

import httpx

from . import Connector, ConnectorError, number

COMPONENTS = {"switch": "switch", "light": "light", "cover": "cover"}


def addresses(text):
    return [a.strip().removeprefix("http://").rstrip("/") for a in re.split(r"[,，;\s]+", str(text or "")) if a.strip()]


class Shelly(Connector):
    kind = "shelly"
    title = "Shelly"
    description = "直接连接局域网里的 Shelly 第二代及以上设备（本地 RPC），支持开关、调光灯和窗帘；不经过云端，暂不支持设置了密码的设备。"
    fields = [{"name": "hosts", "label": "设备地址", "placeholder": "192.168.1.60, 192.168.1.61（多个用逗号分隔）"}]

    def client(self, host):
        return httpx.AsyncClient(base_url=f"http://{host}", timeout=10, transport=self.transport)

    async def rpc(self, client, host, method, **params):
        try:
            response = await client.get(f"/rpc/{method}", params={k: str(v).lower() if isinstance(v, bool) else v for k, v in params.items()} or None)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Shelly 设备 {host}：{type(exc).__name__}，请确认它已通电并和这台电脑在同一网络") from exc
        if response.status_code == 401:
            raise ConnectorError(f"Shelly 设备 {host} 设置了密码，目前不支持带密码的设备，请在设备网页里关闭认证后再试")
        if response.status_code == 404:
            raise ConnectorError(f"{host} 不支持 Shelly 第二代 RPC 接口，第一代 Shelly 设备暂不支持")
        if response.status_code >= 400:
            raise ConnectorError(f"Shelly 设备 {host} 没有执行这个操作（返回 {response.status_code}），窗帘类设备请确认已完成校准")
        try:
            return response.json() if response.content.strip() else None
        except ValueError as exc:
            raise ConnectorError(f"{host} 返回的不是 Shelly 的数据，请确认这个地址是 Shelly 设备") from exc

    async def read(self, host):
        async with self.client(host) as client:
            return await self.rpc(client, host, "Shelly.GetStatus"), await self.rpc(client, host, "Shelly.GetConfig")

    async def devices(self):
        hosts = addresses(self.config.get("hosts"))
        if not hosts:
            raise ConnectorError("请填写至少一个 Shelly 设备地址")
        results = await asyncio.gather(*(self.read(h) for h in hosts), return_exceptions=True)
        if all(isinstance(r, BaseException) for r in results):
            raise results[0] if isinstance(results[0], ConnectorError) else ConnectorError(f"读取 Shelly 设备失败：{type(results[0]).__name__}")
        out = []
        for host, result in zip(hosts, results):
            if isinstance(result, BaseException):
                continue  # its components are unknown while it cannot be reached
            status, config = result
            base = ((config.get("sys") or {}).get("device") or {}).get("name") or host
            keys = [k for k in status if k.split(":")[0] in COMPONENTS and isinstance(status[k], dict)]
            for key in keys:
                component, item = key.split(":")[0], status[key]
                name = (config.get(key) or {}).get("name") or (base if len(keys) == 1 else f"{base} {key}")
                if component == "cover":
                    position = item.get("current_pos")
                    state = {"state": item.get("state"), "position": position}
                    actions = ["open", "close"] + (["set_position"] if item.get("pos_control") else [])
                else:
                    state = {"on": item.get("output"), "power": item.get("apower")}
                    actions = ["turn_on", "turn_off", "toggle"]
                    if component == "light":
                        state["brightness"] = round(item["brightness"]) if isinstance(item.get("brightness"), (int, float)) else None
                        actions.append("set_brightness")
                out.append(self.device(f"{host}/{key}", name, COMPONENTS[component], state=state, actions=actions))
        return out

    async def control(self, native_id, action, value=None):
        host, _, key = native_id.rpartition("/")
        component, _, index = key.partition(":")
        if not host or component not in COMPONENTS or not index.isdigit():
            raise ConnectorError("这不是一个 Shelly 设备 ID，请刷新设备列表")
        ident, service = int(index), component.capitalize()
        if component == "cover":
            if action in ("open", "close"):
                method, params = f"Cover.{action.capitalize()}", {}
            elif action == "set_position":
                method, params = "Cover.GoToPosition", {"pos": round(number(value, 0, 100, "位置"))}
            else:
                raise ConnectorError("Shelly 窗帘只支持 open、close 和 set_position")
        elif action in ("turn_on", "turn_off"):
            method, params = f"{service}.Set", {"on": action == "turn_on"}
        elif action == "toggle":
            method, params = f"{service}.Toggle", {}
        elif action == "set_brightness" and component == "light":
            level = round(number(value, 0, 100, "亮度"))
            method, params = "Light.Set", {"on": True, "brightness": level} if level > 0 else {"on": False}
        else:
            raise ConnectorError("这个 Shelly 设备不支持这个操作")
        async with self.client(host) as client:
            await self.rpc(client, host, method, id=ident, **params)
