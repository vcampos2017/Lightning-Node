"""Check in with Chatty over MQTT (the node network).

The lightning node joins the same network as the rain and greenhouse nodes.
Once a minute it posts a heartbeat and its latest state to Chatty's broker:

    chatty/nodes/lightning/status  {"state": "online", "every": 60, "ts": ...,
                                    "title": "Lightning node", "sensor": "ok"}
    chatty/nodes/lightning/data    {"sensor": "ok", "strikes_today": 0,
                                    "last_strike": {...} or null,
                                    "disturbers_last_hour": 0, ...}

"sensor" is "ok" when the AS3935 answers on I2C and "missing" when it
doesn't (unplugged, or a bad wire). The node stays online either way, so
Chatty can tell "the Pi is up but the sensor is off" apart from "the Pi is
down". A strike goes out right away instead of waiting for the next minute.

Both topics are retained. If the Pi drops off WiFi or the service stops, the
broker posts {"state": "offline"} for it (the last will) and Chatty shows
"Lightning node went quiet".

If paho-mqtt is missing or the broker can't be reached, lightning detection
and Bluesky posting carry on as before.

Finding Chatty: broker_link.py tries her home address, chatty-node.local and
her Tailscale address, learns which WiFi she's on from her `chatty/hub` note,
and follows her when she moves. The heartbeat says how it reached her
("via": "lan" or "tailscale", "chatty_wifi").

Settings (environment variables, e.g. in ~/.bluesky_env):
    MQTT_HOST        broker address (default chatty-node.local, Chatty's Pi)
    MQTT_PORT        default 1883
    NODE_NAME        default "lightning"
    MQTT_HOSTS, MQTT_TAILSCALE, MQTT_SCAN: see broker_link.py
    LIGHTNING_MQTT=0 turn this off
"""
from __future__ import annotations

import json
import os
import socket
import threading
import time
from typing import Any, Callable, Optional

ENABLED = os.getenv("LIGHTNING_MQTT", "1").strip() != "0"
PORT = int(os.getenv("MQTT_PORT", "1883"))
NAME = os.getenv("NODE_NAME", "lightning").strip() or "lightning"
TITLE = "Lightning node"
EVERY = 60  # seconds between heartbeats

STATUS = f"chatty/nodes/{NAME}/status"
DATA = f"chatty/nodes/{NAME}/data"

StateProvider = Callable[[], dict]


def status_payload(state: dict[str, Any], online: str = "online",
                   link_info: Optional[dict] = None) -> str:
    body = {
        "state": online,
        "every": EVERY,
        "ts": int(time.time()),
        "title": TITLE,
        "host": socket.gethostname(),
        "sensor": state.get("sensor", "unknown"),
    }
    body.update(link_info or {})
    return json.dumps(body)


def data_payload(state: dict[str, Any]) -> str:
    body = dict(state)
    body["ts"] = int(time.time())
    return json.dumps(body)


class CheckIn:
    def __init__(self, state_provider: StateProvider):
        self.state_provider = state_provider
        self.client = None
        self.connected = False
        self.link = None
        self._wake = threading.Event()

    # -- paho callbacks --
    def _on_connect(self, client, userdata, flags, *rest):
        self.connected = True
        self._publish_all()

    def _on_disconnect(self, *args):
        if self.connected:
            print("Node network: lost the broker, retrying", flush=True)
        self.connected = False

    def _state(self) -> Optional[dict]:
        try:
            return self.state_provider()
        except Exception as e:
            print("Node network: couldn't read state:", repr(e), flush=True)
            return None

    def _publish_all(self) -> None:
        state = self._state()
        if state is None or self.client is None:
            return
        self.client.publish(STATUS, status_payload(state, link_info=self.link.info() if self.link else None), qos=1, retain=True)
        self.client.publish(DATA, data_payload(state), qos=1, retain=True)

    def publish_now(self) -> None:
        """Send the latest state right away (e.g. after a strike)."""
        self._wake.set()

    def _make_client(self, mqtt):
        cid = f"{NAME}-{socket.gethostname()}"
        try:
            client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cid)
        except AttributeError:  # paho-mqtt 1.x
            client = mqtt.Client(client_id=cid)
        client.will_set(STATUS, json.dumps({"state": "offline"}), qos=1, retain=True)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        return client

    def _loop(self) -> None:
        while True:
            self._wake.wait(EVERY)
            self._wake.clear()
            if self.connected:
                self._publish_all()

    def start(self) -> bool:
        if not ENABLED:
            print("Node network: off (LIGHTNING_MQTT=0)", flush=True)
            return False
        try:
            import paho.mqtt.client as mqtt
        except ImportError:
            print("Node network: paho-mqtt not installed; skipping "
                  "(pip install paho-mqtt in the venv)", flush=True)
            return False
        self.client = self._make_client(mqtt)
        import broker_link
        self.link = broker_link.BrokerLink(self.client, port=PORT, keepalive=60)
        self.link.start()
        threading.Thread(target=self._loop, name="node-mqtt", daemon=True).start()
        return True
