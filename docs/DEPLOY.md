# 部署引擎

Jurvis 的所有功能都由一台 Mac 上的引擎提供。Mac App、iPhone / iPad App 和网页版只是它的界面。先把引擎跑起来，再看[本地模型](MODELS.md)、[网页版](PWA.md)和[原生 App](APPS.md)。

## 要求

- Apple Silicon 的 Mac。本机模型和生图、视频依赖 MLX 与 Metal，Intel Mac 和其他系统不支持。
- 只用云端模型时内存要求很低。默认的本机对话模型（27B，4 bit）需要 32GB 以上内存；目前只在 M5 Pro / 64GB 上验证过。
- Python 3.12（由 uv 管理）、[uv](https://docs.astral.sh/uv/getting-started/installation/)、ffmpeg、完整的 Xcode（只跑引擎和网页版也需要，因为安装脚本会生成工程文件）。
- 想让助手操作浏览器，还需要装有 Chrome、Edge、Brave 或 Chromium 之一。

## 安装

```bash
git clone https://github.com/SpectreMercury/Jurvis.git
```

```bash
cd Jurvis
```

```bash
./scripts/bootstrap.sh
```

`bootstrap.sh` 做四件事：创建 `.venv` 并按锁文件安装依赖；按固定提交安装 ComfyUI 和节点到 `.runtime/`；下载并校验第三方提示词规范；生成 Xcode 工程。它**不下载任何模型权重**。

装完后检查一遍：

```bash
./scripts/check.sh
```

## 启动

```bash
./scripts/start-engine.sh
```

引擎在 `https://0.0.0.0:18777` 监听，使用首次启动时生成的自签名证书。本机模型（18778）和 ComfyUI（18779）只监听 `127.0.0.1`，需要时才启动。

第一次启动会创建 `data/`，里面是你的全部数据：对话、记忆、素材、技能、设备凭据和各种密钥。它已经在 `.gitignore` 里，不要提交，也不要放进共享盘。

没有模型也能启动。此时在设置里填一个云端模型服务的 API Key 就能对话；要用本机模型见[本地模型](MODELS.md)。

## 配置

配置写在项目根目录的 `.env`（参考 [.env.example](../.env.example)），或用同名环境变量。全部可选。

| 变量 | 默认 | 作用 |
|---|---|---|
| `JURVIS_DATA` | `data` | 数据目录 |
| `JURVIS_HOST` | `0.0.0.0` | 监听地址。只在本机使用时改成 `127.0.0.1` |
| `JURVIS_PORT` | `18777` | 引擎端口 |
| `JURVIS_LLM_PORT` / `JURVIS_COMFY_PORT` | `18778` / `18779` | 本机模型和 ComfyUI 的端口 |
| `JURVIS_LOCAL_MODEL` | 空 | 换用别的本机模型，见[本地模型](MODELS.md) |
| `JURVIS_SPEECH_STT` / `JURVIS_SPEECH_TTS` | `speech/sensevoice-small` / `speech/qwen3-tts-0.6b` | 语音识别和合成模型的目录，见[本地模型](MODELS.md#语音模型) |
| `JURVIS_SHARED_SKILLS` | `~/.claude/skills` | 额外读取的技能目录；留空关闭 |
| `JURVIS_BROWSER_HEADLESS` | `false` | 浏览器不显示窗口（自动化测试用） |
| `JURVIS_TEAM_ID` | 空 | Apple 开发团队 ID，只有上传 TestFlight 时用 |

模型服务的 API Key、MCP 服务和智能家居的令牌不写在 `.env`，在 App 的设置里填，保存在 `data/security/`。

## 开机自启

引擎没有自带守护进程。Mac App 打开时会自动拉起引擎；只用网页版或手机时，可以用 launchd 让它开机就跑。把下面的文件存为 `~/Library/LaunchAgents/app.jurvis.engine.plist`，把两处路径换成你的项目位置：

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>app.jurvis.engine</string>
  <key>ProgramArguments</key><array><string>/path/to/Jurvis/scripts/start-engine.sh</string></array>
  <key>WorkingDirectory</key><string>/path/to/Jurvis</string>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
</dict></plist>
```

```bash
launchctl load ~/Library/LaunchAgents/app.jurvis.engine.plist
```

这份配置是按 launchd 的常规写法给出的，本项目没有对它做过长期运行验证。

## 从其他设备访问

- **同一个局域网**：`https://<Mac 的局域网地址>:18777`。首次启动时 macOS 会询问是否允许接入连接，选允许。
- **在外面**：在 Mac 和设备上装 [Tailscale](https://tailscale.com) 并登录同一个账号，用 Mac 的 Tailscale 地址访问。配对邀请会同时带上两个地址，App 自动切换。
- **不要把 18777 端口直接映射到公网**。引擎能读写文件、运行命令，访问控制只有配对凭据这一层。

每台设备都要先配对：在 Mac 上的 **设置 → 配对 iPhone 或 iPad** 创建一次性邀请，在设备上扫码或粘贴。Mac App 用本机凭据直接连接，不需要配对；网页版在每个浏览器里都要配对一次。

## 更新与备份

```bash
git pull
```

```bash
./scripts/bootstrap.sh
```

然后重启引擎。备份前先停掉引擎，再整体复制 `data/`。模型在 `models/`，体积很大，可以单独处理。

## 不要上传的东西

Fork 或提交代码前确认这些都没有进仓库（默认的 `.gitignore` 已经排除）：

- `data/`：对话、记忆、素材，以及 `data/security/` 里的 API Key、TLS 私钥和设备令牌
- `.env`：你的 Apple 团队 ID 等本机配置
- `models/`、`.runtime/`：模型权重和第三方运行时，各有自己的许可证
- `build/`、`xcuserdata/`：构建产物和 Xcode 的个人设置

提交前可以用 `git status --short` 和 `git diff --cached --stat` 看一眼。
