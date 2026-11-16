"""Tests for node_goodbye.py (a proper goodbye to Chatty on shutdown). Plain
Python, no paho needed: a fake client stands in.
Run: python3 tests/test_node_goodbye.py"""
import json
import os
import signal
import sys
import tempfile
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
for d in (os.path.join(HERE, ".."), HERE):
    if os.path.exists(os.path.join(d, "node_goodbye.py")):
        sys.path.insert(0, d)
        break
import node_goodbye  # noqa: E402

T0 = 1_000_000.0
TOPIC = "chatty/nodes/test/status"


class Info:
    def __init__(self, client):
        self.client = client

    def is_published(self):
        return self.client.acked

    def wait_for_publish(self, timeout=None):
        self.client.calls.append("wait")
        self.client.acked = True


class Client:
    def __init__(self, ack_after_loops=1):
        self.calls, self.sent = [], []
        self.acked = False
        self.ack_after = ack_after_loops

    def publish(self, topic, payload, qos=0, retain=False):
        self.calls.append("publish")
        self.sent.append((topic, json.loads(payload), qos, retain))
        return Info(self)

    def loop(self, timeout=1.0):
        self.calls.append("loop")
        self.ack_after -= 1
        if self.ack_after <= 0:
            self.acked = True
        return 0

    def disconnect(self):
        self.calls.append("disconnect")

    def loop_stop(self):
        self.calls.append("loop_stop")


class Link:
    def __init__(self, connected=True):
        self.connected = connected
        self.stopped = False


def test_reason_from_systemd_jobs():
    r = node_goodbye.reason_from_jobs
    assert r("1 reboot.target start waiting\n2 shutdown.target start waiting\n") == "reboot"
    assert r("5 poweroff.target start waiting\n") == "shutdown"
    assert r("9 rain-mqtt.service restart running\n") == "restart"
    assert r("") == "stop"

    class Out:
        stdout = "7 halt.target start waiting\n"
    assert node_goodbye.stop_reason(run=lambda *a, **k: Out()) == "shutdown"

    def boom(*a, **k):
        raise OSError("no systemctl")
    assert node_goodbye.stop_reason(run=boom) == "stop"


def test_message_and_away_note():
    with tempfile.TemporaryDirectory() as d:
        path = os.path.join(d, "away.json")
        old = node_goodbye.AWAY_FILE
        node_goodbye.AWAY_FILE = path
        os.environ.pop("NODE_BACK_MINUTES", None)
        try:
            body = node_goodbye.message("Rain node", 60, reason="reboot", now=T0)
            assert body == {"state": "offline", "clean": True, "reason": "reboot",
                            "ts": int(T0), "every": 60, "title": "Rain node"}
            node_goodbye.set_away(180, now=T0)
            body = node_goodbye.message("Rain node", reason="shutdown", now=T0 + 60)
            assert body["back_within"] == 180 * 60
            # an old note doesn't count
            assert node_goodbye.away_minutes(now=T0 + 3 * 3600) is None
            node_goodbye.set_away(99999, now=T0)
            assert node_goodbye.away_minutes(now=T0) == node_goodbye.MAX_MINUTES
            os.environ["NODE_BACK_MINUTES"] = "30"
            assert node_goodbye.away_minutes(now=T0 + 3 * 3600) == 30
        finally:
            os.environ.pop("NODE_BACK_MINUTES", None)
            node_goodbye.AWAY_FILE = old


def test_leave_with_broker_link_stops_it_and_skips_the_will():
    c, link = Client(ack_after_loops=2), Link()
    body = node_goodbye.message("Rain node", reason="reboot", now=T0, away=None)
    assert node_goodbye.leave(c, TOPIC, body, link=link) is True
    assert link.stopped and not link.connected
    topic, sent, qos, retain = c.sent[0]
    assert topic == TOPIC and qos == 1 and retain and sent["clean"] is True
    assert c.calls == ["publish", "loop", "loop", "disconnect", "loop"]


def test_leave_waits_for_a_running_link_thread():
    c, link = Client(), Link()
    gate = threading.Event()
    t = threading.Thread(target=gate.wait, args=(5,), name="broker-link", daemon=True)
    t.start()
    threading.Timer(0.2, gate.set).start()
    assert node_goodbye.leave(c, TOPIC, {"state": "offline"}, link=link) is True
    assert not t.is_alive()


def test_leave_without_link_and_when_not_connected():
    c = Client()
    assert node_goodbye.leave(c, TOPIC, {"state": "offline"}, connected=True) is True
    assert c.calls == ["publish", "wait", "disconnect", "loop_stop"]
    c = Client()
    assert node_goodbye.leave(c, TOPIC, {}, link=Link(connected=False)) is False
    assert c.calls == []
    assert node_goodbye.leave(None, TOPIC, {}) is False

    class Broken(Client):
        def publish(self, *a, **k):
            raise RuntimeError("socket gone")
    assert node_goodbye.leave(Broken(), TOPIC, {}, connected=True) is False


def test_on_stop_says_goodbye_once_then_exits():
    said, then = [], []
    old = signal.getsignal(signal.SIGTERM)
    try:
        node_goodbye.on_stop(lambda: said.append(1), then=lambda s, f: then.append(s))
        handler = signal.getsignal(signal.SIGTERM)
        for _ in range(2):
            try:
                handler(signal.SIGTERM, None)
                assert False, "should exit"
            except SystemExit:
                pass
        assert said == [1] and then == [signal.SIGTERM, signal.SIGTERM]

        def broken():
            raise RuntimeError("no network")
        node_goodbye.on_stop(broken)
        try:
            signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)
            assert False, "should exit"
        except SystemExit:
            pass
    finally:
        signal.signal(signal.SIGTERM, old)


def test_install_writes_the_drop_in():
    with tempfile.TemporaryDirectory() as d:
        ran = []
        path = node_goodbye.install("rain-mqtt", root=d, reload=lambda *a, **k: ran.append(a))
        assert path == os.path.join(d, "rain-mqtt.service.d", "goodbye.conf")
        text = open(path).read()
        assert "tailscaled.service" in text and "TimeoutStopSec" in text
        assert ran == [(["systemctl", "daemon-reload"],)]
        for bad in ("../x", "a/b"):
            try:
                node_goodbye.install(bad, root=d, reload=lambda *a, **k: None)
                assert False, "should refuse"
            except ValueError:
                pass


if __name__ == "__main__":
    import inspect
    names = [n for n, f in sorted(globals().items()) if n.startswith("test_") and inspect.isfunction(f)]
    for n in names:
        globals()[n]()
        print("ok", n)
    print(f"{len(names)} passed")
