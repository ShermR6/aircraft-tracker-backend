#!/bin/bash
# FinalPing Ground Station Boot Script
# Runs on every boot — starts the setup portal if not configured, otherwise starts the tracker.

DIR="/home/pi/finalping-ground"
CONFIG="$DIR/config.json"
BASE_URL="https://raw.githubusercontent.com/ShermR6/aircraft-tracker-backend/main"

# Self-update: pull latest scripts silently on every boot
if curl -fsSL --max-time 10 "$BASE_URL/finalping_ground.py" -o "$DIR/finalping_ground.py.tmp" 2>/dev/null; then
  mv "$DIR/finalping_ground.py.tmp" "$DIR/finalping_ground.py"
fi
if curl -fsSL --max-time 10 "$BASE_URL/setup_portal.py" -o "$DIR/setup_portal.py.tmp" 2>/dev/null; then
  mv "$DIR/setup_portal.py.tmp" "$DIR/setup_portal.py"
fi

is_configured() {
    python3 - <<'EOF'
import json, sys
try:
    d = json.load(open("/home/pi/finalping-ground/config.json"))
    # Has a valid token — ready to run
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
