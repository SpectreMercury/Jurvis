#!/usr/bin/env bash
# Render every Mac screen against the running engine and save snapshots to build/ui-smoke.
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JURVIS_PROJECT_ROOT"
mkdir -p build/ui-smoke
sources=$(ls native/Sources/*.swift | grep -v Scanner.swift)
swiftc -D UI_SMOKE -parse-as-library -target arm64-apple-macos15.0 $sources scripts/ui_smoke.swift -o build/ui-smoke/ui-smoke 2>&1 | grep -E "error" || true
[ -x build/ui-smoke/ui-smoke ] || { echo "UI smoke build failed"; exit 1; }
rm -f build/ui-smoke/*.png
JURVIS_ROOT="$JURVIS_PROJECT_ROOT" build/ui-smoke/ui-smoke build/ui-smoke
