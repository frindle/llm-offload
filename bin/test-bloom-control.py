#!/usr/bin/env python3
"""Hermetic tests for bloom_control.py and the queue-side hooks (ollama-queue.py BLOOM CONTROL
block, focus_drop_parked, fit_hold release).

NEVER touches the real queue daemon, Darkbloom, BloomGauge or provider.toml: a fake BloomGauge
HTTP server (control API with controlVersion), a fake Darkbloom HTTP stub, a fake `darkbloom` CLI
shim first on PATH, a temp HOME/state.

Usage:  test-bloom-control.py [--queue PATH] [--bc PATH] [--only NAME]
Marker: ALL PASS. Exit 0 = all checks pass. The pipeline-canary seam runs this with --queue/--bc
pointing at the sandbox copy; a revert of the feature must go red."""
import re
import argparse
import importlib.util
import inspect
import json
import os
import shutil
import stat
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
PAIR = ["Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8"]
GEMMA = ["gemma-4-26b-qat-4bit"]
KEY = "SECRETKEYSECRETKEYSECRETKEYSECRETKEY123"
FAILS = []
SEL = []
# queue names bloom_control retirement S1 (197b069) removed (see main)
RETIRED_QUEUE_API = frozenset((
    "_BLOOM_CTL_MOD", "_BLOOM_LOG_HEARTBEAT_S", "_BLOOM_LOG_SEEN", "_BLOOM_NS", "_BLOOM_SYNC",
    "_BLOOM_WARMUP", "bloom_control_hold", "bloom_ctl_mod", "bloom_idle_hold", "bloom_infeasible_note",
    "bloom_next_drain_s", "bloom_noswitch_models", "bloom_noswitch_sync", "bloom_queue_sync",
    "bloom_warmup_running_models", "bloom_warmup_sync"))


def check(name, cond, extra=""):
    print(("ok  : " if cond else "FAIL: ") + name + ("" if cond else "  -- " + str(extra)))
    if not cond:
        FAILS.append(name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


class World:
    def __init__(self, bc_path, serving=GEMMA, mode="on", pinned=False, shape="ok", auto_update=True,
                 start_rc=0, refuse_manual=False, bloom_up=True, slots=2, gauge="1", wu_n="0"):
        self.d = Path(tempfile.mkdtemp(prefix="bctl-"))
        self.home = self.d / "dispatch"
        self.home.mkdir()
        self.dbdir = self.d / "db"
        self.dbdir.mkdir()
        self.bin = self.d / "bin"
        self.bin.mkdir()
        self.posts = []
        self.chats = []          # (model, bloom-control phase at the time) per warm request
        self.chat_codes = {}     # model -> scripted HTTP codes, consumed in order
        self.chat_default = 200
        self.toks = []           # (model, max_tokens) per chat request
        self.adv_extra = []          # advertised in /v1/models but NOT resident (loaded-models.json)
        self.load_on_ok = set()      # a 200 warm request loads this model (the provider loads on request)
        self.health_ok = True        # /health answers 200 (False: 503, the provider is still hashing/preloading)
        self.refuse_until_restart = False   # every warm request answers 429 until the first `darkbloom start`
        self.sv = {"mode": mode, "pinned": pinned, "shape": shape, "version": 1,
                   "refuse_manual": refuse_manual, "gets": 0,
                   "current": "x"}   # "live": currentModel = what the fake provider serves (BloomGauge selection key)
        self.toml = self.d / "provider.toml"
        self.toml.write_text(
            "[backend]\nenabled_models = [ '%s' ]\nidle_timeout_mins = 0\nmax_model_slots = %d\n"
            "preload_models = [ 'qwen3.6-35b-a3b-vl-mtp-mxfp8', 'qwen3.5-9b' ]\n\n"
            "[provider]\nauto_update = %s\n" % ("', '".join(serving), slots, "true" if auto_update else "false"))
        self.loaded = self.d / "loaded-models.json"
        self.loaded.write_text(json.dumps({"models": serving}))
        (self.dbdir / "start_rc").write_text(str(start_rc))
        self.start_delay = self.d / "start_delay"
        w = self

        class Bloom(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if not w.bloom_up:
                    return self._send(503, {})
                w.sv["gets"] += 1
                if self.path != "/api/optimizer/control":
                    return self._send(404, {})
                if w.sv["shape"] == "drift":
                    return self._send(200, {"version": 9, "state": "auto"})
                cur = w.sv["current"]
                if cur == "live":
                    ms = json.loads(w.loaded.read_text()).get("models", [])
                    cur = ms[0] if len(ms) == 1 else ("@combo:" + json.dumps(ms, separators=(",", ":")) if len(ms) == 2 else None)
                self._send(200, {"controlVersion": "v%d" % w.sv["version"], "providerVersion": "p1",
                                 "providerRunning": True, "currentModel": cur,
                                 "automatic": {"mode": w.sv["mode"], "canEnable": True},
                                 "manager": {"pinned": w.sv["pinned"]}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                if self.headers.get("X-Bloom-Action") != "optimizer":
                    return self._send(403, {"error": "bad header"})
                if body.get("expectedControl") != "v%d" % w.sv["version"]:
                    return self._send(409, {"error": "The control status changed. Refresh it."})
                a = body.get("action")
                w.posts.append((a, body.get("enabled")))
                if a == "set-automatic" and body.get("enabled") is False and w.sv["refuse_manual"]:
                    return self._send(409, {"error": "The saved plan changed."})
                if a == "set-automatic":
                    w.sv["mode"] = "on" if body.get("enabled") else "manual"
                elif a == "release-pin":
                    w.sv["pinned"] = False
                w.sv["version"] += 1
                self._send(200, {"controlVersion": "v%d" % w.sv["version"]})

        self.bloom_up = bloom_up
        self.bsrv = ThreadingHTTPServer(("127.0.0.1", 0), Bloom)
        threading.Thread(target=self.bsrv.serve_forever, daemon=True).start()

        class DB(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                ms = json.loads(w.loaded.read_text()).get("models", [])
                if self.path == "/health":
                    if not w.health_ok:
                        self.send_response(503)
                        self.send_header("Content-Length", "0")
                        self.end_headers()
                        return
                    b = json.dumps({"status": "ok"}).encode()
                else:
                    b = json.dumps({"data": [{"id": m} for m in ms + w.adv_extra]}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(n) or b"{}")
                m = body.get("model")
                w.chats.append((m, w.state().get("phase")))
                w.toks.append((m, body.get("max_tokens")))
                codes = w.chat_codes.get(m)
                code = codes.pop(0) if codes else (codes_default(w, m))
                if w.refuse_until_restart and not w.starts():
                    code = 429
                if code == 200 and m in w.load_on_ok:
                    cur = json.loads(w.loaded.read_text()).get("models", [])
                    if m not in cur:
                        w.loaded.write_text(json.dumps({"models": cur + [m]}))
                b = json.dumps({"error": "x"} if code != 200 else {"choices": []}).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

        self.dsrv = ThreadingHTTPServer(("127.0.0.1", 0), DB)
        threading.Thread(target=self.dsrv.serve_forever, daemon=True).start()
        (self.d / "local.json").write_text(json.dumps(
            {"api_key": KEY, "base_url": "http://127.0.0.1:%d/v1" % self.dsrv.server_address[1]}))
        shim = self.bin / "darkbloom"
        shim.write_text(
            "#!/usr/bin/env python3\n"
            "import sys, json, os, re, time, pathlib\n"
            "d = pathlib.Path(%r); toml = pathlib.Path(%r); loaded = pathlib.Path(%r)\n"
            "open(d/'calls.log','a').write(' '.join(sys.argv[1:]) + '\\n')\n"
            "cmd = sys.argv[1] if len(sys.argv) > 1 else ''\n"
            "if cmd == 'autoupdate':\n"
            "    v = 'true' if sys.argv[2] == 'enable' else 'false'\n"
            "    t = toml.read_text()\n"
            "    toml.write_text(re.sub(r'(?m)^auto_update = .*$', 'auto_update = ' + v, t))\n"
            "    sys.exit(0)\n"
            "if cmd == 'status':\n"
            "    sp = d/'status_text'\n"
            "    sys.stdout.write(sp.read_text() if sp.exists() else '')\n"
            "    sys.exit(0)\n"
            "if cmd in ('start', 'switch'):\n"
            "    if cmd == 'switch' and (d/'switch_sets_status').exists(): (d/'status_text').write_text((d/'switch_sets_status').read_text())\n"
            "    if cmd == 'switch' and (d/'switch_rc').exists() and int((d/'switch_rc').read_text()): print((d/'switch_out').read_text() if (d/'switch_out').exists() else 'switch refused'); sys.exit(int((d/'switch_rc').read_text()))\n"
            "    dl = pathlib.Path(%r)\n"
            "    if dl.exists(): time.sleep(float(dl.read_text()))\n"
            "    rc = int((d/'start_rc').read_text())\n"
            "    if rc: print('boom token ' + %r); sys.exit(rc)\n"
            "    ms = [sys.argv[i+1] for i,a in enumerate(sys.argv) if a == '--model']\n"
            "    t = toml.read_text()\n"
            "    toml.write_text(re.sub(r'(?m)^enabled_models = .*$', 'enabled_models = [ ' + ', '.join(repr(m) for m in ms) + ' ]', t))\n"
            "    nf = d/'nofit'\n"
            "    if nf.exists() and len(ms) > 1: ms = [m for m in ms if m not in nf.read_text().split()]\n"
            "    loaded.write_text(json.dumps({'models': ms}))\n"
            "sys.exit(0)\n" % (str(self.dbdir), str(self.toml), str(self.loaded), str(self.start_delay), KEY))
        shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
        self.env = {
            "BLOOMCTL_HOME": str(self.home), "BLOOMCTL_BLOOM_URL": "http://127.0.0.1:%d" % self.bsrv.server_address[1],
            "BLOOMCTL_PROVIDER_TOML": str(self.toml), "BLOOMCTL_LOCAL_JSON": str(self.d / "local.json"),
            "BLOOMCTL_LOADED_JSON": str(self.loaded), "BLOOMCTL_POLL_S": "0.1", "BLOOMCTL_WAIT_S": "20",
            "BLOOMCTL_DRAIN_S": "5", "BLOOMCTL_PAIR": ",".join(PAIR), "BLOOM_GAUGE": gauge,
            "BLOOMCTL_DAEMON_JSON": str(self.d / "daemon-state.json"), "BLOOMCTL_PURGE_CMD": "/usr/bin/false",
            "BLOOMCTL_PROVIDER_PID": str(self.d / "no-provider.pid"),   # hermetic: never read the live provider / run a real purge
            "BLOOM_IDLE_GEMMA": "0",   # legacy scenarios stay exact; session-aware scenarios opt in
            "BLOOMCTL_GAUGE_HANDOFF": "0",   # legacy scenarios pin the pre-2026-10-29 path; gauge-* scenarios opt in
            "BLOOM_WARMUP_N": wu_n, "BLOOM_WARMUP_SPACE_S": "0",   # warm-ups off by default so older scenarios stay exact
            "PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
        }
        for k in ("BLOOMCTL_DARKBLOOM_BIN", "BLOOM_CONTROL", "BLOOM_GAUGE", "BLOOMCTL_DRY_RUN", "BLOOMCTL_STATE"):
            os.environ.pop(k, None)
        os.environ.update(self.env)

    def calls(self):
        p = self.dbdir / "calls.log"
        return p.read_text().splitlines() if p.exists() else []

    def starts(self):
        return [c for c in self.calls() if c.startswith("start")]

    def state(self):
        try:
            return json.loads((self.home / "bloom-control-state.json").read_text())
        except OSError:
            return {}

    def close(self):
        self.bsrv.shutdown()
        self.dsrv.shutdown()
        shutil.rmtree(self.d, ignore_errors=True)


def codes_default(w, m):
    return w.chat_default


def scenario(name):
    def deco(fn):
        fn._name = name
        SEL.append(fn)
        return fn
    return deco


@scenario("hold")
def t_hold(bc, q):
    w = World(bc.__file__)
    try:
        r = bc.hold_for_queue()
        check("hold ok", r["ok"] and not r["noop"], r)
        check("bloom set Manual", w.sv["mode"] == "manual" and ("set-automatic", False) in w.posts, w.posts)
        st = w.starts()
        check("one darkbloom start with BOTH pair models + idle-timeout 0 + local-endpoint + drain timeout",
              len(st) == 1 and all(("--model " + m) in st[0] for m in PAIR) and "--idle-timeout 0" in st[0]
              and "--local-endpoint" in st[0] and "--timeout 5" in st[0], st)
        txt = w.toml.read_text()
        check("toml: preload case fixed + slots 2 + idle 0",
              "'Qwen3.5-9B'" in txt and "qwen3.5-9b" not in txt and "max_model_slots = 2" in txt
              and "idle_timeout_mins = 0" in txt, txt)
        check("auto-update disabled and remembered", "auto_update = false" in txt and w.state().get("autoupdate_was_on"))
        check("state phase ready/queue", w.state().get("phase") == "ready" and w.state().get("mode") == "queue", w.state())
        check("no secret in log", KEY not in (w.home / "bloom-control.log").read_text())
    finally:
        w.close()



BIG = PAIR[1]
SMALL = PAIR[0]
FULL3 = [BIG, SMALL] + GEMMA


def _daemon_state(w, usable, residents=()):
    """A fresh fake daemon-state.json (what the real provider publishes ~1/s)."""
    p = w.d / "daemon-state.json"
    p.write_text(json.dumps({
        "written_at": time.time(), "advertised_models": FULL3,
        "capacity": {"load_usable_gb": usable, "load_headroom_gb": 6.5},
        "autopilot_state": {"max_model_slots": 2, "resident_models": [
            {"model_id": m, "idle_seconds": 1, "resident_gb": g} for m, g in residents]}}))
    os.environ["BLOOMCTL_DAEMON_JSON"] = str(p)
    return p


def _purge_script(w, new_usable, rc=0):
    """A fake purge: counts its runs; on rc 0 publishes `new_usable` in the fake daemon state."""
    sc = w.bin / "fakepurge"
    sc.write_text("#!/usr/bin/env python3\nimport json,time,sys\nopen(%r,'a').write('x')\n"
                  "if %d == 0:\n    p=%r\n    d=json.load(open(p)); d['capacity']['load_usable_gb']=%r; d['written_at']=time.time()\n"
                  "    json.dump(d, open(p,'w'))\nsys.exit(%d)\n" % (
                      str(w.d / "purge.count"), rc, str(w.d / "daemon-state.json"), new_usable, rc))
    sc.chmod(0o755)
    os.environ["BLOOMCTL_PURGE_CMD"] = str(sc)
    return lambda: len((w.d / "purge.count").read_text()) if (w.d / "purge.count").exists() else 0


@scenario("pr-noneed")
def t_pr_noneed(bc, q):
    # the 35B is already resident (Gemma absent / irrelevant): ZERO restarts, provider.toml untouched
    w = World(bc.__file__, serving=[BIG, SMALL], mode="manual", auto_update=False, gauge="0")
    try:
        before = w.toml.read_text()
        r = bc.hold_for_queue()
        check("35B resident: hold ok", r["ok"], r)
        check("35B resident: zero darkbloom start", w.starts() == [], w.calls())
        check("35B resident: provider.toml untouched", w.toml.read_text() == before)
        check("35B resident: gemma_removed not recorded", not w.state().get("gemma_removed"), w.state())
        r2 = bc.release_to_bloom()
        check("release after a no-restart hold is a no-op (no start, no toml edit)",
              r2["ok"] and w.starts() == [] and w.toml.read_text() == before, (r2, w.calls()))
        w2 = World(bc.__file__, serving=[BIG, SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0")
        try:
            bc.hold_for_queue()
            check("35B resident next to Gemma: Gemma is NOT removed (zero starts)", w2.starts() == [], w2.calls())
            bc.release_to_bloom()
            check("... and release does nothing", w2.starts() == [], w2.calls())
        finally:
            w2.close()
    finally:
        w.close()


@scenario("pr-warm1")
def t_pr_warm1(bc, q):
    # 35B advertised, not resident; the first warm request succeeds (provider evicts/loads itself): ZERO restarts
    w = World(bc.__file__, serving=[SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0")
    try:
        w.adv_extra, w.load_on_ok = [BIG], {BIG}
        before = w.toml.read_text()
        r = bc.hold_for_queue()
        check("first warm succeeds: hold ok", r["ok"], r)
        check("first warm succeeds: zero darkbloom start", w.starts() == [], w.calls())
        check("first warm succeeds: toml untouched", w.toml.read_text() == before)
        check("the 35B was asked for (never Gemma)", any(c[0] == BIG for c in w.chats)
              and not any(c[0] in GEMMA for c in w.chats), w.chats)
        check("release after it: no-op", bc.release_to_bloom()["ok"] and w.starts() == [], w.calls())
    finally:
        w.close()


@scenario("pr-restart")
def t_pr_restart(bc, q):
    # 429s past BLOOM_PAIR_RESTART_AFTER_S -> EXACTLY ONE restart without Gemma; release restores Gemma
    os.environ["BLOOM_PAIR_RESTART_AFTER_S"] = "1"
    w = World(bc.__file__, serving=[SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0")
    try:
        w.adv_extra = [BIG]
        w.refuse_until_restart = True
        t0 = time.monotonic()
        r = bc.hold_for_queue()
        el = time.monotonic() - t0
        check("429s past the threshold: hold ok after the restart", r["ok"], r)
        st = w.starts()
        check("exactly one restart", len(st) == 1, w.calls())
        check("restart serves the pair only (no Gemma), with plist args",
              len(st) == 1 and all(("--model " + m) in st[0] for m in PAIR) and GEMMA[0] not in st[0]
              and "--local-endpoint" in st[0] and "--port 8000" in st[0] and "--bind 127.0.0.1" in st[0]
              and "--autopilot" not in st[0] and "--timeout 5" in st[0], st)
        check("provider.toml enabled_models is the pair (backup taken)",
              GEMMA[0] not in w.toml.read_text().split("preload_models")[0]
              and list(w.toml.parent.glob("provider.toml.bak-bloomctl-*")), w.toml.read_text())
        check("the bounded no-switch attempt really tried first (>=2 warm requests to the 35B)",
              len([c for c in w.chats if c[0] == BIG]) >= 2 and el >= 1.0, (w.chats, el))
        check("gemma_removed recorded", w.state().get("gemma_removed") is True, w.state())
        r2 = bc.hold_for_queue()
        check("a second hold does not restart again", len(w.starts()) == 1, w.calls())
        rel = bc.release_to_bloom()
        st = w.starts()
        check("release restores Gemma with exactly one more start (35B first)",
              rel["ok"] and len(st) == 2 and GEMMA[0] in st[1] and st[1].index(BIG) < st[1].index(GEMMA[0]), (rel, st))
        check("toml enabled_models has Gemma again", GEMMA[0] in w.toml.read_text().split("preload_models")[0])
        check("gemma_removed cleared", w.state().get("gemma_removed") is False, w.state())
        bc.release_to_bloom()
        check("second release is a no-op", len(w.starts()) == 2, w.calls())
        os.environ["BLOOM_PAIR_RESTART"] = "0"
        w2 = World(bc.__file__, serving=[SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0")
        try:
            w2.adv_extra = [BIG]
            b2 = w2.toml.read_text()
            check("BLOOM_PAIR_RESTART=0 (kill switch): NO-SWITCH again", bc.noswitch_mode() and not bc.pair_restart_mode())
            r3 = bc.hold_for_queue()
            check("kill switch: hold never restarts or edits the toml", w2.starts() == [] and w2.toml.read_text() == b2, (r3, w2.calls()))
        finally:
            w2.close()
    finally:
        os.environ.pop("BLOOM_PAIR_RESTART", None)
        os.environ.pop("BLOOM_PAIR_RESTART_AFTER_S", None)
        w.close()


@scenario("pr-restore-retry")
def t_pr_restore_retry(bc, q):
    os.environ["BLOOM_PAIR_RESTART_AFTER_S"] = "0.5"
    os.environ["BLOOMCTL_RESTORE_BACKOFF_BASE_S"] = "60"
    w = World(bc.__file__, serving=[SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0")
    try:
        w.adv_extra = [BIG]
        w.refuse_until_restart = True
        bc.hold_for_queue()
        n = len(w.starts())
        (w.dbdir / "start_rc").write_text("1")
        rel = bc.release_to_bloom()
        st = w.state()
        check("failed restore: release not ok, state degraded + release-pending + loud last_error",
              not rel["ok"] and st.get("degraded") is True and st.get("phase") == "release-pending"
              and "restore" in str(st.get("last_error")), (rel, st))
        check("failed restore: Gemma still flagged removed (never silent)", st.get("gemma_removed") is True, st)
        check("failed restore logged WARN", "WARN  RESTORE FAILED" in (w.home / "bloom-control.log").read_text())
        n2 = len(w.starts())
        bc.release_to_bloom()
        check("retry inside the backoff does not hammer the CLI", len(w.starts()) == n2 == n + 1, w.calls())
        (w.dbdir / "start_rc").write_text("0")
        bc.write_state(bc.load_cfg(), restore_next_at=0)
        rel = bc.release_to_bloom()
        check("after the backoff the restore is retried and succeeds", rel["ok"] and len(w.starts()) == n2 + 1
              and GEMMA[0] in w.starts()[-1] and w.state().get("gemma_removed") is False, (rel, w.calls()))
        check("backoff doubles on repeated failure", bc.load_cfg()["restore_backoff_max_s"] >= 60)
    finally:
        os.environ.pop("BLOOM_PAIR_RESTART_AFTER_S", None)
        os.environ.pop("BLOOMCTL_RESTORE_BACKOFF_BASE_S", None)
        w.close()


@scenario("pr-drain")
def t_pr_drain(bc, q):
    # in-flight drain timeout: `darkbloom start --timeout` leaves the service draining and exits non-zero
    os.environ["BLOOM_PAIR_RESTART_AFTER_S"] = "0.5"
    w = World(bc.__file__, serving=[SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0", start_rc=124)
    try:
        w.adv_extra = [BIG]
        w.refuse_until_restart = True
        r = bc.hold_for_queue()
        st = w.state()
        check("drain timeout: hold not ok, phase failed (queue retries; jobs stay in infra-wait)",
              not r["ok"] and st.get("phase") == "failed", (r, st))
        check("drain timeout: the start carried --timeout <drain>", "--timeout 5" in (w.starts() or [""])[0], w.calls())
        (w.dbdir / "start_rc").write_text("0")
        r2 = bc.hold_for_queue()
        check("the next hold retries and succeeds", r2["ok"], r2)
        check("Gemma flagged removed so release restores it", w.state().get("gemma_removed") is True)
    finally:
        os.environ.pop("BLOOM_PAIR_RESTART_AFTER_S", None)
        w.close()


@scenario("pr-memory")
def t_pr_memory(bc, q):
    w = World(bc.__file__, serving=[SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0")
    try:
        os.environ["BLOOMCTL_PURGE_SETTLE_S"] = "3"
        _daemon_state(w, 22.0, [(SMALL, 5.7)])
        n = _purge_script(w, 31.0)
        cfg = bc.load_cfg()
        ok, why, info = bc.ensure_memory(cfg, BIG)
        check("purge lifts usable over the need: proceeds", ok and why is None and n() == 1 and info.get("purged"), (ok, why, info, n()))
        pr = bc.read_state(cfg).get("purge") or {}
        check("purge result recorded with before/after GB", pr.get("before") == 22.0 and pr.get("after") == 31.0 and pr.get("rc") == 0, pr)
        check("purge logged with before/after", "PURGE: ran" in (w.home / "bloom-control.log").read_text()
              and "22.0 -> 31.0" in (w.home / "bloom-control.log").read_text())
        # rate limit: memory drops again, a second purge inside purge_min_s must NOT run
        _daemon_state(w, 22.0, [(SMALL, 5.7)])
        ok, why, info = bc.ensure_memory(cfg, BIG)
        check("second purge inside the rate limit is skipped", not ok and n() == 1 and "rate-limited" in why, (ok, why, n()))
        check("reason is explicit", why.startswith("waiting on memory: need 30.3 GB, have 22.0 GB usable (8.3 GB short)"), why)
        # unavailable (no sudoers rule): one attempt, explicit reason, never a loop
        w3 = World(bc.__file__, serving=[SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0")
        try:
            _daemon_state(w3, 22.0, [(SMALL, 5.7)])
            n3 = _purge_script(w3, 31.0, rc=1)
            cfg3 = bc.load_cfg()
            outs = [bc.ensure_memory(cfg3, BIG) for _ in range(4)]
            check("purge unavailable: not ok with a clear reason", not outs[0][0] and "purge unavailable" in outs[0][1]
                  and "sudoers" in outs[0][1] and outs[0][1].startswith("waiting on memory:"), outs[0])
            check("purge unavailable: attempted once, no loop", n3() == 1, n3())
            check("BLOOM_PURGE=0 never runs a purge",
                  (lambda: (os.environ.__setitem__("BLOOM_PURGE", "0"), bc.read_state(cfg3), True)[2])() and
                  (bc.write_state(cfg3, purge=None), bc.ensure_memory(cfg3, BIG)[0] is False and n3() == 1)[1])
            os.environ.pop("BLOOM_PURGE", None)
            # hold: still short even without the non-needed models -> INFRA WAIT reason, ZERO restarts, no failure
            w3.adv_extra = [BIG]
            _daemon_state(w3, 10.0, [(SMALL, 5.7)])
            bc.write_state(cfg3, purge=None)
            r = bc.hold_for_queue()
            sst = w3.state()
            check("infeasible even after dropping Gemma: hold waits (not ok), zero restarts",
                  not r["ok"] and w3.starts() == [] and "waiting on memory" in str(r.get("why")), (r, w3.calls()))
            inf = sst.get("infeasible") or {}
            check("the reason reaches the queue (bloom_infeasible_note -> INFRA WAIT text)",
                  "waiting on memory" in str(q.bloom_infeasible_note(BIG, state_path=str(w3.home / "bloom-control-state.json"))), sst)
        finally:
            os.environ.pop("BLOOM_PURGE", None)
            w3.close()
        # restart path measures memory AFTER the restart too (Gemma's memory is free by then)
        w4 = World(bc.__file__, serving=[SMALL] + GEMMA, mode="manual", auto_update=False, gauge="0")
        try:
            os.environ["BLOOM_PAIR_RESTART_AFTER_S"] = "0.5"
            w4.adv_extra = [BIG]
            w4.refuse_until_restart = True
            _daemon_state(w4, 25.0, [(SMALL, 5.7), (GEMMA[0], 17.4)])
            n4 = _purge_script(w4, 25.0)         # purge does not help; Gemma freeing (restart) is the way
            (w4.dbdir / "nofit").write_text(BIG)  # the 35B is not resident right after the restart (still loading)
            os.environ["BLOOMCTL_WAIT_S"] = "2"
            r = bc.hold_for_queue()
            check("short only because of Gemma: restarts once without it (no infeasible wait before)",
                  len(w4.starts()) == 1 and GEMMA[0] not in w4.starts()[0], (r, w4.calls()))
            check("purge at most once in the whole hold", n4() == 1, n4())
            check("memory is measured again after the restart (Gemma's memory is free by then)",
                  "after the restart waiting on memory" in (w4.home / "bloom-control.log").read_text(),
                  (w4.home / "bloom-control.log").read_text()[-600:])
        finally:
            os.environ.pop("BLOOM_PAIR_RESTART_AFTER_S", None)
            os.environ["BLOOMCTL_WAIT_S"] = "20"
            w4.close()
    finally:
        os.environ["BLOOMCTL_DAEMON_JSON"] = str(w.d / "no-daemon-state.json")
        os.environ["BLOOMCTL_PURGE_CMD"] = "/usr/bin/false"
        os.environ.pop("BLOOMCTL_PURGE_SETTLE_S", None)
        w.close()



@scenario("pr-unload")
def t_pr_unload(bc, q):
    # 2026-10-22: the 35B needs 30.3 GB, 20 GB usable, the always-ready 9B (5.7 GB) resident and the provider
    # refuses to evict it. Idle (0 unfinished) -> unload it via ONE graceful start on the 35B; busy -> never.
    st0 = "Model switch: serving; %d unfinished request(s)\n"
    check("parser: 0 unfinished", bc.unfinished_requests(run=lambda: "x\n" + st0 % 0) == 0)
    check("parser: 3 unfinished", bc.unfinished_requests(run=lambda: st0 % 3 + "Warm models: a\n") == 3)
    check("parser: no line -> None", bc.unfinished_requests(run=lambda: "Daemon: not running\n") is None)
    os.environ["BLOOMCTL_UNLOAD_WAIT_S"] = "0.5"
    os.environ["BLOOMCTL_UNLOAD_QUIET_S"] = "0"
    os.environ["BLOOMCTL_WAIT_S"] = "5"
    w = World(bc.__file__, serving=[SMALL], mode="manual", auto_update=False, gauge="0")
    try:
        w.adv_extra = [BIG]
        _daemon_state(w, 20.0, [(SMALL, 5.7)])
        os.environ["BLOOMCTL_PAIR"] = BIG                       # production: the queue's pair is the 35B alone
        (w.dbdir / "status_text").write_text(st0 % 2)          # busy: a request on the 9B is unfinished
        r = bc.hold_for_queue()
        check("busy resident: hold waits (not ok) with the unfinished reason", not r["ok"]
              and "unfinished" in str(r.get("why")), r)
        check("busy resident: ZERO starts, the 9B stays loaded", w.starts() == []
              and SMALL in json.loads(w.loaded.read_text())["models"], (w.calls(), w.loaded.read_text()))
        check("busy resident: no restart_min_s stamp (retry is not blocked)", not w.state().get("last_start_at"), w.state())
        (w.dbdir / "status_text").write_text(st0 % 0)          # now idle
        _daemon_state(w, 20.0, [(SMALL, 5.7)])
        r = bc.hold_for_queue()
        st = w.starts()
        check("idle resident: hold ok after unloading it", r["ok"], r)
        check("idle resident: exactly one graceful start on the 35B only (no --force, 9B dropped)",
              len(st) == 1 and ("--model " + BIG) in st[0] and SMALL not in st[0] and "--force" not in st[0], w.calls())
        check("idle resident: the 35B is loaded, the 9B is not", json.loads(w.loaded.read_text())["models"] == [BIG],
              w.loaded.read_text())
        check("unload logged", "UNLOAD: 0 unfinished requests; unloading idle " + SMALL in
              (w.home / "bloom-control.log").read_text())
        check("idle policy untouched (--idle-timeout 0 = keep loaded, as before)",
              "idle_timeout_mins = 0" in w.toml.read_text() and "--idle-timeout 0" in st[0], st)
    finally:
        os.environ["BLOOMCTL_DAEMON_JSON"] = str(w.d / "no-daemon-state.json")
        os.environ.pop("BLOOMCTL_UNLOAD_WAIT_S", None)
        os.environ.pop("BLOOMCTL_UNLOAD_QUIET_S", None)
        os.environ["BLOOMCTL_WAIT_S"] = "20"
        os.environ["BLOOMCTL_PAIR"] = ",".join(PAIR)
        w.close()
    # a start that fails (drain incomplete) must not arm restart_min_s against the retry
    w2 = World(bc.__file__, serving=[SMALL], mode="manual", auto_update=False, gauge="0", start_rc=64)
    try:
        w2.adv_extra = [BIG]
        _daemon_state(w2, 20.0, [(SMALL, 5.7)])
        r = bc.hold_for_queue()
        check("failed start: not ok, last_start_at not left armed", not r["ok"] and len(w2.starts()) == 1
              and not w2.state().get("last_start_at"), (r, w2.state()))
    finally:
        os.environ["BLOOMCTL_DAEMON_JSON"] = str(w2.d / "no-daemon-state.json")
        w2.close()


@scenario("start-inprogress")
def t_start_inprogress(bc, q):
    # 2026-10-13 16:35: a hand-started provider was still hashing/preloading (/health not ok) when the queue's
    # hold issued a SECOND `darkbloom start`, killing the first. A young provider process => WAIT, never start.
    import subprocess
    os.environ["BLOOMCTL_START_WAIT_S"] = "2"
    w = World(bc.__file__, serving=[SMALL], mode="manual", auto_update=False, gauge="0")
    sp = subprocess.Popen(["sleep", "60"])
    try:
        pidf = w.d / "provider.pid"
        pidf.write_text(str(sp.pid))
        os.environ["BLOOMCTL_PROVIDER_PID"] = str(pidf)
        w.health_ok = False
        before = w.toml.read_text()
        busy, why = bc.provider_start_in_progress(bc.load_cfg())
        check("young provider process + /health not ok = start in progress", busy, why)
        r = bc.hold_for_queue()
        check("start in progress: hold waits, NOT ok, zero darkbloom start", not r["ok"] and w.starts() == [], (r, w.calls()))
        check("start in progress: provider.toml untouched", w.toml.read_text() == before)
        check("start in progress: WARN-free explicit reason", "start in progress" in str(r.get("why")), r)
        # it finishes while we wait: hold proceeds with ZERO starts
        def finish():
            time.sleep(0.6)
            w.loaded.write_text(json.dumps({"models": PAIR}))
            w.health_ok = True
        os.environ["BLOOMCTL_START_WAIT_S"] = "10"
        threading.Thread(target=finish, daemon=True).start()
        r = bc.hold_for_queue()
        check("start finishes during the wait: hold ok with ZERO starts", r["ok"] and w.starts() == [], (r, w.calls()))
        bc.release_to_bloom()
    finally:
        sp.kill()
        sp.wait()
        os.environ.pop("BLOOMCTL_START_WAIT_S", None)
        os.environ["BLOOMCTL_PROVIDER_PID"] = str(w.d / "no-provider.pid")
        w.close()
    # genuinely dead provider (pid gone): one start
    w = World(bc.__file__, serving=[SMALL], mode="manual", auto_update=False, gauge="0")
    try:
        dead = subprocess.Popen(["true"])
        dead.wait()
        pidf = w.d / "provider.pid"
        pidf.write_text(str(dead.pid))
        os.environ["BLOOMCTL_PROVIDER_PID"] = str(pidf)
        r = bc.hold_for_queue()
        check("dead provider (health ok but pair missing / no process): exactly one start", r["ok"] and len(w.starts()) == 1, (r, w.calls()))
    finally:
        os.environ["BLOOMCTL_PROVIDER_PID"] = str(w.d / "no-provider.pid")
        w.close()
    # process older than the grace window and still unhealthy = hung, not starting: restart allowed
    w = World(bc.__file__, serving=[SMALL], mode="manual", auto_update=False, gauge="0")
    sp = subprocess.Popen(["sleep", "60"])
    try:
        os.environ["BLOOMCTL_START_GRACE_S"] = "0.5"
        pidf = w.d / "provider.pid"
        pidf.write_text(str(sp.pid))
        os.environ["BLOOMCTL_PROVIDER_PID"] = str(pidf)
        time.sleep(2.2)
        w.health_ok = False
        threading.Thread(target=lambda: (time.sleep(1.5), setattr(w, "health_ok", True)), daemon=True).start()
        busy, why = bc.provider_start_in_progress(bc.load_cfg())
        check("provider older than the grace window is not 'starting'", not busy, why)
        r = bc.hold_for_queue()
        check("after the grace window a genuinely stuck provider IS started (one start)", len(w.starts()) == 1, (r, w.calls()))
    finally:
        sp.kill()
        sp.wait()
        os.environ.pop("BLOOMCTL_START_GRACE_S", None)
        os.environ["BLOOMCTL_PROVIDER_PID"] = str(w.d / "no-provider.pid")
        w.close()


@scenario("warm")
def t_warm(bc, q):
    # first request to the 35B answers 429 "capacity unavailable" twice (memory freeing): the hold
    # must retry it BEFORE reporting ready, so the first queue job never sees the 429
    w = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False)
    try:
        big = PAIR[1]
        w.chat_codes[big] = [429, 429]
        r = bc.hold_for_queue()
        check("hold ok after retrying 429s", r["ok"] and not r["noop"], r)
        n_big = [c for c in w.chats if c[0] == big]
        check("35B was asked 3 times (2x429 then 200)", len(n_big) == 3, w.chats)
        check("every pair model got a warm request", {c[0] for c in w.chats} == set(PAIR), w.chats)
        check("warm requests happened while phase was still switching (launches wait)",
              all(c[1] == "switching" for c in w.chats), w.chats)
        check("35B (primary) is warmed first", w.chats[0][0] == big, w.chats)
        st = w.state()
        check("ready only after warm; warmed recorded", st.get("phase") == "ready"
              and st.get("warmed", {}).get(big) == "ok", st)
        n = len(w.chats)
        r2 = bc.hold_for_queue()
        check("no-op re-hold sends no further warm request", r2["noop"] and len(w.chats) == n, w.chats)
    finally:
        w.close()
    # capacity never frees: the hold still completes (bounded), flagged timeout, never wedges the queue
    os.environ["BLOOMCTL_WARM_S"] = "0.5"
    w = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False)
    try:
        w.chat_default = 429
        t0 = time.time()
        r = bc.hold_for_queue()
        check("perpetual 429: hold ok (bounded)", r["ok"] and time.time() - t0 < 15, (r, time.time() - t0))
        check("perpetual 429: recorded as timeout", w.state().get("warmed", {}).get(PAIR[1]) == "timeout", w.state())
    finally:
        w.close()
    os.environ.pop("BLOOMCTL_WARM_S", None)
    # an endpoint that cannot be asked (404) never blocks or fails the hold
    w = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False)
    try:
        w.chat_default = 404
        r = bc.hold_for_queue()
        check("unaskable endpoint: hold ok, warmed=unknown", r["ok"] and w.state().get("warmed", {}).get(PAIR[0]) == "unknown", (r, w.state()))
        check("unaskable endpoint asked once per model", len(w.chats) == 2, w.chats)
    finally:
        w.close()


@scenario("superset")
def t_superset(bc, q):
    # `darkbloom start --all`: extra models served beside the pair must not trigger a toml rewrite or a restart
    w = World(bc.__file__, serving=PAIR + GEMMA, mode="manual", auto_update=False)
    try:
        before = w.toml.read_text()
        r = bc.hold_for_queue()
        check("superset: hold ok", r["ok"], r)
        check("superset: no darkbloom start", w.starts() == [], w.calls())
        check("superset: provider.toml untouched", w.toml.read_text() == before, w.toml.read_text())
        check("superset: pair warmed", {c[0] for c in w.chats} == set(PAIR), w.chats)
        check("superset: phase ready", w.state().get("phase") == "ready", w.state())
    finally:
        w.close()
    # pair model missing from /v1/models (extras only) -> still starts the pair
    w = World(bc.__file__, serving=[PAIR[0]] + GEMMA, mode="manual", auto_update=False)
    try:
        r = bc.hold_for_queue()
        check("missing pair model: darkbloom start issued", len(w.starts()) == 1, w.calls())
    finally:
        w.close()


@scenario("idempotent")
def t_idem(bc, q):
    w = World(bc.__file__)
    try:
        bc.hold_for_queue()
        n_posts, n_calls = len(w.posts), len(w.calls())
        r = bc.hold_for_queue()
        check("second hold is a no-op", r["ok"] and r["noop"], r)
        check("no new POST / CLI call", len(w.posts) == n_posts and len(w.calls()) == n_calls, (w.posts, w.calls()))
        # pair already served from the start + Manual already -> no darkbloom start at all
        w2 = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False)
        try:
            r2 = bc.hold_for_queue()
            check("pair served + Manual: no start, no POST", r2["ok"] and not w2.starts() and not w2.posts, (r2, w2.calls(), w2.posts))
        finally:
            w2.close()
    finally:
        w.close()


@scenario("release")
def t_release(bc, q):
    w = World(bc.__file__)
    try:
        bc.hold_for_queue()
        w.sv["pinned"] = True
        r = bc.release_to_bloom()
        check("release ok", r["ok"] and not r["noop"], r)
        check("release-pin then set-automatic true", [p[0] for p in w.posts][-2:] == ["release-pin", "set-automatic"]
              and w.sv["mode"] == "on" and not w.sv["pinned"], w.posts)
        check("auto-update re-enabled", "auto_update = true" in w.toml.read_text() and "autoupdate enable" in " ".join(w.calls()))
        check("state back to bloom", w.state().get("mode") == "bloom", w.state())
        n = len(w.posts)
        r2 = bc.release_to_bloom()
        check("second release is a no-op", r2["noop"] and len(w.posts) == n, (r2, w.posts))
    finally:
        w.close()


@scenario("release-retry")
def t_release_retry(bc, q):
    w = World(bc.__file__)
    try:
        bc.hold_for_queue()
        w.bloom_up = False
        r = bc.release_to_bloom()
        check("release with Bloom down is NOT ok and stays pending", not r["ok"] and w.state().get("phase") == "release-pending", (r, w.state()))
        w.bloom_up = True
        r2 = bc.release_to_bloom()
        check("retry succeeds", r2["ok"] and w.sv["mode"] == "on", (r2, w.sv))
    finally:
        w.close()


@scenario("api-drift")
def t_drift(bc, q):
    w = World(bc.__file__, shape="drift")
    try:
        r = bc.hold_for_queue()
        check("API shape drift: degraded but the pair still served via darkbloom start",
              r["ok"] and r.get("degraded") and len(w.starts()) == 1 and not w.posts, (r, w.posts, w.calls()))
        check("state flags degraded", w.state().get("degraded") is True, w.state())
    finally:
        w.close()
    w = World(bc.__file__, refuse_manual=True)
    try:
        r = bc.hold_for_queue()
        check("set-automatic refused -> degraded, start still runs", r["ok"] and r.get("degraded") and len(w.starts()) == 1, r)
    finally:
        w.close()
    w = World(bc.__file__, bloom_up=False)
    try:
        r = bc.hold_for_queue()
        check("BloomGauge down -> degraded hold works", r["ok"] and r.get("degraded") and len(w.starts()) == 1, r)
    finally:
        w.close()


@scenario("warn-once")
def t_warn_once(bc, q):
    """Drift-guard WARN de-noising (smoke 2026-10-09): a persistent DEGRADED condition logs ONCE
    per state change plus a heartbeat at most every 10 min; the guard's behaviour (noop verify,
    degraded flag) is unchanged."""
    bc.clear_log_seen()
    w = World(bc.__file__, bloom_up=False)
    try:
        def n_warn():
            t = (w.home / "bloom-control.log").read_text() if (w.home / "bloom-control.log").exists() else ""
            return t.count("BloomGauge unusable")
        r1 = bc.hold_for_queue()
        r2 = bc.hold_for_queue()      # the ~67s verify: already held, still degraded
        r3 = bc.hold_for_queue()
        r4 = bc.hold_for_queue()
        check("verify path behaviour unchanged (noop, degraded, ok)",
              r1["ok"] and r2["ok"] and r2.get("noop") and r2.get("degraded") and r4.get("noop"), (r1, r2, r4))
        check("4 degraded checks in a row log the WARN ONCE", n_warn() == 1, n_warn())
        cfg = bc.load_cfg()
        t0 = time.time()
        bc.clear_log_seen("hb")
        check("log_on_change: first occurrence logs", bc.log_on_change(cfg, "hb", "a", "x", now=t0) is True)
        check("...same signature inside the heartbeat window is silent",
              bc.log_on_change(cfg, "hb", "a", "x", now=t0 + 540) is False)
        check("...heartbeat after 10 min", bc.log_on_change(cfg, "hb", "a", "x", now=t0 + 601) is True)
        check("...a CHANGED signature logs immediately",
              bc.log_on_change(cfg, "hb", "b", "x2", now=t0 + 602) is True)
        bc.clear_log_seen("hb")
        check("...a cleared condition logs again", bc.log_on_change(cfg, "hb", "b", "x2", now=t0 + 603) is True)
    finally:
        w.close()


@scenario("concurrent")
def t_conc(bc, q):
    w = World(bc.__file__)
    try:
        w.start_delay.write_text("1.0")
        out = []
        ths = [threading.Thread(target=lambda: out.append(bc.hold_for_queue())) for _ in range(5)]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        check("5 concurrent holds -> exactly ONE darkbloom start", len(w.starts()) == 1, w.calls())
        check("all callers ok", all(r["ok"] for r in out) and sum(1 for r in out if r["noop"]) == 4, out)
        check("exactly one Manual POST", [p for p in w.posts if p[0] == "set-automatic"] == [("set-automatic", False)], w.posts)
    finally:
        w.close()


@scenario("start-fail")
def t_fail(bc, q):
    w = World(bc.__file__, start_rc=3)
    try:
        r = bc.hold_for_queue()
        check("darkbloom start failure -> not ok, phase failed", not r["ok"] and w.state().get("phase") == "failed", (r, w.state()))
        check("secret from CLI output scrubbed from the log", KEY not in (w.home / "bloom-control.log").read_text())
    finally:
        w.close()


@scenario("disabled")
def t_disabled(bc, q):
    w = World(bc.__file__)
    try:
        (w.home / "bloom-control.disabled").write_text("")
        r = bc.hold_for_queue()
        r2 = bc.release_to_bloom()
        check("kill-switch file: both no-op, nothing touched", r.get("disabled") and r2.get("disabled") and not w.calls() and not w.posts, (r, w.calls()))
        (w.home / "bloom-control.disabled").unlink()
        os.environ["BLOOM_CONTROL"] = "0"
        r = bc.hold_for_queue()
        check("BLOOM_CONTROL=0: no-op", r.get("disabled") and not w.calls(), r)
        check("queue sync inert when disabled", q.bloom_queue_sync({"jobs": [{"id": "a", "status": "pending"}]}, lambda j: "b", ctl=bc, background=False) is None)
        os.environ.pop("BLOOM_CONTROL")
    finally:
        w.close()


@scenario("baseurl")
def t_baseurl(bc, q):
    """Darkbloom rewrites local.json WITHOUT base_url on restart: bloom_control must fall back to the
    DARKBLOOM_BASE_URL override (like the rest of the pipeline), not guess port 8000."""
    w = World(bc.__file__)
    try:
        cfg = bc.load_cfg()
        cfg["local_json"].write_text(json.dumps({"api_key": "k"}))
        os.environ["DARKBLOOM_BASE_URL"] = "http://127.0.0.1:55555/v1"
        base, key = bc.db_base_key(cfg)
        check("no base_url in local.json -> DARKBLOOM_BASE_URL (/v1 stripped)", base == "http://127.0.0.1:55555" and key == "k", (base, key))
        os.environ.pop("DARKBLOOM_BASE_URL")
        base, _ = bc.db_base_key(cfg)
        check("no override either -> default port 8000", base == "http://127.0.0.1:8000", base)
    finally:
        os.environ.pop("DARKBLOOM_BASE_URL", None)
        w.close()


@scenario("dry-run")
def t_dry(bc, q):
    w = World(bc.__file__)
    try:
        os.environ["BLOOMCTL_DRY_RUN"] = "1"
        r = bc.hold_for_queue()
        os.environ.pop("BLOOMCTL_DRY_RUN")
        check("dry-run hold changes nothing", r["ok"] and not w.posts and not w.calls()
              and "qwen3.5-9b" in w.toml.read_text() and w.sv["mode"] == "on", (r, w.posts, w.calls()))
    finally:
        w.close()


def job(i, st, **kw):
    d = {"id": "%012d" % i, "status": st, "host_pref": "auto"}
    d.update(kw)
    return d


@scenario("hold-gate")
def t_holdgate(bc, q):
    w = World(bc.__file__)
    try:
        check("no hold when state absent", q.bloom_control_hold() is None)
        bc.write_state(bc.load_cfg(), mode="queue", phase="switching")
        check("switching + fresh heartbeat -> launches WAIT (infra-wait)", bool(q.bloom_control_hold()))
        check("bloom_idle_hold surfaces it", bool(q.bloom_idle_hold()))
        check("darkbloom_model_ready False while switching",
              q.darkbloom_model_ready("Qwen3.5-9B", time.time(), probe=lambda m: True, cache={}) is False)
        check("stale heartbeat never freezes the queue", q.bloom_control_hold(now=time.time() + 4000) is None)
        bc.write_state(bc.load_cfg(), phase="ready")
        check("ready -> no hold", q.bloom_control_hold() is None)
    finally:
        w.close()


class FakeCtl:
    """Records what the queue asks of bloom_control; state file drives the decisions."""

    def __init__(self, bc):
        self.bc = bc
        self.calls = []
        self.fail = False

    def enabled(self, cfg=None):
        return True

    def load_cfg(self):
        return self.bc.load_cfg()

    def read_state(self, cfg):
        return self.bc.read_state(cfg)

    def write_state(self, cfg, **kw):
        self.bc.write_state(cfg, **kw)

    def hold_for_queue(self, cfg=None):
        self.calls.append("hold")
        self.phase_at_call = self.bc.read_state(cfg).get("phase")
        if self.fail:
            return {"ok": False, "why": "boom"}
        self.bc.write_state(cfg, mode="queue", phase="ready")
        return {"ok": True, "why": "held"}

    def release_to_bloom(self, cfg=None):
        self.calls.append("release")
        self.bc.write_state(cfg, mode="bloom", phase="idle", autoupdate_was_on=False)
        return {"ok": True, "why": "released"}


@scenario("queue-sync")
def t_sync(bc, q):
    w = World(bc.__file__)
    try:
        pk = lambda j: j.get("bundle") or j["id"]
        ctl = FakeCtl(bc)
        q._BLOOM_SYNC.update(thread=None, last_fail=0.0, last_verify=0.0)
        idle = {"jobs": []}
        check("idle + already with Bloom: nothing happens (no timer, no pin)", q.bloom_queue_sync(idle, pk, ctl=ctl, background=False) is None and not ctl.calls)
        busy = {"jobs": [job(1, "pending")]}
        k = q.bloom_queue_sync(busy, pk, ctl=ctl, background=False)
        check("empty -> non-empty: hold fires once", k == "hold" and ctl.calls == ["hold"], (k, ctl.calls))
        check("switching marker written synchronously BEFORE the hold runs (same-tick launches wait)", getattr(ctl, "phase_at_call", None) == "switching", getattr(ctl, "phase_at_call", None))
        q.bloom_queue_sync(busy, pk, ctl=ctl, background=False)
        check("steady busy: no second hold", ctl.calls == ["hold"], ctl.calls)
        # pending behind a USER-HELD dependency is not work
        held = {"jobs": [job(2, "held", user_hold=True), job(3, "pending", after="000000000002")]}
        check("pending behind a held dependency is not lane work", q.bloom_lane_work(held["jobs"]) == [], q.bloom_lane_work(held["jobs"]))
        done = {"jobs": [job(1, "done")]}
        k = q.bloom_queue_sync(done, pk, ctl=ctl, background=False)
        check("queue done: release fires immediately", k == "release" and ctl.calls == ["hold", "release"], (k, ctl.calls))
        k = q.bloom_queue_sync(done, pk, ctl=ctl, background=False)
        check("after release: idempotent, nothing more", k is None and ctl.calls == ["hold", "release"], (k, ctl.calls))
        # failure backoff
        ctl.fail = True
        bc.write_state(bc.load_cfg(), mode="bloom", phase="idle")
        q.bloom_queue_sync(busy, pk, ctl=ctl, background=False)
        n = len(ctl.calls)
        k = q.bloom_queue_sync(busy, pk, ctl=ctl, background=False)
        check("failed hold: backs off, does not hammer", k is None and len(ctl.calls) == n, ctl.calls)
    finally:
        w.close()


@scenario("queue-done-signals")
def t_done(bc, q):
    pk = lambda j: j.get("bundle") or j["id"]
    td = Path(tempfile.mkdtemp(prefix="bctl-q-"))
    try:
        base = {"jobs": [job(1, "done", bundle="B")]}
        rem, why = q.bloom_queue_work_remaining(base, pk, runs_dir=td, chain_dir=td, hooks={})
        check("nothing live: queue done", rem is False, why)
        rem, why = q.bloom_queue_work_remaining({"jobs": [job(1, "pending", bundle="B")]}, pk, runs_dir=td, chain_dir=td, hooks={})
        check("pending row: work remains", rem is True, why)
        hooks = {"j1": {"bundle": "B", "at": time.time(), "proc": None}}
        rem, why = q.bloom_queue_work_remaining(base, pk, runs_dir=td, chain_dir=td, hooks=hooks)
        check("gate-on-complete hook in flight: work remains", rem is True, why)
        (td / "B.advance.lock").write_text(json.dumps({"pid": os.getpid()}))
        (td / "B.json").write_text(json.dumps({"label": "B", "order": ["s1"], "slices": {"s1": {"status": "pending"}}}))
        rem, why = q.bloom_queue_work_remaining(base, pk, runs_dir=td, chain_dir=td, hooks={})
        check("slicer advance in flight (live lock): work remains", rem is True, why)
        (td / "B.advance.lock").unlink()
        (td / "B.json").unlink()
        st = {"jobs": [job(1, "done", bundle="B")], "_bundle_parked": {"B": {"kind": "blocked"}}}
        rem, why = q.bloom_queue_work_remaining(st, pk, runs_dir=td, chain_dir=td, hooks={})
        check("a parked/blocked bundle with nothing runnable does not hold Darkbloom", rem is False, why)
        st = {"jobs": [job(1, "held", bundle="B", fit_hold=True)]}
        rem, why = q.bloom_queue_work_remaining(st, pk, runs_dir=td, chain_dir=td, hooks={})
        check("a fit-held row does not hold Darkbloom", rem is False, why)
        st = {"jobs": [job(1, "done", bundle="B", host_pref="studio-db"),
                       job(2, "running", bundle="B", host_pref="unraid"),
                       job(3, "pending", bundle="B", host_pref="unraid")]}
        rem, why = q.bloom_queue_work_remaining(st, pk, runs_dir=td, chain_dir=td, hooks={})
        check("a bundle with only Unraid rows left does not hold Darkbloom", rem is False, why)
        st["jobs"].append(job(4, "pending", bundle="B", host_pref="auto"))
        rem, why = q.bloom_queue_work_remaining(st, pk, runs_dir=td, chain_dir=td, hooks={})
        check("an auto row in that bundle still holds Darkbloom", rem is True, why)
        st = {"jobs": [job(1, "pending", bundle="B", host_pref="unraid", fit_rerouted_from="auto")]}
        rem, why = q.bloom_queue_work_remaining(st, pk, runs_dir=td, chain_dir=td, hooks={})
        check("a row fit-rerouted to Unraid off auto still holds Darkbloom", rem is True, why)
        st = {"jobs": [job(1, "pending", bundle="B", host_pref="unraid")], "_bundle_next": {"B": {"step": "s2"}}}
        rem, why = q.bloom_queue_work_remaining(st, pk, runs_dir=td, chain_dir=td, hooks={})
        check("an Unraid-only bundle with a next step queued still holds Darkbloom", rem is True, why)
    finally:
        shutil.rmtree(td, ignore_errors=True)


@scenario("parked-lanes")
def t_parked(bc, q):
    parked = {"P": {"kind": "blocked"}}
    check("parked sticky bundle does not keep the lanes", q.focus_drop_parked("P", parked, None, None, ["P", "N"]) == "N")
    check("parked + nothing else -> None", q.focus_drop_parked("P", parked, None, None, ["P"]) is None)
    check("running parked bundle keeps its lane", q.focus_drop_parked("P", parked, "P", None, ["N"]) == "P")
    check("human override on a parked bundle is honoured", q.focus_drop_parked("P", parked, None, "P", ["N"]) == "P")
    check("non-parked untouched", q.focus_drop_parked("N", parked, None, None, ["P"]) == "N")
    pk = lambda j: j.get("bundle")
    jobs = [job(1, "held", bundle="E", fit_hold=True), job(2, "pending", bundle="E")]
    st, why, _m = q.bundle_commit_status("E", [jobs[0]], pk, {}, {})
    check("bundle whose only live row is fit-held is BLOCKED (parks, releases lanes), not working", st == "blocked", (st, why))
    jobs2 = [job(1, "held", bundle="E", user_hold=True)]
    st, why, _m = q.bundle_commit_status("E", jobs2, pk, {}, {})
    check("user-held only -> blocked", st == "blocked", (st, why))


@scenario("gauge-off")
def t_gauge_off(bc, q):
    # BloomGauge is OPTIONAL (2026-10-13). Default OFF: no BloomGauge call of any kind, no degraded /
    # release-pending / retry loop, provider-side work unchanged.
    w = World(bc.__file__, serving=PAIR, mode="on", auto_update=True, gauge="0")
    try:
        cfg = bc.load_cfg()
        check("env BLOOM_GAUGE=0 -> off", bc.bloomgauge_on(cfg) is False)
        w.bloom_up = False          # the real situation: 8765 is gone (any call would be BloomDown)
        r = bc.hold_for_queue()
        check("off: hold ok, not degraded", r["ok"] and not r.get("degraded"), r)
        st = w.state()
        check("off: phase ready/queue, degraded False, no last_error, bloomgauge False",
              st.get("phase") == "ready" and st.get("mode") == "queue" and st.get("degraded") is False
              and not st.get("last_error") and st.get("bloomgauge") is False, st)
        check("off: BloomGauge never contacted (0 GETs, 0 POSTs)", w.sv["gets"] == 0 and not w.posts, (w.sv["gets"], w.posts))
        check("off: auto-update untouched (still on, nothing remembered)",
              "auto_update = true" in w.toml.read_text() and not st.get("autoupdate_was_on"), (w.toml.read_text(), st))
        check("off: pair served -> no darkbloom start", not w.starts(), w.calls())
        r2 = bc.hold_for_queue()
        check("off: re-hold is a noop why='bloomgauge off'", r2["ok"] and r2["noop"] and r2["why"] == "bloomgauge off", r2)
        r = bc.release_to_bloom()
        check("off: release ok/noop why='bloomgauge off'", r["ok"] and r["noop"] and r["why"] == "bloomgauge off", r)
        st = w.state()
        check("off: release clears the queue hold (mode bloom/idle, not release-pending)",
              st.get("mode") == "bloom" and st.get("phase") == "idle" and not st.get("degraded"), st)
        r = bc.release_to_bloom()
        check("off: second release is a pure noop (no retry loop)", r["ok"] and r["noop"], r)
        log = (w.home / "bloom-control.log").read_text()
        check("off: no WARN / 'unreachable' / 'incomplete' in the log", "WARN" not in log and "unreachable" not in log
              and "incomplete" not in log and "DEGRADED" not in log, log)
        gs = bc.get_state(cfg)
        check("off: get_state skips the control view", gs["bloomgauge"] is False and gs["bloom"] is None
              and gs["bloom_error"] is None, gs)
        check("off: the queue launch hold is clear", q.bloom_idle_hold(state_path=str(w.d / "none.json")) is None)
    finally:
        w.close()
    # stale state from the on-mode era (release-pending/degraded/unreachable) is cleaned by one release
    w = World(bc.__file__, serving=PAIR, gauge="0", bloom_up=False)
    try:
        bc.write_state(bc.load_cfg(), mode="queue", phase="release-pending", degraded=True, last_error="BloomGauge unreachable")
        r = bc.release_to_bloom()
        st = w.state()
        check("off: stale release-pending cleared in one call", r["ok"] and st.get("phase") == "idle"
              and st.get("degraded") is False and not st.get("last_error"), (r, st))
        bc.write_state(bc.load_cfg(), mode="queue", phase="ready", autoupdate_was_on=True)
        r = bc.release_to_bloom()
        check("off: a stale auto-update park is still restored", r["ok"] and "autoupdate enable" in " ".join(w.calls())
              and not w.state().get("autoupdate_was_on"), (r, w.calls(), w.state()))
    finally:
        w.close()
    # a pair that is NOT served still gets the restart-based hold with BloomGauge off (provider work is unchanged)
    w = World(bc.__file__, serving=GEMMA, gauge="0", bloom_up=False)
    try:
        r = bc.hold_for_queue()
        check("off: unserved pair -> darkbloom start still issued, hold ok, undegraded",
              r["ok"] and len(w.starts()) == 1 and not r.get("degraded") and w.sv["gets"] == 0, (r, w.calls()))
    finally:
        w.close()
    # switch resolution: default off; env wins; files
    w = World(bc.__file__, gauge="1")
    try:
        os.environ.pop("BLOOM_GAUGE", None)
        cfg = bc.load_cfg()
        check("default (no env, no file) is OFF", bc.bloomgauge_on(cfg) is False)
        cfg["gauge_on_file"].write_text("")
        check("bloomgauge.on file re-enables", bc.bloomgauge_on(cfg) is True)
        cfg["gauge_off_file"].write_text("")
        check("bloomgauge.off file beats .on", bc.bloomgauge_on(cfg) is False)
        os.environ["BLOOM_GAUGE"] = "1"
        check("env BLOOM_GAUGE=1 beats the off file", bc.bloomgauge_on(cfg) is True)
    finally:
        w.close()
    # on-mode parity (BLOOM_GAUGE=1): today's behaviour exactly, incl. degrade + release-pending when 8765 is down
    w = World(bc.__file__, gauge="1")
    try:
        r = bc.hold_for_queue()
        check("on: hold parks BloomGauge Manual", r["ok"] and w.sv["mode"] == "manual" and ("set-automatic", False) in w.posts, (r, w.posts))
        r = bc.release_to_bloom()
        check("on: release -> Automatic", r["ok"] and not r["noop"] and w.sv["mode"] == "on", (r, w.sv))
    finally:
        w.close()
    w = World(bc.__file__, gauge="1", bloom_up=False)
    try:
        r = bc.hold_for_queue()
        check("on + 8765 down: hold DEGRADED (unchanged)", r["ok"] and r.get("degraded"), r)
        r = bc.release_to_bloom()
        check("on + 8765 down: release-pending (unchanged)", not r["ok"] and w.state().get("phase") == "release-pending", (r, w.state()))
    finally:
        w.close()


def _wu_daemon(w, now, served, started_at=None):
    """Fresh fake daemon-state.json carrying stats.requests_served (+ optionally a recent started_at)."""
    p = w.d / "daemon-state.json"
    d = {"written_at": now, "advertised_models": PAIR, "stats": {"requests_served": served},
         "capacity": {"load_usable_gb": 30.0, "load_headroom_gb": 6.5},
         "autopilot_state": {"max_model_slots": 2, "resident_models": [
             {"model_id": m, "idle_seconds": 1, "resident_gb": 10} for m in PAIR]}}
    if started_at is not None:
        d["started_at"] = started_at
    p.write_text(json.dumps(d))
    os.environ["BLOOMCTL_DAEMON_JSON"] = str(p)


def _wu(w):
    return [m for m, t in w.toks if t == 64]


@scenario("warmup")
def t_warmup(bc, q):
    # (a) restart path: after hold_for_queue restarts the provider, N 64-token requests per resident model
    os.environ["BLOOM_WARMUP_N"] = "3"
    w = World(bc.__file__, serving=GEMMA, mode="manual", auto_update=False, gauge="0", wu_n="3")
    try:
        r = bc.hold_for_queue()
        wu = _wu(w)
        check("restart: hold ok and the provider was restarted", r["ok"] and len(w.starts()) == 1, (r, w.calls()))
        check("restart: 3 warm-ups sent to EACH resident model (64 tokens)",
              all(wu.count(m) == 3 for m in PAIR) and len(wu) == 6, wu)
        check("restart: warm-ups come after the readiness warm (phase ready)", w.chats and w.state().get("phase") == "ready")
        log = (w.home / "bloom-control.log").read_text()
        check("restart: every warm-up is logged, no secret", log.count("WARMUP (after provider restart)") == 6 and KEY not in log, log[-400:])
    finally:
        w.close()
    # (b) no restart (pair already served): no restart warm-up
    w = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False, gauge="0", wu_n="3")
    try:
        bc.hold_for_queue()
        check("no restart: no warm-up", _wu(w) == [] and not w.starts(), (w.toks, w.calls()))
    finally:
        w.close()
    # (c) N=0 kill switch: restart sends none; tick sends none
    os.environ["BLOOM_WARMUP_N"] = "0"
    w = World(bc.__file__, serving=GEMMA, mode="manual", auto_update=False, gauge="0", wu_n="0")
    try:
        bc.hold_for_queue()
        check("N=0: restart sends no warm-up", _wu(w) == [] and len(w.starts()) == 1, w.toks)
        T = time.time()
        _wu_daemon(w, T, 5)
        cfg = bc.load_cfg()
        bc.warmup_tick(cfg, now=T)
        _wu_daemon(w, T + 400, 5)
        res = bc.warmup_tick(cfg, now=T + 400)
        check("N=0: flat counter sends nothing", _wu(w) == [] and res is None, (res, w.toks))
    finally:
        w.close()
    # (d) flat counter -> ONE batch, rate limit holds, counter movement resets
    os.environ["BLOOM_WARMUP_N"] = "2"
    w = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False, gauge="0", wu_n="2")
    try:
        bc.hold_for_queue()
        cfg = bc.load_cfg()
        T = time.time()
        def tick(dt, served=5, busy=(), **kw):
            _wu_daemon(w, T + dt, served, **kw)
            return bc.warmup_tick(cfg, busy_models=busy, now=T + dt)
        check("flat: first sight only records the counter", tick(0) == "counter-moved" and _wu(w) == [])
        check("flat: flat for < 300 s sends nothing", tick(100) == "flat-not-long-enough" and _wu(w) == [])
        check("flat: counter moved -> clock restarts, nothing sent", tick(290, served=6) == "counter-moved" and _wu(w) == [])
        check("flat: still flat 299 s after the move -> nothing", tick(290 + 299, served=6) == "flat-not-long-enough" and _wu(w) == [])
        res = tick(290 + 301, served=6)
        check("flat >= 300 s: ONE batch, N=2 per resident model", res == "sent" and all(_wu(w).count(m) == 2 for m in PAIR)
              and len(_wu(w)) == 4, (res, w.toks))
        n1 = len(_wu(w))
        check("rate limit: flat again 301 s later (< 600 s since the batch) -> nothing",
              tick(290 + 301 + 301, served=6) in ("rate-limited", "flat-not-long-enough") and len(_wu(w)) == n1, w.toks)
        check("rate limit: still nothing at 599 s", tick(290 + 301 + 599, served=6) == "rate-limited" and len(_wu(w)) == n1)
        res = tick(290 + 301 + 905, served=6)
        check("after the min gap a second batch is sent", res == "sent" and len(_wu(w)) == 2 * n1, (res, w.toks))
        log = (w.home / "bloom-control.log").read_text()
        check("idle batches are logged, no secret", "WARMUP (idle)" in log and KEY not in log)
    finally:
        w.close()
    # (e) in flight on a model -> that model is skipped; all busy -> none
    w = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False, gauge="0", wu_n="2")
    try:
        bc.hold_for_queue()
        cfg = bc.load_cfg()
        T = time.time()
        def tick(dt, busy=()):
            _wu_daemon(w, T + dt, 7)
            return bc.warmup_tick(cfg, busy_models=busy, now=T + dt)
        tick(0)
        res = tick(400, busy=[x.upper() for x in PAIR])
        check("in flight on every resident model: nothing sent", res == "in-flight" and _wu(w) == [], (res, w.toks))
        res = tick(401, busy=[BIG])
        wu = _wu(w)
        check("in flight on the 35B only: only the other model is warmed", res == "sent" and set(wu) == {SMALL}, (res, w.toks))
    finally:
        w.close()
    # (f) start / switch in progress -> nothing (provider mid-start: /health not ok, process young)
    w = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False, gauge="0", wu_n="2")
    try:
        bc.hold_for_queue()
        cfg = bc.load_cfg()
        T = time.time()
        _wu_daemon(w, T, 7)
        bc.warmup_tick(cfg, now=T)
        w.health_ok = False
        _wu_daemon(w, T + 400, 7, started_at=T + 380)
        res = bc.warmup_tick(cfg, now=T + 400)
        check("start in progress: nothing sent", res == "start-in-progress" and _wu(w) == [], (res, w.toks))
        w.health_ok = True
        # a hold that is not holding (released) never warms
        bc.release_to_bloom()
        _wu_daemon(w, T + 800, 7)
        res = bc.warmup_tick(cfg, now=T + 800)
        check("not holding (released): nothing sent", res == "not-holding" and _wu(w) == [], (res, w.toks))
    finally:
        w.close()
    # (g) fail-soft: a raising sender / a refusing provider never raises out of the tick or the hold
    os.environ["BLOOM_WARMUP_N"] = "2"
    w = World(bc.__file__, serving=PAIR, mode="manual", auto_update=False, gauge="0", wu_n="2")
    try:
        bc.hold_for_queue()
        cfg = bc.load_cfg()
        T = time.time()
        def boom(c, m):
            raise RuntimeError("boom " + KEY)
        _wu_daemon(w, T, 3)
        bc.warmup_tick(cfg, now=T, sender=boom)
        _wu_daemon(w, T + 400, 3)
        try:
            res = bc.warmup_tick(cfg, now=T + 400, sender=boom)
            ok = True
        except Exception as e:
            res, ok = repr(e), False
        check("a raising sender never raises out of warmup_tick", ok, res)
        check("... and the secret in the error is scrubbed from the log", KEY not in (w.home / "bloom-control.log").read_text())
    finally:
        w.close()
    w = World(bc.__file__, serving=GEMMA, mode="manual", auto_update=False, gauge="0", wu_n="2")
    try:
        w.chat_default = 500
        r = bc.hold_for_queue()
        check("refusing provider (HTTP 500): restart warm-ups never fail the hold", r["ok"], r)
    finally:
        w.close()
    # (h) queue side: delegates to bloom_control with the models that have a RUNNING job
    os.environ["BLOOM_WARMUP_N"] = "3"
    class Ctl:
        calls = []
        def load_cfg(self):
            return {"x": 1}
        def warmup_tick(self, cfg, busy_models=()):
            Ctl.calls.append(list(busy_models))
    st = {"jobs": [{"id": "1", "status": "running", "host_pref": "auto", "model": BIG},
                   {"id": "2", "status": "pending", "host_pref": "auto", "model": SMALL}]}
    q._BLOOM_WARMUP["last"] = 0.0
    q._BLOOM_WARMUP["thread"] = None
    r = q.bloom_warmup_sync(st, now=1000.0, ctl=Ctl(), background=False)
    check("queue: warmup_tick gets only RUNNING Darkbloom models as busy", Ctl.calls == [[BIG]], Ctl.calls)
    r = q.bloom_warmup_sync(st, now=1005.0, ctl=Ctl(), background=False)
    check("queue: throttled (30 s)", r is None and len(Ctl.calls) == 1, (r, Ctl.calls))
    os.environ["BLOOM_WARMUP_N"] = "0"



@scenario("session-aware")
def sc_session_aware(bc, q):
    """s15: pair while Claude is active, +Gemma after 30 min idle; switch first, restart fallback; hysteresis."""
    os.environ["BLOOM_IDLE_GEMMA"] = "1"
    try:
        w = World(bc.__file__, serving=GEMMA, mode="on")
        os.environ["BLOOM_IDLE_GEMMA"] = "1"     # World() resets it to 0
        try:
            cfg = bc.load_cfg()
            T = 100000.0
            idle_s, gap, deb = cfg["idle_s"], cfg["switch_min_gap_s"], cfg["debounce_s"]
            check("cfg: idle 30 min, one change per 10 min", idle_s == 1800 and gap == 600, (idle_s, gap))
            D = lambda st, now, act, work=False, running=False: bc.session_decision(cfg, st, now, work, running, act)[0]
            idle_st = {}                     # Gemma served (not held)
            held = {"mode": "queue", "phase": "ready"}
            check("idle set + Claude active (sustained) -> pair", D({"act_first": T - 400}, T, T - 10) == "pair")
            check("hysteresis: one stray ping does not leave the idle set", D({"act_first": T - 10}, T, T - 5) is None)
            check("hysteresis: no burst recorded -> stays", D(idle_st, T, T - 5) is None)
            check("held pair + 30 min of quiet -> idle (Gemma back)", D(held, T, T - idle_s - 5) == "idle")
            check("held pair + 29 min of quiet -> stays", D(held, T, T - idle_s + 60) is None)
            check("no beacon at all counts as idle", D(held, T, None) == "idle")
            check("a job running: never change the set", D(dict(held), T, T - idle_s - 5, running=True) is None
                  and D({"act_first": T - 400}, T, T - 10, running=True) is None)
            check("rate limit: one change per 10 min", D(dict(held, last_switch_at=T - 100), T, T - idle_s - 5) is None
                  and D(dict(held, last_switch_at=T - gap - 1), T, T - idle_s - 5) == "idle")
            check("queue work -> pair regardless of rate limit", D(dict(idle_st, last_switch_at=T - 5), T, None, work=True) == "pair")
            os.environ["BLOOM_IDLE_GEMMA"] = "0"
            check("BLOOM_IDLE_GEMMA=0 -> legacy (no decision)", D(held, T, T - idle_s - 5) is None and not bc.idle_gemma_enabled())
            os.environ["BLOOM_IDLE_GEMMA"] = "1"
            # note_activity: burst start recorded, cleared when quiet
            now = time.time()
            st = bc.note_activity(cfg, bc.read_state(cfg), now - 500, now)
            check("note_activity records the burst start", st.get("act_first") == now - 500 and bc.read_state(cfg).get("act_first") == now - 500)
            st = bc.note_activity(cfg, st, now - idle_s - 5, now)
            check("note_activity clears it once quiet", not bc.read_state(cfg).get("act_first"))
            # switch first: hold uses `darkbloom switch`, NO start
            r = bc.hold_for_queue()
            check("hold: ok via switch", r["ok"], r)
            check("hold: switch used, no restart", any(c.startswith("switch") for c in w.calls()) and w.starts() == [], w.calls())
            # restore (release) with the switch refused -> restart fallback
            (w.dbdir / "switch_rc").write_text("1")
            r = bc.release_to_bloom()
            check("release: ok", r["ok"], r)
            check("release: switch refused -> falls back to exactly one start", len(w.starts()) == 1, w.calls())
        finally:
            w.close()
    finally:
        os.environ["BLOOM_IDLE_GEMMA"] = "0"


@scenario("session-aware-queue")
def sc_session_queue(bc, q):
    """bloom_queue_sync: after the queue empties, a held pair is kept while Claude is active and released after idle."""
    os.environ["BLOOM_IDLE_GEMMA"] = "1"
    try:
        w = World(bc.__file__, serving=PAIR, mode="on")
        os.environ["BLOOM_IDLE_GEMMA"] = "1"
        try:
            cfg = bc.load_cfg()
            now = time.time()
            class Ctl:
                calls = []
                def __getattr__(self, n): return getattr(bc, n)
                def release_to_bloom(self, c): Ctl.calls.append("release"); return {"ok": True}
                def hold_for_queue(self, c): Ctl.calls.append("hold"); return {"ok": True}
            ctl = Ctl()
            pk = lambda j: "x"
            empty = {"jobs": []}
            bc.write_state(cfg, mode="queue", phase="ready", last_switch_at=0)
            act = cfg["activity_file"]
            act.parent.mkdir(parents=True, exist_ok=True)
            act.write_text("")
            os.utime(act, (now - 30, now - 30))
            r = q.bloom_queue_sync(empty, pk, now=now, ctl=ctl, background=False)
            check("queue empty + Claude active: pair is KEPT (no release)", r is None and not Ctl.calls, (r, Ctl.calls))
            os.utime(act, (now - 1900, now - 1900))
            r = q.bloom_queue_sync(empty, pk, now=now, ctl=ctl, background=False)
            check("queue empty + 30 min quiet: released (Gemma back)", r == "release" and Ctl.calls == ["release"], (r, Ctl.calls))
            Ctl.calls[:] = []
            bc.write_state(cfg, mode="queue", phase="ready")
            os.environ["BLOOM_IDLE_GEMMA"] = "0"
            os.utime(act, (now - 30, now - 30))
            r = q.bloom_queue_sync(empty, pk, now=now + 5000, ctl=ctl, background=False)
            check("BLOOM_IDLE_GEMMA=0: legacy immediate release even if active", r == "release", r)
        finally:
            w.close()
    finally:
        os.environ["BLOOM_IDLE_GEMMA"] = "0"



@scenario("idle-autopilot")
def sc_idle_autopilot(bc, q):
    """2026-10-24: idle set = autopilot selected_models minus gpt-oss (primary, Qwen3.5-9B first); pair path unchanged."""
    w = World(bc.__file__, serving=GEMMA, mode="on")
    try:
        prim = PAIR[1]
        base = w.toml.read_text()
        sel = ["gemma-4-26b", "gpt-oss-20b", "Qwen3.5-9B", "GPT-OSS-120B-mlx", prim, "ternary-bonsai-2-27b"]
        write = lambda ids: w.toml.write_text(base + "\n[backend.model_autopilot]\nselected_models = [\n%s\n]\n" % ",\n".join("'%s'" % i for i in ids))
        os.environ.pop("BLOOM_IDLE_AUTOPILOT", None)
        os.environ.pop("BLOOMCTL_EXCLUDE_MODELS", None)
        old = [prim, "Qwen3.5-9B", "gemma-4-26b-qat-4bit"]
        write(sel)
        cfg = bc.load_cfg()
        got = bc.idle_models(cfg)
        check("idle set excludes gpt-oss (any case/variant) even if selected", not any("gpt-oss" in m.lower() for m in got), got)
        check("idle set keeps every other selected model", set(got) == {"gemma-4-26b", "Qwen3.5-9B", prim, "ternary-bonsai-2-27b"}, got)
        check("idle set: primary first, then Qwen3.5-9B", got[:2] == [prim, "Qwen3.5-9B"], got)
        check("gpt-oss in selected_models logged WARN", "selected_models contains" in (w.home / "bloom-control.log").read_text())
        check("provider.toml not edited by the idle-set logic", w.toml.read_text().count("gpt-oss-20b") == 1)
        write([])
        check("empty selected_models -> old restore list", bc.idle_models(bc.load_cfg()) == old, bc.idle_models(bc.load_cfg()))
        w.toml.write_text(base)
        check("no selected_models key -> old restore list", bc.idle_models(bc.load_cfg()) == old)
        w.toml.write_text(base + "\nselected_models = [ 'gpt-oss-20b' ]\n")
        check("only gpt-oss selected -> old restore list", bc.idle_models(bc.load_cfg()) == old)
        w.toml.write_text(base + "\nselected_models = [ 'a'\n")
        check("unparseable selected_models -> old restore list", bc.idle_models(bc.load_cfg()) == old)
        write(sel)
        os.environ["BLOOM_IDLE_AUTOPILOT"] = "0"
        check("BLOOM_IDLE_AUTOPILOT=0 -> old pair+gemma list", bc.idle_models(bc.load_cfg()) == old, bc.idle_models(bc.load_cfg()))
        os.environ.pop("BLOOM_IDLE_AUTOPILOT", None)
        os.environ["BLOOMCTL_EXCLUDE_MODELS"] = "gpt-oss-20b,bonsai"
        check("BLOOMCTL_EXCLUDE_MODELS extends the denylist", not any("bonsai" in m for m in bc.idle_models(bc.load_cfg())))
        os.environ.pop("BLOOMCTL_EXCLUDE_MODELS", None)
        check("active/hold set unchanged: pair only", bc.load_cfg()["pair"] == [prim] or set(bc.load_cfg()["pair"]) <= set(PAIR), bc.load_cfg()["pair"])
        new, _c = bc.desired_toml(w.toml.read_text(), bc.load_cfg()["pair"], bc.load_cfg()["pair"])
        check("hold toml enables the pair only", "gpt-oss" not in re.search(r"enabled_models\s*=\s*\[([^\]]*)\]", new).group(1))
    finally:
        os.environ.pop("BLOOM_IDLE_AUTOPILOT", None)
        os.environ.pop("BLOOMCTL_EXCLUDE_MODELS", None)
        w.close()


QMODEL = PAIR[1]     # the queue model (production pair = the 35B MTP alone)
HOME = "gemma-4-26b"  # what BloomGauge serves at idle in these worlds (not a target: BloomGauge picks)

# `darkbloom status` texts in the live 0.10.4 format (2026-10-29), identifying lines removed
ST_SERVING = ("darkbloom 0.10.4\nEnabled model filter: %s\n\nDaemon: running (pid 1, up 5m)\n"
              "Model switch: serving; 0 unfinished request(s)\n  Selection: %s\n" % (HOME, HOME))
ST_DRAIN1 = ("darkbloom 0.10.4\nEnabled model filter: %s\n\nDaemon: running (pid 1, up 46m)\n"
             "Not serving: draining, 1 unfinished request(s); new requests are refused until it finishes\n"
             "Autopilot - Experimental: off\nModel switch: timedOut; 1 unfinished request(s)\n"
             "  Request: 4FBAF9FE-2726-4C11-BAEC-F11580C31996\n"
             "  Model-switch drain deadline expired; no accepted request was cancelled. Admission remains closed; "
             "repeat darkbloom switch to reconcile, or use darkbloom restart.\n  Selection: %s\n"
             "Warm models: Qwen3.5-9B\nServing outside the enabled model filter: Qwen3.5-9B (in the running daemon's "
             "selection; the saved filter applies after a restart)\n" % (QMODEL, QMODEL))
ST_DRAIN0 = ST_DRAIN1.replace("draining, 1 unfinished", "draining, 0 unfinished").replace(
    "timedOut; 1 unfinished", "timedOut; 0 unfinished")
ST_SWITCHING = ("darkbloom 0.10.4\nDaemon: running (pid 1, up 5m)\n"
                "Not serving: draining, 1 unfinished request(s); new requests are refused until it finishes\n"
                "Model switch: draining; 1 unfinished request(s)\n  Selection: %s\n" % QMODEL)
OTHER_OP = "Error: Provider is starting, disconnected, updating, or draining for another operation"
BAD_OPS = ("start", "stop", "restart", "autopilot")


def _gauge_env(w=None):
    os.environ["BLOOMCTL_PAIR"] = QMODEL
    os.environ["BLOOMCTL_GAUGE_HANDOFF"] = "1"
    os.environ["BLOOM_IDLE_GEMMA"] = "1"
    os.environ["BLOOM_PAIR_RESTART_AFTER_S"] = "0.3"
    os.environ["BLOOMCTL_SWITCH_WAIT_S"] = "3"
    os.environ["BLOOMCTL_DRAIN_POLL_S"] = "0.2"
    os.environ.pop("BLOOMCTL_DRAIN_ALERT_S", None)
    os.environ.pop("BLOOMCTL_SWITCH_TIMEOUT_S", None)


def _gauge_env_reset():
    for k in ("BLOOM_PAIR_RESTART_AFTER_S", "BLOOMCTL_SWITCH_WAIT_S", "BLOOMCTL_DRAIN_POLL_S", "BLOOMCTL_DRAIN_ALERT_S"):
        os.environ.pop(k, None)
    os.environ["BLOOMCTL_GAUGE_HANDOFF"] = "0"
    os.environ["BLOOM_IDLE_GEMMA"] = "0"
    os.environ["BLOOMCTL_PAIR"] = ",".join(PAIR)


def _gauge_world(bc, serving, status=ST_SERVING, **kw):
    """Production-shaped: pair = the MTP alone, BloomGauge on (its own mode), currentModel live, a readable
    `darkbloom status`, and the leftover queue preload pin found live on 2026-10-29."""
    _gauge_env()
    w = World(bc.__file__, serving=serving, auto_update=False, **kw)
    _gauge_env()
    w.sv["current"] = "live"
    w.toml.write_text(w.toml.read_text().replace(
        "preload_models = [ 'qwen3.6-35b-a3b-vl-mtp-mxfp8', 'qwen3.5-9b' ]", "preload_models = [ '%s' ]" % QMODEL))
    _status(w, status)
    return w


def _status(w, text):
    (w.dbdir / "status_text").write_text(text)


def _served(w):
    return json.loads(w.loaded.read_text())["models"]


def _log(w):
    p = w.home / "bloom-control.log"
    return p.read_text() if p.exists() else ""


def _bad(calls):
    return [c for c in calls if c.split(" ")[0] in BAD_OPS or "--force" in c or "--autopilot" in c]


@scenario("status-parse")
def t_status_parse(bc, q):
    """`darkbloom status` lines are readable state: drain, unfinished count, timedOut, Selection, outside filter."""
    o = bc.parse_status_ops(ST_DRAIN1)
    check("live text: running, draining, admission closed", o["running"] and o["draining"] and o["admission_closed"], o)
    check("live text: timedOut with 1 unfinished", o["switch"] == "timedOut" and o["timed_out"] and o["unfinished"] == 1, o)
    check("live text: Selection + outside-filter models", o["selection"] == QMODEL and o["outside_filter"] == ["Qwen3.5-9B"]
          and o["filter"] == [QMODEL], o)
    check("live text: blocked (no new operation may be stacked)", o["blocked"] and not o["reconcile"], o)
    o0 = bc.parse_status_ops(ST_DRAIN0)
    check("timedOut + 0 unfinished -> reconcile (repeat the switch)", o0["blocked"] and o0["reconcile"], o0)
    s = bc.parse_status_ops(ST_SERVING)
    check("serving: not blocked, 0 unfinished", not s["blocked"] and s["unfinished"] == 0 and not s["draining"], s)
    check("a switch in progress (draining, not timed out) is blocked, no reconcile",
          bc.parse_status_ops(ST_SWITCHING)["blocked"] and not bc.parse_status_ops(ST_SWITCHING)["reconcile"])
    check("'another operation' answer is blocked", bc.parse_status_ops(OTHER_OP)["blocked"])
    check("empty / unreadable -> not blocked, nothing known", not bc.parse_status_ops("")["blocked"]
          and bc.parse_status_ops("")["unfinished"] is None)


@scenario("drain-hold-wait")
def t_drain_hold_wait(bc, q):
    """Live failure 2026-10-29: the daemon is mid-drain (timed-out switch, admission closed). The hold must not stack a
    switch/start/restart: it records phase draining (in-flight, elapsed), logs each poll, alerts past the threshold,
    reconciles with ONE repeated switch once the request finishes, and only then reports the model served."""
    w = _gauge_world(bc, [HOME], status=ST_DRAIN1)
    try:
        r = bc.hold_for_queue()
        st = w.state()
        check("draining: hold not ok, says draining", not r["ok"] and "drain" in str(r.get("why")), r)
        check("draining: no switch/start/stop/restart/--force issued", not [c for c in w.calls() if c != "status"], w.calls())
        d = st.get("drain") or {}
        check("draining: state phase draining with in-flight count + since + elapsed",
              st.get("phase") == "draining" and d.get("unfinished") == 1 and d.get("since") and "elapsed_s" in d
              and d.get("selection") == QMODEL and d.get("admission_closed") is True, st)
        since = d.get("since")
        r2 = bc.hold_for_queue()
        check("second poll: still waiting, nothing issued", not r2["ok"] and not [c for c in w.calls() if c != "status"], w.calls())
        check("a log line per poll", _log(w).count("DRAIN:") >= 2, _log(w)[-600:])
        check("drain since kept across polls", (w.state().get("drain") or {}).get("since") == since, w.state())
        check("no alert before the threshold", "ALERT" not in _log(w))
        os.environ["BLOOMCTL_DRAIN_ALERT_S"] = "0"
        bc.hold_for_queue()
        check("alert past the threshold (a human decides)", "ALERT" in _log(w)
              and (w.state().get("drain") or {}).get("alert") is True, _log(w)[-600:])
        check("queue model not dispatched to: hold never reported ready while draining", w.state().get("phase") != "ready")
        _status(w, ST_DRAIN0)
        (w.dbdir / "switch_sets_status").write_text(ST_SERVING.replace(HOME, QMODEL))
        r3 = bc.hold_for_queue()
        sw = [c for c in w.calls() if c.startswith("switch")]
        check("request finished: exactly ONE reconcile switch to the Selection, never start/restart/--force",
              len(sw) == 1 and ("--model " + QMODEL) in sw[0] and not _bad(w.calls()), w.calls())
        check("after the reconcile: held, drain cleared", r3["ok"] and w.state().get("phase") == "ready"
              and not w.state().get("drain") and _served(w) == [QMODEL], (r3, w.state()))
    finally:
        w.close()
        _gauge_env_reset()


@scenario("drain-no-stack")
def t_drain_no_stack(bc, q):
    """A switch that answers 'draining for another operation' (or leaves the daemon timed out) is never followed by a
    stacked `darkbloom start`; the switch carries a long --timeout."""
    w = _gauge_world(bc, [HOME])
    try:
        (w.dbdir / "switch_rc").write_text("64")
        (w.dbdir / "switch_out").write_text(OTHER_OP)
        (w.dbdir / "switch_sets_status").write_text(ST_SWITCHING)
        r = bc.hold_for_queue()
        sw = [c for c in w.calls() if c.startswith("switch")]
        check("busy daemon: ONE switch, no start fallback", len(sw) == 1 and not _bad(w.calls()), w.calls())
        check("the switch used a long drain timeout (1800 s)", sw and "--timeout 1800" in sw[0], sw)
        check("busy daemon: hold not ok, phase draining", not r["ok"] and w.state().get("phase") == "draining", (r, w.state()))
        n = len(w.calls())
        bc.hold_for_queue()
        check("next poll while draining: nothing issued", not [c for c in w.calls()[n:] if c != "status"], w.calls()[n:])
    finally:
        w.close()
        _gauge_env_reset()


@scenario("drain-release-wait")
def t_drain_release_wait(bc, q):
    """Release waits out a drain too: no toml edit, no BloomGauge call, mode stays queue until the daemon serves again."""
    w = _gauge_world(bc, [HOME])
    try:
        r = bc.hold_for_queue()
        check("hold ok", r["ok"], r)
        _status(w, ST_SWITCHING)
        txt = w.toml.read_text()
        rel = bc.release_to_bloom()
        check("draining: release not ok, mode queue, phase draining", not rel["ok"] and w.state().get("mode") == "queue"
              and w.state().get("phase") == "draining", (rel, w.state()))
        check("draining: provider.toml untouched by the release", w.toml.read_text() == txt)
        _status(w, ST_SERVING.replace(HOME, QMODEL))
        rel = bc.release_to_bloom()
        check("drain over: released", rel["ok"] and w.state().get("mode") == "bloom" and not w.state().get("drain"),
              (rel, w.state()))
    finally:
        w.close()
        _gauge_env_reset()


def _gauge_cycle(bc, w, label):
    """hold -> release with BloomGauge left exactly as the user has it."""
    r = bc.hold_for_queue()
    calls = w.calls()
    check(label + ": hold ok", r["ok"] and not r["noop"], (r, calls))
    check(label + ": queue model served", _served(w) == [QMODEL], _served(w))
    check(label + ": no BloomGauge POST (its settings need the app session)", w.posts == [], w.posts)
    check(label + ": hold not DEGRADED by BloomGauge being read-only", not r.get("degraded"), r)
    check(label + ": never --autopilot/--force", not any("--autopilot" in c or "--force" in c for c in calls), calls)
    check(label + ": hold wrote no provider.toml pin/filter (no backup)",
          not list(w.toml.parent.glob("provider.toml.bak-bloomctl-*")), w.toml.read_text())
    n = len(calls)
    rel = bc.release_to_bloom()
    after = [c for c in w.calls()[n:] if c != "status"]
    check(label + ": release ok", rel["ok"] and not rel["noop"], (rel, after))
    check(label + ": release picks no model (no switch/start): BloomGauge manages", not after, after)
    txt = w.toml.read_text()
    check(label + ": saved filter back to BloomGauge's (%s), queue preload pin gone" % HOME,
          "enabled_models = [ '%s' ]" % HOME in txt and QMODEL not in txt, txt)
    check(label + ": still no BloomGauge POST", w.posts == [], w.posts)
    st = w.state()
    check(label + ": state bloom/idle", st.get("mode") == "bloom" and st.get("phase") == "idle", st)
    return rel


@scenario("gauge-handoff")
def t_gauge_handoff(bc, q):
    """2026-10-29 contract: work -> the queue model by ONE `darkbloom switch`; idle -> BloomGauge manages (left as the
    user has it). The log says Automatic only when a GET of the control view confirms it."""
    w = _gauge_world(bc, [HOME])
    try:
        os.environ.pop("BLOOMCTL_GAUGE_HANDOFF")
        check("default: gauge hand-off on when BloomGauge is on", bc.gauge_handoff() is True)
        os.environ["BLOOMCTL_GAUGE_HANDOFF"] = "1"
        check("default start args carry no --autopilot", "--autopilot" not in bc.load_cfg()["start_extra"])
        _gauge_cycle(bc, w, "auto-on")
        sw = [c for c in w.calls() if c.startswith("switch")]
        check("one switch (no restart)", len(sw) == 1 and w.starts() == [] and ("--model " + QMODEL) in sw[0], w.calls())
        check("log: Automatic confirmed by GET", "BloomGauge managing (Automatic on, confirmed by GET)" in _log(w), _log(w)[-500:])
        n = len(w.calls())
        r2 = bc.release_to_bloom()
        check("idempotent: a second release changes nothing", r2["ok"] and r2["noop"]
              and not [c for c in w.calls()[n:] if c != "status"], (r2, w.calls()[n:]))
    finally:
        w.close()
        _gauge_env_reset()
    w = _gauge_world(bc, [HOME], mode="manual")
    try:
        _gauge_cycle(bc, w, "user-manual")
        check("user's Manual kept and reported as Manual, never 'Automatic again'",
              w.sv["mode"] == "manual" and "Automatic again" not in _log(w) and "BloomGauge Manual (per GET" in _log(w),
              _log(w)[-500:])
    finally:
        w.close()
        _gauge_env_reset()


@scenario("gauge-autopilot-refused")
def t_gauge_autopilot_refused(bc, q):
    """Autopilot still enrolled (switch rc=64 'Autopilot owns model selection', daemon NOT draining): one restart
    without --autopilot is allowed as the fallback."""
    w = _gauge_world(bc, [HOME])
    try:
        (w.dbdir / "switch_rc").write_text("64")
        (w.dbdir / "switch_out").write_text("Error: Autopilot owns model selection")
        r = bc.hold_for_queue()
        st = w.starts()
        check("rc=64 not draining: one switch then ONE start, never --autopilot",
              r["ok"] and len(st) == 1 and ("--model " + QMODEL) in st[0] and "--autopilot" not in st[0]
              and len([c for c in w.calls() if c.startswith("switch")]) == 1, (r, w.calls()))
    finally:
        w.close()
        _gauge_env_reset()


@scenario("gauge-queue-idle")
def t_gauge_queue_idle(bc, q):
    """The 'stayed on MTP forever' bug: Claude activity kept the queue model after the queue emptied. With BloomGauge
    on, the session decision is 'idle' the moment work ends and the queue daemon releases; a draining phase blocks
    launches and is re-polled at the retry interval, not every tick."""
    w = _gauge_world(bc, [HOME])
    try:
        cfg = bc.load_cfg()
        held = {"mode": "queue", "phase": "ready", "act_first": time.time() - 3600}
        now = time.time()
        check("gauge on + Claude active + no work -> idle (release)",
              bc.session_decision(cfg, held, now, False, False, now - 5)[0] == "idle")
        check("gauge on + queue work -> pair", bc.session_decision(cfg, held, now, True, False, now - 5)[0] == "pair")
        check("gauge on + BloomGauge serving + Claude active -> no hold without work",
              bc.session_decision(cfg, {"mode": "bloom", "act_first": now - 3600}, now, False, False, now - 5)[0] != "pair")
        r = bc.hold_for_queue()
        check("queue daemon: held on the queue model", r["ok"] and _served(w) == [QMODEL], r)
        act = cfg["activity_file"]
        act.parent.mkdir(parents=True, exist_ok=True)
        act.write_text("")
        os.utime(act, (now - 30, now - 30))       # Claude active 30 s ago
        q._BLOOM_SYNC.update(thread=None, last_fail=0.0, last_verify=0.0)
        kind = q.bloom_queue_sync({"jobs": []}, lambda j: "x", now=now, ctl=bc, background=False)
        check("queue daemon: empty queue + Claude active -> released now", kind == "release"
              and w.state().get("mode") == "bloom", (kind, w.calls(), w.state()))
        kind = q.bloom_queue_sync({"jobs": []}, lambda j: "x", now=now + 10, ctl=bc, background=False)
        check("queue daemon: no re-hold of the queue model without work", kind is None, kind)
        bc.write_state(cfg, mode="queue", phase="draining", heartbeat=time.time())
        check("launch gate: phase draining blocks Darkbloom launches",
              bool(q.bloom_control_hold(state_path=str(cfg["home"] / "bloom-control-state.json"))), w.state())

        class Ctl:
            calls = []

            def __getattr__(self, n):
                return getattr(bc, n)

            def hold_for_queue(self, c):
                Ctl.calls.append("hold")
                return {"ok": False, "why": "draining"}

            def release_to_bloom(self, c):
                Ctl.calls.append("release")
                return {"ok": False, "why": "draining"}
        busy = {"jobs": [{"id": "j1", "status": "pending", "model": QMODEL}]}
        q._BLOOM_SYNC.update(thread=None, last_fail=time.time(), last_verify=0.0)
        q.bloom_queue_sync(busy, lambda j: "x", ctl=Ctl(), background=False)
        q.bloom_queue_sync({"jobs": []}, lambda j: "x", ctl=Ctl(), background=False)
        check("draining + a recent failure: neither hold nor release re-run inside the retry interval",
              Ctl.calls == [], Ctl.calls)
    finally:
        w.close()
        _gauge_env_reset()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(HERE / "ollama-queue.py"))
    ap.add_argument("--bc", default=str(HERE / "bloom_control.py"))
    ap.add_argument("--only", action="append", default=[])
    a = ap.parse_args()
    os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
    os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
    bc = load(a.bc, "bloom_control_t")
    q = load(a.queue, "oq_t")
    # the queue loads bloom_control.py from its own dir; point it at the one under test
    if hasattr(q, "_BLOOM_CTL_MOD"):
        q._BLOOM_CTL_MOD[:] = [bc]
    retired = []
    for fn in SEL:
        if a.only and fn._name not in a.only:
            continue
        # bloom_control retirement S1 (197b069) took these out of the queue: a scenario driving one is
        # reported retired, not failed (any other missing API still fails; it runs again if they return)
        gone = sorted(n for n in set(re.findall(r"\bq\.(\w+)", inspect.getsource(fn)))
                      if n in RETIRED_QUEUE_API and not hasattr(q, n))
        if gone:
            retired.append(fn._name)
            print("--- " + fn._name + " (retired: the queue no longer has " + ", ".join(gone) + ")")
            continue
        print("--- " + fn._name)
        try:
            fn(bc, q)
        except Exception as e:   # an API the reverted code lacks is a FAIL, not a crash
            import traceback
            traceback.print_exc()
            check(fn._name + " ran without exception", False, repr(e))
    if retired:
        print("\nRETIRED (queue integration removed in 197b069): %d scenario(s): %s"
              % (len(retired), ", ".join(retired)))
    if FAILS:
        print("\nFAILED: %d\n  " % len(FAILS) + "\n  ".join(FAILS))
        return 1
    print("\nALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
