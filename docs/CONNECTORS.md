# 智能家居连接器

Jurvis 把不同平台的设备变成同一种形状，助手和各个客户端只和这一种形状打交道。连接器负责翻译：把平台的设备读成统一的设备，把统一的操作翻成平台的指令。

## 已有的连接器

| 连接器 | 覆盖范围 | 连接方式 | 验证情况 |
|---|---|---|---|
| Home Assistant | Home Assistant 已集成的一切：Matter、HomeKit 设备、Zigbee、米家、涂鸦、Aqara、Ecobee、Sonos 等 | 局域网 REST API + 长期访问令牌 | 用模拟服务做了自动测试，未在真实实例上验证 |
| Philips Hue | Hue 灯和场景 | 局域网桥接器 CLIP v2，不经过云端 | 同上 |
| SmartThings | 三星 SmartThings 里的设备 | 云端 API + 个人访问令牌 | 同上 |
| 涂鸦 / Smart Life | 涂鸦智能、Smart Life 以及用涂鸦模组的品牌 | 涂鸦云 OpenAPI（Access ID / Secret） | 同上；签名算法按官方文档实现并有测试，未用真实账号验证 |
| Hubitat | Hubitat Elevation 中枢上的设备 | Maker API（局域网或云端地址 + 访问令牌） | 按官方文档实现，只用模拟服务测试 |
| openHAB | openHAB 里的 Items（按类型和语义标签归类） | REST API + API 令牌 | 同上；Item 的 JSON 字段和温度指令格式文档里没有示例，按约定实现 |
| IKEA DIRIGERA | DIRIGERA 中枢上的灯、插座、窗帘、空气净化器、传感器和场景 | 局域网 REST（8443 端口）+ 令牌 | IKEA 没有公开文档，按社区客户端实现；窗帘方向未能确认 |
| LG ThinQ | 空调可控制；净化器、风扇、除湿机只有开关；洗衣机、冰箱等只读状态 | ThinQ Connect 云端 API + 个人访问令牌（PAT） | 官方文档站无法访问，按 LG 官方 SDK 源码实现 |
| SwitchBot | 开关、插座、灯、窗帘、门锁、温湿度等传感器和场景 | Open API v1.1（令牌 + 密钥签名） | 按官方文档实现；部分型号按同类设备推断 |
| Sensibo | Sensibo 空调控制器：开关、温度、模式 | 云端 API v2 + API Key | 按官方文档实现 |
| LIFX | LIFX 灯和场景 | 云端 HTTP API + 访问令牌 | 按官方文档实现 |
| Govee | Govee 灯和插座 | 官方 Open API + API Key | 按官方文档实现；颜色数值的打包方式文档未写明 |
| Nanoleaf | Nanoleaf 灯板：开关、亮度、颜色、色温、灯效 | 局域网 REST（16021 端口）+ 令牌 | 按官方文档实现 |
| Yeelight | Yeelight 灯（需在 App 里打开“局域网控制”） | 局域网 TCP 55443 | 按官方互操作规范实现 |
| Shelly | Shelly 第二代及以后的开关、灯和窗帘电机（不支持设了密码的设备） | 局域网 RPC | 按官方文档实现 |
| WLED | WLED 灯带：开关、亮度、颜色、灯效 | 局域网 JSON API，无需令牌 | 按官方文档实现 |

想接入表里没有的生态，最快的办法是先接进 Home Assistant，再用 Home Assistant 连接器。Matter、HomeKit、米家都属于这种情况。另一条路是 MCP：任何提供 MCP 服务的平台都可以在 设置 → MCP 服务 里添加。

## 设置

在网页版的 设置 → 智能家居 里添加，或直接写 `data/security/home.json`：

```json
{"connectors": {
  "home":   {"kind": "homeassistant", "url": "http://homeassistant.local:8123", "token": "…"},
  "hue":    {"kind": "hue", "host": "192.168.1.20", "key": "…"},
  "st":     {"kind": "smartthings", "token": "…"},
  "tuya":   {"kind": "tuya", "region": "cn", "client_id": "…", "secret": "…"}
}}
```

令牌只保存在这台 Mac 上，接口不会把它们返回给任何客户端。

## 安全

- 门锁、门（含车库门、窗帘电机这类 `cover`）和摄像头，以及任何 `lock` / `unlock` / `open` 操作，助手每次都先在对话里问你。
- 其他设备默认直接执行。在设置里把确认方式改成「全部确认」后，助手的每个家居操作都先问。
- 你自己在「家居」页按下的操作不再二次询问（锁和门在客户端有一次确认）。
- 助手只能通过 `home_devices` 和 `home_control` 两个工具接触设备，看不到令牌。

## 统一的设备形状

```json
{"id": "home:light.living_room", "connector": "home", "name": "客厅灯", "room": "客厅", "type": "light",
 "online": true, "state": {"on": true, "brightness": 50, "color": "#ff8800"},
 "actions": ["turn_on", "turn_off", "toggle", "set_brightness", "set_color", "set_color_temperature"]}
```

- `type`：`light` `switch` `outlet` `climate` `cover` `lock` `fan` `media` `vacuum` `sensor` `camera` `scene` `other`
- `actions`（设备只列出自己支持的）：

| 操作 | 值 |
|---|---|
| `turn_on` `turn_off` `toggle` | 无 |
| `set_brightness` `set_position` | 0–100 |
| `set_color` | `#rrggbb` |
| `set_color_temperature` | 开尔文 |
| `set_temperature` | 摄氏度 |
| `set_mode` | 设备 `state.modes` 里的一个 |
| `open` `close` `lock` `unlock` `activate` | 无 |

- `state` 里常见的键：`on` `brightness` `color` `color_temperature` `temperature` `target_temperature` `mode` `modes` `position` `open` `locked` `value` `unit`。没有的键不出现。

## 写一个新的连接器

在 `engine/home/` 里加一个模块，继承 `Connector`，实现两个方法，然后把类加进 `engine/home/__init__.py` 的 `kinds()`：

```python
from . import Connector, ConnectorError, number

class Example(Connector):
    kind = "example"
    title = "Example"
    description = "一句话说明覆盖什么。"
    fields = [{"name": "host", "label": "地址"}, {"name": "token", "label": "令牌", "secret": True}]

    async def devices(self):
        # 读平台的设备列表，逐个用 self.device(...) 变成统一形状。id 用平台自己的标识。
        return [self.device("lamp-1", "台灯", "light", room="书房", state={"on": True}, actions=["turn_on", "turn_off"])]

    async def control(self, native_id, action, value=None):
        # 把统一操作翻成平台指令。做不到就 raise ConnectorError("给用户看的原因")。
        ...
```

约定：

- `fields` 决定设置页的表单；标了 `secret` 的字段不会被接口返回，留空表示保留原值。
- 网络请求用 `httpx.AsyncClient(..., transport=self.transport)`，这样测试可以用 `httpx.MockTransport` 代替真实平台，参考 `tests/test_platform.py` 里的 Home Assistant、Hue、SmartThings 和涂鸦测试。
- 数值先用 `number(value, low, high, "亮度")` 检查范围，颜色用 `parse_color`。
- 错误信息写给用户看：说清哪里不对、怎么改，不要抛平台的原始错误码。
- 会把人挡在门外或放进门里的设备，`type` 要用 `lock`、`cover` 或 `camera`，这样确认规则才会生效。

欢迎为更多生态提交连接器。请在这份文档的表格里写明覆盖范围和你验证过的设备。
