#!/bin/bash
# Rerun deepseek-r1:32b with NATIVE tool_calls instead of --manual-tools.
# It was the one model in the 9-model roster driven on a different protocol
# (worker_flags_for() special-cases only deepseek-r1*) and scored 2/17. This
# checks whether that was the model or the protocol mismatch.
#
# Uses copies of the harness with three lines changed (OUT, SBOX, the
# worker_flags_for case), never the live scripts -- those get re-invoked by
# the Studio roster on every task, and editing a running script corrupts it.
# Separate OUT/SBOX so this run's rows can never land in the roster's own
# results-f2p-multi-65k.csv / results-review-65k.csv or reuse a worktree path.
cd "/Users/user/Desktop/GitHub Projects"
K=machine-config/benchmarks
BIN=machine-config/bin
log(){ echo "[deepseek-retest $(date '+%m-%d %H:%M:%S')] $*"; }

while pgrep -f ".ollama-sandboxes/roster-final.sh" >/dev/null; do sleep 120; done
log "Studio roster finished, starting deepseek-r1:32b native-tools retest"

curl -s -m 5 http://localhost:11434/api/generate -d '{"model":"deepseek-r1:32b","keep_alive":0}' >/dev/null 2>&1

log "--- [1/3] fail-to-pass (13 tasks) ---"
bash "$BIN/run-f2p-deepseek-retest.sh" "$K/tasks-f2p.tsv" "deepseek-r1:32b" || log "f2p FAILED (continuing)"

log "--- [2/3] stacked bugs (4 tasks) ---"
bash "$BIN/run-f2p-deepseek-retest.sh" "$K/tasks-stack.tsv" "deepseek-r1:32b" || log "stack FAILED (continuing)"

log "--- [3/3] review bench ---"
bash "$BIN/run-review-deepseek-retest.sh" "deepseek-r1:32b" || log "review FAILED (continuing)"

curl -s -m 5 http://localhost:11434/api/generate -d '{"model":"deepseek-r1:32b","keep_alive":0}' >/dev/null 2>&1
log "DEEPSEEK RETEST COMPLETE"
