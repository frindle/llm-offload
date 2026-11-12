#!/usr/bin/env python3
"""Diff-aware code-review PRE-FILTER on the Unraid 3080 (qwen3.5:9b).

A cheap first pass that FLAGs risky diffs for human/Claude review and PASSes clearly
cosmetic ones -- runs on the idle 3080, off the Studio and off Claude. Validated
2026-08-30: qwen3.5:9b caught 3/3 seeded real defects (weakened check, wrong command,
perms/timing) with 0/3 false-positives on matched benign decoys. Advisory pre-filter,
NOT a merge gate (Fable: even 30B is not gate-grade on intent-level judgments).

Everything runs THROUGH the ollama queue pinned to Unraid -- never a direct API call.

Usage:
  review-prefilter.py <diff-file>          # classify a unified-diff file
  git diff | review-prefilter.py -         # classify staged/working diff from stdin
  review-prefilter.py <diff-file> --model qwen3:14b

Exit: prints VERDICT.md contents. Job id + livelog printed to stderr.
"""
import argparse, os, subprocess, sys, tempfile, time, json, re

QUEUE = os.path.expanduser("~/bin/ollama-queue.py")
STATE = os.path.expanduser("~/bin/ollama-queue-state.json")

TASK = """You are a code-review pre-filter. The file DIFF.patch in this directory is a
unified diff against a security-sensitive codebase. Read DIFF.patch and assess ONLY the
change shown. Decide whether it changes CORRECTNESS- or SECURITY-relevant behavior such
that a senior reviewer must look at it (FLAG), or whether it is clearly cosmetic and safe
to auto-approve (PASS). Weakening a check, changing which command/data is used, altering
control flow, or altering file permissions/timing is security-relevant; rewording a
message, comment, or log string is not.

Write your answer to VERDICT.md in EXACTLY this format:
VERDICT: FLAG   or   VERDICT: PASS
REASON: <one sentence naming the specific mechanism>
"""

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("diff", help="unified diff file, or - for stdin")
    ap.add_argument("--model", default="qwen3.5:9b")
    ap.add_argument("--num-ctx", type=int, default=8192)
    ap.add_argument("--timeout", type=int, default=600, help="seconds to wait for the queue job")
    args = ap.parse_args()

    diff = sys.stdin.read() if args.diff == "-" else open(args.diff).read()
    if not diff.strip():
        sys.exit("empty diff")

    cwd = tempfile.mkdtemp(prefix="prefilter-")
    open(os.path.join(cwd, "DIFF.patch"), "w").write(diff)
    open(os.path.join(cwd, "TASK.md"), "w").write(TASK)

    label = "prefilter-" + str(int(time.time()))
    cmd = [sys.executable, QUEUE, "enqueue",
           "--model", args.model, "--host", "unraid",
           "--cwd", cwd, "--task-file", os.path.join(cwd, "TASK.md"),
           "--task-kind", "coding",
           "--verify", 'grep -qE "VERDICT:[[:space:]]*(FLAG|PASS)" VERDICT.md',
           "--num-ctx", str(args.num_ctx), "--max-iters", "6",
           "--max-tokens", "8000", "--temperature", "0",
           "--capture-final-as", "VERDICT.md", "--label", label]
    out = subprocess.run(cmd, capture_output=True, text=True)
    sys.stderr.write(out.stdout + out.stderr)
    m = re.search(r"enqueued\s+([0-9a-f]+)", out.stdout)
    if not m:
        sys.exit("enqueue failed")
    jid = m.group(1)

    vpath = os.path.join(cwd, "VERDICT.md")
    deadline = time.time() + args.timeout
    while time.time() < deadline:
        if os.path.exists(vpath):
            time.sleep(1)
            print(open(vpath).read())
            return
        time.sleep(5)
    sys.exit(f"timed out waiting for job {jid}; check: python3 ~/bin/ollama-queue.py status")

if __name__ == "__main__":
    main()
