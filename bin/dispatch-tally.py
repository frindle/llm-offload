#!/usr/bin/env python3
"""Canonical model-dispatch tally for the Agent-Dispatch-Log box.

Fixes two long-standing gaps (the owner, 2026-08-30):
  1. Claude-model agents (Fable/Opus/subagents) never appeared, because they don't run
     through ollama-worker.py and so were never in dispatch-metrics.jsonl. This reads a
     companion file `claude-dispatches.jsonl` (same dir) that Claude sessions append to.
  2. Test/bench dispatches (pre-filter benchmarks, bake-off arms, mock/fake models) polluted
     the count. We now tally ONLY real WORK dispatches.

Classification precedence: an explicit `dispatch_kind` field ("work"|"test") on a record
wins (the worker/queue can set it via --kind going forward). With no such field, a heuristic
classifies obvious tests so the ~254 historical records don't all read as work.

Usage:  python3 ~/bin/dispatch-tally.py            # markdown table, work-only
        python3 ~/bin/dispatch-tally.py --show-test # also print what was excluded
"""
import json, re, sys, collections
from pathlib import Path

LOGDIR = Path.home() / "bin" / "ollama-worker-logs"
WORKER = LOGDIR / "dispatch-metrics.jsonl"
CLAUDE = LOGDIR / "claude-dispatches.jsonl"

# Heuristic test markers (used only when a record has no explicit dispatch_kind).
TEST_MODELS = {"mock", "fake-model", "moresearch/swe7b:latest"}
# Bench/eval task shapes -- the pre-filter benchmark, bake-off scoring, seeded-defect evals.
BENCH_RE = re.compile(
    r"code-review pre-filter|DIFF\.patch|bake-?off|review-bench|seeded (defect|bug)|"
    r"benign decoy|prefilter|FLAG/PASS|synthetic bug", re.I)

def classify(rec: dict) -> str:
    k = rec.get("dispatch_kind")
    if k in ("work", "test"):
        return k
    if rec.get("model") in TEST_MODELS:
        return "test"
    if BENCH_RE.search(rec.get("task_preview") or ""):
        return "test"
    return "work"

def load(path: Path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except Exception:
            pass
    return out

def main():
    show_test = "--show-test" in sys.argv
    recs = load(WORKER) + load(CLAUDE)
    work = collections.Counter()
    test = collections.Counter()
    for r in recs:
        (work if classify(r) == "work" else test)[r.get("model", "?")] += 1

    print("| model | dispatches |")
    print("|---|---:|")
    for m, n in work.most_common():
        print(f"| {m} | {n} |")
    print(f"| **total (work only)** | **{sum(work.values())}** |")

    # STATUS BREAKDOWN, added 2026-09-01 with metrics-on-failure. Until then
    # ollama-worker.py wrote a metrics row ONLY on the converged path, so this
    # tally silently counted successes and called them dispatches. It now sees
    # failures too, which is more correct -- a failed dispatch still consumed a
    # GPU slot -- but it is a step change in a number the owner tracks, so the
    # composition is printed rather than left to look like a sudden surge.
    # Rows written before that change have no failure counterpart at all; the
    # pre-change population is converged-only and must not be compared
    # like-for-like against later totals.
    statuses = collections.Counter(
        r.get("status") or "unrecorded" for r in recs if classify(r) == "work")
    if len(statuses) > 1:
        print("\n| status | dispatches |")
        print("|---|---:|")
        for st, n in statuses.most_common():
            print(f"| {st} | {n} |")
        print("\n_Failures were not recorded at all before 2026-09-01, so totals "
              "spanning that date are not like-for-like: earlier rows are "
              "converged-only. Context-threshold analysis should filter "
              "`status == \"converged\"` to reproduce the original population._")
    print(f"\n_Work-only: {sum(test.values())} test/bench dispatches excluded. "
          f"Sources: dispatch-metrics.jsonl (ollama worker) + claude-dispatches.jsonl "
          f"(Claude-model agents). Regenerate: `python3 ~/bin/dispatch-tally.py`._")
    if show_test:
        print("\n### Excluded (test/bench)")
        for m, n in test.most_common():
            print(f"  {n:4}  {m}")

if __name__ == "__main__":
    main()
