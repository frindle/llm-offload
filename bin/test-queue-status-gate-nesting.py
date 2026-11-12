#!/usr/bin/env python3
"""Both-ways + revert-test for `ollama-queue.py status` GATE NESTING.

The owner, live report: the status listing showed gate/regate rows scattered anywhere in
the queue -- nowhere near the slice they were gating -- because cmd_status was a flat
loop over state["jobs"] in raw insertion order with no grouping at all.

The fix groups rows by BUNDLE (_launch_plan_key, the scheduler's own notion) and nests
each `gate-<id>` / `regate-<id>` row directly under the dispatch row it reviews (the
gate -> source link is carried by the LABEL, via _gate_source_id -- there is no separate
foreign-key field on the job).

Both ways, on ONE seeded state whose gate rows are deliberately inserted out of order:
  1. BITES:     the real file nests the gates under their source and keeps bundles whole.
  2. NOT INERT: a mutated copy with the flat loop restored (the pre-fix code) prints the
                raw insertion order -- i.e. this test FAILS against the old code.
  3. LOSSLESS:  regrouping emits every input row exactly once -- nothing dropped, no dupes.
  4. ANNOTATIONS PRESERVED: the per-line annotations (hold reason, dep, escalation) still
                render, so this stayed a grouping fix and not a rewrite of the row text.

Runs entirely against an isolated HOME (temp state file); the live queue is never opened.

Run: python3 test-queue-status-gate-nesting.py
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

QUEUE = Path(os.environ.get("QUEUE_UNDER_TEST",
                            str(Path(__file__).resolve().parent / "ollama-queue.py")))
FAILS = []

_FIX = ('    for j, _depth in status_display_order(state["jobs"]):\n'
        '        indent = "  " * _depth\n')
_FLAT = ('    for j in state["jobs"]:\n'
         '        indent = ""\n')

# Gate rows deliberately scattered: gate-aaa1 sits between an UNRELATED bundle's row
# and its own bundle's next slice; regate-aaa1 is appended dead last. This is exactly
# the shape the owner saw.
JOBS = [
    {"id": "aaa1", "label": "bg-demo-s1-alpha", "status": "done",
     "model": "qwen", "host": "studio", "exit_code": 0},
    {"id": "ccc1", "label": "bg-other-s1-zulu", "status": "pending",
     "model": "qwen", "host": "studio"},
    {"id": "g1", "label": "gate-aaa1", "status": "running",
     "model": "gate-model", "host": "unraid"},
    {"id": "aaa2", "label": "bg-demo-s2-beta", "status": "held",
     "model": "qwen", "host": "studio", "hold_reason": "pending-gate", "held_on": "g1"},
    {"id": "g3", "label": "gate-ccc1", "status": "pending",
     "model": "gate-model", "host": "unraid"},
    {"id": "g2", "label": "regate-aaa1", "status": "pending",
     "model": "gate-model", "host": "unraid"},
    {"id": "ddd1", "label": "gate-reaped999", "status": "pending",
     "model": "gate-model", "host": "unraid"},
]

INSERTION_ORDER = [j["id"] for j in JOBS]
# bundles in first-appearance order; gates nested under their source, in list order.
EXPECTED = ["aaa1", "g1", "g2", "aaa2", "ccc1", "g3", "ddd1"]
NESTED = {"g1", "g2", "g3"}      # ddd1's source was reaped -> stays a top-level row


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (("  -- " + detail) if not cond else ""))
    if not cond:
        FAILS.append(name)


def fresh_home():
    home = Path(tempfile.mkdtemp())
    (home / "bin").mkdir()
    (home / "bin" / "ollama-queue-state.json").write_text(json.dumps({"jobs": JOBS}))
    return home


def run_status(script):
    home = fresh_home()
    try:
        r = subprocess.run([sys.executable, str(script), "status"],
                           capture_output=True, text=True,
                           env=dict(os.environ, HOME=str(home)), timeout=180)
    finally:
        shutil.rmtree(home, ignore_errors=True)
    return r


def parse(out):
    """[(job_id, indent_len)] for each rendered row."""
    rows = []
    for ln in out.splitlines():
        m = re.match(r"^(\s*)\[\s*\S+\s*\]\s+(\S+)\s", ln)
        if m:
            rows.append((m.group(2), len(m.group(1))))
    return rows


def mutated_copy(src_text, tmpdir):
    p = Path(tmpdir) / "ollama-queue-flat.py"
    p.write_text(src_text)
    return p


src = QUEUE.read_text()
check("fix marker present in ollama-queue.py (test is current)", _FIX in src,
      "status_display_order loop not found -- update this test")

# --- 1. BITES: the real file ------------------------------------------------------
r = run_status(QUEUE)
check("status exits 0", r.returncode == 0, f"rc={r.returncode} err={r.stderr[-400:]}")
rows = parse(r.stdout)
got = [rid for rid, _ in rows]
check("gates nest under their source, bundles stay whole", got == EXPECTED,
      f"got={got} want={EXPECTED}\n{r.stdout}")
check("the fix actually REORDERS (it is not the insertion order by luck)",
      got != INSERTION_ORDER, f"got={got}")

indents = dict(rows)
check("nested gate rows are INDENTED",
      all(indents.get(g) == 2 for g in NESTED),
      f"indents={indents}")
check("dispatch rows and an orphaned gate stay at depth 0",
      all(indents.get(k) == 0 for k in ("aaa1", "aaa2", "ccc1", "ddd1")),
      f"indents={indents}")

# --- 3. LOSSLESS ------------------------------------------------------------------
check("every queued row is printed exactly once",
      sorted(got) == sorted(INSERTION_ORDER), f"got={sorted(got)}")

# --- 4. ANNOTATIONS PRESERVED ------------------------------------------------------
check("hold annotation still rendered on the held row",
      any(rid == "aaa2" for rid, _ in rows) and "pending-gate->g1" in r.stdout,
      r.stdout)
check("row text format unchanged (label/model/host/pid/exit columns)",
      re.search(r"\[done\s*\] aaa1\s+bg-demo-s1-alpha\s+qwen\s+host=studio\s+pid=None\s+"
                r"exit=0", r.stdout) is not None, r.stdout)

# --- 2. NOT INERT: revert by excision -- the pre-fix flat loop ----------------------
tmp = tempfile.mkdtemp()
try:
    flat = mutated_copy(src.replace(_FIX, _FLAT), tmp)
    rf = run_status(flat)
    check("mutant (flat loop) still runs", rf.returncode == 0,
          f"rc={rf.returncode} err={rf.stderr[-400:]}")
    got_flat = [rid for rid, _ in parse(rf.stdout)]
    check("REVERT-TEST: flat loop reproduces the bug (raw insertion order)",
          got_flat == INSERTION_ORDER, f"got={got_flat}")
    check("REVERT-TEST: flat loop FAILS the nesting assertion (this test kills it)",
          got_flat != EXPECTED, f"got={got_flat}")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S): " + ", ".join(FAILS))
    sys.exit(1)
print("all checks passed")
