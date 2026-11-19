"""LIFX, Govee, Nanoleaf, WLED, Yeelight and Shelly connectors against mocked transports (no real hardware or cloud)."""
import json

import httpx
import pytest

from engine.home import ConnectorError
from engine.home import yeelight as yeelight_module
from engine.home.govee import Govee
from engine.home.lifx import Lifx
from engine.home.nanoleaf import Nanoleaf
from engine.home.shelly import Shelly
from engine.home.wled import Wled
from engine.home.yeelight import Yeelight


def body(request):
    return json.loads(request.content) if request.content else None


@pytest.mark.asyncio
async def test_lifx_lights_and_scenes():
    sent = []

    def handler(request):
        assert request.url.host == "api.lifx.com" and request.headers["authorization"] == "Bearer t"
        if request.method == "GET" and request.url.path == "/v1/lights/all":
            return httpx.Response(200, json=[
                {"id": "d073d5000001", "label": "台灯", "connected": True, "power": "on", "brightness": 0.5, "color": {"hue": 0, "saturation": 0, "kelvin": 3500},
                 "group": {"id": "g", "name": "书房"}, "product": {"capabilities": {"has_color": True, "has_variable_color_temp": True}}},
                {"id": "d073d5000002", "label": "白光灯", "connected": False, "power": "off", "brightness": 1.0, "color": {"hue": 0, "saturation": 0, "kelvin": 2700},
                 "group": {"id": "g", "name": "书房"}, "product": {"capabilities": {"has_color": False, "has_variable_color_temp": False}}}])
        if request.method == "GET":
            return httpx.Response(200, json=[{"uuid": "1d52671c", "name": "夜间", "states": []}])
        sent.append((request.method, request.url.path, body(request)))
        return httpx.Response(207, json={"results": [{"id": "d073d5000001", "label": "台灯", "status": "ok"}]})
    lifx = Lifx("lifx", {"token": "t"}, httpx.MockTransport(handler))
    lamp, white, scene = await lifx.devices()
    assert lamp == {"id": "light/d073d5000001", "name": "台灯", "room": "书房", "type": "light", "online": True,
                    "state": {"on": True, "brightness": 50, "color_temperature": 3500},
                    "actions": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_color_temperature"]}
    assert white["online"] is False and white["actions"] == ["turn_on", "turn_off", "toggle", "set_brightness"]
    assert scene["type"] == "scene" and scene["id"] == "scene/1d52671c" and scene["actions"] == ["activate"]
    await lifx.control(lamp["id"], "set_brightness", 80)
    await lifx.control(lamp["id"], "set_color", "#FF8800")
    await lifx.control(lamp["id"], "set_color_temperature", 2700)
    await lifx.control(lamp["id"], "toggle")
    await lifx.control(scene["id"], "activate")
    assert sent == [("PUT", "/v1/lights/id:d073d5000001/state", {"power": "on", "brightness": 0.8}),
                    ("PUT", "/v1/lights/id:d073d5000001/state", {"power": "on", "color": "#ff8800"}),
                    ("PUT", "/v1/lights/id:d073d5000001/state", {"power": "on", "color": "kelvin:2700"}),
                    ("POST", "/v1/lights/id:d073d5000001/toggle", None),
                    ("PUT", "/v1/scenes/scene_id:1d52671c/activate", None)]


@pytest.mark.asyncio
async def test_lifx_reports_a_bad_token_and_an_offline_light():
    lifx = Lifx("lifx", {"token": "bad"}, httpx.MockTransport(lambda request: httpx.Response(401, json={"error": "Invalid token"})))
    with pytest.raises(ConnectorError, match="访问令牌"):
        await lifx.devices()
    offline = httpx.MockTransport(lambda request: httpx.Response(207, json={"results": [{"id": "x", "label": "灯", "status": "offline"}]}))
    with pytest.raises(ConnectorError, match="离线"):
        await Lifx("lifx", {"token": "t"}, offline).control("light/x", "turn_on")


@pytest.mark.asyncio
async def test_govee_devices_and_capability_commands():
    sent = []

    def handler(request):
        assert request.url.host == "openapi.api.govee.com" and request.headers["govee-api-key"] == "k"
        path = request.url.path
        if path == "/router/api/v1/user/devices":
            assert request.method == "GET"
            return httpx.Response(200, json={"code": 200, "message": "success", "data": [
                {"sku": "H6601", "device": "9D:FA:85:EB:D3:00:8B:FF", "deviceName": "灯带", "type": "devices.types.light", "capabilities": [
                    {"type": "devices.capabilities.on_off", "instance": "powerSwitch"}, {"type": "devices.capabilities.toggle", "instance": "gradientToggle"},
                    {"type": "devices.capabilities.range", "instance": "brightness"}, {"type": "devices.capabilities.color_setting", "instance": "colorRgb"},
                    {"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK"}]},
                {"sku": "H5080", "device": "AA:BB", "deviceName": "插座", "type": "devices.types.socket", "capabilities": [
                    {"type": "devices.capabilities.on_off", "instance": "powerSwitch"}]}]})
        payload = body(request)
        assert request.method == "POST" and payload["requestId"]
        if path == "/router/api/v1/device/state":
            light = payload["payload"]["sku"] == "H6601"
            capabilities = [{"type": "devices.capabilities.online", "instance": "online", "state": {"value": light}},
                            {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "state": {"value": 1}}]
            if light:
                capabilities += [{"type": "devices.capabilities.range", "instance": "brightness", "state": {"value": 60}},
                                 {"type": "devices.capabilities.color_setting", "instance": "colorRgb", "state": {"value": 16711680}},
                                 {"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK", "state": {"value": 0}}]
            return httpx.Response(200, json={"requestId": payload["requestId"], "msg": "success", "code": 200, "payload": {**payload["payload"], "capabilities": capabilities}})
        assert path == "/router/api/v1/device/control"
        sent.append(payload["payload"])
        return httpx.Response(200, json={"requestId": payload["requestId"], "msg": "success", "code": 200})
    govee = Govee("govee", {"key": "k"}, httpx.MockTransport(handler))
    light, plug = await govee.devices()
    assert light == {"id": "H6601/9D:FA:85:EB:D3:00:8B:FF", "name": "灯带", "room": "", "type": "light", "online": True,
                     "state": {"on": True, "brightness": 60, "color": "#ff0000"},
                     "actions": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_color_temperature"]}
    assert plug["type"] == "outlet" and plug["online"] is False and plug["actions"] == ["turn_on", "turn_off", "toggle"]
    await govee.control(light["id"], "turn_off")
    await govee.control(light["id"], "set_brightness", 50)
    await govee.control(light["id"], "set_color", "#ff8800")
    await govee.control(light["id"], "set_color_temperature", 4000)
    await govee.control(light["id"], "toggle")
    target = {"sku": "H6601", "device": "9D:FA:85:EB:D3:00:8B:FF"}
    assert sent == [{**target, "capability": {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": 0}},
                    {**target, "capability": {"type": "devices.capabilities.range", "instance": "brightness", "value": 50}},
                    {**target, "capability": {"type": "devices.capabilities.color_setting", "instance": "colorRgb", "value": 0xFF8800}},
                    {**target, "capability": {"type": "devices.capabilities.color_setting", "instance": "colorTemperatureK", "value": 4000}},
                    {**target, "capability": {"type": "devices.capabilities.on_off", "instance": "powerSwitch", "value": 0}}]
    with pytest.raises(ConnectorError, match="API Key"):
        await Govee("govee", {"key": "k"}, httpx.MockTransport(lambda request: httpx.Response(401, json={"message": "Unauthorized"}))).devices()


@pytest.mark.asyncio
async def test_nanoleaf_state_and_effects():
    sent = []

    def handler(request):
        assert request.url.host == "192.168.1.30" and request.url.port == 16021
        path = request.url.path
        if request.method == "PUT":
            sent.append((path, body(request)))
            return httpx.Response(204)
        if path == "/api/v1/tok/state/on":
            return httpx.Response(200, json={"value": True})
        assert path == "/api/v1/tok/"
        return httpx.Response(200, json={"name": "客厅灯板", "serialNo": "S16331A0217", "model": "NL22", "state": {
            "on": {"value": True}, "brightness": {"value": 70, "max": 100, "min": 0}, "hue": {"value": 0, "max": 360, "min": 0},
            "sat": {"value": 0, "max": 100, "min": 0}, "ct": {"value": 4000, "max": 6500, "min": 1200}, "colorMode": "effect"},
            "effects": {"select": "Flames", "effectsList": ["Flames", "Forest", "Snowfall"]}})
    nanoleaf = Nanoleaf("nano", {"host": "192.168.1.30", "token": "tok"}, httpx.MockTransport(handler))
    panels, = await nanoleaf.devices()
    assert panels == {"id": "S16331A0217", "name": "客厅灯板", "room": "", "type": "light", "online": True,
                      "state": {"on": True, "brightness": 70, "modes": ["Flames", "Forest", "Snowfall"], "mode": "Flames"},
                      "actions": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_color_temperature", "set_mode"]}
    await nanoleaf.control(panels["id"], "set_color", "#00ff00")
    await nanoleaf.control(panels["id"], "set_color_temperature", 3000)
    await nanoleaf.control(panels["id"], "set_mode", "Snowfall")
    await nanoleaf.control(panels["id"], "toggle")
    assert sent == [("/api/v1/tok/state", {"on": {"value": True}}), ("/api/v1/tok/state", {"hue": {"value": 120}}),
                    ("/api/v1/tok/state", {"sat": {"value": 100}}), ("/api/v1/tok/state", {"brightness": {"value": 100}}),
                    ("/api/v1/tok/state", {"on": {"value": True}}), ("/api/v1/tok/state", {"ct": {"value": 3000}}),
                    ("/api/v1/tok/effects", {"select": "Snowfall"}),
                    ("/api/v1/tok/state", {"on": {"value": False}})]
    with pytest.raises(ConnectorError, match="授权令牌"):
        await Nanoleaf("nano", {"host": "192.168.1.30", "token": "bad"}, httpx.MockTransport(lambda request: httpx.Response(401))).devices()


@pytest.mark.asyncio
async def test_wled_state_colour_and_effects():
    sent = []
    data = {"state": {"on": True, "bri": 128, "ps": -1, "seg": [{"id": 0, "col": [[255, 160, 0], [0, 0, 0], [0, 0, 0]], "fx": 2}, {"id": 1, "col": [[0, 0, 255]], "fx": 0}]},
            "info": {"name": "电视背光", "mac": "aabbccddeeff", "leds": {"count": 60}}, "effects": ["Solid", "Blink", "Breathe", "RSVD"], "palettes": ["Default"]}

    def handler(request):
        if request.url.host == "192.168.1.41":
            raise httpx.ConnectError("refused")
        assert request.url.host == "192.168.1.40"
        if request.method == "GET":
            assert request.url.path == "/json"
            return httpx.Response(200, json=data)
        sent.append((request.url.path, body(request)))
        return httpx.Response(200, json={"success": True})
    wled = Wled("wled", {"hosts": "192.168.1.40, http://192.168.1.41/"}, httpx.MockTransport(handler))
    strip, missing = await wled.devices()
    assert strip == {"id": "192.168.1.40", "name": "电视背光", "room": "", "type": "light", "online": True,
                     "state": {"on": True, "brightness": 50, "modes": ["Solid", "Blink", "Breathe"], "color": "#ffa000", "mode": "Breathe"},
                     "actions": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_mode"]}
    assert missing["id"] == "192.168.1.41" and missing["online"] is False
    await wled.control(strip["id"], "toggle")
    await wled.control(strip["id"], "turn_off")
    await wled.control(strip["id"], "set_brightness", 100)
    await wled.control(strip["id"], "set_color", "#0000ff")
    await wled.control(strip["id"], "set_mode", "blink")
    assert sent == [("/json/state", {"on": "t"}), ("/json/state", {"on": False}), ("/json/state", {"on": True, "bri": 255}),
                    ("/json/state", {"on": True, "seg": [{"id": 0, "col": [[0, 0, 255]]}, {"id": 1, "col": [[0, 0, 255]]}]}),
                    ("/json/state", {"on": True, "seg": [{"id": 0, "fx": 1}, {"id": 1, "fx": 1}]})]
    with pytest.raises(ConnectorError, match="没有名为"):
        await wled.control(strip["id"], "set_mode", "Nope")
    with pytest.raises(ConnectorError, match="连不上 WLED 设备 192.168.1.41"):
        await wled.control("192.168.1.41", "turn_on")


@pytest.mark.asyncio
async def test_yeelight_lan_commands(monkeypatch):
    sent = []

    async def send(host, port, payload):
        if host == "192.168.1.52":
            raise ConnectionRefusedError()
        sent.append((host, port, payload))
        if payload["method"] == "get_prop":
            assert payload["params"] == ["power", "bright", "ct", "rgb", "color_mode", "name"]
            return {"id": payload["id"], "result": ["on", "40", "4000", "16711680", "2", "床头灯"] if host == "192.168.1.50" else ["off", "100", "", "", "", ""]}
        if payload["method"] == "set_rgb" and host == "192.168.1.51":
            return {"id": payload["id"], "error": {"code": -1, "message": "method not supported"}}
        return {"id": payload["id"], "result": ["ok"]}
    monkeypatch.setattr(yeelight_module, "send", send)
    yeelight = Yeelight("yee", {"hosts": "192.168.1.50,192.168.1.51:55444 192.168.1.52"})
    colour, white, missing = await yeelight.devices()
    assert colour == {"id": "192.168.1.50", "name": "床头灯", "room": "", "type": "light", "online": True, "state": {"on": True, "brightness": 40, "color_temperature": 4000},
                      "actions": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_color_temperature"]}
    assert white["id"] == white["name"] == "192.168.1.51:55444" and white["state"] == {"on": False, "brightness": 100}
    assert white["actions"] == ["turn_on", "turn_off", "toggle", "set_brightness"]
    assert missing["online"] is False and missing["id"] == "192.168.1.52"
    sent.clear()
    await yeelight.control(colour["id"], "turn_off")
    await yeelight.control(colour["id"], "toggle")
    await yeelight.control(colour["id"], "set_brightness", 80)
    await yeelight.control(colour["id"], "set_color", "#ff8800")
    await yeelight.control(white["id"], "set_color_temperature", 2700)
    on = {"id": 1, "method": "set_power", "params": ["on", "smooth", 500]}
    assert sent == [("192.168.1.50", 55443, {"id": 1, "method": "set_power", "params": ["off", "smooth", 500]}),
                    ("192.168.1.50", 55443, {"id": 1, "method": "toggle", "params": []}),
                    ("192.168.1.50", 55443, on), ("192.168.1.50", 55443, {"id": 1, "method": "set_bright", "params": [80, "smooth", 500]}),
                    ("192.168.1.50", 55443, on), ("192.168.1.50", 55443, {"id": 1, "method": "set_rgb", "params": [0xFF8800, "smooth", 500]}),
                    ("192.168.1.51", 55444, on), ("192.168.1.51", 55444, {"id": 1, "method": "set_ct_abx", "params": [2700, "smooth", 500]})]
    with pytest.raises(ConnectorError, match="不支持"):
        await yeelight.control(white["id"], "set_color", "#ff0000")
    with pytest.raises(ConnectorError, match="局域网控制"):
        await yeelight.control(missing["id"], "turn_on")
    # A callable transport replaces the socket function without monkeypatching.
    calls = []

    async def injected(host, port, payload):
        calls.append((host, port, payload["method"]))
        return {"id": payload["id"], "result": ["ok"]}
    await Yeelight("yee", {"hosts": "10.0.0.9"}, injected).control("10.0.0.9", "turn_on")
    assert calls == [("10.0.0.9", 55443, "set_power")]


@pytest.mark.asyncio
async def test_yeelight_send_speaks_json_lines_over_tcp():
    import asyncio
    received = []

    async def bulb(reader, writer):
        received.append(await reader.readline())
        writer.write(b'{"method":"props","params":{"power":"on"}}\r\n{"id":1,"result":["ok"]}\r\n')
        await writer.drain()
        writer.close()
    server = await asyncio.start_server(bulb, "127.0.0.1", 0)
    try:
        port = server.sockets[0].getsockname()[1]
        reply = await yeelight_module.send("127.0.0.1", port, {"id": 1, "method": "toggle", "params": []})
    finally:
        server.close()
        await server.wait_closed()
    assert reply == {"id": 1, "result": ["ok"]} and received == [b'{"id":1,"method":"toggle","params":[]}\r\n']


@pytest.mark.asyncio
async def test_shelly_switches_lights_and_covers():
    sent = []
    devices = {
        "192.168.1.60": ({"sys": {}, "switch:0": {"id": 0, "output": True, "apower": 12.5}, "switch:1": {"id": 1, "output": False}, "input:0": {"id": 0, "state": False}},
                         {"sys": {"device": {"name": "厨房", "mac": "A8032ABE54DC"}}, "switch:0": {"id": 0, "name": "水壶"}, "switch:1": {"id": 1, "name": None}}),
        "192.168.1.61": ({"light:0": {"id": 0, "output": True, "brightness": 30}}, {"sys": {"device": {"name": None}}, "light:0": {"id": 0, "name": "调光灯"}}),
        "192.168.1.62": ({"cover:0": {"id": 0, "state": "open", "current_pos": 100, "pos_control": True}}, {"sys": {"device": {"name": "卧室窗帘"}}, "cover:0": {"id": 0, "name": None}})}

    def handler(request):
        host, method = request.url.host, request.url.path.removeprefix("/rpc/")
        if host == "192.168.1.63":
            return httpx.Response(401)
        assert request.method == "GET" and request.url.path.startswith("/rpc/")
        if method == "Shelly.GetStatus":
            return httpx.Response(200, json=devices[host][0])
        if method == "Shelly.GetConfig":
            return httpx.Response(200, json=devices[host][1])
        sent.append((host, method, request.url.query.decode()))
        return httpx.Response(200, content=b"null") if method.startswith("Cover.") else httpx.Response(200, json={"was_on": False})
    shelly = Shelly("shelly", {"hosts": "192.168.1.60, 192.168.1.61, 192.168.1.62, 192.168.1.63"}, httpx.MockTransport(handler))
    kettle, relay, dimmer, cover = await shelly.devices()
    assert kettle == {"id": "192.168.1.60/switch:0", "name": "水壶", "room": "", "type": "switch", "online": True, "state": {"on": True, "power": 12.5},
                      "actions": ["turn_on", "turn_off", "toggle"]}
    assert relay["name"] == "厨房 switch:1" and relay["state"] == {"on": False}
    assert dimmer["type"] == "light" and dimmer["name"] == "调光灯" and dimmer["state"] == {"on": True, "brightness": 30} and "set_brightness" in dimmer["actions"]
    assert cover == {"id": "192.168.1.62/cover:0", "name": "卧室窗帘", "room": "", "type": "cover", "online": True, "state": {"state": "open", "position": 100},
                     "actions": ["open", "close", "set_position"]}
    await shelly.control(kettle["id"], "turn_off")
    await shelly.control(relay["id"], "toggle")
    await shelly.control(dimmer["id"], "set_brightness", 50)
    await shelly.control(cover["id"], "close")
    await shelly.control(cover["id"], "set_position", 20)
    assert sent == [("192.168.1.60", "Switch.Set", "id=0&on=false"), ("192.168.1.60", "Switch.Toggle", "id=1"),
                    ("192.168.1.61", "Light.Set", "id=0&on=true&brightness=50"),
                    ("192.168.1.62", "Cover.Close", "id=0"), ("192.168.1.62", "Cover.GoToPosition", "id=0&pos=20")]
    with pytest.raises(ConnectorError, match="密码"):
        await shelly.control("192.168.1.63/switch:0", "turn_on")
    with pytest.raises(ConnectorError, match="窗帘只支持"):
        await shelly.control(cover["id"], "turn_on")
