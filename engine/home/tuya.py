"""Tuya / Smart Life cloud (OpenAPI). Needs a cloud project from https://iot.tuya.com with the user's app account linked to it.

Tuya devices describe themselves with "data point" codes that differ by product, so this connector maps the common ones
(switch, switch_1, switch_led, bright_value, temp_value, temp_set, mode, control, percent_control) and leaves the rest unmapped.
"""
import hashlib
import hmac
import json
import secrets
import time

import httpx

from . import Connector, ConnectorError, number, parse_color

REGIONS = {"cn": "https://openapi.tuyacn.com", "us": "https://openapi.tuyaus.com", "eu": "https://openapi.tuyaeu.com", "in": "https://openapi.tuyain.com",
           "us-e": "https://openapi-ueaz.tuyaus.com", "eu-w": "https://openapi-weaz.tuyaeu.com", "sg": "https://openapi-sg.iotbing.com"}
CATEGORIES = {"dj": "light", "dd": "light", "xdd": "light", "fwd": "light", "dc": "light", "kg": "switch", "tgkg": "switch", "cz": "outlet", "pc": "outlet",
              "kt": "climate", "wk": "climate", "qn": "climate", "rs": "climate", "cl": "cover", "clkg": "cover", "ckmkzq": "cover", "ms": "lock", "jtmspro": "lock",
              "fs": "fan", "fsd": "fan", "sd": "vacuum", "sp": "camera", "wsdcg": "sensor", "mcs": "sensor", "pir": "sensor", "ywbj": "sensor", "sj": "sensor"}
SWITCHES = ("switch_led", "switch", "switch_1", "power", "switch_fan")
BRIGHTNESS = ("bright_value_v2", "bright_value")
COLOR_TEMPERATURE = ("temp_value_v2", "temp_value")
COLOR = ("colour_data_v2", "colour_data")


class Tuya(Connector):
    kind = "tuya"
    title = "涂鸦 / Smart Life"
    description = "涂鸦云 OpenAPI，覆盖涂鸦智能、Smart Life 以及使用涂鸦模组的品牌。"
    fields = [{"name": "region", "label": "数据中心", "placeholder": "cn / us / eu / in / sg / us-e / eu-w"},
              {"name": "client_id", "label": "Access ID"}, {"name": "secret", "label": "Access Secret", "secret": True}]
    tokens = {}  # client_id -> (access token, expiry)

    def base(self):
        region = self.config.get("region", "").lower()
        if region not in REGIONS:
            raise ConnectorError("数据中心需是 " + " / ".join(REGIONS) + " 之一")
        return REGIONS[region]

    def signed(self, method, path, body, token=""):
        t, nonce = str(int(time.time() * 1000)), secrets.token_hex(16)
        content = hashlib.sha256(body.encode()).hexdigest()
        text = self.config["client_id"] + token + t + nonce + f"{method}\n{content}\n\n{path}"
        sign = hmac.new(self.config["secret"].encode(), text.encode(), hashlib.sha256).hexdigest().upper()
        headers = {"client_id": self.config["client_id"], "sign": sign, "t": t, "nonce": nonce, "sign_method": "HMAC-SHA256"}
        if token:
            headers["access_token"] = token
        return headers

    async def call(self, client, method, path, payload=None):
        """path must already have its query parameters in alphabetical order (they are part of the signature)."""
        cached = self.tokens.get(self.config["client_id"])
        if not cached or cached[1] < time.time() + 60:
            grant = "/v1.0/token?grant_type=1"
            reply = (await client.get(grant, headers=self.signed("GET", grant, ""))).json()
            if not reply.get("success"):
                raise ConnectorError(f"涂鸦拒绝了凭据：{reply.get('msg', '未知错误')}")
            cached = (reply["result"]["access_token"], time.time() + reply["result"].get("expire_time", 7200))
            Tuya.tokens[self.config["client_id"]] = cached
        body = json.dumps(payload, separators=(",", ":")) if payload is not None else ""
        headers = self.signed(method, path, body, cached[0])
        reply = (await client.request(method, path, content=body or None, headers={**headers, "Content-Type": "application/json"})).json()
        if not reply.get("success"):
            if reply.get("code") in (1010, 1011):  # token expired or invalid: fetch a new one next time
                Tuya.tokens.pop(self.config["client_id"], None)
            raise ConnectorError(f"涂鸦返回错误：{reply.get('msg', reply.get('code'))}")
        return reply.get("result")

    def client(self):
        return httpx.AsyncClient(base_url=self.base(), timeout=20, transport=self.transport)

    async def devices(self):
        try:
            async with self.client() as client:
                found, last = [], ""
                for _ in range(25):
                    path = "/v2.0/cloud/thing/device?" + (f"last_id={last}&" if last else "") + "page_size=20"
                    page = await self.call(client, "GET", path) or []
                    found.extend(page)
                    if len(page) < 20:
                        break
                    last = page[-1]["id"]
                statuses = {}
                for start in range(0, len(found), 20):
                    ids = ",".join(d["id"] for d in found[start:start + 20])
                    for item in await self.call(client, "GET", f"/v1.0/iot-03/devices/status?device_ids={ids}") or []:
                        statuses[item["id"]] = {s["code"]: s["value"] for s in item.get("status") or []}
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上涂鸦云：{type(exc).__name__}") from exc
        out = []
        for device in found:
            status = statuses.get(device["id"], {})
            kind = CATEGORIES.get(device.get("category", ""), "other")
            switch = next((c for c in SWITCHES if c in status), None)
            actions, state = [], {}
            if switch:
                actions += ["turn_on", "turn_off", "toggle"]
                state["on"] = bool(status[switch])
            if any(c in status for c in BRIGHTNESS):
                actions.append("set_brightness")
                state["brightness"] = round(status[next(c for c in BRIGHTNESS if c in status)] / 10)
            if any(c in status for c in COLOR_TEMPERATURE):
                actions.append("set_color_temperature")
            if any(c in status for c in COLOR):
                actions.append("set_color")
            if "temp_set" in status:
                actions.append("set_temperature")
                state["target_temperature"] = status["temp_set"]
            if "temp_current" in status:
                state["temperature"] = status["temp_current"]
            if "mode" in status:
                actions.append("set_mode")
                state["mode"] = status["mode"]
            if "control" in status:
                actions += ["open", "close"]
            if "percent_control" in status:
                actions.append("set_position")
                state["position"] = status["percent_control"]
            out.append(self.device(device["id"], device.get("custom_name") or device.get("name"), kind, online=device.get("is_online", True), state=state, actions=actions))
        return out

    async def control(self, native_id, action, value=None):
        try:
            async with self.client() as client:
                items = await self.call(client, "GET", f"/v1.0/iot-03/devices/status?device_ids={native_id}") or [{}]
                status = {s["code"]: s["value"] for s in items[0].get("status") or []}

                def code(options, what):
                    found = next((c for c in options if c in status), None)
                    if not found:
                        raise ConnectorError(f"这个涂鸦设备不支持{what}")
                    return found
                if action in ("turn_on", "turn_off", "toggle"):
                    switch = code(SWITCHES, "开关")
                    commands = [{"code": switch, "value": (not status[switch]) if action == "toggle" else action == "turn_on"}]
                elif action == "set_brightness":
                    name = code(BRIGHTNESS, "调光")
                    commands = [{"code": name, "value": max(10, round(number(value, 0, 100, "亮度") * 10))}]
                elif action == "set_color_temperature":
                    # Tuya takes 0 (warmest) to 1000 (coolest); map 2700 K - 6500 K onto it.
                    name = code(COLOR_TEMPERATURE, "色温")
                    kelvin = number(value, 2000, 9000, "色温")
                    commands = [{"code": name, "value": round(min(1, max(0, (kelvin - 2700) / 3800)) * 1000)}]
                elif action == "set_color":
                    import colorsys
                    name = code(COLOR, "调色")
                    h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in parse_color(value)))
                    commands = [{"code": name, "value": {"h": round(h * 360), "s": round(s * 1000), "v": round(v * 1000)}}]
                    if "work_mode" in status:
                        commands.insert(0, {"code": "work_mode", "value": "colour"})
                elif action == "set_temperature":
                    commands = [{"code": code(("temp_set",), "设定温度"), "value": round(number(value, 5, 90, "温度"))}]
                elif action == "set_mode":
                    commands = [{"code": code(("mode",), "模式"), "value": str(value)}]
                elif action in ("open", "close"):
                    commands = [{"code": code(("control",), "开合"), "value": action}]
                elif action == "set_position":
                    commands = [{"code": code(("percent_control",), "位置"), "value": round(number(value, 0, 100, "位置"))}]
                else:
                    raise ConnectorError("涂鸦设备不支持这个操作")
                await self.call(client, "POST", f"/v1.0/iot-03/devices/{native_id}/commands", {"commands": commands})
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上涂鸦云：{type(exc).__name__}") from exc
