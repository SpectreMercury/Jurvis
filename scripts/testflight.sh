#!/usr/bin/env bash
# Archive JurvisMobile and upload it to App Store Connect / TestFlight.
# Uses the Apple account signed in to Xcode; the build number is a timestamp so every upload is unique.
# Set JURVIS_TEAM_ID (your Apple team ID) in the environment or in .env.
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$JURVIS_PROJECT_ROOT"
TEAM_ID="${JURVIS_TEAM_ID:-$(sed -n 's/^JURVIS_TEAM_ID=//p' .env 2>/dev/null || true)}"
[[ -n "$TEAM_ID" ]] || { echo '请在 .env 中设置 JURVIS_TEAM_ID（你的 Apple 开发团队 ID）'; exit 1; }
.venv/bin/python scripts/generate_xcode.py
BUILD_NUMBER="${BUILD_NUMBER:-$(date +%Y%m%d%H%M)}"
ARCHIVE="build/testflight/Jurvis-$BUILD_NUMBER.xcarchive"
xcodebuild -project native/Jurvis.xcodeproj -scheme JurvisMobile -configuration Release -destination 'generic/platform=iOS' \
  -archivePath "$ARCHIVE" -allowProvisioningUpdates archive CURRENT_PROJECT_VERSION="$BUILD_NUMBER" DEVELOPMENT_TEAM="$TEAM_ID"
cat > build/testflight/ExportOptions.plist <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>method</key><string>app-store-connect</string>
  <key>destination</key><string>upload</string>
  <key>teamID</key><string>$TEAM_ID</string>
  <key>signingStyle</key><string>automatic</string>
  <key>manageAppVersionAndBuildNumber</key><false/>
</dict></plist>
PLIST
xcodebuild -exportArchive -archivePath "$ARCHIVE" -exportOptionsPlist build/testflight/ExportOptions.plist \
  -exportPath build/testflight/export -allowProvisioningUpdates
echo "已上传 build $BUILD_NUMBER，App Store Connect 处理完后会出现在 TestFlight。"
