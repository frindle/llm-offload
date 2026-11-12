#!/usr/bin/env python3
"""Interrupted authoring resumes AUTHORING, not preflight (2026-10-06).

Canary soak seed 2: the driver was SIGKILLed while its s2 author round ran. The
round then failed at its iteration cap with nobody to run the c1 continuation; the
gate's autofeed re-ran the slicer, whose PENDING resume saw the harness FILES
(harness_authored() is a presence test), preflighted a harness that never
self-checked (refimpl-tests FAIL) and ESCALATED the slice -> bundle parked.

Now:
  * slicer: harness_self_check_fail(wt) runs the self-check (under the tree lock);
    an authored harness that does NOT self-check relaunches AUTO with --resume-author
    instead of preflighting it (and is never wiped);
  * auto: --resume-author on such a harness enters the continuation ladder on the
    same tree (skipping the first dispatch), then escalation/failure handling, then
    preflight; a harness that DOES self-check takes the plain resume path;
    --resume-harness alone still refuses an unconverged harness.

Hermetic: stubs every dispatch / preflight; temp worktrees. AUTO_SRC / SLICE_SRC
point at other sources (--revert-check mutates them and requires RED)."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
SLICE = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [str(path)]
    ld.exec_module(m)
    return m


class Died(Exception):
    pass


def auto_args(wt, **k):
    a = SimpleNamespace(
        model="m", host="h", label="p-s2", target="app/x.py", lang="python", repo=str(wt.parent),
        new_project=None, dest=str(wt), num_ctx=32768, intent="i", interface=None,
        drafter_cmd=None, slice_plan="p", slice_id="s2", require=[], allow_multi_module=False,
        no_auto_slice=True, auto_slice=False, resume_harness=False, resume_author=False,
        author_continue_rounds=2, author_max_iters=24, kind="symbol", anchor=None)
    a.__dict__.update(k)
    return a


def test_auto():
    m = load(AUTO, "oda_ra")
    T = Path(tempfile.mkdtemp(prefix="ra-"))
    wt = T / "wt"
    wt.mkdir()
    calls = []
    m.load_defaults = lambda: ("m", "h")
    m.chain_state_write = lambda *x, **k: None
    m.multi_module_check = lambda *x, **k: ("ok", "")
    m.test_target_refusal = lambda *x: None
    m.bind_scaffold_fixture = lambda *x: None
    m.record_argv = lambda *x: None
    m.clear_attempts = lambda *x: None
    m.die = lambda msg, code=1: (_ for _ in ()).throw(Died(msg))
    m._preflight_loop = lambda *x: calls.append("preflight") or 0
    m._author_escalate = lambda a, wt_, t, v, why: (False, why)
    m._handle_author_failure = lambda a, t, why: calls.append(("author-failed", why)) or 7
    m.do_scaffold = lambda *x: calls.append("scaffold")

    def awc(a, wt_, target, verify_cmd, resumed_why=None):
        calls.append(("author-continue", resumed_why))
        return True, "converged on continuation"
    real_awc = m._author_with_continuations
    m._author_with_continuations = awc
    unconverged = lambda *x, **k: (False, "harness does not self-check (rc=1): FAIL refimpl-tests")

    m.resume_harness_check = unconverged
    calls.clear()
    rc = m.do_auto(auto_args(wt, resume_author=True))
    check("--resume-author on an unconverged harness continues authoring, then preflights",
          [c if isinstance(c, str) else c[0] for c in calls], ["author-continue", "preflight"])
    check("...the continuation is told the first round already ran (no fresh dispatch)",
          bool(calls and isinstance(calls[0], tuple) and calls[0][1]), True)
    check("...and never re-scaffolds", "scaffold" in calls, False)

    calls.clear()
    m._author_with_continuations = lambda *x, **k: (calls.append(("author-continue", 1)), (False, "cap"))[1]
    rc = m.do_auto(auto_args(wt, resume_author=True))
    check("a still-unconverged resumed author goes to the normal author-failure handling",
          [c if isinstance(c, str) else c[0] for c in calls], ["author-continue", "author-failed"])
    m._author_with_continuations = awc

    calls.clear()
    try:
        m.do_auto(auto_args(wt, resume_harness=True))
        r = "returned"
    except Died as e:
        r = "died" if "refused" in str(e) else str(e)
    check("--resume-harness alone still REFUSES an unconverged harness", r, "died")
    check("...without authoring", calls, [])

    m.resume_harness_check = lambda *x, **k: (False, "no authored harness in /x (missing TASK.md)")
    try:
        m.do_auto(auto_args(wt, resume_author=True))
        r = "returned"
    except Died:
        r = "died"
    check("--resume-author with NO harness files refuses (nothing to continue)", r, "died")

    m.resume_harness_check = lambda *x, **k: (True, "harness self-check VERIFY_OK")
    calls.clear()
    m.do_auto(auto_args(wt, resume_author=True))
    check("--resume-author on a converged harness takes the plain resume (preflight only)",
          calls, ["preflight"])

    # the continuation ladder skips the first dispatch when resumed
    m._author_with_continuations = real_awc
    disp = []
    m.dispatch_model = lambda *x, **k: disp.append(1) or (False, "hit iteration cap")
    m._harness_signature = lambda *x: "sig"
    m._harness_check_output = lambda *x: ("FAIL x", None)
    m.record_attempt = lambda *x, **k: None
    m.author_continue_prompt = lambda *x, **k: "p"
    a = auto_args(wt)
    a.author_continue_rounds = 1
    try:
        ok, why = m._author_with_continuations(a, wt, "app/x.py", "v", resumed_why="interrupted")
    except Exception as e:
        ok, why = None, repr(e)
    check("resumed ladder: exactly ONE dispatch (the continuation), not author + continuation",
          len(disp), 1)
    disp.clear()
    m.author_prompt = lambda *x, **k: "author"
    m._author_with_continuations(a, wt, "app/x.py", "v")
    check("fresh ladder still dispatches the first author round", len(disp) >= 1, True)


def git(cwd, *a):
    return subprocess.run(["git", *a], cwd=cwd, capture_output=True, text=True)


def test_slicer():
    sl = load(SLICE, "ods_ra")
    T = Path(tempfile.mkdtemp(prefix="ra-sl-"))
    wt = T / "wt"
    wt.mkdir()
    git(wt, "init", "-q")
    (wt / "auto-harness-check.py").write_text(
        "import sys\nprint('FAIL: refimpl-tests: the reference impl FAILS 1 test')\nsys.exit(1)\n")
    check("slicer: an unconverged harness is reported as not self-checking",
          (sl.harness_self_check_fail(wt) or "").startswith("FAIL: refimpl-tests"), True)
    (wt / "auto-harness-check.py").write_text("print('VERIFY_OK: harness discriminates')\n")
    check("slicer: a converged harness self-checks", sl.harness_self_check_fail(wt), None)
    # the self-check waits for the tree lock (a gate / auto self-check rewriting the target)
    hold = subprocess.Popen([sys.executable, "-c",
                             "import fcntl,time,sys;fh=open(sys.argv[1],'a+');"
                             "fcntl.flock(fh.fileno(),fcntl.LOCK_EX);print('held',flush=True);time.sleep(4)",
                             str(wt / ".git" / "dispatch-tree.lock")], stdout=subprocess.PIPE, text=True)
    hold.stdout.readline()
    import time
    t0 = time.time()
    sl.harness_self_check_fail(wt)
    check("slicer: the self-check waits for the worktree tree lock", time.time() - t0 >= 2.5, True)
    hold.wait()

    src = SLICE.read_text()
    i = src.find("_sc_fail = harness_self_check_fail(wt) if harness_authored(wt) else None")
    j = src.find('if harness_authored(wt) and _sc_fail is None:')
    k = src.find("resume_author = True", j)
    lnc = src.find('*(["--resume-author"] if resume_author else [])')
    check("slicer: the PENDING resume preflights ONLY a harness that self-checks",
          0 < i < j < k and lnc > 0, True)   # --resume-author arg builder lives in the earlier launch helper
    blk = src[j:k]
    check("slicer: an unconverged harness is not wiped (no bound_stale_worktree_retry on it)",
          "bound_stale_worktree_retry" not in blk.split("if harness_authored(wt):\n", 1)[-1], True)


def main():
    test_auto()
    test_slicer()
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    (AUTO, "AUTO_SRC", "auto: --resume-author not honoured",
     '        if (not ok and getattr(a, "resume_author", False)\n',
     '        if (False and getattr(a, "resume_author", False)\n'),
    (AUTO, "AUTO_SRC", "auto: resumed ladder re-dispatches the first round",
     "    if resumed_why:\n        ok, why = False, resumed_why\n",
     "    if False:\n        ok, why = False, resumed_why\n"),
    (SLICE, "SLICE_SRC", "slicer: presence-only resume (no self-check)",
     "            if harness_authored(wt) and _sc_fail is None:\n",
     "            if harness_authored(wt):\n"),
    (SLICE, "SLICE_SRC", "slicer: --resume-author never passed",
     '                          *(["--resume-author"] if resume_author else [])])',
     "                          ])"),
    (SLICE, "SLICE_SRC", "slicer: self-check ignores the tree lock",
     "                    fcntl.flock(lk.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)\n                    break\n",
     "                    break\n"),
]


def revert_check():
    bad = 0
    for path, env, name, old, new in MUTATIONS:
        src = path.read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, env: f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
