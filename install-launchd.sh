#!/bin/bash
# Install a macOS launchd agent to run calendar sync every 15 minutes.
# Usage: ./install-launchd.sh [interval_seconds]

set -euo pipefail

LABEL="com.simonellistonball.calendar-sync"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INTERVAL="${1:-900}"
PYTHON="$(command -v python3)"

if [ -z "$PYTHON" ]; then
    echo "Error: python3 not found on PATH" >&2
    exit 1
fi

if [ ! -f "$SCRIPT_DIR/sync_calendar.py" ]; then
    echo "Error: sync_calendar.py not found in $SCRIPT_DIR" >&2
    exit 1
fi

# Unload existing agent if present
if launchctl list "$LABEL" &>/dev/null; then
    echo "Unloading existing $LABEL..."
    launchctl unload "$PLIST" 2>/dev/null || true
fi

cat > "$PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPT_DIR}/sync_calendar.py</string>
    </array>
    <key>StartInterval</key>
    <integer>${INTERVAL}</integer>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/launchd-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/launchd-stderr.log</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF

launchctl load "$PLIST"

echo "Installed and loaded $LABEL"
echo "  Plist:    $PLIST"
echo "  Interval: every ${INTERVAL}s"
echo "  Python:   $PYTHON"
echo "  Script:   $SCRIPT_DIR/sync_calendar.py"
echo ""
echo "Commands:"
echo "  launchctl list $LABEL          # check status"
echo "  launchctl unload \"$PLIST\"      # stop"
echo "  launchctl load \"$PLIST\"        # start"
