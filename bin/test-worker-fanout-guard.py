#!/usr/bin/env python3
"""Per-turn tool-call fan-out cap + past-EOF read counter, ALL task kinds (job c8f4f6ed95c1,
2026-10-09: one turn emitted 582 read_file calls, offset +100 past EOF; the run ended as a generic
nav_loop). Named exit reasons: tool_fanout_loop, read_past_eof_loop. Unit tests on
worker_robust.TurnFanoutGuard + run_task integration (model stubbed, HOME sandboxed)."""
import importlib.util, os, subprocess, sys, tempfile
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
    os.environ["HOME"] = tempfile.mkdtemp(prefix="fanout-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    os.environ.pop("MODEL_SAMPLING_ARM", None)
    sys.path.insert(0, str(WORKER.parent))
    spec = importlib.util.spec_from_file_location("ow_fanout", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    return m


def turn(calls):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def drive(m, script, max_iters=20):
    wt = Path(tempfile.mkdtemp(prefix="fanout-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.py").write_text("".join(f"x{i} = {i}\n" for i in range(200)))
    seq, seen = list(script), []

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        seen.append([dict(x) for x in messages])
        return seq.pop(0) if seq else turn([("task_complete", {"summary": "done"})])

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "edit a.py", None,
                   max_iters, 0.0, 65536, None, max_tokens=8192)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    return "\n".join(logs), seen


def main():
    import worker_robust as W

    print("== TurnFanoutGuard unit ==")
    g = W.TurnFanoutGuard(cap=12)
    check("at the cap is fine", g.on_turn(12), None)
    v = g.on_turn(582)
    check("1st offence warns with the named reason", v and (v[0], v[1]), ("warn", "tool_fanout_loop"))
    check("...and the corrective names the cap", v and "12" in v[2] and "DISCARDED" in v[2], True)
    v = g.on_turn(13)
    check("2nd offence stops", v and (v[0], v[1]), ("stop", "tool_fanout_loop"))
    g = W.TurnFanoutGuard(cap=12)
    r = [g.on_call("read_file", True) for _ in range(6)]
    check("past-EOF streak: warn at 3, stop at 6",
          [(x[0], x[1]) if x else None for x in r],
          [None, None, ("warn", "read_past_eof_loop"), None, None, ("stop", "read_past_eof_loop")])
    g = W.TurnFanoutGuard(cap=12)
    for _ in range(5):
        g.on_call("read_file", True)
    g.on_call("read_file", False)
    check("a good read resets the streak", g.on_call("read_file", True), None)
    check("past-EOF detection matches the tool's refusal",
          W.is_past_eof_result("ERROR: offset 501 is past the end of verify.test.ts -- the file has 403 lines."), True)
    check("...and nothing else", W.is_past_eof_result("[read_file x: lines 1-5 of 90]"), False)
    check("both reasons registered", {"tool_fanout_loop", "read_past_eof_loop"} <= set(W.NEW_EXIT_REASONS), True)
    fl = (HERE / "failure_ledger.py").read_text()
    q = (HERE / "ollama-queue.py").read_text()
    for r_ in ("tool_fanout_loop", "read_past_eof_loop"):
        check(f"{r_} classified by failure_ledger", f'"{r_}"' in fl, True)
        check(f"{r_} in the queue's _FAILURE_CONTEXT_REASONS",
              f'"{r_}"' in q.split("_FAILURE_CONTEXT_REASONS = ")[1].split("})")[0], True)
    import importlib.machinery
    ld = importlib.machinery.SourceFileLoader("da_fanout", str(HERE / "ollama-dispatch-auto"))
    dspec = importlib.util.spec_from_loader("da_fanout", ld)
    da = importlib.util.module_from_spec(dspec)
    ld.exec_module(da)
    if hasattr(da, "worker_exit_route"):    # absent in a tree that predates the ladder routing
        for r_ in ("tool_fanout_loop", "read_past_eof_loop"):
            k, msg = da.worker_exit_route(r_, "j1")
            check(f"dispatch-auto routes {r_} to re-spec (not blind retry)",
                  (k, msg.startswith(da.RESPEC_ROUTE_PREFIX), da.SPEC_DEFECT_ROUTE_PREFIX in msg), ("respec", True, True))
        check("a plain nav_loop is NOT routed as re-spec", da.worker_exit_route("nav_loop", "j1")[0], None)

    print("== sampling ladder hook is inert when the arm is off ==")
    check("ladder off -> on_abort is a no-op", W.SamplingLadder(enabled=False).on_abort("tool_fanout"), ("off", None))

    print("== run_task integration (coding task, no --capture-final-as) ==")
    m = _load()
    # the incident shape: ONE turn, offsets stepping +100 past EOF (a.py has 200 lines)
    big = turn([("read_file", {"path": "a.py", "offset": 1 + 100 * k, "length": 100}) for k in range(60)])
    log, seen = drive(m, [big])
    check("past-EOF mass read ends read_past_eof_loop (not nav_loop)", "TERMINAL REASON: read_past_eof_loop" in log, True)
    check("...in that same iteration", "iteration 1/" in log.split("EARLY ABORT")[1][:30] if "EARLY ABORT" in log else False, True)
    log, seen = drive(m, [turn([("list_files", {"path": f"d{k}"}) for k in range(30)]),
                          turn([("list_files", {"path": f"e{k}"}) for k in range(30)])])
    check("2 over-cap turns end tool_fanout_loop", "TERMINAL REASON: tool_fanout_loop" in log, True)
    check("...excess calls were discarded (only 12 ran in turn 1)", log.count("per-turn tool-call fan-out cap"), 2)
    log, seen = drive(m, [turn([("list_files", {"path": f"d{k}"}) for k in range(30)])])
    check("ONE over-cap turn only warns, then the run continues", "tool_fanout_loop" in log.split("TERMINAL REASON")[-1], False)
    sent = [x for msgs in seen[1:2] for x in msgs]
    check("...the model is told with a corrective message", any("DISCARDED" in str(x.get("content")) for x in sent), True)
    asst = [x for x in sent if x.get("role") == "assistant" and x.get("tool_calls")]
    check("...and the stored assistant turn matches what ran (12 calls)", len(asst[0]["tool_calls"]) if asst else None, 12)
    check("ladder off -> no ladder activity", "SAMPLING LADDER" in log, False)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
