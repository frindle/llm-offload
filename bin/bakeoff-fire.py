#!/usr/bin/env python3
"""Stage and enqueue bake-off arms from a validated roster.

Each arm must start from the task's BASELINE, never the directory as it sits --
the roster dirs hold SOLVED state, so dispatching one directly would hand the
model the answer and score every arm as a pass. So per arm we copy the task,
reset --hard to baseline, clean -fdx (checkout+clean cannot delete a file the
solution ADDED), and then PRE-FLIGHT that copy: its verify must FAIL. An arm
whose verify already passes before the model runs is not staged, it is scored.

Arms are emitted grouped by model so the FIFO queue does not swap weights
between every job.

Usage: bakeoff-fire.py --roster <json> --model M [--model M2] --host <url>
                       [--arms-dir D] [--num-ctx N] [--max-iters N] [--dry-run]
"""
import argparse, json, shutil, subprocess, sys
from pathlib import Path

QUEUE = Path.home() / "bin" / "ollama-queue.py"


def sh(*a, **kw):
    return subprocess.run(a, capture_output=True, text=True, **kw)


def stage(task, model, arms_dir):
    src = Path(task["dir"]).resolve()
    safe = model.replace(":", "-").replace("/", "-")
    dst = arms_dir / f"{src.name}__{safe}"
    if dst.exists():
        shutil.rmtree(dst)
    shutil.copytree(src, dst, symlinks=True,
                    ignore=shutil.ignore_patterns("node_modules", "__pycache__"))
    base = task["baseline"]
    sh("git", "-C", str(dst), "reset", "-q", "--hard", base)
    sh("git", "-C", str(dst), "clean", "-qfdx")
    return dst


def preflight(d):
    """The arm must start from a FAILING verify, or it measures nothing."""
    if not (d / "verify.sh").exists():
        return False, "no verify.sh"
    if not (d / "TASK.md").exists():
        return False, "no TASK.md"
    r = subprocess.run(["bash", "verify.sh"], cwd=str(d),
                       capture_output=True, text=True, timeout=600)
    if r.returncode == 0:
        return False, "verify PASSES at baseline -- arm would be scored, not staged"
    return True, f"baseline fails (rc={r.returncode})"


def queue_supports_scored_arm():
    """The worker's --scored-arm (batch #8) only reaches it if ollama-queue.py
    forwards it, and the queue is owned by another session -- so this is a
    feature CHECK, not an assumption. Ask the queue what it accepts rather than
    passing a flag that would make every enqueue fail with an argparse error."""
    r = sh(sys.executable, str(QUEUE), "enqueue", "--help")
    return "--scored-arm" in ((r.stdout or "") + (r.stderr or ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--roster", required=True)
    ap.add_argument("--model", action="append", required=True)
    ap.add_argument("--host", required=True)
    ap.add_argument("--arms-dir", default=None)
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--max-iters", type=int, default=30)
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()

    roster = json.loads(Path(a.roster).read_text())
    tasks = [t for t in roster["tasks"] if t.get("eligible")]
    if not tasks:
        print("no eligible tasks in roster"); return 2
    arms_dir = Path(a.arms_dir or (Path(a.roster).parent / "arms"))
    arms_dir.mkdir(parents=True, exist_ok=True)

    planned, refused = [], []
    for model in a.model:                       # grouped by model, not by task
        for t in tasks:
            d = stage(t, model, arms_dir)
            ok, why = preflight(d)
            (planned if ok else refused).append((t["task"], model, d, why))

    for name, model, d, why in refused:
        print(f"  REFUSED  {name:18s} {model:22s} {why}")
    for name, model, d, why in planned:
        print(f"  staged   {name:18s} {model:22s} {why}")
    print(f"--- {len(planned)} arm(s) staged, {len(refused)} refused ---")
    if a.dry_run:
        print("dry run: nothing enqueued"); return 1 if refused else 0

    # Every arm here has been PROVEN by preflight() to fail its verify at
    # baseline -- that is the staging precondition, not a guess -- so the worker
    # can be told outright that this is a scored arm: the baseline failures ARE
    # the task, subtraction off, a still-failing verify never accepted.
    scored = queue_supports_scored_arm()
    if not scored:
        print("  WARNING: this ollama-queue.py does not forward --scored-arm, so arms "
              "will be graded under the LENIENT baseline-subtraction rule. Results are "
              "NOT comparable to scored-arm runs. Update the queue before trusting them.")
    for name, model, d, _ in planned:
        r = sh(sys.executable, str(QUEUE), "enqueue", "--model", model,
               "--host", a.host, "--cwd", str(d),
               "--task-file", str(d / "TASK.md"), "--task-kind", "coding",
               "--verify", "bash verify.sh", "--num-ctx", str(a.num_ctx),
               "--max-iters", str(a.max_iters),
               "--label", f"bo-{name}-{model.split(':')[0]}",
               *(["--scored-arm"] if scored else []))
        msg = (r.stdout or r.stderr or "").strip().splitlines()
        tag = "  enqueued " if r.returncode == 0 else "  ENQUEUE FAILED "
        # never let a silent failure print as a blank line
        print(tag + (msg[0] if msg else f"{name}/{model} (rc={r.returncode}, no output)"))
    return 1 if refused else 0


if __name__ == "__main__":
    sys.exit(main())
