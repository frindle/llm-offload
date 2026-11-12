#!/usr/bin/env python3
"""Regression test: a bundle whose ONLY rows are held/paused by a human releases the lanes AT
ONCE, and the human focus override takes effect (2026-10-12, LLM-only pipeline plan phase 4).

The defect (probe): bundle_commit_status read ('working', moving=True) for a bundle with one
user_hold row plus a `continue` next-step declaration -- and likewise for a waiting chain, a live
slicer plan, an advancing driver or a settling gate. A 'working' committed bundle keeps every
lane (30 min next-step hold, 10-45 min idle ceiling) while other bundles wait with nothing to run.
Now: held-only -> 'waiting' (holds no lane, parked cpu_wait silently, resumes FIRST when a
runnable row exists). Everything else is the unchanged core.
Run: python3 test-bundle-lane-release.py      prints ALL PASS
"""
import importlib.util
import os
import sys
import time
from importlib.machinery import SourceFileLoader
from pathlib import Path

QUEUE = Path(os.environ.get("QUEUE_SRC") or Path(__file__).resolve().parent / "ollama-queue.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    sys.argv = ["x"]
    loader = SourceFileLoader("q_blr", str(QUEUE))
    spec = importlib.util.spec_from_loader("q_blr", loader)
    q = importlib.util.module_from_spec(spec)
    loader.exec_module(q)
    pk = lambda j: j.get("bundle")
    now = time.time()
    n = [0]

    def J(**k):
        n[0] += 1
        return dict({"id": "j%d" % n[0], "bundle": "B", "label": "x", "status": "pending"}, **k)

    ns = {"kind": "continue", "at": now - 10, "expires_at": now + 1500, "actor": "a", "label": "l"}

    def S(jobs, **kw):
        return q.bundle_commit_status("B", jobs, pk, now=now, **kw)

    held = lambda: [J(status="held", user_hold=True)]
    for name, kw in (("next-step hold", {"next_step": ns}),
                     ("waiting chain", {"chain": {"waiting": True, "why": "w"}}),
                     ("runnable slicer plan", {"plan": {"live": [("p", "s1", "pending")], "known": True}}),
                     ("live chain driver", {"chain": {"driver_live": True}}),
                     ("live slicer driver", {"plan": {"driver_live": True}}),
                     ("settling gate", {"settling": ["g"]})):
        st = S(held(), **kw)
        check(f"held-only + {name} -> waiting, holds no lane", (st[0], st[2]), ("waiting", False))
    paused = [J(status="paused", pause_reason="manual")]
    check("human-PAUSED-only + next-step -> waiting", S(paused, next_step=ns)[0], "waiting")
    check("fit_hold-only + next-step -> waiting", S([J(fit_hold=True)], next_step=ns)[0], "waiting")
    # unchanged cases
    check("held-only with nothing else is still BLOCKED (loud park, unchanged)", S(held())[0], "blocked")
    check("held + a runnable pending row -> working", S(held() + [J()])[0], "working")
    check("held + a RUNNING row -> working", S(held() + [J(status="running")])[0], "working")
    check("gate-preempt pause is NOT human-held -> working",
          S([J(status="paused", pause_reason=q.GATE_PREEMPT_REASON)])[0], "working")
    check("a bundle with no held rows + next-step still holds the lane",
          S([], next_step=ns)[0], "working")
    check("pending row behind a held upstream is not runnable -> still waiting (not working)",
          S([J(id="up", status="held", user_hold=True), J(after="up")], next_step=ns)[0], "waiting")
    check("held_only_rows returns [] when a live row exists", q.held_only_rows("B", held() + [J()], pk), [])

    # ---- the commitment: the held bundle yields and another bundle backfills IN THE SAME TICK
    jobs = [J(bundle="A", status="held", user_hold=True), J(bundle="B")]
    pkb = lambda j: j.get("bundle")

    def status_of(k):
        return q.bundle_commit_status(k, jobs, pkb, next_step=ns if k == "A" else None, now=now)

    commit = {"key": "A", "since": now - 5, "empty_since": None, "idle_since": None}
    parked = {}
    c2, ev = q.bundle_commit_step(commit, parked, None, ["B"], status_of, now, skip_grace=lambda k: True)
    check("committed held-only bundle A is parked cpu_wait (silent) and B commits at once",
          ((c2 or {}).get("key"), [e[0] for e in ev], (parked.get("A") or {}).get("kind")),
          ("B", ["cpu_wait", "commit"], "cpu_wait"))
    # A's row is resumed by the human -> A resumes FIRST, ahead of any new bundle
    jobs[0].update({"status": "pending", "user_hold": False})
    c3, ev3 = q.bundle_commit_step(None, parked, None, ["B"], status_of, now + 1, skip_grace=lambda k: True)
    check("when its row is released the parked bundle resumes first", ((c3 or {}).get("key"), [e[0] for e in ev3]), ("A", ["resume"]))

    # ---- the human focus override takes effect even over a held-only commitment
    jobs2 = [J(bundle="A", status="held", user_hold=True), J(bundle="B")]
    st2 = lambda k: q.bundle_commit_status(k, jobs2, pkb, next_step=ns if k == "A" else None, now=now)
    c4, ev4 = q.bundle_commit_step({"key": "A", "since": now - 5, "empty_since": None, "idle_since": None},
                                   {}, None, ["B"], st2, now, override_key="B", skip_grace=lambda k: True)
    check("focus override on B takes the lane from held-only A", ((c4 or {}).get("key"), ev4[0][0]), ("B", "yield"))
    c5, ev5 = q.bundle_commit_step({"key": "B", "since": now - 5, "empty_since": None, "idle_since": None},
                                   {}, None, ["B"], st2, now, override_key="A", skip_grace=lambda k: True)
    check("an override on a held-only bundle is ignored (nothing to run) and B keeps the lane",
          (c5 or {}).get("key"), "B")

    print("\nALL PASS" if not FAILS else "\nFAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
