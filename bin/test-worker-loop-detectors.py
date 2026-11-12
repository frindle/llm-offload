#!/usr/bin/env python3
"""LoopDetector additions (2026-10-09, pipeline-review steal list item 3; OpenHands stuck-detector +
SWE-agent navigation limits), each ending the run with a NAMED exit reason:

  error_loop        same action + same error: ONE nudge at the 3rd, stop on the 4th
  monologue_loop    3 consecutive assistant turns with no tool call
  alternation_loop  A,B,A,B,... (warn at 6 calls, stop at 8)
  nav_loop          grep/find/ls/view/read_file as ONE class: warn at a streak of 15, stop 8 later

Unit tests on worker_robust.LoopDetector + run_task integration (model stubbed, HOME sandboxed).
--revert-check mutates the code and requires RED."""
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
    os.environ["HOME"] = tempfile.mkdtemp(prefix="loopdet-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    sys.path.insert(0, str(WORKER.parent))
    spec = importlib.util.spec_from_file_location("ow_loopdet", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    return m


def tc(name, args, content=""):
    return {"message": {"role": "assistant", "content": content,
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def text(s):
    return {"message": {"role": "assistant", "content": s}, "done_reason": "stop",
            "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def drive(m, script, max_iters=40, task="edit a.py"):
    wt = Path(tempfile.mkdtemp(prefix="loopdet-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.py").write_text("".join(f"x{i} = {i}\n" for i in range(200)))
    seq = list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        return seq.pop(0) if seq else tc("task_complete", {"summary": "done"})

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
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
    return "\n".join(logs)


def run(W, seq, cfg=None):
    """seq: list of iterations; each a list of (name, args, error|None). -> [(iter, action, kind)]"""
    d = W.LoopDetector(cfg)
    out = []
    for it, row in enumerate(seq, 1):
        for n, a, e in row:
            d.observe(n, a, error=e)
        r = d.end_iteration(it)
        if r:
            out.append((it, r[0], r[1]))
            if r[0] == "stop":
                break
    return out


def main():
    import worker_robust as W
    NOW = {"repeat_write": 99}    # isolate from the older identical-write detector
    ERR = "ERROR: old_string not found in a.py"
    call = ("edit_file", {"path": "a.py", "old_string": "q", "new_string": "r"}, ERR)

    print("== same action + same error ==")
    check("nudge at the 3rd, stop on the 4th", run(W, [[call]] * 6), [(3, "warn", "error_streak"), (4, "stop", "error_streak")])
    other = ("edit_file", {"path": "a.py", "old_string": "z", "new_string": "r"}, ERR)
    check("different args breaks the streak", run(W, [[call], [call], [other], [call], [call], [other]], NOW), [])
    okc = ("edit_file", {"path": "a.py", "old_string": "q", "new_string": "r"}, None)
    check("a success in between breaks the streak", run(W, [[call], [call], [okc], [call], [call]], NOW), [])
    diff_err = ("edit_file", {"path": "a.py", "old_string": "q", "new_string": "r"}, "ERROR: something else")
    check("same args but a DIFFERENT error is not the streak", run(W, [[call], [call], [diff_err], [call]], NOW), [])
    refused = ("edit_file", {"path": "a.py", "old_string": "q", "new_string": "r"}, "REFUSED: you have already called edit_file")
    check("the worker's own REFUSED answer continues the streak",
          run(W, [[call], [call], [call], [refused]]), [(3, "warn", "error_streak"), (4, "stop", "error_streak")])
    check("error numbers are normalised (line 12 vs line 13 = same error)",
          W.normalise_error("ERROR at line 12"), W.normalise_error("ERROR at line 13"))

    print("== monologue ==")
    d = W.LoopDetector()
    for _ in range(2):
        d.observe_turn(False)
    check("2 talking turns: fine", d.monologue_verdict(), None)
    d.observe_turn(False)
    v = d.monologue_verdict()
    check("3rd consecutive no-tool turn: stop", v and (v[0], v[1]), ("stop", "monologue"))
    d = W.LoopDetector()
    d.observe_turn(False); d.observe_turn(False); d.observe_turn(True); d.observe_turn(False)
    check("a tool turn resets", d.monologue_verdict(), None)

    print("== A,B,A,B alternation ==")
    A = ("read_file", {"path": "a.py", "offset": 1, "length": 5}, None)
    B = ("run_bash", {"command": "ls"}, None)
    got = run(W, [[A], [B]] * 6, {"repeat_read": 99, "nav_warn": 0, "no_progress_iters": 99})
    check("warn at 6 alternating calls, stop at 8", got, [(6, "warn", "alternation"), (8, "stop", "alternation")])
    C = ("read_file", {"path": "c.py", "offset": 1, "length": 5}, None)
    check("A,B,C,A,B,C is not alternation", run(W, [[A], [B], [C]] * 4, {"repeat_read": 99, "nav_warn": 0, "no_progress_iters": 99}), [])

    print("== navigation class ==")
    navs = [("read_file", {"path": "a.py", "offset": k, "length": 5}, None) if k % 4 == 0 else
            ("run_bash", {"command": f"grep -rn thing{k} ."}, None) if k % 4 == 1 else
            ("run_bash", {"command": f"find . -name 'f{k}*'"}, None) if k % 4 == 2 else
            ("list_files", {"path": f"d{k}"}, None) for k in range(40)]
    got = run(W, [[n] for n in navs], {"no_progress_iters": 99})
    check("mixed read_file/grep/find/ls: warn at 15, stop 8 later", got, [(15, "warn", "nav_streak"), (23, "stop", "nav_streak")])
    check("grep/find/ls/cat are navigation", all(W.is_navigation_call("run_bash", {"command": c}) for c in
          ["grep -rn x .", "ls -la", "find . -name y | head", "cat a.py", "cd src && ls"]), True)
    check("sed -i / redirects / python are not", any(W.is_navigation_call("run_bash", {"command": c}) for c in
          ["sed -i s/a/b/ f", "echo x > f", "python3 t.py", "cat <<EOF > f"]), False)
    edit = ("edit_file", {"path": "a.py", "old_string": "1", "new_string": "2"}, None)
    seq = [[n] for n in navs[:14]] + [[edit]] + [[n] for n in navs[14:28]]
    check("an edit resets the streak", run(W, seq, {"no_progress_iters": 99}), [])

    print("== READONLY_JOB disables only the nav detector ==")
    check("loop_config default keeps nav_warn", W.loop_config({}, env={})["nav_warn"], 15)
    check("loop_config READONLY_JOB=1 -> nav_warn 0", W.loop_config({}, env={"READONLY_JOB": "1"})["nav_warn"], 0)
    check("READONLY_JOB=1 leaves other detectors", W.loop_config({}, env={"READONLY_JOB": "1"})["repeat_read"], W.LOOP_DEFAULTS["repeat_read"])
    ro_cfg = W.loop_config({}, env={"READONLY_JOB": "1"})
    ro_cfg["no_progress_iters"] = 99
    check("40 navs, readonly cfg: no detection", run(W, [[n] for n in navs], ro_cfg), [])
    same = ("read_file", {"path": "a.py", "offset": 1, "length": 5}, None)
    check("readonly cfg still stops identical repeated reads", [r[2] for r in run(W, [[same]] * 8, W.loop_config({}, env={"READONLY_JOB": "1"}))][-1:], ["repeat_read"])

    print("== named exit reasons registered everywhere ==")
    for kind, reason in [("error_streak", "error_loop"), ("monologue", "monologue_loop"),
                         ("alternation", "alternation_loop"), ("nav_streak", "nav_loop")]:
        check(f"{kind} -> {reason}", W.loop_exit_reason(kind), reason)
        check(f"{reason} in worker NEW_EXIT_REASONS", reason in W.NEW_EXIT_REASONS, True)
    check("legacy kinds still end as loop_detected", W.loop_exit_reason("repeat_read"), "loop_detected")
    q = (HERE / "ollama-queue.py").read_text()
    fl = (HERE / "failure_ledger.py").read_text()
    for reason in ("error_loop", "monologue_loop", "alternation_loop", "nav_loop"):
        check(f"{reason} in the queue's _FAILURE_CONTEXT_REASONS", f'"{reason}"' in q.split("_FAILURE_CONTEXT_REASONS = ")[1].split("})")[0], True)
        check(f"{reason} classified by failure_ledger", f'"{reason}"' in fl, True)

    print("== run_task integration ==")
    m = _load()
    log = drive(m, [tc("read_file", {"path": "a.py", "offset": 1 + k, "length": 2}) for k in range(40)], max_iters=40)
    check("navigation without an edit ends nav_loop", "TERMINAL REASON: nav_loop" in log, True)
    check("...after exactly one warning", log.count("loop-detector WARN (nav_streak)"), 1)
    log = drive(m, [tc("edit_file", {"path": "a.py", "old_string": "NOT_THERE", "new_string": "y"})] * 8, max_iters=12)
    check("the same failing edit 4x ends error_loop (not generic loop_detected)",
          "TERMINAL REASON: error_loop" in log, True)
    nav40 = [tc("read_file", {"path": "a.py", "offset": 1 + k, "length": 2}) for k in range(40)]
    log = drive(m, nav40, max_iters=40, task="READ-ONLY\nWorker-flags: READONLY_JOB=1\ninvestigate a.py")
    os.environ.pop("READONLY_JOB", None)
    check("readonly marker in task text: no nav_loop abort", "nav_loop" in log or "nav_streak" in log, False)
    log = drive(m, nav40, max_iters=40)
    check("same script WITHOUT the marker still ends nav_loop", "TERMINAL REASON: nav_loop" in log, True)
    os.environ["WORKER_LOOP_MONOLOGUE"] = "2"
    try:
        log = drive(m, [tc("read_file", {"path": "a.py", "offset": 1, "length": 2}),
                        text("I will now think about this."), text("")], max_iters=12)
    finally:
        del os.environ["WORKER_LOOP_MONOLOGUE"]
    check("consecutive no-tool turns the run kept nudging past end monologue_loop",
          "TERMINAL REASON: monologue_loop" in log, True)
    log = drive(m, [tc("read_file", {"path": "a.py", "offset": 1, "length": 2}),
                    text("All done, nothing to change.")], max_iters=12)
    check("an ordinary final answer is not a monologue", "monologue_loop" in log, False)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("error streak never stops", 'if self.err_sig and ss and self.err_streak >= ss:', 'if False:', "worker_robust.py"),
    ("error streak never nudges", 'and "error_streak" not in self.kind_warned:', 'and False:', "worker_robust.py"),
    ("REFUSED does not continue the streak", 'refused and self.err_sig is not None and self.err_sig[0] == sig', 'False', "worker_robust.py"),
    ("monologue never fires", 'if self.cfg.get("enabled") and n and self.no_tool_streak >= n:', 'if False:', "worker_robust.py"),
    ("tool turn does not reset monologue", 'self.no_tool_streak = 0 if has_tool_call else self.no_tool_streak + 1', 'self.no_tool_streak = self.no_tool_streak + 1', "worker_robust.py"),
    ("alternation never fires", 'if na and self._alternating(na) and "alternation" not in self.kind_warned:', 'if False:', "worker_robust.py"),
    ("alternation never stops", 'if ns and self._alternating(ns):', 'if False:', "worker_robust.py"),
    ("navigation not one class (bash ignored)", 'if name == "run_bash":\n        cmd = str((args or {}).get("command") or "")', 'if False:\n        cmd = str((args or {}).get("command") or "")', "worker_robust.py"),
    ("READONLY_JOB does not disable nav", '    if env.get("READONLY_JOB") == "1":', '    if False:', "worker_robust.py"),
    ("READONLY_JOB not an allowed task flag", '"PROSE_STICKY_NOTHINK", "READONLY_JOB")', '"PROSE_STICKY_NOTHINK")', "worker_levers.py"),
    ("nav never stops", 'if nst and self.nav_streak >= self.nav_warn_at + nst:', 'if False:', "worker_robust.py"),
    ("edit does not reset nav streak", '            self.nav_streak = 0\n            self.nav_warn_at = None', '            pass', "worker_robust.py"),
    ("kind -> generic loop_detected", "                _ld_reason = _wr.loop_exit_reason(_ld_kind)", "                _ld_reason = _wr.REASON_LOOP"),
    ("monologue not checked in run_task", "            _mono = _loop_det.monologue_verdict()", "            _mono = None"),
    ("observe_turn not fed", "            _loop_det.observe_turn(bool(tool_calls))", "            pass"),
    ("tool error not fed to detector", "error=(str(result) if failed else None))", "error=None)"),
    ("queue does not know error_loop", '"error_loop"', '"x_error_loop"', "ollama-queue.py"),
]


def revert_check():
    bad = 0
    for mut in MUTATIONS:
        name, old, new = mut[:3]
        fname = mut[3] if len(mut) > 3 else "ollama-worker.py"
        src = (HERE / fname).read_text()
        n = src.count(old)
        assert n >= 1, f"anchor missing: {name}"
        tmpd = Path(tempfile.mkdtemp(prefix="loopdet-mut-"))
        for dep in ("worker_robust.py", "worker_levers.py", "model_profile.py", "ollama-worker.py", "ollama-queue.py", "failure_ledger.py",
                    "model_profiles.yaml"):
            if (HERE / dep).exists():
                (tmpd / dep).write_text((HERE / dep).read_text())
        (tmpd / fname).write_text(src.replace(old, new, 1) if fname != "ollama-queue.py" else src.replace(old, new))
        test = tmpd / Path(__file__).name
        test.write_text(Path(__file__).read_text())
        r = subprocess.run([sys.executable, str(test)], env={**os.environ, "WORKER_SRC": str(tmpd / "ollama-worker.py")},
                           capture_output=True, text=True, timeout=900)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
