#!/bin/bash
# Final head-to-head, take 2: clamshell out-of-band unlock spec, both finalists, sequential.
# FIX: --cwd must be an ABSOLUTE path. Take 1 used a path relative to $REPO ("clamshell"), which
# git -C resolved against the repo dir for worktree creation but the worker process (spawned with
# cwd = this script's cwd, the BASE dir) resolved differently for its own file I/O -- the two
# disagreed about where the worktree actually was, so the model's file-write tools and its
# --verify command ended up looking at inconsistent locations. Confirmed real damage by hand:
# rebuilding both worktrees and diffing tests/ run directly.
set -uo pipefail
cd "/Users/user/Desktop/GitHub Projects"
BASE="$(pwd)"
REPO="clamshell"
BRANCH="feat/oob-unlock-spec"
OUT=".headtohead-oob/results.csv"
mkdir -p .headtohead-oob
echo "model,verify,files_changed,iters,dur_s,sandbox" > "$OUT"

SPEC=$(git -C "$REPO" show "$BRANCH:oob/SPEC.md")
TASKTEXT="Implement the two Python modules specified below against their existing test suites.
Neither module exists yet -- both test files currently fail on import, which is expected; that is
what you are building. Do not modify the test files. Run the tests with:
  python3 -m unittest discover -s tests -v
(run from the oob/ directory, which is your working directory)

$SPEC"

run_one() {
  local M="$1"
  local NAME="oob-$(echo "$M" | tr ':/.' '---')"
  local WT="$BASE/.headtohead-oob/$NAME"
  local LOG="$BASE/.headtohead-oob/$NAME.log"
  rm -rf "$WT"
  git -C "$REPO" worktree prune
  git -C "$REPO" worktree add --detach "$WT" "$BRANCH" 2>"$BASE/.headtohead-oob/$NAME.wterr"
  echo "=== $M ==="
  T0=$(date +%s)
  python3 /Users/user/bin/ollama-worker-v7.py \
    --model "$M" --host "http://localhost:11434" --cwd "$WT/oob" \
    --task "$TASKTEXT" \
    --verify "python3 -m unittest discover -s tests -v" \
    --max-iters 30 --num-ctx 65536 --temperature 0.3 \
    > "$LOG" 2>&1
  DUR=$(( $(date +%s) - T0 ))
  # Trust nothing but a direct, independent re-run against the real worktree.
  REAL_V=no
  ( cd "$WT/oob" && python3 -m unittest discover -s tests >/dev/null 2>&1 ) && REAL_V=yes
  FC=$(git -C "$WT" status --porcelain | grep -c . | tr -d ' ')
  IT=$(grep -ac -- "--- iteration " "$LOG" | tr -d ' ')
  echo "$M,$REAL_V,$FC,$IT,$DUR,$NAME" >> "$OUT"
  echo "  independently-verified=$REAL_V files=$FC iters=$IT ${DUR}s"
}

run_one "qwen3.8:27b-q8_0"
run_one "ornith-1.5:35b"
echo "HEAD-TO-HEAD COMPLETE -> $OUT"
