#!/usr/bin/env python3
"""Worker-side pieces added 2026-10-09: observation masking, --extra-iterations, the --mode
fixed lane seam (+ fixed_lane_* terminal reasons), and the stale-bytecode hazard.

Pure checks on worker_robust.mask_observations / ollama-worker helpers, plus end-to-end
checks that drive the REAL run_task with the model stubbed (no GPU, no network) and a fake
fixed_lane module.

Run: python3 test-worker-obs-mask-lane.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
failures = []


def ok(name, cond, detail=""):
    print(f"  ok   {name}" if cond else f"  FAIL {name} {detail}")
    if not cond:
        failures.append(name)


os.environ["HOME"] = tempfile.mkdtemp(prefix="obsmask-home-")
os.environ.pop("VAULT_TOKEN_FILE", None)
os.environ.pop("WORKER_OBS_MASK", None)
os.environ.pop("PYTHONDONTWRITEBYTECODE", None)   # the worker must set it itself
sys.path.insert(0, str(WORKER.parent))
spec = importlib.util.spec_from_file_location("ollama_worker_obsmask", WORKER)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
m.log_dispatch_to_vault = lambda *a, **k: None
wr = m._wr


def tcall(name, args, tid=None):
    d = {"function": {"name": name, "arguments": args}}
    if tid:
        d["id"] = tid
    return d


def transcript(n_turns, big=3000, verify_at=None):
    msgs = [{"role": "system", "content": "sys"}, {"role": "user", "content": "task"}]
    for t in range(n_turns):
        cmd = "pytest -q" if t == verify_at else f"echo {t}"
        msgs.append({"role": "assistant", "content": f"thinking {t}",
                     "tool_calls": [tcall("run_bash", {"command": cmd}, f"c{t}")]})
        msgs.append({"role": "tool", "tool_call_id": f"c{t}",
                     "content": json.dumps({"exit_code": 0, "stdout": f"OUT{t} " + "x" * big})})
    return msgs


print("== mask_observations (pure) ==")
cfg = {"keep_turns": 4, "min_chars": 2000, "stride": 2}
msgs = transcript(12)
snapshot = json.dumps(msgs)
view, st = wr.mask_observations(msgs, cfg, {}, None)
ok("input is never mutated", json.dumps(msgs) == snapshot)
ok("old bulky outputs are masked", "old output elided" in view[3]["content"] and st["masked"] > 0)
ok("placeholder keeps the command", "echo 0" in view[3]["content"])
ok("most recent outputs stay in full", view[-1]["content"] == msgs[-1]["content"])
ok("assistant reasoning/actions are kept in full",
   all(a == b for a, b in zip((x for x in view if x["role"] == "assistant"),
                              (x for x in msgs if x["role"] == "assistant"))))
ok("a placeholder is much smaller", len(view[3]["content"]) < 400)
small = transcript(12, big=50)
v2, s2 = wr.mask_observations(small, cfg, {}, None)
ok("small outputs are never masked", s2["masked"] == 0 and v2 == small)
errm = transcript(12)
errm[3]["content"] = "ERROR: " + "y" * 4000
v3, _ = wr.mask_observations(errm, cfg, {}, None)
ok("ERROR results are kept", v3[3]["content"].startswith("ERROR"))
vm = transcript(12, verify_at=1)
v4, _ = wr.mask_observations(vm, cfg, {}, None)
ok("the most recent verify output is NEVER masked even when old",
   v4[5]["content"] == vm[5]["content"] and "old output elided" in v4[3]["content"])
vm2 = transcript(12, verify_at=1)
vm2[2]["tool_calls"][0]["function"]["arguments"] = json.dumps({"command": "pytest -q"})
v5, _ = wr.mask_observations(vm2, cfg, {}, None)
ok("string (JSON) arguments are understood", v5[5]["content"] == vm2[5]["content"])
vv, _ = wr.mask_observations(transcript(12), cfg, {}, "bash run-my-checks.sh")
ok("masking works with a verify string that never ran", "old output elided" in vv[3]["content"])

um = transcript(12)
um[3] = {"role": "user", "content": "[tool result for write_file]: ERROR: your write_file tool-call arguments were not valid JSON (Expecting ',' delimiter: line 1 column 99) " + "z" * 4000}
um[2]["tool_calls"] = [tcall("write_file", {"path": "a.py"})]
v6, _ = wr.mask_observations(um, cfg, {}, None)
ok("parse-error feedback (manual-tools shape) is never masked", v6[3]["content"] == um[3]["content"])

print("== prefix stability across turns (prompt cache) ==")
state = {}
prev = None
changes = 0
for n in range(6, 20):
    mm = transcript(n)
    vw, _ = wr.mask_observations(mm, cfg, state, None)
    if prev is not None:
        pv = prev
        # common prefix of the previous view's messages vs this view's
        k = 0
        while k < len(pv) and pv[k] == vw[k]:
            k += 1
        if k < len(pv):
            changes += 1
    prev = vw
ok("view only rewrites an already-sent prefix on stride boundaries", changes <= 7, f"changes={changes}")
ok("...and not on every turn", changes < 13, f"changes={changes}")
s_a, s_b = {}, {}
ok("deterministic: same transcript + same state history -> same view",
   wr.mask_observations(transcript(14), cfg, s_a, None)[0] == wr.mask_observations(transcript(14), cfg, s_b, None)[0])

print("== kill switch / config ==")
ok("default enabled", wr.obs_mask_enabled({}, {}))
ok("profile obs_mask:false disables", not wr.obs_mask_enabled({"obs_mask": False}, {}))
ok("env WORKER_OBS_MASK=0 disables", not wr.obs_mask_enabled({}, {"WORKER_OBS_MASK": "0"}))
c = wr.obs_mask_config({"obs_mask_keep_turns": 3, "obs_mask_min_chars": 500, "obs_mask_stride": 0})
ok("profile settings read, stride floored at 1", c == {"keep_turns": 3, "min_chars": 500, "stride": 1}, str(c))
prof = m._mp.get_profile("qwen3.6-35b-a3b-vl-mtp-mxfp8", "author")["robust"]
ok("model_profiles.yaml robust section carries the mask settings",
   prof.get("obs_mask") is True and prof.get("obs_mask_keep_turns") and prof.get("obs_mask_min_chars"), str(prof))

print("== --extra-iterations ==")
ok("adds to the ceiling", m.apply_extra_iterations(30, 6) == (36, 6))
ok("0 / None / junk / negative = unchanged",
   all(m.apply_extra_iterations(30, x) == (30, 0) for x in (0, None, "x", -5)))
ok("capped", m.apply_extra_iterations(30, 10 ** 6) == (30 + m.EXTRA_ITERATIONS_MAX, m.EXTRA_ITERATIONS_MAX))
ok("composes with an involuntary-pause resume ceiling",
   m.apply_extra_iterations(m.resume_total_iters(7, 14, "external_sigterm"), 6)[0] == 20)
ap_src = WORKER.read_text()
ok("CLI accepts --extra-iterations and --mode auto|open|fixed",
   '"--extra-iterations"' in ap_src and '"--mode", choices=["auto", "open", "fixed"], default="open"' in ap_src)

print("== per-role thinking policy (model_profile loader) ==")
import copy
_d = m._mp.load_profiles()
_k = next(iter(_d["models"]))
_d2 = copy.deepcopy(_d)
_d2["models"][_k]["modes"]["thinking_coding"]["thinking"] = "off"
_d2["models"][_k]["modes"]["review"]["thinking"] = "low"
ok("no thinking key: profile unchanged", m._mp.get_profile(_k, "author", data=_d)["thinking"] is None
   and m._mp.get_profile(_k, "author", data=_d)["enable_thinking"] is not False)
ok("thinking: off pins enable_thinking False for that role", m._mp.get_profile(_k, "author", data=_d2)["enable_thinking"] is False)
ok("thinking: low is surfaced, enable_thinking untouched",
   m._mp.get_profile(_k, "gate", data=_d2)["thinking"] == "low")
ok("after a prose cut-off the next turn is non-thinking (a78ff3a)",
   any("_next_turn_think = False" in part[:1500] for part in WORKER.read_text().split("_cap_cut_recover = True")[1:]))

print("== stale-bytecode hazard ==")
_PYC_ENV = {k: v for k, v in os.environ.items() if k not in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX")}
env = m._shell_env()
ok("shells/verify run with PYTHONDONTWRITEBYTECODE=1", env.get("PYTHONDONTWRITEBYTECODE") == "1")
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    f = td / "mod.py"
    f.write_text("def f():\n    return 1 + 1\n")
    subprocess.run([sys.executable, "-c", "import mod"], cwd=td, check=True, env=_PYC_ENV)       # leaves mod.cpython-*.pyc
    pcs = list((td / "__pycache__").glob("mod.*.pyc"))
    ok("precondition: a pyc exists", len(pcs) == 1)
    st0 = f.stat()
    r = m.tool_edit_file(td, {"path": "mod.py", "old_string": "1 + 1", "new_string": "9 + 9"})
    ok("edit applied", r.startswith("OK"), r)
    os.utime(f, ns=(st0.st_atime_ns, st0.st_mtime_ns))     # same mtime, same size == the hazard
    rc, out, err = m._run_shell_group("python3 -c 'import mod; print(mod.f())'", td, 30)
    ok("verify sees the NEW code, not the old bytecode", out.strip() == "18", out + err)
    ok("worker's own run left no new bytecode", not list((td / "__pycache__").glob("mod.*.pyc")))
    f.write_text("def f():\n    return 2 + 2\n")
    subprocess.run([sys.executable, "-c", "import mod"], cwd=td, check=True, env=_PYC_ENV)
    st1 = f.stat()
    m.tool_write_file(td, {"path": "mod.py", "content": "def f():\n    return 3 + 3\n"})
    os.utime(f, ns=(st1.st_atime_ns, st1.st_mtime_ns))
    rc, out, err = m._run_shell_group("python3 -c 'import mod; print(mod.f())'", td, 30)
    ok("write_file also drops the stale pyc", out.strip() == "6", out + err)


# ----------------------------------------------------------------------------------------
def drive(model_fn, *, mode="open", lane=None, verify=None, max_iters=4, extra=0, files=None, api="openai"):
    """Run the REAL run_task with the model stubbed. -> (rc, sent_messages_per_call, logs, metrics)."""
    sent, logs = [], []
    td = tempfile.mkdtemp(prefix="obsmask-wt-")
    wt = Path(td)
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=wt, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=wt, check=True)
    for k, v in (files or {"f.txt": "x\n"}).items():
        (wt / k).write_text(v)
    subprocess.run(["git", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "commit", "-qm", "base"], cwd=wt, check=True)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        sent.append(json.loads(json.dumps(messages)))
        return model_fn(len(sent))

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.call_openai_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    if lane is not None:
        sys.modules["fixed_lane"] = lane(wt)
    orig = m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    rc = None
    try:
        rc = m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "Fix f.txt so it says y.", verify,
                        max_iters, 0.0, 65536, None, max_tokens=8192, api_style=api, mode=mode,
                        extra_iterations=extra, fixed_targets=["f.txt"])
    except SystemExit as e:
        rc = e.code
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
        sys.modules.pop("fixed_lane", None)
    return rc, sent, logs, dict(m._dispatch_metrics), wt


def done_msg():
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [tcall("task_complete", {"summary": "done"}, "tc")]},
            "done_reason": "stop", "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}


def bash_msg(n, big=True):
    cmd = f"python3 -c \"print('x'*3000+'{n}')\"" if big else f"echo {n}"
    return {"message": {"role": "assistant", "content": f"step {n}",
                        "tool_calls": [tcall("run_bash", {"command": cmd}, f"b{n}")]},
            "done_reason": "stop", "usage": {"prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110}}


print("== end-to-end: observation masking in the real loop ==")
rc, sent, logs, met, wt = drive(lambda n: bash_msg(n) if n < 15 else done_msg(), max_iters=20)
ok("run completed through the stub", len(sent) == 15, f"calls={len(sent)} {logs[-3:]}")
last = sent[-1]
tool_outs = [x["content"] for x in last if x["role"] == "tool"]
ok("late prompt has elided old outputs", any("old output elided" in t for t in tool_outs))
ok("late prompt keeps the newest outputs in full", "x" * 3000 in tool_outs[-1])
ok("first prompts are untouched (nothing old enough)", all("old output elided" not in str(x) for x in sent[3]))
ok("metrics record the masking", met.get("obs_masked"), str({k: v for k, v in met.items() if "obs" in k}))
os.environ["WORKER_OBS_MASK"] = "0"
rc, sent2, logs2, met2, _ = drive(lambda n: bash_msg(n) if n < 15 else done_msg(), max_iters=20)
os.environ.pop("WORKER_OBS_MASK")
ok("kill switch: nothing masked end-to-end", all("old output elided" not in str(x) for x in sent2[-1]))

tl = met.get("turn_log") or []
ok("turn-level log: one row per model turn", len(tl) == 15 and tl[0]["i"] == 1, str(tl[:1]))
ok("turn rows carry parse outcome, finish_reason, tokens, thinking chars",
   tl and tl[0]["parse"] == "ok" and tl[0]["finish_reason"] == "stop" and tl[0]["completion_tokens"] == 10
   and "thinking_chars" in tl[0])
r_ = m.turn_log_record(3, {"done_reason": "length"}, {"content": "plan plan</think>answer"}, "abc", {}, 2.34, False)
ok("inline (Darkbloom) thinking before </think> counts as thinking chars", r_["thinking_chars"] == 3 + len("plan plan"), str(r_))

print("== end-to-end: --extra-iterations ==")
rc, sent, logs, met, _ = drive(lambda n: bash_msg(n, big=False), max_iters=2, extra=0)
base_calls = len(sent)
rc, sent, logs, met, _ = drive(lambda n: bash_msg(n, big=False), max_iters=2, extra=3)
ok("without the flag the run stops at max_iters", base_calls == 2, str(base_calls))
ok("with --extra-iterations 3 the same run gets 3 more turns", len(sent) == 5, str(len(sent)))
ok("it is logged", any("CONTINUATION BUDGET" in s for s in logs))

print("== end-to-end: --mode fixed ==")


MODS = []


def lane_pass(wt):
    mod = types.ModuleType("fixed_lane")
    mod.calls = []
    MODS.append(mod)
    mod.lane_eligible = lambda task, cfg=None, model=None: (True, [])

    def run_lane(task, **kw):
        mod.calls.append(task)
        (Path(task["cwd"]) / "f.txt").write_text("y\n")
        return {"status": "passed", "reason": None, "tokens": 321, "wall_s": 1.5, "best": {"files": {"f.txt": "y\n"}},
                "handoff": None}
    mod.run_lane = run_lane
    return mod


VERIFY = "grep -q '^y$' f.txt"
rc, sent, logs, met, wt = drive(lambda n: done_msg(), mode="fixed", lane=lane_pass, verify=VERIFY)
ok("lane pass: no open-loop model call", len(sent) == 0, f"calls={len(sent)} {logs[-4:]}")
ok("lane pass: normal verify/gate path ran and run converged (rc 0)", rc == 0 and any("VERIFY PASSED" in s for s in logs), f"rc={rc}")
ok("lane pass: lane tokens reach the metrics", met.get("fixed_lane", {}).get("tokens") == 321 and met.get("status") == "converged", str(met.get("fixed_lane")))
_c = MODS[0].calls[0] if MODS and MODS[0].calls else {}
ok("lane saw the declared targets, verify and cwd",
   _c.get("targets") == ["f.txt"] and _c.get("verify") == VERIFY and os.path.realpath(_c.get("cwd", "")) == os.path.realpath(str(wt)), str(_c))


def lane_green(wt):
    mod = lane_pass(wt)
    mod.run_lane = lambda task, **kw: {"status": "already_green", "reason": None, "tokens": 0, "wall_s": 0.1,
                                       "handoff": None, "best": None}
    return mod


rc, sent, logs, met, wt = drive(lambda n: done_msg(), mode="auto", lane=lane_green, verify="true")
ok("already_green: goes through the normal gate (vacuous-pass guard decides), no model call",
   len(sent) == 0 and any("verify" in s.lower() and "VERIFY PASSED" in s for s in logs) and rc in (0, 1), f"rc={rc}")


def lane_fail(reason):
    def make(wt):
        mod = lane_pass(wt)
        mod.run_lane = lambda task, **kw: {
            "status": "exhausted", "reason": reason, "tokens": 900, "wall_s": 3.0, "best": None,
            "handoff": {"reason": reason, "seed_diff": (
                "diff --git a/f.txt b/f.txt\n--- a/f.txt\n+++ b/f.txt\n@@ -1 +1 @@\n-x\n+SEEDED\n"),
                "seed_files": {"f.txt": "SEEDED\n"},
                "last_verify_output": "AssertionError: LASTVERIFYMARK",
                "baseline_verify_output": "base"}}
        return mod
    return make


for reason in ("fixed_lane_exhausted", "fixed_lane_apply_failed", "fixed_lane_transport_error"):
    rc, sent, logs, met, wt = drive(lambda n: bash_msg(n, big=False), mode="fixed", lane=lane_fail(reason),
                                    verify=VERIFY, max_iters=2)
    first_user = sent[0][1]["content"] if sent else ""
    if reason == "fixed_lane_exhausted":
        ok("lane failure: open loop starts, handoff in the FIRST user message",
           "SEEDED" in first_user and "LASTVERIFYMARK" in first_user, first_user[-300:])
        ok("lane failure: seed diff was applied to the worktree", (wt / "f.txt").read_text() == "SEEDED\n")
        ok("handoff carries verifier facts only (no transcript text)", "Traceback" not in first_user and len(first_user) < 12000)
    ok(f"{reason} is the terminal reason when the open loop ends unconverged",
       met.get("terminal_reason") == reason and any(f"TERMINAL REASON: {reason}" in s for s in logs),
       str(met.get("terminal_reason")))

print("== end-to-end: lane skipped / ineligible / default ==")


def lane_inel(wt):
    mod = lane_pass(wt)
    mod.lane_eligible = lambda task, cfg=None, model=None: (False, ["too big"])
    mod.run_lane = lambda *a, **k: (_ for _ in ()).throw(AssertionError("must not run"))
    return mod


rc, sent, logs, met, wt = drive(lambda n: done_msg(), mode="fixed", lane=lane_inel, verify=VERIFY)
ok("ineligible: open loop untouched (first message has no handoff)",
   len(sent) >= 1 and "HARNESS NOTE" not in sent[0][1]["content"] and any("ineligible" in s for s in logs))
rc, sent, logs, met, wt = drive(lambda n: done_msg(), mode="open", lane=lane_pass, verify=VERIFY)
ok("default mode open never touches the lane", len(sent) >= 1 and not any("FIXED LANE" in s for s in logs))
rc, sent, logs, met, wt = drive(lambda n: done_msg(), mode="fixed", lane=lane_pass, verify=VERIFY, api="ollama")
ok("non-OpenAI api: lane skipped", any("FIXED LANE" in s and "skipped" in s for s in logs))
rc, sent, logs, met, wt = drive(lambda n: done_msg(), mode="fixed", lane=lane_pass, verify=None)
ok("no verify: lane skipped", any("FIXED LANE" in s and "no verify" in s for s in logs))

print()
if failures:
    print(f"OBS_MASK_LANE_FAILED: {len(failures)} check(s) failed: {failures}")
    sys.exit(1)
print("OBS_MASK_LANE_OK ALL PASS: masking, extra-iterations, fixed-lane seam and stale-bytecode guard behave")
