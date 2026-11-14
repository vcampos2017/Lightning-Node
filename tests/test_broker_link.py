"""Tests for broker_link.py (finding Chatty's board on any WiFi). Plain Python,
no paho needed: a fake client stands in. Run: python3 tests/test_broker_link.py"""
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
        ip = "192.168.1.50" if host.endswith(".local") else host
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
    kw.setdefault("tailscale", "100.69.130.79")
    kw.setdefault("scan", False)
    link = broker_link.BrokerLink(client, hub=hub, log=logs.append, **kw)
    link.logs = logs
    return link


def test_kind_of():
    assert broker_link.kind_of("100.69.130.79") == "tailscale"
    assert broker_link.kind_of("192.168.1.50") == "lan"
    assert broker_link.kind_of("chatty-node.local") == "lan"


def test_falls_back_to_tailscale_when_home_name_fails():
    _resolve_everything()
    c = FakeClient(up={"100.69.130.79"})
    link = _link(c)
    assert link.connect_round()
    assert c.tried == ["chatty-node.local", "100.69.130.79"]
    assert link.via == "tailscale" and link.good == "100.69.130.79"
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
    c.on_message(c, None, Msg(broker_link.HUB_TOPIC, b'{"wifi":"GF-Home","lan":["192.168.1.50"]}'))
    assert seen == ["connect", "chatty/forecast/rain"]   # her note isn't passed on
    assert link.hub.wifi == "GF-Home"


def test_learns_her_addresses_and_remembers_them():
    _resolve_everything()
    c = FakeClient(up={"100.69.130.79"})
    link = _link(c)
    link.connect_round()
    note = {"host": "chatty-node", "wifi": "AVC-2G", "lan": ["192.168.1.77"],
            "tailscale": "100.69.130.79", "known": {"GF-Home": "192.168.1.50", "AVC-2G": "192.168.1.77"}}
    c.on_message(c, None, Msg(broker_link.HUB_TOPIC, json.dumps(note).encode()))
    assert any("AVC-2G" in line for line in link.logs)
    # a fresh start reads the note back from disk and tries her home addresses first
    again = broker_link.HubNote(link.hub.path)
    assert again.lan_all() == ["192.168.1.77", "192.168.1.50"]
    c2 = FakeClient(up={"192.168.1.50"})
    link2 = broker_link.BrokerLink(c2, hub=again, hosts=["chatty-node.local"],
                                   tailscale="100.69.130.79", scan=False, log=lambda t: None)
    assert link2.connect_round()
    assert c2.tried == ["192.168.1.77", "192.168.1.50"]
    assert link2.via == "lan"


def test_known_addresses_add_up_across_notes():
    hub = broker_link.HubNote(os.path.join(tempfile.mkdtemp(), "h.json"))
    hub.update(b'{"wifi":"GF-Home","lan":["192.168.1.50"],"known":{"GF-Home":"192.168.1.50"}}', 1)
    hub.update(b'{"wifi":"AVC-2G","lan":["192.168.1.77"],"known":{"AVC-2G":"192.168.1.77"}}', 2)
    assert hub.note["known"] == {"GF-Home": "192.168.1.50", "AVC-2G": "192.168.1.77"}
    assert not hub.update(b"not json", 3)
    assert not hub.update(b'{"wifi":"AVC-2G","lan":["192.168.1.77"],"known":{"x":"rm -rf"}}', 4)
    assert "x" not in hub.note["known"]


def test_follows_her_home_from_tailscale():
    _resolve_everything()
    c = FakeClient(up={"100.69.130.79", "192.168.1.77"})
    link = _link(c, hosts=["chatty-node.local"])
    link.hub.note = {"wifi": "AVC-2G", "lan": [], "known": {}}
    c.up.discard("192.168.1.77")
    assert link.connect_round() and link.via == "tailscale"
    link.hub.note = {"wifi": "AVC-2G", "lan": ["192.168.1.77"], "known": {}}
    broker_link.tcp_open = lambda host, port=1883, timeout=1.0: host == "192.168.1.77"
    c.up.add("192.168.1.77")
    link.maybe_follow()
    assert not link.connected and link.good == "192.168.1.77"
    assert link.connect_round() and link.via == "lan"
    assert c.tried[-1] == "192.168.1.77"


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
    c.on_message(c, None, Msg(broker_link.HUB_TOPIC, b'{"wifi":"AVC-2G","lan":["192.168.1.99"]}'))
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
    c = FakeClient(up={"100.69.130.79"})
    link = _link(c)
    link.connect_round()
    link.hub.note = {"wifi": "AVC-2G"}
    assert link.info() == {"via": "tailscale", "broker": "100.69.130.79", "chatty_wifi": "AVC-2G"}


def _drop_now(c):
    """The way to the board dies (what the broker calls 'exceeded timeout')."""
    c.host = None
    c.on_disconnect(c, None, {}, 7)


def test_early_drop_makes_the_way_shaky_and_tailscale_goes_first():
    _resolve_everything()
    t = [1000.0]
    c = FakeClient(up={"192.168.1.200", "100.69.130.79"})
    link = _link(c, hosts=["192.168.1.200"], clock=lambda: t[0])
    assert link.connect_round() and link.host == "192.168.1.200"
    t[0] += 100
    _drop_now(c)
    assert link.is_shaky("192.168.1.200")
    assert any("dropped after 100 s" in line for line in link.logs)
    assert link.candidates()[-1] == "192.168.1.200"
    link.good = "192.168.1.200"            # even the last good one waits its turn
    assert link.candidates()[-1] == "192.168.1.200"
    assert link.connect_round() and link.via == "tailscale"
    # no following "home" over the shaky way
    link.hub.note = {"wifi": "AVC-2G", "lan": ["192.168.1.200"], "known": {}}
    broker_link.tcp_open = lambda *a, **k: True
    t[0] += broker_link.FOLLOW_EVERY + 1
    link.maybe_follow()
    assert link.connected and link.host == "100.69.130.79"


def test_shaky_time_out_doubles_then_clears_after_a_steady_hour():
    _resolve_everything()
    t = [1000.0]
    c = FakeClient(up={"192.168.1.200"})
    link = _link(c, hosts=["192.168.1.200"], tailscale="", clock=lambda: t[0])
    link.connect_round()
    t[0] += 60
    _drop_now(c)
    assert link.shaky_for["192.168.1.200"] == broker_link.SHAKY_FIRST
    t[0] += broker_link.SHAKY_FIRST + 1
    assert not link.is_shaky("192.168.1.200")
    assert link.connect_round()
    t[0] += 60
    _drop_now(c)
    assert link.shaky_for["192.168.1.200"] == 2 * broker_link.SHAKY_FIRST
    # nothing steadier around: the shaky way is still used
    assert link.connect_round() and link.host == "192.168.1.200"
    t[0] += broker_link.STEADY_AFTER
    _drop_now(c)
    assert "192.168.1.200" not in link.shaky_for
    assert link.shaky_for == {} or not link.is_shaky("192.168.1.200")


def test_a_name_that_leads_to_a_shaky_address_waits_too():
    _resolve_everything()          # chatty-node.local -> 192.168.1.50
    t = [1000.0]
    c = FakeClient(up={"chatty-node.local", "100.69.130.79"})
    link = _link(c, clock=lambda: t[0])
    link.shaky_until["192.168.1.50"] = t[0] + 999
    assert link.connect_round()
    assert link.host == "100.69.130.79"
    assert c.tried == ["chatty-node.local", "100.69.130.79"]


def test_moving_home_on_purpose_is_not_a_shaky_drop():
    _resolve_everything()
    t = [1000.0]
    c = FakeClient(up={"100.69.130.79", "192.168.1.200"})
    link = _link(c, hosts=["nowhere"], clock=lambda: t[0])
    c.up.discard("192.168.1.200")
    link.connect_round()
    assert link.via == "tailscale"
    c.up.add("192.168.1.200")
    link.hub.note = {"wifi": "AVC-2G", "lan": ["192.168.1.200"], "known": {}}
    broker_link.tcp_open = lambda *a, **k: True
    t[0] += 30
    link.maybe_follow()
    assert not link.is_shaky("100.69.130.79") and link.good == "192.168.1.200"


if __name__ == "__main__":
    import inspect
    real = (broker_link.tcp_open, broker_link.scan_home, broker_link.socket.getaddrinfo)
    names = [n for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for n in names:
        globals()[n]()
        broker_link.tcp_open, broker_link.scan_home, broker_link.socket.getaddrinfo = real
        print("ok", n)
    print(f"{len(names)} passed")
