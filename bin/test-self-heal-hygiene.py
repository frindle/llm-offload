#!/usr/bin/env python3
"""Queue hygiene: superseded / dead esc-review rows, and source-D reviews of jobs that
are already continued (dispatch-self-heal.py hygiene_sweep + dispatch-escalation-
watcher.py drop_continued_jobs / prune_escalations, 2026-10-06).

Live shapes (2026-10-06):
  * 05b17e14a15a  pending review of 77d808c3984a, whose relaunch 24123dd86140 was
    already enqueued                                   -> cancel --automated
  * 5e901735dc37 / f69fce37a51a  two reviews of ONE subject, both `failed` with
    "launch failed: No such file" (task file pruned)   -> resolve
  * a pending duplicate review of a subject that already has an older one -> cancel
  * 0fb392936848 needs_opus, re-run as `-v2` in a new worktree; the -v2 PASSED -> resolve
  * running reviews, gate jobs, other rows                -> untouched
  * the daemon launched the row meanwhile                 -> skipped (status re-read)
  * cap per day; one alert per batch.
Hermetic: injected jobs/sidecars/act/notifier, temp ledger. No queue writes.
--revert-check mutates SELF_HEAL_SRC / WATCHER_SRC and requires RED."""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SH = Path(os.environ.get("SELF_HEAL_SRC") or HERE / "dispatch-self-heal.py")
WA = Path(os.environ.get("WATCHER_SRC") or HERE / "dispatch-escalation-watcher.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    s = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    return m


WT_A = "/wt/wt-rt-bfmr-tls-fingerprint"
WT_B = "/wt/wt-rt-bg-sync-guard"


def J(id, label, status, **k):
    d = {"id": id, "label": label, "status": status, "enqueued_at": "2026-10-06T01:00:00+00:00"}
    d.update(k)
    return d


def jobs():
    return [
        J("77d808c3984a", "auto-refine-rt-bfmr-tls-fingerprint-r1", "needs_opus", cwd=WT_A,
          bundle="tls", escalation={"escalated_at": "2026-10-06T04:31:00+00:00"}),
        J("24123dd86140", "auto-refine-rt-bfmr-tls-fingerprint-r1", "pending", cwd=WT_A,
          bundle="tls", enqueued_at="2026-10-06T07:16:22+00:00"),
        J("05b17e14a15a", "esc-review-20261006T053028Z-job-77d808c3984a", "pending",
          bundle="tls", enqueued_at="2026-10-06T05:30:34+00:00"),
        J("0fb392936848", "auto-refine-rt-bg-sync-guard-r1", "needs_opus", cwd=WT_B, bundle="bg",
          escalation={"escalated_at": "2026-10-06T02:00:00+00:00"}),
        J("5e901735dc37", "esc-review-20261006T020328Z-job-0fb392936848", "failed", bundle="bg",
          error="launch failed: [Errno 2] No such file or directory: '/x.task.md'"),
        J("f69fce37a51a", "esc-review-20261006T023840Z-job-0fb392936848", "failed", bundle="bg",
          error="launch failed: [Errno 2] No such file or directory: '/y.task.md'"),
        # subject c1: no continuation; two pending reviews -> the newer is a duplicate
        J("c1c1c1c1c1c1", "auto-refine-rt-other-r1", "needs_opus", cwd="/wt/o", bundle="o"),
        J("r1r1r1r1r1r1", "esc-review-20261006T060000Z-job-c1c1c1c1c1c1", "pending", bundle="o"),
        J("r2r2r2r2r2r2", "esc-review-20261006T063000Z-job-c1c1c1c1c1c1", "pending", bundle="o"),
        # subject gone -> its pending review is moot
        J("r3r3r3r3r3r3", "esc-review-20261006T064000Z-job-deadbeef0000", "pending", bundle="o"),
        # running review of a continued subject: never touched
        J("r4r4r4r4r4r4", "esc-review-20261006T065000Z-job-77d808c3984a", "running", pid=4242,
          bundle="tls"),
        # a review that FAILED after it ran (a real failure, not a launch failure): kept
        J("r5r5r5r5r5r5", "esc-review-20261006T066000Z-job-c1c1c1c1c1c1", "failed",
          bundle="o", error="worker exited 1"),
    ]


SIDECARS = [{"id": "v2v2v2v2v2v2", "label": "auto-author-rt-bg-sync-guard-v2-c2", "status": "done",
             "exit_code": 0, "bundle": "bg", "persisted_at": "2026-10-06T08:30:00+00:00",
             "cwd": "/wt/wt-rt-bg-sync-guard-v2"}]


def main():
    sh = load(SH, "dsh_hyg")
    acts = sh.superseded_review_actions(jobs(), {}, SIDECARS)
    got = {i: v for v, i, _w in acts}
    check("pending review of a job whose relaunch is enqueued -> cancel", got.get("05b17e14a15a"),
          "cancel")
    check("launch-failed reviews -> resolve", (got.get("5e901735dc37"), got.get("f69fce37a51a")),
          ("resolve", "resolve"))
    check("the newer of two pending reviews of one subject -> cancel; the older kept",
          (got.get("r2r2r2r2r2r2"), got.get("r1r1r1r1r1r1")), ("cancel", None))
    check("pending review of a subject that is gone -> cancel", got.get("r3r3r3r3r3r3"), "cancel")
    check("needs_opus whose -v2 re-run PASSED -> resolve", got.get("0fb392936848"), "resolve")
    check("running review untouched", got.get("r4r4r4r4r4r4"), None)
    check("a review that really ran and failed is kept", got.get("r5r5r5r5r5r5"), None)
    check("the relaunch itself and a stuck subject with no continuation are untouched",
          (got.get("24123dd86140"), got.get("c1c1c1c1c1c1"), got.get("77d808c3984a")),
          (None, None, None))
    nopass = sh.superseded_review_actions(jobs(), {}, [dict(SIDECARS[0], exit_code=1,
                                                            status="failed")])
    check("a FAILED -v2 re-run does not retire the subject",
          "0fb392936848" in {i for _v, i, _w in nopass}, False)

    # application: verbs, re-read, cap, alert, ledger
    T = Path(tempfile.mkdtemp(prefix="hyg-"))
    calls, notes = [], []
    cur = {j["id"]: j for j in jobs()}
    cur["r2r2r2r2r2r2"] = dict(cur["r2r2r2r2r2r2"], status="running", pid=99)  # launched meanwhile
    out = sh.hygiene_sweep(jobs=jobs(), ledger_path=T / "led.json", sidecars=SIDECARS,
                           act=lambda v, i: calls.append((v, i)) or True,
                           notifier=lambda *a, **k: notes.append(a[1]),
                           refresh=lambda: cur, decisions=T / "dec.jsonl",
                           now=1791273011.0)
    res = {i: r for v, i, r in out}
    check("acts through the queue verbs (cancel/resolve)", sorted(set(v for v, _i in calls)),
          ["cancel", "resolve"])
    check("a row the daemon launched meanwhile is skipped", res.get("r2r2r2r2r2r2"),
          "skipped: now running")
    check("one alert for the batch", len(notes), 1)
    n1 = len(calls)
    sh.HYGIENE_MAX_PER_DAY = n1 + 1
    calls2 = []
    out2 = sh.hygiene_sweep(jobs=jobs(), ledger_path=T / "led.json", sidecars=SIDECARS,
                            act=lambda v, i: calls2.append((v, i)) or True,
                            notifier=lambda *a, **k: notes.append(a[1]),
                            refresh=lambda: cur, decisions=T / "dec.jsonl", now=1791273011.0)
    check("daily cap: the ledger counts earlier actions today",
          (len(calls2), sum(1 for x in out2 if x[2] == "capped")), (1, n1 - 1))
    check("...and the cap is alerted", "CAP" in notes[-1], True)
    check("dry run acts on nothing",
          all(r.startswith("would:") for _v, _i, r in sh.hygiene_sweep(
              jobs=jobs(), sidecars=SIDECARS, dry_run=True, ledger_path=T / "x.json")), True)

    # stale hand-locks: released (renamed, never deleted) only for done/skipped slices
    sr = T / "slice-runs"
    sr.mkdir()
    wts = {k: T / ("wt-" + k) for k in ("done", "skipped", "escalated")}
    for k, w in wts.items():
        w.mkdir()
        (w / ".hand-harness").write_text("hand-locked\n")
    (sr / "plan.json").write_text(json.dumps({"slices": {
        k: {"status": k, "worktree": str(w)} for k, w in wts.items()}}))
    locks = sh.stale_hand_locks(sr)
    check("stale hand-locks = the done and skipped slices only",
          sorted(sid for _p, sid, _l in locks), ["done", "skipped"])
    sh.release_hand_locks(locks, decisions=T / "dec.jsonl")
    check("released locks are renamed (evidence kept), the live one untouched",
          ((wts["done"] / ".hand-harness").exists(),
           any(x.name.startswith(".hand-harness.released-") for x in wts["done"].iterdir()),
           (wts["escalated"] / ".hand-harness").exists()), (False, True, True))

    # watcher: D escalations of continued jobs are not reviewed; live review sets survive
    wa = load(WA, "wa_hyg")
    found = [{"source": "D", "job_id": "77d808c3984a", "label": "x"},
             {"source": "D", "job_id": "0fb392936848", "label": "y"},
             {"source": "D", "job_id": "c1c1c1c1c1c1", "label": "z"}]
    kept = wa.drop_continued_jobs(found, sh=sh, jobs=jobs(), sidecars=SIDECARS)
    check("watcher drops D for a continued / -vN-passed job, keeps the rest",
          [e["job_id"] for e in kept], ["c1c1c1c1c1c1"])
    d = T / "esc"
    d.mkdir()
    for i in range(7):
        (d / f"2026100{i + 1}T000000Z-job-x.md").write_text("x")
        (d / f"2026100{i + 1}T000000Z-job-x.local-review.task.md").write_text("x")
    live = str(d / "20261001T000000Z-job-x.local-review.task.md")
    wa.prune_escalations(d, keep=3, protect={live})
    check("prune keeps a set a pending review row still reads", Path(live).exists(), True)
    st = T / "state.json"
    st.write_text(json.dumps({"jobs": [{"status": "pending", "task_file": live},
                                       {"status": "done", "task_file": "/old"}]}))
    check("live paths come from active rows only", wa._queue_live_paths(st), {live})

    # dead-driver chain records (driver SIGKILLed mid-run never writes `ended`)
    rd = T / "auto-runs"
    rd.mkdir()
    old = "2026-10-06T07:00:00Z"
    (rd / "b.json").write_text(json.dumps({"label": "b-s2", "phase": "advancing", "pid": 111,
        "runs": {
        "b-s1": {"label": "b-s1", "phase": "ended", "pid": None, "updated_at": old},
        "b-s2": {"label": "b-s2", "phase": "advancing", "pid": 111, "updated_at": old,
                 "rounds": ["j1", "j2"]},
        "b-s3": {"label": "b-s3", "phase": "waiting", "pid": 222, "updated_at": old},
        "b-s4": {"label": "b-s4", "phase": "advancing", "pid": 333,
                 "updated_at": "2026-10-06T07:59:50Z"}}}))
    now = sh._parse_ts("2026-10-06T08:00:00Z")
    alive = lambda pid: pid == 222
    dry = sh.close_dead_driver_records(rd, alive=alive, now=now, dry_run=True)
    check("dry run lists the dead-driver run only, writes nothing",
          ([lab for _f, lab, _p in dry], json.loads((rd / "b.json").read_text())["runs"]["b-s2"]["phase"]),
          (["b-s2"], "advancing"))
    sh.close_dead_driver_records(rd, alive=alive, now=now, decisions=T / "dec.jsonl")
    doc = json.loads((rd / "b.json").read_text())
    check("a dead driver's run is ended (rounds kept, outcome says why)",
          (doc["runs"]["b-s2"]["phase"], doc["runs"]["b-s2"]["rounds"],
           doc["runs"]["b-s2"]["outcome"].startswith("driver-died")), ("ended", ["j1", "j2"], True))
    check("a live driver's run is untouched", doc["runs"]["b-s3"]["phase"], "waiting")
    check("a just-updated record (younger than the min age) is left alone",
          doc["runs"]["b-s4"]["phase"], "advancing")
    check("top-level mirror no longer names the dead run as advancing",
          (doc["label"], doc["phase"]) != ("b-s2", "advancing"), True)
    # a round enqueued but never recorded (driver SIGKILLed between enqueue and the
    # `waiting` write; canary soak seed 47) is adopted into rounds when the run is closed
    (rd / "o.json").write_text(json.dumps({"label": "o-s2", "phase": "advancing", "pid": 111,
        "runs": {"o-s2": {"label": "o-s2", "phase": "advancing", "pid": 111, "updated_at": old,
                          "step": "enqueue auto-author-o-s2-c1", "rounds": ["a1"]}}}))
    qjobs = [{"id": "a1", "label": "auto-author-o-s2", "created_at": "1"},
             {"id": "a2", "label": "auto-author-o-s2-c1", "created_at": "2"},
             {"id": "x1", "label": "auto-author-o-s20", "created_at": "3"},
             {"id": "x2", "label": "auto-author-o-s2-nope", "created_at": "4"},
             {"id": "x3", "label": "gate-a2", "created_at": "5"}]
    check("orphan_round_jobs: own unrecorded round only (no sibling-prefix, no gate job)",
          sh.orphan_round_jobs("o-s2", ["a1"], qjobs), ["a2"])
    sh.close_dead_driver_records(rd, alive=alive, now=now, decisions=T / "dec.jsonl", jobs=qjobs)
    od = json.loads((rd / "o.json").read_text())
    check("closing a dead driver's run adopts its enqueued-but-unrecorded round",
          (od["runs"]["o-s2"]["phase"], od["runs"]["o-s2"]["rounds"]), ("ended", ["a1", "a2"]))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    (SH, "SELF_HEAL_SRC", "continuation not consulted",
     '        if oc in ("passed", "live"):\n            add("cancel", r["id"], "subject %s already continued',
     '        if False:\n            add("cancel", r["id"], "subject %s already continued'),
    (SH, "SELF_HEAL_SRC", "launch-failed not resolved",
     '        if st == "failed" and str(r.get("error") or "").startswith("launch failed"):',
     '        if False:'),
    (SH, "SELF_HEAL_SRC", "no duplicate check", "        if sid in active_subject:",
     "        if False:"),
    (SH, "SELF_HEAL_SRC", "sibling passes ignored", '        if vo == "passed":\n            add("resolve"',
     '        if False:\n            add("resolve"'),
    (SH, "SELF_HEAL_SRC", "no re-read before acting",
     '        if row is None or row.get("status") not in want or row.get("pid"):',
     '        if row is None:'),
    (SH, "SELF_HEAL_SRC", "no cap", "        if used >= HYGIENE_MAX_PER_DAY:", "        if False:"),
    (SH, "SELF_HEAL_SRC", "locks of live slices released",
     '            if not isinstance(s, dict) or s.get("status") not in ("done", "skipped"):',
     '            if not isinstance(s, dict):'),
    (SH, "SELF_HEAL_SRC", "dead-driver records never closed",
     "                    if not hit:\n                        continue\n",
     "                    if True:\n                        continue\n"),
    (SH, "SELF_HEAL_SRC", "a live driver's record closed",
     "                        if pid and alive(pid):\n                            continue\n", ""),
    (SH, "SELF_HEAL_SRC", "no min age",
     "                        if t is not None and now - t < min_age_s:\n                            continue\n", ""),
    (SH, "SELF_HEAL_SRC", "orphan rounds not adopted",
     "                        if orphans:   # enqueued, then the driver died before recording it\n",
     "                        if False:\n"),
    (SH, "SELF_HEAL_SRC", "orphan match too loose (prefix)",
     '(?:-(?:c|r|esc)\\d*)?$"', '.*"'),
    (WA, "WATCHER_SRC", "watcher reviews continued jobs",
     '                if oc in ("passed", "live"):\n                    print("  D %s',
     '                if False:\n                    print("  D %s'),
    (WA, "WATCHER_SRC", "prune ignores live rows",
     "                if any(str(f) in live for f in by_ts[ts]):", "                if False:"),
]


def revert_check():
    bad = 0
    for path, env, name, old, new in MUTATIONS:
        src = path.read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, env: f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
