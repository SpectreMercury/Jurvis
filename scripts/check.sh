#!/usr/bin/env bash
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JURVIS_PROJECT_ROOT"
.venv/bin/python -m pytest -q
.venv/bin/python -m compileall -q engine scripts
bash -n scripts/bootstrap.sh scripts/start-engine.sh scripts/build-apps.sh
echo '后端检查完成。原生编译请运行 scripts/build-apps.sh。'
