#!/usr/bin/env python3
"""Run-status must not call a LIVE auto run's authoring rows BLOCKED (the owner 2026-10-11), and a
bundle-less live auto run must be visible (lane row, idle line, activity).
Fake state files + injected alive/cmdline: live => IN PROGRESS and not clearable; dead pid =>
BLOCKED with the reason; pid reused by an unrelated process => BLOCKED; ended outcome => the old
BLOCKED text unchanged; dashboard lane/idle text shows the driver.
Run: python3 test-runstatus-autolive.py [--src PATH]"""
import importlib.util, json, os, subprocess, sys, tempfile, re
from importlib.machinery import SourceFileLoader
from pathlib import Path

REAL_HOME = os.environ["HOME"]
FAILS = []
OK_MARK = "RUNSTATUS_AUTOLIVE_OK"


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(src):
    home = tempfile.mkdtemp(prefix="autolive-")
    os.environ["HOME"] = home
    os.symlink(os.environ.get("OLLAMA_PIPELINE_BIN", str(Path(REAL_HOME) / "bin")), os.path.join(home, "bin"))
    ld = SourceFileLoader("qapi_autolive", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("qapi_autolive", ld))
    ld.exec_module(m)
    return m


def state(d, label, pid, phase="advancing", outcome=None, step="author stage refimpl try 0"):
    rec = {"key": label, "label": label, "bundle": None, "pid": pid, "started_at": "2026-10-12T04:37:09Z",
           "phase": phase, "phase_since": "2026-10-12T04:52:15Z", "step": step, "job": "j2",
           "rounds": ["j1", "j2"], "outcome": outcome, "updated_at": "2026-10-12T04:52:15Z"}
    (d / f"{label}.json").write_text(json.dumps({"runs": {label: rec}, **rec}))


def rows():
    return [{"id": "j1", "label": "auto-author-feat-s1", "awaiting_signoff": False, "has_gate": True,
             "raw_verdict": "PASS", "verdict": "PASS (pending review)", "auto_pipeline_stage": "author",
             "timestamp": "2026-10-12T04:40:00Z"},
            {"id": "j2", "label": "auto-author-feat-s2", "awaiting_signoff": False, "has_gate": True,
             "raw_verdict": "PASS", "verdict": "PASS (pending review)", "auto_pipeline_stage": "author",
             "timestamp": "2026-10-12T04:50:00Z"}]


def main():
    src = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else Path(REAL_HOME) / "bin" / "ollama-queue-api.py"
    m = load(src)
    ALIVE = lambda pid: pid in (111, 222)
    CMD = lambda pid: {111: "/usr/bin/python ollama-dispatch-auto --label feat --repo x",
                       222: "/usr/bin/vim notes.txt"}.get(pid, "")

    def annotate(label="feat", pid=111, **kw):
        d = Path(tempfile.mkdtemp(prefix="ar-"))
        state(d, label, pid, **kw)
        auto = m._auto_run_states(runs_dir=d, alive=ALIVE, cmdline=CMD)
        rs = rows()
        m._annotate_run_groups(rs, [], auto_runs=auto)
        m._annotate_run_parents(rs)
        m._annotate_eyes_labels(rs)
        return rs, auto, d

    rs, auto, d = annotate()
    check("live run: both authoring rows read IN PROGRESS", [r["verdict"].split(":")[0] for r in rs],
          ["IN PROGRESS -- authoring done, auto run advancing"] * 2)
    check("live run: step is shown", rs[0]["verdict"].endswith("author stage refimpl try 0"), True)
    check("live run: not red (no blocked_reason, raw IN PROGRESS, not needs_eyes)",
          [(r.get("blocked_reason"), r["raw_verdict"], r["group"]) for r in rs], [(None, "IN PROGRESS", "handled")] * 2)
    check("live run: rows flagged not clearable", [r.get("auto_run_live") for r in rs], [True, True])
    check("live run listed by _live_auto_runs", [r["label"] for r in m._live_auto_runs(runs_dir=d, alive=ALIVE, cmdline=CMD)], ["feat"])
    # clearing a live run's row is refused with 409 (does not touch sidecars)
    os.environ["OLLAMA_DISPATCH_AUTO_RUNS_DIR"] = str(d)
    orig_alive, orig_cmd, orig_load = m._pid_alive, m._pid_cmdline, m.q._load_job_result
    m._pid_alive, m._pid_cmdline = ALIVE, CMD
    m.q._load_job_result = lambda jid, ld: {"label": "auto-author-feat-s1"}
    try:
        res = m._archive_run("j1")
    finally:
        m._pid_alive, m._pid_cmdline, m.q._load_job_result = orig_alive, orig_cmd, orig_load
    check("clear of a live run's row is refused 409", (res.get("ok"), res.get("code")), (False, 409))

    rs, auto, _ = annotate(pid=999)
    check("dead pid (phase still advancing): BLOCKED", [r["raw_verdict"] for r in rs], ["BLOCKED", "BLOCKED"])
    check("dead pid: reason from the state file", "died at step 'author stage refimpl try 0' (pid 999 is gone)" in rs[0]["blocked_reason"], True)
    check("dead pid: red severity badge", rs[0]["eyes_severity"], "bad")
    check("dead pid is not listed live", [a["live"] for a in auto], [False])

    rs, auto, _ = annotate(pid=222)
    check("pid reused by an unrelated process: BLOCKED", [r["raw_verdict"] for r in rs], ["BLOCKED", "BLOCKED"])
    check("pid reuse reason names it", "unrelated process" in rs[0]["blocked_reason"], True)

    rs, auto, _ = annotate(phase="ended", outcome="landed", pid=None)
    check("completed outcome: old BLOCKED text unchanged",
          [r["blocked_reason"] for r in rs], ["authoring stage only, the coding slice never ran"] * 2)
    check("completed outcome is not tracked at all", auto, [])

    rs = rows()
    m._annotate_run_groups(rs, [])
    check("no state files: old behaviour", [r["raw_verdict"] for r in rs], ["BLOCKED", "BLOCKED"])

    # --- dashboard side: lane row, idle line, button, node-executed
    html = m.FRONTEND_HTML
    mm = re.search(r"const NOW_LANES = .*?function laneNowHtml\(rows\) \{.*?\n\}\n", html, re.S)
    mi = re.search(r"function idleWaitText\(.*?\n\}\n", html, re.S)
    check("lane + idle helpers present", bool(mm and mi), True)
    pre = ("const escapeHtml=s=>String(s).replace(/&/g,'&amp;').replace(/</g,'&lt;');"
           "const formatElapsed=s=>s==null?'?':Math.round(s)+'s';\n" + mm.group(0) + "\n" + mi.group(0) + "\n")
    act = {"kind": "auto", "label": "feat", "step": "author stage refimpl try 0", "phase": "advancing", "pid": 111,
           "job": "j2", "bundle": None, "elapsed_s": 60}
    out = subprocess.run(["node", "-e", pre + "const rows=laneNowRows([]," + json.dumps([act]) + ",[]);"
        "const rows2=laneNowRows([{id:'j2',status:'running',lane:'studio-db',label:'x',model:'m'}]," + json.dumps([act]) + ",[]);"
        "const rows3=laneNowRows([{id:'z',status:'running',lane:'unraid-cpu-1',label:'q'}],[Object.assign({},"+json.dumps(act)+",{bundle:'bx'})],[{id:'c1',stage:'refimpl-verify',runner:'unraid-cpu-1',running_s:5,label:'rv',bundle:'bx'}]);"
        "console.log(JSON.stringify({cpu:rows[2],h:laneNowHtml(rows),cpu2:rows2[2].idle,h2:rows2.auto.length,h3:laneNowHtml(rows3),"
        "idle:idleWaitText(null,[],null,{activity:" + json.dumps([act]) + "})}));"],
        capture_output=True, text=True)
    r = json.loads(out.stdout) if out.returncode == 0 else {"err": out.stderr[-300:]}
    check("driver is NOT in the CPU lane; separate AUTO RUN (driver) row names it",
          (r.get("cpu", {}).get("idle"), "CPU lane" in r.get("h", "") and "authoring: feat" in r.get("h", ""),
           "AUTO RUN (driver)" in r.get("h", ""), "orchestrating; work runs as queue jobs" in r.get("h", ""),
           "pid 111" in r.get("h", ""), "driver, not in queue" in r.get("h", "")), (True, False, True, True, True, False))
    check("in-flight queue job named with its host", "rv on host unraid-cpu-1" in r.get("h3", ""), True)
    check("a run whose own job is running is not duplicated in the CPU lane", r.get("cpu2"), True)
    check("own-job-running run has no auto row", r.get("h2"), 0)
    check("idle line says an auto run is working outside the queue",
          r.get("idle", "").startswith("Auto run feat: author stage refimpl try 0 (driver working outside the queue"), True)
    # The Run Status panel, with its per-row and bulk clear buttons, went in 67447e8 (2026-11-01). Rows are cleared from
    # the inline bundle verdicts now, whose clear button renders only for a `clearable` row, and every clear path (that
    # button via /api/runs/<id>/clear, the retention sweep, `qctl runs-clear`) archives through _archive_run.
    rs, _auto, d_live = annotate()
    check("clear button suppressed for a live row: its inline verdict row is not clearable",
          [m._bundle_verdict_index(rs, [])["rows"][r["id"]]["clearable"] for r in rs], [False, False])
    rs, _auto, _ = annotate(pid=999)
    check("...while a dead run's rows stay clearable",
          [m._bundle_verdict_index(rs, [])["rows"][r["id"]]["clearable"] for r in rs], [True, True])
    check("the inline clear button renders only for a clearable row", "if (info.clearable) {" in html, True)
    os.environ["OLLAMA_DISPATCH_AUTO_RUNS_DIR"] = str(d_live)
    orig_alive, orig_cmd, orig_load = m._pid_alive, m._pid_cmdline, m.q._load_job_result
    m._pid_alive, m._pid_cmdline = ALIVE, CMD
    m.q._load_job_result = lambda jid, ld: {"label": "auto-author-feat-s1"}
    try:
        res = m._retention_archive("j1", "harness")
    finally:
        m._pid_alive, m._pid_cmdline, m.q._load_job_result = orig_alive, orig_cmd, orig_load
    check("bulk clear skips live rows: the retention sweep's archive of a live run's row is refused 409",
          (res.get("ok"), res.get("code"), res.get("auto_run_live")), (False, 409, True))
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\n" + OK_MARK)
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
