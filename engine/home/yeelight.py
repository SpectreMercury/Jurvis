"""Yeelight bulbs over their LAN control protocol (TCP 55443, one JSON message per line). No cloud account is involved.

"LAN Control" (局域网控制) has to be switched on for each bulb in the Yeelight app first. All socket I/O is in `send`, which tests
replace; a callable passed as the connector's transport is used instead of it.
"""
import asyncio
import json
import re

from . import Connector, ConnectorError, number, parse_color

PORT = 55443
PROPERTIES = ["power", "bright", "ct", "rgb", "color_mode", "name"]
EFFECT = ["smooth", 500]  # every setter takes (effect, duration in ms) after its value


async def send(host, port, payload):
    """Sends one command to a bulb and returns its reply ({"id", "result"} or {"id", "error"})."""
    reader, writer = await asyncio.wait_for(asyncio.open_connection(host, port), 5)
    try:
        writer.write((json.dumps(payload, separators=(",", ":")) + "\r\n").encode())
        await writer.drain()
        while True:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line:
                raise ConnectionError("bulb closed the connection")
            try:
                reply = json.loads(line)
            except ValueError:
                continue
            if isinstance(reply, dict) and reply.get("id") == payload["id"]:  # skips {"method": "props"} notifications
                return reply
    finally:
        writer.close()


def addresses(text):
    out = []
    for item in re.split(r"[,，;\s]+", str(text or "")):
        if item.strip():
            host, _, port = item.strip().partition(":")
            out.append((host, int(port) if port.isdigit() else PORT))
    return out


class Yeelight(Connector):
    kind = "yeelight"
    title = "Yeelight"
    description = "直接连接局域网里的 Yeelight 灯（局域网控制协议），不经过云端；需先在 Yeelight 应用里为每盏灯打开“局域网控制”。"
    fields = [{"name": "hosts", "label": "灯的地址", "placeholder": "192.168.1.50, 192.168.1.51（多个用逗号分隔）"}]

    async def call(self, native_id, method, params):
        host, _, port = native_id.partition(":")
        sender = self.transport if callable(self.transport) else send
        try:
            reply = await sender(host, int(port) if port.isdigit() else PORT, {"id": 1, "method": method, "params": params})
        except (OSError, asyncio.TimeoutError, EOFError) as exc:
            raise ConnectorError(f"连不上 Yeelight 灯 {host}：{type(exc).__name__}，请确认灯已通电、在同一网络，并在 Yeelight 应用里打开了“局域网控制”") from exc
        if not isinstance(reply, dict) or "result" not in reply:
            detail = ((reply or {}).get("error") or {}).get("message", "") if isinstance(reply, dict) else ""
            raise ConnectorError(f"Yeelight 灯 {host} 没有执行这个操作" + (f"（{detail}）" if detail else "") + "，这盏灯可能不支持它")
        return reply["result"]

    async def devices(self):
        targets = [host if port == PORT else f"{host}:{port}" for host, port in addresses(self.config.get("hosts"))]
        if not targets:
            raise ConnectorError("请填写至少一盏 Yeelight 灯的地址")
        results = await asyncio.gather(*(self.call(t, "get_prop", PROPERTIES) for t in targets), return_exceptions=True)
        if all(isinstance(r, BaseException) for r in results):
            raise results[0] if isinstance(results[0], ConnectorError) else ConnectorError(f"读取 Yeelight 灯失败：{type(results[0]).__name__}")
        out = []
        for target, result in zip(targets, results):
            if isinstance(result, BaseException):
                out.append(self.device(target, target, "light", online=False))
                continue
            # Values arrive as strings; a property the bulb does not have comes back as "".
            props = dict(zip(PROPERTIES, list(result) + [""] * len(PROPERTIES)))
            actions = ["turn_on", "turn_off", "toggle"]
            state = {"on": props["power"] == "on"}
            if str(props["bright"]).isdigit():
                actions.append("set_brightness")
                state["brightness"] = int(props["bright"])
            if str(props["rgb"]).isdigit():
                actions.append("set_color")
                if str(props["color_mode"]) == "1":
                    state["color"] = f"#{int(props['rgb']):06x}"
            if str(props["ct"]).isdigit():
                actions.append("set_color_temperature")
                if str(props["color_mode"]) == "2":
                    state["color_temperature"] = int(props["ct"])
            out.append(self.device(target, props["name"] or target, "light", state=state, actions=actions))
        return out

    async def control(self, native_id, action, value=None):
        on = ("set_power", ["on", *EFFECT])
        if action in ("turn_on", "turn_off"):
            commands = [("set_power", ["on" if action == "turn_on" else "off", *EFFECT])]
        elif action == "toggle":
            commands = [("toggle", [])]
        elif action == "set_brightness":
            level = round(number(value, 0, 100, "亮度"))
            commands = [on, ("set_bright", [level, *EFFECT])] if level > 0 else [("set_power", ["off", *EFFECT])]
        elif action == "set_color":
            r, g, b = parse_color(value)
            commands = [on, ("set_rgb", [(r << 16) | (g << 8) | b, *EFFECT])]
        elif action == "set_color_temperature":
            commands = [on, ("set_ct_abx", [round(number(value, 1700, 6500, "色温")), *EFFECT])]
        else:
            raise ConnectorError("Yeelight 灯不支持这个操作")
        for method, params in commands:  # the bulb only accepts brightness and colour changes while it is on
            await self.call(native_id, method, params)
