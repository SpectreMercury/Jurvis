#!/usr/bin/env bash
# Keeps the engine running without the Mac app: started at login, restarted if it stops.
#   ./scripts/install-service.sh           install and start
#   ./scripts/install-service.sh status    is it installed and running
#   ./scripts/install-service.sh remove    stop and uninstall
#   ./scripts/install-service.sh print     show the launchd file without installing it
set -euo pipefail
JURVIS_PROJECT_ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LABEL=app.jurvis.engine
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
DOMAIN="gui/$(id -u)"
PORT="$(grep -E '^JURVIS_PORT=' "$JURVIS_PROJECT_ROOT/.env" 2>/dev/null | tail -1 | cut -d= -f2 | tr -d '"'"'"' ' || true)"
PORT="${PORT:-18777}"

plist() {
  # launchd starts with a bare PATH: ffmpeg, uv and tailscale live where Homebrew puts them.
  cat <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array><string>$JURVIS_PROJECT_ROOT/scripts/start-engine.sh</string></array>
  <key>WorkingDirectory</key><string>$JURVIS_PROJECT_ROOT</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>15</integer>
  <key>ProcessType</key><string>Interactive</string>
  <key>StandardOutPath</key><string>$JURVIS_PROJECT_ROOT/data/logs/service.log</string>
  <key>StandardErrorPath</key><string>$JURVIS_PROJECT_ROOT/data/logs/service.log</string>
</dict></plist>
PLIST
}

loaded() { launchctl print "$DOMAIN/$LABEL" >/dev/null 2>&1; }

case "${1:-install}" in
  print)
    plist ;;
  status)
    if loaded; then
      launchctl print "$DOMAIN/$LABEL" | grep -E '^\s*(state|pid|last exit code) =' || true
    else
      echo '没有安装。运行 ./scripts/install-service.sh 安装。'
    fi ;;
  remove)
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"
    echo '已移除。引擎不再开机自启；已经在运行的那个也停了。' ;;
  install)
    [[ -x "$JURVIS_PROJECT_ROOT/.venv/bin/python" ]] || { echo '请先运行 ./scripts/bootstrap.sh'; exit 1; }
    if ! loaded && lsof -iTCP:"$PORT" -sTCP:LISTEN -n -P >/dev/null 2>&1; then
      echo "端口 $PORT 上已经有一个引擎在运行（多半是手动启动的，或 Mac App 拉起的）。先把它停掉再安装，否则两个会抢同一个端口。"
      exit 1
    fi
    mkdir -p "$HOME/Library/LaunchAgents" "$JURVIS_PROJECT_ROOT/data/logs"
    launchctl bootout "$DOMAIN/$LABEL" 2>/dev/null || true
    plist > "$PLIST"
    launchctl bootstrap "$DOMAIN" "$PLIST"
    echo "已安装：登录后引擎自动启动，停了会在 15 秒内重新拉起。"
    echo "日志：$JURVIS_PROJECT_ROOT/data/logs/service.log 和 engine.log"
    echo "更新代码后重启它：launchctl kickstart -k $DOMAIN/$LABEL" ;;
  *)
    echo '用法：install-service.sh [install|status|remove|print]'; exit 2 ;;
esac
