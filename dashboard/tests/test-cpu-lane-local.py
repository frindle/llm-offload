#!/usr/bin/env python3
"""CPU LANE panel truthfulness (the owner 2026-10-09): the panel showed 'idle' forever because every recent stage
was routed to the Mac (ineligible for the Unraid runner) and the Mac-side work was invisible.

Proves, against the REAL cpu_stage.py + the REAL dashboard source:
  * a stage that falls back to local registers a DISPLAY-ONLY 'running locally' record while local_fn runs
    (stage, bundle, elapsed, short ineligibility reason) and removes it afterwards -- and that record never
    reaches the scheduling store (cpu_lane outstanding_by_bundle stays empty, no local marker job);
  * a stale record (dead pid / old heartbeat) is ignored and reaped, so a crashed stage cannot show
    'running' forever;
  * why_local() groups the last N lane-log routings by reason;
  * GET /api/cpu-lane (real Handler) carries local_running + why_local;
  * the page's laneNowRows()/laneNowHtml() render three states: running on the runner, RUNNING LOCALLY, idle.
Run: python3 tests/test-cpu-lane-local.py [--src API_PY] [--bin BIN_DIR]    (exit 0 = ALL PASSED)
"""
import os
import http.client, importlib.util, json, os, re, subprocess, sys, tempfile, threading, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = os.environ["HOME"]
A = sys.argv
_REPO_SRC = HERE.parent / "src" / "ollama-queue-api.py"      # when run from the dashboard repo's tests/
SRC = (Path(A[A.index("--src") + 1]) if "--src" in A
       else _REPO_SRC if _REPO_SRC.exists() else HERE / "ollama-queue-api.py")   # ~/bin copy: the API is a sibling (symlink)
BIN = Path(A[A.index("--bin") + 1]) if "--bin" in A else Path(REAL_HOME) / "bin"
FAILS = []

HOME = tempfile.mkdtemp(prefix="cpu-lane-local-")
os.environ["HOME"] = HOME
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
for k in ("CPU_LANE", "CPU_LANE_LOG_OFF", "CPU_LANE_LOCAL_MARKER", "CPU_LANE_LOCAL_RUN_DIR"):
    os.environ.pop(k, None)
os.symlink(str(BIN), os.path.join(HOME, "bin"))
sys.path.insert(0, str(BIN))


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def node(js):
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    return json.loads(out.stdout) if out.returncode == 0 else {"err": out.stderr[-400:]}


def main():
    import cpu_stage
    wt = Path(HOME) / "wt-py-bundle"            # no package.json -> ineligible -> local fallback
    wt.mkdir()
    seen = {}

    def local_fn():
        seen["during"] = cpu_stage.local_running()
        try:
            import cpu_lane
            seen["outstanding"] = cpu_lane.get_store().outstanding_by_bundle()
        except Exception as e:                  # noqa: BLE001
            seen["outstanding"] = "err %r" % (e,)
        time.sleep(0.05)
        return (0, "", "", False)

    r = cpu_stage.run_stage(str(wt), "bash verify.sh", 60, stage="relevance", bundle_id="aw-codec-floor",
                            local_fn=local_fn)
    check("stage ran locally", r.ran_on, "local")
    d = seen.get("during") or []
    check("running-locally record visible while local_fn runs",
          [(x["stage"], x["bundle"]) for x in d], [("relevance", "aw-codec-floor")])
    check("record carries the short ineligibility reason",
          d[0]["reason"] if d else None, "not a node project: no root-level *.py, so runner parity cannot be checked")
    check("record has an elapsed time", isinstance(d[0]["running_s"], float) if d else False, True)
    check("record removed after the stage", cpu_stage.local_running(), [])
    check("record carries path=local and the owning pid", (d[0].get("path"), d[0].get("pid")) if d else None,
          ("local", os.getpid()))
    _starts = []
    try:
        for _l in Path(cpu_stage.LOG_FILE).read_text().splitlines():
            _r = json.loads(_l)
            if _r.get("event") == "local-start":
                _starts.append(_r)
    except OSError:
        pass
    check("fallback REASON is logged at stage START (before the stage ends)",
          [(x["stage"], x["bundle"], "not a node project" in x["reason"]) for x in _starts],
          [("relevance", "aw-codec-floor", True)])
    check("start event has no `path` key (why_local ignores it)", all("path" not in x for x in _starts), True)
    check("display record is NOT scheduling state (no outstanding cpu work for the bundle)",
          seen.get("outstanding"), {})

    # stale-proof: dead pid and old heartbeat are ignored + reaped
    rd = cpu_stage._local_run_dir()
    rd.mkdir(parents=True, exist_ok=True)
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    for name, pid, age in (("dead", dead.pid, 0), ("oldhb", os.getpid(), 3600)):
        p = rd / (name + ".json")
        p.write_text(json.dumps({"pid": pid, "stage": "x-" + name, "bundle": "b", "reason": "r",
                                 "started": time.time(), "timeout_s": 600}))
        os.utime(p, (time.time() - age, time.time() - age))
    check("stale records (dead pid / dead heartbeat) are not shown", cpu_stage.local_running(), [])
    check("stale records are reaped", sorted(x.name for x in rd.glob("*.json")), [])
    live = rd / "live.json"
    live.write_text(json.dumps({"pid": os.getpid(), "stage": "lv", "bundle": "b", "reason": "ineligible: x",
                                "started": time.time() - 5, "timeout_s": 600}))
    check("a live record (alive pid, fresh heartbeat) IS shown", [x["stage"] for x in cpu_stage.local_running()], ["lv"])
    live.unlink()

    # why-local: group the last N routing decisions by reason
    lf = Path(HOME) / ".ollama-dispatch" / "cpu-lane.log"
    mm = "ineligible: verify.test.ts uses node:test mock.module (runner node 22 API differs from host node 26)"
    rows = [{"t": 1, "stage": "old", "path": "local", "reason": "no CPU runner online"}]
    rows += [{"t": 2, "stage": "relevance", "path": "local", "reason": mm}] * 3
    rows += [{"t": 3, "stage": "preflight", "path": "runner", "reason": ""}] * 2
    rows += [{"t": 4, "stage": "relevance", "path": "local",
              "reason": "ineligible: not a node project (no package.json): runner python/toolchain differs from the host"}]
    lf.parent.mkdir(parents=True, exist_ok=True)
    lf.write_text("\n".join(json.dumps(x) for x in rows) + "\ngarbage line\n")
    w = cpu_stage.why_local(6)
    check("why_local counts only the last N routings", w["n"], 6)
    check("why_local groups by reason, most common first",
          [(g["path"], g["reason"], g["count"]) for g in w["groups"]],
          [("local", "node:test mock.module needs node 26, runner has 22", 3), ("runner", "ok", 2),
           ("local", "not a node project: runner python/toolchain differs from the host", 1)])

    # the real HTTP route
    ld = SourceFileLoader("qapi_cpulocal", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("qapi_cpulocal", ld))
    ld.exec_module(m)
    srv = m.ThreadingServer(("127.0.0.1", 0), m.Handler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    rd.joinpath("live.json").write_text(json.dumps({"pid": os.getpid(), "stage": "lv2", "bundle": "bb",
                                                    "reason": mm, "started": time.time() - 3, "timeout_s": 600}))
    c = http.client.HTTPConnection("127.0.0.1", srv.server_address[1], timeout=20)
    c.request("GET", "/api/cpu-lane")
    j = json.loads(c.getresponse().read() or b"{}")
    srv.shutdown(); srv.server_close()
    check("/api/cpu-lane exposes local_running", [(x["stage"], x["bundle"], x["reason"]) for x in j.get("local_running", [])],
          [("lv2", "bb", "node:test mock.module needs node 26, runner has 22")])
    check("/api/cpu-lane exposes why_local", (j.get("why_local") or {}).get("n"), 7)   # 6 seeded + the stage this test ran

    # the page
    html = m.FRONTEND_HTML
    mm_ = re.search(r"const NOW_LANES = .*?function laneNowHtml\(rows\) \{.*?\n\}\n", html, re.S)
    check("lane helpers present", bool(mm_), True)
    check("CPU lane panel has a 'why local' block", 'id="cpuWhyLocal"' in html, True)
    check("refreshCpuLane captures local stages for the tile", "window.cpuLaneLocal = d.local_running" in html, True)
    check("refreshCpuLane renders the why-local summary", "Why local (last" in html, True)
    if not mm_:
        return
    pre = ("const escapeHtml=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');"
           "const formatElapsed=s=>s==null?'?':Math.round(s)+'s';\n" + mm_.group(0) + "\n")
    loc = [{"stage": "relevance", "bundle": "aw-codec-floor", "running_s": 41,
            "reason": "node:test mock.module needs node 26, runner has 22"}]
    run = [{"id": "c1234567", "stage": "lint", "runner": "unraid-cpu-1", "running_s": 12, "label": "cpu-stage"}]
    out = node(pre + "const L=" + json.dumps(loc) + ",R=" + json.dumps(run) + ";"
               "const idle=laneNowRows([],[],[],[]), rem=laneNowRows([],[],R,[]), lo=laneNowRows([],[],[],L);"
               "console.log(JSON.stringify({idle:laneNowHtml(idle), rem:laneNowHtml(rem), lo:laneNowHtml(lo),"
               "loRow:lo[2].jobs, idleCpu:idle[2].idle, remLocal:rem[2].jobs.map(x=>!!x.local)}));")
    check("state 1: idle says idle", "CPU lane</span>: idle" in out.get("idle", ""), True)
    check("state 2: runner job still shows its runner", "unraid-cpu-1" in out.get("rem", ""), True)
    check("state 2: runner job is not flagged local", out.get("remLocal"), [False])
    row = (out.get("loRow") or [{}])[0]
    check("state 3: a local-only stage is NOT idle", "CPU lane</span>: idle" in out.get("lo", "idle"), False)
    check("state 3: shows stage, bundle, elapsed",
          (row.get("label"), row.get("bundle"), row.get("elapsed_s")), ("relevance", "aw-codec-floor", 41))
    check("state 3: says RUNNING LOCALLY with the reason", row.get("line"),
          "RUNNING LOCALLY - local: node:test mock.module needs node 26, runner has 22")
    check("state 3: rendered html is flagged local + names the Mac", 'lane-card run local' in out.get("lo", "")
          and "Mac (local" in out.get("lo", ""), True)
    # default (no 4th arg) reads window.cpuLaneLocal -- the tile call sites rely on it
    d2 = node(pre + "global.window={cpuLaneLocal:" + json.dumps(loc) + "};"
              "console.log(JSON.stringify(laneNowRows([],[],[])[2].jobs.length));")
    check("tile call sites (3 args) pick up window.cpuLaneLocal", d2, 1)


try:
    main()
except Exception as e:
    import traceback; traceback.print_exc(); FAILS.append(f"crashed: {e}")
print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nALL PASSED")
sys.exit(1 if FAILS else 0)
