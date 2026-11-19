"""Samsung SmartThings cloud API, with a personal access token from https://account.smartthings.com/tokens."""
import asyncio
import colorsys

import httpx

from . import Connector, ConnectorError, number, parse_color

API = "https://api.smartthings.com/v1"
# capability -> (device type it implies, actions it enables); the first match in this order names the type.
CAPABILITIES = [("lock", "lock", ["lock", "unlock"]), ("doorControl", "cover", ["open", "close"]), ("windowShade", "cover", ["open", "close"]),
                ("windowShadeLevel", "cover", ["set_position"]), ("thermostatCoolingSetpoint", "climate", ["set_temperature"]),
                ("thermostatHeatingSetpoint", "climate", ["set_temperature"]), ("airConditionerMode", "climate", ["set_mode"]),
                ("thermostatMode", "climate", ["set_mode"]), ("colorControl", "light", ["set_color"]), ("colorTemperature", "light", ["set_color_temperature"]),
                ("switchLevel", "light", ["set_brightness"]), ("robotCleanerMovement", "vacuum", []), ("mediaPlayback", "media", []),
                ("fanSpeed", "fan", []), ("videoStream", "camera", []), ("switch", "switch", ["turn_on", "turn_off", "toggle"])]
SENSORS = ("temperatureMeasurement", "relativeHumidityMeasurement", "contactSensor", "motionSensor", "presenceSensor", "waterSensor", "smokeDetector", "powerMeter")


class SmartThings(Connector):
    kind = "smartthings"
    title = "SmartThings"
    description = "三星 SmartThings 云端，使用个人访问令牌。"
    fields = [{"name": "token", "label": "个人访问令牌", "secret": True, "placeholder": "在 account.smartthings.com/tokens 创建，勾选设备读取和控制"}]

    def client(self):
        return httpx.AsyncClient(base_url=API, timeout=20, transport=self.transport, headers={"Authorization": f"Bearer {self.config['token']}"})

    @staticmethod
    def check(response):
        if response.status_code in (401, 403):
            raise ConnectorError("SmartThings 拒绝了令牌，请检查令牌和它的设备权限")
        if response.status_code >= 400:
            raise ConnectorError(f"SmartThings 返回 {response.status_code}")
        return response.json()

    async def pages(self, client, path):
        items, url = [], path
        for _ in range(20):
            page = self.check(await client.get(url))
            items.extend(page.get("items") or [])
            url = ((page.get("_links") or {}).get("next") or {}).get("href")
            if not url:
                break
        return items

    async def devices(self):
        try:
            async with self.client() as client:
                devices = await self.pages(client, "/devices")
                rooms = {}
                for location in {d.get("locationId") for d in devices if d.get("locationId")}:
                    for room in await self.pages(client, f"/locations/{location}/rooms"):
                        rooms[room["roomId"]] = room.get("name", "")
                gate = asyncio.Semaphore(8)

                async def status(device):
                    async with gate:
                        reply = await client.get(f"/devices/{device['deviceId']}/status")
                        return (reply.json().get("components") or {}).get("main") or {} if reply.status_code == 200 else {}
                statuses = await asyncio.gather(*(status(d) for d in devices))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 SmartThings：{type(exc).__name__}") from exc
        out = []
        for device, main in zip(devices, statuses):
            caps = {c["id"] for comp in device.get("components") or [] if comp.get("id") == "main" for c in comp.get("capabilities") or []}
            kind, actions = None, []
            for capability, implied, enabled in CAPABILITIES:
                if capability in caps:
                    kind = kind or implied
                    actions.extend(a for a in enabled if a not in actions)
            if not kind:
                kind = "sensor" if caps & set(SENSORS) else "other"

            def read(capability, attribute):
                return ((main.get(capability) or {}).get(attribute) or {}).get("value")
            state = {"on": read("switch", "switch") == "on" if "switch" in caps else None, "brightness": read("switchLevel", "level"),
                     "color_temperature": read("colorTemperature", "colorTemperature"), "temperature": read("temperatureMeasurement", "temperature"),
                     "target_temperature": read("thermostatCoolingSetpoint", "coolingSetpoint") or read("thermostatHeatingSetpoint", "heatingSetpoint"),
                     "mode": read("airConditionerMode", "airConditionerMode") or read("thermostatMode", "thermostatMode"),
                     "locked": read("lock", "lock") == "locked" if "lock" in caps else None, "humidity": read("relativeHumidityMeasurement", "humidity"),
                     "contact": read("contactSensor", "contact"), "motion": read("motionSensor", "motion")}
            out.append(self.device(device["deviceId"], device.get("label") or device.get("name"), kind, room=rooms.get(device.get("roomId"), ""), state=state, actions=actions))
        return out

    async def control(self, native_id, action, value=None):
        def command(capability, name, *arguments):
            return {"component": "main", "capability": capability, "command": name, "arguments": list(arguments)}
        try:
            async with self.client() as client:
                if action == "toggle":
                    main = (self.check(await client.get(f"/devices/{native_id}/status")).get("components") or {}).get("main") or {}
                    action = "turn_off" if ((main.get("switch") or {}).get("switch") or {}).get("value") == "on" else "turn_on"
                if action in ("turn_on", "turn_off"):
                    commands = [command("switch", "on" if action == "turn_on" else "off")]
                elif action == "set_brightness":
                    commands = [command("switchLevel", "setLevel", round(number(value, 0, 100, "亮度")))]
                elif action == "set_color":
                    h, s, _ = colorsys.rgb_to_hsv(*(c / 255 for c in parse_color(value)))
                    commands = [command("colorControl", "setColor", {"hue": round(h * 100, 1), "saturation": round(s * 100, 1)})]
                elif action == "set_color_temperature":
                    commands = [command("colorTemperature", "setColorTemperature", round(number(value, 1500, 9000, "色温")))]
                elif action == "set_temperature":
                    device = self.check(await client.get(f"/devices/{native_id}"))
                    caps = {c["id"] for comp in device.get("components") or [] for c in comp.get("capabilities") or []}
                    target = number(value, 5, 40, "温度")
                    commands = [command("thermostatHeatingSetpoint", "setHeatingSetpoint", target)] if "thermostatCoolingSetpoint" not in caps else [command("thermostatCoolingSetpoint", "setCoolingSetpoint", target)]
                elif action == "set_mode":
                    device = self.check(await client.get(f"/devices/{native_id}"))
                    caps = {c["id"] for comp in device.get("components") or [] for c in comp.get("capabilities") or []}
                    commands = [command("airConditionerMode", "setAirConditionerMode", str(value))] if "airConditionerMode" in caps else [command("thermostatMode", "setThermostatMode", str(value))]
                elif action in ("lock", "unlock"):
                    commands = [command("lock", action)]
                elif action in ("open", "close"):
                    device = self.check(await client.get(f"/devices/{native_id}"))
                    caps = {c["id"] for comp in device.get("components") or [] for c in comp.get("capabilities") or []}
                    commands = [command("doorControl" if "doorControl" in caps else "windowShade", action)]
                elif action == "set_position":
                    commands = [command("windowShadeLevel", "setShadeLevel", round(number(value, 0, 100, "位置")))]
                else:
                    raise ConnectorError("SmartThings 设备不支持这个操作")
                self.check(await client.post(f"/devices/{native_id}/commands", json={"commands": commands}))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 SmartThings：{type(exc).__name__}") from exc
