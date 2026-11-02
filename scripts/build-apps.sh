#!/usr/bin/env bash
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JURVIS_PROJECT_ROOT"
.venv/bin/python scripts/generate_xcode.py
xcodebuild -project native/Jurvis.xcodeproj -scheme JurvisMac -configuration Release -derivedDataPath build/release -destination 'platform=macOS' build CODE_SIGNING_ALLOWED=NO
codesign --force --deep --sign - build/release/Build/Products/Release/Jurvis.app
xcodebuild -project native/Jurvis.xcodeproj -scheme JurvisMobile -configuration Release -derivedDataPath build/mobile-release -destination 'generic/platform=iOS Simulator' build CODE_SIGN_IDENTITY=- CODE_SIGNING_ALLOWED=YES
echo 'Mac: build/release/Build/Products/Release/Jurvis.app'
echo 'iPhone / iPad 模拟器: build/mobile-release/Build/Products/Release-iphonesimulator/Jurvis.app'
echo '安装真机：在 Xcode 中打开 native/Jurvis.xcodeproj，选择 JurvisMobile 和你的开发团队。'
