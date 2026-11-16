"""Say goodbye to Chatty before this node powers down or restarts.

The same file lives in every node's repo (rain, greenhouse, lightning, word
clock). Keep the copies the same.

Why: without it, a node that is shut down or restarted on purpose looks just
like one that lost power or WiFi. The broker posts its last will, and Chatty
says "Rain node went quiet" (and later buzzes the phone) for a planned reboot.

How: systemd stops the node's service with SIGTERM on `sudo reboot`,
`sudo shutdown`, `systemctl restart` and `systemctl stop`. on_stop() catches
it, posts (retained) on chatty/nodes/<name>/status

    {"state": "offline", "clean": true, "reason": "reboot", "ts": ...,
     "every": 60, "title": "Rain node", "back_within": 10800}

("reason": reboot / shutdown / restart (only this service) / stop;
"back_within" seconds only when you asked for it, see below) and then leaves
the broker properly, so the broker does NOT post the last will. A crash, a
power cut or a WiFi drop still ends with the last will, exactly as before.

Chatty then waits quietly: 10 minutes after a reboot or restart, an hour after
a power-down or stop (her CHATTY_MESH_* settings), or "back_within". Back in
time: no notice at all. Not back: "Rain node went quiet (shut down properly at
3:10 PM and hasn't come back)", and the phone later, as before.

Going to be away longer (moving it, fixing a wire)? Tell her first:

    python3 node_goodbye.py away 180      # minutes; counts for the next 2 hours
    sudo shutdown -h now

`python3 node_goodbye.py` shows what it would say right now.

So the goodbye still gets out at shutdown, the service should stop before the
network and Tailscale do. Once per node (adds a small systemd drop-in):

    sudo python3 node_goodbye.py install <service>    # e.g. rain-mqtt

Settings (environment):
    NODE_GOODBYE=0       don't say goodbye (Chatty treats a stop like a drop)
    NODE_BACK_MINUTES    always ask Chatty to wait this long (default: her own waits)
    NODE_AWAY_FILE       where `away` is kept (default /dev/shm/node-away.json,
                         gone after a restart)
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import threading
import time
from typing import Callable, Optional

ENABLED = os.getenv("NODE_GOODBYE", "1").strip() != "0"
AWAY_FILE = os.getenv("NODE_AWAY_FILE", "/dev/shm/node-away.json")
AWAY_GOOD_FOR = 2 * 3600     # an `away` note counts for a shutdown within this time
MAX_MINUTES = 24 * 60        # Chatty caps it at her own maximum anyway
DROP_IN = """[Unit]
# Stop before the network and Tailscale at shutdown, so the goodbye to Chatty
# still gets out (node_goodbye.py).
After=network-online.target NetworkManager.service tailscaled.service
Wants=network-online.target

[Service]
TimeoutStopSec=20
"""


# ----- what the Pi is doing -----

def reason_from_jobs(jobs: str) -> str:
    """Read `systemctl list-jobs --no-legend` (JOB UNIT TYPE STATE per line)."""
    units, types = set(), set()
    for line in jobs.splitlines():
        parts = line.split()
        if len(parts) >= 3:
            units.add(parts[1])
            types.add(parts[2])
    if units & {"reboot.target", "kexec.target", "soft-reboot.target"}:
        return "reboot"
    if units & {"poweroff.target", "halt.target", "shutdown.target"}:
        return "shutdown"
    if "restart" in types:
        return "restart"
    return "stop"


def stop_reason(run=subprocess.run) -> str:
    """'reboot', 'shutdown', 'restart' or 'stop'. Asks systemd; no root needed."""
    try:
        out = run(["systemctl", "list-jobs", "--no-legend", "--no-pager"],
                  capture_output=True, text=True, timeout=2).stdout
    except Exception:
        return "stop"
    return reason_from_jobs(out or "")


# ----- "I'll be away a while" -----

def set_away(minutes: float, path: str = None, now: float = None) -> dict:
    note = {"minutes": max(1.0, min(float(minutes), MAX_MINUTES)),
            "set": time.time() if now is None else now}
    with open(path or AWAY_FILE, "w", encoding="utf-8") as f:
        json.dump(note, f)
    return note


def away_minutes(path: str = None, now: float = None) -> Optional[float]:
    """Minutes asked for with `away` (if it's recent), else NODE_BACK_MINUTES, else None."""
    now = time.time() if now is None else now
    try:
        with open(path or AWAY_FILE, encoding="utf-8") as f:
            note = json.load(f)
        if 0 <= now - float(note["set"]) <= AWAY_GOOD_FOR:
            return float(note["minutes"])
    except (OSError, ValueError, KeyError, TypeError):
        pass
    try:
        fixed = float(os.getenv("NODE_BACK_MINUTES", "") or 0)
    except ValueError:
        fixed = 0.0
    return fixed if fixed > 0 else None


def message(title: str, every: int = 60, reason: str = None, now: float = None,
            away: Optional[float] = -1.0) -> dict:
    """The goodbye itself. away=-1 means: look it up (away_minutes())."""
    now = time.time() if now is None else now
    body = {"state": "offline", "clean": True, "reason": reason or stop_reason(),
            "ts": int(now), "every": every, "title": title}
    minutes = away_minutes(now=now) if away == -1.0 else away
    if minutes:
        body["back_within"] = int(minutes * 60)
    return body


# ----- saying it -----

def leave(client, topic: str, body: dict, link=None, connected: Optional[bool] = None,
          timeout: float = 3.0) -> bool:
    """Post the goodbye (retained) and leave the broker properly, so it doesn't
    post the last will. `link` is the node's broker_link.BrokerLink, if it uses
    one (its thread drives the connection: stop it first so it doesn't come
    back). Without a link the client runs its own loop (loop_start). Never
    raises. True if the goodbye got out."""
    if client is None:
        return False
    try:
        if link is not None:
            link.stopped = True
            for t in threading.enumerate():
                if t.name == "broker-link" and t is not threading.current_thread():
                    t.join(2.5)
            connected = link.connected if connected is None else connected
        if connected is False:
            return False
        info = client.publish(topic, json.dumps(body), qos=1, retain=True)
        end = time.monotonic() + timeout
        if link is not None:
            while not info.is_published() and time.monotonic() < end:
                if client.loop(timeout=0.2) != 0:
                    break
        else:
            try:
                info.wait_for_publish(timeout)
            except TypeError:        # old paho: no timeout
                while not info.is_published() and time.monotonic() < end:
                    time.sleep(0.1)
        sent = info.is_published()
        if link is not None:
            link.since = 0.0         # a leave on purpose, not a shaky way
        client.disconnect()
        if link is not None:
            client.loop(timeout=0.2)
            link.connected = False
        else:
            client.loop_stop()
        print(f"Node network: said goodbye to Chatty ({body.get('reason')})"
              if sent else "Node network: goodbye may not have reached Chatty", flush=True)
        return sent
    except Exception as e:
        print("Node network: goodbye failed:", repr(e), flush=True)
        return False


def on_stop(goodbye: Callable[[], object], then: Callable = None) -> None:
    """On SIGTERM (systemd stopping the service): say goodbye once, then `then`
    (signum, frame) or exit. Call from the main thread."""
    done = []

    def handler(signum, frame):
        if not done:
            done.append(1)
            if ENABLED:
                try:
                    goodbye()
                except Exception as e:
                    print("Node network: goodbye failed:", repr(e), flush=True)
        if then is not None:
            then(signum, frame)
        raise SystemExit(0)

    try:
        signal.signal(signal.SIGTERM, handler)
    except ValueError:      # not the main thread: keep the old behavior
        print("Node network: can't listen for shutdown here (not the main thread)", flush=True)


# ----- once per node -----

def install(service: str, root: str = "/etc/systemd/system", reload=subprocess.run) -> str:
    """Order the service to stop before the network and Tailscale (systemd drop-in)."""
    name = service if service.endswith(".service") else service + ".service"
    if "/" in name or name.startswith("."):
        raise ValueError("just the service name, e.g. rain-mqtt")
    folder = os.path.join(root, name + ".d")
    os.makedirs(folder, exist_ok=True)
    path = os.path.join(folder, "goodbye.conf")
    with open(path, "w", encoding="utf-8") as f:
        f.write(DROP_IN)
    reload(["systemctl", "daemon-reload"], check=False)
    return path


def _main(argv) -> int:
    if argv[:1] == ["away"] and len(argv) == 2:
        try:
            note = set_away(float(argv[1]))
        except (ValueError, OSError) as e:
            print("Couldn't note that:", e)
            return 1
        print(f"OK: if this node shuts down in the next {AWAY_GOOD_FOR // 3600} hours, "
              f"Chatty will wait {note['minutes']:.0f} minutes before she worries.")
        return 0
    if argv[:1] == ["install"] and len(argv) == 2:
        try:
            path = install(argv[1])
        except (ValueError, OSError) as e:
            print("Couldn't install:", e, "(run it with sudo)")
            return 1
        print("Installed", path)
        print(f"It takes effect now. Restart the service once to be sure: "
              f"sudo systemctl restart {argv[1]}")
        return 0
    if argv:
        print(__doc__)
        return 1
    print("Goodbye on:", "yes" if ENABLED else "no (NODE_GOODBYE=0)")
    print("It would say:", json.dumps(message("This node")))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
