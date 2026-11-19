"""LIFX through its cloud HTTP API (https://api.lifx.com/v1) with a personal access token from https://cloud.lifx.com/settings."""
import colorsys

import httpx

from . import Connector, ConnectorError, number, parse_color


class Lifx(Connector):
    kind = "lifx"
    title = "LIFX"
    description = "通过 LIFX 云端 HTTP API 控制灯和场景，需要在 cloud.lifx.com 生成的个人访问令牌。"
    fields = [{"name": "token", "label": "个人访问令牌", "secret": True, "placeholder": "在 cloud.lifx.com/settings 生成"}]

    def client(self):
        return httpx.AsyncClient(base_url="https://api.lifx.com", timeout=20, headers={"Authorization": f"Bearer {self.config['token']}"}, transport=self.transport)

    @staticmethod
    def check(response):
        if response.status_code in (401, 403):
            raise ConnectorError("LIFX 拒绝了访问令牌，请到 cloud.lifx.com/settings 重新生成并填写")
        if response.status_code == 404:
            raise ConnectorError("LIFX 找不到这个灯或场景，可能已被删除，请刷新设备列表")
        if response.status_code == 429:
            raise ConnectorError("LIFX 请求太频繁，请稍后再试")
        if response.status_code >= 400:
            raise ConnectorError(f"LIFX 云端返回 {response.status_code}，请稍后再试")
        return response

    async def devices(self):
        try:
            async with self.client() as client:
                lights = self.check(await client.get("/v1/lights/all")).json()
                scenes = self.check(await client.get("/v1/scenes")).json()
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 LIFX 云端：{type(exc).__name__}，请检查网络") from exc
        out = []
        for light in lights:
            capabilities = (light.get("product") or {}).get("capabilities") or {}
            color = light.get("color") or {}
            brightness = light.get("brightness")
            actions = ["turn_on", "turn_off", "toggle", "set_brightness"]
            state = {"on": light.get("power") == "on", "brightness": round(brightness * 100) if brightness is not None else None}
            if capabilities.get("has_color"):
                actions.append("set_color")
            if capabilities.get("has_variable_color_temp"):
                actions.append("set_color_temperature")
            if capabilities.get("has_color") and color.get("saturation"):
                rgb = colorsys.hsv_to_rgb((color.get("hue") or 0) / 360, color["saturation"], 1)
                state["color"] = "#" + "".join(f"{round(c * 255):02x}" for c in rgb)
            elif color.get("kelvin"):
                state["color_temperature"] = color["kelvin"]
            out.append(self.device("light/" + light["id"], light.get("label"), "light", room=(light.get("group") or {}).get("name", ""),
                                   online=light.get("connected", True), state=state, actions=actions))
        for scene in scenes:
            out.append(self.device("scene/" + scene["uuid"], scene.get("name"), "scene", actions=["activate"]))
        return out

    async def control(self, native_id, action, value=None):
        kind, ident = native_id.split("/", 1)
        if kind == "scene":
            if action != "activate":
                raise ConnectorError("LIFX 场景只能执行 activate")
            method, path, body = "PUT", f"/v1/scenes/scene_id:{ident}/activate", None
        elif action == "toggle":
            method, path, body = "POST", f"/v1/lights/id:{ident}/toggle", None
        else:
            method, path = "PUT", f"/v1/lights/id:{ident}/state"
            if action in ("turn_on", "turn_off"):
                body = {"power": "on" if action == "turn_on" else "off"}
            elif action == "set_brightness":
                level = number(value, 0, 100, "亮度")
                body = {"power": "on", "brightness": level / 100} if level > 0 else {"power": "off"}
            elif action == "set_color":
                body = {"power": "on", "color": "#%02x%02x%02x" % parse_color(value)}
            elif action == "set_color_temperature":
                body = {"power": "on", "color": f"kelvin:{round(number(value, 1500, 9000, '色温'))}"}
            else:
                raise ConnectorError("LIFX 灯不支持这个操作")
        try:
            async with self.client() as client:
                response = self.check(await client.request(method, path, json=body))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 LIFX 云端：{type(exc).__name__}，请检查网络") from exc
        try:
            results = response.json().get("results") or []
        except ValueError:
            results = []
        if results and all(r.get("status") != "ok" for r in results):
            raise ConnectorError("LIFX 灯没有响应，可能已断电或离线，请检查灯的电源和 Wi-Fi")
