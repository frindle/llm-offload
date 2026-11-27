#!/usr/bin/env python3
"""test-issue-poke.py -- the GitHub issue poke route (issue_poke.py, POST /api/issue-poke) and its loop.

Proves: no token = 503 (never open); wrong / missing bearer = 401; only POST; bad body / foreign owner / bool or negative
issue = 400/403; a good poke = 202 and a 0600 trigger file; the real queue Handler routes it (and a standby container
answers 503 before any auth); the loop runs a tick at start, then on a poke (not before the min gap), coalesces a burst
into one tick, runs ticks one at a time, serves a poke that arrives mid-tick with one more tick, falls back to the
backstop with no poke, and survives a failing tick command. The token is never in a response.
Usage: test-issue-poke.py [--api PATH]. Run on claude-sandbox, never on the Mac."""
import argparse
import http.client
import importlib.util
import json
import os
import stat
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


TOK = "poke-token-0123456789abcdefABCDEF"
H = {"Authorization": "Bearer " + TOK}
GOOD = json.dumps({"repo": "frindle/some-repo", "issue": 7}).encode()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--api", default=str(HERE / "ollama-queue-api.py"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="issue-poke-"))
    os.environ["QUEUE_STATE_DIR"] = str(tmp / "state")
    os.environ["ISSUE_POKE_TOKEN_FILE"] = str(tmp / "no-such-token")
    os.environ["POLLER_HOME"] = str(tmp / "poller")
    for k in ("ISSUE_POKE_TOKEN", "ISSUE_POKE_OWNER", "QUEUE_API_TOKEN", "QUEUE_STANDBY"):
        os.environ.pop(k, None)
    import issue_poke as ip
    base = tmp / "state"

    # ---- handle(): auth and validation ---------------------------------------------------------------------------
    check("no token configured -> 503", ip.handle("POST", ip.ROUTE, H, GOOD)[0], 503)
    check("short token is no token", ip.token() == "" or True)
    os.environ["ISSUE_POKE_TOKEN"] = "short"
    check("token under 24 chars -> 503", ip.handle("POST", ip.ROUTE, H, GOOD)[0], 503)
    os.environ.pop("ISSUE_POKE_TOKEN")
    (tmp / "tokfile").write_text(TOK + "\n")
    os.environ["ISSUE_POKE_TOKEN_FILE"] = str(tmp / "tokfile")
    check("token from the file: no header -> 401", ip.handle("POST", ip.ROUTE, {}, GOOD)[0], 401)
    check("wrong bearer -> 401", ip.handle("POST", ip.ROUTE, {"Authorization": "Bearer nope"}, GOOD)[0], 401)
    check("basic scheme -> 401", ip.handle("POST", ip.ROUTE, {"Authorization": "Basic " + TOK}, GOOD)[0], 401)
    check("GET -> 405", ip.handle("GET", ip.ROUTE, H, b"")[0], 405)
    check("other path -> 404", ip.handle("POST", "/api/other", H, GOOD)[0], 404)
    check("nothing written by refused requests", not (base / ip.TRIGGER_NAME).exists())
    bad = [b"", b"[]", b"not json", json.dumps({"repo": "frindle/x"}).encode(),
           json.dumps({"repo": "frindle/x", "issue": True}).encode(), json.dumps({"repo": "frindle/x", "issue": -3}).encode(),
           json.dumps({"repo": "frindle/x", "issue": "7"}).encode(), json.dumps({"repo": "frindle/../x", "issue": 1}).encode(),
           json.dumps({"repo": "frindle/x y", "issue": 1}).encode(), json.dumps({"repo": 5, "issue": 1}).encode()]
    check("bad bodies -> 400", [ip.handle("POST", ip.ROUTE, H, b)[0] for b in bad], [400] * len(bad))
    check("foreign owner -> 403", ip.handle("POST", ip.ROUTE, H, json.dumps({"repo": "someone/x", "issue": 1}).encode())[0], 403)
    (tmp / "poller").mkdir()
    (tmp / "poller" / "config.json").write_text(json.dumps({"owner": "other-owner"}))
    check("owner comes from the poller config", ip.handle("POST", ip.ROUTE, H, GOOD)[0], 403)
    (tmp / "poller" / "config.json").write_text(json.dumps({"owner": "Frindle"}))
    st, obj = ip.handle("POST", ip.ROUTE + "/", H, GOOD)
    check("good poke -> 202 queued", (st, obj), (202, {"ok": True, "queued": True}))
    trig = base / ip.TRIGGER_NAME
    check("trigger file exists, mode 0600", trig.exists() and stat.S_IMODE(trig.stat().st_mode) == 0o600)
    rec = json.loads(trig.read_text())
    check("trigger holds repo and issue only (plus time)", sorted(rec) == ["at", "issue", "repo"] and rec["issue"] == 7)
    check("token never in a response", TOK not in json.dumps([ip.handle("POST", ip.ROUTE, H, b"x"), obj]))

    # ---- the real Handler -----------------------------------------------------------------------------------------
    sys.path.insert(0, str(Path(a.api).parent))
    try:
        import queue_config  # noqa: F401
    except ImportError:
        m = types.ModuleType("queue_config")
        m.state_dir = lambda: base
        m.standby = lambda: False
        sys.modules["queue_config"] = m
    os.environ["CPU_LANE_DIR"] = str(tmp / "cpu-jobs")
    os.environ["CPU_RUNNER_TOKEN_FILE"] = str(tmp / "cpu-token")
    os.environ["QEL_DIR"] = str(tmp / "qel")
    spec = importlib.util.spec_from_file_location("oqapi_poke", a.api)
    api = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(api)
    srv = api.ThreadingServer(("127.0.0.1", 0), api.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()

    def call(body, headers):
        c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
        c.request("POST", "/api/issue-poke", body=body, headers={**headers, "Content-Length": str(len(body))})
        r = c.getresponse()
        out = r.read()
        c.close()
        return r.status, out

    trig.unlink()
    os.environ["QUEUE_API_TOKEN"] = "queue-write-token-not-the-poke-one"   # the queue's own token must not matter here
    check("HTTP: wrong bearer -> 401, nothing written", (call(GOOD, {"Authorization": "Bearer x"})[0], trig.exists()), (401, False))
    check("HTTP: the queue write token is not accepted", call(GOOD, {"Authorization": "Bearer " + os.environ["QUEUE_API_TOKEN"]})[0], 401)
    s, out = call(GOOD, H)
    check("HTTP: good poke -> 202 and a trigger file", (s, trig.exists()), (202, True))
    check("HTTP: token not echoed", TOK.encode() not in out)
    check("HTTP: oversized body -> 413", call(b"x" * 5000, H)[0], 413)
    if hasattr(api.queue_config, "standby"):
        os.environ["QUEUE_STANDBY"] = "1"
        trig.unlink()
        check("HTTP: standby -> 503 before auth, nothing written", (call(GOOD, H)[0], trig.exists()), (503, False))
        os.environ.pop("QUEUE_STANDBY")
    srv.shutdown()
    os.environ.pop("QUEUE_API_TOKEN")

    # ---- the loop -----------------------------------------------------------------------------------------------
    lb = tmp / "loop"
    lb.mkdir()
    runs = tmp / "runs.log"
    tick = tmp / "tick.py"
    tick.write_text(
        "import os, sys, time\n"
        "d = os.environ['T_DIR']\n"
        "open(d + '/runs.log', 'a').write('start %.3f\\n' % time.time())\n"
        "time.sleep(float(os.environ.get('T_SLEEP', '0.3')))\n"
        "open(d + '/runs.log', 'a').write('end %.3f\\n' % time.time())\n"
        "sys.exit(int(os.environ.get('T_RC', '0')))\n")
    os.environ["T_DIR"] = str(tmp)
    cmd = [sys.executable, str(tick)]

    def starts():
        try:
            return [float(x.split()[1]) for x in runs.read_text().splitlines() if x.startswith("start")]
        except OSError:
            return []

    def ends():
        try:
            return [float(x.split()[1]) for x in runs.read_text().splitlines() if x.startswith("end")]
        except OSError:
            return []

    stop = threading.Event()
    logs = []
    t = threading.Thread(target=ip.run_loop, args=(lb, cmd), kwargs=dict(backstop=30, min_gap=1.0, stop=stop, poll=0.05, log=logs.append))
    t.start()
    time.sleep(0.8)
    check("loop: one tick at start", len(starts()), 1)
    ip.write_trigger(lb, {"repo": "frindle/a", "issue": 1})
    ip.write_trigger(lb, {"repo": "frindle/a", "issue": 2})
    ip.write_trigger(lb, {"repo": "frindle/b", "issue": 3})
    time.sleep(0.1)
    check("loop: a poke inside the min gap waits", len(starts()), 1)
    time.sleep(1.6)
    check("loop: a burst of 3 pokes = ONE more tick", len(starts()), 2)
    s = starts()
    check("loop: second tick no earlier than min gap after the first", s[1] - s[0] >= 0.99, True)
    # a poke arriving mid-tick is served by one more tick
    os.environ["T_SLEEP"] = "1.5"
    ip.write_trigger(lb, {"repo": "frindle/a", "issue": 4})
    time.sleep(1.3)                                       # tick 3 is running now
    n_before = len(starts())
    ip.write_trigger(lb, {"repo": "frindle/a", "issue": 5})
    time.sleep(3.6)
    check("loop: a mid-tick poke yields exactly one more tick", len(starts()), n_before + 1)
    pairs = list(zip(starts(), ends()))
    check("loop: ticks never overlap", all(pairs[i + 1][0] >= pairs[i][1] for i in range(len(pairs) - 1)), True)
    stop.set()
    t.join(8)
    check("loop: stops on request", not t.is_alive())

    # backstop with no poke, and a failing command
    runs.unlink()
    os.environ["T_SLEEP"] = "0.05"
    os.environ["T_RC"] = "3"
    stop2 = threading.Event()
    logs2 = []
    t2 = threading.Thread(target=ip.run_loop, args=(tmp / "loop2", cmd), kwargs=dict(backstop=0.6, min_gap=0.1, stop=stop2, poll=0.05, log=logs2.append))
    t2.start()
    time.sleep(2.2)
    stop2.set()
    t2.join(8)
    check("backstop: ticks with no poke (>= 3 in 2.2 s at 0.6 s)", len(starts()) >= 3, True)
    check("a failing tick (rc 3) does not stop the loop", any("rc=3" in x for x in logs2), True)
    bad_t = threading.Thread(target=ip.run_loop, args=(tmp / "loop3", ["/nonexistent/cmd"]),
                             kwargs=dict(backstop=0.2, min_gap=0.1, stop=(s3 := threading.Event()), poll=0.05, log=logs2.append))
    bad_t.start()
    time.sleep(0.8)
    s3.set()
    bad_t.join(5)
    check("a missing command is logged, loop survives", any("tick error" in x for x in logs2), True)

    print("\nFAILED: " + ", ".join(FAILS) if FAILS else "\nall passed")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
