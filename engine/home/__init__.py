"""Smart-home connectors: one device model over many ecosystems.

Every connector turns its platform's devices into the same shape and accepts the
same small set of actions, so the assistant (and the apps) never deal with a
vendor's API directly:

    device  {"id": "<connector>:<native id>", "name", "room", "type", "online", "state": {...}, "actions": [...]}
    types   light switch outlet climate cover lock fan media vacuum sensor camera scene other
    actions turn_on turn_off toggle set_brightness(0-100) set_color("#rrggbb") set_color_temperature(kelvin)
            set_temperature(°C) set_mode(text) open close set_position(0-100) lock unlock activate

To add an ecosystem, subclass Connector in a new module, implement devices() and
control(), and add the class to KINDS. See docs/CONNECTORS.md.
"""
import asyncio
import time

from ..llm import Prefs

TYPES = ("light", "switch", "outlet", "climate", "cover", "lock", "fan", "media", "vacuum", "sensor", "camera", "scene", "other")
ACTIONS = ("turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_color_temperature", "set_temperature", "set_mode",
           "open", "close", "set_position", "lock", "unlock", "activate")
# Things that guard the home: acting on them always asks the user first.
SENSITIVE_TYPES = {"lock", "cover", "camera"}
SENSITIVE_ACTIONS = {"unlock", "open", "lock"}
CACHE_SECONDS = 20


class ConnectorError(Exception):
    """A failure with a message the user can act on."""


class Connector:
    kind = ""
    title = ""
    description = ""
    fields = []  # [{"name", "label", "secret": bool, "placeholder", "optional": bool}] for the settings form

    def __init__(self, ident, config, transport=None):
        self.id, self.config, self.transport = ident, config, transport

    async def devices(self):
        """All devices as normalized dicts whose "id" is the platform's own identifier."""
        raise NotImplementedError

    async def control(self, native_id, action, value=None):
        """Performs one normalized action; raises ConnectorError when the device cannot do it."""
        raise NotImplementedError

    @staticmethod
    def device(native_id, name, kind, *, room="", online=True, state=None, actions=()):
        return {"id": str(native_id), "name": str(name or native_id), "room": str(room or ""), "type": kind if kind in TYPES else "other",
                "online": bool(online), "state": {k: v for k, v in (state or {}).items() if v is not None}, "actions": [a for a in actions if a in ACTIONS]}


def kinds():
    from .dirigera import Dirigera
    from .govee import Govee
    from .homeassistant import HomeAssistant
    from .hubitat import Hubitat
    from .hue import Hue
    from .lgthinq import LGThinQ
    from .lifx import Lifx
    from .nanoleaf import Nanoleaf
    from .openhab import OpenHAB
    from .sensibo import Sensibo
    from .shelly import Shelly
    from .smartthings import SmartThings
    from .switchbot import SwitchBot
    from .tuya import Tuya
    from .wled import Wled
    from .yeelight import Yeelight
    # hubs that cover many brands first, then single brands
    order = (HomeAssistant, SmartThings, Tuya, Hubitat, OpenHAB, Hue, Dirigera, LGThinQ, SwitchBot, Sensibo, Lifx, Govee, Nanoleaf, Yeelight, Shelly, Wled)
    return {c.kind: c for c in order}


# For the "connect a brand" gallery: hubs cover many brands, and LOCAL ones never leave the home network.
CATEGORY = {"homeassistant": "hub", "smartthings": "hub", "tuya": "hub", "hubitat": "hub", "openhab": "hub", "dirigera": "hub",
            "hue": "light", "lifx": "light", "govee": "light", "nanoleaf": "light", "yeelight": "light", "wled": "light"}
LOCAL = {"homeassistant", "hubitat", "openhab", "hue", "dirigera", "nanoleaf", "yeelight", "shelly", "wled"}


def parse_color(value):
    """"#ff8800" -> (255, 136, 0)."""
    text = str(value or "").strip().lstrip("#")
    if len(text) != 6:
        raise ConnectorError("颜色请用 #rrggbb 格式")
    try:
        return tuple(int(text[i:i + 2], 16) for i in (0, 2, 4))
    except ValueError as exc:
        raise ConnectorError("颜色请用 #rrggbb 格式") from exc


def number(value, low, high, what):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise ConnectorError(f"{what}需要一个数字") from exc
    if not low <= result <= high:
        raise ConnectorError(f"{what}需在 {low} 到 {high} 之间")
    return result


class Home:
    """The user's configured connectors (data/security/home.json holds their tokens)."""

    def __init__(self, settings, prefs, transport=None):
        self.path = settings.data / "security" / "home.json"
        self.prefs, self.transport = prefs, transport
        self.cache, self.cached_at, self.errors = [], 0.0, {}

    def config(self):
        items = Prefs.load(self.path).get("connectors")
        return {str(k): v for k, v in items.items() if isinstance(v, dict) and v.get("kind") in kinds()} if isinstance(items, dict) else {}

    def connectors(self):
        available = kinds()
        return [available[c["kind"]](ident, c, self.transport) for ident, c in self.config().items() if c.get("enabled") is not False]

    def catalog(self):
        return [{"kind": c.kind, "title": c.title, "description": c.description, "fields": c.fields,
                 "category": CATEGORY.get(c.kind, "appliance"), "local": c.kind in LOCAL} for c in kinds().values()]

    def public(self):
        """Configured connectors without their secrets."""
        available = kinds()
        out = []
        for ident, c in self.config().items():
            secret = {f["name"] for f in available[c["kind"]].fields if f.get("secret")}
            out.append({"id": ident, "kind": c["kind"], "title": c.get("title") or available[c["kind"]].title, "enabled": c.get("enabled") is not False,
                        "settings": {k: v for k, v in c.items() if k not in secret and k not in ("kind", "title", "enabled")},
                        "error": self.errors.get(ident), "devices": sum(1 for d in self.cache if d["connector"] == ident)})
        return out

    def save(self, ident, kind, values, title=None, enabled=True):
        available = kinds()
        if kind not in available:
            raise ValueError("不支持的连接器类型")
        if not ident or not ident.replace("-", "").replace("_", "").isalnum() or len(ident) > 32 or ":" in ident:
            raise ValueError("连接器标识只能用字母、数字、- 和 _")
        current = self.config()
        entry = dict(current.get(ident) or {}) if (current.get(ident) or {}).get("kind") == kind else {}
        for field in available[kind].fields:
            value = values.get(field["name"])
            if value not in (None, ""):
                entry[field["name"]] = str(value).strip()
            elif field["name"] not in entry and not field.get("optional"):
                raise ValueError(f"请填写{field['label']}")
        entry.update(kind=kind, enabled=bool(enabled))
        if title:
            entry["title"] = title[:60]
        current[ident] = entry
        Prefs.save(self.path, {"connectors": current}, private=True)
        self.cached_at = 0.0

    def remove(self, ident):
        current = self.config()
        if current.pop(ident, None) is not None:
            Prefs.save(self.path, {"connectors": current}, private=True)
        self.cached_at = 0.0
        self.errors.pop(ident, None)

    async def devices(self, refresh=False):
        if not refresh and self.cache and time.time() - self.cached_at < CACHE_SECONDS:
            return self.cache
        connectors = self.connectors()
        results = await asyncio.gather(*(asyncio.wait_for(c.devices(), 30) for c in connectors), return_exceptions=True)
        devices, self.errors = [], {}
        for connector, result in zip(connectors, results):
            if isinstance(result, BaseException):
                self.errors[connector.id] = ("连接超时" if isinstance(result, asyncio.TimeoutError) else str(result) or type(result).__name__)[:300]
                continue
            for device in result:
                devices.append({**device, "id": f"{connector.id}:{device['id']}", "connector": connector.id})
        self.cache, self.cached_at = devices, time.time()
        return devices

    async def find(self, device_id):
        for device in await self.devices():
            if device["id"] == device_id:
                return device
        raise ValueError(f"没有 ID 为 {device_id} 的设备，请先用 home_devices 查看")

    def needs_confirmation(self, device, action):
        if self.prefs.get()["home"]["confirm"] == "always":
            return True
        return device["type"] in SENSITIVE_TYPES or action in SENSITIVE_ACTIONS

    async def control(self, device_id, action, value=None):
        if action not in ACTIONS:
            raise ValueError("不支持的操作：" + action)
        device = await self.find(device_id)
        connector = next((c for c in self.connectors() if c.id == device["connector"]), None)
        if not connector:
            raise ValueError("这个设备的连接器已停用")
        if device["actions"] and action not in device["actions"]:
            raise ValueError(f"{device['name']} 不支持 {action}，可用操作：{', '.join(device['actions'])}")
        try:
            await asyncio.wait_for(connector.control(device_id.split(":", 1)[1], action, value), 30)
        except ConnectorError as exc:
            raise ValueError(str(exc)) from exc
        except asyncio.TimeoutError as exc:
            raise ValueError("设备没有在 30 秒内响应") from exc
        self.cached_at = 0.0
        return {"device": device["name"], "action": action, **({"value": value} if value is not None else {}), "status": "sent"}

    @staticmethod
    def brief(device):
        return {k: device[k] for k in ("id", "name", "room", "type", "online", "state", "actions") if device.get(k) not in ("", [], {})}

    async def search(self, query="", room="", kind=""):
        devices = await self.devices()
        terms = [t for t in query.lower().split() if t]

        def match(d):
            text = f"{d['name']} {d['room']} {d['type']}".lower()
            return (all(t in text for t in terms) and (not room or room.lower() in d["room"].lower()) and (not kind or d["type"] == kind))
        found = [d for d in devices if match(d)]
        result = {"devices": [self.brief(d) for d in found[:60]], "total": len(found)}
        if len(found) > 60:
            result["note"] = "Showing 60. Narrow with query, room or type."
        if self.errors:
            result["unavailable"] = self.errors
        if not self.config():
            result["note"] = "No smart-home connector is set up yet. The user can add one in Settings → 智能家居."
        return result
