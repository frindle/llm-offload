#!/usr/bin/env python3
"""test-queue-gate-priority.py -- behavioural test of GATE PRIORITY under a bundle
commitment + the studio-db coding+gate SLOT model (the owner 2026-10-06).

Rules proven here (all on STUBBED state; DISPATCH_VERIFY_SANDBOX=1; the real queue,
its state file, darkbloom and every lane are never touched):
  1. the committed bundle's gate launches on an idle lane while its coding job runs
     elsewhere (any lane the model fits), and sorts FIRST (ahead of its own coding job
     and of other bundles' gates);
  2. another bundle's GATE may use an idle lane, but only if the committed bundle has no
     job for that lane, never a busy lane, never a swap/eviction, never a preemption;
  3. another bundle's NON-gate jobs stay held;
  4. gates sort ahead of other pending work;
  5. studio_slot_decision (pure): coding(1)+gate(1) on the one warm model, <=2 total,
     serving cap, memory headroom, kill switch gate_slots=0;
  6. measurement + config plumbing, and the cmd_run wiring.

Usage: test-queue-gate-priority.py [--queue PATH]. The revert test passes the
~/bin/ollama-queue.py.bak-*-gateprio backup and must FAIL. Exit 0 = all pass.
"""
import argparse
import importlib.machinery
import importlib.util
import inspect
import json
import os
import sys
import tempfile
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ.pop("GATE_SLOTS", None)

RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def guarded(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001 -- an API the old code lacks is a FAIL, not a crash
        RESULTS.append(False)
        print(f"FAIL {name}: raised {type(e).__name__}: {e}")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


STUDIO, UNRAID = "studio-db", "unraid"
M = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
CK = "bundle-A"


def J(id_, label, bundle, status="pending", lane=None, model=M, host="auto", **kw):
    return dict(id=id_, label=label, bundle=bundle, status=status, lane=lane, model=model,
                host_pref=host, num_ctx=65536, **kw)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(Path.home() / "bin" / "ollama-queue.py"))
    a = ap.parse_args()
    tmp = Path(tempfile.mkdtemp(prefix="q-gateprio-"))
    q = load(a.queue, "oq_gateprio")
    q.STATE_PATH, q.LOCK_PATH, q.LOG_DIR = tmp / "state.json", tmp / "state.lock", tmp / "logs"
    pk = lambda j: j.get("bundle")

    # --- stub the lane/host plumbing (module globals; nothing real is probed) ---------
    sizes = {M: 24e9}
    hosts = {"studio": {"url": STUDIO, "usable_bytes": 50e9},
             "unraid": {"url": UNRAID, "usable_bytes": 30e9}}
    q._hosts_table = lambda w: hosts
    q._host_url_for = lambda w, name, default=None: (hosts.get(name) or {}).get("url", default)
    q._host_budget_or_zero = lambda w, name: int((hosts.get(name) or {}).get("usable_bytes") or 0)
    q._model_size_cached = lambda w, model: sizes.get(model)
    q._lane_name = lambda u: u
    q._is_darkbloom_only_model = lambda m: False
    q._darkbloom_model = lambda m: m
    q._candidate_lanes = lambda job, w: [UNRAID] if job.get("host_pref") == "unraid" else [STUDIO]
    st = {"unfinished": 1, "usable_gb": 22.6}
    q.darkbloom_status = lambda *a_, **k_: st
    q.darkbloom_slot_cap = lambda s_, m=None: 4
    q._darkbloom_model_config = lambda m: {"stub": 1}
    q.kv_bytes_per_token = lambda cfg: 20480
    CFG = {"gate_slots": 1, "coding_slots": 1, "max_total": 2, "reserve_gb": 3.0}

    coder = J("c1", "rt-x-s1", CK, "running", STUDIO)
    own_gate = J("g1", "gate-c1", CK, host="unraid")
    own_regate = J("g2", "regate-c1", CK, host="studio")
    own_coding = J("c2", "rt-x-s2", CK)
    foreign_gate = J("fg", "gate-zzz", "bundle-B", host="unraid")
    foreign_job = J("fj", "rt-y-s1", "bundle-B")
    foreign_auth = J("fa", "auto-author-y", "bundle-B")

    # ---- 4. ordering --------------------------------------------------------------
    def s_order():
        order = [foreign_job, own_coding, foreign_gate, foreign_auth, own_gate, own_regate]
        got = [j["id"] for j in q.gate_priority_order(order, CK, pk)]
        check("4 order: committed gates FIRST (ahead of own coding job and other bundles' gates), "
              "then other bundles' gates, then everything else (stable)",
              got, ["g1", "g2", "fg", "fj", "c2", "fa"])
        check("4 order: no commitment -> unchanged",
              [j["id"] for j in q.gate_priority_order(order, None, pk)], [j["id"] for j in order])
        sg = J("s1", "secondop-c1", CK)
        rv = J("r1", "esc-review-20261006T000000Z-x", CK)
        check("4 order: secondop-/esc-review- rows are gate-family too",
              [j["id"] for j in q.gate_priority_order([own_coding, sg, rv], CK, pk)], ["s1", "r1", "c2"])
        check("4 order: a gate stamped with the committed tag counts as committed even if its "
              "parent row resolves elsewhere",
              [j["id"] for j in q.gate_priority_order(
                  [foreign_gate, dict(own_gate, id="gp")], CK,
                  lambda j: "pruned-parent" if j["id"] == "gp" else j["bundle"])], ["gp", "fg"])
    guarded("4 order", s_order)

    # ---- 3 + focus ----------------------------------------------------------------
    def s_focus():
        sk = lambda job, **kw: q.focus_skips_job(job, pk(job), CK, True, CK, set(), None, None, **kw)
        check("3 other bundle's NON-gate job stays held (even with gate_ok / backfill)",
              (sk(foreign_job, gate_ok=True), sk(foreign_auth, gate_ok=True),
               sk(foreign_job, gate_ok=True, backfill_ok=True)), (True, True, True))
        check("3 ...and the committed bundle's own jobs are never skipped",
              (sk(own_coding), sk(own_gate)), (False, False))
        check("2 another bundle's gate is not focus-held (the launch loop confines its lane)",
              sk(foreign_gate, gate_ok=True), False)
        check("2 ...and the old behaviour (gate_ok off) still holds it",
              sk(foreign_gate), True)
    guarded("3 focus", s_focus)

    # ---- 1. committed gate launches on an idle lane while the coder runs -----------
    def s_commit_gate():
        jobs = [coder, own_regate, own_gate]
        urls = q.gate_alt_lane_urls(own_regate, None)
        check("1 committed regate pinned to studio-db: the fitting idle lane (unraid) is added "
              "as a candidate", urls, [UNRAID])
        sizes[M] = 40e9
        check("1 ...but only when the model provably FITS that lane (40GB > unraid 30GB -> none)",
              q.gate_alt_lane_urls(own_regate, None), [])
        sizes[M] = 24e9
        check("1 committed gate on an idle unraid lane while the coder runs on studio-db: "
              "no extra restriction -> legacy first claim applies",
              q.lane_slot_gate(own_gate, UNRAID, [], jobs, pk, CK, None, CFG), None)
        check("1 committed regate beside the running coder on studio-db: takes the GATE slot "
              "(same warm model, 22.6GB headroom)",
              q.lane_slot_gate(own_regate, STUDIO, [coder], jobs, pk, CK, None, CFG)[0], True)
        check("1 ...kill switch gate_slots=0 -> no second slot (old single-lane behaviour)",
              q.lane_slot_gate(own_regate, STUDIO, [coder], jobs, pk, CK, None,
                               dict(CFG, gate_slots=0)), None)
        check("1 gate_has_free_path (so the coder is NOT preempted for it)",
              q.gate_has_free_path(own_regate, jobs, pk, CK, None, CFG), True)
        check("1 ...and with the slot off + unraid busy there is no free path (old preempt applies)",
              q.gate_has_free_path(own_regate, jobs + [J("u1", "x", "o", "running", UNRAID)], pk, CK,
                                   None, dict(CFG, gate_slots=0)), False)
    guarded("1 committed gate", s_commit_gate)

    # ---- 2. other bundles' gates: idle non-conflicting lane only -------------------
    def s_foreign_gate():
        jobs = [coder, own_coding, foreign_gate]
        check("2 other bundle's gate on an IDLE lane the committed bundle has no job for "
              "(unraid) runs",
              q.lane_slot_gate(foreign_gate, UNRAID, [], jobs, pk, CK, None, CFG)[0], True)
        want_unraid = [coder, J("c3", "rt-x-s3", CK, host="unraid"), foreign_gate]
        check("2 ...but NOT when the committed bundle has a job whose lane is unraid "
              "(would delay it / evict its model)",
              q.lane_slot_gate(foreign_gate, UNRAID, [], want_unraid, pk, CK, None, CFG)[0], False)
        check("2 ...never onto a BUSY lane (no second slot, no preemption)",
              q.lane_slot_gate(foreign_gate, UNRAID, [J("u1", "x", "o", "running", UNRAID)],
                               jobs, pk, CK, None, CFG)[0], False)
        fg_db = J("fg2", "gate-yyy", "bundle-B", host="studio")
        check("2 other bundle's gate beside the committed coder on studio-db: only the free "
              "GATE slot, same model, headroom",
              q.lane_slot_gate(fg_db, STUDIO, [coder], [coder, fg_db], pk, CK, None, CFG)[0], True)
        fg_swap = dict(fg_db, model="other-model")
        check("2 ...never a model swap/eviction of the coder's warm model",
              q.lane_slot_gate(fg_swap, STUDIO, [coder], [coder, fg_swap], pk, CK, None, CFG)[0], False)
        check("2 ...kill switch (gate_slots=0): another bundle's gate never joins a busy studio-db",
              q.lane_slot_gate(fg_db, STUDIO, [coder], [coder, fg_db], pk, CK, None,
                               dict(CFG, gate_slots=0))[0], False)
        waiting = J("cw", "rt-x-s9", CK, "pending", model="coder-model-2")
        check("2 other bundle's gate on an EMPTY studio-db is denied when the committed bundle's "
              "waiting coder needs a different model",
              q.lane_slot_gate(fg_db, STUDIO, [], [waiting, fg_db], pk, CK, None, CFG)[0], False)
        check("2 ...but allowed when nothing of the committed bundle waits on another model",
              q.lane_slot_gate(fg_db, STUDIO, [], [fg_db], pk, CK, None, CFG)[0], True)
        gj = [coder, fg_db]
        g_pk = lambda j: j["bundle"]
        victim_pk = g_pk
        check("2 a foreign gate never preempts under a commitment (wiring)",
              "gate_is_committed(_gj, _pk_g(_gj), _ck_g)" in inspect.getsource(q.cmd_run), True)
    guarded("2 foreign gate", s_foreign_gate)

    # ---- 5. studio_slot_decision table --------------------------------------------
    def s_table():
        def D(job, running, cfg=CFG, head=22.6, kv=1.4, waiting=(), unfinished=1, cap=4):
            return q.studio_slot_decision(job, running, cfg, head, kv, waiting, unfinished, cap)[0]
        gate = {"kind": "gate", "model": M, "committed": True}
        fgate = {"kind": "gate", "model": M, "committed": False}
        code = {"kind": "coding", "model": M, "committed": True}
        rc = [{"kind": "coding", "model": M}]
        rg = [{"kind": "gate", "model": M}]
        rows = [
            ("empty lane, coder", D(code, []), True),
            ("empty lane, gate", D(gate, []), True),
            ("gate beside coder", D(gate, rc), True),
            ("coder beside gate (coding slot)", D(code, rg), True),
            ("second coder never", D(code, rc), False),
            ("second gate never (gate_slots=1)", D(gate, rg), False),
            ("two gates + coder over max_total", D(gate, rc + rg, dict(CFG, gate_slots=2)), False),
            ("gate_slots=0 kill switch", D(gate, rc, dict(CFG, gate_slots=0)), False),
            ("gate_slots=0 empty lane still ok", D(gate, [], dict(CFG, gate_slots=0)), True),
            ("model differs -> never swap", D(dict(gate, model="x"), rc), False),
            ("running models mixed", D(gate, rc + [{"kind": "gate", "model": "x"}]), False),
            ("serving cap reached (fleet)", D(gate, rc, unfinished=4), False),
            ("cap-1 in flight still ok", D(gate, rc, unfinished=3), True),
            ("serving load unknown", D(gate, rc, unfinished=None), False),
            ("cap unknown", D(gate, rc, cap=None), False),
            ("headroom unknown", D(gate, rc, head=None), False),
            ("kv need unknown", D(gate, rc, kv=None), False),
            ("headroom below KV+reserve", D(gate, rc, head=4.0, kv=1.4), False),
            ("headroom exactly KV+reserve", D(gate, rc, head=4.4, kv=1.4), True),
            ("foreign gate, committed waits on same model", D(fgate, rc, waiting={M}), True),
            ("foreign gate, committed waits on other model", D(fgate, rc, waiting={"y"}), False),
            ("foreign gate empty lane, other model waits", D(fgate, [], waiting={"y"}), False),
            ("committed gate ignores waiting set", D(gate, rc, waiting={"y"}), True),
        ]
        for name, got, want in rows:
            check(f"5 slot table: {name}", got, want)
        check("5 slot_kind", (q.slot_kind({"label": "gate-x"}), q.slot_kind({"label": "regate-x"}),
                              q.slot_kind({"label": "rt-x"}), q.slot_kind({"label": "auto-author-x"})),
              ("gate", "gate", "coding", "coding"))
    guarded("5 slot table", s_table)

    # ---- 6. config + measurement + wiring ------------------------------------------
    def s_cfg():
        p = tmp / "slots.json"
        check("6 config default gate_slots=1", q.load_slot_config(p, env={})["gate_slots"], 1)
        p.write_text(json.dumps({"gate_slots": 0}))
        check("6 config file gate_slots=0 is the kill switch", q.load_slot_config(p, env={})["gate_slots"], 0)
        p.write_text("{not json")
        check("6 malformed file -> defaults", q.load_slot_config(p, env={})["gate_slots"], 1)
        p.write_text(json.dumps({"gate_slots": -3, "max_total": "x"}))
        c = q.load_slot_config(p, env={})
        check("6 bad values ignored", (c["gate_slots"], c["max_total"]), (1, 2))
        check("6 GATE_SLOTS env override", q.load_slot_config(p, env={"GATE_SLOTS": "0"})["gate_slots"], 0)

        recs, seen = [], {}
        rate = {"c1": 40.0, "g1": None}
        q.slot_measure_update(seen, [{"id": "c1", "kind": "coding"}], 100.0, rate.get, recs.append)
        q.slot_measure_update(seen, [{"id": "c1", "kind": "coding"}, {"id": "g1", "kind": "gate"}],
                              140.0, rate.get, recs.append)
        q.slot_measure_update(seen, [{"id": "c1", "kind": "coding"}], 170.0, rate.get, recs.append)
        ends = [r for r in recs if r["ev"] == "end"]
        check("6 measure: a gate that ran beside the coder is recorded with its duration, conc=True",
              [(r["job"], r["dur_s"], r["conc"]) for r in ends], [("g1", 30.0, True)])
        samp = [(r["job"], r["n"], r["tokps"]) for r in recs if r["ev"] == "sample" and r["job"] == "c1"]
        check("6 measure: coder tok/s sampled alone (n=1) and concurrent (n=2)",
              [s_[1] for s_ in samp], [1, 2, 1])
    guarded("6 config/measure", s_cfg)

    def s_wiring():
        src = inspect.getsource(q.cmd_run)
        for needle in ("gate_priority_order(", "gate_ok=True", "lane_slot_gate(", "gate_alt_lane_urls(",
                       "gate_has_free_path(", "slot_measure_update(", "load_slot_config()",
                       "and not is_bundle_gate_job(job)"):
            check(f"6 cmd_run wiring: {needle}", needle in src, True)
        check("6 the warm of `darkbloom status` happens OUTSIDE the state lock",
              0 < src.find("darkbloom_status()") < src.find("with _Locked() as lock:\n            state = lock.load()"),
              True)
    guarded("6 wiring", s_wiring)

    # ---- 7. dead-driver commit releases; idle-with-pending alerts -------------------
    def s_idle():
        T = 1_000_000.0
        work = lambda k: ("working", "needs_opus row healing", False)
        commit = {"key": "tls", "since": T, "empty_since": None, "idle_since": T}
        c, ev = q.bundle_commit_step(dict(commit), {}, None, ["tls", "egift"], work, T + 11 * 60)
        check("7 committed bundle idle 11 min (dead driver) with ANOTHER bundle waiting -> "
              "parked as stalled, queue moves on", ([e[0] for e in ev][:1], c and c["key"]),
              (["park"], "egift"))
        c, ev = q.bundle_commit_step(dict(commit), {}, None, ["tls"], work, T + 11 * 60)
        check("7 ...with nothing else waiting it keeps the hold (45 min ceiling unchanged)",
              (ev, c["key"]), ([], "tls"))
        st = {"since": None, "alerted": 0.0}
        r = [q.idle_pending_alert_due(st, False, 5, T + t) for t in (0, 300, 601, 700, 601 + 1800)]
        check("7 idle+pending alert fires after 10 min, once, repeats after 30 min",
              r, [False, False, True, False, True])
        check("7 a running job resets the idle clock",
              (q.idle_pending_alert_due(st, True, 5, T + 5000), st["since"]), (False, None))
    guarded("7 idle", s_idle)

    # ---- 8. darkbloom model not served = infra wait, never a job failure -------------
    def s_infra():
        E = "darkbloom not serving 'm' at http://127.0.0.1:8000 -- check `darkbloom status`"
        jobs = [dict(id="a", label="rt-x-s4", status="failed", error=E, _streak_counted=True),
                dict(id="b", label="auto-refine-y-r1", status="needs_opus", error=E,
                     escalation={"reason": "x"}),
                dict(id="c", label="esc-review-2026-job-b", status="failed", error=E),
                dict(id="d", label="real-fail", status="failed", error="exit 1 nonconvergence"),
                dict(id="e", label="ok", status="done")]
        rq, dr = q.infra_requeue(jobs)
        check("8 never-ran rows (failed + needs_opus) go back to pending, error/escalation cleared",
              (rq, [(j["id"], j["status"], "error" in j, "escalation" in j) for j in jobs[:2]]),
              (["a", "b"], [("a", "pending", False, False), ("b", "pending", False, False)]))
        check("8 their esc-review children are dropped; real failures untouched",
              (dr, [j["id"] for j in jobs], jobs[-2]["status"]), (["c"], ["a", "b", "d", "e"], "failed"))
        calls = []
        probe = lambda m: (calls.append(m) or False)
        cache = {}
        r = [q.darkbloom_model_ready("m", t, probe, cache) for t in (0, 5, 10, 31)]
        check("8 a DOWN verdict is cached 30s (no probe storm), then re-probed", (r, len(calls)),
              ([False] * 4, 2))
        check("8 an UP model is probed every time", 
              [q.darkbloom_model_ready("u", t, lambda m: True, cache) for t in (0, 1)], [True, True])
        check("8 drift: needed-but-unserved models are named (case-insensitive)",
              q.darkbloom_drift({"Qwen3", "gemma"}, ["qwen3"]), ["gemma"])
        check("8 the exact fix command is printed",
              q.darkbloom_fix_command({"qwen3.6-35b-a3b-vl-mtp-mxfp8"}),
              "darkbloom start --model qwen3.6-35b-a3b-vl-mtp-mxfp8 --model Qwen3.5-9B --local-endpoint")
        src = inspect.getsource(q.cmd_run)
        check("8 launch loop no longer fails the job (infra wait + continue)",
              ('job["status"] = "failed"\n                        job["error"] = (f"darkbloom not serving' not in src
               and "darkbloom_model_ready(" in src and "infra_requeue(" in src), True)
    guarded("8 infra", s_infra)

    n_ok = sum(RESULTS)
    print(f"\n{n_ok}/{len(RESULTS)} checks passed")
    print("GATE_PRIORITY_TEST_OK" if all(RESULTS) else "GATE_PRIORITY_TEST_FAILED")
    return 0 if all(RESULTS) else 1


if __name__ == "__main__":
    sys.exit(main())
