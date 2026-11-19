"""Nanoleaf panels (Light Panels, Canvas, Shapes, Elements, Lines) over the Open API the controller serves on the local network.

The token comes from the controller itself: hold its power button for 5-7 seconds until the LEDs flash, then within 30 seconds
send `POST http://<address>:16021/api/v1/new`; the reply is {"auth_token": "..."}.
"""
import colorsys

import httpx

from . import Connector, ConnectorError, number, parse_color

PORT = 16021


class Nanoleaf(Connector):
    kind = "nanoleaf"
    title = "Nanoleaf"
    description = "直接连接局域网里的 Nanoleaf 灯板控制器（Open API），不经过云端。"
    fields = [{"name": "host", "label": "控制器地址", "placeholder": "192.168.1.30"},
              {"name": "token", "label": "授权令牌", "secret": True, "placeholder": "长按电源键 5–7 秒后向 /api/v1/new 发 POST 得到的 auth_token"}]

    def client(self):
        host = self.config["host"].strip().removeprefix("http://").rstrip("/")
        if ":" not in host:
            host = f"{host}:{PORT}"
        return httpx.AsyncClient(base_url=f"http://{host}/api/v1/{self.config['token']}", timeout=10, transport=self.transport)

    @staticmethod
    def check(response):
        if response.status_code in (401, 403):
            raise ConnectorError("Nanoleaf 拒绝了授权令牌，请长按控制器电源键 5–7 秒后重新创建令牌")
        if response.status_code == 422:
            raise ConnectorError("Nanoleaf 不接受这个取值，请换一个在范围内的值")
        if response.status_code == 404:
            raise ConnectorError("Nanoleaf 找不到这个灯效或功能，请刷新设备列表后再试")
        if response.status_code >= 400:
            raise ConnectorError(f"Nanoleaf 控制器返回 {response.status_code}，请确认地址和令牌")
        return response

    def unreachable(self, exc):
        return ConnectorError(f"连不上 Nanoleaf 控制器（{self.config['host']}）：{type(exc).__name__}，请确认它已通电并和这台电脑在同一网络")

    async def devices(self):
        try:
            async with self.client() as client:
                info = self.check(await client.get("/")).json()
        except httpx.HTTPError as exc:
            raise self.unreachable(exc) from exc
        raw, effects = info.get("state") or {}, info.get("effects") or {}
        value = lambda key: (raw.get(key) or {}).get("value")
        mode = raw.get("colorMode")
        state = {"on": value("on"), "brightness": value("brightness"), "modes": [str(e) for e in effects.get("effectsList") or []]}
        if mode == "ct":
            state["color_temperature"] = value("ct")
        elif mode == "hs":
            rgb = colorsys.hsv_to_rgb((value("hue") or 0) / 360, (value("sat") or 0) / 100, 1)
            state["color"] = "#" + "".join(f"{round(c * 255):02x}" for c in rgb)
        elif mode == "effect":
            state["mode"] = effects.get("select")
        actions = ["turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_color_temperature"] + (["set_mode"] if state["modes"] else [])
        return [self.device(info.get("serialNo") or "panels", info.get("name") or "Nanoleaf", "light", state=state, actions=actions)]

    async def control(self, native_id, action, value=None):
        on = {"on": {"value": True}}
        # The Open API documents one attribute per PUT /state, so a colour change is sent as separate writes.
        if action in ("turn_on", "turn_off", "toggle"):
            writes = [("/state", {"on": {"value": action == "turn_on"}})]
        elif action == "set_brightness":
            level = round(number(value, 0, 100, "亮度"))
            writes = [("/state", on), ("/state", {"brightness": {"value": level}})] if level > 0 else [("/state", {"on": {"value": False}})]
        elif action == "set_color":
            h, s, v = colorsys.rgb_to_hsv(*(c / 255 for c in parse_color(value)))
            writes = [("/state", on), ("/state", {"hue": {"value": round(h * 360)}}), ("/state", {"sat": {"value": round(s * 100)}}),
                      ("/state", {"brightness": {"value": max(1, round(v * 100))}})]
        elif action == "set_color_temperature":
            writes = [("/state", on), ("/state", {"ct": {"value": round(number(value, 1200, 6500, "色温"))}})]
        elif action == "set_mode":
            if not str(value or "").strip():
                raise ConnectorError("请给出灯效名称，可用名称见设备状态里的 modes")
            writes = [("/effects", {"select": str(value).strip()})]
        else:
            raise ConnectorError("Nanoleaf 不支持这个操作")
        try:
            async with self.client() as client:
                if action == "toggle":
                    current = self.check(await client.get("/state/on")).json()
                    writes = [("/state", {"on": {"value": not current.get("value")}})]
                for path, body in writes:
                    self.check(await client.put(path, json=body))
        except httpx.HTTPError as exc:
            raise self.unreachable(exc) from exc
