#!/usr/bin/env python3
"""The output-cap prose loop is BOUNDED (ollama-worker.py, 2026-10-02).

Live: Rivian s4 author 368bc923a303 produced EIGHT consecutive turns of exactly 8192
tokens of the same prose with no tool call (iters 11-18, ~25 min). Each was answered
by output_cap_cut_nudge and the loop continued, adding ~8k ctx per turn until the
context pause fired -- and the queue resumed it at a bigger window to loop again.
Now: after OUTPUT_CAP_CUT_MAX nudges the next consecutive cut stops the run with
early_abort/terminal_reason=output_cap_loop. A turn that DOES make a tool call resets
the streak, so isolated cuts between real work are still nudged, not aborted.

Drives the REAL run_task with the model stubbed (HOME sandboxed, no GPU/network).
--revert-check mutates the worker (WORKER_SRC env) and requires RED."""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="caploop-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    spec = importlib.util.spec_from_file_location("ow_caploop", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert str(m.LOG_DIR).startswith(os.environ["HOME"]), m.LOG_DIR
    return m


LOOP_TEXT = "\n".join(["Let me re-read the INTENT one more time:", "I think this means:",
                       "But that doesn't make sense.", "Unless... a different number."] * 300)


def cut():
    return {"message": {"role": "assistant", "content": LOOP_TEXT},
            "done_reason": "length", "usage": {"prompt_tokens": 1000, "completion_tokens": 8192}}


def tc(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def drive(m, script, max_iters=12):
    wt = Path(tempfile.mkdtemp(prefix="caploop-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    sent, seq = [], list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        prior = [mm.get("content") or "" for mm in messages if mm.get("role") == "assistant"]
        sent.append({"temperature": temperature, "repeat_penalty": kw.get("repeat_penalty"),
                     "max_assistant_chars": max([len(c) for c in prior] or [0]),
                     "prior_assistant": prior})
        return seq.pop(0) if seq else tc("task_complete", {"summary": "done"})

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "edit a.txt", None,
                   max_iters, 0.0, 65536, None, max_tokens=8192)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    return sent, "\n".join(logs)


def main():
    m = _load()
    sent, log = drive(m, [cut()] * 10)
    check("a run of consecutive cuts stops after MAX nudges + 1 (not at the ctx wall)",
          len(sent), m.OUTPUT_CAP_CUT_MAX + 1)
    check("the stop is reported as output_cap_loop", "TERMINAL REASON: output_cap_loop" in log, True)
    # 3 cuts (not 4): 4 cuts in 10 iterations is the PROSE_LOOP density abort
    # (test-worker-prose-loop-budget.py); this case isolates the streak reset.
    sent, log = drive(m, [cut(), cut(), tc("read_file", {"path": "a.txt"}), cut()])
    check("a tool call resets the streak -> no early abort", "EARLY ABORT" in log, False)
    check("...and the run went on to the model's task_complete", len(sent) >= 5, True)

    # RECOVERY (2026-10-04, output_cap_loop on Darkbloom): the cut turn is compacted
    # before it is re-read, and only the next turn samples off the near-greedy loop.
    sent, log = drive(m, [cut(), tc("read_file", {"path": "a.txt"})])
    check("first call is the normal sampler (temp 0.0, default penalty)",
          (sent[0]["temperature"], sent[0]["repeat_penalty"]), (0.0, m.DEFAULT_REPEAT_PENALTY))
    check("the cut turn is compacted in the context the next call re-reads",
          sent[1]["max_assistant_chars"] <= m.OUTPUT_CAP_CUT_KEEP_CHARS + 200, True)
    check("...each distinct line kept once, the drop labelled",
          sent[1]["prior_assistant"][0].count("I think this means:") == 1
          and "was dropped" in sent[1]["prior_assistant"][0], True)
    check("the call after a cut runs hotter (>= REASONING_LOOP_RETRY_TEMPERATURE)",
          sent[1]["temperature"] >= m.REASONING_LOOP_RETRY_TEMPERATURE, True)
    check("...with the stronger repetition penalty",
          sent[1]["repeat_penalty"], m.OUTPUT_CAP_RECOVERY_REPEAT_PENALTY)
    check("...for that one turn only (next call back to normal)",
          (sent[2]["temperature"], sent[2]["repeat_penalty"]), (0.0, m.DEFAULT_REPEAT_PENALTY))
    short, dropped = m.compact_cut_off_prose("short answer\nline two")
    check("compaction leaves a short, non-repeating turn untouched", (short, dropped),
          ("short answer\nline two", 0))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("no bound (old unbounded continue)", "            if _cap_cut_streak > OUTPUT_CAP_CUT_MAX:",
     "            if False:"),
    ("streak never resets", "        if tool_calls:\n            _cap_cut_streak = 0\n",
     "        if tool_calls:\n            pass\n"),
    ("cut turn not compacted", '                msg["content"] = _compact\n', '                pass\n'),
    ("no recovery turn after a cut", "            _cap_cut_recover = True\n            continue",
     "            continue"),
    ("recovery turn keeps the default penalty",
     "            _turn_repeat_penalty = max(repeat_penalty or 1.0, OUTPUT_CAP_RECOVERY_REPEAT_PENALTY)\n",
     ""),
    ("recovery never switched off", "            _cap_cut_recover = False\n            log(", "            log("),
    ("terminal reason not mapped",
     '        if _ea in ("write_thrash", "read_thrash", "reasoning_freeze", "output_cap_loop",\n',
     '        if _ea in ("write_thrash", "read_thrash", "reasoning_freeze",\n'),
]


def revert_check():
    bad = 0
    src = WORKER.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-worker.py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "WORKER_SRC": f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
