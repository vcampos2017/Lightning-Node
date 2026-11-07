#!/usr/bin/env bash
# WiFi keeper: stay on the best saved WiFi that really reaches the internet,
# restart the Pi as a last resort, and turn on the hardware watchdog.
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

# Hardware watchdog: if the whole Pi freezes (nothing runs, not even this
# keeper), the Pi's own watchdog chip restarts it after about 15 seconds.
# Skip with: NODE_WATCHDOG=0 bash install_wifi_keeper.sh
if grep -qs '^RuntimeWatchdogSec' $(ls /etc/systemd/system.conf.d/*.conf 2>/dev/null | grep -v node-watchdog.conf) /dev/null; then
  echo "Hardware watchdog is already on."
elif [ "${NODE_WATCHDOG:-1}" != "0" ] && [ -e /dev/watchdog ]; then
  sudo mkdir -p /etc/systemd/system.conf.d
  sudo tee /etc/systemd/system.conf.d/node-watchdog.conf >/dev/null <<'WD'
[Manager]
RuntimeWatchdogSec=15s
RebootWatchdogSec=2min
WD
  sudo systemctl daemon-reexec
  echo "Hardware watchdog on: a frozen Pi restarts itself."
elif [ "${NODE_WATCHDOG:-1}" != "0" ]; then
  echo "No /dev/watchdog on this Pi, so no hardware watchdog (the WiFi keeper still runs)."
fi

# Keep the system log on the SD card, so what happened before a restart
# is still there afterwards (journalctl -b -1).
if [ ! -d /var/log/journal ]; then
  sudo mkdir -p /var/log/journal
  sudo systemctl restart systemd-journald
  echo "System log now kept across restarts."
fi

sudo systemctl daemon-reload
sudo systemctl enable --now $UNIT
sudo systemctl restart $UNIT    # pick up a newer wifi_keeper.py on a re-run
sleep 2
sudo systemctl --no-pager status $UNIT | head -n 8
echo
sudo python3 "$REPO_DIR/wifi_keeper.py" --status
echo
echo "Done. Watch it with: journalctl -u node-wifi -f"
