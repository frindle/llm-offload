#!/usr/bin/env python3
"""auto-harness-check.py must FAIL CLOSED when git is unusable in the worktree.

Root cause (2026-10-02, Rivian s1 auto-author 054f6829bd98): inside the queue's
context git failed ("not a git repository: (null)" / "couldn't read
.git/packed-refs: Resource deadlock avoided" -- iCloud-evicted .git metadata). The
generated check treated the failed `git diff --name-only HEAD` as "nothing
modified", so it never reset the target, measured the model's applied refimpl as
the baseline, and reported "verify.sh PASSES at baseline -- the fixture cannot
fail"; a clean re-measure then disagreed. Here:
  * broken git  -> exit 3 with the HARNESS ERROR marker, never the baseline-green
                   verdict, and the model's target bytes left untouched;
  * flaky git (fails twice, then works) -> the check retries and passes;
  * healthy git -> the dirty target is reset for the measurement (VERIFY_OK).
--revert-check mutates the template and requires this suite to go RED.
AUTO_SRC env overrides the ollama-dispatch-auto under test."""
import importlib.util, json, os, shutil, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

AUTO = Path(os.environ.get("AUTO_SRC") or Path(__file__).resolve().parent / "ollama-dispatch-auto")
FAILS = []
REAL_GIT = shutil.which("git")


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    loader = SourceFileLoader("oda_gfc", str(AUTO))
    spec = importlib.util.spec_from_loader("oda_gfc", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def build(root, oda):
    wt = root / "wt"
    wt.mkdir(parents=True)
    g = lambda *a: subprocess.run([REAL_GIT, "-C", str(wt), *a], check=True, capture_output=True)
    g("init", "-q")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (wt / "target.txt").write_text("stub\n")
    g("add", "target.txt")
    g("commit", "-qm", "base")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `target.txt`; do not edit verify.sh\n")
    (wt / "verify.sh").write_text(
        'f=0\ngrep -q MARK target.txt 2>/dev/null || { echo "no MARK"; f=1; }\n'
        'echo "--- $f failed ---"; [ "$f" -eq 0 ] && echo VERIFY_OK || exit 1\n')
    (wt / "refimpl.py").write_text("open('target.txt','w').write('MARK\\n')\n")
    (wt / "test_fixture.py").write_text("# fixture\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "target.txt"}))
    oda.write_harness_check(wt, "py")
    # the model ALREADY applied its refimpl (exactly what 054f6829bd98 left behind)
    (wt / "target.txt").write_text("MARK\n# model's edit\n")
    return wt


def shim(root, fail_first):
    """A `git` on PATH that fails the first `fail_first` calls like the evicted
    repo did (fail_first=-1: always), then execs the real git."""
    d = root / "shim"
    d.mkdir()
    cnt = root / "count"
    cnt.write_text("0")
    (d / "git").write_text(
        "#!/bin/bash\n"
        f'n=$(cat "{cnt}"); echo $((n+1)) > "{cnt}"\n'
        f'if [ {fail_first} -lt 0 ] || [ "$n" -lt {fail_first} ]; then\n'
        '  echo "fatal: not a git repository: (null)" >&2; exit 128; fi\n'
        f'exec "{REAL_GIT}" "$@"\n')
    os.chmod(d / "git", 0o755)
    return d


def run(wt, shimdir=None):
    env = dict(os.environ)
    if shimdir:
        env["PATH"] = f"{shimdir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                          capture_output=True, text=True, env=env, timeout=300)


def main():
    oda = load()
    root = Path(tempfile.mkdtemp(prefix="gfc-")).resolve()

    wt = build(root / "a", oda)
    r = run(wt, shim(root / "a", -1))
    out = r.stdout + r.stderr
    check("broken git -> exit 3 (harness error, not a verdict)", r.returncode, 3)
    check("broken git -> HARNESS ERROR marker printed", "HARNESS ERROR: git is unusable" in out, True)
    check("broken git -> NEVER the baseline-green verdict", "PASSES at baseline" in out, False)
    check("broken git -> the model's target bytes are untouched",
          (wt / "target.txt").read_text(), "MARK\n# model's edit\n")

    wt = build(root / "b", oda)
    r = run(wt, shim(root / "b", 2))
    check("flaky git (2 failures) -> retried, VERIFY_OK", ("VERIFY_OK" in r.stdout, r.returncode), (True, 0))

    wt = build(root / "c", oda)
    r = run(wt)
    check("healthy git -> dirty target reset for the measurement -> VERIFY_OK",
          ("VERIFY_OK" in r.stdout, r.returncode), (True, 0))
    check("healthy git -> model's target bytes restored afterwards",
          (wt / "target.txt").read_text(), "MARK\n# model's edit\n")

    # CREATION task: target untracked, the model left its refimpl output in it
    wt = root / "d" / "wt"
    wt.mkdir(parents=True)
    g = lambda *a: subprocess.run([REAL_GIT, "-C", str(wt), *a], check=True, capture_output=True)
    g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (wt / "README").write_text("x\n"); g("add", "README"); g("commit", "-qm", "base")
    (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `new.ts`; do not edit verify.sh\n")
    (wt / "verify.sh").write_text(
        'f=0\ngrep -q MARK new.ts 2>/dev/null || { echo "no MARK"; f=1; }\n'
        'echo "--- $f failed ---"; [ "$f" -eq 0 ] && echo VERIFY_OK || exit 1\n')
    (wt / "refimpl.py").write_text("open('new.ts','w').write('MARK\\n')\n")
    (wt / "test_fixture.py").write_text("# fixture\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "new.ts", "creation_task": True}))
    oda.write_harness_check(wt, "py")
    (wt / "new.ts").write_text("MARK\n// the model ran refimpl.py itself\n")
    r = run(wt)
    check("creation task, solution left in the untracked target -> measured vs the stub -> VERIFY_OK",
          ("VERIFY_OK" in r.stdout, "PASSES at baseline" in r.stdout, r.returncode), (True, False, 0))
    check("creation task -> the model's target bytes restored afterwards",
          (wt / "new.ts").read_text(), "MARK\n// the model ran refimpl.py itself\n")

    sl = SourceFileLoader("sl_gfc", str(Path(__file__).resolve().parent / "ollama-dispatch-slice"))
    spec = importlib.util.spec_from_loader("sl_gfc", sl)
    s = importlib.util.module_from_spec(spec)
    sys.argv = ["ollama-dispatch-slice"]
    sl.exec_module(s)
    reason, det = s.auto_failure_reason(
        "  HARNESS ERROR: git is unusable in this worktree (`git status` rc=128)\n")
    check("slicer surfaces the harness error as the reason, NOT deterministic",
          (bool(reason) and "HARNESS ERROR" in reason, det), (True, False))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("old fail-open git()", "        if r.returncode == 0:\n            return r\n        _t.sleep",
     "        return r\n        _t.sleep"),
    ("no retry", "    for _i in range(4):\n        r = subprocess.run([\"git\"",
     "    for _i in range(1):\n        r = subprocess.run([\"git\""),
    ("creation baseline measured as-is",
     '        elif (_t and _hjd.get("creation_task") and _t in pre_snap):',
     '        elif False:'),
    ("exits 1 like a fixture failure", "    sys.exit(3)\n\n\ndef fail(m):", "    sys.exit(1)\n\n\ndef fail(m):"),
]


def revert_check():
    bad = 0
    src = AUTO.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(AUTO.parent)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True, timeout=900)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
