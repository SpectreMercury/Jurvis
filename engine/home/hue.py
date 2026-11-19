"""Philips Hue, talking to the bridge on the local network (CLIP API v2). No cloud account is involved."""
import colorsys

import httpx

from . import Connector, ConnectorError, number, parse_color


def rgb_to_xy(r, g, b):
    """sRGB to the CIE xy point Hue lights take (wide-gamut D65 conversion from Philips' developer notes)."""
    def linear(c):
        c /= 255
        return ((c + 0.055) / 1.055) ** 2.4 if c > 0.04045 else c / 12.92
    r, g, b = linear(r), linear(g), linear(b)
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505
    total = x + y + z
    return (round(x / total, 4), round(y / total, 4)) if total else (0.3127, 0.329)


class Hue(Connector):
    kind = "hue"
    title = "Philips Hue"
    description = "直接连接局域网里的 Hue 桥接器，不经过云端。"
    fields = [{"name": "host", "label": "桥接器地址", "placeholder": "192.168.1.20"},
              {"name": "key", "label": "应用密钥", "secret": True, "placeholder": "按下桥接器按钮后创建的 username"}]

    def client(self):
        # The bridge serves a certificate signed by Signify's private CA for its bridge ID, not for its address, so it cannot be verified by hostname.
        options = {"transport": self.transport} if self.transport else {"verify": False}
        return httpx.AsyncClient(base_url=f"https://{self.config['host']}", timeout=15, headers={"hue-application-key": self.config["key"]}, **options)

    async def get(self, client, kind):
        response = await client.get(f"/clip/v2/resource/{kind}")
        if response.status_code in (401, 403):
            raise ConnectorError("Hue 桥接器拒绝了应用密钥")
        if response.status_code >= 400:
            raise ConnectorError(f"Hue 桥接器返回 {response.status_code}")
        return response.json().get("data") or []

    async def devices(self):
        try:
            async with self.client() as client:
                lights, rooms, scenes = await self.get(client, "light"), await self.get(client, "room"), await self.get(client, "scene")
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Hue 桥接器：{type(exc).__name__}") from exc
        room_of = {child["rid"]: room.get("metadata", {}).get("name", "") for room in rooms for child in room.get("children") or []}
        room_name = {room["id"]: room.get("metadata", {}).get("name", "") for room in rooms}
        out = []
        for light in lights:
            actions = ["turn_on", "turn_off", "toggle"]
            state = {"on": (light.get("on") or {}).get("on")}
            if "dimming" in light:
                actions.append("set_brightness")
                state["brightness"] = round(light["dimming"].get("brightness", 0))
            if "color" in light:
                actions.append("set_color")
            if "color_temperature" in light:
                actions.append("set_color_temperature")
                mirek = light["color_temperature"].get("mirek")
                state["color_temperature"] = round(1_000_000 / mirek) if mirek else None
            out.append(self.device("light/" + light["id"], light.get("metadata", {}).get("name"), "light",
                                   room=room_of.get((light.get("owner") or {}).get("rid"), ""), state=state, actions=actions))
        for scene in scenes:
            out.append(self.device("scene/" + scene["id"], scene.get("metadata", {}).get("name"), "scene",
                                   room=room_name.get((scene.get("group") or {}).get("rid"), ""), actions=["activate"]))
        return out

    async def control(self, native_id, action, value=None):
        kind, ident = native_id.split("/", 1)
        try:
            async with self.client() as client:
                if kind == "scene":
                    body = {"recall": {"action": "active"}}
                elif action == "toggle":
                    current = await client.get(f"/clip/v2/resource/light/{ident}")
                    data = current.json().get("data") or [{}]
                    body = {"on": {"on": not (data[0].get("on") or {}).get("on")}}
                elif action in ("turn_on", "turn_off"):
                    body = {"on": {"on": action == "turn_on"}}
                elif action == "set_brightness":
                    level = number(value, 0, 100, "亮度")
                    body = {"on": {"on": level > 0}, "dimming": {"brightness": max(level, 1)}}
                elif action == "set_color":
                    r, g, b = parse_color(value)
                    x, y = rgb_to_xy(r, g, b)
                    body = {"on": {"on": True}, "color": {"xy": {"x": x, "y": y}}, "dimming": {"brightness": max(1, round(colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)[2] * 100))}}
                elif action == "set_color_temperature":
                    body = {"on": {"on": True}, "color_temperature": {"mirek": round(1_000_000 / number(value, 2000, 6500, "色温"))}}
                else:
                    raise ConnectorError("Hue 灯不支持这个操作")
                response = await client.put(f"/clip/v2/resource/{kind}/{ident}", json=body)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Hue 桥接器：{type(exc).__name__}") from exc
        if response.status_code >= 400:
            raise ConnectorError(f"Hue 桥接器返回 {response.status_code}")
