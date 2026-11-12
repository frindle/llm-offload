#!/usr/bin/env python3
"""Prose-loop density abort + wall budget (ollama-worker.py, 2026-10-06).

Live: BFMR TLS refine 77d808c3984a ran >2h, 82+ iterations, 12 output-cap cut-offs
spaced between single tool calls (cut, read, cut, cut, edit, cut ...). The consecutive
streak (OUTPUT_CAP_CUT_MAX) resets on every tool call, so it never fired.
Now:
  * >= PROSE_LOOP_CUTS cut-offs inside PROSE_LOOP_WINDOW iterations stops the run with
    terminal_reason=prose_loop, even when tool calls are interleaved.
  * a run that cuts off only occasionally (sparser than the density) is untouched.
  * an auto author/refine/continue task stops at its wall budget (terminal_reason=
    wall_budget); a plain task is uncapped; WORKER_WALL_BUDGET_S overrides / 0 disables.
  * the queue classifies both as context-class failures, not bare model incapacity.

Drives the REAL run_task with a looping stub model (HOME sandboxed, no GPU/network).
--revert-check mutates the worker (WORKER_SRC) and requires RED for each mutation."""
import importlib.util, os, re, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
QUEUE = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="proseloop-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    os.environ.pop("WORKER_WALL_BUDGET_S", None)
    spec = importlib.util.spec_from_file_location("ow_proseloop", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert str(m.LOG_DIR).startswith(os.environ["HOME"]), m.LOG_DIR
    return m


LOOP_TEXT = "\n".join(["Wait, the abort branch -- let me think about Buffer.concat again.",
                       "Actually the statusText mapping needs another case.",
                       "Hmm, but then the survivor at line 41 ..."] * 300)


def cut():
    return {"message": {"role": "assistant", "content": LOOP_TEXT},
            "done_reason": "length", "usage": {"prompt_tokens": 1000, "completion_tokens": 8192}}


def tc(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


_N = [0]


def READ():
    """A real, distinct unit of work each time (a new file), so no thrash guard fires."""
    _N[0] += 1
    return tc("write_file", {"path": f"f{_N[0]}.txt", "content": f"step {_N[0]}\n"})


def drive(m, script, task="edit a.txt", max_iters=40, clock=None):
    wt = Path(tempfile.mkdtemp(prefix="proseloop-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    sent, seq = [], list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        sent.append(1)
        if clock is not None:
            clock[0] += 900.0          # every model turn "takes" 15 minutes
        return seq.pop(0) if seq else tc("task_complete", {"summary": "done"})

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    real_mono = m.time.monotonic
    if clock is not None:
        base = real_mono()
        m.time.monotonic = lambda: base + clock[0] + (real_mono() - base)
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), task, None,
                   max_iters, 0.0, 65536, None, max_tokens=8192)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
        m.time.monotonic = real_mono
    return sent, "\n".join(logs)


def main():
    m = _load()
    # 77d808c3984a rhythm: a cut, a tool call, a cut ... -- never 3 consecutive cuts.
    rhythm = [x for _ in range(15) for x in (cut(), READ())]
    sent, log = drive(m, rhythm)
    check("interleaved cut/tool rhythm is stopped as a prose loop",
          "TERMINAL REASON: prose_loop" in log, True)
    check("...at the 4th cut (iteration 7), not at the iteration cap", len(sent), 7)
    check("...the log says why ('looping in prose')", "looping in prose" in log, True)
    sparse = [x for _ in range(3) for x in [cut()] + [READ() for _ in range(4)]]
    sent, log = drive(m, sparse)
    check("sparse cut-offs (1 per 5 iterations) are NOT aborted", "EARLY ABORT" in log, False)
    check("...and the run reaches task_complete", len(sent), len(sparse) + 1)

    refine = "# REFINE TASK -- close the gaps the relevance gate found\n\nedit a.txt"
    check("kind: refine", m.harness_task_kind(refine), "refine")
    check("kind: author", m.harness_task_kind("# AUTHORING TASK -- write a ..."), "author")
    check("kind: continue", m.harness_task_kind("# CONTINUE -- your authoring run ..."), "continue")
    check("kind: plain task", m.harness_task_kind("fix the bug in x.py"), None)
    check("refine default budget is 60 min", m.wall_budget_s(refine, env={}), 3600)
    check("plain task uncapped", m.wall_budget_s("fix x", env={}), None)
    check("env override", m.wall_budget_s("fix x", env={"WORKER_WALL_BUDGET_S": "120"}), 120.0)
    check("env 0 disables", m.wall_budget_s(refine, env={"WORKER_WALL_BUDGET_S": "0"}), None)

    clock = [0.0]
    sent, log = drive(m, [READ() for _ in range(30)], task=refine, clock=clock)
    check("a refine whose turns take 15 min each stops on the wall budget",
          "TERMINAL REASON: wall_budget" in log, True)
    check("...after 4 turns (60 min), not 30", len(sent), 4)
    clock = [0.0]
    sent, log = drive(m, [READ() for _ in range(6)], task="edit a.txt", clock=clock)
    check("a plain (non-harness) task with slow turns is NOT capped", "wall_budget" in log, False)

    q = QUEUE.read_text()
    blk = re.search(r"_FAILURE_CONTEXT_REASONS = frozenset\(\{(.*?)\}\)", q, re.S).group(1)
    check("queue classifies prose_loop as a context failure", '"prose_loop"' in blk, True)
    check("queue classifies wall_budget as a context failure", '"wall_budget"' in blk, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("no density check", "            if prose_loop_tripped(_cap_cut_iters, i):", "            if False:"),
    ("cuts not recorded", "            _cap_cut_iters.append(i)\n", ""),
    ("no wall check", "        if _wall_budget and time.monotonic() - _wall_t0 > _wall_budget:",
     "        if False:"),
    ("refine not budgeted", '"refine": 3600, ', ""),
    ("terminal reason not mapped", '                   "prose_loop", "wall_budget"):\n            terminal_reason = _ea',
     '                   ):\n            terminal_reason = _ea'),
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
