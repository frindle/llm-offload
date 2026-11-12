#!/usr/bin/env python3
"""verify-relevance.py --applied (the GATE's relevance step) measures an UNTRACKED
creation target declared in .dispatch-harness.json (2026-10-03,
idle-test-repo-duration2 41f36e7a653e: gate verify_relevance unproven "exited 3:
the reference impl produced no tracked diff; nothing to mutate").

Asserted (real verify-relevance.py on throwaway repos, target in a NEW dir):
  1. declared creation target + strong verify -> verdict relevant (rc 0)
  2. declared creation target + weak verify  -> verdict low (mutants really land)
  3. after the run the target is still UNTRACKED with its bytes intact (the
     intent-to-add was undone; --applied leaves the fix in place)
  4. NO manifest -> still exit 3 (no undecidable untracked file is mutated)
  5. creation_task false -> still exit 3

VR_SRC env overrides the verify-relevance.py under test (revert-check: the .bak).
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

VR = os.environ.get("VR_SRC") or str(Path(__file__).resolve().parent / "verify-relevance.py")
FAILS = []
IMPL = '''def to_secs(s):
    s = s.strip()
    if not s:
        raise ValueError("empty")
    n = int(s[:-1])
    if s.endswith("m"):
        return n * 60
    if s.endswith("h"):
        return n * 3600
    return n
'''
STRONG = '''import sys
sys.path.insert(0, "idlepure")
from duration import to_secs
f = 0
for a, b in (("2m", 120), ("1h", 3600), ("5s", 5), (" 3m ", 180)):
    if to_secs(a) != b:
        print("FAIL", a); f += 1
try:
    to_secs("  "); f += 1
except ValueError as e:
    if str(e) != "empty": f += 1
print(f"--- {f} failed ---")
sys.exit(1 if f else 0)
'''
WEAK = '''import sys
sys.path.insert(0, "idlepure")
import duration
print("--- 0 failed ---")
sys.exit(0 if hasattr(duration, "to_secs") else 1)
'''


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def g(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def build(wt: Path, fixture, manifest=True, creation=True):
    wt.mkdir(parents=True)
    g(wt, "init", "-q")
    (wt / "README.md").write_text("x\n")
    g(wt, "add", "-A")
    g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    (wt / "test_fixture.py").write_text(fixture)
    (wt / "verify.sh").write_text(
        "export PYTHONDONTWRITEBYTECODE=1\npython3 test_fixture.py || exit 1\necho VERIFY_OK\n")
    g(wt, "add", "test_fixture.py", "verify.sh")
    g(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seal")
    if manifest:
        (wt / ".dispatch-harness.json").write_text(json.dumps(
            {"authored": ["verify.sh", "test_fixture.py"],
             "target": "idlepure/duration.py", "creation_task": creation}))
        ex = wt / ".git" / "info" / "exclude"
        ex.parent.mkdir(parents=True, exist_ok=True)
        ex.write_text((ex.read_text() if ex.exists() else "") + "\n.dispatch-harness.json\n")
    (wt / "idlepure").mkdir()
    (wt / "idlepure" / "duration.py").write_text(IMPL)   # the job's finished output
    return wt


def run(wt):
    r = subprocess.run([sys.executable, VR, str(wt), "--applied", "--verify",
                        "bash verify.sh", "--json", "--max-mutants", "25"],
                       capture_output=True, text=True, timeout=600)
    try:
        rec = json.loads(r.stdout) if r.stdout.strip().startswith("{") else {}
    except ValueError:
        rec = {}
    return r.returncode, rec, r.stderr


def main():
    with tempfile.TemporaryDirectory() as td:
        T = Path(td)
        wt = build(T / "strong", STRONG)
        rc, rec, err = run(wt)
        if "-v" in sys.argv:
            print(rc, json.dumps(rec)[:600], err[-600:])
        check("declared creation target, strong verify -> relevant",
              rec.get("verdict"), "relevant")
        check("target still untracked afterwards",
              g(wt, "status", "--porcelain", "-uall", "--", "idlepure").stdout.strip(),
              "?? idlepure/duration.py")
        check("target bytes intact", (wt / "idlepure" / "duration.py").read_text(), IMPL)

        wt = build(T / "weak", WEAK)
        rc, rec, err = run(wt)
        check("declared creation target, weak verify -> low", rec.get("verdict"), "low")

        wt = build(T / "nomanifest", STRONG, manifest=False)
        rc, rec, err = run(wt)
        check("no manifest -> still exit 3 (nothing undecidable mutated)", rc, 3)

        wt = build(T / "notcreation", STRONG, creation=False)
        rc, rec, err = run(wt)
        check("creation_task false -> still exit 3", rc, 3)

    print(f"\n--- {len(FAILS)} failed ---")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
