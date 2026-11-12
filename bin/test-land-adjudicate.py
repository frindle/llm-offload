#!/usr/bin/env python3
"""Behaviour tests for land_adjudicate.py (CONCERNS adjudication + automatic execution)."""
import json, os, sys, tempfile
from pathlib import Path
BIN = Path(os.environ.get("LAND_BIN_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(BIN))
import land_adjudicate as A
import land_go

FAILS = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ((" -- " + str(extra)[:400]) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)

check("valid VERDICT line parses", A.parse_answer('blah\nVERDICT: {"decision":"accept","reasons":["fp"]}')["decision"] == "accept")
check("re_run / rerun spelling normalised", A.parse_answer('VERDICT: {"decision":"re_run","reasons":[]}')["decision"] == "re-run")
check("a usage-limit refusal is NOT an answer", A.parse_answer("You've hit your session limit, resets 7pm") is None)
check("unknown decision rejected", A.parse_answer('VERDICT: {"decision":"merge"}') is None)
check("bare JSON without the VERDICT marker is NOT an answer", A.parse_answer('{"decision":"accept","reasons":[]}') is None)
check("broken JSON rejected", A.parse_answer('VERDICT: {"decision":') is None)

PK = {"chain": "c", "diff_sha256": "d1", "blockers": ["gate verdict CONCERNS with no accepting adjudication"], "gate": {"verdict": "concerns"}, "findings": []}
def fake(ans):
    calls = []
    def f(prompt, **kw): calls.append(prompt); return 0, ans, ""
    f.calls = calls
    return f

with tempfile.TemporaryDirectory() as td:
    cl = fake('VERDICT: {"decision":"accept","reasons":["scope notes only"]}')
    a = A.adjudicate(PK, "intent", "diff", cache_dir=td, claude=cl)
    check("accept returned with reasons", a["decision"] == "accept" and a["reasons"] == ["scope notes only"], a)
    check("prompt carries the evidence and the one-line contract", "VERDICT:" in cl.calls[0] and "gate_verdict" in cl.calls[0])
    a2 = A.adjudicate(PK, "intent", "diff", cache_dir=td, claude=cl)
    check("same inputs -> cached, the model is not asked again (no verdict shopping)", a2["cached"] and len(cl.calls) == 1)
    A.adjudicate(PK, "intent", "diff CHANGED", cache_dir=td, claude=cl)
    check("new evidence (different diff) -> asked again", len(cl.calls) == 2)
    a3 = A.adjudicate(dict(PK), "x", "d", cache_dir=td, claude=fake("limit reached"))
    check("refusal -> 'unavailable', never an implicit accept", a3["decision"] == "unavailable", a3)
    # policy guard: accept with a real mechanical blocker is overridden
    pk2 = dict(PK, blockers=["repo checks FAIL on the staged branch"])
    a4 = A.adjudicate(pk2, "i", "d", cache_dir=td, claude=fake('VERDICT: {"decision":"accept","reasons":["ok"]}'))
    check("accept is overridden when a mechanical blocker exists", a4["decision"] == "re-run" and "overridden" in a4["forced"], a4)
    # no-progress: the same diff was re-run already
    hist = [{"executed": "re-run", "diff_sha256": "d1"}]
    a5 = A.adjudicate(dict(PK, chain="c5"), "i5", "d5", cache_dir=td, history=hist, claude=fake('VERDICT: {"decision":"re-run","reasons":["bad attempt"]}'))
    check("same diff re-run before -> escalated to re-spec", a5["decision"] == "re-spec" and a5.get("forced"), a5)

    # ---- execution
    land_go.DISPATCH = Path(td)
    runs = Path(td) / "ar"; (runs / "argv").mkdir(parents=True)
    (runs / "argv" / "lab.json").write_text(json.dumps({"label": "lab", "argv": ["--intent", "do X", "--label", "lab"]}))
    sp = []
    spawner = lambda c, lp: sp.append(c)
    e = A.execute({"decision": "accept", "reasons": []}, "lab", spawner=spawner, runs_dir=runs)
    check("accept executes nothing (flags land-ready only)", e["executed"] == "accept" and not sp)
    e = A.execute({"decision": "re-run", "reasons": ["x"]}, "lab", spawner=spawner, runs_dir=runs)
    check("re-run relaunches the recorded driver with --resume-harness", e["executed"] == "re-run" and sp[-1][-1] == "--resume-harness", (e, sp))
    e = A.execute({"decision": "re-spec", "reasons": ["bad spec"], "amendment": "clamp to [0,1]"}, "lab", spawner=spawner, runs_dir=runs)
    check("re-spec relaunches with the amendment and --resume-author", e["executed"] == "re-spec" and sp[-1][-1] == "--resume-author" and "clamp to [0,1]" in sp[-1][sp[-1].index("--intent") + 1], (e, sp[-1]))
    h = [{"executed": "re-run"}, {"executed": "re-run"}]
    sp.clear()
    e = A.execute({"decision": "re-run", "reasons": []}, "lab", history=h, spawner=spawner, runs_dir=runs)
    check("re-run budget exhausted (2) -> converted to re-spec", e["executed"] == "re-spec", e)
    e = A.execute({"decision": "re-spec", "reasons": []}, "lab", history=h + [{"executed": "re-spec"}], spawner=spawner, runs_dir=runs)
    check("re-spec budget exhausted (1) -> parked, nothing relaunched", e["executed"] is None and "exhausted" in e["detail"] and not sp[1:], e)
    e = A.execute({"decision": "unavailable", "reasons": ["limit"]}, "lab", spawner=spawner, runs_dir=runs)
    check("unavailable executes nothing", e["executed"] is None)
    e = A.execute({"decision": "re-run", "reasons": []}, "ghost", spawner=spawner, runs_dir=runs)
    check("no recorded argv -> reported", e["executed"] is None and "no recorded" in e["detail"], e)
    n0 = len(sp)
    e = A.execute({"decision": "re-run", "reasons": []}, "lab", spawner=spawner, runs_dir=runs, dry=True)
    check("dry run spawns nothing", e["ok"] and len(sp) == n0, (e, sp))
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
