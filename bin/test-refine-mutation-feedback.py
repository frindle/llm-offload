#!/usr/bin/env python3
"""Bounded mutation-survivor feedback loop (ollama-dispatch-auto mutfb_*, 2026-10-09).

Surviving mutants are fed back to the fixture-strengthening (refine) step for at most
MUTFB_MAX_ROUNDS (3) rounds, stopping EARLY when a round killed nothing new and removed
no survivor; per-round kill counts are printed, logged (kind "mutfb") and carried in the
escalation message; survivors the opt-in LLM classifier flagged equivalent are not fed.
Drives the REAL _preflight_loop with preflight / dispatch stubbed (no queue, no GPU).

Usage: test-refine-mutation-feedback.py            (AUTO_SRC=path to test another copy)
       test-refine-mutation-feedback.py --revert-check
"""
import importlib.util, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
FAILS = []
F = "app/main.py"


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load():
    ld = SourceFileLoader("oda_mutfb", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_mutfb", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)
    return m


def surv(line, **kw):
    d = {"file": F, "line": line, "mutation": f"m{line}", "snippet": "x", "original": f"stmt_{line}(x)"}
    d.update(kw)
    return d


def drive(m, script, max_rounds=8):
    """script(round_index) -> (killed, evidence, survivors list). Returns calls/outcome/decisions."""
    wt = Path(tempfile.mkdtemp(prefix="mutfb-wt-"))
    (wt / "TASK.md").write_text("# TASK\n## Must contain\n- `stmt`\n")
    (wt / "refimpl.py").write_text("NEW = ''\n")
    st = {"n": 0}
    calls, outcome, decisions = [], {}, []

    def preflight(wt_, req, a):
        k, ev, ss = script(st["n"])
        st["n"] += 1
        if ss is None:
            return 0, {"verdict": "GO"}
        return 1, {"verdict": "NO-GO", "survs": ss, "verify_relevance": {
            "killed": k, "evidence_mutants": ev, "survived": len(ss), "survivors": ss},
            "blockers": [{"check": "verify-relevance"}]}

    def dispatch(wt_, prompt, label, verify_cmd, a, max_iters=None):
        calls.append({"label": label, "prompt": prompt})
        return True, "converged"

    m.slice_already_satisfied = lambda *x: False
    m.slice_taken_elsewhere = lambda *x: None
    m.driver_gone = lambda a: None
    m.chain_state_write = lambda *x, **k: None
    m.enforce_target_at_head = lambda *x: []
    m.run_preflight = preflight
    m.wait_out_operational_blockers = lambda wt_, req, a, rc, data: (rc, data)
    m.repair_state_only_blockers = lambda wt_, req, a, rc, data: (rc, data)
    m.survivors_of = lambda data: data.get("survs") or []
    m.relevance_verdict = lambda data: "FAIL" if data.get("survs") else None
    m.write_refine_guard = lambda *x, **k: None
    m.clear_refine_guard = lambda *x, **k: None
    m.map_survivor_lines = lambda *x, **k: None
    m.patched_target_text = lambda *x, **k: ""
    m.must_contain_from_task = lambda wt_: []
    m.dispatch_model = dispatch
    m.pause_for_review = lambda *x, **k: outcome.setdefault("go", True) and 0
    m._autoslice_on_failure = lambda a, t, why: outcome.setdefault("fail", why) and 9
    m._print_diff = lambda *x, **k: None
    m.log_decision = lambda kind, a, decision, detail="", persist=True, **extra: decisions.append(
        (kind, decision, detail, extra))
    a = SimpleNamespace(require=[], max_rounds=max_rounds, no_progress_rounds=3, author_max_iters=24,
                        lang="python", slice_plan=None, slice_id=None, label="x")
    m._preflight_loop(a, wt, F, "python3 auto-harness-check.py")
    return calls, outcome, decisions


def main():
    m = load()
    H = lambda *rows: [dict(round=i + 1, evidence=e, killed=k, survived=s, survivors=s, lines=l)
                       for i, (k, e, s, l) in enumerate(rows)]

    # ---- pure ----
    check("cap is 3 survivor-feedback rounds", m.MUTFB_MAX_ROUNDS, 3)
    check("first measurement -> feed", m.mutfb_decide(H((5, 12, 7, 3)))[0], "feed")
    check("2 refine rounds spent with progress -> still feed",
          m.mutfb_decide(H((5, 12, 7, 3), (8, 12, 4, 2), (10, 12, 2, 1)))[0], "feed")
    check("3 refine rounds spent -> stop at the cap",
          m.mutfb_decide(H((5, 12, 7, 3), (8, 12, 4, 2), (10, 12, 2, 1), (11, 12, 1, 1)))[0], "stop-cap")
    check("a round with no new kills and no fewer survivors -> stop early",
          m.mutfb_decide(H((5, 12, 7, 3), (5, 12, 7, 3)))[0], "stop-no-kills")
    check("fewer survivors without a kill (unrequired line deleted) IS progress",
          m.mutfb_decide(H((5, 12, 7, 3), (5, 11, 6, 3)))[0], "feed")
    check("more kills with the same survivors count IS progress",
          m.mutfb_decide(H((5, 12, 7, 3), (6, 13, 7, 3)))[0], "feed")
    check("25 first-seen survivor lines need 5 batches: cap stretches (first feeds are not re-feeds)",
          m.mutfb_decide(H(*[(0, 40, 25, 25)] + [(i, 40, 25 - i, 25 - i) for i in range(1, 5)]))[0], "feed")
    check("...but not beyond those batches",
          m.mutfb_decide(H(*[(0, 40, 25, 25)] + [(i, 40, 25 - i, 25 - i) for i in range(1, 6)]))[0], "stop-cap")
    check("new_kills is None for the first round, a delta after",
          (m.mutfb_new_kills(None, H((5, 12, 7, 3))[0]), m.mutfb_new_kills(H((5, 12, 7, 3))[0], H((8, 12, 4, 3))[0])),
          (None, 3))
    txt = m.mutfb_counts_text(H((5, 12, 7, 3), (8, 12, 4, 2)))
    check("counts text lists each round with the kill delta", "r1: killed 5/12, survived 7" in txt and "(+3 kills)" in txt, True)
    feed, skipped = m.feedback_survivors([surv(1), surv(2, llm_equivalent=True)])
    check("llm-flagged survivors are not fed", ([s["line"] for s in feed], skipped), ([1], 1))
    snap = m.mutfb_snapshot(2, {"verify_relevance": {"killed": 4, "evidence_mutants": 9, "survived": 5,
                                                     "generation": {"equivalent_filtered": [1, 2]},
                                                     "llm_equivalent": 1}}, [surv(1), surv(1), surv(2)])
    check("snapshot carries kills, evidence, survivors, lines, filtered-equivalents",
          (snap["killed"], snap["evidence"], snap["survived"], snap["survivors"], snap["lines"],
           snap["equivalent_filtered"], snap["llm_equivalent"]), (4, 9, 5, 3, 2, 2, 1))

    # ---- driven: progress every round, survivors never reach zero -> the CAP ends it at 3 refine rounds
    def slow(i):                       # each round kills 1 more; a fresh survivor line each time
        return (i + 1, 20, [surv(10 + i), surv(40 + i)])
    calls, out, dec = drive(m, slow)
    check("progressing but never converging: exactly 3 refine rounds, then stop", len(calls), 3)
    check("...escalated through _autoslice_on_failure with the NO PROGRESS marker",
          out.get("fail", "").startswith("[auto] NO PROGRESS for 3 survivor-feedback round(s)"), True)
    check("...the escalation carries the per-round kill counts",
          all(f"r{i}: killed" in out.get("fail", "") for i in (1, 2, 3, 4)), True)
    mf = [d for d in dec if d[0] == "mutfb"]
    check("each measured round is logged as a 'mutfb' decision with the history",
          [d[1] for d in mf], ["feed", "feed", "feed", "stop-cap"])
    check("...the last record's rounds list has 4 snapshots", len(mf[-1][3]["rounds"]), 4)

    # ---- driven: a round that kills nothing new -> stops after ONE refine round
    def flat(i):
        return (3, 20, [surv(10 + i), surv(40 + i)])
    calls, out, dec = drive(m, flat)
    check("no new kills: stops after the 1st refine round (not 3)", len(calls), 1)
    check("...and says why", "killed nothing new" in out.get("fail", ""), True)
    check("...decisions: feed then stop-no-kills", [d[1] for d in dec if d[0] == "mutfb"], ["feed", "stop-no-kills"])

    # ---- driven: survivors shrink with no kills (deleting unrequired code) keeps going; converges to GO
    def shrinking(i):
        if i >= 3:
            return (3, 20, None)
        return (3, 20 - i, [surv(10 + j) for j in range(3 - i)])
    calls, out, dec = drive(m, shrinking)
    check("survivor-shrinking rounds are progress and reach GO", (len(calls), out.get("go")), (3, True))

    # ---- driven: LLM-flagged survivors are not handed to the author
    def flagged(i):
        return (3, 20, [surv(10, llm_equivalent=True), surv(11, llm_equivalent=True)])
    calls, out, dec = drive(m, flagged)
    check("all survivors flagged equivalent: nothing dispatched to the author", len(calls), 0)
    check("...escalated, naming the classifier", "LLM classifier" in out.get("fail", ""), True)

    def mixed(i):
        if i >= 1:
            return (4, 20, None)
        return (3, 20, [surv(10, llm_equivalent=True), surv(77)])
    calls, out, dec = drive(m, mixed)
    check("mixed: only the unflagged survivor is shown to the author",
          (len(calls), ":77`" in calls[0]["prompt"] if calls else None, ":10`" in calls[0]["prompt"] if calls else None),
          (1, True, False))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("cap removed", "    if fed >= mx:\n", "    if False:\n"),
    ("early stop on no kills removed", "    if len(history) >= 2 and not mutfb_progress(history[-2], history[-1]):",
     "    if False:"),
    ("loop never stops", "            if _mf_act != \"feed\":", "            if False:"),
    ("deleting a survivor not progress", "    return cur[\"killed\"] > prev[\"killed\"] or cur[\"survived\"] < prev[\"survived\"]",
     "    return cur[\"killed\"] > prev[\"killed\"]"),
    ("first-feed batches not honoured", "max(MUTFB_MAX_ROUNDS, first_batches)", "MUTFB_MAX_ROUNDS"),
    ("llm-flagged survivors fed anyway", "        _batch, _deferred = select_refine_batch(_feed)", "        _batch, _deferred = select_refine_batch(survs)"),
    ("llm-flagged fast path removed", "            if survs and not _feed and not rel_blockers:", "            if False:"),
    ("per-round counts dropped from escalation", "Per-round kill counts -- {mutfb_counts_text(_mf_hist)}. ", ""),
    ("decision not logged", "            log_decision(\"mutfb\", a, _mf_act,", "            (lambda *z, **k: None)(\"mutfb\", a, _mf_act,"),
    ("cap default 3 -> 9", 'os.environ.get("MUTFB_MAX_ROUNDS", "3")', 'os.environ.get("MUTFB_MAX_ROUNDS", "9")'),
]


def revert_check():
    bad = 0
    src = AUTO.read_text()
    for name, old, new in MUTATIONS:
        if src.count(old) != 1:
            print(f"ANCHOR MISSING ({src.count(old)}): {name}")
            bad += 1
            continue
        with tempfile.NamedTemporaryFile("w", suffix="-auto", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "AUTO_SRC": f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
