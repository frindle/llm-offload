"""Pre-registered falsification criteria, checked against a round's results.

The point of stating these in advance is that they cannot be rationalised
afterwards. A criterion invented after seeing the data is a criterion chosen to
fit the data. So: declare what result would mean the round is not usable, commit
it to the repository before dispatch, and then run this before believing
anything the round produced.

Each check answers one question: is this an instrument failure or a finding?
That distinction is the entire value of the exercise. A benchmark that cannot
tell them apart reports its own defects as model properties, and the more
confident the report the worse the damage.

THE CRITERIA
------------

  gate_failure        More than `max_not_ready_pct` of runs landed
                      host_ready=no, counting ONLY cells whose gate was
                      classified REACHABLE before the round. A model whose
                      honest threshold exceeds clean-host available memory
                      pages no matter how fresh the host is; that is a property
                      of the hardware and a routing fact in its own right
                      ("needs a bigger host"), and counting it here would
                      falsify the round for something gating never claimed to
                      fix. The REACHABLE/STRUCTURAL split must be fixed before
                      dispatch, never after seeing results.

  wall_binding        Any timed_out=true. The wall must never be the binding
                      constraint. A run killed by the clock is an instrument
                      defect to investigate, not a model outcome — and the
                      correct response is usually to find out why iterations
                      got slow, not to raise the budget.

  verify_not_running  verify_passed=not_run on a majority of rows for any task.
                      If the verify step is not reaching the thing it was built
                      to check, the task's outcome column says nothing.

  token_accounting    prompt/output tokens absent (-1) on a majority of rows.
                      Every tokens-per-second conclusion must then be withdrawn.
                      Wall-killed and load-failed runs legitimately read -1
                      because they never reached a summary; "majority" already
                      absorbs that expected floor.

  context_starvation  Rows terminating at the iteration ceiling with an
                      oversized single tool result in the transcript. This is
                      the failure that voided a cell of the shipped round: one
                      uncapped read of a very large file consumed the context
                      window, after which every remaining iteration was
                      fighting for room. It reads in the results table as
                      incompetence and is not.

Exit code is 0 if the round survives its own criteria, 1 if any criterion fires.
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import Counter, defaultdict
from pathlib import Path


def _truthy(v: str) -> bool:
    return str(v).strip().lower() in ("true", "yes", "1")


def check(rows: list[dict], *, max_not_ready_pct: float = 20.0,
          iter_cap_void_pct: float = 50.0) -> list[tuple[str, bool, str]]:
    """Return (criterion, fired, detail). fired=True means the round is
    compromised on that criterion."""
    out: list[tuple[str, bool, str]] = []
    n = len(rows)

    # -- gate_failure --------------------------------------------------------
    reachable = [r for r in rows
                 if (r.get("gate_class") or "REACHABLE").upper() != "STRUCTURAL"]
    not_ready = [r for r in reachable if (r.get("host_ready") or "") == "no"]
    pct = (100.0 * len(not_ready) / len(reachable)) if reachable else 0.0
    structural = n - len(reachable)
    out.append((
        "gate_failure",
        pct > max_not_ready_pct,
        f"{len(not_ready)}/{len(reachable)} REACHABLE-gate runs were host_ready=no "
        f"({pct:.1f}%, threshold {max_not_ready_pct}%); "
        f"{structural} rows excluded as STRUCTURAL"))

    # -- wall_binding --------------------------------------------------------
    timed = [r for r in rows if _truthy(r.get("timed_out", ""))]
    out.append((
        "wall_binding", bool(timed),
        f"{len(timed)}/{n} runs hit the wall clock"
        + (" — investigate why iterations slowed, do not simply raise the budget"
           if timed else "")))

    # -- verify_not_running --------------------------------------------------
    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in rows:
        by_task[r.get("task", "?")].append(r)
    bad_tasks = []
    for t, rs in by_task.items():
        nr = sum(1 for r in rs if (r.get("verify_passed") or "") in ("not_run", ""))
        if nr * 2 > len(rs):
            bad_tasks.append(f"{t} ({nr}/{len(rs)})")
    out.append((
        "verify_not_running", bool(bad_tasks),
        ("verify never ran on a majority of rows for: " + ", ".join(bad_tasks))
        if bad_tasks else "verify reached a verdict on a majority of rows for every task"))

    # -- token_accounting ----------------------------------------------------
    def _missing(col: str) -> int:
        return sum(1 for r in rows if str(r.get(col, "-1")).strip() in ("-1", "", "None"))
    mp, mo = _missing("prompt_tokens"), _missing("output_tokens")
    out.append((
        "token_accounting", (mp * 2 > n) or (mo * 2 > n),
        f"prompt_tokens missing on {mp}/{n}, output_tokens on {mo}/{n}"))

    # -- context_starvation --------------------------------------------------
    per_cell: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for r in rows:
        per_cell[(r.get("task", "?"), r.get("arm", "base"))].append(r)
    starved = []
    for (t, arm), rs in per_cell.items():
        cap = sum(1 for r in rs
                  if r.get("stop_reason") in ("iter_cap", "config_ceiling",
                                              "native_ceiling"))
        if len(rs) >= 3 and 100.0 * cap / len(rs) >= iter_cap_void_pct:
            starved.append(f"{t}/{arm} ({cap}/{len(rs)})")
    out.append((
        "context_starvation", bool(starved),
        ("cells where a majority of runs never converged: " + ", ".join(starved)
         + " — treat these cells as VOID until the cause is identified")
        if starved else "no cell had a majority of runs terminate at a ceiling"))

    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(prog="adh falsify")
    ap.add_argument("--csv", required=True)
    ap.add_argument("--max-not-ready-pct", type=float, default=20.0)
    args = ap.parse_args(argv)

    p = Path(args.csv)
    if not p.is_file():
        print(f"no results at {p}", file=sys.stderr)
        return 2
    rows = list(csv.DictReader(p.open()))
    if not rows:
        print("results file is empty", file=sys.stderr)
        return 2

    results = check(rows, max_not_ready_pct=args.max_not_ready_pct)
    print(f"pre-registered falsification check — {len(rows)} rows from {p}\n")
    fired = 0
    for name, hit, detail in results:
        mark = "FIRED " if hit else "ok    "
        fired += bool(hit)
        print(f"  [{mark}] {name:<20} {detail}")

    print()
    stops = Counter(r.get("stop_reason", "?") for r in rows)
    print("stop_reason: " + ", ".join(f"{k}={v}" for k, v in stops.most_common()))
    verdicts = Counter(r.get("verify_passed", "?") for r in rows)
    print("verify:      " + ", ".join(f"{k}={v}" for k, v in verdicts.most_common()))

    print()
    if fired:
        print(f"{fired} criterion/criteria fired. Treat the affected results as "
              f"provisional at best; do not publish a verdict built on them "
              f"without saying which cells are void and why.")
        return 1
    print("The round survives its own pre-registered criteria. That makes the "
          "results usable; it does not by itself make them right.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
