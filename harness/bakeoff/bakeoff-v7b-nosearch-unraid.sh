#!/bin/bash
# v7b — no-search control, Unraid only. See bakeoff-v7b-nosearch-lib.sh header.
#
# Both models failed v7 at the SEARCH step and never reached the edit step.
# This run hands them the file path AND the file contents so only editing
# ability is under test. Same harness, same grading, same verify as v7 -- the
# only variable removed is search.
#
# Interpreting the outcome:
#   touched_target=yes + build passes  -> the v7 failure was search-specific.
#                                         The model is usable for narrow,
#                                         supervised, file-in-hand work.
#   touched_target=no, or build breaks -> the failure is not about search.
#                                         Write it off with evidence.
set -uo pipefail
export RUN_TAG=v7b
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v7b-nosearch-lib.sh"

HOST="http://192.0.2.82:11434"
BACKEND="unraid"

echo "=== $RUN_TAG UNRAID START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

# The model this test exists for. Its recorded failures are all locating or
# naming failures, and it got the SwiftPM placement right where 32b and
# MFDoom did not -- so "can it edit code in front of it" is a genuinely open
# question.
run_model_v7b "qwen2.5-coder:7b" "qwen2.5-coder-7b-unraid" 32768 1800 no  no  "$BACKEND" "$HOST"
unload_model "qwen2.5-coder:7b" "$HOST"

# Control. Expected to fail again: its failures are not search failures --
# placeholder prose written as file contents, a .ts file in a Swift project,
# a hallucinated URL with CJK characters in the hostname. Included so the
# comparison is not a single-model anecdote.
run_model_v7b "deepseek-r1:7b"   "deepseek-r1-7b-unraid"   32768 1800 yes yes "$BACKEND" "$HOST"
unload_model "deepseek-r1:7b" "$HOST"

echo "=== $RUN_TAG UNRAID COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
