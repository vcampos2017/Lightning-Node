#!/usr/bin/env bash
# Restart the lightning service and report its state.
# (The AS3935 library patch from fix_as3935_lib.sh is no longer needed;
#  lightning_bluesky.py includes the fix.)
set -e
sudo systemctl restart lightning-bluesky.service
systemctl is-active lightning-bluesky.service
