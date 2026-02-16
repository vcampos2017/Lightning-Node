#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Lightning detector -> Bluesky publisher.

Best-practice notes:
- Posting behavior is controlled by a boolean `dry_run` loaded from ~/.bluesky_credentials.ini
  under the [app] section (dry_run = true|false).
- Default is dry_run = true to prevent accidental posting during testing.
- You can override at runtime with environment variable LIGHTNING_DRY_RUN=true|false.
"""

import os
import sys
import logging
import time
import threading
import configparser
import math
import json
from collections import deque
from datetime import datetime
from pathlib import Path
from typing import Optional, Tuple

from bluesky_post_controller import BlueskyPostController
from error_handler import init_logging  # keep existing logging setup

# ---------------- NOAA / NWS plausibility (optional) ----------------
# Uses api.weather.gov (NOAA/NWS). No API key required, but NOAA requests a proper User-Agent.
# We intentionally avoid external deps (no requests) and use stdlib urllib.
import urllib.request
import urllib.error

HAVE_NOAA = True

# ---------------- Local helpers ----------------
# ---------------- Local helpers ----------------
def warn(msg: str, context: str = "") -> None:
    """Compatibility shim: earlier versions used warn(...)."""
    if context:
        logging.warning("[%s] %s", context, msg)
    else:
        logging.warning("%s", msg)

def log_exception(err: Exception, context: str = "") -> None:
    """Log an exception without relying on error_handler.handle_error (which may be broken)."""
    if context:
        logging.exception("ERROR (%s): %s", context, err)
    else:
        logging.exception("ERROR: %s", err)

# ---------------- Configuration ----------------
CONFIG_PATH = os.path.expanduser("~/.bluesky_credentials.ini")

def _env_bool(name: str):
    """Parse an environment variable into bool or None if unset."""
    val = os.getenv(name)
    if val is None:
        return None
    val = val.strip().lower()
    if val in ("1", "true", "yes", "y", "on"):
        return True
    if val in ("0", "false", "no", "n", "off"):
        return False
    return None

def load_app_config(config_path: str = CONFIG_PATH):
    """Load Bluesky creds + app settings from an INI file.

    Expected structure:

    [bluesky]
    handle = your.handle.bsky.social
    app_password = xxxx-xxxx-xxxx-xxxx

    [app]
    dry_run = true|false
    """
    cfg = configparser.ConfigParser()
    read_files = cfg.read(config_path)

    if not read_files:
        raise FileNotFoundError(
            f"Credentials file not found or unreadable: {config_path}. "
            "Create it and ensure permissions are correct."
        )

    if not cfg.has_section("bluesky"):
        raise KeyError(f"Missing [bluesky] section in {config_path}")

    handle = cfg.get("bluesky", "handle", fallback="").strip()
    app_password = cfg.get("bluesky", "app_password", fallback="").strip()

    if not handle or not app_password:
        raise ValueError(f"Missing handle/app_password in [bluesky] section of {config_path}")

    # Safe default: dry-run ON unless explicitly disabled.
    dry_run = True
    if cfg.has_section("app"):
        try:
            dry_run = cfg.getboolean("app", "dry_run", fallback=dry_run)
        except ValueError:
            dry_run = True

    # Optional env override for quick testing without editing files.
    env_override = _env_bool("LIGHTNING_DRY_RUN")
    if env_override is not None:
        dry_run = env_override

    return handle, app_password, dry_run

# Initialize logging for the app (writes to lightning_bluesky.log)
init_logging()

# Mirror logging to stdout so messages appear in `journalctl -u lightning-bluesky`
# (init_logging() writes to lightning_bluesky.log; systemd captures stdout/stderr).
def _ensure_stdout_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Avoid duplicate stream handlers if the service restarts.
    for h in list(root.handlers):
        if isinstance(h, logging.StreamHandler) and getattr(h, "stream", None) in (sys.stdout, sys.stderr):
            return

    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(logging.INFO)
    sh.setFormatter(logging.Formatter("%(message)s"))
    root.addHandler(sh)

_ensure_stdout_logging()

try:
    BLUESKY_HANDLE, BLUESKY_APP_PASSWORD, DRY_RUN = load_app_config(CONFIG_PATH)
except Exception as e:
    log_exception(e, context="loading Bluesky credentials / app config")
    sys.exit(1)

# Controller only handles rate limiting/deduping and respects dry_run.
POST_CONTROLLER = BlueskyPostController(
    state_path="posting_state.json",
    dry_run=DRY_RUN,
)

print(f"BOOT: dry_run={DRY_RUN!r} type={type(DRY_RUN)}")
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

# ---------- GPIO + AS3935 ----------
import RPi.GPIO as GPIO

# Robust import: avoid "module object is not callable" issues across variants
AS3935_CLASS = None
_as3935_import_errors = []

try:
    # Common packaging
    from RPi_AS3935.RPi_AS3935 import RPi_AS3935 as _AS3935
    AS3935_CLASS = _AS3935
except Exception as e:
    _as3935_import_errors.append(f"RPi_AS3935.RPi_AS3935 import failed: {e}")

if AS3935_CLASS is None:
    try:
        # Alternate packaging
        from RPi_AS3935 import RPi_AS3935 as _AS3935
        AS3935_CLASS = _AS3935
    except Exception as e:
        _as3935_import_errors.append(f"RPi_AS3935 import failed: {e}")

if AS3935_CLASS is None:
    raise ImportError("Could not import AS3935 class. Errors: " + " | ".join(_as3935_import_errors))

if not callable(AS3935_CLASS):
    raise TypeError(f"AS3935_CLASS resolved to non-callable object: {AS3935_CLASS!r}")

# Use the resolved class name expected later
RPi_AS3935 = AS3935_CLASS

# ---------- NOAA gate (storm plausibility) ----------
NOAA_ENABLED = os.getenv("NOAA_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y", "on")
NOAA_REQUIRED = os.getenv("NOAA_REQUIRED", "true").strip().lower() in ("1", "true", "yes", "y", "on")
NOAA_USER_AGENT = os.getenv("NOAA_USER_AGENT", "").strip()
NOAA_LAT = os.getenv("NOAA_LAT", "").strip()
NOAA_LON = os.getenv("NOAA_LON", "").strip()
NOAA_CACHE_TTL_S = int(os.getenv("NOAA_CACHE_TTL_S", "600"))

_NOAA_CACHE: Optional[Tuple[float, bool, str]] = None  # (checked_at, ok, reason)

def _noaa_get_json(url: str, user_agent: str, timeout_s: int = 8) -> dict:
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": user_agent,
            "Accept": "application/geo+json, application/json;q=0.9, */*;q=0.8",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = resp.read().decode("utf-8", errors="replace")
    return json.loads(data)

def noaa_storm_plausible() -> Tuple[Optional[bool], str]:
    """Return (ok, reason)

    ok=True/False if NOAA check performed
    ok=None if NOAA unavailable/misconfigured/disabled

    Logic:
      - Fetch /points/{lat},{lon} to discover forecastHourly URL
      - Fetch forecastHourly and look for thunderstorm keywords in next ~18 hours
      - Also check active alerts near the point
    """
    global _NOAA_CACHE

    if not NOAA_ENABLED:
        return None, "NOAA gating disabled (NOAA_ENABLED=false)."

    if not HAVE_NOAA:
        return None, "NOAA/urllib unavailable."

    if not NOAA_USER_AGENT or not NOAA_LAT or not NOAA_LON:
        return None, "Missing NOAA env (NOAA_USER_AGENT/NOAA_LAT/NOAA_LON)."

    try:
        lat = float(NOAA_LAT)
        lon = float(NOAA_LON)
    except ValueError:
        return None, "NOAA_LAT/NOAA_LON not parseable as floats."

    now = time.time()

    if _NOAA_CACHE:
        checked_at, ok, reason = _NOAA_CACHE
        if now - checked_at <= NOAA_CACHE_TTL_S:
            return ok, f"(cached) {reason}"

    try:
        points_url = f"https://api.weather.gov/points/{lat:.4f},{lon:.4f}"
        points = _noaa_get_json(points_url, NOAA_USER_AGENT)
        props = (points or {}).get("properties") or {}
        forecast_hourly_url = props.get("forecastHourly")

        alerts_url = f"https://api.weather.gov/alerts/active?point={lat:.4f},{lon:.4f}"
        alerts = _noaa_get_json(alerts_url, NOAA_USER_AGENT)
        features = (alerts or {}).get("features") or []

        alert_hit = False
        for f in features:
            p = (f or {}).get("properties") or {}
            ev = (p.get("event") or "").lower()
            if any(k in ev for k in ("severe thunderstorm", "thunderstorm", "tornado", "flash flood")):
                alert_hit = True
                break

        forecast_hit = False
        if forecast_hourly_url:
            fh = _noaa_get_json(forecast_hourly_url, NOAA_USER_AGENT)
            periods = ((fh or {}).get("properties") or {}).get("periods") or []
            for per in periods[:18]:
                short_fc = (per.get("shortForecast") or "").lower()
                detailed_fc = (per.get("detailedForecast") or "").lower()
                text = f"{short_fc} {detailed_fc}"
                if any(k in text for k in ("thunderstorm", "t-storm", "tstorm", "lightning")):
                    forecast_hit = True
                    break

        ok = bool(alert_hit or forecast_hit)
        reason = "NOAA storm plausibility: POSITIVE (storm signals detected)." if ok else "NOAA storm plausibility: NEGATIVE (no storm signals detected)."
        _NOAA_CACHE = (now, ok, reason)
        return ok, reason

    except urllib.error.HTTPError as e:
        return None, f"NOAA HTTPError: {e.code} {getattr(e, 'reason', '')}".strip()
    except Exception as e:
        return None, f"NOAA check exception: {e}"

# ---------- Output / logging ----------
# ---------- Output / logging ----------
# NOAA gate log throttling (prevents journal spam while still suppressing posts)
NOAA_LOG_COOLDOWN_S = int(os.getenv("NOAA_LOG_COOLDOWN_S", "60"))
_LAST_NOAA_SUPPRESS_LOG_AT = 0.0

def send_line(message: str) -> None:
    """Print + flush so it appears in journalctl immediately."""
    print(message, flush=True)

# ---------- Posting ----------
def post_bluesky(text: str, image_path: Optional[str] = None) -> None:
    """Post to Bluesky, with optional image embed.

    - Uses NOAA plausibility gate (if enabled)
    - Uses BlueskyPostController for spam control / persistence
    """
    # 1) NOAA plausibility gate (prevents false alarms)
    ok, reason = noaa_storm_plausible()
    if ok is False:
        global _LAST_NOAA_SUPPRESS_LOG_AT
        now_ts = time.time()
        if now_ts - _LAST_NOAA_SUPPRESS_LOG_AT >= NOAA_LOG_COOLDOWN_S:
            warn(f"Suppressed by NOAA gate: {reason}", context="NOAA gate")
            _LAST_NOAA_SUPPRESS_LOG_AT = now_ts
        return
    if ok is None and NOAA_REQUIRED:
        warn(f"Suppressed (NOAA required but unavailable): {reason}", context="NOAA gate")
        return

    handle = BLUESKY_HANDLE
    app_pw = BLUESKY_APP_PASSWORD

    if not handle or not app_pw:
        warn(
            "Missing BLUESKY_HANDLE or BLUESKY_APP_PASSWORD – skipping Bluesky post.",
            context="Bluesky credentials",
        )
        return

    # 2) Post gating (prevents spam, survives restarts)
    dedupe_key = "storm_summary" if image_path else "lightning"
    event = {
        "type": "bluesky_post",
        "timestamp": int(time.time()),
        "dedupe_key": dedupe_key,
    }

    decision = POST_CONTROLLER.should_post(event)
    if not decision.allow:
        warn(
            f"Suppressed: {decision.reason} (retry_after={decision.retry_after_s}s)",
            context="Bluesky post controller",
        )
        return

    # 3) Attempt the post
    try:
        # Lazy import: atproto is heavy on Raspberry Pi; only import when we actually post.
        from atproto import Client, models

        client = Client()
        client.login(handle, app_pw)

        if image_path:
            try:
                with open(image_path, "rb") as f:
                    img_bytes = f.read()
            except FileNotFoundError as e:
                log_exception(e, context=f"opening image file for Bluesky post: {image_path}")
                return

            blob_output = client.upload_blob(img_bytes)

            embed = models.AppBskyEmbedImages.Main(
                images=[
                    models.AppBskyEmbedImages.Image(
                        alt="Lightning storm summary chart",
                        image=blob_output.blob,
                    )
                ]
            )

            client.send_post(text=text, embed=embed)
            POST_CONTROLLER.record_post(event)
            send_line(f"[Bluesky] Posted with image: {image_path}")
        else:
            client.send_post(text=text)
            POST_CONTROLLER.record_post(event)
            send_line("[Bluesky] Posted (text-only).")

    except Exception as e:
        log_exception(e, context="posting to Bluesky")

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

    bins = []
    counts = []
    energies = []

    t0 = storm_start
    t1 = storm_end
    nbins = int(math.ceil((t1 - t0) / SUMMARY_BIN_SIZE))

    for i in range(nbins + 1):
        bin_start = t0 + i * SUMMARY_BIN_SIZE
        bin_end = bin_start + SUMMARY_BIN_SIZE
        in_bin = [e for (t, _, e) in data if bin_start <= t < bin_end]
        bins.append(datetime.fromtimestamp(bin_start))
        counts.append(len(in_bin))
        energies.append(sum(in_bin) if in_bin else 0)

    plt.figure(figsize=(10, 4))
    plt.plot(bins, counts, label="Strikes/bin")
    plt.plot(bins, energies, label="Energy/bin")
    plt.title("Storm Summary")
    plt.xlabel("Time")
    plt.ylabel("Count / Energy")
    plt.legend()
    plt.tight_layout()

    out_path = "storm_summary.png"
    plt.savefig(out_path)
    plt.close()
    return out_path

def handle_storm_state():
    """Evaluate storm session start/end + summary posting."""
    global STORM_ACTIVE, STORM_START, STORM_END, LAST_SUMMARY_POSTED

    now = time.time()
    recent = [(t, d, e) for (t, d, e) in STRIKE_HISTORY if now - t <= STORM_WINDOW]

    # Start storm if enough strikes in window
    if not STORM_ACTIVE and len(recent) >= STORM_MIN_STRIKES:
        STORM_ACTIVE = True
        STORM_START = recent[0][0]
        send_line(f"{STATUS_STORM} Storm session started at {datetime.fromtimestamp(STORM_START):%Y-%m-%d %H:%M:%S}")

    # End storm if quiet long enough
    if STORM_ACTIVE:
        last_strike_time = STRIKE_HISTORY[-1][0] if STRIKE_HISTORY else now
        if now - last_strike_time >= STORM_GAP_TO_END:
            STORM_ACTIVE = False
            STORM_END = last_strike_time
            send_line(f"{STATUS_MONITORING} Storm session ended at {datetime.fromtimestamp(STORM_END):%Y-%m-%d %H:%M:%S}")

            # Post summary after delay
            if LAST_SUMMARY_POSTED is None or now - LAST_SUMMARY_POSTED >= SUMMARY_DELAY:
                chart = make_storm_chart(STORM_START, STORM_END)
                if chart:
                    send_line("[Storm] Posting summary chart to Bluesky...")
                    threading.Thread(
                        target=post_bluesky,
                        args=(f"Storm summary for {NODE_REGION} ({NODE_ID})", chart),
                        daemon=True,
                    ).start()
                LAST_SUMMARY_POSTED = now

def main_loop():
    print_startup_banner()
    send_line(f"{STATUS_IDLE} System ready. Listening for lightning strikes... ({NODE_ID} · {NODE_REGION} · {NODE_CHANNEL})")

    while True:
        try:
            handle_storm_state()

            # interrupt reason from sensor
            reason = sensor.get_interrupt()
            if reason == 0:
                pass
            elif reason == 1:
                send_line("🟢 Noise level too high (noise).")
            elif reason == 4:
                send_line("🟢 Disturber detected — masking subsequent disturbers.")
            elif reason == 8:
                # Lightning
                dist = sensor.get_distance()
                energy = sensor.get_energy()
                record_strike(dist, energy)
                icon = current_status_icon()
                send_tweet(
                    f"{icon} Lightning detected! Energy: {energy} — distance: {dist} km ({dist*0.621371:.1f} mi) | "
                    f"Éclair détecté ! Puissance : {energy} — distance : {dist} km ({dist*0.621371:.1f} mi)"
                )
            else:
                send_line(f"🟡 Unknown interrupt reason: {reason}")

            time.sleep(0.1)

        except KeyboardInterrupt:
            send_line("Exiting...")
            break
        except Exception as e:
            log_exception(e, context="main loop")
            time.sleep(2)

if __name__ == "__main__":
    main_loop()