#!/bin/bash
# v7 launcher. Safe to start at any time -- it blocks until v6 is completely
# finished, then starts both legs.
#
# WHY THE GUARD EXISTS
# v7 reuses the SAME worktrees and the SAME GPUs as v6. Starting it early
# would (a) git reset --hard a tree a v6 model is actively working in, which
# destroys that model's result silently, and (b) contend for VRAM, which makes
# every duration and every /api/ps footprint meaningless. Both failure modes
# are silent -- they produce plausible numbers, not errors. That is exactly
# the class of bug that has cost this bake-off the most.
#
# It waits for the v6 DEVSTRAL marker, not the v6 MACSTUDIO marker: the v6
# devstral llama-server re-run starts AFTER the macstudio leg completes and
# runs on the same box.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
OUTDIR="$BASE/model-buildoff-2026-08-22"
DRIVER_LOG="$OUTDIR/driver.log"

echo "[v7-launch] waiting for v6 to finish completely @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"

# 1. v6 devstral llama-server re-run is the last thing v6 does.
while ! grep -aq "DEVSTRAL LLAMA-SERVER RE-RUN COMPLETE" "$DRIVER_LOG"; do sleep 60; done

# 2. Belt and braces: no v6 driver or Mac Studio worker still alive.
#
# The worker check is scoped to --host http://localhost on purpose. A bare
# `pgrep -f ollama-worker.py` would also match the hand-launched v7 UNRAID
# workers, which run against 192.0.2.82 and have nothing to do with the GPU
# this leg needs -- that would stall the macstudio leg behind an unrelated
# run for no reason.
while pgrep -f "bakeoff-v6-" >/dev/null 2>&1 || pgrep -f "ollama-worker.py.*--host http://localhost" >/dev/null 2>&1; do
  echo "[v7-launch] v6 marker seen but a process is still alive -- waiting" >> "$DRIVER_LOG"
  sleep 60
done

echo "[v7-launch] v6 clear -- starting both v7 legs @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

cd "$BASE"
nohup ./bakeoff-v7-macstudio.sh > "$OUTDIR/v7-macstudio-stdout.log" 2>&1 &
disown

# The UNRAID LEG IS DELIBERATELY NOT STARTED HERE. It was launched by hand on
# 2026-08-22 while the v6 macstudio leg was still running, because Unraid was
# idle and parallel time is free. Starting it again from this script would:
#   * append duplicate rows for both 7B models to results-v7.csv, and
#   * git reset --hard / clean -fd the two unraid worktrees, destroying the
#     v7 diffs those runs already produced.
# Re-enable ONLY if the hand-launched run did not happen or must be redone.
#   nohup ./bakeoff-v7-unraid.sh > "$OUTDIR/v7-unraid-stdout.log" 2>&1 &
#   disown

nohup ./bakeoff-v7-devstral-llamaserver.sh > "$OUTDIR/v7-devstral-stdout.log" 2>&1 &
disown

echo "[v7-launch] both legs started @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"
