#!/bin/bash
# v5 Mac Studio, ADDITIONAL models — the three that were never in the matrix.
#
# My error: I described the v5 matrix as "the full matrix", but I rebuilt it
# from bakeoff-driver-macstudio-remaining.sh, which was already a SUBSET
# (the models left over from the original round). These three were never in
# any v2/v3/v4/v5 script:
#
#   devstral:24b        - only ever tested as a separate llama-server one-off,
#                         on the old broken harness. Its "confirmed non-viable"
#                         verdict was later overturned; no clean data exists.
#   deepseek-r1:70b     - produced the single genuine pass of the v1 round
#                         (exit=0, 404s, 5 files) -- but pre-fix, so invalid.
#   qwen3-14b-agentic   - never tested at all. The original attempt died when
#                         the LAN mount to Unraid dropped mid-run. It IS in the
#                         shared archive; absence was never the problem.
#
# Runs AFTER the main macstudio leg so nothing contends for memory.
#
# WORKTREE CONTAMINATION -- read before adding any further model:
# qwen3-14b-agentic had no worktrees, so they were created fresh. Branching
# from HEAD put the ANSWER KEY in the clamshell worktree: the repo's HEAD had
# moved to 526bcb2 "Add ConfirmationBridge: challenge-signed remote
# confirmation (P-256)" -- i.e. the exact module the clamshell task asks the
# model to write, self-test included, as TRACKED files. Reset to 8803d67, the
# baseline every other clamshell worktree uses. The vault records this same
# class of incident hitting all 9 worktrees earlier the same day.
#   => ALWAYS create new bake-off worktrees from the SAME baseline commit the
#      existing ones use, never from HEAD.
# Environment parity was also set up (npm install + prisma generate); the
# lib's pristine-tree preflight will refuse to dispatch if that is wrong.
set -uo pipefail
source "/Users/user/Desktop/GitHub Projects/bakeoff-v5-lib-path.sh" 2>/dev/null || \
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v2-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

echo "[v5-extra] waiting for the main macstudio leg to finish @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
while ! grep -aq "v5 MACSTUDIO COMPLETE" "$DRIVER_LOG"; do
  sleep 30
done

echo "=== $RUN_TAG MACSTUDIO-EXTRA START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

# devstral:24b -- native Ollama tool-calling (capabilities ['completion',
# 'tools'], real tool template confirmed), ctx 65536 and temp 0.2 both carried
# over from the settings proven working for it earlier.
run_model "devstral:24b"       "devstral-24b"       65536   1800  no   no   "$BACKEND" "$HOST"
unload_model "devstral:24b" "$HOST"

# deepseek-r1:70b -- deepseek family: manual tools + nudge, 131072 ctx.
# Not in local Ollama; ensure_model_cached will copy it from the shared
# archive (confirmed present: library/deepseek-r1/70b).
run_model "deepseek-r1:70b"    "deepseek-r1-70b"    131072  1800  yes  yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:70b" "$HOST"

# qwen3-14b-agentic -- native tool-calling assumed (the name implies it, and
# the qwen family has real templates), qwen sampling via the lib (temp 0.2).
# UNVERIFIED: if it emits zero tool calls, that is the template gap, not the
# model -- retry once with --manual-tools before recording any verdict.
run_model "qwen3-14b-agentic"  "qwen3-14b-agentic"  32768   1800  no   no   "$BACKEND" "$HOST"
unload_model "qwen3-14b-agentic" "$HOST"

echo "=== $RUN_TAG MACSTUDIO-EXTRA COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
