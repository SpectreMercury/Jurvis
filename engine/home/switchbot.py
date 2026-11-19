"""SwitchBot cloud (Open API v1.1), with the token and secret key from the SwitchBot app (Profile → Preferences → Developer Options).

Written to https://github.com/OpenWonderLabs/SwitchBotAPI. Every request is signed: sign = base64(HMAC-SHA256(secret, token + t + nonce)).
Positions are normalized so that 100 is fully open; SwitchBot counts Curtain and Roller Shade the other way round (0 open, 100 closed).
The account is limited to 10,000 calls a day, and reading a device's state costs one call per device.
"""
import asyncio
import base64
import hashlib
import hmac
import time
import uuid

import httpx

from . import Connector, ConnectorError, number, parse_color

API = "https://api.switch-bot.com"
# deviceType -> (our type, has a documented "toggle" command)
SWITCHES = {"Bot": ("switch", False), "Plug": ("outlet", False), "Plug Mini (US)": ("outlet", True), "Plug Mini (JP)": ("outlet", True),
            "Plug Mini (EU)": ("outlet", True), "Relay Switch 1": ("switch", True), "Relay Switch 1PM": ("switch", True)}
# deviceType -> actions beyond on/off/toggle/brightness
LIGHTS = {"Color Bulb": ["set_color", "set_color_temperature"], "Strip Light": ["set_color"], "Strip Light 3": ["set_color", "set_color_temperature"],
          "Ceiling Light": ["set_color_temperature"], "Ceiling Light Pro": ["set_color_temperature"], "Floor Lamp": ["set_color", "set_color_temperature"]}
COVERS = {"Curtain": "curtain", "Curtain3": "curtain", "Curtain 3": "curtain", "Blind Tilt": "tilt", "Roller Shade": "roller"}
LOCKS = {"Smart Lock", "Smart Lock Pro", "Smart Lock Ultra", "Smart Lock Lite", "Lock", "Lock Pro", "Lock Ultra", "Lock Lite"}
SENSORS = {"Hub 2", "Hub 3", "Motion Sensor", "Contact Sensor", "Water Detector"}  # plus every "Meter…" type
ERRORS = {151: "这个设备类型不支持该命令", 152: "SwitchBot 找不到这个设备，可能已从账号移除", 160: "这个设备不支持该命令",
          161: "设备离线，请检查电量和蓝牙/Wi-Fi 连接", 171: "设备所连的 Hub 离线，请检查 Hub 的电源和网络",
          190: "设备内部错误或状态未同步，请在 SwitchBot App 里确认设备可用并已开启云服务"}


def classify(device_type):
    """deviceType -> (our type, native id prefix) or None when the type is not mapped."""
    if device_type in SWITCHES:
        return SWITCHES[device_type][0], "switch"
    if device_type in LIGHTS:
        return "light", "light"
    if device_type in COVERS:
        return "cover", COVERS[device_type]
    if device_type in LOCKS:
        return "lock", "lock"
    if device_type in SENSORS or "Meter" in device_type:
        return "sensor", "sensor"
    return None


class SwitchBot(Connector):
    kind = "switchbot"
    title = "SwitchBot"
    description = "SwitchBot 云端开放 API，覆盖开关/插座、窗帘和卷帘、灯、门锁、温湿度计与场景。"
    fields = [{"name": "token", "label": "Token", "secret": True, "placeholder": "SwitchBot App → 个人主页 → 偏好设置 → 连点 App 版本号 → 开发者选项"},
              {"name": "secret", "label": "Secret Key", "secret": True, "placeholder": "与 Token 在同一页面"}]

    def headers(self):
        token, t, nonce = self.config["token"], str(int(time.time() * 1000)), str(uuid.uuid4())
        digest = hmac.new(self.config["secret"].encode(), f"{token}{t}{nonce}".encode(), hashlib.sha256).digest()
        return {"Authorization": token, "sign": base64.b64encode(digest).decode(), "t": t, "nonce": nonce}

    def client(self):
        return httpx.AsyncClient(base_url=API, timeout=20, transport=self.transport)

    async def call(self, client, method, path, payload=None):
        headers = self.headers()
        if payload is not None:
            headers["Content-Type"] = "application/json; charset=utf8"
        response = await client.request(method, path, json=payload, headers=headers)
        if response.status_code in (401, 403):
            raise ConnectorError("SwitchBot 拒绝了请求：请检查 Token 和 Secret Key；如果当天调用已超过 10000 次，也会这样提示，需等到第二天")
        if response.status_code == 429:
            raise ConnectorError("SwitchBot 请求过于频繁，请稍后再试")
        if response.status_code >= 400:
            raise ConnectorError(f"SwitchBot 服务返回 {response.status_code}，请稍后再试")
        reply = response.json()
        code = reply.get("statusCode")
        if code != 100:
            raise ConnectorError(ERRORS.get(code, f"SwitchBot 没有执行这个请求（状态码 {code}），请在 SwitchBot App 里确认设备可用"))
        return reply.get("body")

    @staticmethod
    def state(prefix, status):
        power = status.get("power")
        on = power.lower() == "on" if isinstance(power, str) else (bool(status["switchStatus"]) if "switchStatus" in status else None)
        if prefix == "switch":
            return {"on": on, "mode": status.get("deviceMode"), "battery": status.get("battery")}
        if prefix == "light":
            color = [int(c) for c in str(status.get("color") or "").split(":") if c.isdigit()]
            return {"on": on, "brightness": status.get("brightness"), "color": "#%02x%02x%02x" % tuple(color) if len(color) == 3 else None,
                    "color_temperature": status.get("colorTemperature") or None}
        if prefix in ("curtain", "roller"):
            position = status.get("slidePosition")
            return {"position": 100 - position if isinstance(position, (int, float)) else None, "moving": status.get("moving"), "battery": status.get("battery")}
        if prefix == "tilt":
            return {"direction": status.get("direction"), "moving": status.get("moving")}
        if prefix == "lock":
            lock = status.get("lockState")
            return {"locked": str(lock).lower() in ("lock", "locked", "latchboltlocked") if lock is not None else None, "value": lock,
                    "contact": status.get("doorState"), "battery": status.get("battery")}
        leak = {0: "dry", 1: "leak"}.get(status.get("status")) if status.get("deviceType") == "Water Detector" else None
        return {"temperature": status.get("temperature"), "humidity": status.get("humidity"), "co2": status.get("CO2"), "motion": status.get("moveDetected"),
                "contact": status.get("openState"), "light": status.get("brightness") or status.get("lightLevel"), "value": leak, "battery": status.get("battery")}

    async def devices(self):
        try:
            async with self.client() as client:
                listed = (await self.call(client, "GET", "/v1.1/devices") or {}).get("deviceList") or []
                scenes = await self.call(client, "GET", "/v1.1/scenes") or []
                known = [(d, classify(d.get("deviceType") or "")) for d in listed]
                gate = asyncio.Semaphore(5)

                async def status(device, mapped):
                    if not mapped or device.get("enableCloudService") is False:
                        return None
                    async with gate:
                        try:
                            return await self.call(client, "GET", f"/v1.1/devices/{device['deviceId']}/status") or {}
                        except ConnectorError:
                            return None  # offline or not synced: still list the device
                statuses = await asyncio.gather(*(status(d, mapped) for d, mapped in known))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 SwitchBot 云端，请检查网络：{type(exc).__name__}") from exc
        out = []
        for (device, mapped), status in zip(known, statuses):
            device_type = device.get("deviceType") or ""
            if not mapped:
                continue
            kind, prefix = mapped
            actions = []
            if prefix == "switch":
                actions = ["turn_on", "turn_off"] + (["toggle"] if SWITCHES[device_type][1] else [])
            elif prefix == "light":
                actions = ["turn_on", "turn_off", "toggle", "set_brightness"] + LIGHTS[device_type]
            elif prefix in ("curtain", "tilt", "roller"):
                actions = ["open", "close", "set_position"]
            elif prefix == "lock":
                actions = ["lock", "unlock"]
            online = status is not None and status.get("onlineStatus") != "offline" and status.get("online") is not False
            out.append(self.device(f"{prefix}/{device['deviceId']}", device.get("deviceName"), kind, online=online,
                                   state=self.state(prefix, status or {}), actions=actions))
        for scene in scenes:
            out.append(self.device(f"scene/{scene['sceneId']}", scene.get("sceneName"), "scene", actions=["activate"]))
        return out

    async def control(self, native_id, action, value=None):
        prefix, _, ident = native_id.partition("/")
        parameter = "default"
        if prefix == "scene":
            command = None
        elif action in ("turn_on", "turn_off") and prefix in ("switch", "light"):
            command = "turnOn" if action == "turn_on" else "turnOff"
        elif action == "toggle" and prefix in ("switch", "light"):
            command = "toggle"
        elif action == "set_brightness" and prefix == "light":
            level = round(number(value, 0, 100, "亮度"))
            command, parameter = ("setBrightness", level) if level else ("turnOff", "default")
        elif action == "set_color" and prefix == "light":
            command, parameter = "setColor", "%d:%d:%d" % parse_color(value)
        elif action == "set_color_temperature" and prefix == "light":
            command, parameter = "setColorTemperature", round(number(value, 2700, 6500, "色温"))
        elif action in ("open", "close") and prefix == "curtain":
            command = "turnOn" if action == "open" else "turnOff"
        elif action in ("open", "close") and prefix == "tilt":
            command = "fullyOpen" if action == "open" else "closeDown"
        elif action in ("open", "close") and prefix == "roller":
            command, parameter = "setPosition", 0 if action == "open" else 100
        elif action == "set_position" and prefix in ("curtain", "tilt", "roller"):
            position = round(number(value, 0, 100, "位置"))
            if prefix == "curtain":
                parameter = f"0,ff,{100 - position}"
            elif prefix == "tilt":
                parameter = f"down;{position // 2 * 2}"  # 0 closed, 100 open, even numbers only
            else:
                parameter = 100 - position
            command = "setPosition"
        elif action in ("lock", "unlock") and prefix == "lock":
            command = action
        else:
            raise ConnectorError("这个 SwitchBot 设备不支持该操作")
        try:
            async with self.client() as client:
                if prefix == "scene":
                    await self.call(client, "POST", f"/v1.1/scenes/{ident}/execute")
                else:
                    await self.call(client, "POST", f"/v1.1/devices/{ident}/commands", {"command": command, "parameter": parameter, "commandType": "command"})
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 SwitchBot 云端，请检查网络：{type(exc).__name__}") from exc
