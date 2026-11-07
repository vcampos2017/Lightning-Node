#!/usr/bin/env python3
"""
WiFi keeper for a Chatty network node: stay on the best WiFi this Pi knows
the password to, the same way Chatty does.

Once a minute it checks that the Pi can really reach the internet (not just
"connected": a small web check, like the one NetworkManager itself uses).

  - No internet twice in a row: rescan and try the saved networks, strongest
    signal first, until one really works. The network that let it down is
    skipped for a while (NODE_WIFI_BAD_MINUTES) so it doesn't hop straight
    back onto it.
  - Only one saved network, or none of the others work: it reconnects to the
    usual one, and if that still doesn't help after a few tries it switches
    the WiFi off and on again (that un-sticks a Pi Zero or Pi 3 that has
    "connected" but stopped passing traffic).
  - Online but on a weak signal, while a saved network nearby is much
    stronger (3 checks in a row): it moves over, and moves back if the new
    one has no internet. At most once every NODE_WIFI_ROAM_MINUTES.
  - With a network cable plugged in and working, it leaves WiFi alone.
  - At start it turns WiFi power saving off, which makes Pi WiFi drop out.
  - Last resort: offline for NODE_WIFI_REBOOT_MINUTES (30) in a row, and the
    home router doesn't answer either, it restarts the Pi. If the router
    answers, the internet itself is down and a restart wouldn't help, so it
    waits. At most NODE_WIFI_REBOOTS_PER_DAY (3) restarts a day, so it can
    never get stuck restarting over and over.

Works with NetworkManager (Raspberry Pi OS Bookworm / Trixie, nmcli) and with
the older wpa_supplicant + dhcpcd setup (Bullseye and before, wpa_cli). It
picks whichever one is running.

Passwords: it only uses networks that are already saved (desktop WiFi menu,
`nmcli`, raspi-config or wpa_supplicant.conf). It never reads, prints or
logs a password: nmcli is never asked for secrets, and wpa_supplicant.conf
is never opened.

Everything goes to the journal: journalctl -u node-wifi -f
Current state: /run/node-wifi.json

Settings (optional, in /etc/node-wifi.env):
    NODE_WIFI=0                     turn this off
    NODE_WIFI_EVERY                 seconds between checks (default 60)
    NODE_WIFI_FAILS                 failed checks in a row before acting (default 2)
    NODE_WIFI_BAD_MINUTES           skip a network that let it down this long (default 30)
    NODE_WIFI_ROAM=0                never leave a working network for a stronger one
    NODE_WIFI_WEAK                  signal percent counted as weak (default 40)
    NODE_WIFI_MARGIN                how much stronger the other one must be (default 25)
    NODE_WIFI_ROAM_MINUTES          least time between moves to a stronger one (default 15)
    NODE_WIFI_RESET_AFTER           failed recoveries before WiFi off/on (default 3)
    NODE_WIFI_POWERSAVE=1           leave WiFi power saving as it is
    NODE_WIFI_REBOOT_MINUTES        offline this long before a restart (default 30, 0 = never)
    NODE_WIFI_REBOOTS_PER_DAY       most restarts in 24 hours (default 3)
    NODE_WIFI_CHECK_URLS            comma list of check pages (defaults below)
    NODE_WIFI_AVOID                 comma list of saved network names (SSIDs) never to pick

See what it sees (no changes):    sudo python3 wifi_keeper.py --status
"""
from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional


def _num(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


ENABLED = os.getenv("NODE_WIFI", "1") != "0"
EVERY = max(15.0, _num("NODE_WIFI_EVERY", 60))
FAILS = max(1, int(_num("NODE_WIFI_FAILS", 2)))
BAD_SECONDS = _num("NODE_WIFI_BAD_MINUTES", 30) * 60
ROAM = os.getenv("NODE_WIFI_ROAM", "1") != "0"
WEAK = _num("NODE_WIFI_WEAK", 40)
MARGIN = _num("NODE_WIFI_MARGIN", 25)
ROAM_SECONDS = _num("NODE_WIFI_ROAM_MINUTES", 15) * 60
RESET_AFTER = max(1, int(_num("NODE_WIFI_RESET_AFTER", 3)))
POWERSAVE_OFF = os.getenv("NODE_WIFI_POWERSAVE", "0") != "1"
AVOID = {s.strip() for s in os.getenv("NODE_WIFI_AVOID", "").split(",") if s.strip()}
ROAM_CHECKS = 3             # weak checks in a row before moving to a stronger one
RESCAN_SECONDS = 300        # fresh scan at most this often while things are fine
RETRY_SECONDS = 300         # after trying everything and failing, wait this long
STATUS_FILE = os.getenv("NODE_WIFI_STATUS_FILE", "/run/node-wifi.json")
REBOOT_SECONDS = _num("NODE_WIFI_REBOOT_MINUTES", 30) * 60
REBOOTS_PER_DAY = max(0, int(_num("NODE_WIFI_REBOOTS_PER_DAY", 3)))
# Kept on the SD card (not /run) so the count survives the restart itself.
REBOOT_FILE = os.getenv("NODE_WIFI_REBOOT_FILE", "/var/lib/node-wifi/reboots.json")

# The same kind of check NetworkManager and phones use. Both are plain
# "are you there?" pages; nothing about the node is sent.
CHECKS = [
    ("http://nmcheck.gnome.org/check_network_status.txt", 200, b"NetworkManager is online"),
    ("http://cp.cloudflare.com/generate_204", 204, b""),
]
_urls = [u.strip() for u in os.getenv("NODE_WIFI_CHECK_URLS", "").split(",") if u.strip()]
if _urls:
    CHECKS = [(u, None, b"") for u in _urls]


def log(msg: str) -> None:
    print(msg, flush=True)


def internet_ok(timeout: float = 6.0) -> bool:
    """True if any check page answers the way it should. A captive portal
    (a login page) or a dead connection doesn't count."""
    for url, want_status, want_body in CHECKS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "node-wifi-check"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(200)
                if want_status is not None and r.status != want_status:
                    continue
                if want_status is None and not 200 <= r.status < 400:
                    continue
                if want_body and want_body not in body:
                    continue
                return True
        except Exception:
            continue
    return False


def default_gateway(route_file: str = "/proc/net/route") -> Optional[str]:
    """The home router's address, from the kernel's routing table."""
    try:
        with open(route_file) as f:
            next(f)
            for line in f:
                p = line.split()
                if len(p) > 2 and p[1] == "00000000" and p[2] != "00000000":
                    g = int(p[2], 16)
                    return ".".join(str((g >> (8 * i)) & 255) for i in range(4))
    except (OSError, StopIteration, ValueError):
        pass
    return None


def router_ok() -> bool:
    """True if the home router answers a ping: then the WiFi works and it's
    the internet beyond it that's down."""
    gw = default_gateway()
    if not gw:
        return False
    try:
        return subprocess.run(["ping", "-c", "2", "-W", "3", gw], capture_output=True,
                              timeout=15).returncode == 0
    except Exception:
        return False


def reboot_now() -> None:
    subprocess.run(["systemctl", "reboot"], timeout=30)


def split_terse(line: str) -> List[str]:
    """Split one line of `nmcli -t` output. Colons inside a value come out
    as \\: and backslashes as \\\\."""
    out, cur, i = [], [], 0
    while i < len(line):
        c = line[i]
        if c == "\\" and i + 1 < len(line):
            cur.append(line[i + 1])
            i += 2
            continue
        if c == ":":
            out.append("".join(cur))
            cur = []
        else:
            cur.append(c)
        i += 1
    out.append("".join(cur))
    return out


def dbm_to_percent(dbm: int) -> int:
    """-50 dBm or better is 100%, -100 dBm is 0% (what NetworkManager shows)."""
    return max(0, min(100, 2 * (dbm + 100)))


@dataclass
class Saved:
    name: str       # what the current connection is called (NM name, or SSID)
    uuid: str       # NM uuid, or wpa_supplicant network id
    ssid: str
    autoconnect: bool = True


@dataclass
class Seen:
    ssid: str
    signal: int
    in_use: bool = False


class _Runner:
    def __init__(self, run: Callable = subprocess.run):
        self._run = run

    def _cmd(self, cmd: List[str], timeout: float = 20) -> Optional[str]:
        try:
            p = self._run(cmd, capture_output=True, text=True, timeout=timeout)
        except Exception as e:
            log(f"{cmd[0]} {cmd[1] if len(cmd) > 1 else ''} failed: {e}")
            return None
        if p.returncode != 0:
            err = (p.stderr or "").strip().splitlines()
            log(f"{' '.join(cmd[:4])}: {err[0] if err else 'error ' + str(p.returncode)}")
            return None
        return p.stdout or ""

    def powersave_off(self, ifname: str) -> None:
        if shutil.which("iw") is None:
            return
        if self._cmd(["iw", "dev", ifname, "set", "power_save", "off"]) is not None:
            log(f"WiFi power saving off on {ifname}")


class NM(_Runner):
    """NetworkManager, through the few nmcli calls the keeper needs. Never
    asks for secrets (no --show-secrets), so passwords never pass through."""
    kind = "NetworkManager"

    def __init__(self, run: Callable = subprocess.run,
                 pause: Callable[[float], None] = time.sleep):
        super().__init__(run)
        self.pause = pause
        self._saved: Optional[List[Saved]] = None
        self._saved_at = 0.0

    def _nmcli(self, *args: str, timeout: float = 20) -> Optional[str]:
        return self._cmd(["nmcli", *args], timeout=timeout)

    def devices(self) -> List[Dict[str, str]]:
        out = self._nmcli("-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "device", "status") or ""
        rows = []
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) >= 4:
                rows.append({"device": f[0], "type": f[1], "state": f[2], "connection": f[3]})
        return rows

    def wifi_device(self) -> Optional[Dict[str, str]]:
        for d in self.devices():
            if d["type"] == "wifi" and not d["device"].startswith("p2p"):
                return d
        return None

    def wired_connected(self) -> bool:
        return any(d["type"] == "ethernet" and d["state"].startswith("connected")
                   for d in self.devices())

    def saved(self, now: float = 0.0) -> List[Saved]:
        if self._saved is not None and now - self._saved_at < 300:
            return self._saved
        out = self._nmcli("-t", "-f", "NAME,UUID,TYPE", "connection", "show")
        if out is None:
            return self._saved or []
        result = []
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) < 3 or f[2] not in ("802-11-wireless", "wifi"):
                continue
            name, uuid = f[0], f[1]
            ssid = (self._nmcli("-g", "802-11-wireless.ssid", "connection", "show", uuid) or "").strip()
            mode = (self._nmcli("-g", "802-11-wireless.mode", "connection", "show", uuid) or "").strip()
            if mode in ("ap", "adhoc", "mesh"):
                continue        # a hotspot it would host, not one to join
            auto = (self._nmcli("-g", "connection.autoconnect", "connection", "show", uuid) or "yes").strip()
            result.append(Saved(name, uuid, ssid.replace("\\:", ":") or name, auto != "no"))
        self._saved, self._saved_at = result, now
        return result

    def scan(self, ifname: str, rescan: bool) -> List[Seen]:
        out = self._nmcli("-t", "-f", "IN-USE,SSID,SIGNAL", "device", "wifi", "list",
                          "ifname", ifname, "--rescan", "yes" if rescan else "no",
                          timeout=40) or ""
        best: Dict[str, Seen] = {}
        for line in out.splitlines():
            f = split_terse(line)
            if len(f) < 3 or not f[1]:
                continue
            try:
                sig = int(f[2])
            except ValueError:
                continue
            s = best.get(f[1])
            in_use = f[0].strip() == "*"
            if s is None:
                best[f[1]] = Seen(f[1], sig, in_use)
            else:       # same name, several access points: keep the strongest
                s.signal = max(s.signal, sig)
                s.in_use = s.in_use or in_use
        return sorted(best.values(), key=lambda s: -s.signal)

    def up(self, uuid: str, ifname: str) -> bool:
        return self._nmcli("--wait", "45", "connection", "up", "uuid", uuid,
                           "ifname", ifname, timeout=60) is not None

    def reset(self, ifname: str) -> None:
        self._nmcli("radio", "wifi", "off")
        self.pause(5)
        self._nmcli("radio", "wifi", "on")
        self.pause(20)       # NetworkManager reconnects on its own


class WPA(_Runner):
    """wpa_supplicant + dhcpcd (older Raspberry Pi OS), through wpa_cli.
    wpa_cli's list_networks never shows passwords, and the config file is
    never read."""
    kind = "wpa_supplicant"

    def __init__(self, ifname: Optional[str] = None, run: Callable = subprocess.run,
                 sysfs: str = "/sys/class/net", pause: Callable[[float], None] = time.sleep):
        super().__init__(run)
        self.sysfs = sysfs
        self.pause = pause
        self.ifname = ifname or self._find_wifi()

    def _find_wifi(self) -> Optional[str]:
        names = sorted(os.path.basename(os.path.dirname(p))
                       for p in glob.glob(os.path.join(self.sysfs, "*", "wireless")))
        return names[0] if names else None

    def _wpa(self, *args: str, timeout: float = 15) -> Optional[str]:
        out = self._cmd(["wpa_cli", "-i", self.ifname or "wlan0", *args], timeout=timeout)
        if out is not None and out.strip() == "FAIL":
            log(f"wpa_cli {' '.join(args)}: FAIL")
            return None
        return out

    def _status(self) -> Dict[str, str]:
        out = self._wpa("status") or ""
        return dict(l.split("=", 1) for l in out.splitlines() if "=" in l)

    def wifi_device(self) -> Optional[Dict[str, str]]:
        if not self.ifname:
            return None
        st = self._status()
        if not st:
            return None
        ok = st.get("wpa_state") == "COMPLETED"
        return {"device": self.ifname, "type": "wifi",
                "state": "connected" if ok else st.get("wpa_state", "unknown").lower(),
                "connection": st.get("ssid", "") if ok else "", "id": st.get("id", ""),
                "bssid": st.get("bssid", "").lower()}

    def wired_connected(self) -> bool:
        for path in glob.glob(os.path.join(self.sysfs, "e*", "carrier")):
            try:
                with open(path) as f:
                    if f.read().strip() == "1":
                        return True
            except OSError:
                continue
        return False

    def saved(self, now: float = 0.0) -> List[Saved]:
        out = self._wpa("list_networks") or ""
        result = []
        for line in out.splitlines()[1:]:           # first line is the header
            f = line.split("\t")
            if len(f) < 2 or not f[0].strip().isdigit():
                continue
            flags = f[3] if len(f) > 3 else ""
            # [DISABLED] that we set ourselves with select_network is undone
            # in up(); one you set in the config file is respected.
            result.append(Saved(f[1], f[0].strip(), f[1], "[DISABLED]" not in flags))
        return result

    def scan(self, ifname: str, rescan: bool) -> List[Seen]:
        if rescan and self._wpa("scan") is not None:
            self.pause(5)
        out = self._wpa("scan_results") or ""
        here = (self.wifi_device() or {}).get("bssid", "")
        best: Dict[str, Seen] = {}
        for line in out.splitlines()[1:]:
            f = line.split("\t")
            if len(f) < 5 or not f[4]:
                continue
            try:
                sig = dbm_to_percent(int(f[2]))
            except ValueError:
                continue
            in_use = bool(here) and f[0].lower() == here
            s = best.get(f[4])
            if s is None:
                best[f[4]] = Seen(f[4], sig, in_use)
            else:
                s.signal = max(s.signal, sig)
                s.in_use = s.in_use or in_use
        return sorted(best.values(), key=lambda s: -s.signal)

    def up(self, uuid: str, ifname: str) -> bool:
        if self._wpa("select_network", uuid) is None:
            return False
        ok = False
        for _ in range(15):                         # up to ~30 s to join
            self.pause(2)
            st = self._status()
            if st.get("wpa_state") == "COMPLETED" and st.get("id") == uuid:
                ok = True
                break
        # select_network turns the others off; turn them back on so
        # wpa_supplicant can still use them on its own (nothing is saved).
        self._wpa("enable_network", "all")
        if ok:
            self.pause(8)                           # dhcpcd gets an address
        return ok

    def reset(self, ifname: str) -> None:
        self._wpa("reconfigure")
        self.pause(20)


def pick_backend(run: Callable = subprocess.run):
    """NetworkManager if it's running, else wpa_supplicant, else None."""
    if shutil.which("nmcli"):
        try:
            p = run(["nmcli", "-t", "-f", "RUNNING", "general"], capture_output=True,
                    text=True, timeout=10)
            if p.returncode == 0 and "running" in (p.stdout or ""):
                return NM(run=run)
        except Exception:
            pass
    if shutil.which("wpa_cli"):
        w = WPA(run=run)
        if w.ifname and w._status():
            return w
    return None


@dataclass
class Choice:
    saved: Saved
    signal: int


def candidates(saved: List[Saved], seen: List[Seen], bad: Dict[str, float], now: float,
               avoid=frozenset()) -> List[Choice]:
    """Saved networks that are in range right now, strongest first, without
    the ones that let it down recently or that you said to avoid."""
    signal = {s.ssid: s.signal for s in seen}
    out = []
    for s in saved:
        if s.ssid not in signal or s.ssid in avoid or not s.autoconnect:
            continue
        if bad.get(s.uuid, 0) > now:
            continue
        out.append(Choice(s, signal[s.ssid]))
    return sorted(out, key=lambda c: -c.signal)


class Keeper:
    def __init__(self, nm, online: Callable[[], bool] = internet_ok,
                 notice: Optional[Callable[[str], None]] = None,
                 clock: Callable[[], float] = time.time,
                 settle: Callable[[], None] = lambda: time.sleep(5),
                 router: Callable[[], bool] = router_ok,
                 reboot: Callable[[], None] = reboot_now):
        self.nm = nm
        self.online = online
        self.notice = notice or log
        self.clock = clock
        self.settle = settle
        self.fails = 0
        self.weak = 0
        self.bad: Dict[str, float] = {}
        self.last_roam = 0.0
        self.last_scan = 0.0
        self.retry_after = 0.0
        self.recoveries = 0         # recoveries in a row that found nothing
        self.told_offline = False
        self.status: Dict = {}
        self.router = router
        self.reboot = reboot
        self.last_online = True
        self.offline_since: Optional[float] = None
        self.reboot_note = ""       # last "not restarting because..." line logged

    # -- helpers ---------------------------------------------------------
    def _current(self, saved: List[Saved], dev: Dict[str, str]) -> Optional[Saved]:
        if not dev["state"].startswith("connected"):
            return None
        if dev.get("id"):           # wpa_supplicant knows the network id
            for s in saved:
                if s.uuid == dev["id"]:
                    return s
        for s in saved:
            if s.name == dev["connection"]:
                return s
        return None

    def _try(self, choice: Choice, ifname: str) -> bool:
        log(f"Trying {choice.saved.ssid} (signal {choice.signal}%)")
        if not self.nm.up(choice.saved.uuid, ifname):
            return False
        self.settle()
        if self.online():
            return True
        self.settle()
        return self.online()

    def _save_status(self, **kw) -> None:
        self.status = dict(kw, ts=int(self.clock()))
        try:
            tmp = STATUS_FILE + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.status, f)
            os.chmod(tmp, 0o644)
            os.replace(tmp, STATUS_FILE)
        except OSError:
            pass

    def step(self) -> str:
        result = self._step()
        return self._watch_reboot(result)

    # -- last resort: restart the Pi ---------------------------------------
    def _reboots(self, now: float) -> List[float]:
        try:
            with open(REBOOT_FILE) as f:
                times = [float(t) for t in json.load(f)]
        except (OSError, ValueError, TypeError):
            times = []
        return [t for t in times if now - t < 86400]

    def _note(self, key: str, text: str) -> str:
        """Log a "not restarting because..." line once per reason, not every minute."""
        if key != self.reboot_note:
            self.reboot_note = key
            log(text)
        return text

    def _watch_reboot(self, result: str) -> str:
        now = self.clock()
        if self.last_online:
            self.offline_since = None
            self.reboot_note = ""
            return result
        if self.offline_since is None:
            self.offline_since = now
        if REBOOT_SECONDS <= 0 or now - self.offline_since < REBOOT_SECONDS:
            return result
        mins = (now - self.offline_since) / 60
        if self.router():
            return self._note("isp", f"Offline {mins:.0f} min, but the router answers: the internet "
                              "itself is down, so restarting wouldn't help.")
        times = self._reboots(now)
        if len(times) >= REBOOTS_PER_DAY:
            return self._note("cap", f"Offline {mins:.0f} min; already restarted {len(times)} times "
                              "today, so not restarting again.")
        try:
            os.makedirs(os.path.dirname(REBOOT_FILE), exist_ok=True)
            with open(REBOOT_FILE, "w") as f:
                json.dump(times + [now], f)
        except OSError as e:
            # Without the count it could restart forever, so don't.
            return self._note("file", f"Can't save the restart count ({e}), so not restarting.")
        log(f"Offline {mins:.0f} min and the router doesn't answer: restarting the Pi "
            f"(restart {len(times) + 1} of {REBOOTS_PER_DAY} allowed today).")
        self.offline_since = None
        self.reboot()
        return "restarting"

    # -- one check -------------------------------------------------------
    def _step(self) -> str:
        now = self.clock()
        dev = self.nm.wifi_device()
        if dev is None:
            # The WiFi chip itself can vanish on a Pi Zero; a restart brings it back.
            self.last_online = self.online()
            return "no wifi device"
        ifname = dev["device"]
        if self.nm.wired_connected() and self.online():
            self.fails = 0
            self.last_online = True
            return "on a cable, leaving WiFi alone"

        saved = self.nm.saved(now)
        current = self._current(saved, dev)

        self.last_online = self.online()
        if self.last_online:
            self.fails = 0
            self.recoveries = 0
            if self.told_offline:
                self.told_offline = False
                self.notice(f"Back online on the {current.ssid if current else dev['connection']} WiFi.")
            return self._maybe_roam(now, ifname, saved, current)

        self.fails += 1
        self._save_status(online=False, ssid=current.ssid if current else None)
        if self.fails < FAILS:
            return f"no internet ({self.fails} of {FAILS})"
        if now < self.retry_after:
            return "no internet, waiting before trying again"
        return self._recover(now, ifname, saved, current)

    def _recover(self, now, ifname, saved, current) -> str:
        if current:
            self.bad[current.uuid] = now + BAD_SECONDS
            log(f"{current.ssid} has no internet; skipping it for {BAD_SECONDS / 60:.0f} min")
        seen = self.nm.scan(ifname, rescan=True)
        self.last_scan = now
        tried = []
        for c in candidates(saved, seen, self.bad, now, AVOID):
            tried.append(c.saved.ssid)
            if self._try(c, ifname):
                self.fails = 0
                self.recoveries = 0
                was = f": {current.ssid} had no internet" if current else ""
                self.notice(f"Moved to the {c.saved.ssid} WiFi{was}.")
                self._save_status(online=True, ssid=c.saved.ssid, signal=c.signal)
                return f"switched to {c.saved.ssid}"
            self.bad[c.saved.uuid] = now + BAD_SECONDS
        # Nothing else worked, so it's probably the internet itself, or the
        # WiFi chip is stuck. Reconnect to the usual one (that alone often
        # fixes it), and every few rounds switch WiFi off and on. Forget the
        # "bad" marks so everything gets another go, and wait a bit.
        self.bad.clear()
        self.recoveries += 1
        if self.recoveries % RESET_AFTER == 0:
            log("Still offline: switching WiFi off and on")
            self.nm.reset(ifname)
        else:
            usual = current or next((s for s in saved if s.autoconnect and s.ssid not in AVOID), None)
            if usual:
                log(f"Reconnecting to {usual.ssid}")
                self.nm.up(usual.uuid, ifname)
        self.retry_after = now + RETRY_SECONDS
        if not self.told_offline:
            self.told_offline = True
            if tried:
                self.notice("Offline: none of the saved WiFi networks reach the internet right now.")
            else:
                self.notice("Offline, and no other saved WiFi network is in range.")
        return "offline, nothing better found" if tried else "offline, no other saved network in range"

    def _maybe_roam(self, now, ifname, saved, current) -> str:
        rescan = now - self.last_scan >= RESCAN_SECONDS
        seen = self.nm.scan(ifname, rescan=rescan) if (ROAM or rescan) else []
        if rescan:
            self.last_scan = now
        here = next((s for s in seen if s.in_use), None)
        sig = here.signal if here else None
        self._save_status(online=True, ssid=current.ssid if current else None, signal=sig)
        if not ROAM or current is None or sig is None or sig >= WEAK:
            self.weak = 0
            return f"online ({sig}%)" if sig is not None else "online"
        others = [c for c in candidates(saved, seen, self.bad, now, AVOID)
                  if c.saved.uuid != current.uuid and c.signal >= sig + MARGIN]
        if not others:
            self.weak = 0
            return f"online, weak signal ({sig}%), nothing better in range"
        self.weak += 1
        if self.weak < ROAM_CHECKS or now - self.last_roam < ROAM_SECONDS:
            return f"online, weak signal ({sig}%), {others[0].saved.ssid} is stronger"
        self.weak = 0
        self.last_roam = now
        best = others[0]
        if self._try(best, ifname):
            self.notice(f"Moved to the {best.saved.ssid} WiFi: it's stronger "
                        f"({best.signal}% vs {sig}%).")
            self._save_status(online=True, ssid=best.saved.ssid, signal=best.signal)
            return f"moved to stronger {best.saved.ssid}"
        log(f"{best.saved.ssid} had no internet; going back to {current.ssid}")
        self.bad[best.saved.uuid] = now + BAD_SECONDS
        self.nm.up(current.uuid, ifname)
        return f"stayed on {current.ssid}"


def show_status(nm) -> None:
    print(f"Network manager: {nm.kind}")
    dev = nm.wifi_device()
    if dev is None:
        print("No WiFi device found.")
        return
    print(f"WiFi device {dev['device']}: {dev['state']}, network: {dev['connection'] or '-'}")
    print(f"Network cable connected: {'yes' if nm.wired_connected() else 'no'}")
    print(f"Internet check: {'OK' if internet_ok() else 'FAILED'}")
    saved = nm.saved()
    seen = {s.ssid: s for s in nm.scan(dev["device"], rescan=True)}
    print("\nSaved WiFi networks (passwords are never shown):")
    for s in sorted(saved, key=lambda s: -(seen[s.ssid].signal if s.ssid in seen else -1)):
        here = seen.get(s.ssid)
        where = f"signal {here.signal}%" if here else "not in range"
        flags = []
        if here and here.in_use:
            flags.append("in use")
        if not s.autoconnect:
            flags.append("autoconnect off, skipped")
        if s.ssid in AVOID:
            flags.append("in NODE_WIFI_AVOID, skipped")
        extra = f"  ({', '.join(flags)})" if flags else ""
        print(f"  {s.ssid:<28} {where}{extra}")
    if not saved:
        print("  (none found)")
    if len(saved) < 2:
        print("\nOnly one saved network: it can't switch, but it will still reconnect\n"
              "and restart WiFi when the internet drops. Add your other WiFi to let it switch.")


def main() -> None:
    nm = pick_backend()
    if "--status" in sys.argv:
        if nm is None:
            print("Found neither NetworkManager nor wpa_supplicant running.")
        else:
            show_status(nm)
        return
    if not ENABLED:
        log("NODE_WIFI=0: WiFi keeper is off.")
        while True:         # stay up quietly so systemd doesn't keep restarting it
            time.sleep(3600)
    if nm is None:
        log("Found neither NetworkManager nor wpa_supplicant running; trying again in 5 min.")
        time.sleep(300)
        sys.exit(1)

    dev = nm.wifi_device()
    if dev and POWERSAVE_OFF:
        nm.powersave_off(dev["device"])
    keeper = Keeper(nm)
    log(f"WiFi keeper started ({nm.kind}): checking every {EVERY:.0f} s"
        f"{'' if ROAM else ', not roaming to stronger networks'}.")
    last = None
    while True:
        try:
            result = keeper.step()
        except Exception as e:
            result = f"check failed: {e}"
        if result != last and not result.startswith("online ("):
            log(result)
        last = result
        time.sleep(EVERY)


if __name__ == "__main__":
    main()
