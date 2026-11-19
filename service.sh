#!/bin/zsh
# Manage the always-on launchd service.  Usage: ./service.sh install | uninstall | status | logs
set -euo pipefail
DIR="$(cd "$(dirname "$0")" && pwd)"
LABEL="local.jobhunt.agents"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"
PY="$(command -v python3)"

case "${1:-}" in
  install)
    mkdir -p "$DIR/logs" "$HOME/Library/LaunchAgents"
    cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>$LABEL</string>
  <key>ProgramArguments</key><array>
    <string>$PY</string><string>$DIR/orchestrator.py</string>
  </array>
  <key>WorkingDirectory</key><string>$DIR</string>
  <key>EnvironmentVariables</key><dict>
    <key>PATH</key><string>$HOME/.local/bin:/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string>
    <key>JOBHUNT_INTERVAL_MIN</key><string>${JOBHUNT_INTERVAL_MIN:-60}</string>
  </dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>ThrottleInterval</key><integer>300</integer>
  <key>StandardOutPath</key><string>$DIR/logs/launchd.out.log</string>
  <key>StandardErrorPath</key><string>$DIR/logs/launchd.err.log</string>
</dict></plist>
EOF
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    launchctl bootstrap "gui/$(id -u)" "$PLIST"
    echo "Installed and started $LABEL. Watch: ./service.sh logs" ;;
  uninstall)
    launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
    rm -f "$PLIST"; echo "Stopped and removed $LABEL" ;;
  status)
    launchctl print "gui/$(id -u)/$LABEL" 2>/dev/null | grep -E "state|pid|last exit" || echo "not running"
    cat "$DIR/state.json" 2>/dev/null || true ;;
  logs)
    tail -f "$DIR/logs/orchestrator.log" "$DIR/logs/launchd.err.log" ;;
  *) echo "usage: $0 install|uninstall|status|logs"; exit 1 ;;
esac
