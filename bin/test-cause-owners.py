#!/usr/bin/env python3
"""Tests for pipeline_cause_owners.py (cause -> owner table + lint + flip attribution).
Usage: test-cause-owners.py [--bin DIR]   (DIR holds the module + the pipeline sources;
default = this file's directory). Prints CAUSE_OWNERS_OK on success."""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
BIN = Path(sys.argv[sys.argv.index("--bin") + 1]) if "--bin" in sys.argv else HERE
spec = importlib.util.spec_from_file_location("pco", BIN / "pipeline_cause_owners.py")
pco = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pco)

fails = []


def check(name, got, want):
    ok = got == want
    print(("ok     " if ok else "FAIL   ") + name + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        fails.append(name)


# Real historical flips (fields trimmed from ollama-queue-logs/<id>.gate.json, 2026-10).
FLIPS = {
    # pre fail -> pass, regate authoritative, clean baseline: ONE owner (the regate)
    "1557f87e773b": ({"job_id": "1557f87e773b", "pregate_verdict": "fail", "verdict": "pass",
                      "gate_authority": "studio-27b-regate", "regate": "done"},
                     "owned", ["gate-on-complete.py:merge_review"]),
    # pre pass -> concerns, regate authoritative: ONE owner
    "143d9acfe43a": ({"job_id": "143d9acfe43a", "pregate_verdict": "pass", "verdict": "concerns",
                      "gate_authority": "studio-27b-regate", "regate": "done"},
                     "owned", ["gate-on-complete.py:merge_review"]),
    # pre fail -> concerns where the regate AND the dirty-launch-baseline guard both set the
    # verdict: ONE owner (the regate); the baseline guard is a recorded contributor, not a second owner
    "b90e77ace2b2": ({"job_id": "b90e77ace2b2", "pregate_verdict": "fail", "verdict": "concerns",
                      "gate_authority": "studio-27b-regate", "regate": "done",
                      "untrusted": ["launch baseline was dirty (1 path(s))"]},
                     "owned", ["gate-on-complete.py:merge_review"]),
}

for jid, (gate, status, owners) in FLIPS.items():
    r = pco.attribute_flip(gate)
    check(f"flip {jid} attribution status", r["status"], status)
    check(f"flip {jid} owners", r["owners"], owners)

# a flip with NO recorded mechanism is flagged unowned, never silently attributed
r = pco.attribute_flip({"pregate_verdict": "pass", "verdict": "fail", "gate_authority": None})
check("flip with no evidence is unowned", (r["flip"], r["status"]), (True, "unowned"))
# no flip -> not attributed at all
check("pass->pass is no flip", pco.attribute_flip({"pregate_verdict": "pass", "verdict": "pass"})["status"], "no-flip")
check("no pregate verdict is no flip", pco.attribute_flip({"verdict": "pass"})["flip"], False)

# the dirty-baseline guard is a contributor to an authoritative flip, and owns it only alone
r = pco.attribute_flip(FLIPS["b90e77ace2b2"][0])
check("dirty-baseline recorded as contributor", r["contributors"], ["ollama-queue.py:seal_prev_round_baseline"])
r = pco.attribute_flip({"pregate_verdict": "pass", "verdict": "fail", "untrusted": ["dirty"]})
check("dirty-baseline alone owns the flip", (r["status"], r["owners"]), ("owned", ["ollama-queue.py:seal_prev_round_baseline"]))

# live table vs code
probs = pco.lint(BIN)
check("lint(real code) is clean", probs, [])

# every cause names an owner + invariants + owners exist
for c, s in pco.CAUSES.items():
    if not (s["owner"]["file"] and s["owner"]["func"] and s["leaves"]):
        check(f"{c} fully specified", False, True)
check("the five core failure classes are in the table",
      all(f"failure_class:{c}" in pco.CAUSES for c in ("model", "context", "harness", "spec", "stale-base")), True)
check("stage-check-failed + stale-base are owned",
      (pco.owner_key("attempt:stage-check-failed"), pco.owner_key("failure_class:stale-base")),
      ("ollama-dispatch-auto:classify_author_failure", "ollama-queue.py:refresh_stale_base"))

# lint bites: a code-referenced cause missing from the table, and a dangling owner
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    for n in (pco.Q, pco.A, pco.G, pco.H, pco.C):
        t = (BIN / n).read_text(errors="replace")
        if n == pco.G:
            t = t.replace('["gate_authority"] = "review-missing-terminal"', '["gate_authority"] = "brand-new-authority"')
        if n == pco.Q:
            t = t.replace("def refresh_stale_base(", "def refresh_stale_base_gone(")
        (d / n).write_text(t)
    p = pco.lint(d)
    check("lint flags an unlisted cause", any("brand-new-authority" in x and "missing from table" in x for x in p), True)
    check("lint flags a stale table cause", any("review-missing-terminal" in x and "stale" in x for x in p), True)
    check("lint flags a missing owner function", any("refresh_stale_base does not exist" in x for x in p), True)

# live scan: every flip gets a defined status (owned / unowned / double-owned)
logs = BIN / "ollama-queue-logs"
if not logs.is_dir():
    logs = HERE / "ollama-queue-logs"
flips = pco.scan_flips(logs) if logs.is_dir() else []
check("live flips all have a defined status",
      all(f["status"] in ("owned", "unowned", "double-owned") for f in flips), True)

print(f"{len(fails)} failed" if fails else "CAUSE_OWNERS_OK")
sys.exit(1 if fails else 0)
