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
