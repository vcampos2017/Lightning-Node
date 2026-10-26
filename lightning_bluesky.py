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
from datetime import datetime, timezone
from typing import Optional, Tuple
try:
    from zoneinfo import ZoneInfo
except ImportError:
    ZoneInfo = None

from bluesky_post_controller import BlueskyPostController
from error_handler import init_logging

import urllib.request
import urllib.error

HAVE_NOAA = True


def warn(msg: str, context: str = "") -> None:
    if context:
        logging.warning("[%s] %s", context, msg)
    else:
        logging.warning("%s", msg)


def log_exception(err: Exception, context: str = "") -> None:
    if context:
        logging.exception("ERROR (%s): %s", context, err)
    else:
        logging.exception("ERROR: %s", err)


def get_greenhouse_metrics_safe():
    """Fetch optional environment metrics from greenhouse-node.

    This is enrichment only. If the greenhouse node is offline or slow,
    lightning detection and Bluesky posting must continue normally.
    """
    try:
        url = os.getenv("GREENHOUSE_STATUS_URL", "http://greenhouse-pi:5000/status")
        with urllib.request.urlopen(url, timeout=3) as response:
            data = json.load(response)
        return data.get("metrics", {})
    except Exception as e:
        warn(f"fetch failed: {e}", context="Greenhouse")
        return {}


def format_environment(metrics):
    """Format optional greenhouse environmental metrics for compact posts."""
    if not metrics:
        return ""
    try:
        t = round(metrics.get("air_temperature_c", 0), 1)
        h = int(round(metrics.get("air_humidity", 0)))
        p = round(metrics.get("air_pressure_hpa", 0), 1)
        return f" | 🌡️ {t}°C 💧 {h}% 📉 {p} hPa"
    except Exception as e:
        warn(f"format failed: {e}", context="Greenhouse")
        return ""


CONFIG_PATH = os.path.expanduser("~/.bluesky_credentials.ini")


def _env_bool(name: str):
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
    logging.info("Bluesky credentials source: ~/.bluesky_credentials.ini")

    if not handle or not app_password:
        raise ValueError(f"Missing handle/app_password in [bluesky] section of {config_path}")

    dry_run = True
    if cfg.has_section("app"):
        try:
            dry_run = cfg.getboolean("app", "dry_run", fallback=dry_run)
        except ValueError:
            dry_run = True

    env_override = _env_bool("LIGHTNING_DRY_RUN")
    if env_override is not None:
        dry_run = env_override

    return handle, app_password, dry_run


init_logging()


def _ensure_stdout_logging() -> None:
    root = logging.getLogger()
    root.setLevel(logging.INFO)

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

POST_CONTROLLER = BlueskyPostController(
    state_path="posting_state.json",
    dry_run=DRY_RUN,
)

print(f"BOOT: dry_run={DRY_RUN!r} type={type(DRY_RUN)}")
NODE_ID = "PASS-LN-01"
NODE_REGION = "Greater Harmony Hills"
NODE_CHANNEL = "Atmospheric Telemetry"


def print_startup_banner() -> None:
    print("\n================ LightningSensorBluesky ================")
    print(f" Node ID     : {NODE_ID}")
    print(f" Region      : {NODE_REGION}")
    print(f" Channel     : {NODE_CHANNEL}")
    print(f" Bluesky     : {BLUESKY_HANDLE}")
    print(" Log file    : lightning_bluesky.log")
    print(" Status      : Starting up and listening for lightning...")
    print("=======================================================\n")


STATUS_IDLE = "🟢"
STATUS_MONITORING = "🟡"
STATUS_STORM = "🔴"

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAVE_MATPLOTLIB = True
except Exception as e:
    HAVE_MATPLOTLIB = False
    print(f"[Warning] matplotlib not available: {e}; charts will not be generated.")

import RPi.GPIO as GPIO

AS3935_CLASS = None
_as3935_import_errors = []

try:
    from RPi_AS3935.RPi_AS3935 import RPi_AS3935 as _AS3935
    AS3935_CLASS = _AS3935
except Exception as e:
    _as3935_import_errors.append(f"RPi_AS3935.RPi_AS3935 import failed: {e}")

if AS3935_CLASS is None:
    try:
        from RPi_AS3935 import RPi_AS3935 as _AS3935
        AS3935_CLASS = _AS3935
    except Exception as e:
        _as3935_import_errors.append(f"RPi_AS3935 import failed: {e}")

if AS3935_CLASS is None:
    raise ImportError("Could not import AS3935 class. Errors: " + " | ".join(_as3935_import_errors))

if not callable(AS3935_CLASS):
    raise TypeError(f"AS3935_CLASS resolved to non-callable object: {AS3935_CLASS!r}")



class LightningSensor(AS3935_CLASS):
    """RPi_AS3935 with fixes for current Raspberry Pi OS.

    - smbus2 requires an explicit length for read_i2c_block_data, which the
      stock library leaves out. We read registers 0x00-0x08 explicitly, so the
      installed library no longer needs patching (replaces fix_as3935_lib.sh).
    - Brief retries on transient I2C errors (e.g. Errno 121 Remote I/O error).
    """

    I2C_RETRIES = 5
    I2C_RETRY_DELAY_S = 0.05

    def _retry(self, fn):
        last_err = None
        for _ in range(self.I2C_RETRIES):
            try:
                return fn()
            except OSError as e:
                last_err = e
                time.sleep(self.I2C_RETRY_DELAY_S)
        raise last_err

    def read_data(self):
        self.registers = self._retry(
            lambda: self.i2cbus.read_i2c_block_data(self.address, 0x00, 9)
        )

    def set_byte(self, register, value):
        self._retry(lambda: self.i2cbus.write_byte_data(self.address, register, value))


NOAA_ENABLED = os.getenv("NOAA_ENABLED", "true").strip().lower() in ("1", "true", "yes", "y", "on")
NOAA_REQUIRED = os.getenv("NOAA_REQUIRED", "true").strip().lower() in ("1", "true", "yes", "y", "on")
NOAA_USER_AGENT = os.getenv("NOAA_USER_AGENT", "").strip()
NOAA_LAT = os.getenv("NOAA_LAT", "").strip()
NOAA_LON = os.getenv("NOAA_LON", "").strip()
NOAA_CACHE_TTL_S = int(os.getenv("NOAA_CACHE_TTL_S", "600"))
# How many hourly forecast periods to scan for thunder (was a fixed 18 hours).
NOAA_FORECAST_HOURS = int(os.getenv("NOAA_FORECAST_HOURS", "3"))

_NOAA_CACHE: Optional[Tuple[float, bool, str]] = None


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
            for per in periods[:NOAA_FORECAST_HOURS]:
                short_fc = (per.get("shortForecast") or "").lower()
                detailed_fc = (per.get("detailedForecast") or "").lower()
                text = f"{short_fc} {detailed_fc}"
                if any(k in text for k in ("thunderstorm", "t-storm", "tstorm", "lightning")):
                    forecast_hit = True
                    break

        ok = bool(alert_hit or forecast_hit)
        reason = (
            "NOAA storm plausibility: POSITIVE (storm signals detected)."
            if ok
            else "NOAA storm plausibility: NEGATIVE (no storm signals detected)."
        )
        _NOAA_CACHE = (now, ok, reason)
        return ok, reason

    except urllib.error.HTTPError as e:
        return None, f"NOAA HTTPError: {e.code} {getattr(e, 'reason', '')}".strip()
    except Exception as e:
        return None, f"NOAA check exception: {e}"


NOAA_LOG_COOLDOWN_S = int(os.getenv("NOAA_LOG_COOLDOWN_S", "60"))
_LAST_NOAA_SUPPRESS_LOG_AT = 0.0


def send_line(message: str) -> None:
    print(message, flush=True)


MAX_POSTS_PER_DAY = int(os.getenv("MAX_POSTS_PER_DAY", "4"))
MIN_POST_INTERVAL_S = int(os.getenv("MIN_POST_INTERVAL_S", str(3 * 60 * 60)))
CALM_WINDOW_S = int(os.getenv("CALM_WINDOW_S", str(2 * 60 * 60)))
LOCAL_TIMEZONE = os.getenv("LOCAL_TIMEZONE", "America/Chicago")

# Posting policy state.
# A "storm session" opens with the first lightning post that actually goes out,
# and closes once no lightning has been detected for CALM_WINDOW_S. Closing the
# session means the next storm (even later the same day) gets a fresh
# "storm_open" post instead of a "still continuing" follow-up.
POST_STATE = {
    "day": None,
    "posts_today": 0,
    "last_post_ts": 0.0,
    "storm_opened": False,
    "last_lightning_ts": 0.0,
}
_STATE_LOCK = threading.RLock()
_POST_LOCK = threading.Lock()  # only one Bluesky post in flight at a time


def _today_str() -> str:
    if ZoneInfo is not None:
        return datetime.now(ZoneInfo(LOCAL_TIMEZONE)).strftime("%Y-%m-%d")
    return datetime.now().strftime("%Y-%m-%d")


def reset_day_if_needed() -> None:
    """Reset the daily post counter at local midnight.

    Only the counter resets; storm state is left alone so a storm that runs
    past midnight is not announced as a brand-new storm.
    """
    with _STATE_LOCK:
        today = _today_str()
        if POST_STATE["day"] != today:
            POST_STATE["day"] = today
            POST_STATE["posts_today"] = 0


def can_post_now(now_ts: float, ignore_min_interval: bool = False) -> bool:
    with _STATE_LOCK:
        if POST_STATE["posts_today"] >= MAX_POSTS_PER_DAY:
            return False
        if ignore_min_interval:
            return True
        if POST_STATE["last_post_ts"] and (now_ts - POST_STATE["last_post_ts"] < MIN_POST_INTERVAL_S):
            return False
        return True


def record_policy_post(now_ts: float, post_type: str) -> None:
    """Count a post against the policy. Called only AFTER a post succeeds."""
    with _STATE_LOCK:
        reset_day_if_needed()
        POST_STATE["posts_today"] += 1
        POST_STATE["last_post_ts"] = now_ts
        if post_type == "storm_open":
            POST_STATE["storm_opened"] = True


def _close_storm_if_quiet(now_ts: float) -> bool:
    """Close the storm session if it has been quiet for CALM_WINDOW_S.

    Returns True if a session was just closed.
    """
    with _STATE_LOCK:
        last = POST_STATE["last_lightning_ts"]
        if POST_STATE["storm_opened"] and last > 0 and (now_ts - last) >= CALM_WINDOW_S:
            POST_STATE["storm_opened"] = False
            return True
        return False


def note_lightning_event(now_ts: float) -> None:
    with _STATE_LOCK:
        POST_STATE["last_lightning_ts"] = now_ts


def classify_lightning_post(now_ts: float) -> Optional[str]:
    """Decide what kind of post (if any) this strike deserves.

    Does NOT change posting state: the post is only counted once it has
    actually been published (see record_policy_post). Call this BEFORE
    note_lightning_event so the quiet-period check sees the previous strike.
    """
    with _STATE_LOCK:
        reset_day_if_needed()
        _close_storm_if_quiet(now_ts)
        if not can_post_now(now_ts):
            return None
        return "followup" if POST_STATE["storm_opened"] else "storm_open"


def maybe_post_calm_summary() -> None:
    now_ts = time.time()
    with _STATE_LOCK:
        reset_day_if_needed()
        if not _close_storm_if_quiet(now_ts):
            return
        # The calm post is the single closing message for a storm. It skips the
        # minimum gap between posts (CALM_WINDOW_S is shorter than
        # MIN_POST_INTERVAL_S, so it would almost always be blocked) but still
        # counts toward MAX_POSTS_PER_DAY.
        allowed = can_post_now(now_ts, ignore_min_interval=True)

    # The storm session is closed either way; the calm post is a single attempt.
    if not allowed:
        send_line("🌤️ Storm session closed (calm post skipped: daily post limit reached).")
        return
    publish(
        "🌤️ Conditions have quieted. No recent lightning has been detected, and weather conditions appear calmer.",
        post_type="calm",
        noaa_gate=False,  # the calm post makes no lightning claim, so no NOAA corroboration needed
    )


def post_bluesky(text: str, image_path: Optional[str] = None, noaa_gate: bool = True) -> bool:
    """Publish to Bluesky. Returns True if the post went out (or would have, in dry run)."""
    if noaa_gate:
        ok, reason = noaa_storm_plausible()
        if ok is False:
            global _LAST_NOAA_SUPPRESS_LOG_AT
            now_ts = time.time()
            if now_ts - _LAST_NOAA_SUPPRESS_LOG_AT >= NOAA_LOG_COOLDOWN_S:
                warn(f"Suppressed by NOAA gate: {reason}", context="NOAA gate")
                _LAST_NOAA_SUPPRESS_LOG_AT = now_ts
            return False
        if ok is None and NOAA_REQUIRED:
            warn(f"Suppressed (NOAA required but unavailable): {reason}", context="NOAA gate")
            return False

    handle = BLUESKY_HANDLE
    app_pw = BLUESKY_APP_PASSWORD

    if not handle or not app_pw:
        warn(
            "Missing BLUESKY_HANDLE or BLUESKY_APP_PASSWORD – skipping Bluesky post.",
            context="Bluesky credentials",
        )
        return False

    dedupe_key = "storm_summary" if image_path else "lightning"
    event = {
        "type": "bluesky_post",
        "timestamp": int(time.time()),
        "dedupe_key": dedupe_key,
    }

    decision = POST_CONTROLLER.should_post(event)
    if not decision.allow:
        if decision.reason == "dry_run":
            # Behave as if posted so dry runs exercise the same storm/calm flow as live mode.
            send_line(f"[DRY RUN] Would post{' with image' if image_path else ''}: {text}")
            return True
        warn(
            f"Suppressed: {decision.reason} (retry_after={decision.retry_after_s}s)",
            context="Bluesky post controller",
        )
        return False

    try:
        from atproto import Client, models

        client = Client()
        client.login(handle, app_pw)

        if image_path:
            try:
                with open(image_path, "rb") as f:
                    img_bytes = f.read()
            except FileNotFoundError as e:
                log_exception(e, context=f"opening image file for Bluesky post: {image_path}")
                return False

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
        return True

    except Exception as e:
        log_exception(e, context="posting to Bluesky")
        return False


def _publish_worker(message: str, post_type: Optional[str], image_path: Optional[str], noaa_gate: bool) -> None:
    if not _POST_LOCK.acquire(blocking=False):
        warn("Another post is already in progress; skipping this one.", context="Bluesky")
        return
    try:
        if image_path:
            text = message
        else:
            # Optional enrichment from other nodes. Done here (off the main loop)
            # so slow or offline nodes never delay lightning detection.
            env_str = format_environment(get_greenhouse_metrics_safe())
            rain_str = format_rain(get_rain_metrics())
            text = f"{datetime.now():%Y-%m-%d %H:%M:%S} — {message}{env_str}{rain_str}"
        send_line(text)
        if post_bluesky(text, image_path=image_path, noaa_gate=noaa_gate) and post_type:
            record_policy_post(time.time(), post_type)
    except Exception as e:
        log_exception(e, context="publish worker")
    finally:
        _POST_LOCK.release()


def publish(
    message: str,
    post_type: Optional[str] = None,
    image_path: Optional[str] = None,
    noaa_gate: bool = True,
) -> None:
    """Post in a background thread. post_type ("storm_open" / "followup" / "calm")
    is counted against the posting policy only if the post succeeds."""
    threading.Thread(
        target=_publish_worker,
        args=(message, post_type, image_path, noaa_gate),
        daemon=True,
    ).start()


GPIO.setmode(GPIO.BCM)
pin = 17

sensor = LightningSensor(address=0x03, bus=1)
sensor.set_indoors(False)
sensor.set_noise_floor(1)
sensor.calibrate(tun_cap=0x01)
sensor.set_min_strikes(5)

# ---------------- Rain Gauge Setup ----------------
# SparkFun / Weather Meter tipping-bucket rain gauge.
# Disabled by default until hardware is connected.
RAIN_GAUGE_ENABLED = os.getenv("RAIN_GAUGE_ENABLED", "false").strip().lower() in ("1", "true", "yes", "y", "on")
RAIN_GPIO_PIN = int(os.getenv("RAIN_GPIO_PIN", "27"))
RAIN_MM_PER_TIP = float(os.getenv("RAIN_MM_PER_TIP", "0.2794"))
RAIN_BOUNCETIME_MS = int(os.getenv("RAIN_BOUNCETIME_MS", "500"))

RAIN_TIP_COUNT = 0
RAIN_LAST_TIP_TS = 0.0


def rain_tip_callback(channel):
    """GPIO callback for rain gauge tipping-bucket closure."""
    global RAIN_TIP_COUNT, RAIN_LAST_TIP_TS
    RAIN_TIP_COUNT += 1
    RAIN_LAST_TIP_TS = time.time()
    rain_mm = RAIN_TIP_COUNT * RAIN_MM_PER_TIP
    rain_in = rain_mm / 25.4
    send_line(
        f"🌧️ Rain gauge tip detected: tips={RAIN_TIP_COUNT}, "
        f"rain={rain_mm:.2f} mm ({rain_in:.3f} in)"
    )


def setup_rain_gauge():
    """Initialize optional rain gauge GPIO input."""
    if not RAIN_GAUGE_ENABLED:
        send_line("🌧️ Rain gauge disabled (RAIN_GAUGE_ENABLED=false).")
        return

    GPIO.setup(RAIN_GPIO_PIN, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.add_event_detect(
        RAIN_GPIO_PIN,
        GPIO.FALLING,
        callback=rain_tip_callback,
        bouncetime=RAIN_BOUNCETIME_MS,
    )
    send_line(
        f"🌧️ Rain gauge enabled on GPIO{RAIN_GPIO_PIN}; "
        f"{RAIN_MM_PER_TIP} mm/tip, debounce={RAIN_BOUNCETIME_MS} ms."
    )


def get_rain_metrics():
    """Return rain gauge metrics.

    Prefer the dedicated rain-node API when available. If it is offline,
    fall back to the local placeholder GPIO counters.
    """
    rain_url = os.getenv("RAIN_STATUS_URL", "http://rain-node:5000/status")

    try:
        with urllib.request.urlopen(rain_url, timeout=3) as response:
            data = json.load(response)

        return {
            "rain_source": "rain-node",
            "rain_enabled": True,
            "rain_gpio_pin": None,
            "rain_tip_count": int(data.get("tip_count", 0)),
            "rain_total_mm": round(float(data.get("rain_mm", 0)), 2),
            "rain_total_in": round(float(data.get("rain_in", 0)), 3),
            "rain_rate_mm_hr": round(float(data.get("rate_mm_hr", 0)), 2),
            "rain_last_tip_ts": None,
            "rain_last_tip_iso": data.get("timestamp_utc"),
        }
    except Exception as e:
        warn(f"fetch failed: {e}", context="Rain Node")

    rain_mm = RAIN_TIP_COUNT * RAIN_MM_PER_TIP
    last_tip_iso = None
    if RAIN_LAST_TIP_TS:
        last_tip_iso = datetime.fromtimestamp(RAIN_LAST_TIP_TS).isoformat(timespec="seconds")

    return {
        "rain_source": "local",
        "rain_enabled": RAIN_GAUGE_ENABLED,
        "rain_gpio_pin": RAIN_GPIO_PIN,
        "rain_tip_count": RAIN_TIP_COUNT,
        "rain_total_mm": round(rain_mm, 2),
        "rain_total_in": round(rain_mm / 25.4, 3),
        "rain_rate_mm_hr": 0.0,
        "rain_last_tip_ts": RAIN_LAST_TIP_TS,
        "rain_last_tip_iso": last_tip_iso,
    }


def format_rain(metrics):
    """Format rain totals only after at least one rain gauge tip."""
    if not metrics or metrics.get("rain_tip_count", 0) <= 0:
        return ""
    return f" | 🌧️ {metrics.get('rain_total_in', 0):.3f} in"


STRIKE_HISTORY = deque(maxlen=2000)

STORM_ACTIVE = False
STORM_START = None
STORM_END = None
LAST_SUMMARY_POSTED = None

STORM_MIN_STRIKES = 3
STORM_WINDOW = 10 * 60
STORM_GAP_TO_END = 20 * 60
SUMMARY_DELAY = 60 * 60

SUMMARY_BIN_SIZE = 5 * 60


def current_status_icon() -> str:
    if STORM_ACTIVE:
        return STATUS_STORM
    now = time.time()
    recent = [t for (t, _, _) in STRIKE_HISTORY if now - t <= STORM_WINDOW]
    if recent:
        return STATUS_MONITORING
    return STATUS_IDLE


def record_strike(distance_km: Optional[int], energy: int):
    """Record a strike. distance_km is None when the sensor reports out of range."""
    now = time.time()
    STRIKE_HISTORY.append((now, distance_km, energy))

    # Persist structured telemetry for Chatty Node ingestion.
    try:
        event = {
            "distance_km": distance_km,
            "distance_mi": round(distance_km * 0.621371, 1) if distance_km is not None else None,
            "energy": energy,
            "event": "strike",
            "node_id": NODE_ID,
            "region": NODE_REGION,
            "ts_iso": datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z"),
            "unix_ts": now,
        }
        with open("lightning_telemetry.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(event) + "\n")
    except Exception as e:
        log_exception(e, context="write lightning telemetry jsonl")


def _get_strikes_during(storm_start, storm_end):
    return [(t, d, e) for (t, d, e) in STRIKE_HISTORY if storm_start <= t <= storm_end]


def make_storm_chart(storm_start, storm_end):
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

    # Strike counts and energy are on very different scales (single digits vs.
    # hundreds of thousands), so give each its own y-axis.
    fig, ax_count = plt.subplots(figsize=(10, 4))
    ax_energy = ax_count.twinx()
    line_count, = ax_count.plot(bins, counts, color="tab:blue", label="Strikes per 5 min")
    line_energy, = ax_energy.plot(bins, energies, color="tab:orange", label="Energy per 5 min")
    ax_count.set_xlabel("Time")
    ax_count.set_ylabel("Strikes")
    ax_energy.set_ylabel("Energy (sensor units)")
    ax_count.set_title(f"Storm Summary — {NODE_REGION} ({NODE_ID})")
    ax_count.legend(handles=[line_count, line_energy], loc="upper left")
    fig.autofmt_xdate()
    fig.tight_layout()

    out_path = "storm_summary.png"
    fig.savefig(out_path)
    plt.close(fig)
    return out_path


def handle_storm_state():
    global STORM_ACTIVE, STORM_START, STORM_END, LAST_SUMMARY_POSTED

    now = time.time()
    recent = [(t, d, e) for (t, d, e) in STRIKE_HISTORY if now - t <= STORM_WINDOW]

    if not STORM_ACTIVE and len(recent) >= STORM_MIN_STRIKES:
        STORM_ACTIVE = True
        STORM_START = recent[0][0]
        send_line(f"{STATUS_STORM} Storm session started at {datetime.fromtimestamp(STORM_START):%Y-%m-%d %H:%M:%S}")

    if STORM_ACTIVE:
        last_strike_time = STRIKE_HISTORY[-1][0] if STRIKE_HISTORY else now
        if now - last_strike_time >= STORM_GAP_TO_END:
            STORM_ACTIVE = False
            STORM_END = last_strike_time
            send_line(f"{STATUS_MONITORING} Storm session ended at {datetime.fromtimestamp(STORM_END):%Y-%m-%d %H:%M:%S}")

            if LAST_SUMMARY_POSTED is None or now - LAST_SUMMARY_POSTED >= SUMMARY_DELAY:
                chart = make_storm_chart(STORM_START, STORM_END)
                if chart:
                    send_line("[Storm] Posting summary chart to Bluesky...")
                    publish(f"Storm summary for {NODE_REGION} ({NODE_ID})", image_path=chart)
                LAST_SUMMARY_POSTED = now


def main_loop():
    print_startup_banner()
    setup_rain_gauge()
    send_line(f"{STATUS_IDLE} System ready. Listening for lightning strikes... ({NODE_ID} · {NODE_REGION} · {NODE_CHANNEL})")

    # Make a misconfigured NOAA gate obvious at startup instead of silently
    # suppressing every lightning post later.
    noaa_ok, noaa_reason = noaa_storm_plausible()
    if noaa_ok is None and NOAA_REQUIRED:
        warn(
            f"NOAA_REQUIRED=true but the NOAA check is unavailable ({noaa_reason}). "
            "Lightning posts will be suppressed until this is fixed.",
            context="NOAA gate",
        )
    else:
        send_line(f"NOAA gate: {noaa_reason}")

    while True:
        try:
            handle_storm_state()
            maybe_post_calm_summary()

            reason = sensor.get_interrupt()
            if reason == 0:
                pass
            elif reason == 1:
                send_line("🟢 Noise level too high (noise).")
            elif reason == 4:
                send_line("🟢 Disturber detected — masking subsequent disturbers.")
            elif reason == 8:
                raw_dist = sensor.get_distance()  # km, or False when out of range
                dist_km = None if raw_dist is False else raw_dist
                dist_str = f"{dist_km} km" if dist_km is not None else "out of range"
                energy = sensor.get_energy()
                record_strike(dist_km, energy)
                icon = current_status_icon()

                now_ts = time.time()
                post_kind = classify_lightning_post(now_ts)  # before note_lightning_event (see docstring)
                note_lightning_event(now_ts)

                if post_kind == "storm_open":
                    publish(
                        f"{icon} Lightning detected. NOAA-supported storm conditions appear active in {NODE_REGION}. Monitoring from {NODE_ID}.",
                        post_type="storm_open",
                    )
                elif post_kind == "followup":
                    publish(
                        f"{icon} Additional lightning detected in {NODE_REGION}. Storm activity appears to be continuing.",
                        post_type="followup",
                    )
                else:
                    send_line(
                        f"{icon} Lightning (logged, not posted): Energy={energy}, distance={dist_str}"
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