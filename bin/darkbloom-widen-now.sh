#!/bin/bash
# Drain the queue, widen Darkbloom's model filter via `darkbloom start`, verify both queue models warm, release held jobs. Rolls back to the 2-model pair on failure.
if [ "${DARKBLOOM_BY_OWNER:-}" != "1" ]; then
  echo "darkbloom-widen-now.sh: retired: BloomGauge runs Darkbloom and the queue's BloomGauge control picks models; a hand-run darkbloom/BloomGauge change fights it and spends BloomGauge's hourly change budget. The owner only: DARKBLOOM_BY_OWNER=1" >&2
  exit 2
fi
Q="python3 $HOME/bin/ollama-queue.py"
DB=$(python3 "$HOME/bin/queue_config.py" darkbloom-base 2>/dev/null); DB=${DB:-http://127.0.0.1:8000}   # base from env / local.json / config
HELD=/tmp/widen-held.ids; : > "$HELD"
PAIR=(--model qwen3.6-35b-a3b-vl-mtp-mxfp8 --model Qwen3.5-9B)
WIDE=("${PAIR[@]}" --model nvidia-nemotron-3.5-lightning --model qwen3.5-35b-a3b --model gemma-4-26b-qat-4bit)
echo "$(date -u +%T) drain start"; zero=0
while true; do
  OUT=$($Q status 2>/dev/null)
  for id in $(printf '%s\n' "$OUT" | sed -nE 's/^\[pending[[:space:]]*\][[:space:]]+([0-9a-f]{12}).*/\1/p'); do
    $Q hold "$id" >/dev/null 2>&1 && echo "$id" >> "$HELD"
  done
  RUN=$(printf '%s\n' "$OUT" | grep -cE '^\[(running|paused)[[:space:]]*\]')
  if [ "$RUN" -eq 0 ]; then zero=$((zero+1)); else zero=0; fi
  [ "$zero" -ge 2 ] && break
  sleep 5
done
echo "$(date -u +%T) idle; darkbloom start (wide)"
darkbloom start "${WIDE[@]}" --local-endpoint 2>&1 | tail -5
ok=0
for i in $(seq 1 60); do
  sleep 10
  if curl -s -m 5 "$DB/v1/models" | grep -q 'qwen3.6-35b-a3b-vl-mtp-mxfp8' && curl -s -m 5 "$DB/v1/models" | grep -qi 'qwen3.5-9b'; then ok=1; break; fi
done
if [ "$ok" -ne 1 ]; then
  echo "$(date -u +%T) WIDE FAILED -> rolling back to pair"
  darkbloom start "${PAIR[@]}" --local-endpoint 2>&1 | tail -3
  sleep 60
fi
echo "$(date -u +%T) filter now:"; darkbloom status 2>&1 | grep -i "filter"
echo "$(date -u +%T) releasing $(sort -u "$HELD" | wc -l | tr -d ' ') held jobs"
for id in $(sort -u "$HELD"); do $Q resume "$id" >/dev/null 2>&1; done
echo "$(date -u +%T) done ok=$ok"
