#!/bin/bash
set -euo pipefail

APP_DIR="/home/pi/Lightning-Node"
ENV_FILE="/home/pi/.bluesky_env"
LOG_FILE="$APP_DIR/lightning_bluesky.log"

cd "$APP_DIR"

# Load Bluesky creds if present
if [ -f "$ENV_FILE" ]; then
  set -a
  source "$ENV_FILE"
  set +a
fi

# Always use the venv (where smbus works)
source "$APP_DIR/.venv/bin/activate"

exec /home/pi/Lightning-Node/.venv/bin/python3 /home/pi/Lightning-Node/lightning_bluesky.py >> "$LOG_FILE" 2>&1

