#!/usr/bin/env bash
# FinalPing Ground Station Setup
# Usage: curl -sSL https://raw.githubusercontent.com/ShermR6/aircraft-tracker-backend/main/setup.sh | sudo bash -s -- "YOUR_TOKEN"
# Omit token to install in hotspot-portal mode (for pre-flashed SD cards).

set -e

TOKEN="$1"
BASE_URL="https://raw.githubusercontent.com/ShermR6/aircraft-tracker-backend/main"
INSTALL_DIR="/home/pi/finalping-ground"
SERVICE_FILE="/etc/systemd/system/finalping-ground.service"

echo "================================"
echo " FinalPing Ground Station"
echo "================================"

# Install dir
mkdir -p "$INSTALL_DIR"
echo "[1/6] Created $INSTALL_DIR"

# Download scripts
echo "[2/6] Downloading scripts..."
curl -sSL "$BASE_URL/finalping_ground.py" -o "$INSTALL_DIR/finalping_ground.py"
curl -sSL "$BASE_URL/setup_portal.py"    -o "$INSTALL_DIR/setup_portal.py"
curl -sSL "$BASE_URL/finalping_boot.sh"  -o "$INSTALL_DIR/finalping_boot.sh"
chmod +x "$INSTALL_DIR/finalping_boot.sh"

# Write config if token provided
if [ -n "$TOKEN" ]; then
  echo "[3/6] Writing config..."
  cat > "$INSTALL_DIR/config.json" <<EOF
{"token": "$TOKEN"}
EOF
else
  echo "[3/6] No token provided — will use setup portal on first boot"
  rm -f "$INSTALL_DIR/config.json"
fi

# Set ownership
if id "pi" &>/dev/null; then
  chown -R pi:pi "$INSTALL_DIR"
fi

# Install dependencies
echo "[4/6] Installing dependencies..."
apt-get update -qq
apt-get install -y python3-requests hostapd dnsmasq

# Disable hostapd/dnsmasq auto-start (only used by portal when needed)
systemctl disable hostapd 2>/dev/null || true
systemctl disable dnsmasq 2>/dev/null || true
systemctl stop hostapd   2>/dev/null || true

# Install adsb.lol feeder using their official installer
echo "[4b] Installing adsb.lol feeder..."
curl -L -o /tmp/lol-feed.sh https://adsb.lol/feed.sh 2>/dev/null && bash /tmp/lol-feed.sh || true

# Install hourly auto-updater
cat > /usr/local/bin/finalping-update.sh <<'UPDATESCRIPT'
#!/bin/bash
DIR="/home/pi/finalping-ground"
BASE_URL="https://raw.githubusercontent.com/ShermR6/aircraft-tracker-backend/main"
CHANGED=0

for script in finalping_boot.sh finalping_ground.py setup_portal.py; do
  if curl -fsSL --max-time 15 "$BASE_URL/$script" -o "$DIR/$script.tmp" 2>/dev/null; then
    if ! cmp -s "$DIR/$script.tmp" "$DIR/$script" 2>/dev/null; then
      mv "$DIR/$script.tmp" "$DIR/$script"
      chmod +x "$DIR/$script"
      echo "$(date '+%H:%M:%S') [UPDATE] $script updated"
      CHANGED=1
    else
      rm -f "$DIR/$script.tmp"
    fi
  else
    rm -f "$DIR/$script.tmp"
  fi
done

if [ "$CHANGED" = "1" ]; then
  echo "$(date '+%H:%M:%S') [UPDATE] Restarting ground station..."
  systemctl restart finalping-ground
fi
UPDATESCRIPT
chmod +x /usr/local/bin/finalping-update.sh

cat > /etc/systemd/system/finalping-update.service <<EOF
[Unit]
Description=FinalPing Ground Station Updater

[Service]
Type=oneshot
ExecStart=/usr/local/bin/finalping-update.sh
StandardOutput=journal
StandardError=journal
EOF

cat > /etc/systemd/system/finalping-update.timer <<EOF
[Unit]
Description=FinalPing Ground Station Hourly Update Check

[Timer]
OnBootSec=2min
OnUnitActiveSec=1h
Unit=finalping-update.service

[Install]
WantedBy=timers.target
EOF

systemctl daemon-reload
systemctl enable finalping-update.timer
systemctl start finalping-update.timer

# Install systemd service
echo "[5/6] Installing service..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=FinalPing Ground Station
After=network.target dump1090-fa.service

[Service]
Type=simple
User=root
WorkingDirectory=$INSTALL_DIR
ExecStart=/bin/bash $INSTALL_DIR/finalping_boot.sh
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable finalping-ground
systemctl restart finalping-ground

echo ""
echo "================================"
if [ -n "$TOKEN" ]; then
  echo " Done! Ground station running."
else
  echo " Done! On next boot, connect to"
  echo " WiFi: FinalPing_Setup"
  echo " Then open http://192.168.4.1"
fi
echo " Logs: sudo journalctl -u finalping-ground -f"
echo "================================"
