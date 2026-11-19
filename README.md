<p align="center"><img src="docs/logo.png" width="128" height="128" alt="Jurvis"></p>

<h1 align="center">Jurvis</h1>

<p align="center">English · <a href="README.zh-CN.md">简体中文</a> · <a href="README.ja.md">日本語</a></p>

A personal assistant that runs on your Mac and is built around one conversation: chat, image understanding, image generation, video, short dramas, code changes and smart-home control all happen in the same thread. Models can be local or in the cloud. Mac, iPhone and iPad share one SwiftUI interface, and there is a web app that installs to the home screen of any phone or computer. All of them connect to the same local task engine. Current version: **0.1.0 developer preview**. It has been validated with real models and real media, but has not yet been through on-device testing, long-term stability testing or release signing.

The app interface is currently in Chinese. Labels quoted below are shown as they appear in the app, with an English gloss.

## Documentation

| | |
|---|---|
| [Deploying the engine](docs/DEPLOY.md) | Install, start, configure, start at login, reach it from other devices |
| [Local models](docs/MODELS.md) | Use your own MLX model, connect Ollama / LM Studio, cloud providers |
| [Web app (PWA)](docs/PWA.md) | Open, pair, install to the home screen |
| [Native apps](docs/APPS.md) | Build the Mac app, install on iPhone / iPad, TestFlight |
| [Smart-home connectors](docs/CONNECTORS.md) | Supported brands and the token each one needs |

These documents are currently available in Chinese only.

## What works today

- **Models**: A local vision-language model is the default and works fully offline. Enter a [Miavo](https://miavo.xyz) API key to use cloud models instead (pick a default in settings; each conversation can choose its own), or add any OpenAI-compatible service (OpenAI, DeepSeek, OpenRouter, Ollama, LM Studio and so on).
- **Web**: With a Miavo model, web search is provided by Miavo alongside the model and sources are listed under the reply. With the local model, the assistant can still search through Miavo if it is configured. Any model can read a web link you give it (it will not read local or private-network addresses).
- **Memory**: After each reply, facts worth keeping are extracted from the conversation in the background (never passwords or keys), and the relevant few are retrieved before the next conversation. With an embedding model configured, retrieval is semantic; otherwise a built-in keyword match is used, which works offline. Memories can be viewed, edited, disabled and cleared. An 「无痕」 (incognito) conversation neither reads nor writes memory. Early parts of long conversations are folded into a summary instead of being dropped.
- **Skills**: Write how to do a kind of task as `data/skills/<name>/SKILL.md`. The assistant loads the full text only when the task matches, and reads a skill's reference files on demand. The format is the same SKILL.md other agents use: skills written for Claude Code in `~/.claude/skills/` show up directly (disable them one by one in settings, or turn them all off with `JURVIS_SHARED_SKILLS=`), and a connected code project can carry its own `.jurvis/skills/`. When names collide, yours win.
- **MCP**: Connect MCP servers (local process or HTTP) and their tools appear in the conversation. Tools not marked read-only ask you first, every time. Past 24 tools they are no longer all listed to the model; it looks them up by keyword and then calls them.
- **Browser**: The assistant can open a browser window on the Mac (Chrome, Edge, Brave or Chromium must be installed; it uses a separate profile and does not touch your own browser data), read pages, list clickable elements, click, type and take screenshots. Opening and reading need no confirmation; clicking, typing and running scripts ask first, with the option 「本任务内都允许」 (allow for this task). Downloads are always refused. Enter passwords and payment details yourself in the window.
- **Plan**: 「计划」 (Plan) under the input box makes the assistant look without acting and hand you a checklist first; it starts after you confirm or edit it. With cloud models, larger tasks also get a checklist automatically, ticked off item by item.
- **Output preview**: HTML pages and SVG images the assistant writes render directly in the workspace and the library, switchable between preview and source. Pages run in an isolated frame that cannot read Jurvis sign-in data and cannot reach the network.
- **Smart home**: After connecting Home Assistant, Philips Hue, SmartThings or Tuya, say "dim the living-room lights a little" in the conversation, or operate devices directly on the 「家居」 (Home) page. Locks, doors and cameras ask first, every time. See the [connector guide](docs/CONNECTORS.md).
- **Subtasks**: The assistant can hand a side task such as research to a read-only helper and bring only the conclusion back. With cloud models, several subtasks handed off in one step run at the same time; the local model does one at a time.
- **File search**: Besides reading and writing workspace documents, the assistant can find files by path pattern and search contents with regular expressions (in the workspace and in connected code projects), and reads long files in line ranges.
- **Conversation**: Searchable history grouped by date, image understanding, calculation, workspace text files, and creative and coding tool calls. The 「图片 / 视频 / 短剧」 (Image / Video / Short drama) switches under the input box set what to generate, and the 「代码」 (Code) switch attaches a code project to a new conversation.
- **Running commands**: When it needs to install a plugin, run a script, use git or inspect the system, Jurvis runs shell commands on the Mac. Each command first appears as a card with the reason, the command and the directory, and runs only when you click 「运行」 (Run), with live output. You can choose 「本对话都允许」 (allow for this conversation), but dangerous commands such as deletion, sudo, git push and kill still ask every time. Commands that need to keep running (dev servers, watchers) can go to the background; the assistant checks their output at any time and stops them when done, and they end with the engine.
- **Goal mode**: After clicking 「目标」 (Goal), the message you send is a goal to carry through to completion. Jurvis works autonomously (up to 40 steps and 45 minutes), applies code changes directly (undoable), and must verify with tools before ending with finish_goal. A separate independent review checks the execution record and sends the work back if the evidence is insufficient. When stuck or missing information, it stops and asks you.
- **Code**: After attaching a project directory you have authorised on the Mac, Jurvis reads and searches the source itself. Changes appear as diff cards in the reply and are written only when you click 「应用修改」 (Apply changes), with file-version conflict checks and undo. Supports Python pytest, Swift Package tests, and the npm test/build/lint/typecheck scripts a project declares; scripts run only when you click.
- **Images**: Qwen Image 2.1 text-to-image, reference-image editing, seed and aspect ratio, asset import and export.
- **Short dramas**: Synopsis, characters, storyboard, reference images, first and last frames, Chinese dialogue prompts, shot versions, shot-by-shot review, and H.264/AAC export of the whole film. First and last frames can be extracted from a video to keep creating.
- **Tasks**: A persistent SQLite queue with cancel, retry, interruption marking on restart and idempotent submission. Tasks that use the local GPU (local model, images, video) run one at a time; cloud-model conversations run alongside them, up to 4 at once. The Mac is kept from idle sleep during heavy tasks.
- **Connection**: TLS, certificate fingerprint pinning, one-time pairing invitations, device credentials in the keychain, and device revocation. Mobile devices operate on the data and authorised code projects on the Mac.

## Using it on this Mac

Open `build/release/Build/Products/Release/Jurvis.app`. It connects to a running engine, and tries to start one from the current project directory if none is online.

Say what you want in the conversation, or click 「图片 / 视频 / 短剧」 under the input box first. To change code, click 「代码 → 连接项目文件夹…」 (Code → Connect project folder…), pick a directory, and make your request in that conversation. 「素材 / 短剧 / 任务 / 更多」 (Assets / Short dramas / Tasks / More) at the bottom left are the management pages. Assets and results are stored in `data/`; original models are stored in `models/`.

Create an invitation in **设置 → 配对 iPhone 或 iPad** (Settings → Pair iPhone or iPad). After installing Jurvis on the mobile device, scan the QR code or paste the link. The device must be able to reach the Mac over the network, and the Mac must stay on. The connection goes directly from the device to the Mac, with no cloud relay.

**Away from home**: Install [Tailscale](https://tailscale.com) on the Mac and the phone and sign in to the same account. The invitation carries both the Mac's LAN address and its Tailscale address; the app uses the LAN at home and switches to Tailscale automatically when you leave. Devices that are already paired learn the new address the next time they connect at home, without pairing again. Away from home, open the web app at `https://<the Mac's Tailscale address>:18777/app/`.

### Web app (PWA)

The engine ships with the web app. Its source is in `web/` and needs no build step. Open `https://127.0.0.1:18777/app/` on the Mac, or `https://<the Mac's address>:18777/app/` on another device on the same network, and paste the code from a pairing invitation. The `web_link` returned by the invitation API can be opened directly to complete pairing.

The engine uses a self-signed certificate, so the browser warns that it is untrusted the first time and you have to continue manually. Under such a certificate browsers do not allow a Service Worker to be registered, so the offline shell and install prompt after "Add to Home Screen" need a trusted certificate (for example one issued with mkcert with its root installed on the device, or access through Tailscale HTTPS or a reverse proxy). Without one the web app works as usual; it just cannot open offline.

## Development setup and build

Requirements: Apple Silicon, full Xcode, Python 3.12 (uv can manage it), uv and ffmpeg. The machine used for real validation is an M5 Pro with 64GB; machines with less memory have not been validated. Dependencies and models take a lot of disk space.

```bash
./scripts/bootstrap.sh
./scripts/check.sh
./scripts/build-apps.sh
```

Full acceptance (real models, separate ports and a temporary data directory, so the engine and data you are using are not affected):

```bash
./scripts/e2e.sh
./scripts/scenarios.sh
./scripts/longtasks.sh
./scripts/ui-smoke.sh
./scripts/ui-typing.sh
./scripts/ui-press.sh
```

`e2e.sh` uses the same API as the apps to run conversation and tool calls, image understanding, stop and retry, image generation, video, first and last frames, the full short-drama flow, coding change/test/undo, and pairing and revocation. `scenarios.sh` runs more than 20 multi-turn conversations with a real model (small talk, follow-up calculation, memory, files, injection defence, image/video/short drama, coding conversations, language switching, capability limits, long text, stopping, vague requests, running a command to install a plugin, declining a command, goal mode fixing code until tests pass, asking first on a vague goal), checks turn by turn which tools were called, their arguments and the reply, and writes a readable transcript. `longtasks.sh` runs long tasks that exceed a single model output with a real model, judged by measured results and not by what the model says: writing a 5-episode script and then changing only episode 2 with the file updated in place; a 10-episode script of a thousand characters each in one file (word count checked per episode, and episodes must differ); a 5,000-character story written directly in the conversation; goal mode writing 6 episode files plus an index; a short-drama storyboard plan (durations summed, no shot over 15 seconds) with follow-up questions; and a 22-turn conversation (early details, returning to the original task after a change of topic, editing a document in place). The whole set takes one to two hours on the local model; you can run chosen cases only: `./scripts/longtasks.sh <temp dir> "ten episodes in one file"`, with the transcript written to `longtasks.md` in that directory. Current status (local Qwen 27B, 2026-11-20): all 6 groups have passed, but not in one single run. The most recent complete run passed 4 groups, and the remaining 2 passed when rerun on their own after fixes. The 26 regression conversations scored 25/26 in three consecutive rounds, with a different one failing each round (occasional arithmetic slips and wrong-language replies by the model), so treat this as acceptance with some variance, not as constantly green.

`ui-typing.sh` simulates keyboard, mouse and Chinese input method in the real input box and checks focus, composition and response latency. `ui-press.sh` clicks buttons with real mouse events and checks that a busy state appears within 50 ms, that width does not change, that repeated clicks do not run twice and that a failure can be retried; it needs no engine and touches no data. `ui-smoke.sh` connects to a running engine and renders every page and every asset detail (including video) of the Mac app off-screen, saving screenshots to `build/ui-smoke/`.

`bootstrap.sh` uses locked Python dependencies and pinned ComfyUI and node commits, generates local model paths, and does not replace existing models. To run the backend alone: `./scripts/start-engine.sh`. During development you can also open `native/Jurvis.xcodeproj` in Xcode.

Build outputs:

| Target | Location |
|---|---|
| Local Mac app | `build/release/Build/Products/Release/Jurvis.app` |
| iPhone / iPad simulator | `build/mobile-release/Build/Products/Release-iphonesimulator/Jurvis.app` |
| Device build | Select `JurvisMobile` in Xcode and set your own development team and signing |
| TestFlight | Set `JURVIS_TEAM_ID` in `.env`, create the app record in App Store Connect, then run `./scripts/testflight.sh` |

The Mac app currently depends on the engine, runtime and models inside this working directory. It is not yet an installer that can be moved to any machine. After moving the project, run the install and build scripts again. Mac notarisation, iOS signing and App Store release have not been done.

## Creative extensions and exact versions

Install extensions in settings, or run `.venv/bin/python scripts/install_components.py <component ID>`. Downloads resume and are verified with SHA-256 when complete. Downloaded does not mean the combination has been tested.

| Component | ID | How it is integrated |
|---|---|---|
| H3 FL2VA INT8 Pruned | `h3_convrot_int8` | Native UNETLoader plus an Apple Silicon quantisation adapter; hash matches the version published on Civitai |
| Motion Speed / FoS v1.0 | `h3_motion_fos` | Strength -8 to +8, 0 is off; affects generated motion, not playback speed |
| Turbo multistep v2 | `h3_turbo_v2` | A separate 4/6/8-step scheme, video/audio shift 6/3; needs the exact weight file |
| DaSiWa C-MMH3 v3.3 | `dasiwa_v33` | Original archive verification plus a core-flow adaptation for Mac: res_multistep/simple, 25 steps, shift 11/4 |

On this machine, the exact files for ConvRot and Motion Speed have been installed and the original DaSiWa archive has been verified. The ConvRot + DaSiWa core flow has actually generated video; Motion Speed has been run in a separate GGUF video. See the validation record for resolutions, timings and test scope.

The DaSiWa adaptation does not execute arbitrary nodes from third-party workflows, and does not enable RTX enhancement, latent continuation, RefMod or full multimodal Ref2VA. The current first/last-frame flow uses the FL2VA weights. The original workflow and model paths are pinned in `config/media-catalog.json` and are never silently replaced by another version with the same name.

If a source requires sign-in: download the exact file from the component's source page, put it where settings shows, then click 「安装 / 校验」 (Install / Verify). Do not write site tokens into source code or workflows. Turbo v2 could not be downloaded anonymously in this environment, so its configuration is wired in but it has not been enabled or tested.

The [Ref2VA Prompt Rewriter](https://huggingface.co/lightx2v/MiniMax-H3-Prompt-Rewriter-Qwen3.8-27B-Ref2VA) originally provided is a dedicated prompt-rewriting model. The current implementation orchestrates prompts with the existing Qwen vision-language model and the corresponding official FL2VA prompt guide; **this separate SFT weight has not been downloaded or run**, and the result should not be considered equivalent to the dedicated rewriter. The guide's sources are pinned in `engine/prompts/sources.json`.

## Data, operation and maintenance

- `engine/`: API, task scheduling, model routing (`llm.py`), memory (`memory.py`), skills, MCP, smart-home connectors (`home/`), tools, the code workbench and media workflows.
- `web/`: the web app (PWA).
- `native/Sources/`: the shared SwiftUI interface, TLS client, keychain and QR scanning.
- `config/`: pinned dependency versions, runtime sources and extension checksums.
- `data/jurvis.sqlite3`: conversations, tasks, the asset index, storyboards, memory, devices and code projects.
- `data/assets/`: generated media; `data/coding/`: code change snapshots; `data/logs/`: run logs.
- `data/settings.json`: preferences for the default model, memory and home; `data/skills/`: skills.
- `data/security/`: the device-local token and TLS private key, plus model-provider API keys (`providers.json`), MCP servers (`mcp.json`) and smart-home connector tokens (`home.json`). The whole of `data/` is readable only by the current user by default and should not be uploaded to a repository.

The engine listens on TLS 18777; MLX and ComfyUI listen only on localhost, on 18778 and 18779. Local models are switched serially to keep memory under control. When a cloud model is selected, the conversation (including attached images, retrieved memories and tool results) is sent to the chosen service; the local model sends nothing. After you quit the Mac interface, an engine that is already running keeps handling tasks from mobile devices.

Stop the engine before backing up, then back up `data/` as a whole. Code undo runs only while the file still matches the version from that apply; edits from other editors trigger a conflict prompt. Code execution and the assistant's commands run directly on the machine with no container sandbox. Assistant commands run as your user with no standard input, and never commit, push or publish on their own.

Real acceptance results and what is not yet covered are in the [validation record](docs/VALIDATION.md). Third-party component sources are in the [source record](docs/SOURCES.md).

## License

The Jurvis source code is open source under the [Apache License 2.0](LICENSE). Model weights, ComfyUI nodes and third-party prompt guides are not distributed with the source; each follows its own license and terms of use, detailed in the [source record](docs/SOURCES.md).
