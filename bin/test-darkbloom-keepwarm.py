#!/usr/bin/env python3
"""Hermetic tests for darkbloom-keepwarm.py and the queue's darkbloom_warm_hold guard.

A stub Darkbloom models the REAL eviction rule found in the research: `slots` models resident
at once; a request for an enabled, non-resident model lazy-loads it and evicts the
least-recently-used resident model; a request for a resident model just refreshes its recency.
Nothing here touches the real daemon / Darkbloom / provider.toml / BloomGauge: temp HOME, temp
queue-state, stub HTTP server.

Usage: test-darkbloom-keepwarm.py [--script P] [--queue P] [--only X] | --revert-check
Markers: ALL PASS / REVERT CHECK OK."""
import argparse
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPT = HERE / "darkbloom-keepwarm.py"
QUEUE = HERE / "ollama-queue.py"
A, B = "Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8"      # the queue's two models
X, Y = "gemma-4-26b-qat-4bit", "nvidia-nemotron-3.5-lightning"   # third-party / candidate models
KEY = "SECRETKEYSECRETKEYSECRETKEYSECRETKEY123"
T0 = 1_800_000_000.0
FAILS = []


def check(name, cond, extra=""):
    print(("ok  : " if cond else "FAIL: ") + name + ("" if cond else "  -- " + str(extra)))
    if not cond:
        FAILS.append(name)


class World:
    def __init__(self, resident=(A, B), slots=2, enabled=(A, B, X, Y), health_ok=True, thrash=False):
        self.d = Path(tempfile.mkdtemp(prefix="kw-"))
        self.qs = self.d / "queue-state.json"
        self.loaded_json = self.d / "loaded-models.json"
        self.state_path = self.d / "kw-state.json"
        self.log_path = self.d / "kw.log"
        self.enabled_file = self.d / "kw.enabled"
        self.requests = []                 # (model, max_tokens, auth_ok) in arrival order
        self.slots, self.enabled_models, self.health_ok, self.thrash = slots, list(enabled), health_ok, thrash
        self.lru = list(resident)          # oldest first
        self.sync()
        self.set_queue([])
        w = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, body):
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if self.path == "/health":
                    self._send(200 if w.health_ok else 503, {"status": "ok" if w.health_ok else "down"})
                elif self.path == "/v1/models":
                    self._send(200, {"data": [{"id": m} for m in w.enabled_models]})
                else:
                    self._send(404, {})

            def do_POST(self):
                n = int(self.headers.get("Content-Length", "0"))
                body = json.loads(self.rfile.read(n))
                m = body.get("model")
                w.requests.append((m, body.get("max_tokens"), self.headers.get("Authorization") == "Bearer " + KEY))
                if getattr(w, "echo_key", False):
                    self._send(500, {"error": "bad auth header Bearer " + KEY})
                    return
                if m not in w.enabled_models:
                    self._send(404, {"error": "model not served"})
                    return
                if m in w.lru:
                    w.lru.remove(m)
                    w.lru.append(m)                      # refresh recency
                else:
                    while len(w.lru) >= w.slots:
                        w.lru.pop(0)                     # evict the least-recently-used
                    w.lru.append(m)
                    if w.thrash:                         # public traffic evicts it again at once
                        w.lru.remove(m)
                w.sync()
                self._send(200, {"choices": [{"message": {"content": "p"}}]})

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        (self.d / "local.json").write_text(json.dumps({"api_key": KEY, "base_url": "http://127.0.0.1:%d" % self.srv.server_port}))

    def sync(self):
        self.loaded_json.write_text(json.dumps({"models": list(self.lru)}))

    def set_queue(self, jobs):
        self.qs.write_text(json.dumps({"jobs": jobs}))

    def enable(self, on=True):
        if on:
            self.enabled_file.write_text("1")
        elif self.enabled_file.exists():
            self.enabled_file.unlink()

    def state(self):
        try:
            return json.loads(self.state_path.read_text())
        except (OSError, ValueError):
            return None

    def log(self):
        try:
            return self.log_path.read_text()
        except OSError:
            return ""

    def models_hit(self):
        return [r[0] for r in self.requests]

    def run(self, script, now=T0, conf=None):
        cf = self.d / "conf.json"
        cf.write_text(json.dumps(conf or {}))
        env = dict(os.environ, HOME=str(self.d), KEEPWARM_HOME=str(self.d), KEEPWARM_ENABLED=str(self.enabled_file),
                   KEEPWARM_STATE=str(self.state_path), KEEPWARM_LOG=str(self.log_path),
                   KEEPWARM_LOCK=str(self.d / "kw.lock"), KEEPWARM_CONF=str(cf),
                   KEEPWARM_QUEUE_STATE=str(self.qs), KEEPWARM_LOCAL_JSON=str(self.d / "local.json"),
                   KEEPWARM_LOADED_JSON=str(self.loaded_json), KEEPWARM_NOW=str(now), KEEPWARM_NO_NOTIFY="1", KEEPWARM_BLOOM_IDLE_STATE=str(self.d / "bloom-idle-state.json"))
        r = subprocess.run([sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60)
        self.out = r.stdout + r.stderr
        return r

    def close(self):
        self.srv.shutdown()
        shutil.rmtree(self.d, ignore_errors=True)


def t_kill_switch_off(S):
    w = World(resident=(A, X))
    w.state_path.write_text(json.dumps({"heartbeat": T0, "hold": [B]}))
    r = w.run(S)
    check("kill switch OFF: exit 0, no request, no log", r.returncode == 0 and w.requests == [] and w.log() == "", (w.requests, w.log()))
    check("kill switch OFF: stale state removed so the queue stops holding", w.state() is None, w.state())
    w.close()


def t_all_resident_quiet(S):
    w = World()
    w.enable()
    w.run(S, T0, {"touch_interval_s": 600})
    first = list(w.models_hit())
    check("first sight: both models touched (recency) with max_tokens=1 and the key",
          sorted(first) == sorted([A, B]) and all(r[1] == 1 and r[2] for r in w.requests), w.requests)
    w.run(S, T0 + 120, {"touch_interval_s": 600})
    check("within the touch interval: no further request", w.models_hit() == first, w.models_hit())
    w.run(S, T0 + 700, {"touch_interval_s": 600})
    check("after the touch interval: touched again", len(w.models_hit()) == 4, w.models_hit())
    st = w.state()
    check("state: heartbeat set, nothing on hold", st and st["heartbeat"] == T0 + 700 and st["hold"] == [], st)
    w.close()


def t_touch_disabled(S):
    w = World()
    w.enable()
    w.run(S, T0, {"touch_interval_s": 0})
    check("touch_interval_s=0: no touches at all", w.requests == [], w.requests)
    w.close()


def t_rewarm_after_eviction(S):
    # B was evicted by third-party traffic; the LRU order is [A, X]: A is the oldest. A naive
    # "just request B" would evict A. The guard must touch A FIRST so X is the victim.
    w = World(resident=(A, X))
    w.enable()
    w.run(S, T0)
    check("re-warm touches the RESIDENT queue model first, then loads the missing one",
          w.models_hit() == [A, B], w.models_hit())
    check("both queue models resident afterwards, the third-party model was the victim",
          set(w.lru) == {A, B}, w.lru)
    st = w.state()
    check("state: hold cleared once resident, attempt recorded", st["hold"] == [] and len(st["models"][B]["attempts"]) == 1, st)
    check("eviction alerted + logged", "ALERT" in w.log() and B in w.log(), w.log())
    w.close()


def t_hold_visible_during_warm(S):
    # the state written BEFORE the request carries hold=[B] (the queue waits meanwhile)
    w = World(resident=(A, X))
    w.enable()
    seen = {}
    orig = w.srv.RequestHandlerClass.do_POST

    def spy(self):
        try:
            if not seen and w.state_path.exists() and json.loads(w.state_path.read_text()).get("hold"):
                seen["hold"] = json.loads(w.state_path.read_text())["hold"]
        except (OSError, ValueError):
            pass
        orig(self)
    w.srv.RequestHandlerClass.do_POST = spy
    w.run(S, T0)
    check("hold=[missing model] is published before the load request is sent", seen.get("hold") == [B], seen)
    w.close()


def t_running_job_blocks(S):
    w = World(resident=(A, X))
    w.enable()
    w.set_queue([{"id": "j1", "status": "running"}])
    w.run(S, T0)
    check("a running queue job: no request at all", w.requests == [], w.requests)
    check("...the missing model stays on hold for the queue", (w.state() or {}).get("hold") == [B], w.state())
    w.set_queue([])
    w.qs.write_text("{broken")
    w.run(S, T0 + 60)
    check("unreadable queue state counts as running (fail safe): no request", w.requests == [], w.requests)
    w.close()


def t_rate_limit_thrash(S):
    w = World(resident=(A, X), thrash=True)        # every load is evicted again immediately
    w.enable()
    conf = {"max_rewarm_per_hour": 2, "rewarm_min_gap_s": 0, "touch_interval_s": 0}
    for i in range(5):
        w.run(S, T0 + i * 60, conf)
    b_hits = [m for m in w.models_hit() if m == B]
    check("re-warm attempts capped at max_rewarm_per_hour", len(b_hits) == 2, w.models_hit())
    check("thrash is alerted loudly", "keeps getting evicted" in w.log(), w.log())
    check("once exhausted the queue is NOT held (it falls back to a plain cold load)", (w.state() or {}).get("hold") == [], w.state())
    w.run(S, T0 + 3700, conf)
    check("an hour later it tries again", len([m for m in w.models_hit() if m == B]) == 3, w.models_hit())
    w.close()


def t_min_gap(S):
    w = World(resident=(A, X), thrash=True)
    w.enable()
    conf = {"rewarm_min_gap_s": 120, "touch_interval_s": 0, "max_rewarm_per_hour": 10}
    w.run(S, T0, conf)
    n = len([m for m in w.models_hit() if m == B])
    w.run(S, T0 + 60, conf)
    check("min gap between re-warm attempts honoured (and still held for the queue)",
          len([m for m in w.models_hit() if m == B]) == n and (w.state() or {}).get("hold") == [B], (w.models_hit(), w.state()))
    w.close()


def t_idle_switch_owns(S):
    w = World(resident=(A, X))
    w.enable()
    ids = w.d / "bloom-idle-state.json"
    ids.write_text(json.dumps({"state": "away", "heartbeat": T0 - 30}))
    w.run(S, T0)
    check("bloom-idle-switch away: no touches/re-warm (optimizer owns the choice), nothing held",
          w.requests == [] and (w.state() or {}).get("hold") == [], (w.requests, w.state()))
    ids.write_text(json.dumps({"state": "restoring", "heartbeat": T0 - 30}))
    w.run(S, T0 + 60)
    check("bloom-idle-switch restoring: still hands off", w.requests == [], w.requests)
    ids.write_text(json.dumps({"state": "away", "heartbeat": T0 - 4000}))
    w.run(S, T0 + 120)
    check("a STALE idle-switch heartbeat is ignored: the guard defends the pair again", w.models_hit()[-1:] == [B], w.models_hit())
    ids.write_text(json.dumps({"state": "home", "heartbeat": T0 + 200}))
    w.run(S, T0 + 300)
    check("state home: normal guarding", B in w.lru and A in w.lru, w.lru)
    w.close()


def t_provider_down(S):
    w = World(resident=(A, X), health_ok=False)
    w.enable()
    r = w.run(S, T0)
    check("provider /health down: no request, exit 0, nothing on hold", r.returncode == 0 and w.requests == [] and (w.state() or {}).get("hold") == [], (w.requests, w.state()))
    w.close()


def t_never_restarts(S):
    src = Path(S).read_text()
    check("the guard never invokes darkbloom start/restart/stop/switch (static)",
          not any(w in src for w in ('"start"', '"restart"', '"stop"', '"switch"', "darkbloom start", "darkbloom restart")), "")
    check("the guard never writes provider.toml (static)", "provider.toml\").write" not in src and "provider_toml" not in src)


def t_no_secrets(S):
    w = World(resident=(A, X))
    w.enable()
    r = w.run(S, T0)
    check("api key never in stdout/stderr/log/state", KEY not in w.out and KEY not in w.log() and KEY not in json.dumps(w.state()), w.log())
    w.echo_key = True                                   # a server error body that echoes the key must be scrubbed
    w.run(S, T0 + 500)
    check("api key echoed by a server error is never in stdout/stderr/log/state (never in)",
          KEY not in w.out and KEY not in w.log() and KEY not in json.dumps(w.state()), w.log())
    w.close()


def t_lock(S):
    import fcntl
    w = World(resident=(A, X))
    w.enable()
    with open(w.d / "kw.lock", "w") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        r = w.run(S, T0)
    check("overlapping tick exits 0 without acting", r.returncode == 0 and w.requests == [], w.requests)
    w.close()


def t_queue_guard(Q):
    os.environ["HOME"] = tempfile.mkdtemp(prefix="kw-q-")
    ld = SourceFileLoader("oq_kw", str(Q))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oq_kw", ld))
    ld.exec_module(m)
    d = Path(tempfile.mkdtemp(prefix="kw-qs-"))
    p = d / "s.json"
    N = 50_000.0

    def hold(model, now=N, **st):
        p.write_text(json.dumps(st))
        return m.darkbloom_warm_hold(model, now, str(p))

    check("warm hold: model on hold + fresh heartbeat -> hold", bool(hold(B, hold=[B], heartbeat=N - 5)))
    check("warm hold: case-insensitive", bool(hold(B.upper(), hold=[B], heartbeat=N - 5)))
    check("warm hold: a different model is NOT held", hold(A, hold=[B], heartbeat=N - 5) is None)
    check("warm hold: empty hold list -> none", hold(B, hold=[], heartbeat=N - 5) is None)
    check("warm hold: STALE heartbeat (guard dead) never freezes the queue", hold(B, hold=[B], heartbeat=N - 3600) is None)
    check("warm hold: missing file -> none", m.darkbloom_warm_hold(B, N, str(d / "nope.json")) is None)
    p.write_text("garbage")
    check("warm hold: corrupt file -> none", m.darkbloom_warm_hold(B, N, str(p)) is None)
    import time as _t
    os.environ["DARKBLOOM_KEEPWARM_STATE"] = str(p)
    os.environ["BLOOM_IDLE_STATE"] = str(d / "none.json")
    p.write_text(json.dumps({"hold": [B], "heartbeat": _t.time()}))
    probed = []
    ok = m.darkbloom_model_ready(B, _t.time(), probe=lambda x: probed.append(x) or True, cache={})
    check("darkbloom_model_ready False while keep-warm re-warms that model (probe not even asked)", ok is False and probed == [], (ok, probed))
    ok = m.darkbloom_model_ready(A, _t.time(), probe=lambda x: True, cache={})
    check("...but the OTHER queue model launches normally", ok is True, ok)
    c = {}
    m.darkbloom_model_ready(B, _t.time(), probe=lambda x: True, cache=c)
    check("the hold verdict is never cached", c == {}, c)
    p.write_text(json.dumps({"hold": [], "heartbeat": _t.time()}))
    check("model ready again once the hold clears", m.darkbloom_model_ready(B, _t.time(), probe=lambda x: True, cache={}) is True)
    # listed-but-not-loaded (evicted since the guard's last tick) must wait while the guard is live
    lj = d / "loaded.json"
    os.environ["DARKBLOOM_LOADED_JSON"] = str(lj)
    p.write_text(json.dumps({"hold": [], "missing": [], "heartbeat": _t.time()}))
    lj.write_text(json.dumps({"models": [A]}))
    check("loaded-not-just-listed: model absent from loaded-models.json + live guard -> hold", bool(m.darkbloom_warm_hold(B)), "")
    check("loaded-not-just-listed: the loaded model is not held", m.darkbloom_warm_hold(A) is None)
    check("loaded-not-just-listed: darkbloom_model_ready False for the evicted model",
          m.darkbloom_model_ready(B, _t.time(), probe=lambda x: True, cache={}) is False)
    p.write_text(json.dumps({"hold": [], "missing": [B], "heartbeat": _t.time()}))
    check("guard gave up on it (thrash cap): not held, the queue may cold-load", m.darkbloom_warm_hold(B) is None)
    p.write_text(json.dumps({"hold": [], "missing": [], "heartbeat": _t.time() - 4000}))
    check("dead guard (stale heartbeat): listed is enough, never freezes the queue", m.darkbloom_warm_hold(B) is None)
    lj.write_text("garbage")
    p.write_text(json.dumps({"hold": [], "missing": [], "heartbeat": _t.time()}))
    check("unreadable loaded-models.json: fail open", m.darkbloom_warm_hold(B) is None)
    del os.environ["DARKBLOOM_LOADED_JSON"]
    del os.environ["DARKBLOOM_KEEPWARM_STATE"], os.environ["BLOOM_IDLE_STATE"]
    src = Path(Q).read_text()
    check("launch infra-wait path wired to the warm hold",
          "_bih = darkbloom_warm_hold(dbm)" in src       # darkbloom_launch_wait (2026-10-12 refactor)
          and "_dbw = darkbloom_launch_wait(_dbm, time.time())" in src)   # ...and the launch loop calls it


TESTS = [t_kill_switch_off, t_all_resident_quiet, t_touch_disabled, t_rewarm_after_eviction, t_hold_visible_during_warm,
         t_running_job_blocks, t_rate_limit_thrash, t_min_gap, t_idle_switch_owns, t_provider_down, t_never_restarts, t_no_secrets, t_lock]


def run_all(script, queue, only=None):
    for t in TESTS:
        if only and only not in t.__name__:
            continue
        print("--", t.__name__)
        t(script)
    if not only or "guard" in only:
        print("-- t_queue_guard")
        t_queue_guard(queue)
    print("ALL PASS" if not FAILS else "FAILED: %s" % FAILS)
    return 0 if not FAILS else 1


def mutate(src, old, new, dest):
    txt = Path(src).read_text()
    assert txt.count(old) == 1, "anchor not unique: %r" % old
    Path(dest).write_text(txt.replace(old, new))
    os.chmod(dest, 0o755)


def revert_check(script, queue):
    tmp = Path(tempfile.mkdtemp(prefix="kw-rev-"))
    muts = [
        ("residents NOT touched first (evicts the other queue model)", "script",
         "            order = [m for m in models if m not in missing] + missing   # touch residents FIRST\n",
         "            order = list(missing)\n", "touches the RESIDENT"),
        ("acts while a job is running", "script", "        if busy:\n            log(cfg, msg + ", "        if False:\n            log(cfg, msg + ", "running queue job"),
        ("no rate limit", "script", "                    if len(recent) >= cfg[\"max_rewarm_per_hour\"]:\n", "                    if False:\n", "capped at max_rewarm"),
        ("min gap ignored", "script", "                    if recent and now - recent[-1] < cfg[\"rewarm_min_gap_s\"]:\n", "                    if False:\n", "min gap"),
        ("fights the idle-switch's away state", "script", '        return (d.get("state") in ("away", "restoring")\n', '        return (False and d.get("state") in ("away", "restoring")\n', "bloom-idle-switch away"),
        ("kill switch ignored", "script", "    if not cfg[\"enabled\"].exists():\n", "    if False:\n", "kill switch OFF"),
        ("hold not published before the request", "script", "                    st[\"hold\"] = hold\n                    write_state(cfg, st)           # the queue waits while the request is in flight\n", "", "published before"),
        ("unreadable queue state treated as idle", "script", "    return True\n\n\ndef db_base_key", "    return False\n\n\ndef db_base_key", "unreadable queue state"),
        ("key leaks into the log", "script", '    return _SECRETISH.sub("[redacted]", str(t))[:limit].replace("\\n", " ")', '    return str(t)[:limit].replace("\\n", " ")', "never in"),
        ("queue accepts listed-but-not-loaded", "queue", "            if isinstance(ld, list) and low not in", "            if False and isinstance(ld, list) and low not in", "loaded-not-just-listed"),
        ("queue ignores the warm hold", "queue", "    if darkbloom_warm_hold(model, now):\n        return False\n", "    if False:\n        return False\n", "False while keep-warm"),
        ("queue ignores a stale heartbeat (dead guard freezes the queue)", "queue",
         '        if now - float(st.get("heartbeat") or 0) > float(os.environ.get("BLOOM_IDLE_HB_MAX", "300")):\n            return None\n        low = str(model or "")',
         '        low = str(model or "")', "STALE heartbeat"),
    ]
    bad = 0
    for name, which, old, new, expect in muts:
        dest = tmp / ("s.py" if which == "script" else "q.py")
        mutate(script if which == "script" else queue, old, new, dest)
        args = [sys.executable, str(Path(__file__).resolve()), "--script", str(dest if which == "script" else script),
                "--queue", str(dest if which == "queue" else queue)]
        r = subprocess.run(args, capture_output=True, text=True, timeout=600)
        out = r.stdout + r.stderr
        red = r.returncode != 0 and expect in "".join(l for l in out.splitlines() if l.startswith("FAIL:"))
        print(("ok  : " if red else "NOT RED: ") + "mutant '%s' -> red on '%s'" % (name, expect))
        if not red:
            bad += 1
            print(out[-1200:])
    baks = sorted(HERE.glob("ollama-queue.py.bak-*-keepwarm"))
    if baks:
        r = subprocess.run([sys.executable, str(Path(__file__).resolve()), "--script", str(script), "--queue", str(baks[0]), "--only", "guard"],
                           capture_output=True, text=True, timeout=300)
        print(("ok  : " if r.returncode != 0 else "NOT RED: ") + "pre-feature queue backup fails the guard tests (%s)" % baks[0].name)
        bad += 0 if r.returncode != 0 else 1
    shutil.rmtree(tmp, ignore_errors=True)
    print("REVERT CHECK OK" if not bad else "REVERT CHECK FAILED (%d)" % bad)
    return 1 if bad else 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--script", default=str(SCRIPT))
    ap.add_argument("--queue", default=str(QUEUE))
    ap.add_argument("--only")
    ap.add_argument("--revert-check", action="store_true")
    a = ap.parse_args()
    return revert_check(a.script, a.queue) if a.revert_check else run_all(a.script, a.queue, a.only)


if __name__ == "__main__":
    sys.exit(main())
