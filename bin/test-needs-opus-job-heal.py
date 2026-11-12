#!/usr/bin/env python3
"""test-needs-opus-job-heal.py -- replay of the 2026-10-05 rt-bg-commitments-fix park.

THE INCIDENT. ollama-dispatch-auto (no slicer) could not converge the getCommitments
harness: 87ef87235fd3 hit its cap, continuation c1 95234578bc40 left the tree
byte-identical, and park_visible escalated c1 to needs_opus. The escalation watcher
reviewed it (VERDICT b: fixture/harness defect) and then self-heal said
"skip:job is not a slice of any plan" -- every heal lever was a slicer lever. A human
enqueued c2 3f75be3df79a --continues 95234578bc40; it converged (VERIFY_OK) at 21:13Z
and its done row was pruned. 25 minutes later the bundle commitment still counted the
needs_opus c1 row as stuck and parked the bundle "needs the owner".

What must hold now:
  (a) a non-slice auto-author needs_opus row with a b/c review gets ONE bounded
      continuation authoring round on the LOCAL model (never Opus), capped per chain,
      per bundle per day and per day, with one deduped alert when a cap blocks it;
  (b) a bundle does not park on a needs_opus row whose continuation is live or that
      the job ladder will still heal;
  (c) once a continuation PASSES, the needs_opus row is cleared (bundle not parked;
      the sweep resolves the row so the index janitor ticks its rows).

Everything is STUBBED: temp dirs, in-memory jobs, stub enqueue/resolve/notify.
DISPATCH_VERIFY_SANDBOX=1 and OLLAMA_QUEUE_NO_NOTIFY=1 are set; nothing reaches the
real queue (its CLI refuses mutations under the sandbox anyway).

Usage: test-needs-opus-job-heal.py [--queue P] [--heal P] [--watcher P]
Defaults are the installed ~/bin copies. The revert run passes the .bak files and
must FAIL. Exit 0 = all checks pass.
"""
import argparse
import importlib.machinery
import importlib.util
import inspect
import io
import json
import os
import sys
import tempfile
import time
from contextlib import redirect_stdout
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"

BIN = Path.home() / "bin"
RESULTS = []
B = "rt-bg-commitments-fix"
C1, C2 = "95234578bc40", "3f75be3df79a"


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def guarded(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        RESULTS.append(False)
        print(f"FAIL {name}: raised {type(e).__name__}: {e}")


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def iso(t):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))


REVIEW_B = ("VERDICT: b -- The verify.test.ts fixture uses a shared global `responses` "
            "array across all 5 tests, causing state to leak between tests.\n")


def c1_row(wt, esc_at, **kw):
    r = {"id": C1, "label": "auto-author-rt-bg-commitments-fix-getcommitments-c1",
         "status": "needs_opus", "bundle": B, "cwd": str(wt),
         "task_file": str(Path(wt) / "AUTO-TASK.md"), "model": "qwen3.6-35b-a3b-vl-mtp-mxfp8",
         "host_pref": "studio", "num_ctx": 98304, "max_iters": 24,
         "verify": "python3 auto-harness-check.py", "continues": "87ef87235fd3",
         "escalation": {"category": "persistent-nogo", "escalated_at": iso(esc_at),
                        "reason": "authoring stuck: ... needs harness help"}}
    r.update(kw)
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--queue", default=str(BIN / "ollama-queue.py"))
    ap.add_argument("--heal", default=str(BIN / "dispatch-self-heal.py"))
    ap.add_argument("--watcher", default=str(BIN / "dispatch-escalation-watcher.py"))
    a = ap.parse_args()
    heal = load(a.heal, "dsh_under_test")
    q = load(a.queue, "oq_under_test")
    w = load(a.watcher, "dew_under_test")
    td = Path(tempfile.mkdtemp(prefix="needsopus-test-"))
    # 2026-10-09: at the cap / on a (d) the ladder RE-SPECS from the recorded dispatch-auto
    # argv instead of parking; hermetic here = an EMPTY auto-runs dir, so "no argv" parks.
    heal.AUTO_RUNS = td / "autoruns"
    os.environ["ESC_ACTIONS"] = "on"
    wt = td / "wt"
    wt.mkdir()
    (wt / "AUTO-TASK.md").write_text("CONTINUE -- your authoring run ran out of iterations\n")
    runs = td / "slice-runs"
    runs.mkdir()
    logs = td / "logs"
    (logs / "archive").mkdir(parents=True)
    led = td / "self-heal.json"
    dec = td / "decisions.jsonl"
    # never touch the live ledger / escalations / slice runs from this test
    heal.HEAL_LEDGER, heal.ESC_DIR, heal.SLICE_RUNS = led, td / "esc", runs
    heal.DECISIONS, heal.QUEUE_LOG_DIR = dec, logs
    now = time.time()

    enq, alerts = [], []

    def stub_enqueue(argv):
        enq.append(list(argv))
        return "%012x" % (0xabc000 + len(enq)), "enqueued"

    def stub_notify(t, m, **k):
        alerts.append(k.get("dedupe_key"))

    # ---- (a) the incident: verdict b on a non-slice auto-author needs_opus row ----
    def t_heal_job():
        led.unlink(missing_ok=True)
        jobs = [c1_row(wt, now - 600)]
        got = heal.heal_job(jobs[0], REVIEW_B, jobs=jobs, ledger_path=led, slice_runs=runs,
                            enqueue=stub_enqueue, notifier=stub_notify, decisions=dec,
                            log_dir=logs)
        check("(a) verdict b on a NON-slice auto-author job -> ONE continuation round "
              "(was: skip:job is not a slice of any plan)", got.startswith("heal-continuation:"), True)
        argv = enq[-1] if enq else []
        val = lambda f: argv[argv.index(f) + 1] if f in argv else None
        check("(a) ...it --continues the stuck job, in the SAME bundle and worktree",
              (val("--continues"), val("--bundle"), val("--cwd")), (C1, B, str(wt)))
        check("(a) ...on the job's own LOCAL model (no Opus on this path)",
              val("--model"), "qwen3.6-35b-a3b-vl-mtp-mxfp8")
        check("(a) ...as the next continuation label",
              val("--label"), "auto-author-rt-bg-commitments-fix-getcommitments-c2")
        check("(a) ...and the prompt carries the review's findings",
              "shared global `responses`" in (wt / "AUTO-TASK.md").read_text(), True)
    guarded("(a) heal_job", t_heal_job)

    def t_cli():
        # the exact watcher -> self-heal CLI path that printed the dead skip
        led.unlink(missing_ok=True)
        enq.clear()
        jobs = [c1_row(wt, now - 600)]
        rv = td / "r.review.md"
        rv.write_text(REVIEW_B)
        heal._queue_jobs = lambda *x, **k: jobs
        heal._queue_enqueue = stub_enqueue
        old = sys.argv
        sys.argv = ["dispatch-self-heal.py", "--job-label", jobs[0]["label"], "--job-id", C1,
                    "--context", str(td / "ctx.md"), "--review", str(rv)]
        buf = io.StringIO()
        try:
            with redirect_stdout(buf):
                heal.main()
        except SystemExit:
            pass
        finally:
            sys.argv = old
        out = buf.getvalue().strip().splitlines()
        check("(a) CLI --job-label on a non-slice job heals instead of skipping",
              bool(out) and out[-1].startswith("heal-continuation:"), True)
    guarded("(a) CLI", t_cli)

    def t_watcher_passes_job_id():
        seen = []

        class R:
            stdout, stderr, returncode = "heal-continuation:x\n", "", 0

        def run(argv, **k):
            seen.append(argv)
            return R()
        tool = td / "fake-heal.py"
        tool.write_text("")
        w.self_heal({"source": "D", "label": "auto-author-x-c1", "job_id": C1},
                    td / "ctx.md", td / "r.md", run=run, tool=tool)
        check("(a) the watcher hands the job id to self-heal (--job-id)",
              bool(seen) and "--job-id" in seen[0] and C1 in seen[0], True)
    guarded("(a) watcher", t_watcher_passes_job_id)

    def t_caps():
        led.unlink(missing_ok=True)
        enq.clear()
        alerts.clear()
        jobs = [c1_row(wt, now - 600)]
        r1 = heal.heal_job(jobs[0], REVIEW_B, jobs=jobs, ledger_path=led, slice_runs=runs,
                           enqueue=stub_enqueue, notifier=stub_notify, decisions=dec, log_dir=logs)
        r2 = heal.heal_job(jobs[0], REVIEW_B, jobs=jobs, ledger_path=led, slice_runs=runs,
                           enqueue=stub_enqueue, notifier=stub_notify, decisions=dec, log_dir=logs)
        r3 = heal.heal_job(jobs[0], REVIEW_B, jobs=jobs, ledger_path=led, slice_runs=runs,
                           enqueue=stub_enqueue, notifier=stub_notify, decisions=dec, log_dir=logs)
        check("caps: per-chain cap = %d rounds, the next is the FINAL rung"
              % heal.JOB_HEAL_MAX_PER_CHAIN,
              (r1.split(":")[0], r2.split(":")[0], r3, len(enq)),
              ("heal-continuation", "heal-continuation", "park:final-rung", 2))
        check("caps: ONE alert, keyed per chain (notify dedupes on it)",
              alerts, ["job-heal-cap:" + heal.job_chain_key(jobs[0])])
        r4 = heal.heal_job(jobs[0], REVIEW_B, jobs=jobs, ledger_path=led, slice_runs=runs,
                           enqueue=stub_enqueue, notifier=stub_notify, decisions=dec, log_dir=logs)
        check("caps: a capped chain never enqueues again (and re-alerts on the SAME dedupe key)",
              (r4, len(enq), set(alerts)), ("park:final-rung", 2,
                                            {"job-heal-cap:" + heal.job_chain_key(jobs[0])}))
        # per-bundle/day and global/day caps across distinct chains
        led.unlink(missing_ok=True)
        enq.clear()
        outs = []
        for i in range(heal.JOB_HEAL_MAX_PER_DAY + 1):
            wti = td / ("wt%d" % i)
            wti.mkdir(exist_ok=True)
            row = c1_row(wti, now - 600, id="%012d" % i, bundle="b%d" % (i % 3),
                         label="auto-author-chain%d-c1" % i)
            outs.append(heal.heal_job(row, REVIEW_B, jobs=[row], ledger_path=led,
                                      slice_runs=runs, enqueue=stub_enqueue,
                                      notifier=stub_notify, decisions=dec, log_dir=logs))
        check("caps: at most JOB_HEAL_MAX_PER_DAY=%d rounds per UTC day across chains"
              % heal.JOB_HEAL_MAX_PER_DAY, (len(enq), outs[-1]),
              (heal.JOB_HEAL_MAX_PER_DAY, "park:final-rung"))
        led.unlink(missing_ok=True)
        enq.clear()
        outs = []
        for i in range(heal.JOB_HEAL_MAX_PER_BUNDLE_DAY + 1):
            wti = td / ("wb%d" % i)
            wti.mkdir(exist_ok=True)
            row = c1_row(wti, now - 600, id="%012d" % (100 + i), label="auto-author-bb%d-c1" % i)
            outs.append(heal.heal_job(row, REVIEW_B, jobs=[row], ledger_path=led,
                                      slice_runs=runs, enqueue=stub_enqueue,
                                      notifier=stub_notify, decisions=dec, log_dir=logs))
        check("caps: at most JOB_HEAL_MAX_PER_BUNDLE_DAY=%d rounds per bundle per day"
              % heal.JOB_HEAL_MAX_PER_BUNDLE_DAY, (len(enq), outs[-1]),
              (heal.JOB_HEAL_MAX_PER_BUNDLE_DAY, "park:final-rung"))
    guarded("caps", t_caps)

    def t_not_eligible():
        led.unlink(missing_ok=True)
        enq.clear()
        row = c1_row(wt, now - 600)
        got = heal.heal_job(row, "VERDICT: d -- model incapacity", jobs=[row], ledger_path=led,
                            slice_runs=runs, enqueue=stub_enqueue, notifier=stub_notify,
                            decisions=dec, log_dir=logs)
        check("a (d) verdict with no bigger model and no recorded argv PARKS (never a blind round)",
              (got.startswith("park:"), len(enq)), (True, 0))
        check("...and is no longer heal-pending (the bundle may park on it)",
              heal.job_heal_pending(row, json.loads(led.read_text()), now, runs), False)
        coding = c1_row(wt, now - 600, label="rt-bg-commitments-fix-getcommitments")
        check("a CODING job is never re-authored by the job ladder",
              heal.heal_job(coding, REVIEW_B, jobs=[coding], ledger_path=led, slice_runs=runs,
                            enqueue=stub_enqueue, notifier=stub_notify, decisions=dec,
                            log_dir=logs).startswith("skip:"), True)
        live = [c1_row(wt, now - 600), {"id": "zz", "status": "running", "cwd": str(wt),
                                        "label": "something-else"}]
        check("no heal round while another job is live on the worktree",
              heal.heal_job(live[0], REVIEW_B, jobs=live, ledger_path=led, slice_runs=runs,
                            enqueue=stub_enqueue, notifier=stub_notify, decisions=dec,
                            log_dir=logs).startswith("wait:"), True)
    guarded("eligibility", t_not_eligible)

    def t_no_opus_structural():
        src = "".join(inspect.getsource(f) for f in (
            heal.heal_job, heal.job_heal_sweep, heal.job_row_state, heal._queue_enqueue))
        check("structural: the job-heal path never spawns claude / an Opus model",
              ("claude" in src.lower(), "--model opus" in src.lower(), "\"opus\"" in src.lower()),
              (False, False, False))
    guarded("no-opus", t_no_opus_structural)

    # ---- (c) clear-on-pass: the sweep resolves a row whose continuation passed ------
    def t_sweep():
        led.unlink(missing_ok=True)
        jobs = [c1_row(wt, now - 4000)]
        # the incident's c2: enqueued by hand BEFORE continued_by existed, row pruned,
        # only its done.json sidecar left (same base, same worktree, after the escalation)
        sc = [{"id": C2, "label": "auto-author-rt-bg-commitments-fix-getcommitments-c2",
               "cwd": str(wt), "status": "done", "exit_code": 0, "persisted_at": iso(now - 1500)}]
        resolved = []
        resumed = []
        _kw = {"resume": lambda j, cid, **k: resumed.append((j.get("id"), cid)) or "resumed:stub"} \
            if "resume" in heal.job_heal_sweep.__code__.co_varnames else {}
        out = heal.job_heal_sweep(jobs=jobs, ledger_path=led, slice_runs=runs,
                                  resolve=lambda j: resolved.append(j) or True, log_dir=logs,
                                  esc_dir=td / "esc", decisions=dec, sidecars=sc, **_kw)
        check("(c) a needs_opus row whose continuation PASSED is resolved by the sweep",
              (resolved, sorted({x[0] for x in out})), ([C1], [C1]))
        sc[0]["exit_code"] = 1
        sc[0]["status"] = "failed"
        resolved.clear()
        heal.job_heal_sweep(jobs=jobs, ledger_path=led, slice_runs=runs,
                            resolve=lambda j: resolved.append(j) or True, log_dir=logs,
                            esc_dir=td / "esc", decisions=dec, sidecars=sc)
        check("(c) ...a FAILED continuation never clears it", resolved, [])
        old = dict(sc[0], persisted_at=iso(now - 9000), status="done", exit_code=0)
        heal.job_heal_sweep(jobs=jobs, ledger_path=led, slice_runs=runs,
                            resolve=lambda j: resolved.append(j) or True, log_dir=logs,
                            esc_dir=td / "esc", decisions=dec, sidecars=[old])
        check("(c) ...nor a same-label pass from BEFORE the escalation", resolved, [])
    guarded("(c) sweep", t_sweep)

    def t_sweep_next_round():
        led.unlink(missing_ok=True)
        enq.clear()
        jobs = [c1_row(wt, now - 4000)]
        r1 = heal.heal_job(jobs[0], REVIEW_B, jobs=jobs, ledger_path=led, slice_runs=runs,
                           enqueue=stub_enqueue, notifier=stub_notify, decisions=dec, log_dir=logs)
        hid = r1.split(":", 1)[1]
        (td / "esc").mkdir(exist_ok=True)
        (td / "esc" / ("20261005T200725Z-job-%s.review.md" % C1)).write_text(REVIEW_B)
        sc = [{"id": hid, "label": "auto-author-rt-bg-commitments-fix-getcommitments-c2",
               "cwd": str(wt), "status": "failed", "exit_code": 1, "persisted_at": iso(now - 60)}]
        calls = []
        heal.job_heal_sweep(jobs=jobs, ledger_path=led, slice_runs=runs, log_dir=logs,
                            esc_dir=td / "esc", decisions=dec, sidecars=sc,
                            heal=lambda j, rv, **k: calls.append((j["id"], rv)) or "heal-continuation:n")
        check("a FAILED heal round gets the next capped round from the sweep",
              [c[0] for c in calls], [C1])
    guarded("sweep next round", t_sweep_next_round)

    # ---- (b)+(c) on the queue: the bundle commitment ---------------------------------
    pk = lambda j: j.get("bundle")

    def t_status():
        rows = [c1_row(wt, now - 4000)]
        st = lambda s: q.bundle_commit_status(B, rows, pk, plan={}, chain={},
                                              esc_state=lambda j: s)[0]
        check("(c) bundle_commit_status: a needs_opus row whose continuation passed "
              "does NOT park the bundle", st("cleared"), "complete")
        check("(b) bundle_commit_status: a row the job ladder is still healing keeps the "
              "bundle WORKING", st("pending"), "working")
        check("bundle_commit_status: a genuinely stuck row still parks (unchanged)",
              st(None), "blocked")
    guarded("(b)/(c) status", t_status)

    def t_apply_wiring():
        rd, cd = td / "q-runs", td / "q-chain"
        rd.mkdir(exist_ok=True)
        cd.mkdir(exist_ok=True)
        (logs / ("%s.done.json" % C2)).write_text(json.dumps(
            {"id": C2, "label": "auto-author-rt-bg-commitments-fix-getcommitments-c2",
             "cwd": str(wt), "status": "done", "exit_code": 0, "continues": C1,
             "persisted_at": iso(now - 1500)}))

        def run_tick(rows):
            state = {"jobs": rows, "_bundle_commit": {"key": B, "since": now - 9000,
                                                      "empty_since": None, "idle_since": None}}
            parks = []
            q._apply_bundle_commit(state, pk, None, [], now, kick=lambda *x: False,
                                   alert=lambda k, why, n: parks.append((k, why)),
                                   runs_dir=rd, chain_dir=cd, hooks={}, log_dir=logs)
            return parks
        p = run_tick([c1_row(wt, now - 4000, continued_by=[C2])])
        check("(c) wiring: the incident tick -- c2 PASSED and pruned -- does NOT park "
              "rt-bg-commitments-fix 'needs the owner'", p, [])
        p = run_tick([c1_row(wt, now - 4000, continued_by=["deadbeef0000"])])
        check("wiring: a stale, unhealable needs_opus row still parks loudly",
              [k for k, _w in p], [B])
        p = run_tick([c1_row(wt, now - 120)])
        check("(b) wiring: a FRESH eligible escalation (review + heal still coming) "
              "does not park", p, [])
        p = run_tick([c1_row(wt, now - 4000),
                      {"id": "c3", "label": "auto-author-x-c3", "status": "pending",
                       "bundle": B, "continues": C1, "cwd": str(wt)}])
        check("(b) wiring: a queued continuation keeps the bundle working", p, [])
    guarded("(b)/(c) wiring", t_apply_wiring)

    def t_stale_park_dropped():
        rd, cd = td / "q-runs2", td / "q-chain2"
        rd.mkdir(exist_ok=True)
        cd.mkdir(exist_ok=True)
        rows = [{"id": "87ef87235fd3", "label": "auto-author-rt-bg-commitments-fix-getcommitments",
                 "status": "failed", "bundle": B, "cwd": str(wt)},
                {"id": "7ed88ee015e5", "label": "auto-author-rt-egift-link-s1-s0-db-schema-c1",
                 "status": "running", "bundle": "rt-egift-link-s1", "cwd": str(td)}]
        state = {"jobs": rows,
                 "_bundle_commit": {"key": "rt-egift-link-s1", "since": now - 600,
                                    "empty_since": None, "idle_since": None},
                 "_bundle_parked": {B: {"since": now - 300, "kind": "blocked",
                                        "why": "95234578bc40: ... needs_opus",
                                        "commit_since": now - 9000}}}
        q._apply_bundle_commit(state, pk, "rt-egift-link-s1", [], now, kick=lambda *x: False,
                               alert=lambda *x: None, runs_dir=rd, chain_dir=cd, hooks={},
                               log_dir=logs)
        check("(c) a parked bundle whose blocker was resolved is DROPPED from _bundle_parked "
              "while another bundle holds the commitment (its 'needs the owner' row then closes)",
              (B in state["_bundle_parked"], (state.get("_bundle_commit") or {}).get("key")),
              (False, "rt-egift-link-s1"))
    guarded("stale park", t_stale_park_dropped)

    def t_continued_by():
        jobs = [c1_row(wt, now - 4000)]
        q._mark_continued_by(jobs, {"id": C2, "continues": C1})
        check("enqueue --continues stamps continued_by on the needs_opus row (flag only, "
              "status untouched)", (jobs[0].get("continued_by"), jobs[0]["status"]),
              ([C2], "needs_opus"))
    guarded("continued_by", t_continued_by)

    def t_done_json_carries_continues():
        src = inspect.getsource(q._persist_job_completion)
        check("done.json records `continues` (clear-on-pass after the row is pruned)",
              '"continues"' in src, True)
    guarded("done.json", t_done_json_carries_continues)

    n_ok, n = sum(RESULTS), len(RESULTS)
    print(f"\n{n_ok}/{n} checks passed")
    return 0 if n_ok == n else 1


if __name__ == "__main__":
    sys.exit(main())
