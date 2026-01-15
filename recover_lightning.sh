#!/usr/bin/env bash
set -e
/home/pi/Lightning-Node/fix_as3935_lib.sh
sudo systemctl restart lightning-bluesky.service
systemctl is-active lightning-bluesky.service
