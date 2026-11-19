"""Sensibo smart AC controllers (cloud API v2), with an API key from https://home.sensibo.com/me/api.

Written to https://sensibo.github.io/api/ and its OpenAPI file. The documented way to authenticate these endpoints is the apiKey query
parameter, so that is what is sent; error messages here never include the request URL.
Sensibo rate-limits polling hard, so this connector only reads when asked.
"""
import httpx

from . import Connector, ConnectorError, number

API = "https://home.sensibo.com/api/v2"


def celsius(value, unit):
    if not isinstance(value, (int, float)):
        return None
    return round((value - 32) * 5 / 9, 1) if str(unit).upper() == "F" else value


class Sensibo(Connector):
    kind = "sensibo"
    title = "Sensibo"
    description = "Sensibo 空调控制器（Sky / Air / Air PRO 等）云端 API：开关、温度、模式，以及室内温湿度。"
    fields = [{"name": "key", "label": "API Key", "secret": True, "placeholder": "在 home.sensibo.com/me/api 创建"}]

    def client(self):
        return httpx.AsyncClient(base_url=API, timeout=20, transport=self.transport, params={"apiKey": self.config["key"]})

    @staticmethod
    def check(response):
        if response.status_code in (401, 403):
            raise ConnectorError("Sensibo 拒绝了 API Key，请到 home.sensibo.com/me/api 重新创建后填入")
        if response.status_code == 404:
            raise ConnectorError("Sensibo 找不到这个设备，可能已从账号移除")
        if response.status_code == 429:
            raise ConnectorError("Sensibo 请求过于频繁，被限流了，请过几分钟再试")
        if response.status_code >= 400:
            raise ConnectorError(f"Sensibo 没有执行这个请求（HTTP {response.status_code}），请确认空调控制器在线")
        return response.json().get("result")

    async def devices(self):
        try:
            async with self.client() as client:
                pods = self.check(await client.get("/users/me/pods", params={"fields": "*"})) or []
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Sensibo 云端，请检查网络：{type(exc).__name__}") from exc
        out = []
        for pod in pods:
            ac, measured = pod.get("acState") or {}, pod.get("measurements") or {}
            room = (pod.get("room") or {}).get("name", "")
            modes = list(((pod.get("remoteCapabilities") or {}).get("modes") or {}).keys())
            state = {"on": ac.get("on"), "mode": ac.get("mode"), "target_temperature": celsius(ac.get("targetTemperature"), ac.get("temperatureUnit")),
                     "temperature": measured.get("temperature"), "humidity": measured.get("humidity"), "fan": ac.get("fanLevel"), "modes": modes or None}
            out.append(self.device(pod["id"], room or pod.get("id"), "climate", room=room, online=(pod.get("connectionStatus") or {}).get("isAlive", True),
                                   state=state, actions=["turn_on", "turn_off", "toggle", "set_temperature", "set_mode"]))
        return out

    async def control(self, native_id, action, value=None):
        try:
            async with self.client() as client:
                async def current():
                    return self.check(await client.get(f"/pods/{native_id}", params={"fields": "acState,remoteCapabilities"})) or {}
                if action in ("turn_on", "turn_off"):
                    name, new = "on", action == "turn_on"
                elif action == "toggle":
                    name, new = "on", not ((await current()).get("acState") or {}).get("on")
                elif action == "set_temperature":
                    target = number(value, 10, 35, "温度")
                    unit = ((await current()).get("acState") or {}).get("temperatureUnit", "C")
                    name, new = "targetTemperature", round(target * 9 / 5 + 32) if str(unit).upper() == "F" else round(target)
                elif action == "set_mode":
                    modes = list((((await current()).get("remoteCapabilities") or {}).get("modes") or {}).keys())
                    name, new = "mode", str(value or "").strip().lower()
                    if modes and new not in modes:
                        raise ConnectorError("这台空调支持的模式是：" + "、".join(modes))
                else:
                    raise ConnectorError("Sensibo 空调控制器不支持这个操作")
                self.check(await client.patch(f"/pods/{native_id}/acStates/{name}", json={"newValue": new}))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 Sensibo 云端，请检查网络：{type(exc).__name__}") from exc
