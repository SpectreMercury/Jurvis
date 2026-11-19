"""LG ThinQ Connect (the open cloud API), with a Personal Access Token from https://connect-pat.lgthinq.com.

Written to LG's own SDK, https://github.com/thinq-connect/pythinqconnect (thinq_api.py, country.py, devices/*.py): the header set, the regional
hosts, and the {resource: {property: value}} control payload all come from there. A device's profile says which properties are writable and
with which values, so nothing is sent that the profile does not list.

Air conditioners, purifiers, fans and (de)humidifiers can be switched; washers, dryers, dishwashers, ovens, fridges and the rest are read-only
here on purpose: starting a heating or water appliance should be done by a person standing next to it.
"""
import asyncio
import base64
import hashlib
import uuid

import httpx

from . import Connector, ConnectorError, number

# The API key LG publishes in its open SDK (thinqconnect/const.py); it identifies the open API, not the user.
API_KEY = "v6GFvkweNo7DK7yD3ylIZ9w52aKBU0eJ7wLXkSR3"
REGIONS = {
    "kic": "AU BD CN HK ID IN JP KH KR LA LK MM MY NP NZ PH SG TH TW VN",
    "aic": "AG AR AW BB BO BR BS BZ CA CL CO CR CU DM DO EC GD GT GY HN HT JM KN LC MX NI PA PE PR PY SR SV TT US UY VC VE",
    "eic": "AE AF AL AM AO AT AZ BA BE BF BG BH BJ BY CD CF CG CH CI CM CV CY CZ DE DJ DK DZ EE EG ES ET FI FR GA GB GE GH GM GN GQ GR HR HU IE IL IQ IR IS IT "
           "JO KE KG KW KZ LB LR LT LU LV LY MA MD ME MK ML MR MT MU MW NE NG NL NO OM PK PL PS PT QA RO RS RU RW SA SD SE SI SK SL SN SO ST SY TD TG TN TR "
           "TZ UA UG UZ XK YE ZA ZM",
}
# deviceType -> (our type, power property under "operation", resource holding currentJobMode)
SWITCHABLE = {"DEVICE_AIR_CONDITIONER": ("climate", "airConOperationMode", "airConJobMode"),
              "DEVICE_AIR_PURIFIER": ("fan", "airPurifierOperationMode", "airPurifierJobMode"),
              "DEVICE_AIR_PURIFIER_FAN": ("fan", "airFanOperationMode", "airFanJobMode"),
              "DEVICE_CEILING_FAN": ("fan", "ceilingfanOperationMode", None),
              "DEVICE_DEHUMIDIFIER": ("switch", "dehumidifierOperationMode", "dehumidifierJobMode"),
              "DEVICE_HUMIDIFIER": ("switch", "humidifierOperationMode", "humidifierJobMode")}
READ_ONLY = {"DEVICE_REFRIGERATOR": "sensor", "DEVICE_WINE_CELLAR": "sensor", "DEVICE_KIMCHI_REFRIGERATOR": "sensor", "DEVICE_ROBOT_CLEANER": "vacuum"}
ERRORS = {"1103": "LG 拒绝了访问令牌，请到 connect-pat.lgthinq.com 重新生成 PAT", "1218": "LG 拒绝了访问令牌，请到 connect-pat.lgthinq.com 重新生成 PAT",
          "1302": "LG 找不到这个访问令牌，请到 connect-pat.lgthinq.com 重新生成 PAT", "1306": "LG ThinQ 接口调用次数超限，请过一会儿再试",
          "1307": "LG ThinQ 开放接口不支持这个国家代码，请填写 ThinQ 账号注册时的国家", "1310": "国家代码与 ThinQ 账号所在区域不符，请填写账号注册时的国家",
          "1304": "需要先在 LG ThinQ App 里同意最新的服务条款", "1308": "这个账号没有该设备的控制权限", "1222": "设备离线，请检查电源和 Wi-Fi",
          "1205": "LG 找不到这个设备，可能已从账号移除", "2301": "设备没有开启远程控制，请在机器上打开“远程控制/Remote Start”后再试",
          "2302": "设备当前状态下不能执行这个操作", "2304": "设备处于关机状态，请先开机", "2305": "设备当前模式下不能执行这个操作",
          "2208": "设备没有执行这个命令，请稍后再试", "2209": "设备响应超时，请稍后再试"}


def celsius(value, unit):
    if not isinstance(value, (int, float)):
        return None
    return round((value - 32) * 5 / 9, 1) if str(unit).upper() == "F" else value


def writable(profile, resource, name):
    """The values a profile lets us write to resource.name: a list for enums, a dict (min/max/step) for ranges, None when read-only."""
    spec = ((profile.get("property") or {}).get(resource) if isinstance(profile.get("property"), dict) else None) or {}
    if isinstance(spec, list):  # temperatureInUnits: one entry per unit
        spec = next((s for s in spec if s.get("unit") == "C"), {})
    spec = spec.get(name)
    if not isinstance(spec, dict) or "w" not in (spec.get("mode") or []):
        return None
    return (spec.get("value") or {}).get("w")


class LGThinQ(Connector):
    kind = "lgthinq"
    title = "LG ThinQ"
    description = "LG ThinQ Connect 官方开放云 API：空调可开关、调温、换模式，净化器/风扇/除湿机可开关，洗衣机、冰箱等只读取运行状态。"
    fields = [{"name": "token", "label": "个人访问令牌 (PAT)", "secret": True, "placeholder": "在 connect-pat.lgthinq.com 用 ThinQ 账号登录后创建"},
              {"name": "country", "label": "国家代码", "placeholder": "ThinQ 账号注册时的国家，如 CN / US / KR / DE"}]
    profiles = {}  # device id -> profile; profiles do not change, and LG counts every call

    def client(self):
        country = str(self.config.get("country", "")).strip().upper()
        region = next((r for r, countries in REGIONS.items() if country in countries.split()), None)
        if not region:
            raise ConnectorError("国家代码需是两位字母（如 CN、US、KR、DE），并且是 LG ThinQ 开放接口支持的国家")
        # LG asks for one stable, unique client id per client; derive it from the token so it survives restarts without being stored.
        client_id = str(uuid.UUID(hashlib.sha256(("jurvis:" + self.config["token"]).encode()).hexdigest()[:32]))
        headers = {"Authorization": f"Bearer {self.config['token']}", "x-country": country, "x-client-id": client_id, "x-api-key": API_KEY, "x-service-phase": "OP"}
        return httpx.AsyncClient(base_url=f"https://api-{region}.lgthinq.com", timeout=20, transport=self.transport, headers=headers)

    async def call(self, client, method, path, payload=None, headers=None):
        message_id = base64.urlsafe_b64encode(uuid.uuid4().bytes)[:-2].decode()
        response = await client.request(method, path, json=payload, headers={"x-message-id": message_id, **(headers or {})})
        try:
            reply = response.json()
        except ValueError:
            reply = {}
        if response.status_code < 400:
            return reply.get("response")
        error = reply.get("error") or {}
        code = str(error.get("code", ""))
        if code in ERRORS:
            raise ConnectorError(ERRORS[code])
        if response.status_code in (401, 403):
            raise ConnectorError("LG 拒绝了访问令牌，请检查 PAT 和它的权限")
        raise ConnectorError(f"LG ThinQ 没有执行这个请求：{error.get('message') or 'HTTP ' + str(response.status_code)}")

    async def profile(self, client, device_id):
        if device_id not in LGThinQ.profiles:
            LGThinQ.profiles[device_id] = await self.call(client, "GET", f"/devices/{device_id}/profile") or {}
        return LGThinQ.profiles[device_id]

    @staticmethod
    def temperatures(state):
        """(current, target) in °C from temperatureInUnits (a list with one entry per unit) or the older single "temperature" resource."""
        units = state.get("temperatureInUnits")
        entry = next((u for u in units if u.get("unit") == "C"), units[0] if units else {}) if isinstance(units, list) else (state.get("temperature") or {})
        if not isinstance(entry, dict):
            return None, None
        return celsius(entry.get("currentTemperature"), entry.get("unit", "C")), celsius(entry.get("targetTemperature"), entry.get("unit", "C"))

    def normalize(self, item, state, profile, online):
        info = item.get("deviceInfo") or {}
        device_type, name = info.get("deviceType", ""), info.get("alias") or info.get("modelName")
        if device_type in SWITCHABLE:
            kind, power, job = SWITCHABLE[device_type]
            main = state if isinstance(state, dict) else {}
            values = writable(profile, "operation", power) or []
            actions = ["turn_on", "turn_off", "toggle"] if "POWER_ON" in values and "POWER_OFF" in values else []
            current, target = self.temperatures(main)
            mode = (main.get(job) or {}).get("currentJobMode") if job else None
            air = main.get("airQualitySensor") or {}
            shown = {"on": (main.get("operation") or {}).get(power) == "POWER_ON" if (main.get("operation") or {}).get(power) else None,
                     "mode": mode.lower() if isinstance(mode, str) else None, "temperature": current, "target_temperature": target,
                     "humidity": air.get("humidity") or (main.get("humidity") or {}).get("currentHumidity"), "pm25": air.get("PM2")}
            if kind == "climate":
                modes = [m.lower() for m in writable(profile, job, "currentJobMode") or []]
                shown["modes"] = modes or None
                actions += (["set_temperature"] if writable(profile, "temperatureInUnits", "targetTemperature") else []) + (["set_mode"] if modes else [])
            return self.device(item["deviceId"], name, kind, online=online, state=shown, actions=actions)
        # Everything else is read-only. Appliances with several compartments report a list, one entry per location.
        parts = state if isinstance(state, list) else [state] if isinstance(state, dict) else []
        main = parts[0] if parts else {}
        timer = main.get("timer") or {}
        remaining = timer.get("remainHour", 0) * 60 + timer.get("remainMinute", 0) if "remainHour" in timer or "remainMinute" in timer else None
        run = (main.get("runState") or {}).get("currentState")
        shown = {"value": run.lower() if isinstance(run, str) else None, "remaining_minutes": remaining,
                 "remote_control": (main.get("remoteControlEnable") or {}).get("remoteControlEnabled")}
        for entry in main.get("temperatureInUnits") or [] if isinstance(main.get("temperatureInUnits"), list) else []:
            if entry.get("locationName"):
                shown[f"{entry['locationName'].lower()}_target_temperature"] = celsius(entry.get("targetTemperature"), entry.get("unit", "C"))
        doors = [d for d in main.get("doorStatus") or [] if isinstance(d, dict)] if isinstance(main.get("doorStatus"), list) else []
        if doors:
            shown["contact"] = "open" if any(str(d.get("doorState", "")).upper() == "OPEN" for d in doors) else "closed"
        return self.device(item["deviceId"], name, READ_ONLY.get(device_type, "other"), online=online, state=shown)

    async def devices(self):
        try:
            async with self.client() as client:
                listed = await self.call(client, "GET", "/devices") or []
                gate = asyncio.Semaphore(4)

                async def load(item):
                    async with gate:
                        try:
                            state = await self.call(client, "GET", f"/devices/{item['deviceId']}/state")
                            switchable = (item.get("deviceInfo") or {}).get("deviceType") in SWITCHABLE
                            return state, await self.profile(client, item["deviceId"]) if switchable else {}, True
                        except ConnectorError:
                            return None, {}, False  # offline or not readable right now: still list it
                loaded = await asyncio.gather(*(load(item) for item in listed))
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 LG ThinQ 云端，请检查网络：{type(exc).__name__}") from exc
        return [self.normalize(item, state, profile, online) for item, (state, profile, online) in zip(listed, loaded)]

    async def control(self, native_id, action, value=None):
        try:
            async with self.client() as client:
                listed = await self.call(client, "GET", "/devices") or []
                item = next((d for d in listed if d.get("deviceId") == native_id), None)
                device_type = ((item or {}).get("deviceInfo") or {}).get("deviceType")
                if device_type not in SWITCHABLE:
                    raise ConnectorError("这类 LG 设备在这里只能查看状态，不能远程控制")
                _, power, job = SWITCHABLE[device_type]
                profile = await self.profile(client, native_id)
                if action in ("turn_on", "turn_off", "toggle"):
                    if action == "toggle":
                        state = await self.call(client, "GET", f"/devices/{native_id}/state") or {}
                        action = "turn_off" if (state.get("operation") or {}).get(power) == "POWER_ON" else "turn_on"
                    wanted = "POWER_ON" if action == "turn_on" else "POWER_OFF"
                    if wanted not in (writable(profile, "operation", power) or []):
                        raise ConnectorError("这台 LG 设备不支持远程开关")
                    payload = {"operation": {power: wanted}}
                elif action == "set_temperature" and device_type == "DEVICE_AIR_CONDITIONER":
                    limits = writable(profile, "temperatureInUnits", "targetTemperature")
                    if not limits:
                        raise ConnectorError("这台 LG 空调不支持远程设定温度")
                    low, high = (limits.get("min", 16), limits.get("max", 30)) if isinstance(limits, dict) else (16, 30)
                    target = number(value, low, high, "温度")
                    payload = {"temperatureInUnits": {"targetTemperature": int(target) if target == int(target) else target, "unit": "C"}}
                elif action == "set_mode" and device_type == "DEVICE_AIR_CONDITIONER":
                    modes = writable(profile, job, "currentJobMode") or []
                    wanted = str(value or "").strip().upper()
                    if wanted not in modes:
                        raise ConnectorError("这台 LG 空调支持的模式是：" + "、".join(m.lower() for m in modes) if modes else "这台 LG 空调不支持远程切换模式")
                    payload = {job: {"currentJobMode": wanted}}
                else:
                    raise ConnectorError("这台 LG 设备不支持这个操作")
                await self.call(client, "POST", f"/devices/{native_id}/control", payload, {"x-conditional-control": "true"})
        except httpx.HTTPError as exc:
            raise ConnectorError(f"连不上 LG ThinQ 云端，请检查网络：{type(exc).__name__}") from exc
