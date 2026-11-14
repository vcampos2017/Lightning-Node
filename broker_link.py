"""Find Chatty's message board (MQTT broker), whichever WiFi she is on.

The same file lives in every node's repo (rain, greenhouse, lightning, word
clock). Keep the copies the same.

Why: Chatty's WiFi keeper can move her from GF-Home to the AVC-2G extender
(or back). Her home address changes, and "chatty-node.local" often doesn't
make it across the extender, so the nodes lost her (2026-11-13, 4:21 PM).

What it does instead of a single broker address:
  1. Tries a list of addresses, one after another, until one answers:
       - the address that worked last time
       - her home addresses she told us about (one per WiFi she has used)
       - MQTT_HOST (default chatty-node.local)
       - her Tailscale address (works on any WiFi, as long as both Pis have
         internet and Tailscale; default 100.69.130.79)
  2. Listens to Chatty's retained note on `chatty/hub`:
       {"host": "chatty-node", "wifi": "AVC-2G", "lan": ["192.168.1.77"],
        "tailscale": "100.69.130.79", "known": {"GF-Home": "192.168.1.50", ...}}
     and keeps it in a small file, so after a restart it still knows where
     she might be. That's how a node knows which WiFi Chatty is on.
  3. Follows her: when it had to go the long way round (Tailscale) and her
     note shows a home address that answers, it moves back to that one
     (checked every 5 minutes). Home addresses keep working without internet.
  4. Remembers shaky ways: a connection that drops within 10 minutes marks
     that address shaky for 30 minutes (doubling each time, up to 6 hours,
     forgotten after it holds for an hour). Shaky addresses are tried last and
     never followed back to. On 2026-11-13 the way across the AVC-2G extender
     opened fine but dropped every 1-5 minutes, and following her "home" over
     it every 5 minutes kept the rain node blinking.
  5. Last resort, when nothing on the list answers for a few rounds: knocks
     on port 1883 of every address on this node's own home network (at most
     every 10 minutes) and keeps one only if Chatty's note shows up there.

It only ever finds the board. It sends nothing to Chatty but what the node
already posted, and Chatty still never sends the nodes commands.

Settings (environment):
    MQTT_HOST        her name or address (default chatty-node.local)
    MQTT_HOSTS       a full list to use instead, comma-separated, tried in order
    MQTT_PORT        default 1883
    MQTT_TAILSCALE   her Tailscale address (default 100.69.130.79, "" = don't use)
    MQTT_SCAN=0      never knock on the home network
    MQTT_HUB_FILE    where her note is kept
                     (default ~/.local/state/chatty-hub.json)

Use it in place of connect_async + loop_start:

    client = mqtt.Client(...)            # will, callbacks as before
    link = broker_link.BrokerLink(client)
    link.start()                         # runs in the background
    link.host, link.via                  # e.g. "100.69.130.79", "tailscale"
"""
from __future__ import annotations

import ipaddress
import json
import os
import socket
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, List, Optional

HUB_TOPIC = "chatty/hub"
PORT = int(os.getenv("MQTT_PORT", "1883"))
DEFAULT_TAILSCALE = "100.69.130.79"
FOLLOW_EVERY = 300        # how often to look for a shorter way to her, seconds
SCAN_AFTER_ROUNDS = 3     # full rounds with no answer before knocking around
SCAN_GAP = 600            # at most one knock-around per 10 minutes
SCAN_PROOF_SECONDS = 15   # a knocked-on board must show Chatty's note in this time
SHAKY_WITHIN = 600        # a connection that drops sooner than this marks its way shaky
SHAKY_FIRST = 1800        # first time out for a shaky way, seconds (doubles each time)
SHAKY_MAX = 6 * 3600
STEADY_AFTER = 3600       # a connection that holds this long clears its way's record
TAILNET = ipaddress.ip_network("100.64.0.0/10")


def _env_list(name: str) -> List[str]:
    return [h.strip() for h in os.getenv(name, "").split(",") if h.strip()]


def kind_of(address: str) -> str:
    """'tailscale' for 100.64.0.0/10, else 'lan'."""
    try:
        return "tailscale" if ipaddress.ip_address(address) in TAILNET else "lan"
    except ValueError:
        return "lan"


def _is_ip(text: str) -> bool:
    try:
        ipaddress.ip_address(text)
        return True
    except ValueError:
        return False


def tcp_open(host: str, port: int = PORT, timeout: float = 1.0) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def own_lan_addresses() -> List[str]:
    """This Pi's own home-network IPv4 addresses (no loopback, no Tailscale)."""
    found = []
    try:
        import subprocess
        out = subprocess.run(["ip", "-4", "-o", "addr", "show"], capture_output=True,
                             text=True, timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if "inet" in parts:
                found.append(parts[parts.index("inet") + 1])
    except Exception:
        pass
    keep = []
    for cidr in found:
        try:
            iface = ipaddress.ip_interface(cidr)
        except ValueError:
            continue
        if iface.ip.is_loopback or iface.ip in TAILNET or not iface.ip.is_private:
            continue
        keep.append(str(iface))
    return keep


def scan_home(port: int = PORT, timeout: float = 0.3, workers: int = 32,
              mine: Optional[List[str]] = None) -> List[str]:
    """Addresses on this Pi's own /24 home network with port 1883 open."""
    targets = []
    for cidr in (own_lan_addresses() if mine is None else mine):
        iface = ipaddress.ip_interface(cidr)
        net = iface.network if iface.network.prefixlen >= 24 else \
            ipaddress.ip_network(f"{iface.ip}/24", strict=False)
        targets += [str(h) for h in net.hosts() if h != iface.ip]
    if not targets:
        return []
    with ThreadPoolExecutor(max_workers=workers) as pool:
        hits = pool.map(lambda h: h if tcp_open(h, port, timeout) else None, targets)
    return [h for h in hits if h]


class HubNote:
    """Chatty's last note about where she is, kept on disk across restarts."""

    def __init__(self, path: Optional[str] = None):
        self.path = os.path.expanduser(path or os.getenv(
            "MQTT_HUB_FILE", "~/.local/state/chatty-hub.json"))
        self.note: dict = {}
        self.seen = 0.0           # when we last heard it (this run)
        self.load()

    def load(self) -> None:
        try:
            with open(self.path) as f:
                note = json.load(f)
            if isinstance(note, dict):
                self.note = note
        except (OSError, ValueError):
            pass

    def update(self, payload: bytes, now: float) -> bool:
        try:
            note = json.loads(payload.decode("utf-8", "ignore") or "{}")
        except ValueError:
            return False
        if not isinstance(note, dict):
            return False
        # remember every home address she has told us, even from older notes
        known = dict(self.note.get("known") or {})
        known.update(note.get("known") or {})
        note["known"] = {str(k)[:40]: str(v)[:45] for k, v in known.items()
                         if isinstance(v, str) and _is_ip(v)}
        changed = {k: note.get(k) for k in ("wifi", "lan", "tailscale")} != \
                  {k: self.note.get(k) for k in ("wifi", "lan", "tailscale")}
        self.note, self.seen = note, now
        self.save()
        return changed

    def save(self) -> None:
        try:
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.note, f)
            os.replace(tmp, self.path)
        except OSError:
            pass

    @property
    def wifi(self) -> str:
        return str(self.note.get("wifi") or "")

    def lan_now(self) -> List[str]:
        return [a for a in (self.note.get("lan") or []) if isinstance(a, str) and _is_ip(a)]

    def lan_all(self) -> List[str]:
        """Where she is now first, then where she was on her other WiFi."""
        out = self.lan_now()
        for a in (self.note.get("known") or {}).values():
            if a not in out:
                out.append(a)
        return out

    def tailscale(self) -> str:
        ts = self.note.get("tailscale")
        return ts if isinstance(ts, str) and _is_ip(ts) else ""


class BrokerLink:
    """Keeps a paho client connected to Chatty's broker, trying every way to her.
    Wraps the client's own on_connect / on_disconnect / on_message (set them
    before start()). Never raises out of its thread."""

    def __init__(self, client, port: int = PORT, keepalive: int = 30,
                 hosts: Optional[List[str]] = None, tailscale: Optional[str] = None,
                 scan: Optional[bool] = None, hub: Optional[HubNote] = None,
                 clock: Callable[[], float] = time.time, log: Callable[[str], None] = None):
        self.client = client
        self.port = port
        self.keepalive = keepalive
        self.fixed = _env_list("MQTT_HOSTS") if hosts is None else list(hosts)
        if not self.fixed:
            self.fixed = [os.getenv("MQTT_HOST", "chatty-node.local").strip() or "chatty-node.local"]
        self.ts_default = (os.getenv("MQTT_TAILSCALE", DEFAULT_TAILSCALE)
                           if tailscale is None else tailscale).strip()
        self.scan_ok = (os.getenv("MQTT_SCAN", "1") != "0") if scan is None else scan
        self.hub = hub or HubNote()
        self.clock = clock
        self.log = log or (lambda text: print("Node network: " + text, flush=True))
        self.connected = False
        self.host = ""            # the address we're on (or trying)
        self.via = ""             # "lan" or "tailscale"
        self.good = ""            # the last address that worked
        self.scanned: List[str] = []
        self.unproven = False     # on a knocked-on board, waiting for her note
        self.since = 0.0          # when this connection came up
        self.failed_rounds = 0
        self.last_scan = 0.0
        self.last_follow = 0.0
        self.stopped = False
        self.addr = ""            # the IP address the current host resolved to
        self.shaky_until: dict = {}   # address or name -> time it may be trusted again
        self.shaky_for: dict = {}     # address or name -> its current time out, seconds
        self._moving = False      # a drop we made on purpose (following her home)
        self._wrap()

    # ----- which addresses, in what order -----
    def candidates(self) -> List[str]:
        out: List[str] = []

        def add(a):
            if a and a not in out:
                out.append(a)
        add(self.good)
        for a in self.hub.lan_all():
            add(a)
        for a in self.fixed:
            add(a)
        add(self.hub.tailscale())
        add(self.ts_default)
        for a in self.scanned:
            add(a)
        # shaky ways last: still tried, but only when nothing steadier answers
        return [a for a in out if not self.is_shaky(a)] + [a for a in out if self.is_shaky(a)]

    # ----- shaky ways -----
    def is_shaky(self, *names: str) -> bool:
        now = self.clock()
        return any(n and self.shaky_until.get(n, 0) > now for n in names)

    def _lost(self) -> None:
        """The connection just dropped. Was it a shaky way?"""
        if not self.since:
            return
        now = self.clock()
        held = now - self.since
        names = list(dict.fromkeys(n for n in (self.host, self.addr) if n))
        if self._moving:
            self._moving = False
        elif held < SHAKY_WITHIN:
            for n in names:
                wait = min(SHAKY_MAX, self.shaky_for.get(n, SHAKY_FIRST / 2) * 2)
                self.shaky_for[n] = wait
                self.shaky_until[n] = now + wait
            self.log(f"the way to {self.host} dropped after {int(held)} s; "
                     f"trying other ways first for {int(self.shaky_for[names[0]] // 60)} min")
        elif held >= STEADY_AFTER:
            for n in names:
                self.shaky_for.pop(n, None)
        self.since = 0.0

    # ----- paho callbacks, wrapped -----
    def _wrap(self) -> None:
        c = self.client
        user_connect, user_disconnect, user_message = c.on_connect, c.on_disconnect, c.on_message

        def on_connect(client, userdata, flags, *rest):
            rc = rest[0] if rest else 0
            failed = getattr(rc, "is_failure", None)
            ok = (not failed) if failed is not None else (rc == 0)
            if not ok:
                self.log(f"{self.host} said no ({rc})")
                return
            self.connected = True
            self.since = self.clock()
            if not self.unproven:
                self.good = self.host
            self.failed_rounds = 0
            try:
                client.subscribe(HUB_TOPIC, qos=1)
            except Exception:
                pass
            self.log(f"connected to {self.host}:{self.port} ({self.via})")
            if user_connect:
                user_connect(client, userdata, flags, *rest)

        def on_disconnect(*args):
            was = self.connected
            self.connected = False
            if was:
                self._lost()
            if user_disconnect and was:
                user_disconnect(*args)

        def on_message(client, userdata, msg):
            if getattr(msg, "topic", "") == HUB_TOPIC:
                if self.hub.update(msg.payload, self.clock()):
                    where = ", ".join(self.hub.lan_now()) or "no home address"
                    self.log(f"Chatty is on {self.hub.wifi or 'an unknown network'} ({where})")
                if self.unproven:
                    self.unproven = False
                    self.good = self.host
                    self.log(f"{self.host} is Chatty's board")
                return
            if user_message:
                user_message(client, userdata, msg)

        c.on_connect, c.on_disconnect, c.on_message = on_connect, on_disconnect, on_message

    # ----- connecting -----
    def _try(self, host: str) -> bool:
        """Start a connection to one address. True if the socket opened (the
        broker's yes comes a moment later, in on_connect)."""
        self.host = host
        try:
            addr = socket.getaddrinfo(host, self.port, socket.AF_INET, socket.SOCK_STREAM)[0][4][0]
        except OSError:
            return False
        self.via = kind_of(addr)
        self.addr = addr
        try:
            self.client.connect(host, self.port, keepalive=self.keepalive)
            return True
        except Exception:
            return False

    def _settle(self, seconds: float = 6.0) -> bool:
        """Let paho read the broker's answer. True once on_connect said yes."""
        end = time.monotonic() + seconds
        while time.monotonic() < end and not self.connected:
            if self.client.loop(timeout=0.5) != 0:
                return False
        return self.connected

    def connect_round(self) -> bool:
        """Try each address once. True when connected."""
        steady_left = [h for h in self.candidates() if not self.is_shaky(h)]
        for host in self.candidates():
            if self.stopped:
                return False
            steady_left = [h for h in steady_left if h != host]
            self.unproven = host in self.scanned and host not in self.hub.lan_all()
            opened = self._try(host)
            if opened and steady_left and self.is_shaky(self.addr) and not self.is_shaky(host):
                # a name (chatty-node.local) that leads to a shaky address: try the rest first
                self._drop()
                continue
            if opened and self._settle():
                return True
            self._drop()
        self.failed_rounds += 1
        if self.failed_rounds == 1 or self.failed_rounds % 10 == 0:
            self.log("can't reach Chatty at " + ", ".join(self.candidates()) + "; will keep trying")
        return False

    def _drop(self) -> None:
        try:
            self.client.disconnect()
        except Exception:
            pass
        self.connected = False

    def maybe_scan(self) -> None:
        now = self.clock()
        if not self.scan_ok or self.failed_rounds < SCAN_AFTER_ROUNDS or now - self.last_scan < SCAN_GAP:
            return
        self.last_scan = now
        self.scanned = [h for h in scan_home(self.port) if h not in self.candidates()][:4]
        if self.scanned:
            self.log("knocked around the home network, found a board at " + ", ".join(self.scanned))

    def maybe_follow(self) -> None:
        """On the long way (Tailscale)? Move to her home address once it answers."""
        now = self.clock()
        if not self.connected or self.via != "tailscale" or now - self.last_follow < FOLLOW_EVERY:
            return
        self.last_follow = now
        for a in self.hub.lan_now():
            if self.is_shaky(a):
                continue
            if tcp_open(a, self.port):
                self.log(f"Chatty answers at home again ({a}), moving over")
                self._moving = True
                self._drop()
                self._lost()
                self.good = a
                return

    def check_proof(self) -> None:
        if self.connected and self.unproven and self.clock() - self.since > SCAN_PROOF_SECONDS:
            self.log(f"{self.host} isn't Chatty's board, forgetting it")
            self.scanned = [h for h in self.scanned if h != self.host]
            self._drop()

    def run(self) -> None:
        backoff = 2.0
        while not self.stopped:
            try:
                if not self.connected:
                    if self.connect_round():
                        backoff = 2.0
                    else:
                        self.maybe_scan()
                        time.sleep(backoff)
                        backoff = min(30.0, backoff * 2)
                    continue
                if self.client.loop(timeout=1.0) != 0:
                    if self.connected:
                        self.connected = False
                        self._lost()
                    continue
                self.check_proof()
                self.maybe_follow()
            except Exception as e:
                self.log("link trouble: " + repr(e))
                self.connected = False
                time.sleep(5)

    def start(self) -> threading.Thread:
        t = threading.Thread(target=self.run, daemon=True, name="broker-link")
        t.start()
        return t

    def info(self) -> dict:
        """For the node's heartbeat: how it reached her and which WiFi she's on."""
        if not self.connected:
            return {"via": "", "broker": "", "chatty_wifi": self.hub.wifi}
        return {"via": self.via, "broker": self.host, "chatty_wifi": self.hub.wifi}
