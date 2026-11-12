#!/bin/bash
# Drain the queue (sticky-hold every pending job), restart the daemon once nothing is running, release what we held.
# Usage: queue-drain-restart.sh   (log: /tmp/drain-restart.log)
Q="python3 $HOME/bin/ollama-queue.py"
HELD=/tmp/drain-held.ids; : > "$HELD"
echo "$(date -u +%T) drain start"
zero=0
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
echo "$(date -u +%T) idle; restarting daemon"
launchctl kickstart -k "gui/$(id -u)/com.example.ollama-queue-daemon"
sleep 20
echo "$(date -u +%T) releasing $(sort -u "$HELD" | wc -l | tr -d ' ') held jobs"
for id in $(sort -u "$HELD"); do $Q resume "$id" >/dev/null 2>&1; done
echo "$(date -u +%T) done"
