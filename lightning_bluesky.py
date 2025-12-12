#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# lightning_tweeter_bluesky_bilingual_FIXED.py
# Bilingual lightning alerts (EN/FR) with screen print, log file, and optional Bluesky posting.
# Now with CEU-style PASS node metadata, per-strike Bluesky posts, & post-storm summaries.

import os
import time
import threading
import configparser
from datetime import datetime
from pathlib import Path
from collections import deque
import os
import configparser
from error_handler import init_logging, handle_error, warn
import math
import json


from datetime import datetime, timedelta
# ...
# ---------- Bluesky rate limiter ----------

class BlueskyRateLimiter:
    """
    Prevents posting too frequently to Bluesky.

    - min_interval_seconds: minimum time between posts
    - max_per_hour: hard cap on posts in any rolling 60-minute window
    """

    def __init__(self, min_interval_seconds: int = 300, max_per_hour: int = 20):
        self.min_interval = timedelta(seconds=min_interval_seconds)
        self.max_per_hour = max_per_hour
        self.post_times: list[datetime] = []

    def can_post(self) -> tuple[bool, str | None]:
        now = datetime.utcnow()

        # Keep only posts from the last hour
        one_hour_ago = now - timedelta(hours=1)
        self.post_times = [t for t in self.post_times if t >= one_hour_ago]

        # Check minimum spacing between posts
        if self.post_times:
            since_last = now - self.post_times[-1]
            if since_last < self.min_interval:
                wait_seconds = int((self.min_interval - since_last).total_seconds())
                return (
                    False,
                    f"Rate limit: last Bluesky post was {int(since_last.total_seconds())}s ago; "
                    f"waiting another ~{wait_seconds}s before posting again.",
                )

        # Check hourly cap
        if len(self.post_times) >= self.max_per_hour:
            return (
                False,
                f"Rate limit: reached {self.max_per_hour} posts in the last hour. "
                "Skipping this lightning event to avoid spamming Bluesky.",
            )

        # Approved; record post time
        self.post_times.append(now)
        return True, None


# Global limiter instance for this process
RATE_LIMITER = BlueskyRateLimiter(
    min_interval_seconds=300,   # 5 minutes between posts
    max_per_hour=20             # safety cap
)

from error_handler import init_logging, handle_error, warn

# Initialize logging for the app
init_logging()

# ---------- Bluesky credentials loading ----------

CONFIG_PATH = os.path.expanduser("~/.bluesky_credentials.ini")

try:
    if not os.path.exists(CONFIG_PATH):
        raise FileNotFoundError(
            f"Credentials file not found at {CONFIG_PATH}."
        )

    config = configparser.ConfigParser()
    read_files = config.read(CONFIG_PATH)

    if not read_files:
        raise ValueError(
            f"Unable to read credentials file at {CONFIG_PATH}."
        )

    if "bluesky" not in config:
        raise KeyError(
            "Missing [bluesky] section in credentials file."
        )

    BLUESKY_HANDLE = config["bluesky"].get("handle", "").strip()
    BLUESKY_APP_PASSWORD = config["bluesky"].get("app_password", "").strip()

    if not BLUESKY_HANDLE or not BLUESKY_APP_PASSWORD:
        raise ValueError(
            "Both 'handle' and 'app_password' must be set in the [bluesky] section."
        )

except Exception as e:
    handle_error(
        e,
        context=f"loading Bluesky credentials from {CONFIG_PATH}",
        fatal=True,
    )

# ---------- CEU / PASS Node Identity ----------
NODE_ID = "PASS-LN-01"                    # Lightning Node ID
NODE_REGION = "Greater Harmony Hills"     # Human-readable region
NODE_CHANNEL = "Atmospheric Telemetry"    # “subchannel” label
# ---------- Startup banner ----------

def print_startup_banner() -> None:
    """Print a one-time startup summary so we know what node is running."""
    print("\n================ LightningSensorBluesky ================")
    print(f" Node ID     : {NODE_ID}")
    print(f" Region      : {NODE_REGION}")
    print(f" Channel     : {NODE_CHANNEL}")
    print(f" Bluesky     : {BLUESKY_HANDLE}")
    print(" Log file    : lightning_bluesky.log")
    print(" Status      : Starting up and listening for lightning...")
    print("=======================================================\n")

# Status icons for fun + quick scanning
STATUS_IDLE = "🟢"
STATUS_MONITORING = "🟡"
STATUS_STORM = "🔴"

# ---------- Optional charting ----------
try:
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    HAVE_MATPLOTLIB = True
except Exception as e:
    HAVE_MATPLOTLIB = False
    print(f"[Warning] matplotlib not available: {e}; charts will not be generated.")

import RPi.GPIO as GPIO
from atproto import Client, models
from RPi_AS3935.RPi_AS3935 import RPi_AS3935

# ---------- Log files ----------
SCRIPT_DIR = Path(__file__).resolve().parent
LOG_FILE = SCRIPT_DIR / 'lightning_alerts.log'
JSON_LOG_FILE = SCRIPT_DIR / 'lightning_telemetry.jsonl'


def send_line(line: str):
    """Print + append to text log."""
    print(line)
    try:
        with LOG_FILE.open('a', encoding='utf-8') as fp:
            fp.write(line + '\n')
    except Exception as e:
        print(f"[Log error] {e}")


def log_json(event: dict):
    """Append structured JSON telemetry (one JSON object per line)."""
    try:
        event["ts_iso"] = datetime.utcnow().isoformat() + "Z"
        with JSON_LOG_FILE.open('a', encoding='utf-8') as fp:
            fp.write(json.dumps(event) + '\n')
    except Exception as e:
        print(f"[JSON log error] {e}")


def def post_bluesky(text, image_path: str = None):
    """
    Post a message to Bluesky using atproto Client.
    If image_path is provided, attach the PNG chart as an embedded image.

    Now includes:
    - Credential checks with clear warnings
    - Rate limiting so storms don't spam Bluesky
    - Centralized error handling via error_handler.py
    """
    handle = BLUESKY_HANDLE
    app_pw = BLUESKY_APP_PASSWORD

    # 1) Check credentials first
    if not (handle and app_pw):
        warn(
            "Missing BLUESKY_HANDLE or BLUESKY_APP_PASSWORD – skipping Bluesky post.",
            context="Bluesky credentials",
        )
        return

    # 2) Rate limiting
    allowed, reason = RATE_LIMITER.can_post()
    if not allowed:
        # Soft failure: we just skip this post and log/print the reason
        warn(reason, context="Bluesky rate limit")
        return

    # 3) Attempt the post
    try:
        client = Client()
        client.login(handle, app_pw)

        if image_path:
            # Read image bytes
            try:
                with open(image_path, "rb") as f:
                    img_bytes = f.read()
            except FileNotFoundError as e:
                # More specific context if the image is missing
                handle_error(
                    e,
                    context=f"opening image file for Bluesky post: {image_path}",
                )
                return

            # Upload as blob
            blob_output = client.upload_blob(img_bytes)

            # Build image embed
            embed = models.AppBskyEmbedImages.Main(
                images=[
                    models.AppBskyEmbedImages.Image(
                        alt="Lightning storm summary chart",
                        image=blob_output.blob,
                    )
                ]
            )

            client.send_post(text=text, embed=embed)
            print(f"[Bluesky] Posted with image: {image_path}")
        else:
            client.send_post(text=text)
            print("[Bluesky] Posted (text-only).")

    except Exception as e:
        # Non-fatal: log nicely but don't crash the whole lightning loop
        handle_error(e, context="posting to Bluesky")

def send_tweet(message: str):
    """
    Wrap a message with timestamp + send to text log + async Bluesky.
    Used for high-level summaries AND per-strike notifications.
    """
    ts = f"{datetime.now():%Y-%m-%d %H:%M:%S} — {message}"
    send_line(ts)
    threading.Thread(target=post_bluesky, args=(ts,), daemon=True).start()


# ---------------- Sensor Setup ----------------
GPIO.setmode(GPIO.BCM)
pin = 17  # IRQ pin

sensor = RPi_AS3935(address=0x03, bus=1)
sensor.set_indoors(False)
sensor.set_noise_floor(1)
sensor.calibrate(tun_cap=0x01)
sensor.set_min_strikes(5)


# ---------------- Storm Tracking ----------------
STRIKE_HISTORY = deque(maxlen=2000)  # (timestamp, distance_km, energy)

STORM_ACTIVE = False
STORM_START = None
STORM_END = None
LAST_SUMMARY_POSTED = None

STORM_MIN_STRIKES = 3
STORM_WINDOW = 10 * 60        # 10 min
STORM_GAP_TO_END = 20 * 60    # 20 min quiet -> end
SUMMARY_DELAY = 60 * 60       # 1 hour after last strike

SUMMARY_BIN_SIZE = 5 * 60     # 5-minute bins


def current_status_icon() -> str:
    """Return a status icon based on current storm state."""
    if STORM_ACTIVE:
        return STATUS_STORM
    # If we’ve seen strikes recently but storm not formally active yet
    now = time.time()
    recent = [t for (t, _, _) in STRIKE_HISTORY if now - t <= STORM_WINDOW]
    if recent:
        return STATUS_MONITORING
    return STATUS_IDLE


def record_strike(distance_km: float, energy: int):
    """Record a strike into history for storm analytics."""
    now = time.time()
    STRIKE_HISTORY.append((now, distance_km, energy))


def _get_strikes_during(storm_start, storm_end):
    return [(t, d, e) for (t, d, e) in STRIKE_HISTORY if storm_start <= t <= storm_end]


def make_storm_chart(storm_start, storm_end):
    """Create a PNG chart of strikes per bin during the storm. Returns path or None."""
    if not HAVE_MATPLOTLIB:
        return None

    data = _get_strikes_during(storm_start, storm_end)
    if not data:
        return None

    duration = max(storm_end - storm_start, SUMMARY_BIN_SIZE)
    num_bins = max(1, math.ceil(duration / SUMMARY_BIN_SIZE))
    counts = [0] * num_bins

    for t, _, _ in data:
        idx = int((t - storm_start) // SUMMARY_BIN_SIZE)
        if idx >= num_bins:
            idx = num_bins - 1
        counts[idx] += 1

    xs = list(range(num_bins))
    fig, ax = plt.subplots(figsize=(8, 4))
    ax.plot(xs, counts, marker='o')
    ax.set_xticks(xs)

    labels = []
    for i in xs:
        start_min = int((i * SUMMARY_BIN_SIZE) / 60)
        end_min = int(((i + 1) * SUMMARY_BIN_SIZE) / 60)
        labels.append(f"{start_min}-{end_min}m")

    ax.set_xticklabels(labels, rotation=45, ha='right')
    ax.set_ylabel('Strikes / 5 min')
    ax.set_title('Lightning Activity During Storm')
    plt.tight_layout()

    chart_path = SCRIPT_DIR / 'storm_summary.png'
    fig.savefig(chart_path)
    plt.close(fig)
    return chart_path


def build_storm_summary(storm_start, storm_end):
    """Build a human-readable text summary of the storm session."""
    data = _get_strikes_during(storm_start, storm_end)
    if not data:
        return "Storm summary: no strikes recorded."

    duration_min = (storm_end - storm_start) / 60
    total = len(data)

    duration = max(storm_end - storm_start, SUMMARY_BIN_SIZE)
    num_bins = max(1, math.ceil(duration / SUMMARY_BIN_SIZE))
    bins = [0] * num_bins

    for t, _, _ in data:
        idx = int((t - storm_start) // SUMMARY_BIN_SIZE)
        if idx >= num_bins:
            idx = num_bins - 1
        bins[idx] += 1

    peak = max(bins)

    # CEU / PASS-flavored summary text, kept reasonably short for Bluesky
    header = (
        f"⚡ {NODE_ID} · {NODE_REGION}\n"
        f"Channel: {NODE_CHANNEL}\n"
    )

    body = (
        f"- Duration: {duration_min:.0f} minutes\n"
        f"- Total lightning strikes: {total}\n"
        f"- Peak: {peak} strikes / 5 min\n"
    )

    footer = "◦ Shardless atmospheric telemetry · CEU prototype"

    summary = header + body + footer
    return summary


def generate_and_post_storm_summary(storm_start, storm_end):
    """
    Generate chart + text summary and post the summary to Bluesky.
    If a chart PNG exists, include it as an embedded image.
    """
    chart_path = make_storm_chart(storm_start, storm_end)
    summary_text = build_storm_summary(storm_start, storm_end)

    if chart_path:
        # Timestamp + summary text
        ts = f"{datetime.now():%Y-%m-%d %H:%M:%S} — {summary_text}"

        # Log locally
        send_line(ts)
        send_line(f"{STATUS_IDLE} Storm summary chart saved: {chart_path}")

        # Post to Bluesky with the PNG embed in a background thread
        threading.Thread(
            target=post_bluesky,
            args=(ts, str(chart_path)),
            daemon=True,
        ).start()
    else:
        # Fallback: just do a text-only summary post
        send_tweet(summary_text)

    log_json({
        "event": "storm_summary_posted",
        "node_id": NODE_ID,
        "region": NODE_REGION,
        "unix_ts": time.time(),
        "storm_start": storm_start,
        "storm_end": storm_end,
        "chart_path": str(chart_path) if chart_path else None,
    })


def maybe_handle_storm_summary():
    """
    Check if a storm has ended and, if enough time has passed,
    generate/post a summary. Call this periodically from the main loop.
    """
    global STORM_ACTIVE, STORM_START, STORM_END, LAST_SUMMARY_POSTED
    now = time.time()

    # Detect storm end (quiet gap)
    if STORM_ACTIVE and STORM_END and (now - STORM_END >= STORM_GAP_TO_END):
        STORM_ACTIVE = False
        send_line(
            f"{STATUS_MONITORING} Storm session ended at "
            f"{datetime.fromtimestamp(STORM_END):%Y-%m-%d %H:%M:%S}"
        )
        log_json({
            "event": "storm_end",
            "node_id": NODE_ID,
            "region": NODE_REGION,
            "unix_ts": STORM_END,
        })
        LAST_SUMMARY_POSTED = None

    # After storm ended, wait SUMMARY_DELAY then post summary once
    if (not STORM_ACTIVE
        and STORM_START
        and STORM_END
        and LAST_SUMMARY_POSTED is None
        and now - STORM_END >= SUMMARY_DELAY):

        generate_and_post_storm_summary(STORM_START, STORM_END)
        LAST_SUMMARY_POSTED = now


# ---------------- Interrupt Handler ----------------
def handle_interrupt(channel):
    global STORM_ACTIVE, STORM_START, STORM_END
    current_timestamp = datetime.now()
    time.sleep(0.003)
    reason = sensor.get_interrupt()
    now = time.time()

    if reason == 0x01:
        send_line(f"{current_status_icon()} Noise level too high — raising noise floor.")
        sensor.raise_noise_floor()
        log_json({
            "event": "noise_floor_raise",
            "node_id": NODE_ID,
            "region": NODE_REGION,
            "unix_ts": now,
        })

    elif reason == 0x04:
        send_line(f"{current_status_icon()} Disturber detected — masking subsequent disturbers.")
        sensor.set_mask_disturber(True)
        log_json({
            "event": "disturber_masked",
            "node_id": NODE_ID,
            "region": NODE_REGION,
            "unix_ts": now,
        })

    elif reason == 0x08:
        # Get raw distance in km, convert to miles
        distance_km = sensor.get_distance()
        energy = sensor.get_energy()
        distance_mi = distance_km * 0.621371

        dist_km = round(distance_km, 1)
        dist_mi = round(distance_mi, 1)

        # Bilingual message with km + mi
        msg = (
            f"{current_status_icon()} Lightning detected! "
            f"Energy: {energy} — distance: {dist_km} km ({dist_mi} mi) "
            f"| Éclair détecté ! Puissance : {energy} — distance : {dist_km} km ({dist_mi} mi)"
        )

        # This will:
        #  - prepend a timestamp
        #  - print it
        #  - append to lightning_alerts.log
        #  - post to Bluesky via atproto (in a background thread)
        send_tweet(msg)

        # Keep kilometers as the canonical value for analytics
        record_strike(distance_km, energy)

        # Structured telemetry entry
        log_json({
            "event": "strike",
            "node_id": NODE_ID,
            "region": NODE_REGION,
            "distance_km": distance_km,
            "distance_mi": distance_mi,
            "energy": energy,
            "unix_ts": now,
        })

        # ---- Storm state machine (uses recent history) ----
        recent_times = [t for (t, _, _) in STRIKE_HISTORY if now - t <= STORM_WINDOW]

        if (not STORM_ACTIVE) and len(recent_times) >= STORM_MIN_STRIKES:
            STORM_ACTIVE = True
            STORM_START = recent_times[0]
            STORM_END = now
            send_line(
                f"{STATUS_STORM} Storm session started at "
                f"{datetime.fromtimestamp(STORM_START):%Y-%m-%d %H:%M:%S}"
            )
            log_json({
                "event": "storm_start",
                "node_id": NODE_ID,
                "region": NODE_REGION,
                "unix_ts": STORM_START,
            })

        elif STORM_ACTIVE:
            STORM_END = now

    else:
        send_line(f"{current_status_icon()} Unknown interrupt reason: {reason}")
        log_json({
            "event": "unknown_interrupt",
            "node_id": NODE_ID,
            "region": NODE_REGION,
            "reason": reason,
            "unix_ts": now,
        })


# ---------------- Wiring + Main Loop ----------------
GPIO.setup(pin, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
sensor.set_mask_disturber(False)
GPIO.add_event_detect(pin, GPIO.RISING, callback=handle_interrupt)

send_line(
    f"{STATUS_IDLE} System ready. Listening for lightning strikes... "
    f"({NODE_ID} · {NODE_REGION} · {NODE_CHANNEL})"
)
log_json({
    "event": "node_start",
    "node_id": NODE_ID,
    "region": NODE_REGION,
    "unix_ts": time.time(),
})

try:
    while True:
        time.sleep(10)
        maybe_handle_storm_summary()

except KeyboardInterrupt:
    pass
finally:
    GPIO.cleanup()
    send_line(f"{STATUS_IDLE} GPIO cleaned up. Node shutting down.")
    log_json({
        "event": "node_shutdown",
        "node_id": NODE_ID,
        "region": NODE_REGION,
        "unix_ts": time.time(),
    })