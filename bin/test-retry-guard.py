#!/usr/bin/env python3
"""Unattended-readiness (1): RETRY GUARD in ollama-dispatch-slice.

Before an automatic slice retry (bound_stale_worktree_retry), the reference impl is
applied to a throwaway copy of the SEALED baseline and verify.sh is run:
  - refimpl turns the fixture green          -> the retry proceeds (worktree cleared)
  - refimpl no longer applies / verify red   -> ESCALATED, NOT retried, worktree kept
  - fixture green at baseline + red w/ ref   -> rejected as encoding the BUGGY shape
  - the worktree's fixture edited vs sealed  -> FLAGGED (s["retry_flags"])
Authoring-stage slices (no coding job_id) are not judged: re-authoring is the cure.

SLICE_SRC=<path> points it at another copy (the .bak for the revert-test).
"""
import importlib.util
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
SRC = Path(os.environ.get("SLICE_SRC") or BIN / "ollama-dispatch-slice")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    sys.path.insert(0, str(SRC.parent))
    loader = SourceFileLoader("slice_rg", str(SRC))
    spec = importlib.util.spec_from_loader("slice_rg", loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(SRC)]
    loader.exec_module(m)
    return m


TARGET = "def add(a, b):\n    return a - b\n"
VERIFY = ("#!/usr/bin/env bash\ncd \"$(dirname \"$0\")\" || exit 1\n"
          "python3 test_fixture.py && echo VERIFY_OK || exit 1\n")
GOOD_FIX = "from target import add\nassert add(2, 3) == 5, 'add'\nprint('ok')\n"
BUGGY_FIX = "from target import add\nassert add(2, 3) == -1, 'add'\nprint('ok')\n"
REF_OK = ("import pathlib, sys\nwt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.')\n"
          "p = wt / 'target.py'\nt = p.read_text()\n"
          "assert t.count('return a - b') == 1, 'refimpl anchor 1 not found -- did the target change?'\n"
          "p.write_text(t.replace('return a - b', 'return a + b'))\n")
REF_STALE = REF_OK.replace("return a - b", "return a * b")


def mkwt(root, name, fixture, ref):
    wt = Path(root) / name
    wt.mkdir()
    (wt / "target.py").write_text(TARGET)
    (wt / "verify.sh").write_text(VERIFY)
    (wt / "test_fixture.py").write_text(fixture)
    (wt / "refimpl.py").write_text(ref)
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True,
                                   check=True)
    g("init", "-q")
    g("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seal dispatch harness")
    return str(wt)


def main():
    m = load()
    rg = getattr(m, "retry_guard", None)
    chk("retry_guard exists", callable(rg), True)
    root = tempfile.mkdtemp(prefix="rg-test-")
    good = mkwt(root, "good", GOOD_FIX, REF_OK)
    stale = mkwt(root, "stale", GOOD_FIX, REF_STALE)
    buggy = mkwt(root, "buggy", BUGGY_FIX, REF_OK)
    st = {"label": "rgtest", "slices": {}, "order": []}
    S = lambda **k: {"status": m.FAILED, "job_id": "abc123def456", **k}
    if callable(rg):
        ok, why, flags = rg(st, "s1", S(), good)
        chk("consistent harness: retry allowed", (ok, why, flags), (True, None, []))
        ok, why, _ = rg(st, "s1", S(), stale)
        chk("stale refimpl (anchor gone): retry REFUSED", ok, False)
        chk("...reason names the disagreement", "DISAGREE" in str(why), True)
        chk("...and carries the refimpl's own error", "anchor 1 not found" in str(why), True)
        ok, why, _ = rg(st, "s1", S(), buggy)
        chk("buggy-shape fixture: REJECTED", ok, False)
        chk("...named as encoding the BUGGY shape", "BUGGY shape" in str(why), True)
        ok, why, flags = rg(st, "s1", {"status": m.FAILED, "job_id": None}, stale)
        chk("authoring-stage slice (no job_id): not judged", (ok, flags), (True, []))
        # a coding attempt that edited its own fixture: flagged; the SEALED one is judged
        Path(good, "test_fixture.py").write_text("print('ok')\n")
        ok, why, flags = rg(st, "s1", S(), good)
        chk("fixture edited in the worktree: FLAGGED", any("FIXTURE_EDITED" in f for f in flags),
            True)
        chk("...the sealed fixture (not the edit) is what is judged -> still allowed", ok, True)
        Path(stale, "test_fixture.py").write_text("print('ok')\n")
        ok, _, flags = rg(st, "s1", S(), stale)
        chk("edited fixture cannot launder a stale refimpl (still refused)", ok, False)
        chk("guard leaves the worktree untouched (target still unfixed)",
            Path(good, "target.py").read_text(), TARGET)

    # ---- integration: bound_stale_worktree_retry honours the guard -----------------
    removed = []
    m.save_state = lambda *a, **k: None
    m.slice_job_awaiting_gate = lambda *a, **k: None
    m.remove_worktree = lambda cwt, wt: removed.append(wt)
    m.refresh_author_job_ids = lambda st, sid, s: 0
    s = S()
    st2 = {"label": "rgtest", "slices": {"s1": s}, "order": ["s1"]}
    r = m.bound_stale_worktree_retry(st2, "s1", s, stale, "/nonexistent/cwt", "test")
    chk("inconsistent harness: bound_stale_worktree_retry returns False (no retry)", r, False)
    chk("...slice is ESCALATED", s.get("status"), m.ESCALATED)
    chk("...worktree NOT cleared", removed, [])
    chk("...no author attempt was burned", s.get("author_attempts"), None)
    chk("...the fixture edit is carried into the record",
        any("FIXTURE_EDITED" in f for f in (s.get("retry_flags") or [])), True)
    s2 = S()
    st3 = {"label": "rgtest", "slices": {"s1": s2}, "order": ["s1"]}
    fresh = mkwt(root, "fresh", GOOD_FIX, REF_OK)
    r = m.bound_stale_worktree_retry(st3, "s1", s2, fresh, "/nonexistent/cwt", "test")
    chk("consistent harness: the retry proceeds (True, worktree cleared)",
        (r, removed), (True, [fresh]))
    subprocess.run(["rm", "-rf", root])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAIL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
