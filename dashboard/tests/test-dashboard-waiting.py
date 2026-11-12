#!/usr/bin/env python3
"""WAITING is visible (the owner 2026-10-10, rt-amazon-cardlink): the author job sat PENDING while
the queue waited on Darkbloom (INFRA WAIT), yet the slice row said a green "authoring", the
lane cards said plain "idle", and the tiles said "Nothing on the GPU / Queue empty".
Runs the page's own functions under node against fake state:
  * sliceWaitInfo: a slice whose only live row is PENDING -> waiting + the wait reason
    (daemon queue_wait.short beats wait_reason); a running row -> null (still "authoring");
  * the slice line renders the 'waiting' chip + 'waiting: <why>' instead of sl.phase;
  * laneWaitText / idle lane cards: 'STUDIO-DB: waiting - <infra reason>' not 'idle';
    a truly idle lane stays 'idle'; CPU lane waits on pending stages;
  * idleWaitText: names the job-level reason; a driver-active slice with NO queue row is
    never 'Queue empty'; a truly empty queue still is;
  * the Queue tile subtitle counts driver-active slices; pending still counts.
Run: python3 tests/test-dashboard-waiting.py [--src PATH] [--mutants]"""
import importlib.util, json, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = os.environ["HOME"]
FAILS = []
DEFAULT_SRC = HERE.parent / "src" / "ollama-queue-api.py"

MUTANTS = [
    ("slice wait ignores pending rows", "if (c.status === 'pending' && !pend) pend = c;", "if (false) pend = c;"),
    ("slice wait ignores a running row", "if (c.status === 'running') return null;", ""),
    ("daemon reason not preferred", "return (j.queue_wait && j.queue_wait.short) || j.wait_reason || '';", "return j.wait_reason || '';"),
    ("slice chip still says phase", "if (wi) return {phase: 'waiting',", "if (false) return {phase: 'waiting',"),
    ("slice detail omits the reason", "detail: 'waiting: ' + wi.reason}", "detail: ''}"),
    ("slice line ignores the chip info", "const sPhase = sc.phase, sCls = sc.cls, sDetail = sc.detail;", "const sPhase = sl.phase, sCls = sl.phase, sDetail = sl.detail;"),
    ("idle lane ignores the daemon lane reason", "if (s) return 'waiting - ' + String(s)", "if (false) return 'waiting - ' + String(s)"),
    ("idle lane ignores pending rows", "return pj ? 'waiting - '", "return false ? 'waiting - '"),
    ("lane card never prints the wait", "${r.wait ? '<span class=\"lane-wait\">'", "${false ? '<span class=\"lane-wait\">'"),
    ("lane rows drop the wait", "laneWaitText(k, wv, jobs, cpuPend)}", "''}"),
    ("cpu lane never waits", "if (lane === 'cpu') return cpuPending ?", "if (lane === 'cpu') return false ?"),
    ("driver-active slice reads Queue empty", "if (dr.length) return 'Bundle '", "if (false) return 'Bundle '"),
    ("driver-active counts slices with a live row", "if ((sl.history || []).some(h => h.live)) continue;", ""),
    ("job-level reason dropped from the idle tile", "if (jw && jw !== 'next in line') return", "if (false) return"),
    ("queue tile note dropped", "function driverNote(n) { return n ? ", "function driverNote(n) { return false ? "),
    ("queue tile never appends the note", "${driverNote(drv.length)}</div>`;", "</div>`;"),
]


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def html_of(src):
    home = tempfile.mkdtemp(prefix="dash-wait-")
    os.environ["HOME"] = home
    os.symlink(os.environ.get("OLLAMA_PIPELINE_BIN", str(Path(REAL_HOME) / "bin")), os.path.join(home, "bin"))
    ld = SourceFileLoader("qapi_wait", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("qapi_wait", ld))
    ld.exec_module(m)
    return m.FRONTEND_HTML


def grab(html, name):
    m = re.search(r"^(?:async )?function " + name + r"\(.*?\n}\n", html, re.S | re.M)
    return m.group(0) if m else None


def run(src):
    html = html_of(src)
    names = ["jobWaitText", "driverActiveSlices", "sliceWaitInfo", "sliceChipInfo", "driverNote", "laneWaitText", "laneOfJob", "idleWaitText",
             "cpuQueueRows", "cpuQueueCounts", "laneNowRows", "autoRunHtml", "laneNowHtml", "escapeHtml", "formatElapsed"]
    parts = {n: grab(html, n) for n in names}
    for n, p in parts.items():
        check(f"{n} present", bool(p), True)
    if not all(parts.values()):
        return
    lanes = re.search(r"^const NOW_LANES = .*$", html, re.M).group(0)
    prelude = ("let queueWait = null; globalThis.window = {cpuLaneData: null, cpuLaneRunning: [], cpuLaneLocal: []};\n"
               + lanes + "\n" + "\n".join(parts.values()))
    pend = {"id": "6e6944f9ab84", "status": "pending", "lane": "studio-db", "label": "auto-author-x-s1",
            "wait_reason": "next in line",
            "queue_wait": {"short": "darkbloom restoring/re-warming qwen3.6-35b-a3b-vl-mtp-mxfp8", "code": "infra-darkbloom"}}
    run_ = dict(pend, status="running", queue_wait=None)
    sl = {"sid": "s1", "phase": "authoring", "active": True, "history": [{"id": "6e6944f9ab84", "live": True, "status": "pending"}]}
    infra = {"source": "state", "lanes": {"studio-db": {"state": "idle", "pending": 1,
             "reason": {"sentence": "INFRA WAIT: bloom-control switching Darkbloom onto the queue's pair"}},
             "unraid": {"state": "idle", "pending": 0}}}
    drv_views = {"views": {"rt-amazon-cardlink": {"slices": [
        {"sid": "s1", "phase": "authoring", "active": True, "attention": False, "history": []}]}}}
    tests = {
        "slice_pending": "sliceWaitInfo(%s, [%s])" % (json.dumps(sl), json.dumps(pend)),
        "slice_pending_no_daemon_reason": "sliceWaitInfo(%s, [%s])" % (json.dumps(sl), json.dumps(dict(pend, queue_wait=None))),
        "slice_running": "sliceWaitInfo(%s, [%s])" % (json.dumps(sl), json.dumps(run_)),
        "slice_pend_and_running": "sliceWaitInfo(%s, [%s, %s])" % (
            json.dumps(dict(sl, history=sl["history"] + [{"id": "r1", "live": True}])), json.dumps(pend), json.dumps(dict(run_, id="r1"))),
        "chip_wait": "sliceChipInfo(%s, [%s])" % (json.dumps(dict(sl, detail="x")), json.dumps(pend)),
        "chip_plain": "sliceChipInfo(%s, [%s])" % (json.dumps(dict(sl, detail="refining (round 2) -- foo")), json.dumps(run_)),
        "note1": "driverNote(1)", "note0": "driverNote(0)",
        "lane_infra": "laneWaitText('studio-db', %s, [], 0)" % json.dumps(infra),
        "lane_idle": "laneWaitText('unraid', %s, [], 0)" % json.dumps(infra),
        "lane_from_rows": "laneWaitText('studio-db', null, [%s], 0)" % json.dumps(pend),
        "lane_cpu": "laneWaitText('cpu', null, [], 2)",
        "lane_cpu_idle": "laneWaitText('cpu', null, [], 0)",
        "cards": "laneNowHtml(laneNowRows([%s], [], [], [], %s))" % (json.dumps(pend), json.dumps(infra)),
        "tile_job_reason": "idleWaitText(null, [%s], null, null)" % json.dumps(pend),
        "tile_driver": "idleWaitText(null, [], null, %s)" % json.dumps(drv_views),
        "tile_empty": "idleWaitText(null, [], null, {views: {}})",
        "tile_done_driver_has_row": "idleWaitText(null, [], null, %s)" % json.dumps(
            {"views": {"k": {"slices": [{"sid": "s", "phase": "coding", "active": True, "history": [{"live": True}]}]}}}),
    }
    js = prelude + "\nconst T=" + "{" + ",".join(f"{k}:()=>{v}" for k, v in tests.items()) + "};\n" \
        "const out={};for(const k in T){try{out[k]=T[k]();}catch(e){out[k]='ERR '+e.message;}}console.log(JSON.stringify(out));"
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    if r.returncode != 0:
        check("node ran", r.stderr[-400:], "")
        return
    o = json.loads(r.stdout)
    reason = "darkbloom restoring/re-warming qwen3.6-35b-a3b-vl-mtp-mxfp8"
    check("slice with only a pending row is WAITING with the daemon reason", o["slice_pending"], {"reason": reason, "id": "6e6944f9ab84"})
    check("falls back to wait_reason", o["slice_pending_no_daemon_reason"]["reason"], "next in line")
    check("a running row keeps the slice 'authoring'", o["slice_running"], None)
    check("pending + running together is not waiting", o["slice_pend_and_running"], None)
    check("slice chip: waiting + reason, not the phase", o["chip_wait"], {"phase": "waiting", "cls": "queued", "detail": "waiting: " + reason})
    check("running slice keeps its own phase/detail", o["chip_plain"], {"phase": "authoring", "cls": "authoring", "detail": "foo"})
    check("queue tile note names driver-active slices", o["note1"], " &middot; 1 driver-active (no queue row yet)")
    check("queue tile note absent at 0", o["note0"], "")
    check("studio-db lane: infra reason, INFRA WAIT prefix stripped", o["lane_infra"],
          "waiting - bloom-control switching Darkbloom onto the queue's pair")
    check("lane with no pending stays plain idle", o["lane_idle"], "")
    check("lane waits from its pending rows", o["lane_from_rows"], "waiting - " + reason)
    check("cpu lane waits on pending stages", o["lane_cpu"], "waiting - 2 stages for a runner")
    check("cpu lane idle", o["lane_cpu_idle"], "")
    cards = o["cards"]
    check("studio-db card says waiting + why", "studio-db</span>: <span class=\"lane-wait\">waiting - bloom-control" in cards, True)
    check("unraid card still says idle", 'data-lane="unraid"><span class="lane-name">unraid</span>: idle</div>' in cards, True)
    check("idle tile names the job-level reason", o["tile_job_reason"], "Waiting on: " + reason)
    check("driver-active slice is never 'Queue empty'", o["tile_driver"],
          "Bundle rt-amazon-cardlink: authoring in progress (driver working, queue job not enqueued yet)")
    check("truly empty queue still reads empty", o["tile_empty"], "Queue empty")
    check("slice that HAS a live row is not driver-only", o["tile_done_driver_has_row"], "Queue empty")
    # render wiring (source-level: the DOM render itself needs the full page)
    check("tile passes bundleViews to idleWaitText", "idleWaitText(queueWait, jobs, bundleViews.active, bundleViews)" in html, True)
    check("queue tile subtitle counts driver-active slices", "driver-active (no queue row yet)" in html, True)
    check("slice line renders sliceChipInfo", "const sc = sliceChipInfo(sl, g.children);" in html, True)
    check("slice line draws the chip info", "const sPhase = sc.phase, sCls = sc.cls, sDetail = sc.detail;" in html, True)
    check("queue tile appends the driver note", "${driverNote(drv.length)}</div>`;" in html, True)


def main():
    src = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else DEFAULT_SRC
    if "--mutants" in sys.argv:
        text = src.read_text()
        bad = 0
        for name, old, new in MUTANTS:
            if text.count(old) != 1:
                print(f"MUTANT-STALE: {name} (anchor count {text.count(old)})"); bad += 1; continue
            td = Path(tempfile.mkdtemp())
            for sib in src.parent.glob("*.py"):      # the api imports its sibling modules
                if sib != src:
                    (td / sib.name).write_text(sib.read_text())
            tmp = td / "m.py"
            tmp.write_text(text.replace(old, new))
            r = subprocess.run([sys.executable, __file__, "--src", str(tmp)], capture_output=True, text=True)
            killed = r.returncode != 0
            print(("killed  " if killed else "SURVIVED") + f": {name}")
            bad += (not killed)
        print("MUTANTS OK" if not bad else f"{bad} mutant problem(s)")
        sys.exit(1 if bad else 0)
    run(src)
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)


main()
