# 第三方来源

Jurvis 的代码和本地数据独立于第三方模型、节点和工作流。下列资源保留各自的许可证和使用条款；本项目未将第三方权重纳入 Git。

- [MiniMax H3 原始模型](https://huggingface.co/MiniMaxAI/MiniMax-H3) 与 [Comfy-Org 打包](https://huggingface.co/Comfy-Org/MiniMax-H3)：模型按其仓库的 MiniMax H3 Community License 处理。
- [ConvRot 量化版本](https://civitai.com/models/2830065?modelVersionId=3193337)：SHA-256 与 Comfy-Org 的 `minimax_h3_fl2va_pruned_int8_convrot.safetensors` 一致。
- [FoS Motion Speed v1.0](https://civitai.com/models/2951265?modelVersionId=3342524)：文件为 `H3_speed_slider_1.1.safetensors`。本地安装使用公开的 [RunningHub 文件](https://huggingface.co/RunningHubAI/rh-h3-speed-slider-1.1-lora)，已按来源记录核对完整哈希。
- [Turbo multistep v2](https://civitai.com/models/2929860?modelVersionId=3357658)：Hyperflow / EMA600 的作者混合版本；配方与原先的 Parasyte Turbo 分开保存。
- [DaSiWa v3.3](https://civitai.com/models/2831978?modelVersionId=3195699)：原版从作者 [dasiwa-comfyui-workflows](https://github.com/darksidewalker/dasiwa-comfyui-workflows) 的 `5267cd244440747ddad6075b9807a428aa448b9b` 提交下载；原版工作流仓库为 GPL-3.0。下载存档不随 Jurvis 源码分发，核心采样参数单独映射到已安装的原生 ComfyUI 节点。
- [ComfyUI-DaSiWa-Nodes](https://github.com/darksidewalker/ComfyUI-DaSiWa-Nodes)：用于核查能力和平台限制，当前未安装此节点包。
- [LightX2V FL2VA Prompt Rewriter](https://huggingface.co/lightx2v/MiniMax-H3-Prompt-Rewriter-Qwen3.8-27B-FL2VA)：提示词编排规范；具体文件提交与哈希见 `engine/prompts/sources.json`。
- MLX / MLX-LM / MLX-VLM、ComfyUI、ComfyUI-GGUF、ComfyUI-AppleSilicon-FP8 和 PlagueKind 节点：运行版本见 `config/runtime-sources.json` 与两份依赖锁。运行时 Git checkout 内保留上游许可证文件。

本项目只接入上面列出的组件。
