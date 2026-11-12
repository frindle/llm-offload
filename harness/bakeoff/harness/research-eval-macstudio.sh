#!/bin/bash
# RESEARCH / DATA-ANALYSIS EVAL — thinking models, Mac Studio. n=3.
#
# WHY THIS EXISTS SEPARATELY FROM THE CODING BAKE-OFF
# ---------------------------------------------------
# The v7/v8 bake-off asks whether a model can edit files and make a build pass.
# That is the hardest case, and the answer so far is no: 19 usable v7 runs
# produced 2 `correct`. Concluding "local models cannot be offloaded to" from
# that would be wrong, because it is a conclusion about AGENTIC CODING drawn from
# an agentic-coding test.
#
# Research and analysis is a different job with a different failure mode. Here
# the model reads data it is given and reports on it. The risk is not that it
# cannot edit a file -- it is that it answers fluently and invents one number.
# We have already seen exactly that locally: qwen3-coder-next gets architecture
# right while fabricating specific figures and UUIDs, and silently dropping real
# details. A fluent wrong answer is worse than a refusal because it gets believed.
#
# SO THE DECIDING METRIC IS FABRICATION RATE, NOT ACCURACY.
# 7 of the 24 tasks ask about data that is genuinely NOT in the dataset. A model
# that abstains on those is usable at 60% accuracy, because you can trust what it
# does say. A model that invents answers is unusable at 85%, because you cannot
# tell which answers to check -- and if you must check them all, it saved nothing.
#
# GROUND TRUTH IS COMPUTED, NEVER TYPED. research-eval-gen.py derives every
# answer from the real results-v7.csv at generation time, and the key was then
# independently recomputed (7/7 match) before any model ran. This project has
# lost two whole vision sweeps and five v7 verdicts to instrument defects; the
# answer key is an instrument.
#
# HOST CONTROL: the same B3 discipline as v8. This roster includes a 42.5GB model
# on a 64GB machine, so memory pressure is not hypothetical -- it is the expected
# state. Restart between models and record telemetry per task.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
HOSTTEL="$BASE/bakeoff/harness/bakeoff-v8-hosttel.py"
HOST="http://localhost:11434"
LOG="$BASE/research-eval-driver.log"
RESULTS="$BASE/research-eval-results.csv"

# ctx is modest ON PURPOSE. The dataset is ~3KB, and answers are one JSON object,
# so a large window buys nothing and costs memory that the 70b needs. Thinking
# models still get ample room for reasoning tokens.
CTX=32768

# model|slug|temperature
# deepseek-r1:70b leads because it is the actual open question -- your largest
# local model, never benchmarked in any round. phi4:14b is the small control: if
# a 9GB model matches a 42GB one here, that is the finding.
ROSTER=(
  "deepseek-r1:70b|deepseek-r1-70b|0.6"
  "deepseek-r1:32b-qwen-distill-q8_0|deepseek-r1-32b-distill-q8|0.6"
  "qwen3.8:27b-q8_0|qwen3.8-27b-q8_0|0.2"
  "deepseek-r1:32b|deepseek-r1-32b|0.6"
  "phi4:14b|phi4-14b|0.2"
  # POSITIVE CONTROL (Fable). qwen3-coder-next is the model whose observed
  # behaviour -- right architecture, fabricated specifics, silently omitted
  # details -- is the reason fabrication rate is the headline metric at all. If
  # this instrument scores IT at 0% fabrication, doubt the instrument before
  # trusting any other model's score. No current roster member can validate the
  # eval in that direction. It pages badly at 51.7GB, which is fine here:
  # correctness is measured, not latency -- just do not read its timings as a
  # model property.
  "qwen3-coder-next:q4_K_M|qwen3-coder-next-q4_K_M|0.2"
)

restart_and_wait() {
  # Stage between models, never during a run -- same reasoning as the coding
  # harness. --keep protects nothing here on purpose: this roster is large
  # (deepseek-r1:70b is 42.5GB) and the models must rotate through local disk
  # sequentially, evicting as they go.
  if [ -n "${1:-}" ]; then
    echo "[research-eval] staging $1 ..." >> "$LOG"
    python3 "$BASE/bakeoff/harness/bakeoff-v8-stage.py" stage "$1" >> "$LOG" 2>&1 \
      || echo "[research-eval] WARNING: staging $1 failed" >> "$LOG"
  fi
  echo "[research-eval] restarting ollama ..." >> "$LOG"
  /opt/homebrew/bin/brew services restart ollama >/dev/null 2>&1
  for _ in $(seq 1 30); do
    sleep 2
    curl -s --max-time 5 "$HOST/api/tags" >/dev/null 2>&1 && break
  done
  # Gate on available memory, never on swap -- macOS does not shrink swap files
  # promptly, so a swap gate blocks long after the memory is actually back.
  local NEED=20000 AVAIL=0
  for _ in $(seq 1 36); do
    AVAIL=$(python3 "$HOSTTEL" --available)
    [ "$AVAIL" -ge "$NEED" ] && break
    sleep 5
  done
  echo "[research-eval] host avail=${AVAIL}MB (want >= ${NEED}MB)" >> "$LOG"
}

# Resume safety: re-running this appends duplicate (model,rep) rows, and the
# scorer would silently merge two samples into one cell. Archive any existing
# results first rather than corrupting a cell.
if [ -f "$RESULTS" ]; then
  mv "$RESULTS" "${RESULTS%.csv}-$(date '+%Y%m%dT%H%M%S').csv"
  echo "archived a previous results file before starting" >> "$LOG"
fi

echo "=== RESEARCH EVAL START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
python3 "$BASE/bakeoff/harness/research-eval-gen.py" >> "$LOG" 2>&1

for REP in 1 2 3; do
  echo "=== REP $REP/3 @ $(date '+%H:%M:%S') ===" >> "$LOG"
  for ENTRY in "${ROSTER[@]}"; do
    IFS='|' read -r MODEL SLUG TEMP <<< "$ENTRY"

    restart_and_wait "$MODEL"
    TEL=$(python3 "$HOSTTEL")
    echo "[research-eval] START $MODEL rep $REP ctx=$CTX temp=$TEMP host=$TEL" >> "$LOG"

    python3 "$BASE/bakeoff/harness/research-eval-run.py" \
      --model "$MODEL" --host "$HOST" --num-ctx "$CTX" \
      --temperature "$TEMP" --rep "$REP" --out "$RESULTS" \
      >> "$LOG" 2>&1

    curl -s --max-time 30 "$HOST/api/generate" \
      -d "{\"model\":\"$MODEL\",\"keep_alive\":0}" >/dev/null 2>&1
    echo "[research-eval] DONE $MODEL rep $REP" >> "$LOG"
  done
done

echo "=== RESEARCH EVAL COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$LOG"
python3 "$BASE/bakeoff/harness/research-eval-score.py" >> "$LOG" 2>&1
