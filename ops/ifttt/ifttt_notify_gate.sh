#!/usr/bin/env bash
#
# ifttt_notify_gate.sh
# Version: 1.0.0
#
# Purpose:
#   Gate and rate-limit IFTTT webhook notifications for the
#   lightning-bluesky systemd service.
#
#   This script:
#   - Suppresses notification spam during restart storms
#   - Separates status vs failure alerts
#   - Emits a single "lightning_recovered" event after a failure
#   - Suppresses redundant "lightning_started" messages after recovery
#
# Expected events:
#   lightning_started     (status)
#   lightning_stopped     (status)
#   lightning_failed      (failure)
#   lightning_recovered   (recovery)
#
# State directory:
#   /var/lib/ifttt_notify/
#   Used to persist timestamps across restarts.
#
# Tuning knobs (seconds):
#   COOLDOWN_STRIKE   – max 1 strike alert per minute
#   COOLDOWN_STATUS  – debounce rapid status transitions
#   COOLDOWN_FAILED  – max 1 failure alert per 15 minutes
#   RECOVERY_WINDOW  – if service starts within this window
#                      after a failure, emit "recovered"
#
# Changelog:
#   1.0.0 – Initial stable release with cooldown gating and recovery detection
#
set -euo pipefail

EVENT="${1:-}"
MSG="${2:-}"
HIGH_PRIORITY="${3:-false}"

STATE_DIR="/var/lib/ifttt_notify"
mkdir -p "$STATE_DIR"
now="$(date +%s)"

# ---- Tuning knobs (seconds) ----
COOLDOWN_STRIKE=60        # at most 1 strike-ish alert per minute
COOLDOWN_STATUS=10        # protect against accidental double-fires
COOLDOWN_FAILED=900       # at most 1 failure alert per 15 minutes
RECOVERY_WINDOW=1800      # if failure happened within 30 min, treat next start as recovery

failed_stamp="$STATE_DIR/last_failed.ts"
last_sent_dir="$STATE_DIR/last_sent"
mkdir -p "$last_sent_dir"

send_ifttt() {
  /usr/local/bin/ifttt_notify.sh "$EVENT" "$MSG" "$HIGH_PRIORITY"
}

rate_limit_or_exit() {
  local key="$1"
  local cooldown="$2"
  local stamp="$last_sent_dir/${key}.ts"

  if [[ -f "$stamp" ]]; then
    local last
    last="$(cat "$stamp" 2>/dev/null || echo 0)"
    local delta=$(( now - last ))
    if (( delta < cooldown )); then
      exit 0
    fi
  fi

  echo "$now" > "$stamp"
}

maybe_send_recovered() {
  # Called on "lightning_started"
  if [[ -f "$failed_stamp" ]]; then
    local last_failed
    last_failed="$(cat "$failed_stamp" 2>/dev/null || echo 0)"
    local delta=$(( now - last_failed ))

    if (( delta <= RECOVERY_WINDOW )); then
      EVENT="lightning_recovered"
      MSG="Lightning Bluesky service recovered after failure"
      HIGH_PRIORITY="true"
      rm -f "$failed_stamp"

      # Ultra-clean signal: do NOT also emit lightning_started
      rate_limit_or_exit "recovered" "$COOLDOWN_STATUS"
      send_ifttt
      exit 0
    fi
  fi
}

case "$EVENT" in
  lightning_failed)
    # remember failure moment for recovery logic
    echo "$now" > "$failed_stamp"
    rate_limit_or_exit "failed" "$COOLDOWN_FAILED"
    send_ifttt
    ;;

  lightning_started)
    maybe_send_recovered
    rate_limit_or_exit "started" "$COOLDOWN_STATUS"
    send_ifttt
    ;;

  lightning_stopped)
    rate_limit_or_exit "stopped" "$COOLDOWN_STATUS"
    send_ifttt
    ;;

  lightning_strike|lightning_alert|strike)
    rate_limit_or_exit "strike" "$COOLDOWN_STRIKE"
    send_ifttt
    ;;

  *)
    # default: conservative status cooldown
    rate_limit_or_exit "misc" "$COOLDOWN_STATUS"
    send_ifttt
    ;;
esac
