# Jurvis

Mac 驱动的原生 Apple 个人助理，包含独立的 Coding、生图和短剧工作台。Mac、iPhone、iPad 共用 SwiftUI 界面和本地任务引擎。当前版本 **0.1.0 开发预览**：已有真实模型和媒体验证，尚未完成真机、长期稳定性和发布签名验收。

## 已实现

- **助理**：本地视觉语言模型、持久化对话、图片理解、计算、工作区文本文件、可编辑记忆，以及创作和编码工具调用。
- **Coding**：Mac 授权项目目录，源码浏览和搜索，选择上下文，生成修改建议，统一差异审阅，文件版本冲突检查，应用和撤销快照。支持 Python pytest、Swift Package 测试，以及项目声明的 npm test/build/lint/typecheck；脚本由用户明确点击运行。
- **图片**：Qwen Image 2.1 文生图、参考图编辑、种子和画幅、素材导入与导出。
- **短剧**：故事梗概、角色、分镜、参考图、首尾帧、中文对白提示、分镜版本、逐镜审片、整片 H.264/AAC 导出。视频可提取首尾帧继续创作。
- **任务**：SQLite 持久化串行队列、取消、重试、重启中断标记、幂等提交；重任务期间阻止 Mac 自动空闲睡眠。
- **连接**：TLS、证书指纹固定、一次性配对邀请、设备凭据钥匙串保存、设备撤销。移动端操作 Mac 上的数据和已授权的代码项目。

## 在这台 Mac 使用

打开 `build/release/Build/Products/Release/Jurvis.app`。它会连接已经运行的引擎；引擎不在线时自动尝试从当前项目目录启动。

进入 **Coding → 连接项目** 选择你的代码目录。进入 **创作** 生成图片或视频，进入 **短剧** 创建分镜项目。素材和结果保存在 `data/`，原始模型保存在 `models/`。

在 **设置 → 配对 iPhone 或 iPad** 创建邀请。移动设备安装 Jurvis 后扫描二维码或粘贴链接；设备需与 Mac 网络互通，Mac 需要保持开机。当前是直接局域网连接，没有云端中继。

## 开发安装与构建

要求：Apple Silicon、完整 Xcode、Python 3.12（uv 可管理）、uv、ffmpeg。当前真实验证机器为 M5 Pro / 64GB；更低内存机型尚未验证。依赖和模型占用大量磁盘空间。

```bash
./scripts/bootstrap.sh
./scripts/check.sh
./scripts/build-apps.sh
```

完整验收（真实模型，独立端口和临时数据目录，不影响正在使用的引擎和数据）：

```bash
./scripts/e2e.sh
./scripts/ui-smoke.sh
```

`e2e.sh` 通过 App 使用的同一套 API 跑完对话与工具调用、读图、停止与重试、生图、视频、首尾帧、短剧全流程、Coding 修改/测试/撤销、配对与撤销。`ui-smoke.sh` 连接正在运行的引擎，离屏渲染 Mac 版每个页面和每个素材详情（包括视频），截图保存在 `build/ui-smoke/`。

`bootstrap.sh` 使用锁定的 Python 依赖、ComfyUI 和节点提交，生成本机模型路径，不替换原有模型。单独运行后端：`./scripts/start-engine.sh`。开发时也可直接在 Xcode 打开 `native/Jurvis.xcodeproj`。

构建产物：

| 目标 | 位置 |
|---|---|
| Mac 本地 App | `build/release/Build/Products/Release/Jurvis.app` |
| iPhone / iPad 模拟器 | `build/mobile-release/Build/Products/Release-iphonesimulator/Jurvis.app` |
| 真机工程 | Xcode 中选 `JurvisMobile`，配置自己的开发团队与签名 |

Mac App 当前依赖此工作目录内的引擎、运行时和模型。它还不是可独立移到任意机器运行的安装包。项目移动后需重新运行安装和构建脚本；Mac 分发公证、iOS 签名和 App Store 发布均未执行。

## 创作扩展与准确版本

扩展在设置中安装，或运行 `.venv/bin/python scripts/install_components.py <组件ID>`。下载可续传，完成后核验 SHA-256；已下载不代表组合效果已通过实测。

| 组件 | ID | 接入方式 |
|---|---|---|
| H3 FL2VA INT8 Pruned | `h3_convrot_int8` | 原生 UNETLoader + Apple Silicon 量化适配；与 Civitai 发布版本哈希一致 |
| Motion Speed / FoS v1.0 | `h3_motion_fos` | -8 到 +8 强度，0 关闭；影响生成运动，不是播放速度 |
| Turbo multistep v2 | `h3_turbo_v2` | 独立的 4/6/8 步方案，视频/音频 shift 6/3；需要准确的权重文件 |
| DaSiWa C-MMH3 v3.3 | `dasiwa_v33` | 原版存档校验 + Mac 核心流程适配：res_multistep/simple、25 步、shift 11/4 |

本机已完成 ConvRot 和 Motion Speed 的准确文件安装，以及 DaSiWa 原版存档校验。ConvRot + DaSiWa 核心流程已实际生成视频；Motion Speed 已在另一段 GGUF 视频中实跑。具体分辨率、耗时与测试范围见验收记录。

DaSiWa 适配不执行第三方工作流里的任意节点，也未启用 RTX 增强、潜空间连续生成、RefMod 和完整多模态 Ref2VA。当前首尾帧流程使用 FL2VA 权重。原版工作流及模型路径固定在 `config/media-catalog.json`，不会用同名的其他版本静默替代。

若来源要求登录：从组件来源页下载准确文件，放到设置显示的位置，再点击「安装 / 校验」。不要把网站令牌写入源码或工作流。Turbo v2 在本轮环境中未能匿名下载，因此其配置已接入，但尚未启用和实测。

最初提供的 [Ref2VA Prompt Rewriter](https://huggingface.co/lightx2v/MiniMax-H3-Prompt-Rewriter-Qwen3.8-27B-Ref2VA) 是专门的提示词改写模型。当前实现使用已有 Qwen 视觉语言模型和对应 FL2VA 官方提示规范完成编排，**尚未下载或运行这份独立 SFT 权重**；不能视为与专用改写模型效果相同。规范来源固定在 `engine/prompts/sources.json`。

## 数据、运行和维护

- `engine/`：API、持久化队列、模型进程、工具、代码工作台、媒体工作流。
- `native/Sources/`：共享的 SwiftUI 界面、TLS 客户端、钥匙串和扫码。
- `config/`：固定的依赖版本、运行时来源、扩展校验清单。
- `data/jurvis.sqlite3`：对话、任务、素材索引、分镜、记忆、设备和代码项目。
- `data/assets/`：生成媒体；`data/coding/`：代码修改快照；`data/logs/`：运行日志。
- `data/security/`：设备本地令牌和 TLS 私钥；整个 `data/` 默认仅当前用户可访问，不应上传到仓库。

引擎监听 TLS 18777；MLX 和 ComfyUI 分别只监听本机 18778/18779。模型串行切换以控制内存。退出 Mac 界面后已启动的引擎可继续处理移动端任务。

备份前停止引擎，然后整体备份 `data/`。代码撤销仅在文件仍与本次应用后的版本一致时执行；其他编辑器的更新会触发冲突提示。代码执行是本机项目脚本，当前没有容器沙箱、自动提交、推送或发布。

真实验收结果与尚未覆盖的项目见 [验收记录](docs/VALIDATION.md)。第三方组件来源见 [来源记录](docs/SOURCES.md)。
