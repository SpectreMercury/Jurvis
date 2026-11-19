"""Govee through the official Govee Open API (https://openapi.api.govee.com/router/api/v1). The API key is requested in the
Govee Home app (个人资料 → 设置 → Apply for API Key). Only Wi-Fi devices appear there; Bluetooth-only ones do not."""
import asyncio
import uuid

import httpx

from . import Connector, ConnectorError, number, parse_color

BASE = "/router/api/v1"
DEVICE_TYPES = {"devices.types.light": "light", "devices.types.socket": "outlet", "devices.types.fan": "fan", "devices.types.heater": "climate",
                "devices.types.thermometer": "sensor", "devices.types.sensor": "sensor", "devices.types.air_quality_monitor": "sensor"}
ON_OFF, RANGE, COLOR, ONLINE = (f"devices.capabilities.{name}" for name in ("on_off", "range", "color_setting", "online"))
# (capability type, instance) -> normalized actions
CAPABILITIES = {(ON_OFF, "powerSwitch"): ["turn_on", "turn_off", "toggle"], (RANGE, "brightness"): ["set_brightness"],
                (COLOR, "colorRgb"): ["set_color"], (COLOR, "colorTemperatureK"): ["set_color_temperature"]}


class Govee(Connector):
    kind = "govee"
    title = "Govee"
    description = "通过 Govee 官方云端 Open API 控制支持 Wi-Fi 的灯和插座，需要在 Govee Home 应用里申请的 API Key。"
    fields = [{"name": "key", "label": "API Key", "secret": True, "placeholder": "Govee Home → 设置 → Apply for API Key"}]

    def client(self):
        return httpx.AsyncClient(base_url="https://openapi.api.govee.com", timeout=20, headers={"Govee-API-Key": self.config["key"]}, transport=self.transport)

    @staticmethod
    def check(response):
        if response.status_code in (401, 403):
            raise ConnectorError("Govee 拒绝了 API Key，请在 Govee Home 应用里重新申请并填写")
        if response.status_code == 429:
            raise ConnectorError("Govee 请求太频繁（云端有每日和每分钟限额），请稍后再试")
        if response.status_code == 404:
            raise ConnectorError("Govee 找不到这个设备或它不支持这个功能，请刷新设备列表")
        if response.status_code == 400:
            raise ConnectorError("Govee 不接受这个取值，请换一个在设备支持范围内的值")
        if response.status_code >= 400:
            raise ConnectorError(f"Govee 云端返回 {response.status_code}，请稍后再试")
        try:
            reply = response.json()
        except ValueError as exc:
            raise ConnectorError("Govee 云端返回了无法识别的内容，请稍后再试") from exc
        if reply.get("code") not in (None, 200):
            raise ConnectorError(f"Govee 没有执行这个请求：{reply.get('msg') or reply.get('message') or reply.get('code')}")
        return reply

    async def status(self, client, sku, device):
        """{instance: value} for one device, with "online" among them."""
        reply = self.check(await client.post(BASE + "/device/state", json={"requestId": str(uuid.uuid4()), "payload": {"sku": sku, "device": device}}))
        return {c.get("instance"): (c.get("state") or {}).get("value") for c in (reply.get("payload") or {}).get("capabilities") or []}

    async def devices(self):
        try:
            async with self.client() as client:
                found = self.check(await client.get(BASE + "/user/devices")).get("data") or []
                statuses = await asyncio.gather(*(self.status(client, d["sku"], d["device"]) for d in found), return_exceptions=True)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Govee 云端：{type(exc).__name__}，请检查网络") from exc
        out = []
        for device, status in zip(found, statuses):
            status = {} if isinstance(status, BaseException) else status  # the list is still useful when one state query is rate limited
            actions, state = [], {}
            for capability in device.get("capabilities") or []:
                actions += CAPABILITIES.get((capability.get("type"), capability.get("instance")), [])
            if "turn_on" in actions and status.get("powerSwitch") is not None:
                state["on"] = bool(status["powerSwitch"])
            if "set_brightness" in actions and isinstance(status.get("brightness"), (int, float)):
                state["brightness"] = round(status["brightness"])
            if "set_color_temperature" in actions and status.get("colorTemperatureK"):
                state["color_temperature"] = status["colorTemperatureK"]
            elif "set_color" in actions and isinstance(status.get("colorRgb"), int):
                state["color"] = f"#{status['colorRgb']:06x}"
            kind = DEVICE_TYPES.get(device.get("type"), "light" if "set_brightness" in actions or "set_color" in actions else "other")
            out.append(self.device(f"{device['sku']}/{device['device']}", device.get("deviceName") or device["sku"], kind,
                                   online=status.get("online") is not False, state=state, actions=actions))
        return out

    async def control(self, native_id, action, value=None):
        sku, device = native_id.split("/", 1)
        if action in ("turn_on", "turn_off", "toggle"):
            capability = {"type": ON_OFF, "instance": "powerSwitch", "value": int(action == "turn_on")}
        elif action == "set_brightness":
            level = number(value, 0, 100, "亮度")
            capability = {"type": RANGE, "instance": "brightness", "value": round(level)} if level >= 1 else {"type": ON_OFF, "instance": "powerSwitch", "value": 0}
        elif action == "set_color":
            r, g, b = parse_color(value)
            capability = {"type": COLOR, "instance": "colorRgb", "value": (r << 16) | (g << 8) | b}
        elif action == "set_color_temperature":
            capability = {"type": COLOR, "instance": "colorTemperatureK", "value": round(number(value, 2000, 9000, "色温"))}
        else:
            raise ConnectorError("Govee 设备不支持这个操作")
        try:
            async with self.client() as client:
                if action == "toggle":  # the API has no toggle, so read the switch first
                    capability["value"] = int(not (await self.status(client, sku, device)).get("powerSwitch"))
                self.check(await client.post(BASE + "/device/control", json={"requestId": str(uuid.uuid4()), "payload": {"sku": sku, "device": device, "capability": capability}}))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Govee 云端：{type(exc).__name__}，请检查网络") from exc
