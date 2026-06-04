#!/bin/bash
# FinalPing Ground Station Boot Script
# Runs on every boot — self-updates all scripts, then starts portal or tracker.

DIR="/home/pi/finalping-ground"
CONFIG="$DIR/config.json"
BASE_URL="https://raw.githubusercontent.com/ShermR6/aircraft-tracker-backend/main"

# Self-update all scripts — if this boot script itself updates, re-exec the new version
_updated=0
for script in finalping_boot.sh finalping_ground.py setup_portal.py; do
  if curl -fsSL --max-time 10 "$BASE_URL/$script" -o "$DIR/$script.tmp" 2>/dev/null; then
    if ! cmp -s "$DIR/$script.tmp" "$DIR/$script" 2>/dev/null; then
      mv "$DIR/$script.tmp" "$DIR/$script"
      [ "$script" = "finalping_boot.sh" ] && _updated=1
    else
      rm -f "$DIR/$script.tmp"
    fi
  else
    rm -f "$DIR/$script.tmp"
  fi
done

# If boot script itself was updated, re-exec the new version and exit
if [ "$_updated" = "1" ]; then
  chmod +x "$DIR/finalping_boot.sh"
  exec /bin/bash "$DIR/finalping_boot.sh"
  exit 0
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
