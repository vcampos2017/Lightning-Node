"""Tests for broker_link.py (finding Chatty's board on any WiFi). Plain Python,
no paho needed: a fake client stands in. Run: python3 tests/test_broker_link.py"""
import errno
import json
import os
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
for d in (os.path.join(HERE, ".."), HERE):
    if os.path.exists(os.path.join(d, "broker_link.py")):
        sys.path.insert(0, d)
        break
import broker_link  # noqa: E402


class Msg:
    def __init__(self, topic, payload):
        self.topic, self.payload = topic, payload


class FakeClient:
    """Answers only at the addresses in `up`."""

    def __init__(self, up=()):
        self.up = set(up)
        self.on_connect = self.on_disconnect = self.on_message = None
        self.tried, self.subs, self.host = [], [], None
        self.pending = False
        self.disconnects = 0

    def connect(self, host, port, keepalive=30):
        self.tried.append(host)
        if host not in self.up:
            raise OSError("no answer")
        self.host, self.pending = host, True

    def loop(self, timeout=1.0):
        if self.pending:
            self.pending = False
            self.on_connect(self, None, {}, 0)
        return 0 if self.host else 4

    def subscribe(self, topic, qos=0):
        self.subs.append(topic)

    def disconnect(self):
        self.disconnects += 1
        self.host = None


def _resolve_everything(monkey=None):
    # every test name resolves to itself; .local names to a home address
    def fake(host, port, *a):
        ip = "192.168.1.110" if host.endswith(".local") else host
        return [(2, 1, 6, "", (ip, port))]
    broker_link.socket.getaddrinfo = fake


def _link(client, hub_note=None, **kw):
    path = os.path.join(tempfile.mkdtemp(), "hub.json")
    if hub_note:
        with open(path, "w") as f:
            json.dump(hub_note, f)
    hub = broker_link.HubNote(path)
    logs = []
    kw.setdefault("hosts", ["chatty-node.local"])
    kw.setdefault("tailscale", "100.64.0.10")
    kw.setdefault("scan", False)
    link = broker_link.BrokerLink(client, hub=hub, log=logs.append, **kw)
    link.logs = logs
    return link


def test_kind_of():
    assert broker_link.kind_of("100.64.0.10") == "tailscale"
    assert broker_link.kind_of("192.168.1.110") == "lan"
    assert broker_link.kind_of("chatty-node.local") == "lan"


def test_falls_back_to_tailscale_when_home_name_fails():
    _resolve_everything()
    c = FakeClient(up={"100.64.0.10"})
    link = _link(c)
    assert link.connect_round()
    assert c.tried == ["chatty-node.local", "100.64.0.10"]
    assert link.via == "tailscale" and link.good == "100.64.0.10"
    assert broker_link.HUB_TOPIC in c.subs


def test_users_callbacks_still_run():
    _resolve_everything()
    c = FakeClient(up={"chatty-node.local"})
    seen = []
    c.on_connect = lambda *a: seen.append("connect")
    c.on_message = lambda cl, u, m: seen.append(m.topic)
    link = _link(c)
    assert link.connect_round() and seen == ["connect"]
    c.on_message(c, None, Msg("chatty/forecast/rain", b"{}"))
    c.on_message(c, None, Msg(broker_link.HUB_TOPIC, b'{"wifi":"HomeWiFi","lan":["192.168.1.110"]}'))
    assert seen == ["connect", "chatty/forecast/rain"]   # her note isn't passed on
    assert link.hub.wifi == "HomeWiFi"


def test_learns_her_addresses_and_remembers_them():
    _resolve_everything()
    c = FakeClient(up={"100.64.0.10"})
    link = _link(c)
    link.connect_round()
    note = {"host": "chatty-node", "wifi": "ExtenderWiFi", "lan": ["192.168.1.120"],
            "tailscale": "100.64.0.10", "known": {"HomeWiFi": "192.168.1.110", "ExtenderWiFi": "192.168.1.120"}}
    c.on_message(c, None, Msg(broker_link.HUB_TOPIC, json.dumps(note).encode()))
    assert any("ExtenderWiFi" in line for line in link.logs)
    # a fresh start reads the note back from disk and tries her home addresses first
    again = broker_link.HubNote(link.hub.path)
    assert again.lan_all() == ["192.168.1.120", "192.168.1.110"]
    c2 = FakeClient(up={"192.168.1.110"})
    link2 = broker_link.BrokerLink(c2, hub=again, hosts=["chatty-node.local"],
                                   tailscale="100.64.0.10", scan=False, log=lambda t: None)
    assert link2.connect_round()
    assert c2.tried == ["192.168.1.120", "192.168.1.110"]
    assert link2.via == "lan"


def test_known_addresses_add_up_across_notes():
    hub = broker_link.HubNote(os.path.join(tempfile.mkdtemp(), "h.json"))
    hub.update(b'{"wifi":"HomeWiFi","lan":["192.168.1.110"],"known":{"HomeWiFi":"192.168.1.110"}}', 1)
    hub.update(b'{"wifi":"ExtenderWiFi","lan":["192.168.1.120"],"known":{"ExtenderWiFi":"192.168.1.120"}}', 2)
    assert hub.note["known"] == {"HomeWiFi": "192.168.1.110", "ExtenderWiFi": "192.168.1.120"}
    assert not hub.update(b"not json", 3)
    assert not hub.update(b'{"wifi":"ExtenderWiFi","lan":["192.168.1.120"],"known":{"x":"rm -rf"}}', 4)
    assert "x" not in hub.note["known"]


def test_follows_her_home_from_tailscale():
    _resolve_everything()
    c = FakeClient(up={"100.64.0.10", "192.168.1.120"})
    link = _link(c, hosts=["chatty-node.local"])
    link.hub.note = {"wifi": "ExtenderWiFi", "lan": [], "known": {}}
    c.up.discard("192.168.1.120")
    assert link.connect_round() and link.via == "tailscale"
    link.hub.note = {"wifi": "ExtenderWiFi", "lan": ["192.168.1.120"], "known": {}}
    broker_link.tcp_open = lambda host, port=1883, timeout=1.0: host == "192.168.1.120"
    c.up.add("192.168.1.120")
    link.maybe_follow()
    assert not link.connected and link.good == "192.168.1.120"
    assert link.connect_round() and link.via == "lan"
    assert c.tried[-1] == "192.168.1.120"


def test_no_follow_on_home_network():
    _resolve_everything()
    c = FakeClient(up={"chatty-node.local"})
    link = _link(c)
    link.connect_round()
    broker_link.tcp_open = lambda *a, **k: True
    link.maybe_follow()
    assert link.connected


def test_knocked_on_board_must_prove_it_is_hers():
    _resolve_everything()
    c = FakeClient(up={"192.168.1.99"})
    t = [1000.0]
    link = _link(c, scan=True, clock=lambda: t[0])
    broker_link.scan_home = lambda port=1883: ["192.168.1.99"]
    for _ in range(broker_link.SCAN_AFTER_ROUNDS):
        assert not link.connect_round()
    link.maybe_scan()
    assert link.scanned == ["192.168.1.99"]
    assert link.connect_round() and link.unproven and link.good != "192.168.1.99"
    t[0] += broker_link.SCAN_PROOF_SECONDS + 1
    link.check_proof()
    assert not link.connected and link.scanned == []
    # this time her note shows up: it's kept
    link.scanned = ["192.168.1.99"]
    assert link.connect_round()
    c.on_message(c, None, Msg(broker_link.HUB_TOPIC, b'{"wifi":"ExtenderWiFi","lan":["192.168.1.99"]}'))
    assert not link.unproven and link.good == "192.168.1.99"


def test_no_scan_when_turned_off_or_too_soon():
    c = FakeClient()
    link = _link(c, scan=False)
    link.failed_rounds = 99
    called = []
    broker_link.scan_home = lambda port=1883: called.append(1) or []
    link.maybe_scan()
    assert called == []
    link.scan_ok = True
    link.maybe_scan()
    link.maybe_scan()
    assert called == [1]


def test_scan_home_skips_itself():
    broker_link.tcp_open = lambda host, port=1883, timeout=1.0: host in ("192.168.1.5", "192.168.1.9")
    hits = broker_link.scan_home(mine=["192.168.1.9/24"])
    assert hits == ["192.168.1.5"]


def test_info():
    _resolve_everything()
    c = FakeClient(up={"100.64.0.10"})
    link = _link(c)
    link.connect_round()
    link.hub.note = {"wifi": "ExtenderWiFi"}
    assert link.info() == {"via": "tailscale", "broker": "100.64.0.10", "chatty_wifi": "ExtenderWiFi"}


def _drop_now(c):
    """The way to the board dies (what the broker calls 'exceeded timeout')."""
    c.host = None
    c.on_disconnect(c, None, {}, 7)


def test_early_drop_makes_the_way_shaky_and_tailscale_goes_first():
    _resolve_everything()
    t = [1000.0]
    c = FakeClient(up={"192.168.1.130", "100.64.0.10"})
    link = _link(c, hosts=["192.168.1.130"], clock=lambda: t[0])
    assert link.connect_round() and link.host == "192.168.1.130"
    t[0] += 100
    _drop_now(c)
    assert link.is_shaky("192.168.1.130")
    assert any("dropped after 100 s" in line for line in link.logs)
    assert link.candidates()[-1] == "192.168.1.130"
    link.good = "192.168.1.130"            # even the last good one waits its turn
    assert link.candidates()[-1] == "192.168.1.130"
    assert link.connect_round() and link.via == "tailscale"
    # no following "home" over the shaky way
    link.hub.note = {"wifi": "ExtenderWiFi", "lan": ["192.168.1.130"], "known": {}}
    broker_link.tcp_open = lambda *a, **k: True
    t[0] += broker_link.FOLLOW_EVERY + 1
    link.maybe_follow()
    assert link.connected and link.host == "100.64.0.10"


def test_shaky_time_out_doubles_then_clears_after_a_steady_hour():
    _resolve_everything()
    t = [1000.0]
    c = FakeClient(up={"192.168.1.130"})
    link = _link(c, hosts=["192.168.1.130"], tailscale="", clock=lambda: t[0])
    link.connect_round()
    t[0] += 60
    _drop_now(c)
    assert link.shaky_for["192.168.1.130"] == broker_link.SHAKY_FIRST
    t[0] += broker_link.SHAKY_FIRST + 1
    assert not link.is_shaky("192.168.1.130")
    assert link.connect_round()
    t[0] += 60
    _drop_now(c)
    assert link.shaky_for["192.168.1.130"] == 2 * broker_link.SHAKY_FIRST
    # nothing steadier around: the shaky way is still used
    assert link.connect_round() and link.host == "192.168.1.130"
    t[0] += broker_link.STEADY_AFTER
    _drop_now(c)
    assert "192.168.1.130" not in link.shaky_for
    assert link.shaky_for == {} or not link.is_shaky("192.168.1.130")


def test_a_name_that_leads_to_a_shaky_address_waits_too():
    _resolve_everything()          # chatty-node.local -> 192.168.1.110
    t = [1000.0]
    c = FakeClient(up={"chatty-node.local", "100.64.0.10"})
    link = _link(c, clock=lambda: t[0])
    link.shaky_until["192.168.1.110"] = t[0] + 999
    assert link.connect_round()
    assert link.host == "100.64.0.10"
    assert c.tried == ["chatty-node.local", "100.64.0.10"]


def test_moving_home_on_purpose_is_not_a_shaky_drop():
    _resolve_everything()
    t = [1000.0]
    c = FakeClient(up={"100.64.0.10", "192.168.1.130"})
    link = _link(c, hosts=["nowhere"], clock=lambda: t[0])
    c.up.discard("192.168.1.130")
    link.connect_round()
    assert link.via == "tailscale"
    c.up.add("192.168.1.130")
    link.hub.note = {"wifi": "ExtenderWiFi", "lan": ["192.168.1.130"], "known": {}}
    broker_link.tcp_open = lambda *a, **k: True
    t[0] += 30
    link.maybe_follow()
    assert not link.is_shaky("100.64.0.10") and link.good == "192.168.1.130"


# ----- never stuck (2026-11-19) -----

class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


class NoFilesClient(FakeClient):
    """This program ran out of open files: nothing can connect."""

    def connect(self, host, port, keepalive=30):
        self.tried.append(host)
        raise OSError(errno.EMFILE, "Too many open files")


def _stuck_link(client, outside=lambda hosts, port: "", files=lambda: (10, 1024), **kw):
    restarts = []
    clock = Clock()
    link = _link(client, clock=clock, restart=restarts.append, restart_after=900,
                 outside=outside, files=files, watchdog=False, **kw)
    link.restarts, link.t = restarts, clock
    return link


def test_says_why_it_cant_reach_her():
    _resolve_everything()
    link = _stuck_link(FakeClient(up=()))
    assert not link.connect_round()
    assert "last problem: 100.64.0.10: no answer" in link.logs[-1]


def test_out_of_open_files_restarts_after_three_rounds():
    _resolve_everything()
    link = _stuck_link(NoFilesClient())
    for _ in range(2):
        assert not link.connect_round()
        assert not link.check_stuck()
    assert not link.connect_round()
    assert link.check_stuck()
    assert len(link.restarts) == 1 and "Too many open files" in link.restarts[0]
    assert not link.alive()


def test_ordinary_failures_dont_count_as_out_of_files():
    _resolve_everything()
    link = _stuck_link(FakeClient(up=()))
    for _ in range(5):
        link.connect_round()
        assert not link.check_stuck()
    assert link.local_rounds == 0 and not link.restarts


def test_restarts_when_a_fresh_program_reaches_her():
    _resolve_everything()
    asked = []
    link = _stuck_link(FakeClient(up=()),
                       outside=lambda hosts, port: asked.append(hosts) or "192.168.1.110")
    link.connect_round()
    link.t.t += 14 * 60
    assert not link.check_stuck() and not asked        # not long enough yet
    link.t.t += 2 * 60
    assert link.check_stuck()
    assert asked and "chatty-node.local" in asked[0]
    assert "192.168.1.110" in link.restarts[0]


def test_waits_when_she_is_really_away():
    _resolve_everything()
    asked = []
    link = _stuck_link(FakeClient(up=()), outside=lambda hosts, port: asked.append(1) or "")
    link.t.t += 16 * 60
    assert not link.check_stuck() and len(asked) == 1
    assert "she's away" in link.logs[-1]
    link.t.t += 5 * 60
    assert not link.check_stuck() and len(asked) == 1   # not asked again so soon
    link.t.t += 11 * 60
    assert not link.check_stuck() and len(asked) == 2
    assert not link.restarts and link.alive()


def test_no_helper_program_means_restart():
    _resolve_everything()
    link = _stuck_link(FakeClient(up=()), outside=lambda hosts, port: None)
    link.t.t += 16 * 60
    assert link.check_stuck() and link.restarts


def test_connected_clears_the_clock_and_zero_means_never():
    _resolve_everything()
    c = FakeClient(up={"chatty-node.local"})
    link = _stuck_link(c)
    assert link.connect_round() and link.down_since is None
    link.t.t += 3600
    assert not link.check_stuck()
    never = _stuck_link(FakeClient(up=()), outside=lambda h, p: "192.168.1.110")
    never.restart_after = 0
    never.t.t += 24 * 3600
    assert not never.check_stuck() and not never.restarts


def test_an_open_file_leak_restarts_even_while_linked():
    _resolve_everything()
    c = FakeClient(up={"chatty-node.local"})
    link = _stuck_link(c, files=lambda: (950, 1024))
    link.connect_round()
    assert link.check_files() and "950 of its 1024" in link.restarts[0]
    fine = _stuck_link(FakeClient(up=()), files=lambda: (40, 1024))
    assert not fine.check_files()

    def cant_look():
        raise OSError(errno.EMFILE, "Too many open files")
    worse = _stuck_link(FakeClient(up=()), files=cant_look)
    assert worse.check_files() and worse.restarts


def test_counts_its_own_open_files():
    used, allowed = broker_link.files_in_use()
    if allowed is None:
        return
    if used is not None:
        assert 0 < used < allowed


def test_watchdog_pings_systemd_only_when_asked():
    import socket
    path = os.path.join(tempfile.mkdtemp(), "notify")
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    srv.bind(path)
    srv.settimeout(1)
    old = os.environ.get("NOTIFY_SOCKET")
    os.environ["NOTIFY_SOCKET"] = path
    try:
        quiet = _stuck_link(FakeClient())
        quiet.beat()
        on = _stuck_link(FakeClient())
        on.watchdog, on._last_ping = True, 0.0
        on.beat()
        assert srv.recv(64) == b"WATCHDOG=1"
        on.beat()                                   # at most every 10 s
        srv.settimeout(0.2)
        try:
            srv.recv(64)
            raise AssertionError("pinged twice")
        except socket.timeout:
            pass
    finally:
        srv.close()
        if old is None:
            os.environ.pop("NOTIFY_SOCKET", None)
        else:
            os.environ["NOTIFY_SOCKET"] = old


def test_alive_goes_false_when_the_thread_stops_moving():
    link = _stuck_link(FakeClient())
    assert link.alive()
    link.beat_at -= broker_link.ALIVE_WITHIN + 1
    assert not link.alive()


def test_outside_check_talks_to_a_real_board():
    import socket
    import threading
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(2)
    port = srv.getsockname()[1]
    got = []

    def board():
        conn, _ = srv.accept()
        with conn:
            got.append(conn.recv(64))
            conn.sendall(b"\x20\x02\x00\x00")
            got.append(conn.recv(8))
    t = threading.Thread(target=board, daemon=True)
    t.start()
    closed = socket.socket()
    closed.bind(("127.0.0.1", 0))
    dead_port = closed.getsockname()[1]
    closed.close()
    assert broker_link.outside_check(["127.0.0.1"], dead_port) == ""
    assert broker_link.outside_check(["127.0.0.1"], port) == "127.0.0.1"
    t.join(5)
    srv.close()
    assert got[0][0] == 0x10 and b"MQTT" in got[0] and b"probe-" in got[0]
    assert got[1] == b"\xe0\x00"                   # it said goodbye properly
    assert broker_link.outside_check([], port) == ""


def test_outside_check_without_a_helper():
    def no_spawn(*a, **k):
        raise OSError(errno.EMFILE, "Too many open files")
    assert broker_link.outside_check(["192.168.1.110"], 1883, run=no_spawn) is None


def test_restart_only_under_systemd():
    old = os.environ.pop("INVOCATION_ID", None)
    try:
        broker_link.restart_process("test")      # by hand: says so, keeps running
    finally:
        if old is not None:
            os.environ["INVOCATION_ID"] = old


def test_install_drop_in():
    root = tempfile.mkdtemp()
    ran = []
    path = broker_link.install("lightning-bluesky", root=root, reload=lambda *a, **k: ran.append(a))
    assert path.endswith("lightning-bluesky.service.d/recover.conf")
    text = open(path).read()
    assert "Restart=always" in text and "WatchdogSec=" in text and "BROKER_LINK_WATCHDOG=1" in text
    assert ran
    try:
        broker_link.install("../etc", root=root, reload=lambda *a, **k: None)
        raise AssertionError("took a path")
    except ValueError:
        pass


if __name__ == "__main__":
    import inspect
    real = (broker_link.tcp_open, broker_link.scan_home, broker_link.socket.getaddrinfo)
    names = [n for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for n in names:
        globals()[n]()
        broker_link.tcp_open, broker_link.scan_home, broker_link.socket.getaddrinfo = real
        print("ok", n)
    print(f"{len(names)} passed")
