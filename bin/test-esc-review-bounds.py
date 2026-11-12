#!/usr/bin/env python3
"""test-esc-review-bounds.py -- an escalation review is BOUNDED and EVIDENCE-GATED (2026-10-09,
esc-review 2ac42be77bcc for needs-opus-auto-rt-bg-getcommitments-status: a 499-byte stub context, a model
review that re-read two tiny files, listed $HOME (2149 entries) and ran away for 7+ minutes).

  1  worker_robust: repeat_call_key / RepeatCallGuard (2x in a row, 3x total), review_capped_text marker
  2  escalation_verdict.is_no_verdict_review (REVIEW CAPPED / NO EVIDENCE AVAILABLE first line)
  3  watcher: resolve_job_evidence (label -> chain record -> worktree/driver log), build_context carries it,
     context_has_evidence, handle_no_evidence (no spawn, deterministic verdict, index row, open),
     render-failure exemption, handle_no_verdict_review, spawn_review enqueues --role review --max-tokens
  4  self-heal refuses to act on a capped / no-evidence review (heal_job, heal, review_says_satisfied)
  5  worker run_task: repeat_call_loop, output_cap_review (+ REVIEW CAPPED captured), list_files huge-dir
     refusal, and NO effect on a research task without --capture-final-as
Usage: test-esc-review-bounds.py [--bin DIR]. Prints ESC_REVIEW_BOUNDS_TEST_OK.
"""
import argparse, importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
a = ap.parse_args()
BIN = Path(a.bin)
HOME = Path(tempfile.mkdtemp(prefix="escbounds-home-"))
os.environ["HOME"] = str(HOME)
os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ.pop("VAULT_TOKEN_FILE", None)
sys.path.insert(0, str(BIN))
R = []


def check(name, got, want):
    ok = got == want
    R.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def load(path, name):
    ld = importlib.machinery.SourceFileLoader(name, str(path))
    sp = importlib.util.spec_from_loader(name, ld)
    m = importlib.util.module_from_spec(sp)
    sys.modules[name] = m
    ld.exec_module(m)
    return m


import worker_robust as WR
import escalation_verdict as EV

# ---- 1 worker_robust helpers
rk = WR.repeat_call_key
check("1 key: read_file ignores length, defaults offset 1",
      rk("read_file", {"path": "x", "length": 100}) == rk("read_file", {"path": "x", "offset": 1, "length": 0}), True)
check("1 key: a different offset is a different page", rk("read_file", {"path": "x", "offset": 50}) == rk("read_file", {"path": "x"}), False)
check("1 key: relative and absolute spellings match via resolve",
      rk("read_file", {"path": "./x"}, lambda p: "/abs/x") == rk("read_file", {"path": "/abs/x"}, lambda p: "/abs/x"), True)
check("1 key: run_bash / web_fetch are not guarded", (rk("run_bash", {"command": "ls"}), rk("web_fetch", {"url": "u"})), (None, None))
g = WR.RepeatCallGuard()
A, B = ("read_file", "a", 1), ("read_file", "b", 1)
check("1 guard: A then B is fine", (g.observe(A), g.observe(B)), (None, None))
check("1 guard: A,B,A is fine (2 total)", g.observe(A), None)
check("1 guard: A,B,A,B,A trips on the 3rd total", bool(g.observe(B) is None and g.observe(A)), True)
g = WR.RepeatCallGuard()
check("1 guard: the same call twice in a row trips", (g.observe(A), bool(g.observe(A))), (None, True))
g = WR.RepeatCallGuard()
check("1 guard: a non-guarded call resets the run", (g.observe(A), g.observe(None), g.observe(A)), (None, None, None))
cap = WR.review_capped_text("repeat_call_loop", "read_file x re-issued", 5)
check("1 capped text: first line is the REVIEW CAPPED marker naming the reason",
      cap.splitlines()[0].startswith("REVIEW CAPPED (repeat_call_loop)"), True)
check("1 capped text carries no verdict line", "VERDICT" in cap.upper().replace("NO MODEL VERDICT", "").replace("BEFORE GIVING A VERDICT", ""), False)
check("1 bounds only for research + capture", [WR.review_bounds_active(k, c) for k, c in
      (("research", "R.txt"), ("research", None), ("coding", "R.txt"))], [True, False, False])
check("1 new exit reasons registered", all(r in WR.NEW_EXIT_REASONS for r in ("repeat_call_loop", "output_cap_review")), True)
check("1 failure_ledger tags them", all(f'"{r}"' in (BIN / "failure_ledger.py").read_text() for r in ("repeat_call_loop", "output_cap_review")), True)

# ---- 2 marker
for t, want in (("REVIEW CAPPED (output_cap_review) -- x", True), ("\n\n NO EVIDENCE AVAILABLE -- y\nVERDICT: b", True),
                ("VERDICT: a -- fine", False), ("", False), ("blah\nREVIEW CAPPED later", False)):
    check("2 is_no_verdict_review(%r)" % t[:30], EV.is_no_verdict_review(t), want)

# ---- 3 watcher
W = load(BIN / "dispatch-escalation-watcher.py", "w_escbounds")
dd = HOME / ".ollama-dispatch"
runs, wts = dd / "auto-runs", dd / "worktrees"
runs.mkdir(parents=True); (wts / "wt-feat-x").mkdir(parents=True)
(wts / "wt-feat-x" / "TASK.md").write_text("# task\n\n## Must contain\n- in lib/a.ts: `Thing`\n")
(dd / "feat-x-auto.log").write_text("driver line 1\ndriver line 2: NO PROGRESS\n")
(runs / "bund-1.json").write_text(json.dumps({"runs": {"feat-x": {
    "key": "bund-1", "label": "feat-x", "bundle": "bund-1", "phase": "ended", "outcome": "exit 1", "rounds": ["r1r1r1r1r1r1"],
    "parked": {"reason": "authoring stuck: NO PROGRESS for 2 rounds", "log": str(dd / "feat-x-auto.log"),
               "worktree": str(wts / "wt-feat-x")}}}}))
state = HOME / "qstate.json"
state.write_text(json.dumps({"jobs": [{"id": "aaaaaaaaaaaa", "label": "needs-opus-auto-feat-x", "status": "needs_opus",
                                       "cwd": str(wts / "wt-feat-x"), "bundle": "bund-1"}]}))
e = {"source": "D", "kind": "job", "plan": None, "slice_id": None, "job_id": "aaaaaaaaaaaa",
     "label": "needs-opus-auto-feat-x", "signature": "needs_opus", "reason": "queue parked this job as needs_opus: x"}
ev = W.resolve_job_evidence(e, state_path=state, auto_runs=runs, worktrees=wts, dispatch_dir=dd)
check("3 evidence: bundle/chain/worktree/driver log resolved from the LABEL",
      (ev.get("bundle"), Path(ev.get("chain_file", "")).name, ev.get("worktree"), Path(ev.get("driver_log", "")).name),
      ("bund-1", "bund-1.json", str(wts / "wt-feat-x"), "feat-x-auto.log"))
check("3 evidence: park reason + rounds", (ev.get("parked_reason"), ev.get("rounds")),
      ("authoring stuck: NO PROGRESS for 2 rounds", ["r1r1r1r1r1r1"]))
ev0 = W.resolve_job_evidence(dict(e, label="needs-opus-auto-nothing-here", job_id="zzzzzzzzzzzz"),
                             state_path=state, auto_runs=runs, worktrees=wts, dispatch_dir=dd)
check("3 evidence: nothing resolves for an unknown label", ev0, {})
body = W.build_context(e, None, evidence=ev)
check("3 context carries the driver evidence block + park reason + driver log tail",
      ("## driver evidence" in body, "NO PROGRESS for 2 rounds" in body, "driver line 2" in body), (True, True, True))
check("3 context carries the literals block when the worktree has TASK.md", "machine-checked spec literals" in body, True)
check("3 context_has_evidence: resolved context passes", W.context_has_evidence(body)[0], True)
stub = W.build_context(e, None, evidence={})
check("3 the evidence-less context is the stub (no evidence block)", W.context_has_evidence(stub)[0], False)
check("3 a slice context (## plan) counts as evidence", W.context_has_evidence("# x\n\n## plan\n- label: `a`\n")[0], True)

idx, escd = HOME / "IDX.md", HOME / "esc"
escd.mkdir()
sent = []
W.append_index = lambda e_, ctx_, line, index_md=None, **k: sent.append(("idx", line))
W.notify_desktop = lambda *a_, **k: sent.append(("notify", a_))
ctx = escd / "c.md"
ctx.write_text(stub)
check("3 gate: an evidence-less context is HANDLED (no review spawn)",
      W.handle_no_evidence(e, stub, ctx, "c", esc_dir=escd, index_md=idx), True)
rv = (escd / "c.review.md").read_text()
check("3 gate: review file starts NO EVIDENCE AVAILABLE and carries a machine verdict line",
      (rv.startswith("NO EVIDENCE AVAILABLE"), "VERDICT: " in rv, "NO ACTION TAKEN" in rv), (True, True, True))
check("3 gate: the marker makes it a no-verdict review", EV.is_no_verdict_review(rv), True)
check("3 gate: context file says NO EVIDENCE AVAILABLE", "## NO EVIDENCE AVAILABLE" in ctx.read_text(), True)
check("3 gate: index row says the escalation stays open",
      any(k == "idx" and "escalation stays open" in v and "NO EVIDENCE AVAILABLE" in v for k, v in sent), True)
check("3 gate: a context WITH evidence proceeds to review", W.handle_no_evidence(e, body, ctx, "c", esc_dir=escd, index_md=idx), False)
check("3 gate: a render failure still announces + reviews (exempt)",
      W.handle_no_evidence(e, "# minimal\n", ctx, "c", render_failed=True, esc_dir=escd, index_md=idx), False)
sent.clear()
check("3 capped review: handled, index row names the exit reason, nothing else",
      (W.handle_no_verdict_review(e, ctx, "REVIEW CAPPED (output_cap_review) -- x", idx),
       any(k == "idx" and "output_cap_review" in v and "stays open" in v for k, v in sent)), (True, True))
check("3 a real verdict review is not intercepted", W.handle_no_verdict_review(e, ctx, "VERDICT: b -- spec", idx), False)
check("3 ensure_verdict leaves a capped review alone (no classify re-run, no machine verdict appended)",
      W.ensure_verdict(e, ctx, "c", "REVIEW CAPPED (repeat_call_loop) -- x\n"), "REVIEW CAPPED (repeat_call_loop) -- x\n")

calls = []
W._queue_rows = lambda: []
class _P:  # enqueue stub: report an id, then the job "finishes"
    stdout, stderr, returncode = "enqueued abcdef123456\n", "", 0
def _run(cmd, **k):
    calls.append(cmd); return _P()
W.subprocess.run = _run
W.ESC_DIR = escd
out_path = escd / "c.local-review.txt"
out_path.write_text("REVIEW CAPPED (output_cap_review) -- x\n")   # the finished job's captured answer
W.spawn_review(ctx, timeout=5)
cmd = calls[-1] if calls else []
check("3 spawn_review enqueues the bounded role + token cap",
      ("--max-tokens" in cmd and cmd[cmd.index("--max-tokens") + 1] == str(W.ESC_REVIEW_MAX_TOKENS),
       "--role" in cmd and cmd[cmd.index("--role") + 1] == "review", W.ESC_REVIEW_MAX_TOKENS <= 8192), (True, True, True))
task_txt = (escd / "c.local-review.task.md").read_text() if (escd / "c.local-review.task.md").exists() else ""
check("3 the task tells the reviewer: read once, never list '.'", "AT MOST ONCE" in task_txt and "never list '.'" in task_txt, True)

# ---- 4 self-heal
SH = load(BIN / "dispatch-self-heal.py", "sh_escbounds")
capped = "REVIEW CAPPED (output_cap_review) -- x\n\nVERDICT: a -- (would claim satisfied)\n"
check("4 review_says_satisfied is False for a capped review even with an (a) line", SH.review_says_satisfied(capped), False)
row = {"id": "aaaaaaaaaaaa", "label": "needs-opus-auto-feat-x", "status": "needs_opus", "cwd": str(wts / "wt-feat-x")}
r1 = SH._heal_job(row, capped, jobs=[row])
check("4 _heal_job: capped review -> skip, no action", r1.startswith("skip:no model verdict"), True)
r2 = SH._heal_job(row, "NO EVIDENCE AVAILABLE -- x\nVERDICT: b -- machine", jobs=[row])
check("4 _heal_job: no-evidence review -> skip, no action", r2.startswith("skip:no model verdict"), True)
check("4 heal (slice path): capped review -> skip", SH.heal("p", "s", None, capped).startswith("skip:no model verdict"), True)

# ---- 5 worker integration
os.environ["HOME"] = tempfile.mkdtemp(prefix="escbounds-whome-")
spec = importlib.util.spec_from_file_location("ow_escbounds", BIN / "ollama-worker.py")
m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
m.log_dispatch_to_vault = lambda *a_, **k: None


def tc(name, args):
    return {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def cut(txt="x " * 50):
    return {"message": {"role": "assistant", "content": txt}, "done_reason": "length",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 6144}}


def drive(script, capture="REVIEW.txt", kind="research", pre=None):
    wt = Path(tempfile.mkdtemp(prefix="escbounds-wt-"))
    (wt / "a.txt").write_text("alpha\n"); (wt / "b.txt").write_text("beta\n")
    for k in range(450):
        (wt / "big").mkdir(exist_ok=True); (wt / "big" / f"f{k}").write_text("x")
    seq = list(script)
    def fake(host, model, messages, temperature, num_ctx, **kw):
        return seq.pop(0) if seq else {"message": {"role": "assistant", "content": "VERDICT: b -- ok"}, "done_reason": "stop",
                                       "usage": {"prompt_tokens": 1000, "completion_tokens": 20}}
    m.call_ollama = fake
    m.call_ollama_streaming = lambda *a_, **k: fake(*a_, **k)
    m.ensure_model_ready = lambda *a_, **k: None
    m.set_keep_alive = lambda *a_, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a_, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "review it", None, 20, 0.0, 65536, None,
                   max_tokens=6144, task_kind=kind, capture_final_as=capture)
    except SystemExit:
        pass
    except Exception as ex:
        logs.append(f"run_task raised: {ex!r}")
    finally:
        m.log = orig
    cap_f = wt / capture if capture else None
    return "\n".join(logs), (cap_f.read_text() if cap_f and cap_f.exists() else None)


RA, RB = ("read_file", {"path": "a.txt"}), ("read_file", {"path": "b.txt", "length": 10})
log, capt = drive([tc(*RA), tc(*RB), tc("read_file", {"path": "a.txt", "offset": 1, "length": 100}),
                   tc("read_file", {"path": "./b.txt"}), tc("read_file", {"path": "a.txt"})])
check("5 A,B,A,B,A (spelled differently) ends repeat_call_loop", "TERMINAL REASON: repeat_call_loop" in log, True)
check("5 ...and the captured file is the REVIEW CAPPED marker", (capt or "").startswith("REVIEW CAPPED (repeat_call_loop)"), True)
log, capt = drive([tc(*RA), tc(*RA)])
check("5 the same read twice in a row ends repeat_call_loop at once", "TERMINAL REASON: repeat_call_loop" in log, True)
log, capt = drive([tc(*RA), cut()])
check("5 a turn that hits the output cap ends output_cap_review (no recovery turn)",
      ("TERMINAL REASON: output_cap_review" in log, "recovery turn" in log, (capt or "").startswith("REVIEW CAPPED (output_cap_review)")),
      (True, False, True))
log, capt = drive([tc(*RA), tc(*RB), {"message": {"role": "assistant", "content": "VERDICT: b -- fine\nESC_RESULT: {}"},
                                      "done_reason": "stop", "usage": {"prompt_tokens": 9, "completion_tokens": 9}}])
check("5 a normal bounded review is untouched (final answer captured)",
      ("repeat_call_loop" in log, "output_cap_review" in log, (capt or "").startswith("VERDICT: b")), (False, False, True))
log, capt = drive([tc(*RA), tc(*RA), tc(*RA)], capture=None)
check("5 research WITHOUT --capture-final-as keeps the old behaviour (no new bound)", "repeat_call_loop" in log, False)
log, capt = drive([tc(*RA), tc(*RA)], kind="coding")
check("5 a coding task is not subject to the review bounds", "repeat_call_loop" in log, False)
wt0 = Path(tempfile.mkdtemp(prefix="escbounds-big-"))
for k in range(450):
    (wt0 / f"f{k}").write_text("x")
res = m.tool_list_files(wt0, {"path": "."})
check("5 list_files refuses a 450-entry directory with a way forward",
      (res.startswith("ERROR:"), "subdirectory" in res, len(res) < 1500), (True, True, True))
(wt0 / "small").mkdir(); (wt0 / "small" / "z").write_text("1")
check("5 list_files still lists a small directory", m.tool_list_files(wt0, {"path": "small"}), "z  (1 bytes)")

# ---- 6 the run_once SEAMS (gate before spawn; capped review stops before classify/self-heal)
os.environ["HOME"] = str(HOME)
W2 = load(BIN / "dispatch-escalation-watcher.py", "w2_escbounds")
for fn in ("prune_escalations", "retire_sweep", "index_janitor", "worktree_reap_daily", "handoff_autoclear"):
    setattr(W2, fn, lambda *a_, **k: None)
class _Q:
    stdout, stderr, returncode = "", "", 0
W2.subprocess.run = lambda *a_, **k: _Q()
W2.notify_desktop = lambda *a_, **k: None
W2.drop_continued_jobs = lambda found, *a_, **k: found
W2.review_bundle = lambda e_: None
cur = []
W2.detect_stuck_jobs = lambda *a_, **k: list(cur)
spawned, healed = [], []
reply = {"txt": "VERDICT: b -- spec\nESC_RESULT: {\"verdict\": \"b\", \"confidence\": \"low\", \"why\": \"x\"}"}
def _spawn(ctx_, **k):
    spawned.append(Path(ctx_).read_text()); return reply["txt"]
W2.spawn_review = _spawn
W2.self_heal = lambda *a_, **k: healed.append(1) or "heal-called"
W2.ensure_verdict = lambda e_, ctx_, stem_, rv, **k: rv
def esc(jid, label):
    return {"source": "D", "kind": "job", "plan": None, "slice_id": None, "job_id": jid, "label": label,
            "signature": "needs_opus", "reason": "queue parked this job as needs_opus: x"}
cur[:] = [esc("bbbbbbbbbbbb", "needs-opus-auto-no-such-thing")]
W2.run_once()
idx_txt = (W2.INDEX_MD.read_text() if W2.INDEX_MD.exists() else "")
check("6 run_once: an evidence-less job row spawns NO model review and is NOT self-healed", (len(spawned), len(healed)), (0, 0))
check("6 run_once: ...and the index row says NO EVIDENCE AVAILABLE / open", "NO EVIDENCE AVAILABLE" in idx_txt and "stays open" in idx_txt, True)
cur[:] = [esc("cccccccccccc", "needs-opus-auto-feat-x")]
(W2.AUTO_RUNS).mkdir(parents=True, exist_ok=True)
(W2.AUTO_RUNS / "bund-1.json").write_text(json.dumps({"runs": {"feat-x": {"bundle": "bund-1", "phase": "ended",
    "parked": {"reason": "authoring stuck: x", "worktree": str(wts / "wt-feat-x"), "log": str(dd / "feat-x-auto.log")}}}}))
reply["txt"] = "REVIEW CAPPED (output_cap_review) -- the review stopped\n"
W2.run_once()
check("6 run_once: a job row WITH resolvable evidence is reviewed, the context carries the evidence",
      (len(spawned), "## driver evidence" in (spawned[0] if spawned else "")), (1, True))
check("6 run_once: a CAPPED review is never self-healed", len(healed), 0)
check("6 run_once: ...and the index row names the cap", "REVIEW CAPPED (output_cap_review)" in W2.INDEX_MD.read_text(), True)
cur[:] = [esc("dddddddddddd", "needs-opus-auto-feat-x")]
reply["txt"] = "VERDICT: b -- spec\nESC_RESULT: {\"verdict\": \"b\", \"confidence\": \"low\", \"why\": \"x\"}"
W2.run_once()
check("6 control: a real verdict review IS self-healed (the gates did not disable the action path)", (len(spawned), len(healed)), (2, 1))

print("ESC_REVIEW_BOUNDS_TEST_OK" if all(R) else f"FAILED {R.count(False)}/{len(R)}")
sys.exit(0 if all(R) else 1)
