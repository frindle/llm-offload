#!/bin/bash
# v8 EXPERIMENT ARMS — the two questions v7 raised and then dropped.
#
# These were sections 4 and 5 of v7's open questions. They were cut from v7 for
# time and never re-planned, which is how a "cheap test" becomes a thing nobody
# ever runs. They are restored here as their own driver, deliberately SEPARATE
# from the base round, for two reasons:
#
#   1. The base round is what a GO/NO-GO decision rests on. It must not be
#      blocked behind ~6 more hours of arm runs, and an arm failure must not
#      contaminate the base CSV.
#   2. Each arm is a single-cell hypothesis. The salience arm is about the photo
#      task; the API-surface arm is about the clamshell task. Running both tasks
#      for either would generate rows that arm cannot interpret.
#
# Both arms write to the SAME results-v8.csv as the base round, distinguished by
# the `arm` column. That is the point of having the column: the comparison is
# base-vs-arm on the same cell at the same context on the same controlled host,
# and splitting them across files is what would make that comparison manual and
# error-prone.
#
# ---------------------------------------------------------------------------
# ARM 1 — `repomap`: does exploration failure track SALIENCE rather than
# willingness to look?
#
# qwen3-14b-agentic is exploration-fail on photo (one file among ~40 components
# in a large Next.js app) and transfer-fail on clamshell (evidence is one
# manifest plus one directory listing). Same model, two tasks, two different
# failure modes -- which is exactly what a salience effect looks like.
#
# Treatment: a repo map (git ls-files digest, generated from the worktree at
# dispatch time) prepended to the prompt. If exploration failures collapse, this
# is an ADDRESSABLE PROMPT PROBLEM rather than a model defect -- a much better
# finding than the current framing.
#
# qwen3.8 is in this arm as a CONTROL, not as a subject. It already engages with
# the existing implementation. If the repo map "improves" qwen3.8 too, the arm is
# measuring a general prompt-strengthening effect rather than salience, and the
# result on the other two models cannot be read as a salience finding.
#
# ---------------------------------------------------------------------------
# ARM 2 — `apisurface`: is the CryptoKit failure RECALL or reasoning?
#
# Three models invented three DIFFERENT wrong identifiers for the same call
# (P256.KeyPair, P256.SigningKey, P256.Signing.Signature). Three independent
# wrong answers to one question is the signature of absent recall, not of bad
# reasoning. Only qwen3.8 recovered, and only via the compiler error.
#
# Treatment: the real P256 signing surface, excerpted VERBATIM from the SDK's own
# .swiftinterface at dispatch time (build_api_surface). Not hand-transcribed --
# a subtly wrong hand-written excerpt would test our typing rather than the
# model, and this is precisely the kind of instrument defect that cost v7 five
# scorer bugs before any verdict rested on it.
#
# If supplying the surface removes the failure class, the finding for real
# dispatch work is concrete and immediately usable: supply the API surface, do
# not expect recall.
#
# ---------------------------------------------------------------------------
# Read the arms against the BASE rows for the same cells, never on their own. An
# arm row with no base row to compare against says nothing.
#
# ---------------------------------------------------------------------------
# n=3, NOT n=2 -- Fable proposed n=2 and this round overrules it on its own data.
#
# The n=2 argument is that a screen only needs to see a failure class collapse:
# 2/2 collapses, 0/2 does not, and 1/2 earns a third rep. The safeguard sounds
# complete and is not, because it only catches SPLIT cells. The dangerous case
# is a cell that reads a clean unanimous 2/2 and would have been 2/3 -- that
# never triggers the third rep, and v7 produced it TWICE in four fully-usable
# cells:
#
#   qwen3-14b-agentic / clam   transfer, transfer, EXPLORATION
#   deepseek-r1:32b   / photo  inert,    inert,    EXPLORATION
#
# Both read as settled at n=2 and were wrong. Only one v7 cell out of ten was
# unanimous at n=3.
#
# It is worse here than in general, because the salience arm's two subjects ARE
# qwen3-14b-agentic and deepseek-r1:32b -- precisely the two models with
# demonstrated rep-3 instability. Screening them at n=2 would apply the smaller
# sample exactly where the smaller sample has already failed.
#
# It also contradicts v8's own standing rule: no per-model claim until every rep
# for that cell has landed; cells that split get a distribution, not a label.
#
# The ~6-7h is funded instead by dropping qwen3-coder-next from the apisurface
# arm (below), which costs no information at all.
#
# qwen3-coder-next is DROPPED from the apisurface arm (saves ~3h). Its v7
# clamshell rows died before ever reaching the API-recall failure point, so
# there is no base-arm comparison to make -- the arm could not say anything
# about it either way. Running it would have generated rows that look like data
# and answer nothing.
set -uo pipefail
export RUN_TAG=v8
source "/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

echo "=== $RUN_TAG EXPERIMENT ARMS START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

# Fail fast rather than silently degrading arm 2 into a duplicate of the base
# arm -- a missing excerpt would produce base-identical prompts labelled
# `apisurface`, which is worse than not running the arm at all.
if [ "$(build_api_surface | head -1)" = "__API_SURFACE_UNAVAILABLE__" ]; then
  echo "ABORT: CryptoKit .swiftinterface not found -- arm 2 cannot run" | tee -a "$DRIVER_LOG"
  exit 1
fi

# --- Arm 1: salience, photo task only ---
SALIENCE=(
  "qwen3-14b-agentic|qwen3-14b-agentic|3600|no|no"        # subject: exploration-fail on photo
  "deepseek-r1:32b|deepseek-r1-32b|6000|yes|yes"          # subject: inert/exploration on photo
  "qwen3.8:27b-q8_0|qwen3.8-27b-q8_0|4200|no|no"          # CONTROL: already engages
)

for REP in 1 2 3; do
  echo "=== $RUN_TAG ARM repomap REPEAT $REP/3 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
  ORDER=$(shuffled_roster "$(( 100 + REP ))" "${SALIENCE[@]}")
  while IFS='|' read -r MODEL SLUG TMO MANUAL NUDGE_FLAG; do
    [ -z "$MODEL" ] && continue
    ONLY_TASK=photo run_model_v8 "$MODEL" "$SLUG" "$TMO" "$MANUAL" "$NUDGE_FLAG" \
                                 "$BACKEND" "$HOST" "$REP" "repomap" </dev/null
    unload_model "$MODEL" "$HOST"
  done <<< "$ORDER"
done

# --- Arm 2: API surface, clamshell task only ---
APISURF=(
  "qwen3-14b-agentic|qwen3-14b-agentic|3600|no|no"
  "deepseek-r1:32b|deepseek-r1-32b|6000|yes|yes"
  "qwen3.8:27b-q8_0|qwen3.8-27b-q8_0|4200|no|no"          # CONTROL: the one that recovered
)

for REP in 1 2 3; do
  echo "=== $RUN_TAG ARM apisurface REPEAT $REP/3 @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
  ORDER=$(shuffled_roster "$(( 200 + REP ))" "${APISURF[@]}")
  while IFS='|' read -r MODEL SLUG TMO MANUAL NUDGE_FLAG; do
    [ -z "$MODEL" ] && continue
    ONLY_TASK=clam run_model_v8 "$MODEL" "$SLUG" "$TMO" "$MANUAL" "$NUDGE_FLAG" \
                                "$BACKEND" "$HOST" "$REP" "apisurface" </dev/null
    unload_model "$MODEL" "$HOST"
  done <<< "$ORDER"
done

echo "=== $RUN_TAG EXPERIMENT ARMS COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
