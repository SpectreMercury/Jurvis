"""Connectors for SwitchBot, Sensibo, LG ThinQ, Hubitat, IKEA DIRIGERA and openHAB, each against a mocked platform."""
import base64
import hashlib
import hmac
import json

import httpx
import pytest

from engine.home import ACTIONS, TYPES, ConnectorError
from engine.home.dirigera import Dirigera
from engine.home.hubitat import Hubitat
from engine.home.lgthinq import API_KEY, LGThinQ
from engine.home.openhab import OpenHAB
from engine.home.sensibo import Sensibo
from engine.home.switchbot import SwitchBot


def well_formed(devices):
    for device in devices:
        assert device["type"] in TYPES and set(device["actions"]) <= set(ACTIONS) and isinstance(device["id"], str) and device["name"]
    return {d["id"]: d for d in devices}


@pytest.mark.asyncio
async def test_switchbot_signs_requests_and_maps_devices_and_scenes():
    posts = []
    statuses = {"B1": {"deviceType": "Bot", "power": "off", "battery": 100, "deviceMode": "switchMode"},
                "C1": {"deviceType": "Curtain", "slidePosition": 20, "moving": False, "battery": 40},
                "L1": {"deviceType": "Color Bulb", "power": "on", "brightness": 60, "color": "255:136:0", "colorTemperature": 3000},
                "K1": {"deviceType": "Smart Lock", "lockState": "locked", "doorState": "closed", "battery": 90},
                "M1": {"deviceType": "Meter", "temperature": 26.1, "humidity": 52, "battery": 100}}

    def handler(request):
        assert request.url.host == "api.switch-bot.com" and request.headers["Authorization"] == "tok"
        text = "tok" + request.headers["t"] + request.headers["nonce"]
        assert request.headers["sign"] == base64.b64encode(hmac.new(b"sec", text.encode(), hashlib.sha256).digest()).decode()
        assert len(request.headers["t"]) == 13
        path = request.url.path
        if request.method == "POST":
            posts.append((path, json.loads(request.content) if request.content else None))
            return httpx.Response(200, json={"statusCode": 161 if "/OFF1/" in path else 100, "body": {}, "message": "x"})
        if path == "/v1.1/devices":
            body = {"deviceList": [{"deviceId": "B1", "deviceName": "鱼缸开关", "deviceType": "Bot", "enableCloudService": True, "hubDeviceId": "H"},
                                   {"deviceId": "C1", "deviceName": "客厅窗帘", "deviceType": "Curtain", "enableCloudService": True},
                                   {"deviceId": "L1", "deviceName": "台灯", "deviceType": "Color Bulb", "enableCloudService": True},
                                   {"deviceId": "K1", "deviceName": "大门", "deviceType": "Smart Lock", "enableCloudService": True},
                                   {"deviceId": "M1", "deviceName": "温湿度计", "deviceType": "Meter", "enableCloudService": True},
                                   {"deviceId": "H1", "deviceName": "Hub Mini", "deviceType": "Hub Mini"}], "infraredRemoteList": []}
        elif path == "/v1.1/scenes":
            body = [{"sceneId": "T02-1", "sceneName": "离家"}]
        else:
            body = statuses[path.split("/")[3]]
        return httpx.Response(200, json={"statusCode": 100, "body": body, "message": "success"})
    bot = SwitchBot("sb", {"token": "tok", "secret": "sec"}, httpx.MockTransport(handler))
    devices = well_formed(await bot.devices())
    assert list(devices) == ["switch/B1", "curtain/C1", "light/L1", "lock/K1", "sensor/M1", "scene/T02-1"]
    assert devices["switch/B1"]["type"] == "switch" and devices["switch/B1"]["actions"] == ["turn_on", "turn_off"] and devices["switch/B1"]["state"]["on"] is False
    assert devices["curtain/C1"]["type"] == "cover" and devices["curtain/C1"]["state"]["position"] == 80
    assert devices["light/L1"]["state"] == {"on": True, "brightness": 60, "color": "#ff8800", "color_temperature": 3000}
    assert devices["lock/K1"]["type"] == "lock" and devices["lock/K1"]["state"]["locked"] is True and devices["lock/K1"]["actions"] == ["lock", "unlock"]
    assert devices["sensor/M1"]["state"] == {"temperature": 26.1, "humidity": 52, "battery": 100} and devices["scene/T02-1"]["actions"] == ["activate"]
    await bot.control("switch/B1", "turn_on")
    await bot.control("curtain/C1", "set_position", 70)
    await bot.control("curtain/C1", "open")
    await bot.control("roller/R1", "close")
    await bot.control("light/L1", "set_color", "#ff8800")
    await bot.control("light/L1", "set_color_temperature", 4000)
    await bot.control("lock/K1", "unlock")
    await bot.control("scene/T02-1", "activate")

    def command(name, parameter="default"):
        return {"command": name, "parameter": parameter, "commandType": "command"}
    assert posts == [("/v1.1/devices/B1/commands", command("turnOn")), ("/v1.1/devices/C1/commands", command("setPosition", "0,ff,30")),
                     ("/v1.1/devices/C1/commands", command("turnOn")), ("/v1.1/devices/R1/commands", command("setPosition", 100)),
                     ("/v1.1/devices/L1/commands", command("setColor", "255:136:0")), ("/v1.1/devices/L1/commands", command("setColorTemperature", 4000)),
                     ("/v1.1/devices/K1/commands", command("unlock")), ("/v1.1/scenes/T02-1/execute", None)]
    with pytest.raises(ConnectorError, match="离线"):
        await bot.control("switch/OFF1", "turn_on")
    with pytest.raises(ConnectorError, match="不支持"):
        await bot.control("lock/K1", "turn_on")
    denied = SwitchBot("sb", {"token": "tok", "secret": "sec"}, httpx.MockTransport(lambda request: httpx.Response(401, json={"message": "Unauthorized"})))
    with pytest.raises(ConnectorError, match="Token"):
        await denied.devices()


@pytest.mark.asyncio
async def test_sensibo_reads_the_ac_and_patches_one_property():
    patches = []
    pod = {"id": "AbCd1234", "room": {"name": "卧室"}, "connectionStatus": {"isAlive": True},
           "acState": {"on": False, "mode": "cool", "fanLevel": "auto", "targetTemperature": 77, "temperatureUnit": "F"},
           "measurements": {"temperature": 27.4, "humidity": 61.2}, "remoteCapabilities": {"modes": {"cool": {}, "heat": {}, "dry": {}}}}

    def handler(request):
        assert request.url.host == "home.sensibo.com" and request.url.params["apiKey"] == "k"
        if request.method == "PATCH":
            patches.append((request.url.path, json.loads(request.content)))
            return httpx.Response(200, json={"status": "success", "result": {}})
        if request.url.path == "/api/v2/users/me/pods":
            assert request.url.params["fields"] == "*"
            return httpx.Response(200, json={"status": "success", "result": [pod]})
        assert request.url.path == "/api/v2/pods/AbCd1234"
        return httpx.Response(200, json={"status": "success", "result": pod})
    ac = Sensibo("ac", {"key": "k"}, httpx.MockTransport(handler))
    [device] = await ac.devices()
    well_formed([device])
    assert device["type"] == "climate" and device["room"] == "卧室" and device["online"] is True
    assert device["state"] == {"on": False, "mode": "cool", "target_temperature": 25.0, "temperature": 27.4, "humidity": 61.2, "fan": "auto", "modes": ["cool", "heat", "dry"]}
    await ac.control("AbCd1234", "turn_on")
    await ac.control("AbCd1234", "toggle")
    await ac.control("AbCd1234", "set_temperature", 24)
    await ac.control("AbCd1234", "set_mode", "Heat")
    assert patches == [("/api/v2/pods/AbCd1234/acStates/on", {"newValue": True}), ("/api/v2/pods/AbCd1234/acStates/on", {"newValue": True}),
                       ("/api/v2/pods/AbCd1234/acStates/targetTemperature", {"newValue": 75}), ("/api/v2/pods/AbCd1234/acStates/mode", {"newValue": "heat"})]
    with pytest.raises(ConnectorError, match="cool、heat、dry"):
        await ac.control("AbCd1234", "set_mode", "turbo")
    limited = Sensibo("ac", {"key": "k"}, httpx.MockTransport(lambda request: httpx.Response(429)))
    with pytest.raises(ConnectorError, match="限流") as failure:
        await limited.devices()
    assert "apiKey" not in str(failure.value)


@pytest.mark.asyncio
async def test_lg_thinq_controls_the_air_conditioner_and_only_reads_the_washer():
    LGThinQ.profiles.clear()
    posts, message_ids = [], set()
    profile = {"property": {"operation": {"airConOperationMode": {"type": "enum", "mode": ["r", "w"], "value": {"r": ["POWER_ON", "POWER_OFF"], "w": ["POWER_ON", "POWER_OFF"]}}},
                            "airConJobMode": {"currentJobMode": {"type": "enum", "mode": ["r", "w"], "value": {"r": ["COOL", "AIR_DRY", "FAN"], "w": ["COOL", "AIR_DRY", "FAN"]}}},
                            "temperatureInUnits": [{"unit": "C", "targetTemperature": {"type": "range", "mode": ["r", "w"], "value": {"w": {"min": 18, "max": 30, "step": 1}}}},
                                                   {"unit": "F", "targetTemperature": {"type": "range", "mode": ["r", "w"], "value": {"w": {"min": 64, "max": 86, "step": 1}}}}]}}
    states = {"AC1": {"operation": {"airConOperationMode": "POWER_ON"}, "airConJobMode": {"currentJobMode": "COOL"},
                      "temperatureInUnits": [{"unit": "F", "currentTemperature": 79, "targetTemperature": 75}, {"unit": "C", "currentTemperature": 26, "targetTemperature": 24}]},
              "W1": [{"location": {"locationName": "MAIN"}, "runState": {"currentState": "RUNNING"}, "timer": {"remainHour": 1, "remainMinute": 12},
                      "remoteControlEnable": {"remoteControlEnabled": False}}]}

    def handler(request):
        assert request.url.host == "api-kic.lgthinq.com"
        headers = request.headers
        assert headers["Authorization"] == "Bearer pat" and headers["x-country"] == "CN" and headers["x-api-key"] == API_KEY and headers["x-service-phase"] == "OP"
        assert len(headers["x-client-id"]) == 36 and headers["x-message-id"] not in message_ids
        message_ids.add(headers["x-message-id"])
        path = request.url.path
        if request.method == "POST":
            posts.append((path, headers.get("x-conditional-control"), json.loads(request.content)))
            return httpx.Response(200, json={"messageId": "m", "response": {}})
        if path == "/devices":
            response = [{"deviceId": "AC1", "deviceInfo": {"deviceType": "DEVICE_AIR_CONDITIONER", "modelName": "RAC", "alias": "客厅空调", "reportable": True}},
                        {"deviceId": "W1", "deviceInfo": {"deviceType": "DEVICE_WASHER", "modelName": "WM", "alias": "洗衣机", "reportable": True}},
                        {"deviceId": "X1", "deviceInfo": {"deviceType": "DEVICE_AIR_PURIFIER", "modelName": "AP", "alias": "净化器", "reportable": True}}]
        elif path == "/devices/X1/state":
            return httpx.Response(416, json={"error": {"code": "1222", "message": "Not connected device"}})
        elif path.endswith("/profile"):
            assert path == "/devices/AC1/profile"
            response = profile
        else:
            response = states[path.split("/")[2]]
        return httpx.Response(200, json={"messageId": "m", "response": response})
    lg = LGThinQ("lg", {"token": "pat", "country": "cn"}, httpx.MockTransport(handler))
    devices = well_formed(await lg.devices())
    ac, washer, purifier = devices["AC1"], devices["W1"], devices["X1"]
    assert ac["type"] == "climate" and ac["actions"] == ["turn_on", "turn_off", "toggle", "set_temperature", "set_mode"]
    assert ac["state"] == {"on": True, "mode": "cool", "temperature": 26, "target_temperature": 24, "modes": ["cool", "air_dry", "fan"]}
    assert washer["type"] == "other" and washer["actions"] == [] and washer["state"] == {"value": "running", "remaining_minutes": 72, "remote_control": False}
    assert purifier["type"] == "fan" and purifier["online"] is False and purifier["actions"] == []
    await lg.control("AC1", "turn_off")
    await lg.control("AC1", "toggle")
    await lg.control("AC1", "set_temperature", 26)
    await lg.control("AC1", "set_mode", "air_dry")
    assert posts == [("/devices/AC1/control", "true", {"operation": {"airConOperationMode": "POWER_OFF"}}),
                     ("/devices/AC1/control", "true", {"operation": {"airConOperationMode": "POWER_OFF"}}),
                     ("/devices/AC1/control", "true", {"temperatureInUnits": {"targetTemperature": 26, "unit": "C"}}),
                     ("/devices/AC1/control", "true", {"airConJobMode": {"currentJobMode": "AIR_DRY"}})]
    with pytest.raises(ConnectorError, match="只能查看"):
        await lg.control("W1", "turn_on")
    with pytest.raises(ConnectorError, match="18 到 30"):
        await lg.control("AC1", "set_temperature", 35)
    with pytest.raises(ConnectorError, match="cool、air_dry、fan"):
        await lg.control("AC1", "set_mode", "heat")
    assert len(posts) == 4
    with pytest.raises(ConnectorError, match="国家代码"):
        await LGThinQ("lg", {"token": "pat", "country": "ZZ"}, httpx.MockTransport(handler)).devices()
    expired = LGThinQ("lg", {"token": "pat", "country": "US"}, httpx.MockTransport(lambda request: httpx.Response(401, json={"error": {"code": "1103", "message": "Invalid token"}})))
    with pytest.raises(ConnectorError, match="PAT"):
        await expired.devices()


@pytest.mark.asyncio
async def test_hubitat_maps_capabilities_and_sends_maker_api_commands():
    calls = []

    def entry(ident, label, capabilities, attributes, commands):
        return {"id": ident, "name": "generic", "label": label, "type": "Virtual", "capabilities": capabilities, "attributes": attributes, "commands": [{"command": c} for c in commands]}
    found = [entry("1", "落地灯", ["Switch", "SwitchLevel", "ColorControl", "ColorTemperature", "Light"], {"switch": "on", "level": 40, "colorTemperature": 2700},
                   ["on", "off", "setLevel", "setColor", "setColorTemperature"]),
             entry("2", "前门", ["Lock", "Battery"], {"lock": "locked", "battery": 80}, ["lock", "unlock"]),
             entry("3", "卷帘", ["WindowShade", "SwitchLevel"], {"windowShade": "open", "position": 100}, ["open", "close", "setPosition"]),
             entry("4", "温控器", ["Thermostat"], {"temperature": 68, "thermostatSetpoint": 70, "thermostatMode": "heat"},
                   ["setHeatingSetpoint", "setCoolingSetpoint", "setThermostatMode", "off"]),
             entry("5", "门磁", ["ContactSensor", "TemperatureMeasurement"], {"contact": "closed", "temperature": 50}, [])]

    def handler(request):
        assert request.url.params["access_token"] == "tok" and request.method == "GET"
        assert request.url.host == "192.168.1.50" and request.url.path.startswith("/apps/api/12/")
        path = request.url.raw_path.decode().split("?")[0].removeprefix("/apps/api/12")
        if path == "/devices/all":
            return httpx.Response(200, json=found)
        if path.count("/") == 2:
            return httpx.Response(200, json=next(d for d in found if d["id"] == path.rsplit("/", 1)[1]))
        calls.append(path)
        return httpx.Response(200, json=found[0])
    hub = Hubitat("hub", {"url": "http://192.168.1.50", "app_id": "12", "token": "tok", "unit": "F"}, httpx.MockTransport(handler))
    devices = well_formed(await hub.devices())
    light, lock, shade, thermostat, sensor = (devices[i] for i in "12345")
    assert light["type"] == "light" and light["actions"] == ["set_color", "set_color_temperature", "turn_on", "turn_off", "toggle", "set_brightness"]
    assert light["state"] == {"on": True, "brightness": 40.0, "color_temperature": 2700.0}
    assert lock["type"] == "lock" and lock["actions"] == ["lock", "unlock"] and lock["state"] == {"locked": True, "battery": 80.0}
    assert shade["type"] == "cover" and shade["actions"] == ["open", "close", "set_position"] and shade["state"]["position"] == 100.0
    assert thermostat["type"] == "climate" and thermostat["actions"] == ["set_temperature", "set_mode"]
    assert thermostat["state"]["temperature"] == 20.0 and thermostat["state"]["target_temperature"] == 21.1 and thermostat["state"]["mode"] == "heat"
    assert sensor["type"] == "sensor" and sensor["actions"] == [] and sensor["state"] == {"temperature": 10.0, "contact": "closed"}
    await hub.control("1", "toggle")
    await hub.control("1", "set_brightness", 55)
    await hub.control("1", "set_color", "#ff0400")
    await hub.control("2", "unlock")
    await hub.control("3", "set_position", 30)
    await hub.control("4", "set_temperature", 21)
    await hub.control("4", "set_mode", "Emergency Heat")
    assert calls == ["/devices/1/off", "/devices/1/setLevel/55", "/devices/1/setColor/%7B%22hex%22%3A%22FF0400%22%7D", "/devices/2/unlock",
                     "/devices/3/setPosition/30", "/devices/4/setHeatingSetpoint/70", "/devices/4/setThermostatMode/emergency%20heat"]
    cloud = Hubitat("hub", {"url": "https://cloud.hubitat.com/api/uid-1/apps/12/devices?access_token=old", "token": "tok"}, None)
    assert cloud.base() == "https://cloud.hubitat.com/api/uid-1/apps/12"
    with pytest.raises(ConnectorError, match="应用编号"):
        Hubitat("hub", {"url": "192.168.1.50", "token": "tok"}, None).base()
    denied = Hubitat("hub", {"url": "http://192.168.1.50", "app_id": "12", "token": "bad"}, httpx.MockTransport(lambda request: httpx.Response(401, text="unauthorized")))
    with pytest.raises(ConnectorError, match="访问令牌"):
        await denied.devices()


@pytest.mark.asyncio
async def test_dirigera_lights_blinds_purifier_sensors_and_scenes():
    writes = []

    def entry(ident, kind, name, attributes, can, room="客厅", reachable=True):
        return {"id": ident, "type": kind, "deviceType": kind, "isReachable": reachable, "attributes": {"customName": name, **attributes},
                "capabilities": {"canSend": [], "canReceive": can}, "room": {"id": "r", "name": room}}
    found = [entry("L1", "light", "吊灯", {"isOn": True, "lightLevel": 80, "colorTemperature": 2700, "colorTemperatureMin": 4000, "colorTemperatureMax": 2202, "colorMode": "temperature"},
                   ["customName", "isOn", "lightLevel", "colorTemperature"]),
             entry("O1", "outlet", "咖啡机", {"isOn": False, "lightLevel": 100}, ["customName", "isOn", "lightLevel"], reachable=False),
             entry("B1", "blinds", "卷帘", {"blindsCurrentLevel": 30, "blindsTargetLevel": 30, "blindsState": "stopped", "batteryPercentage": 77}, ["customName", "blindsTargetLevel", "blindsState"]),
             entry("P1", "airPurifier", "净化器", {"fanMode": "off", "motorState": 0, "currentPM25": 9}, ["customName", "fanMode", "motorState"]),
             entry("S1", "environmentSensor", "空气", {"currentTemperature": 22.5, "currentRH": 48, "currentPM25": 5}, ["customName"]),
             entry("G1", "gateway", "DIRIGERA", {}, ["customName"])]

    def handler(request):
        assert str(request.url).startswith("https://192.168.1.30:8443/v1/") and request.headers["Authorization"] == "Bearer tok"
        path = request.url.path.removeprefix("/v1")
        if request.method in ("PATCH", "POST"):
            writes.append((request.method, path, json.loads(request.content) if request.content else None))
            return httpx.Response(202 if request.method == "POST" else 204)
        if path == "/devices":
            return httpx.Response(200, json=found)
        if path == "/scenes":
            return httpx.Response(200, json=[{"id": "SC1", "type": "userScene", "info": {"name": "晚安", "icon": "scenes_moon"}}])
        return httpx.Response(200, json=next(d for d in found if d["id"] == path.rsplit("/", 1)[1]))
    hub = Dirigera("ikea", {"host": "192.168.1.30", "token": "tok"}, httpx.MockTransport(handler))
    devices = well_formed(await hub.devices())
    assert list(devices) == ["L1", "O1", "B1", "P1", "S1", "scene/SC1"]
    assert devices["L1"]["actions"] == ["turn_on", "turn_off", "toggle", "set_brightness", "set_color_temperature"] and devices["L1"]["room"] == "客厅"
    assert devices["L1"]["state"] == {"on": True, "brightness": 80, "color_temperature": 2700}
    assert devices["O1"]["type"] == "outlet" and devices["O1"]["actions"] == ["turn_on", "turn_off", "toggle"] and devices["O1"]["online"] is False
    assert devices["B1"]["type"] == "cover" and devices["B1"]["state"] == {"battery": 77, "position": 70, "value": "stopped"}
    assert devices["P1"]["type"] == "fan" and devices["P1"]["state"]["on"] is False and devices["P1"]["state"]["pm25"] == 9
    assert devices["S1"]["type"] == "sensor" and devices["S1"]["state"] == {"temperature": 22.5, "humidity": 48, "pm25": 5}
    await hub.control("L1", "toggle")
    await hub.control("L1", "set_brightness", 35)
    await hub.control("L1", "set_color_temperature", 6500)
    await hub.control("B1", "set_position", 25)
    await hub.control("B1", "open")
    await hub.control("P1", "turn_on")
    await hub.control("scene/SC1", "activate")
    assert writes == [("PATCH", "/devices/L1", [{"attributes": {"isOn": False}}]), ("PATCH", "/devices/L1", [{"attributes": {"lightLevel": 35}}]),
                      ("PATCH", "/devices/L1", [{"attributes": {"colorTemperature": 4000}}]), ("PATCH", "/devices/B1", [{"attributes": {"blindsTargetLevel": 75}}]),
                      ("PATCH", "/devices/B1", [{"attributes": {"blindsTargetLevel": 0}}]), ("PATCH", "/devices/P1", [{"attributes": {"fanMode": "auto"}}]),
                      ("POST", "/scenes/SC1/trigger", None)]
    denied = Dirigera("ikea", {"host": "192.168.1.30", "token": "bad"}, httpx.MockTransport(lambda request: httpx.Response(401)))
    with pytest.raises(ConnectorError, match="令牌"):
        await denied.devices()

    def unreachable(request):
        raise httpx.ConnectError("no route")
    with pytest.raises(ConnectorError, match="连不上"):
        await Dirigera("ikea", {"host": "192.168.1.30", "token": "tok"}, httpx.MockTransport(unreachable)).devices()


@pytest.mark.asyncio
async def test_openhab_items_become_devices_and_commands_are_plain_text():
    posts = []
    items = [{"name": "Kitchen_Light", "label": "厨房灯", "type": "Color", "state": "30,100,50", "tags": ["Lightbulb"]},
             {"name": "Hall_Dimmer", "label": "走廊灯", "type": "Dimmer", "state": "0", "tags": []},
             {"name": "Bed_Shutter", "label": "卧室卷帘", "type": "Rollershutter", "state": "100", "tags": []},
             {"name": "Front_Lock", "label": "前门锁", "type": "Switch", "state": "ON", "tags": ["Lock"]},
             {"name": "Garage", "label": "车库门", "type": "Switch", "state": "OFF", "tags": ["GarageDoor"]},
             {"name": "Plug", "label": "插座", "type": "Switch", "state": "NULL", "tags": ["PowerOutlet"]},
             {"name": "Living_Temp", "label": "客厅温度", "type": "Number:Temperature", "state": "70 °F", "tags": ["Measurement", "Temperature"]},
             {"name": "Living_Setpoint", "label": "客厅设定", "type": "Number:Temperature", "state": "21.5 °C", "tags": ["Setpoint", "Temperature"]},
             {"name": "Window", "label": "窗磁", "type": "Contact", "state": "OPEN", "tags": []},
             {"name": "gAll", "label": "全部", "type": "Group", "state": "NULL", "tags": []},
             {"name": "Note", "label": "备注", "type": "String", "state": "hi", "tags": []}]

    def handler(request):
        assert request.url.host == "192.168.1.10" and request.url.port == 8080
        assert request.headers["Authorization"] == "Basic " + base64.b64encode(b"oh.jurvis.token:").decode()
        path = request.url.path
        if request.method == "POST":
            assert request.headers["Content-Type"] == "text/plain"
            posts.append((path, request.content.decode()))
            return httpx.Response(200)
        if path == "/rest/items":
            assert request.url.params["recursive"] == "false"
            return httpx.Response(200, json=items)
        found = next((i for i in items if i["name"] == path.rsplit("/", 1)[1]), None)
        return httpx.Response(200, json=found) if found else httpx.Response(404, json={"error": {"message": "Item does not exist"}})
    oh = OpenHAB("oh", {"url": "http://192.168.1.10:8080/", "token": "oh.jurvis.token"}, httpx.MockTransport(handler))
    devices = well_formed(await oh.devices())
    assert list(devices) == ["Kitchen_Light", "Hall_Dimmer", "Bed_Shutter", "Front_Lock", "Garage", "Plug", "Living_Temp", "Living_Setpoint", "Window"]
    assert devices["Kitchen_Light"]["state"] == {"on": True, "brightness": 50, "color": "#804000"}
    assert devices["Kitchen_Light"]["actions"] == ["turn_on", "turn_off", "toggle", "set_brightness", "set_color"]
    assert devices["Hall_Dimmer"]["state"] == {"on": False, "brightness": 0} and devices["Bed_Shutter"]["type"] == "cover" and devices["Bed_Shutter"]["state"] == {"position": 0}
    assert devices["Front_Lock"]["type"] == "lock" and devices["Front_Lock"]["state"] == {"locked": True} and devices["Front_Lock"]["actions"] == ["lock", "unlock"]
    assert devices["Garage"]["type"] == "cover" and devices["Garage"]["actions"] == ["open", "close"] and devices["Plug"]["type"] == "outlet" and devices["Plug"]["state"] == {}
    assert devices["Living_Temp"]["type"] == "sensor" and devices["Living_Temp"]["state"] == {"temperature": 21.1}
    assert devices["Living_Setpoint"]["type"] == "climate" and devices["Living_Setpoint"]["state"] == {"target_temperature": 21.5}
    assert devices["Window"]["state"] == {"contact": "open"}
    await oh.control("Kitchen_Light", "toggle")
    await oh.control("Kitchen_Light", "set_color", "#ff8800")
    await oh.control("Hall_Dimmer", "set_brightness", 40)
    await oh.control("Bed_Shutter", "open")
    await oh.control("Bed_Shutter", "set_position", 25)
    await oh.control("Front_Lock", "unlock")
    await oh.control("Garage", "open")
    await oh.control("Living_Setpoint", "set_temperature", 22.5)
    assert posts == [("/rest/items/Kitchen_Light", "OFF"), ("/rest/items/Kitchen_Light", "32,100,100"), ("/rest/items/Hall_Dimmer", "40"), ("/rest/items/Bed_Shutter", "UP"),
                     ("/rest/items/Bed_Shutter", "75"), ("/rest/items/Front_Lock", "OFF"), ("/rest/items/Garage", "ON"), ("/rest/items/Living_Setpoint", "22.5 °C")]
    with pytest.raises(ConnectorError, match="不支持"):
        await oh.control("Window", "open")
    with pytest.raises(ConnectorError, match="找不到"):
        await oh.control("Missing", "turn_on")
    anonymous = OpenHAB("oh", {"url": "192.168.1.10:8080"}, httpx.MockTransport(lambda request: httpx.Response(401)))
    with pytest.raises(ConnectorError, match="API 令牌"):
        await anonymous.devices()
