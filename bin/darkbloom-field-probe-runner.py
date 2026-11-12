#!/usr/bin/env python3
"""darkbloom-field-probe-runner.py -- queue runner for darkbloom-field-probe.py.

Launched only by ~/bin/ollama-queue.py (it is in ALLOWED_RUNNERS). The queue passes ONLY
--model --host --num-ctx --cwd --task-file; --model/--host/--num-ctx are lane placeholders
here (the probe reads the live Darkbloom endpoint + key from ~/.darkbloom/local.json
itself). The whole probe run is ONE queue job so it is visible in queue state and sits
under the GPU/model-residency guards instead of being run ad hoc.

Spec (task file, JSON, every key optional):
  {"probes": "1,2,3,4,5", "budget": 150, "models": "a,b", "n_tools": 10, "n_pairs": 10}

Writes <cwd>/darkbloom-probe-results.json and prints the markdown report to stdout (the
queue's live log). Exit 0 when the probe run and the report both succeeded, else 1. It
never exits 3/5, which the queue reads as the worker's paused/unconverged codes.

Self-test: darkbloom-field-probe-runner.py --self-test (asserts argv construction, no network).
"""
import argparse
import json
import subprocess
import sys
from pathlib import Path

PROBE = Path(__file__).resolve().parent / "darkbloom-field-probe.py"
RESULTS = "darkbloom-probe-results.json"
RC_OK, RC_FAIL = 0, 1


def build_run_argv(spec, out_path, probe=PROBE):
    argv = [sys.executable, str(probe), "run", "--out", str(out_path)]
    for key, flag in (("probes", "--probes"), ("budget", "--budget"), ("models", "--models"),
                      ("n_tools", "--n-tools"), ("n_tools_9b", "--n-tools-9b"),
                      ("n_pairs", "--n-pairs")):
        if spec.get(key) is not None:
            argv += [flag, str(spec[key])]
    return argv


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--model")
    ap.add_argument("--host")
    ap.add_argument("--num-ctx", default=None)
    ap.add_argument("--cwd")
    ap.add_argument("--task-file")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    if not (a.cwd and a.task_file):
        ap.error("--cwd and --task-file are required")
    try:
        spec = json.loads(Path(a.task_file).read_text())
    except (OSError, ValueError) as e:
        print(f"[probe-runner] cannot read task file {a.task_file}: {e}", flush=True)
        return RC_FAIL
    out = Path(a.cwd) / RESULTS
    run = subprocess.run(build_run_argv(spec, out), cwd=a.cwd)
    if run.returncode != 0 or not out.is_file():
        print(f"[probe-runner] probe run failed (exit {run.returncode}, results "
              f"{'present' if out.is_file() else 'missing'})", flush=True)
        return RC_FAIL
    rep = subprocess.run([sys.executable, str(PROBE), "report", str(out)], cwd=a.cwd)
    return RC_OK if rep.returncode == 0 else RC_FAIL


def self_test():
    fails = []

    def ok(name, cond):
        print(("  ok   " if cond else "  FAIL ") + name)
        if not cond:
            fails.append(name)

    argv = build_run_argv({"probes": "1,6", "budget": 99, "models": "x,y"}, "/tmp/o.json", probe="/p.py")
    ok("run subcommand + --out", argv[2] == "run" and argv[3:5] == ["--out", "/tmp/o.json"])
    ok("probes/budget/models forwarded", "--probes" in argv and argv[argv.index("--probes") + 1] == "1,6"
       and argv[argv.index("--budget") + 1] == "99" and argv[argv.index("--models") + 1] == "x,y")
    ok("absent keys add no flags", "--n-tools" not in argv and "--n-pairs" not in argv)
    ok("probe script exists", PROBE.is_file())
    rc = main(["--model", "m", "--host", "h", "--num-ctx", "1", "--cwd", "/nonexistent",
               "--task-file", "/nonexistent/task.json"])
    ok("the five queue flags parse; unreadable task file fails closed (1, not 3/5)", rc == RC_FAIL)
    if fails:
        print(f"\n{len(fails)} FAILED: {fails}")
        return 1
    print("\nPROBE_RUNNER_OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
