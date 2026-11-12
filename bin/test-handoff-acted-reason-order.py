#!/usr/bin/env python3
"""handoff-emit --acted --reason ordering (2026-10-06).

Two defects, both reproduced live against the real script in a sandbox:
  1. `--acted X --reason "landed in main"` (no --commit) printed REFUSED and
     exited 2 -- but the --acted block had ALREADY written X to acted.json, so the
     refusal cleared the job anyway. The merge-claim gate was decorative.
  2. `--acted X --override-signoff W --reason R` wrote the SIGN-OFF OVERRIDDEN
     entry, then a second block REPLACED it, erasing signoff_override_reason --
     the audit trail the override exists to leave.
Pinned: refused merge claim -> exit 2 AND X absent from acted.json; plain reason
-> recorded; override+reason -> both override audit and reason survive.
--revert-check runs the same cases against a .bak (expects them to FAIL there).
HANDOFF_SRC env overrides the file under test."""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("HANDOFF_SRC") or HERE / "handoff-emit.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def run(tmp, *args):
    env = dict(os.environ, HANDOFF_DIR=str(tmp / "ho"))
    p = subprocess.run([sys.executable, str(tmp / "handoff-emit.py"), *args],
                       capture_output=True, text=True, env=env)
    acted = json.loads((tmp / "ho" / "acted.json").read_text()) if (tmp / "ho" / "acted.json").exists() else {}
    return p.returncode, acted


def main(src):
    tmp = Path(tempfile.mkdtemp(prefix="horeason-"))
    (tmp / "ho").mkdir()
    shutil.copy(src, tmp / "handoff-emit.py")
    # signoff.json is read from the script's own directory.
    (tmp / "signoff.json").write_text(json.dumps({
        "fffffff00002": {"required": True, "reviewer": "owner", "verdict": None,
                         "reason": "test: requires sign-off"}}))
    rc, acted = run(tmp, "--acted", "fffffff00001", "--reason", "landed in main")
    chk("merge-claim reason w/o --commit exits 2", rc, 2)
    chk("refused merge claim does NOT mark acted", "fffffff00001" in acted, False)

    rc, acted = run(tmp, "--acted", "fffffff00003", "--reason", "superseded by later job")
    chk("plain reason exits 0", rc, 0)
    chk("plain reason recorded", (acted.get("fffffff00003") or {}).get("reason"), "superseded by later job")

    rc, acted = run(tmp, "--acted", "fffffff00002", "--override-signoff", "WHY-X",
                    "--reason", "superseded by later job")
    e = acted.get("fffffff00002") or {}
    chk("override+reason exits 0", rc, 0)
    chk("override audit survives a --reason", e.get("signoff_override_reason"), "WHY-X")
    chk("reason also recorded with override", e.get("reason"), "superseded by later job")
    shutil.rmtree(tmp, ignore_errors=True)
    return not FAILS


if __name__ == "__main__":
    if "--revert-check" in sys.argv:
        bak = Path(sys.argv[sys.argv.index("--revert-check") + 1])
        ok = main(bak)
        print("REVERT-CHECK:", "test bites (fails on pre-fix source)" if not ok else "VACUOUS -- passes on pre-fix source")
        sys.exit(0 if not ok else 1)
    ok = main(SRC)
    print("PASS" if ok else f"FAILED: {FAILS}")
    sys.exit(0 if ok else 1)
