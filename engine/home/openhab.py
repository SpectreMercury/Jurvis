"""openHAB through its REST API, usually on the local network (http://<host>:8080).

Written to https://www.openhab.org/docs/configuration/restdocs.html (GET /rest/items, POST /rest/items/{name} with a text/plain command, the API
token sent as the basic-auth user name with an empty password), https://www.openhab.org/docs/concepts/items.html (item types, command formats)
and openhab-core's ItemResource for GET /rest/items/{name}. Items become devices by item type, refined by semantic tags.
A Rollershutter counts 0 as up and 100 as down; positions are flipped so that 100 is fully open here.
"""
import colorsys

import httpx

from . import Connector, ConnectorError, number, parse_color

LIGHT_TAGS = {"Lightbulb", "LightStripe", "LightStrip", "LightSource", "Lamp", "Light"}
COVER_TAGS = {"GarageDoor", "Door", "FrontDoor", "BackDoor", "InnerDoor", "CellarDoor", "SideDoor", "Gate", "Blinds", "Window"}
SWITCHING = ["turn_on", "turn_off", "toggle"]


def classify(item):
    """An item -> (device type, actions), or None for item types that are not devices (groups, strings, dates, …)."""
    kind, tags = str(item.get("type") or ""), set(item.get("tags") or [])
    if kind == "Switch":
        if "Lock" in tags:
            return "lock", ["lock", "unlock"]
        if tags & COVER_TAGS:
            return "cover", ["open", "close"]
        return ("light" if tags & LIGHT_TAGS else "outlet" if "PowerOutlet" in tags else "fan" if tags & {"Fan", "CeilingFan", "KitchenHood"} else "switch"), SWITCHING
    if kind == "Dimmer":
        return "light", SWITCHING + ["set_brightness"]
    if kind == "Color":
        return "light", SWITCHING + ["set_brightness", "set_color"]
    if kind == "Rollershutter":
        return "cover", ["open", "close", "set_position"]
    if kind == "Number:Temperature":
        return ("climate", ["set_temperature"]) if "Setpoint" in tags else ("sensor", [])
    if kind == "Contact" or (kind.startswith("Number") and tags & {"Measurement", "Humidity", "Temperature", "Power", "Energy", "CO2", "Light"}):
        return "sensor", []
    return None


def quantity(state):
    """"21.5 °C" -> (21.5, "°C"); (None, "") for NULL, UNDEF and anything that is not a number."""
    text = str(state or "").split(" ", 1)
    try:
        return float(text[0]), text[1].strip() if len(text) > 1 else ""
    except ValueError:
        return None, ""


def celsius(state):
    value, unit = quantity(state)
    return value if value is None or "F" not in unit else round((value - 32) * 5 / 9, 1)


class OpenHAB(Connector):
    kind = "openhab"
    title = "openHAB"
    description = "通过 REST API 连接自己搭建的 openHAB（通常在局域网内），按 Item 类型和语义标签识别开关、灯、卷帘、温度和门窗传感器。"
    fields = [{"name": "url", "label": "openHAB 地址", "placeholder": "http://192.168.1.10:8080"},
              {"name": "token", "label": "API 令牌", "secret": True, "optional": True, "placeholder": "在 openHAB 个人资料页点 Create new API token；局域网允许匿名访问时可留空"}]

    def client(self):
        url = str(self.config.get("url", "")).strip().rstrip("/")
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        token = self.config.get("token")
        return httpx.AsyncClient(base_url=url.removesuffix("/rest") + "/rest", timeout=20, transport=self.transport, auth=(token, "") if token else None)

    @staticmethod
    def check(response):
        if response.status_code in (401, 403):
            raise ConnectorError("openHAB 拒绝了访问：请在个人资料页创建 API 令牌并填入；若已填写，请确认令牌没有被删除")
        if response.status_code == 404:
            raise ConnectorError("openHAB 里找不到这个 Item，可能已被改名或删除")
        if response.status_code == 400:
            raise ConnectorError("openHAB 不接受这个命令，这个 Item 的类型可能不支持该操作")
        if response.status_code >= 400:
            raise ConnectorError(f"openHAB 没有执行这个请求（HTTP {response.status_code}），请查看 openHAB 的日志")
        return response

    @staticmethod
    def state(item, kind):
        raw, item_type = item.get("state"), item.get("type")
        if raw in (None, "NULL", "UNDEF"):
            return {}
        if item_type == "Switch":
            return {"locked": raw == "ON"} if kind == "lock" else {"open": raw == "ON"} if kind == "cover" else {"on": raw == "ON"}
        if item_type == "Dimmer":
            level, _ = quantity(raw)
            return {"on": level > 0, "brightness": round(level)} if level is not None else {}
        if item_type == "Color":
            try:
                h, s, b = (float(part) for part in str(raw).split(","))
            except ValueError:
                return {}
            return {"on": b > 0, "brightness": round(b), "color": "#%02x%02x%02x" % tuple(round(c * 255) for c in colorsys.hsv_to_rgb(h / 360, s / 100, b / 100))}
        if item_type == "Rollershutter":
            level, _ = quantity(raw)
            return {"position": round(100 - level)} if level is not None else {}
        if item_type == "Number:Temperature":
            return {"target_temperature" if kind == "climate" else "temperature": celsius(raw)}
        if item_type == "Contact":
            return {"contact": "open" if raw == "OPEN" else "closed"}
        value, unit = quantity(raw)
        return {"humidity": value} if "Humidity" in (item.get("tags") or []) else {"value": raw if value is None else value, "unit": unit or None}

    async def devices(self):
        try:
            async with self.client() as client:
                items = self.check(await client.get("/items", params={"recursive": "false"})).json()
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 openHAB，请检查地址和端口（默认 8080）：{type(exc).__name__}") from exc
        except ValueError as exc:
            raise ConnectorError("这个地址返回的不是 openHAB 的数据，请检查地址和端口（默认 8080）") from exc
        out = []
        for item in items if isinstance(items, list) else []:
            mapped = classify(item)
            if mapped:
                out.append(self.device(item["name"], item.get("label") or item["name"], mapped[0], state=self.state(item, mapped[0]), actions=mapped[1]))
        return out

    async def control(self, native_id, action, value=None):
        try:
            async with self.client() as client:
                item = self.check(await client.get(f"/items/{native_id}")).json()
                mapped = classify(item)
                if not mapped or action not in mapped[1]:
                    raise ConnectorError("这个 openHAB Item 不支持该操作")
                shutter = item.get("type") == "Rollershutter"
                if action == "toggle":
                    command = "OFF" if self.state(item, mapped[0]).get("on") else "ON"
                elif action in ("turn_on", "turn_off"):
                    command = "ON" if action == "turn_on" else "OFF"
                elif action in ("open", "close"):
                    command = ("UP" if action == "open" else "DOWN") if shutter else ("ON" if action == "open" else "OFF")
                elif action in ("lock", "unlock"):
                    command = "ON" if action == "lock" else "OFF"
                elif action == "set_brightness":
                    command = str(round(number(value, 0, 100, "亮度")))
                elif action == "set_position":
                    command = str(100 - round(number(value, 0, 100, "位置")))
                elif action == "set_color":
                    h, s, b = colorsys.rgb_to_hsv(*(c / 255 for c in parse_color(value)))
                    command = f"{round(h * 360)},{round(s * 100)},{round(b * 100)}"
                else:  # set_temperature: a quantity with its unit, which openHAB converts to the item's own unit
                    command = f"{round(number(value, 5, 40, '温度'), 1):g} °C"
                self.check(await client.post(f"/items/{native_id}", content=command.encode(), headers={"Content-Type": "text/plain", "Accept": "application/json"}))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 openHAB，请检查地址和端口（默认 8080）：{type(exc).__name__}") from exc
