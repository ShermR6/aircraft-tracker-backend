#!/bin/bash
# FinalPing Ground Station Boot Script
# Runs on every boot — starts the setup portal if not configured, otherwise starts the tracker.

DIR="/home/pi/finalping-ground"
CONFIG="$DIR/config.json"

is_configured() {
    python3 - <<'EOF'
import json, sys
try:
    d = json.load(open("/home/pi/finalping-ground/config.json"))
    sys.exit(0 if d.get("token") else 1)
except:
    sys.exit(1)
EOF
}

if is_configured; then
    echo "$(date '+%H:%M:%S') [OK] Config found — starting tracker"
    exec python3 "$DIR/finalping_ground.py"
else
    echo "$(date '+%H:%M:%S') [INFO] No config — starting setup portal"
    exec python3 "$DIR/setup_portal.py"
fi
