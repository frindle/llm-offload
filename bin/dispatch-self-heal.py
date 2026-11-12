#!/usr/bin/env python3
"""Act on an escalation watcher diagnosis for a HARNESS-side slice escalation.

THE GAP THIS CLOSES (the owner 2026-09-26: "I shouldn't be having to manually override
things to get my queue to do what I want")
-----------------------------------------------------------------------------
dispatch-escalation-watcher.py detects an ESCALATED slice, has a headless reviewer
diagnose it, and writes a `git apply --check`-validated diff beside the escalation.
It is deliberately read-only, so the fix then waited for a human. On 2026-09-26 the
review for sidecar-bfmr-login-fetch s1 named the right fix ("the fixture never
asserts isLoggedOut's dead-context return value") and the chain still sat parked
until the coordinator applied it by hand and ran --regate.

This tool is the ACT half, and it is kept narrow on purpose:

  * HARNESS ONLY. It applies a proposed diff only when every file the diff touches
    is one of the slice's harness files that may carry the proof: the fixture(s)
    named in the scaffold manifest (.dispatch-harness.json "authored") and
    refimpl.py. The target, TASK.md (the spec), verify.sh, check_literals.py (the
    frozen literals) and anything else are refused outright. It never edits
    product code.
  * THE GATE STILL DECIDES. After a patch it runs `ollama-dispatch-slice <plan>
    --regate <sid>`, which re-seals the tree and runs preflight both-ways +
    verify-relevance. A harness that is still not GO simply re-ESCALATES.
  * BOUNDED. At most MAX_ATTEMPTS self-heal actions per plan/slice, ever
    (self-heal.json ledger). After that the escalation stays parked for a human.
  * LOGGED. Every decision, including a refusal, is appended to the escalation's
    context file and to the ledger.

Actions by escalation class:
  relevance / preflight NO-GO   -> apply the validated harness-only diff, --regate
  authoring made no progress /
  deterministic authoring fail  -> --retry-slice (re-author clean)
  converged harness, only blocker
  verify-relevance (verdict c)  -> ONE in-place repair job in the KEPT worktree, then
                                   --regate (harness_repair_sweep); see IN-PLACE HARNESS REPAIR
  anything else                 -> nothing (a human decision: gate FAIL on the
                                   coding job, integration, baseline-green audit ...)

    dispatch-self-heal.py --plan-label L --slice S --context CTX [--review R]
    dispatch-self-heal.py --self-test
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
# iCloud "Optimize Storage" evicts the Desktop repos' .git metadata; without this a
# git child gets EDEADLK ("not a git repository: (null)") -- see dataless_policy.py.
try:
    import importlib.util as _dp_iu, os as _dp_os
    _dp_s = _dp_iu.spec_from_file_location("dataless_policy", _dp_os.path.join(
        _dp_os.path.dirname(_dp_os.path.realpath(__file__)), "dataless_policy.py"))
    _dp_m = _dp_iu.module_from_spec(_dp_s)
    _dp_s.loader.exec_module(_dp_m)
    _dp_m.enable()
except Exception:
    pass
import tempfile
import time
from pathlib import Path

DISPATCH_DIR = Path.home() / ".ollama-dispatch"
SLICE_RUNS = DISPATCH_DIR / "slice-runs"
ESC_DIR = DISPATCH_DIR / "escalations"
HEAL_LEDGER = ESC_DIR / "self-heal.json"
HAND_LOCK = ".hand-harness"   # mirrors ollama-dispatch-slice HAND_LOCK
MAX_ATTEMPTS = 2

# Harness files a self-heal may NEVER touch even though the scaffold wrote them:
# the spec, the verify contract and the frozen literals are the gate's own inputs.
PROTECTED_HARNESS = {"TASK.md", "verify.sh", "check_literals.py", "Package.swift",
                     "REPO_MAP.md", "fix.patch", ".dispatch-harness.json",
                     "auto-harness-check.py",
                     # C# harness project/build isolation: the verify contract, not proof
                     "DispatchTests.csproj", "Directory.Build.props",
                     "Directory.Build.targets", "Directory.Packages.props", ".gitignore"}
FALLBACK_FIXTURES = ("test_fixture.py", "verify.test.ts", "verify.test.mts",
                     "Tests/verifyTests.swift", "DispatchTests/VerifyImpl.cs")

# A stale base (the chain tip moved past the tree this slice was seeded from) is
# MECHANICAL: --retry-slice re-seeds the worktree off the current chain tip, which
# is exactly the prescribed fix. It used to fall to 'none' and park the bundle.
_STALE_BASE = re.compile(r"stale[- ]base|re-seed the worktree|moved \d+ commit\(s\) past the launch", re.I)
_RELEVANCE = re.compile(r"relevance|preflight\)? NO-GO|NO-GO", re.I)
_AUTHORING = re.compile(
    r"authoring made no progress|authoring failed DETERMINISTICALLY|"
    r"authoring has now been launched", re.I)
_PATCH_RE = re.compile(r"```(?:diff|patch)\s*\n(.*?)```", re.S | re.I)


# ----------------------------------------------------------------------------
# pure decisions
# ----------------------------------------------------------------------------
def classify(reason: str) -> str:
    """PURE. 'regate' | 'retry' | 'none' for a slice escalation_reason. Authoring
    is checked first: an authoring streak whose signature quotes a relevance
    line is still an authoring failure (there is no harness to patch)."""
    r = reason or ""
    if _AUTHORING.search(r) or _STALE_BASE.search(r):
        return "retry"
    if "PIPELINE BUG SUSPECTED" in r or "already satisfied" in r.lower():
        return "none"
    if _RELEVANCE.search(r):
        return "regate"
    return "none"


def allowed_files(manifest: dict | None, target: str | None) -> set:
    """PURE. The worktree-relative paths a self-heal diff may touch."""
    authored = (manifest or {}).get("authored") or list(FALLBACK_FIXTURES) + ["refimpl.py"]
    out = {p for p in authored if Path(p).name not in PROTECTED_HARNESS}
    out.add("refimpl.py")
    if target:
        out.discard(target)
    return out


def patch_files(patch: str) -> list:
    """PURE. Every path a unified diff touches (both sides, a/ b/ stripped)."""
    files = []
    for line in (patch or "").splitlines():
        m = re.match(r"^(?:---|\+\+\+) (?:[ab]/)?(\S+)", line)
        if m and m.group(1) != "/dev/null":
            files.append(m.group(1))
        m = re.match(r"^diff --git a/(\S+) b/(\S+)", line)
        if m:
            files += [m.group(1), m.group(2)]
        m = re.match(r"^(?:rename|copy) (?:from|to) (\S+)", line)
        if m:
            files.append(m.group(1))
    seen, out = set(), []
    for f in files:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def refused_files(patch: str, allowed: set) -> list:
    """PURE. Paths the diff touches that a self-heal may not write."""
    return [f for f in patch_files(patch) if f not in allowed]


def extract_patch(review: str):
    """The LONGEST fenced diff block that looks like a real unified diff (same
    rule as the watcher's extract_patch), or None."""
    best = None
    for m in _PATCH_RE.finditer(review or ""):
        body = m.group(1)
        if not ("\n@@" in "\n" + body and (("--- " in body and "+++ " in body)
                                          or "diff --git " in body)):
            continue
        if best is None or len(body) > len(best):
            best = body
    if best is None:
        return None
    return best if best.endswith("\n") else best + "\n"


# ----------------------------------------------------------------------------
# ledger / log
# ----------------------------------------------------------------------------
def _load(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def heal_key(plan, sid):
    return f"{plan}/{sid}"


def attempts_used(ledger, plan, sid):
    return int(((ledger or {}).get(heal_key(plan, sid)) or {}).get("attempts") or 0)


def record(plan, sid, entry, ctx=None, count=False, ledger_path=None):
    """Append one self-heal decision to the ledger (and bump the attempt count
    when an ACTION was taken) and to the escalation's context file."""
    lp = Path(ledger_path or HEAL_LEDGER)
    led = _load(lp) or {}
    rec = led.setdefault(heal_key(plan, sid), {"attempts": 0, "log": []})
    if count:
        rec["attempts"] = int(rec.get("attempts") or 0) + 1
    entry = dict(entry, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                 attempt=rec["attempts"])
    rec["log"].append(entry)
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        tmp = lp.with_suffix(".tmp")
        tmp.write_text(json.dumps(led, indent=1))
        tmp.replace(lp)
    except Exception:
        pass
    if ctx:
        try:
            with open(ctx, "a") as fh:
                fh.write("\n## Self-heal (%s, attempt %d/%d)\n\n- action: %s\n- detail: %s\n"
                         % (entry["at"], rec["attempts"], MAX_ATTEMPTS,
                            entry.get("action"), entry.get("detail")))
        except Exception:
            pass
    print("# self-heal %s/%s: %s -- %s" % (plan, sid, entry.get("action"), entry.get("detail")))
    return rec["attempts"]


# ----------------------------------------------------------------------------
# verified auto-skip (the owner 2026-10-02: "when our own gates decide a slice is
# already satisfied, the retirement must happen automatically, not by hand")
# ----------------------------------------------------------------------------
# A review that says VERDICT (a) / already satisfied is a CLAIM, never trusted as
# prose. It is acted on only after a deterministic proof at the chain tip:
#   1. the chain worktree is CLEAN (nothing uncommitted rides along);
#   2. the slice's own frozen harness (verify.sh + fixture + frozen literals),
#      copied into a throwaway detached worktree AT THE TIP, PASSES (VERIFY_OK);
#   3. the same verify FAILS with the target reverted to the chain BASE (the
#      merge-base with the repo's HEAD) -- i.e. it still catches the property
#      being absent, so "green" is not a leaky fixture that passes on anything.
# Only then: --skip-slice with a reason naming the tip sha and the review.
_SATISFIED_RE = re.compile(
    r"VERDICT:?\s*\(?a\)?(?:\s|$|[^a-z])|already\s+satisfied", re.I)
_NOISE = ("node_modules", ".tsbuildinfo", ".refine-guard.json")


def review_says_satisfied(review: str) -> bool:
    """PURE. The review's VERDICT line claims (a) / already satisfied. A capped /
    no-evidence review (escalation_verdict.NO_VERDICT_MARKERS) claims nothing."""
    if _ev().is_no_verdict_review(review):
        return False
    for line in (review or "").splitlines():
        if line.strip().upper().startswith("VERDICT"):
            return bool(_SATISFIED_RE.search(line))
    return False


def _git(cwd, *args, timeout=60):
    r = subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True,
                       timeout=timeout)
    return r.returncode, (r.stdout or "").strip(), (r.stderr or "").strip()


def _harness_paths(wt, target):
    man = _load(Path(wt) / ".dispatch-harness.json") or {}
    names = ["verify.sh", "check_literals.py", "TASK.md"] + list(man.get("authored") or []) \
        + list(FALLBACK_FIXTURES) + ["verify_impl.mts", "verify_impl.mjs", "verify_impl.js"]
    out = []
    for n in names:
        if n and n != target and n != "refimpl.py" and (Path(wt) / n).is_file() and n not in out:
            out.append(n)
    return out


def _run_verify(tree, timeout=900):
    try:
        r = subprocess.run(["bash", "verify.sh"], cwd=str(tree), capture_output=True,
                           text=True, timeout=timeout,
                           env={**os.environ, "DISPATCH_VERIFY_SANDBOX": "1"})  # VERIFY-SANDBOX
    except subprocess.TimeoutExpired:
        return False, "verify timed out"
    out = (r.stdout or "") + (r.stderr or "")
    return r.returncode == 0 and "VERIFY_OK" in out, out[-400:]


def verify_satisfied_at_tip(st, s, runner=None):
    """Deterministic proof that slice `s` is already satisfied at the chain tip.
    Returns (ok, detail, tip_sha). Never raises. `runner(tree)->(ok, out)` is
    injectable for tests (default: bash verify.sh)."""
    runner = runner or _run_verify
    chain = st.get("chain_worktree")
    swt = s.get("worktree")
    target = s.get("target") or st.get("target")
    if not chain or not Path(chain).is_dir():
        return False, "chain worktree missing", None
    if not swt or not Path(swt).is_dir() or not (Path(swt) / "verify.sh").is_file():
        return False, "slice harness (verify.sh) missing -- nothing frozen to prove with", None
    rc, tip, _ = _git(chain, "rev-parse", "HEAD")
    if rc:
        return False, "cannot read chain tip", None
    rc, dirty, _ = _git(chain, "status", "--porcelain")
    dirty = [l for l in dirty.splitlines() if l.strip() and not any(n in l for n in _NOISE)]
    if dirty:
        return False, "chain worktree is DIRTY (%s)" % ", ".join(dirty[:3]), tip
    repo = st.get("repo") or chain
    rc, rhead, _ = _git(repo, "rev-parse", "HEAD")
    rc2, base, _ = _git(chain, "merge-base", tip, rhead) if not rc else (1, "", "")
    if rc or rc2 or not base:
        return False, "cannot find the chain base (merge-base with the repo HEAD)", tip
    if base == tip:
        return False, "chain tip == base: nothing landed that could satisfy it", tip
    tmp = Path(tempfile.mkdtemp(prefix="autoskip-"))
    tree = tmp / "tip"
    try:
        # The tip tree is EXTRACTED (`git archive <tip> | tar -x`), not a `git worktree
        # add`. Root cause of the Rivian s1 124 timeout (2026-10-02, measured): the
        # worktree add took 106s of a 160s proof -- not the checkout (the archive of
        # the same 98-file tree takes 0.02s) but git's .git/worktrees + reflog
        # bookkeeping, which blocks on iCloud-evicted (dataless) metadata files in
        # the Desktop repo's git dir, and it ran against a 120s timeout. archive only
        # reads objects, registers nothing, and leaves nothing to clean up.
        tree.mkdir(parents=True)
        ok_x, why_x = _extract_tree(chain, tip, tree)
        if not ok_x:
            return False, "could not extract the tip tree: %s" % why_x, tip
        for n in _harness_paths(swt, target):
            dst = tree / n
            dst.parent.mkdir(parents=True, exist_ok=True)
            dst.write_bytes((Path(swt) / n).read_bytes())
        # Warm tsc: a fresh tree has no incremental cache, so `tsc --noEmit` ran cold
        # (35s vs 2s in the slice worktree). The cache is keyed by content hashes, so
        # a copied one can only skip files that are byte-identical.
        for cache in ("tsconfig.tsbuildinfo",):
            src = Path(swt) / cache
            if src.is_file() and not (tree / cache).exists():
                (tree / cache).write_bytes(src.read_bytes())
        nm = Path(swt) / "node_modules"
        if nm.exists() and not (tree / "node_modules").exists():
            (tree / "node_modules").symlink_to(nm.resolve())
        ok, out = runner(tree)
        if not ok:
            return False, "the slice's frozen verify does NOT pass at tip %s: %s" % (
                tip[:12], out.strip().splitlines()[-1:] or ""), tip
        if target:
            ok_r, why_r = _file_at(chain, base, target, tree / target)
            if not ok_r:
                return False, "could not revert %s to the chain base: %s" % (target, why_r), tip
        ok2, out2 = runner(tree)
        if ok2:
            return False, ("the verify ALSO passes with %s reverted to the chain base %s -- "
                           "it does not discriminate (leaky fixture), so green proves "
                           "nothing" % (target, base[:12])), tip
        return True, ("verify passes at tip %s and fails with %s at chain base %s"
                      % (tip[:12], target, base[:12])), tip
    except Exception as exc:
        return False, "verification error: %s" % exc, None
    finally:
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)


def _extract_tree(repo, rev, dest, timeout=300):
    """`git archive rev` to a temp file, then `tar -xf` it. (ok, why). Reads objects only.

    NOT a pipe (2026-10-02, test-auto-skip.py 8 failures): macOS bsdtar stops reading
    at the end-of-archive marker, before git has written the tar's trailing record
    padding, so git died of SIGPIPE (rc -13, empty stderr) while tar had extracted
    the full tree with rc 0 -- every skip proof was refused as "could not extract the
    tip tree: " (timing-dependent, so flaky). Through a file both return codes mean
    what they say."""
    fd, arch = tempfile.mkstemp(prefix="selfheal-tip-", suffix=".tar")
    try:
        import os as _os
        _os.close(fd)
        g = subprocess.run(["git", "-C", str(repo), "archive", "--format=tar",
                            "--output", arch, rev], capture_output=True, timeout=timeout)
        if g.returncode:
            return False, ("git archive rc=%s: %s" % (
                g.returncode, g.stderr.decode(errors="replace").strip()))[-160:]
        t = subprocess.run(["tar", "-x", "-f", arch, "-C", str(dest)],
                           capture_output=True, timeout=timeout)
        if t.returncode:
            return False, ("tar rc=%s: %s" % (
                t.returncode, t.stderr.decode(errors="replace").strip()))[-160:]
    except Exception as exc:
        return False, str(exc)
    finally:
        try:
            Path(arch).unlink()
        except OSError:
            pass
    return True, ""


def _file_at(repo, rev, path, dest, timeout=120):
    """Write `rev:path` to dest; a path absent at rev is REMOVED (the target did not
    exist at the chain base). (ok, why)."""
    rc, _o, _e = _git(repo, "cat-file", "-e", "%s:%s" % (rev, path), timeout=timeout)
    if rc:
        try:
            Path(dest).unlink()
        except FileNotFoundError:
            pass
        return True, ""
    r = subprocess.run(["git", "-C", str(repo), "show", "%s:%s" % (rev, path)],
                       capture_output=True, timeout=timeout)
    if r.returncode:
        return False, r.stderr.decode(errors="replace")[-120:]
    Path(dest).write_bytes(r.stdout)
    return True, ""


def slice_job_labels(plan, sid):
    """PURE. The base label every queue row of this slice derives from."""
    return f"{plan}-{sid}"


def label_base(label):
    """PURE. Strip auto-author-/auto-refine-/gate-/regate-/secondop- and trailing
    -rN/-cN/-esc decorations."""
    b = re.sub(r"^(?:needs-opus-auto-|auto-(?:author|refine)-|gate-|regate-|secondop-)", "", str(label or ""))
    prev = None
    while prev != b:
        prev = b
        b = re.sub(r"-(?:r\d+|c\d+|esc|rs\d+)$", "", b)
    return b


def plan_slice_for_label(label, slice_runs=None):
    """(plan, sid) for a queue label that belongs to a slice plan, else (None, None)."""
    base = label_base(label)
    d = Path(slice_runs or SLICE_RUNS)
    for f in sorted(d.glob("*.json"), key=lambda p: -len(p.stem)):
        plan = f.stem
        if not base.startswith(plan + "-"):
            continue
        st = _load(f) or {}
        sid = base[len(plan) + 1:]
        if sid in (st.get("slices") or {}):
            return plan, sid
    return None, None


def _queue_jobs(queue_state=None):
    st = _load(queue_state or (Path.home() / "bin" / "ollama-queue-state.json")) or {}
    return st.get("jobs") or []


def retire_rows(plan, sid, why, jobs=None, resolve=None):
    """Resolve needs_opus / escalated / failed queue rows of a slice that is now
    retired (skipped, or passed clean later). Never touches awaiting-signoff,
    running or pending rows. Returns the ids resolved."""
    resolve = resolve or (lambda jid: subprocess.run(
        [sys.executable, str(Path(__file__).resolve().parent / "ollama-queue.py"),
         "resolve", jid], capture_output=True, text=True, timeout=60).returncode == 0)
    want = slice_job_labels(plan, sid)
    out = []
    for j in (jobs if jobs is not None else _queue_jobs()):
        if j.get("status") not in ("needs_opus", "escalated", "failed", "blocked"):
            continue
        if j.get("awaiting_signoff"):
            continue
        if label_base(j.get("label")) != want:
            continue
        if resolve(j.get("id")):
            out.append(j.get("id"))
    if out:
        print("# self-heal %s/%s: retired %d superseded row(s) %s -- %s"
              % (plan, sid, len(out), out, why))
    return out


def retire_sweep(slice_runs=None, jobs=None, resolve=None):
    """Every needs_opus/escalated/failed row whose slice is now DONE or SKIPPED is
    superseded -> resolve it (extends the janitor's failed-only supersede)."""
    jobs = jobs if jobs is not None else _queue_jobs()
    done = []
    for j in jobs:
        if j.get("status") not in ("needs_opus", "escalated", "failed", "blocked"):
            continue
        plan, sid = plan_slice_for_label(j.get("label"), slice_runs)
        if not plan:
            continue
        st = _load(Path(slice_runs or SLICE_RUNS) / f"{plan}.json") or {}
        if ((st.get("slices") or {}).get(sid) or {}).get("status") in ("done", "skipped"):
            done += retire_rows(plan, sid, "slice is %s" % st["slices"][sid]["status"],
                                jobs=[j], resolve=resolve)
    return done


def verified_skip(plan, sid, review_text, review_path=None, ctx=None, slice_runs=None,
                  ledger_path=None, launch=None, verifier=None, resolve=None, jobs=None):
    """Act on a review's 'already satisfied' claim ONLY after the deterministic
    proof. Returns 'skip-verified:<sha12>' or 'skip:not verified (<why>)'."""
    launch = launch or _slicer_sync
    st = _load(Path(slice_runs or SLICE_RUNS) / f"{plan}.json") or {}
    s = (st.get("slices") or {}).get(sid) or {}
    if s.get("status") in ("done", "skipped"):
        return "skip:slice already %s" % s.get("status")
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import plan_cancel
        if plan_cancel.cancelled(plan, runs_dir=slice_runs or SLICE_RUNS):
            return "skip:cancelled"
    except Exception:
        pass
    ok, detail, tip = (verifier or verify_satisfied_at_tip)(st, s)
    if not ok:
        record(plan, sid, {"action": "skip-refused", "detail": "review claims already "
                           "satisfied, but the deterministic check FAILED: " + detail},
               ctx, ledger_path=ledger_path)
        return "skip:not verified (%s)" % detail[:160]
    plan_path = st.get("plan_path")
    if not plan_path or not Path(plan_path).is_file():
        return "skip:no plan file"
    reason = "auto: verify passed at %s (%s), review %s" % (
        (tip or "?")[:12], detail, review_path or "?")
    rc = launch(plan_path, ["--skip-slice", sid, "--reason", reason],
                Path(slice_runs or SLICE_RUNS) / f"{plan}.advance.log")
    if rc not in (0, None):
        record(plan, sid, {"action": "skip-failed", "detail": "--skip-slice rc=%s" % rc},
               ctx, ledger_path=ledger_path)
        return "skip:--skip-slice failed rc=%s" % rc
    record(plan, sid, {"action": "skip-verified", "detail": reason}, ctx, count=True,
           ledger_path=ledger_path)
    retire_rows(plan, sid, "slice auto-skipped (verified)", jobs=jobs, resolve=resolve)
    return "skip-verified:%s" % (tip or "?")[:12]


_RETIRED_MSG = "old slicer retired -- nothing launched (use mini-swe)"


def _slicer_sync(plan_path, args, log_path):
    """Retired: the old slicer is gone. Logs a note, launches nothing, returns rc 1."""
    try:
        with open(log_path, "a") as lf:
            lf.write("\n===== self-heal %s %s: %s =====\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), " ".join(args), _RETIRED_MSG))
    except OSError:
        pass
    return 1


# ----------------------------------------------------------------------------
# act
# ----------------------------------------------------------------------------
def _git_apply(wt, patch, check_only):
    with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
        fh.write(patch)
        tmp = fh.name
    try:
        # --recount: model-written diffs miscount @@ line counts; the content
        # lines are authoritative (sidecar-bfmr s2, 2026-09-27: "corrupt patch").
        argv = ["git", "apply", "--recount"] + (["--check"] if check_only else []) + [tmp]
        r = subprocess.run(argv, cwd=str(wt), capture_output=True, text=True, timeout=60)
        return r.returncode == 0, (r.stderr or r.stdout or "").strip()[-300:]
    finally:
        Path(tmp).unlink(missing_ok=True)


def _slicer_detached(plan_path, args, log_path):
    """Retired: the old slicer is gone. Logs a note, launches nothing, returns None."""
    try:
        with open(log_path, "a") as lf:
            lf.write("\n===== self-heal %s %s: %s =====\n" % (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), " ".join(args), _RETIRED_MSG))
    except OSError:
        pass
    return None


# ----------------------------------------------------------------------------
# the rung ladder (the owner 2026-10-02: "take Claude and me out of the loop for
# escalations so nothing hangs for days")
# ----------------------------------------------------------------------------
# After the esc-review verdict the system ACTS, within MAX_ATTEMPTS per slice:
#   (a) already satisfied  -> verified auto-skip (verify_satisfied_at_tip)
#   (c) harness defect     -> harness-only patch + --regate when the review gave a
#                             usable one; otherwise RESET the harness (--retry-slice
#                             clears the worktree) and re-author with the review's
#                             findings attached
#   (b) spec under-spec.   -> re-author with the review's findings attached
#   (d) capability         -> re-author with the findings attached (a different
#                             attempt, not the same one blind). There is no model
#                             or ctx rung today: every ladder model maps to the one
#                             Darkbloom model and Darkbloom sizes its own KV.
# FINAL RUNG (the only one that parks for a human): the budget is spent, or the
# escalation itself is "PIPELINE BUG SUSPECTED". It is never silent: ONE alert to
# The owner (notify-owner.py, deduped) naming the slice and the exact qctl commands, and
# a decisions.jsonl entry that `qctl status` shows.
DECISIONS = DISPATCH_DIR / "decisions.jsonl"
_VERDICT_RE = re.compile(r"^\W*VERDICT\W*\(?\s*([abcd])\b", re.I)


def review_verdict(review):
    """PURE. 'a'|'b'|'c'|'d' from the review (VERDICT line, ESC_RESULT json, or an inferred
    classification), else None. The ladder itself uses escalation_verdict.resolve, which
    never returns None."""
    try:
        v, _src = _ev().parse_review(review)
        if v:
            return v
    except Exception:
        pass
    for line in (review or "").splitlines():
        m = _VERDICT_RE.match(line.strip())
        if m:
            return m.group(1).lower()
        if line.strip().upper().startswith("VERDICT"):
            return None
    return None


def rung_for(kind, verdict, reason):
    """PURE. The action for an escalated slice BEFORE budget accounting:
    'final' | 'regate' | 'retry' | 'retry-notes'."""
    if "PIPELINE BUG SUSPECTED" in (reason or ""):
        return "final"
    if kind == "regate":
        return "regate"                      # a usable patch decides; else retry-notes
    if verdict in ("b", "c", "d"):
        return "retry-notes"
    if kind == "retry":
        return "retry"
    return "final"


def may_heal(reason):
    """PURE. Could the escalation ladder still act on this escalation once its review
    lands? The queue asks this BEFORE the review exists, so the verdict is unknown:
    True when SOME verdict leads to a non-final rung. classify() alone answered
    'none' for every reason that is not authoring/relevance-class (a gate FAIL, an
    auto-land refusal, ...), so the queue declared those slices stuck and parked the
    bundle even though a b/c/d review re-authors them via retry-notes (2026-10-02)."""
    kind = classify(reason)
    return any(rung_for(kind, v, reason) != "final" for v in ("b", "c", "d", None))


def log_decision(plan, sid, action, outcome, detail="", path=None):
    rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "actor": "self-heal",
           "action": action, "bundle": plan, "slice": sid, "job": None,
           "outcome": outcome, "detail": (detail or "")[:400]}
    try:
        pp = Path(path or DECISIONS)
        pp.parent.mkdir(parents=True, exist_ok=True)
        with pp.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except OSError:
        pass
    return rec


def final_rung(plan, sid, why, ctx=None, ledger_path=None, notifier=None):
    """Park for a human -- loudly. Returns 'park:final-rung'."""
    record(plan, sid, {"action": "final-rung", "detail": why}, ctx, ledger_path=ledger_path)
    log_decision(plan, sid, "final-rung", "parked-for-human", why)
    msg = (f"{plan} / {sid} needs a human: {why[:300]}. "
           f"See: qctl status {plan}. Then: qctl retry {plan} {sid}  |  "
           f"qctl skip {plan} {sid} --reason '...'")
    try:
        if notifier is None:
            import importlib.util as _ilu
            _s = _ilu.spec_from_file_location(
                "notify_owner", Path(__file__).resolve().parent / "notify-owner.py")
            _m = _ilu.module_from_spec(_s)
            _s.loader.exec_module(_m)
            notifier = _m.notify
        notifier("dispatch needs you", msg, dedupe_key=f"final-rung:{plan}/{sid}",
                 dedupe_s=12 * 3600)
    except Exception:
        pass
    return "park:final-rung"


def _write_notes(plan, sid, review_text):
    try:
        ESC_DIR.mkdir(parents=True, exist_ok=True)
        f = ESC_DIR / f"{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}-{plan}-{sid}.review-notes.md"
        f.write_text((review_text or "").strip()[:2500] + "\n")
        return f
    except OSError:
        return None


def _retry_with_notes(plan, sid, plan_path, review_text, why, launch, log_path, ctx,
                      ledger_path):
    nf = _write_notes(plan, sid, review_text)
    record(plan, sid, {"action": "retry-slice+notes", "detail": why}, ctx, count=True,
           ledger_path=ledger_path)
    log_decision(plan, sid, "retry+notes", "launched", why)
    launch(plan_path, ["--retry-slice", sid] + (["--review-notes", str(nf)] if nf else []),
           log_path)
    return "retry"


def heal(plan, sid, ctx=None, review_text=None, slice_runs=None, ledger_path=None,
         launch=None):
    """Decide and (when warranted) act. Returns the action taken:
    'regate' | 'retry' | 'skip:<why>'. `launch` is injectable for the self-test."""
    launch = launch or _slicer_detached
    if _ev().is_no_verdict_review(review_text):
        return "skip:no model verdict (review capped / no evidence) -- escalation stays open, no action taken"
    st = _load(Path(slice_runs or SLICE_RUNS) / f"{plan}.json") or {}
    s = (st.get("slices") or {}).get(sid) or {}
    # A HUMAN cancel is terminal (plan_cancel.py): the ev-service-screen retry of
    # 2026-09-27 18:42Z re-enqueued a job the owner had cancelled 37 min earlier.
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import plan_cancel
        _c = plan_cancel.cancelled(plan, runs_dir=slice_runs or SLICE_RUNS)
    except Exception:
        _c = None
    if _c:
        record(plan, sid, {"action": "none", "detail": "plan CANCELLED by a human (%s) -- "
                           "not healing" % (_c.get("at") or "?")}, ctx, ledger_path=ledger_path)
        return "skip:cancelled"
    if s.get("status") != "escalated":
        return "skip:slice is %r, not escalated" % s.get("status")
    # HAND-LOCK (2026-10-04, 2e): a human owns this harness -- no rung (retry,
    # harness patch + regate, skip) touches it. See ollama-dispatch-slice HAND_LOCK.
    _wt = s.get("worktree")
    if _wt and os.path.exists(os.path.join(_wt, HAND_LOCK)):
        record(plan, sid, {"action": "none", "detail": "worktree hand-locked (%s) -- "
                           "not healing" % os.path.join(_wt, HAND_LOCK)}, ctx,
               ledger_path=ledger_path)
        return "skip:hand-locked"
    if review_says_satisfied(review_text):
        return verified_skip(plan, sid, review_text, review_path=getattr(heal, "_review_path", None),
                             ctx=ctx, slice_runs=slice_runs, ledger_path=ledger_path,
                             verifier=getattr(heal, "_verifier", None),
                             launch=getattr(heal, "_launch", None),
                             resolve=getattr(heal, "_resolve", None),
                             jobs=getattr(heal, "_jobs", None))
    reason = s.get("escalation_reason") or ""
    kind = classify(reason)
    # A verdict is NEVER None (2026-10-09): structured/line/inferred review, else the
    # escalation reason's keywords, else (b) re-spec. Cached by input hash, never re-asked.
    _ev_ = _ev()
    _vd = _ev_.resolve(review_text, None, reason, cache_path=getattr(heal, "_verdict_cache", None),
                       key_parts=(plan, sid))
    verdict = _vd["verdict"]
    if verdict in ("b", "c", "d") and _vd["source"] in GROUNDING_REVIEW_SOURCES:
        try:
            _cx = Path(ctx).read_text(errors="replace") if ctx else ""
        except OSError:
            _cx = ""
        _ga, review_text, _gr = grounding_gate(
            review_text, _cx, "slice:%s:%s" % (plan, sid), "slice:%s:%s:" % (plan, sid),
            _vd["source"], review_path=getattr(heal, "_review_path", None),
            ledger_path=ledger_path, esc_dir=Path(ledger_path).parent if ledger_path else None,
            label_="%s/%s" % (plan, sid))
        if _ga and _ga.startswith("wait:"):
            return _ga
        if _ga and _ga.startswith("park:"):
            return final_rung(plan, sid, _ga[5:], ctx, ledger_path, getattr(heal, "_notifier", None))
    # a verdict that is only the DEFAULT (no evidence either way) must not turn a plain
    # authoring-class retry into a notes retry; but it also never leaves a
    # non-authoring escalation without an action.
    rung = rung_for(kind, None if _vd["source"] == "default" else verdict, reason)
    if rung == "final" and _vd["source"] == "default" and "PIPELINE BUG SUSPECTED" not in (reason or ""):
        rung = "retry-notes"
    notifier = getattr(heal, "_notifier", None)
    # IN-PLACE HARNESS REPAIR (2026-10-10): a converged harness whose only blocker is
    # verify-relevance (verdict c) is repaired in its KEPT worktree by one bounded queue
    # job -- never wiped by --retry-slice, never parked for a human. Decided BEFORE the
    # attempt budget / re-spec below (those end in --retry-slice or the final rung).
    _rcause = harness_repair_cause(reason, verdict, kind)
    _rstate = {"done": False, "act": None}

    def _try_repair():
        if not _rcause:
            return None
        if not _rstate["done"]:
            _rstate["done"] = True
            _rstate["act"] = heal_harness_repair(plan, sid, st, s, _rcause, review_text, ctx,
                                                 ledger_path, getattr(heal, "_repair_hooks", None))
        return _rstate["act"]
    if _rcause and rung != "final" and not extract_patch(review_text or ""):
        _ract = _try_repair()
        if _ract:
            return _ract
    if rung == "final":
        return final_rung(plan, sid, "no automatic action applies (verdict %s): %s"
                          % (verdict or "none", reason[:200]), ctx, ledger_path, notifier)
    _led = _load(Path(ledger_path or HEAL_LEDGER)) or {}
    used = attempts_used(_led, plan, sid)
    plan_path = st.get("plan_path")
    if not plan_path or not Path(plan_path).is_file():
        record(plan, sid, {"action": "none", "detail": "plan file missing (%r)" % plan_path},
               ctx, ledger_path=ledger_path)
        return "skip:no plan file"
    log_path = Path(slice_runs or SLICE_RUNS) / f"{plan}.advance.log"
    # ABANDON-AFTER-N (research 2026-10-09): the SAME mechanical failure signature N times
    # (default 2), or the attempt budget spent, means another identical retry is pointless:
    # go to RE-SPEC (one per slice) instead of parking for a human.
    _ract = _try_repair()           # a spent attempt budget must not wipe a repairable harness
    if _ract:
        return _ract
    _cfg = _ev_.load_config()
    _sig = _ev_.failure_signature("slice", reason)
    _rec_s = _led.get(heal_key(plan, sid)) or {}
    _n_same = _ev_.same_signature_count(list(_rec_s.get("sigs") or []) + [_sig], _sig)
    if (_n_same >= int(_cfg["abandon_same_signature_n"]) or used >= MAX_ATTEMPTS):
        why_ = ("same failure signature %d times" % _n_same
                if _n_same >= int(_cfg["abandon_same_signature_n"])
                else "%d attempts spent" % used)
        if _ev_.actions_killed() or int(_rec_s.get("respecs") or 0) >= int(_cfg["slice_respec_max"]):
            return final_rung(plan, sid, "%s and the re-spec is %s (last verdict %s): %s"
                              % (why_, "disabled" if _ev_.actions_killed() else "spent", verdict,
                                 reason[:200]), ctx, ledger_path, notifier)
        _lp = Path(ledger_path or HEAL_LEDGER)
        _l2 = _load(_lp) or {}
        _r2 = _l2.setdefault(heal_key(plan, sid), {"attempts": 0, "log": []})
        _r2["respecs"] = int(_r2.get("respecs") or 0) + 1
        _r2.setdefault("sigs", []).append(_sig)
        try:
            _lp.parent.mkdir(parents=True, exist_ok=True)
            _tmp = _lp.with_suffix(".tmp")
            _tmp.write_text(json.dumps(_l2, indent=1))
            _tmp.replace(_lp)
        except OSError:
            pass
        _notes = ("RE-SPEC (automated): %s. Do NOT repeat the previous approach. The slice's "
                  "intent/must_contain/verify_shape are the suspect: author a harness whose "
                  "fixture FAILS at baseline and passes only with the reference implementation, "
                  "narrowed to the ONE property named in the intent.\n\n%s"
                  % (why_, (review_text or "").strip()[-1800:]))
        return _retry_with_notes(plan, sid, plan_path, _notes,
                                 "RE-SPEC: %s (verdict %s)" % (why_, verdict),
                                 launch, log_path, ctx, ledger_path)
    try:
        _lp = Path(ledger_path or HEAL_LEDGER)
        _l2 = _load(_lp) or {}
        _l2.setdefault(heal_key(plan, sid), {"attempts": 0, "log": []}).setdefault(
            "sigs", []).append(_sig)
        _tmp = _lp.with_suffix(".tmp")
        _tmp.write_text(json.dumps(_l2, indent=1))
        _tmp.replace(_lp)
    except OSError:
        pass

    def _fallback(why):
        # (c) harness defect with no usable patch: RESET the harness and re-author
        # with the findings, instead of parking on "the review proposed no diff".
        if verdict in ("b", "c", "d"):
            return _retry_with_notes(plan, sid, plan_path, review_text,
                                     "verdict %s; %s -- resetting the harness and re-authoring "
                                     "with the review's findings" % (verdict, why),
                                     launch, log_path, ctx, ledger_path)
        return None

    if rung == "retry-notes":
        return _retry_with_notes(plan, sid, plan_path, review_text,
                                 "verdict %s (%s) -- re-authoring with the review's findings"
                                 % (verdict, reason[:160]), launch, log_path, ctx, ledger_path)

    if kind == "retry":
        record(plan, sid, {"action": "retry-slice", "detail": "authoring-class escalation "
                           "(%s) -- re-authoring clean" % reason[:160]}, ctx, count=True,
               ledger_path=ledger_path)
        launch(plan_path, ["--retry-slice", sid], log_path)
        return "retry"

    wt = s.get("worktree")
    if not wt or not Path(wt).is_dir():
        record(plan, sid, {"action": "none", "detail": "worktree %r is gone -- nothing to "
                           "patch in place" % wt}, ctx, ledger_path=ledger_path)
        return _fallback("worktree gone") or "skip:no worktree"
    patch = extract_patch(review_text or "")
    if not patch:
        record(plan, sid, {"action": "none", "detail": "the review proposed no diff"},
               ctx, ledger_path=ledger_path)
        return _fallback("no usable diff") or "skip:no patch"
    manifest = _load(Path(wt) / ".dispatch-harness.json")
    target = (manifest or {}).get("target") or st.get("target")
    bad = refused_files(patch, allowed_files(manifest, target))
    if bad:
        record(plan, sid, {"action": "refused", "detail": "the diff touches non-harness "
                           "file(s) %s -- self-heal only edits the fixture/refimpl" % bad},
               ctx, ledger_path=ledger_path)
        return _fallback("the diff touched non-harness files") or "skip:non-harness diff"
    ok, why = _git_apply(wt, patch, check_only=True)
    if not ok:
        record(plan, sid, {"action": "refused", "detail": "diff does not apply in %s: %s"
                           % (wt, why)}, ctx, ledger_path=ledger_path)
        return _fallback("the diff did not apply") or "skip:does not apply"
    ok, why = _git_apply(wt, patch, check_only=False)
    if not ok:
        record(plan, sid, {"action": "refused", "detail": "git apply failed: %s" % why},
               ctx, ledger_path=ledger_path)
        return "skip:apply failed"
    record(plan, sid, {"action": "patch+regate", "detail": "applied the reviewed harness "
                       "diff (%s) in %s; re-gating (preflight + verify-relevance decide GO)"
                       % (", ".join(patch_files(patch)), wt)}, ctx, count=True,
           ledger_path=ledger_path)
    launch(plan_path, ["--regate", sid], log_path)
    return "regate"


# ----------------------------------------------------------------------------
# IN-PLACE HARNESS REPAIR (2026-10-10, aw-codec-floor s3 -- seen twice in one day)
# ----------------------------------------------------------------------------
# THE GAP. "HARNESS CONVERGED, PREFLIGHT NO-GO -- worktree KEPT ... last blockers=
# ['verify-relevance']" with an esc-review verdict (c) ("the fixture's assertions are
# satisfied by BOTH the baseline and the refimpl") and "proposed fix: none written".
# The ladder then had only: apply a reviewed diff (none exists) or --retry-slice
# (DELETES the converged worktree and re-authors, the thing the converged-harness
# keep exists to prevent) -- and once the attempt budget was spent, a final-rung park.
# A human/agent had to run the relevance gate, strengthen test_fixture.py in place,
# re-run preflight --auto-seal and `--regate`.
#
# THE ACTION (owner: this file; table row harness_repair:*). When the escalation is
# regate-class AND (the converged-harness-NO-GO reason names verify-relevance, OR the
# review verdict is (c) and the reason is a verify-relevance NO-GO):
#   1. run the relevance gate (preflight --json) on the KEPT worktree to get the exact
#      SURVIVING mutants -- no survivors / no verify-relevance blocker = not ours, fall
#      through to the existing ladder untouched;
#   2. enqueue ONE queue job, label auto-refine-<plan>-<sid>-r10N (the existing
#      refine-round lane: continuation-round launch baseline, authoring-tier, gate-
#      exempt), stamped with the slice's bundle, on the slice's own local model, in the
#      KEPT worktree. Its prompt (harness_repair.repair_prompt) carries the survivors
#      and the review's reasoning and confines the edit to the fixture; its --verify is
#      `python3 auto-harness-check.py && python3 harness-repair-verify.py <wt>`, i.e.
#      VERIFY_OK AND the relevance gate reports no survivors -- the gate itself, never a
#      bypass;
#   3. harness_repair_sweep (every watcher pass, via --job-sweep) reads the finished
#      job: scope-checks that only the fixture changed (a violation is reverted and
#      counts as a failed attempt), then launches the existing `--regate` (never
#      --retry-slice; the worktree is never deleted) so the slice re-enters through the
#      normal preflight + enqueue. A failed attempt re-surveys and tries again.
# BOUNDS. HARNESS_REPAIR_MAX attempts per slice (ledger self-heal.json
# [plan/sid].repairs, survives restarts); one in flight at a time (ledger + queue
# label scan + the queue's own duplicate-label refusal); a spent budget falls back to
# the EXISTING park + ONE deduped alert (final_rung). Protected files are never edited.
HARNESS_REPAIR_MAX = 2
HARNESS_REPAIR_CAUSES = ("converged-nogo-verify-relevance", "review-c-relevance-survivors")
_CONVERGED_NOGO_RE = re.compile(r"HARNESS CONVERGED,\s*PREFLIGHT NO-GO", re.I)
_REPAIR_LIVE = ("pending", "running", "queued", "scheduled", "held", "paused", "planned")
REPAIR_VERIFY = Path(__file__).resolve().parent / "harness-repair-verify.py"
REPAIR_ROUND_BASE = 100          # label rounds r101, r102: never collide with authoring r1..rN


def _hr():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import harness_repair
    return harness_repair


def harness_repair_cause(reason, verdict, kind):
    """PURE. Which in-place-repair cause (HARNESS_REPAIR_CAUSES) an escalation is, or None.
    Only regate-class reasons qualify (authoring / stale-base / already-satisfied /
    PIPELINE BUG are other owners'); the converged-harness NO-GO needs verify-relevance
    among its blockers; a verdict-(c) review qualifies on a verify-relevance NO-GO."""
    r = reason or ""
    if kind != "regate" or "PIPELINE BUG SUSPECTED" in r or "already satisfied" in r.lower():
        return None
    if _CONVERGED_NOGO_RE.search(r) and "verify-relevance" in r:
        return HARNESS_REPAIR_CAUSES[0]
    if verdict == "c" and ("verify-relevance" in r or "LOW relevance" in r):
        return HARNESS_REPAIR_CAUSES[1]
    return None


def repair_label(plan, sid, n):
    return "auto-refine-%s-%s-r%d" % (plan, sid, REPAIR_ROUND_BASE + int(n))


_REPAIR_LABEL_RE = re.compile(r"-r10\d$")


def _repair_state(led, plan, sid):
    rp = ((led or {}).get(heal_key(plan, sid)) or {}).get("repairs") or {}
    return {"n": int(rp.get("n") or 0), "jobs": list(rp.get("jobs") or []),
            "clean_regates": int(rp.get("clean_regates") or 0)}


def _repair_update(plan, sid, fn, ledger_path=None):
    lp = Path(ledger_path or HEAL_LEDGER)
    led = _load(lp) or {}
    rec = led.setdefault(heal_key(plan, sid), {"attempts": 0, "log": []})
    rp = rec.setdefault("repairs", {"n": 0, "jobs": []})
    fn(rp)
    rp["jobs"] = rp.get("jobs", [])[-6:]
    try:
        lp.parent.mkdir(parents=True, exist_ok=True)
        tmp = lp.with_suffix(".tmp")
        tmp.write_text(json.dumps(led, indent=1))
        tmp.replace(lp)
    except OSError:
        pass
    return rp


def _repair_job_row(jid, jobs):
    return next((j for j in (jobs or []) if j.get("id") == jid), None)


def _repair_live_jobs(plan, sid, jobs):
    """Queue rows that are a LIVE repair/refine round of this slice (any -r10N label)."""
    base = "%s-%s" % (plan, sid)
    out = []
    for j in jobs or []:
        lab = str(j.get("label") or "")
        if (lab.startswith("auto-refine-" + base + "-r") and _REPAIR_LABEL_RE.search(lab)
                and j.get("status") in _REPAIR_LIVE):
            out.append(j)
    return out


def _repair_model(s, jobs, sidecar=None):
    """(model, host, num_ctx) of the slice's most recent authoring job -- the local model
    the harness was authored with -- else the plan defaults."""
    for jid in reversed(list(s.get("author_job_ids") or [])):
        row = _repair_job_row(jid, jobs) or (sidecar or _sidecar)(jid) or {}
        if row.get("model"):
            return (str(row["model"]), str(row.get("host_pref") or row.get("host") or "studio"),
                    int(row.get("num_ctx") or 131072))
    return "qwen3.6-35b-a3b-vl-mtp-mxfp8", "studio", 131072


def _auto_mod():
    """ollama-dispatch-auto loaded as a module (refine wording + refresh_harness_check +
    write_refine_guard are reused, not re-implemented). None when it cannot load."""
    try:
        from importlib.machinery import SourceFileLoader as _SFL
        import importlib.util as _ilu
        sl = _SFL("_oda_for_repair", str(AUTO_TOOL))
        sp = _ilu.spec_from_loader("_oda_for_repair", sl)
        m = _ilu.module_from_spec(sp)
        sys.modules["_oda_for_repair"] = m
        sl.exec_module(m)
        return m
    except BaseException:
        return None


def _latest_review(plan, sid, esc_dir=None):
    try:
        c = sorted(Path(esc_dir or ESC_DIR).glob("*-%s-%s*.review.md" % (plan, sid)))
        return c[-1].read_text() if c else ""
    except OSError:
        return ""


def heal_harness_repair(plan, sid, st, s, cause, review_text="", ctx=None, ledger_path=None,
                        hooks=None):
    """Enqueue ONE in-place harness-repair job for a parked slice (see the block comment).
    Returns 'repair:<job id>' | 'regate' | 'wait:<why>' | None. None = not applicable /
    budget spent -> the caller falls through to the existing ladder, which is unchanged.
    `hooks` (tests): jobs, enqueue, survey, launch, auto_mod (False = none), esc_dir."""
    hooks = hooks or {}
    ev = _ev()
    if ev.actions_killed():
        return None
    wt = s.get("worktree")
    if not wt or not Path(wt).is_dir():
        return None
    if Path(wt, HAND_LOCK).exists():
        return None
    jobs = hooks["jobs"] if "jobs" in hooks else _queue_jobs()
    led = _load(Path(ledger_path or HEAL_LEDGER)) or {}
    rp = _repair_state(led, plan, sid)
    if any(j.get("state") == "open" for j in rp["jobs"]) or _repair_live_jobs(plan, sid, jobs):
        return "wait:a harness-repair job for %s/%s is already in flight" % (plan, sid)
    if rp["n"] >= HARNESS_REPAIR_MAX:
        record(plan, sid, {"action": "harness-repair-capped",
                           "detail": "%d/%d in-place repair attempts spent -- falling back to the "
                                     "existing ladder (park + alert)" % (rp["n"], HARNESS_REPAIR_MAX)},
               ctx, ledger_path=ledger_path)
        return None
    hr = _hr()
    man = _load(Path(wt) / ".dispatch-harness.json") or {}
    target = man.get("target") or st.get("target") or "the target"
    reqs = list(s.get("must_contain") or [])
    sv = (hooks.get("survey") or hr.survey)(wt, reqs)
    if hr.survey_clean(sv):
        # the fixture already pins the property (fixed by hand / an earlier round): the
        # only missing step is the re-gate. Once per slice, then the gate decides.
        if rp["clean_regates"] >= 1:
            return None
        _repair_update(plan, sid, lambda r: r.__setitem__("clean_regates", rp["clean_regates"] + 1),
                       ledger_path)
        record(plan, sid, {"action": "harness-repair-regate", "detail":
                           "[%s] relevance gate already reports no survivors on the kept harness -- "
                           "re-gating (preflight decides GO)" % cause}, ctx, ledger_path=ledger_path)
        (hooks.get("launch") or _slicer_detached)(
            st.get("plan_path"), ["--regate", sid],
            Path(hooks.get("slice_runs") or SLICE_RUNS) / ("%s.advance.log" % plan))
        return "regate"
    if not (sv.get("parsed") and sv.get("relevance_blocked") and sv.get("survivors")):
        record(plan, sid, {"action": "harness-repair-none", "detail":
                           "[%s] the relevance gate named no surviving mutants (%s) -- nothing "
                           "mechanical to feed a repair; existing ladder applies"
                           % (cause, (sv.get("reason") or "no verify-relevance survivors")[:160])},
               ctx, ledger_path=ledger_path)
        return None
    fixtures = hr.fixture_files(wt, target)
    if not fixtures:
        return None
    am = hooks["auto_mod"] if "auto_mod" in hooks else _auto_mod()
    hints, line_map, lang = {}, None, st.get("lang") or "python"
    if am:
        try:
            hints = {k: getattr(am, k, "") for k in
                     ("SURVIVOR_KILL_RULE", "REFINE_WORK_RULE", "MOCK_FIDELITY_HINT")}
            try:
                line_map = am.map_survivor_lines(
                    sv["survivors"], target, (Path(wt) / "refimpl.py").read_text(),
                    am.patched_target_text(Path(wt), target))
            except Exception:
                line_map = None
            am.refresh_harness_check(Path(wt), lang, reqs)
            am.write_refine_guard(Path(wt), target, sv["survivors"], lang)
        except Exception:
            pass
    n = rp["n"] + 1
    label = repair_label(plan, sid, n)
    prompt = hr.repair_prompt(fixtures[0], target, sv["survivors"], review_text,
                              sv.get("other_blockers") or [], hints=hints, line_map=line_map)
    esc = Path(hooks.get("esc_dir") or ESC_DIR)
    try:
        esc.mkdir(parents=True, exist_ok=True)
        tf = esc / ("%s-%s-%s.repair-%d.task.md" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                                                 plan, sid, n))
        tf.write_text(prompt)
    except OSError as e:
        return "skip:cannot write the repair task: %s" % e
    import shlex
    model, host, nctx = _repair_model(s, jobs, hooks.get("sidecar"))
    verify = "python3 auto-harness-check.py && python3 %s %s %s" % (
        shlex.quote(str(REPAIR_VERIFY)), shlex.quote(str(wt)),
        " ".join(shlex.quote("--require=" + r) for r in reqs))
    bundle = st.get("bundle") or plan
    argv = ["--model", model, "--host", host, "--cwd", str(wt), "--task-file", str(tf),
            "--task-kind", "coding", "--verify", verify.strip(), "--max-iters", "24",
            "--num-ctx", str(nctx), "--label", label, "--bundle", str(bundle)]
    rc_, head_, _e = _git(wt, "rev-parse", "HEAD")
    new_id, out = (hooks.get("enqueue") or _queue_enqueue)(argv)
    if not new_id:
        dup = re.search(r"duplicate-label\s+([0-9a-f]{8,})", out or "")
        if dup:
            return "wait:duplicate-label %s is already live" % dup.group(1)
        record(plan, sid, {"action": "harness-repair-enqueue-failed", "detail": " ".join((out or "").split())[-200:]},
               ctx, ledger_path=ledger_path)
        return "skip:repair enqueue failed"
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

    def _add(r):
        r["n"] = int(r.get("n") or 0) + 1
        r.setdefault("jobs", []).append({
            "id": new_id, "label": label, "n": n, "cause": cause, "at": stamp,
            "state": "open", "base_head": head_ if rc_ == 0 else None, "fixtures": fixtures,
            "survivors": len(sv["survivors"])})
    _repair_update(plan, sid, _add, ledger_path)
    log_decision(plan, sid, "harness-repair", "enqueued",
                 "[%s] job %s (%s, %s) fixes %d surviving mutant(s) in place in %s"
                 % (cause, new_id, label, model, len(sv["survivors"]), wt))
    record(plan, sid, {"action": "harness-repair", "detail":
                       "[%s] enqueued in-place repair job %s (%s, model %s, bundle %s) for %d "
                       "surviving mutant(s) in the KEPT worktree %s; success = VERIFY_OK + relevance "
                       "gate clean, then --regate (attempt %d/%d)"
                       % (cause, new_id, label, model, bundle, len(sv["survivors"]), wt, n,
                          HARNESS_REPAIR_MAX)}, ctx, ledger_path=ledger_path)
    return "repair:%s" % new_id


def _repair_revert(wt, base_head, paths, run=subprocess.run):
    """Undo out-of-scope edits a repair job made: restore tracked paths from `base_head`,
    delete files it added. Best-effort; never touches the allowed fixture."""
    for p in paths:
        try:
            r = run(["git", "-C", str(wt), "cat-file", "-e", "%s:%s" % (base_head, p)],
                    capture_output=True, text=True, timeout=30)
            if base_head and r.returncode == 0:
                run(["git", "-C", str(wt), "checkout", base_head, "--", p],
                    capture_output=True, text=True, timeout=60)
            else:
                run(["git", "-C", str(wt), "rm", "-f", "--quiet", "--", p],
                    capture_output=True, text=True, timeout=60)
                Path(wt, p).unlink(missing_ok=True)
        except Exception:
            pass


def harness_repair_sweep(ledger_path=None, slice_runs=None, hooks=None):
    """Every watcher pass. For each OPEN repair job in the ledger: wait while it is live;
    when finished, scope-check it and (passed) launch the existing --regate, or (failed)
    spend the next capped attempt / fall back to the park + alert. Returns
    [(plan/sid, action)]."""
    hooks = hooks or {}
    lp = Path(ledger_path or HEAL_LEDGER)
    led = _load(lp) or {}
    jobs = hooks["jobs"] if "jobs" in hooks else _queue_jobs()
    out = []
    for key, rec in list(led.items()):
        if "/" not in key or not isinstance(rec, dict) or not (rec.get("repairs") or {}).get("jobs"):
            continue
        plan, sid = key.split("/", 1)
        for j in [x for x in rec["repairs"]["jobs"] if x.get("state") == "open"]:
            jid = j.get("id")
            row = _repair_job_row(jid, jobs)
            if row and (row.get("status") in _REPAIR_LIVE):
                out.append((key, "wait:%s is %s" % (jid, row.get("status"))))
                continue
            st = _load(Path(slice_runs or SLICE_RUNS) / ("%s.json" % plan)) or {}
            s = (st.get("slices") or {}).get(sid) or {}
            wt = s.get("worktree")
            fin = row or (hooks.get("sidecar") or _sidecar)(jid) or {}
            status = fin.get("status")
            if not fin:
                out.append((key, "wait:%s has no queue record yet" % jid))
                continue
            try:
                passed = status in ("done", "done_unconverged") and int(fin.get("exit_code") or 0) == 0
            except (TypeError, ValueError):
                passed = False

            def _close(state, why):
                def _f(r):
                    for x in r.get("jobs", []):
                        if x.get("id") == jid:
                            x["state"], x["closed_at"], x["why"] = state, time.strftime(
                                "%Y-%m-%dT%H:%M:%SZ", time.gmtime()), why[:300]
                _repair_update(plan, sid, _f, ledger_path)
            if s.get("status") != "escalated" or not wt or not Path(wt).is_dir():
                _close("moot", "slice is %r / worktree gone" % s.get("status"))
                out.append((key, "moot"))
                continue
            try:
                (Path(wt) / ".refine-guard.json").unlink(missing_ok=True)
            except OSError:
                pass
            viol = []
            if passed:
                viol = _hr().scope_violations(wt, j.get("base_head"), j.get("fixtures") or [])
                if viol:
                    _repair_revert(wt, j.get("base_head"), viol)
                    passed = False
            if str(status) == "needs_opus":
                try:
                    (hooks.get("resolve") or _queue_resolve)(jid)
                except Exception:
                    pass
            if passed:
                _close("passed", "verify green + relevance gate clean; --regate launched")
                record(plan, sid, {"action": "harness-repair-regate", "detail":
                                   "repair job %s passed (VERIFY_OK + relevance gate clean); "
                                   "re-gating the kept worktree with --regate" % jid},
                       None, ledger_path=ledger_path)
                (hooks.get("launch") or _slicer_detached)(
                    st.get("plan_path"), ["--regate", sid],
                    Path(slice_runs or SLICE_RUNS) / ("%s.advance.log" % plan))
                out.append((key, "regate"))
                continue
            why = ("repair job %s ended %s (exit %s)" % (jid, status, fin.get("exit_code"))
                   + ("; reverted out-of-scope edits to %s" % viol if viol else ""))
            _close("failed", why)
            record(plan, sid, {"action": "harness-repair-failed", "detail": why}, None,
                   ledger_path=ledger_path)
            nxt = None
            reason = s.get("escalation_reason") or ""
            cause = harness_repair_cause(reason, "c", classify(reason)) or j.get("cause")
            if cause:
                nxt = heal_harness_repair(plan, sid, st, s, cause,
                                          _latest_review(plan, sid, hooks.get("esc_dir")),
                                          None, ledger_path, hooks)
            if nxt is None:
                final_rung(plan, sid, "in-place harness repair spent (%d/%d attempts) and the relevance "
                           "gate still blocks: %s" % (_repair_state(_load(lp) or {}, plan, sid)["n"],
                                                       HARNESS_REPAIR_MAX, reason[:200]),
                           None, ledger_path, hooks.get("notifier"))
                out.append((key, "park:final-rung"))
            else:
                out.append((key, nxt))
    return out


# ----------------------------------------------------------------------------
# JOB-LEVEL heal for a NON-SLICE authoring job (2026-10-05, rt-bg-commitments-fix)
# ----------------------------------------------------------------------------
# THE GAP. A plain `ollama-dispatch-auto` run (no slicer) that cannot converge its
# authoring PARKS VISIBLY: park_visible escalates its last auto-author job to the
# queue's needs_opus lane. The watcher detects it (source D), a local review runs and
# gives a verdict -- and then self-heal answered "skip:job is not a slice of any plan",
# because every lever above is a SLICER lever (--retry-slice / --regate). Nothing
# acted, the driver had already exited, and the bundle parked "needs the owner"
# (95234578bc40, verdict b: a fixture/harness defect). A human then enqueued exactly
# the obvious remedy -- one more continuation authoring round on the SAME worktree
# with --continues -- and it converged (3f75be3df79a, VERIFY_OK).
#
# THE LEVER, and its bounds:
#   * only an `auto-author-*` needs_opus row (the harness-authoring stage), not a
#     slice job, with a review verdict b/c (spec/fixture/harness defect) -- (a) has
#     its own verified-skip path, (d) model-incapacity is not fixed by one more round;
#   * the remedy is ONE continuation authoring round on the LOCAL model the job
#     already used (the queue's own model/host/ctx), enqueued `--continues <stuck>`
#     in the same bundle, prompt = the stuck job's prompt + the review's findings.
#     NO Opus / claude -p is ever spawned here: Opus spend on this path is ZERO by
#     construction (the review itself is a local queue job since 2026-10-01);
#   * HARD CAPS (ledger escalations/self-heal.json "jobs" section, survives restarts):
#     JOB_HEAL_MAX_PER_CHAIN rounds per job chain (label base + worktree), ever;
#     JOB_HEAL_MAX_PER_BUNDLE_DAY per bundle per UTC day; JOB_HEAL_MAX_PER_DAY in
#     total per UTC day. A cap that blocks a heal fires ONE deduped alert (final
#     rung) and the row stays parked for a human -- never a loop;
#   * the worktree must exist, not be hand-locked, and carry no other live job.
# CLEAR ON PASS (job_heal_sweep, every watcher pass): a needs_opus row whose
# continuation -- by `continues`/`superseded_by`, the heal ledger, or a same-base
# same-worktree job that finished AFTER the escalation -- ended done/exit 0 is
# RESOLVED through `ollama-queue.py resolve`; the escalation index janitor then ticks
# its D row (job gone) and the bundle's Q row (no longer parked). A continuation that
# FAILED leaves the row parked; the sweep spends the next capped round on it.
JOB_HEAL_MAX_PER_CHAIN = 2
JOB_HEAL_MAX_PER_BUNDLE_DAY = 3
JOB_HEAL_MAX_PER_DAY = 6
JOB_HEAL_WINDOW_S = 30 * 60      # the queue treats a fresh eligible row as heal-pending this long
JOB_HEAL_VERDICTS = ("a", "b", "c", "d")   # every verdict drives an action (2026-10-09)
AUTHOR_LABEL_PREFIXES = ("auto-author-", "needs-opus-auto-")
QUEUE_PY = Path(__file__).resolve().parent / "ollama-queue.py"
QUEUE_LOG_DIR = Path.home() / "bin" / "ollama-queue-logs"
_LIVE = ("pending", "running", "queued", "scheduled", "held", "paused", "planned")
_REVIEW_NOTES_HEAD = "## ESCALATION REVIEW FINDINGS -- fix these FIRST"


def _utc_day(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime(ts if ts is not None else time.time()))


def _parse_ts(s):
    """Epoch seconds from an ISO timestamp ('...Z' or '+00:00'), else None."""
    if not s:
        return None
    try:
        from datetime import datetime, timezone
        t = str(s).strip().replace("Z", "+00:00")
        d = datetime.fromisoformat(t)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d.timestamp()
    except Exception:
        return None


def job_chain_key(job):
    """PURE. One key per authoring chain: label base + worktree."""
    return "job:%s@%s" % (label_base(job.get("label")), job.get("cwd") or "?")


def job_heal_budget(ledger, job, now=None):
    """PURE. (ok: bool, why: str) -- may one more heal round be spent on `job`?"""
    jl = (ledger or {}).get("jobs") or {}
    rec = (jl.get("chains") or {}).get(job_chain_key(job)) or {}
    if len(rec.get("rounds") or []) >= JOB_HEAL_MAX_PER_CHAIN:
        return False, ("job-heal cap: %d/%d rounds already spent on chain %s"
                       % (len(rec["rounds"]), JOB_HEAL_MAX_PER_CHAIN, job_chain_key(job)))
    day = _utc_day(now)
    spent = [r for r in (jl.get("log") or []) if r.get("day") == day]
    if len(spent) >= JOB_HEAL_MAX_PER_DAY:
        return False, "job-heal daily cap: %d/%d rounds spent today (%s UTC)" % (
            len(spent), JOB_HEAL_MAX_PER_DAY, day)
    b = job.get("bundle")
    if b and sum(1 for r in spent if r.get("bundle") == b) >= JOB_HEAL_MAX_PER_BUNDLE_DAY:
        return False, "job-heal bundle cap: %d/%d rounds spent today on bundle %s" % (
            JOB_HEAL_MAX_PER_BUNDLE_DAY, JOB_HEAL_MAX_PER_BUNDLE_DAY, b)
    return True, "ok"


def job_heal_eligible(job, slice_runs=None):
    """PURE-ish (reads the slice-run dir). (ok, why) for the structural conditions."""
    lbl = str(job.get("label") or "")
    if job.get("status") != "needs_opus":
        return False, "status %r, not needs_opus" % job.get("status")
    if not lbl.startswith(AUTHOR_LABEL_PREFIXES):
        return False, "not an auto-author (harness authoring) job"
    if plan_slice_for_label(lbl, slice_runs)[0]:
        return False, "a slice job (the slice ladder owns it)"
    if job.get("awaiting_signoff") or job.get("user_hold"):
        return False, "held for a human"
    wt = job.get("cwd")
    if not wt or not Path(wt).is_dir():
        return False, "worktree %r is gone" % wt
    if Path(wt, HAND_LOCK).exists():
        return False, "worktree hand-locked"
    return True, "ok"


def job_heal_pending(job, ledger=None, now=None, slice_runs=None, window=JOB_HEAL_WINDOW_S,
                     since=None):
    """Would the job ladder still act on this needs_opus row by itself? The queue asks
    this before parking a bundle on it (mirrors escalation_heal_pending for slices):
    eligible, budget left, no final rung recorded, and either freshly escalated
    (within `window`, the watcher+review latency) or its last heal round FAILED less
    than `window` ago (the sweep spends the next round on its next pass)."""
    now = time.time() if now is None else now
    try:
        if not job_heal_eligible(job, slice_runs)[0]:
            return False
        if ledger is None:
            ledger = _load(HEAL_LEDGER) or {}
        if not job_heal_budget(ledger, job, now)[0]:
            return False
        rec = (((ledger or {}).get("jobs") or {}).get("chains") or {}).get(job_chain_key(job)) or {}
        if rec.get("final_at") and (_parse_ts(rec["final_at"]) or 0) >= (
                _parse_ts((job.get("escalation") or {}).get("escalated_at")) or 0) - 1:
            return False
        if rec.get("declined_at") and rec.get("declined_job") == job.get("id"):
            return False
        ref = max(_parse_ts((job.get("escalation") or {}).get("escalated_at")) or 0,
                  _parse_ts(rec.get("last_at")) or 0, float(since or 0))
        return (now - ref) < window
    except Exception:
        return False


def _job_ledger_update(fn, ledger_path=None):
    p = Path(ledger_path or HEAL_LEDGER)
    led = _load(p) or {}
    jl = led.setdefault("jobs", {})
    jl.setdefault("chains", {})
    jl.setdefault("log", [])
    fn(jl)
    jl["log"] = jl["log"][-200:]
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(led, indent=1))
    os.replace(tmp, p)
    return led


def _sidecar(jid, log_dir=None):
    d = Path(log_dir or QUEUE_LOG_DIR)
    for base in (d, d / "archive"):
        rec = _load(base / ("%s.done.json" % jid))
        if isinstance(rec, dict):
            return rec
    return None


def _all_sidecars(log_dir=None):
    d = Path(log_dir or QUEUE_LOG_DIR)
    out = []
    for base in (d, d / "archive"):
        try:
            files = list(base.glob("*.done.json"))
        except OSError:
            continue
        for f in files:
            rec = _load(f)
            if isinstance(rec, dict) and rec.get("id"):
                out.append(rec)
    return out


def _passed(rec):
    try:
        return rec.get("status") == "done" and int(rec.get("exit_code")) == 0
    except (TypeError, ValueError):
        return False


def continuation_outcome(job, jobs, ledger=None, log_dir=None, sidecars=None, only=None):
    """The outcome of `job`'s continuations: ('passed', id) | ('live', id) |
    ('failed', id) | (None, None). A continuation is a job that (1) is named by the
    stuck row's `superseded_by`, (2) has `continues == job.id`, (3) is a heal round
    the ledger recorded for this chain, or (4) shares the label base AND worktree
    and finished after the escalation. Passed wins over live wins over failed.
    `only` (a set of ids): consider exactly those candidates (the queue's cheap path)."""
    jid = job.get("id")
    base, cwd = label_base(job.get("label")), job.get("cwd")
    esc_at = _parse_ts((job.get("escalation") or {}).get("escalated_at")) or 0
    rec = (((ledger or {}).get("jobs") or {}).get("chains") or {}).get(job_chain_key(job)) or {}
    named = {x for x in [job.get("superseded_by")] + list(job.get("continued_by") or []) if x}
    named |= {r.get("job") for r in rec.get("rounds") or [] if r.get("job")}
    found = {}
    for j in jobs or []:
        if j.get("id") == jid:
            continue
        if only is not None:
            if j.get("id") in only:
                found[j["id"]] = j
            continue
        if (j.get("id") in named or j.get("continues") == jid
                or (label_base(j.get("label")) == base and cwd and j.get("cwd") == cwd
                    and (_parse_ts(j.get("enqueued_at")) or 0) > esc_at)):
            found[j["id"]] = j
    for r in (sidecars if sidecars is not None else _all_sidecars(log_dir)):
        if r.get("id") == jid or r.get("id") in found:
            continue
        if only is not None:
            if r.get("id") in only:
                found[r["id"]] = r
            continue
        if (r.get("id") in named or r.get("continues") == jid
                or (label_base(r.get("label")) == base and cwd and r.get("cwd") == cwd
                    and (_parse_ts(r.get("persisted_at")) or 0) > esc_at)):
            found[r["id"]] = r
    for want in ("passed", "live", "failed"):
        for k, v in found.items():
            st = v.get("status")
            if want == "passed" and _passed(v):
                return "passed", k
            if want == "live" and st in _LIVE:
                return "live", k
            if want == "failed" and st in ("failed", "done_unconverged", "cancelled",
                                           "needs_opus", "blocked", "error"):
                return "failed", k
    return None, None


def _next_c_label(job, jobs, log_dir=None):
    base = label_base(job.get("label"))
    seen = [0]
    names = [str(j.get("label") or "") for j in jobs or []]
    try:
        names += [p.name for p in Path(log_dir or QUEUE_LOG_DIR).glob("*-auto-author-%s-c*.log" % base)]
    except OSError:
        pass
    for n in names:
        m = re.search(r"auto-author-%s-c(\d+)(?:\.log)?$" % re.escape(base), n)
        if m:
            seen.append(int(m.group(1)))
    return "auto-author-%s-c%d" % (base, max(seen) + 1)


def heal_prompt(prev_prompt, review_text):
    """PURE. The stuck round's prompt with the review's findings attached (replacing
    any findings section an earlier heal round attached)."""
    p = str(prev_prompt or "")
    if _REVIEW_NOTES_HEAD in p:
        p = p.split("\n" + _REVIEW_NOTES_HEAD, 1)[0].rstrip()
    return (p + "\n\n" + _REVIEW_NOTES_HEAD + "\n"
            "An automated review of the round that got stuck diagnosed it as below. It is "
            "a DIAGNOSIS, not ground truth: confirm it against the self-check output "
            "(`python3 auto-harness-check.py`) before acting, and if the real failure is "
            "elsewhere (refimpl.py, a fake's shape), fix that instead. Never edit the "
            "target file directly -- the fix belongs in refimpl.py.\n\n"
            + str(review_text or "").strip()[:2500] + "\n")


def _queue_enqueue(argv):
    r = subprocess.run([sys.executable, str(QUEUE_PY), "enqueue"] + argv,
                       capture_output=True, text=True, timeout=180)
    m = re.search(r"enqueued\s+([0-9a-f]{8,})", r.stdout or "")
    return (m.group(1) if m else None), ((r.stdout or "") + (r.stderr or ""))[-400:]


def _queue_resolve(jid):
    r = subprocess.run([sys.executable, str(QUEUE_PY), "resolve", jid],
                       capture_output=True, text=True, timeout=60)
    return r.returncode == 0


def _ev():
    """The verdict/config module (escalation_verdict.py, beside this file)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import escalation_verdict as ev
    return ev


_CHAIN_PREFIX_RE = re.compile(r"^(?:needs-opus-)?(?:auto-(?:author|refine)-)?")


def run_label_candidates(label):
    """PURE. Names an auto-run argv record may be filed under for a queue label:
    the label minus its job decorations (kept -rN first, then fully stripped)."""
    s = _CHAIN_PREFIX_RE.sub("", str(label or ""))
    out = [s]
    for rx in (r"-(?:c\d+|esc)$",):
        s2 = re.sub(rx, "", s)
        if s2 not in out:
            out.append(s2)
    b = label_base(label)
    if b not in out:
        out.append(b)
    return [x for x in out if x]


def job_failure_sig(row):
    ev = _ev()
    return ev.failure_signature(row.get("failure_class"), row.get("failure_detail"),
                                row.get("terminal_reason"))


def chain_signatures(job, jobs=None, log_dir=None, limit=12):
    """MECHANICAL signature history of the authoring chain ending at `job`, oldest
    first, ending with `job`'s own: walks `continues` back through live rows and the
    done.json sidecars (failure_class / failure_detail / terminal_reason)."""
    jobs = jobs or []
    by_id = {j.get("id"): j for j in jobs}
    sigs, seen, cur = [], set(), job
    while cur and cur.get("id") not in seen and len(sigs) < limit:
        seen.add(cur.get("id"))
        row = cur
        if not row.get("failure_class"):
            row = dict(_sidecar(cur.get("id"), log_dir) or {}, **{k: v for k, v in cur.items() if v})
        sigs.append(job_failure_sig(row))
        nxt = cur.get("continues")
        cur = by_id.get(nxt) or (_sidecar(nxt, log_dir) if nxt else None)
    return list(reversed(sigs))


def plan_job_action(job, verdict, signatures, cfg, rec=None, source=None):
    """PURE. The action for one stuck authoring row.
      verdict   : 'a'|'b'|'c'|'d'    signatures: chain history, oldest->newest (current last)
      returns   : {"kind": resume|close|continue|bigger|respec|reslice|park, "why": str}
    The research rule: the SAME mechanical failure signature N times (default 2) means
    retrying is pointless -> re-spec. A capped re-spec that is spent parks (digest)."""
    rec = rec or {}
    cur = signatures[-1] if signatures else None
    n_same = _ev().same_signature_count(signatures, cur)
    n_abandon = int(cfg.get("abandon_same_signature_n") or 2)
    respecs = len(rec.get("respecs") or [])
    can_respec = respecs < int(cfg.get("respec_max_per_chain") or 1)
    has_model = bool(job.get("model")) and bool(job.get("task_file") or job.get("cwd"))
    repeat = n_same >= n_abandon
    attempts = len(signatures)

    def respec_or_park(why):
        if can_respec:
            return {"kind": "respec", "why": why}
        return {"kind": "park", "why": why + "; the re-spec budget is spent (%d)" % respecs}

    if repeat:
        return respec_or_park("same failure signature %d times (>= %d): not retrying, re-spec"
                              % (n_same, n_abandon))
    if attempts > int(cfg.get("max_attempts_per_task") or 3):
        return respec_or_park("%d attempts on this task (cap %s): re-spec"
                              % (attempts, cfg.get("max_attempts_per_task")))
    if verdict == "a":
        if source == "mechanical":
            return {"kind": "resume" if has_model else "respec",
                    "why": "not a real failure (operator stop/pause): resume the authoring"}
        return {"kind": "close", "why": "reviewer says already satisfied: verify, then close"}
    if verdict == "d":
        bm = cfg.get("bigger_model")
        tried = any(r.get("kind") == "bigger" for r in rec.get("rounds") or [])
        if bm and has_model and job.get("model") != bm and not tried:
            return {"kind": "bigger", "why": "model incapable: one attempt on %s" % bm}
        return respec_or_park("model incapable and no bigger model configured: re-slice")
    # b / c
    if not has_model:
        return respec_or_park("no runnable job to continue (placeholder row): re-spec")
    return {"kind": "continue",
            "why": "verdict %s: one %s round with the findings attached"
                   % (verdict, "harness-repair" if verdict == "c" else "re-author")}


def _launch_detached(cmd, cwd, log):
    log.parent.mkdir(parents=True, exist_ok=True)
    fh = open(log, "a")
    p = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                         stdin=subprocess.DEVNULL, start_new_session=True,
                         env=dict(os.environ, PYTHONUNBUFFERED="1"))
    return p.pid


REWRITE_CACHE = ESC_DIR / "intent-rewrite-cache.json"


def _claude_sonnet(prompt, timeout=420):
    env = dict(os.environ, CLAUDE_HEADLESS_DIAGNOSIS="1")
    r = subprocess.run(["claude", "-p", "--model", "sonnet", "--tools", "", "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
    return r.stdout or ""


def rewrite_intent_argv(argv, reason, notes, rewriter=None, cache_path=None):
    """The argv with its --intent rewritten by ONE Sonnet call that sees the old intent and
    the mechanical diagnosis. Cached by input hash (never re-asked). Returns argv unchanged
    when anything is off (no --intent, empty/short answer, no claude)."""
    ev = _ev()
    if ev.actions_killed() or "--intent" not in argv:
        return argv
    i = argv.index("--intent")
    old = argv[i + 1]
    key = ev.input_hash("intent-v1", old, reason, notes)
    cp = Path(cache_path or REWRITE_CACHE)
    cache = _load(cp) or {}
    new = cache.get(key)
    if not new:
        prompt = ("A dispatch-auto run failed. Rewrite its INTENT so the next run can succeed. "
                  "Keep every symptom fact, MUST HOLD clause and literal identifier; change only "
                  "what the diagnosis says is wrong (e.g. if a file to CREATE already exists, say "
                  "MODIFY/EXTEND it explicitly). Output ONLY the new intent text, no preface.\n\n"
                  "DIAGNOSIS:\n%s\n\nOLD INTENT:\n%s\n" % ((reason or notes)[:1500], old))
        new = ((rewriter or _claude_sonnet)(prompt) or "").strip()
        if len(new) < max(80, len(old) // 3) or new.lower().startswith(("i can't", "i cannot", "sorry")):
            return argv
        cache[key] = new
        try:
            _save_json(cp, cache)
        except OSError:
            pass
    out = list(argv)
    out[i + 1] = new
    return out


def _save_json(p, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, p)


def respec_argv(argv, new_label, notes, reslice=False):
    """PURE. The recorded dispatch-auto argv re-labelled, with the diagnosis appended to
    --interface (added when absent) and --auto-slice when re-slicing. Nothing else moves."""
    out, i, seen_iface = [], 0, False
    argv = list(argv)
    while i < len(argv):
        a = argv[i]
        if a == "--label" and i + 1 < len(argv):
            out += ["--label", new_label]
            i += 2
            continue
        if a == "--interface" and i + 1 < len(argv):
            out += ["--interface", argv[i + 1].rstrip() + "\n\n" + notes]
            seen_iface = True
            i += 2
            continue
        if a in ("--resume-harness", "--resume-author", "--auto-slice", "--no-auto-slice"):
            i += 1
            continue
        out.append(a)
        i += 1
    if not seen_iface:
        out += ["--interface", notes]
    if "--label" not in out:
        out += ["--label", new_label]
    out.append("--auto-slice" if reslice else "--no-auto-slice")
    return out


def respec_notes(job, review_text, why):
    ev = _ev()
    v = ev.parse_structured(review_text) or {}
    body = (v.get("why") or "").strip()
    if not body:
        t = str(review_text or "")
        if "</think>" in t:
            t = t.rsplit("</think>", 1)[1]
        body = t.strip()[-900:]
    return ("RE-SPEC NOTES (automated, %s): the previous authoring attempt(s) failed with the "
            "same mechanical signature (class=%s detail=%s). Do NOT repeat that approach. %s\n"
            "Diagnosis: %s\nAuthor a harness whose fixture FAILS at baseline and passes only "
            "with the reference implementation; fill every TODO and freeze the literals."
            % (time.strftime("%Y-%m-%d", time.gmtime()), job.get("failure_class") or "?",
               str(job.get("failure_detail") or "?")[:120], why, body[:1200]))


def respec_job(job, review_text, why, reslice=False, ledger_path=None, runs_dir=None,
               launch=None, now=None, decisions=None, resolve=None, ctx=None, alive=None,
               intent_rewriter=None):
    """Re-spec a stuck authoring row: relaunch dispatch-auto from the recorded argv under
    a NEW label (`<run>-rsN`, same bundle) with the diagnosis in --interface, then resolve
    the old needs_opus row (superseded). Capped per chain and per day (config). Returns
    'respec:<label> pid N' | 'skip:<why>' | 'park:<why>'."""
    ev = _ev()
    cfg = ev.load_config()
    lbl = str(job.get("label") or "")
    led = _load(Path(ledger_path or HEAL_LEDGER)) or {}
    jl = led.get("jobs") or {}
    key = job_chain_key(job)
    rec = (jl.get("chains") or {}).get(key) or {}
    root = label_base(lbl)
    n_root = len([r for r in jl.get("respec_log") or [] if r.get("root") == root])
    if n_root >= int(cfg["respec_max_per_chain"]):
        return "park:re-spec budget spent for %s (%d/%s)" % (root, n_root, cfg["respec_max_per_chain"])
    day = _utc_day(now)
    if len([r for r in jl.get("respec_log") or [] if r.get("day") == day]) >= int(cfg["respec_max_per_day"]):
        return "park:re-spec daily cap %s" % cfg["respec_max_per_day"]
    argrec, run_label = None, None
    for cand in run_label_candidates(lbl):
        r = _load(Path(runs_dir or AUTO_RUNS) / "argv" / ("%s.json" % cand))
        if isinstance(r, dict) and r.get("argv"):
            argrec, run_label = r, cand
            break
    if not argrec:
        return "park:no recorded dispatch-auto argv for %s (cannot re-spec mechanically)" % lbl
    if auto_driver_live(run_label, job.get("bundle"), runs_dir, alive):
        return "skip:driver for %s is alive" % run_label
    _m = re.search(r"-rs(\d+)$", run_label)
    n = max(n_root, int(_m.group(1)) if _m else 0) + 1     # never reuse a label (= worktree)
    new_label = "%s-rs%d" % (re.sub(r"-rs\d+$", "", run_label), n)
    notes = respec_notes(job, review_text, why)
    argv2 = list(argrec["argv"])
    # The diagnosis often says the INTENT itself is wrong (e.g. SPEC_DEFECT: creation target
    # exists at base). Re-launching the same intent just fails the same way, so ONE Sonnet
    # call (headless, no tools, cached by input hash) rewrites the --intent. Never under
    # the verify sandbox / kill switch; a refusal or junk answer keeps the old intent.
    if intent_rewriter is not False and os.environ.get("DISPATCH_VERIFY_SANDBOX") != "1":
        try:
            argv2 = rewrite_intent_argv(argv2, (job.get("escalation") or {}).get("reason") or "",
                                        notes, intent_rewriter)
        except Exception:
            pass
    cmd = [sys.executable, str(AUTO_TOOL)] + respec_argv(argv2, new_label, notes, reslice)
    if launch is None:
        return "skip:old auto driver retired (use mini-swe); respec not launched"
    if launch is None:
        if os.environ.get("DISPATCH_VERIFY_SANDBOX") == "1":
            return "skip:DISPATCH_VERIFY_SANDBOX=1 (no launch)"
        if ledger_path is not None and Path(ledger_path).resolve() != Path(HEAL_LEDGER).resolve():
            return "skip:non-production ledger %s (no launch)" % ledger_path
        launch = _launch_detached
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now) if now else time.gmtime())
    log = Path(runs_dir or AUTO_RUNS) / "logs" / ("%s-%s.log" % (new_label, ts))
    cwd = argrec.get("cwd") if argrec.get("cwd") and Path(str(argrec["cwd"])).is_dir() else str(Path.home())
    pid = launch(cmd, cwd, log)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now) if now else time.gmtime())

    def _rec(j):
        c = j["chains"].setdefault(key, {"rounds": []})
        c.setdefault("respecs", []).append({"label": new_label, "for": job.get("id"), "at": stamp,
                                            "why": why[:200], "pid": pid, "reslice": bool(reslice)})
        j.setdefault("respec_log", []).append({"day": day, "at": stamp, "chain": key,
                                               "label": new_label, "root": root})
        j["respec_log"] = j["respec_log"][-200:]
    _job_ledger_update(_rec, ledger_path)
    log_decision(job.get("bundle") or "job", lbl, "respec", "launched",
                 "%s -> %s (pid %s, log %s)" % (why, new_label, pid, log), path=decisions)
    if ctx:
        try:
            with open(ctx, "a") as fh:
                fh.write("\n- self-heal %s: RE-SPEC (%s) -> dispatch-auto relaunched as %s (pid %s)\n"
                         % (stamp, why, new_label, pid))
        except OSError:
            pass
    if job.get("id"):
        try:
            (resolve or _queue_resolve)(job["id"])
        except Exception:
            pass
    return "respec:%s pid %s" % (new_label, pid)


def verified_close(job, runner=None):
    """(ok, detail) -- a verdict (a) on a JOB is trusted only if the job's own harness
    self-check passes in its worktree (auto-harness-check.py exits 0)."""
    cwd = job.get("cwd")
    if not cwd or not Path(cwd, "auto-harness-check.py").is_file():
        return False, "no auto-harness-check.py in %s" % cwd
    run = runner or (lambda: subprocess.run([sys.executable, "auto-harness-check.py"], cwd=cwd,
                                            capture_output=True, text=True, timeout=900))
    try:
        r = run()
        return r.returncode == 0, "auto-harness-check rc=%s" % r.returncode
    except Exception as exc:
        return False, "auto-harness-check error %s" % type(exc).__name__


# --- GROUNDING GATE (2026-10-11) ------------------------------------------------------
# The reviewer's b/c/d verdict used to be acted on as-is. Two wrong ones were (2f4d18945f75:
# "bare names resolve to the original function" -- really a NameError swallowed by a bare
# except; cardlink s2: a byte-literal mismatch already removed). escalation_grounding.py
# verifies every cited file:line + quote. An UNVERIFIED reviewer verdict is NOT acted on:
#   1st time -> ONE fresh re-diagnosis (the stale review is set aside, the watcher's
#               announce-once key is dropped so its next pass re-reviews with the grounded
#               prompt; nothing else is touched), action "wait:UNVERIFIED ...";
#   2nd time -> park for a human (needs-opus) with the UNVERIFIED label in the reason/ctx.
# Only reviews carrying the watcher's policy stamp are enforced; older review files, mechanical
# / default / reason verdicts and (a) behave exactly as before. ESC_GROUNDING=off disables.
GROUNDING_REVIEW_SOURCES = ("structured", "verdict-line", "inferred", "keyword")


def _eg():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import escalation_grounding as eg
    return eg


def _unannounce_prefix(prefix, esc_dir=None):
    """Drop the watcher's announced keys starting with `prefix` so its next pass re-reviews."""
    lp = Path(esc_dir or ESC_DIR) / "ledger.json"
    led = _load(lp)
    if not isinstance(led, dict):
        return 0
    before = list(led.get("announced") or [])
    led["announced"] = [k for k in before if not str(k).startswith(prefix)]
    n = len(before) - len(led["announced"])
    if n:
        tmp = lp.with_suffix(".tmp")
        tmp.write_text(json.dumps(led, indent=1))
        tmp.replace(lp)
    return n


def grounding_gate(review_text, ctx_text, scope, prefix, source, review_path=None,
                   ledger_path=None, esc_dir=None, decisions=None, label_=None):
    """-> (action_or_None, review_text_to_use, report). action None = proceed (the review is
    exempt / grandfathered / grounded; ungrounded claim lines are stripped from the text).
    'wait:...' = re-diagnosis requested; 'park:UNVERIFIED ...' = the caller parks for a human."""
    eg = _eg()
    if source not in GROUNDING_REVIEW_SOURCES or not eg.enforced(review_text):
        return None, review_text, None
    rep = eg.assess(review_text, ctx_text or "")
    if rep["status"] != "UNVERIFIED":
        return None, eg.sanitize(review_text, rep), rep
    lp = Path(ledger_path or HEAL_LEDGER)
    led = _load(lp) or {}
    g = led.setdefault("grounding", {}).setdefault(scope, {"rediag": 0})
    why = eg.label(rep)
    if int(g.get("rediag") or 0) < 1:
        g["rediag"] = 1
        g["at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        try:
            lp.parent.mkdir(parents=True, exist_ok=True)
            tmp = lp.with_suffix(".tmp")
            tmp.write_text(json.dumps(led, indent=1))
            tmp.replace(lp)
        except OSError:
            pass
        moved = None
        if review_path:
            try:
                rp = Path(review_path)
                moved = rp.with_name(rp.name[:-3] + ".unverified-1.md")
                rp.rename(moved)
            except OSError:
                moved = None
        n = _unannounce_prefix(prefix, esc_dir)
        log_decision(scope, label_ or scope, "grounding-gate", "rediagnose",
                     "%s; stale review set aside (%s), %d watcher key(s) re-armed" % (why, moved, n),
                     path=decisions)
        return ("wait:%s -- not acted on; ONE fresh re-diagnosis requested (%d key re-armed)"
                % (why, n), review_text, rep)
    log_decision(scope, label_ or scope, "grounding-gate", "parked-unverified", why, path=decisions)
    return "park:%s after a fresh re-diagnosis -- needs-opus, label UNVERIFIED" % why, review_text, rep


def _heal_job(job, review_text, jobs=None, ledger_path=None, slice_runs=None, now=None,
             enqueue=None, notifier=None, decisions=None, log_dir=None, ctx=None,
             respec=None, closer=None, verdict_cache=None, config=None, runs_dir=None,
             resolve=None, intent_rewriter=None):
    """Act on ONE needs_opus authoring job. Returns one word:
    'heal-continuation:<id>' | 'respec:<label> ...' | 'closed:<why>' | 'retired:<id>' |
    'wait:<why>' | 'skip:<why>' | 'park:final-rung'. The verdict ALWAYS drives an action
    (escalation_verdict.resolve never returns None); all side effects are injectable.
      a not a real failure  -> resume (operator stop) or verified close
      b spec wrong          -> continuation with findings; the same signature again -> re-spec
      c harness defect      -> harness-repair continuation; the same signature again -> re-spec
      d model incapable     -> ONE round on the configured bigger model, else re-slice"""
    ev = _ev()
    if ev.is_no_verdict_review(review_text):
        return "skip:no model verdict (review capped / no evidence) -- escalation stays open, no action taken"
    enqueue = enqueue or _queue_enqueue
    jobs = jobs if jobs is not None else _queue_jobs()
    ok, why = job_heal_eligible(job, slice_runs)
    if not ok:
        return "skip:" + why
    if ev.actions_killed():
        return "skip:ACTIONS-OFF kill switch set"
    led = _load(Path(ledger_path or HEAL_LEDGER)) or {}
    outcome, cid = continuation_outcome(job, jobs, led, log_dir)
    if outcome == "live":
        return "wait:continuation %s is live" % cid
    if outcome == "passed":
        return "skip:continuation %s already passed (the sweep retires it)" % cid
    cfg = config or ev.load_config()
    vd = ev.resolve(review_text, job.get("failure_class"),
                    (job.get("escalation") or {}).get("reason"),
                    cache_path=verdict_cache, key_parts=(job.get("id"),))
    verdict = vd["verdict"]
    if verdict in ("b", "c", "d") and vd["source"] in GROUNDING_REVIEW_SOURCES:
        try:
            _cx = Path(ctx).read_text(errors="replace") if ctx else ""
        except OSError:
            _cx = ""
        _ga, review_text, _gr = grounding_gate(
            review_text, _cx, "job:%s" % job.get("id"), "job:%s:" % job.get("id"), vd["source"],
            review_path=getattr(_heal_job, "_review_path", None)
            or _find_job_review(job, Path(ledger_path).parent if ledger_path else None),
            ledger_path=ledger_path, esc_dir=Path(ledger_path).parent if ledger_path else None,
            decisions=decisions,
            label_=job.get("label"))
        if _ga and _ga.startswith("wait:"):
            return _ga
        if _ga and _ga.startswith("park:"):
            return _park_final(job, job_chain_key(job), _ga[5:], notifier, decisions, ledger_path, now)
    key = job_chain_key(job)
    rec = ((led.get("jobs") or {}).get("chains") or {}).get(key) or {}
    _root = label_base(job.get("label"))
    rec = dict(rec, respecs=[r for r in (led.get("jobs") or {}).get("respec_log") or []
                             if r.get("root") == _root])   # the cap is per ROOT task, not per worktree
    sigs = chain_signatures(job, jobs, log_dir)
    plan = plan_job_action(job, verdict, sigs, cfg, rec, source=vd["source"])
    kind = plan["kind"]
    if kind == "close":
        okc, det = verified_close(job, closer)
        if okc:
            try:
                (resolve or _queue_resolve)(job["id"])
            except Exception:
                pass
            log_decision(job.get("bundle") or "job", job.get("label"), "job-close", "closed", det,
                         path=decisions)
            return "closed:%s" % det
        kind, plan = "continue", {"kind": "continue", "why": "(a) not verified (%s): treated as (b)" % det}
        verdict = "b"
        if not (job.get("model") and job.get("cwd")):
            kind = "respec"
    if kind in ("respec", "reslice"):
        act = (respec or respec_job)(job, review_text, plan["why"], reslice=(verdict == "d"),
                                     ledger_path=ledger_path, now=now, decisions=decisions,
                                     ctx=ctx, runs_dir=runs_dir, intent_rewriter=intent_rewriter)
        if act.startswith("park:"):
            return _park_final(job, key, act[5:], notifier, decisions, ledger_path, now)
        return act
    if kind == "park":
        return _park_final(job, key, plan["why"], notifier, decisions, ledger_path, now)
    if any(j.get("status") in _LIVE and j.get("cwd") == job.get("cwd") for j in jobs):
        return "wait:another job is live on %s" % job.get("cwd")
    ok, why = job_heal_budget(led, job, now)
    if not ok:
        # the continuation budget is spent: that is itself "retrying does not help" -> re-spec
        if (respec or respec_job) and len(rec.get("respecs") or []) < int(cfg["respec_max_per_chain"]):
            act = (respec or respec_job)(job, review_text, "continuation budget spent (%s)" % why,
                                         reslice=(verdict == "d"), ledger_path=ledger_path, now=now,
                                         decisions=decisions, ctx=ctx, runs_dir=runs_dir,
                                         intent_rewriter=intent_rewriter)
            if not act.startswith("park:"):
                return act
            why = act[5:]
        return _park_final(job, key, why, notifier, decisions, ledger_path, now, job_cap=True)
    tf = Path(str(job.get("task_file") or Path(job["cwd"]) / "AUTO-TASK.md"))
    try:
        prev = tf.read_text()
    except OSError:
        prev = ""
    label = _next_c_label(job, jobs, log_dir)
    pf = Path(job["cwd"]) / "AUTO-TASK.md"
    pf.write_text(heal_prompt(prev, review_text))
    model = str(job.get("model"))
    host = str(job.get("host_pref") or "studio")
    if kind == "bigger":
        model = str(cfg["bigger_model"])
        host = str(cfg.get("bigger_host") or host)
    argv = ["--model", model, "--host", host,
            "--cwd", str(job["cwd"]), "--task-file", str(pf), "--task-kind", "coding",
            "--verify", str(job.get("verify") or "python3 auto-harness-check.py"),
            "--max-iters", str(job.get("max_iters") or 24), "--label", label,
            "--continues", str(job.get("id"))]
    ncx = cfg.get("bigger_num_ctx") if kind == "bigger" else job.get("num_ctx")
    if ncx:
        argv += ["--num-ctx", str(ncx)]
    if job.get("bundle"):
        argv += ["--bundle", str(job["bundle"])]
    new_id, out = enqueue(argv)
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now) if now else time.gmtime())
    if not new_id:
        log_decision(job.get("bundle") or "job", job.get("label"), "job-heal",
                     "enqueue-failed", out, path=decisions)
        return "skip:enqueue failed: %s" % " ".join(out.split())[-160:]

    def _rec(jl):
        c = jl["chains"].setdefault(key, {"rounds": []})
        c["rounds"].append({"job": new_id, "for": job.get("id"), "at": stamp,
                            "verdict": verdict, "label": label, "kind": kind,
                            "model": model, "sig": sigs[-1] if sigs else None})
        c["last_at"] = stamp
        jl["log"].append({"day": _utc_day(now), "at": stamp, "bundle": job.get("bundle"),
                          "chain": key, "job": new_id})
    _job_ledger_update(_rec, ledger_path)
    log_decision(job.get("bundle") or "job", job.get("label"), "job-heal",
                 "launched", "verdict %s (%s) %s -> continuation %s (%s, model %s) --continues %s"
                 % (verdict, vd["source"], kind, new_id, label, model, job.get("id")),
                 path=decisions)
    if ctx:
        try:
            with open(ctx, "a") as fh:
                fh.write("\n- self-heal %s: verdict %s (%s) -> %s: continuation authoring round "
                         "%s (%s, model %s) --continues %s\n"
                         % (stamp, verdict, vd["source"], plan["why"], new_id, label, model,
                            job.get("id")))
        except OSError:
            pass
    return "heal-continuation:%s" % new_id


ACTED_PREFIXES = ("heal-continuation:", "respec:", "closed:", "retired:", "resumed:", "ladder-exit:")


def close_index_rows(jid, action, index=None, log_path=None):
    """The ACTION handled this escalation: tick every OPEN index row that names job `jid`
    (its D row and the bundle's PARKED row) with the same-length in-place flip, and record
    the action taken in ESCALATIONS-AUTOCLOSE.jsonl (row, rule, evidence). Never raises."""
    closed = []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import escalation_index_janitor as eij
        p = Path(index or (ESC_DIR / "ESCALATIONS.md"))
        data = p.read_bytes()
        off = 0
        for raw in data.split(b"\n"):
            line = raw.decode("utf-8", "replace")
            if line.startswith("- [ ] ") and jid in line:
                if eij._flip(p, off, line):
                    closed.append(line)
            off += len(raw) + 1
        lp = Path(log_path or (p.parent / "ESCALATIONS-AUTOCLOSE.jsonl"))
        with lp.open("a") as fh:
            for line in closed:
                fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                     "file": str(p), "row": line,
                                     "rule": "action taken: %s" % action}) + "\n")
    except Exception:
        pass
    return closed


def heal_job(job, review_text, *a, close_rows=True, index=None, **kw):
    """_heal_job + auto-close: an ACTED outcome ticks the escalation's index rows and
    records the action (nobody has to read ESCALATIONS.md)."""
    out = _heal_job(job, review_text, *a, **kw)
    lp = kw.get("ledger_path")
    # TIER LADDER (2026-10-12): "park for a human" and "not an auto-author job" are not exits any
    # more -- the row walks local reviewer -> Sonnet -> (tooling only) Opus and ends in an
    # action or a NAMED exit (pipeline_ladder). Production only (ladder_enabled).
    if ladder_enabled(lp) and str(out).startswith(("park:", "skip:not an auto-author")):
        try:
            out = ladder_run(job, review_text, kw.get("jobs"), ledger_path=lp, close_rows=False)
        except Exception as e:      # noqa: BLE001 -- advisory: never break the pass
            out = "%s [ladder error %s]" % (out, type(e).__name__)
    prod = lp is None or Path(lp).resolve() == Path(HEAL_LEDGER).resolve()
    if close_rows and prod and index is None and os.environ.get("DISPATCH_VERIFY_SANDBOX") == "1":
        prod = False                      # never touch the real index from a sandboxed test
    if close_rows and (prod or index is not None) and str(out).startswith(ACTED_PREFIXES) and job.get("id"):
        close_index_rows(job["id"], out, index=index)
    return out


def _park_final(job, key, why, notifier, decisions, ledger_path, now, job_cap=False):
    """The only rung that parks for a human: ONE deduped alert + a ledger final_at."""
    def _fin(jl):
        jl["chains"].setdefault(key, {"rounds": []})["final_at"] = time.strftime(
            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    _job_ledger_update(_fin, ledger_path)
    log_decision(job.get("bundle") or "job", job.get("label"), "job-heal-cap",
                 "parked-for-human", why, path=decisions)
    if ladder_enabled(ledger_path) and notifier is None:
        return "park:final-rung"       # the ladder (heal_job) takes the row from here: no human alert
    msg = ("%s (%s) needs a human: %s. Inspect: python3 ~/bin/ollama-queue.py needs-opus; "
           "then resolve/cancel %s." % (job.get("label"), job.get("id"), why, job.get("id")))
    try:
        if notifier is None:
            import importlib.util as _ilu
            _s = _ilu.spec_from_file_location(
                "notify_owner", Path(__file__).resolve().parent / "notify-owner.py")
            _m = _ilu.module_from_spec(_s)
            _s.loader.exec_module(_m)
            notifier = _m.notify
        dk = ("job-heal-daily-cap:" + _utc_day(now)) if "daily cap" in why else (
            "job-heal-cap:" + key)
        notifier("dispatch needs you", msg, dedupe_key=dk, dedupe_s=24 * 3600)
    except Exception:
        pass
    return "park:final-rung"


def job_row_state(job, jobs, ledger=None, now=None, log_dir=None, slice_runs=None):
    """For the queue's bundle commitment, per needs_opus row, every tick (CHEAP: reads
    only the sidecars of continuations NAMED for this row, never the whole log dir):
      'cleared' -- a named continuation passed (done, exit 0): the row is handled and
                   must not park its bundle (the sweep resolves it within one pass);
      'pending' -- a continuation is live, or the job ladder will still act on it
                   (job_heal_pending): the bundle is WORKING, not parked;
      None      -- stuck: only a human can clear it."""
    try:
        if ledger is None:
            ledger = _load(HEAL_LEDGER) or {}
        rec = ((ledger.get("jobs") or {}).get("chains") or {}).get(job_chain_key(job)) or {}
        named = {x for x in [job.get("superseded_by")] + list(job.get("continued_by") or [])
                 if x}
        named |= {r.get("job") for r in rec.get("rounds") or [] if r.get("job")}
        named |= {j.get("id") for j in jobs or [] if j.get("continues") == job.get("id")}
        sc = [s for s in (_sidecar(x, log_dir) for x in named if x) if s]
        jj = [j for j in jobs or [] if j.get("id") in named]
        outcome, _cid = continuation_outcome(job, jj, ledger, log_dir, sidecars=sc,
                                             only=named)
        if outcome == "passed":
            return "cleared"
        if outcome == "live":
            return "pending"
        # the window restarts when the last heal round FAILED (its sidecar time), not
        # when it was enqueued -- a 25-min round must not eat the sweep's window
        _since = max([_parse_ts(x.get("persisted_at")) or 0 for x in sc] or [0])
        if job_heal_pending(job, ledger, now, slice_runs, since=_since):
            return "pending"
    except Exception:
        return None
    return None


def _find_job_review(job, esc_dir=None):
    """The newest watcher review FILE (path) for a job escalation, or None."""
    try:
        c = sorted(Path(esc_dir or ESC_DIR).glob("*-job-%s.review.md" % job.get("id")))
        return str(c[-1]) if c else None
    except OSError:
        return None


def _job_review(job, esc_dir=None):
    """The newest watcher review text for a job escalation, or ''."""
    d = Path(esc_dir or ESC_DIR)
    try:
        cands = sorted(d.glob("*-job-%s.review.md" % job.get("id")))
    except OSError:
        return ""
    try:
        return cands[-1].read_text() if cands else ""
    except OSError:
        return ""


# --- RESUME the dispatch-auto driver once its harness converges (2026-10-05) --------
# Retiring the needs_opus row is only half the job: rt-bg-commitments-fix getcommitments
# had a CONVERGED harness (c2 3f75be3df79a passed) and still nothing ran preflight on it,
# because the dispatch-auto driver had exited at the park. When a continuation passes,
# relaunch that driver with --resume-harness (it re-proves VERIFY_OK, then runs the
# bounded preflight/refine loop and stops at GO for the relevance review). Local models
# only; never Opus. Caps: once per passed continuation, RESUME_MAX_PER_LABEL_DAY per
# run label per UTC day, RESUME_MAX_PER_DAY overall.
RESUME_MAX_PER_LABEL_DAY = 2
RESUME_MAX_PER_DAY = 6
AUTO_TOOL = Path(__file__).resolve().parent / "ollama-dispatch-auto"
AUTO_RUNS = Path.home() / ".ollama-dispatch" / "auto-runs"


def _pid_alive(pid):
    try:
        os.kill(int(pid), 0)
        return True
    except (OSError, ValueError, TypeError):
        return False


def _driver_alive(pid):
    """A live pid that is actually an ollama-dispatch-auto process. A bare pid probe
    is fooled by pid reuse: 2026-10-06 the chain record of aw-sched-routes-s15 (driver
    gone since 2026-09-25, never wrote `ended`) pointed at pid 1174, now macOS's
    diagnostics_agent, so the run read as "driver live" forever and was never resumed."""
    if not _pid_alive(pid):
        return False
    try:
        r = subprocess.run(["ps", "-o", "command=", "-p", str(int(pid))],
                           capture_output=True, text=True, timeout=15)
        return "ollama-dispatch-auto" in (r.stdout or "")
    except Exception:
        return True   # cannot tell -> keep the old (pid-only) answer


def auto_driver_live(label, bundle=None, runs_dir=None, alive=None):
    """Is a dispatch-auto driver for run `label` alive right now? Reads the bundle's
    shared chain record (runs[label]) and the legacy single-run shape."""
    alive = alive or _driver_alive
    for key in [k for k in (bundle, label) if k]:
        d = _load(Path(runs_dir or AUTO_RUNS) / ("%s.json" % key))
        if not isinstance(d, dict):
            continue
        recs = list((d.get("runs") or {}).values()) if isinstance(d.get("runs"), dict) else []
        recs.append(d)
        for r in recs:
            if (isinstance(r, dict) and r.get("label") == label and r.get("phase") != "ended"
                    and r.get("pid") and alive(r.get("pid"))):
                return True
    return False


def resume_budget(ledger, label, now=None):
    """PURE. (ok, why) for one more driver resume of run `label`."""
    day = _utc_day(now)
    log = [r for r in (((ledger or {}).get("jobs") or {}).get("resumes") or [])
           if r.get("day") == day]
    if len(log) >= RESUME_MAX_PER_DAY:
        return False, "resume daily cap %d/%d (%s UTC)" % (len(log), RESUME_MAX_PER_DAY, day)
    n = sum(1 for r in log if r.get("label") == label)
    if n >= RESUME_MAX_PER_LABEL_DAY:
        return False, "resume cap for %s: %d/%d today" % (label, n, RESUME_MAX_PER_LABEL_DAY)
    return True, "ok"


def prefetch_register(label, kind, why, bundle=None, mod=None):
    """Register `label` with cpu-prefetch's backlog. Idempotent, never raises; returns a status string."""
    try:
        if mod is None:
            import importlib.util as _ilu
            _s = _ilu.spec_from_file_location(
                "cpu_prefetch", str(Path(__file__).resolve().parent / "cpu-prefetch.py"))
            mod = _ilu.module_from_spec(_s)
            _s.loader.exec_module(mod)
        return mod.register(label, None, kind, bundle, source="self-heal: " + why)
    except BaseException as e:
        if isinstance(e, KeyboardInterrupt):
            raise
        return "error:%s" % type(e).__name__


def resume_auto_driver(job, cid, ledger_path=None, runs_dir=None, launch=None, now=None,
                       decisions=None, slice_runs=None, alive=None, notifier=None):
    """After continuation `cid` of needs_opus auto-author `job` PASSED: relaunch its
    dispatch-auto driver with --resume-harness. Returns an action string. Idempotent
    per (label, cid); capped; refuses under DISPATCH_VERIFY_SANDBOX unless `launch`
    is injected (tests)."""
    lbl = str(job.get("label") or "")
    if not lbl.startswith("auto-author-"):
        return "skip:not an auto-author job"
    if plan_slice_for_label(lbl, slice_runs)[0]:
        return "skip:slice job (the slicer drives it)"
    run_label = label_base(lbl)
    led = _load(Path(ledger_path or HEAL_LEDGER)) or {}
    done = [r for r in ((led.get("jobs") or {}).get("resumes") or [])
            if r.get("label") == run_label and r.get("cid") == cid]
    if done:
        return "skip:already resumed for %s" % cid
    if auto_driver_live(run_label, job.get("bundle"), runs_dir, alive):
        return "skip:driver for %s is alive" % run_label
    rec = _load(Path(runs_dir or AUTO_RUNS) / "argv" / ("%s.json" % run_label))
    if not isinstance(rec, dict) or not rec.get("argv"):
        why = "no recorded argv for %s (driver predates argv records)" % run_label
        log_decision(job.get("bundle") or "job", lbl, "resume-driver", "unavailable", why,
                     path=decisions)
        return "skip:" + why
    ok, why = resume_budget(led, run_label, now)
    if not ok:
        log_decision(job.get("bundle") or "job", lbl, "resume-driver", "capped", why,
                     path=decisions)
        try:
            if notifier is None:
                import importlib.util as _ilu
                _s = _ilu.spec_from_file_location(
                    "notify_owner", Path(__file__).resolve().parent / "notify-owner.py")
                _m = _ilu.module_from_spec(_s)
                _s.loader.exec_module(_m)
                notifier = _m.notify
            notifier("dispatch resume capped",
                     "%s: harness converged but the driver resume is capped (%s). Resume "
                     "by hand (old auto driver retired -- use mini-swe)" % (run_label, why),
                     dedupe_key="resume-cap:" + run_label, dedupe_s=24 * 3600)
        except Exception:
            pass
        return "park:" + why
    cmd = [sys.executable, str(AUTO_TOOL)] + list(rec["argv"]) + ["--resume-harness"]
    if launch is None:
        return "skip:old auto driver retired (use mini-swe); resume not launched"
    if launch is None:
        if os.environ.get("DISPATCH_VERIFY_SANDBOX") == "1":
            return "skip:DISPATCH_VERIFY_SANDBOX=1 (no launch)"
        if ledger_path is not None and Path(ledger_path).resolve() != Path(HEAL_LEDGER).resolve():
            # a caller with a private ledger is a test/fixture: never launch a REAL driver
            return "skip:non-production ledger %s (no launch)" % ledger_path

        def launch(cmd, cwd, log):
            log.parent.mkdir(parents=True, exist_ok=True)
            fh = open(log, "a")
            p = subprocess.Popen(cmd, cwd=cwd, stdout=fh, stderr=subprocess.STDOUT,
                                 stdin=subprocess.DEVNULL, start_new_session=True,
                                 env=dict(os.environ, PYTHONUNBUFFERED="1"))
            return p.pid
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    log = Path(runs_dir or AUTO_RUNS) / "logs" / ("%s-resume-%s.log" % (run_label, ts))
    cwd = rec.get("cwd") if rec.get("cwd") and Path(rec["cwd"]).is_dir() else str(Path.home())
    try:
        pid = launch(cmd, cwd, log)
    except Exception as e:
        # The immediate relaunch failed (fork/disk/permission). Do not lose the converged harness:
        # hand it to the CPU-prefetch backlog, which retries it under its own gates (single authority
        # on whether it may start). Never raises into the sweep.
        st = prefetch_register(run_label, "resume", "self-heal resume launch failed: %s" % e,
                               bundle=job.get("bundle"))
        log_decision(job.get("bundle") or "job", lbl, "resume-driver", "deferred",
                     "relaunch of %s failed (%s: %s); prefetch backlog: %s"
                     % (run_label, type(e).__name__, e, st), path=decisions)
        return "deferred:%s" % st
    # the immediate path stays the default: a stuck chain must not wait on prefetch admission/headroom

    def _add(jl):
        jl.setdefault("resumes", []).append({
            "label": run_label, "cid": cid, "job": job.get("id"), "pid": pid,
            "day": _utc_day(now), "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
            "log": str(log)})
        jl["resumes"] = jl["resumes"][-200:]
    _job_ledger_update(_add, ledger_path)
    log_decision(job.get("bundle") or "job", lbl, "resume-driver", "launched",
                 "continuation %s passed -- relaunched the dispatch-auto driver for %s with "
                 "--resume-harness (pid %s, log %s)" % (cid, run_label, pid, log), path=decisions)
    return "resumed:%s pid %s" % (run_label, pid)


def job_heal_sweep(jobs=None, ledger_path=None, slice_runs=None, resolve=None, heal=None,
                   log_dir=None, esc_dir=None, decisions=None, sidecars=None, resume=None):
    """Every watcher pass. For each needs_opus row: a PASSED continuation -> resolve
    the row (its escalation/park rows then close via the index janitor); a FAILED
    heal round with budget left -> spend the next one (heal_job). Returns
    [(job_id, action)]."""
    resolve = resolve or _queue_resolve
    jobs = jobs if jobs is not None else _queue_jobs()
    led = _load(Path(ledger_path or HEAL_LEDGER)) or {}
    sc = sidecars if sidecars is not None else _all_sidecars(log_dir)
    out = []
    for j in list(jobs):
        if j.get("status") != "needs_opus" or j.get("awaiting_signoff"):
            continue
        outcome, cid = continuation_outcome(j, jobs, led, log_dir, sidecars=sc)
        if outcome == "passed":
            if resolve(j.get("id")):
                log_decision(j.get("bundle") or "job", j.get("label"), "retire-on-pass",
                             "resolved", "continuation %s passed (done, exit 0) -- the "
                             "needs_opus row %s is handled" % (cid, j.get("id")), path=decisions)
                out.append((j.get("id"), "retired:continuation %s passed" % cid))
                try:
                    act = (resume or resume_auto_driver)(
                        j, cid, ledger_path=ledger_path, decisions=decisions,
                        slice_runs=slice_runs)
                except Exception as e:
                    act = "skip:resume error %s" % type(e).__name__
                out.append((j.get("id"), act))
            continue
        if outcome == "failed":
            rec = ((led.get("jobs") or {}).get("chains") or {}).get(job_chain_key(j)) or {}
            if any(r.get("job") == cid for r in rec.get("rounds") or []):
                rv = _job_review(j, esc_dir)
                if rv:
                    act = (heal or heal_job)(j, rv, jobs=jobs, ledger_path=ledger_path,
                                             slice_runs=slice_runs, log_dir=log_dir,
                                             decisions=decisions)
                    out.append((j.get("id"), act))
    return out


# ----------------------------------------------------------------------------
# HYGIENE SWEEP (2026-10-06, seams 3b). Superseded esc-review rows burned (or would
# burn) a ~30 min GPU review on a question nobody asks any more, and dead ones sat
# in the worklist forever:
#   * 05b17e14a15a reviewed 77d808c3984a after its continuation 24123dd86140 was
#     already enqueued;
#   * 5e901735dc37 / f69fce37a51a (two reviews of ONE subject) failed at launch
#     after the retention cap pruned their task file (watcher fix: prune_escalations
#     now skips sets a live row reads) and stayed in the queue as `failed`;
#   * 0fb392936848 (sync-guard r1) was re-run as a `-v2` sibling in a NEW worktree,
#     which continuation_outcome (same label base + cwd) never matches.
# Only the queue's own verbs: `cancel <id> --automated` (job-scoped) for a PENDING
# review that never launched, `resolve <id>` for a finished row. Capped per day,
# logged to decisions.jsonl, one deduped alert per action batch.
HYGIENE_MAX_PER_DAY = 30
_ESC_REVIEW_RE = re.compile(r"^esc-review-(\d{8}T\d{6}Z)-job-([0-9a-f]{8,})$")
_STUCK = ("needs_opus", "escalated", "failed", "blocked")


def _sibling_version(subject, rows):
    """('passed'|'live', id) of a `<base>-vN` re-run of `subject` in the same bundle,
    enqueued/persisted after the subject, else (None, None). Passed wins."""
    base = label_base(subject.get("label"))
    t0 = (_parse_ts((subject.get("escalation") or {}).get("escalated_at"))
          or _parse_ts(subject.get("enqueued_at")) or 0)
    pat = re.compile(r"^%s-v\d+$" % re.escape(base))
    best = (None, None)
    for r in rows:
        if r.get("id") == subject.get("id") or not pat.match(label_base(r.get("label"))):
            continue
        if subject.get("bundle") and r.get("bundle") and r.get("bundle") != subject.get("bundle"):
            continue
        if (_parse_ts(r.get("enqueued_at")) or _parse_ts(r.get("persisted_at")) or 0) <= t0:
            continue
        if _passed(r):
            return "passed", r.get("id")
        if r.get("status") in _LIVE and best[0] is None:
            best = ("live", r.get("id"))
    return best


def superseded_review_actions(jobs, ledger=None, sidecars=None, runs=None):
    """PURE. [(verb, id, why)] with verb 'cancel' (a pending, never-launched review)
    or 'resolve' (a finished row). Never touches running rows, gate jobs, rows
    awaiting sign-off, or anything that is not an esc-review -- except a needs_opus
    SUBJECT whose `-vN` sibling re-run PASSED."""
    jobs = list(jobs or [])
    sc = list(sidecars or [])
    by_id = {j.get("id"): j for j in jobs}
    out, seen = [], set()

    def add(verb, jid, why):
        if jid not in seen:
            seen.add(jid)
            out.append((verb, jid, why))
    reviews = []
    for j in jobs:
        m = _ESC_REVIEW_RE.match(str(j.get("label") or ""))
        if m and not j.get("awaiting_signoff"):
            reviews.append((m.group(1), m.group(2), j))
    reviews.sort(key=lambda t: (t[0], t[2].get("enqueued_at") or ""))
    active_subject = {}
    for ts, sid, r in reviews:
        st = r.get("status")
        never_ran = st == "pending" and not r.get("launched_at") and not r.get("pid")
        subj = by_id.get(sid)
        if st == "failed" and str(r.get("error") or "").startswith("launch failed"):
            add("resolve", r["id"], "review never ran (launch failed: %s)"
                % str(r.get("error"))[15:90])
            continue
        if not never_ran:
            if st in ("running", "queued", "scheduled", "held"):
                active_subject.setdefault(sid, r["id"])
            continue
        if subj is None or subj.get("status") not in _STUCK:
            add("cancel", r["id"], "subject %s is no longer stuck (%s)"
                % (sid, "gone" if subj is None else subj.get("status")))
            continue
        oc, cid = continuation_outcome(subj, jobs, ledger, sidecars=sc)
        if oc in ("passed", "live"):
            add("cancel", r["id"], "subject %s already continued by %s (%s)" % (sid, cid, oc))
            continue
        vo, vid = _sibling_version(subj, jobs + sc)
        if vo in ("passed", "live"):
            add("cancel", r["id"], "subject %s re-run as sibling %s (%s)" % (sid, vid, vo))
            continue
        if sid in active_subject:
            add("cancel", r["id"], "duplicate: review %s of the same subject %s is older"
                % (active_subject[sid], sid))
            continue
        active_subject[sid] = r["id"]
    for j in jobs:
        if j.get("status") != "needs_opus" or j.get("awaiting_signoff"):
            continue
        vo, vid = _sibling_version(j, jobs + sc)
        if vo == "passed":
            add("resolve", j["id"], "re-run as sibling %s, which passed" % vid)
    if runs is not None:
        for verb, jid, why in passed_lineage_actions(jobs, runs, sc):
            add(verb, jid, why)
    return out


_LINEAGE_FAIL = ("failed", "needs_opus", "blocked", "done_unconverged")


def auto_run_passes(runs_dir=None):
    """[(label, bundle, ended_epoch)] for every ended auto-run record whose outcome is
    a clean `exit 0` (the chain PASSED). Reads ~/.ollama-dispatch/auto-runs/*.json
    (per-label `runs` map + the top-level latest record). Best-effort: unreadable -> []."""
    seen, out = set(), []
    try:
        files = sorted(Path(runs_dir or AUTO_RUNS).glob("*.json"))
    except OSError:
        return out
    for f in files:
        d = _load(f)
        if not isinstance(d, dict):
            continue
        for r in list((d.get("runs") or {}).values()) + [d]:
            if not isinstance(r, dict) or not r.get("label"):
                continue
            if r.get("phase") != "ended" or str(r.get("outcome") or "").strip() != "exit 0":
                continue
            t = _parse_ts(r.get("updated_at")) or _parse_ts(r.get("phase_since"))
            bundle = r.get("bundle") or r.get("key")
            k = (r["label"], bundle, t)
            if t and k not in seen:
                seen.add(k)
                out.append(k)
    return out


def _lineage_matches(label, chain_label):
    """PURE. True when queue `label` is a round of auto-run chain `chain_label`: the same
    label after stripping stage/round decoration, or a `-sN` sub-slice of it."""
    stem = label_base(label)
    return stem == chain_label or re.fullmatch(re.escape(chain_label) + r"-s\d+", stem) is not None


def _row_epoch(r):
    ts = [_parse_ts(r.get(k)) for k in ("enqueued_at", "launched_at", "finished_at",
                                        "ended_at", "persisted_at", "superseded_at")]
    ts = [t for t in ts if t]
    return max(ts) if ts else 0


def passed_lineage_actions(jobs, runs, sidecars=None):
    """PURE. [('resolve', id, why)] for failed/needs_opus/blocked queue rows (and the
    job-form esc-review rows about them) whose lineage PROVABLY passed later, so a
    superseded retry attempt no longer sits in Needs attention (the owner 2026-10-09,
    rt-bg-commitments-guard: 5 failed rows of a chain whose later round exited 0).
    Two proofs, nothing weaker:
      (1) an ended auto-run `exit 0` for chain L (same bundle) finished AFTER the row, and
          the row's label is L or an `L-sN` round of it;
      (2) the row's superseded_by/continued_by chain reaches a done exit-0 job.
    Never when the lineage still has a live row or a stuck row newer than the pass."""
    jobs = list(jobs or [])
    by_id = {j.get("id"): j for j in jobs}
    for r in sidecars or []:
        by_id.setdefault(r.get("id"), r)
    out, resolved = [], set()

    def reach_pass(row):
        seen, todo = {row.get("id")}, [row]
        while todo and len(seen) < 40:
            cur = todo.pop()
            nxt = [x for x in [cur.get("superseded_by")] + list(cur.get("continued_by") or []) if x]
            for n in nxt:
                if n in seen:
                    continue
                seen.add(n)
                nr = by_id.get(n)
                if nr is None:
                    continue
                if _passed(nr):
                    return n
                todo.append(nr)
        return None
    for j in jobs:
        if j.get("status") not in _LINEAGE_FAIL or j.get("awaiting_signoff"):
            continue
        lab = str(j.get("label") or "")
        if _ESC_REVIEW_RE.match(lab) or lab.startswith(("esc-review-", "gate-", "regate-", "secondop-")):
            continue
        t = _row_epoch(j)
        why = None
        cid = reach_pass(j)
        if cid:
            why = "superseded chain reached passing job %s" % cid
        for (L, bundle, end) in runs or []:
            if why:
                break
            if not _lineage_matches(lab, L) or t >= end:
                continue
            if j.get("bundle") and bundle and j.get("bundle") != bundle:
                continue
            mates = [x for x in jobs if x is not j and _lineage_matches(x.get("label"), L)
                     and (not x.get("bundle") or not bundle or x.get("bundle") == bundle)]
            if any((x.get("status") in _LIVE or x.get("status") in _LINEAGE_FAIL)
                   and _row_epoch(x) >= end for x in mates):
                continue
            if any(x.get("status") in _LIVE for x in mates):
                continue
            why = "chain %s ended exit 0 after this attempt" % L
        if why:
            out.append(("resolve", j["id"], why))
            resolved.add(j["id"])
    for j in jobs:
        m = _ESC_REVIEW_RE.match(str(j.get("label") or ""))
        if not m or j.get("awaiting_signoff") or j.get("status") not in _LINEAGE_FAIL:
            continue
        sid = m.group(2)
        if sid in resolved:
            out.append(("resolve", j["id"], "review of %s, whose lineage passed" % sid))
        elif sid not in {x.get("id") for x in jobs} and sid in by_id and _passed(by_id[sid]):
            out.append(("resolve", j["id"], "review of %s, which passed" % sid))
    return out


def hygiene_sweep(jobs=None, ledger_path=None, sidecars=None, act=None, notifier=None,
                  now=None, decisions=None, dry_run=False, refresh=None):
    """Apply superseded_review_actions through the queue's own verbs, re-reading each
    row's status right before acting (a review the daemon launched meanwhile is left
    alone). Capped HYGIENE_MAX_PER_DAY. Returns [(verb, id, result)]."""
    jobs = jobs if jobs is not None else _queue_jobs()
    led = _load(Path(ledger_path or HEAL_LEDGER)) or {}
    sc = sidecars if sidecars is not None else _all_sidecars()
    acts = superseded_review_actions(jobs, led, sc, runs=auto_run_passes())
    if dry_run or not acts:
        return [(v, i, "would: " + w) for v, i, w in acts] if dry_run else []
    day = _utc_day(now)
    used = sum(1 for e in ((led.get("hygiene") or {}).get("log") or []) if e.get("day") == day)

    def _act(verb, jid):
        argv = [sys.executable, str(QUEUE_PY), verb, jid] + (["--automated"] if verb == "cancel" else [])
        r = subprocess.run(argv, capture_output=True, text=True, timeout=60)
        return r.returncode == 0
    act = act or _act
    refresh = refresh or (lambda: {j.get("id"): j for j in _queue_jobs()})
    cur = refresh()
    out, done = [], []
    for verb, jid, why in acts:
        row = cur.get(jid)
        want = ("pending",) if verb == "cancel" else ("failed", "needs_opus", "done",
                                                       "done_unconverged", "blocked")
        if row is None or row.get("status") not in want or row.get("pid"):
            out.append((verb, jid, "skipped: now %s" % (row or {}).get("status")))
            continue
        if used >= HYGIENE_MAX_PER_DAY:
            out.append((verb, jid, "capped"))
            continue
        ok = act(verb, jid)
        out.append((verb, jid, "ok" if ok else "refused"))
        if ok:
            used += 1
            done.append((verb, jid, why))
            log_decision(row.get("bundle") or "job", row.get("label"), "hygiene-" + verb,
                         "applied", why, path=decisions)
    if done:
        p = Path(ledger_path or HEAL_LEDGER)
        led = _load(p) or {}
        h = led.setdefault("hygiene", {})
        h["log"] = (list(h.get("log") or []) + [{"day": day, "verb": v, "id": i, "why": w}
                                                for v, i, w in done])[-200:]
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".tmp-hyg")
        tmp.write_text(json.dumps(led, indent=1))
        os.replace(tmp, p)
    capped = [x for x in out if x[2] == "capped"]
    if done or capped:
        msg = "; ".join("%s %s (%s)" % (v, i, w[:80]) for v, i, w in done)
        if capped:
            msg += " | CAP %d/day reached, %d left" % (HYGIENE_MAX_PER_DAY, len(capped))
        try:
            if notifier is None:
                import importlib.util as _ilu
                _s = _ilu.spec_from_file_location(
                    "notify_owner", Path(__file__).resolve().parent / "notify-owner.py")
                _m = _ilu.module_from_spec(_s)
                _s.loader.exec_module(_m)
                notifier = _m.notify
            notifier("queue hygiene", msg[:900], dedupe_key="hygiene:%s:%s" % (
                day, ",".join(i for _v, i, _w in done) or "cap"), dedupe_s=12 * 3600)
        except Exception:
            pass
    return out


def stale_hand_locks(slice_runs=None):
    """[(plan, sid, lock_path)] for a HAND_LOCK left in the worktree of a slice that is
    now done or skipped -- the lock guards a harness nobody will retry (it only blocks
    automatic retry/clear/resume of THAT slice), so it is stale state."""
    out = []
    for f in sorted(Path(slice_runs or SLICE_RUNS).glob("*.json")):
        st = _load(f) or {}
        for sid, s in (st.get("slices") or {}).items():
            if not isinstance(s, dict) or s.get("status") not in ("done", "skipped"):
                continue
            wt = s.get("worktree")
            lp = Path(wt) / HAND_LOCK if wt else None
            if lp is not None and lp.is_file():
                out.append((f.stem, sid, lp))
    return out


def release_hand_locks(locks, now=None, decisions=None):
    """Rename each stale lock to `<lock>.released-<ts>` (evidence kept, never deleted)."""
    ts = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(now))
    done = []
    for plan, sid, lp in locks:
        try:
            os.replace(lp, lp.with_name(lp.name + ".released-" + ts))
            log_decision(plan, sid, "hand-lock-release", "released",
                         "slice is done/skipped; lock kept as %s.released-%s" % (lp.name, ts),
                         path=decisions)
            done.append((plan, sid))
        except OSError:
            pass
    return done


DEAD_RECORD_MIN_AGE_S = 30


_ROUND_LABEL_RE = r"^auto-(?:author|refine)-%s(?:-(?:c|r|esc)\d*)?$"


def orphan_round_jobs(lab, rounds, jobs):
    """Queue job ids of THIS run's authoring/refine rounds that the record never named
    (canary soak seed 47, 2026-10-06): the driver enqueues a round and only THEN writes
    `waiting` + the job id into runs[<label>].rounds, so a SIGKILL in that window leaves
    a live (or finished) round in the queue the record does not know. Matches the
    run's own labels exactly (auto-author-<lab>[-cN|-rN|-esc], auto-refine-<lab>[-rN]),
    oldest first; ids already in `rounds` are skipped."""
    import re
    rx = re.compile(_ROUND_LABEL_RE % re.escape(lab))
    have = {str(x.get("job") if isinstance(x, dict) else x) for x in (rounds or [])}
    mine = [j for j in jobs or [] if rx.match(str(j.get("label") or "")) and j.get("id")
            and str(j.get("id")) not in have]
    mine.sort(key=lambda j: str(j.get("created_at") or j.get("enqueued_at") or ""))
    return [str(j["id"]) for j in mine]


def close_dead_driver_records(runs_dir=None, alive=None, now=None, decisions=None,
                              dry_run=False, min_age_s=DEAD_RECORD_MIN_AGE_S, jobs=None):
    """Mark auto-run records whose driver DIED mid-run as ended (2026-10-06).

    A SIGKILLed / crashed dispatch-auto never writes `ended`, so its runs[<label>]
    stays `advancing`/`waiting` with a dead pid forever (canary soak, seed 1: the s2
    driver was killed while polling its c1 continuation; the slicer resumed the slice
    through its own path, and the record still said advancing). Readers already treat
    a dead pid as "not a driver", so this is state hygiene, not a behaviour change:
    phase -> ended, outcome -> "driver-died (pid N, closed by hygiene)", rounds kept.
    Same lock + top-level mirror as ollama-dispatch-auto._chain_locked_update; the
    pid is re-checked under the lock; a record younger than min_age_s is left alone.
    Returns [(file, label, pid)]."""
    import fcntl
    alive = alive or _driver_alive
    now = time.time() if now is None else now
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
    closed = []
    if jobs is None:
        try:
            jobs = _queue_jobs()
        except Exception:
            jobs = []
    for f in sorted(Path(runs_dir or AUTO_RUNS).glob("*.json")):
        try:
            with open(f.with_name(f.name + ".lock"), "a+") as lk:
                fcntl.flock(lk, fcntl.LOCK_EX)
                try:
                    try:
                        doc = json.loads(f.read_text())
                    except (OSError, ValueError):
                        continue
                    runs = doc.get("runs") if isinstance(doc, dict) else None
                    if not isinstance(runs, dict):
                        continue
                    hit = []
                    for lab, r in runs.items():
                        if not isinstance(r, dict) or r.get("phase") in (None, "ended"):
                            continue
                        pid = r.get("pid")
                        t = _parse_ts(r.get("updated_at")) or _parse_ts(r.get("phase_since"))
                        if t is not None and now - t < min_age_s:
                            continue
                        if pid and alive(pid):
                            continue
                        hit.append((lab, pid))
                    if not hit:
                        continue
                    closed += [(f.name, lab, pid) for lab, pid in hit]
                    if dry_run:
                        continue
                    for lab, pid in hit:
                        r = runs[lab]
                        orphans = orphan_round_jobs(lab, r.get("rounds"), jobs)
                        if orphans:   # enqueued, then the driver died before recording it
                            r["rounds"] = list(r.get("rounds") or []) + orphans
                        r.update(phase="ended", pid=None, phase_since=ts, updated_at=ts,
                                 outcome="driver-died (pid %s, was %s; closed by hygiene)"
                                         % (pid, r.get("phase")))
                    top = sorted(runs.values(), key=lambda r: (
                        r.get("phase") == "ended", -(_parse_ts(r.get("updated_at")) or 0)))[0]
                    for k in ("key", "label", "bundle", "pid", "started_at", "phase",
                              "phase_since", "step", "job", "rounds", "outcome", "updated_at"):
                        doc[k] = top.get(k)
                    tmp = f.with_name(f.name + ".tmp%d" % os.getpid())
                    tmp.write_text(json.dumps(doc, indent=1))
                    os.replace(tmp, f)
                finally:
                    fcntl.flock(lk, fcntl.LOCK_UN)
        except OSError:
            continue
        if not dry_run:
            for fn, lab, pid in [c for c in closed if c[0] == f.name]:
                log_decision(f.stem, lab, "dead-driver-record", "ended",
                             "driver pid %s gone without writing `ended`" % pid, path=decisions)
    return closed


def stale_drivers(runs_dir=None, alive=None, auto=None):
    """Live dispatch-auto drivers running code older than the file on disk:
    [(label, pid, why)]. A stamped record ("code", written by drivers started after
    2026-10-06) compares the sha; an unstamped one compares started_at with the file's
    mtime. REPORT ONLY -- a driver re-execs itself between rounds (maybe_restart_stale
    in ollama-dispatch-auto); nothing here kills or restarts anything."""
    import hashlib
    alive = alive or _driver_alive
    auto = Path(auto or AUTO_TOOL)
    try:
        disk = hashlib.sha256(auto.read_bytes()).hexdigest()[:16]
        mtime = auto.stat().st_mtime
    except OSError:
        return []
    out, seen = [], set()
    for f in sorted(Path(runs_dir or AUTO_RUNS).glob("*.json")):
        d = _load(f)
        if not isinstance(d, dict):
            continue
        recs = list((d.get("runs") or {}).values()) if isinstance(d.get("runs"), dict) else []
        recs.append(d)
        for r in recs:
            if not isinstance(r, dict) or r.get("phase") == "ended" or not r.get("pid"):
                continue
            pid = r.get("pid")
            if pid in seen or not alive(pid):
                continue
            seen.add(pid)
            code = r.get("code") if isinstance(r.get("code"), str) else None
            if code:
                if code != disk:
                    out.append((r.get("label"), pid, "runs code %s, disk has %s" % (code, disk)))
            else:
                t = _parse_ts(r.get("started_at"))
                if t and t < mtime:
                    out.append((r.get("label"), pid, "unstamped driver started %s, before "
                                "the file's last change" % r.get("started_at")))
    return out


# --- TIER LADDER WIRING (2026-10-12) -----------------------------------------------------
# pipeline_ladder.py owns the logic (local reviewer -> Sonnet headless -> Opus for tooling
# only; named exits; grounded findings only). This is the production wiring: the hooks
# (queue enqueue/resolve, re-spec, verify) and the sweep. Replaces "park for a human" and
# `skip:not an auto-author job`: every needs_opus row ends in an ACTION or a NAMED exit.
LADDER_ACTED = ("retired:", "closed:", "heal-continuation:", "respec:", "ladder-exit:")
_REAL_HEAL_LEDGER = Path.home() / ".ollama-dispatch" / "escalations" / "self-heal.json"


def _pl():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import pipeline_ladder as pl
    return pl


def ladder_enabled(ledger_path=None, env=None):
    """Production only: never under the verify sandbox, a test ledger, LADDER=off or the
    escalation ACTIONS-OFF kill switch."""
    env = os.environ if env is None else env
    if str(env.get("LADDER", "on")).lower() == "off" or env.get("DISPATCH_VERIFY_SANDBOX") == "1":
        return False
    if ledger_path is not None and Path(ledger_path).resolve() != Path(HEAL_LEDGER).resolve():
        return False
    if Path(HEAL_LEDGER).resolve() != _REAL_HEAL_LEDGER.resolve():
        return False                  # a redirected ledger (tests / sandboxes) is never production
    try:
        return not _ev().actions_killed()
    except Exception:
        return True


def ladder_local_verdict(job, review_text):
    """Tier 1 as the ladder consumes it: (verdict, grounded, findings). Grounded = the
    reviewer's b/c/d verdict has >=1 claim a script verified (escalation_grounding.assess)."""
    ev, eg = _ev(), _eg()
    if not review_text or ev.is_no_verdict_review(review_text):
        return None, False, []
    vd = ev.resolve(review_text, job.get("failure_class"), (job.get("escalation") or {}).get("reason"),
                    key_parts=(job.get("id"),))
    v = vd["verdict"]
    if v not in ("b", "c", "d") or vd["source"] not in GROUNDING_REVIEW_SOURCES:
        return v, False, []
    try:
        rep = eg.assess(review_text, "cwd `%s`" % (job.get("cwd") or ""))
    except Exception:
        return v, False, []
    if rep.get("status") != "GROUNDED":
        return v, False, []
    return v, True, [{"file": c["file"], "line": c["line"], "quote": c["quote"]}
                     for c in rep.get("claims") or [] if c.get("grounded")]


def _ladder_run_verify(job):
    try:
        r = subprocess.run(str(job["verify"]), shell=True, cwd=job["cwd"], capture_output=True,
                           text=True, timeout=900)
        return r.returncode, ((r.stdout or "") + (r.stderr or ""))[-300:]
    except Exception as e:      # noqa: BLE001
        return 99, "verify error %s" % type(e).__name__


def ladder_hooks():
    pl = _pl()

    def _respec(job, notes, why, reslice):
        return respec_job(job, notes, why, reslice=reslice)
    return {"enqueue": _queue_enqueue, "resolve": _queue_resolve, "respec": _respec,
            "sonnet": pl.sonnet_call,
            "opus": pl.opus_call if os.environ.get("LADDER_OPUS", "off").lower() == "on" else None,
            "run_verify": _ladder_run_verify, "local_verdict": ladder_local_verdict,
            "esc_dir": ESC_DIR}


def ladder_run(job, review_text="", jobs=None, hooks=None, ledger_path=None, now=None, close_rows=True):
    """Walk ONE row down the ladder; an ACTED outcome ticks its escalation index rows with
    the action taken. Returns the action word-string."""
    pl = _pl()
    jobs = jobs if jobs is not None else _queue_jobs()
    out = pl.process_job(job, jobs, hooks or ladder_hooks(), ledger_path or HEAL_LEDGER, now,
                         review_text, dict(_ev().load_config()))
    if close_rows and str(out).startswith(LADDER_ACTED) and job.get("id"):
        close_index_rows(job["id"], out)
        log_decision(job.get("bundle") or "job", job.get("label"), "ladder", out.split(":", 1)[0], out[:300])
    return out


def ladder_sweep(jobs=None, hooks=None, ledger_path=None, now=None, only=None, dry_run=False):
    """Every watcher pass (--job-sweep): each needs_opus row the normal heal path is not
    still working on. [(job_id, action)]."""
    pl = _pl()
    jobs = jobs if jobs is not None else _queue_jobs()
    led = _load(Path(ledger_path or HEAL_LEDGER)) or {}
    cand = []
    for j in jobs:
        if j.get("status") != "needs_opus" or (only and j.get("id") not in only):
            continue
        if plan_slice_for_label(j.get("label"))[0]:
            continue                                  # a slice job: the slice ladder owns it
        try:
            if job_heal_pending(j, led, now):
                continue                              # the job ladder is still healing it
        except Exception:
            pass
        cand.append(j)
    if dry_run:
        return pl.sweep(jobs, ladder_hooks(), ledger_path or HEAL_LEDGER, now, only={j["id"] for j in cand},
                        dry_run=True)
    out = []
    for j in cand:
        out.append((j["id"], ladder_run(j, _job_review(j), jobs, hooks, ledger_path, now)))
    return out


# ----------------------------------------------------------------------------
def self_test():
    fails = []

    def check(name, got, want=True):
        ok = got == want
        print(("  PASS " if ok else "  FAIL ") + name + ("" if ok else " -- got %r" % (got,)))
        if not ok:
            fails.append(name)

    print("dispatch-self-heal self-test")
    check("relevance NO-GO -> regate", classify(
        "mechanical relevance gate (preflight) NO-GO -- verify-relevance: LOW relevance: "
        "8/28 property-breaking mutants survived (score 0.714)"), "regate")
    check("identical authoring streak -> retry", classify(
        "authoring made no progress across 2 consecutive attempts (2 total), each ending "
        "on the identical failure: unauthored -> TASK.md:missing"), "retry")
    check("deterministic authoring failure -> retry", classify(
        "authoring failed DETERMINISTICALLY (AUTO rc=2) -- ..."), "retry")
    check("a coding-job gate FAIL is a human call", classify(
        "gate verdict fail on coding job abc: correctness"), "none")
    check("a stale-base failure is MECHANICAL -> retry (re-seed off the chain tip)", classify(
        "hit iteration cap (24/24); VERIFY FAILED; slice/p moved 3 commit(s) past the launch "
        "baseline 87fd8a4c while this ran -- re-seed the worktree onto slice/p before any "
        "retry"), "retry")
    check("a class=stale-base escalation -> retry", classify(
        "coding job cb21d014c3cd failed class=stale-base"), "retry")
    check("a suspected pipeline bug is a human call", classify(
        "PIPELINE BUG SUSPECTED: baseline-green claim contradicted"), "none")

    man = {"authored": ["TASK.md", "check_literals.py", "refimpl.py", "verify.sh",
                        "verify.test.ts"], "target": "sidecar/src/bfmr.js"}
    allow = allowed_files(man, man["target"])
    check("fixture + refimpl are allowed", allow, {"refimpl.py", "verify.test.ts"})
    fix = ("--- a/verify.test.ts\n+++ b/verify.test.ts\n@@ -1 +1,2 @@\n x\n+y\n")
    tgt = ("--- a/sidecar/src/bfmr.js\n+++ b/sidecar/src/bfmr.js\n@@ -1 +1 @@\n-a\n+b\n")
    spec = ("--- a/TASK.md\n+++ b/TASK.md\n@@ -1 +1 @@\n-a\n+b\n")
    check("a fixture-only diff is accepted", refused_files(fix, allow), [])
    check("a diff touching the TARGET is refused",
          refused_files(fix + tgt, allow), ["sidecar/src/bfmr.js"])
    check("a diff touching TASK.md / verify.sh is refused",
          refused_files(spec, allow), ["TASK.md"])
    check("a rename into the target is refused",
          "sidecar/src/bfmr.js" in refused_files(
              "diff --git a/verify.test.ts b/sidecar/src/bfmr.js\nrename from verify.test.ts\n"
              "rename to sidecar/src/bfmr.js\n", allow))

    # end to end, offline: a real git worktree, a fake slicer launcher
    with tempfile.TemporaryDirectory() as td:
        td = Path(td)
        wt, runs = td / "wt", td / "runs"
        wt.mkdir()
        runs.mkdir()
        subprocess.run(["git", "init", "-q", str(wt)], check=True)
        (wt / "verify.test.ts").write_text("x\n")
        (wt / "sidecar").mkdir()
        (wt / "sidecar" / "bfmr.js").write_text("a\n")
        (wt / ".dispatch-harness.json").write_text(json.dumps(man))
        plan = td / "plan.json"
        plan.write_text("{}")
        base = {"label": "p", "plan_path": str(plan), "target": "sidecar/src/bfmr.js",
                "slices": {"s1": {"status": "escalated", "worktree": str(wt),
                                  "escalation_reason": "mechanical relevance gate "
                                  "(preflight) NO-GO -- verify-relevance: LOW"}}}
        (runs / "p.json").write_text(json.dumps(base))
        led, ctx = td / "heal.json", td / "ctx.md"
        ctx.write_text("# ctx\n")
        calls, alerts = [], []
        launch = lambda pp, args, lp: calls.append(args)
        # never alert the owner / write the live decisions log from a self-test
        heal._notifier = lambda t, m, **k: alerts.append((m, k.get("dedupe_key")))
        # the in-place repair rung would run the REAL relevance gate on this toy tree
        heal._repair_hooks = {"jobs": [], "survey": lambda w, r: {
            "parsed": False, "survivors": [], "reason": "self-test: no relevance gate"}}
        global DECISIONS, ESC_DIR
        _saved = (DECISIONS, ESC_DIR)
        DECISIONS, ESC_DIR = td / "decisions.jsonl", td / "esc"
        rv = "VERDICT: c -- fixture gap\n```diff\n" + fix + "```\n"

        got = heal("p", "s1", ctx, rv + "```diff\n" + fix + tgt + "```\n", runs, led, launch)
        check("a diff that also edits the target is never APPLIED; verdict c resets the "
              "harness and re-authors with the findings instead of parking", got, "retry")
        check("...and the target is untouched", (wt / "sidecar" / "bfmr.js").read_text(), "a\n")
        check("...via --retry-slice with --review-notes",
              [c[:3] for c in calls], [["--retry-slice", "s1", "--review-notes"]])
        check("...the notes file carries the review",
              calls and "fixture gap" in Path(calls[0][3]).read_text(), True)
        calls.clear()
        led.unlink()

        got = heal("p", "s1", ctx, rv, runs, led, launch)
        check("a harness-only diff is applied and re-gated", got, "regate")
        check("...the fixture now carries the fix", (wt / "verify.test.ts").read_text(), "x\ny\n")
        check("...via --regate (the relevance gate still decides GO)", calls, [["--regate", "s1"]])
        check("...and the action is logged to the escalation context",
              "patch+regate" in ctx.read_text(), True)

        # a correct fixture diff with miscounted @@ counts still applies
        # (live: sidecar-bfmr s2 attempt 2 refused as "corrupt patch at line 22")
        (wt / "verify.test.ts").write_text("x\n")
        ok, why = _git_apply(wt, "--- a/verify.test.ts\n+++ b/verify.test.ts\n"
                             "@@ -1,4 +1,7 @@\n x\n+z\n", check_only=False)
        check("a diff with miscounted @@ line counts applies (--recount): " + why,
              (ok, (wt / "verify.test.ts").read_text()), (True, "x\nz\n"))
        (wt / "verify.test.ts").write_text("x\n")
        heal("p", "s1", ctx, rv, runs, led, launch)
        calls.clear()
        got = heal("p", "s1", ctx, rv, runs, led, launch)
        check("self-heal is BOUNDED: the third action is the FINAL rung", got, "park:final-rung")
        check("...no lever is pulled on the final rung", calls, [])
        check("...it is logged to the escalation context", "final-rung" in ctx.read_text(), True)
        check("...ONE alert names the slice and the exact qctl command",
              (len(alerts), "qctl retry p s1" in alerts[0][0] if alerts else None,
               alerts[0][1] if alerts else None), (1, True, "final-rung:p/s1"))
        dec = [json.loads(l) for l in DECISIONS.read_text().splitlines()]
        check("...and the decision lands in decisions.jsonl (qctl status shows it)",
              (dec[-1]["action"], dec[-1]["outcome"], dec[-1]["slice"]),
              ("final-rung", "parked-for-human", "s1"))

        # verdict-driven rungs on escalations classify() alone would park
        check("verdict parse: 'VERDICT: (b) --'", review_verdict("x\nVERDICT: (b) -- spec"), "b")
        check("verdict parse: '**VERDICT:** d'", review_verdict("**VERDICT:** d -- cap"), "d")
        check("verdict parse: none", review_verdict("(no verdict)"), None)
        base["slices"]["s4"] = {"status": "escalated", "escalation_reason":
                                "gate verdict fail on coding job abc: correctness"}
        (runs / "p.json").write_text(json.dumps(base))
        calls.clear()
        got = heal("p", "s4", ctx, "VERDICT: (b) -- under-specified: say X", runs, led, launch)
        check("verdict (b) on a 'none'-class escalation -> re-author with the findings",
              (got, [c[:3] for c in calls]), ("retry", [["--retry-slice", "s4", "--review-notes"]]))
        base["slices"]["s5"] = {"status": "escalated", "escalation_reason":
                                "PIPELINE BUG SUSPECTED: baseline-green claim contradicted"}
        (runs / "p.json").write_text(json.dumps(base))
        calls.clear()
        check("PIPELINE BUG SUSPECTED goes straight to the final rung, no lever",
              (heal("p", "s5", ctx, "VERDICT: (c) -- x", runs, led, launch), calls),
              ("park:final-rung", []))

        base["slices"]["s2"] = {"status": "escalated", "escalation_reason":
                                "authoring failed DETERMINISTICALLY (AUTO rc=2)"}
        (runs / "p.json").write_text(json.dumps(base))
        calls.clear()
        check("an authoring-class escalation is retried clean",
              heal("p", "s2", ctx, "", runs, led, launch), "retry")
        check("...via --retry-slice", calls, [["--retry-slice", "s2"]])

        # a HUMAN cancel is terminal: no lever is pulled for a cancelled plan
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import plan_cancel
        plan_cancel.mark_cancelled("p", "the owner cancelled", runs_dir=runs)
        base["slices"]["s2"]["status"] = "escalated"
        (runs / "p.json").write_text(json.dumps(base))
        calls.clear()
        led.unlink()
        check("a CANCELLED plan is never healed (ev-service-screen 18:42Z retry)",
              (heal("p", "s2", ctx, "", runs, led, launch), calls), ("skip:cancelled", []))
        plan_cancel.clear_cancelled("p", runs_dir=runs)
        check("...and once uncancelled it heals again",
              heal("p", "s2", ctx, "", runs, led, launch), "retry")

        base["slices"]["s3"] = {"status": "done"}
        (runs / "p.json").write_text(json.dumps(base))
        check("a slice that is no longer escalated is left alone",
              heal("p", "s3", ctx, rv, runs, led, launch).startswith("skip:"), True)

        DECISIONS, ESC_DIR = _saved
        heal._notifier = None
    print("SELF_TEST_OK" if not fails else "SELF_TEST_FAILED: %s" % fails)
    return 0 if not fails else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan-label")
    ap.add_argument("--slice")
    ap.add_argument("--context")
    ap.add_argument("--review", help="path to the watcher's .review.md")
    ap.add_argument("--job-label", help="a queue job's label: resolve its plan/slice "
                    "(for needs_opus JOB escalations)")
    ap.add_argument("--job-id", help="the needs_opus job's id (with --job-label)")
    ap.add_argument("--job-sweep", action="store_true",
                    help="retire needs_opus rows whose continuation passed; spend the next "
                         "capped heal round on a failed one")
    ap.add_argument("--retire-sweep", action="store_true",
                    help="resolve needs_opus/escalated/failed rows whose slice is done/skipped")
    ap.add_argument("--hygiene-sweep", action="store_true",
                    help="retire superseded/dead esc-review rows (cancel --automated a "
                         "pending one, resolve a launch-failed one) and resolve a needs_opus "
                         "row whose -vN re-run passed; capped, alerting")
    ap.add_argument("--ladder-sweep", action="store_true",
                    help="walk every needs_opus row down the tier ladder (local reviewer -> Sonnet "
                         "-> Opus for tooling only); each ends in an action or a NAMED exit")
    ap.add_argument("--ladder-only", nargs="+", metavar="JOB_ID", help="with --ladder-sweep: only these rows")
    ap.add_argument("--dry-run", action="store_true", help="with --hygiene-sweep/--ladder-sweep: print only")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    if a.hygiene_sweep:
        for v, i, r in hygiene_sweep(dry_run=a.dry_run):
            print("# hygiene %s %s: %s" % (v, i, r))
        for plan, sid, lp in stale_hand_locks():
            print("# hygiene hand-lock %s/%s: %s" % (
                plan, sid, "would release " + str(lp) if a.dry_run else "released"))
        if not a.dry_run:
            release_hand_locks(stale_hand_locks())
        for fn, lab, pid in close_dead_driver_records(dry_run=a.dry_run):
            print("# hygiene dead-driver record %s/%s (pid %s): %s" % (
                fn, lab, pid, "would end" if a.dry_run else "ended"))
        st = stale_drivers()
        for lab, pid, why in st:
            print("# stale driver %s pid %s: %s (it re-execs itself between rounds if it "
                  "has the stamp; an unstamped one finishes on old code)" % (lab, pid, why))
        if st and not a.dry_run:
            try:
                import importlib.util as _ilu
                _s = _ilu.spec_from_file_location(
                    "notify_owner", Path(__file__).resolve().parent / "notify-owner.py")
                _m = _ilu.module_from_spec(_s)
                _s.loader.exec_module(_m)
                _m.notify("stale dispatch drivers", "; ".join(
                    "%s pid %s" % (l, p) for l, p, _w in st)[:600],
                    dedupe_key="stale-drivers:%s" % ",".join(sorted(str(p) for _l, p, _w in st)),
                    dedupe_s=24 * 3600)
            except Exception:
                pass
        return 0
    if a.retire_sweep:
        print("retired %d" % len(retire_sweep()))
        return 0
    review = Path(a.review).read_text() if a.review and Path(a.review).is_file() else ""
    if a.ladder_sweep:
        if not (ladder_enabled() or a.dry_run):
            print("# ladder disabled (LADDER=off / kill switch / sandbox)")
            return 0
        for jid, act in ladder_sweep(only=set(a.ladder_only or []) or None, dry_run=a.dry_run):
            print("# ladder %s: %s" % (jid, act))
        return 0
    if a.job_sweep:
        for jid, act in job_heal_sweep():
            print("# job-heal sweep %s: %s" % (jid, act))
        if ladder_enabled():
            try:
                for jid, act in ladder_sweep():
                    print("# ladder %s: %s" % (jid, act))
            except Exception as e:      # advisory: never break the watcher pass
                print("# ladder sweep error: %s: %s" % (type(e).__name__, e))
        try:
            for key, act in harness_repair_sweep():
                print("# harness-repair sweep %s: %s" % (key, act))
        except Exception as e:      # advisory: never break the watcher pass
            print("# harness-repair sweep error: %s: %s" % (type(e).__name__, e))
        return 0
    if a.job_label and not (a.plan_label and a.slice):
        a.plan_label, a.slice = plan_slice_for_label(a.job_label)
        if not a.plan_label:
            # NON-SLICE job (2026-10-05): the job ladder, not a dead "skip".
            jobs = _queue_jobs()
            row = next((j for j in jobs if (a.job_id and j.get("id") == a.job_id)
                        or (not a.job_id and j.get("label") == a.job_label
                            and j.get("status") == "needs_opus")), None)
            if row is None:
                print("skip:job is not a slice of any plan, and no needs_opus row for it")
                return 0
            _heal_job._review_path = a.review
            print(heal_job(row, review, jobs=jobs, ctx=a.context))
            return 0
        # A job escalation: only the verified 'already satisfied' path applies.
        if not review_says_satisfied(review):
            print("skip:not an already-satisfied verdict")
            return 0
        print(verified_skip(a.plan_label, a.slice, review, review_path=a.review, ctx=a.context))
        return 0
    if not (a.plan_label and a.slice):
        ap.error("--plan-label and --slice are required")
    heal._review_path = a.review
    print(heal(a.plan_label, a.slice, a.context, review))
    return 0


if __name__ == "__main__":
    sys.exit(main())
