#!/usr/bin/env bash
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JURVIS_PROJECT_ROOT"
command -v uv >/dev/null || { echo '请先安装 uv：https://docs.astral.sh/uv/getting-started/installation/'; exit 1; }
command -v ffmpeg >/dev/null || { echo '请先安装 ffmpeg（例如 brew install ffmpeg）'; exit 1; }
command -v xcodebuild >/dev/null || { echo '请先安装 Xcode 并完成首次启动'; exit 1; }
[[ -x .venv/bin/python ]] || uv venv --python 3.12 .venv
uv pip sync --python .venv/bin/python config/engine-requirements.lock
.venv/bin/python scripts/setup_media.py
.venv/bin/python scripts/generate_xcode.py
echo '引擎依赖已准备好。运行 scripts/build-apps.sh 构建原生 App。模型权重保留在 models/，不会自动下载替换。'
