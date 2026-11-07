#!/usr/bin/env bash
# WiFi keeper: stay on the best saved WiFi that really reaches the internet.
# Run this ON the node Pi, from the repo folder:
#   bash install_wifi_keeper.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")" && pwd)"
UNIT=node-wifi.service

if ! command -v nmcli >/dev/null && ! command -v wpa_cli >/dev/null; then
  echo "Found neither nmcli (NetworkManager) nor wpa_cli (wpa_supplicant)."
  exit 1
fi

sudo tee /etc/systemd/system/$UNIT >/dev/null <<UNITFILE
[Unit]
Description=Node WiFi keeper (stays on the best saved WiFi that reaches the internet)
After=network.target NetworkManager.service wpa_supplicant.service

[Service]
Type=simple
# Root so it may switch networks. It only uses networks that are already
# saved and never reads or prints passwords.
User=root
WorkingDirectory=$REPO_DIR
EnvironmentFile=-/etc/node-wifi.env
Environment=PYTHONUNBUFFERED=1
ExecStart=/usr/bin/python3 $REPO_DIR/wifi_keeper.py
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
UNITFILE

sudo systemctl daemon-reload
sudo systemctl enable --now $UNIT
sleep 2
sudo systemctl --no-pager status $UNIT | head -n 8
echo
sudo python3 "$REPO_DIR/wifi_keeper.py" --status
echo
echo "Done. Watch it with: journalctl -u node-wifi -f"
