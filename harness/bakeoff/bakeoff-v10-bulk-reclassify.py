#!/usr/bin/env python3
"""Fix #1 (Opus diagnosis 2026-09-09): a bake-off cell where the model called NO
tool at all must be marked INVALID/untested, not scored as 0/N — otherwise a
model that simply answered in prose (never invoked a tool) masquerades as
codemod incapacity. First hit: devstral:24b 0/49 stop=converged, which was pure
prose with zero tool executions.

STANDALONE + READ-ONLY on the live CSV. The bake-off driver appends to
results-v10-bulk-cellE.csv while running, so this NEVER writes that file — it
reads it, inspects each cell's worker log, and emits a DERIVED
`*-reclassified.csv` plus a summary. Safe to run mid-run.

Signal: the worker logs one `[worker] tool <name>(...) -> ...` line per EXECUTED
tool call. Zero such lines == the model never invoked a tool == INVALID cell,
regardless of stop_reason (a 0-tool cell always has 0 sites anyway).
"""
import csv, re, sys
from pathlib import Path

DEFAULT_CSV = (Path(__file__).parent / "model-buildoff-2026-08-22"
               / "results-v10-bulk-cellE.csv")
TOOL_EXEC = re.compile(r"^\[worker\] tool ")

def count_tool_execs(log_path: Path) -> int | None:
    if not log_path.is_file():
        return None
    n = 0
    with log_path.open(errors="replace") as f:
        for line in f:
            if TOOL_EXEC.match(line):
                n += 1
    return n

def main():
    csv_path = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_CSV
    if not csv_path.is_file():
        print(f"CSV not found: {csv_path}", file=sys.stderr)
        return 1
    log_dir = csv_path.parent
    rows = list(csv.reader(csv_path.open()))
    if not rows:
        print("empty CSV", file=sys.stderr)
        return 1
    header, data = rows[0], rows[1:]
    ti = header.index("transcript")
    si = header.index("stop_reason")
    sc = header.index("sites_correct")

    out_header = header + ["tools_executed", "verdict"]
    out_rows = []
    invalid, unknown = [], []
    for r in data:
        if len(r) <= ti:
            continue
        log = log_dir / r[ti]
        n = count_tool_execs(log)
        if n is None:
            verdict, ncol = "UNKNOWN_log_missing", "NA"
            unknown.append(r)
        elif n == 0:
            verdict, ncol = "INVALID_no_tool_calls", "0"
            invalid.append(r)
        else:
            verdict, ncol = "VALID", str(n)
        out_rows.append(r + [ncol, verdict])

    out_path = csv_path.with_name(csv_path.stem + "-reclassified.csv")
    with out_path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(out_header)
        w.writerows(out_rows)

    print(f"read {len(data)} cells from {csv_path.name}")
    print(f"wrote {out_path.name} (+tools_executed, +verdict)")
    print(f"  VALID:   {len(data) - len(invalid) - len(unknown)}")
    print(f"  INVALID (0 tools called): {len(invalid)}")
    for r in invalid:
        print(f"    - {r[0]} rep{r[2]} stop={r[si]} scored {r[sc]}/{header and r[header.index('sites_total')]}"
              f"  -> INVALID/untested (never invoked a tool)")
    if unknown:
        print(f"  UNKNOWN (log missing): {len(unknown)}")
        for r in unknown:
            print(f"    - {r[0]} rep{r[2]} (log {r[ti]} not found)")
    return 0

if __name__ == "__main__":
    sys.exit(main())
