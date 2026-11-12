#!/usr/bin/env python3
"""auto-harness-check: a CREATION target in a NEW untracked directory is measured
against the scaffold stub, not the model's on-disk solution (2026-10-02,
idle-test-repo-duration 2184ebe8952d output_cap_loop).

Plain `git status --porcelain` collapses the new dir to "?? idlepure/", so the
target never reached pre_snap, the creation-stub branch was skipped, and the self
check reported "verify.sh PASSES at baseline ... no baseline target could be
determined from .dispatch-harness.json" for a fixture that discriminates.

Asserted:
  1. new-dir creation target holding the SOLUTION -> self-check PASSES (baseline
     measured on the stub fails as it must; refimpl makes it green)
  2. the model's on-disk solution is left exactly as found afterwards
  3. control: same target in an EXISTING tracked dir -> PASSES (no regression)
  4. a genuinely vacuous fixture in the new dir still FAILS "PASSES at baseline"
     (the fix does not blind the discrimination check)

AUTO_SRC env overrides the ollama-dispatch-auto under test (revert-check: the .bak).
"""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

AUTO = os.environ.get("AUTO_SRC") or os.path.expanduser("~/bin/ollama-dispatch-auto")
loader = SourceFileLoader("oda_nd", AUTO)
spec = importlib.util.spec_from_loader("oda_nd", loader)
oda = importlib.util.module_from_spec(spec)
loader.exec_module(oda)
FAILS = []

SOLUTION = "def secs(s):\n    return int(s[:-1]) * 60  # MARKER\n"


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def build(wt: Path, target: str, vacuous=False, tracked_dir=False):
    wt.mkdir(parents=True)
    run = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True,
                                    capture_output=True)
    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (wt / "README.md").write_text("x\n")
    if tracked_dir:
        (wt / Path(target).parent).mkdir(parents=True)
        (wt / Path(target).parent / "keep.py").write_text("X = 1\n")
    run("add", "-A")
    run("commit", "-qm", "base")
    (wt / ".dispatch-harness.json").write_text(json.dumps(
        {"authored": ["TASK.md", "verify.sh", "test_fixture.py", "refimpl.py"],
         "target": target, "fixture": "test_fixture.py", "creation_task": True}))
    (wt / "TASK.md").write_text(
        f"## Must contain\n- `MARKER`\nOnly edit `{target}`; do not edit verify.sh\n")
    cond = "true" if vacuous else f"grep -q 'return int' {target} 2>/dev/null"
    (wt / "verify.sh").write_text(
        f'f=0\n{cond} || {{ echo "no impl"; f=1; }}\n'
        'echo "--- $f failed ---"; [ "$f" -eq 0 ] && echo VERIFY_OK || exit 1\n')
    (wt / "test_fixture.py").write_text("# fixture\n")
    (wt / "refimpl.py").write_text(
        "from pathlib import Path\n"
        f"p = Path({target!r}); p.parent.mkdir(parents=True, exist_ok=True)\n"
        f"p.write_text({SOLUTION!r})\n")
    # the MODEL already ran refimpl itself: the solution sits in the new file
    (wt / target).parent.mkdir(parents=True, exist_ok=True)
    (wt / target).write_text(SOLUTION)
    oda.write_harness_check(wt, "python")


def run_check(wt):
    return subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                          capture_output=True, text=True, timeout=300)


def main():
    with tempfile.TemporaryDirectory() as td:
        T = Path(td)
        t = "idlepure/duration.py"

        wt = T / "newdir"
        build(wt, t)
        r = run_check(wt)
        out = r.stdout + r.stderr
        if "-v" in sys.argv:
            print(out[-1500:])
        check("new-dir creation target: self-check passes",
              r.returncode == 0 and "VERIFY_OK" in r.stdout, True)
        check("new-dir: no 'PASSES at baseline' false alarm",
              "PASSES at baseline" in out, False)
        check("new-dir: model's solution left as found",
              (wt / t).read_text(), SOLUTION)

        wt = T / "trackeddir"
        build(wt, t, tracked_dir=True)
        r = run_check(wt)
        check("existing-dir control: self-check passes",
              r.returncode == 0 and "VERIFY_OK" in r.stdout, True)

        wt = T / "vacuous"
        build(wt, t, vacuous=True)
        r = run_check(wt)
        check("vacuous fixture in new dir still FAILS at baseline",
              r.returncode != 0 and "PASSES at baseline" in (r.stdout + r.stderr), True)

    print(f"\n--- {len(FAILS)} failed ---")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
