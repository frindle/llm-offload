#!/usr/bin/env python3
"""Hermetic tests for Darkbloom NO-SWITCH handling (2026-10-13).

The owner's decision: Darkbloom advertises THREE models (qwen3.6-35b, Qwen3.5-9B, gemma-4-26b-qat-4bit) with
2 slots, autopilot on, nothing pinned, and the queue must NOT switch/restart models. The provider loads a
requested model by evicting an IDLE slot and REFUSES (HTTP 429 'Provider capacity is temporarily
unavailable') when the others are serving. The queue's only levers are WAIT and RETRY.

Covers: the verdict matrix over {35B,9B} / {Gemma,9B} busy / {Gemma,9B} idle / only-35B / nothing resident /
load_transition_active / stale state; hold_for_queue never runs start/stop/switch/restart or edits
provider.toml when the pair is advertised (and DOES, unchanged, when it is not, or BLOOM_NOSWITCH=0);
noswitch_ensure (one bounded warm, bounded backoff, refusal is never a failure, release on resident);
the queue's INFRA WAIT gate, capacity-pause resume (uncharged, backoff), lane work, unsat escalation after
QUEUE_BLOOM_WAIT_WARN_S, and the worker's capacity wait (uncharged, named) vs the legacy path.

NEVER touches the real daemon/Darkbloom/BloomGauge: fake daemon-state.json / loaded-models.json, a fake
Darkbloom HTTP stub and CLI shim (reuses test-bloom-control.py's World for BloomGauge + shim).
Usage: test-bloom-noswitch.py [--queue P] [--bc P] [--worker P] [--tc P] [--only NAME]   Marker: ALL PASS."""
import argparse
import importlib.util
import json
import os
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIG, SMALL, GEM = "qwen3.6-35b-a3b-vl-mtp-mxfp8", "Qwen3.5-9B", "gemma-4-26b-qat-4bit"
ALL3 = [SMALL, BIG, GEM]
FAILS = []
SEL = []
NOW = 1_800_000_000.0


def check(name, cond, extra=""):
    print(("ok  : " if cond else "FAIL: ") + name + ("" if cond else "  -- " + str(extra)))
    if not cond:
        FAILS.append(name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def scenario(name):
    def deco(fn):
        fn._name = name
        SEL.append(fn)
        return fn
    return deco


GB = {SMALL: 5.7, BIG: 20.0, GEM: 15.0}


class Prov:
    """The provider's state files + an HTTP stub, in a temp dir. set() rewrites daemon-state.json /
    loaded-models.json the way the real daemon does (~1/s)."""

    def __init__(self, tc_world=None):
        self.d = Path(tempfile.mkdtemp(prefix="nsw-"))
        self.ds = self.d / "daemon-state.json"
        self.ld = self.d / "loaded-models.json"
        self.chats = []
        self.codes = {}            # model -> [codes], consumed in order
        self.default = 200
        self.load_on_ok = True     # a 200 for a non-resident model makes the provider load it (evicting idle)
        self.advertised = list(ALL3)
        self.resident = []
        self.idle = {}
        self.usable = 20.0
        self.transition = False
        self.slots = 2
        self.autopilot = True
        p = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                b = json.dumps({"status": "ok"} if self.path == "/health"
                               else {"data": [{"id": m} for m in p.advertised]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                m = body.get("model")
                p.chats.append(m)
                cs = p.codes.get(m)
                code = cs.pop(0) if cs else p.default
                if code == 200 and p.load_on_ok and m not in p.resident:
                    # provider evicts the longest-idle slot when full, then loads
                    if len(p.resident) >= p.slots:
                        p.resident.sort(key=lambda r: -(p.idle.get(r, 0)))
                        p.resident.pop(0)
                    p.resident.append(m)
                    p.idle[m] = 0
                    p.set()
                b = json.dumps({"error": "Provider capacity is temporarily unavailable."} if code != 200
                               else {"choices": []}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.srv.server_address[1]
        self.local = self.d / "local.json"
        self.local.write_text(json.dumps({"api_key": "k" * 8, "base_url": self.base + "/v1"}))
        self.toml = self.d / "provider.toml"
        self.toml.write_text("[backend]\nmax_model_slots = 2\npreload_models = [ 'Qwen3.5-9B' ]\n")
        self.home = self.d / "home"
        self.home.mkdir()
        self.set()

    def set(self, resident=None, idle=None, usable=None, transition=None, age=0.0, autopilot=None, advertised=None):
        if resident is not None:
            self.resident = list(resident)
        if idle is not None:
            self.idle = dict(idle)
        if usable is not None:
            self.usable = usable
        if transition is not None:
            self.transition = transition
        if autopilot is not None:
            self.autopilot = autopilot
        if advertised is not None:
            self.advertised = list(advertised)
        ds = {"schema": 1, "written_at": time.time() - age, "advertised_models": self.advertised,
              "warm_models": self.resident, "load_transition_active": self.transition,
              "capacity": {"load_usable_gb": self.usable, "load_headroom_gb": 6.5,
                           "load_transition_active": self.transition, "free_for_load_gb": 0}}
        if self.autopilot:
            ds["autopilot_state"] = {"max_model_slots": self.slots, "resident_models": [
                {"model_id": m, "idle_seconds": self.idle.get(m, 0), "resident_gb": GB.get(m, 10.0),
                 "weights_gb": GB.get(m, 10.0)} for m in self.resident]}
        self.ds.write_text(json.dumps(ds))
        self.ld.write_text(json.dumps({"models": self.resident}))

    def env(self):
        e = {"BLOOMCTL_DAEMON_JSON": str(self.ds), "BLOOMCTL_LOADED_JSON": str(self.ld),
             "BLOOMCTL_LOCAL_JSON": str(self.local), "BLOOMCTL_HOME": str(self.home),
             "BLOOMCTL_PROVIDER_TOML": str(self.toml), "BLOOMCTL_PAIR": ",".join([SMALL, BIG]),
             "BLOOMCTL_POLL_S": "0.1", "BLOOMCTL_WARM_S": "1", "BLOOMCTL_WARM_REQ_S": "5"}
        e["BLOOMCTL_PROVIDER_PID"] = str(self.home / "no-provider.pid")
        e["BLOOM_PAIR_RESTART"] = "0"          # these tests cover the NO-SWITCH mode (the pair-restart kill-switch state)
        os.environ.update(e)
        for k in ("BLOOM_NOSWITCH", "BLOOM_CONTROL", "BLOOMCTL_DRY_RUN", "BLOOMCTL_STATE", "BLOOMCTL_DARKBLOOM_BIN"):
            os.environ.pop(k, None)
        return e

    def state(self):
        try:
            return json.loads((self.home / "bloom-control-state.json").read_text())
        except OSError:
            return {}

    def close(self):
        self.srv.shutdown()


def fresh_est(bc):
    bc._NS_EST_CACHE.update(at=0.0, est=None)


# ----------------------------------------------------------------------------------------- verdicts
@scenario("verdict")
def t_verdict(bc, q, wk, tc):
    print("--- verdict matrix (3 advertised / 2 resident)")
    p = Prov()
    try:
        p.env()
        cfg = bc.load_cfg()

        def v(model, **kw):
            p.set(**kw)
            return bc.model_verdict(cfg, model, bc.provider_snapshot(cfg))

        r = v(BIG, resident=[BIG, SMALL], idle={BIG: 2, SMALL: 19}, usable=5.7)
        check("{35B,9B}: a 35B job is resident -> nothing to wait for", r["state"] == "resident", r)
        r = v(GEM, resident=[BIG, SMALL], idle={BIG: 2, SMALL: 500}, usable=5.7)
        check("{35B,9B}, 35B serving: Gemma blocked, blocker named (the busy 35B)", r["state"] == "blocked"
              and BIG in r["reason"] and "23.9 GB" in r["reason"] and "5.7 GB" in r["reason"], r)
        r = v(BIG, resident=[GEM, SMALL], idle={GEM: 1, SMALL: 2}, usable=16.9)
        check("{Gemma,9B} BUSY: the 35B is blocked (needs 30.3, have 16.9), Gemma named",
              r["state"] == "blocked" and GEM in r["reason"] and "needs 30.3 GB" in r["reason"]
              and "load_usable 16.9 GB" in r["reason"] and r["blockers"] == [GEM, SMALL], r)
        check("blocked reason is the dashboard text 'qwen 35B blocked by Gemma serving'",
              r["reason"].startswith(BIG + " blocked by " + GEM), r["reason"])
        r = v(BIG, resident=[GEM, SMALL], idle={GEM: 1, SMALL: 1}, usable=40.0)
        check("{Gemma,9B} BUSY with plenty of memory: both slots in use -> still blocked (slot, not memory, is the limit)",
              r["state"] == "blocked" and "model slots in use" in r["reason"], r)
        r = v(BIG, resident=[GEM, SMALL], idle={GEM: 300, SMALL: 300}, usable=16.9)
        check("{Gemma,9B} IDLE: the provider can evict -> evict-idle (warm request decides)",
              r["state"] == "evict-idle" and GEM in r["blockers"], r)
        r = v(BIG, resident=[GEM, SMALL], idle={GEM: 300, SMALL: 1}, usable=16.9)
        check("{Gemma idle, 9B busy}: evicting Gemma alone is enough (16.9+15 >= 30.3) -> evict-idle",
              r["state"] == "evict-idle", r)
        r = v(BIG, resident=[GEM, SMALL], idle={GEM: 300, SMALL: 1}, usable=5.0)
        check("{Gemma idle, 9B busy}: evicting Gemma alone is NOT enough -> blocked by the busy 9B",
              r["state"] == "blocked" and SMALL in r["reason"], r)
        r = v(SMALL, resident=[BIG], idle={BIG: 1}, usable=14.0)
        check("only-35B resident: a free slot and 14 GB usable -> the 9B fits", r["state"] == "fits", r)
        r = v(GEM, resident=[BIG], idle={BIG: 1}, usable=14.0)
        check("only-35B resident: Gemma (23.9) does NOT fit 14 GB and the 35B is busy -> blocked by the 35B",
              r["state"] == "blocked" and BIG in r["reason"], r)
        r = v(BIG, resident=[], usable=40.0)
        check("nothing resident, 40 GB usable: the 35B fits", r["state"] == "fits", r)
        r = v(BIG, resident=[], usable=20.0)
        check("nothing resident, 20 GB usable: blocked by memory held OUTSIDE Darkbloom (no false blocker)",
              r["state"] == "blocked" and "outside Darkbloom" in r["reason"] and not r["blockers"], r)
        r = v(BIG, resident=[GEM, SMALL], idle={GEM: 1, SMALL: 1}, usable=16.9, transition=True)
        check("load_transition_active: wait a moment (transition), no blocker accusation",
              r["state"] == "transition", r)
        r = v(BIG, resident=[GEM, SMALL], usable=16.9, autopilot=False, transition=False)
        check("no idle signal in the state file: unknown -> let the warm request decide", r["state"] == "unknown", r)
        p.set(age=120.0, autopilot=True)
        r = bc.model_verdict(cfg, BIG, bc.provider_snapshot(cfg))
        check("stale daemon-state.json (>30 s): unknown/unreadable, never a blocked verdict",
              r["state"] == "unknown" and "unreadable" in r["reason"], r)
        r = v("some-other-model", resident=[BIG, SMALL], idle={}, usable=5.0)
        check("a model the provider does not advertise -> not-advertised (old restart path)",
              r["state"] == "not-advertised", r)
        # need table: documented fallback and the CLI source
        fresh_est(bc)
        snap = bc.provider_snapshot(cfg)
        n = {m: round(bc.model_need_gb(cfg, m, snap), 1) for m in (BIG, GEM, SMALL)}
        check("need = estimate + load_headroom_gb: 35B 30.3, Gemma ~23.9, 9B ~13.3 (fallback table)",
              n == {BIG: 30.3, GEM: 23.9, SMALL: 13.3}, n)
        est = bc.model_estimates(cfg, refresh=True, run=lambda: json.dumps(
            {"models": [{"id": BIG, "estimated_memory_gb": 30.0}]}), now=NOW)
        check("estimates are taken from `darkbloom models list --all --json` when available",
              est[BIG] == 30.0 and est[GEM] == bc.NS_FALLBACK_EST_GB[GEM.lower()], est)
        fresh_est(bc)
        est = bc.model_estimates(cfg, refresh=True, run=lambda: "not json", now=NOW)
        check("CLI unavailable/garbage -> fallback table, never an exception", est[BIG] == 23.8, est)
        fresh_est(bc)
        check("the CLI is NEVER run on the hot path (refresh=False)", bc.model_estimates(
            cfg, run=lambda: (_ for _ in ()).throw(AssertionError("ran"))) is not None)
    finally:
        fresh_est(bc)
        p.close()


# ----------------------------------------------------------------------------------------- ensure/warm
@scenario("ensure")
def t_ensure(bc, q, wk, tc):
    print("--- noswitch_ensure: bounded warm, backoff, release")
    p = Prov()
    try:
        p.env()
        cfg = bc.load_cfg()
        # evict-idle -> exactly one warm; the provider (stub) evicts the idle slot and loads
        p.set(resident=[GEM, SMALL], idle={GEM: 300, SMALL: 5}, usable=16.9)
        r = bc.noswitch_ensure(cfg, [BIG], now=NOW)
        check("evict-idle: one warm request, the provider loaded the 35B", p.chats == [BIG] and BIG in p.resident
              and GEM not in p.resident, (p.chats, p.resident))
        check("released: no record left for the model", BIG.lower() not in (p.state().get("noswitch") or {}), p.state())
        check("heartbeat written (the queue gate trusts a live sync)", p.state().get("noswitch_hb"), p.state())
        r2 = bc.noswitch_ensure(cfg, [BIG], now=NOW + 1)
        check("resident: no further request", p.chats == [BIG] and r2[BIG] == "resident", (p.chats, r2))
        # blocked + the provider refuses: backoff, no failure, no extra requests inside the window
        p.chats.clear()
        p.set(resident=[GEM, SMALL], idle={GEM: 1, SMALL: 1}, usable=16.9)
        p.default = 429
        t = NOW + 100
        bc.noswitch_ensure(cfg, [BIG], now=t)
        rec = p.state()["noswitch"][BIG.lower()]
        check("blocked: one probe warm sent, refusal recorded (attempts 1, state blocked, blockers named)",
              p.chats == [BIG] and rec["attempts"] == 1 and rec["state"] == "blocked" and GEM in rec["blockers"], (p.chats, rec))
        bc.noswitch_ensure(cfg, [BIG], now=t + 1)
        check("inside the backoff/probe window: NO second request", p.chats == [BIG], p.chats)
        check("a refusal is never a failure: phase untouched, no 'failed'", p.state().get("phase") in (None, "idle"), p.state())
        # bounded probe cadence: after ns_probe_s another probe; backoff capped
        for i in range(1, 9):
            t += cfg["ns_probe_s"] + cfg["ns_backoff_max_s"] + 1
            bc.noswitch_ensure(cfg, [BIG], now=t)
        rec = p.state()["noswitch"][BIG.lower()]
        check("probes continue at a bounded cadence (one per window)", len(p.chats) == 9, len(p.chats))
        check("backoff is capped at ns_backoff_max_s", rec["next_warm_at"] - t <= cfg["ns_backoff_max_s"] + 1, (rec, t))
        check("'since' is stable across probes (escalation clock)", abs(rec["since"] - (NOW + 100)) < 1, rec)
        # the blocker goes idle -> the next due warm succeeds and the record is cleared
        p.default = 200
        p.set(resident=[GEM, SMALL], idle={GEM: 400, SMALL: 400}, usable=16.9)
        rec = p.state()["noswitch"][BIG.lower()]
        t += 1000
        r = bc.noswitch_ensure(cfg, [BIG], now=t)
        check("blocker idle again: warm succeeded, 35B resident, record cleared",
              BIG in p.resident and BIG.lower() not in (p.state().get("noswitch") or {}), (p.resident, p.state()))
        # transition: no warm
        p.chats.clear()
        p.set(resident=[GEM, SMALL], idle={GEM: 400, SMALL: 400}, usable=16.9, transition=True)
        p.resident = [GEM, SMALL]
        p.set()
        bc.noswitch_ensure(cfg, [BIG], now=t + 5000)
        check("load_transition_active: no warm request while the provider is mid-load", p.chats == [], p.chats)
        # a model nothing needs any more is forgotten
        p.set(transition=False)
        bc.noswitch_ensure(cfg, [], now=t + 6000)
        check("records of models nothing needs are dropped", not p.state().get("noswitch"), p.state())
        # no live provider state at all (sandbox / daemon down): no request, no state written
        p.chats.clear()
        p.set(age=300.0)
        out = bc.noswitch_ensure(cfg, [BIG], now=t + 6500)
        check("stale/absent daemon-state.json: never warm blind (no request)", out == {} and p.chats == [], (out, p.chats))
        p.set(age=0.0)
        # unaskable endpoint (404): treated as 'cannot tell', probed at the slow cadence, never an error
        p.chats.clear()
        p.default = 404
        p.set(resident=[GEM, SMALL], idle={GEM: 400, SMALL: 400}, usable=16.9)
        out = bc.noswitch_ensure(cfg, [BIG], now=t + 7000)
        check("404: warm-unknown, no exception, one request", out[BIG] == "warm-unknown" and len(p.chats) == 1, (out, p.chats))
    finally:
        p.close()


# ----------------------------------------------------------------------------------------- hold
@scenario("hold")
def t_hold(bc, q, wk, tc):
    print("--- hold_for_queue never switches when the pair is advertised")
    w = tc.World(bc.__file__, serving=[GEM, SMALL], mode="on", auto_update=False)
    p = Prov()
    try:
        p.env()
        for k, v in w.env.items():
            os.environ[k] = v
        os.environ["BLOOMCTL_DAEMON_JSON"] = str(p.ds)
        os.environ["BLOOMCTL_LOADED_JSON"] = str(p.ld)
        os.environ["BLOOMCTL_LOCAL_JSON"] = str(p.local)
        os.environ["BLOOMCTL_HOME"] = str(w.home)
        p.home = w.home
        os.environ["BLOOMCTL_PROVIDER_TOML"] = str(w.toml)
        p.set(resident=[GEM, SMALL], idle={GEM: 1, SMALL: 1}, usable=16.9)      # 3 advertised, {Gemma,9B} resident
        before = w.toml.read_text()
        r = bc.hold_for_queue()
        check("hold ok in no-switch mode", r["ok"] and r.get("noswitch"), r)
        bad = [c for c in w.calls() if c.split()[0] in ("start", "stop", "switch", "restart", "pause", "pin")]
        check("NO darkbloom start/stop/switch/restart/pause/pin was run", not bad, w.calls())
        check("provider.toml byte-identical (no edit)", w.toml.read_text() == before, w.toml.read_text())
        check("phase ready, mode queue", w.state().get("phase") == "ready" and w.state().get("mode") == "queue", w.state())
        check("BloomGauge parked on Manual (does not fight the provider)", w.sv["mode"] == "manual", w.posts)
        check("no warm/inference request from the hold itself", p.chats == [], p.chats)
        r2 = bc.hold_for_queue()
        check("re-hold is a no-op", r2["ok"] and r2["noop"] and not [c for c in w.calls() if c.startswith("start")], r2)
        # the 35B NOT advertised at all -> the restart-based hold still runs (unchanged)
        p.set(advertised=[SMALL, GEM], resident=[GEM, SMALL])
        w2 = tc.World(bc.__file__, serving=[SMALL, GEM], mode="manual", auto_update=False)
        for k, v in w2.env.items():
            os.environ[k] = v
        os.environ["BLOOMCTL_LOCAL_JSON"] = str(p.local)
        os.environ["BLOOMCTL_DAEMON_JSON"] = str(p.ds)
        os.environ["BLOOMCTL_PROVIDER_TOML"] = str(w2.toml)
        os.environ["BLOOMCTL_LOADED_JSON"] = str(w2.loaded)
        w2.loaded.write_text(json.dumps({"models": [SMALL, GEM]}))
        try:
            r3 = bc.hold_for_queue()
            check("pair NOT advertised: the restart-based hold still issues `darkbloom start`",
                  any(c.startswith("start") for c in w2.calls()), (r3, w2.calls()))
        finally:
            w2.close()
        # kill switch BLOOM_NOSWITCH=0 -> today's behaviour for an advertised-but-not-resident pair
        w3 = tc.World(bc.__file__, serving=[GEM, SMALL], mode="manual", auto_update=False)
        try:
            for k, v in w3.env.items():
                os.environ[k] = v
            os.environ["BLOOMCTL_LOCAL_JSON"] = str(p.local)
            os.environ["BLOOMCTL_DAEMON_JSON"] = str(p.ds)
            p.set(advertised=ALL3, resident=[GEM, SMALL])
            os.environ["BLOOM_NOSWITCH"] = "0"
            r4 = bc.hold_for_queue()
            check("BLOOM_NOSWITCH=0: reverts to the restart-based hold (a start is issued)",
                  any(c.startswith("start") for c in w3.calls()) and not r4.get("noswitch"), (r4, w3.calls()))
            check("BLOOM_NOSWITCH=0: noswitch_mode False and the gate is silent",
                  bc.noswitch_mode() is False and bc.noswitch_gate_reason(BIG) is None)
        finally:
            os.environ.pop("BLOOM_NOSWITCH", None)
            w3.close()
    finally:
        os.environ.pop("BLOOM_NOSWITCH", None)
        p.close()
        w.close()


# ----------------------------------------------------------------------------------------- gate
@scenario("gate")
def t_gate(bc, q, wk, tc):
    print("--- queue INFRA WAIT gate")
    p = Prov()
    try:
        p.env()
        q._BLOOM_CTL_MOD[:] = [bc]
        cfg = bc.load_cfg()
        p.set(resident=[GEM, SMALL], idle={GEM: 1, SMALL: 1}, usable=16.9)
        check("sync not alive (no heartbeat): the gate fails OPEN (a dead sync never freezes the queue)",
              bc.noswitch_gate_reason(BIG) is None)
        bc.write_state(cfg, noswitch_hb=time.time())
        g = bc.noswitch_gate_reason(BIG)
        check("blocked: reason names the blocker, the need and load_usable",
              bool(g) and GEM in g and "30.3 GB" in g and "16.9 GB" in g, g)
        wn = q.darkbloom_launch_wait(BIG, time.time(), ready=True)
        check("launch wait is an INFRA WAIT note naming 'blocked by gemma' (not a failure)",
              wn is not None and "infra" in json.dumps(wn).lower() and GEM in json.dumps(wn), wn)
        check("a resident model launches", q.darkbloom_launch_wait(SMALL, time.time(), ready=True) is None)
        p.set(resident=[GEM, SMALL], idle={GEM: 300, SMALL: 300}, usable=16.9)
        g2 = bc.noswitch_gate_reason(BIG)
        check("evict-idle: waits as 'warming' (the background warm makes the provider evict)",
              bool(g2) and g2.startswith("warming " + BIG), g2)
        p.set(resident=[BIG, SMALL], idle={BIG: 1, SMALL: 1}, usable=5.0)
        check("released the moment the model is resident (no interval needed)",
              q.darkbloom_launch_wait(BIG, time.time(), ready=True) is None)
        p.set(resident=[GEM, SMALL], idle={GEM: 1, SMALL: 1}, usable=16.9, age=120.0)
        check("stale provider state: fail open", bc.noswitch_gate_reason(BIG) is None)
        p.set(age=0.0)
        check("a model the provider does not advertise is not gated here (existing not-served path)",
              bc.noswitch_gate_reason("zzz-model") is None)
        os.environ["BLOOM_NOSWITCH"] = "0"
        check("BLOOM_NOSWITCH=0: no gate", bc.noswitch_gate_reason(BIG) is None
              and q.darkbloom_noswitch_gate(BIG, time.time()) is None)
        os.environ.pop("BLOOM_NOSWITCH")
        os.environ["BLOOM_CONTROL"] = "0"
        check("BLOOM_CONTROL=0: integration off -> no gate", bc.noswitch_gate_reason(BIG) is None)
        os.environ.pop("BLOOM_CONTROL")
        # sync wiring
        q._darkbloom_url = lambda: "http://127.0.0.1:1"
        q._darkbloom_model = lambda m: m
        jobs = [{"id": "a", "status": "pending", "host_pref": "studio-db", "model": BIG, "label": "x"},
                {"id": "b", "status": "pending", "host_pref": "unraid", "model": "qwen3:8b", "label": "y"},
                {"id": "c", "status": "pending", "host_pref": "auto", "model": SMALL, "label": "z"},
                {"id": "d", "status": "done", "host_pref": "auto", "model": GEM, "label": "w"}]
        check("only Darkbloom-lane jobs' models are gated; Unraid/Ollama + finished jobs untouched",
              q.bloom_noswitch_models(jobs) == [BIG, SMALL], q.bloom_noswitch_models(jobs))
        p.set(resident=[GEM, SMALL], idle={GEM: 300, SMALL: 300}, usable=16.9)
        q._BLOOM_NS["last"] = 0.0
        r = q.bloom_noswitch_sync({"jobs": jobs}, now=time.time(), ctl=bc, background=False)
        check("daemon-tick sync warmed the 35B once (the provider evicted Gemma) and not the resident 9B",
              r == "sync" and p.chats == [BIG] and BIG in p.resident, (r, p.chats))
        check("sync is throttled per tick", q.bloom_noswitch_sync({"jobs": jobs}, now=time.time(), ctl=bc,
                                                                  background=False) is None)
        qsrc = Path(q.__file__).read_text()
        check("the daemon tick calls bloom_noswitch_sync right after bloom_queue_sync (wired once)",
              qsrc.count("            bloom_queue_sync(state, _pk, _now)\n            bloom_noswitch_sync(state, _now)\n") == 1)
        os.environ["BLOOM_NOSWITCH"] = "0"
        q._BLOOM_NS["last"] = 0.0
        p.chats.clear()
        check("BLOOM_NOSWITCH=0: the sync does nothing", q.bloom_noswitch_sync(
            {"jobs": jobs}, now=time.time() + 100, ctl=bc, background=False) is None and p.chats == [])
        os.environ.pop("BLOOM_NOSWITCH")
    finally:
        p.close()


# ----------------------------------------------------------------------------------------- pause/resume
@scenario("resume")
def t_resume(bc, q, wk, tc):
    print("--- capacity pause: uncharged, backoff, resume when resident")
    cause = q.CAPACITY_WAIT_CAUSE + ": " + BIG + " " + BIG + " blocked by " + GEM
    check("worker and queue agree on the pause-cause prefix", wk.CAPACITY_WAIT_CAUSE == q.CAPACITY_WAIT_CAUSE)

    def job(**kw):
        j = {"id": "j1", "label": "l", "status": "paused", "pause_reason": "chat_request_failed",
             "host_pref": "studio-db", "model": BIG, "pause_meta": {"lane_cause": cause}}
        j.update(kw)
        return j
    d = q._lane_failure_decide(job(), 500, True, infeasible="35B blocked by gemma")
    check("gate still names a blocker -> skip (waiting for capacity)", d[0] == "skip" and "capacity" in d[1], d)
    d = q._lane_failure_decide(job(), 5, True)
    check("resumable but inside the backoff -> skip", d == ("skip", "capacity backoff"), d)
    d = q._lane_failure_decide(job(), 100, True)
    check("loadable + past the backoff -> resume", d[0] == "resume", d)
    d = q._lane_failure_decide(job(lane_fail_resume_count=99), 100, True)
    check("resume budget already spent (>= max) does NOT stop a capacity wait (never charged)", d[0] == "resume", d)
    check("backoff grows with each capacity pause and is capped",
          q.capacity_resume_backoff_s(0) == 30 and q.capacity_resume_backoff_s(2) == 120
          and q.capacity_resume_backoff_s(40) == q.CAPACITY_RESUME_MAX_S)
    d = q._lane_failure_decide(job(capacity_wait_n=3), 100, True)
    check("n=3 backoff (240 s) not elapsed at 100 s", d[0] == "skip", d)
    j = job(lane_fail_resume_count=2)
    import tempfile as _t
    lp = Path(_t.mkdtemp()) / "r.log"
    changed = q._lane_failure_apply(j, ("resume", "x"), log_path=lp)
    check("apply: pending again, lane_fail_resume_count NOT incremented, capacity_wait_n is only a backoff counter",
          changed and j["status"] == "pending" and j["lane_fail_resume_count"] == 2 and j["capacity_wait_n"] == 1, j)
    j = job()
    q._lane_failure_apply(j, ("resume", "x"), log_path=lp)
    check("also from a fresh row: still 0 charged", j.get("lane_fail_resume_count", 0) == 0, j)
    check("the lane-failure watchdog owns a capacity pause even past the resume budget (stranded watchdog stays off)",
          q._lane_failure_owns(job(lane_fail_resume_count=99)) is True)
    check("a genuine lane failure past the budget is NOT owned (unchanged)",
          q._lane_failure_owns({"pause_reason": "chat_request_failed", "host_pref": "studio-db",
                                "lane_fail_resume_count": 99, "pause_meta": {"lane_cause": "darkbloom lane down"}}) is False)
    lw = q.bloom_lane_work([job()])
    check("a capacity-paused row counts as lane work (bloom hold is not released under it)",
          lw and lw[0][1] == "capacity-waiting", lw)
    check("a plain chat_request_failed pause is still not lane work",
          q.bloom_lane_work([job(pause_meta={"lane_cause": "darkbloom lane down"})]) == [])
    # the existing infra pause (cold infeasible) keeps its semantics
    d = q._lane_failure_decide(job(pause_meta={"lane_cause": q.COLD_INFEASIBLE_CAUSE + ": x"}), 100, True, infeasible="needs 30 GB")
    check("cold-infeasible pause unchanged (skip while infeasible)", d[0] == "skip" and "cannot load" in d[1], d)


# ----------------------------------------------------------------------------------------- escalation
@scenario("escalate")
def t_escalate(bc, q, wk, tc):
    print("--- dashboard escalation after QUEUE_BLOOM_WAIT_WARN_S")
    p = Prov()
    try:
        p.env()
        q._BLOOM_CTL_MOD[:] = [bc]
        q._darkbloom_url = lambda: "http://127.0.0.1:1"
        q._darkbloom_model = lambda m: m
        q.darkbloom_cold_gate = lambda *a, **k: None      # hermetic: never consult the LIVE `darkbloom status` memory arithmetic
        p.set(resident=[GEM, SMALL], idle={GEM: 1, SMALL: 1}, usable=16.9)
        job = {"id": "ja", "label": "costco-x", "status": "pending", "host_pref": "studio-db", "model": BIG}
        r = q.unsat_capacity_reason(job)
        check("blocked job -> 'qwen 35B blocked by Gemma serving' reason",
              bool(r) and r.startswith("darkbloom capacity wait: " + BIG + " blocked by " + GEM), r)
        check("Unraid job untouched", q.unsat_capacity_reason(dict(job, host_pref="unraid", model="qwen3:8b")) is None)
        check("running job untouched", q.unsat_capacity_reason(dict(job, status="running")) is None)
        check("a resident model's job untouched", q.unsat_capacity_reason(dict(job, model=SMALL)) is None)
        check("human-held job untouched", q.unsat_capacity_reason(dict(job, user_hold=True)) is None)
        pj = dict(job, status="paused", pause_reason="chat_request_failed",
                  pause_meta={"lane_cause": q.CAPACITY_WAIT_CAUSE + ": x"})
        check("a capacity-paused job is covered too", q.unsat_capacity_reason(pj) is not None)
        # end to end through the banner path with a real (temp) state file + ledger
        sp = Path(tempfile.mkdtemp()) / "state.json"
        sp.write_text(json.dumps({"jobs": [job]}))
        seen = Path(tempfile.mkdtemp()) / "seen.json"
        t0 = 2_000_000_000.0
        w0 = q.unsat_warnings_from_state(now=t0, state_path=sp, seen_path=seen)
        w1 = q.unsat_warnings_from_state(now=t0 + 300, state_path=sp, seen_path=seen)
        check("not yet: under QUEUE_BLOOM_WAIT_WARN_S (600) nothing is shown", w0 == [] and w1 == [], (w0, w1))
        w2 = q.unsat_warnings_from_state(now=t0 + 590, state_path=sp, seen_path=seen)
        check("599 s: still silent", w2 == [], w2)
        w3 = q.unsat_warnings_from_state(now=t0 + 610, state_path=sp, seen_path=seen)
        check("> 600 s: warning names the job, model, blocker and carries kind=capacity",
              len(w3) == 1 and w3[0]["kind"] == "capacity" and GEM in w3[0]["reason"] and w3[0]["id"] == "ja", w3)
        check("the job is NOT failed/cancelled/rewritten by the warning",
              json.loads(sp.read_text())["jobs"][0]["status"] == "pending")
        p.set(resident=[BIG, SMALL], idle={BIG: 1, SMALL: 1}, usable=5.0)
        w4 = q.unsat_warnings_from_state(now=t0 + 620, state_path=sp, seen_path=seen)
        check("cleared as soon as the model is resident", w4 == [], w4)
        # env override of the threshold
        os.environ["QUEUE_BLOOM_WAIT_WARN_S"] = "5"
        p.set(resident=[GEM, SMALL], idle={GEM: 1, SMALL: 1}, usable=16.9)
        seen2 = Path(tempfile.mkdtemp()) / "seen2.json"
        q.unsat_warnings_from_state(now=t0 + 1000, state_path=sp, seen_path=seen2)
        w5 = q.unsat_warnings_from_state(now=t0 + 1010, state_path=sp, seen_path=seen2)
        check("QUEUE_BLOOM_WAIT_WARN_S is honoured", len(w5) == 1, w5)
        os.environ.pop("QUEUE_BLOOM_WAIT_WARN_S")
        # an existing infeasible warning is unaffected (still threshold 600 via the original path)
        uw = q.unsat_track({}, {"x": {"reason": "r", "label": "l", "model": "m"}}, 100.0, warn_s=10)
        check("unsat_track without a per-entry threshold behaves as before", uw[1] == [] and
              q.unsat_track(uw[0], {"x": {"reason": "r"}}, 111.0, warn_s=10)[1][0]["age_s"] == 11.0)
        # the dashboard banner (node) renders the capacity kind distinctly
        apisrc = Path(os.environ.get("NSW_API_SRC") or (HERE / "ollama-queue-api.py")).resolve()
        html = apisrc.read_text() if apisrc.exists() else ""
        import re
        import subprocess

        def grab(name):
            m = re.search(r"^(?:async )?function " + name + r"\(.*?\n}\n", html, re.S | re.M)
            return m.group(0) if m else None
        fns = [grab("escapeHtml"), grab("fmtDur"), grab("unsatBannerLines")]
        check("dashboard page has unsatBannerLines", all(fns))
        if all(fns):
            js = "\n".join(fns) + "\nconst W=%s;\nconsole.log(JSON.stringify(unsatBannerLines({unsat:W})));" % json.dumps(
                [dict(w3[0], age_s=700), {"id": "z", "label": "old", "model": "m", "reason": "r", "age_s": 700}])
            r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
            lines = json.loads(r.stdout) if r.returncode == 0 else []
            check("banner: capacity entry says 'waiting for Darkbloom capacity' + names the blocker, old kind unchanged",
                  len(lines) == 2 and "waiting for Darkbloom capacity" in lines[0] and GEM in lines[0]
                  and ("no host can serve" in lines[1] or "cannot be served by any host" in lines[1])
                  and "no host can serve" not in lines[0] and "cannot be served" not in lines[0], lines)
    finally:
        p.close()


# ----------------------------------------------------------------------------------------- worker
@scenario("worker")
def t_worker(bc, q, wk, tc):
    print("--- worker: capacity refusal is a named, uncharged wait")
    host = "http://127.0.0.1:8000"
    detail = "HTTP Error 429 -- body: Provider capacity is temporarily unavailable."
    slept = []
    on_gone = lambda m: (True, False, "%s blocked by %s (serving)" % (BIG, GEM))
    on_resident = lambda m: (True, True, None)
    off = lambda m: (False, True, None)
    nof = lambda m: None
    st = {}
    n = 0
    raised = None
    try:
        while wk._slot_busy_retry(host, detail, st, sleep=slept.append, model=BIG, infeasible=nof, noswitch=on_gone):
            n += 1
            if n > 500:
                break
    except wk.DarkbloomCapacityWait as e:
        raised = e
    check("no-switch + model not resident: short inline grace, then DarkbloomCapacityWait",
          raised is not None and sum(slept) <= wk.NOSWITCH_INLINE_WAIT_S + 60, (n, sum(slept)))
    check("the pause cause carries the prefix and the named blocker",
          bool(raised) and str(raised).startswith(wk.CAPACITY_WAIT_CAUSE) and GEM in str(raised), raised)
    check("lane cause recorded for pause_meta", wk._LANE_LAST_CAUSE[0] == str(raised), wk._LANE_LAST_CAUSE)
    check("it is a RuntimeError subclass (handled by the resumable-pause path, like cold-infeasible)",
          isinstance(raised, wk.DarkbloomColdLoadInfeasible) and isinstance(raised, RuntimeError))
    # resident model, genuine busy slot: still waits the full cap, then PAUSES uncharged (was: charged)
    slept.clear()
    st = {}
    raised = None
    try:
        while wk._slot_busy_retry(host, "model slot(s) are active; cannot load 'x'", st, sleep=slept.append,
                                  model=SMALL, infeasible=nof, noswitch=on_resident, max_wait=300):
            pass
    except wk.DarkbloomCapacityWait as e:
        raised = e
    check("resident model busy: waits to the cap, then an uncharged capacity pause (not 'return False')",
          raised is not None and sum(slept) >= 295, (sum(slept), raised))
    # legacy / kill switch: unchanged (returns False after the cap; no exception)
    slept.clear()
    st = {}
    res = True
    while res:
        res = wk._slot_busy_retry(host, detail, st, sleep=slept.append, model=BIG, infeasible=nof, noswitch=off, max_wait=300)
    check("BLOOM_NOSWITCH=0 / legacy: bounded wait then False (today's behaviour), no exception",
          sum(slept) >= 295 and "slots busy" in str(wk._LANE_LAST_CAUSE[0]), (sum(slept), wk._LANE_LAST_CAUSE))
    # the cold-infeasible fast path is untouched and still wins
    st = {}
    try:
        wk._slot_busy_retry(host, detail, st, sleep=slept.append, model=BIG, infeasible=lambda m: "needs 30.3 GB",
                            noswitch=on_gone)
        got = None
    except wk.DarkbloomColdLoadInfeasible as e:
        got = e
    check("memgate fast path still first (cold infeasible)", got is not None and not isinstance(got, wk.DarkbloomCapacityWait)
          and str(got).startswith(wk.COLD_INFEASIBLE_CAUSE), got)
    # through _lane_restart_retry (the call sites' entry point), seams threaded
    st = {}
    try:
        wk._lane_restart_retry(host, 429, detail, st, sleep=slept.append, model=BIG, infeasible=nof, noswitch=on_gone)
        for _ in range(100):
            wk._lane_restart_retry(host, 429, detail, st, sleep=slept.append, model=BIG, infeasible=nof, noswitch=on_gone)
        got = None
    except wk.DarkbloomCapacityWait as e:
        got = e
    check("_lane_restart_retry threads the capacity wait", got is not None, got)
    check("a non-capacity 4xx is not a capacity wait", wk._lane_restart_retry(
        host, 400, "bad request", {}, sleep=slept.append, model=BIG, noswitch=on_gone) is False)
    # _noswitch_info is fail-safe: any doubt = old behaviour
    check("_noswitch_info never raises and degrades to the old behaviour",
          wk._noswitch_info(None)[0] in (True, False))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(HERE / "ollama-queue.py"))
    ap.add_argument("--bc", default=str(HERE / "bloom_control.py"))
    ap.add_argument("--worker", default=str(HERE / "ollama-worker.py"))
    ap.add_argument("--tc", default=str(HERE / "test-bloom-control.py"))
    ap.add_argument("--only", action="append", default=[])
    a = ap.parse_args()
    os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
    os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
    bc = load(a.bc, "bloom_control_ns")
    q = load(a.queue, "oq_ns")
    q._BLOOM_CTL_MOD[:] = [bc]
    wk = load(a.worker, "ow_ns")
    tc = load(a.tc, "tbc_ns")
    # the worker loads bloom_control.py from ITS dir for _noswitch_info; tests inject seams instead
    for fn in SEL:
        if a.only and fn._name not in a.only:
            continue
        print("--- " + fn._name)
        try:
            fn(bc, q, wk, tc)
        except Exception as e:
            import traceback
            traceback.print_exc()
            check(fn._name + " ran without exception", False, repr(e))
    if FAILS:
        print("\nFAILED: %d\n  " % len(FAILS) + "\n  ".join(FAILS))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
