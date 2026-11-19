"""IKEA DIRIGERA hub, talking to it on the local network (https://<hub>:8443/v1). No cloud account is involved.

IKEA publishes no documentation for this API. This follows what the community clients observed and use, mainly
https://github.com/Leggin/dirigera (hub.py and devices/*.py): GET /devices, PATCH /devices/{id} with [{"attributes": {...}}],
GET /scenes and POST /scenes/{id}/trigger, all with a bearer token. Only attributes a device lists under capabilities.canReceive are written.
Blind levels are taken as 0 = fully open and 100 = fully closed (the IKEA app's "percent closed") and flipped so that 100 is open here.
"""
import colorsys

import httpx

from . import Connector, ConnectorError, number, parse_color

SENSORS = ("environmentSensor", "motionSensor", "openCloseSensor", "waterSensor", "lightSensor", "occupancySensor")
FAN_MODES = ("off", "low", "medium", "high", "auto")


def kelvin_bounds(attrs):
    """Supported colour temperature range; some bulbs report the bounds in mireds (below 1000) although the value itself is in kelvin."""
    bounds = [round(1_000_000 / b) if 0 < b < 1000 else b for b in (attrs.get("colorTemperatureMin"), attrs.get("colorTemperatureMax")) if isinstance(b, (int, float)) and b > 0]
    return (min(bounds), max(bounds)) if len(bounds) == 2 else (2000, 6500)


class Dirigera(Connector):
    kind = "dirigera"
    title = "IKEA DIRIGERA"
    description = "直接连接局域网里的宜家 DIRIGERA 网关，不经过云端：灯、插座、窗帘、空气净化器、传感器和场景。"
    fields = [{"name": "host", "label": "网关地址", "placeholder": "192.168.1.30"},
              {"name": "token", "label": "访问令牌", "secret": True, "placeholder": "需先配对获取：用社区工具（如 dirigera 的 generate-token）发起配对，并按一下网关底部的操作按钮"}]

    def client(self):
        # The hub serves a self-signed certificate, so it cannot be verified against a public CA.
        options = {"transport": self.transport} if self.transport else {"verify": False}
        return httpx.AsyncClient(base_url=f"https://{self.config['host']}:8443/v1", timeout=15, headers={"Authorization": f"Bearer {self.config['token']}"}, **options)

    @staticmethod
    def check(response):
        if response.status_code in (401, 403):
            raise ConnectorError("DIRIGERA 网关拒绝了访问令牌，请重新配对生成令牌（网关恢复出厂设置后旧令牌会失效）")
        if response.status_code == 404:
            raise ConnectorError("DIRIGERA 网关里找不到这个设备或场景，可能已被删除")
        if response.status_code >= 400:
            raise ConnectorError(f"DIRIGERA 网关没有执行这个请求（HTTP {response.status_code}），请确认设备在线并支持该操作")
        return response

    def normalize(self, device):
        attrs, kind = device.get("attributes") or {}, device.get("deviceType") or device.get("type")
        can = set((device.get("capabilities") or {}).get("canReceive") or [])
        switching = ["turn_on", "turn_off", "toggle"] if "isOn" in can else []
        state = {"on": attrs.get("isOn"), "battery": attrs.get("batteryPercentage")}
        if kind == "light":
            mapped, actions = "light", switching + (["set_brightness"] if "lightLevel" in can else []) + (["set_color"] if {"colorHue", "colorSaturation"} <= can else []) \
                + (["set_color_temperature"] if "colorTemperature" in can else [])
            state.update(brightness=attrs.get("lightLevel"), color_temperature=attrs.get("colorTemperature") if attrs.get("colorMode") != "color" else None)
        elif kind == "outlet":
            mapped, actions = "outlet", switching
            state["power"] = attrs.get("currentActivePower")
        elif kind == "blinds":
            mapped, actions = "cover", ["open", "close", "set_position"] if "blindsTargetLevel" in can else []
            level = attrs.get("blindsCurrentLevel")
            state.update(on=None, position=100 - level if isinstance(level, (int, float)) else None, value=attrs.get("blindsState"))
        elif kind == "airPurifier":
            mapped, actions = "fan", ["turn_on", "turn_off", "toggle", "set_mode"] if "fanMode" in can else []
            mode = attrs.get("fanMode")
            state.update(on=mode != "off" if mode else None, mode=mode, modes=list(FAN_MODES), pm25=attrs.get("currentPM25"))
        elif kind in SENSORS:
            mapped, actions = "sensor", []
            state.update(on=None, temperature=attrs.get("currentTemperature"), humidity=attrs.get("currentRH"), pm25=attrs.get("currentPM25"), co2=attrs.get("currentCO2"),
                         contact=("open" if attrs["isOpen"] else "closed") if "isOpen" in attrs else None, motion=attrs.get("isDetected"), water=attrs.get("waterLeakDetected"))
        else:
            return None  # gateway, remotes, repeaters and device kinds this connector has not mapped
        return self.device(device["id"], attrs.get("customName"), mapped, room=(device.get("room") or {}).get("name", ""), online=device.get("isReachable", True),
                           state=state, actions=actions)

    async def devices(self):
        try:
            async with self.client() as client:
                devices, scenes = self.check(await client.get("/devices")).json(), self.check(await client.get("/scenes")).json()
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 DIRIGERA 网关，请检查网关地址，以及这台电脑是否和网关在同一网络：{type(exc).__name__}") from exc
        out = [d for d in map(self.normalize, devices) if d]
        for scene in scenes:
            out.append(self.device("scene/" + scene["id"], (scene.get("info") or {}).get("name"), "scene", actions=["activate"]))
        return out

    async def control(self, native_id, action, value=None):
        try:
            async with self.client() as client:
                if native_id.startswith("scene/"):
                    self.check(await client.post(f"/scenes/{native_id[6:]}/trigger"))
                    return

                async def current():
                    return self.check(await client.get(f"/devices/{native_id}")).json()
                if action in ("turn_on", "turn_off", "toggle"):
                    device = await current()
                    attrs = device.get("attributes") or {}
                    if (device.get("deviceType") or device.get("type")) == "airPurifier":
                        on = attrs.get("fanMode") == "off" if action == "toggle" else action == "turn_on"
                        change = {"fanMode": "auto" if on else "off"}
                    else:
                        change = {"isOn": (not attrs.get("isOn")) if action == "toggle" else action == "turn_on"}
                elif action == "set_brightness":
                    level = round(number(value, 0, 100, "亮度"))
                    change = {"lightLevel": level} if level else {"isOn": False}
                elif action == "set_color":
                    hue, saturation, _ = colorsys.rgb_to_hsv(*(c / 255 for c in parse_color(value)))
                    change = {"colorHue": round(hue * 360, 1), "colorSaturation": round(saturation, 3)}
                elif action == "set_color_temperature":
                    low, high = kelvin_bounds((await current()).get("attributes") or {})
                    change = {"colorTemperature": round(min(high, max(low, number(value, 1500, 9000, "色温"))))}
                elif action in ("open", "close"):
                    change = {"blindsTargetLevel": 0 if action == "open" else 100}
                elif action == "set_position":
                    change = {"blindsTargetLevel": 100 - round(number(value, 0, 100, "位置"))}
                elif action == "set_mode":
                    mode = str(value or "").strip().lower()
                    if mode not in FAN_MODES:
                        raise ConnectorError("空气净化器的模式需是 " + " / ".join(FAN_MODES) + " 之一")
                    change = {"fanMode": mode}
                else:
                    raise ConnectorError("DIRIGERA 设备不支持这个操作")
                self.check(await client.patch(f"/devices/{native_id}", json=[{"attributes": change}]))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 DIRIGERA 网关，请检查网关地址，以及这台电脑是否和网关在同一网络：{type(exc).__name__}") from exc
