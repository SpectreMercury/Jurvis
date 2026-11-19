# 本地模型

Jurvis 里的对话模型有三种来源，可以同时存在，在设置里选默认的，每段对话也能单独选：

| 来源 | 怎么接 | 适合 |
|---|---|---|
| 内置的本机模型 | 把 MLX 格式的模型放进 `models/`，由引擎自己启动 | 完全离线，支持读图 |
| 本机的其他模型服务 | Ollama、LM Studio、llama.cpp 等，作为 OpenAI 兼容服务添加 | 已经在用这些工具，或想用 GGUF 模型 |
| 云端服务 | Miavo（内置）或任何 OpenAI 兼容的接口 | 机器内存不够，或需要更强的模型 |

仓库不包含任何模型权重，也不会自动下载。

## 方式一：内置的本机模型

引擎用 [mlx-vlm](https://github.com/Blaizzy/mlx-vlm) 的服务加载一个 MLX 格式的模型目录。默认位置是 `models/qwen/aeon-mxfp4-mtplx`（Qwen 27B 的视觉语言模型，4 bit 量化）。

**换成自己的模型**：

1. 准备一个 mlx-vlm 能加载的模型目录（里面有 `config.json`、`tokenizer.json` 和 `*.safetensors`）。可以从 Hugging Face 下载现成的 MLX 转换版：

   ```bash
   .venv/bin/hf download <组织>/<模型仓库> --local-dir models/<你起的名字>
   ```

   也可以用 `.venv/bin/python -m mlx_vlm.convert` 把原始模型转换并量化，参数见 mlx-vlm 的文档。

2. 在 `.env` 里指向它。相对路径从 `models/` 算起，也可以写绝对路径：

   ```bash
   JURVIS_LOCAL_MODEL=<你起的名字>
   ```

3. 重启引擎。设置页的模型列表里，「本机」会显示这个目录的名字。

**选模型时要知道的**：

- 内存：模型文件大小加上几个 GB 的余量要小于可用内存。本机模型和生图、视频共用显卡内存，引擎会串行切换。
- 工具调用：Jurvis 靠模型调用工具来读写文件、搜索、生图。引擎按 OpenAI 的 `tool_calls` 格式解析，并兼容 Qwen 系列在文本里输出的 `<function=…>` 写法。聊天模板不支持工具的模型只能聊天，做不了事。
- 读图：要用图片理解，选视觉语言模型（VLM）。纯文本模型也能用，但发图片会出错。
- 目前只有默认的 Qwen 27B 跑过完整的场景验收。换别的模型后建议跑一遍 `./scripts/scenarios.sh` 看它能否正确调用工具。

启动失败时看 `data/logs/llm.log`。

## 方式二：Ollama、LM Studio 等本机服务

这些工具都提供 OpenAI 兼容的接口，在 Jurvis 里和云端服务一样添加，但数据不出本机。

1. 启动服务并拉好模型。例如 Ollama：

   ```bash
   ollama pull qwen3:14b
   ```

2. 在 Jurvis 的 **设置 → 模型服务 → 添加 OpenAI 兼容服务** 填写：

   | 工具 | 接口地址 | API Key |
   |---|---|---|
   | Ollama | `http://127.0.0.1:11434/v1` | 随便填，例如 `ollama` |
   | LM Studio | `http://127.0.0.1:1234/v1` | 随便填 |
   | llama.cpp `llama-server` | `http://127.0.0.1:8080/v1` | 启动时没设就随便填 |

3. 保存后从它的模型列表里选一个，设为默认或在某段对话里单独选。模型名写成 `服务名:模型名`，例如 `ollama:qwen3:14b`。

同样要选支持工具调用的模型。Jurvis 把这类服务当作「云端」对待：对话不占用内置模型的串行队列，可以几段同时进行。Ollama 和 LM Studio 的接入走的是通用的 OpenAI 兼容路径，本项目没有逐个做过实测。

## 方式三：云端服务

**设置 → 模型服务**。Miavo 是内置的，填 API Key 即可，联网搜索由它随模型提供。其他服务（OpenAI、DeepSeek、OpenRouter 等）点「添加 OpenAI 兼容服务」，填接口地址（到 `/v1` 为止）和 Key。

Key 保存在 `data/security/providers.json`，只有当前用户可读，不会发给配对的设备。使用云端模型时，对话内容、附带的图片、取回的记忆和工具结果会发送给所选的服务。

## 辅助模型

设置里还有两个可选项：

- **工具模型**：用来写摘要、提取记忆、改写搜索词的便宜模型。留空则用对话模型。
- **向量模型**：填 `服务名:模型名` 后记忆按语义检索；留空用内置的关键词匹配，离线可用。

## 语音模型

语音输入、朗读和语音对话由 Mac 上的两个模型完成，手机和网页版只负责录音和播放，录音不会发到别处。两个模型都用 [mlx-audio](https://github.com/Blaizzy/mlx-audio) 加载，第一次使用时载入，闲置 10 分钟后自动释放内存。

| 用途 | 默认目录 | 默认模型 | 大小 |
|---|---|---|---|
| 语音识别 | `models/speech/sensevoice-small` | [SenseVoiceSmall](https://huggingface.co/mlx-community/SenseVoiceSmall)，中英日韩粤 | 约 0.9GB |
| 语音合成 | `models/speech/qwen3-tts-0.6b` | [Qwen3-TTS 0.6B CustomVoice 8bit](https://huggingface.co/mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit)，9 种声音 | 约 1.8GB |

安装：

```bash
.venv/bin/hf download mlx-community/SenseVoiceSmall --local-dir models/speech/sensevoice-small
```

```bash
.venv/bin/hf download mlx-community/Qwen3-TTS-12Hz-0.6B-CustomVoice-8bit --local-dir models/speech/qwen3-tts-0.6b
```

装好后重新打开设置页，「语音」一栏会显示两个模型的名字，输入框旁出现麦克风和语音对话按钮。只装识别模型时只有语音输入；两个都装才有语音对话。在设置里可以选朗读的声音。

换用 mlx-audio 支持的其他模型时，在 `.env` 里用 `JURVIS_SPEECH_STT` 和 `JURVIS_SPEECH_TTS` 指向它们的目录（相对路径从 `models/` 算起）。目前只有上面两个默认模型实测过；其他合成模型的声音列表和调用方式可能不同。

在 M5 Pro 上的实测：识别 5 秒的录音约 0.05 秒；合成一句话约 0.6 到 2 秒，比播放快。模型首次载入需要几秒到半分钟。

网页版用麦克风需要浏览器允许，iPhone 上的 Safari 还要求页面是受信任的 HTTPS，见[网页版](PWA.md)。

## 生图和视频模型

生图（Qwen Image 2.1）和视频（MiniMax H3）走 ComfyUI，权重放在 `models/qwen-image/` 和 `models/minimax-h3/`，具体文件名在设置页的模型状态里列出。可选的扩展用 `.venv/bin/python scripts/install_components.py <组件ID>` 安装并校验，清单和来源见 [README](../README.zh-CN.md#创作扩展与准确版本) 与[来源记录](SOURCES.md)。
