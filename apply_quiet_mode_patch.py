from pathlib import Path

path = Path("/home/pi/Lightning-Node/lightning_bluesky.py")
text = path.read_text()

anchor = 'SUMMARY_BIN_SIZE = 5 * 60     # 5-minute bins\n'
insert = '''
# ---------------- Notification / quiet-mode controls ----------------
# Keep collecting strikes, but suppress repetitive outbound posts.
ALERT_MIN_INTERVAL = 15 * 60      # minimum seconds between lightning alert posts
QUIET_TRIGGER_COUNT = 5           # if this many strikes happen within QUIET_WINDOW...
QUIET_WINDOW = 2 * 60             # ...inside this many seconds...
QUIET_DURATION = 30 * 60          # ...enter quiet mode for this long

LAST_ALERT_AT = 0.0
QUIET_UNTIL = 0.0

def alerts_quiet_now() -> bool:
    return time.time() < QUIET_UNTIL

def recent_strike_count(window_s: int = QUIET_WINDOW) -> int:
    now = time.time()
    return sum(1 for (t, _, _) in STRIKE_HISTORY if now - t <= window_s)

def should_send_lightning_alert() -> bool:
    """Decide whether to send an outbound lightning alert.

    Rules:
    - Always keep recording strikes locally.
    - If too many recent strikes occur, enter quiet mode.
    - While quiet mode is active, suppress outbound alerts.
    - Enforce a minimum interval between outbound alerts.
    """
    global LAST_ALERT_AT, QUIET_UNTIL

    now = time.time()

    if recent_strike_count() >= QUIET_TRIGGER_COUNT:
        if now >= QUIET_UNTIL:
            QUIET_UNTIL = now + QUIET_DURATION
            send_line(
                f"🟡 Quiet mode entered for {QUIET_DURATION // 60} min "
                f"(burst: {recent_strike_count()} strikes in {QUIET_WINDOW} s)."
            )
        return False

    if now < QUIET_UNTIL:
        return False

    if (now - LAST_ALERT_AT) < ALERT_MIN_INTERVAL:
        return False

    LAST_ALERT_AT = now
    return True

'''

old = """            elif reason == 8:
                # Lightning
                dist = sensor.get_distance()
                energy = sensor.get_energy()
                record_strike(dist, energy)
                icon = current_status_icon()
                send_tweet(
                    f\"{icon} Lightning detected! Energy: {energy} — distance: {dist} km ({dist*0.621371:.1f} mi) | \"
                    f\"Éclair détecté ! Puissance : {energy} — distance : {dist} km ({dist*0.621371:.1f} mi)\"
                )
"""

new = """            elif reason == 8:
                # Lightning
                dist = sensor.get_distance()
                energy = sensor.get_energy()
                record_strike(dist, energy)
                icon = current_status_icon()

                if should_send_lightning_alert():
                    send_tweet(
                        f\"{icon} Lightning detected! Energy: {energy} — distance: {dist} km ({dist*0.621371:.1f} mi) | \"
                        f\"Éclair détecté ! Puissance : {energy} — distance : {dist} km ({dist*0.621371:.1f} mi)\"
                    )
                else:
                    send_line(
                        f\"{icon} Lightning (logged, suppressed): Energy={energy}, distance={dist} km\"
                    )
"""

if anchor not in text:
    raise SystemExit("Anchor not found")

if old not in text:
    raise SystemExit("Lightning block not found")

text = text.replace(anchor, anchor + insert, 1)
text = text.replace(old, new, 1)
path.write_text(text)
print("Patched successfully")
