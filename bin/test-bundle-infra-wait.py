#!/usr/bin/env python3
"""Regression test: a committed bundle whose only rows sit in an INFRA WAIT releases the lanes
(2026-11-15, smoke-darkbloom-clamp-val: one Darkbloom validate row waited 18 min behind a Darkbloom
sandbox hold and held the Unraid lane; QUEUE-IDLE fired).

The defect: _bundle_commit_status_core returned ('working', moving=True) for ANY pending live row, so
bundle_commit_step renewed the H4 lease every tick and never parked the bundle. Now, with
`infra_wait`, a bundle whose live rows are ALL pending rows in an INFRA WAIT (nothing running, no live
driver) is 'waiting': parked cpu_wait (silent), other bundles commit, and it resumes FIRST when the
wait clears. The QUEUE-IDLE alert does not count rows in an INFRA WAIT.
Run: python3 test-bundle-infra-wait.py      prints ALL PASS
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
    loader = SourceFileLoader("q_biw", str(QUEUE))
    spec = importlib.util.spec_from_loader("q_biw", loader)
    q = importlib.util.module_from_spec(spec)
    loader.exec_module(q)
    pk = lambda j: j.get("bundle")
    now = time.time()
    n = [0]

    def J(**k):
        n[0] += 1
        return dict({"id": "j%d" % n[0], "bundle": "B", "label": "x", "status": "pending"}, **k)

    hold = q.wait_note("darkbloom-sandbox", "darkbloom sandbox hold since 10:17", "INFRA WAIT: sandbox hold",
                       {"kind": "infra", "detail": "darkbloom sandbox hold"})
    waiting_ids = set()
    iw = lambda j: hold if j.get("id") in waiting_ids else None

    def S(jobs, **kw):
        return q.bundle_commit_status("B", jobs, pk, now=now, **kw)

    # ---- the status
    r1 = J()
    waiting_ids.add(r1["id"])
    st = S([r1], infra_wait=iw)
    check("only row in an INFRA WAIT -> waiting, not moving", (st[0], st[2]), ("waiting", False))
    check("the reason names the infra wait", "INFRA WAIT" in st[1] and "sandbox hold" in st[1], True)
    check("without infra_wait the old answer stands (working, moving)", S([r1])[0::2], ("working", True))
    r2 = J()
    check("one infra row + one launchable row -> working", S([r1, r2], infra_wait=iw)[0::2], ("working", True))
    r3 = J()
    waiting_ids.add(r3["id"])
    check("two rows, both in an INFRA WAIT -> waiting", S([r1, r3], infra_wait=iw)[0], "waiting")
    check("infra row + a RUNNING row -> working",
          S([r1, J(status="running")], infra_wait=iw)[0::2], ("working", True))
    check("infra row + a live chain driver -> working (the driver may enqueue)",
          S([r1], infra_wait=iw, chain={"driver_live": True})[0::2], ("working", True))
    check("infra row + a live slicer driver -> working",
          S([r1], infra_wait=iw, plan={"driver_live": True})[0::2], ("working", True))
    boom = lambda j: (_ for _ in ()).throw(RuntimeError("x"))
    check("an infra_wait that raises counts the row as live work (old behaviour)",
          S([r1], infra_wait=boom)[0::2], ("working", True))
    check("held-only rule still wins over the infra rule",
          S([J(status="held", user_hold=True)], infra_wait=iw, next_step={"kind": "continue", "at": now - 5,
                                                                            "expires_at": now + 900})[0], "waiting")

    # ---- the commitment: B (infra-only) yields, A commits in the same tick; B resumes first when the wait clears
    a1 = J(bundle="A")
    jobs = [r1, a1]
    st_of = lambda k: q.bundle_commit_status(k, jobs, pk, now=now, infra_wait=iw)
    commit = {"key": "B", "since": now - 600, "empty_since": None, "idle_since": None}
    parked = {}
    c2, ev = q.bundle_commit_step(commit, parked, None, ["A"], st_of, now, skip_grace=lambda k: True)
    check("committed infra-only bundle B parks cpu_wait (silent) and A commits at once",
          ((c2 or {}).get("key"), [e[0] for e in ev], (parked.get("B") or {}).get("kind")),
          ("A", ["cpu_wait", "commit"], "cpu_wait"))
    # A finishes; the hold is released: B resumes first
    jobs.remove(a1)
    waiting_ids.discard(r1["id"])
    c3, ev3 = q.bundle_commit_step(None, parked, None, ["B"], st_of, now + 60, skip_grace=lambda k: True)
    check("when the wait clears the parked bundle resumes", ((c3 or {}).get("key"), [e[0] for e in ev3][:1]),
          ("B", ["resume"]))
    # the old behaviour, for contrast: no infra_wait -> B keeps the lane while A waits
    jobs2 = [J(bundle="B", id="b9"), J(bundle="A", id="a9")]
    st_old = lambda k: q.bundle_commit_status(k, jobs2, pk, now=now)
    c4, _ev4 = q.bundle_commit_step({"key": "B", "since": now - 600, "empty_since": None, "idle_since": None},
                                    {}, None, ["A"], st_old, now, skip_grace=lambda k: True)
    check("(old behaviour, no infra_wait) B keeps the lane", (c4 or {}).get("key"), "B")

    # ---- infra_seen_update
    seen = {}
    jw = {"p1": hold, "p2": q.wait_note("lane-busy", "lane busy"), "p3": hold}
    rows = [{"id": "p1", "status": "pending"}, {"id": "p2", "status": "pending"}, {"id": "p3", "status": "running"}]
    q.infra_seen_update(seen, jw, rows)
    check("infra_seen_update keeps pending rows' infra notes only", sorted(seen), ["p1"])
    q.infra_seen_update(seen, {"p1": q.wait_note("focus", "behind bundle X")}, rows)
    check("a later non-infra note (e.g. focus) keeps the entry (the loop skipped the infra check)", sorted(seen), ["p1"])
    q.infra_seen_update(seen, {}, [{"id": "p1", "status": "running"}])
    check("a row that left pending is dropped", seen, {})
    tree = q.wait_note("tree-lock", "worktree tree lock held")
    q.infra_seen_update(seen, {"p1": tree}, rows)
    check("a non-infra code is never recorded", seen, {})

    # ---- infra_wait_recheck dispatches on the code and fails open
    q._darkbloom_url = lambda: "http://db.invalid:8000"
    q._darkbloom_model = lambda m: m
    calls = []
    q.darkbloom_launch_wait = lambda dbm, t: calls.append(dbm) or hold
    check("recheck of a darkbloom code asks darkbloom_launch_wait",
          (q.infra_wait_recheck({"id": "x", "model": "m1"}, hold, now) is hold, calls), (True, ["m1"]))
    q.darkbloom_launch_wait = lambda dbm, t: None
    check("a cleared darkbloom wait rechecks as None", q.infra_wait_recheck({"id": "x", "model": "m1"}, hold, now), None)
    q.darkbloom_launch_wait = lambda dbm, t: (_ for _ in ()).throw(RuntimeError("probe"))
    check("a raising recheck fails open (None)", q.infra_wait_recheck({"id": "x", "model": "m1"}, hold, now), None)
    q._darkbloom_url = lambda: None
    check("no Darkbloom lane configured -> None", q.infra_wait_recheck({"id": "x", "model": "m1"}, hold, now), None)
    pa = q.wait_note("infra-path-access", "infra: path held", "x", {"kind": "infra", "detail": "path held"})
    q.path_access_wait = lambda j, t: pa
    check("recheck of a path-access code asks path_access_wait", q.infra_wait_recheck({"id": "x"}, pa, now), pa)
    check("an unknown code rechecks as None", q.infra_wait_recheck({"id": "x"}, {"code": "lane-busy"}, now), None)

    # ---- QUEUE-IDLE counts only rows not in an INFRA WAIT
    rows = [{"id": "i1", "status": "pending"}, {"id": "i2", "status": "pending"}, {"id": "i3", "status": "done"}]
    only_i1 = lambda j: hold if j["id"] == "i1" else None
    check("idle_alert_pending drops infra-waiting rows",
          [j["id"] for j in q.idle_alert_pending(rows, only_i1)], ["i2"])
    check("idle_alert_pending without infra_wait counts every pending row",
          [j["id"] for j in q.idle_alert_pending(rows)], ["i1", "i2"])
    st_idle = {}
    check("all pending rows in an INFRA WAIT -> no QUEUE-IDLE",
          q.idle_pending_alert_due(st_idle, False, len(q.idle_alert_pending(rows[:1], only_i1)), now - 4000)
          or q.idle_pending_alert_due(st_idle, False, len(q.idle_alert_pending(rows[:1], only_i1)), now), False)

    # ---- the daemon wires it: the commit passes infra_wait, the launch loop records, the alert filters
    src = QUEUE.read_text()
    check("_apply_bundle_commit passes infra_wait to bundle_commit_status",
          "infra_wait=(infra_wait if infra_wait is not None else" in src, True)
    check("the launch loop records infra waits (infra_seen_update)",
          "infra_seen_update(_INFRA_SEEN, _job_wait, state[\"jobs\"])" in src, True)
    check("the QUEUE-IDLE alert counts idle_alert_pending", "_ipend = idle_alert_pending(state[\"jobs\"]" in src, True)

    print("\nALL PASS" if not FAILS else "\nFAILED: %s" % FAILS)
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
