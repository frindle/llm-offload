#!/bin/bash
# Final head-to-head, immurok-yk re-port task: both finalists, sequential.
# Same fixed pattern as headtohead-oob.sh -- absolute paths throughout, independent
# post-hoc re-verification against the real deliverable, never trust the worker's
# own self-report.
set -uo pipefail
BASE="/Users/user/Desktop/GitHub Projects"
cd "$BASE"
SRC="$BASE/immurok-yk-oob-dispatch"
OUT="$BASE/.headtohead-immurok/results.csv"
mkdir -p "$BASE/.headtohead-immurok"
echo "model,verify,files_changed,iters,dur_s,sandbox" > "$OUT"

TASKTEXT="$(cat "$SRC/CONCEPT.md")

Full repository is at your working directory. PROTOCOL.md and SECURITY.md are also present. Do not
modify anything under Sources/immurok-tests/ or Package.swift. scripts/ is not present in this
tree -- any test vectors you need are already embedded in the test files."

run_one() {
  local M="$1"
  local NAME="immurok-$(echo "$M" | tr ':/.' '---')"
  local WT="$BASE/.headtohead-immurok/$NAME"
  local LOG="$BASE/.headtohead-immurok/$NAME.log"
  local GRADE="$BASE/.headtohead-immurok/$NAME-grade"
  rm -rf "$WT"
  cp -R "$SRC" "$WT"
  rm -rf "$WT/.git"
  echo "=== $M ==="
  T0=$(date +%s)
  python3 /Users/user/bin/ollama-worker-v7.py \
    --model "$M" --host "http://localhost:11434" --cwd "$WT" \
    --task "$TASKTEXT" \
    --verify "swift build --product immurok-tests && .build/debug/immurok-tests" \
    --max-iters 30 --num-ctx 65536 --temperature 0.3 \
    > "$LOG" 2>&1
  DUR=$(( $(date +%s) - T0 ))
  # Fable: nothing stopped a model from editing tests/ or Package.swift, and .git is
  # stripped so there's no history to catch it. Transplant fix -- grade a PRISTINE copy
  # of the source tree with ONLY the model's Sources/ImmurokKit/ swapped in, so nothing
  # else it touched can cross into the graded result.
  rm -rf "$GRADE"
  cp -R "$SRC" "$GRADE"
  rm -rf "$GRADE/.git" "$GRADE/Sources/ImmurokKit"
  cp -R "$WT/Sources/ImmurokKit" "$GRADE/Sources/ImmurokKit"
  REAL_V=no
  ( cd "$GRADE" && swift build --product immurok-tests >/dev/null 2>&1 && .build/debug/immurok-tests >/dev/null 2>&1 ) && REAL_V=yes
  FC=$(find "$WT/Sources/ImmurokKit" -name "*.swift" | wc -l | tr -d ' ')
  IT=$(grep -ac -- "--- iteration " "$LOG" | tr -d ' ')
  echo "$M,$REAL_V,$FC,$IT,$DUR,$NAME" >> "$OUT"
  echo "  independently-verified=$REAL_V (transplant-graded) ImmurokKit_files=$FC iters=$IT ${DUR}s"
}

run_one "qwen3.8:27b-q8_0"
run_one "ornith-1.5:35b"
echo "IMMUROK HEAD-TO-HEAD COMPLETE -> $OUT"
