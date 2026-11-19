"""Home Assistant: the widest bridge. Anything Home Assistant integrates (Matter,
HomeKit devices, Zigbee, Mi Home, Tuya, Aqara, Ecobee, Sonos and a few thousand
more) is reachable through its REST API with one long-lived access token."""
import httpx

from . import Connector, ConnectorError, number, parse_color

DOMAINS = {"light": "light", "switch": "switch", "input_boolean": "switch", "climate": "climate", "cover": "cover", "lock": "lock", "fan": "fan",
           "media_player": "media", "vacuum": "vacuum", "sensor": "sensor", "binary_sensor": "sensor", "camera": "camera", "scene": "scene",
           "script": "scene", "humidifier": "other", "water_heater": "climate"}
ACTIONS = {"light": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_color_temperature"], "switch": ["turn_on", "turn_off", "toggle"],
           "input_boolean": ["turn_on", "turn_off", "toggle"], "climate": ["turn_on", "turn_off", "set_temperature", "set_mode"],
           "water_heater": ["turn_on", "turn_off", "set_temperature", "set_mode"], "cover": ["open", "close", "set_position"], "lock": ["lock", "unlock"],
           "fan": ["turn_on", "turn_off", "toggle", "set_mode"], "media_player": ["turn_on", "turn_off", "toggle"], "vacuum": ["turn_on", "turn_off"],
           "scene": ["activate"], "script": ["activate"], "humidifier": ["turn_on", "turn_off", "toggle", "set_mode"]}
AREAS = "{% for s in states %}{{ s.entity_id }}\t{{ area_name(s.entity_id) or '' }}\n{% endfor %}"


class HomeAssistant(Connector):
    kind = "homeassistant"
    title = "Home Assistant"
    description = "通过 Home Assistant 接入 Matter、HomeKit、Zigbee、米家、涂鸦、Aqara 等几千种设备。"
    fields = [{"name": "url", "label": "地址", "placeholder": "http://homeassistant.local:8123"},
              {"name": "token", "label": "长期访问令牌", "secret": True, "placeholder": "在 Home Assistant 个人资料页创建"}]

    def client(self):
        return httpx.AsyncClient(base_url=self.config["url"].rstrip("/"), timeout=20, transport=self.transport,
                                 headers={"Authorization": f"Bearer {self.config['token']}", "Content-Type": "application/json"})

    @staticmethod
    def check(response):
        if response.status_code == 401:
            raise ConnectorError("Home Assistant 拒绝了令牌，请重新创建长期访问令牌")
        if response.status_code >= 400:
            raise ConnectorError(f"Home Assistant 返回 {response.status_code}")
        return response

    async def devices(self):
        try:
            async with self.client() as client:
                states = self.check(await client.get("/api/states")).json()
                areas = {}
                reply = await client.post("/api/template", json={"template": AREAS})
                if reply.status_code == 200:
                    areas = dict(line.split("\t", 1) for line in reply.text.splitlines() if "\t" in line)
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Home Assistant：{type(exc).__name__}") from exc
        out = []
        for entity in states:
            ident = entity.get("entity_id", "")
            domain = ident.split(".", 1)[0]
            if domain not in DOMAINS:
                continue
            a, value = entity.get("attributes") or {}, entity.get("state")
            state = {"on": value not in ("off", "unavailable", "unknown", "idle", "standby", "closed", "locked", "docked")} if domain in ("light", "switch", "input_boolean", "fan", "media_player", "vacuum", "humidifier") else {}
            if domain in ("sensor", "binary_sensor"):
                state = {"value": value, "unit": a.get("unit_of_measurement"), "class": a.get("device_class")}
            elif domain == "light":
                rgb = a.get("rgb_color")
                state.update(brightness=round(a["brightness"] / 255 * 100) if a.get("brightness") is not None else None,
                             color_temperature=a.get("color_temp_kelvin"), color="#%02x%02x%02x" % tuple(rgb[:3]) if rgb else None)
            elif domain in ("climate", "water_heater"):
                state = {"mode": value, "temperature": a.get("current_temperature"), "target_temperature": a.get("temperature"), "modes": a.get("hvac_modes") or a.get("operation_list")}
            elif domain == "cover":
                state = {"open": value == "open", "position": a.get("current_position")}
            elif domain == "lock":
                state = {"locked": value == "locked"}
            elif domain in ("fan", "humidifier"):
                state.update(mode=a.get("preset_mode") or a.get("mode"), modes=a.get("preset_modes") or a.get("available_modes"))
            elif domain == "media_player":
                state.update(playing=a.get("media_title"), volume=round(a["volume_level"] * 100) if a.get("volume_level") is not None else None)
            out.append(self.device(ident, a.get("friendly_name") or ident, DOMAINS[domain], room=areas.get(ident, ""), online=value != "unavailable",
                                   state=state, actions=ACTIONS.get(domain, [])))
        return out

    async def control(self, native_id, action, value=None):
        domain = native_id.split(".", 1)[0]
        data = {"entity_id": native_id}
        if action in ("turn_on", "turn_off", "toggle"):
            service = (domain, action if domain != "vacuum" else {"turn_on": "start", "turn_off": "return_to_base"}.get(action, "start"))
        elif action == "set_brightness":
            service, data["brightness_pct"] = ("light", "turn_on"), round(number(value, 0, 100, "亮度"))
        elif action == "set_color":
            service, data["rgb_color"] = ("light", "turn_on"), list(parse_color(value))
        elif action == "set_color_temperature":
            service, data["color_temp_kelvin"] = ("light", "turn_on"), round(number(value, 1500, 9000, "色温"))
        elif action == "set_temperature":
            service, data["temperature"] = (domain, "set_temperature"), number(value, 5, 90, "温度")
        elif action == "set_mode":
            if not value:
                raise ConnectorError("请给出模式")
            if domain == "climate":
                service, data["hvac_mode"] = ("climate", "set_hvac_mode"), str(value)
            elif domain == "water_heater":
                service, data["operation_mode"] = ("water_heater", "set_operation_mode"), str(value)
            elif domain == "humidifier":
                service, data["mode"] = ("humidifier", "set_mode"), str(value)
            else:
                service, data["preset_mode"] = ("fan", "set_preset_mode"), str(value)
        elif action in ("open", "close"):
            service = ("cover", f"{action}_cover")
        elif action == "set_position":
            service, data["position"] = ("cover", "set_cover_position"), round(number(value, 0, 100, "位置"))
        elif action in ("lock", "unlock"):
            service = ("lock", action)
        elif action == "activate":
            service = (domain, "turn_on")
        else:
            raise ConnectorError("不支持的操作")
        try:
            async with self.client() as client:
                self.check(await client.post(f"/api/services/{service[0]}/{service[1]}", json=data))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Home Assistant：{type(exc).__name__}") from exc
