#!/usr/bin/env python3
"""SOURCE-TEXT HARNESS gate (2026-10-05, slice rt-bfmr-link-sync-feedback).

That slice passed preflight, pre-gate, re-gate (PASS) and second opinion (agree)
with a verify.test.ts that readFileSync'd components/BfmrReservationLinker.tsx
and asserted content.includes(...) / regexes over its lines -- it never ran the
code. Its refine rounds "killed" mutants by regexing the mutated line. The
fixture is copied VERBATIM into test-fixtures-source-text-harness/ (never run).

Pinned here, each against the REAL function in the real tool:
  A. source_text_harness.scan_text: the slice fixture -> source-text (never
     imports the target); a behavioural fixture (imports + asserts outputs, one
     absence check) -> ok; a minority mix on an imported target -> mixed, and
     strict mode turns mixed into source-text; Python CASES-table/helper reads
     -> source-text; Python behavioural -> ok; test-file targets and data
     targets are never flagged.
  B. verify-quality.py (preflight's delegate): problem `source-text-harness`
     on the slice worktree, none on the behavioural one.
  C. verify-relevance.measure_applied: LOW with source_text_harness BEFORE any
     mutant runs (the verify command is `exit 99`, so if the core ran the
     verdict would be `unproven`); the behavioural worktree reaches the core.
  D. ollama-dispatch-auto's self-check (auto-harness-check.py): fails with
     SOURCE-TEXT on the slice fixture; the behavioural one gets past it.
     Author/refine prompts carry the rules; a SOURCE-TEXT blocker overrides
     "do not delete existing cases".
  E. gate-on-complete: autofix_classify -> grader-suspect-source-text ->
     undecidable-relevance; measure_relevance keeps the block; the re-gate and
     second-opinion task contexts carry REVIEW_FIXTURE_RULE.

Red-on-revert: point the *_SRC env vars at the pre-change backups
(`*.bak-20261005T181605Z-srctext`) and B-E fail.
Env overrides: STH_SRC VQ_SRC VR_SRC AUTO_SRC GATE_SRC. Hermetic: temp dirs only,
GATE_TEST_MODE=1, DISPATCH_VERIFY_SANDBOX=1; never touches a worktree or queue.
"""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

os.environ["GATE_TEST_MODE"] = "1"
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
HERE = Path(__file__).resolve().parent
FX = HERE / "test-fixtures-source-text-harness"
STH = Path(os.environ.get("STH_SRC") or HERE / "source_text_harness.py")
VQ = Path(os.environ.get("VQ_SRC") or HERE / "verify-quality.py")
VR = Path(os.environ.get("VR_SRC") or HERE / "verify-relevance.py")
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
GATE = Path(os.environ.get("GATE_SRC") or HERE / "gate-on-complete.py")
SLICE_FX = FX / "rt-bfmr-link-sync-feedback.verify.test.ts"
GOOD_FX = FX / "behavioural.verify.test.ts"
TARGET = "components/BfmrReservationLinker.tsx"
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    argv, sys.argv = sys.argv, [str(path)]
    try:
        ld.exec_module(m)
    finally:
        sys.argv = argv
    return m


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def make_wt(fixture: Path, verify_body="exit 1\n") -> Path:
    """A throwaway auto-style worktree: target stub, manifest, verify.sh naming
    the fixture in TEST_FILES, TASK.md, refimpl.py, check_literals.py."""
    wt = Path(tempfile.mkdtemp(prefix="sth-"))
    (wt / "components").mkdir()
    (wt / TARGET).write_text("export function BfmrReservationLinker() { return null; }\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps(
        {"target": TARGET, "fixture": "verify.test.ts", "creation_task": False,
         "authored": ["TASK.md", "check_literals.py", "refimpl.py", "verify.sh",
                      "verify.test.ts"]}))
    (wt / "verify.sh").write_text('#!/usr/bin/env bash\nTEST_FILES="verify.test.ts"\n'
                                  + verify_body)
    (wt / "verify.test.ts").write_text(fixture.read_text())
    (wt / "TASK.md").write_text(
        "# TASK: x\n\n## Required change\nsync shows webError.\n\n## Must contain\n"
        "- `webError`\n\n## Scope\nOnly edit `" + TARGET + "` (via refimpl.py).\n")
    (wt / "refimpl.py").write_text("import pathlib\npathlib.Path('" + TARGET
                                   + "').write_text('export const webError = 1;\\n')\n")
    (wt / "check_literals.py").write_text(
        "import pathlib\nassert 'webError' in pathlib.Path('" + TARGET + "').read_text()\n")
    git(wt, "init", "-q")
    git(wt, "add", TARGET, ".dispatch-harness.json")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return wt


def part_a(sth):
    s = sth.scan_text(SLICE_FX.read_text(), "verify.test.ts", [TARGET])
    chk("A1 slice fixture -> source-text", s["verdict"], "source-text")
    chk("A1 slice fixture never imports the target", s["imports_target"], [])
    chk("A1 slice fixture: >= 20 positive source-text assertions", s["text_asserts"] >= 20, True)
    chk("A1 reason says no case runs the code", "NOT ONE case" in s["reason"], True)
    g = sth.scan_text(GOOD_FX.read_text(), "verify.test.ts", [TARGET])
    chk("A2 behavioural fixture -> ok", g["verdict"], "ok")
    chk("A2 behavioural fixture imports the target", g["imports_target"], [TARGET])
    chk("A2 its absence check is counted, not flagged", g["absence_asserts"], 1)
    mixed = GOOD_FX.read_text() + (
        "\ntest('pin', () => {\n  const s = require('node:fs').readFileSync("
        "'./components/BfmrReservationLinker.tsx', 'utf8');\n"
        "  assert.ok(/webNeeded\\s*>\\s*0/.test(s));\n});\n")
    mx = sth.scan_text(mixed, "verify.test.ts", [TARGET])
    chk("A3 one regex pin among behavioural cases -> mixed (advisory)", mx["verdict"], "mixed")
    chk("A3 strict (authoring) mode -> source-text",
        sth.scan_text(mixed, "verify.test.ts", [TARGET], strict=True)["verdict"], "source-text")
    py = sth.scan_text((FX / "source_text_cases.test_fixture.py").read_text(),
                       "test_fixture.py", ["app/sync.py"])
    chk("A4 python CASES table over a reader helper -> source-text", py["verdict"], "source-text")
    chk("A4 python: the `not in` case is an absence check", py["absence_asserts"] >= 1, True)
    pg = sth.scan_text((FX / "behavioural.test_fixture.py").read_text(),
                       "test_fixture.py", ["app/sync.py"])
    chk("A5 python behavioural fixture -> ok", pg["verdict"], "ok")
    chk("A6 a TEST-FILE target is never flagged",
        sth.scan_text("x = 'a' in open('test_sorter.py').read()\nassert x\n",
                      "test_fixture.py", ["test_sorter.py"])["verdict"], "ok")
    chk("A6 a DATA target (json) is never flagged",
        sth.scan_text("assert 'k' in open('config.json').read()\n",
                      "test_fixture.py", ["config.json"])["verdict"], "ok")
    ex = sth.scan_text("import vm from 'node:vm';\nimport assert from 'node:assert';\n"
                       "const src = require('fs').readFileSync('./components/"
                       "BfmrReservationLinker.tsx','utf8');\nconst out = vm.runInNewContext(src, {});\n"
                       "assert.equal(out, 3);\n", "verify.test.ts", [TARGET])
    chk("A7 source that is EXECUTED (vm) is not a text assertion", ex["verdict"], "ok")


def part_b():
    for name, fx, want in (("slice", SLICE_FX, True), ("behavioural", GOOD_FX, False)):
        wt = make_wt(fx)
        try:
            p = subprocess.run([sys.executable, str(VQ), "--verify", "bash verify.sh",
                                "--cwd", str(wt), "--task-file", str(wt / "TASK.md"),
                                "--no-baseline", "--json"], capture_output=True, text=True,
                               timeout=120)
            d = json.loads(p.stdout or "{}")
            ids = {f["id"] for f in d.get("findings", []) if f.get("kind") == "problem"}
            chk(f"B verify-quality: {name} -> problem source-text-harness",
                "source-text-harness" in ids, want)
            if want:
                chk("B verify-quality exits 1 (NO-GO) on the slice", p.returncode, 1)
        finally:
            shutil.rmtree(wt, ignore_errors=True)


def part_c():
    vr = load(VR, "vr_sth")
    diff = (f"diff --git a/{TARGET} b/{TARGET}\n--- a/{TARGET}\n+++ b/{TARGET}\n"
            "@@ -1 +1 @@\n-export function BfmrReservationLinker() { return null; }\n"
            "+export const webError = 1;\n")
    for name, fx in (("slice", SLICE_FX), ("behavioural", GOOD_FX)):
        wt = make_wt(fx)
        try:
            rec = vr.measure_applied(wt, "exit 99", diff, max_mutants=4, budget_s=30)
            sth = rec.get("source_text_harness") or {}
            if name == "slice":
                chk("C relevance: slice -> LOW", rec.get("verdict"), "low")
                chk("C relevance: slice carries source_text_harness", sth.get("verdict"), "source-text")
                chk("C relevance: decided BEFORE any mutant ran", rec.get("tried"), 0)
                chk("C relevance: no survivors handed to the refine loop", rec.get("survivors"), [])
                chk("C relevance reason names the fix",
                    "SOURCE-TEXT HARNESS" in rec.get("reason", "") and "EXECUTE" in rec.get("reason", ""),
                    True)
            else:
                chk("C relevance: behavioural fixture reaches the mutation core "
                    "(verify `exit 99` is not green -> unproven)", rec.get("verdict"), "unproven")
                chk("C relevance: behavioural fixture not tagged", bool(sth), False)
        finally:
            shutil.rmtree(wt, ignore_errors=True)


def part_d():
    auto = load(AUTO, "auto_sth")
    for name, fx in (("slice", SLICE_FX), ("behavioural", GOOD_FX)):
        wt = make_wt(fx)
        try:
            auto.write_harness_check(wt, "typescript")
            p = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=str(wt),
                               capture_output=True, text=True, timeout=300)
            out = p.stdout + p.stderr
            if name == "slice":
                chk("D self-check: slice fixture FAILS as SOURCE-TEXT",
                    p.returncode == 1 and "SOURCE-TEXT HARNESS" in out, True)
                chk("D self-check failure tells the model what to do instead",
                    "exported pure function" in out and "## Must contain" in out, True)
            else:
                chk("D self-check: behavioural fixture is NOT rejected as source-text",
                    "SOURCE-TEXT" in out, False)
        finally:
            shutil.rmtree(wt, ignore_errors=True)
    auto.load_attempts = lambda a: []
    tmp = Path(tempfile.mkdtemp(prefix="sth-ap-"))
    a = SimpleNamespace(intent="sync() must show webError", interface="", lang="typescript",
                        label="x", repo=str(tmp), new_project=None, dest=None, target=TARGET)
    ap = auto.author_prompt(a, TARGET)
    chk("D author prompt forbids reading the target's source as text",
        "NEVER read the target file's source as text" in ap, True)
    chk("D author prompt says to extract an exported pure function",
        "EXTRACT it into an exported" in ap, True)
    surv = [{"file": TARGET, "line": 3, "mutation": "> -> >=", "snippet": "x >= 0",
             "original": "x > 0", "class": "cmp"}]
    rp = auto.refine_prompt(TARGET, surv, [], "typescript")
    chk("D refine prompt: kill survivors with INPUTS, never a regex of the line",
        "NEVER kill a survivor by reading the target's source" in rp, True)
    rb = auto.refine_prompt(TARGET, [], [{"check": "verify-relevance",
                                          "message": "SOURCE-TEXT HARNESS: the fixture reads ...",
                                          "fix": "x"}], "typescript")
    chk("D refine prompt: a SOURCE-TEXT blocker overrides 'do not delete existing cases'",
        "DELETE them (this overrides" in rb, True)
    shutil.rmtree(tmp, ignore_errors=True)


def part_e():
    g = load(GATE, "gate_sth")
    g.TEST_MODE = True
    payload = {"verdict": "concerns", "issues": [],
               "verify_relevance": {"verdict": "low", "survivors": [],
                                    "source_text_harness": {"verdict": "source-text",
                                                            "reason": "reads the target"}}}
    dec = g.autofix_classify(payload)
    chk("E autofix_classify -> grader-suspect-source-text", dec.get("class"),
        "grader-suspect-source-text")
    chk("E it escalates (a harness re-author, never a code re-run)", dec.get("action"), "escalate")
    chk("E category is an existing queue category",
        g.autofix_escalation_category("grader-suspect-source-text"), "undecidable-relevance")
    # measure_relevance keeps the block: stub the subprocess with a canned record
    rec = {"verdict": "low", "score": 0.0, "survivors": [], "reason": "SOURCE-TEXT HARNESS: x",
           "source_text_harness": {"verdict": "source-text", "reason": "x"}}
    real_run = g.subprocess.run
    g.subprocess.run = lambda *a, **k: SimpleNamespace(stdout=json.dumps(rec), stderr="",
                                                       returncode=1)
    try:
        tmp = Path(tempfile.mkdtemp(prefix="sth-gate-"))
        pl = {}
        g.measure_relevance(pl, tmp, "bash verify.sh")
    finally:
        g.subprocess.run = real_run
    chk("E measure_relevance carries source_text_harness into gate.json",
        (pl.get("verify_relevance") or {}).get("source_text_harness", {}).get("verdict"),
        "source-text")
    out = Path(tempfile.mkdtemp(prefix="sth-gate-out-"))
    (out / "p1.diff").write_text(f"diff --git a/{TARGET} b/{TARGET}\n+++ b/{TARGET}\n+x\n+y\n")
    gj = out / "p1.gate.json"
    gj.write_text("{}")
    g._escalate_regate("p1", gj, {"verdict": "concerns", "issues": []}, out)
    ctx = json.loads((out / "p1-regate" / "task.json").read_text())["context"]
    chk("E re-gate context carries the fixture rule", "TEST/FIXTURE RULE" in ctx, True)
    g.SECOND_OPINION = True
    g.SECOND_OPINION_MIN_LINES = 0
    g._second_opinion_eligible = lambda payload, d: (True, "forced for test")
    g.request_second_opinion("p1", gj, {"verdict": "pass"}, out)
    ctx2 = json.loads((out / "p1-secondop" / "task.json").read_text())["context"]
    chk("E second-opinion context carries the fixture rule", "TEST/FIXTURE RULE" in ctx2, True)
    rule = getattr(g, "REVIEW_FIXTURE_RULE", "")
    chk("E the rule names readFileSync + includes/regex as a HIGH defect",
        all(w in rule for w in ("readFileSync", "regex", "HIGH")), True)
    shutil.rmtree(out, ignore_errors=True)


def main():
    sth = load(STH, "sth_mod") if STH.is_file() else None
    if sth is None:
        chk("A source_text_harness.py exists", False, True)
    else:
        part_a(sth)
    part_b()
    part_c()
    part_d()
    part_e()
    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED'}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
