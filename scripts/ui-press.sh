#!/usr/bin/env bash
# Click real buttons and check they answer at once. Needs no engine and touches no data.
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JURVIS_PROJECT_ROOT"
mkdir -p build/ui-press
sources=$(ls native/Sources/*.swift | grep -v Scanner.swift)
swiftc -D UI_SMOKE -parse-as-library -target arm64-apple-macos15.0 $sources scripts/ui_press.swift -o build/ui-press/ui-press 2>&1 | grep -E "error" || true
[ -x build/ui-press/ui-press ] || { echo "UI press build failed"; exit 1; }
build/ui-press/ui-press build/ui-press
