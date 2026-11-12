#!/bin/bash
# Stops the v6 Mac Studio leg cleanly once deepseek-r1:14b has finished BOTH
# of its tasks. Stop only -- it does NOT launch v6.1. The relaunch is manual,
# after Fable signs off on v6.1.
#
# Why a watcher rather than just killing v6 now: killing mid-model would waste
# the ~15 minutes deepseek-r1:14b has already spent and leave a half-measured
# model. Waiting for its clamshell DONE line means the last thing v6 records
# is a complete model.
#
# The race: after that DONE line the driver immediately starts the next
# model's preflight (npm run build, ~10-30s) and then its worker. So this
# kills the driver first, then any worker that already started. A worker
# killed mid-run writes NO CSV row (the row is written after `wait`), so the
# worst case is a partially-run model with no row -- clean, and v6.1 re-runs
# everything anyway.
#
# It also kills bakeoff-v6-devstral-llamaserver.sh, which is blocked forever
# waiting for a "v6 MACSTUDIO COMPLETE" marker that will now never be written.
# devstral is covered by bakeoff-v61-devstral-llamaserver.sh instead.
set -uo pipefail

OUTDIR="/Users/user/Desktop/GitHub Projects/bakeoff/model-buildoff-2026-08-22"
DRIVER_LOG="$OUTDIR/driver.log"
MARKER="DONE: deepseek-r1:14b / clamshell-confirmation-bridge"

echo "[v6-stopper] armed @ $(date '+%H:%M:%S') -- waiting for: $MARKER" >> "$DRIVER_LOG"

while ! grep -aq "$MARKER" "$DRIVER_LOG"; do sleep 15; done

echo "[v6-stopper] marker seen @ $(date '+%H:%M:%S') -- stopping v6" >> "$DRIVER_LOG"

# Drivers first, so nothing new is started while we clean up.
pkill -f "bakeoff-v6-macstudio.sh"           2>/dev/null
pkill -f "bakeoff-v6-devstral-llamaserver.sh" 2>/dev/null
sleep 2

# Then any Mac Studio worker the driver managed to start in the race window.
# Scoped to --host http://localhost so Unraid workers are never touched.
pkill -f "ollama-worker.*--host http://localhost" 2>/dev/null
sleep 2

echo "[v6-stopper] v6 stopped. alive now: $(pgrep -f 'bakeoff-v6-|ollama-worker' | tr '\n' ' ')" >> "$DRIVER_LOG"
echo "=== v6 HALTED AFTER deepseek-r1:14b @ $(date '+%Y-%m-%d %H:%M:%S') -- v6.1 relaunch is manual ===" >> "$DRIVER_LOG"
