#!/usr/bin/env python3
"""harness_lint.py: every check, both ways (a defect is flagged; the corrected spec is clean).
  test-harness-lint.py                 run
  test-harness-lint.py --revert-check  neuter each check; the suite must go RED
"""
import importlib.util, json, os, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REVERT = "--revert-check" in sys.argv
sp = importlib.util.spec_from_file_location("harness_lint", HERE / "harness_lint.py")
hl = importlib.util.module_from_spec(sp); sp.loader.exec_module(hl)
if REVERT:
    for n in ("check_literals", "check_consistency", "check_stale_prompt", "check_retry", "check_stage_dryrun", "check_refimpl_vs_fixture"):
        setattr(hl, n, lambda *a, **k: [])
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[:300]))
    if not ok:
        FAILS.append(name)


BODY = "const skip = new Set(l.map(s => normalize(s.orderNumber)));\nlet cancelled = false;\nexport function POST() {}\n"
TASK = ("# T\n## Required change\nx\n## Must contain\n- `normalize(orderNumber)`\n- `cancelledMarked`\n"
        "## Scope\nOnly edit `app/r.ts`; do not edit `verify.sh` or `TASK.md`.\n")
REFIMPL = ("import sys,pathlib\nwt=pathlib.Path(sys.argv[1]); p=wt/'app/r.ts'\n"
           "p.write_text(p.read_text()+'\\nconst cancelledMarked = 0;\\n')\n")


def repo(task=TASK, refimpl=REFIMPL):
    d = Path(tempfile.mkdtemp(prefix="hlint-t-"))
    g = lambda *a: subprocess.run(["git", "-C", str(d), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (d / "app").mkdir(); (d / "app/r.ts").write_text(BODY)
    g("add", "-A"); g("commit", "-q", "-m", "b")
    (d / "TASK.md").write_text(task); (d / "refimpl.py").write_text(refimpl)
    (d / ".dispatch-harness.json").write_text(json.dumps({"target": "app/r.ts"}))
    return d


def codes(fs):
    return sorted(f["code"] for f in fs)


d = repo()
fs = hl.run_lint(d, attempts=[])
check("walmart shape: near-miss literal -> LITERAL_NEAR_MISS with did-you-mean",
      "LITERAL_NEAR_MISS" in codes(fs) and any("did you mean `normalize(s.orderNumber)`" in f["message"] for f in fs), fs)
check("a literal the refimpl DOES write is clean (found in the refimpl-applied tree)",
      not any("cancelledMarked" in f["message"] for f in fs), fs)
fs = hl.run_lint(d, quick=True, attempts=[])
check("--quick skips the refimpl-applied proof but still catches the near-miss", "LITERAL_NEAR_MISS" in codes(fs))
t2 = TASK.replace("- `normalize(orderNumber)`\n", "- `neverWritten`\n")
d2 = repo(t2)
fs = hl.run_lint(d2, attempts=[])
check("(b) literal absent at HEAD and never written by refimpl -> REFIMPL_LITERAL_ABSENT",
      "REFIMPL_LITERAL_ABSENT" in codes(fs), fs)
d3 = repo(TASK.replace("- `normalize(orderNumber)`\n", "- `normalize(s.orderNumber)`\n"))
check("corrected spec is clean", hl.run_lint(d3, attempts=[]) == [], hl.run_lint(d3, attempts=[]))

# consistency
t4 = TASK.replace("Only edit `app/r.ts`;", "Only edit `app/r.ts`;").replace("- `cancelledMarked`\n", "- in lib/z.ts: `zzzzzzzz`\n- `cancelledMarked`\n")
fs = hl.check_consistency(t4, "app/r.ts")
check("(3) literal pinned to a file outside the scope -> LITERAL_OUTSIDE_SCOPE", "LITERAL_OUTSIDE_SCOPE" in codes(fs), fs)
t5 = TASK.replace("do not edit `verify.sh`", "do not edit `app/r.ts`, `verify.sh`")
check("(3) forbidden-edit list naming the target -> SCOPE_FORBIDS_TARGET",
      "SCOPE_FORBIDS_TARGET" in codes(hl.check_consistency(t5, "app/r.ts")))
check("(3) consistent spec is clean", hl.check_consistency(TASK, "app/r.ts") == [])

# stale prompt
(d3 / "AUTO-TASK.md").write_text("MISSING literal in app/r.ts (x): >>>normalize(orderNumber)<<<\nMust contain `normalize(s.orderNumber)`\n")
fs = hl.check_stale_prompt(d3, (d3 / "TASK.md").read_text())
check("(5) pending prompt quoting a literal the current TASK.md dropped -> STALE_SPEC_IN_PROMPT",
      codes(fs) == ["STALE_SPEC_IN_PROMPT"], fs)
(d3 / "AUTO-TASK.md").write_text("Must contain `normalize(s.orderNumber)`\n")
check("(5) prompt in sync with TASK.md is clean", hl.check_stale_prompt(d3, (d3 / "TASK.md").read_text()) == [])

# retry accounting
now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
H = [{"job": "a", "at": now, "missing_literals": [["f", "x"]]}, {"job": "b", "at": now, "missing_literals": [["f", "x"]]}]
check("(6) identical missing-literal set twice -> REPEAT_MISSING_LITERALS", codes(hl.check_retry(H)) == ["REPEAT_MISSING_LITERALS"], hl.check_retry(H))
check("(6) different sets are fine", hl.check_retry([H[0], dict(H[1], missing_literals=[["f", "y"]])]) == [])
check("(6) TASK.md edited AFTER the last failed round (operator re-spec) lifts the block",
      hl.check_retry(H, task_mtime=time.time() + 60) == [])
S = "AssertionError: expected 3 got 2\nnot ok 1 - case one\n"
check("(6) identical failure signature twice -> REPEAT_FAILURE_SIGNATURE",
      codes(hl.check_retry([{"job": "a", "at": now, "check_tail": S}, {"job": "b", "at": now, "check_tail": S}])) == ["REPEAT_FAILURE_SIGNATURE"])

# stage dry-run
fs = hl.check_stage_dryrun(d3, "task", "app/r.ts")
check("(4) the stage's static self-check executes and prints STAGE_OK/STAGE_FAIL", fs == [], fs)

# (7) refimpl vs the harness's own verify.sh (rt-walmart-cancel-import: lint OK while refimpl failed 0 !== 1)
def with_verify(d, want):
    (d / "verify.sh").write_text('#!/bin/sh\ngrep -q "cancelledMarked = %s" app/r.ts && echo VERIFY_OK && exit 0\necho "FAILED CASE: not ok 1 - mixed"; exit 1\n' % want)
    (d / "verify.test.ts").write_text("// real fixture\n")
    (d / ".dispatch-harness.json").write_text(json.dumps({"target": "app/r.ts", "authored": ["TASK.md", "verify.sh", "verify.test.ts", "refimpl.py"]}))
    return d
dbad = with_verify(repo(TASK.replace("- `normalize(orderNumber)`\n", "- `normalize(s.orderNumber)`\n")), 1)
fs = hl.run_lint(dbad, attempts=[])
check("(7) refimpl that does not make verify.sh print VERIFY_OK -> REFIMPL_FAILS_FIXTURE",
      "REFIMPL_FAILS_FIXTURE" in codes(fs) and any("FAILED CASE" in f["message"] for f in fs), fs)
dok = with_verify(repo(TASK.replace("- `normalize(orderNumber)`\n", "- `normalize(s.orderNumber)`\n")), 0)
check("(7) refimpl that satisfies verify.sh is clean", hl.run_lint(dok, attempts=[]) == [], hl.run_lint(dok, attempts=[]))
check("(7) --quick (stage entry, no refimpl yet) skips the proof", "REFIMPL_FAILS_FIXTURE" not in codes(hl.run_lint(dbad, quick=True, attempts=[])))
(dbad / "verify.test.ts").write_text("test('SCAFFOLD: cases not yet authored')\n")
check("(7) scaffold fixture (not authored yet) is not judged", "REFIMPL_FAILS_FIXTURE" not in codes(hl.run_lint(dbad, attempts=[])))

# CLI contract: exit 3 + SPEC_DEFECT line on a defect, 0 on clean
r = subprocess.run([sys.executable, str(HERE / "harness_lint.py"), str(d), "--quick"], capture_output=True, text=True)
check("CLI: defect -> exit 3 and a SPEC_DEFECT: line", r.returncode == 3 and r.stdout.startswith("SPEC_DEFECT: "), (r.returncode, r.stdout))
r = subprocess.run([sys.executable, str(HERE / "harness_lint.py"), str(d3), "--quick"], capture_output=True, text=True)
check("CLI: clean -> exit 0", r.returncode == 0 and "HARNESS_LINT_OK" in r.stdout, r.stdout)

print()
print(f"{len(FAILS)} FAILED: {FAILS}" if FAILS else "HARNESS_LINT_OK")
if REVERT:
    print("REVERT-CHECK:", "RED as required" if FAILS else "STILL GREEN -- the suite does not bite")
    sys.exit(0 if FAILS else 1)
sys.exit(1 if FAILS else 0)
