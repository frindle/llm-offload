#!/usr/bin/env python3
"""Prove each bake-off task is DECIDABLE before any arm runs.

A bake-off scores models on whether they can make a verify pass. If a task's
verify cannot fail at baseline, every arm "passes" it and the task contributes
nothing; if it cannot pass on a known-good solution, every arm fails it and the
task contributes nothing. Either way a whole arm's GPU time is spent measuring
noise, and you find out mid-run.

So this checks both directions per task, against real artifacts:
  baseline commit  -> verify MUST fail   (it can tell done from not-done)
  solved state     -> verify MUST pass   (it does not fail correct work)

A task that fails either direction is reported INELIGIBLE with the reason and
kept out of the roster, rather than discovered halfway through a bake-off.

Usage: bakeoff-roster.py --dir <task-dir> [--dir ...] [--out roster.json]
"""
import argparse, json, shutil, subprocess, sys, tempfile, time
from pathlib import Path


def run_verify(d: Path, timeout=300):
    v = d / "verify.sh"
    if not v.exists():
        return None, "no verify.sh"
    t0 = time.monotonic()
    try:
        r = subprocess.run(["bash", str(v)], cwd=str(d), capture_output=True,
                           text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return None, f"verify timed out after {timeout}s"
    return r.returncode, f"{time.monotonic()-t0:.1f}s"


def check(task_dir: Path):
    """Copy, drive both directions, never touch the original."""
    src = task_dir.resolve()
    baseline = subprocess.run(["git", "-C", str(src), "log", "--format=%H", "--reverse"],
                              capture_output=True, text=True)
    if baseline.returncode != 0 or not baseline.stdout.strip():
        return {"task": src.name, "eligible": False, "why": "not a git repo / no commits"}
    base_sha = baseline.stdout.split()[0]

    tmp = Path(tempfile.mkdtemp())
    work = tmp / src.name
    shutil.copytree(src, work, symlinks=True,
                    ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    try:
        # SOLVED state first -- it is what the directory currently holds.
        solved_rc, solved_t = run_verify(work)
        # then reset to the pre-task baseline.
        # `checkout <sha> -- .` cannot DELETE a file the baseline never had:
        # a solution that ADDS a file leaves it tracked, so checkout skips it
        # (not in the target tree) and `clean` skips it (not untracked). The
        # solution then survives into the "baseline" run and the task reports
        # verify-passes-at-baseline -- excluding a perfectly good task for a
        # harness defect. reset --hard moves the index too, so added files go.
        subprocess.run(["git", "-C", str(work), "reset", "-q", "--hard", base_sha],
                       capture_output=True)
        subprocess.run(["git", "-C", str(work), "clean", "-qfdx"], capture_output=True)
        base_rc, base_t = run_verify(work)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

    reasons = []
    if base_rc == 0:
        reasons.append("verify PASSES at baseline -- it cannot tell done from not-done")
    if base_rc is None:
        reasons.append(f"baseline verify unusable ({base_t})")
    if solved_rc != 0:
        reasons.append("verify FAILS on the solved state -- it would fail correct work")
    return {"task": src.name, "dir": str(src), "baseline": base_sha[:12],
            "baseline_rc": base_rc, "solved_rc": solved_rc,
            "verify_seconds": solved_t,
            "eligible": not reasons, "why": "; ".join(reasons) or "decidable both ways"}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", action="append", required=True)
    ap.add_argument("--out")
    a = ap.parse_args()
    rows = [check(Path(d)) for d in a.dir]
    ok = [r for r in rows if r["eligible"]]
    for r in rows:
        mark = "ELIGIBLE  " if r["eligible"] else "INELIGIBLE"
        print(f"  {mark} {r['task']:<20} baseline_rc={r.get('baseline_rc')} "
              f"solved_rc={r.get('solved_rc')}  {r['why']}")
    print(f"\n{len(ok)} of {len(rows)} task(s) decidable both ways.")
    if a.out:
        Path(a.out).write_text(json.dumps({"tasks": ok}, indent=1))
        print(f"roster -> {a.out}")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
