#!/bin/bash
end=$(( $(date +%s) + 10800 ))
while [ "$(date +%s)" -lt "$end" ]; do
  n=$(python3 "$HOME/bin/ollama-queue.py" status 2>/dev/null | grep -c '^\[running')
  if [ "$n" = "0" ]; then
    sleep 4
    n2=$(python3 "$HOME/bin/ollama-queue.py" status 2>/dev/null | grep -c '^\[running')
    if [ "$n2" = "0" ]; then
      launchctl kickstart -k "gui/$(id -u)/com.example.ollama-queue-daemon" && echo "RESTARTED $(date)"
      exit 0
    fi
  fi
  sleep 5
done
echo "GAVE UP $(date)"
