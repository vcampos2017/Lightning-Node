import importlib.util
import os
import sys
import tempfile
from types import SimpleNamespace

_path = os.path.join(os.path.dirname(__file__), "..", "wifi_keeper.py")
_spec = importlib.util.spec_from_file_location("wifi_keeper", _path)
wk = importlib.util.module_from_spec(_spec)
sys.modules["wifi_keeper"] = wk
_spec.loader.exec_module(wk)
wk.STATUS_FILE = os.path.join(tempfile.mkdtemp(), "wifi.json")
wk.REBOOT_FILE = os.path.join(tempfile.mkdtemp(), "state", "reboots.json")
REBOOTS = []


class FakeAir:
    """A pretend NetworkManager + the air around the Pi."""

    def __init__(self, saved, signals, internet, current=None):
        self.saved_list = saved                  # [(name, uuid, ssid, autoconnect)]
        self.signals = signals                   # ssid -> signal
        self.internet = internet                 # ssid -> has internet?
        self.current = current                   # ssid in use
        self.calls = []
        self.wired = False

    def by_uuid(self, uuid):
        return next(s for s in self.saved_list if s[1] == uuid)

    def __call__(self, cmd, **kw):
        assert cmd[0] == "nmcli"
        assert "--show-secrets" not in cmd and "-s" not in cmd   # never ask for passwords
        args = cmd[1:]
        self.calls.append(args)
        out, rc = "", 0
        if args[-2:] == ["device", "status"]:
            rows = ["wlan0:wifi:%s:%s" % (("connected", self.name_of(self.current))
                                          if self.current else ("disconnected", ""))]
            rows.append("eth0:ethernet:%s:" % ("connected" if self.wired else "unavailable"))
            rows.append("lo:loopback:connected (externally):lo")
            out = "\n".join(rows)
        elif args[:5] == ["-t", "-f", "NAME,UUID,TYPE", "connection", "show"]:
            out = "\n".join(f"{n.replace(':', chr(92) + ':')}:{u}:802-11-wireless"
                            for n, u, _, _ in self.saved_list) + "\nWired connection 1:w1:802-3-ethernet"
        elif args[0] == "-g":
            n, u, ssid, auto = self.by_uuid(args[-1])
            out = {"802-11-wireless.ssid": ssid.replace(":", "\\:"),
                   "802-11-wireless.mode": "infrastructure",
                   "connection.autoconnect": "yes" if auto else "no"}[args[1]] + "\n"
        elif "wifi" in args and "list" in args:
            out = "\n".join(f"{'*' if s == self.current else ' '}:{s.replace(':', chr(92) + ':')}:{sig}"
                            for s, sig in self.signals.items())
        elif "up" in args:
            uuid = args[args.index("uuid") + 1]
            ssid = self.by_uuid(uuid)[2]
            if ssid in self.signals:
                self.current = ssid
            else:
                rc = 4
        return SimpleNamespace(returncode=rc, stdout=out, stderr="Error: no network" if rc else "")

    def name_of(self, ssid):
        return next(n for n, _, s, _ in self.saved_list if s == ssid)

    def online(self):
        return bool(self.current and self.internet.get(self.current))


SAVED = [("AVC-2G", "u1", "AVC-2G", True),
         ("AVC-5G", "u2", "AVC-5G", True),
         ("Cafe: Guest", "u3", "Cafe: Guest", True)]


def make(air, t):
    notices = []
    k = wk.Keeper(wk.NM(run=air, pause=lambda s: None), online=air.online, notice=notices.append,
                  clock=lambda: t[0], settle=lambda: None,
                  router=lambda: False, reboot=lambda: REBOOTS.append(t[0]))
    return k, notices


def test_split_terse_handles_escaped_colons():
    assert wk.split_terse(r"*:Cafe\: Guest:71") == ["*", "Cafe: Guest", "71"]
    assert wk.split_terse(r" :back\\slash:5") == [" ", "back\\slash", "5"]


def test_stays_put_when_all_is_well():
    air = FakeAir(SAVED, {"AVC-2G": 70, "AVC-5G": 90}, {"AVC-2G": True, "AVC-5G": True}, "AVC-2G")
    t = [1000.0]
    k, notices = make(air, t)
    for _ in range(5):
        k.step()
        t[0] += 60
    assert air.current == "AVC-2G"         # 70% is fine, no hopping around
    assert not notices
    assert not any("up" in c for c in air.calls)


def test_no_internet_moves_to_the_best_working_one():
    air = FakeAir(SAVED, {"AVC-2G": 80, "AVC-5G": 60, "Cafe: Guest": 75},
                  {"AVC-2G": False, "AVC-5G": True, "Cafe: Guest": False}, "AVC-2G")
    t = [1000.0]
    k, notices = make(air, t)
    assert "1 of" in k.step()               # one miss isn't enough
    t[0] += 60
    assert k.step() == "switched to AVC-5G"  # tried the cafe (stronger) first, then 5G
    assert air.current == "AVC-5G"
    assert notices == ["Moved to the AVC-5G WiFi: AVC-2G had no internet."]
    # The bad one is skipped for a while even though it's strongest.
    assert k.bad["u1"] > t[0]


def test_weak_signal_moves_to_a_much_stronger_one():
    air = FakeAir(SAVED, {"AVC-2G": 25, "AVC-5G": 80}, {"AVC-2G": True, "AVC-5G": True}, "AVC-2G")
    t = [1000.0]
    k, notices = make(air, t)
    for _ in range(wk.ROAM_CHECKS - 1):
        k.step()
        t[0] += 60
    assert air.current == "AVC-2G"
    assert k.step() == "moved to stronger AVC-5G"
    assert air.current == "AVC-5G"
    assert "stronger" in notices[0]


def test_goes_back_if_the_stronger_one_has_no_internet():
    air = FakeAir(SAVED, {"AVC-2G": 25, "AVC-5G": 80}, {"AVC-2G": True, "AVC-5G": False}, "AVC-2G")
    t = [1000.0]
    k, notices = make(air, t)
    for _ in range(wk.ROAM_CHECKS):
        r = k.step()
        t[0] += 60
    assert r == "stayed on AVC-2G"
    assert air.current == "AVC-2G"
    assert not notices
    for _ in range(10):                      # and doesn't keep trying it
        k.step()
        t[0] += 60
    assert air.current == "AVC-2G"


def test_nothing_works_tells_the_face_once_and_reconnects():
    air = FakeAir(SAVED, {"AVC-2G": 80, "AVC-5G": 60}, {}, "AVC-2G")
    t = [1000.0]
    k, notices = make(air, t)
    k.step()
    t[0] += 60
    assert k.step() == "offline, nothing better found"
    assert air.current == "AVC-2G"           # back on the usual one
    assert len(notices) == 1 and "Offline" in notices[0]
    for _ in range(10):
        k.step()
        t[0] += 60
    assert len(notices) == 1                 # not nagging every few minutes
    air.internet["AVC-2G"] = True            # the router comes back
    k.step()
    assert notices[-1] == "Back online on the AVC-2G WiFi."


def test_skips_avoided_and_autoconnect_off_networks():
    saved = SAVED[:2] + [("Neighbor", "u4", "Neighbor", False)]
    air = FakeAir(saved, {"AVC-2G": 80, "AVC-5G": 60, "Neighbor": 99},
                  {"AVC-5G": True, "Neighbor": True}, "AVC-2G")
    t = [1000.0]
    k, notices = make(air, t)
    old = wk.AVOID
    wk.AVOID = {"AVC-5G"}
    try:
        k.step()
        t[0] += 60
        assert k.step() == "offline, no other saved network in range"
    finally:
        wk.AVOID = old
    assert air.current == "AVC-2G"
    assert not any("u4" in c or "u2" in c for c in air.calls if "up" in c)


def test_leaves_wifi_alone_on_a_cable():
    air = FakeAir(SAVED, {"AVC-2G": 10, "AVC-5G": 90}, {"AVC-2G": False}, "AVC-2G")
    air.wired = True
    k, _ = make(air, [1000.0])
    k.online = lambda: True
    for _ in range(5):
        assert k.step() == "on a cable, leaving WiFi alone"
    assert air.current == "AVC-2G"


def test_status_file_has_no_secrets():
    air = FakeAir(SAVED, {"AVC-2G": 70}, {"AVC-2G": True}, "AVC-2G")
    k, _ = make(air, [1000.0])
    k.step()
    import json
    data = json.load(open(wk.STATUS_FILE))
    assert data["ssid"] == "AVC-2G" and data["online"] is True and data["signal"] == 70
    assert set(data) == {"online", "ssid", "signal", "ts"}




def test_one_saved_network_still_gets_unstuck():
    air = FakeAir(SAVED[:1], {"AVC-2G": 70}, {"AVC-2G": False}, "AVC-2G")
    t = [1000.0]
    k, notices = make(air, t)
    for _ in range(wk.RESET_AFTER * 6):
        k.step()
        t[0] += 60
    ups = [c for c in air.calls if "up" in c]
    radio = [c for c in air.calls if c[:1] == ["radio"]]
    assert ups                                   # reconnects to the only one
    assert ["radio", "wifi", "off"] in radio and ["radio", "wifi", "on"] in radio
    assert len(notices) == 1


# --- wpa_supplicant (older Raspberry Pi OS) ---------------------------------

class FakeWpa:
    """A pretend wpa_supplicant behind wpa_cli."""

    def __init__(self, nets, signals, internet, current=None):
        self.nets = nets                         # [(id, ssid)]
        self.signals = signals                   # ssid -> dBm
        self.internet = internet
        self.current = current                   # id in use
        self.disabled = set()
        self.calls = []

    def ssid(self, nid):
        return dict(self.nets)[nid]

    def bssid(self, ssid):
        return "aa:bb:cc:00:00:%02x" % (sorted(self.signals).index(ssid) + 1)

    def __call__(self, cmd, **kw):
        assert cmd[:3] == ["wpa_cli", "-i", "wlan0"]
        args = cmd[3:]
        self.calls.append(args)
        out = ""
        if args == ["status"]:
            if self.current is not None:
                s = self.ssid(self.current)
                out = (f"bssid={self.bssid(s).upper()}\nfreq=2437\nssid={s}\nid={self.current}\n"
                       "mode=station\nwpa_state=COMPLETED\nip_address=192.168.1.50\n")
            else:
                out = "wpa_state=SCANNING\n"
        elif args == ["list_networks"]:
            rows = ["network id / ssid / bssid / flags"]
            for nid, s in self.nets:
                flags = "[CURRENT]" if nid == self.current else ("[DISABLED]" if nid in self.disabled else "")
                rows.append(f"{nid}\t{s}\tany\t{flags}")
            out = "\n".join(rows)
        elif args == ["scan"]:
            out = "OK"
        elif args == ["scan_results"]:
            rows = ["bssid / frequency / signal level / flags / ssid"]
            for s, dbm in self.signals.items():
                rows.append(f"{self.bssid(s)}\t2437\t{dbm}\t[WPA2-PSK-CCMP][ESS]\t{s}")
            out = "\n".join(rows)
        elif args[0] == "select_network":
            nid = args[1]
            self.disabled = {n for n, _ in self.nets if n != nid}
            self.current = nid if self.ssid(nid) in self.signals else None
            out = "OK"
        elif args == ["enable_network", "all"]:
            self.disabled = set()
            out = "OK"
        elif args == ["reconfigure"]:
            out = "OK"
        else:
            out = "FAIL"
        return SimpleNamespace(returncode=0, stdout=out, stderr="")

    def online(self):
        return self.current is not None and self.internet.get(self.ssid(self.current), False)


def make_wpa(air, t, sysfs):
    notices = []
    w = wk.WPA(run=air, sysfs=sysfs, pause=lambda s: None)
    k = wk.Keeper(w, online=air.online, notice=notices.append,
                  clock=lambda: t[0], settle=lambda: None,
                  router=lambda: False, reboot=lambda: REBOOTS.append(t[0]))
    return w, k, notices


def _sysfs(carrier="0"):
    d = tempfile.mkdtemp()
    os.makedirs(os.path.join(d, "wlan0", "wireless"))
    os.makedirs(os.path.join(d, "eth0"))
    with open(os.path.join(d, "eth0", "carrier"), "w") as f:
        f.write(carrier)
    return d


def test_dbm_to_percent():
    assert wk.dbm_to_percent(-40) == 100
    assert wk.dbm_to_percent(-70) == 60
    assert wk.dbm_to_percent(-110) == 0


def test_wpa_finds_device_saved_and_signal():
    air = FakeWpa([("0", "AVC-2G"), ("1", "GF-Home")], {"AVC-2G": -80, "GF-Home": -55},
                  {"AVC-2G": True}, "0")
    w, k, _ = make_wpa(air, [1000.0], _sysfs())
    assert w.ifname == "wlan0"
    assert [s.ssid for s in w.saved()] == ["AVC-2G", "GF-Home"]
    seen = w.scan("wlan0", rescan=True)
    assert seen[0].ssid == "GF-Home" and seen[0].signal == 90
    assert next(s for s in seen if s.ssid == "AVC-2G").in_use
    assert not w.wired_connected()
    assert w.wifi_device()["connection"] == "AVC-2G"


def test_wpa_no_internet_moves_and_reenables_the_others():
    air = FakeWpa([("0", "AVC-2G"), ("1", "GF-Home")], {"AVC-2G": -50, "GF-Home": -60},
                  {"AVC-2G": False, "GF-Home": True}, "0")
    t = [1000.0]
    w, k, notices = make_wpa(air, t, _sysfs())
    k.step()
    t[0] += 60
    assert k.step() == "switched to GF-Home"
    assert air.current == "1"
    assert air.disabled == set()             # select_network's side effect undone
    assert notices == ["Moved to the GF-Home WiFi: AVC-2G had no internet."]


def test_wpa_weak_signal_roams():
    air = FakeWpa([("0", "AVC-2G"), ("1", "GF-Home")], {"AVC-2G": -85, "GF-Home": -50},
                  {"AVC-2G": True, "GF-Home": True}, "0")
    t = [1000.0]
    w, k, notices = make_wpa(air, t, _sysfs())
    for _ in range(wk.ROAM_CHECKS):
        r = k.step()
        t[0] += 60
    assert r == "moved to stronger GF-Home"
    assert air.current == "1"


def test_wpa_cable_leaves_wifi_alone():
    air = FakeWpa([("0", "AVC-2G")], {"AVC-2G": -90}, {}, "0")
    w, k, _ = make_wpa(air, [1000.0], _sysfs("1"))
    k.online = lambda: True
    assert k.step() == "on a cable, leaving WiFi alone"


def test_never_touches_password_files():
    import builtins
    real_open = builtins.open
    opened = []

    def spy(path, *a, **kw):
        opened.append(str(path))
        return real_open(path, *a, **kw)

    builtins.open = spy
    try:
        air = FakeWpa([("0", "AVC-2G"), ("1", "GF-Home")], {"AVC-2G": -50, "GF-Home": -60},
                      {"GF-Home": True}, "0")
        t = [1000.0]
        w, k, _ = make_wpa(air, t, _sysfs())
        for _ in range(4):
            k.step()
            t[0] += 60
    finally:
        builtins.open = real_open
    assert not any("wpa_supplicant" in p or "NetworkManager" in p for p in opened)


# --- last resort: restart the Pi --------------------------------------------

def _fresh_reboots():
    REBOOTS.clear()
    if os.path.exists(wk.REBOOT_FILE):
        os.remove(wk.REBOOT_FILE)


def test_restarts_after_long_offline_when_router_is_gone():
    _fresh_reboots()
    air = FakeAir(SAVED[:1], {"AVC-2G": 70}, {}, "AVC-2G")
    t = [1000.0]
    k, _ = make(air, t)
    results = []
    for _ in range(40):
        results.append(k.step())
        t[0] += 60
    assert "restarting" in results
    assert results.index("restarting") >= 29         # not before 30 minutes
    assert len(REBOOTS) == 1                         # then waits another 30 min


def test_never_restarts_more_than_the_daily_cap():
    _fresh_reboots()
    air = FakeAir(SAVED[:1], {"AVC-2G": 70}, {}, "AVC-2G")
    t = [1000.0]
    for _ in range(6):                                # six "boots" in a row
        k, _ = make(air, t)                           # a new process each boot
        for _ in range(35):
            k.step()
            t[0] += 60
    assert len(REBOOTS) == wk.REBOOTS_PER_DAY
    t[0] += 86400                                     # a day later it may again
    k, _ = make(air, t)
    for _ in range(35):
        k.step()
        t[0] += 60
    assert len(REBOOTS) == wk.REBOOTS_PER_DAY + 1


def test_no_restart_when_only_the_internet_is_down():
    _fresh_reboots()
    air = FakeAir(SAVED[:1], {"AVC-2G": 70}, {}, "AVC-2G")
    t = [1000.0]
    k, _ = make(air, t)
    k.router = lambda: True                           # router answers: ISP outage
    for _ in range(90):
        r = k.step()
        t[0] += 60
    assert not REBOOTS
    assert "internet itself is down" in r


def test_no_restart_when_internet_comes_back():
    _fresh_reboots()
    air = FakeAir(SAVED[:1], {"AVC-2G": 70}, {}, "AVC-2G")
    t = [1000.0]
    k, _ = make(air, t)
    for i in range(60):
        air.internet["AVC-2G"] = (i % 20 == 19)       # back for a minute every 20
        k.step()
        t[0] += 60
    assert not REBOOTS


def test_restart_when_wifi_chip_vanishes():
    _fresh_reboots()
    air = FakeAir(SAVED[:1], {"AVC-2G": 70}, {}, None)
    t = [1000.0]
    k, _ = make(air, t)
    k.nm.wifi_device = lambda: None
    for _ in range(35):
        k.step()
        t[0] += 60
    assert len(REBOOTS) == 1


def test_default_gateway_from_route_table():
    d = tempfile.mkdtemp()
    p = os.path.join(d, "route")
    with open(p, "w") as f:
        f.write("Iface\tDestination\tGateway\tFlags\n"
                "wlan0\t0001A8C0\t00000000\t0001\n"
                "wlan0\t00000000\t0101A8C0\t0003\n")
    assert wk.default_gateway(p) == "192.168.1.1"
    assert wk.default_gateway(os.path.join(d, "missing")) is None


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn()
            print("ok", name)
