#!/usr/bin/env bash
# Start an isolated engine (own ports and data dir), run the multi-turn conversation scenario
# suite with the real model, then stop it. Your normal engine and data are untouched.
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JURVIS_PROJECT_ROOT"
work="${1:-$(mktemp -d -t jurvis-e2e)}"
mkdir -p "$work/data"
export JURVIS_DATA="$work/data" JURVIS_PORT=18787 JURVIS_LLM_PORT=18788 JURVIS_COMFY_PORT=18789
.venv/bin/python -m engine > "$work/engine.log" 2>&1 &
engine=$!
trap 'kill -TERM $engine 2>/dev/null; wait $engine 2>/dev/null || true' EXIT
for _ in $(seq 1 30); do [ -f "$work/data/security/local.json" ] && curl -sk https://127.0.0.1:18787/health >/dev/null && break; sleep 1; done
.venv/bin/python -W ignore scripts/scenarios.py "$work/data" "$work" "${@:2}"
