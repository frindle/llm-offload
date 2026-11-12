#!/usr/bin/env python3
"""auto-harness-check names the FIXTURE as the suspect when the same case fails
with refimpl.py applied on consecutive runs (2026-10-02, Rivian s5: a case-2 fetch
stub never counted calls; 7 authoring attempts edited refimpl/target instead).
Builds a real worktree from the REAL template: run 1 -> no suspect hint; run 2
(same failing case) -> hint naming that case; a DIFFERENT failing case -> no hint.
--revert-check removes the repeat block from the template -> RED."""
import importlib.util, json, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + extra[-400:]))
    if not ok:
        FAILS.append(name)


def main():
    ld = SourceFileLoader("oda_fs", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_fs", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    wt = Path(tempfile.mkdtemp(prefix="fs-")) / "wt"
    wt.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (wt / "target.txt").write_text("stub\n"); g("add", "target.txt"); g("commit", "-qm", "b")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `target.txt`\n")
    (wt / "case.txt").write_text("case2: made only 1 fetch call")
    (wt / "verify.sh").write_text(
        'grep -q MARK target.txt || { echo "FAIL - no MARK"; exit 1; }\n'
        'echo "ok - case1"; echo "FAIL - $(cat case.txt)  (got: 0)"; echo "--- 1 failed ---"; exit 1\n')
    (wt / "refimpl.py").write_text("open('target.txt','w').write('MARK\\n')\n")
    (wt / "verify_impl.mts").write_text("// fixture\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "target.txt"}))
    m.write_harness_check(wt, "ts")
    run = lambda: subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                                 capture_output=True, text=True, timeout=120).stdout
    o1 = run()
    check("run 1: refimpl-fails reported", "does not make verify.sh print" in o1, o1)
    check("run 1: no fixture-suspect hint yet", "SUSPECT THE FIXTURE" not in o1, o1)
    o2 = run()
    check("run 2, same case: fixture named as suspect, case quoted",
          "SUSPECT THE FIXTURE" in o2 and "'case2: made only 1 fetch call'" in o2, o2)
    (wt / "case.txt").write_text("case3: something else")
    o3 = run()
    check("a DIFFERENT failing case: no suspect hint", "SUSPECT THE FIXTURE" not in o3, o3)
    st = subprocess.run(["git", "-C", str(wt), "status", "--porcelain", "--", "target.txt"],
                        capture_output=True, text=True).stdout
    check("target left at baseline", st.strip() == "", st)
    leaked = [p.name for p in wt.iterdir() if "failcases" in p.name]
    check("no state file leaked into the worktree", not leaked, str(leaked))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    src = AUTO.read_text()
    a = src.index("    # REPEATED CASE -> SUSPECT THE FIXTURE")
    b = src.index('    fail("the reference impl (refimpl.py) does not make verify.sh print "', a)
    with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
        f.write(src[:a] + src[b:])
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                       capture_output=True, text=True, timeout=300)
    os.unlink(f.name)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + ": remove the repeat block -> suite " + ("RED" if red else "green"))
    print("REVERT-CHECK OK" if red else "REVERT-CHECK FAILED")
    return 0 if red else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
