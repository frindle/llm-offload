#!/usr/bin/env python3
"""Coverage audit that counts MEASUREMENTS, not rows.

WHY THIS EXISTS: parity was miscounted three times in one evening, each time by
treating a row as evidence when it was not one.

  1. `ABORT_NO_WORKTREE` rows counted as newcomer debug coverage. They never ran.
  2. v8's debug rows counted as incumbent coverage. That cell is VOID -- it was
     context-starved pre-read-cap, 10 of 18 runs hit the ceiling at 5-6
     iterations, and routing.json carries no verdict for any model in it.
  3. Diff line-count read as "work done", when `git diff` omits untracked files
     and the created component was the actual deliverable.

Each miscount pointed at a different wrong action: run reps nobody needed, skip
reps somebody did, and declare a working cell broken. So the filter is the tool,
not a step someone remembers to do.

A row counts as a MEASUREMENT only if all hold:
  * it is not an ABORT of any kind
  * its cell is not on the VOID list
  * it did not stop at a context ceiling (Fable: config_ceiling is a bug, and a
    native_ceiling row measures the window, not the work)
  * it is not timed_out (an instrument outcome, not a model outcome)

VALIDITY IS SEPARATE FROM OUTCOME. A failed run is a measurement; an aborted run
is not. Never filter on `verify_passed` here -- that is the outcome, and on a
non-discriminating cell it is not even that.
"""
import argparse
import csv
from collections import defaultdict
from pathlib import Path

D = Path("/Users/user/Desktop/GitHub Projects/bakeoff/model-buildoff-2026-08-22")

# (file, task-substring) pairs whose cell is void and carries no verdict.
VOID_CELLS = {("results-v8.csv", "plex-release-group-debug")}

SOURCES = ["results-v8.csv", "results-v9.csv", "results-v10catchup.csv",
           "results-v10parity.csv", "results-v10-bulk.csv"]


def is_measurement(fname, r):
    task = r.get("task", "")
    for vf, vt in VOID_CELLS:
        if fname == vf and vt in task:
            return False, "void cell"
    blob = (r.get("stop_reason") or "") + "|" + (r.get("exit_code") or "")
    if "ABORT" in blob:
        return False, "abort"
    stop = r.get("stop_reason") or ""
    # Fable's ruling, and the two ceilings are NOT interchangeable:
    #   config_ceiling -- a BUG in how we ran it. Re-runnable. NOT a measurement.
    #   native_ceiling -- a real measurement OF THE MODEL: it cannot do the cell
    #                     even at its own maximum window. Keepable.
    # Collapsing them cost a real misreading: qwen3-coder:30b's debug cell was
    # reported as "0/3" as though the model had failed, when all six of its runs
    # (v8 and v9) stopped at config_ceiling at 65536 of a native 262144. It was
    # never measured on that cell at all.
    if "config_ceiling" in stop:
        return False, "config_ceiling (bug, re-runnable)"
    if (r.get("timed_out") or "").lower() == "true":
        return False, "timed_out"
    return True, ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arm", default="base", help="arm to audit (default base)")
    a = ap.parse_args()

    valid = defaultdict(lambda: defaultdict(int))
    dropped = defaultdict(int)
    for f in SOURCES:
        p = D / f
        if not p.is_file():
            continue
        for r in csv.DictReader(open(p)):
            m, t = r.get("model", "").strip(), r.get("task", "").strip()
            if not m or not t:
                continue
            if (r.get("arm") or "base") != a.arm:
                continue
            ok, why = is_measurement(f, r)
            cell = ("photo" if "photo" in t else
                    "clamshell" if "clamshell" in t else
                    "debug" if "debug" in t else t)
            if ok:
                valid[m][cell] += 1
            else:
                dropped[why] += 1

    # two-turn files have their own shape
    for f, lab in [("results-v9-r3.csv", "twoturn"),
                   ("results-v10catchup-twoturn.csv", "twoturn"),
                   ("results-v10parity-tiebreak.csv", "twoturn")]:
        p = D / f
        if not p.is_file():
            continue
        for r in csv.DictReader(open(p)):
            if r.get("model"):
                valid[r["model"]][lab] += 1

    cells = ["debug", "clamshell", "photo", "twoturn", "bulk-codemod"]
    models = sorted(valid)
    print(f"VALID MEASUREMENTS per model x cell (arm={a.arm})")
    print(f"{'model':26s}" + "".join(f"{c:>14s}" for c in cells))
    print("-" * (26 + 14 * len(cells)))
    for m in models:
        print(f"{m:26s}" + "".join(f"{valid[m].get(c,0):>14d}" for c in cells))

    print("\nrows dropped as non-measurements: " +
          ", ".join(f"{k}={v}" for k, v in sorted(dropped.items())) or "none")

    # Deliberate retirements are NOT gaps. qwen2.5-coder:14b's photo cell is
    # settled: native_ceiling x3 in v7 at 32768, which IS its maximum, so there
    # is no larger window to retry at (bakeoff-v8-lib.sh:404-413). Counting it
    # as a gap would schedule ~30 min/rep to reproduce a known ceiling.
    RETIRED = {("qwen2.5-coder:14b", "photo")}

    print("\nPARITY GAPS (target = the max any model has in that cell):")
    print("  retired cells excluded: " +
          ", ".join(f"{m}/{c}" for m, c in sorted(RETIRED)))
    gaps = 0
    for c in cells:
        have = {m: valid[m].get(c, 0) for m in models if (m, c) not in RETIRED}
        tgt = max(have.values()) if have else 0
        if tgt == 0:
            print(f"  {c:14s} no data for anyone")
            continue
        short = {m: tgt - n for m, n in have.items() if n < tgt}
        if not short:
            print(f"  {c:14s} AT PARITY (all {tgt})")
        else:
            gaps += sum(short.values())
            print(f"  {c:14s} target {tgt}; short: " +
                  ", ".join(f"{m}(+{d})" for m, d in sorted(short.items())))
    print(f"\ntotal runs to close all gaps: {gaps}")


if __name__ == "__main__":
    main()
