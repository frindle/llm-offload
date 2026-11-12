#!/usr/bin/env python3
"""Phase-2 (d): mechanical auto-resolution of green-at-baseline slices (2026-10-03).

park_if_already_satisfied used to send the FIRST green-at-baseline slice of every plan
to a human. baseline_green_discriminates() now swaps the declared target back to its
content at the chain base and re-runs verify.sh:
  * RED at base, GREEN at tip     -> SKIPPED, evidence recorded       (auto-resolved)
  * GREEN at base (leaky fixture) -> ESCALATED as before              (REVERT-CHECK)
  * target unchanged base..tip    -> ESCALATED as before              (REVERT-CHECK)
  * creation target absent at base-> SKIPPED when the fixture discriminates
  * the target's bytes (or absence) are restored EXACTLY in every case
  * a SUSPECT baseline-green claim (verify actually red) still escalates as a pipeline bug
  * a discriminator crash falls through to the human escalation

Real git repo + worktree in a temp dir; save_state is stubbed (no live state touched).
Usage: python3 ~/bin/test-baseline-green-discriminate.py
Revert: SLICE=~/bin/ollama-dispatch-slice.bak-autoresolve python3 this.py -> FAIL
"""
import importlib.util
import os
import shutil
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE", HERE / "ollama-dispatch-slice"))
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


loader = SourceFileLoader("slice_d", str(SLICE))
spec = importlib.util.spec_from_loader("slice_d", loader)
sl = importlib.util.module_from_spec(spec)
loader.exec_module(sl)
sl.save_state = lambda st, *a, **k: None

ROOT = Path(tempfile.mkdtemp(prefix="bgdisc-"))
ENV = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
           GIT_COMMITTER_EMAIL="t@t", GIT_CONFIG_GLOBAL="/dev/null")


def g(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], env=ENV, check=True,
                          capture_output=True, text=True).stdout.strip()


def make(name, base_files, chain_files, verify, target):
    """repo (main = base_files) + worktree on chain branch (one commit = chain_files)."""
    repo = ROOT / name / "repo"
    repo.mkdir(parents=True)
    g(repo, "init", "-q", "-b", "main")
    for f, c in base_files.items():
        (repo / f).write_text(c)
    (repo / "README").write_text("r\n")
    g(repo, "add", "-A")
    g(repo, "commit", "-qm", "base")
    wt = ROOT / name / "wt"
    g(repo, "worktree", "add", "-q", "-b", "chain", str(wt))
    for f, c in chain_files.items():
        (wt / f).write_text(c)
    g(wt, "add", "-A")
    g(wt, "commit", "-qm", "earlier slice")
    (wt / "verify.sh").write_text(verify)
    (wt / ".dispatch-harness.json").write_text('{"target": "%s"}' % target)
    return {"repo": str(repo), "label": name, "chain_branch": "chain"}, str(wt)


def park(st, wt):
    s = {"status": "pending"}
    try:
        sl.park_if_already_satisfied(st, "sX", s, wt, "preflight")
    except Exception as e:
        s["status"] = "RAISED %r" % (e,)
    return s


DISC = "grep -q 'x = 1' t.py && echo VERIFY_OK\n"
LEAKY = "echo VERIFY_OK\n"

# 1. discriminating fixture, target changed by an earlier slice -> auto-skip
st, wt = make("disc", {"t.py": "x = 0\n"}, {"t.py": "x = 1\n"}, DISC, "t.py")
tip_bytes = (Path(wt) / "t.py").read_bytes()
s = park(st, wt)
chk("red-at-base + green-at-tip -> SKIPPED (auto-resolved)", s["status"], sl.SKIPPED)
chk("...evidence recorded on the slice",
    ((s.get("skip_evidence") or {}).get("base_verify_ok"),
     (s.get("skip_evidence") or {}).get("restored_exactly")), (False, True))
chk("...target bytes restored exactly", (Path(wt) / "t.py").read_bytes(), tip_bytes)
chk("...worktree left clean (only the untracked harness files)",
    sorted(l[3:] for l in g(wt, "status", "--porcelain").splitlines()),
    [".dispatch-harness.json", "verify.sh"])

# 2. leaky fixture -> green at base too -> human, unchanged
st, wt = make("leaky", {"t.py": "x = 0\n"}, {"t.py": "x = 1\n"}, LEAKY, "t.py")
s = park(st, wt)
chk("REVERT: leaky fixture (green at base) still ESCALATED", s["status"], sl.ESCALATED)
chk("...reason names the failed mechanical check",
    "could not tell them apart" in (s.get("escalation_reason") or "") and
    "ALSO green" in (s.get("escalation_reason") or ""), True)
chk("...target restored", (Path(wt) / "t.py").read_text(), "x = 1\n")

# 3. earlier slice never touched the target -> no discrimination possible
st, wt = make("untouched", {"t.py": "x = 1\n"}, {"other.py": "y\n"}, DISC, "t.py")
s = park(st, wt)
chk("REVERT: target unchanged base..tip -> still ESCALATED", s["status"], sl.ESCALATED)
chk("...reason says identical at base and tip", "identical" in (s.get("escalation_reason") or ""), True)

# 6. (2026-10-06, rt-egift-link-s1 s1/s4) the SLICE worktree branches off the chain and
#    its own "seal" commit carries the model's draft of the target. Verify is red at the
#    chain base and green at the slice HEAD -- but the chain itself never got the change.
#    Must NOT be skipped as "satisfied by an earlier slice".
st, cwt = make("sealdraft", {"t.py": "x = 0\n"}, {"other.py": "y\n"}, DISC, "t.py")
swt = ROOT / "sealdraft" / "slicewt"
g(Path(st["repo"]), "worktree", "add", "-q", "-b", "dispatch/sX", str(swt), "chain")
(swt / "t.py").write_text("x = 1\n")
(swt / "verify.sh").write_text(DISC)
(swt / ".dispatch-harness.json").write_text('{"target": "t.py"}')
g(swt, "add", "t.py")
g(swt, "commit", "-qm", "seal dispatch harness (clean launch baseline)")
s = park(st, str(swt))
chk("sealed slice draft (chain never got it) -> NOT skipped", s["status"], sl.ESCALATED)
chk("...reason says the target is not the chain tip's copy",
    "NOT the chain tip's copy" in (s.get("escalation_reason") or ""), True)
chk("...slice draft restored", (swt / "t.py").read_text(), "x = 1\n")
# 6b. same shape but the chain DID land it (slice wt target == chain tip) -> skipped
st, cwt = make("chainland", {"t.py": "x = 0\n"}, {"t.py": "x = 1\n"}, DISC, "t.py")
swt = ROOT / "chainland" / "slicewt"
g(Path(st["repo"]), "worktree", "add", "-q", "-b", "dispatch/sY", str(swt), "chain")
(swt / "verify.sh").write_text(DISC)
(swt / ".dispatch-harness.json").write_text('{"target": "t.py"}')
s = park(st, str(swt))
chk("slice wt off a chain that landed the target -> SKIPPED", s["status"], sl.SKIPPED)

# 4. creation target absent at base
st, wt = make("create", {}, {"new.py": "x = 1\n"}, "test -f new.py && echo VERIFY_OK\n", "new.py")
s = park(st, wt)
chk("creation target absent at base + discriminating -> SKIPPED", s["status"], sl.SKIPPED)
chk("...absent-at-base recorded", (s.get("skip_evidence") or {}).get("target_absent_at_base"), True)
chk("...created file restored after the run", (Path(wt) / "new.py").read_text(), "x = 1\n")

# 5. suspect claim: verify is actually RED now -> pipeline bug, never skipped
st, wt = make("suspect", {"t.py": "x = 0\n"}, {"t.py": "x = 1\n"}, "exit 1\n", "t.py")
s = park(st, wt)
chk("REVERT: a false baseline-green claim still escalates as PIPELINE BUG",
    (s["status"], (s.get("escalation_reason") or "").startswith("PIPELINE BUG")), (sl.ESCALATED, True))

# 6. discriminator crash -> human escalation, never a skip
st, wt = make("crash", {"t.py": "x = 0\n"}, {"t.py": "x = 1\n"}, DISC, "t.py")
_orig = getattr(sl, "baseline_green_discriminates", None)
if _orig:
    sl.baseline_green_discriminates = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
s = park(st, wt)
if _orig:
    sl.baseline_green_discriminates = _orig
chk("REVERT: discriminator crash -> ESCALATED, not skipped", s["status"], sl.ESCALATED)

shutil.rmtree(ROOT, ignore_errors=True)
print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
