"""Tests for lightning_status.py (the SSH login summary) and the state file
node_mqtt.py writes for it. Plain Python, no paho or sensor needed.
Run: python3 tests/test_lightning_status.py"""
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
import lightning_status as ls  # noqa: E402
import node_mqtt  # noqa: E402


def _tmp(name, text=""):
    p = os.path.join(tempfile.mkdtemp(), name)
    with open(p, "w") as f:
        f.write(text)
    return p


def _run(state=None, log_lines=(), services=None, cred=None, env=""):
    d = tempfile.mkdtemp()
    ls.STATE_FILE = os.path.join(d, "state.json")
    if state is not None:
        with open(ls.STATE_FILE, "w") as f:
            json.dump(state, f)
    ls.TELEMETRY = _tmp("telemetry.jsonl", "".join(json.dumps(x) + "\n" for x in log_lines))
    ls.CREDENTIALS = _tmp("cred.ini", cred) if cred is not None else os.path.join(d, "none.ini")
    ls.ENV_FILE = _tmp("env", env)
    ls.WIFI_STATUS = _tmp("wifi.json", json.dumps({"ssid": "GF-Home", "signal": 70, "online": True}))
    services = services or {}
    ls.service_state = lambda name: services.get(name, "active")
    ls.mqtt_peer = lambda: None
    out = io.StringIO()
    with redirect_stdout(out):
        ls.main()
    return out.getvalue()


def test_missing_sensor_is_shown_gently():
    now = int(time.time())
    out = _run({"sensor": "missing", "strikes_today": 0, "last_strike": None, "ts": now,
                "mqtt": {"linked": True, "via": "lan"}})
    assert "LIGHTNING" in out
    assert "missing" in out and "check the wires" in out
    assert "0 today" in out and "none yet" in out
    assert "mqtt lan" in out
    assert "GF-Home 70%" in out


def test_strike_from_state_file():
    now = int(time.time())
    out = _run({"sensor": "ok", "strikes_today": 3, "storm_active": True, "ts": now,
                "last_strike": {"ts": now - 600, "distance_mi": 6.2},
                "disturbers_last_hour": 2, "noise_last_hour": 1,
                "mqtt": {"linked": False, "why": "off"}})
    assert "AS3935 answering" in out
    assert "3 today" in out and "storm going on" in out
    assert "about 6.2 mi away, 10 min ago" in out
    assert "3 false alarms" in out
    assert "mqtt off" in out


def test_no_state_file_falls_back_to_strike_log():
    now = time.time()
    log = [{"event": "strike", "unix_ts": now - 7200, "distance_mi": 9.3},
           {"event": "strike", "unix_ts": now - 60, "distance_mi": 3.1},
           {"event": "strike", "unix_ts": now - 3 * 86400, "distance_mi": 1.0}]
    out = _run(None, log_lines=log, services={"lightning-bluesky": "inactive"})
    assert "unknown" in out
    assert "lightning inactive" in out
    # the old strike is from another day, so today depends on the time; the
    # newest one is always the last
    assert "about 3.1 mi away, 1 min ago" in out
    count, last = ls.strikes_from_log(ls.TELEMETRY, now=now)
    assert last["distance_mi"] == 3.1
    assert 1 <= count <= 2


def test_stale_state_file_is_flagged():
    out = _run({"sensor": "ok", "strikes_today": 9, "ts": int(time.time()) - 3600})
    assert "Stuck?" in out
    assert "9 today" not in out  # old numbers are not trusted


def test_bluesky_never_prints_the_password():
    cred = "[bluesky]\nhandle = someone.bsky.social\napp_password = SECRET-pw-123\n[app]\ndry_run = true\n"
    out = _run({"sensor": "ok", "ts": int(time.time())}, cred=cred)
    assert "practice" in out
    assert "SECRET" not in out
    assert "no location set" in out
    out = _run({"sensor": "ok", "ts": int(time.time())}, cred=cred.replace("true", "false"),
               env="NOAA_LAT=35.1\nexport NOAA_LON='-97.4'\n")
    assert "live" in out and "NOAA check: on" in out
    out = _run({"sensor": "ok", "ts": int(time.time())}, cred=cred.replace("true", "false"),
               env="LIGHTNING_DRY_RUN=1\nNOAA_ENABLED=false\n")
    assert "practice" in out and "NOAA" not in out


def test_no_credentials_file():
    out = _run({"sensor": "ok", "ts": int(time.time())})
    assert "not set up" in out


def test_node_mqtt_writes_state_file():
    p = os.path.join(tempfile.mkdtemp(), "lightning-node.json")
    assert node_mqtt.write_status_file({"sensor": "missing", "strikes_today": 0},
                                       {"linked": False, "why": "off"}, path=p)
    with open(p) as f:
        data = json.load(f)
    assert data["sensor"] == "missing" and data["mqtt"]["why"] == "off"
    assert abs(data["ts"] - time.time()) < 5
    assert not os.path.exists(p + ".tmp")
    # a folder that isn't there: no crash
    assert not node_mqtt.write_status_file({}, {}, path="/nonexistent/dir/x.json")
    assert not node_mqtt.write_status_file({}, {}, path="")


def test_checkin_writes_file_without_mqtt():
    p = os.path.join(tempfile.mkdtemp(), "lightning-node.json")
    node_mqtt.STATUS_FILE = p
    c = node_mqtt.CheckIn(lambda: {"sensor": "ok", "strikes_today": 1})
    c._write_local()
    with open(p) as f:
        data = json.load(f)
    assert data["sensor"] == "ok"
    assert data["mqtt"]["linked"] is False


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
