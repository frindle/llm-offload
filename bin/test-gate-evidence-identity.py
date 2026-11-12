#!/usr/bin/env python3
"""Behavioural test: gate-on-complete's success path needs MACHINE proof and a
CURRENT verdict (2026-10-06 gate audit).

  1. classify_advance: a clean PASS with no both-ways proof / unproven relevance
     on an auto-deploy repo must NOT auto-apply (downgrade to 'ready', gaps named);
     a fully machine-proven, stamped PASS still auto-applies.
  2. measure_relevance carries verify-relevance's `optout_rejected` into the gate
     record (it used to be dropped by the fixed key list).
  3. apply_fix (live auto-apply) refuses a STALE verdict -- the worktree file
     changed after the gate judged it -- and an UNSTAMPED one; main is untouched.
     A current stamp still lands.
  4. stamp_judged_identity names the product files (not scaffold) by hash.

Hermetic: temp git repos only; the queue escalate call is stubbed; no enqueue.

Usage: python3 test-gate-evidence-identity.py [path/to/gate-on-complete.py]
(pass the .bak to prove the test bites: it must FAIL against the pre-fix file).
"""
import importlib.machinery
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

target = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("gate-on-complete.py"))
os.environ.pop("GATE_TEST_MODE", None)
_ld = importlib.machinery.SourceFileLoader("goc_under_test", str(target))
spec = importlib.util.spec_from_loader("goc_under_test", _ld)
g = importlib.util.module_from_spec(spec)
# gate_identity.py is resolved next to __file__; a .bak lives in the same dir.
_ld.exec_module(g)
g.TEST_MODE = False

ok = True


def check(name, fn, want):
    global ok
    try:
        got = fn()
    except Exception as e:
        got = f"<raised {type(e).__name__}: {e}>"
    good = got == want
    ok &= good
    print(("PASS " if good else "FAIL ") + name + ("" if good else f": got {got!r} want {want!r}"))


# ---- 1. classify_advance ------------------------------------------------------
UNPROVEN = {"verdict": "pass", "issues": [], "review_verdict": "PASS",
            "verify_relevance": {"verdict": "unproven", "survivors": []}}
PROVEN = {"verdict": "pass", "issues": [], "review_verdict": "PASS",
          "verify_failed_at_baseline": True,
          "verify_relevance": {"verdict": "relevant", "survivors": [], "killed": 4},
          "judged": {"files": {"src/a.ts": "h"}, "toplevel": "/x"}}
check("unproven clean PASS on auto-deploy repo -> ready, not auto-apply",
      lambda: g.classify_advance(UNPROVEN, "rt-x", "resell-tracker")["policy"], "ready")
check("unproven PASS names its evidence gaps",
      lambda: any("both-ways" in x for x in
                  g.classify_advance(UNPROVEN, "rt-x", "resell-tracker")["evidence_gaps"]), True)
check("machine-proven stamped PASS still auto-applies",
      lambda: g.classify_advance(PROVEN, "rt-x", "resell-tracker")["policy"], "auto-apply")

# ---- 2. measure_relevance keeps optout_rejected ---------------------------------
td = Path(tempfile.mkdtemp(prefix="gate-evid-"))
_fake = {"verdict": "relevant", "score": 1.0, "killed": 3, "survived": 0, "survivors": [],
         "optout_rejected": [{"file": "a.py", "line": 7, "why": "no reason given"}]}


class _R:
    returncode, stdout, stderr = 0, json.dumps(_fake), ""


def _measure():
    real_run = g.subprocess.run
    saved = (g._tree_lock_acquire, g._tree_lock_release, g._locked_test_target_fixture)
    g._tree_lock_acquire = lambda *a, **k: None
    g._tree_lock_release = lambda *a, **k: None
    g._locked_test_target_fixture = lambda *a, **k: False
    g.subprocess.run = lambda *a, **k: _R()
    try:
        p = {}
        g.measure_relevance(p, td, "bash verify.sh")
        return (p.get("verify_relevance") or {}).get("optout_rejected")
    finally:
        g.subprocess.run = real_run
        g._tree_lock_acquire, g._tree_lock_release, g._locked_test_target_fixture = saved


check("measure_relevance carries optout_rejected", _measure, _fake["optout_rejected"])

# ---- 3. apply_fix refuses stale / unstamped verdicts -----------------------------
env = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
       "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}


def git(*a, cwd):
    return subprocess.run(["git", *a], cwd=str(cwd), capture_output=True, text=True, env=env)


repo = td / "origin"; repo.mkdir()
git("init", "-q", cwd=repo)
(repo / "app").mkdir(); (repo / "app" / "m.py").write_text("ORIGINAL\n")
git("add", "-A", cwd=repo); git("commit", "-qm", "i", cwd=repo)
wt = td / "wt"
git("worktree", "add", "-q", str(wt), cwd=repo)
logs = td / "logs"; logs.mkdir()
esc = []
g.autofix_build_escalate = lambda *a, **k: (esc.append(a[0]) or ["true"])
g.AUTO_PIPELINE_MODE = "live"


def sha(p):
    import hashlib
    return hashlib.sha256(p.read_bytes()).hexdigest()


def run_apply(jid, payload):
    gj = logs / f"{jid}.gate.json"
    g.apply_fix(jid, payload, gj, "auto-apply")
    return json.loads(gj.read_text()).get("auto_pipeline_status"), (repo / "app" / "m.py").read_text()


(wt / "app" / "m.py").write_text("JUDGED\n")
stamp = {"files": {"app/m.py": sha(wt / "app" / "m.py")}, "toplevel": str(wt)}
(wt / "app" / "m.py").write_text("EDITED-AFTER-THE-GATE\n")
check("STALE verdict (file edited after gate) -> parked, main untouched",
      lambda: run_apply("stale1", {"changed_files": ["app/m.py"], "cwd": str(wt), "judged": stamp}),
      ("apply-failed", "ORIGINAL\n"))
check("UNSTAMPED verdict -> parked, main untouched",
      lambda: run_apply("nostamp1", {"changed_files": ["app/m.py"], "cwd": str(wt)}),
      ("apply-failed", "ORIGINAL\n"))
(wt / "app" / "m.py").write_text("JUDGED\n")
check("CURRENT stamp -> applied to main",
      lambda: run_apply("cur1", {"changed_files": ["app/m.py"], "cwd": str(wt), "judged": stamp}),
      ("applied-to-main", "JUDGED\n"))

# ---- 4. the stamp itself ----------------------------------------------------------
d = td / "j.diff"
d.write_text("--- a/app/m.py\n+++ b/app/m.py\n@@ -1 +1 @@\n-ORIGINAL\n+JUDGED\n"
             "--- a/TASK.md\n+++ b/TASK.md\n@@ -1 +1 @@\n-a\n+b\n")


def _stamp():
    p = {}
    g.stamp_judged_identity(p, "j9", wt, d, base="deadbeef")
    j = p.get("judged") or {}
    return (j.get("job_id"), j.get("base"), sorted(j.get("files") or {}),
            (j.get("files") or {}).get("app/m.py") == sha(wt / "app" / "m.py"))


check("stamp names job/base and hashes product files only", _stamp,
      ("j9", "deadbeef", ["app/m.py"], True))

print("ALL PASS" if ok else "SOME FAILED")
sys.exit(0 if ok else 1)
