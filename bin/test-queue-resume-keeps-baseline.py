#!/usr/bin/env python3
"""Behavioural test: a RESUMED relaunch (orphan requeue / pause-resume WITH a resume
transcript) must keep its first launch's CLEAN baseline instead of re-reading its own
in-progress edits as a dirty launch tree.

Origin: rt-egift-link-s1-s1-parse-link bc5c5c80306a (2026-10-06). The owner's daemon
kickstart orphaned the worker; the new daemon requeued it with its transcript, the
launch path re-measured dirty=1 (the job's own half-done lib/egiftLink.ts), and the
gate marked a correct verify-green result UNTRUSTED -> concerns -> escalated.

Drives the real launch-time decision on a real git worktree: clean first launch ->
stamp; the job edits its target; then the relaunch decision for (a) resume -> keep,
(b) no transcript -> re-measure (dirty), (c) HEAD moved -> re-measure, (d) dirty first
launch -> re-measure, (e) enqueue-time stamp -> re-measure.

Usage: test-queue-resume-keeps-baseline.py [--target PATH] (revert test: point at the
pre-fix backup; it must FAIL).
"""
import importlib.machinery, importlib.util, subprocess, sys, tempfile
from pathlib import Path

SRC = Path(__file__).resolve().parent / "ollama-queue.py"
if "--target" in sys.argv:
    SRC = Path(sys.argv[sys.argv.index("--target") + 1])
loader = importlib.machinery.SourceFileLoader("q_resume_bl", str(SRC))
spec = importlib.util.spec_from_loader("q_resume_bl", loader)
q = importlib.util.module_from_spec(spec)
loader.exec_module(q)

fails = 0


def check(name, got, want):
    global fails
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"  -- got {got!r}, want {want!r}"))
    if not ok:
        fails += 1


def relaunch_baseline(job):
    """The exact launch-path decision the daemon makes (mirrors the call site)."""
    keep = getattr(q, "resume_keeps_launch_baseline", None)
    head = getattr(q, "_head_of", lambda c: None)(job["cwd"])
    if keep is not None and keep(job, head):
        return job
    return q.apply_launch_baseline(job, q.measure_baseline(job["cwd"]))


def git(wt, *a):
    subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True)


with tempfile.TemporaryDirectory() as td:
    wt = Path(td)
    git(wt, "init", "-q")
    git(wt, "config", "user.email", "t@t")
    git(wt, "config", "user.name", "t")
    (wt / "lib.ts").write_text("export const a = 1;\n")
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "base")

    def first_launch(resume=None):
        j = {"id": "j1", "label": "x", "cwd": str(wt), "resume_transcript": resume}
        return q.apply_launch_baseline(j, q.measure_baseline(str(wt)))

    j = first_launch()
    check("control: the first launch measured clean", j["launch_baseline"]["dirty"], 0)
    head0 = j["launch_baseline"]["head"]
    (wt / "lib.ts").write_text("export const a = 2;\n")      # the job's own edit

    # (a) resume after an orphan: keep the clean first-launch stamp
    ja = dict(j, launch_baseline=dict(j["launch_baseline"]),
              resume_transcript="/tmp/t.json")
    relaunch_baseline(ja)
    check("(a) resumed relaunch keeps dirty=0", ja["launch_baseline"]["dirty"], 0)
    check("(a) ...and the original head", ja["launch_baseline"]["head"], head0)

    # (b) no transcript: a fresh rerun -> re-measure, the dirt is visible
    jb = dict(j, launch_baseline=dict(j["launch_baseline"]), resume_transcript=None)
    relaunch_baseline(jb)
    check("(b) no transcript re-measures (dirty seen)", jb["launch_baseline"]["dirty"] > 0, True)

    # (d) the first launch was itself dirty -> never laundered clean
    jd = dict(j, launch_baseline={"head": head0, "dirty": 2}, resume_transcript="/tmp/t.json")
    relaunch_baseline(jd)
    check("(d) dirty first launch is re-measured, not kept", jd["launch_baseline"]["dirty"] > 0, True)

    # (e) only an enqueue-time (provisional) stamp -> re-measure
    je = dict(j, launch_baseline={"head": head0, "dirty": 0}, baseline_at="enqueue",
              resume_transcript="/tmp/t.json")
    relaunch_baseline(je)
    check("(e) provisional enqueue stamp is re-measured", je["launch_baseline"]["dirty"] > 0, True)

    # (c) HEAD moved since the first launch -> re-measure
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "moved")
    (wt / "lib.ts").write_text("export const a = 3;\n")
    jc = dict(j, launch_baseline={"head": head0, "dirty": 0}, resume_transcript="/tmp/t.json")
    relaunch_baseline(jc)
    check("(c) HEAD moved -> re-measured (new head)", jc["launch_baseline"]["head"] != head0, True)

# wiring: the daemon's launch path must consult the helper before re-measuring
src = SRC.read_text().split("def _self_test", 1)[0]
i_keep = src.find("if resume_keeps_launch_baseline(job, _head_of(job[")
i_meas = src.find("apply_launch_baseline(job, measure_baseline(job[")
i_pop = src.find("proc = subprocess.Popen(cmd, cwd=job[")
check("wiring: launch path consults resume_keeps_launch_baseline before measuring",
      0 < i_keep < i_meas < i_pop, True)

print(f"--- {fails} failed ---")
sys.exit(1 if fails else 0)
