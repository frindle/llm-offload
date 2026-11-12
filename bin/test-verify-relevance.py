#!/usr/bin/env python3
"""Both-ways proof for verify-relevance.py -- does the RELEVANCE CHECK
discriminate a relevant verify from a discriminating-but-irrelevant one?

Every verify below FAILS at baseline and PASSES with the reference impl, i.e.
every one of them clears the existing gate's both-ways proof. The only thing
that separates them is whether they test the property. That is the gap this
module closes, so that is what this file asserts:

  relevant   a behavioural verify with adversarial cases        -> "relevant"
  proxies    grep-a-literal / AST-shape / benign case / half the
             property                                            -> "low"
  robustness innocuous padding in the refimpl must not drag a
             relevant verify down; a non-green refimpl and a
             single-hunk non-Python target must be UNPROVEN, never
             a pass; the tree must be left exactly as found

Expected counts are asserted (EXPECTED_CHECKS) so a case silently dropped
from this file cannot read as coverage -- the same guard signoff-canary uses.

  test-verify-relevance.py            # all
  test-verify-relevance.py -v         # print every mutant outcome
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

BIN = Path(os.environ.get("RELEVANCE_BIN", Path(__file__).resolve().parent))
spec = importlib.util.spec_from_file_location("vr", BIN / "verify-relevance.py")
vr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vr)

TARGET_BUGGY = '''\
"""Toy target: decides whether a superseded item may be hard-deleted."""

THRESHOLD = 3


def is_safe(item, flag):
    # BUG: no elapsed-time gate at all, so a freshly-created item is "safe".
    return bool(flag) and (item.get("count") or 0) > 0
'''

TARGET_FIXED = '''\
"""Toy target: decides whether a superseded item may be hard-deleted."""

THRESHOLD = 3


def is_safe(item, flag):
    if not flag:
        return False
    return (item.get("count") or 0) >= THRESHOLD * 2
'''

# The same fix with innocuous lines added: a comment, a print, a logger call.
# Mutating THESE would produce equivalent mutants; the innocuous filter must
# keep them out of the evidence set.
TARGET_FIXED_PADDED = '''\
"""Toy target: decides whether a superseded item may be hard-deleted."""
import logging

THRESHOLD = 3
log = logging.getLogger("toy")


def is_safe(item, flag):
    # the flag gates everything else
    if not flag:
        log.debug("flag off for %r", item)
        return False
    print("checking", item)
    return (item.get("count") or 0) >= THRESHOLD * 2
'''

TASK = '''\
# TASK -- gate `is_safe` on the count threshold

## The defect (CONFIRMED)
`is_safe()` returns True for count=1.

## Must contain (literal tokens)
- `def is_safe(item, flag):`
- `THRESHOLD * 2`
'''

FIXTURE_ADVERSARIAL = '''\
import sys, importlib.util
spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
CASES = [
    ({"count": 1}, True, False),
    ({"count": 5}, True, False),
    ({"count": 6}, True, True),
    ({"count": 99}, True, True),
    ({"count": 99}, False, False),
    ({}, True, False),
    ({"count": None}, True, False),
]
fails = 0
for item, flag, want in CASES:
    try:
        got = m.is_safe(item, flag)
    except Exception as e:
        print("  FAIL is_safe(%r, %r) raised %s" % (item, flag, e)); fails += 1; continue
    if bool(got) != want:
        print("  FAIL is_safe(%r, %r) -> %r, want %r" % (item, flag, got, want)); fails += 1
sys.exit(1 if fails else 0)
'''

# BENIGN: discriminates (buggy code returns True for count=1... no: this case
# alone is satisfied by the buggy code too -- so add the one case the buggy
# code fails, count=1 -> False, and nothing else. Fails at baseline, passes
# with the fix, tests almost nothing about the boundary or the flag.
FIXTURE_BENIGN = '''\
import sys, importlib.util
spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fails = 0
if m.is_safe({"count": 99}, True) is not True: fails += 1
if m.is_safe({"count": 1}, True) is not False: fails += 1
sys.exit(1 if fails else 0)
'''

# HALF THE PROPERTY: tests the flag gate thoroughly, never the threshold.
FIXTURE_HALF = '''\
import sys, importlib.util
spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fails = 0
if m.is_safe({"count": 99}, False) is not False: fails += 1
if m.is_safe({"count": 1}, False) is not False: fails += 1
if m.is_safe({"count": 1}, True) is not False: fails += 1
sys.exit(1 if fails else 0)
'''

VERIFY_BEHAVIOURAL = '''\
#!/bin/bash
set -u
cd "$(dirname "$0")"
fails=0
python3 -c "import ast; ast.parse(open('target.py').read())" || fails=$((fails+1))
grep -q "THRESHOLD \\* 2" target.py || { echo "  FAIL literal"; fails=$((fails+1)); }
if python3 test_fixture.py; then echo "  ok cases"; else echo "  FAIL cases"; fails=$((fails+1)); fi
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
'''

VERIFY_GREP_PROXY = '''\
#!/bin/bash
set -u
cd "$(dirname "$0")"
fails=0
python3 -c "import ast; ast.parse(open('target.py').read())" || fails=$((fails+1))
grep -q "THRESHOLD \\* 2" target.py || { echo "  FAIL literal"; fails=$((fails+1)); }
grep -q "if not flag" target.py || { echo "  FAIL flag gate"; fails=$((fails+1)); }
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
'''

VERIFY_AST_PROXY = '''\
#!/bin/bash
set -u
cd "$(dirname "$0")"
fails=0
python3 - <<'PY' || fails=$((fails+1))
import ast, sys
t = ast.parse(open("target.py").read())
ok = any(isinstance(n, ast.Compare) and any(isinstance(o, ast.GtE) for o in n.ops)
         for n in ast.walk(t))
ok2 = any(isinstance(n, ast.If) for n in ast.walk(t))
sys.exit(0 if ok and ok2 else 1)
PY
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
'''


# SMOKE-RUN proxy: EXECUTES the fixed code (so every mutant is reached) and
# asserts nothing but "it did not crash". Discriminates both-ways through the
# literal grep. The shape a crash-kill-inflated scorer would flatter.
VERIFY_SMOKE_PROXY = '''\
#!/bin/bash
set -u
cd "$(dirname "$0")"
fails=0
grep -q "THRESHOLD \\* 2" target.py || { echo "  FAIL literal"; fails=$((fails+1)); }
python3 -c "import target; target.is_safe({'count': 1}, True); target.is_safe({}, False)" || fails=$((fails+1))
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
'''

# The relevant fix plus a SECOND added property the fixture never exercises.
TARGET_FIXED_UNEXERCISED = TARGET_FIXED + '''

def grace_days(item):
    days = (item.get("age") or 0) // 7 + 1
    return days
'''

# WIDE change: six independent gates, the fixture tests five adversarially and
# never calls the sixth. Every AVERAGE clears the threshold (5 of 6 sites at
# 1.0), which is exactly how a whole untested property hid inside a RELEVANT
# on the real wt-tokens dispatch (20 sites, one single-mutant hole -> 0.9).
_WIDE_BUGGY = "".join(f"def gate_{i}(x):\n    return x > 0\n\n\n" for i in range(1, 7))
_WIDE_FIXED = "".join(f"def gate_{i}(x):\n    return x >= 6\n\n\n" for i in range(1, 7))
FIXTURE_WIDE = '''\
import sys, importlib.util
spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fails = 0
for i in range(1, 6):
    g = getattr(m, "gate_%d" % i)
    for x, want in ((1, False), (5, False), (6, True), (7, True), (99, True)):
        if bool(g(x)) != want:
            print("  FAIL gate_%d(%r) -> %r, want %r" % (i, x, g(x), want)); fails += 1
sys.exit(1 if fails else 0)
'''
VERIFY_WIDE = '''\
#!/bin/bash
set -u
cd "$(dirname "$0")"
fails=0
python3 -c "import ast; ast.parse(open('target.py').read())" || fails=$((fails+1))
if python3 test_fixture.py; then echo "  ok cases"; else echo "  FAIL cases"; fails=$((fails+1)); fi
echo "--- $fails failed ---"
[ "$fails" -eq 0 ] && echo VERIFY_OK || exit 1
'''
# no Must-contain: a literal `x >= 6` would make every compare/const mutant
# literal-breaking and leave nothing to score
TASK_WIDE = "# TASK -- gate_1..gate_6 must require at least six\n"


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                          capture_output=True, text=True)


def build(root: Path, *, fixed=TARGET_FIXED, verify=VERIFY_BEHAVIOURAL,
          fixture=FIXTURE_ADVERSARIAL, buggy=TARGET_BUGGY, task=TASK,
          target_name="target.py"):
    root.mkdir(parents=True)
    (root / target_name).write_text(buggy)
    for c in (["git", "init", "-q", "-b", "main"],
              ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"],
              ["git", "add", "-A"], ["git", "commit", "-qm", "baseline"]):
        sh(c, root)
    (root / "TASK.md").write_text(task)
    (root / "verify.sh").write_text(verify)
    (root / "test_fixture.py").write_text(fixture)
    (root / target_name).write_text(fixed)
    (root / "fix.patch").write_text(sh(["git", "diff"], root).stdout)
    sh(["git", "checkout", "--", "."], root)
    return root


def both_ways(root: Path) -> bool:
    """Every case here must clear the EXISTING gate: red at baseline, green
    with the fix. If it does not, the case is testing the wrong thing."""
    rc0 = sh("bash verify.sh", root).returncode
    sh(["git", "apply", "fix.patch"], root)
    p = sh("bash verify.sh", root)
    sh(["git", "checkout", "--", "."], root)
    return rc0 != 0 and p.returncode == 0 and "VERIFY_OK" in p.stdout


def run_cli(root: Path, *extra):
    p = subprocess.run([sys.executable, str(BIN / "verify-relevance.py"),
                        str(root), "--refimpl", str(root / "fix.patch"),
                        "--json", *extra], capture_output=True, text=True)
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, {"verdict": "PARSE-ERROR", "_raw": p.stdout + p.stderr}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("-v", action="store_true")
    ap.add_argument("--keep", action="store_true")
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="relevance-test-"))
    checks = fails = 0

    def check(ok, label, extra=""):
        nonlocal checks, fails
        checks += 1
        fails += (not ok)
        print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{extra}]" if extra else ""))

    def show(rec):
        if a.v:
            for m in rec.get("mutants", []):
                if m["killed"] is None:
                    continue
                print(f"        {'KILLED ' if m['killed'] else 'SURVIVE'} "
                      f"{m['class']:<16} L{m['line']} {m['mutation']}")

    print("A. every case clears the EXISTING gate (both-ways) -- precondition")
    cases = {
        "relevant":  build(tmp / "relevant"),
        "grep":      build(tmp / "grep", verify=VERIFY_GREP_PROXY),
        "ast":       build(tmp / "ast", verify=VERIFY_AST_PROXY),
        "benign":    build(tmp / "benign", fixture=FIXTURE_BENIGN),
        "half":      build(tmp / "half", fixture=FIXTURE_HALF),
        "padded":    build(tmp / "padded", fixed=TARGET_FIXED_PADDED),
    }
    for name, root in cases.items():
        check(both_ways(root), f"{name:<9} fails at baseline, green with fix")

    print("\nB. the relevance verdict separates relevant from proxy")
    scores = {}
    for name, want in (("relevant", "relevant"), ("grep", "low"),
                       ("ast", "low"), ("benign", "low"), ("half", "low"),
                       ("padded", "relevant")):
        rc, rec = run_cli(cases[name])
        scores[name] = rec.get("score")
        check(rec.get("verdict") == want,
              f"{name:<9} -> {rec.get('verdict')} (want {want})",
              f"score={rec.get('score')} killed={rec.get('killed')}/"
              f"{rec.get('evidence_mutants')} lit-breaking={rec.get('literal_breaking')}")
        show(rec)
        want_rc = {"relevant": 0, "low": 1}[want]
        check(rc == want_rc, f"{name:<9} exit code {rc} (want {want_rc})")
        if want == "low":
            check(len(rec.get("survivors", [])) > 0,
                  f"{name:<9} names its survivors ({len(rec.get('survivors', []))})")

    # The margin matters more than the pass: a threshold that separates the
    # arms by 0.05 is a coin flip on the next real verify.
    lo = max(scores[k] or 0 for k in ("grep", "ast", "benign", "half"))
    hi = min(scores[k] or 0 for k in ("relevant", "padded"))
    thr = vr.DEFAULT_THRESHOLD
    check(hi >= thr + 0.1 and lo <= thr - 0.1,
          f"relevant ({hi}) and best proxy ({lo}) both >= 0.1 clear of threshold {thr}")

    print("\nC. literal-breaking mutants are generated but EXCLUDED from evidence")
    rc, rec = run_cli(cases["grep"])
    check(rec.get("literal_breaking", 0) > 0,
          f"grep case generated {rec.get('literal_breaking')} literal-breaking mutant(s)")
    ev = [m for m in rec.get("mutants", []) if m["literal_preserving"]]
    check(all(m["killed"] is not None for m in ev[:rec.get("tried", 0)]) and
          rec["evidence_mutants"] == rec["killed"] + rec["survived"],
          "score is computed over literal-preserving mutants only")

    print("\nD. innocuous padding is not mutated")
    rc, rec = run_cli(cases["padded"])
    klasses = [(m["class"], m["mutation"]) for m in rec.get("mutants", [])]
    check(not any("log.debug" in d or "print(" in d for _, d in klasses),
          "no mutant targets the print/log lines")

    print("\nE. UNPROVEN, never pass, when nothing can be measured")
    bad = build(tmp / "notgreen")
    (bad / "fix.patch").write_text(
        (bad / "fix.patch").read_text().replace("THRESHOLD * 2", "THRESHOLD * 3"))
    rc, rec = run_cli(bad)
    check(rec.get("verdict") == "unproven" and rc == 3,
          f"refimpl not green -> {rec.get('verdict')} exit {rc}",
          rec.get("reason", "")[:70])

    # Non-Python, single hunk: nothing to mutate.
    sw = (tmp / "swift")
    sw.mkdir()
    (sw / "T.swift").write_text("func f(_ x: Int) -> Bool { return x > 0 }\n")
    for c in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"],
              ["git", "commit", "-qm", "b"]):
        sh(c, sw)
    (sw / "verify.sh").write_text(
        '#!/bin/bash\ncd "$(dirname "$0")"\ngrep -q "x >= 6" T.swift && echo VERIFY_OK || exit 1\n')
    (sw / "T.swift").write_text("func f(_ x: Int) -> Bool { return x >= 6 }\n")
    (sw / "fix.patch").write_text(sh(["git", "diff"], sw).stdout)
    sh(["git", "checkout", "--", "."], sw)
    rc, rec = run_cli(sw)
    check(rec.get("verdict") == "unproven" and rc == 3,
          f"single-hunk non-Python -> {rec.get('verdict')} exit {rc}",
          rec.get("reason", "")[:70])

    print("\nF. language-agnostic hunk reverts on a multi-hunk non-Python fix")
    sh2 = (tmp / "shell")
    sh2.mkdir()
    (sh2 / "tool.sh").write_text("#!/bin/bash\nA=1\n\n\n\nB=1\necho $A $B\n")
    for c in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"],
              ["git", "commit", "-qm", "b"]):
        sh(c, sh2)
    (sh2 / "verify.sh").write_text(
        '#!/bin/bash\ncd "$(dirname "$0")"\n[ "$(bash tool.sh)" = "2 3" ] && echo VERIFY_OK || exit 1\n')
    (sh2 / "tool.sh").write_text("#!/bin/bash\nA=2\n\n\n\nB=3\necho $A $B\n")
    (sh2 / "fix.patch").write_text(sh(["git", "diff"], sh2).stdout)
    sh(["git", "checkout", "--", "."], sh2)
    rc, rec = run_cli(sh2, "--min-mutants", "2")
    check(rec.get("verdict") == "relevant" and rec.get("evidence_mutants") == 2,
          f"two-hunk shell fix -> {rec.get('verdict')} over {rec.get('evidence_mutants')} hunk reverts")
    # ...and a verify that only checks ONE hunk is caught by the other's revert
    (sh2 / "verify.sh").write_text(
        '#!/bin/bash\ncd "$(dirname "$0")"\ngrep -q "A=2" tool.sh && echo VERIFY_OK || exit 1\n')
    rc, rec = run_cli(sh2, "--min-mutants", "2")
    # The revert of the grepped hunk is literal-breaking (the verify greps
    # "A=2"), so it is excluded and only ONE evidence mutant remains: below
    # min_mutants, hence UNPROVEN. Either "low" or "unproven" is acceptable
    # here; "relevant" never is.
    check(rec.get("verdict") in ("low", "unproven"),
          f"verify covering one of two hunks -> {rec.get('verdict')} (never relevant)")
    # ...and since its ONE tried mutant SURVIVED (killed 0/1), that is positive
    # evidence the verify never runs the changed code -- LOW, never unproven
    # (2026-10-05 rt-bg-commitments-fix-sync-guard: an inlined-copy fixture with
    # killed 0/1 read as unproven and reached GO).
    check(rec.get("killed") == 0 and rec.get("verdict") == "low",
          f"zero kills on a below-min sample -> {rec.get('verdict')} "
          f"(killed {rec.get('killed')}/{rec.get('evidence_mutants')}), must be low")

    print("\nG. the tree is left exactly as found")
    for name in ("relevant", "grep"):
        st = sh(["git", "status", "--porcelain"], cases[name]).stdout
        tracked = [l for l in st.splitlines() if l[:2] != "??"]
        check(not tracked, f"{name:<9} no tracked modification after the run")
        check(not (cases[name] / "__pycache__").exists(),
              f"{name:<9} no __pycache__ left behind")
    # --applied mode: the fix stays applied and the file is byte-identical
    ap_root = build(tmp / "applied")
    sh(["git", "apply", "fix.patch"], ap_root)
    p = subprocess.run([sys.executable, str(BIN / "verify-relevance.py"),
                        str(ap_root), "--applied", "--json"],
                       capture_output=True, text=True)
    rec = json.loads(p.stdout) if p.stdout.strip().startswith("{") else {}
    check(rec.get("verdict") == "relevant" and
          (ap_root / "target.py").read_text() == TARGET_FIXED,
          "--applied measures in place and restores the FIXED file")

    print("\nH. stale-bytecode guard: an equal-length mutant in the same second is still killed")
    # `==` -> `!=` is the same byte length. With bytecode caching this would
    # load the previous mutant's .pyc and read as a survivor.
    eq_root = tmp / "eqlen"
    eq_root.mkdir()
    (eq_root / "target.py").write_text("def ok(x):\n    return x != 3\n")
    for c in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"],
              ["git", "commit", "-qm", "b"]):
        sh(c, eq_root)
    (eq_root / "test_fixture.py").write_text(
        "import sys, target\nsys.exit(0 if target.ok(3) and not target.ok(4) and not target.ok(5) else 1)\n")
    (eq_root / "verify.sh").write_text(
        '#!/bin/bash\ncd "$(dirname "$0")"\npython3 test_fixture.py && echo VERIFY_OK || exit 1\n')
    (eq_root / "target.py").write_text("def ok(x):\n    return x == 3\n")
    (eq_root / "fix.patch").write_text(sh(["git", "diff"], eq_root).stdout)
    sh(["git", "checkout", "--", "."], eq_root)
    # warm a pycache with the FIXED module the way a real verify would
    sh(["git", "apply", "fix.patch"], eq_root)
    sh([sys.executable, "-c", "import target"], eq_root)
    sh(["git", "checkout", "--", "."], eq_root)
    rc, rec = run_cli(eq_root, "--min-mutants", "1")
    m = [x for x in rec.get("mutants", []) if x["mutation"] == "== -> !="]
    check(bool(m) and m[0]["killed"] is True, "== -> != (same length) killed, not stale")

    print("\nI. the three scorer holes measured on wt-tokens (2026-09-03) stay closed")
    # I.1 a smoke-run proxy reaches every mutant and asserts nothing: LOW, not
    #     flattered by crash kills
    smoke = build(tmp / "smoke", verify=VERIFY_SMOKE_PROXY)
    check(both_ways(smoke), "smoke     fails at baseline, green with fix")
    rc, rec = run_cli(smoke)
    check(rec.get("verdict") == "low" and rc == 1,
          f"smoke     -> {rec.get('verdict')} (want low)",
          f"score={rec.get('score')} crash_kills={rec.get('crash_kills')}")
    check((rec.get("score") or 0) <= vr.DEFAULT_THRESHOLD - 0.1,
          f"smoke     score {rec.get('score')} is >= 0.1 under the threshold")
    check(len(rec.get("survivors", [])) > 0, "smoke     names its survivors")

    # I.2 an UNEXERCISED site is a verdict, not a footnote
    unex = build(tmp / "unex", fixed=TARGET_FIXED_UNEXERCISED)
    check(both_ways(unex), "unex      fails at baseline, green with fix")
    rc, rec = run_cli(unex)
    u = rec.get("unexercised_sites") or []
    check(rec.get("verdict") == "low" and "UNEXERCISED" in rec.get("reason", ""),
          f"unex      -> {rec.get('verdict')} with the reason naming UNEXERCISED sites")
    check(any(s.endswith(":13") for s in u) and any(s.endswith(":14") for s in u),
          f"unex      names the untested lines: {u}")

    # I.3 the averages CLEAR the threshold and the verdict is still LOW: this is
    #     the single-mutant hole (wt-tokens L247, 20 sites -> RELEVANT 0.9)
    wide = build(tmp / "wide", buggy=_WIDE_BUGGY, fixed=_WIDE_FIXED,
                 verify=VERIFY_WIDE, fixture=FIXTURE_WIDE, task=TASK_WIDE)
    check(both_ways(wide), "wide      fails at baseline, green with fix")
    rc, rec = run_cli(wide)
    ms, ss = rec.get("mutant_score") or 0, rec.get("site_score") or 0
    check(ms >= vr.DEFAULT_THRESHOLD and ss >= vr.DEFAULT_THRESHOLD,
          f"wide      both averages clear the threshold (mutant={ms} site={ss}) -- "
          f"the old verdict would have been relevant")
    check(rec.get("verdict") == "low",
          f"wide      -> {rec.get('verdict')} (want low: gate_6 is never called)")
    check(any(s.endswith(":22") for s in (rec.get("unexercised_sites") or [])),
          f"wide      names gate_6's line: {rec.get('unexercised_sites')}")

    # I.4 truncation is SITE-first, and an untried site is UNPROVEN, never relevant
    tr = build(tmp / "trunc")
    rc, rec = run_cli(tr, "--max-mutants", "3")
    tried_sites = sorted({m["line"] for m in rec.get("mutants", []) if m["killed"] is not None})
    check(tried_sites == [7, 8, 9] and rec.get("verdict") == "relevant",
          f"trunc     cap=3 tries every site once: {tried_sites} -> {rec.get('verdict')}")
    check(rec.get("truncated") is True and not rec.get("untried_sites"),
          "trunc     cap=3 is truncated but leaves no site untried")
    rc, rec = run_cli(tr, "--max-mutants", "2", "--min-mutants", "1")
    check(rec.get("verdict") == "unproven" and rc == 3,
          f"trunc     cap=2 (< 3 sites) -> {rec.get('verdict')} exit {rc} (never relevant)")
    check(rec.get("untried_sites") == ["target.py:9"] and "never tried" in rec.get("reason", ""),
          f"trunc     names the untried site: {rec.get('untried_sites')}")

    # I.5 the ordering itself: first N picks are N distinct sites
    fake = [vr.Mutant("f.py", k, f"{k}@{ln}", f"src-{k}-{ln}", ln)
            for ln in (30, 10, 20) for k in ("const-int", "compare-flip", "const-int")]
    order = vr._balanced_sample(fake, 3)
    check([m.lineno for m in order] == [10, 20, 30],
          f"_balanced_sample is site-first and deterministic: {[m.lineno for m in order]}")

    print("\nJ. a NEW/untracked file the refimpl creates is mutated (new-file blind spot)")
    # A refimpl that adds a brand-new file shows up in neither `git diff` nor
    # `git status` as a tracked change, so its lines used to be invisible: the
    # diff was empty and the run returned UNPROVEN "no tracked diff". The fix
    # stages such files intent-to-add before diffing, so their full contents
    # count as added lines and get mutated -- and the tree is still restored.
    nf = tmp / "newfile"
    nf.mkdir()
    (nf / "keep.py").write_text("# pre-existing, untouched by the refimpl\nX = 1\n")
    for c in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"],
              ["git", "commit", "-qm", "b"]):
        sh(c, nf)
    # the refimpl-cmd CREATES a new module (no tracked file is modified)
    (nf / "refimpl.sh").write_text(
        '#!/bin/bash\ncd "$(dirname "$0")"\ncat > helper.py <<'"'"'PY'"'"'\n'
        "def all_positive(xs):\n"
        "    if len(xs) == 0:\n"
        "        return False\n"
        "    return all(x > 0 for x in xs)\n"
        "PY\n")
    (nf / "TASK.md").write_text(
        "# TASK -- add all_positive\n\n## Must contain (literal tokens)\n- `def all_positive(xs):`\n")
    # a behavioural verify with the adversarial partial case -> must be RELEVANT
    (nf / "fixture.py").write_text(
        "import sys, importlib.util\n"
        "spec = importlib.util.spec_from_file_location('h', 'helper.py')\n"
        "m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)\n"
        "CASES = [([], False), ([1, 2], True), ([1, -1], False), ([5], True), ([0], False)]\n"
        "fails = sum(1 for xs, want in CASES if bool(m.all_positive(xs)) != want)\n"
        "sys.exit(1 if fails else 0)\n")
    (nf / "verify-behav.sh").write_text(
        '#!/bin/bash\ncd "$(dirname "$0")"\npython3 fixture.py && echo VERIFY_OK || exit 1\n')
    # a grep-only proxy verify -> must NOT be relevant
    (nf / "verify-grep.sh").write_text(
        '#!/bin/bash\ncd "$(dirname "$0")"\ngrep -q "def all_positive" helper.py && echo VERIFY_OK || exit 1\n')

    def run_cmd(root, verify):
        p = subprocess.run(
            [sys.executable, str(BIN / "verify-relevance.py"), str(root),
             "--refimpl-cmd", "bash refimpl.sh", "--verify", verify,
             "--min-mutants", "1", "--json"], capture_output=True, text=True)
        try:
            return p.returncode, json.loads(p.stdout)
        except Exception:
            return p.returncode, {"verdict": "PARSE-ERROR", "_raw": p.stdout + p.stderr}

    rc, rec = run_cmd(nf, "bash verify-behav.sh")
    check(rec.get("verdict") == "relevant" and rc == 0,
          f"new-file behavioural verify -> {rec.get('verdict')} (want relevant)",
          f"killed={rec.get('killed')}/{rec.get('evidence_mutants')}")
    check((rec.get("evidence_mutants") or 0) >= 1 and
          any(m["file"] == "helper.py" for m in rec.get("mutants", [])),
          "new-file lines produced mutants (added-lines no longer empty)")
    rc, rec = run_cmd(nf, "bash verify-grep.sh")
    check(rec.get("verdict") in ("low", "unproven") and rc != 0,
          f"new-file grep-only verify -> {rec.get('verdict')} (never relevant)")
    # the created file must be gone and no tracked/intent-to-add residue left
    st = sh(["git", "status", "--porcelain"], nf).stdout
    check(not (nf / "helper.py").exists(),
          "new-file refimpl output removed after the run")
    check(not any(l[:2] == "A " or (l[:2] != "??" and "helper.py" in l)
                  for l in st.splitlines()),
          "no intent-to-add residue for the new file (tree restored)")

    print("\nK. relevance SIGN-OFF: the mechanical completeness gate")
    # signoff_requirements derives the three classes from a measurement record.
    # A non-.py file means behavioral_added_lines returns None for it, so
    # no_mutant is empty and covered/survivor come straight from the mutants.
    fake_rec = {"mutants": [
        {"file": "a.ts", "line": 10, "mutation": "flip", "killed": True,  "snippet": "if (x)"},
        {"file": "a.ts", "line": 10, "mutation": "drop", "killed": False, "snippet": "if (x)"},
        {"file": "a.ts", "line": 22, "mutation": "flip", "killed": False, "snippet": "return y"},
        {"file": "a.ts", "line": 99, "mutation": "n/a",  "killed": None,  "snippet": ""},  # untried: ignored
    ]}
    reqs = vr.signoff_requirements(fake_rec, tmp, "diff --git a/a.ts b/a.ts\n")
    check(reqs["covered"] == ["a.ts:10"],
          f"requirements: killed-mutant line is COVERED -> {reqs['covered']}")
    check(reqs["survivor"] == ["a.ts:22"],
          f"requirements: all-survived line is SURVIVOR -> {reqs['survivor']}")
    check("a.ts:99" not in reqs["covered"] + reqs["survivor"],
          "requirements: an untried (killed=None) line is not a requirement")

    reqs2 = {"covered": ["a.ts:10"], "survivor": [], "no_mutant": ["s.sql:3"], "snippets": {}}
    # THE MANDATED SELF-TEST: an unmapped changed (behavioural) line is rejected.
    unmapped = vr.check_signoff(reqs2, {"mappings": [], "waivers": [
        {"line": "s.sql:3", "reason": "pure DDL"}]})
    check(not unmapped["ok"] and any("a.ts:10" in p for p in unmapped["problems"]),
          "sign-off REJECTS an unmapped behavioural line",
          f"problems={unmapped['problems']}")
    # a behavioural line cannot be waived away
    waived_behav = vr.check_signoff(reqs2, {"mappings": [], "waivers": [
        {"line": "a.ts:10", "reason": "trust me"}, {"line": "s.sql:3", "reason": "DDL"}]})
    check(not waived_behav["ok"] and any("cannot be waived" in p for p in waived_behav["problems"]),
          "sign-off REJECTS waiving a line that has a killed mutant")
    # a no-mutant line with no waiver is a hole
    no_waiver = vr.check_signoff(reqs2, {"mappings": [
        {"line": "a.ts:10", "killing_case": "handles x"}], "waivers": []})
    check(not no_waiver["ok"] and any("s.sql:3" in p for p in no_waiver["problems"]),
          "sign-off REJECTS a no-mutant line left unwaived")
    # an empty killing_case is not a mapping
    empty_case = vr.check_signoff(reqs2, {"mappings": [
        {"line": "a.ts:10", "killing_case": "  "}], "waivers": [
        {"line": "s.sql:3", "reason": "DDL"}]})
    check(not empty_case["ok"],
          "sign-off REJECTS a mapping with an empty killing_case")
    # a complete, consistent proposal passes
    complete = vr.check_signoff(reqs2, {"mappings": [
        {"line": "a.ts:10", "killing_case": "rejects when x is false"}], "waivers": [
        {"line": "s.sql:3", "reason": "pure schema DDL, no behavioural mutant"}]})
    check(complete["ok"] and not complete["problems"],
          "sign-off ACCEPTS a complete map-or-waive proposal")
    # a survivor line can never be signed off
    surv = vr.check_signoff({"covered": [], "survivor": ["a.ts:22"], "no_mutant": []},
                            {"mappings": [{"line": "a.ts:22", "killing_case": "x"}], "waivers": []})
    check(not surv["ok"] and any("NO-GO" in p for p in surv["problems"]),
          "sign-off REJECTS a survivor line (NO-GO, not sign-off-able)")

    EXPECTED_CHECKS = 66
    print()
    if checks < EXPECTED_CHECKS:
        print(f"RELEVANCE TEST TRUNCATED: ran {checks}, expected >= {EXPECTED_CHECKS}")
        return 1
    if fails:
        print(f"RELEVANCE TEST FAILED ({fails} of {checks})")
        return 1
    print(f"RELEVANCE TEST PASSED ({checks} checks): relevant verify scores high, "
          f"every proxy scores low, unmeasurable is UNPROVEN, tree restored")
    if not a.keep:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
