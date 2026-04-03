#!/bin/bash
# Install macOS launchd agents for calendar sync.
# Installs both corporate (sync_calendar.py) and iCloud (sync_icloud.py) agents.
# Usage: ./install-launchd.sh [interval_seconds]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
INTERVAL="${1:-900}"
PYTHON="$(command -v python3)"

if [ -z "$PYTHON" ]; then
    echo "Error: python3 not found on PATH" >&2
    exit 1
fi

install_agent() {
    local label="$1"
    local script="$2"
    local log_prefix="$3"
    local plist="$HOME/Library/LaunchAgents/${label}.plist"

    if [ ! -f "$SCRIPT_DIR/$script" ]; then
        echo "Skipping $script (not found)"
        return
    fi

    # Unload existing agent if present
    if launchctl list "$label" &>/dev/null; then
        echo "Unloading existing $label..."
        launchctl unload "$plist" 2>/dev/null || true
    fi

    cat > "$plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${label}</string>
    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPT_DIR}/${script}</string>
    </array>
    <key>StartInterval</key>
    <integer>${INTERVAL}</integer>
    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>
    <key>StandardOutPath</key>
    <string>${SCRIPT_DIR}/${log_prefix}-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${SCRIPT_DIR}/${log_prefix}-stderr.log</string>
    <key>RunAtLoad</key>
    <true/>
</dict>
</plist>
EOF

    launchctl load "$plist"

    echo "Installed and loaded $label"
    echo "  Plist:    $plist"
    echo "  Script:   $SCRIPT_DIR/$script"
}

# Install both agents
install_agent "com.simonellistonball.calendar-sync" "sync_calendar.py" "launchd"
echo ""
install_agent "com.simonellistonball.icloud-sync" "sync_icloud.py" "launchd-icloud"

echo ""
echo "Interval: every ${INTERVAL}s"
echo "Python:   $PYTHON"
echo ""
echo "Commands:"
echo "  launchctl list | grep simonellistonball    # check status"
echo "  launchctl unload ~/Library/LaunchAgents/com.simonellistonball.*.plist  # stop all"
