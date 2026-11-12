#!/usr/bin/env python3
"""test-pipeline-ladder.py -- the TIER LADDER (pipeline_ladder.py + its self-heal wiring).

Every rung, hermetic (temp ledger, fake hooks, no queue, no network, no claude):
  families / supersession, protection (signoff, hold, claim file, fresh row, live owner),
  grounding (a fabricated quote is DROPPED and never drives an action), tier 1 vs tier 2,
  Sonnet refusal does not spend a rung, continuation / close / re-spec / NAMED exits,
  tier-3 only for a tooling verdict, and the production wiring in dispatch-self-heal.py.
Usage: test-pipeline-ladder.py [--bin DIR]      prints LADDER_TEST_OK
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "\n     got=%r\n    want=%r" % (got, want)))


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bin", default=str(Path.home() / "bin"))
    a = ap.parse_args()
    BIN = Path(a.bin)
    sys.path.insert(0, str(BIN))
    pl = load(BIN / "pipeline_ladder.py", "pl_t")
    td = Path(tempfile.mkdtemp(prefix="ladder-test-"))
    led = td / "self-heal.json"
    now = time.time()
    wt = td / "wt"
    wt.mkdir()
    (wt / "lib.js").write_text("const a = 1;\nfunction loginQueue() {\n  return getSettings();\n}\n")
    tf = td / "TASK.txt"
    tf.write_text("Fix loginQueue.")
    old = time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 3600))

    def job(jid, label, **kw):
        d = {"id": jid, "label": label, "status": "needs_opus", "bundle": label, "cwd": str(wt),
             "task_file": str(tf), "model": "qwen3.8:27b-q4_K_M", "host_pref": "studio",
             "enqueued_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 7200)),
             "escalation": {"reason": "x", "escalated_at": old}}
        d.update(kw)
        return d

    calls = {"enq": [], "res": [], "son": 0, "resp": [], "opus": 0}

    def mk_hooks(sonnet_out=None, verify_rc=1, respec_out="skip:no argv", local=None, opus=False):
        def _son(prompt):
            calls["son"] += 1
            if callable(sonnet_out):
                return sonnet_out(prompt)
            return sonnet_out
        return {"enqueue": lambda argv: (calls["enq"].append(argv) or ("newjob%d" % len(calls["enq"]), "ok")),
                "resolve": lambda j: calls["res"].append(j) or True,
                "respec": lambda j, n, w, r: calls["resp"].append((n, w, r)) or respec_out,
                "sonnet": _son, "run_verify": lambda j: (verify_rc, "tail"),
                "local_verdict": local or (lambda j, r: (None, False, [])),
                "alive": lambda p: False, "protect_path": str(td / "protect.json"),
                "esc_dir": str(td), "tooling_path": str(td / "tooling.jsonl"),
                "opus": (lambda p: (calls.__setitem__("opus", calls["opus"] + 1) or "root cause: foo.py:1")) if opus else None}

    def reset():
        for k in ("enq", "res", "resp"):
            calls[k].clear()
        calls["son"] = 0
        calls["opus"] = 0
        if led.exists():
            led.unlink()

    # ---- families / supersession
    check("F1 numlock attempts share a family",
          {pl.job_family(x) for x in ("qagent-rt-sidecar-nl-numlock-120805", "qagent-rt-sidecar-nl-numlock2-121335",
                                      "qagent-rt-sidecar-nl-numlock4-152438")}, {"rt-sidecar-nl-numlock"})
    check("F2 distinct tasks stay distinct", pl.job_family("qagent-rt-sidecar-s1a-lib-090743")
          != pl.job_family("qagent-rt-sidecar-s1c-lq-094351"), True)
    check("F3 auto-fix rounds share a family", pl.job_family("amazon [auto-fix r2]"), pl.job_family("amazon [auto-fix r1]"))

    reset()
    j1 = job("j1", "qagent-x-fam-100000", enqueued_at=time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 9000)))
    j2 = job("j2", "qagent-x-fam2-110000")
    out = pl.process_job(j1, [j1, j2], mk_hooks(), led, now)
    check("S1 an older attempt is retired as superseded, no model call",
          (out.startswith("retired:superseded"), calls["res"], calls["son"]), (True, ["j1"], 0))
    check("S1b the retire is recorded", json.loads(led.read_text())["ladder"]["jobs"]["j1"]["exit"], "superseded")

    # ---- protection
    reset()
    check("P1 awaiting_signoff is never touched",
          pl.process_job(job("p1", "a-p1", awaiting_signoff=True), [], mk_hooks(), led, now).startswith("protected:"), True)
    check("P2 user_hold is never touched",
          pl.process_job(job("p2", "a-p2", user_hold=True), [], mk_hooks(), led, now).startswith("protected:"), True)
    (td / "protect.json").write_text(json.dumps({"claims": [{"match": "arr-webhook", "by": "a5040d",
                                                             "until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + 600))}]}))
    check("P3 a claimed row (an agent works it) is never touched",
          pl.process_job(job("p3", "arr-webhook [auto-fix r1]"), [], mk_hooks(), led, now), "protected:claimed by a5040d: arr-webhook")
    (td / "protect.json").write_text(json.dumps({"claims": [{"match": "arr-webhook", "by": "a5040d",
                                                             "until": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 600))}]}))
    check("P3b an EXPIRED claim does not protect",
          pl.protect_reason(job("p3", "arr-webhook [auto-fix r1]"), [], now, lambda p: False, str(td / "protect.json")), None)
    fresh = job("p4", "a-p4", escalation={"reason": "x", "escalated_at": time.strftime("%Y-%m-%dT%H:%M:%S+00:00", time.gmtime(now - 30))})
    check("P4 a fresh row is left to the watcher first",
          str(pl.protect_reason(fresh, [], now, lambda p: False, str(td / "protect.json"))).startswith("escalated"), True)
    own = job("p5", "a-p5", launched_by="/tmp/cc-socks/1.sock")
    check("P5 the newest attempt of a live launching session is protected",
          str(pl.protect_reason(own, [own], now, lambda p: True, str(td / "protect.json"))).startswith("owned by the live agent"), True)
    check("P5b ... but not when that session is gone",
          pl.protect_reason(own, [own], now, lambda p: False, str(td / "protect.json")), None)
    liv = job("p6", "a-p6")
    other = job("p6b", "b-p6", status="running")
    check("P6 a live job in the same worktree protects the row",
          str(pl.protect_reason(liv, [liv, other], now, lambda p: False, str(td / "protect.json"))).startswith("live job"), True)

    # ---- grounding
    kept, dropped = pl.ground_findings(
        [{"file": "lib.js", "line": 3, "quote": "return getSettings();"},
         {"file": "lib.js", "line": 2, "quote": "const hasLink = true;"},
         {"file": "nope.js", "line": 1, "quote": "whatever text here"},
         {"file": "lib.js", "line": 99, "quote": "diff only text snippet"}],
        [str(wt)], "+ diff only text snippet here")
    check("G1 real quote kept, fabricated / missing-file dropped, diff-quote kept",
          ([f["quote"] for f in kept], sorted(d["code"] for d in dropped)),
          (["return getSettings();", "diff only text snippet"], ["no-file", "quote-absent"]))

    # ---- tier 2
    good = json.dumps({"verdict": "c", "why": "mock misses getSettings", "fix_notes": "mock lib.getSettings",
                       "findings": [{"file": "lib.js", "line": 3, "quote": "return getSettings();"}]})
    bad = json.dumps({"verdict": "c", "why": "hasLink is wrong", "fix_notes": "x",
                      "findings": [{"file": "lib.js", "line": 2, "quote": "const hasLink = true;"}]})
    jv = job("t1", "auto-author-x-t1", verify="python3 auto-harness-check.py")
    reset()
    out = pl.process_job(jv, [jv], mk_hooks("Sorry, usage limit reached. Try again later."), led, now)
    check("T0 a Sonnet refusal waits and spends no rung",
          (out.startswith("wait:Sonnet unavailable"), calls["enq"], "sonnet_calls" in (json.loads(led.read_text())["ladder"]["jobs"].get("t1") or {}) if led.exists() else False),
          (True, [], False))
    reset()
    out1 = pl.process_job(jv, [jv], mk_hooks(bad), led, now)
    check("T1 an UNGROUNDED Sonnet verdict is not acted on (one more try allowed)",
          (out1.startswith("wait:Sonnet verdict UNVERIFIED"), calls["enq"], calls["resp"]), (True, [], []))
    out2 = pl.process_job(jv, [jv], mk_hooks(bad), led, now)
    check("T2 ... and after the second ungrounded answer: NAMED exit, resolved, still no action on the hallucination",
          (out2.startswith("ladder-exit:ungrounded-diagnosis"), calls["enq"], calls["res"]), (True, [], ["t1"]))
    check("T2b the exit is recorded with its reason",
          [t["reason"] for t in json.loads(led.read_text())["ladder"]["terminal"]], ["ungrounded-diagnosis"])
    reset()
    out = pl.process_job(jv, [jv], mk_hooks(good), led, now)
    argv = calls["enq"][0] if calls["enq"] else []
    check("T3 a GROUNDED Sonnet verdict c enqueues ONE continuation (--continues, own verify, findings in the task)",
          (out.startswith("heal-continuation:"), "--continues" in argv and argv[argv.index("--continues") + 1] == "t1",
           argv[argv.index("--verify") + 1] if argv else None,
           "return getSettings();" in Path(argv[argv.index("--task-file") + 1]).read_text() if argv else None),
          (True, True, "python3 auto-harness-check.py", True))
    # verdict d -> bigger model
    gd = json.dumps({"verdict": "d", "why": "incapable", "fix_notes": "", "findings": [{"file": "lib.js", "line": 3, "quote": "return getSettings();"}]})
    reset()
    pl.process_job(jv, [jv], mk_hooks(gd), led, now, cfg={"bigger_model": "big:70b", "bigger_host": "unraid"})
    argv = calls["enq"][0] if calls["enq"] else []
    check("T4 verdict d goes to the bigger model", argv[argv.index("--model") + 1] if argv else None, "big:70b")

    # ---- tier 1 grounded verdict skips Sonnet
    reset()
    out = pl.process_job(jv, [jv], mk_hooks(lambda p: 1 / 0, local=lambda j, r: ("b", True, [{"file": "lib.js", "line": 3, "quote": "return getSettings();"}])),
                         led, now, review_text="VERDICT: b")
    check("T5 a grounded LOCAL verdict never calls Sonnet", (out.startswith("heal-continuation:"), calls["son"]), (True, 0))

    # ---- verdict a
    ga = json.dumps({"verdict": "a", "why": "exists", "fix_notes": "", "findings": [{"file": "lib.js", "line": 3, "quote": "return getSettings();"}]})
    reset()
    out = pl.process_job(jv, [jv], mk_hooks(ga, verify_rc=0), led, now)
    check("A1 verdict a + verify rc 0 -> closed:satisfied", (out.startswith("closed:"), calls["res"], calls["enq"]), (True, ["t1"], []))
    reset()
    out = pl.process_job(jv, [jv], mk_hooks(ga, verify_rc=1), led, now)
    check("A2 verdict a but verify FAILS -> treated as b (continuation), not closed", (out.startswith("heal-continuation:"), calls["res"]), (True, []))

    # ---- budgets: re-spec then a NAMED exit
    reset()
    for k in range(2):
        pl._log(led, jv, "continue", "x", now)
    out = pl.process_job(jv, [jv], mk_hooks(good, respec_out="respec:lbl-rs1 pid 1"), led, now)
    check("B1 continuation budget spent -> re-spec", (out.startswith("respec:"), calls["enq"]), (True, []))
    reset()
    for k in range(2):
        pl._log(led, jv, "continue", "x", now)
    pl._log(led, jv, "respec", "x", now)
    out = pl.process_job(jv, [jv], mk_hooks(good), led, now)
    check("B2 re-spec also spent -> NAMED exit ladder-budget-spent, row resolved",
          (out.startswith("ladder-exit:ladder-budget-spent"), calls["res"]), (True, ["t1"]))
    reset()
    jq = job("q1", "qagent-y-q1-100000", verify=None)
    out = pl.process_job(jq, [jq], mk_hooks(good), led, now)
    check("B3 a row with nothing runnable (no verify) exits no-runnable-spec", out.startswith("ladder-exit:no-runnable-spec"), True)

    # ---- tier 3 only for tooling
    gt = json.dumps({"verdict": "t", "why": "qagent scope guard bug", "fix_notes": "", "findings": [{"file": "lib.js", "line": 3, "quote": "return getSettings();"}]})
    reset()
    out = pl.process_job(jv, [jv], mk_hooks(gt, opus=True), led, now)
    check("O1 a tooling verdict reaches tier 3 (Opus once) and exits tooling-root-cause",
          (out.startswith("ladder-exit:tooling-root-cause"), calls["opus"], (td / "tooling.jsonl").exists()), (True, 1, True))
    reset()
    pl.process_job(jv, [jv], mk_hooks(good, opus=True), led, now)
    check("O2 a non-tooling verdict NEVER calls Opus", calls["opus"], 0)

    # ---- sweep dry run has no side effects
    reset()
    out = pl.sweep([j1, j2], mk_hooks(), led, now, dry_run=True)
    check("W1 dry-run reports without acting", (sorted(a_[1][:12] for a_ in out), calls["res"], led.exists()),
          (["would-adjudi", "would-retire"], [], False))

    # ---- production wiring in self-heal / watcher
    heal = load(BIN / "dispatch-self-heal.py", "dsh_l")
    src = (BIN / "dispatch-self-heal.py").read_text()
    check("X1 heal_job hands park:/not-an-auto-author to the ladder",
          'ladder_enabled(lp) and str(out).startswith(("park:", "skip:not an auto-author"))' in src, True)
    check("X2 --job-sweep runs the ladder sweep", "for jid, act in ladder_sweep():" in src, True)
    check("X3 no human alert from _park_final in production", "the ladder (heal_job) takes the row from here: no human alert" in src, True)
    check("X4 ladder-exit is an ACTED prefix (self-heal + watcher)",
          ('"ladder-exit:")' in src) and ('"ladder-exit:"' in (BIN / "dispatch-escalation-watcher.py").read_text()), True)
    check("X5 the ladder is OFF under the verify sandbox / a test ledger",
          (heal.ladder_enabled(None, env={"DISPATCH_VERIFY_SANDBOX": "1"}), heal.ladder_enabled(str(td / "other.json"), env={}),
           heal.ladder_enabled(None, env={"LADDER": "off"})), (False, False, False))
    # the legacy behaviour is untouched off-production: a custom-ledger park stays a park
    j_dead = job("z1", "auto-author-zzz-z1", cwd=str(td / "gone"))
    legacy = heal.heal_job(j_dead, "VERDICT: b -- x", jobs=[j_dead], ledger_path=str(td / "z.json"), close_rows=False)
    check("X6 off-production heal_job still returns the legacy skip (no ladder)", legacy.startswith("skip:"), True)
    n = sum(RESULTS)
    print("\n%d/%d checks passed" % (n, len(RESULTS)))
    if n == len(RESULTS):
        print("LADDER_TEST_OK")
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
