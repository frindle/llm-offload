#!/usr/bin/env python3
"""Regression test: reviewer false code-highs (s4 5983078bfe69, s5 96a36dee6870, 2026-10-02).

Three defects, each pinned with the REAL artifacts from those two gates:
  A. code-review-agent: a REAL_DEFECT whose probe reasons from an assumption /
     admits it cannot see the code (probe_unsound) was still kept as REAL_DEFECT
     (the mirror rescue for NOT_A_DEFECT existed; this direction did not).
  B. code-review-agent: the verifier only ever saw the diff hunk, never the
     helpers the hunk calls or removes -- referenced_definitions() + REF_SOURCES.
  C. gate-on-complete merge_review: rows the reviewer put under "Uncertain --
     needs a human" (review verdict PASS WITH CAVEATS) were parsed from the
     summary table as code_high and hard-FAILed the gate.

Usage: CRA=<code-review-agent.py> GATE=<gate-on-complete.py> python3 this.py
Revert check: point CRA/GATE at the unpatched files -> must FAIL.
"""
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
FX = HERE / "fixtures" if (HERE / "fixtures").is_dir() else HERE / "test-fixtures-review-false-code-high"
CRA = Path(os.environ.get("CRA", Path.home() / "bin" / "code-review-agent.py"))
GATE = Path(os.environ.get("GATE", Path.home() / "bin" / "gate-on-complete.py"))

sys.path.insert(0, str(Path.home() / "bin"))   # sibling modules (darkbloom_chat)
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name
          + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class FakeModel:
    def __init__(self, payload):
        self.payload, self.prompts = payload, []

    def chat_json(self, system, user, schema, num_predict=0):
        self.prompts.append(user)
        return dict(self.payload)


def verifier_payload(run_json):
    f = json.loads((FX / run_json).read_text())["findings"][0]
    keep = ("probe", "probe_shows_difference", "concrete_trigger")
    out = {k: f[k] for k in keep if k in f}
    out["verdict"] = f.get("downgraded_from") or f["verdict"]
    out["reason"] = f.get("verdict_reason", "")
    return f, out


def finding_for(cra, diff_name, saved):
    hunks = cra.parse_diff((FX / diff_name).read_text())
    h = [h for h in hunks if h.path == "lib/rivian.ts"][0]
    return {"path": h.path, "hunk_text": h.text(), "quote": saved["quote"],
            "claim": saved["claim"], "failure_scenario": saved["failure_scenario"],
            "severity": "high", "source": "model"}


def stats0():
    return {"quote_rejected": 0, "no_failure_scenario": 0, "review_parse_fail": 0,
            "verify_parse_fail": 0, "diagnose_parse_fail": 0, "unfalsifiable": 0}


cra = load(CRA, "cra")
cra.log = lambda *a, **k: None

# --- A. s5: REAL_DEFECT on an assumed premise must not stand ------------------
saved, payload = verifier_payload("s5-regate-run.json")
chk("A s5 fixture: verifier said REAL_DEFECT", payload["verdict"], "REAL_DEFECT")
f = finding_for(cra, "s5.diff", saved)
getattr(cra, "REF_SOURCES", {}).clear()
out = cra.stage_verify(FakeModel(payload), [f], "intent", stats0())
chk("A s5: REAL_DEFECT resting on an unsound probe is demoted to UNSURE",
    out[0]["verdict"], "UNSURE")

# A2: a blind-spot admission without the word 'assume' is unsound too
blind = ("Does fetchRivianServiceThreads() use VS_GATEWAY? We don't see its "
         "implementation. Without seeing the old helper we cannot confirm this.")
chk("A2 blind-spot admission counts as unsound",
    bool(cra.probe_is_unsound(blind)), True)
# A3: a sound probe still lets a REAL_DEFECT stand (no over-demotion)
sound = dict(payload, probe=("OLD gql(Q, {}, h, VS_GATEWAY) routes to VS_GATEWAY; "
                             "NEW gql(GET_ASYNC_MESSAGE_THREAD_LIST, {}, authHeaders(tokens)) "
                             "routes to GATEWAY: different endpoint for the same query."),
             probe_shows_difference=True)
out = cra.stage_verify(FakeModel(sound), [finding_for(cra, "s5.diff", saved)], "intent", stats0())
chk("A3 a sound probe keeps REAL_DEFECT", out[0]["verdict"], "REAL_DEFECT")

# --- B. the verifier is shown the helpers the hunk calls/removes --------------
has_defs = hasattr(cra, "referenced_definitions")
chk("B referenced_definitions exists", has_defs, True)
if has_defs:
    s5_hunk = finding_for(cra, "s5.diff", saved)["hunk_text"]
    base5 = (FX / "s5-base-rivian.ts").read_text()
    defs = cra.referenced_definitions(s5_hunk, base5, "")
    chk("B s5: removed helper fetchRivianServiceThreads body is shown",
        "function fetchRivianServiceThreads" in defs, True)
    body = cra._definition_of("fetchRivianServiceThreads", base5)
    chk("B s5: ...and it shows the helper's gql call WITHOUT VS_GATEWAY (refutes the claim)",
        ("GET_ASYNC_MESSAGE_THREAD_LIST" in body, "VS_GATEWAY" in body), (True, False))
    saved4, _ = verifier_payload("s4-regate-run.json")
    s4_hunk = finding_for(cra, "s4.diff", saved4)["hunk_text"]
    base4 = (FX / "s4-base-rivian.ts").read_text()
    defs4 = cra.referenced_definitions(s4_hunk, base4, "")
    chk("B s4: pickActiveWorkOrder body (with its Array.isArray guard) is shown",
        "Array.isArray(threads)" in defs4, True)
    cra.REF_SOURCES["lib/rivian.ts"] = (base5, "")
    cra.REF_BUDGET = cra.ref_budget_for(32768) if hasattr(cra, "ref_budget_for") else 6000
    fm = FakeModel(payload)
    cra.stage_verify(fm, [finding_for(cra, "s5.diff", saved)], "intent", stats0())
    chk("B verifier prompt carries REFERENCED DEFINITIONS when the repo is known",
        "REFERENCED DEFINITIONS" in fm.prompts[0], True)
    cra.REF_SOURCES.clear()
    fm = FakeModel(payload)
    cra.stage_verify(fm, [finding_for(cra, "s5.diff", saved)], "intent", stats0())
    chk("B no repo -> prompt unchanged (hunk-only, as before)",
        "REFERENCED DEFINITIONS" in fm.prompts[0], False)

# --- D. the helper block is sized from the tier's num_ctx ---------------------
has_budget = hasattr(cra, "ref_budget_for") and hasattr(cra, "effective_ref_budget")
chk("D ref_budget_for / effective_ref_budget exist", has_budget, True)
if has_budget and has_defs:
    chk("D pregate window (6144) -> no helper block", cra.ref_budget_for(6144), 0)
    chk("D 8192 -> no helper block", cra.ref_budget_for(8192), 0)
    chk("D regate window (32768) -> ~6k chars", 5000 <= cra.ref_budget_for(32768) <= 7000, True)
    chk("D grows with the window and is capped",
        (cra.ref_budget_for(65536) > cra.ref_budget_for(32768), cra.ref_budget_for(10**7) <= 24000),
        (True, True))
    chk("D a tier request can only shrink, never exceed, the actual-ctx budget",
        (cra.effective_ref_budget(6144, 6000), cra.effective_ref_budget(32768, 100000),
         cra.effective_ref_budget(32768, 2000)),
        (0, cra.ref_budget_for(32768), 2000))
    big = cra.referenced_definitions(s5_hunk, base5, "", budget=cra.ref_budget_for(32768))
    chk("D emitted block never exceeds the budget", len(big) <= cra.ref_budget_for(32768), True)

# Behavioural, independent of helper names: a verifier running at the pregate
# window (num_ctx 6144) with the repo known must NOT get the helper block.
if has_defs:
    cra.REF_SOURCES["lib/rivian.ts"] = ((FX / "s5-base-rivian.ts").read_text(), "")
    if hasattr(cra, "effective_ref_budget"):
        cra.REF_BUDGET = cra.effective_ref_budget(6144, 6000)   # what main() sets at 6144
    fm = FakeModel(payload)
    cra.stage_verify(fm, [finding_for(cra, "s5.diff", saved)], "intent", stats0())
    chk("D at num_ctx 6144 the verifier prompt carries NO helper block",
        "REFERENCED DEFINITIONS" in fm.prompts[0], False)
    cra.REF_SOURCES.clear()

# --- C. gate: an UNCERTAIN row must not become code_high ----------------------
gate = load(GATE, "gate")


def _boom(*a, **k):
    raise RuntimeError("test must not reach a live side effect")


gate._escalate_regate = _boom
# SANDBOX the gate's writable roots. merge_review runs the post-verdict hooks
# (auto-fix ledger, completed-code drop, janitor log), which write under BIN /
# COMPLETED_ROOT; with the REAL ids of s4/s5 this rewrote the live
# ollama-queue-logs/auto-fix/<id>.rounds.json (2026-10-02). Reads that matter
# here come from the temp out_dir, so a temp BIN is safe.
_SANDBOX = Path(tempfile.mkdtemp(prefix="rfch-sandbox-"))
(_SANDBOX / "ollama-queue-logs" / "auto-fix").mkdir(parents=True)
_REAL_AUTOFIX = Path(gate.BIN) / "ollama-queue-logs" / "auto-fix"
gate.BIN = _SANDBOX
gate.COMPLETED_ROOT = _SANDBOX / "Completed"
gate.TEST_MODE = True


def _real_ledger_snapshot():
    return {p.name: p.stat().st_mtime_ns for p in _REAL_AUTOFIX.glob("*.rounds.json")} \
        if _REAL_AUTOFIX.is_dir() else {}


_REAL_BEFORE = _real_ledger_snapshot()
gate.subprocess = types.SimpleNamespace(run=_boom, Popen=_boom, PIPE=-1, DEVNULL=-3,
                                        TimeoutExpired=Exception, CalledProcessError=Exception)


def merge(fixture_gate, fixture_report, parent):
    td = Path(tempfile.mkdtemp())
    try:
        pl = json.loads((FX / fixture_gate).read_text())
        pl["issues"] = [i for i in pl.get("issues", []) if i.get("source") != "review"]
        for k in ("gate_authority", "regate", "verdict", "counts", "review_verdict"):
            pl.pop(k, None)
        pl["verdict"] = "pass-pending-review"
        (td / f"{parent}.gate.json").write_text(json.dumps(pl))
        rd = td / f"regate-{parent}"
        rd.mkdir()
        shutil.copy(FX / fixture_report, rd / "report.md")
        a = types.SimpleNamespace(job_label=f"regate-{parent}", cwd=str(rd), job_id="t0",
                                  out_dir=str(td))
        try:
            gate.merge_review(a, td, prefix="regate-", authoritative=True)
        except RuntimeError as e:
            print("   (merge stopped at a guarded side effect:", e, ")")
        return json.loads((td / f"{parent}.gate.json").read_text())
    finally:
        shutil.rmtree(td, ignore_errors=True)


chk("C/D gate computes the tier budget: pregate 0, regate > 0",
    (hasattr(gate, "ref_budget_for") and gate.ref_budget_for(gate.PREGATE_NUM_CTX) == 0,
     hasattr(gate, "ref_budget_for") and gate.ref_budget_for(gate.REGATE_NUM_CTX) > 0),
    (True, True))
src = GATE.read_text()
chk("C/D gate writes ref_budget into both reviewer task files",
    (src.count('"ref_budget": ref_budget_for(REGATE_NUM_CTX)'),
     src.count('"ref_budget": ref_budget_for(PREGATE_NUM_CTX)')), (1, 1))
g4 = merge("s4.gate.json", "s4-regate-report.md", "5983078bfe69")
chk("C s4: PASS WITH CAVEATS (uncertain row only) -> code_high 0",
    g4.get("counts", {}).get("code_high"), 0)
chk("C s4: ...and the gate does not hard-FAIL", g4.get("verdict") != "fail", True)
# GATE FINDING CHECK (2026-10-04): s4's only finding is the reviewer's own UNCERTAIN
# caveat on lib/rivian.ts (not mechanically checkable) on a green, relevance-proven
# verify -> it is now recorded as an ADVISORY caveat instead of parking the slice.
# Visibility is what this check pins: it must stay on the record either way.
chk("C s4: ...but the caveat stays visible (as a code finding, or as an advisory caveat)",
    [i.get("uncertain") for i in g4.get("issues", []) if i.get("source") == "review"]
    or [i.get("uncertain") for i in g4.get("advisory_caveats", [])
        if i.get("source") == "review" and i.get("finding_check") == "advisory"], [True])
g5 = merge("s5.gate.json", "s5-regate-report.md", "96a36dee6870")
chk("C s5 report (a row under '## Defects') still counts as code_high -> fail",
    (g5.get("counts", {}).get("code_high"), g5.get("verdict")), (1, "fail"))
chk("sandbox: the live auto-fix ledger is untouched by this test",
    _real_ledger_snapshot() == _REAL_BEFORE, True)
shutil.rmtree(_SANDBOX, ignore_errors=True)

print(f"--- {fails} failed ---")
sys.exit(1 if fails else 0)
