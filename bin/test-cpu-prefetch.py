#!/usr/bin/env python3
"""test-cpu-prefetch.py -- cpu-prefetch.py starts the CPU prep of QUEUED work early, safely, bounded.

Hermetic (temp dirs for every state path; the real queue, auto-runs and ~/.ollama-dispatch are never
read or written; the only real processes are throwaway `sleep`/fake-driver children).
  1. add: start vs resume entries (resume reads auto-runs/argv/<label>.json, adds --resume-harness once)
  2. SAFETY gates: hold, holds.txt, blocked/stalled park, needs_opus row, user_hold, open READY-TO-LAND
     row (held HARNESS GO), superseded, slicer-owned bundle, live driver, pid-reuse (a live pid that is
     not a driver does not count), held tree lock, .hand-harness, live row owning the worktree,
     resume without an authored harness; benign parks (cpu_wait/yielded) do not block
  3. BOUNDS: slots math (max_prep, target_ready), load and CPU-lane headroom, nothing launches when
     the GPU runway is already full
  4. ORDER: resumes-first bundle, then kind=resume, priority, FIFO
  5. IDEMPOTENT: a second pass / a concurrent pass never double-launches; a finished driver is
     reconciled done/failed and is NOT relaunched; kill switch; fail closed on unreadable queue state
  6. real spawn: a fake driver is started detached, found by ps by its --label, not relaunched
Exit 0 = all pass (prints CPU_PREFETCH_OK)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("CPU_PREFETCH_SRC") or HERE / "cpu-prefetch.py")
T = Path(tempfile.mkdtemp(prefix="prefetch-test-"))
os.environ.update(CPU_PREFETCH_DIR=str(T / "pf"), OLLAMA_DISPATCH_DIR=str(T / "od"),
                  OLLAMA_DISPATCH_AUTO_RUNS_DIR=str(T / "od" / "auto-runs"),
                  OLLAMA_QUEUE_STATE=str(T / "queue.json"), CPU_PREFETCH_AUTO=str(T / "fake-auto"),
                  HOME=str(T / "home"))
(T / "home").mkdir()
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else " -- got %r, want %r" % (got, want)))
    if not ok:
        FAILS.append(name)


def load():
    s = importlib.util.spec_from_file_location("cpu_prefetch_t", str(SRC))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


pf = load()
CFG = dict(pf.DEFAULTS)


def reset():
    import shutil
    for d in (T / "pf", T / "od"):
        shutil.rmtree(d, ignore_errors=True)
    (T / "od" / "auto-runs").mkdir(parents=True)


def view(jobs=(), parked=None, commit=None):
    return {"jobs": list(jobs), "parked": parked or {}, "commit": commit or {}}


def row(i, label, status, bundle=None, **kw):
    d = {"id": i, "label": label, "status": status, "bundle": bundle or label}
    d.update(kw)
    return d


def mkwt(name, harness=True):
    wt = T / "wt" / name
    wt.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(wt)], check=True)
    if harness:
        for f in pf.HARNESS_FILES:
            (wt / f).write_text("x")
    return str(wt)


NODRV = []        # empty process table: no driver alive
HELP = dict(open_index_rows=lambda k: set(), superseded=lambda k: set(), slicer_owns=lambda b: False,
            tree_lock_held=lambda w: False)


def reason(e, v=None, rows=NODRV, **h):
    return pf.block_reason(e, v or view(), rows, holds=set(), helpers=dict(HELP, **h))


# ---------------------------------------------------------------- 1. add
reset()
e = pf.add_entry("lab-a", ["--repo", "/r", "--label", "lab-a", "--bundle", "bun-a"], "start", priority=3)
check("start entry keeps argv + bundle + priority", (e["argv"][-2:], e["bundle"], e["priority"], e["status"]),
      (["--bundle", "bun-a"], "bun-a", 3, "queued"))
e = pf.add_entry("lab-b", ["--repo", "/r"], "start")
check("--label is appended when the argv lacks it", pf.argv_label(e["argv"]), "lab-b")
try:
    pf.add_entry("lab-a", ["--label", "lab-a"], "start")
    dup = False
except SystemExit:
    dup = True
check("a queued label cannot be added twice", dup, True)
try:
    pf.add_entry("lab-x", ["--label", "other"], "start")
    mism = False
except SystemExit:
    mism = True
check("--label mismatch with the argv is refused", mism, True)
wtr = mkwt("lab-r")
pf._write_json(T / "od" / "auto-runs" / "argv" / "lab-r.json",
               {"label": "lab-r", "bundle": "bun-r", "worktree": wtr, "argv": ["--repo", "/r", "--label", "lab-r"], "cwd": "/"})
e = pf.add_entry("lab-r", kind="resume")
check("resume: argv from the record + exactly one --resume-harness, worktree/bundle carried",
      (e["argv"].count("--resume-harness"), e["bundle"], e["worktree"]), (1, "bun-r", wtr))

# ---------------------------------------------------------------- 2. safety gates
def ent(label="g", bundle=None, kind="start", wt=None, **kw):
    d = {"label": label, "bundle": bundle or label, "kind": kind, "argv": ["--label", label], "worktree": wt,
         "added_at": "2026-10-09T00:00:00Z", "priority": 0, "status": "queued", "launches": []}
    d.update(kw)
    return d


check("clean entry is allowed", reason(ent()), None)
check("entry hold blocks", "hold" in (reason(ent(hold=True)) or ""), True)
check("holds.txt blocks", "holds.txt" in (pf.block_reason(ent(), view(), NODRV, holds={"g"}, helpers=HELP) or ""), True)
check("blocked park blocks", "parked" in (reason(ent(), view(parked={"g": {"kind": "blocked"}})) or ""), True)
check("stalled park blocks", "parked" in (reason(ent(), view(parked={"g": {"kind": "stalled"}})) or ""), True)
check("cpu_wait park (benign) does not block", reason(ent(), view(parked={"g": {"kind": "cpu_wait"}})), None)
check("yielded park (benign) does not block", reason(ent(), view(parked={"g": {"kind": "yielded"}})), None)
check("needs_opus row blocks", "needs_opus" in (reason(ent(), view([row("1", "auto-author-g", "needs_opus", "g")])) or ""), True)
check("user_hold row blocks", "held" in (reason(ent(), view([row("1", "g", "pending", "g", user_hold=True)])) or ""), True)
check("open READY-TO-LAND / ESCALATIONS row blocks",
      "index row" in (reason(ent(), open_index_rows=lambda k: {"g"}) or ""), True)
check("superseded/accepted bundle blocks", "superseded" in (reason(ent(), superseded=lambda k: {"g"}) or ""), True)
check("slicer-owned bundle blocks", "slice plan" in (reason(ent(), slicer_owns=lambda b: True) or ""), True)
check("live driver (ps ground truth) blocks",
      "already alive" in (reason(ent(), rows=[(41, "Fri Oct  9 10:00:00 2026", "python3 /b/ollama-dispatch-auto --label g --repo /r")]) or ""), True)
check("a different label's driver does not block",
      reason(ent(), rows=[(41, "Fri Oct  9 10:00:00 2026", "python3 /b/ollama-dispatch-auto --label gg --repo /r")]), None)
# pid reuse: the chain record names a LIVE pid (ours) whose command is not a driver
pf._write_json(T / "od" / "auto-runs" / "g.json", {"runs": {"g": {"label": "g", "pid": os.getpid(), "phase": "waiting"}}})
check("pid reuse: live pid that is not a dispatch-auto driver does NOT count",
      pf.live_driver("g", "g", [(os.getpid(), "x", "python3 something-else")]), False)
check("...but the same pid as a driver counts",
      pf.live_driver("g", "g", [(os.getpid(), "x", "python3 /b/ollama-dispatch-auto --label g")]), True)
os.remove(T / "od" / "auto-runs" / "g.json")
wt = mkwt("g")
check("worktree with a live queue row blocks",
      "owns the worktree" in (reason(ent(wt=wt), view([row("1", "g", "running", "zz", cwd=wt)])) or ""), True)
(Path(wt) / ".hand-harness").write_text("")
check("hand-locked worktree blocks", "hand-locked" in (reason(ent(wt=wt)) or ""), True)
os.remove(Path(wt) / ".hand-harness")
# real flock probe
import fcntl
gd = subprocess.run(["git", "-C", wt, "rev-parse", "--absolute-git-dir"], capture_output=True, text=True).stdout.strip()
fh = open(Path(gd) / "dispatch-tree.lock", "a+")
fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
check("tree lock held (real flock) blocks", "tree lock" in (pf.block_reason(ent(wt=wt), view(), NODRV, holds=set(),
      helpers={k: v for k, v in HELP.items() if k != "tree_lock_held"}) or ""), True)
fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
check("tree lock free (real probe) does not block", pf.block_reason(ent(wt=wt), view(), NODRV, holds=set(),
      helpers={k: v for k, v in HELP.items() if k != "tree_lock_held"}), None)
fh.close()
nh = mkwt("noharness", harness=False)
check("resume without an authored harness is refused", "harness not authored" in (reason(ent(kind="resume", wt=nh)) or ""), True)
check("resume with an authored harness is allowed", reason(ent(kind="resume", wt=wt)), None)

# ---------------------------------------------------------------- 3. bounds
check("slots: empty runway -> max_prep", pf.compute_slots(CFG, 0, 0), 2)
check("slots: one prepping -> one more", pf.compute_slots(CFG, 0, 1), 1)
check("slots: max_prep drivers already advancing -> none", pf.compute_slots(CFG, 0, 2), 0)
check("slots: runway already full (3 ready) -> none", pf.compute_slots(CFG, 3, 0), 0)
check("slots: 2 ready + 0 prepping -> one", pf.compute_slots(CFG, 2, 0), 1)
check("headroom: ok", pf.headroom(CFG, load=1.0, ncpu=16, lane=0)[0], True)
check("headroom: Mac load too high", pf.headroom(CFG, load=13.0, ncpu=16, lane=0)[0], False)
check("headroom: CPU lane saturated", pf.headroom(CFG, load=1.0, ncpu=16, lane=2)[0], False)
check("ready_bundles: running + runnable pending, not held/gpu-exclusive",
      sorted(pf.ready_bundles([row("1", "a", "running", "A"), row("2", "b", "pending", "B"),
                               row("3", "c", "pending", "C", user_hold=True),
                               row("4", "d", "pending", "D", lane="gpu-exclusive"),
                               row("5", "e", "done", "E")])), ["A", "B"])


def passrun(entries_, jobs=(), rows=NODRV, **kw):
    reset()
    for x in entries_:
        pf._write_json(pf.entry_path(x["label"]), x)
    launched = []
    out = pf.run_pass(launch=lambda cmd, cwd, log: launched.append(cmd) or 4242 + len(launched), rows=rows,
                      view=kw.pop("view", view(jobs)), cfg=kw.pop("cfg", CFG), helpers=HELP,
                      load=kw.pop("load", 0.5), lane=kw.pop("lane", 0), **kw)
    return out, launched


es = [ent("e1"), ent("e2"), ent("e3")]
out, ln = passrun(es)
check("empty runway launches exactly max_prep (2) entries", (len(ln), [o[1] for o in out]), (2, ["launched", "launched", "wait"]))
out, ln = passrun(es, jobs=[row("1", "a", "running", "A"), row("2", "b", "pending", "B"), row("3", "c", "pending", "C")])
check("full GPU runway launches nothing (no needless prep)", (len(ln), out[0][1]), (0, "wait"))
out, ln = passrun(es, load=14.0)
check("high Mac load launches nothing", (len(ln), out[0][1]), (0, "wait"))
out, ln = passrun(es, lane=5)
check("saturated CPU lane launches nothing", (len(ln), out[0][1]), (0, "wait"))
out, ln = passrun([ent("e1", hold=True), ent("e2")])
check("a held entry is skipped, the next one still starts", (len(ln), [o[1] for o in out]), (1, ["skip", "launched"]))
check("...and it is the unheld one", [o[0] for o in out if o[1] == "launched"], ["e2"])

# ---------------------------------------------------------------- 4. order
reset()
v = view(parked={"pb": {"kind": "cpu_wait"}}, commit={"key": "cb"})
order = sorted([ent("old", added_at="2026-10-09T01:00:00Z"), ent("new-hi", priority=5, added_at="2026-10-09T03:00:00Z"),
                ent("res", kind="resume", added_at="2026-10-09T04:00:00Z"), ent("parkedb", bundle="pb", added_at="2026-10-09T05:00:00Z"),
                ent("committed", bundle="cb", added_at="2026-10-09T06:00:00Z"), ent("old2", added_at="2026-10-09T02:00:00Z")],
               key=lambda x: pf.order_key(x, v))
check("order: resumes-first/committed bundle, then kind=resume, then priority, then FIFO",
      [x["label"] for x in order], ["parkedb", "committed", "res", "new-hi", "old", "old2"])

# ---------------------------------------------------------------- 5. idempotency / kill switch / fail closed
reset()
pf._write_json(pf.entry_path("once"), ent("once"))
n = []
fake = lambda cmd, cwd, log: n.append(pf.argv_label(cmd)) or 4300
pf.run_pass(launch=fake, rows=NODRV, view=view(), cfg=CFG, helpers=HELP, load=0.5, lane=0)
pf.run_pass(launch=fake, rows=NODRV, view=view(), cfg=CFG, helpers=HELP, load=0.5, lane=0)
check("two passes launch an entry once", len(n), 1)
check("entry recorded as launched with its pid", (pf._read_json(pf.entry_path("once"))["status"],
                                                    pf._read_json(pf.entry_path("once"))["launches"][0]["pid"]), ("launched", 4300))
# concurrent pass: hold launch.lock from another fd -> the pass declines
(T / "pf").mkdir(exist_ok=True)
lk = open(T / "pf" / "launch.lock", "a+")
fcntl.flock(lk.fileno(), fcntl.LOCK_EX)
pf._write_json(pf.entry_path("conc"), ent("conc"))
n.clear()
out = pf.run_pass(launch=fake, rows=NODRV, view=view(), cfg=CFG, helpers=HELP, load=0.5, lane=0)
check("a concurrent pass (launch.lock held) launches nothing", (len(n), out[0][1]), (0, "skip"))
fcntl.flock(lk.fileno(), fcntl.LOCK_UN)
lk.close()
# reconcile: launched entry, driver gone, long ago
e = pf._read_json(pf.entry_path("once"))
e["launches"][-1]["t"] = time.time() - 3600
pf._write_json(pf.entry_path("once"), e)
n.clear()
pf.run_pass(launch=fake, rows=NODRV, view=view(), cfg=CFG, helpers=HELP, load=0.5, lane=0)
check("a launched entry whose driver is gone is reconciled to failed (no clean outcome) and NOT relaunched",
      (pf._read_json(pf.entry_path("once"))["status"], n.count("once")), ("failed", 0))
check("...status is failed/done, never queued again", pf._read_json(pf.entry_path("once"))["status"] in ("failed", "done"), True)
pf._write_json(T / "od" / "auto-runs" / "once.json", {"runs": {"once": {"label": "once", "phase": "ended", "outcome": "exit 0"}}})
e = pf._read_json(pf.entry_path("once")); e["status"] = "launched"; pf._write_json(pf.entry_path("once"), e)
pf.run_pass(launch=fake, rows=NODRV, view=view(), cfg=CFG, helpers=HELP, load=0.5, lane=0)
check("a driver that ended exit 0 reconciles to done", pf._read_json(pf.entry_path("once"))["status"], "done")
# kill switch / fail closed
reset()
pf._write_json(pf.entry_path("k"), ent("k"))
(T / "od" / "cpu-prefetch.disabled").write_text("")
out = pf.run_pass(launch=fake, rows=NODRV, view=view(), cfg=CFG, helpers=HELP, load=0.5, lane=0)
check("kill switch: nothing launches", (out[0][1], "kill switch" in out[0][2]), ("skip", True))
os.remove(T / "od" / "cpu-prefetch.disabled")
out = pf.run_pass(launch=fake, rows=NODRV, view=None, cfg=CFG, helpers=HELP, load=0.5, lane=0)
check("unreadable queue state fails closed", (out[0][1], "fail closed" in out[0][2]), ("skip", True))

# ---------------------------------------------------------------- 6. real spawn
reset()
Path(os.environ["CPU_PREFETCH_AUTO"]).write_text("import sys, time\ntime.sleep(25)\n")
pf.AUTO = Path(os.environ["CPU_PREFETCH_AUTO"])
# the fake driver's command line must look like a driver: name it ollama-dispatch-auto
fakebin = T / "ollama-dispatch-auto"
fakebin.write_text("import sys, time\ntime.sleep(25)\n")
pf.AUTO = fakebin
pf._write_json(pf.entry_path("real"), ent("real", argv=["--label", "real", "--repo", "/r"]))
pf._write_json(pf.entry_path("real2"), ent("real2", argv=["--label", "real2", "--repo", "/r"], hold=True))
out = pf.run_pass(rows=pf.ps_rows(), view=view(), cfg=CFG, helpers=HELP, load=0.5, lane=0)
time.sleep(0.5)
rows_now = pf.ps_rows()
pid = pf._read_json(pf.entry_path("real"))["launches"][0]["pid"]
check("real spawn: launched", [o[:2] for o in out if o[0] == "real"], [("real", "launched")])
check("real spawn: ps ground truth finds the driver by --label", pf.driver_pids("real", rows_now), [pid])
check("real spawn: counted as prepping (spawned moments ago, no chain record yet)",
      "real" in pf.drivers_advancing(rows_now, pf.load_entries(), grace=600), True)
out = pf.run_pass(rows=rows_now, view=view(), cfg=CFG, helpers=HELP, load=0.5, lane=0)
check("real spawn: second pass does not relaunch", pf._read_json(pf.entry_path("real"))["launches"].__len__(), 1)
try:
    os.killpg(os.getpgid(pid), 15)
except OSError:
    pass

import shutil
shutil.rmtree(T, ignore_errors=True)
print("CPU_PREFETCH_OK" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
