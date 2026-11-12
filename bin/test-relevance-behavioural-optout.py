#!/usr/bin/env python3
"""Behavioural + revert test: a `relevance: unobservable` opt-out on a
BEHAVIOURAL line (branch condition / return of a computed value / comparison /
status-response construction) is NOT honoured by verify-relevance.py, while a
genuinely unobservable line (logging, cleanup, constant table) still is.

Defect (2026-10-06): authoring models opted out the decision lines --
rt-bfmr-link-sync-feedback r1 (`if (webError)`, `webRows === 0`) and the
rt-bg-commitments-fix-sync-guard refimpl (the whole guard, both copies) -- and
every mutant there vanished, so preflight read RELEVANT on fixtures that never
tested those branches.

  test-relevance-behavioural-optout.py                 # against ~/bin
  RELEVANCE_BIN_FILE=<old verify-relevance.py> ...     # revert-test: must FAIL
Also exercises the auto-harness-check static rule (HARNESS_CHECK in
ollama-dispatch-auto) unless RELEVANCE_BIN_FILE is set.
"""
from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
VR_FILE = Path(os.environ.get("RELEVANCE_BIN_FILE", BIN / "verify-relevance.py"))
_vl = SourceFileLoader("vr", str(VR_FILE))
vr = importlib.util.module_from_spec(importlib.util.spec_from_loader("vr", _vl))
_vl.exec_module(vr)

BUGGY = '''\
THRESHOLD = 3


def is_safe(item, flag):
    return bool(flag)
'''

# The fix, with the author's opt-out on the THRESHOLD comparison -- the property.
FIXED_OPTOUT_BEHAVIOURAL = '''\
import logging
THRESHOLD = 3
log = logging.getLogger("t")


def is_safe(item, flag):
    if not flag:
        log.debug("flag off")  # relevance: unobservable
        return False
    return (item.get("count") or 0) >= THRESHOLD * 2  # relevance: unobservable
'''

# A fixture that tests ONLY the flag gate -- never the threshold. Both-ways
# discriminating (buggy is_safe({}, True) is True), irrelevant to the property.
FIXTURE_HALF = '''\
import sys, importlib.util
spec = importlib.util.spec_from_file_location("target", "target.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
fails = 0
if m.is_safe({"count": 99}, False) is not False: fails += 1
if m.is_safe({"count": 1}, False) is not False: fails += 1
if m.is_safe({"count": 1}, True) is not False: fails += 1
if m.is_safe({"count": 0}, True) is not False: fails += 1
sys.exit(1 if fails else 0)
'''

VERIFY = '''\
#!/bin/bash
cd "$(dirname "$0")"
if python3 test_fixture.py; then echo VERIFY_OK; else echo "  FAIL cases"; exit 1; fi
'''


def sh(cmd, cwd):
    return subprocess.run(cmd, cwd=cwd, shell=isinstance(cmd, str),
                          capture_output=True, text=True)


checks = fails = 0


def check(ok, label, extra=""):
    global checks, fails
    checks += 1
    fails += (not ok)
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{extra}]" if extra else ""))


def build(root: Path, fixed: str) -> str:
    root.mkdir(parents=True)
    (root / "target.py").write_text(BUGGY)
    for c in (["git", "init", "-q", "-b", "main"], ["git", "config", "user.email", "t@t"],
              ["git", "config", "user.name", "t"], ["git", "add", "-A"],
              ["git", "commit", "-qm", "baseline"]):
        sh(c, root)
    (root / "verify.sh").write_text(VERIFY)
    (root / "test_fixture.py").write_text(FIXTURE_HALF)
    (root / "TASK.md").write_text("# TASK\n")
    (root / "target.py").write_text(fixed)
    return sh(["git", "diff", "-U0", "--", "target.py"], root).stdout


def main() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="optout-behav-"))

    print("A. end-to-end: an opt-out on the property's comparison does not hide it")
    root = tmp / "half"
    diff = build(root, FIXED_OPTOUT_BEHAVIOURAL)
    check(sh("bash verify.sh", root).returncode == 0, "precondition: fix turns the half fixture green")
    rec = vr.measure_applied(root, "bash verify.sh", diff, max_mutants=40, budget_s=300)
    check(rec.get("verdict") == "low",
          f"half-property fixture -> {rec.get('verdict')} (want low; the opted-out "
          f"comparison's mutants must be counted)",
          f"score={rec.get('score')} killed={rec.get('killed')}/{rec.get('evidence_mutants')}")
    lines_mutated = {m["line"] for m in rec.get("mutants", [])}
    check(10 in lines_mutated, "mutants WERE generated on the opted-out `>= THRESHOLD * 2` line")
    check(8 not in lines_mutated, "the opted-out log line is still honoured (no mutants)")
    rej = rec.get("optout_rejected") or []
    check([r.get("line") for r in rej] == [10],
          f"the rejected opt-out is REPORTED (line 10 only): {[r.get('line') for r in rej]}")
    check("IGNORED" in (rec.get("reason") or ""), "the reason names the ignored opt-out")
    check(sh(["git", "status", "--porcelain", "--", "target.py"], root).stdout.startswith(" M"),
          "tree left carrying the fix (restored after mutation)")

    print("\nB. pure classifier: behavioural lines vs genuinely unobservable ones")
    K = getattr(vr, "behavioural_optout_kind", None)
    behavioural = [
        "        if ((d.webError as string | undefined) && String(d.webError)) { // relevance: unobservable",
        "          } else if ((d.webRows ?? null) !== null && Number(d.webRows) === 0) { // relevance: unobservable",
        "          const webNeededVal = d.webNeeded != null ? Number(d.webNeeded) : undefined; // relevance: unobservable",
        "  if (commitments.length === 0 && existing.length > 0) { // relevance: unobservable",
        "    return Response.json( // relevance: unobservable",
        "      { status: 502 }, // relevance: unobservable",
        "    return {\"error\": \"JSON is not an object\"}  # relevance: unobservable",
        "    if not isinstance(obj, dict):  # relevance: unobservable",
        "    x = a if cond else b  # relevance: ignore",
        "    raise HTTPException(status_code=409)  # relevance: benign",
        "  return res.status(409).json({ error: 'busy' }); // relevance: unobservable",
        "    ok = total >= limit  # relevance: unobservable",
    ]
    benign = [
        "    log.debug(\"flag off == %s\", x)  # relevance: unobservable",
        "                log(f\"[worker] saved {n} chars ({a > b}).\")  # relevance: unobservable",
        "  console.log('a === b', x); // relevance: unobservable",
        "    conn.close()  # relevance: unobservable",
        "    text.setFont(\"Helvetica\", 10)  # relevance: unobservable (font choice)",
        "      page_no: String(pageNo), // relevance: unobservable",
        "    (\"dns\", (\"err_name_not_resolved\", \"nxdomain\")),  # relevance: unobservable",
        "    return None  # relevance: unobservable",
        "    esc = False  # relevance: unobservable -- if x == y else z, prose in the comment",
        "    const handler = (e) => e;  // relevance: unobservable",
        "    items: Array<string> = [];  // relevance: unobservable",
        "    msg = \"a >= b and c\"  # relevance: unobservable",
    ]
    if K is None:
        check(False, "verify-relevance exposes behavioural_optout_kind()")
    else:
        for ln in behavioural:
            check(K(ln) is not None, f"REJECT  {ln.strip()[:70]}", K(ln))
        for ln in benign:
            check(K(ln) is None, f"honour  {ln.strip()[:70]}", K(ln))

    if os.environ.get("RELEVANCE_BIN_FILE") is None:
        print("\nC. auto-harness-check fails a refimpl that opts out a behavioural line")
        auto = SourceFileLoader("auto", os.environ.get("AUTO_FILE", str(BIN / "ollama-dispatch-auto")))
        aspec = importlib.util.spec_from_loader("auto", auto)
        amod = importlib.util.module_from_spec(aspec)
        auto.exec_module(amod)
        for name, line, want_fail in (
                ("bad", "  if (rows === 0) { // relevance: unobservable", True),
                ("bad-py", "    return total >= limit  # relevance: unobservable", True),
                ("good", "  console.log('rows', rows); // relevance: unobservable", False)):
            wt = tmp / f"hc-{name}"
            wt.mkdir()
            sh(["git", "init", "-q", "-b", "main"], wt)
            (wt / "TASK.md").write_text("# TASK\nOnly edit `lib/x.ts`.\n\n## Must contain\n- `rows`\n")
            (wt / "verify.sh").write_text("#!/bin/bash\nexit 1\n")
            (wt / "test_fixture.py").write_text("import sys\nsys.exit(1)\n")
            (wt / "refimpl.py").write_text("NEW = r'''\n" + line + "\n'''\n")
            amod.write_harness_check(wt, "python")
            r = sh([sys.executable, "auto-harness-check.py"], wt)
            failed = r.returncode == 1 and "BEHAVIOURAL OPT-OUT" in r.stdout
            check(failed == want_fail,
                  f"{name}: self-check {'FAILS on the opt-out' if failed else 'does not flag it'} "
                  f"(want {'FAIL' if want_fail else 'no flag'})",
                  (r.stdout + r.stderr).strip()[-160:])

    if os.environ.get("RELEVANCE_BIN_FILE") is None:
        print("\nD. auto-harness-check fails a scope line that lets the coding job edit the harness")
        for name, scope, want_fail in (
                ("harness-in-scope", "Only edit `lib/x.ts` (via refimpl.py), `verify.test.ts`, and `TASK.md`.", True),
                ("verify-sh-in-scope", "Only edit `lib/x.ts`, `verify.sh`.", True),
                ("correct", "Only edit `lib/x.ts`; do not edit `verify.sh`, `verify.test.ts` or `TASK.md`.", False),
                ("test-file-target", "Only edit `lib/x.test.ts`; do not edit `verify.sh`.", False)):
            wt = tmp / f"scope-{name}"
            wt.mkdir()
            sh(["git", "init", "-q", "-b", "main"], wt)
            (wt / "TASK.md").write_text(f"# TASK\n{scope}\n\n## Must contain\n- `rows`\n")
            (wt / "verify.sh").write_text("#!/bin/bash\nexit 1\n")
            (wt / "verify.test.ts").write_text("// fixture\n")
            (wt / "refimpl.py").write_text("NEW = r'''\n  const rows = 1;\n'''\n")
            amod.write_harness_check(wt, "ts")
            r = sh([sys.executable, "auto-harness-check.py"], wt)
            failed = r.returncode == 1 and "lets the coding job edit harness" in r.stdout
            check(failed == want_fail,
                  f"{name}: {'FAILS on scope' if failed else 'scope accepted'} "
                  f"(want {'FAIL' if want_fail else 'accept'})", r.stdout.strip()[-140:])

    if os.environ.get("RELEVANCE_BIN_FILE") is None:
        print("\nE. multi-module guard ignores the dispatch's own harness file names")
        txt = ("refimpl.py must extract it; do not edit verify.sh, verify.test.ts or "
               "check_literals.py")
        got = amod.foreign_module_files(txt, "components/X.tsx", None)
        check(got == [], f"harness names are not foreign modules: {got}")
        got2 = amod.foreign_module_files(txt + "; also build lib/newThing.ts and lib/other.ts",
                                         "components/X.tsx", None)
        check(got2 == ["lib/newThing.ts", "lib/other.ts"],
              f"real foreign modules are still detected: {got2}")

    print(f"\n{checks - fails}/{checks} checks passed")
    print("BEHAVIOURAL OPT-OUT TEST PASSED" if not fails else "BEHAVIOURAL OPT-OUT TEST FAILED")
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
