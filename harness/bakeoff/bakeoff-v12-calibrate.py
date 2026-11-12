#!/usr/bin/env python3
"""Grader calibration for the v12 Strata head-to-head (run by the driver before ANY arm).

"Suspect the grader before incapacity": a false REJECT looks exactly like a model that
cannot do the task, and a false ACCEPT launders a non-fix into a pass. So both graders
are driven against KNOWN states, and the round does not run unless every one lands:

  DEBUG CELL (plex-release-group-debug) -- the grader is the cell's own RUN_VERIFY
  (restore both test files from the baseline, run them). Cases, each on a fresh clone
  of the debug-cell repo at the pinned baseline:
    pristine                    must FAIL  (the planted defect is present)
    known-good fix              must PASS  (the fix qwen3.8 shipped in v10-h2h r1,
                                            reviewed by hand: digits allowed in the token)
    alternate correct fix       must PASS  (different TEXT, same behaviour -- proves the
                                            grader checks behaviour, not one spelling)
    test-gaming (no code fix,   must FAIL  (the verify restores tests from the baseline,
      failing asserts deleted)              so editing the repro cannot score a pass)
    partial fix (digits OK but  must FAIL  (a plausible near-miss is not accepted)
      case-folding dropped)
  CELL E (bulk-codemod-v10) -- its byte-diff oracle's own --calibrate (known-good 49/49,
  known-bad named, pristine floor).

Usage: bakeoff-v12-calibrate.py [--json]   exit 0 = calibrated, 1 = do not run
"""
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

BAKEOFF = Path("/Users/user/Desktop/GitHub Projects/bakeoff")
DEBUG_REPO = Path("/Users/user/Desktop/GitHub Projects/plex-automation/plex-automation-debugcell")
BASELINE = "33d4735"
PYBIN = "/Users/user/bin/plex-bakeoff-venv/bin/python"
RUN_VERIFY = (f"git checkout {BASELINE} -- test_release_group_numeric.py test_release_group.py"
              f" && {PYBIN} test_release_group_numeric.py && {PYBIN} test_release_group.py")
CELL_E_SCORER = BAKEOFF / "bakeoff-v10-bulk-score.py"

OLD_RE = "RELEASE_GROUP_RE = re.compile(r'-([A-Za-z]+)$')"
OLD_RET = "return m.group(1).lower() if m else None"


def _sub(path, old, new):
    t = path.read_text()
    if t.count(old) != 1:
        raise RuntimeError(f"calibration fixture drifted: {old!r} not found once in {path.name}")
    path.write_text(t.replace(old, new))


def _strip_numeric_asserts(path):
    keep = [ln for ln in path.read_text().splitlines()
            if not ("assert" in ln and any(g in ln for g in ("GROUP2", "3EVILS", "x0r", "-1080'")))]
    path.write_text("\n".join(keep) + "\n")


CASES = [
    ("pristine", None, False),
    ("known-good fix", lambda w: _sub(w / "arr-webhook.py", OLD_RE,
                                      "RELEASE_GROUP_RE = re.compile(r'-([A-Za-z0-9]+)$')"), True),
    ("alternate correct fix", lambda w: _sub(w / "arr-webhook.py", OLD_RE,
                                             "RELEASE_GROUP_RE = re.compile(r'-([a-z\\d]+)$', re.IGNORECASE)"), True),
    ("test-gaming (repro asserts deleted, no fix)",
     lambda w: _strip_numeric_asserts(w / "test_release_group_numeric.py"), False),
    ("partial fix (case-folding dropped)",
     lambda w: (_sub(w / "arr-webhook.py", OLD_RE, "RELEASE_GROUP_RE = re.compile(r'-([A-Za-z0-9]+)$')"),
                _sub(w / "arr-webhook.py", OLD_RET, "return m.group(1) if m else None")), False),
]


def debug_cell():
    rows = []
    tmp = Path(tempfile.mkdtemp(prefix="v12cal-"))
    try:
        for name, mutate, want_pass in CASES:
            w = tmp / f"wt{len(rows)}"
            r = subprocess.run(["git", "clone", "-q", "--no-checkout", str(DEBUG_REPO), str(w)],
                               capture_output=True, text=True)
            if r.returncode == 0:
                r = subprocess.run(["git", "-C", str(w), "checkout", "-q", BASELINE],
                                   capture_output=True, text=True)
            if r.returncode != 0:
                rows.append({"case": name, "ok": False, "why": f"clone/checkout failed: {r.stderr[-200:]}"})
                continue
            try:
                if mutate:
                    mutate(w)
            except Exception as e:
                rows.append({"case": name, "ok": False, "why": str(e)})
                continue
            v = subprocess.run(["bash", "-c", RUN_VERIFY], cwd=str(w), capture_output=True,
                               text=True, timeout=300)
            passed = v.returncode == 0
            rows.append({"case": name, "want": "PASS" if want_pass else "FAIL",
                         "got": "PASS" if passed else "FAIL", "ok": passed == want_pass})
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return rows


def cell_e():
    r = subprocess.run([sys.executable, str(CELL_E_SCORER), "--calibrate"],
                       capture_output=True, text=True, timeout=600)
    return {"case": "cell E oracle --calibrate", "ok": r.returncode == 0,
            "tail": (r.stdout.strip().splitlines() or [""])[-1]}


def main():
    rows = debug_cell() + [cell_e()]
    if "--json" in sys.argv:
        print(json.dumps(rows, indent=1))
    for r in rows:
        tag = "ok  " if r["ok"] else "FAIL"
        extra = f"want {r['want']} got {r['got']}" if "want" in r else (r.get("why") or r.get("tail") or "")
        print(f"  {tag} {r['case']:<46s} {extra}")
    good = all(r["ok"] for r in rows)
    print("CALIBRATION PASSED -- v12 may dispatch" if good else "CALIBRATION FAILED -- do not run")
    return 0 if good else 1


if __name__ == "__main__":
    sys.exit(main())
