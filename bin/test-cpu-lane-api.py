#!/usr/bin/env python3
"""test-cpu-lane-api.py -- the queue-side /api/cpu/* routes served by the REAL dashboard
Handler (ollama-queue-api.py) against a throwaway store + token (CPU LANE, 2026-10-08).

Proves: bearer token on every route but health (401 without / wrong / empty-token-file),
requests carrying Cloudflare-edge headers or a public Host are refused even WITH a valid
token (403), the full producer/runner flow, atomic oldest-first claim, heartbeat cancel,
409 on a stale lease, lease expiry -> pending -> (3rd) failed_infra, release, cancel of
pending jobs, durability across an API restart (jobs, blobs, leases), the read-only
dashboard feed leaks neither token nor command, and the token file is created 0600.

Remote lane (2026-11-04, remote_lane()): spec.setup/llm/artifacts/log_stream validation, caps-gated claims and why a
pending job waits, the Darkbloom Sandbox hold (claims, views, the dashboard feed, the kill switch), heartbeat phase /
progress and the cut lease of a cancelled job, the streamed log, the artifacts tarball, and the model relay
(jobs/<id>/llm/v1/*) against a FAKE loopback endpoint and a FAKE local.json and hold view: the Darkbloom key is added
on this side and cut from every answer, retries, timeouts, redirects, size caps, budgets, concurrency, holds met in
flight, lost leases, the models list cache, and the relay's own admission pool. Never touches real Darkbloom.

Runner limits (2026-11-05, the "limits" section): what a runner reports on every claim -- accept (all | plain | lane |
none: plain leaves spec.setup / spec.llm jobs alone, lane takes nothing else (a lane-only runner, CPU_RUNNER_ACCEPT=lane:
a skipped plain job stays pending, its attempt unchanged), none claims nothing, absent or unknown = all), hold ({all,
lane, plain} reasons, replaced by each claim, bad shapes dropped, 300 characters), lane_max (integers 0..256, never a
bool), disk (numeric fields, bad keys / bools / NaN / inf / strings dropped, shown with its age for 900 s) -- stored even
when the claim answers 204, shown in the dashboard feed (runners[].hold / lane_max / lane_busy / disk / disk_s_ago,
running[].lane), and turned into the reason a pending job waits ("rid1: why; rid2: why" in runner_id order, a runner
without the job's caps left out, a Darkbloom hold first; hold all covers every job, lane a lane job, plain any other, so a
lane-only runner explains the plain jobs it leaves to the main runner).

Usage: test-cpu-lane-api.py [--api PATH]. Exit 0 = all pass.
"""
import argparse
import base64
import gzip
import http.client
import http.server
import importlib.util
import json
import os
import socket
import stat
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


class Srv:
    def __init__(self, api_mod):
        self.api = api_mod
        self.srv = api_mod.ThreadingServer(("127.0.0.1", 0), api_mod.Handler)
        self.port = self.srv.server_address[1]
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()

    def stop(self):
        self.srv.shutdown()
        self.srv.server_close()


def call_h(port, method, path, body=None, raw=None, token="TOKEN", headers=None, as_bytes=False, timeout=20):
    """(status, JSON body or raw bytes, {lowercased header: value})."""
    h = {}
    if token:
        h["Authorization"] = "Bearer " + token
    h.update(headers or {})
    data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
    if data is not None:
        h["Content-Length"] = str(len(data))
    c = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    c.request(method, path, body=data, headers=h)
    r = c.getresponse()
    b = r.read()
    hdrs = {k.lower(): v for k, v in r.getheaders()}
    c.close()
    if as_bytes:
        return r.status, b, hdrs
    try:
        return r.status, (json.loads(b) if b else None), hdrs
    except ValueError:
        return r.status, b, hdrs


def call(port, method, path, body=None, raw=None, token="TOKEN", headers=None):
    st, b, _ = call_h(port, method, path, body, raw, token, headers)
    return st, b


# ---- remote lane fakes: the model endpoint, its local.json and the Darkbloom Sandbox hold are all FAKE --------------
FAKE_KEY = "sk-fake-darkbloom-KEY-7f3a9c"
MODEL = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
CHAT = {"model": MODEL, "messages": [{"role": "user", "content": "hi"}]}
HOLD = {"on": False, "cause": "", "calls": 0, "raise": False}


def fake_view(*a, **k):
    """Stands in for ollama-queue.py darkbloom_view (the queue's hold reading): the test drives the hold."""
    HOLD["calls"] += 1
    if HOLD["raise"]:
        raise RuntimeError("view broke")
    return {"hold": HOLD["on"], "cause": HOLD["cause"]}


def wait_for(pred, timeout=8.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            if pred():
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.02)
    return False


def bg(fn):
    """Run fn() in a thread -> (thread, {"r": result} | {"e": error})."""
    out = {}

    def run():
        try:
            out["r"] = fn()
        except Exception as e:  # noqa: BLE001
            out["e"] = repr(e)
    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, out


def ecode(r):
    """(status, OpenAI-style error code) of a call_h result."""
    b = r[1]
    err = b.get("error") if isinstance(b, dict) else None
    return r[0], (err.get("code") if isinstance(err, dict) else None)


def emsg(r):
    b = r[1]
    err = b.get("error") if isinstance(b, dict) else None
    return err.get("message") if isinstance(err, dict) else None


class FakeUp(http.server.ThreadingHTTPServer):
    """A FAKE local model endpoint on loopback: records every request, answers by mode (seq first, then mode)."""
    daemon_threads = True
    block_on_close = False

    def __init__(self):
        self.reqs, self.lock, self.seq = [], threading.Lock(), []
        self.mode, self.models_mode, self.slow_s, self.big_n = "ok", "ok", 3.0, 5000
        fake = self

        class H(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.0"

            def log_message(self, *a):
                pass

            def _out(self, code, obj=None, raw=None, headers=None):
                data = raw if raw is not None else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _go(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n) if n else b""
                with fake.lock:
                    fake.reqs.append((self.command, self.path, {k.lower(): v for k, v in self.headers.items()}, body))
                    if self.path.endswith("/models"):
                        mode = "models-" + fake.models_mode
                    else:
                        mode = fake.seq.pop(0) if fake.seq else fake.mode
                if mode == "models-down":
                    return self._out(503, {"error": {"message": "loading"}})
                if mode.startswith("models-"):
                    return self._out(200, {"object": "list", "data": [
                        {"id": MODEL, "object": "model", "note": "served with " + FAKE_KEY},
                        {"id": "Other-Model", "object": "model"}, {"id": 7}, "junk"]})
                if mode == "slow":
                    time.sleep(fake.slow_s)
                    mode = "ok"
                if mode == "ok":
                    return self._out(200, {"id": "c1", "object": "chat.completion", "model": MODEL,
                                           "echo_auth": self.headers.get("Authorization"),
                                           "choices": [{"index": 0, "finish_reason": "stop",
                                                        "message": {"role": "assistant", "content": "hello"}}]})
                if mode == "503":
                    return self._out(503, {"error": {"message": "model loading"}}, headers={"Retry-After": "1"})
                if mode == "400key":
                    return self._out(400, {"error": {"message": "bad request for key " + FAKE_KEY}})
                if mode == "429":
                    return self._out(429, {"error": {"message": "slow down"}}, headers={"Retry-After": "7"})
                if mode == "429date":
                    return self._out(429, {"error": {"message": "slow down"}},
                                     headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})
                if mode == "redirect":
                    return self._out(302, raw=b"", headers={"Location": "http://127.0.0.1:%d/followed" % fake.port})
                if mode == "big":
                    return self._out(200, raw=b"x" * fake.big_n)
                return self._out(500, {"error": {"message": "boom"}})

            do_GET = do_POST = _go

        super().__init__(("127.0.0.1", 0), H)
        self.port = self.server_address[1]
        threading.Thread(target=self.serve_forever, daemon=True).start()

    def handle_error(self, request, client_address):
        pass                                   # a client that hung up on a slow answer

    def all_reqs(self):
        with self.lock:
            return list(self.reqs)

    def chat_reqs(self):
        return [r for r in self.all_reqs() if not r[1].endswith("/models")]

    def models_reqs(self):
        return [r for r in self.all_reqs() if r[1].endswith("/models")]

    def stop(self):
        self.shutdown()
        self.server_close()


JOB_VIEW_KEYS = sorted(["id", "status", "attempt", "result", "runner_id", "phase", "progress", "progress_t", "log_size",
                        "has_artifacts", "llm_calls", "llm_errors", "llm_held", "llm_wait_s", "cancel", "model_hold",
                        "waiting"])
KNOBS = ("HOLD_TTL_S", "HOLD_POLL_S", "_LLM_DELAYS", "LLM_RETRY_S", "LLM_TIMEOUT_S", "LLM_RESP_MAX", "LLM_BODY_MAX",
         "LOG_MAX", "LOG_GET_MAX", "ARTIFACTS_MAX")


def remote_lane(api, cpu_lane, tmp):
    """Remote lane jobs (spec.setup/llm/artifacts/log_stream) through the REAL Handler, each section on a fresh store.
    The model endpoint, its local.json and the hold view are fakes; the knobs are shrunk so the waits take ~1 s."""
    fake = FakeUp()
    for k in ("DARKBLOOM_BASE_URL", "DARKBLOOM_LOCAL_JSON"):
        os.environ.pop(k, None)
    lj = tmp / "darkbloom" / "local.json"
    lj.parent.mkdir(parents=True, exist_ok=True)
    os.environ["CPU_LANE_LLM_LOCAL_JSON"] = str(lj)
    good = {"api_key": FAKE_KEY, "base_url": "http://127.0.0.1:%d/v1" % fake.port}

    def put_lj(obj=None, text=None):
        lj.write_text(text if text is not None else json.dumps(good if obj is None else obj))

    put_lj()
    saved = {k: getattr(cpu_lane, k) for k in KNOBS}
    cpu_lane.HOLD_TTL_S, cpu_lane.HOLD_POLL_S = 0, 0.2
    cpu_lane._LLM_DELAYS, cpu_lane.LLM_RETRY_S, cpu_lane.LLM_TIMEOUT_S = (0.05, 0.1, 0.1, 0.15), 0.8, 6.0
    delays = cpu_lane._LLM_DELAYS
    S = Srv(api)
    p = S.port
    J = "/api/cpu/jobs/"
    base = {"cmd": "python3 run.py", "cwd": ".", "timeout_s": 600}
    ALL = {"setup_phase": True, "llm_relay": True, "artifacts": True, "log_stream": True}
    tgz = gzip.compress(b"artifact bytes " * 100)

    def b64(s):
        return base64.b64encode(s if isinstance(s, bytes) else s.encode()).decode()

    def fresh(name):
        cpu_lane._store = cpu_lane.Store(tmp / ("rl-" + name))
        cpu_lane._llm_models.update(t=None, base=None, data=None)
        cpu_lane._llm_good.update(rec=None, t=0.0)
        cpu_lane._llm_inflight.clear()
        HOLD.update({"on": False, "cause": "", "raise": False})
        api.q.SANDBOX_HOLD_WAIT = True
        fake.mode, fake.models_mode, fake.seq[:] = "ok", "ok", []

    def post_spec(extra):
        return call(p, "POST", "/api/cpu/jobs", {"spec": dict(base, **extra), "bundle_id": "bRL"})

    def mk(extra=None, label="rl"):
        c, b = call(p, "POST", "/api/cpu/jobs", {"spec": dict(base, **(extra or {})), "label": label,
                                                 "stage": "verify", "bundle_id": "bRL"})
        assert c == 201, (c, b)
        jid = b["id"]
        assert call(p, "PUT", J + jid + "/payload", raw=b"P")[0] == 200
        assert call(p, "POST", J + jid + "/ready", {})[0] == 200
        return jid

    OMIT = object()          # a limits field left out of the claim body (None sends a JSON null)

    def claim_c(rid, caps=None, lease_s=60, slots=None, accept=OMIT, hold=OMIT, lane_max=OMIT, disk=OMIT):
        """POST /api/cpu/claim -> (status, body).  The runner's limits (accept / hold / lane_max / disk) go out as given."""
        body = {"runner_id": rid, "lease_s": lease_s}
        if caps is not None:
            body["caps"] = caps
        if slots is not None:
            body["slots"] = slots
        for k, v in (("accept", accept), ("hold", hold), ("lane_max", lane_max), ("disk", disk)):
            if v is not OMIT:
                body[k] = v
        return call(p, "POST", "/api/cpu/claim", body)

    def claim(rid, **kw):
        c, b = claim_c(rid, **kw)
        return b["job"] if c == 200 else None

    def view(jid):
        return call(p, "GET", J + jid)[1]

    def chat(jid, lease, body=None, raw=None, method="POST", sub="chat/completions", headers=None, timeout=20):
        h = dict(headers or {})
        if lease is not None:
            h["X-Lease-Token"] = lease
        if method == "POST" and body is None and raw is None:
            body = CHAT
        return call_h(p, method, J + jid + "/llm/v1/" + sub, body=body, raw=raw, headers=h, timeout=timeout)

    def lanewait(jid, lease):
        r = chat(jid, lease, method="GET", sub="models")
        return (r[1] or {}).get("cpu_lane") if r[0] == 200 and isinstance(r[1], dict) else r

    def section(name, fn):
        try:
            fn()
        except Exception:  # noqa: BLE001
            check("remote lane section %r ran to the end" % name, traceback.format_exc(limit=4), "")

    # ---- 1. spec validation ------------------------------------------------------------------------------------------
    def s_spec():
        fresh("spec")
        cmd_t = "spec.setup.cmd: a non-empty string or list of strings"
        to_t = "spec.setup.timeout_s: a positive integer"
        llm_t = "spec.llm: true or {models, max_calls, max_concurrency}"
        ms_t = "spec.llm.models: a list of 1-16 model ids"
        bad = [({"setup": "  "}, "spec.setup: empty command"),
               ({"setup": {"cmd": "x", "shell": True}}, "spec.setup: unknown key(s) shell"),
               ({"setup": {"cmd": ""}}, cmd_t), ({"setup": {"cmd": []}}, cmd_t), ({"setup": {"cmd": ["npm", 1]}}, cmd_t),
               ({"setup": {"cmd": "npm ci", "timeout_s": 0}}, to_t), ({"setup": {"cmd": "npm ci", "timeout_s": True}}, to_t),
               ({"setup": {"cmd": "npm ci", "timeout_s": "60"}}, to_t),
               ({"setup": 5}, "spec.setup: a command string or {cmd, timeout_s}"),
               ({"llm": {}}, llm_t), ({"llm": "yes"}, llm_t),
               ({"llm": {"models": [MODEL], "key": "x"}}, "spec.llm: unknown key(s) key"),
               ({"llm": {"models": []}}, ms_t), ({"llm": {"models": ["m"] * 17}}, ms_t), ({"llm": {"models": [""]}}, ms_t),
               ({"llm": {"models": ["m" * 201]}}, ms_t), ({"llm": {"models": MODEL}}, ms_t),
               ({"llm": {"max_calls": 0}}, "spec.llm.max_calls: an integer 1..2000"),
               ({"llm": {"max_calls": 2001}}, "spec.llm.max_calls: an integer 1..2000"),
               ({"llm": {"max_calls": True}}, "spec.llm.max_calls: an integer 1..2000"),
               ({"llm": {"max_concurrency": 5}}, "spec.llm.max_concurrency: an integer 1..4"),
               ({"llm": {"max_concurrency": 1.5}}, "spec.llm.max_concurrency: an integer 1..4"),
               ({"artifacts": "yes"}, "spec.artifacts: true or false"), ({"artifacts": 1}, "spec.artifacts: true or false"),
               ({"log_stream": "on"}, "spec.log_stream: true or false")]
        miss = []
        for x, want in bad:
            got = post_spec(x)
            if got != (400, {"error": want}):
                miss.append((x, got))
        check("spec: %d malformed remote lane fields are each refused with their own 400 text" % len(bad), miss, [])
        goods = [{"setup": "npm ci"}, {"setup": {"cmd": ["npm", "ci"], "timeout_s": 900}}, {"setup": None},
                 {"setup": False}, {"llm": True}, {"llm": False}, {"llm": None},
                 {"llm": {"models": [MODEL], "max_calls": 2000, "max_concurrency": 4}},
                 {"artifacts": True, "log_stream": False}]
        check("spec: well formed (and switched-off) remote lane fields are accepted",
              [post_spec(x)[0] for x in goods], [201] * len(goods))
        check("spec: cmd is still required", call(p, "POST", "/api/cpu/jobs", {"spec": {"llm": True}}),
              (400, {"error": "spec.cmd required"}))

    # ---- 2. the pure helpers ------------------------------------------------------------------------------------------
    def s_units():
        check("spec_needs: the runner caps a spec needs, in a stable order",
              [cpu_lane.spec_needs(x) for x in ({}, None, {"llm": True},
                                                {"log_stream": True, "artifacts": True, "llm": True, "setup": "x"},
                                                {"llm": False, "setup": None})],
              [[], [], ["llm_relay"], ["setup_phase", "llm_relay", "artifacts", "log_stream"], []])
        check("llm_limits: (allowed models, max_calls, max_concurrency): defaults, hard caps, junk ignored",
              [cpu_lane.llm_limits(x) for x in ({"llm": True},
                                                {"llm": {"models": [MODEL, ""], "max_calls": 5, "max_concurrency": 1}},
                                                {"llm": {"max_calls": 99999, "max_concurrency": 99}},
                                                {"llm": {"models": "x", "max_calls": True, "max_concurrency": "3"}},
                                                None)],
              [(None, 400, 2), ([MODEL], 5, 1), (None, 2000, 4), (None, 400, 2), (None, 400, 2)])
        check("_lease_s: clamped to 10..3600, junk and <= 0 take the default",
              [cpu_lane._lease_s(v) for v in (None, "x", 0, -5, 1, 30, "45", 99999, 3600)],
              [60, 60, 60, 60, 10, 30, 45, 3600, 3600])
        check("_progress_text: printable characters only, stripped, at most 200",
              [cpu_lane._progress_text(v) for v in ("  npm ci 3/10\n", "a\x1b[31mred\x07", 5, None, "x" * 300)],
              ["npm ci 3/10", "a[31mred", "", "", "x" * 200])
        lb = {"http://127.0.0.1:8000/v1": "http://127.0.0.1:8000", "http://127.0.0.1:8000/v1/": "http://127.0.0.1:8000",
              "http://localhost:9/": "http://localhost:9", "https://[::1]:8443/v1": "https://[::1]:8443",
              "http://127.0.0.2:8000": "http://127.0.0.2:8000",
              "http://192.0.2.15:8000/v1": None, "http://example.com/v1": None, "ftp://127.0.0.1/v1": None,
              "http://user:pw@127.0.0.1:8000": None, "http://127.0.0.1:8000/v1?x=1": None,
              "http://127.0.0.1:8000/v1#f": None, "http://127.0.0.1:99999": None, "": None, None: None,
              "http://127.0.0.1.nip.io:8000": None, "http://0x7f000001:8000": None}
        check("_loopback_base: the key only ever goes to a plain loopback http(s) URL (mismatches)",
              {k: cpu_lane._loopback_base(k) for k, v in lb.items() if cpu_lane._loopback_base(k) != v}, {})

    # ---- 3. caps-gated claims, why a pending job waits, runner slots -------------------------------------------------
    def s_caps():
        fresh("caps")
        jl = mk({"llm": True}, "caps-llm")
        check("caps: a pending job with no runner seen says so", view(jl)["waiting"], "no runner online")
        check("caps: a runner that reports no caps is not handed a spec.llm job", claim("r-old"), None)
        check("caps: ...and the job says which cap it waits for", view(jl)["waiting"], "no runner reports llm_relay")
        ja = mk({"setup": "npm ci", "llm": True, "artifacts": True, "log_stream": True}, "caps-all")
        every = "no runner reports setup_phase, llm_relay, artifacts, log_stream"
        check("caps: a job needing every feature names them all", view(ja)["waiting"], every)
        check("caps: a runner reporting llm_relay gets the llm job",
              (claim("r-new", caps={"llm_relay": True}) or {}).get("id"), jl)
        check("caps: ...but not the one that also needs setup/artifacts/log_stream", claim("r-new"), None)
        check("caps: ...which still names every need (no one runner has them all)", view(ja)["waiting"], every)
        check("caps: a runner with every feature gets it", (claim("r-full", caps=ALL) or {}).get("id"), ja)
        jl2 = mk({"llm": True}, "caps-llm2")
        with cpu_lane.get_store()._conn() as c:
            c.execute("UPDATE runners SET caps_t=caps_t-200 WHERE runner_id='r-new'")
        check("caps: a caps report older than CAPS_FRESH_S no longer counts", claim("r-new"), None)
        rs = {x["runner"]: x for x in call(p, "GET", "/api/cpu/runners")[1]["runners"]}
        check("caps: ...and /api/cpu/runners stops serving it", "caps" in rs["r-new"], False)
        check("caps: a fresh report counts again", (claim("r-new", caps={"llm_relay": True}) or {}).get("id"), jl2)
        claim("r-full", slots=1)
        rr = {x["runner"]: x for x in call(p, "GET", "/api/cpu-lane", token=None)[1]["runners"]}
        check("slots: a runner's slots, busy count and saturation are in the feed",
              tuple(rr["r-full"].get(k) for k in ("slots", "busy", "saturated")), (1, 1, True))
        claim("r-new", slots=0)
        claim("r-old", slots=True)
        rr = {x["runner"]: x for x in call(p, "GET", "/api/cpu-lane", token=None)[1]["runners"]}
        check("slots: 0 and true are not slot counts", ("slots" in rr["r-new"], "slots" in rr["r-old"]), (False, False))

    # ---- 4. the Darkbloom Sandbox hold -------------------------------------------------------------------------------
    def s_hold():
        fresh("hold")
        HOLD.update(on=True, cause="ci-job-77")
        held_t = "darkbloom sandbox hold: model jobs wait until it is released"
        jh = mk({"llm": True}, "hold-llm")          # first: a hold must not block the plain job queued behind it
        jp = mk({}, "hold-plain")
        check("hold: a spec.llm job is not handed out during a hold; a plain job behind it is",
              (claim("r-h", caps=ALL) or {}).get("id"), jp)
        check("hold: ...and nothing else is claimable", claim("r-h"), None)
        v = view(jh)
        check("hold: the waiting model job says why", (v["status"], v["waiting"], v["model_hold"]),
              ("pending", held_t, True))
        check("hold: a job without spec.llm is not marked held", view(jp)["model_hold"], False)
        d = call(p, "GET", "/api/cpu-lane", token=None)[1]
        check("hold: the dashboard feed carries the hold and the pending job's reason",
              (d.get("model_hold"), [x.get("waiting") for x in d.get("pending", []) if x["id"] == jh]),
              ({"held": True, "cause": "ci-job-77"}, [held_t]))
        api.q.SANDBOX_HOLD_WAIT = False
        try:
            r = (cpu_lane.model_hold(fresh=True), view(jh)["model_hold"])
        finally:
            api.q.SANDBOX_HOLD_WAIT = True
        check("hold: the queue's kill switch (SANDBOX_HOLD_WAIT=0) turns the reading off", r, ((False, ""), False))
        HOLD["raise"] = True
        try:
            r = cpu_lane.model_hold(fresh=True)
        finally:
            HOLD["raise"] = False
        check("hold: a view that raises reads as not held", r, (False, ""))
        HOLD["cause"] = "c" * 300
        check("hold: the cause is cut to 120 characters", cpu_lane.model_hold(fresh=True), (True, "c" * 120))
        HOLD.update(on=False, cause="")
        check("hold: once released the model job is claimable", (claim("r-h") or {}).get("id"), jh)
        check("hold: the API pointed cpu_lane at the queue's own reading (and it was read)",
              (cpu_lane.HOLD_VIEW is api._cpu_hold_view, HOLD["calls"] > 0), (True, True))

    # ---- 4b. a runner's limits: accept / hold / lane_max / disk (cpu_lane module doc "Runner limits") ---------------
    def s_limits():
        why_l = "low disk: 3.1 GB free on /work, lane jobs need 6.0 GB"
        why_a = "low disk: 1.2 GB free on /cache, every job needs 2.0 GB"
        why_c = "clearing the npm cache (over its cap)"
        why_p = "lane-only runner (CPU_RUNNER_ACCEPT=lane)"      # what a lane-only runner always holds (plain jobs)
        why_n = "lane-job limit: 1 of 1 running"
        held_t = "darkbloom sandbox hold: model jobs wait until it is released"
        disk = {"work_gb": 1.23456, "work_free_gb": 31.5, "cache_free_gb": 410.25, "min_free_gb": 6, "uv_gb": 3.1,
                "uv_max_gb": 10, "npm_gb": 0.4, "npm_max_gb": 4, "nm_gb": 1.2, "nm_max_gb": 6}
        disk_out = {k: round(float(v), 3) for k, v in disk.items()}

        def feed():
            return call(p, "GET", "/api/cpu-lane", token=None)[1]

        def runner(rid):
            """The runner's entry in the public dashboard feed ({} when it is not there)."""
            return {x["runner"]: x for x in feed()["runners"]}.get(rid, {})

        def waits(jid):
            """(the job view's waiting, the dashboard feed's waiting) of a pending job."""
            return view(jid)["waiting"], [x["waiting"] for x in feed()["pending"] if x["id"] == jid][0]

        def age_disk(rid, secs):
            with cpu_lane.get_store()._conn() as c:
                c.execute("UPDATE runners SET disk_t=disk_t-? WHERE runner_id=?", (secs, rid))

        # accept: "plain" leaves the lane jobs (spec.setup / spec.llm) to another runner, "none" claims nothing,
        # anything else (or nothing) is "all"
        fresh("limits-accept")
        j_setup, j_llm = mk({"setup": "npm ci"}, "lim-setup"), mk({"llm": True}, "lim-llm")
        j_plain, j_off = mk({}, "lim-plain"), mk({"setup": False, "llm": None}, "lim-off")
        jobs4 = (j_setup, j_llm, j_plain, j_off)
        check("limits: accept none -> 204 although four jobs are pending, and none is taken",
              (claim_c("r-p", caps=ALL, accept="none")[0], [view(j)["status"] for j in jobs4]), (204, ["pending"] * 4))
        check("limits: accept plain hands out the plain job, skipping the older lane jobs (setup, llm)",
              (claim("r-p", caps=ALL, accept="plain") or {}).get("id"), j_plain)
        check("limits: ...then the job whose lane fields are switched off (a plain job)",
              (claim("r-p", caps=ALL, accept="plain") or {}).get("id"), j_off)
        check("limits: ...and with only lane jobs left, accept plain gets 204 and leaves them pending",
              (claim_c("r-p", caps=ALL, accept="plain")[0], [view(j)["status"] for j in (j_setup, j_llm)]),
              (204, ["pending", "pending"]))
        check("limits: no accept, and accept all, are the same: lane jobs go out, oldest first",
              [(claim("r-p", caps=ALL) or {}).get("id"), (claim("r-p", caps=ALL, accept="all") or {}).get("id")],
              [j_setup, j_llm])
        miss = []
        for v in ("bogus", "PLAIN", "", 5, True, ["plain"], {"accept": "none"}, None):
            jn = mk({"llm": True}, "lim-unknown")
            got = (claim("r-p", caps=ALL, accept=v) or {}).get("id")
            if got != jn:
                miss.append((v, got))
        check("limits: an unknown accept (or null) means all: a lane job is handed out", miss, [])

        # accept "lane" (CPU_RUNNER_ACCEPT=lane): a second runner that takes lane jobs only and leaves the gate jobs
        # (plain ones) to the main runner. A job it passes over stays queued as it was.
        fresh("limits-laneonly")
        p1, p_off = mk({}, "lo-plain1"), mk({"setup": False, "llm": None}, "lo-off")
        l_setup, p2, l_llm = mk({"setup": "npm ci"}, "lo-setup"), mk({}, "lo-plain2"), mk({"llm": True}, "lo-llm")
        plains = (p1, p_off, p2)

        def states(jids):
            return [(view(j)["status"], view(j)["attempt"]) for j in jids]
        check("limits lane-only: accept lane skips the older plain jobs and hands out the oldest lane job (spec.setup)",
              (claim("r-lo", caps=ALL, accept="lane", lane_max=2) or {}).get("id"), l_setup)
        check("limits lane-only: ...then the next one (spec.llm), again behind a plain job",
              (claim("r-lo", caps=ALL, accept="lane") or {}).get("id"), l_llm)
        check("limits lane-only: ...and the plain jobs it passed over are untouched: pending, attempt 0",
              states(plains), [("pending", 0)] * 3)
        check("limits lane-only: the lane jobs it took are running on its first attempt",
              [(view(j)["status"], view(j)["attempt"], view(j)["runner_id"]) for j in (l_setup, l_llm)],
              [("running", 1, "r-lo")] * 2)
        check("limits lane-only: with no lane job left accept lane gets 204 and the plain jobs stay pending, attempt 0",
              (claim_c("r-lo", caps=ALL, accept="lane")[0], states(plains)), (204, [("pending", 0)] * 3))
        check("limits lane-only: the dashboard counts its running lane jobs (lane_busy next to lane_max)",
              (runner("r-lo").get("lane_max"), runner("r-lo").get("lane_busy")), (2, 2))
        check("limits lane-only: the plain jobs are still there for a runner that takes them, oldest first",
              [(claim("r-pl", caps=ALL, accept="plain") or {}).get("id") for _ in range(3)], [p1, p_off, p2])
        check("limits lane-only: ...on their first attempt (the lane-only runner's passes did not count)",
              [(view(j)["runner_id"], view(j)["attempt"]) for j in plains], [("r-pl", 1)] * 3)

        # only the exact word is lane-only; anything else is an unknown accept, which means all
        fresh("limits-laneonly-spelling")
        miss = []
        for v in ("LANE", "Lane", " lane", "lane ", "lane\n", "lanes", "lane-only", "lane_only"):
            jn = mk({}, "lo-spelling")                    # a plain job: only accept "all" hands it out
            got = (claim("r-sp", caps=ALL, accept=v) or {}).get("id")
            if got != jn:
                miss.append((v, got))
        check("limits lane-only: only the exact word lane is lane-only, any other spelling means all (takes a plain job)",
              miss, [])

        # accept none still records what the runner reports
        fresh("limits-none")
        jn = mk({}, "lim-none")
        check("limits: accept none -> 204 with a plain job pending ...",
              claim_c("r-n", caps=ALL, accept="none", hold={"all": why_a}, lane_max=3, disk=disk)[0], 204)
        e = runner("r-n")
        check("limits: ... yet its hold, lane_max and disk report are stored (and the job is untouched)",
              ((e.get("hold"), e.get("lane_max"), e.get("lane_busy"), e.get("disk")), view(jn)["status"]),
              (({"all": why_a}, 3, 0, disk_out), "pending"))

        # hold: stored, replaced by every claim, shown in the feed; bad shapes dropped; text cut at 300
        fresh("limits-hold")
        claim("r-h", hold={"lane": why_l})
        check("limits hold: stored and shown in the feed (runners[].hold)", runner("r-h").get("hold"), {"lane": why_l})
        claim("r-h", hold={"all": why_a})
        check("limits hold: each claim replaces it (the lane text of the claim before is gone)",
              runner("r-h").get("hold"), {"all": why_a})
        claim("r-h", hold={"all": why_a, "lane": why_l})
        check("limits hold: both reasons at once", runner("r-h").get("hold"), {"all": why_a, "lane": why_l})
        claim("r-h")
        check("limits hold: a claim with no hold clears it", "hold" in runner("r-h"), False)
        for how, h in (("an empty hold", {}), ("a null hold", None)):
            claim("r-h", hold={"lane": why_l})
            claim("r-h", hold=h)
            check("limits hold: %s clears it too" % how, "hold" in runner("r-h"), False)
        kept = []
        for bad in ("lane", ["lane"], 5, True, {"lane": 5}, {"lane": ""}, {"lane": "   "}, {"lane": "\n\t"},
                    {"lane": ["x"]}, {"all": None}, {"other": why_l}):
            claim("r-h", hold={"lane": why_l})
            claim("r-h", hold=bad)
            if "hold" in runner("r-h"):
                kept.append(bad)
        check("limits hold: shapes that are no hold (not an object, no text, other keys) are dropped", kept, [])
        claim("r-h", hold={"lane": why_l, "all": 5, "extra": "x"})
        check("limits hold: a valid reason is kept, the rest of a bad object dropped", runner("r-h").get("hold"),
              {"lane": why_l})
        claim("r-h", hold={"all": "x" * 400, "lane": "y" * 300, "junk": "z"})
        check("limits hold: each text is cut at 300 characters", runner("r-h").get("hold"),
              {"all": "x" * 300, "lane": "y" * 300})
        claim("r-h", hold={"lane": "  a\x1b[31mb\nc\t  "})
        check("limits hold: printable characters only, stripped", runner("r-h").get("hold"), {"lane": "a[31mbc"})
        # a lane-only runner always holds "plain" (the plain jobs it leaves to the main runner)
        claim("r-h", hold={"plain": why_p})
        check("limits hold plain: stored and shown in the feed (a lane-only runner's reason)",
              runner("r-h").get("hold"), {"plain": why_p})
        check("limits hold plain: the token route /api/cpu/runners shows it too",
              {x["runner"]: x for x in call(p, "GET", "/api/cpu/runners")[1]["runners"]}["r-h"].get("hold"), {"plain": why_p})
        claim("r-h", hold={"lane": why_l})
        check("limits hold plain: each claim replaces it (the plain text of the claim before is gone)",
              runner("r-h").get("hold"), {"lane": why_l})
        claim("r-h", hold={"all": why_a, "lane": why_l, "plain": why_p})
        check("limits hold plain: all three reasons at once", runner("r-h").get("hold"),
              {"all": why_a, "lane": why_l, "plain": why_p})
        claim("r-h", hold={"plain": "p" * 400, "lane": "y" * 300})
        check("limits hold plain: cut at 300 characters like the others", runner("r-h").get("hold"),
              {"plain": "p" * 300, "lane": "y" * 300})
        claim("r-h", hold={"plain": "  a\x1b[31mb\nc\t  "})
        check("limits hold plain: printable characters only, stripped", runner("r-h").get("hold"), {"plain": "a[31mbc"})
        kept = []
        for bad in ({"plain": ""}, {"plain": "   "}, {"plain": "\n\t"}, {"plain": 5}, {"plain": True}, {"plain": None},
                    {"plain": ["x"]}, {"plain": {"a": "b"}}, {"Plain": why_p}, {"PLAIN": why_p}, {"plain ": why_p},
                    {"lane-only": why_p}, {"lane_only": why_p}):
            claim("r-h", hold={"plain": why_p})
            claim("r-h", hold=bad)
            if "hold" in runner("r-h"):
                kept.append(bad)
        check("limits hold plain: no text, not a string, or another spelling of the key: no hold is stored", kept, [])
        claim("r-h", hold={"plain": why_p, "lane": 5, "all": None, "extra": "x"})
        check("limits hold plain: a valid plain reason is kept, the rest of a bad object dropped",
              runner("r-h").get("hold"), {"plain": why_p})

        # why a pending job waits, once the runners' holds are what keeps it (job view and feed)
        fresh("limits-wait")
        jl, jm, jp = mk({"setup": "npm ci"}, "wait-setup"), mk({"llm": True}, "wait-llm"), mk({}, "wait-plain")
        check("limits waiting: no runner online", [waits(j) for j in (jl, jm, jp)], [("no runner online",) * 2] * 3)
        claim_c("r-b", caps=ALL, accept="none", hold={"lane": why_l})
        check("limits waiting: one online runner holding lane jobs: a lane job (setup / llm) shows exactly its text",
              (waits(jl), waits(jm)), ((why_l, why_l), (why_l, why_l)))
        check("limits waiting: ...and the plain job shows none", waits(jp), (None, None))
        claim_c("r-b", caps=ALL, accept="none", hold={"all": why_a})
        check("limits waiting: hold all: every job shows it", [waits(j) for j in (jl, jm, jp)], [(why_a, why_a)] * 3)
        claim_c("r-b", caps=ALL, accept="none", hold={"all": why_a, "lane": why_l})
        check("limits waiting: hold all comes before hold lane, for a lane job too",
              [waits(j) for j in (jl, jp)], [(why_a, why_a)] * 2)
        claim_c("r-b", caps=ALL, accept="none", hold={"lane": why_l})
        claim_c("r-a", caps=ALL, accept="none")
        check("limits waiting: two online runners, one of them free: nothing to explain",
              [waits(j) for j in (jl, jm, jp)], [(None, None)] * 3)
        claim_c("r-a", caps=ALL, accept="none", hold={"all": why_a})
        claim_c("r-b", caps=ALL, accept="none", hold={"lane": why_l})   # r-b: first seen, polled last: not runner_id order
        both = "r-a: %s; r-b: %s" % (why_a, why_l)
        check("limits waiting: every runner held for a lane job: all reasons, 'runner: why', in runner_id order",
              (waits(jl), waits(jm)), ((both, both), (both, both)))
        check("limits waiting: ...a plain job is held only by r-a: r-b takes plain jobs", waits(jp), (None, None))
        claim_c("r-b", caps=ALL, accept="none", hold={"all": why_c})
        both = "r-a: %s; r-b: %s" % (why_a, why_c)
        check("limits waiting: both runners hold all: every job lists both", [waits(j) for j in (jl, jm, jp)],
              [(both, both)] * 3)
        with cpu_lane.get_store()._conn() as c:
            c.execute("UPDATE runners SET last_seen=last_seen-1000 WHERE runner_id='r-a'")
        check("limits waiting: a runner that stopped polling is not online: its hold is not listed",
              [waits(j) for j in (jl, jm, jp)], [(why_c, why_c)] * 3)

        fresh("limits-caps")
        jm, jp = mk({"llm": True}, "caps-llm"), mk({}, "caps-plain")
        claim_c("r-full", caps=ALL, accept="none", hold={"lane": why_l})
        claim_c("r-bare", accept="none")
        check("limits waiting: a runner without the job's caps is left out: the model job shows r-full's text alone",
              waits(jm), (why_l, why_l))
        check("limits waiting: ...and r-bare is free for the plain job", waits(jp), (None, None))
        claim_c("r-bare", accept="none", hold={"all": why_c})
        check("limits waiting: ...its own hold stays out of the model job's reason", waits(jm), (why_l, why_l))
        claim_c("r-full", caps=ALL, accept="none", hold={"all": why_a})
        both = "r-bare: %s; r-full: %s" % (why_c, why_a)
        check("limits waiting: ...but a plain job needs no caps, so both count for it", (waits(jp), waits(jm)),
              ((both, both), (why_a, why_a)))
        HOLD.update(on=True, cause="ci-job-77")
        check("limits waiting: a Darkbloom Sandbox hold still comes first for a model job",
              (waits(jm)[0], waits(jp)[0]), (held_t, both))
        HOLD.update(on=False, cause="")

        # a lane-only runner always holds "plain": a plain job shows that reason, a lane job the runner's hold lane, and
        # hold all still covers both
        fresh("limits-laneonly-wait")
        jl, jm, jp = mk({"setup": "npm ci"}, "low-setup"), mk({"llm": True}, "low-llm"), mk({}, "low-plain")
        claim_c("r-b", caps=ALL, accept="none", hold={"plain": why_p})
        check("limits lane-only waiting: one online lane-only runner: the plain job shows its reason, lane jobs none",
              (waits(jp), waits(jl), waits(jm)), ((why_p, why_p), (None, None), (None, None)))
        claim_c("r-b", caps=ALL, accept="none", hold={"lane": why_n, "plain": why_p})
        check("limits lane-only waiting: ...at its lane limit it holds both: lane jobs show hold lane, plain jobs hold plain",
              (waits(jp), waits(jl), waits(jm)), ((why_p, why_p), (why_n, why_n), (why_n, why_n)))
        claim_c("r-b", caps=ALL, accept="none", hold={"all": why_c, "plain": why_p})
        check("limits lane-only waiting: hold all covers both kinds and comes first",
              [waits(j) for j in (jp, jl, jm)], [(why_c, why_c)] * 3)
        claim_c("r-b", caps=ALL, accept="none", hold={"all": why_c, "lane": why_n, "plain": why_p})
        check("limits lane-only waiting: ...also with all three reasons", [waits(j) for j in (jp, jl, jm)],
              [(why_c, why_c)] * 3)
        claim_c("r-b", caps=ALL, accept="none", hold={"plain": why_p})
        claim_c("r-a", caps=ALL, accept="none")
        check("limits lane-only waiting: next to a free main runner nothing waits (main takes plain, lane-only takes lane)",
              [waits(j) for j in (jp, jl, jm)], [(None, None)] * 3)
        claim_c("r-a", caps=ALL, accept="none", hold={"lane": why_l})
        check("limits lane-only waiting: main runner at its lane limit still takes plain jobs: still nothing waits",
              [waits(j) for j in (jp, jl, jm)], [(None, None)] * 3)
        claim_c("r-a", caps=ALL, accept="none", hold={"all": why_a})
        both = "r-a: %s; r-b: %s" % (why_a, why_p)
        check("limits lane-only waiting: main held for all: the plain job lists both reasons in runner_id order "
              "(r-b was seen first), lane jobs go to the lane-only runner",
              (waits(jp), waits(jl), waits(jm)), ((both, both), (None, None), (None, None)))
        claim_c("r-b", caps=ALL, accept="none", hold={"lane": why_n, "plain": why_p})
        both_p = "r-a: %s; r-b: %s" % (why_a, why_p)
        both_l = "r-a: %s; r-b: %s" % (why_a, why_n)
        check("limits lane-only waiting: both held (main: all, lane-only: its lane limit): each kind lists its own reasons",
              (waits(jp), waits(jl), waits(jm)), ((both_p, both_p), (both_l, both_l), (both_l, both_l)))
        claim_c("r-a", caps=ALL, accept="none", hold={"lane": why_l})
        both_l = "r-a: %s; r-b: %s" % (why_l, why_n)
        check("limits lane-only waiting: main only at its lane limit: it takes the plain job, nothing waits for it; the "
              "lane jobs list both limits",
              (waits(jp), waits(jl), waits(jm)), ((None, None), (both_l, both_l), (both_l, both_l)))
        claim_c("r-b", caps=ALL, accept="none", hold={"plain": why_p})
        check("limits lane-only waiting: ...and with the lane-only runner free again the lane jobs go to it",
              [waits(j) for j in (jp, jl, jm)], [(None, None)] * 3)
        HOLD.update(on=True, cause="ci-job-88")
        check("limits lane-only waiting: a Darkbloom Sandbox hold still comes first for a model job",
              (waits(jm)[0], waits(jl)[0]), (held_t, None))
        HOLD.update(on=False, cause="")

        # lane_max: kept only for integers 0..256 (not bools); lane_busy: that runner's running lane jobs
        fresh("limits-lane")
        claim("r-l", caps=ALL)
        e = runner("r-l")
        check("limits lane_max: a runner that never sent one has neither lane_max nor lane_busy",
              ("lane_max" in e, "lane_busy" in e), (False, False))
        got = []
        for v in (0, 1, 2, 32, 256):
            claim("r-l", lane_max=v)
            got.append(runner("r-l").get("lane_max"))
        check("limits lane_max: integers 0..256 are kept (0 = lane jobs off)", got, [0, 1, 2, 32, 256])
        claim("r-l", lane_max=3)
        moved = []
        for v in (257, -1, 1000, True, False, "4", 2.5, [2], {"a": 2}, None):
            claim("r-l", lane_max=v)
            if runner("r-l").get("lane_max") != 3:
                moved.append((v, runner("r-l").get("lane_max")))
        claim("r-l")
        check("limits lane_max: anything else (out of range, bool, float, string, null, absent) keeps the last good one",
              (moved, runner("r-l").get("lane_max")), ([], 3))

        fresh("limits-busy")
        jb = [mk({"setup": "npm ci"}, "busy-setup"), mk({"llm": True}, "busy-llm"), mk({}, "busy-plain"),
              mk({"setup": "npm ci"}, "busy-setup2"), mk({"llm": True}, "busy-llm2")]
        cs = [claim("r-x", caps=ALL, lane_max=2) for _ in range(3)] + [claim("r-y", caps=ALL, lane_max=1)]
        check("limits lane_busy: the runners took the jobs in order", [(c or {}).get("id") for c in cs], jb[:4])
        d = feed()
        rx, ry = {x["runner"]: x for x in d["runners"]}["r-x"], {x["runner"]: x for x in d["runners"]}["r-y"]
        check("limits lane_busy: counts the runner's own running lane jobs (setup and llm, not the plain one)",
              ((rx["lane_max"], rx["lane_busy"]), (ry["lane_max"], ry["lane_busy"])), ((2, 2), (1, 1)))
        check("limits running[].lane: true for a spec.setup / spec.llm job, false for a plain one",
              {x["id"]: x["lane"] for x in d["running"]}, dict(zip(jb[:4], (True, True, False, True))))
        check("limits lane_busy: a finished job no longer counts",
              (call(p, "POST", J + jb[0] + "/result", {"lease_token": cs[0]["lease_token"], "exit_code": 0})[0],
               runner("r-x")["lane_busy"], runner("r-y")["lane_busy"]), (200, 1, 1))
        old = claim("r-z", caps=ALL)
        e = runner("r-z")
        check("limits lane_busy: a runner that never sent lane_max shows no lane_busy even with a lane job running",
              (old["id"], [x["lane"] for x in feed()["running"] if x["id"] == old["id"]], "lane_max" in e, "lane_busy" in e),
              (jb[4], [True], False, False))

        # disk: numeric fields named [a-z_] (<= 32), shown with the report's age while it is under 900 s old
        fresh("limits-disk")
        claim("r-d", disk=disk)
        e = runner("r-d")
        check("limits disk: a valid report is shown (numbers to 3 decimals) with its age",
              (e.get("disk"), isinstance(e.get("disk_s_ago"), (int, float)) and 0 <= e["disk_s_ago"] < 5), (disk_out, True))
        junk = {"Work_GB": 1, "work-gb": 2, "work gb": 3, "": 4, "k" * 33: 5, "work1": 6, "b_true": True, "b_false": False,
                "nan": float("nan"), "inf": float("inf"), "ninf": float("-inf"), "s": "7", "n": None, "l": [1], "d": {"a": 1}}
        claim("r-j", disk=junk)
        e = runner("r-j")
        check("limits disk: bad keys, bools, NaN, infinity, strings, null, lists and objects are all dropped",
              (e.get("disk"), "disk_s_ago" in e), (None, False))
        shapes = []
        for bad in ("work_gb", [1, 2], 5, True, {}, None):
            claim("r-j", disk=bad)
            if "disk" in runner("r-j"):
                shapes.append(bad)
        check("limits disk: a report that is not an object with a usable field is not stored", shapes, [])
        claim("r-j", disk=dict(junk, ok_key=6, work_gb=2))
        check("limits disk: the usable fields of a mixed report are kept", runner("r-j").get("disk"),
              {"ok_key": 6.0, "work_gb": 2.0})
        many = {"k_%s%s" % (chr(97 + i // 26), chr(97 + i % 26)): i for i in range(40)}
        claim("r-m", disk=many)
        check("limits disk: at most 32 fields are kept (the first ones)",
              sorted(runner("r-m").get("disk") or {}), sorted(list(many)[:32]))
        age_disk("r-d", 100)
        claim("r-d")
        claim("r-d", disk=junk)
        e = runner("r-d")
        check("limits disk: a claim without a report (or with an unusable one) keeps the last one and its age",
              (e.get("disk"), 99 < e.get("disk_s_ago", 0) < 110), (disk_out, True))
        claim("r-d", disk=dict(disk, work_gb=9))
        e = runner("r-d")
        check("limits disk: a new report replaces the old and resets its age",
              (e["disk"]["work_gb"], 0 <= e["disk_s_ago"] < 5), (9.0, True))
        age_disk("r-d", 890)
        e = runner("r-d")
        check("limits disk: a report 890 s old is still shown", ("disk" in e, 889 <= e.get("disk_s_ago", 0) < 900),
              (True, True))
        age_disk("r-d", 20)
        e = runner("r-d")
        check("limits disk: a report older than 900 s is not shown (no disk, no disk_s_ago), the runner still is",
              ("r-d" in {x["runner"] for x in feed()["runners"]}, "disk" in e, "disk_s_ago" in e), (True, False, False))
        claim("r-d", disk=disk)
        check("limits disk: a fresh report shows again", runner("r-d").get("disk"), disk_out)
        tok = {x["runner"]: x for x in call(p, "GET", "/api/cpu/runners")[1]["runners"]}
        pub = {x["runner"]: x for x in feed()["runners"]}
        check("limits: the token route /api/cpu/runners serves the same limits as the dashboard feed",
              [(tok[r].get("disk"), tok[r].get("hold"), tok[r].get("lane_max")) for r in ("r-d", "r-j")],
              [(pub[r].get("disk"), pub[r].get("hold"), pub[r].get("lane_max")) for r in ("r-d", "r-j")])

    # ---- 5. heartbeat phase/progress, a cancelled job's cut lease ----------------------------------------------------
    def s_heartbeat():
        fresh("hb")
        jid = mk({}, "hb")
        lt = claim("r-hb", caps=ALL)["lease_token"]

        def hb(**kw):
            return call(p, "POST", J + jid + "/heartbeat",
                        dict({"lease_token": lt, "runner_id": "r-hb", "lease_s": 60}, **kw))
        hb(phase="deps", progress="npm ci 3/10")
        v = view(jid)
        check("heartbeat: the phase and progress line are recorded",
              (v["phase"], v["progress"], isinstance(v["progress_t"], float)), ("deps", "npm ci 3/10", True))
        hb(phase="bogus", progress="")
        v = view(jid)
        check("heartbeat: an unknown phase or an empty progress line keeps what is stored",
              (v["phase"], v["progress"]), ("deps", "npm ci 3/10"))
        hb(progress="\x1b[2Kbuild " + "y" * 300)
        pr = view(jid)["progress"]
        check("heartbeat: progress is cut to printable text, 200 characters",
              (pr[:9], len(pr), "\x1b" in pr), ("[2Kbuild ", 200, False))
        call(p, "DELETE", J + jid)
        r = hb(lease_s=3600)
        left = cpu_lane.get_store().get(jid)["lease_expires"] - time.time()
        check("heartbeat: a cancelled job hears cancel and its lease is cut to CANCEL_LEASE_S",
              (r, 0 < left <= cpu_lane.CANCEL_LEASE_S + 1), ((200, {"ok": True, "cancel": True}), True))
        s = cpu_lane.get_store()
        real = s.clock
        s.clock = lambda: time.time() + cpu_lane.CANCEL_LEASE_S + 5
        try:
            s.reap()
        finally:
            s.clock = real
        check("heartbeat: ...so the reaper settles it as cancelled soon after", view(jid)["status"], "cancelled")

    # ---- 6. the streamed log -------------------------------------------------------------------------------------------
    def s_log():
        fresh("log")
        jid = mk({"log_stream": True}, "log")
        lt = claim("r-log", caps=ALL)["lease_token"]
        lp = J + jid + "/log"

        def plog(off, data="", lease=None, raw_b64=None, **kw):
            return call(p, "POST", lp, dict({"lease_token": lt if lease is None else lease, "off": off,
                                             "data_b64": b64(data) if raw_b64 is None else raw_b64}, **kw))
        check("log: the first chunk", plog(0, "hello "), (200, {"ok": True, "size": 6}))
        check("log: the next chunk (carrying phase/progress)", plog(6, "world\n", phase="setup", progress="pip install"),
              (200, {"ok": True, "size": 12}))
        check("log: a resent overlap adds only what is new", plog(6, "world\nmore\n"), (200, {"ok": True, "size": 17}))
        check("log: a full resend adds nothing", plog(0, "hello world\nmore\n"), (200, {"ok": True, "size": 17}))
        check("log: a chunk past the end is a gap (416 with the size held)", plog(40, "x"),
              (416, {"error": "gap", "size": 17}))
        check("log: a wrong lease -> 409", plog(17, "x", lease="bad")[0], 409)
        check("log: a bad offset or data is a 400",
              [plog(-1, "x")[0], plog("3", "x")[0], plog(17, raw_b64="not base64!!")[0], plog(True, "x")[0],
               plog(17, raw_b64=5)[0]], [400] * 5)
        v = view(jid)
        check("log: the view has the posted phase/progress and the log size",
              (v["phase"], v["progress"], v["log_size"]), ("setup", "pip install", 17))
        c, b, hd = call_h(p, "GET", lp, as_bytes=True)
        check("log: GET returns the raw log with X-Log-Size",
              (c, b, hd.get("x-log-size"), hd.get("content-type")),
              (200, b"hello world\nmore\n", "17", "text/plain; charset=utf-8"))
        check("log: ?off= reads from an offset (past the end = empty)",
              [call_h(p, "GET", lp + "?off=" + o, as_bytes=True)[:2] for o in ("12", "99")],
              [(200, b"more\n"), (200, b"")])
        check("log: a bad ?off= is a 400",
              [call_h(p, "GET", lp + "?off=" + o, as_bytes=True)[0] for o in ("-1", "x", "1e3", "9" * 16)], [400] * 4)
        cpu_lane.LOG_GET_MAX = 5
        try:
            c, b, hd = call_h(p, "GET", lp, as_bytes=True)
        finally:
            cpu_lane.LOG_GET_MAX = saved["LOG_GET_MAX"]
        check("log: one GET returns at most LOG_GET_MAX bytes (X-Log-Size says how much is held)",
              (b, hd.get("x-log-size")), (b"hello", "17"))
        cpu_lane.LOG_MAX = 20
        try:
            r = plog(17, "0123456789")
        finally:
            cpu_lane.LOG_MAX = saved["LOG_MAX"]
        check("log: a log over LOG_MAX is refused (413 with the size held)", r,
              (413, {"error": "log over 20 bytes", "size": 17}))
        check("log: an unknown job's log is a 404", call_h(p, "GET", J + "c" * 32 + "/log", as_bytes=True)[0], 404)

    # ---- 7. the artifacts tarball ---------------------------------------------------------------------------------------
    def s_artifacts():
        fresh("art")
        jid = mk({"artifacts": True}, "art")
        lt = claim("r-art", caps=ALL)["lease_token"]
        ap = J + jid + "/artifacts"
        check("artifacts: none before an upload -> 404", call_h(p, "GET", ap, as_bytes=True)[0], 404)
        c = call(p, "PUT", ap, raw=tgz, headers={"X-Lease-Token": "bad"})[0]
        check("artifacts: a wrong lease -> 409 and nothing is kept",
              (c, call_h(p, "GET", ap, as_bytes=True)[0], view(jid)["has_artifacts"]), (409, 404, False))
        check("artifacts: an upload under the lease", call(p, "PUT", ap, raw=tgz, headers={"X-Lease-Token": lt}),
              (200, {"ok": True, "bytes": len(tgz)}))
        c, b, hd = call_h(p, "GET", ap, as_bytes=True)
        check("artifacts: the producer gets the same bytes back (application/gzip)",
              (c, b == tgz, hd.get("content-type"), view(jid)["has_artifacts"]), (200, True, "application/gzip", True))

        def raw_put(lines, body=b"", shut=False):
            s = socket.create_connection(("127.0.0.1", p), timeout=10)
            try:
                head = ("PUT %s HTTP/1.1\r\nHost: 127.0.0.1\r\nAuthorization: Bearer TOKEN\r\nX-Lease-Token: %s\r\n"
                        % (ap, lt)) + "".join(x + "\r\n" for x in lines) + "\r\n"
                s.sendall(head.encode() + body)
                if shut:
                    s.shutdown(socket.SHUT_WR)
                out = b""
                while True:
                    try:
                        chunk = s.recv(65536)
                    except (ConnectionResetError, socket.timeout):
                        break
                    if not chunk:
                        break
                    out += chunk
                return out
            finally:
                s.close()

        def code_of(raw):
            try:
                return int(raw.split(b" ", 2)[1])
            except (IndexError, ValueError):
                return None
        check("artifacts: no Content-Length -> 411", code_of(raw_put([])), 411)
        check("artifacts: over ARTIFACTS_MAX -> 413 (the body is never read)",
              code_of(raw_put(["Content-Length: %d" % (cpu_lane.ARTIFACTS_MAX + 1)])), 413)
        check("artifacts: a junk Content-Length -> 400", code_of(raw_put(["Content-Length: abc"])), 400)
        out = raw_put(["Content-Length: 1000"], b"0123456789", shut=True)
        parts = sorted(x.name for x in (cpu_lane.get_store().blobs / jid).iterdir() if x.name.endswith(".part"))
        check("artifacts: a short body gets no answer, leaves no temp file and keeps the last good tarball",
              (out, parts, call_h(p, "GET", ap, as_bytes=True)[1] == tgz), (b"", [], True))

    # ---- 8. the model relay -------------------------------------------------------------------------------------------
    def s_relay():
        fresh("llm")
        jl = mk({"llm": {"models": [MODEL]}}, "llm")
        lt = claim("r-llm", caps=ALL, lease_s=900)["lease_token"]
        n0 = len(fake.chat_reqs())
        c, b, hd = chat(jl, lt)
        reqs = fake.chat_reqs()
        last = reqs[-1] if reqs else (None, None, {}, None)
        check("relay: a chat call reaches the endpoint once, with the Darkbloom key added on this side "
              "(no lease token, no runner bearer, the body as sent)",
              (c, len(reqs) - n0, last[2].get("authorization"), "x-lease-token" in last[2],
               "TOKEN" in json.dumps(last[2]), last[1], last[3]),
              (200, 1, "Bearer " + FAKE_KEY, False, False, "/v1/chat/completions", json.dumps(CHAT).encode()))
        check("relay: the key is cut from the answer",
              ((b or {}).get("echo_auth"), FAKE_KEY in json.dumps(b)), ("Bearer [redacted]", False))
        v = view(jl)
        check("relay: the call is counted (an answer that did not wait adds no wait)",
              (v["llm_calls"], v["llm_errors"], v["llm_wait_s"]), (1, 0, 0.0))

        # upstream answers
        fake.mode = "400key"
        r = chat(jl, lt)
        raw = json.dumps(r[1])
        v = view(jl)
        check("relay: an upstream 4xx is relayed with the key cut, counted as a failed call",
              (r[0], FAKE_KEY in raw, "[redacted]" in raw, v["llm_calls"], v["llm_errors"]), (400, False, True, 2, 1))
        fake.mode = "429"
        r = chat(jl, lt)
        check("relay: a numeric Retry-After is relayed", (r[0], r[2].get("retry-after")), (429, "7"))
        fake.mode = "429date"
        r = chat(jl, lt)
        check("relay: a non-numeric Retry-After is dropped", (r[0], "retry-after" in r[2]), (429, False))
        fake.mode = "ok"
        fake.seq[:] = ["503", "503"]
        n0 = len(fake.chat_reqs())
        w0 = cpu_lane.get_store().get(jl)["llm_wait_s"]
        l0 = cpu_lane.wait_live_s(jl)
        r = chat(jl, lt)
        w1 = cpu_lane.get_store().get(jl)["llm_wait_s"]
        check("relay: the live wait count grows by the same wait as llm_wait_s (an answered call: no dip, no double count)",
              (abs((cpu_lane.wait_live_s(jl) - l0) - (w1 - w0)) < 1e-6, cpu_lane._llm_waits.get(jl)), (True, None))
        check("relay: a reloading endpoint (503) is retried until it answers", (r[0], len(fake.chat_reqs()) - n0),
              (200, 3))
        check("relay: the answer adds the retries' wait (two delays, 0.05 + 0.1 s) to llm_wait_s",
              0.14 <= w1 - w0 < 1.0, True)
        fake.mode = "503"
        e0 = view(jl)["llm_errors"]
        w0 = cpu_lane.get_store().get(jl)["llm_wait_s"]
        l0 = cpu_lane.wait_live_s(jl)
        t0 = time.monotonic()
        try:
            r = chat(jl, lt)
        finally:
            fake.mode = "ok"
        el = time.monotonic() - t0
        check("relay: a 503 given up on adds no wait (the client counts that outage itself)",
              cpu_lane.get_store().get(jl)["llm_wait_s"] - w0, 0.0)
        check("relay: ... but its retries' wait stays in the live wait count (a run that credited it as it grew "
              "keeps it, and its outage loop takes off only the rest)", 0.25 <= cpu_lane.wait_live_s(jl) - l0 < el, True)
        check("relay: an endpoint that stays 503 is given up on after LLM_RETRY_S (a failed call)",
              (r[0], view(jl)["llm_errors"] - e0, el < 3), (503, 1, True))

        # local.json, read per call
        lj.unlink()
        rs = [ecode(chat(jl, lt))]
        put_lj()
        rs.append(ecode(chat(jl, lt)))
        put_lj({})
        rs.append(ecode(chat(jl, lt)))
        put_lj()
        rs.append(ecode(chat(jl, lt)))
        put_lj(text='{"api_key": "sk-')
        rs.append(ecode(chat(jl, lt)))
        cpu_lane._llm_good["t"] -= cpu_lane.LLM_STICKY_S + 1
        rs.append(ecode(chat(jl, lt)))
        put_lj()
        off = (503, "provider_off")
        check("relay: local.json is read per call: missing = off, good = on, {} = off (never masked), "
              "torn = the last good record for LLM_STICKY_S, then off",
              rs, [off, (200, None), off, (200, None), (200, None), off])

        # the key only goes to loopback
        n0 = len(fake.chat_reqs())
        try:
            put_lj({"api_key": FAKE_KEY, "base_url": "http://192.0.2.1:%d/v1" % fake.port})
            r1 = ecode(chat(jl, lt))
            n1 = len(fake.chat_reqs()) - n0
            put_lj({"api_key": FAKE_KEY})
            os.environ["DARKBLOOM_BASE_URL"] = "http://127.0.0.1:%d" % fake.port
            r2 = ecode(chat(jl, lt))
            os.environ["DARKBLOOM_BASE_URL"] = "http://192.0.2.1:8000"
            r3 = ecode(chat(jl, lt))
        finally:
            os.environ.pop("DARKBLOOM_BASE_URL", None)
            put_lj()
        check("relay: the key only goes to a loopback endpoint (base_url, or DARKBLOOM_BASE_URL for a key-only record)",
              (r1, n1, r2, r3), (off, 0, (200, None), off))

        # transport failures
        sk = socket.socket()
        sk.bind(("127.0.0.1", 0))
        dead = sk.getsockname()[1]
        sk.close()
        put_lj({"api_key": FAKE_KEY, "base_url": "http://127.0.0.1:%d/v1" % dead})
        try:
            r = chat(jl, lt)
        finally:
            put_lj()
        check("relay: nothing listening -> 502 provider_unreachable naming the error",
              (ecode(r), "ConnectionRefusedError" in str(emsg(r))), ((502, "provider_unreachable"), True))
        fake.mode = "redirect"
        try:
            r = chat(jl, lt)
        finally:
            fake.mode = "ok"
        check("relay: a redirect is not followed (502 upstream_redirect)",
              (ecode(r), [q[1] for q in fake.all_reqs() if q[1] == "/followed"]), ((502, "upstream_redirect"), []))
        fake.mode, fake.slow_s = "slow", 3.0
        cpu_lane.LLM_TIMEOUT_S = 1.2
        t0 = time.monotonic()
        try:
            r = chat(jl, lt)
        finally:
            cpu_lane.LLM_TIMEOUT_S = 6.0
            fake.mode = "ok"
        el = time.monotonic() - t0
        check("relay: no answer within LLM_TIMEOUT_S -> 504 timeout, not retried", (ecode(r), el < 2.5),
              ((504, "timeout"), True))
        fake.mode, fake.big_n = "big", 5000
        cpu_lane.LLM_RESP_MAX = 1000
        try:
            r = chat(jl, lt)
        finally:
            cpu_lane.LLM_RESP_MAX = saved["LLM_RESP_MAX"]
            fake.mode = "ok"
        check("relay: an answer over LLM_RESP_MAX -> 502 response_too_large", ecode(r), (502, "response_too_large"))

        # the check order: nothing below reaches the budget
        jn = mk({}, "llm-off")
        ltn = claim("r-llm", caps=ALL, lease_s=900)["lease_token"]
        k0 = view(jl)["llm_calls"]
        got = [ecode(chat("b" * 32, lt)), ecode(chat(jl, "bad")), ecode(chat(jl, None)), ecode(chat(jn, ltn))]
        r = chat(jl, lt, sub="models", body={"x": 1})
        got.append(ecode(r) + (r[2].get("allow"),))
        r = chat(jl, lt, method="GET")
        got.append(ecode(r) + (r[2].get("allow"),))
        for kw in ({"raw": b"not json"}, {"body": [1, 2]}, {"body": {"messages": []}},
                   {"body": {"model": "other-model", "messages": []}}):
            r = chat(jl, lt, **kw)
            got.append(ecode(r) + (emsg(r),))
        cpu_lane.LLM_BODY_MAX = 100
        try:
            got.append(ecode(chat(jl, lt, body={"model": MODEL, "messages": [{"role": "user", "content": "z" * 200}]})))
        finally:
            cpu_lane.LLM_BODY_MAX = saved["LLM_BODY_MAX"]
        check("relay: refusals in order (404 job, 409 lease, 403 llm off, 405 method, 400 body/model, 403 model, "
              "413 size), none of them spending the budget",
              (got, view(jl)["llm_calls"]),
              ([(404, "not_found"), (409, "lease_lost"), (409, "lease_lost"), (403, "llm_not_enabled"),
                (405, "method_not_allowed", "GET"), (405, "method_not_allowed", "POST"),
                (400, "invalid_request_error", "the body must be a JSON object"),
                (400, "invalid_request_error", "the body must be a JSON object"),
                (400, "invalid_request_error", "model required"),
                (403, "model_not_allowed", "model other-model is not in this job's spec.llm.models"),
                (413, "invalid_request_error")], k0))
        r = chat(jl, lt, body={"model": MODEL.upper(), "messages": [{"role": "user", "content": "hi"}]})
        check("relay: spec.llm.models matches case-insensitively", (r[0], view(jl)["llm_calls"]), (200, k0 + 1))

        # budget
        jb = mk({"llm": {"max_calls": 2}}, "llm-budget")
        ltb = claim("r-llm", caps=ALL, lease_s=900)["lease_token"]
        got = [chat(jb, ltb)[0], chat(jb, ltb)[0], ecode(chat(jb, ltb))]
        v = view(jb)
        check("relay: spec.llm.max_calls is a budget (then 403 budget_spent, not a failed call)",
              (got, v["llm_calls"], v["llm_errors"]), ([200, 200, (403, "budget_spent")], 2, 0))

        # concurrency
        jc = mk({"llm": {"max_concurrency": 1}}, "llm-cc")
        ltc = claim("r-llm", caps=ALL, lease_s=900)["lease_token"]
        fake.mode, fake.slow_s = "slow", 1.0
        try:
            t, out = bg(lambda: chat(jc, ltc))
            w = wait_for(lambda: cpu_lane._llm_inflight.get(jc) == 1)
            r = chat(jc, ltc)
            t.join(15)
        finally:
            fake.mode = "ok"
        v = view(jc)
        check("relay: spec.llm.max_concurrency: one call too many in flight -> 429 (Retry-After 2), not counted",
              (w, ecode(r), r[2].get("retry-after"), (out.get("r") or (out.get("e"),))[0], v["llm_calls"],
               v["llm_errors"], dict(cpu_lane._llm_inflight)),
              (True, (429, "concurrency_limit"), "2", 200, 1, 0, {}))

        # a hold met in flight
        jh = mk({"llm": True}, "llm-hold")
        jhc = claim("r-llm", caps=ALL, lease_s=900)
        lth = jhc["lease_token"]
        r1 = call(p, "POST", J + jh + "/log", {"lease_token": lth, "off": 0, "data_b64": b64("attempt 1\n"),
                                                "phase": "running", "progress": "step 1"})[0]
        r2 = call(p, "PUT", J + jh + "/artifacts", raw=tgz, headers={"X-Lease-Token": lth})[0]
        check("hold relay: attempt 1 streamed a log and stored artifacts", (jhc["attempt"], r1, r2), (1, 200, 200))
        HOLD.update(on=True, cause="ci-9")
        n0 = len(fake.chat_reqs())
        try:
            t, out = bg(lambda: chat(jh, lth))
            w = wait_for(lambda: view(jh)["llm_held"] == 1)
            time.sleep(0.5)
            live1 = lanewait(jh, lth)
            time.sleep(0.3)
            live2 = lanewait(jh, lth)
            during = len(fake.chat_reqs()) - n0
        finally:
            HOLD.update(on=False, cause="")
        t.join(15)
        v = view(jh)
        check("hold relay: a call that meets a hold waits it out (nothing reaches the endpoint), then goes through",
              (w, during, (out.get("r") or (out.get("e"),))[0], v["llm_held"], v["llm_errors"], v["llm_calls"]),
              (True, 0, 200, 1, 0, 1))
        check("hold relay: while the call waits, v1/models reports the wait so far as it grows "
              "(cpu_lane.wait_live_s; wait_s holds only answered calls)",
              (isinstance(live1, dict) and live1.get("wait_s"), isinstance(live1, dict) and live1.get("wait_live_s", 0) >= 0.4,
               isinstance(live2, dict) and live1.get("wait_live_s", 0) + 0.2 <= live2.get("wait_live_s", 0)),
              (0.0, True, True))
        r = chat(jh, lth, method="GET", sub="models")
        check("hold relay: the hold's wait goes to llm_wait_s, and v1/models reports it as cpu_lane.wait_s and wait_live_s",
              (v["llm_wait_s"] >= 0.5, (r[1] or {}).get("cpu_lane") if isinstance(r[1], dict) else r[1]),
              (True, {"wait_s": v["llm_wait_s"], "wait_live_s": v["llm_wait_s"]}))
        wh = v["llm_wait_s"]
        cpu_lane.HOLD_POLL_S, cpu_lane.LLM_TIMEOUT_S = 0.4, 1.0
        HOLD.update(on=True, cause="ci-10")
        e0 = view(jh)["llm_errors"]
        try:
            r = chat(jh, lth)
        finally:
            cpu_lane.HOLD_POLL_S, cpu_lane.LLM_TIMEOUT_S = 0.2, 6.0
            HOLD.update(on=False, cause="")
        v = view(jh)
        check("hold relay: a hold that outlasts LLM_TIMEOUT_S -> 503 sandbox_hold naming the cause "
              "(counted as held, not as a failed call, and not as a wait: the client counts that outage)",
              (ecode(r), emsg(r), r[2].get("retry-after"), v["llm_errors"] - e0, v["llm_held"], v["llm_wait_s"]),
              ((503, "sandbox_hold"), cpu_lane.HOLD_TEXT + " (ci-10)", "1", 0, 2, wh))
        lw = lanewait(jh, lth)
        check("hold relay: ... its wait stays in the live count (what the run credited while it grew is not taken back)",
              (lw.get("wait_s"), lw.get("wait_live_s", 0) >= wh + 0.5) if isinstance(lw, dict) else lw, (wh, True))
        HOLD.update(on=True, cause="ci-11")
        try:
            t, out = bg(lambda: chat(jh, lth))
            w = wait_for(lambda: view(jh)["llm_held"] == 3)
            rel = call(p, "POST", J + jh + "/release", {"lease_token": lth})[0]
            t.join(15)
        finally:
            HOLD.update(on=False, cause="")
        r = out.get("r") or (None, None, {})
        check("hold relay: a lease lost while the call waits out a hold -> 409",
              (w, rel, ecode(r), emsg(r)),
              (True, 200, (409, "lease_lost"), "lease lost while the call waited out a darkbloom sandbox hold"))
        j2 = claim("r-llm", caps=ALL, lease_s=900) or {}
        v = view(jh)
        check("re-claim: attempt 2 starts clean (no log, phase, progress, model counts or artifacts of attempt 1)",
              (j2.get("id"), j2.get("attempt"), v["llm_calls"], v["llm_errors"], v["llm_held"], v["llm_wait_s"],
               v["phase"], v["progress"], v["log_size"], v["has_artifacts"],
               call_h(p, "GET", J + jh + "/artifacts", as_bytes=True)[0]),
              (jh, 2, 0, 0, 0, 0.0, None, None, 0, False, 404))
        lth = j2.get("lease_token")

        # a lease lost while the call waits for a reloading endpoint
        jr = mk({"llm": True}, "llm-retry")
        ltr = claim("r-llm", caps=ALL, lease_s=900)["lease_token"]
        fake.mode = "503"
        cpu_lane.LLM_RETRY_S, cpu_lane._LLM_DELAYS = 5.0, (0.5,)
        n0 = len(fake.chat_reqs())
        try:
            t, out = bg(lambda: chat(jr, ltr))
            w = wait_for(lambda: len(fake.chat_reqs()) > n0)
            rel = call(p, "POST", J + jr + "/release", {"lease_token": ltr})[0]
            t.join(15)
        finally:
            fake.mode = "ok"
            cpu_lane.LLM_RETRY_S, cpu_lane._LLM_DELAYS = 0.8, delays
        r = out.get("r") or (None, None, {})
        check("relay: a lease lost while the call waits for a reloading endpoint -> 409",
              (w, rel, ecode(r), emsg(r)),
              (True, 200, (409, "lease_lost"), "lease lost while the call waited for the model endpoint"))

        # v1/models
        cpu_lane._llm_models.update(t=None, base=None, data=None)
        m0 = len(fake.models_reqs())
        r = chat(jl, lt, method="GET", sub="models")
        data = (r[1] or {}).get("data") or [] if isinstance(r[1], dict) else []
        check("models: the endpoint's list cut to spec.llm.models, junk entries dropped, the key cut",
              (r[0], [m.get("id") for m in data], FAKE_KEY in json.dumps(r[1]), (data or [{}])[0].get("note")),
              (200, [MODEL], False, "served with [redacted]"))
        r = chat(jh, lth, method="GET", sub="models")
        data = (r[1] or {}).get("data") or [] if isinstance(r[1], dict) else []
        check("models: a job allowing any model sees the whole list, served from the cache (one upstream call)",
              (r[0], [m.get("id") for m in data], len(fake.models_reqs()) - m0), (200, [MODEL, "Other-Model"], 1))
        fake.models_mode = "down"
        try:
            cpu_lane._llm_models["t"] -= 40
            r = chat(jl, lt, method="GET", sub="models")
            data = (r[1] or {}).get("data") or [] if isinstance(r[1], dict) else []
            check("models: a stale copy stands in while the endpoint does not answer",
                  (r[0], [m.get("id") for m in data]), (200, [MODEL]))
            cpu_lane._llm_models["t"] -= 400
            r = chat(jl, lt, method="GET", sub="models")
            check("models: ...but not past LLM_MODELS_STALE_S", (ecode(r), emsg(r)),
                  ((503, "provider_unavailable"), "the local model endpoint is not answering"))
            lw = (r[1] or {}).get("cpu_lane") if isinstance(r[1], dict) else None
            check("models: the 503 still carries the job's wait count (cpu_lane)",
                  sorted(lw) if isinstance(lw, dict) else lw, ["wait_live_s", "wait_s"])
            lj.unlink()
            r = chat(jl, lt, method="GET", sub="models")
            check("models: no local.json -> off", (ecode(r), emsg(r)),
                  ((503, "provider_unavailable"), "the local model endpoint is off"))
        finally:
            put_lj()
            fake.models_mode = "ok"

        # the relay's own admission pool
        pool = api._CPU_LLM_SLOTS
        api._CPU_LLM_SLOTS = threading.BoundedSemaphore(1)
        fake.mode, fake.slow_s = "slow", 1.0
        try:
            t, out = bg(lambda: chat(jl, lt))
            w = wait_for(lambda: cpu_lane._llm_inflight.get(jl) == 1)
            r = call_h(p, "POST", J + jh + "/llm/v1/chat/completions", body=CHAT, headers={"X-Lease-Token": lth},
                       as_bytes=True)
            hbc = call(p, "POST", J + jl + "/heartbeat", {"lease_token": lt, "runner_id": "r-llm", "lease_s": 900})[0]
            t.join(15)
        finally:
            api._CPU_LLM_SLOTS = pool
            fake.mode = "ok"
        check("admission: model calls draw from their own pool: full -> 503 busy (Retry-After 2) "
              "while heartbeats are still served",
              (w, r[0], r[1], r[2].get("retry-after"), hbc, (out.get("r") or (out.get("e"),))[0]),
              (True, 503, b"server busy, retry shortly", "2", 200, 200))

        # nothing leaks
        v = view(jl)
        check("view: exactly the job_view keys", sorted(v), JOB_VIEW_KEYS)
        d = call(p, "GET", "/api/cpu-lane", token=None)[1]
        check("feed: the running row carries llm and the same counts as the view",
              [(x.get("llm"), x.get("llm_calls"), x.get("llm_errors"), x.get("llm_held"))
               for x in d.get("running", []) if x["id"] == jl],
              [(True, v["llm_calls"], v["llm_errors"], v["llm_held"])])
        blob = json.dumps(v) + json.dumps(d)
        check("leaks: neither the view nor the feed carries the key, a lease token, the command or a bearer",
              [s for s in (FAKE_KEY, lt, "python3 run.py", "Bearer", "lease_token") if s in blob], [])
        check("leaks: the endpoint never saw the runner bearer or a lease token",
              [q[1] for q in fake.all_reqs() if "x-lease-token" in q[2] or "TOKEN" in json.dumps(q[2])], [])

    try:
        for name, fn in (("spec", s_spec), ("units", s_units), ("caps", s_caps), ("hold", s_hold),
                         ("limits", s_limits), ("heartbeat", s_heartbeat), ("log", s_log),
                         ("artifacts", s_artifacts), ("relay", s_relay)):
            section(name, fn)
    finally:
        for k, v in saved.items():
            setattr(cpu_lane, k, v)
        HOLD.update({"on": False, "cause": "", "raise": False})
        S.stop()
        fake.stop()
        os.environ.pop("CPU_LANE_LLM_LOCAL_JSON", None)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=str(Path.home() / "bin" / "ollama-queue-api.py"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="cpulane-api-"))
    tokfile = tmp / "cfg" / "token"
    os.environ["CPU_LANE_DIR"] = str(tmp / "cpu-jobs")
    os.environ["CPU_RUNNER_TOKEN_FILE"] = str(tokfile)
    os.environ["CPU_LANE_LLM_LOCAL_JSON"] = str(tmp / "no-local.json")    # never the real local.json
    sys.path.insert(0, str(Path(a.api).parent))
    import cpu_lane
    spec = importlib.util.spec_from_file_location("oqapi_cpulane", a.api)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    api.q.darkbloom_view = fake_view               # never the real Darkbloom state: the test drives the hold
    api.q.SANDBOX_HOLD_WAIT = True

    # token file creation: 0600, 64 hex chars, never overwritten
    tok = cpu_lane.read_token(create=True)
    mode = stat.S_IMODE(os.stat(tokfile).st_mode)
    check("token: auto-created 0600 with 32 random bytes (hex)", (mode, len(tok)), (0o600, 64))
    tokfile.write_text("TOKEN\n")
    os.chmod(tokfile, 0o600)
    check("token: an existing file is kept and re-read", cpu_lane.read_token(create=True), "TOKEN")

    S = Srv(api)
    p = S.port
    spec_ = {"cmd": "echo hi", "cwd": ".", "timeout_s": 60, "env": {"SECRET_X": "do-not-leak"},
             "payload_kind": "bundle", "stage": "verify", "network": "none"}

    # ---- auth -----------------------------------------------------------------------------
    check("auth: health is open", call(p, "GET", "/api/cpu/health", token=None)[0], 200)
    check("auth: no token -> 401", call(p, "POST", "/api/cpu/jobs", {"spec": spec_}, token=None)[0], 401)
    check("auth: wrong token -> 401", call(p, "POST", "/api/cpu/jobs", {"spec": spec_}, token="nope")[0], 401)
    check("auth: claim without token -> 401", call(p, "POST", "/api/cpu/claim", {"runner_id": "r"}, token=None)[0], 401)
    check("auth: payload download without token -> 401", call(p, "GET", "/api/cpu/jobs/" + "0" * 32 + "/payload", token=None)[0], 401)
    check("auth: a token with a matching PREFIX is not enough", call(p, "GET", "/api/cpu/runners", token="TOKENX")[0], 401)
    for hdr in ({"Cf-Ray": "abc-LAX"}, {"Cf-Connecting-Ip": "1.2.3.4"}, {"X-Forwarded-For": "1.2.3.4"},
                {"Cf-Access-Jwt-Assertion": "x"}, {"Host": "queue.example.com"}):
        check("cloudflare path %s: refused even WITH a valid token" % list(hdr)[0],
              call(p, "POST", "/api/cpu/jobs", {"spec": spec_}, headers=hdr)[0], 403)
    check("cloudflare path: and without a token it is also refused (never a 200)",
          call(p, "POST", "/api/cpu/claim", {"runner_id": "r"}, token=None, headers={"Cf-Ray": "x"})[0] in (401, 403), True)
    # an unrelated dashboard route is untouched by the lane (still served, no token needed)
    check("dashboard routes keep their old trust model (no cpu token)", call(p, "GET", "/api/cpu-lane", token=None)[0], 200)
    tokfile.write_text("")
    check("auth: an EMPTY token file rejects everything (even an empty bearer)",
          call(p, "POST", "/api/cpu/jobs", {"spec": spec_}, token="x")[0], 401)
    tokfile.write_text("TOKEN\n")

    # ---- enrollment (single-use code -> token, LAN-only, rate limited) ----------------------
    st_ = cpu_lane.get_store()
    code = st_.mint_enroll(3600)
    c, b = call(p, "POST", "/api/cpu/enroll", {"code": code, "runner_id": "u1"}, token=None)
    check("enroll: a live code returns the token + runner config (no bearer needed)",
          (c, b.get("token"), b.get("config", {}).get("concurrency")), (200, "TOKEN", 5))
    check("enroll: the same code a second time is refused (single use)",
          call(p, "POST", "/api/cpu/enroll", {"code": code, "runner_id": "u1"}, token=None)[0], 403)
    check("enroll: an unknown code is refused", call(p, "POST", "/api/cpu/enroll", {"code": "x" * 12}, token=None)[0], 403)
    code2 = st_.mint_enroll(3600)
    check("enroll: Cloudflare-edge request is refused even with a valid code",
          call(p, "POST", "/api/cpu/enroll", {"code": code2}, token=None, headers={"Cf-Ray": "x"})[0], 403)
    check("enroll: ...and that refusal did NOT consume the code",
          call(p, "POST", "/api/cpu/enroll", {"code": code2}, token=None)[0], 200)
    code3 = st_.mint_enroll(3600)
    real = st_.clock
    st_.clock = lambda: time.time() + 7200
    expired = call(p, "POST", "/api/cpu/enroll", {"code": code3}, token=None)[0]
    st_.clock = real
    check("enroll: an expired code is refused", expired, 403)
    check("enroll: the code is stored hashed only", code3 in open(st_.db, "rb").read().decode("latin1"), False)
    check("config: needs the bearer", call(p, "GET", "/api/cpu/config", token=None)[0], 401)
    check("config: runner tunables served", call(p, "GET", "/api/cpu/config")[1]["config"]["lease_s"], 60)
    cpu_lane._enroll_fail[:] = []
    codes = [call(p, "POST", "/api/cpu/enroll", {"code": "bad-code-%d" % i}, token=None)[0] for i in range(6)]
    check("enroll: repeated failures are rate limited (429 after 5)", codes, [403] * 5 + [429])
    good = st_.mint_enroll(3600)
    check("enroll: while limited even a valid code is refused (and stays unspent)",
          call(p, "POST", "/api/cpu/enroll", {"code": good}, token=None)[0], 429)
    cpu_lane._enroll_fail[:] = []
    check("enroll: after the window the unspent code works",
          call(p, "POST", "/api/cpu/enroll", {"code": good}, token=None)[0], 200)
    cpu_lane.rotate_token()
    check("rotate: the old token is rejected immediately",
          call(p, "GET", "/api/cpu/runners", token="TOKEN")[0], 401)
    newtok = cpu_lane.read_token()
    check("rotate: the new token (0600) works", (len(newtok), call(p, "GET", "/api/cpu/runners", token=newtok)[0]), (64, 200))
    tokfile.write_text("TOKEN\n")
    os.chmod(tokfile, 0o600)

    # ---- flow -----------------------------------------------------------------------------
    def make(bundle="bA", label="l1", payload=b"PAYLOAD", patch=None, tools=None, ready=True):
        c, b = call(p, "POST", "/api/cpu/jobs", {"spec": spec_, "label": label, "stage": "verify", "bundle_id": bundle})
        assert c == 201, c
        jid = b["id"]
        if payload is not None:
            assert call(p, "PUT", f"/api/cpu/jobs/{jid}/payload", raw=payload)[0] == 200
        if patch:
            call(p, "PUT", f"/api/cpu/jobs/{jid}/patch", raw=patch)
        if tools:
            call(p, "PUT", f"/api/cpu/jobs/{jid}/tools", raw=tools)
        if ready:
            call(p, "POST", f"/api/cpu/jobs/{jid}/ready", {})
        return jid

    c, b = call(p, "POST", "/api/cpu/jobs", {"spec": spec_, "bundle_id": "bA"})
    j0 = b["id"]
    check("flow: ready without a payload -> 400", call(p, "POST", f"/api/cpu/jobs/{j0}/ready", {})[0], 400)
    check("flow: created job is 'uploading'", call(p, "GET", f"/api/cpu/jobs/{j0}")[1]["status"], "uploading")
    check("flow: nothing claimable while uploading", call(p, "POST", "/api/cpu/claim", {"runner_id": "r1"})[0], 204)
    call(p, "DELETE", f"/api/cpu/jobs/{j0}")
    check("flow: cancelling an uploading job -> cancelled", call(p, "GET", f"/api/cpu/jobs/{j0}")[1]["status"], "cancelled")

    big = os.urandom(3 * 1024 * 1024 + 17)
    j1 = make(label="first", payload=big, patch=b"PATCH", tools=b"TOOLS")
    j2 = make(label="second", bundle="bB")
    check("flow: ready -> pending", call(p, "GET", f"/api/cpu/jobs/{j1}")[1]["status"], "pending")
    c, claim = call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "lease_s": 30})
    job = claim["job"]
    check("claim: oldest pending first, attempt 1, has_patch/has_tools derived server-side",
          (c, job["id"], job["attempt"], job["spec"]["has_patch"], job["spec"]["has_tools"]), (200, j1, 1, True, True))
    check("claim: spec comes back intact", job["spec"]["cmd"], "echo hi")
    check("blob: a multi-MB payload round-trips byte for byte",
          call(p, "GET", f"/api/cpu/jobs/{j1}/payload")[1] == big, True)
    check("blob: patch + tools served", (call(p, "GET", f"/api/cpu/jobs/{j1}/patch")[1],
                                         call(p, "GET", f"/api/cpu/jobs/{j1}/tools")[1]), (b"PATCH", b"TOOLS"))
    check("blob: j2 has no patch -> 404", call(p, "GET", f"/api/cpu/jobs/{j2}/patch")[0], 404)
    check("blob: upload to a job that is no longer 'uploading' -> 409",
          call(p, "PUT", f"/api/cpu/jobs/{j1}/payload", raw=b"x")[0], 409)
    c, claim2 = call(p, "POST", "/api/cpu/claim", {"runner_id": "r2", "lease_s": 30})
    check("claim: a second runner gets the NEXT job (atomic, no double claim)", claim2["job"]["id"], j2)
    check("claim: queue empty -> 204", call(p, "POST", "/api/cpu/claim", {"runner_id": "r1"})[0], 204)
    call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "caps": {"node": 26, "python": "3.14"}})
    rs = {x["runner"]: x for x in call(p, "GET", "/api/cpu/runners")[1]["runners"]}
    check("claim caps: a runner's reported capabilities are served by /api/cpu/runners (r2 reported none)",
          (rs["r1"].get("caps"), "caps" in rs["r2"]), ({"node": 26, "python": "3.14"}, False))
    st = call(p, "GET", f"/api/cpu/jobs/{j1}")[1]
    check("status: running with runner id", (st["status"], st["runner_id"], st["attempt"]), ("running", "r1", 1))
    check("heartbeat: ok + cancel false",
          call(p, "POST", f"/api/cpu/jobs/{j1}/heartbeat", {"runner_id": "r1", "lease_token": job["lease_token"], "lease_s": 30})[1],
          {"ok": True, "cancel": False})
    check("heartbeat: wrong lease token -> 409",
          call(p, "POST", f"/api/cpu/jobs/{j1}/heartbeat", {"runner_id": "r1", "lease_token": "bad"})[0], 409)
    check("cancel: DELETE on a running job -> heartbeat reports cancel:true",
          (call(p, "DELETE", f"/api/cpu/jobs/{j2}")[0],
           call(p, "POST", f"/api/cpu/jobs/{j2}/heartbeat", {"lease_token": claim2["job"]["lease_token"]})[1]["cancel"]), (200, True))
    check("result: stale token -> 409 and nothing recorded",
          call(p, "POST", f"/api/cpu/jobs/{j1}/result", {"lease_token": "bad", "exit_code": 0})[0], 409)
    check("result: ok", call(p, "POST", f"/api/cpu/jobs/{j1}/result",
                             {"runner_id": "r1", "lease_token": job["lease_token"], "exit_code": 3, "timed_out": False,
                              "stdout_tail": "out", "stderr_tail": "err", "timings": {"run_s": 1.5}})[0], 200)
    st = call(p, "GET", f"/api/cpu/jobs/{j1}")[1]
    check("result: done, exit code + tails + timings stored, lease token NOT echoed",
          (st["status"], st["result"]["exit_code"], st["result"]["stdout_tail"], st["result"]["timings"],
           "lease_token" in st["result"]), ("done", 3, "out", {"run_s": 1.5}, False))
    check("result: a second result for the same lease -> 409 (idempotent-safe)",
          call(p, "POST", f"/api/cpu/jobs/{j1}/result", {"lease_token": job["lease_token"], "exit_code": 0})[0], 409)
    call(p, "POST", f"/api/cpu/jobs/{j2}/result", {"lease_token": claim2["job"]["lease_token"], "exit_code": 0})
    check("cancel: a cancelled running job ends 'cancelled', not done",
          call(p, "GET", f"/api/cpu/jobs/{j2}")[1]["status"], "cancelled")

    # ---- release + cancel pending ---------------------------------------------------------
    j3 = make(label="rel")
    c3 = call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "lease_s": 30})[1]["job"]
    check("release: ok", call(p, "POST", f"/api/cpu/jobs/{j3}/release", {"lease_token": c3["lease_token"]})[0], 200)
    check("release: job back to pending", call(p, "GET", f"/api/cpu/jobs/{j3}")[1]["status"], "pending")
    call(p, "DELETE", f"/api/cpu/jobs/{j3}")
    check("cancel: a pending job is cancelled and never claimed",
          (call(p, "GET", f"/api/cpu/jobs/{j3}")[1]["status"], call(p, "POST", "/api/cpu/claim", {"runner_id": "r1"})[0]),
          ("cancelled", 204))

    # ---- lease expiry + 3 attempts --------------------------------------------------------
    # Leases are clamped to 10..3600 s, so the store clock moves past each one instead of the test sleeping.
    j4 = make(label="dies", bundle="bL")
    seen, leases = [], []
    s4 = cpu_lane.get_store()
    real4, shift = s4.clock, [0.0]
    s4.clock = lambda: real4() + shift[0]
    try:
        for n in (1, 2, 3):
            c, cl = call(p, "POST", "/api/cpu/claim", {"runner_id": "rdead", "lease_s": 1})
            seen.append((c, cl["job"]["attempt"] if cl else None))
            row = s4.get(j4)
            leases.append(round(row["lease_expires"] - row["claimed_at"]))
            shift[0] += 11
            s4.reap()
    finally:
        s4.clock = real4
    check("lease expiry: claimed 3 times (attempts 1,2,3)", seen, [(200, 1), (200, 2), (200, 3)])
    check("lease: a 1 s ask is held to the 10 s floor", leases, [10, 10, 10])
    st = call(p, "GET", f"/api/cpu/jobs/{j4}")[1]
    check("3 attempts exhausted -> failed_infra with an infra_error",
          (st["status"], "lease expired" in st["result"]["infra_error"]), ("failed_infra", True))
    check("...and it is never offered again", call(p, "POST", "/api/cpu/claim", {"runner_id": "r1"})[0], 204)
    check("lease expiry: a late heartbeat from the dead runner -> 409",
          call(p, "POST", f"/api/cpu/jobs/{j4}/heartbeat", {"lease_token": "whatever"})[0], 409)

    # an infra_error result is failed_infra at once (isolation_unavailable is retried)
    j5 = make(label="infra")
    cl = call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "lease_s": 30})[1]["job"]
    call(p, "POST", f"/api/cpu/jobs/{j5}/result", {"lease_token": cl["lease_token"], "exit_code": None, "infra_error": "isolation_unavailable"})
    check("result: isolation_unavailable on attempt 1 -> re-queued", call(p, "GET", f"/api/cpu/jobs/{j5}")[1]["status"], "pending")
    cl = call(p, "POST", "/api/cpu/claim", {"runner_id": "r1", "lease_s": 30})[1]["job"]
    call(p, "POST", f"/api/cpu/jobs/{j5}/result", {"lease_token": cl["lease_token"], "exit_code": None, "infra_error": "disk full"})
    check("result: any other infra_error -> failed_infra", call(p, "GET", f"/api/cpu/jobs/{j5}")[1]["status"], "failed_infra")

    # ---- durability across an API restart ---------------------------------------------------
    j6 = make(label="pend-surviving", payload=b"P6", patch=b"D6", bundle="bR")
    j7 = make(label="run-surviving", payload=b"P7", bundle="bR")
    cl7 = call(p, "POST", "/api/cpu/claim", {"runner_id": "rX", "lease_s": 120})[1]["job"]
    # (j6 is older, so it was claimed first -- remember who got what)
    first = cl7["id"]
    other = j7 if first == j6 else j6
    S.stop()
    cpu_lane._store = None                       # forget the in-memory singleton: a fresh process
    S = Srv(api)
    p = S.port
    st_first = call(p, "GET", f"/api/cpu/jobs/{first}")[1]
    st_other = call(p, "GET", f"/api/cpu/jobs/{other}")[1]
    check("restart: the running job is still running with its runner", (st_first["status"], st_first["runner_id"]), ("running", "rX"))
    check("restart: the pending job is still pending", st_other["status"], "pending")
    check("restart: the runner's old lease token still heartbeats (no 409 after an API restart)",
          call(p, "POST", f"/api/cpu/jobs/{first}/heartbeat", {"lease_token": cl7["lease_token"], "lease_s": 120})[0], 200)
    check("restart: blobs survived", (call(p, "GET", f"/api/cpu/jobs/{j6}/payload")[1], call(p, "GET", f"/api/cpu/jobs/{j6}/patch")[1]),
          (b"P6", b"D6"))
    check("restart: the pending job can still be claimed after the restart",
          call(p, "POST", "/api/cpu/claim", {"runner_id": "rY", "lease_s": 30})[1]["job"]["id"], other)
    check("restart: finished jobs keep their results", call(p, "GET", f"/api/cpu/jobs/{j1}")[1]["result"]["exit_code"], 3)
    check("restart: outstanding_by_bundle sees the in-flight jobs (the queue's lane-release input)",
          sorted(cpu_lane.get_store().outstanding_by_bundle().get("bR", [])), sorted([j6, j7]))

    # ---- dashboard feed -------------------------------------------------------------------
    c, d = call(p, "GET", "/api/cpu-lane", token=None)
    blob = json.dumps(d)
    check("dashboard: counts + running (with runner id) + recent durations present",
          (c, d["enabled"], {r["runner"] for r in d["running"]} >= {"rX", "rY"}, bool(d["recent"]),
           "queue_depth" in d), (200, True, True, True, True))
    check("dashboard: read-only feed never exposes the command, env or token",
          ("echo hi" in blob, "do-not-leak" in blob, "TOKEN" in blob), (False, False, False))
    check("dashboard: the HTML carries the CPU lane section", "cpuLanePanel" in api.FRONTEND_HTML, True)

    # prune: finished rows/blobs older than the window go; live ones stay
    s = cpu_lane.get_store()
    real_clock = s.clock
    s.clock = lambda: time.time() + 30 * 86400
    n = s.prune()
    s.clock = real_clock
    check("prune: old finished rows are deleted, in-flight ones kept",
          (n >= 3, call(p, "GET", f"/api/cpu/jobs/{j1}")[0], call(p, "GET", f"/api/cpu/jobs/{first}")[0]), (True, 404, 200))
    S.stop()

    remote_lane(api, cpu_lane, tmp)

    bad = RESULTS.count(False)
    print("CPU_LANE_API_TEST_OK" if not bad else f"{bad} FAILED")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
