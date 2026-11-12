#!/bin/bash
# B3 END-TO-END EXERCISE — Fable blocker 4, the last thing standing before dispatch.
#
# WHAT IT PROVES, AND WHY IT CANNOT BE SKIPPED
# --------------------------------------------
# B3 restarts the inference server before every run and gates on available memory
# recovering. That path has never once executed end to end -- a recovery run and
# then a vision sweep have held the server all day. Dispatching ~16h of runs on
# an unexercised control path would mean every host_ready value in the CSV is
# unvalidated, and host_ready is the column the whole degradation argument rests
# on.
#
# It also answers a question the round cannot interpret its own results without:
# HOW MUCH MEMORY DOES A CLEAN, JUST-RESTARTED HOST ACTUALLY HAVE FREE? Each
# model's honest gate is resident x 1.2 + KV. Any model whose gate exceeds the
# clean-host figure can never pass it -- not because B3 failed, but because the
# model does not fit this hardware. Those cells are STRUCTURAL, and the
# preregistration's falsifier ("&gt;20% host_ready=no means B3 did not work")
# counts only reachable-gate cells. This run is what fixes which is which, and
# it is decided BEFORE any results exist rather than after.
set -uo pipefail

BASE="/Users/user/Desktop/GitHub Projects"
HOSTTEL="$BASE/bakeoff/harness/bakeoff-v8-hosttel.py"
HOST="http://localhost:11434"
source "$BASE/bakeoff/harness/bakeoff-v8-lib.sh"

echo "--- B3 exercise starting ---"
echo "before restart: $(python3 "$HOSTTEL")"

if pgrep -f "gc-sweep.py|queue-macstudio-vision" >/dev/null; then
  echo "ABORT: a vision sweep is still on the server. Restarting Ollama would kill it."
  exit 1
fi

T0=$(date +%s)
echo "restarting ollama ..."
/opt/homebrew/bin/brew services restart ollama >/dev/null 2>&1

UP=1
for _ in $(seq 1 30); do
  sleep 2
  if curl -s --max-time 5 "$HOST/api/tags" >/dev/null 2>&1; then UP=0; break; fi
done
T_UP=$(( $(date +%s) - T0 ))
if [ "$UP" -ne 0 ]; then
  echo "FAIL: ollama did not come back within 60s"
  exit 1
fi
echo "ollama answering again after ${T_UP}s"

# Let the machine settle, then take the number everything else is judged against.
sleep 30
CLEAN=$(python3 "$HOSTTEL" --available)
TOTAL=$(python3 "$HOSTTEL" --total)
echo "CLEAN-HOST AVAILABLE: ${CLEAN}MB of ${TOTAL}MB total"
echo "full telemetry: $(python3 "$HOSTTEL")"
echo

echo "--- per-model gate reachability on THIS host ---"
printf "%-26s %-8s %-9s %-9s %s\n" "MODEL" "CTX" "RESIDENT" "GATE" "REACHABLE?"
REACHABLE=0; STRUCTURAL=0
for M in qwen3.8:27b-q8_0 qwen3-14b-agentic deepseek-r1:32b qwen2.5-coder:14b qwen3-coder-next:q4_K_M qwen3-coder:30b; do
  C=$(ctx_for "$M"); G=$(readiness_threshold_mb "$M" "$C"); R=$(resident_mb_for "$M")
  if [ "$G" -le "$CLEAN" ]; then VERDICT="yes"; REACHABLE=$((REACHABLE+1))
  else VERDICT="NO -- STRUCTURAL (needs a bigger host)"; STRUCTURAL=$((STRUCTURAL+1)); fi
  printf "%-26s %-8s %-9s %-9s %s\n" "$M" "$C" "$R" "$G" "$VERDICT"
done
echo
echo "reachable: $REACHABLE   structural: $STRUCTURAL"
echo
echo "Record this in v8-PREREGISTRATION.md before dispatch: the structural cells"
echo "are EXCLUDED from falsifier #1, because their host_ready=no is a routing"
echo "fact ('requires a >64GB host'), not a failure of B3."

# The gate path itself, exercised for real on the smallest model.
echo
echo "--- exercising restart_inference_server() for real (qwen2.5-coder:14b) ---"
if restart_inference_server "$HOST" "qwen2.5-coder:14b" 32768; then
  echo "PASS: gate met, host_ready would be yes"
else
  # This is the SMALLEST model in the roster (gate 15600MB). If a freshly
  # restarted host cannot clear even that, the problem is the host, not a
  # model that does not fit it -- and every later host_ready value would be
  # meaningless. Fail the phase rather than let the pipeline proceed.
  echo "FAIL: the smallest roster model's gate was not met on a fresh host."
  echo "      That is a host problem, not a structural cell. Investigate before"
  echo "      dispatching -- do not treat this as an expected host_ready=no."
  exit 1
fi
echo "after: $(python3 "$HOSTTEL")"
echo "--- B3 exercise complete ---"
