#!/usr/bin/env python3
"""Unattended-readiness (2d): the FAILED auto-heal ("auto-retrying (clearing the
losing attempt's worktree ...)") SKIPS a worktree with uncommitted HAND edits.

When a slice enters FAILED, save_state fingerprints the worktree's dirty files.
A later file whose content differs from that fingerprint and whose mtime is newer
than the failure (and the failing job's gate/done sidecars) is a hand edit:
execute() neither auto-lands nor clears that tree. The losing attempt's own
leftovers, a mere touch, cache noise and a late gate write are NOT hand edits, so
the normal auto-retry still runs for them. SLICE_SRC=<path> for the revert-test.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import time
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
    loader = SourceFileLoader("slice_he", str(SRC))
    spec = importlib.util.spec_from_loader("slice_he", loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(SRC)]
    loader.exec_module(m)
    return m


def mkwt(root, name):
    wt = Path(root) / name
    wt.mkdir()
    (wt / "target.py").write_text("def f():\n    return 1\n")
    (wt / "test_fixture.py").write_text("assert True\n")
    (wt / "verify.sh").write_text("echo VERIFY_OK\n")
    (wt / ".gitignore").write_text("")
    g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], capture_output=True, check=True)
    g("init", "-q")
    g("-c", "user.email=t@t", "-c", "user.name=t", "add", "-A")
    g("-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "seal")
    # the LOSING attempt's own edit, made before the failure
    (wt / "target.py").write_text("def f():\n    return 2  # model attempt\n")
    return str(wt)


def future(p, dt=30):
    t = time.time() + dt
    os.utime(p, (t, t))


def main():
    m = load()
    root = tempfile.mkdtemp(prefix="he-test-")
    m.LOG_DIR = os.path.join(root, "logs")
    os.makedirs(m.LOG_DIR)
    m.save_state = lambda *a, **k: None   # execute()'s saves are irrelevant here

    def failed_slice(wt):
        """Drive the REAL transition hook: a slice entering FAILED from ENQUEUED."""
        s = {"status": m.FAILED, "worktree": wt, "job_id": "jobhe0000001",
             "depends_on": [], "intent": "x"}
        st = {"label": "he", "order": ["s1"], "slices": {"s1": s}, "repo": root,
              "dag_published": True}
        stamp = getattr(m, "_stamp_failed_tree", None)
        if stamp:
            stamp(st, {"slices": {"s1": {"status": m.ENQUEUED}}})
        return st, s

    he = getattr(m, "hand_edits_since_failure", None)
    chk("hand_edits_since_failure exists", callable(he), True)

    # ---- unit: what counts as a hand edit ----------------------------------------
    wt = mkwt(root, "u")
    st, s = failed_slice(wt)
    chk("entering FAILED fingerprints the losing attempt's dirty file",
        sorted((s.get("failed_tree") or {}).keys()), ["target.py"])
    if callable(he):
        chk("no edits since the failure -> none", he(s, wt), [])
        future(os.path.join(wt, "target.py"))
        chk("a touch without a content change -> none", he(s, wt), [])
        os.makedirs(os.path.join(wt, "__pycache__"))
        Path(wt, "__pycache__", "x.pyc").write_bytes(b"\0")
        future(os.path.join(wt, "__pycache__", "x.pyc"))
        chk("cache noise -> none", he(s, wt), [])
        Path(wt, "test_fixture.py").write_text("assert 1 == 1  # fixed by hand\n")
        future(os.path.join(wt, "test_fixture.py"))
        chk("fixture fixed in place after the failure -> hand edit", he(s, wt),
            ["test_fixture.py"])
        Path(wt, "helper.py").write_text("X = 1\n")
        future(os.path.join(wt, "helper.py"))
        chk("new file added by hand -> hand edit", he(s, wt), ["helper.py", "test_fixture.py"])
        # a gate that finished AFTER the transition may have written the tree: its
        # writes (older than its own .gate.json) are not hand edits
        wt2 = mkwt(root, "g")
        st2, s2 = failed_slice(wt2)
        Path(wt2, "target.py").write_text("def f():\n    return 3  # gate autofix\n")
        future(os.path.join(wt2, "target.py"), 10)
        gj = os.path.join(m.LOG_DIR, "jobhe0000001.gate.json")
        Path(gj).write_text("{}")
        future(gj, 20)
        chk("a late gate write (older than the job's .gate.json) -> none", he(s2, wt2), [])
        Path(gj).unlink()

    # ---- behaviour: execute()'s FAILED branch ------------------------------------
    calls = []
    m.ensure_chain_worktree = lambda st: os.path.join(root, "cwt")
    m.harvest_ready_slices = lambda st, cwt: []
    m.auto_land_passed = lambda st, cwt: None
    m.refresh_state = lambda st: st
    m._plan_cancelled = lambda label: None
    m.vacuous_gate_reason = lambda *a, **k: None
    m.any_dep_failed = lambda st, sid: False
    m.slice_job_inflight = lambda st, sid: None
    m.deps_done = lambda st, sid: True
    m.slice_job_awaiting_gate = lambda *a, **k: None
    m._land_green_slice = lambda *a, **k: (calls.append("land") or (False, "red"))

    class Stop(Exception):
        pass

    def _bound(st, sid, s, wt, cwt, origin):
        calls.append("clear")
        raise Stop()
    m.bound_stale_worktree_retry = _bound

    def run_execute(st):
        calls.clear()
        try:
            m.execute(st, "model", "host", 20, 600, "auto", None)
        except Stop:
            pass
        return list(calls)

    wt3 = mkwt(root, "e1")
    st3, s3 = failed_slice(wt3)
    Path(wt3, "test_fixture.py").write_text("assert 2 == 2  # fixed by hand\n")
    future(os.path.join(wt3, "test_fixture.py"))
    got = run_execute(st3)
    chk("execute(): FAILED slice WITH hand edits -> neither auto-landed nor cleared", got, [])
    chk("...and it stays FAILED, the hand edit intact",
        (s3["status"], "fixed by hand" in Path(wt3, "test_fixture.py").read_text()),
        (m.FAILED, True))
    wt4 = mkwt(root, "e2")
    st4, s4 = failed_slice(wt4)
    got = run_execute(st4)
    chk("execute(): FAILED slice with only the losing attempt's edits -> auto-heal runs",
        got, ["land", "clear"])
    subprocess.run(["rm", "-rf", root])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAIL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
