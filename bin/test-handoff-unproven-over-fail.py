#!/usr/bin/env python3
"""handoff-emit final_verdict: an INCONCLUSIVE reviewer must not mask a machine FAIL
(2026-10-05, s3fix). The 16 replay-endorse s3 auto-author jobs had a RED verify
(gate.json verdict 'fail') but showed 'UNPROVEN' on the panel because the qwen3:14b
pre-gate reviewer hit its token cap -- it read as a review-path problem.

Usage:  python3 test-handoff-unproven-over-fail.py          -> ALL PASS
        HANDOFF=<handoff-emit .bak> python3 ...             -> FAILs (revert-check)
"""
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("HANDOFF") or HERE / "handoff-emit.py")
ld = SourceFileLoader("handoff_t", str(SRC))
sp = importlib.util.spec_from_loader("handoff_t", ld)
h = importlib.util.module_from_spec(sp)
ld.exec_module(h)

fails = []


def chk(name, got, want):
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        fails.append(name)


REPORT = {}
h._report_verdict = lambda jid, kind: REPORT.get(kind)

# feb75357ae3c's real gate.json shape: machine fail, reviewer UNPROVEN
REPORT = {"review": "UNPROVEN"}
g = {"verdict": "fail", "review_verdict": "UNPROVEN", "regate": "not-warranted"}
v = h.final_verdict("feb75357ae3c", g)
chk("machine FAIL + reviewer UNPROVEN -> shows fail, not UNPROVEN",
    str(v).lower().startswith("fail"), True)
chk("...and still names the reviewer's verdict", "UNPROVEN" in str(v), True)
REPORT = {"review": "CONCERNS"}
chk("machine FAIL + reviewer CONCERNS -> shows fail",
    str(h.final_verdict("x", {"verdict": "fail"})).lower().startswith("fail"), True)
# unchanged behaviour
REPORT = {"review": "PASS"}
chk("machine FAIL + reviewer PASS keeps the existing floor text",
    h.final_verdict("x", {"verdict": "fail"}), "fail (reviewer said PASS; decidable finding stands)")
REPORT = {"review": "FAIL"}
chk("reviewer FAIL passes through", h.final_verdict("x", {"verdict": "fail"}), "FAIL")
REPORT = {"review": "UNPROVEN"}
chk("machine CONCERNS + reviewer UNPROVEN is untouched (not a decidable fail)",
    h.final_verdict("x", {"verdict": "concerns"}), "UNPROVEN")
REPORT = {"review": "PASS"}
chk("clean PASS untouched", h.final_verdict("x", {"verdict": "pass"}), "PASS")

print()
print("ALL PASS" if not fails else f"{len(fails)} FAIL")
sys.exit(1 if fails else 0)
