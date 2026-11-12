#!/usr/bin/env python3
"""faithful-harness-lint -- does this fixture DRIVE the real symbol, or just
inspect the shape of what the code happens to build?

WHY THIS EXISTS
---------------
Mutation testing (verify-relevance.py) answers "does this verify NOTICE a
changed line?". It cannot answer "does this verify test the PROPERTY?" -- and
there is one fixture shape where those two come apart completely:

    a PROXY fixture asserts on the QUERY/FILTER OBJECT the code builds
    instead of evaluating it against real inputs.

Such a fixture mutates beautifully (change the filter, the assertion notices)
and proves nothing: it re-states the implementation. The scar is rt-pl-exclude
(2026-09-17) -- a fixture that pattern-matched a Prisma `where` shape passed the
mutation gate, and the P&L-breaking bug underneath it was caught by a HUMAN, not
by the harness. The bug was `NOT: { giftCards: { every: { ccSubmittedAt: null } } }`,
whose `every` over an EMPTY relation is VACUOUSLY TRUE, so every order with no
gift cards was silently dropped from the P&L. No shape assertion can see that;
only evaluating the filter against an order with no gift cards can.

WHAT THIS DOES, AND WHAT IT DELIBERATELY DOES NOT
-------------------------------------------------
It is a STATIC heuristic over the fixture source, not a prover. It looks for
three specific smells, each one a real observed failure:

  proxy-shape      assertions whose subject is a CAPTURED call argument (the
                   query/filter/props object the code built) while NOTHING in the
                   file asserts on a value produced by EXECUTING the real symbol.
                   Shape assertions are not banned -- the good rt-pl-exclude
                   fixture asserts `c.where.userId` too. What makes a fixture a
                   proxy is shape assertions INSTEAD OF behaviour, never
                   alongside it.

  vacuous-truth    an `every`/`all()`-style universal over a relation or
                   collection, with no case in the file exercising the EMPTY
                   collection. A universal over an empty set is true, so the one
                   input that separates the right filter from the wrong one is
                   exactly the one nobody wrote.

  harness-stub     the fixture never obtains the real symbol at all (no import /
                   require / importlib of the target). It tests its own mocks --
                   green forever, including on a tree where the target is empty.

False positives are the failure mode that gets a gate ignored, so the blocking
verdicts require the STRONG conjunction (smell present AND no behavioural
evidence anywhere in the file); the weaker signals are warnings.

API: lint_text(text, lang=None, path=None) -> list[Finding]
     lint_file(path) -> list[Finding]
     worst(findings) -> "fail" | "warn" | "ok"
CLI: faithful-harness-lint.py FIXTURE [FIXTURE...] [--json] [--self-test]
     exit 1 if any finding is blocking.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

FAIL, WARN, OK = "fail", "warn", "ok"

TS_EXTS = {".ts", ".tsx", ".mts", ".cts", ".js", ".mjs", ".cjs", ".jsx"}
PY_EXTS = {".py"}


class Finding:
    def __init__(self, rule, level, message, evidence=""):
        self.rule, self.level, self.message, self.evidence = (
            rule, level, message, evidence)

    def as_dict(self):
        return {"rule": self.rule, "level": self.level,
                "message": self.message, "evidence": self.evidence}

    def __repr__(self):
        return f"<{self.rule}:{self.level}>"


# --- the smell vocabulary ---------------------------------------------------
# A "captured shape" subject: something the code under test BUILT and the
# fixture intercepted, rather than something it RETURNED.
_CAPTURED = re.compile(
    r"""(?x)
    (?:\bcalls?\s*\[ [^\]]* \]            # calls[0], call[i]
      | \.mock\.calls                      # jest/node mock.calls
      | \bmock\.calls
      | \bargs?\b\s*[.\[]                  # args.where, arg[0]
      | \blastCall\b
      | \bcapturedd?\b
      | \breceived(?:Args|Query|Where)\b
    )""")
# The specific objects a proxy fixture pattern-matches on.
# NOT `.body` / `.payload`: those are usually the RESPONSE a faithful fixture
# asserts on, and a lint that flags them cries wolf on every good integration
# fixture. These are the ones you only ever read off a query the code BUILT.
_SHAPE_FIELD = re.compile(
    r"\.(where|filter|query|params|select|include|args|options)\b")
# Assertion call sites, per language family.
_ASSERT_TS = re.compile(
    r"\b(assert(?:\.\w+)*\s*\(|expect\s*\(|t\.(?:equal|deepEqual|ok|is)\s*\()")
_ASSERT_PY = re.compile(r"\bassert\b|\bself\.assert\w+\s*\(|\bCASES\b")
# Serialise-and-compare is the purest proxy form.
_STRINGIFY = re.compile(r"JSON\.stringify\s*\(|\brepr\s*\(|\bstr\s*\(\s*\w+\s*\)")

# Evidence that the REAL symbol is obtained.
_IMPORTS_TS = re.compile(
    r"""(?x)
      \bimport\s+[^;\n]*\bfrom\s*['"][^'"]+['"]
    | \bawait\s+import\s*\(
    | \brequire\s*\(\s*['"][^'"]+['"]\s*\)
    | \brequire\.resolve\s*\(
    """)
_IMPORTS_PY = re.compile(
    r"\bimportlib\.util\.spec_from_file_location\s*\(|^\s*(?:from|import)\s+\w",
    re.M)
# Evidence that it is EXECUTED and its OUTPUT is what gets asserted: a value
# bound from a call, or a call appearing directly inside an assertion.
_AWAITED_CALL = re.compile(
    r"\b(?:const|let|var)\s+(\w+)\s*(?::[^=\n]+)?=\s*(?:await\s+)?[\w.\[\]]+\s*\(")
_PY_BOUND_CALL = re.compile(r"^\s*(\w+)\s*=\s*[\w.]+\s*\(", re.M)
_LAMBDA_CALL = re.compile(r"lambda\s*:\s*\w+\.\w+\s*\(")
# Empty-collection evidence for the vacuous-truth check.
_EMPTY_CASE = re.compile(
    r"\[\s*\]|\{\s*\}|\blen\s*\(\s*\w+\s*\)\s*==\s*0|\bempty\b", re.I)
_UNIVERSAL = re.compile(r"\bevery\b|\ball\s*\(|\bnone\s*:|\bNOT\s*:")


def _lang_for(path, lang):
    if lang:
        return lang
    ext = Path(path or "").suffix.lower()
    if ext in PY_EXTS:
        return "python"
    if ext in TS_EXTS:
        return "ts"
    return "ts"


def _strip_comments(text, lang):
    """Comments EXPLAIN a fixture; they must never be what makes it look
    faithful. The rt-pl-exclude fixture's own header says 'BEHAVIOURAL, not
    structural' -- a proxy fixture could say the same and be lying."""
    if lang == "python":
        out = re.sub(r"(?m)#.*$", "", text)
        return re.sub(r'(?s)("""|\'\'\').*?\1', "", out)
    out = re.sub(r"(?s)/\*.*?\*/", "", text)
    return re.sub(r"(?m)//.*$", "", out)


def _assert_lines(body, lang):
    pat = _ASSERT_PY if lang == "python" else _ASSERT_TS
    return [ln for ln in body.splitlines() if pat.search(ln)]


def _behavioural_evidence(body, lang, asserts):
    """Does ANY assertion rest on a value the real code produced?

    Two accepted forms, both requiring an actual call:
      * a variable bound from a call (`const res = await GET(...)`,
        `body = await res.json()`, `got = target.is_safe(...)`) that is then
        asserted on;
      * a call made directly inside the assertion / case tuple
        (`assert.equal(probe(cc), false)`, `lambda: target.is_safe(...)`).
    """
    bound = set(m.group(1) for m in _AWAITED_CALL.finditer(body))
    if lang == "python":
        bound |= set(m.group(1) for m in _PY_BOUND_CALL.finditer(body))
    for ln in asserts:
        # a call inside the assertion itself, on something that is not a
        # captured-shape accessor
        stripped = _CAPTURED.sub("", ln)
        if re.search(r"\w\s*\(", stripped.split("(", 1)[-1] if "(" in stripped else ""):
            if not _SHAPE_FIELD.search(ln):
                return True
        for name in bound:
            if re.search(rf"\b{re.escape(name)}\b", ln) and not _CAPTURED.search(ln):
                return True
    if lang == "python" and _LAMBDA_CALL.search(body):
        return True
    return False


def lint_text(text, lang=None, path=None):
    lang = _lang_for(path, lang)
    body = _strip_comments(text, lang)
    findings = []
    asserts = _assert_lines(body, lang)

    # --- harness-stub: the real symbol is never even obtained ---------------
    imports = (_IMPORTS_PY if lang == "python" else _IMPORTS_TS).search(body)
    if not imports:
        findings.append(Finding(
            "harness-stub", FAIL,
            "the fixture never imports/loads the target module -- it can only be "
            "testing its own mocks, which stays green on an empty target",
            evidence="no import / require / spec_from_file_location found"))

    # --- proxy-shape --------------------------------------------------------
    # Reading `.where` / `.select` / `.filter` off ANYTHING inside an assertion
    # is shape inspection -- the variable is usually a loop alias over the
    # captured calls (`for (const c of calls) assert(c.where...)`), so keying on
    # the accessor name rather than the binding is what actually catches it.
    shape_asserts = [ln for ln in asserts
                     if _SHAPE_FIELD.search(ln)
                     or (_CAPTURED.search(ln) and _STRINGIFY.search(ln))]
    behavioural = _behavioural_evidence(body, lang, asserts)
    if shape_asserts and not behavioural:
        findings.append(Finding(
            "proxy-shape", FAIL,
            f"{len(shape_asserts)} assertion(s) inspect a CAPTURED query/filter "
            "object and nothing in this fixture asserts on a value the real "
            "symbol RETURNED -- this re-states the implementation instead of "
            "testing it, and it will pass the mutation gate while proving nothing",
            evidence=shape_asserts[0].strip()[:200]))
    elif shape_asserts and behavioural:
        findings.append(Finding(
            "proxy-shape", WARN,
            f"{len(shape_asserts)} shape assertion(s) present, but the fixture "
            "also drives the real symbol -- fine, as long as the BEHAVIOURAL "
            "cases are the ones that would fail on a wrong filter",
            evidence=shape_asserts[0].strip()[:200]))
    elif not asserts:
        findings.append(Finding(
            "proxy-shape", WARN,
            "no assertions recognised in this fixture -- nothing to judge",
            evidence=""))

    # --- vacuous-truth ------------------------------------------------------
    if _UNIVERSAL.search(body) and not _EMPTY_CASE.search(body):
        findings.append(Finding(
            "vacuous-truth", FAIL,
            "a universal (`every` / `all()` / `NOT`+relation) is exercised but no "
            "case uses an EMPTY collection. A universal over an empty set is "
            "TRUE, so the single input that separates the right filter from the "
            "wrong one is the one this fixture does not have (rt-pl-exclude: "
            "every order with no gift cards was silently dropped from the P&L)",
            evidence="universal present, no empty-collection case"))

    if not findings:
        findings.append(Finding("faithful", OK,
                                "drives the real symbol and asserts on what it "
                                "produced"))
    return findings


def lint_file(path):
    p = Path(path)
    try:
        text = p.read_text(errors="replace")
    except OSError as e:
        return [Finding("unreadable", WARN, f"{p}: {e}")]
    return lint_text(text, path=str(p))


def worst(findings):
    for level in (FAIL, WARN):
        if any(f.level == level for f in findings):
            return level
    return OK


# --- self-test --------------------------------------------------------------
# Distilled from the REAL fixtures on both sides of the rt-pl-exclude incident.
PROXY_SAMPLE = '''\
import { test } from 'node:test';
import assert from 'node:assert/strict';
const calls: any[] = [];
test('route carries the exclusion filter', async () => {
  const GET = await getHandler();
  await GET(new Request('http://localhost/api/analytics'));
  for (const c of calls) {
    assert.deepEqual(c.where.NOT, { giftCards: { every: { ccSubmittedAt: null } } });
    assert.equal(c.select.giftCards, true);
  }
});
'''

FAITHFUL_SAMPLE = '''\
import { test } from 'node:test';
import assert from 'node:assert/strict';
const calls: any[] = [];
function matchWhere(where: any, o: any): boolean { return true; }
test('POLARITY: no-giftcard order stays in the P&L', async () => {
  const GET = await getHandler();
  dataset = [makeRow(703, 30, 60, [])];
  const res = await GET(new Request('http://localhost/api/analytics'));
  const body: any = await res.json();
  const cm = body.periods.find((p: any) => p.period === 'current_month');
  assert.equal(cm.current.orderCount, 1);
  assert.equal(cm.current.profit, 30);
  for (const c of calls) { assert.equal(c.where.userId, null); }
});
'''

STUB_SAMPLE = '''\
const rows = [{ id: 1 }];
test('rows are filtered', () => {
  const kept = rows.filter((r) => r.id > 0);
  assert.equal(kept.length, 1);
});
'''

PY_FAITHFUL = '''\
import importlib.util, sys
spec = importlib.util.spec_from_file_location("target", "/x/target.py")
target = importlib.util.module_from_spec(spec)
sys.modules["target"] = target
spec.loader.exec_module(target)
CASES = [
    ("empty relation is kept", lambda: target.is_safe({"cards": []}, True), True),
    ("all unsubmitted is dropped", lambda: target.is_safe({"cards": [None]}, True), False),
]
'''


def _self_test():
    cases = [
        ("proxy is a hard finding", PROXY_SAMPLE, "ts", "proxy-shape", FAIL),
        ("faithful is not blocked", FAITHFUL_SAMPLE, "ts", None, None),
        ("stub-only is a hard finding", STUB_SAMPLE, "ts", "harness-stub", FAIL),
        ("python faithful is not blocked", PY_FAITHFUL, "python", None, None),
    ]
    failures = []
    for name, text, lang, want_rule, want_level in cases:
        fs = lint_text(text, lang=lang)
        got = worst(fs)
        if want_rule is None:
            ok = got != FAIL
            detail = f"worst={got} ({[f.rule for f in fs]})"
        else:
            ok = any(f.rule == want_rule and f.level == want_level for f in fs)
            detail = f"{[(f.rule, f.level) for f in fs]}"
        print(f"  {'ok  ' if ok else 'FAIL'} {name:<32} {detail}")
        if not ok:
            failures.append(name)

    # The discriminating pair: the SAME property, one fixture proxying it and
    # one driving it. If both land the same way the lint measures nothing.
    p, f = worst(lint_text(PROXY_SAMPLE, lang="ts")), worst(lint_text(FAITHFUL_SAMPLE, lang="ts"))
    ok = p == FAIL and f != FAIL
    print(f"  {'ok  ' if ok else 'FAIL'} {'discriminates the real pair':<32} "
          f"proxy={p} faithful={f}")
    if not ok:
        failures.append("discriminates the real pair")

    # Vacuous truth: a universal with no empty case is caught; adding the empty
    # case clears it. Same file, one line apart.
    vac = 'assert.deepEqual(x, { every: { ccSubmittedAt: null } });\nimport a from "b";'
    fixed = vac + '\ndataset = [makeRow(1, [])];\nconst res = await GET(r);\nassert.equal(res.n, 1);'
    ok = (any(f.rule == "vacuous-truth" for f in lint_text(vac, lang="ts"))
          and not any(f.rule == "vacuous-truth" for f in lint_text(fixed, lang="ts")))
    print(f"  {'ok  ' if ok else 'FAIL'} {'vacuous-truth clears on fix':<32} ")
    if not ok:
        failures.append("vacuous-truth clears on fix")

    print(f"\n--- {len(failures)} failed ---")
    if failures:
        print("FAILED: " + ", ".join(failures))
        return 1
    print("SELF_CHECK_OK: the lint separates a proxy fixture from one that "
          "drives the real symbol")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("fixtures", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if not a.fixtures:
        ap.error("give at least one fixture path (or --self-test)")
    out, blocking = {}, False
    for f in a.fixtures:
        fs = lint_file(f)
        out[f] = [x.as_dict() for x in fs]
        if worst(fs) == FAIL:
            blocking = True
        if not a.json:
            print(f"\n{f}")
            for x in fs:
                print(f"  [{x.level:4}] {x.rule}: {x.message}")
                if x.evidence:
                    print(f"         {x.evidence}")
    if a.json:
        print(json.dumps(out, indent=2))
    return 1 if blocking else 0


if __name__ == "__main__":
    sys.exit(main())
