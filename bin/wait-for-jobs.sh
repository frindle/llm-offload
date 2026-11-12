#!/bin/bash
# Usage: wait-for-jobs.sh JOBID [JOBID...]  -- exits when ALL named jobs have left pending/running/held.
# Costs no tokens: run with run_in_background so the session is re-invoked on exit.
while true; do
  live=0
  for id in "$@"; do
    st=$(python3 ~/bin/ollama-queue.py status 2>/dev/null | sed -nE "s/^\[([a-z_]+)[[:space:]]*\][[:space:]]+${id}[[:space:]].*/\1/p" | head -1)
    case "$st" in running|pending|scheduled|held|queued) live=1 ;; esac
  done
  [ "$live" -eq 0 ] && break
  sleep 60
done
for id in "$@"; do python3 ~/bin/ollama-queue.py status 2>/dev/null | grep -E "^\[[a-z_ ]+\][[:space:]]+${id}" | cut -c1-200; done
