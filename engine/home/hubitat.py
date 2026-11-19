"""Hubitat Elevation through its built-in Maker API app, on the local network or through Hubitat's cloud relay.

Written to https://docs2.hubitat.com/en/apps/maker-api and the driver capability list (https://docs2.hubitat.com/en/developer/driver/capability-list).
Maker API takes its access token as a query parameter and sends commands as GET /devices/[id]/[command]/[value]; only the devices
selected in the Maker API app are visible. The hub reports temperatures in its own scale, which Maker API does not expose, hence the unit setting.
"""
import json
from urllib.parse import quote, urlsplit

import httpx

from . import Connector, ConnectorError, number, parse_color

SWITCHING = ["turn_on", "turn_off", "toggle"]
# capability -> (device type it implies, actions it enables); the first match in this order names the type.
CAPABILITIES = [("Lock", "lock", ["lock", "unlock"]), ("GarageDoorControl", "cover", ["open", "close"]), ("DoorControl", "cover", ["open", "close"]),
                ("WindowShade", "cover", ["open", "close", "set_position"]), ("WindowBlind", "cover", ["open", "close", "set_position"]),
                ("Thermostat", "climate", ["set_temperature", "set_mode"]), ("ThermostatMode", "climate", ["set_mode"]),
                ("ThermostatCoolingSetpoint", "climate", ["set_temperature"]), ("ThermostatHeatingSetpoint", "climate", ["set_temperature"]),
                ("FanControl", "fan", []), ("ColorControl", "light", ["set_color"]), ("ColorTemperature", "light", ["set_color_temperature"]),
                ("Light", "light", SWITCHING), ("Bulb", "light", SWITCHING), ("SwitchLevel", "light", ["set_brightness"]), ("Outlet", "outlet", SWITCHING),
                ("Valve", "other", ["open", "close"]), ("Switch", "switch", SWITCHING)]
SENSORS = ("TemperatureMeasurement", "RelativeHumidityMeasurement", "ContactSensor", "MotionSensor", "WaterSensor", "SmokeDetector", "PresenceSensor", "IlluminanceMeasurement")
# normalized action -> the Maker API command it needs
COMMANDS = {"turn_on": "on", "turn_off": "off", "toggle": "on", "set_brightness": "setLevel", "set_color": "setColor", "set_color_temperature": "setColorTemperature",
            "set_mode": "setThermostatMode", "open": "open", "close": "close", "set_position": "setPosition", "lock": "lock", "unlock": "unlock"}
MODES = ("auto", "off", "heat", "emergency heat", "cool")


def attributes(device):
    """Maker API shows attributes as {"switch": "off"}; tolerate the [{"name", "currentValue"}] form some hub versions return."""
    raw = device.get("attributes") or {}
    return {a.get("name"): a.get("currentValue") for a in raw if isinstance(a, dict)} if isinstance(raw, list) else raw


def commands(device):
    return {c.get("command") if isinstance(c, dict) else c for c in device.get("commands") or []}


def numeric(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class Hubitat(Connector):
    kind = "hubitat"
    title = "Hubitat"
    description = "Hubitat Elevation 中枢的 Maker API，可走局域网，也可走 Hubitat 云端转发地址。"
    fields = [{"name": "url", "label": "中枢地址", "placeholder": "http://192.168.1.50，或 Maker API 页面给出的云端地址（到应用编号为止）"},
              {"name": "app_id", "label": "Maker API 应用编号", "placeholder": "Maker API 示例地址里 /apps/api/ 后面的数字", "optional": True},
              {"name": "token", "label": "访问令牌", "secret": True, "placeholder": "Maker API 示例地址里的 access_token"},
              {"name": "unit", "label": "中枢温度单位", "placeholder": "C 或 F，默认 C", "optional": True}]

    def base(self):
        url = str(self.config.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        parts = urlsplit(url)
        path = parts.path.split("/devices")[0].rstrip("/")
        if "/apps/" not in path:  # a bare hub address: add the documented local path
            if not self.config.get("app_id"):
                raise ConnectorError("请填写 Maker API 应用编号，或在中枢地址里粘贴 Maker API 页面给出的完整地址")
            path = f"{path}/apps/api/{str(self.config['app_id']).strip()}"
        return f"{parts.scheme}://{parts.netloc}{path}"

    def fahrenheit(self):
        return str(self.config.get("unit", "C")).strip().upper() == "F"

    def client(self):
        return httpx.AsyncClient(base_url=self.base(), timeout=20, transport=self.transport, params={"access_token": self.config["token"]})

    @staticmethod
    def check(response):
        if response.status_code in (401, 403):
            raise ConnectorError("Hubitat 拒绝了访问令牌，请到 Maker API 应用里核对 access_token（云端地址还需在应用里允许云端访问）")
        if response.status_code == 404:
            raise ConnectorError("Hubitat 找不到这个地址或设备，请核对 Maker API 应用编号，并确认设备已在 Maker API 里勾选")
        if response.status_code >= 400:
            raise ConnectorError(f"Hubitat 没有执行这个请求（HTTP {response.status_code}），这个设备可能不支持该命令")
        try:
            return response.json()
        except ValueError as exc:
            raise ConnectorError("这个地址返回的不是 Maker API 的数据，请核对中枢地址和应用编号") from exc

    def normalize(self, device):
        caps = {c for c in device.get("capabilities") or [] if isinstance(c, str)}
        attrs, available = attributes(device), commands(device)
        kind, actions = None, []

        def supported(action):
            if action == "set_temperature":
                return bool({"setCoolingSetpoint", "setHeatingSetpoint"} & available)
            return COMMANDS[action] in available and (action != "toggle" or "off" in available)
        for capability, implied, enabled in CAPABILITIES:
            if capability in caps:
                kind = kind or implied
                actions.extend(a for a in enabled if a not in actions and supported(a))
        if not kind:
            kind = "sensor" if caps & set(SENSORS) else "other"

        def degrees(name):
            value = numeric(attrs.get(name))
            return value if value is None or not self.fahrenheit() else round((value - 32) * 5 / 9, 1)
        mode = attrs.get("thermostatMode")
        state = {"on": attrs.get("switch") == "on" if "switch" in attrs else None, "brightness": numeric(attrs.get("level")) if "SwitchLevel" in caps else None,
                 "color_temperature": numeric(attrs.get("colorTemperature")), "temperature": degrees("temperature"),
                 "target_temperature": degrees("thermostatSetpoint") if kind == "climate" else None, "mode": mode,
                 "modes": list(MODES) if kind == "climate" and "set_mode" in actions else None,
                 "locked": attrs.get("lock") == "locked" if "lock" in attrs else None,
                 "position": numeric(attrs.get("position")) if kind == "cover" else None,
                 "value": attrs.get("door") or attrs.get("windowShade") or attrs.get("windowBlind") or attrs.get("valve") or attrs.get("speed"),
                 "humidity": numeric(attrs.get("humidity")), "contact": attrs.get("contact"), "motion": attrs.get("motion"), "water": attrs.get("water"),
                 "smoke": attrs.get("smoke"), "presence": attrs.get("presence"), "illuminance": numeric(attrs.get("illuminance")), "battery": numeric(attrs.get("battery"))}
        return self.device(device["id"], device.get("label") or device.get("name"), kind, room=device.get("room") or "", state=state, actions=actions)

    async def devices(self):
        try:
            async with self.client() as client:
                found = self.check(await client.get("/devices/all"))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Hubitat 中枢，请检查地址，以及这台电脑是否和中枢在同一网络：{type(exc).__name__}") from exc
        if not isinstance(found, list):
            raise ConnectorError("这个地址返回的不是 Maker API 的设备列表，请核对中枢地址和应用编号")
        return [self.normalize(d) for d in found]

    async def control(self, native_id, action, value=None):
        try:
            async with self.client() as client:
                async def current():
                    return self.check(await client.get(f"/devices/{native_id}"))
                argument = None
                if action == "toggle":
                    command = "off" if attributes(await current()).get("switch") == "on" else "on"
                elif action == "set_brightness":
                    command, argument = "setLevel", str(round(number(value, 0, 100, "亮度")))
                elif action == "set_color":
                    command, argument = "setColor", json.dumps({"hex": "%02X%02X%02X" % parse_color(value)}, separators=(",", ":"))
                elif action == "set_color_temperature":
                    command, argument = "setColorTemperature", str(round(number(value, 1500, 9000, "色温")))
                elif action == "set_temperature":
                    device = await current()
                    available, mode = commands(device), attributes(device).get("thermostatMode")
                    command = "setCoolingSetpoint" if "setCoolingSetpoint" in available and (mode == "cool" or "setHeatingSetpoint" not in available) else "setHeatingSetpoint"
                    if command not in available:
                        raise ConnectorError("这个 Hubitat 设备不支持设定温度")
                    target = number(value, 5, 40, "温度")
                    argument = str(round(target * 9 / 5 + 32) if self.fahrenheit() else round(target, 1))
                elif action == "set_mode":
                    command, argument = "setThermostatMode", str(value or "").strip().lower()
                    if argument not in MODES:
                        raise ConnectorError("温控器模式需是 " + " / ".join(MODES) + " 之一")
                elif action == "set_position":
                    command, argument = "setPosition", str(round(number(value, 0, 100, "位置")))
                elif action in COMMANDS:
                    command = COMMANDS[action]
                else:
                    raise ConnectorError("Hubitat 设备不支持这个操作")
                self.check(await client.get(f"/devices/{native_id}/{command}" + (f"/{quote(argument, safe='')}" if argument is not None else "")))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Hubitat 中枢，请检查地址，以及这台电脑是否和中枢在同一网络：{type(exc).__name__}") from exc
