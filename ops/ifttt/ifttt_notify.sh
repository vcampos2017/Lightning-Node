#!/bin/bash
set -euo pipefail

# IFTTT notifier (one-shot)
# Usage: ifttt_notify.sh <event_name> <message> [value2] [value3]

EVENT="${1:-}"
MSG="${2:-}"
VALUE2="${3:-$(hostname)}"
VALUE3="${4:-$(date -Is)}"

if [ -z "$EVENT" ] || [ -z "$MSG" ]; then
  echo "Usage: $0 <event_name> <message> [value2] [value3]" >&2
  exit 2
fi

# Load IFTTT key
if [ -f /etc/lightning/ifttt.env ]; then
  # shellcheck disable=SC1091
  source /etc/lightning/ifttt.env
fi

if [ -z "${IFTTT_KEY:-}" ]; then
  echo "ERROR: IFTTT_KEY not set in /etc/lightning/ifttt.env" >&2
  exit 3
fi

URL="https://maker.ifttt.com/trigger/${EVENT}/with/key/${IFTTT_KEY}"

esc() { sed 's/"/\\"/g'; }

PAYLOAD=$(printf '{"value1":"%s","value2":"%s","value3":"%s"}' \
  "$(printf "%s" "$MSG"    | esc)" \
  "$(printf "%s" "$VALUE2" | esc)" \
  "$(printf "%s" "$VALUE3" | esc)")

curl -fsS -X POST -H "Content-Type: application/json" -d "$PAYLOAD" "$URL" >/dev/null
