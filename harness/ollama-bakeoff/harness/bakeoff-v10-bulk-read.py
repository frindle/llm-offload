#!/usr/bin/env python3
"""Results-read for cell E, with host_ready reconstructed from the driver log.

WHY RECONSTRUCTION IS NEEDED: the cell-E CSV has no `host_ready` column. I built
the schema without one, so whether a run was memory-degraded exists only in
driver.log. That is a real instrument gap -- evidence should live in the data,
not in a log someone has to re-derive it from -- and it is recorded here rather
than quietly patched, because the reconstruction is only as good as the log
interleaving.

WHAT THIS DELIBERATELY REFUSES TO DO
------------------------------------
Report `sites_correct` alone as the result. gpt-oss:20b scored 3/49 while
CORRUPTING the file: per-site presence cannot see that surrounding context was
destroyed, so a site can score while the code around it is broken. The site count
and the syntax gate are one result, never two numbers to quote separately.

It also refuses to rank on a single rep. On this cell the weak models reproduce
exactly (qwen2.5-coder 2/49 twice) while the strong ones swing by 11-17 sites
between identical runs. A single high score says a model CAN do the task, not
that it WILL.
"""
import csv
import re
import statistics
import sys
from collections import defaultdict
from pathlib import Path

D = Path("/Users/user/Desktop/GitHub Projects/bakeoff/model-buildoff-2026-08-22")


def reconstruct_host_ready(log: Path):
    """Walk driver.log pairing each cell-E START with the gate line before it."""
    # ORDER MATTERS AND IT IS START-THEN-GATE, not gate-then-START. The driver
    # logs START, then stages, then restarts ollama, then prints READY/DEGRADED.
    # The first version of this function assumed the reverse, which mislabelled
    # every row and manufactured a FALSE falsifier trip (reported 27% degraded
    # against a 20% bar, and showed qwen3.8 as READY when it had degraded).
    # Caught only by running it on partial data and noticing the answer
    # contradicted what had been watched live.
    state = {}
    current = None
    for line in log.read_text(errors="replace").splitlines():
        if "v10-bulk" not in line:
            continue
        m = re.search(r"START: (\S+) rep (\d+)", line)
        if m:
            current = (m.group(1), m.group(2))
            state[current] = "unknown"
        elif current and "host-control: DEGRADED" in line:
            state[current] = "no"
        elif current and "host-control: READY" in line:
            state[current] = "yes"
    return state


def main():
    csv_path = D / "results-v10-bulk-cellE.csv"
    if not csv_path.is_file():
        csv_path = D / "results-v10-bulk.csv"
    rows = list(csv.DictReader(open(csv_path)))
    hr = reconstruct_host_ready(D / "driver.log")

    per = defaultdict(list)
    for r in rows:
        per[r["model"]].append(r)

    print(f"cell E — {len(rows)} rows from {csv_path.name}\n")
    print(f"{'model':26s} {'reps':>18s} {'mean':>6s} {'range':>7s}  {'gate':<12s} note")
    print("-" * 92)
    order = sorted(per.items(),
                   key=lambda kv: -statistics.mean(int(x["sites_correct"]) for x in kv[1]))
    for model, rs in order:
        scores = [int(x["sites_correct"]) for x in rs]
        gates = [hr.get((model, x["rep"]), "?") for x in rs]
        spread = max(scores) - min(scores)
        note = []
        if spread >= 10:
            note.append(f"VARIES by {spread}")
        if len(set(scores)) == 1 and len(scores) > 1:
            note.append("reproduces exactly")
        if any(g == "no" for g in gates):
            note.append("ran degraded")
        print(f"{model:26s} {'/'.join(map(str,scores)):>18s} "
              f"{statistics.mean(scores):6.1f} {spread:7d}  "
              f"{','.join(gates):<12s} {'; '.join(note)}")

    print("\nident vs meta (does the metacharacter trap discriminate?)")
    for model, rs in order:
        i = sum(int(x["ident_correct"]) for x in rs)
        it = sum(int(x["ident_total"]) for x in rs)
        m = sum(int(x["meta_correct"]) for x in rs)
        mt = sum(int(x["meta_total"]) for x in rs)
        ip = 100 * i / it if it else 0
        mp = 100 * m / mt if mt else 0
        flag = "  <-- INVERTED (meta > ident)" if mp > ip + 15 else ""
        print(f"  {model:26s} ident {i:3d}/{it:<3d} ({ip:5.1f}%)   "
              f"meta {m:3d}/{mt:<3d} ({mp:5.1f}%){flag}")

    deg = sum(1 for k, v in hr.items() if v == "no")
    struct = sum(1 for (mo, _), v in hr.items()
                 if v == "no" and mo in ("qwen3.8:27b-q8_0", "qwen3-coder-next:q4_K_M"))
    nonstruct_total = sum(1 for (mo, _) in hr if mo not in
                          ("qwen3.8:27b-q8_0", "qwen3-coder-next:q4_K_M"))
    print(f"\nhost gate: {deg}/{len(hr)} degraded ({struct} of them STRUCTURAL "
          f"— qwen3.8 / qwen3-coder-next, excluded by v9 falsifier #1)")
    if nonstruct_total:
        print(f"           non-structural degraded: {deg-struct}/{nonstruct_total} = "
              f"{100*(deg-struct)/nonstruct_total:.0f}%  (falsifier bar is 20%)")
    print("\nREAD sites_correct TOGETHER WITH the syntax gate. A model can score sites")
    print("while destroying the file — gpt-oss:20b did exactly that at 3/49.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
