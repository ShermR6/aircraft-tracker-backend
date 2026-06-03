#!/usr/bin/env bash
# FinalPing Ground Station Setup
# Usage: curl -sSL https://raw.githubusercontent.com/ShermR6/aircraft-tracker-backend/main/setup.sh | sudo bash -s -- "YOUR_TOKEN"

set -e

TOKEN="$1"

if [ -z "$TOKEN" ]; then
  echo "[ERR] No token provided."
  echo "Usage: curl -sSL .../setup.sh | sudo bash -s -- \"YOUR_TOKEN\""
  exit 1
fi

INSTALL_DIR="/home/pi/finalping-ground"
SERVICE_FILE="/etc/systemd/system/finalping-ground.service"
SCRIPT_URL="https://raw.githubusercontent.com/ShermR6/aircraft-tracker-backend/main/finalping_ground.py"

echo "=============================="
echo " FinalPing Ground Station"
echo "=============================="

# Install dir
mkdir -p "$INSTALL_DIR"
echo "[1/5] Created $INSTALL_DIR"

# Download ground station script
echo "[2/5] Downloading finalping_ground.py..."
curl -sSL "$SCRIPT_URL" -o "$INSTALL_DIR/finalping_ground.py"
chmod +x "$INSTALL_DIR/finalping_ground.py"

# Write config
echo "[3/5] Writing config..."
cat > "$INSTALL_DIR/config.json" <<EOF
{"token": "$TOKEN"}
EOF

# Set ownership (in case running as sudo)
if id "pi" &>/dev/null; then
  chown -R pi:pi "$INSTALL_DIR"
fi

# Install Python dependency
echo "[4/5] Installing dependencies..."
apt-get update -qq
apt-get install -y python3-requests

# Install systemd service
echo "[5/5] Installing service..."
cat > "$SERVICE_FILE" <<EOF
[Unit]
Description=FinalPing Ground Station
After=network.target dump1090-fa.service

[Service]
Type=simple
User=pi
WorkingDirectory=$INSTALL_DIR
ExecStart=/usr/bin/python3 $INSTALL_DIR/finalping_ground.py
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
echo "=============================="
echo " Done! Ground station running."
echo " Check status: sudo systemctl status finalping-ground"
echo " View logs:    sudo journalctl -u finalping-ground -f"
echo "=============================="
