#!/usr/bin/env bash
# Type into the Coding page's text fields (ASCII and Chinese IME) against the running
# engine while the app polls, and fail if text, composition or focus is lost.
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JURVIS_PROJECT_ROOT"
mkdir -p build/ui-typing
sources=$(ls native/Sources/*.swift | grep -v Scanner.swift)
swiftc -D UI_SMOKE -parse-as-library -target arm64-apple-macos15.0 $sources scripts/ui_typing.swift -o build/ui-typing/ui-typing 2>&1 | grep -E "error" || true
[ -x build/ui-typing/ui-typing ] || { echo "UI typing build failed"; exit 1; }
JURVIS_ROOT="$JURVIS_PROJECT_ROOT" build/ui-typing/ui-typing "$@"
