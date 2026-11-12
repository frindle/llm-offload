#!/usr/bin/env python3
"""Regression test: every job gets a bundle at enqueue + a failure-waiting bundle holds
no lane (the owner 2026-10-02).

Default bundle (enqueue_bundle_key / default_bundle_from_label):
  * explicit --bundle wins (enqueue only derives when the row has none);
  * gate-/regate-/secondop-<parent id> inherit the parent's stamped bundle;
  * esc-review-<ts>-<plan>-sN-... joins the plan's bundle;
  * a standalone label drops -sN/-rN/-cN, so x, x-r2, x-c1, x-s3 are ONE bundle;
  * nothing left -> the job id.
Lane release (commit_waiting_on_failure + the cmd_run wiring):
  * committed bundle, nothing queued, next slice failed/escalated/parked -> no hold;
  * a pending/running GATE of the bundle keeps the hold (gates exempt);
  * a live slicer driver / a pending job / a normal pending slice keeps the hold;
  * auto single-job bundle that finished is "complete", never "blocked".
Run: python3 test-bundle-default-focus.py [--revert-check]
"""
import importlib.util
import inspect
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

QUEUE = Path(os.environ.get("QUEUE_SRC") or Path(__file__).resolve().parent / "ollama-queue.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    loader = SourceFileLoader("q_bdf", str(QUEUE))
    spec = importlib.util.spec_from_loader("q_bdf", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main():
    q = load()
    logd = tempfile.mkdtemp()
    rev = {"plan-a-s1-x": "plan-a", "plan-a-s2-y": "plan-a"}
    jobs = [{"id": "p1", "label": "plan-a-s1-x", "bundle": "plan-a", "status": "done"},
            {"id": "p2", "label": "solo-fix", "bundle": "custom-tag", "status": "done"}]
    ek = lambda lab, jid="NEWID": q.enqueue_bundle_key({"label": lab, "id": jid}, jobs,
                                                       reverse=rev, log_dir=logd)
    print("== default bundle ==")
    check("gate inherits parent", ek("gate-p1"), "plan-a")
    check("regate inherits parent", ek("regate-p2"), "custom-tag")
    check("secondop inherits parent", ek("secondop-p2"), "custom-tag")
    check("esc-review joins the plan's bundle",
          ek("esc-review-20261001T230128Z-plan-a-s1-x"), "plan-a")
    check("slice author/refine rows -> plan", ek("auto-refine-plan-a-s2-y-r3"), "plan-a")
    check("standalone -rN stripped", ek("rt-941-fix-r2"), "rt-941-fix")
    check("standalone -cN stripped", ek("rt-941-fix-c1"), "rt-941-fix")
    check("standalone -sN stripped", ek("foo-bar-s3"), "foo-bar")
    check("standalone plain label is its own bundle", ek("rt-941-fix"), "rt-941-fix")
    check("auto-fix annotation ignored", ek("rt-941-fix [auto-fix r2]"), "rt-941-fix")
    check("nothing left -> job id", q.default_bundle_from_label("-r2"), "")
    check("...and enqueue then uses the job id", ek("-r2", "JID123"), "JID123")
    src = inspect.getsource(q.cmd_enqueue) if hasattr(q, "cmd_enqueue") else QUEUE.read_text()
    check("enqueue only derives when the row has no bundle (explicit wins)",
          "if not job.get(BUNDLE_FIELD):" in src and "enqueue_bundle_key(job, state[\"jobs\"])" in src, True)

    print("== failure-waiting bundle holds no lane ==")
    pk = lambda j: j.get("bundle")
    plan_fail = {"known": True, "live": [("ev", "s3", "failed")], "driver_live": False}
    plan_esc = {"known": True, "live": [("ev", "s3", "escalated (self-heal pending)")]}
    plan_ok = {"known": True, "live": [("ev", "s4", "pending")]}
    none = []
    check("next slice failed, nothing queued -> no hold",
          q.commit_waiting_on_failure("ev", none, pk, plan_fail), True)
    check("next slice escalated (self-heal pending) -> no hold",
          q.commit_waiting_on_failure("ev", none, pk, plan_esc), True)
    check("a normal pending slice keeps the hold",
          q.commit_waiting_on_failure("ev", none, pk, plan_ok), False)
    check("a pending GATE of the bundle keeps the hold (gates exempt)",
          q.commit_waiting_on_failure("ev", [{"bundle": "ev", "label": "gate-x", "status": "pending"}],
                                      pk, plan_fail), False)
    check("a running job keeps the hold",
          q.commit_waiting_on_failure("ev", [{"bundle": "ev", "status": "running"}], pk, plan_fail), False)
    check("a live slicer driver keeps the hold",
          q.commit_waiting_on_failure("ev", none, pk, dict(plan_fail, driver_live=True)), False)
    check("a terminal row of the bundle does not keep it",
          q.commit_waiting_on_failure("ev", [{"bundle": "ev", "status": "failed"}], pk, plan_fail), True)
    st = q.bundle_commit_status("solo", [{"bundle": "solo", "status": "done", "label": "solo"}],
                                pk, {"known": False}, {})
    check("auto single-job bundle that finished is complete, not blocked", st[0], "complete")
    # end-to-end through _apply_bundle_commit with a real slice-runs dir
    import json
    rd = Path(tempfile.mkdtemp())
    (rd / "ev.json").write_text(json.dumps({"label": "ev", "order": ["s3"],
                                            "slices": {"s3": {"status": "failed"}}}))
    state = {"jobs": [{"id": "o1", "bundle": "other", "status": "pending", "label": "other"}],
             "_bundle_commit": {"key": "ev", "since": 0, "empty_since": None, "idle_since": None}}
    os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
    k, _ev = q._apply_bundle_commit(state, pk, None, ["other"], 100.0, kick=lambda *a: False,
                                    alert=lambda *a: None, runs_dir=rd, chain_dir=rd)
    check("apply: commitment kept on ev", k, "ev")
    check("apply: marks it waiting_on_failure", (state.get("_bundle_commit") or {}).get("waiting_on_failure"), True)
    # POLICY REVERSED (the owner 2026-10-05, no-bounce): a committed bundle holds the
    # lanes UNCONDITIONALLY; waiting_on_failure is a logged state only. A heal that
    # never comes is parked loudly by BUNDLE_IDLE_CEILING (test-queue-bundle-no-bounce.py).
    _hold = q.commit_hold_decision(state.get("_bundle_commit"))
    check("cmd_run KEEPS the hold for a waiting_on_failure commitment",
          (_hold, "_hold = commit_hold_decision(" in inspect.getsource(q.cmd_run)), (True, True))
    check("...so focus_skips_job keeps every other bundle off the lane",
          q.focus_skips_job({"id": "o1"}, "other", "ev", _hold, "ev", set(), None, None), True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("secondop not inherited", 'm = re.match(r"^(?:gate|regate|secondop)-(.+)$", lab)\n    if m:\n        parent = by_id',
     'm = re.match(r"^(?:gate|regate)-(.+)$", lab)\n    if m:\n        parent = by_id'),
    ("no decoration stripping", "        k = default_bundle_from_label(k) or (job or {}).get(\"id\") or k",
     "        k = k"),
    ("esc-review not mapped", "        probe = dict(job, label=em.group(1))", "        probe = job"),
    ("failure-waiting still holds", "    return all(str(stt).split(\" \")[0].lower() in _WAITING_FAILURE_SLICE_STATES",
     "    return False and all(str(stt).split(\" \")[0].lower() in _WAITING_FAILURE_SLICE_STATES"),
    ("gate does not keep hold", "            if pk(j) == key and (j.get(\"status\") in _LIVE_ROW_STATES",
     "            if pk(j) == key and not str(j.get('label','')).startswith('gate-') and (j.get(\"status\") in _LIVE_ROW_STATES"),
    ("hold not lifted in cmd_run",
     '                _hold = not (state.get("_bundle_commit") or {}).get("waiting_on_failure")',
     '                _hold = True or not (state.get("_bundle_commit") or {}).get("waiting_on_failure")'),
]


def revert_check():
    bad = 0
    src = QUEUE.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut.py", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "QUEUE_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
