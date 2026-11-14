"""Print a short lightning-node status summary in the terminal.

Meant to run when you log in over SSH (see README), like the rain node's
rain_status.py and the greenhouse's greenhouse_status.py. Any time:

    python3 ~/Lightning-Node/lightning_status.py

It only reads things (the state file lightning-bluesky writes once a minute,
the strike log, the Bluesky settings' dry_run switch, systemctl,
/run/node-wifi.json, `ss`), so it is safe to run as often as you like. It never
reads or prints the Bluesky password. Plain Python, no venv needed.

Settings (optional):
    LIGHTNING_STATUS_FILE  the service's state file (default /dev/shm/lightning-node.json)
    LIGHTNING_TELEMETRY    the strike log (default lightning_telemetry.jsonl next to this file)
"""
from __future__ import annotations

import configparser
import json
import os
import socket
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
STATE_FILE = os.getenv("LIGHTNING_STATUS_FILE", "/dev/shm/lightning-node.json")
TELEMETRY = os.getenv("LIGHTNING_TELEMETRY", os.path.join(HERE, "lightning_telemetry.jsonl"))
CREDENTIALS = os.path.expanduser("~/.bluesky_credentials.ini")
ENV_FILE = os.path.expanduser("~/.bluesky_env")
WIFI_STATUS = os.getenv("NODE_WIFI_STATUS_FILE", "/run/node-wifi.json")
MQTT_PORT = int(os.getenv("MQTT_PORT", "1883"))

SERVICES = [("lightning-bluesky", "lightning"), ("node-wifi", "wifi"), ("tailscaled", "tailscale")]
STALE_SECONDS = 180  # the service writes its state every 60 s

COLOR = sys.stdout.isatty()


def paint(text, code):
    return f"\033[{code}m{text}\033[0m" if COLOR else text


def dot(good):
    return paint("●", "32" if good else "31")


def get_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return "no network"


def ago(seconds):
    seconds = max(0, seconds)
    minutes = int(seconds // 60)
    if seconds < 60:
        return f"{int(seconds)} s ago"
    if minutes < 60:
        return f"{minutes} min ago"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes} min ago"
    return f"{hours // 24} days ago"


def read_state(path=None):
    path = STATE_FILE if path is None else path
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def tail_lines(path, max_bytes=256 * 1024):
    """The last lines of a file (the strike log can grow big)."""
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            data = f.read()
    except OSError:
        return []
    lines = data.splitlines()
    if size > max_bytes and lines:
        lines = lines[1:]  # probably cut in the middle
    return [x.decode("utf-8", "replace") for x in lines if x.strip()]


def strikes_from_log(path=None, now=None):
    """(strikes today, last strike) from the strike log, for when the
    service's state file isn't there (service stopped, or an older version)."""
    path = TELEMETRY if path is None else path
    now = time.time() if now is None else now
    today = datetime.fromtimestamp(now).date()
    count, last = 0, None
    for line in tail_lines(path):
        try:
            ev = json.loads(line)
            ts = float(ev.get("unix_ts"))
        except (ValueError, TypeError, AttributeError):
            continue
        if ev.get("event", "strike") != "strike":
            continue
        if datetime.fromtimestamp(ts).date() == today:
            count += 1
        if last is None or ts >= last["ts"]:
            last = {"ts": ts, "distance_mi": ev.get("distance_mi"), "distance_km": ev.get("distance_km")}
    return count, last


def strike_text(last, now=None):
    if not last or not last.get("ts"):
        return "none yet"
    now = time.time() if now is None else now
    mi = last.get("distance_mi")
    where = f"about {mi:g} mi away" if isinstance(mi, (int, float)) else "out of range"
    when = datetime.fromtimestamp(float(last["ts"]))
    return f"{where}, {ago(now - float(last['ts']))} ({when:%a %H:%M})"


def sensor_line(state):
    if state is None:
        return paint("unknown", "33") + "  (no word from the service)"
    if state.get("sensor") == "ok":
        return f"{dot(True)} AS3935 answering"
    return (f"{dot(False)} " + paint("missing", "33")
            + "  (not answering on I2C; check the wires)")


def read_env_file(path=None):
    """KEY=VALUE lines from ~/.bluesky_env (comments and quotes allowed)."""
    path = ENV_FILE if path is None else path
    out = {}
    try:
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("export "):
                    key = key[7:].strip()
                out[key] = value.strip().strip("'\"")
    except OSError:
        pass
    return out


def _bool(value):
    v = (value or "").strip().lower()
    if v in ("1", "true", "yes", "y", "on"):
        return True
    if v in ("0", "false", "no", "n", "off"):
        return False
    return None


def bluesky_line(cred_path=None, env=None):
    """Practice or live, and whether the NOAA storm check is set up.
    Reads only the [app] dry_run switch, never the password."""
    cred_path = CREDENTIALS if cred_path is None else cred_path
    env = read_env_file() if env is None else env
    cfg = configparser.ConfigParser(interpolation=None)
    try:
        found = cfg.read(cred_path)
    except configparser.Error:
        found = []
    if not found:
        text = paint("not set up", "33") + "  (no ~/.bluesky_credentials.ini)"
    else:
        dry = True
        try:
            dry = cfg.getboolean("app", "dry_run", fallback=True)
        except (ValueError, configparser.Error):
            dry = True
        override = _bool(env.get("LIGHTNING_DRY_RUN"))
        if override is not None:
            dry = override
        text = (paint("practice", "33") + " (dry run, nothing is posted)") if dry \
            else paint("live", "32") + " (posts storms)"
    noaa_on = _bool(env.get("NOAA_ENABLED", "true")) is not False
    if noaa_on and not (env.get("NOAA_LAT") and env.get("NOAA_LON")):
        text += "   NOAA check: " + paint("no location set", "33")
    elif noaa_on:
        text += "   NOAA check: on"
    return text


def service_state(name):
    try:
        result = subprocess.run(["systemctl", "is-active", name],
                                capture_output=True, text=True, timeout=5)
        return result.stdout.strip() or "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def mqtt_peer():
    """The broker's address if this Pi holds an open MQTT link, else None."""
    try:
        out = subprocess.run(["ss", "-Htn", "state", "established",
                              f"( dport = :{MQTT_PORT} )"],
                             capture_output=True, text=True, timeout=5).stdout
    except (OSError, subprocess.TimeoutExpired):
        return None
    for line in out.splitlines():
        cols = line.split()
        if len(cols) >= 4:
            return cols[-1].rsplit(":", 1)[0].strip("[]")
    return None


def mqtt_text(state, fresh):
    info = (state or {}).get("mqtt") if fresh else None
    if isinstance(info, dict):
        if info.get("linked"):
            via = info.get("via") or "lan"
            return f"{dot(True)} mqtt {via}"
        why = info.get("why")
        return f"{dot(False)} mqtt " + (why if why else "not linked")
    peer = mqtt_peer()
    if peer:
        return f"{dot(True)} mqtt " + ("tailscale" if peer.startswith("100.") else "lan")
    return f"{dot(False)} mqtt not linked"


def wifi_line():
    try:
        with open(WIFI_STATUS) as f:
            st = json.load(f)
    except (OSError, ValueError):
        st = None
    if isinstance(st, dict):
        ssid = st.get("ssid") or "no WiFi"
        sig = st.get("signal")
        sig_txt = f" {sig}%" if isinstance(sig, (int, float)) else ""
        weak = isinstance(sig, (int, float)) and sig < 40
        net = "internet ok" if st.get("online") else "no internet"
        return f"{dot(st.get('online') and not weak)} {ssid}{sig_txt}  ({net})"
    try:  # keeper not installed: at least name the network
        ssid = subprocess.run(["iwgetid", "-r"], capture_output=True,
                              text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        ssid = ""
    return f"{dot(bool(ssid))} {ssid or 'unknown'}"


def pi_line():
    bits = []
    try:
        with open("/sys/class/thermal/thermal_zone0/temp") as f:
            temp = int(f.read().strip()) / 1000
        bits.append(paint(f"{temp:.0f} C", "31" if temp >= 75 else "0"))
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/uptime") as f:
            up = float(f.read().split()[0])
        days, rest = divmod(int(up), 86400)
        hours = rest // 3600
        bits.append(f"up {days} d {hours} h" if days else f"up {hours} h {rest % 3600 // 60} min")
    except (OSError, ValueError, IndexError):
        pass
    return "   ".join(bits)


def main():
    now = time.time()
    print(paint(f" LIGHTNING  {socket.gethostname()} @ {get_ip()} ", "30;43"))

    state = read_state()
    age = now - float(state.get("ts", 0)) if state else None
    fresh = state is not None and age is not None and age <= STALE_SECONDS
    service = service_state("lightning-bluesky")

    print(f"  Sensor:    {sensor_line(state if fresh else None)}")
    if fresh:
        today = state.get("strikes_today", 0)
        last = state.get("last_strike")
        storm = "   " + paint("storm going on", "33") if state.get("storm_active") else ""
        print(f"  Strikes:   {today} today{storm}")
        print(f"  Last:      {strike_text(last, now)}")
        noise = state.get("disturbers_last_hour", 0) + state.get("noise_last_hour", 0)
        if noise:
            print(f"  Static:    {noise} false alarms in the last hour (filtered out)")
    else:
        today, last = strikes_from_log(now=now)
        print(f"  Strikes:   {today} today (from the strike log)")
        print(f"  Last:      {strike_text(last, now)}")
        if state is not None and service == "active":
            print("  " + paint(f"The service last checked in {ago(age)}. Stuck?", "31"))
    print(f"  Bluesky:   {bluesky_line()}")

    states = []
    for unit, label in SERVICES:
        st = service if unit == "lightning-bluesky" else service_state(unit)
        if label == "tailscale" and st in ("inactive", "unknown"):
            continue  # not installed or not used: leave it out
        states.append(f"{dot(st == 'active')} {label} {st}")
    states.append(mqtt_text(state, fresh))
    print("  Services:  " + "   ".join(states))
    print(f"  WiFi:      {wifi_line()}")
    pi = pi_line()
    if pi:
        print(f"  Pi:        {pi}")


if __name__ == "__main__":
    main()
