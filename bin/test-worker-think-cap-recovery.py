#!/usr/bin/env python3
"""Revert-test for the output-cap-spent-on-thinking recovery (ollama-worker.py).

Live 2026-09-27: 8/876 iterations (e.g. cb21d014c3cd it5) generated exactly 8192
tokens -- all `thinking` -- and emitted no content and no tool call. The worker
stripped that reasoning, sent the generic one-shot "your response was empty" nudge,
and the model re-derived ~10 min of work from scratch; a second truncation would have
been accepted as a blank final answer.

Unit checks cover the pure helpers. The end-to-end check drives the REAL run_task
with call_ollama stubbed (no GPU, no network): turn 1 = 8192 thinking-only tokens with
done_reason=length, turn 2 = task_complete. It asserts on what the worker SENDS on
turn 2 (the reasoning tail is handed back, the budget is raised but stays under the
context gate) and that the empty-answer nudge was NOT spent.

Red-on-revert: make think_cap_truncated return False and the e2e checks go red.

Run: python3 test-worker-think-cap-recovery.py
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

WORKER = Path(__file__).resolve().parent / "ollama-worker.py"
failures = []


def ok(name, cond, detail=""):
    print(f"  ok   {name}" if cond else f"  FAIL {name} {detail}")
    if not cond:
        failures.append(name)


def _load():
    # SANDBOX: run_task writes a transcript + dispatch-metrics under Path.home()/bin and
    # appends to the vault dispatch log. Point HOME at a temp dir BEFORE import (the
    # module computes its paths at import) and drop the vault token, so this test can
    # never touch the real ledgers (it did once, 2026-09-27, before this guard).
    os.environ["HOME"] = tempfile.mkdtemp(prefix="thinkcap-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    spec = importlib.util.spec_from_file_location("ollama_worker_thinkcap", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    m = _load()
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert str(m.LOG_DIR).startswith(os.environ["HOME"]), m.LOG_DIR
    CAP = m.DEFAULT_MAX_TOKENS
    print("== think_cap_truncated ==")
    ok("empty + done_reason=length -> truncated",
       m.think_cap_truncated({"content": ""}, {"completion_tokens": 10}, "length", CAP))
    ok("empty + completion == cap (no done_reason) -> truncated",
       m.think_cap_truncated({"content": ""}, {"completion_tokens": CAP}, None, CAP))
    ok("empty + stopped by choice (done_reason=stop, under cap) -> NOT truncated",
       not m.think_cap_truncated({"content": ""}, {"completion_tokens": 900}, "stop", CAP))
    ok("a tool call at the cap is not an empty turn",
       not m.think_cap_truncated({"content": "", "tool_calls": [{}]},
                                 {"completion_tokens": CAP}, "length", CAP))
    ok("content at the cap is not an empty turn",
       not m.think_cap_truncated({"content": "x"}, {"completion_tokens": CAP}, "length", CAP))

    print("== think_cap_recovery_budget respects the context gate ==")
    R = m.CONTEXT_REVIEW_THRESHOLD
    ok("plenty of room -> factor x cap",
       m.think_cap_recovery_budget(CAP, 20000, 131072) == CAP * m.THINK_CAP_BUDGET_FACTOR)
    b = m.think_cap_recovery_budget(CAP, 45000, 65536)
    ok("tight room -> capped so prompt + budget stays under the review threshold",
       CAP < b < CAP * m.THINK_CAP_BUDGET_FACTOR and 45000 + b + m.THINK_CAP_CTX_MARGIN <= int(65536 * R), str(b))
    ok("no room -> never below the normal cap",
       m.think_cap_recovery_budget(CAP, 60000, 65536) == CAP)
    ok("unknown num_ctx -> normal cap", m.think_cap_recovery_budget(CAP, 0, 0) == CAP)

    print("== think_cap_nudge hands the reasoning back ==")
    th = "A" * 5000 + "CONCLUSION: mutate L59 with a URL object"
    n = m.think_cap_nudge(th, CAP)
    ok("tail of the lost reasoning is included", "CONCLUSION: mutate L59" in n)
    ok("bounded to THINK_CAP_TAIL_CHARS", len(n) < m.THINK_CAP_TAIL_CHARS + 1000)
    ok("tells the model to act, not re-derive", "Do NOT re-derive" in n)

    print("== end-to-end: real run_task, stubbed model ==")
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
        (wt / "f.txt").write_text("x\n")
        sent = []
        thinking = "step " * 3000 + "FINAL-PLAN: write the fixture for L59"

        def fake_call(host, model, messages, temperature, num_ctx, **kw):
            sent.append({"messages": json.loads(json.dumps(messages)),
                         "max_tokens": kw.get("max_tokens")})
            if len(sent) == 1:
                return {"message": {"role": "assistant", "content": "", "thinking": thinking},
                        "done_reason": "length",
                        "usage": {"prompt_tokens": 9000, "completion_tokens": CAP,
                                  "total_tokens": 9000 + CAP}}
            return {"message": {"role": "assistant", "content": "",
                                "tool_calls": [{"function": {"name": "task_complete",
                                                             "arguments": {"summary": "done"}}}]},
                    "done_reason": "stop",
                    "usage": {"prompt_tokens": 10000, "completion_tokens": 50,
                              "total_tokens": 10050}}

        m.call_ollama = fake_call
        m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
        m.ensure_model_ready = lambda *a, **k: None
        m.set_keep_alive = lambda *a, **k: None
        m.clamp_unraid_ctx = lambda host, model, n: n
        logs = []
        _orig_log = m.log
        m.log = lambda s, *a, **k: logs.append(str(s))
        try:
            m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "do the thing", None,
                       6, 0.0, 65536, None, max_tokens=CAP)
        except SystemExit:
            pass
        except Exception as e:  # surface, but still assert on what was sent
            logs.append(f"run_task raised: {e!r}")
        finally:
            m.log = _orig_log
        ok("the model was called twice (truncated turn, then the recovery turn)",
           len(sent) >= 2, f"calls={len(sent)} tail={logs[-5:]}")
        if len(sent) >= 2:
            last_user = [x for x in sent[1]["messages"] if x.get("role") == "user"][-1]["content"]
            ok("recovery turn carries the lost reasoning's conclusion",
               "FINAL-PLAN: write the fixture for L59" in last_user, last_user[:200])
            ok("recovery turn is NOT the generic empty-answer nudge",
               "Your last response was empty" not in last_user)
            ok("recovery turn gets a larger num_predict than the cap",
               (sent[1]["max_tokens"] or 0) > CAP, str(sent[1]["max_tokens"]))
            ok("...but stays under the context review threshold",
               9000 + (sent[1]["max_tokens"] or 0) <= int(65536 * m.CONTEXT_REVIEW_THRESHOLD))
            ok("the stored assistant turn still has its thinking stripped (no context inflation)",
               all("thinking" not in x for x in sent[1]["messages"] if x.get("role") == "assistant"))
        ok("the truncation is logged as a budget event",
           any("spent the whole" in s and "THINKING" in s for s in logs))
        ok("the empty-answer nudge was never used",
           not any("no tool calls AND no content" in s for s in logs))

    print()
    if failures:
        print(f"THINK_CAP_RECOVERY_FAILED: {len(failures)} check(s) failed")
        sys.exit(1)
    print("THINK_CAP_RECOVERY_OK: a turn truncated in thinking is recovered, not treated as a blank answer")


if __name__ == "__main__":
    main()
