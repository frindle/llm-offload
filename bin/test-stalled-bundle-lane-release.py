#!/usr/bin/env python3
"""A COMMITTED bundle whose slicer-owned slices cannot advance (escalated + self-heal pending, or
runnable but the queue's own slicer kick left nothing), with no queue row, no live driver/marker and no
gate hook, must release its GPU lane at once instead of pinning it until the lease lapses
(2026-10-09, rt-costco-receipt-attach held every lane with the GPU idle).
Hermetic: temp slice-runs dir, in-memory state, fake clock, stubbed kick/alert/heal.
Run: python3 test-stalled-bundle-lane-release.py [--bin DIR] -> STALLED_LANE_RELEASE_OK"""
import argparse, importlib.util as u, json, os, sys, tempfile
from pathlib import Path
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ap = argparse.ArgumentParser(); ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
BIN = Path(ap.parse_args().bin).resolve()
sp = u.spec_from_file_location("q_sl", BIN / "ollama-queue.py"); q = u.module_from_spec(sp); sp.loader.exec_module(q)
fails = []
def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c: fails.append(n)

NOW = 2_000_000.0
C, D = "costco", "other"
runs = Path(tempfile.mkdtemp())
def write_plan(label, statuses, deps=None):
    order = list(statuses)
    sl = {s: {"status": st, "depends_on": (deps or {}).get(s, []), "escalation_reason": "relevance NO-GO"}
          for s, st in statuses.items()}
    (runs / f"{label}.json").write_text(json.dumps({"label": label, "order": order, "slices": sl}))
ST = {"s1": "escalated", "s2": "pending", "s3": "pending"}
DEPS = {"s2": ["s1"], "s3": ["s2"]}
write_plan(C, ST, DEPS)
heal = {"on": True}
q.escalation_heal_pending = lambda plan, sid, reason, seen, now, **kw: heal["on"]
alive = lambda pid: False

# 1. runnability: the escalated heal-pending slice and its dependents are flagged `heal`
pl = q.slice_plan_runnability(C, runs_dir=runs, alive=alive, now=NOW, esc_seen={}, cancelled=lambda l: None)
chk("runnability flags heal-pending slice + dependents", sorted(pl["heal"]) == [(C, "s1"), (C, "s2"), (C, "s3")] and len(pl["live"]) == 3)
pk = lambda j: j.get("bundle")
def st(plan=None, jobs=(), **kw):
    return q.bundle_commit_status(C, list(jobs), pk, plan if plan is not None else pl, {}, now=NOW, **kw)
r = st()
chk("heal-only plan, no row/driver/hook -> waiting (holds no lane), names slice+state",
    r[0] == "waiting" and "s1" in r[1] and "escalated" in r[1] and r[2] is False)
chk("a live pending row of the bundle still makes it working", st(jobs=[{"id": "p", "status": "pending", "bundle": C}])[0] == "working")
chk("a live slicer driver still makes it working", st({**pl, "driver_live": True})[0] == "working")
chk("a request marker still makes it working", st({**pl, "marker": True})[0] == "working")
chk("a settling gate hook still makes it working", st(settling=["g"])[0] == "working")
# runnable slicer work (no heal): working until the kick is proven futile
write_plan(C, {"s1": "pending", "s2": "pending"}, {"s2": ["s1"]})
pl2 = q.slice_plan_runnability(C, runs_dir=runs, alive=alive, now=NOW, esc_seen={}, cancelled=lambda l: None)
chk("untried runnable slicer work stays working", st(pl2)[0] == "working")
r = st(pl2, kick_stalled={C: 120.0})
chk("kick fired and left nothing -> blocked (loud park), names slice and state",
    r[0] == "blocked" and "s1" in r[1] and "pending" in r[1] and "120s" in r[1])
chk("kick on ANOTHER plan label does not block this one", st(pl2, kick_stalled={"zzz": 99.0})[0] == "working")

# 2. _kick_stalled: signature / grace / driver / marker
sig = q._plan_sig(pl2, C)
src = {"kicked": {C: NOW - 200}, "kick_sig": {C: sig}}
chk("kick_stalled: same sig past grace -> stalled", C in q._kick_stalled(pl2, src, NOW))
chk("kick_stalled: inside the grace -> not yet", q._kick_stalled(pl2, {"kicked": {C: NOW - 10}, "kick_sig": {C: sig}}, NOW) == {})
chk("kick_stalled: slice state changed since the kick -> not stalled", q._kick_stalled(pl2, {"kicked": {C: NOW - 200}, "kick_sig": {C: "[]"}}, NOW) == {})
chk("kick_stalled: live driver -> not stalled", q._kick_stalled({**pl2, "driver_live": True}, src, NOW) == {})
chk("kick_stalled: marker -> not stalled", q._kick_stalled({**pl2, "marker": True}, src, NOW) == {})

# 3. end to end through _apply_bundle_commit with a fake clock
def run(label_statuses, deps, ticks):
    write_plan(C, label_statuses, deps)
    state = {"jobs": [{"id": "d1", "status": "pending", "bundle": D, "label": "d1"}],
             "_bundle_commit": {"key": C, "since": NOW - 900, "empty_since": None, "idle_since": NOW - 5},
             "_bundle_parked": {}}
    alerts, kicks, out = [], [], []
    for t in ticks:
        k, ev = q._apply_bundle_commit(state, pk, None, [D, C], t, runs_dir=runs, chain_dir=runs, hooks={},
                                       cpu_outstanding={}, log_dir=runs,
                                       kick=lambda l, p: (kicks.append(l) or True),
                                       alert=lambda k_, w_, n_: alerts.append((k_, w_)))
        out.append((t, k, [e[0] for e in ev]))
    return state, alerts, kicks, out
# 3a. heal pending: lane released on the FIRST tick, silent park, no kick
state, alerts, kicks, out = run(ST, DEPS, [NOW, NOW + 30])
chk("3a heal-wait: other bundle gets the lane on the very first tick", out[0][1] == D and "cpu_wait" in out[0][2])
chk("3a parked kind cpu_wait with the slice named; no alert, no pointless kick",
    state["_bundle_parked"][C]["kind"] == "cpu_wait" and "s1" in state["_bundle_parked"][C]["why"] and not alerts and not kicks)
chk("3a stays parked (no bounce) on later ticks", out[1][1] == D and C in state["_bundle_parked"])
state["jobs"].append({"id": "re1", "status": "pending", "bundle": C, "label": "re1"})
k, ev = q._apply_bundle_commit(state, pk, None, [D, C], NOW + 60, runs_dir=runs, chain_dir=runs, hooks={},
                               cpu_outstanding={}, log_dir=runs, kick=lambda l, p: True, alert=lambda *a: None)
chk("3a the heal's row resumes the bundle FIRST", k == C and C not in state["_bundle_parked"])
# 3b. heal window ended -> stuck -> loud blocked park
heal["on"] = False
state2, alerts2, _k, out2 = run(ST, DEPS, [NOW])
chk("3b heal gone: loud park (alert) naming the slice, lane released", out2[0][1] == D and alerts2 and "s1" in alerts2[0][1])
heal["on"] = True
# 3c. runnable-but-futile slicer work: held until the kick is proven futile, then loud park, no bounce
PEND, PD = {"s1": "pending", "s2": "pending"}, {"s2": ["s1"]}
state3, alerts3, kicks3, out3 = run(PEND, PD, [NOW + 70, NOW + 90, NOW + 200, NOW + 230, NOW + 260])
chk("3c idle >=60s -> slicer kicked once", kicks3 == [C])
chk("3c still holds the lane inside the kick grace", out3[1][1] == C)
chk("3c past the grace with nothing changed: loud park + lane to the other bundle",
    out3[2][1] == D and alerts3 and "STALLED" in alerts3[0][1] and "s1" in alerts3[0][1])
chk("3c no park/resume bounce afterwards (kick record travels with the park)",
    out3[3][1] == D and out3[4][1] == D and all("resume" not in o[2] for o in out3[3:]) and len(alerts3) == 1)
state3["jobs"] = []; state3["_bundle_commit"] = None          # the other bundle finished: lane is free
k, ev = q._apply_bundle_commit(state3, pk, None, [C], NOW + 280, runs_dir=runs, chain_dir=runs, hooks={},
                               cpu_outstanding={}, log_dir=runs, kick=lambda l, p: True, alert=lambda *a: None)
chk("3c lane free + nothing changed: the stalled bundle is NOT re-committed (no bounce)", k is None and C in state3["_bundle_parked"])
write_plan(C, {"s1": "enqueued", "s2": "pending"}, PD)       # a human/event moved a slice
k, ev = q._apply_bundle_commit(state3, pk, None, [D, C], NOW + 300, runs_dir=runs, chain_dir=runs, hooks={},
                               cpu_outstanding={}, log_dir=runs, kick=lambda l, p: True, alert=lambda *a: None)
chk("3c a changed slice state resumes the bundle FIRST", k == C and C not in state3["_bundle_parked"])

# 4. wait text never claims live slicer ownership for a stalled/idle bundle
w = "3 slice(s) still owned by the slicer (next s1 confirmed)"
t1 = q.commit_wait_why(w, ("blocked", "slicer STALLED: slice s1 is pending", False), {})
chk("4 non-working commit status replaces the slicer claim", "still owned by the slicer" not in t1 and "STALLED" in t1)
t2 = q.commit_wait_why(w, ("working", "x", False), {"idle_since": NOW})
chk("4 idle committed bundle: text says IDLE, banner kind is not 'slicer'",
    "IDLE" in t2 and q.focus_wait_note("B", t2, True)["waiting_on"]["kind"] == "bundle")
chk("4 a moving/non-idle bundle keeps the original text", q.commit_wait_why(w, ("working", "x", True), {"idle_since": None}) == w)
print("STALLED_LANE_RELEASE_OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
