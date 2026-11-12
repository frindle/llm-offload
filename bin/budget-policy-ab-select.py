#!/usr/bin/env python3
"""budget-policy-ab-select.py -- READ-ONLY: list completed authoring jobs that can be replayed for
the budget-policy A/B (see ~/bin/budget-policy-ab.md). It enqueues nothing and touches no worktree.

A worker transcript (ollama-worker-logs/<ts>.json) stores the task text and a start snapshot
(`HEAD sha --STATUS-- porcelain --DIFF-- tracked diff`). The snapshot names the untracked files that
existed at job start but NOT their contents, so a job is replayable only when the start tree can be
rebuilt exactly: HEAD sha still in the repo, tracked tree clean at start, and every untracked file at
start is a frozen SCAFFOLD file that still exists unchanged-in-kind in the original worktree (the
authored target must NOT have existed at start: that is a round-1 / creation job). The verify command
and max_iters come from the matching queue row (same cwd, latest auto-author row enqueued before the
transcript).

  budget-policy-ab-select.py [-n 30] [--since 2026-09-12] [--json] [--include-converged-only]
"""
import argparse, glob, json, os, re, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

LOGS = Path.home() / "bin" / "ollama-worker-logs"
STATE = Path.home() / "bin" / "ollama-queue-state.json"
SCAFFOLD = re.compile(r"^(TASK\.md|AUTO-TASK\.md|verify\.sh|verify\.test\.\w+|refimpl\.py|check_literals\.py|"
                      r"dispatch-env\.\w+|auto-harness-check\.py|\.dispatch-harness\.json|\.refine-guard\.json|"
                      r"\.preflight-state\.json|test_fixture\.py)$")


def parse_snapshot(snap):
    if not snap:
        return None
    head, _, rest = snap.partition("\n---STATUS---\n")
    status, _, diff = rest.partition("\n---DIFF---\n")
    untracked = [ln[3:] for ln in status.splitlines() if ln.startswith("?? ")]
    tracked_dirty = [ln for ln in status.splitlines() if ln and not ln.startswith("?? ")]
    return {"head": head.strip(), "untracked": untracked, "tracked_dirty": tracked_dirty, "diff": diff.strip()}


def ts_of(name):
    try:
        return datetime.strptime(name[:16], "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-n", type=int, default=30)
    ap.add_argument("--since", default="2026-09-12")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--converged-only", action="store_true", help="only jobs that converged originally")
    a = ap.parse_args()
    try:
        jobs = json.load(open(STATE)).get("jobs") or []
    except Exception as e:
        print(f"cannot read queue state: {e}", file=sys.stderr)
        return 2
    rows = [j for j in jobs if str(j.get("label", "")).startswith("auto-author-") and j.get("verify")]
    why = {}
    ok = []
    for f in sorted(glob.glob(str(LOGS / "2026*.json")), reverse=True):
        base = os.path.basename(f)
        t = ts_of(base)
        if t is None or base[:8] < a.since.replace("-", ""):
            continue
        try:
            d = json.load(open(f))
        except Exception:
            continue
        task, cwd = d.get("task") or "", d.get("cwd") or ""
        if not ("STAGED AUTHORING" in task or "AUTHORING" in task[:200].upper() or "# REFINE TASK" in task[:200]):
            continue
        def no(reason):
            why[reason] = why.get(reason, 0) + 1
        if "# REFINE TASK" in task[:200]:
            no("refine round (target existed at start)"); continue
        if not os.path.isdir(cwd):
            no("worktree reaped"); continue
        snap = parse_snapshot(d.get("worktree_start_snapshot"))
        if not snap:
            no("no start snapshot"); continue
        if snap["tracked_dirty"] or snap["diff"]:
            no("tracked tree dirty at start (diff body not replayable)"); continue
        stray = [u for u in snap["untracked"] if not SCAFFOLD.match(u)]
        if stray:
            no("non-scaffold untracked file existed at start (continuation: contents lost)"); continue
        if subprocess.run(["git", "-C", cwd, "cat-file", "-e", snap["head"] + "^{commit}"],
                          capture_output=True).returncode != 0:
            no("start HEAD sha no longer in the repo"); continue
        missing = [u for u in snap["untracked"] if not os.path.exists(os.path.join(cwd, u))]
        if missing:
            no(f"scaffold file(s) gone: {missing[:2]}"); continue
        cand = [j for j in rows if j.get("cwd") == cwd and (j.get("enqueued_at") or "") <= t.isoformat()]
        row = max(cand, key=lambda j: j.get("enqueued_at") or "") if cand else None
        if not row:
            no("no matching queue row (verify/max_iters unknown)"); continue
        if a.converged_only and not d.get("converged"):
            continue
        ok.append({"transcript": base, "cwd": cwd, "start_head": snap["head"], "scaffold": snap["untracked"],
                   "converged": bool(d.get("converged")), "iterations": d.get("iterations"),
                   "model": d.get("model"), "verify": row.get("verify"), "max_iters": row.get("max_iters"),
                   "num_ctx": row.get("num_ctx"), "job": row.get("id"), "label": row.get("label")})
        if len(ok) >= a.n:
            break
    if a.json:
        print(json.dumps({"replayable": ok, "excluded": why}, indent=1))
        return 0
    for r in ok:
        print(f"{r['transcript']}  {'CONV' if r['converged'] else 'CAPD'} it={r['iterations']}/{r['max_iters']}  "
              f"{r['model']}  head={r['start_head'][:8]}  {r['cwd']}")
    print(f"\n{len(ok)} replayable. Excluded: " + (", ".join(f"{v}x {k}" for k, v in sorted(why.items(), key=lambda x: -x[1])) or "none"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
