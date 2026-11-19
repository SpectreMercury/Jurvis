# 创作扩展与准确版本

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
