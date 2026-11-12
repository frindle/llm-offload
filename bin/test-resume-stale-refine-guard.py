#!/usr/bin/env python3
"""--resume-harness must not be refused by a STALE .refine-guard.json left by a
driver stopped mid-refine (2026-10-06, rt-bfmr-tls-fingerprint: resume refused
"REFINE NOT DONE: 30 surviving mutation(s)" for a converged harness).
Uses the REAL generated auto-harness-check.py. --revert-check -> RED."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[-400:]))
    if not ok:
        FAILS.append(name)


def main():
    ld = SourceFileLoader("oda_rsg", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_rsg", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    wt = Path(tempfile.mkdtemp(prefix="rsg-")) / "wt"
    wt.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (wt / "target.txt").write_text("stub\n"); g("add", "target.txt"); g("commit", "-qm", "b")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `target.txt`\n")
    (wt / "verify.sh").write_text('grep -q MARK target.txt && echo VERIFY_OK && exit 0; echo "FAIL - no MARK"; exit 1\n')
    (wt / "refimpl.py").write_text("open('target.txt','w').write('MARK\\n')\n")
    (wt / "verify.test.ts").write_text("// fixture\n")
    (wt / "check_literals.py").write_text("")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "target.txt"}))
    for f in getattr(m, "HARNESS_FILES_REQUIRED", ()):
        if not (wt / f).exists():
            (wt / f).write_text("")
    m.write_harness_check(wt, "ts")
    m.write_refine_guard(wt, "target.txt", [{"file": "target.txt", "line": 1}], "ts")
    pre = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                         capture_output=True, text=True).stdout
    check("precondition: stale guard makes the bare self-check fail", "REFINE NOT DONE" in pre, pre)
    ok, why = m.resume_harness_check(wt, "target.txt", "ts", reset=lambda w, t: [])
    check("resume accepts a converged harness despite a stale guard", ok, why)
    check("stale guard removed", not (wt / m.REFINE_GUARD).exists())
    # a harness that genuinely does not self-check is still refused
    (wt / "refimpl.py").write_text("pass\n")
    ok2, why2 = m.resume_harness_check(wt, "target.txt", "ts", reset=lambda w, t: [])
    check("broken harness still refused", not ok2, why2)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


def revert_check():
    src = AUTO.read_text()
    a = src.index("    # STALE REFINE GUARD (2026-10-06")
    b = src.index("    if check is None:", a)
    with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
        f.write(src[:a] + src[b:])
    r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                       capture_output=True, text=True, timeout=300)
    os.unlink(f.name)
    red = r.returncode != 0
    print(("bites" if red else "INERT") + ": remove stale-guard clear -> suite " + ("RED" if red else "green"))
    print("REVERT-CHECK OK" if red else "REVERT-CHECK FAILED")
    return 0 if red else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
