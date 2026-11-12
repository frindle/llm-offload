#!/usr/bin/env python3
"""Phase 2 item 5: a relevance SURVIVOR with VERIFY_OK goes back as a bounded harness
refine (request file + counter + auto-decision + driver relaunch seam), not a park; the
HARNESS_REFINE_CAP-th time it falls through to the escalation, reworded as RE-SPEC.
Run: python3 test-gate-harness-refine.py   (prints ALL PASS)"""
import importlib.util, json, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(sys.argv[sys.argv.index("--src") + 1]) if "--src" in sys.argv else HERE / "gate-on-complete.py"
FAILS = []
home = Path(tempfile.mkdtemp(prefix="ghr-"))
os.environ["OLLAMA_DISPATCH_HOME"] = str(home)
os.environ["GATE_BIN"] = str(HERE)
os.environ["GATE_TEST_MODE"] = "1"


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


ld = SourceFileLoader("goc_ghr", str(SRC))
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("goc_ghr", ld))
sys.argv = [str(SRC)]
ld.exec_module(m)

payload = {"verdict": "concerns", "issues": [],
           "verify_relevance": {"verdict": "low", "survivors": [
               {"file": "app/route.ts", "line": 12, "mutation": "replace call with undefined"}]}}
dec = m.autofix_classify(payload)
check("classify still says escalate/grader-suspect-relevance", (dec["action"], dec["class"]),
      ("escalate", "grader-suspect-relevance"))

(home / "auto-runs" / "argv").mkdir(parents=True)
(home / "auto-runs" / "argv" / "lbl.json").write_text(json.dumps({"argv": ["x", "--label", "lbl"], "cwd": str(home)}))
launched = []
d1 = m.harness_refine_apply(dec, payload, "lbl [auto-fix r1]", launch=lambda cmd, cwd: launched.append(cmd) or 4242)
check("1st: refine, not escalate", (d1["class"], d1["action"]), ("harness-refine", "harness-refine"))
check("1st: no driver relaunched (old auto driver retired)", launched, [])
rec = json.loads((home / "harness-refine" / "lbl.json").read_text())
check("1st: counter + survivors persisted", (rec["count"], rec["survivors"][0]["line"]), (1, 12))
log = (home / "auto-decisions.jsonl").read_text()
check("1st: auto-decision logged", '"harness-refine"' in log and '"refine"' in log, True)
d2 = m.harness_refine_apply(dec, payload, "lbl", launch=lambda c, w: 1)
check("2nd: still refine", d2["action"], "harness-refine")
d3 = m.harness_refine_apply(dec, payload, "lbl", launch=lambda c, w: 1)
check("3rd (cap spent): back to escalate", (d3["action"], d3["class"]), ("escalate", "grader-suspect-relevance"))
check("3rd: reworded as RE-SPEC", "re-spec" in d3["reasons"][0].lower(), True)
other = {"class": "undecidable", "action": "escalate", "reasons": ["x"]}
check("non-relevance classes untouched", m.harness_refine_apply(other, payload, "lbl"), other)
check("no label -> untouched", m.harness_refine_apply(dec, payload, ""), dec)
print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
