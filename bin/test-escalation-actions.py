#!/usr/bin/env python3
"""test-escalation-actions.py -- the verdict must DRIVE an action (2026-10-09).

Incident: the escalation watcher produced diagnosis-only verdicts; self-heal answered
"skip: verdict none is not a fixture/harness defect (b/c)" and "skip: not an auto-author
(harness authoring) job" for 0e0e52120517 / b335c783fbcc / 08d41a94da0a, and rows piled up.

Must hold:
  V1 a review with NO VERDICT line still resolves to a verdict (structured JSON, inferred,
     queue failure_class, reason keywords, default b) -- never None; LLM verdicts are cached
  V2 failure_class (mechanical) wins over review prose
  A1 needs-opus-auto-* rows are eligible; (b) -> continuation with findings
  A2 operator stop (class=operator) -> RESUME, not "skip"
  A3 the SAME failure signature twice -> re-spec is RETIRED with the old auto driver: skipped, nothing launched
  A4 (d): configured bigger model => one round on it (not hardcoded); none => re-slice
  A5 (a) from a reviewer is trusted only after the harness self-check passes
  A6 kill switch ACTIONS-OFF => no action
  S1 slice: same signature twice => RE-SPEC retry-with-notes (once), then final rung
  W1 watcher: ensure_verdict re-runs a classify-only review once, then machine-resolves
  H1 handoff-autoclear: guards (signoff/live/worktree-live/code-high/landable) win over every
     rule and over a model "clear"; mechanical rules clear; LLM batch cached + rate limited
Everything is STUBBED (temp dirs, in-memory jobs); nothing reaches the real queue.
Usage: test-escalation-actions.py [--heal P] [--watcher P] [--verdict P] [--handoff P]
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
os.environ.pop("ESC_ACTIONS", None)
BIN = Path.home() / "bin"
RESULTS = []


def check(name, got, want):
    ok = got == want
    RESULTS.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else "\n     got=%r\n    want=%r" % (got, want)))


def guarded(name, fn):
    try:
        fn()
    except Exception as e:  # noqa: BLE001
        import traceback
        RESULTS.append(False)
        print("FAIL %s: raised %s: %s" % (name, type(e).__name__, e))
        traceback.print_exc()


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--heal", default=str(BIN / "dispatch-self-heal.py"))
    ap.add_argument("--watcher", default=str(BIN / "dispatch-escalation-watcher.py"))
    ap.add_argument("--verdict", default=str(BIN / "escalation_verdict.py"))
    ap.add_argument("--handoff", default=str(BIN / "handoff-autoclear.py"))
    a = ap.parse_args()
    sys.path.insert(0, str(Path(a.verdict).parent))
    ev = load(a.verdict, "ev_under_test")
    sys.modules["escalation_verdict"] = ev
    heal = load(a.heal, "dsh_act")
    td = Path(tempfile.mkdtemp(prefix="escact-test-"))
    ev.KILL_FILE = td / "ACTIONS-OFF"
    (td / "cfg.json").write_text(json.dumps({"respec_max_per_chain": 1}))
    ev.CONFIG_PATH = td / "cfg.json"
    cache = td / "vc.json"

    # ---------------------------------------------------------------- V1 / V2
    def t_verdict():
        ramble = "Let me analyze.\n1. job ran\nSo my verdict is (c) - quite so.\n"
        check("V1 inferred from prose", ev.resolve(ramble, None, "x", cache_path=td / "c1.json")["verdict"], "c")
        js = 'blah\nESC_RESULT: {"verdict": "d", "confidence": "high", "why": "model"}\n'
        r = ev.resolve(js, None, "x", cache_path=td / "c2.json")
        check("V1 structured ESC_RESULT wins", (r["verdict"], r["source"]), ("d", "structured"))
        r = ev.resolve("nothing useful here", None, "fixture PASSES at baseline", cache_path=td / "c3.json")
        check("V1 reason keywords", (r["verdict"], r["source"]), ("b", "reason"))
        r = ev.resolve("", None, "", cache_path=td / "c4.json")
        check("V1 never None: default b", (r["verdict"], r["source"]), ("b", "default"))
        r = ev.resolve("VERDICT: (d) -- model", "spec", "x", cache_path=td / "c5.json")
        check("V2 mechanical failure_class beats prose", (r["verdict"], r["source"]), ("b", "mechanical"))
        r1 = ev.resolve("the verdict is (a)", None, "x", cache_path=td / "c6.json", key_parts=("k",))
        r2 = ev.resolve("the verdict is (a)", None, "x", cache_path=td / "c6.json", key_parts=("k",))
        check("V1 cached by input hash, not re-asked", (r1["cached"], r2["cached"], r2["verdict"]),
              (False, True, "a"))
        s1 = ev.failure_signature("spec", "fixture passes at baseline 12", "nonconvergence")
        s2 = ev.failure_signature("spec", "fixture passes at baseline 99", "nonconvergence")
        check("signature masks digits", s1, s2)
        cfg = ev.load_config(path=td / "none.json", env={"ESC_BIGGER_MODEL": "big-x"})
        check("bigger model comes from config/env, never hardcoded",
              (cfg["bigger_model"], ev.DEFAULTS["bigger_model"]), ("big-x", None))
    guarded("verdict", t_verdict)

    # ---------------------------------------------------------------- jobs
    wt = td / "wt"
    wt.mkdir()
    (wt / "AUTO-TASK.md").write_text("task\n")
    led = td / "self-heal.json"
    dec = td / "decisions.jsonl"
    runs = td / "slice-runs"
    runs.mkdir()
    autoruns = td / "autoruns"
    (autoruns / "argv").mkdir(parents=True)
    heal.AUTO_RUNS = autoruns
    heal.DECISIONS = dec
    now = time.time()
    iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))

    def row(jid="aaaa11112222", label="auto-author-smoke-x-c1", cls="spec", detail="fixture passes at baseline",
            cont=None, model="qwen-test", **kw):
        r = {"id": jid, "label": label, "status": "needs_opus", "model": model, "host_pref": "studio",
             "cwd": str(wt), "task_file": str(wt / "AUTO-TASK.md"), "verify": "python3 auto-harness-check.py",
             "max_iters": 24, "num_ctx": 65536, "bundle": "smoke-x", "failure_class": cls,
             "failure_detail": detail, "terminal_reason": "nonconvergence", "continues": cont,
             "escalation": {"escalated_at": iso(now - 300), "reason": "authoring stuck"}}
        r.update(kw)
        return r

    enq = []

    def stub_enqueue(argv):
        enq.append(argv)
        return "bbbb%08d" % len(enq), "enqueued"

    alerts = []
    notify = lambda t, m, dedupe_key=None, dedupe_s=0: alerts.append(dedupe_key)
    common = dict(ledger_path=led, slice_runs=runs, enqueue=stub_enqueue, notifier=notify,
                  decisions=dec, log_dir=td / "logs", verdict_cache=td / "vc-heal.json")

    def t_eligibility_and_b():
        led.unlink(missing_ok=True)
        r = row(label="needs-opus-auto-smoke-x-r2")
        got = heal.heal_job(r, "VERDICT: b -- spec", jobs=[r], **common)
        check("A1 needs-opus-auto-* label is eligible and (b) -> continuation",
              (got.startswith("heal-continuation:"), len(enq)), (True, 1))
        check("A1 prompt carries the findings", "ESCALATION REVIEW FINDINGS" in (wt / "AUTO-TASK.md").read_text(), True)
    guarded("eligibility", t_eligibility_and_b)

    def t_operator_resume():
        led.unlink(missing_ok=True)
        enq.clear()
        r = row(cls="operator", detail="force stopped")
        got = heal.heal_job(r, "no verdict at all, ramble", jobs=[r], **common)
        check("A2 operator stop + review with NO verdict -> resume continuation (was: skip verdict none)",
              (got.startswith("heal-continuation:"), len(enq)), (True, 1))
        rounds = [r for c in json.loads(led.read_text())["jobs"]["chains"].values() for r in c["rounds"]]
        check("A2 the round is recorded as kind=resume with the mechanical source (not an unverified close)",
              [r.get("kind") for r in rounds], ["resume"])
    guarded("operator", t_operator_resume)

    def t_repeat_respec():
        led.unlink(missing_ok=True)
        enq.clear()
        (autoruns / "argv" / "smoke-x.json").write_text(json.dumps(
            {"label": "smoke-x", "argv": ["--repo", "/r", "--label", "smoke-x", "--bundle", "smoke-x",
                                          "--intent", "do thing", "--interface", "iface"]}))
        prev = row(jid="cccc33334444", label="auto-author-smoke-x", cont=None)
        cur = row(jid="dddd55556666", label="auto-author-smoke-x-c1", cont="cccc33334444")
        launched, resolved = [], []
        # drive respec_job directly through heal_job with injected launch via module hook
        orig = heal._launch_detached
        heal._launch_detached = lambda cmd, cwd, log: launched.append(cmd) or 4242
        os.environ.pop("DISPATCH_VERIFY_SANDBOX", None)
        orig_ql = heal._queue_resolve
        heal._queue_resolve = lambda j: resolved.append(j) or True
        orig_h = heal.HEAL_LEDGER
        heal.HEAL_LEDGER = led
        try:
            got = heal.heal_job(cur, "ramble no verdict", jobs=[prev, cur], runs_dir=autoruns, intent_rewriter=False, **common)
        finally:
            heal._launch_detached = orig
            heal._queue_resolve = orig_ql
            heal.HEAL_LEDGER = orig_h
            os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
        check("A3 same signature twice -> re-spec retired (old auto driver): skipped, nothing launched",
              (str(got).startswith("skip:old auto driver retired"), launched, len(enq)), (True, [], 0))
    guarded("respec", t_repeat_respec)

    def t_pure_plan():
        cfg = dict(ev.DEFAULTS, respec_max_per_chain=1)
        j = row()
        p = heal.plan_job_action(j, "d", ["s1"], cfg, {})
        check("A4 (d) without a configured bigger model -> re-slice (respec)", p["kind"], "respec")
        cfg["bigger_model"] = "hosted-big"
        p = heal.plan_job_action(j, "d", ["s1"], cfg, {})
        check("A4 (d) with bigger_model configured -> bigger", p["kind"], "bigger")
        p = heal.plan_job_action(j, "d", ["s1"], cfg, {"rounds": [{"kind": "bigger"}]})
        check("A4 bigger model is tried ONCE", p["kind"], "respec")
        p = heal.plan_job_action(j, "c", ["s1"], cfg, {})
        check("(c) first time -> harness-repair continuation", p["kind"], "continue")
        p = heal.plan_job_action(j, "c", ["s1", "s1"], cfg, {})
        check("(c) same signature twice -> respec", p["kind"], "respec")
        p = heal.plan_job_action(j, "b", ["s1", "s2", "s3", "s4"], cfg, {})
        check("attempt cap 3 -> respec", p["kind"], "respec")
        p = heal.plan_job_action(j, "b", ["s1", "s1"], cfg, {"respecs": [1]})
        check("respec spent -> park", p["kind"], "park")
        p = heal.plan_job_action(dict(j, model=None, task_file=None, cwd=None), "b", ["s1"], cfg, {})
        check("placeholder row (no model) -> respec", p["kind"], "respec")
        p = heal.plan_job_action(j, "a", ["s1"], cfg, {}, source="structured")
        check("A5 (a) from a reviewer -> verify-then-close", p["kind"], "close")
    guarded("plan", t_pure_plan)

    def t_close_rows():
        idx = td / "ESC.md"
        idx.write_text("- [ ] `x` **aaaa11112222** D - t - v - `c`\n- [ ] `b` **BUNDLE PARKED** Q - t - parked: aaaa11112222: foo\n"
                       "- [ ] `y` **zzzz** D - t - v - `c`\n")
        got = heal.close_index_rows("aaaa11112222", "respec:foo pid 1", index=idx)
        txt = idx.read_text().splitlines()
        check("C1 an acted outcome ticks the job's D row and its PARKED row, not others",
              (len(got), [l[:6] for l in txt]), (2, ["- [x] ", "- [x] ", "- [ ] "]))
        logd = (td / "ESCALATIONS-AUTOCLOSE.jsonl").read_text()
        check("C1 the action taken is recorded", "action taken: respec:foo" in logd, True)
    guarded("close rows", t_close_rows)

    def t_rewrite():
        argv = ["--intent", "CREATE lib/x.ts with the thing " * 6, "--label", "q"]
        calls = []
        rw = lambda p: calls.append(p) or "MODIFY and EXTEND the existing lib/x.ts so that the thing holds " * 3
        out = heal.rewrite_intent_argv(argv, "SPEC_DEFECT: creation target exists at base", "n", rw,
                                       cache_path=td / "rw.json")
        again = heal.rewrite_intent_argv(argv, "SPEC_DEFECT: creation target exists at base", "n", rw,
                                         cache_path=td / "rw.json")
        check("R1 respec rewrites --intent from the diagnosis (one Sonnet call, cached)",
              (out[1].startswith("MODIFY"), again == out, len(calls)), (True, True, 1))
        junk = heal.rewrite_intent_argv(argv, "d", "n", lambda p: "Sorry, I cannot", cache_path=td / "rw2.json")
        check("R1 a refusal keeps the old intent", junk, argv)
        check("label_base strips -rsN and needs-opus-auto-",
              heal.label_base("needs-opus-auto-smoke-x-rs2"), "smoke-x")
    guarded("rewrite", t_rewrite)

    def t_bigger_and_close_and_kill():
        led.unlink(missing_ok=True)
        enq.clear()
        r = row(cls="model", detail="hit iteration cap")
        cfg = dict(ev.DEFAULTS, bigger_model="hosted-big", bigger_host="studio2")
        got = heal.heal_job(r, "VERDICT: d -- model", jobs=[r], config=cfg, **common)
        argv = enq[0] if enq else []
        check("A4 bigger-model round launched on the configured model",
              (got.startswith("heal-continuation:"), argv[argv.index("--model") + 1] if argv else None),
              (True, "hosted-big"))
        led.unlink(missing_ok=True)
        enq.clear()
        res = []
        r = row(cls="model", detail="x")
        got = heal.heal_job(r, 'VERDICT: a -- already satisfied', jobs=[r],
                            closer=lambda: type("R", (), {"returncode": 0})(),
                            resolve=lambda j: res.append(j), **common)
        # no auto-harness-check.py in wt -> not verified -> must NOT close
        check("A5 (a) without a passing self-check is NOT closed", (got.startswith("closed:"), res), (False, []))
        (wt / "auto-harness-check.py").write_text("")
        led.unlink(missing_ok=True)
        got = heal.heal_job(r, 'VERDICT: a -- already satisfied', jobs=[r],
                            closer=lambda: type("R", (), {"returncode": 0})(),
                            resolve=lambda j: res.append(j), **common)
        check("A5 (a) with a passing self-check closes and resolves the row",
              (got.startswith("closed:"), res), (True, ["aaaa11112222"]))
        ev.KILL_FILE.write_text("x")
        led.unlink(missing_ok=True)
        enq.clear()
        got = heal.heal_job(row(), "VERDICT: b -- spec", jobs=[row()], **common)
        ev.KILL_FILE.unlink()
        check("A6 kill switch: no action", (got.startswith("skip:ACTIONS-OFF"), len(enq)), (True, 0))
    guarded("bigger/close/kill", t_bigger_and_close_and_kill)

    # ---------------------------------------------------------------- slices
    def t_slice():
        plan_p = td / "plan.json"
        plan_p.write_text("{}")
        runs2 = td / "runs2"
        runs2.mkdir()
        (runs2 / "p.json").write_text(json.dumps({"plan_path": str(plan_p), "slices": {"s1": {
            "status": "escalated", "escalation_reason": "authoring made no progress: same thing",
            "worktree": str(wt)}}}))
        led2 = td / "slice-heal.json"
        calls = []
        launch = lambda pp, args, lp: calls.append(args)
        heal._verdict_cache = None
        heal.heal._verdict_cache = td / "vc-slice.json"
        outs = []
        orig_d = heal.DECISIONS
        for i in range(4):
            outs.append(heal.heal("p", "s1", None, "no verdict ramble", slice_runs=runs2,
                                  ledger_path=led2, launch=launch))
        notes = [("--review-notes" in c) for c in calls]
        check("S1 slice: 1st retry, then RE-SPEC at the 2nd identical signature, then final rung",
              (outs[0], outs[1], outs[2], len(calls)), ("retry", "retry", "park:final-rung", 2))
        led_d = json.loads(led2.read_text())
        check("S1 respec recorded once", led_d["p/s1"].get("respecs"), 1)
    guarded("slice", t_slice)

    # ---------------------------------------------------------------- watcher
    def t_watcher():
        w = load(a.watcher, "dew_act")
        w.ESC_DIR = td / "esc"
        w.LEDGER = w.ESC_DIR / "ledger.json"
        w.ESC_DIR.mkdir()
        ctx = w.ESC_DIR / "x.md"
        ctx.write_text("context\n")
        e = {"source": "D", "kind": "job", "job_id": "zzzz", "label": "auto-author-q", "reason": "fixture PASSES at baseline"}
        calls = []
        rr = lambda c, prompt=None, bundle=None, **k: calls.append(prompt) or "VERDICT: c -- harness\n"
        out = w.ensure_verdict(e, ctx, "x", "rambling without any verdict", rerun=rr, ev=ev)
        check("W1 verdict-less review -> ONE classify-only re-run is used",
              (len(calls), "VERDICT: c" in out), (1, True))
        calls.clear()
        out = w.ensure_verdict(e, ctx, "x", "rambling without any verdict", rerun=rr, ev=ev)
        check("W1 re-run is capped per escalation, then machine-resolved (never left)",
              (len(calls), "machine-resolved" in out, ev.parse_review(out)[0]), (0, True, "b"))
        out = w.ensure_verdict(e, ctx, "x", "VERDICT: d -- fine", rerun=rr, ev=ev)
        check("W1 a review with a verdict is untouched", out, "VERDICT: d -- fine")
        check("watcher: acted prefixes mark the row [x]",
              (w.heal_retired_it("respec:foo pid 1"), w.heal_retired_it("heal-continuation:abc"),
               w.heal_retired_it("skip:x"), w.heal_retired_it("park:final-rung")), (True, True, False, False))
        check("watcher: prompt demands the machine-checkable line and stays format-safe",
              "ESC_RESULT" in w.REVIEW_PROMPT.format(ctx="/x"), True)
    guarded("watcher", t_watcher)

    # ---------------------------------------------------------------- handoff
    def t_handoff():
        h = load(a.handoff, "hac_under_test")
        h.WORK = td / "hwork"
        h.LLM_CACHE = h.WORK / "cache.json"
        h.LLM_STAMP = h.WORK / "stamp.json"
        h.LOG = h.WORK / "log.jsonl"
        mk = lambda jid, **kw: dict({"id": jid, "label": "auto-author-zz", "status": "done", "gate": "skipped",
                                     "files": 0, "awaiting_signoff": False, "code_high": 0,
                                     "untrusted": False, "cwd": "/nowhere/%s" % jid}, **kw)
        live_by_id, live_cwds = h.live_maps([{"id": "L1", "status": "running", "cwd": "/nowhere/shared"},
                                             {"id": "N1", "status": "needs_opus", "cwd": "/n"}])
        G = lambda r: h.guard(r, live_by_id, live_cwds, handlocked=False)
        check("H1 sign-off guard", G(mk("a1", awaiting_signoff=True)), "awaiting sign-off")
        check("H1 live-in-queue guard", G(mk("N1"))is not None, True)
        check("H1 worktree-live guard", G(mk("a2", cwd="/nowhere/shared")) is not None, True)
        check("H1 code-high guard", G(mk("a3", code_high=2)), "code-high findings")
        check("H1 landable deliverable guard", G(mk("a4", gate="PASS", files=3)) is not None, True)
        check("H1 plain finished authoring row passes the guards", G(mk("a5")), None)
        data = {"complete": [mk("r1"), mk("r2", awaiting_signoff=True), mk("r3", label="odd-job", status="failed"),
                             mk("r4", label="odd-job2", status="failed", cwd="/nowhere/shared")]}
        asked = []

        def claude(prompt):
            asked.append(prompt)
            # a hostile model: says clear for EVERYTHING incl. the sign-off row and the live one
            ids = [d["id"] for d in json.loads(prompt.split("JOBS:\n", 1)[1])]
            return 0, json.dumps([{"id": i, "decision": "clear", "reason": "routine"} for i in ids]
                                 + [{"id": "ghost", "decision": "clear", "reason": "x"}])
        sidecars = {"__ts__": {}}
        pl = h.plan(data, [{"id": "L1", "status": "running", "cwd": "/nowhere/shared"}], sidecars, {},
                    claude=claude, now=1000.0, handlocked=False)
        cleared = sorted(r["id"] for r, _ in pl["clear"])
        check("H1 no-deliverable rule + LLM clear applied; sign-off and live-cwd rows NEVER cleared",
              cleared, ["r1", "r3"])
        check("H1 LLM saw only guard-free, rule-free rows", len(asked), 1)
        pl2 = h.plan(data, [{"id": "L1", "status": "running", "cwd": "/nowhere/shared"}], sidecars, {},
                     claude=claude, now=1010.0, handlocked=False)
        check("H1 LLM verdict cached by input hash (not re-asked)", (len(asked), pl2["llm"]), (1, "cached"))
        data2 = {"complete": [mk("r9", label="odd-job9", status="failed")]}
        pl3 = h.plan(data2, [], sidecars, {}, claude=claude, now=1020.0, handlocked=False)
        check("H1 rate limit: a second NEW batch within 20 min is not sent", (len(asked), pl3["llm"].startswith("rate")), (1, True))
        sc = {"/nowhere/s": [{"id": "later", "status": "done", "exit_code": 0, "_ts": 50, "cwd": "/nowhere/s"}],
              "__ts__": {"old": 10}}
        check("H1 superseded rule", (h.mechanical(mk("old", status="failed", cwd="/nowhere/s", label="x"), sc, {}) or "")[:10],
              "superseded")
        led_ = {"jobs": {"chains": {"c": {"rounds": [{"for": "oo", "job": "n1", "kind": "continue"}]}}}}
        check("H1 handled-by-self-heal rule", (h.mechanical(mk("oo", status="failed", label="x"), {"__ts__": {}}, led_) or "")[:7], "handled")
        M0 = lambda **kw: (h.mechanical(mk("td", **kw), {"__ts__": {}}, {}) or "")[:17]
        check("H1 test-draft reject rule (failed, -rN replay, gate FAIL on done)",
              [M0(label="plexrepo-x-test-draft", status="failed"), M0(label="docsum-render-test-draft-r2", status="failed"),
               M0(label="ledger-y-test-draft", status="done", gate="FAIL")], ["test-draft reject"] * 3)
        check("H1 test-draft ACCEPT and look-alike labels are left to the guards/LLM",
              [M0(label="ledger-y-test-draft", status="done", gate="PASS"), M0(label="test-draft-tools", status="failed"),
               M0(label="x-test-drafter", status="failed")], ["", "", ""])
        cl = []
        done = h.apply({"clear": [(mk("r1"), "no deliverable: x")], "followup": [(mk("r7"), "do y", "needs work")]},
                       clearer=lambda ids, reason: cl.append((ids, reason)) or (0, "ok"),
                       follow=lambda row, task: "f123456789")
        check("H1 follow-up becomes a queue job and the original is cleared with its id",
              (sorted(done), any("f123456789" in r for _, r in cl)), (["r1", "r7"], True))
        check("H1 merge-claim words are refused in reasons", _raises(lambda: h.clear(["x"], "landed in main")), True)
        os.environ["HANDOFF_AUTOCLEAR"] = "off"
        check("H1 kill switch", h.killed(), True)
        del os.environ["HANDOFF_AUTOCLEAR"]
    guarded("handoff", t_handoff)

    n = sum(RESULTS)
    print("\n%d/%d checks passed" % (n, len(RESULTS)))
    return 0 if n == len(RESULTS) else 1


def _raises(fn):
    try:
        fn()
    except AssertionError:
        return True
    return False


if __name__ == "__main__":
    sys.exit(main())
