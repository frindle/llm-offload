#!/usr/bin/env python3
"""Unattended-readiness (2a): `ollama-dispatch-slice --regate` accepts a FAILED slice
(and, 2026-10-05, a PENDING slice whose worktree a human hand-locked).

A FAILED slice whose harness was fixed in place must be re-gateable without
deleting the worktree (--retry-slice) and before the FAILED auto-heal clears it.
ESCALATED keeps working; every other status is still refused; a missing worktree
or a live job is still refused. SLICE_SRC=<path> for the revert-test.
"""
import importlib.util
import os
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
    loader = SourceFileLoader("slice_ra", str(SRC))
    spec = importlib.util.spec_from_loader("slice_ra", loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(SRC)]
    loader.exec_module(m)
    return m


class Died(Exception):
    pass


def main():
    m = load()
    enq = []
    live = {}

    def _die(msg):
        raise Died(msg)
    m.die = _die
    m.save_state = lambda *a, **k: None
    m.slice_job_inflight = lambda st, sid: live.get(sid)
    m.confirm_and_enqueue = lambda st, sid, s, wt, *a, **k: enq.append((sid, s["status"], wt))
    wt = tempfile.mkdtemp(prefix="regate-wt-")

    def attempt(status, wtp=wt, sid="s1"):
        st = {"label": "ra", "order": [sid],
              "slices": {sid: {"status": status, "worktree": wtp}}}
        enq.clear()
        try:
            m.regate_slice(st, sid, "model", "host", 20, 600, "auto", None)
        except Died as e:
            return ("refused", str(e)[:80], st["slices"][sid]["status"])
        return ("regated", list(enq), st["slices"][sid]["status"])

    r = attempt(m.FAILED)
    chk("FAILED slice is re-gated (not refused)", r[0], "regated")
    chk("...through confirm_and_enqueue as CONFIRMED, same worktree",
        r[1], [("s1", m.CONFIRMED, wt)] if r[0] == "regated" else None)
    r = attempt(m.ESCALATED)
    chk("ESCALATED slice still re-gated", r[0], "regated")
    for bad in (m.PENDING, m.DONE, m.ENQUEUED, m.SKIPPED):
        r = attempt(bad)
        chk(f"{bad} slice still refused (and left {bad})", (r[0], r[2]), ("refused", bad))
    # PENDING + HAND-LOCKED (2026-10-05, rt-egift-link-s1-s0): --execute tells the
    # operator to "move past it with --regate"; --regate must accept it (and only it).
    lock = os.path.join(wt, m.HAND_LOCK)
    open(lock, "w").write("hand\n")
    r = attempt(m.PENDING)
    chk("hand-locked PENDING slice is re-gated (the advertised exit is not a dead end)",
        (r[0], r[1]), ("regated", [("s1", m.CONFIRMED, wt)]))
    os.unlink(lock)
    r = attempt(m.PENDING)
    chk("...an UNLOCKED pending slice is still refused", (r[0], r[2]), ("refused", m.PENDING))
    r = attempt(m.FAILED, wtp=os.path.join(wt, "gone"))
    chk("FAILED slice with its worktree gone: refused", r[0], "refused")
    live["s1"] = ("j1", "running", "label")
    r = attempt(m.FAILED)
    chk("FAILED slice with a live job: refused, status untouched", (r[0], r[2]),
        ("refused", m.FAILED))
    os.rmdir(wt)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAIL")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
