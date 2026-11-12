"""BloomGauge control over the app's REAL client (agent_mcp.py from the app bundle, loaded the way the
daemon loads it) against a fake BloomGauge HTTP server on an ephemeral port (BLOOMGAUGE_PORT), so the
seams the other tests stub out are checked end to end: the client's error text for a refused or hung
app (policy NO_ANSWER_RE / the app-down wait), the app's own 429 text (collector.py; policy
RATE_LIMITED_RE and its backoff), and the turn-on's operation id (request_id -> the app's answer ->
control.op_id_of -> tick.settle). The 2026-11-03 ringer burst. Skipped when no BloomGauge app is
installed. Never reaches the real app: every test asserts the client's port is the fake's first, and
the agent key the client sends is a dummy file the fake names."""
import json
import socket
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

import pytest

import bloomgauge_control as C
import bloomgauge_policy as P
import bloomgauge_tick as T

NOW = 100000.0
REAL_PORT = 8765          # agent_mcp.DEFAULT_PORT: the real app; never used here
IDLE = {"busy": False, "why": "queue done", "target": None}
BUSY = {"busy": True, "why": "1 lane row(s)", "target": None}
DUMMY_KEY = "ringer-dummy-agent-key"


def app_429(minutes=37):
    """BloomGauge's own 429 answer (collector.py, agent_access CHANGES_PER_HOUR 2 / SETTINGS_PER_HOUR 6)."""
    return {"error": "Agents can switch models or restart Darkbloom at most 2 times an hour, and change the "
                     f"Manager at most 6 times. Try again in about {minutes} min.",
            "retryAfterMinutes": minutes}


class App:
    """The fake app's state. mode: 'ok' answers; '429' refuses every change with the app's 429; 'hang'
    holds every request until release (or 5 s) without answering."""

    def __init__(self, key_path):
        self.mode = "ok"
        self.key_path = key_path
        self.manager_on = False
        self.op = None
        self.events = []
        self.clock = NOW
        self.posts = []           # (path, body, action header, key sent)
        self.gets = []
        self.release = threading.Event()

    def pending(self):
        return self.op is not None and self.op["status"] in P.OP_RUNNING

    def optimizer(self):
        return {"automatic": {"mode": "on" if (self.manager_on or self.pending()) else "manual",
                              "canEnable": not self.pending(), "blocker": None},
                "operation": dict(self.op or {}), "controlVersion": "cv-1", "currentModel": "m1"}

    def activate(self):
        self.op = dict(self.op, status="active", detail="On.")
        self.manager_on = True
        self.events.append({"at": self.clock, "event": "mode.change", "fields": {"from": "off", "to": "on", "why": "user-on"}})


def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):        # quiet, and never a header in a log
            pass

        def _hold(self):
            if app.mode == "hang":
                app.release.wait(5.0)
                return True
            return False

        def _send(self, code, body):
            data = json.dumps(body).encode()
            try:
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            except OSError:               # the client gave up (hang)
                pass

        def do_GET(self):
            path = urlsplit(self.path).path
            app.gets.append(path)
            if self._hold():
                return
            if path == "/api/model-control":
                return self._send(200, {"currentModel": "m1", "servingModels": ["m1"],
                                        "providerControl": {"status": "running", "operationPending": False}})
            if path == "/api/optimizer/control":
                return self._send(200, app.optimizer())
            if path == "/api/agent-access":
                return self._send(200, {"enabled": True, "keyPath": app.key_path})
            if path == "/api/activity/log":
                return self._send(200, {"events": list(app.events)})
            return self._send(404, {"error": "no such route"})

        def do_POST(self):
            path = urlsplit(self.path).path
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            sent_key = any(DUMMY_KEY in str(v) for v in self.headers.values())
            app.posts.append((path, body, self.headers.get("X-Bloom-Action"), sent_key))
            if self._hold():
                return
            if app.mode == "429":
                return self._send(429, app_429())
            if path != "/api/optimizer/control" or body.get("action") != "set-automatic":
                return self._send(400, {"error": "not a change this fake makes"})
            rid = body.get("requestId")
            app.events += [{"at": app.clock, "event": "agent.request", "fields": {"what": "manager", "model": None, "result": "ok"}},
                           {"at": app.clock, "event": "control.request",
                            "fields": {"what": "manager", "on": "yes" if body.get("enabled") else "no",
                                       "why": "user", "result": "ok", "from": "mac"}}]
            if body.get("enabled"):
                app.op = {"id": rid, "status": "pending", "detail": "Checking readiness before turning On."}
                return self._send(200, {"lastRequestId": rid, "operation": dict(app.op), "automatic": {"mode": "on"}})
            if app.manager_on:
                app.events.append({"at": app.clock + 1, "event": "mode.change",
                                   "fields": {"from": "on", "to": "off", "why": "user-off"}})
            app.manager_on = False
            return self._send(200, {"lastRequestId": rid, "operation": dict(app.op or {})})
    return Handler


@pytest.fixture
def client():
    mod = C.load_client()
    if mod is None:
        pytest.skip("no BloomGauge app (agent_mcp.py) installed")
    return mod


@pytest.fixture
def app(tmp_path, monkeypatch, client):
    key = tmp_path / "agent.key"
    key.write_text(DUMMY_KEY)
    a = App(str(key))
    srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(a))
    srv.daemon_threads = True
    th = threading.Thread(target=srv.serve_forever, daemon=True)
    th.start()
    a.port = srv.server_address[1]
    point(monkeypatch, client, a.port)
    yield a
    a.release.set()
    srv.shutdown()
    srv.server_close()


@pytest.fixture(autouse=True)
def fresh_state():
    T._STATE.update(idle_since=None, next_poll=0.0, alert=None, log_bad_since=None, log_bad_n=0, log_alerted=False)
    deliver = T._STATE.pop("deliver", None)
    yield
    T._STATE.update(idle_since=None, next_poll=0.0, alert=None, log_bad_since=None, log_bad_n=0, log_alerted=False)
    if deliver is not None:
        T._STATE["deliver"] = deliver


def point(monkeypatch, client, port):
    """Aim the real client at port, and prove it before any request: never the real app."""
    monkeypatch.setenv("BLOOMGAUGE_PORT", str(port))
    assert port != REAL_PORT and client.port() == port


def closed_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def cfg(tmp_path):
    return C.config({"QUEUE_BLOOMGAUGE_CONTROL": "on", "QUEUE_BG_IDLE_MIN": "1", "QUEUE_BG_QUIET_MIN": "0"}, tmp_path)


def rows(c, phase=None):
    try:
        got = [json.loads(x) for x in open(c["ledger_path"]).read().splitlines()]
    except FileNotFoundError:
        got = []
    return [r for r in got if phase is None or r.get("phase") == phase]


def holding(c, app):
    """The queue turned the Manager off 600 s ago for its work and holds it (as after the ringer's acquire)."""
    for phase in ("attempt", "result"):
        r = {"ts": NOW - 600.0, "action": "mgr_off", "model": None, "mode": "on", "phase": phase,
             "reason": "acquire: queue work; Manager off"}
        if phase == "result":
            r.update(result="ok", err=None)
        C._ledger_append(c["ledger_path"], r)
    app.events += [{"at": NOW - 600.0, "event": "agent.request", "fields": {"what": "manager", "model": None, "result": "ok"}},
                   {"at": NOW - 600.0, "event": "control.request",
                    "fields": {"what": "manager", "on": "no", "why": "user", "result": "ok", "from": "mac"}},
                   {"at": NOW - 599.0, "event": "mode.change", "fields": {"from": "on", "to": "off", "why": "user-off"}}]
    lease = {"state": "HELD", "since": NOW - 600.0, "target": None, "prior_manager_on": True,
             "mgr_off_at": NOW - 600.0, "heartbeat": NOW - 30.0, "mode": "on"}
    C.lease_save(c["lease_path"], lease)
    return lease


def step(c, client, app, now, work):
    app.clock = now
    return T.tick(work, now, c, client)


# ---------------------------------------------------------------- BloomGauge down
def test_a_refused_connection_waits_with_the_lease_kept_and_resumes_when_the_app_is_back(tmp_path, monkeypatch, client, app):
    c = cfg(tmp_path)
    holding(c, app)
    point(monkeypatch, client, closed_port())                       # the app is not running
    for t in (0.0, 30.0, 60.0):
        d = T.tick(BUSY, NOW + t, c, client)
        assert d == {"action": "wait", "model": None, "reason": "app-down: BloomGauge not answering"}
        lease = C.lease_load(c["lease_path"])
        assert lease["state"] == "HELD" and lease["since"] == NOW - 600.0
    assert [r["phase"] for r in rows(c)][2:] == ["decision"]          # one row for the outage, no write attempted
    assert app.posts == [] and app.gets == []
    point(monkeypatch, client, app.port)                             # the app is back, Manager still off
    d = step(c, client, app, NOW + 90.0, BUSY)
    assert d["action"] == "none" and d["reason"] == "held" and app.posts == []
    assert C.lease_load(c["lease_path"])["state"] == "HELD"


def test_a_hung_app_times_out_as_app_down_without_holding_the_tick(tmp_path, monkeypatch, client, app):
    c = cfg(tmp_path)
    holding(c, app)
    monkeypatch.setattr(client, "TIMEOUT", 0.3)
    app.mode = "hang"
    t0 = time.monotonic()
    d = step(c, client, app, NOW, BUSY)
    took = time.monotonic() - t0
    assert d["reason"] == "app-down: BloomGauge not answering"
    assert took < 3.0, took                                          # 2 timed-out reads, not the app's 25 s each
    assert app.posts == [] and C.lease_load(c["lease_path"])["state"] == "HELD"


# ---------------------------------------------------------------- BloomGauge refusing changes (its hourly limit)
def test_the_apps_429_backs_off_at_least_its_window_then_the_change_goes_through(tmp_path, client, app):
    c = cfg(tmp_path)
    app.manager_on, app.mode = True, "429"
    d = step(c, client, app, NOW, BUSY)
    assert d["action"] == "mgr_off"
    (res,) = rows(c, "result")
    assert res["result"] == "failed" and P.RATE_LIMITED_RE.search(res["err"]), res["err"]
    assert len(app.posts) == 1 and app.posts[0][2] == "optimizer" and app.posts[0][3]   # the app's route + key
    bud = P.budget(rows(c), None, NOW + 1.0, c)
    assert bud["mgr_fails"] == 1 and bud["mgr_retry_at"] >= NOW + P.RATE_LIMITED_MIN_S
    for t in range(30, int(P.RATE_LIMITED_MIN_S), 30):               # every tick inside the window: no write
        d = step(c, client, app, NOW + t, BUSY)
        assert d["action"] == "wait" and d["reason"] == "acquire: last Manager change failed; backing off", (t, d)
    assert len(app.posts) == 1 and T._STATE["alert"] is None        # one refusal, no alert
    app.mode = "ok"                                                  # the app's window has passed
    d = step(c, client, app, NOW + P.RATE_LIMITED_MIN_S + 30.0, BUSY)
    assert d["action"] == "mgr_off" and rows(c, "result")[-1]["result"] == "ok" and app.manager_on is False
    assert C.lease_load(c["lease_path"]) is not None


# ---------------------------------------------------------------- the turn-on's operation id, end to end
def test_the_hand_back_follows_the_operation_the_real_client_started(tmp_path, client, app):
    c = cfg(tmp_path)
    lease = holding(c, app)
    C.lease_save(c["lease_path"], dict(lease, state="DRAIN", idle_since=NOW - 2000.0))
    d = step(c, client, app, NOW, IDLE)
    assert d["action"] == "mgr_on"
    (path, body, action, sent_key) = app.posts[-1]
    assert (path, body["action"], body["enabled"], action, sent_key) == ("/api/optimizer/control", "set-automatic", True, "optimizer", True)
    assert rows(c, "result")[-1]["op_id"] == body["requestId"] == app.op["id"]
    assert C.lease_load(c["lease_path"])["state"] == "HANDBACK"
    d = step(c, client, app, NOW + 30.0, IDLE)
    assert d["reason"] == "handback: BloomGauge is turning the Manager on" and len(app.posts) == 1
    app.clock = NOW + 40.0
    app.activate()
    d = step(c, client, app, NOW + 60.0, IDLE)
    assert d["reason"] == "handback: Manager already on; released" and C.lease_load(c["lease_path"]) is None
    assert [(r["op_id"], r["status"]) for r in rows(c, "outcome")] == [(app.op["id"], "active")]
    assert len(app.posts) == 1
