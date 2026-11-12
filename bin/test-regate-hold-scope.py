#!/usr/bin/env python3
"""Regression tests for Bug #3 (regate hold blast radius) and Bug #6 (the gate must
actually GET the lane, not merely hold others) -- ollama-queue.py.

Bug #3: the hard hold-on-regate barrier (the owner 2026-09-17) flipped EVERY fresh
authoring/refine/coding job to `held` while any gate/regate was unresolved. One
studio regate thereby froze ~24 unrelated jobs, including Unraid-bound ones that
can neither contend for the studio lane nor move the studio gate's worktree. The
fix scopes the hold to the gate's lane: a job PINNED to a different concrete lane is
not held. Auto/unpinned jobs stay held (conservative -- preserves the barrier for
the common single-GPU case).

Bug #6: holding everyone else does not get the GATE the lane if a coding job is
already running on it. The gate must PREEMPT to the lane. This asserts the
mechanism that delivers that: a pending studio regate preempts a running coding
job (_gate_preempt_victim), and the launch order puts the gate AHEAD of fresh
coding work (pending_launch_order) so it claims the freed lane first.

Red-on-revert:
  - Bug #3: revert _pending_gate_hold to `return (True, gate)` unconditionally ->
    the "different-lane pin is NOT held" assertion fails.
  - Bug #6: if _gate_preempt_victim stopped returning the running coding job, or
    pending_launch_order stopped ordering the regate ahead of fresh work, those
    assertions fail.

Run: python3 test-regate-hold-scope.py
"""
import importlib.util
import sys
from pathlib import Path

QUEUE = Path(__file__).resolve().parent / "ollama-queue.py"


def _load():
    spec = importlib.util.spec_from_file_location("oq_holdscope", QUEUE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def test_hold_scope(q):
    regate = {"id": "regate-abc", "label": "regate-abc", "status": "pending",
              "host_pref": "studio", "lane": None}
    jobs = [regate]

    def coding(host_pref):
        return {"id": "c1", "label": "auto-author-x", "status": "pending",
                "task_kind": "coding", "host_pref": host_pref}

    # Same lane as the gate (studio) -> HELD.
    hold, g = q._pending_gate_hold(coding("studio"), jobs)
    ok("hold-scope: a studio-pinned coding job IS held by a studio regate", hold is True)

    # Different concrete lane (unraid) -> NOT held (the Bug #3 fix).
    hold, g = q._pending_gate_hold(coding("unraid"), jobs)
    ok("hold-scope: an unraid-pinned coding job is NOT held by a studio regate",
       hold is False)

    # Auto/unpinned -> conservatively still held.
    hold, g = q._pending_gate_hold(coding("auto"), jobs)
    ok("hold-scope: an auto/unpinned coding job stays held (conservative)", hold is True)
    hold, g = q._pending_gate_hold(coding(None), jobs)
    ok("hold-scope: a host_pref=None coding job stays held (conservative)", hold is True)

    # No unresolved gate -> never held.
    hold, g = q._pending_gate_hold(coding("studio"), [])
    ok("hold-scope: no gate -> not held", hold is False)

    # A resume (in-flight) job is never fresh -> never held.
    resume = coding("studio")
    resume["resume_transcript"] = "/t/x.json"
    hold, g = q._pending_gate_hold(resume, jobs)
    ok("hold-scope: a resuming job is never held (would strand in-flight work)",
       hold is False)


def test_gate_gets_lane(q):
    w = q.worker()
    from datetime import datetime, timedelta, timezone
    old = (datetime.now(timezone.utc) - timedelta(
        seconds=q.PREEMPT_MIN_PROGRESS_S + 60)).isoformat()

    longcoder = {"id": "lc", "label": "auto-author-demo", "status": "running",
                 "task_kind": "coding", "model": q.DARKBLOOM_DEFAULT_MODEL,
                 "host_pref": "studio", "lane": "studio", "pid": 1, "launched_at": old}
    regate = {"id": "regate-lc", "label": "regate-lc", "status": "pending",
              "model": "qwen3.8:27b-q4_K_M", "host_pref": "studio",
              "lane": None, "pid": 2}

    # Bug #6: the gate PREEMPTS the running coding job to get the lane (not just holds).
    v = q._gate_preempt_victim(regate, [regate, longcoder], w)
    ok("gate-gets-lane: pending studio regate preempts the running coding job",
       v is longcoder)

    # Bug #6: the freed lane is claimed by the GATE first -- the launch order puts the
    # regate ahead of a fresh coding job.
    # Depth-first by bundle (2026-09-18): the gate outranks fresh coding WITHIN its own
    # plan (its parent `lc`'s bundle). A different plan's job is rightly ordered by plan
    # focus, so the fresh job here is the same plan's next authoring round.
    fresh = {"id": "f1", "label": "auto-author-demo-c1", "status": "pending",
             "task_kind": "coding"}
    order = q.pending_launch_order([fresh, regate, longcoder])
    ok("gate-gets-lane: launch order puts the regate ahead of fresh coding work",
       order.index(regate) < order.index(fresh))


def main():
    q = _load()
    # Hermetic: the studio lane IS the Darkbloom endpoint, which exists only when a
    # local.json record is present. Point the queue at a fixture record rather than
    # depending on the operator's live ~/.darkbloom (absent under a fake $HOME).
    import tempfile
    fx = Path(tempfile.mkdtemp(prefix="regate-hold-")) / "local.json"
    fx.write_text('{"api_key": "fixture-key"}')
    q.DARKBLOOM_LOCAL_JSON = fx
    test_hold_scope(q)
    test_gate_gets_lane(q)
    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nall regate-hold-scope tests passed")


if __name__ == "__main__":
    main()
