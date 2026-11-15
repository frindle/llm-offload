#!/usr/bin/env python3
"""parity_ci.py -- the container checks the image workflow runs (parity tests 2, 3, 4, 5, 6, 11, 13, 15, 16 of the
parity review, plus the standby checks). Made-up data and fake tokens only; every check prints one PASS / FAIL line;
exit 1 on any FAIL. Run by parity-ci.sh inside the built image (or natively on a test host). Never run it against a
real state dir: it makes its own temp HOME, state dir and dispatch dir.

What each check proves is stated in its line. Where the real thing needs the Mac (Darkbloom, launchd) the check is a
reduced stand-in and says so.
"""
import contextlib
import http.server
import importlib.util
import json
import os
import re
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CODE = Path(os.environ.get("QUEUE_CODE") or Path(__file__).resolve().parent)
TMP = Path(tempfile.mkdtemp(prefix="parity-ci."))
HOME = TMP / "home"
STATE = TMP / "state"
DISP = TMP / "disp"
for d in (HOME, STATE, DISP):
    d.mkdir()
(HOME / "bin").symlink_to(CODE)
FAKE_TOK = "fake-token-" + "0123456789abcdef"
BASE_ENV = dict(os.environ, HOME=str(HOME), QUEUE_STATE_DIR=str(STATE), QUEUE_DISPATCH_DIR=str(DISP),
                QEL_DIR=str(DISP), OLLAMA_QUEUE_NO_NOTIFY="1",
                INFRA_INCIDENTS_PATH=str(TMP / "incidents.jsonl"), INFRA_INCIDENTS_SCREEN_LOCKED="0",
                QUEUE_STANDBY="", PYTHONDONTWRITEBYTECODE="1")
for k in ("QUEUE_AGENT_URL", "QUEUE_AGENT_TOKEN", "QUEUE_API_TOKEN", "QUEUE_API"):
    BASE_ENV.pop(k, None)
os.environ.update({k: v for k, v in BASE_ENV.items() if k.startswith(("QUEUE_", "QEL_", "INFRA_", "OLLAMA_", "DISPATCH_")) or k == "HOME"})
os.environ.pop("QUEUE_AGENT_URL", None)
sys.path.insert(0, str(CODE))
FAILS = []
T0 = time.time()

NOW = time.time()
START = "Checking for provider update..."
UP = "2026-01-01T10:00:00-0800 info darkbloom-provider: [HummingbirdCore] Server started and listening on 127.0.0.1:8000"
CACHE = ("2026-01-01T10:00:00-0800 warning darkbloom.ModelScanner.Discovery: [ProviderCore] Failed to read cache "
         "directory /models/cache: The file \u201ccache\u201d couldn\u2019t be opened.")
UNAVAIL = "local API: request failed: model_unavailable (modelNotLoaded, model_load)"
DRAINING = '{"coordinator_acknowledged":false,"operation":"provider_drain","in_flight":0,"reason":"draining"}'
BEGIN = ('{"addon":"example.sandbox","in_flight":0,"lease_seconds":60,"operation":"addon_hold",'
         '"state":"draining","transition":"begin"}')


def hold(tr, lease=60):
    return ('{"addon":"example.sandbox","lease_seconds":%d,"operation":"addon_hold","state":"held",'
            '"transition":"%s"}' % (lease, tr))


def report(name, ok, detail=""):
    if not ok:
        FAILS.append(name)
    print(("PASS " if ok else "FAIL ") + name + (f" -- {detail}" if detail and not ok else ""), flush=True)


def guard(name):
    def deco(fn):
        def run():
            try:
                r = fn()
                if r is None:
                    r = (True, "")
                report(name, r[0], r[1])
            except Exception as e:  # noqa: BLE001
                report(name, False, f"{type(e).__name__}: {str(e)[:200]}")
        run.__name__ = fn.__name__
        return run
    return deco


def load_queue():
    spec = importlib.util.spec_from_file_location("oq_parity", str(CODE / "ollama-queue.py"))
    m = importlib.util.module_from_spec(spec)
    sys.modules["oq_parity"] = m
    spec.loader.exec_module(m)
    return m


Q = None


def q():
    global Q
    if Q is None:
        Q = load_queue()
    return Q


def sh(argv, timeout=120, env=None, cwd=None):
    p = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, env=env or BASE_ENV, cwd=cwd)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ------------------------------------------------------------------ fake status service (Mac agent stand-in)
class Agent:
    view = {"cause": None, "hold": False}
    up = True

    def __enter__(self):
        outer = self

        class H(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                if self.headers.get("Authorization") != "Bearer " + FAKE_TOK:
                    self.send_response(401); self.end_headers(); return
                if self.path.startswith("/v1/hold"):
                    b = json.dumps({"view": outer.view}).encode()
                    self.send_response(200); self.send_header("Content-Length", str(len(b))); self.end_headers()
                    self.wfile.write(b)
                else:
                    self.send_response(404); self.end_headers()
        self.srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        os.environ["QUEUE_AGENT_URL"] = f"http://127.0.0.1:{self.srv.server_address[1]}"
        os.environ["QUEUE_AGENT_TOKEN"] = FAKE_TOK
        return self

    def __exit__(self, *a):
        self.srv.shutdown(); self.srv.server_close()
        os.environ.pop("QUEUE_AGENT_URL", None); os.environ.pop("QUEUE_AGENT_TOKEN", None)


def write_log(path, lines, age_s=0):
    Path(path).write_text("\n".join(lines) + "\n")
    t = time.time() - age_s
    os.utime(path, (t, t))


LOG = TMP / "provider.log"
SCENARIOS = [
    ("hold", [START, UP, BEGIN, hold("held"), hold("renewed")], 5, "sandbox-hold", True),
    ("released", [START, UP, BEGIN, hold("held"), hold("released")], 5, None, False),
    ("restarting", [UP, START], 5, "restarting", False),
    ("cache", [START, UP, CACHE], 5, "model-cache-unreadable", False),
    ("draining", [START, UP, DRAINING], 5, "draining", False),
    ("not-loaded", [START, UP, UNAVAIL], 5, "model-not-loaded", False),
    ("up", [START, UP], 5, None, False),
]


@guard("P02 background jobs run one pass on made-up state (no traceback, exit 0)")
def p02():
    (STATE / "ollama-queue-state.json").write_text(json.dumps({"jobs": []}))
    bad = []
    for name, argv in (("queue-event-log", ["queue-event-log.py", "--once"]),
                       ("dispatch-ack-reconcile", ["dispatch-ack-reconcile.py", "--dry-run"]),
                       ("dispatch-escalation-watcher", ["dispatch-escalation-watcher.py", "--once"]),
                       ("verify-orphan-reap", ["verify-orphan-reap.py"]),
                       ("log-rotate", ["log-rotate.py"]),
                       ("cpu-prefetch", ["cpu-prefetch.py", "--once"])):
        rc, out = sh([sys.executable, str(CODE / argv[0])] + argv[1:], timeout=90)
        if rc != 0 or "Traceback" in out:
            bad.append(f"{name} rc={rc}")
    return (not bad, ", ".join(bad))


@guard("P03 hold view: container path (status-service view) equals the direct log parse on 7 scenarios")
def p03():
    bad = []
    with Agent() as ag:
        for name, lines, age, cause, held in SCENARIOS:
            write_log(LOG, lines, age)
            direct = q().darkbloom_view(log_path=str(LOG))
            ag.view = {k: v for k, v in direct.items() if k != "at"}
            via = q().darkbloom_view()
            if (direct.get("cause"), direct.get("hold")) != (cause, held):
                bad.append(f"{name}: direct {direct.get('cause')},{direct.get('hold')}")
            if (via.get("cause"), via.get("hold")) != (direct.get("cause"), direct.get("hold")):
                bad.append(f"{name}: agent path differs")
    return (not bad, "; ".join(bad))


@guard("P04 stale or unreadable evidence fails open (stale log, stale view, no log, dead agent)")
def p04():
    bad = []
    m = q()
    write_log(LOG, SCENARIOS[0][1], age_s=m.DB_HOLD_STALE_S * 3 + 60)
    if m.darkbloom_view(log_path=str(LOG)).get("hold"):
        bad.append("stale log still held")
    old = {"cause": "sandbox-hold", "hold": True, "at": time.time() - m.DB_VIEW_FRESH_S - 30}
    if m.darkbloom_hold_wait("m", view=old) is not None:
        bad.append("stale view gave a wait note")
    if m.darkbloom_cause_phrase(view=old) != "":
        bad.append("stale view gave a cause phrase")
    v = m.darkbloom_view(log_path=str(TMP / "absent.log"))
    if not (v.get("unreadable") and not v.get("hold")):
        bad.append("missing log not unreadable+not held")
    os.environ["QUEUE_AGENT_URL"] = "http://127.0.0.1:9"       # nothing listens
    os.environ["QUEUE_AGENT_TOKEN"] = FAKE_TOK
    try:
        v = m.darkbloom_view()
        if not (v.get("unreadable") and not v.get("hold")):
            bad.append("dead agent not unreadable+not held")
    finally:
        os.environ.pop("QUEUE_AGENT_URL"); os.environ.pop("QUEUE_AGENT_TOKEN")
    return (not bad, "; ".join(bad))


@guard("P05 CPU-lane hold flips within the 10 s cache TTL, both ways (fake status service)")
def p05():
    import cpu_lane
    m = q()
    with Agent() as ag:
        cpu_lane.HOLD_VIEW = m.darkbloom_view
        try:
            ag.view = {"cause": None, "hold": False}
            cpu_lane.model_hold(fresh=True)
            res = []
            for want, view in ((True, {"cause": "sandbox-hold", "hold": True}), (False, {"cause": None, "hold": False})):
                ag.view = view
                t = time.monotonic()
                got = None
                while time.monotonic() - t < cpu_lane.HOLD_TTL_S + 0.5:
                    got = cpu_lane.model_hold()[0]
                    if got == want:
                        break
                    time.sleep(0.25)
                res.append(got == want)
            return (all(res), f"flip results {res}")
        finally:
            cpu_lane.HOLD_VIEW = None


@guard("P06 (reduced) a fresh hold makes the queue yield: INFRA WAIT note + sandbox-hold incident")
def p06():
    m = q()
    bad = []
    v = {"cause": "sandbox-hold", "hold": True, "at": time.time(), "sandbox_jobs": 1}
    note = m.darkbloom_hold_wait("some-model", view=v)
    if not note or "darkbloom-sandbox" not in json.dumps(note, default=str):
        bad.append("no wait note")
    m._darkbloom_url = lambda: "http://127.0.0.1:9"
    m.prime_darkbloom_view(view=dict(v), fetch=lambda: [])
    inc = Path(os.environ["INFRA_INCIDENTS_PATH"])
    if not inc.exists() or "sandbox-hold" not in inc.read_text():
        bad.append("no sandbox-hold incident")
    return (not bad, "; ".join(bad))


WORKER = r"""
import importlib.util, sys
spec = importlib.util.spec_from_file_location("oqw", sys.argv[1] + "/ollama-queue.py")
m = importlib.util.module_from_spec(spec); sys.modules["oqw"] = m; spec.loader.exec_module(m)
for _ in range(int(sys.argv[2])):
    with m._Locked() as lk:
        s = lk.load(); s["n"] = s.get("n", 0) + 1; lk.save(s)
"""


@guard("P11 concurrent state writers lose no update, leave valid JSON and no corrupt-* file")
def p11():
    sp = STATE / "ollama-queue-state.json"
    sp.write_text(json.dumps({"jobs": [], "n": 0}))
    procs = [subprocess.Popen([sys.executable, "-c", WORKER, str(CODE), "40"], env=BASE_ENV,
                              stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL) for _ in range(4)]
    rcs = [p.wait(timeout=120) for p in procs]
    n = json.loads(sp.read_text()).get("n")
    corrupt = list(STATE.glob("ollama-queue-state.corrupt-*"))
    return (rcs == [0] * 4 and n == 160 and not corrupt, f"rcs={rcs} n={n} corrupt={len(corrupt)}")


@guard("P13 private-info scan passes over the shipped code")
def p13():
    scan = Path(__file__).resolve().parent / "scan-private-info.py"
    allow = scan.parent / "scan-allow.txt"
    files = sorted(str(p) for p in CODE.glob("*.py")) + sorted(str(p) for p in CODE.glob("*.sh"))
    args = [sys.executable, str(scan), "--base", str(CODE)]
    if allow.exists():
        args += ["--allow", str(allow)]
    rc, out = sh(args + [str(Path(f).name) for f in files if Path(f).name.startswith(("ollama-queue", "queue_", "cpu_", "notify"))], timeout=120)
    return (rc == 0, out[-300:])


@guard("P15 API auth: LAN peer needs the bearer on writes, loopback and GETs unchanged, runner token separate")
def p15():
    t = CODE / "test-queue-api-auth.py"
    if not t.exists():
        return (False, "test-queue-api-auth.py not in the image")
    rc, out = sh([sys.executable, str(t)], timeout=300)
    m = re.findall(r"(\d+) checks?, (\d+) failed", out)
    return (rc == 0, (m[-1] if m else out[-200:]))


@guard("P16 (reduced) kill -9 of writers leaves valid state JSON and a sound CPU-lane sqlite; every.sh stops on TERM")
def p16():
    bad = []
    sp = STATE / "ollama-queue-state.json"
    sp.write_text(json.dumps({"jobs": [], "n": 0}))
    w = subprocess.Popen([sys.executable, "-c", WORKER, str(CODE), "100000"], env=BASE_ENV,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    lane = TMP / "lane"
    lw = subprocess.Popen([sys.executable, "-c",
                           "import sys; sys.path.insert(0, sys.argv[1]); import cpu_lane\n"
                           "s = cpu_lane.Store(sys.argv[2])\n"
                           "while True: s.create({'kind': 'x', 'n': 1})", str(CODE), str(lane)],
                          env=BASE_ENV, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    time.sleep(2.5)
    for p in (w, lw):
        p.send_signal(signal.SIGKILL)
        p.wait()
    try:
        if json.loads(sp.read_text()).get("n", 0) < 1:
            bad.append("state writer never ran")
    except Exception as e:  # noqa: BLE001
        bad.append(f"state json: {e}")
    c = sqlite3.connect(str(lane / "jobs.sqlite"))
    ic = c.execute("PRAGMA integrity_check").fetchone()[0]
    if c.execute("SELECT count(*) FROM jobs").fetchone()[0] < 1:
        bad.append("lane writer never ran")
    c.close()
    if ic != "ok":
        bad.append(f"sqlite {ic}")
    ev = Path(__file__).resolve().parent / "every.sh"
    if not ev.exists():
        ev = Path("/usr/local/bin/every.sh")
    if ev.exists():
        p = subprocess.Popen(["bash", str(ev), "1", "sleep", "30"], env=dict(BASE_ENV, QUEUE_STANDBY="0"),
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        time.sleep(1.0)
        p.send_signal(signal.SIGTERM)
        try:
            rc = p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill(); rc = "hung"
        if rc != 0:
            bad.append(f"every.sh rc={rc}")
    return (not bad, "; ".join(bad))


@guard("STANDBY writes answer 503, safe import refuses bad archives, nothing is sent to the Mac (99 checks)")
def standby():
    t = CODE / "test-queue-standby.py"
    if not t.exists():
        return (False, "test-queue-standby.py not in the image")
    p = subprocess.run([sys.executable, str(t)], capture_output=True, text=True, timeout=300, env=BASE_ENV)
    # the API's request log goes to stderr; a failure is reported by the test's own FAIL lines (with got/want)
    fails = [ln for ln in (p.stdout or "").splitlines() if ln.startswith(("FAIL ", "     got=", "    want="))]
    detail = "\n".join(fails[-15:]) or (p.stdout or "")[-400:] or (p.stderr or "")[-400:]
    return (p.returncode == 0, detail)


def main():
    for fn in (p02, p03, p04, p05, p06, p11, p13, p15, p16, standby):
        fn()
    with contextlib.suppress(Exception):
        import shutil
        shutil.rmtree(TMP, ignore_errors=True)
    print(f"{'FAIL' if FAILS else 'OK'}: {len(FAILS)} failed ({', '.join(FAILS)}) in {time.time() - T0:.0f}s" if FAILS
          else f"OK: all parity checks passed in {time.time() - T0:.0f}s")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
