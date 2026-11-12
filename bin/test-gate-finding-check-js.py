#!/usr/bin/env python3
"""GATE FINDING CHECK, TypeScript/JavaScript path (cardBonuses-v2 964c4a833b82, 2026-10-14).

The real case: the 27b regate reviewer FAILED a correct lib/cardBonuses.ts with a HIGH finding whose
"Verified trigger" (spend 1e15, perDollars 1 -> 999999999999999.9, under-counts by 1) is false:
1e15/1 === 1e15 exactly. The finding check was Python-only, so the false FAIL parked as needs_opus.

Fixtures (test-fixtures-gate-finding-check/js-cardbonus/): the job's real gate.json + regate report,
base/ (launch baseline), head/ (baseline + the gated diff). Reproducers are HAND-WRITTEN (the executor
is validated against ground truth, not against the model that made the claim).

Proves: lang_of / plan accept .ts/.js (and still refuse .tsx / out-of-diff); the executor returns
UNCONFIRMED for the false claim, CONFIRMED for a genuine under-count (counterfactual head without the
1e-9 epsilon), pre-existing when the baseline fails identically, and never counts a reproducer that
does not touch the cited file (vacuous / broken) as a confirmation; parse_reply enforces the JS shape.
Run: python3 test-gate-finding-check-js.py   (exit 0, prints JS_FCHECK_OK)
"""
import json, os, shutil, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import gate_finding_check as g

FX = HERE / "test-fixtures-gate-finding-check" / "js-cardbonus"
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {want!r} got {got!r}"))
    if not ok:
        FAILS.append(name)


if not g._node():
    print("SKIP (no node)")
    print("JS_FCHECK_OK")
    sys.exit(0)

CITED = "lib/cardBonuses.ts"
payload = json.loads((FX / "gate.json").read_text())

# --- lang_of / plan ---------------------------------------------------------------------------
chk("lang_of .py", g.lang_of("a/b.py"), "py")
chk("lang_of .ts", g.lang_of("lib/x.ts"), "js")
chk("lang_of .mjs", g.lang_of("x.mjs"), "js")
chk("lang_of .tsx is not checkable", g.lang_of("c.tsx"), "")
chk("lang_of .md", g.lang_of("README.md"), "")
chk("real gate record, relevance LOW -> none (fixture not proven)", g.plan(payload, [CITED])["action"], "none")
rel = json.loads(json.dumps(payload))
rel["verify_relevance"] = {"verdict": "relevant", "survivors": []}
chk("same record, relevance relevant -> check (TS finding is now checkable)", g.plan(rel, [CITED])["action"], "check")
chk("...but a finding outside the diff -> none", g.plan(rel, ["other.ts"])["action"], "none")
tsx = json.loads(json.dumps(rel)); tsx["issues"][0]["file"] = "components/X.tsx"
chk(".tsx finding -> none", g.plan(tsx, ["components/X.tsx"])["action"], "none")

# --- parse_reply ------------------------------------------------------------------------------
good = "import assert from 'node:assert/strict';\nexport function reproduce(): void { assert.ok(true); }\n"
src, _, prob = g.parse_reply({"reproducer": good, "explanation": "e"}, "js")
chk("parse_reply js ok", (src is not None, prob), (True, None))
chk("parse_reply js without export -> problem",
    g.parse_reply({"reproducer": "function reproduce() {}\n", "explanation": ""}, "js")[2] is not None, True)
chk("parse_reply js syntax error -> problem",
    "compile" in (g.parse_reply({"reproducer": "export function reproduce() { ((( }\n", "explanation": ""}, "js")[2] or ""), True)
chk("parse_reply strips a ```typescript fence",
    g.parse_reply({"reproducer": "```typescript\n" + good + "```", "explanation": ""}, "js")[0] is not None, True)

# --- executor ---------------------------------------------------------------------------------
def rp(name):
    return (FX / "repros" / name).read_text()


head, base = FX / "head", FX / "base"
r = g.run_reproducer(head, rp("fp-1e15.mts"), CITED)
chk("false claim: reviewer's own trigger PASSES on the gated code", r.get("kind"), "pass")
chk("...and it really ran the cited file (non-vacuous)", r.get("touched_cited"), True)
chk("classify -> unconfirmed", g.classify_row(r, None)["outcome"], "unconfirmed")

v = g.run_reproducer(head, rp("vacuous.mts"), CITED)
chk("vacuous reproducer (never touches the cited file) is NOT a failure", v.get("kind"), "invalid")
chk("classify vacuous -> no-reproducer (lands as advisory, never confirms)", g.classify_row(v, None)["outcome"], "no-reproducer")
v2 = g.run_reproducer(head, rp("vacuous-import-only.mts"), CITED)
chk("importing the cited module without calling it does not count as touching it (top-level is setup)",
    (v2.get("kind"), v2.get("touched_cited")), ("invalid", False))
b = g.run_reproducer(head, rp("broken.mts"), CITED)
chk("reproducer that itself throws a TypeError is invalid, not a confirmation", b.get("kind"), "invalid")

# an exception thrown BY the code under test (not an assertion) is a failure attributed to the product
tmp0 = Path(tempfile.mkdtemp(prefix="test-gfc-js-thr-"))
try:
    thr = tmp0 / "thr"
    shutil.copytree(head, thr)
    (thr / CITED).write_text("export function calculateCardBonus(rule: any, spend: number) {\n  throw new RangeError('boom');\n}\n")
    th = g.run_reproducer(thr, rp("calls-it.mts"), CITED)
    chk("code-under-test exception -> fail, in_product", (th.get("kind"), th.get("exc"), th.get("in_product")), ("fail", "RangeError", True))
    chk("...located in the cited file", str(th.get("where")).startswith("lib/cardBonuses.ts:"), True)
    chk("same reproducer passes on the correct head (no assertion, no throw)", g.run_reproducer(head, rp("calls-it.mts"), CITED).get("kind"), "pass")
finally:
    shutil.rmtree(tmp0, ignore_errors=True)

# counterfactual: a genuine under-count (no epsilon) must be CONFIRMED, against the correct baseline
tmp = Path(tempfile.mkdtemp(prefix="test-gfc-js-"))
try:
    bad = tmp / "bad"
    shutil.copytree(head, bad)
    f = bad / CITED
    f.write_text(f.read_text().replace("Math.floor(spend / perDollars + 1e-9)", "Math.floor(spend / perDollars)"))
    assert "Math.floor(spend / perDollars + 1e-9)" not in f.read_text()
    tp = ("import assert from 'node:assert/strict';\nimport { calculateCardBonus } from './lib/cardBonuses.ts';\n"
          "export function reproduce(): void {\n  assert.equal(calculateCardBonus({ points: 1, perDollars: 0.1 }, 0.3).times, 3);\n}\n")
    h = g.run_reproducer(bad, tp, CITED)
    chk("genuine under-count: reproducer FAILS on the epsilon-less head", (h.get("kind"), h.get("exc")), ("fail", "AssertionError"))
    chk("...in the cited file's call path", h.get("touched_cited"), True)
    fcdir = tmp / "fc"
    shutil.copytree(bad, fcdir / "head"); shutil.copytree(base, fcdir / "base")
    shutil.rmtree(fcdir / "head"); shutil.copytree(bad, fcdir / "head")
    # base = the CORRECT file -> reproducer passes there -> CONFIRMED (introduced by the change)
    shutil.rmtree(fcdir / "base"); shutil.copytree(head, fcdir / "base")
    issues = [{"severity": "high", "category": "code", "source": "review", "file": CITED, "line": 59, "what": "x"}]
    res = g.check_rows(issues, [0], {"rows": [{"idx": 0, "file": CITED, "line": 59, "reproducer": tp}]}, fcdir)
    chk("check_rows: genuine defect introduced by the change -> confirmed", res[0]["outcome"], "confirmed")
    chk("...reproducer saved with a .mts name", str(res[0].get("reproducer", "")).endswith(".mts"), True)
    # base also under-counts -> pre-existing (advisory)
    shutil.rmtree(fcdir / "base"); shutil.copytree(bad, fcdir / "base")
    res = g.check_rows(issues, [0], {"rows": [{"idx": 0, "file": CITED, "line": 59, "reproducer": tp}]}, fcdir)
    chk("check_rows: fails identically on the baseline -> pre-existing", res[0]["outcome"], "pre-existing")
    # the real false positive end to end through check_rows + apply_outcome: verdict fail -> pass
    fcdir2 = tmp / "fc2"
    shutil.copytree(head, fcdir2 / "head"); shutil.copytree(base, fcdir2 / "base")
    pl = json.loads(json.dumps(rel))
    res = g.check_rows(pl["issues"], [0], {"rows": [{"idx": 0, "file": pl["issues"][0]["file"],
                       "line": pl["issues"][0]["line"], "reproducer": rp("fp-1e15.mts")}]}, fcdir2)
    ov = g.apply_outcome(pl, res)
    chk("real false positive: check_rows -> unconfirmed", res[0]["outcome"], "unconfirmed")
    chk("...apply_outcome moves it to advisory and the verdict FAIL becomes pass", (ov, pl["verdict"]), ("unconfirmed", "pass"))
    # deps_from: a node_modules next to the job worktree is linked into the throwaway tree
    dep = tmp / "jobwt" / "node_modules" / "leftpad"; dep.mkdir(parents=True)
    (dep / "package.json").write_text('{"name":"leftpad","version":"1.0.0","main":"i.js"}')
    (dep / "i.js").write_text("module.exports = (s) => 'L' + s;")
    dtree = tmp / "dtree"; (dtree / "lib").mkdir(parents=True)
    (dtree / "lib" / "m.ts").write_text("import lp from 'leftpad';\nexport function f(x: string): string { return lp(x); }\n")
    dsrc = ("import assert from 'node:assert/strict';\nimport { f } from './lib/m.ts';\n"
            "export function reproduce(): void { assert.equal(f('a'), 'La'); }\n")
    chk("package import resolves via deps_from", g.run_reproducer(dtree, dsrc, "lib/m.ts", deps_from=str(tmp / "jobwt")).get("kind"), "pass")
    chk("...and without deps_from it is an env error, never a confirmation",
        g.run_reproducer(dtree, dsrc, "lib/m.ts").get("kind"), "env")
finally:
    shutil.rmtree(tmp, ignore_errors=True)

# --- runner side: a JS finding gets the JS prompt and a .mts-shaped reproducer ------------------
class _Model:
    model = "stub"
    def __init__(self): self.sys = []
    def chat_json(self, system, user, schema, num_predict=0):
        self.sys.append((system, user))
        return {"reproducer": rp("fp-1e15.mts"), "explanation": "calls it with the reviewer's trigger"}

od = Path(tempfile.mkdtemp(prefix="test-gfc-js-run-"))
try:
    m = _Model()
    spec = {"rows": [{"idx": 0, "file": CITED, "line": 59, "what": "w", "detail": "d"}],
            "head_tree": str(FX / "head"), "diff": str(FX / "job.diff"), "intent": "i"}
    g.runner_main(spec, m, od)
    out = json.loads((od / "refute.json").read_text())
    chk("runner_main: JS row uses the JS system prompt", "export function reproduce" in m.sys[0][0], True)
    chk("...and a typescript code fence in the user prompt", "```typescript" in m.sys[0][1], True)
    chk("...and returns the reproducer", bool(out["rows"][0]["reproducer"]), True)
finally:
    shutil.rmtree(od, ignore_errors=True)

print("JS_FCHECK_OK" if not FAILS else "FAILED: " + "; ".join(FAILS))
sys.exit(1 if FAILS else 0)
