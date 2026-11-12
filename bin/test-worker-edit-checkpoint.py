#!/usr/bin/env python3
"""EDIT_CHECKPOINT regression (ollama-worker.py): the model reaches a PASSING patch mid-run (it
runs the task's verify via run_bash, exit 0) and then overwrites it with a broken one; the run
ends at the iteration cap WITHOUT convergence. With EDIT_CHECKPOINT=1 the passing snapshot is kept
beside the transcript, reported in the log + dispatch metrics (exit note
unconverged_with_passing_checkpoint) and is a patch that `git apply`s the GOOD content.
With the flag unset: no checkpoint file, no metric (default behaviour unchanged).
Also: a pass whose required files are missing takes no snapshot; a clean-converging run reports none.
Drives the REAL run_task with the model stubbed (no GPU/network). Run: python3 test-worker-edit-checkpoint.py
"""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

WORKER = Path(os.environ.get("WORKER_SRC") or Path(__file__).resolve().parent / "ollama-worker.py")
FAILS = []


def ok(name, cond, detail=""):
    print(f"  ok   {name}" if cond else f"  FAIL {name} {detail}")
    if not cond:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="eck-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    spec = importlib.util.spec_from_file_location("ollama_worker_eck", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    return m


def tc(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def drive(m, script, max_iters, task="make a.txt contain GOOD"):
    wt = Path(tempfile.mkdtemp(prefix="eck-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("start\n")
    (wt / "verify.sh").write_text("#!/bin/bash\ngrep -q GOOD a.txt\n")
    subprocess.run(["git", "add", "."], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"], cwd=wt, check=True)
    seq = list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        return seq.pop(0) if seq else tc("run_bash", {"command": "true"})

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    m._dispatch_metrics.clear()
    for stale in Path(m.LOG_DIR).glob("*.checkpoint.patch"):   # transcript names are per-second
        stale.unlink()
    logs = []
    orig = m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    rc = None
    try:
        rc = m.run_task("stub-model", "http://127.0.0.1:9", str(wt), task, "bash verify.sh",
                        max_iters, 0.0, 65536, None, max_tokens=8192)
    except SystemExit as e:
        rc = e.code
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    return wt, logs, dict(m._dispatch_metrics), rc


def overwrite_script():
    return [tc("write_file", {"path": "a.txt", "content": "GOOD\n"}),
            tc("run_bash", {"command": "bash verify.sh"}),          # passes -> checkpoint
            tc("write_file", {"path": "a.txt", "content": "BROKEN\n"}),
            tc("run_bash", {"command": "echo still going"}),
            tc("run_bash", {"command": "echo still going 2"})]


def main():
    m = _load()
    print("== EDIT_CHECKPOINT=1: good patch reached, then destroyed, cap hit ==")
    os.environ["EDIT_CHECKPOINT"] = "1"
    wt, logs, met, rc = drive(m, overwrite_script(), max_iters=5)
    ck = met.get("edit_checkpoint") or {}
    ok("run did not converge", met.get("status") != "converged", met.get("status"))
    ok("final tree is the destroyed one", (wt / "a.txt").read_text() == "BROKEN\n")
    ok("checkpoint metric present with distinct exit note", ck.get("exit") == "unconverged_with_passing_checkpoint", ck)
    p = Path(ck.get("path") or "/nonexistent")
    ok("checkpoint patch file exists beside the transcript", p.is_file(), str(p))
    if p.is_file():
        ok("checkpoint patch holds the GOOD content", "+GOOD" in p.read_text() and "BROKEN" not in p.read_text())
        r = subprocess.run(["git", "checkout", "--", "a.txt"], cwd=wt, capture_output=True)
        r = subprocess.run(["git", "apply", str(p)], cwd=wt, capture_output=True, text=True)
        ok("git apply of the checkpoint restores GOOD", r.returncode == 0 and (wt / "a.txt").read_text() == "GOOD\n", r.stderr)
        v = subprocess.run(["bash", "verify.sh"], cwd=wt)
        ok("restored tree passes the verify", v.returncode == 0)
    ok("log names the checkpoint + exit note", any("unconverged_with_passing_checkpoint" in l for l in logs))

    print("== EDIT_CHECKPOINT unset: default behaviour unchanged ==")
    os.environ.pop("EDIT_CHECKPOINT")
    wt2, logs2, met2, _ = drive(m, overwrite_script(), max_iters=5)
    ok("no edit_checkpoint metric", "edit_checkpoint" not in met2, met2.get("edit_checkpoint"))
    tp = met2.get("transcript_path")
    ok("no .checkpoint.patch written", bool(tp) and not Path(tp).with_suffix(".checkpoint.patch").exists(), tp)
    ok("no EDIT-CHECKPOINT log line", not any("EDIT-CHECKPOINT" in l for l in logs2))

    print("== EDIT_CHECKPOINT=1: required file missing -> no snapshot ==")
    os.environ["EDIT_CHECKPOINT"] = "1"
    task = "make a.txt contain GOOD\n\n## Required files\n- `needed.txt`\n"
    _, logs3, met3, _ = drive(m, overwrite_script(), max_iters=5, task=task)
    ok("no snapshot when required file absent", "edit_checkpoint" not in met3, met3.get("edit_checkpoint"))
    ok("log says criteria unmet", any("required criteria are unmet" in l for l in logs3))

    print("== EDIT_CHECKPOINT=1: clean convergence reports nothing ==")
    _, logs4, met4, _ = drive(m, [tc("write_file", {"path": "a.txt", "content": "GOOD\n"}),
                                  tc("run_bash", {"command": "bash verify.sh"}),
                                  tc("task_complete", {"summary": "done"})], max_iters=6)
    ok("converged", met4.get("status") == "converged", met4.get("status"))
    ok("no edit_checkpoint metric on a converged run", "edit_checkpoint" not in met4)
    os.environ.pop("EDIT_CHECKPOINT", None)

    print("== MODEL_PROFILE_LOG: ollama-style default off; openai-style on once per distinct request; =0 off; =1 always ==")
    logs5 = []
    orig = m.log
    m.log = lambda s_, *a, **k: logs5.append(str(s_))
    _seen = set(m._PROFILE_LOG_SEEN)
    m._PROFILE_LOG_SEEN.clear()
    os.environ.pop("MODEL_PROFILE_LOG", None)
    try:
        m._profile_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "author", "ollama")
        ok("no request line for ollama-style when MODEL_PROFILE_LOG unset", not any("request profile=" in l for l in logs5))
        f0 = m._profile_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "author", "openai")
        n1 = sum("request profile=" in l for l in logs5)
        ok("openai-style logs one line by default", n1 == 1, str(logs5))
        m._profile_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "author", "openai")
        ok("identical openai request is deduped", sum("request profile=" in l for l in logs5) == n1)
        os.environ["MODEL_PROFILE_LOG"] = "0"
        m._PROFILE_LOG_SEEN.clear()
        m._profile_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "reviewer", "openai")
        ok("MODEL_PROFILE_LOG=0 silences openai-style", sum("request profile=" in l for l in logs5) == n1)
        os.environ["MODEL_PROFILE_LOG"] = "1"
        f = m._profile_fields("qwen3.6-35b-a3b-vl-mtp-mxfp8", "review", "openai")
        line = [l for l in logs5 if "request profile=" in l][-1]
        ok("request line names profile + mode + role", "profile=qwen3.6-35b-a3b-vl-mtp-mxfp8:review role=review" in line, line)
        ok("request line carries the effective params actually returned",
           json.dumps(f, sort_keys=True, default=str) in line and '"top_k": 20' in line, line)
    finally:
        os.environ.pop("MODEL_PROFILE_LOG", None)
        m._PROFILE_LOG_SEEN.clear(); m._PROFILE_LOG_SEEN.update(_seen)
        m.log = orig

    print()
    if FAILS:
        print("FAILED:", FAILS)
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
