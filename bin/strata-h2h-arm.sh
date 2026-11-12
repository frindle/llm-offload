#!/bin/bash
# strata-h2h-arm.sh <cell> -- the Strata arm of the v12 head-to-head, ONE cell (debug|bulk).
# Only ever the --cmd of an `ollama-queue.py enqueue-gpu` job on --host unraid: the
# GPU-exclusive runner has already evicted Unraid Ollama and checked the card's VRAM;
# strata-serve.sh's own guard re-measures it (and records what was resident) before load.
#
#   1. fetch the Strata API key into a 0600 file in a private run dir (never argv/echo)
#   2. ssh claude-sandbox strata-serve.sh start   (guard -> budgeted run config -> /health)
#   3. ssh tunnel 127.0.0.1:18180 -> sandbox 127.0.0.1:18080 (the server binds loopback)
#   4. bakeoff-v12-strata-h2h.sh with H2H_ARM=strata
#   5. ALWAYS (trap): tunnel down, server stop, key file removed, remote run dir copied back
# Exit = the driver's (0 complete, 4 incomplete -> re-enqueue resumes), 1 on setup failure.
set -uo pipefail
CELL="${1:-}"
case "$CELL" in debug|bulk) ;; *) echo "usage: $0 debug|bulk" >&2; exit 2 ;; esac
[ -n "${OLLAMA_DISPATCH_VIA_QUEUE:-}" ] || [ "${H2H_DRY_RUN:-0}" = "1" ] || {
  echo "ABORT: run this only through ollama-queue.py enqueue-gpu" >&2; exit 1; }

BAKEOFF="/Users/user/Desktop/GitHub Projects/bakeoff"
DRIVER="$BAKEOFF/bakeoff-v12-strata-h2h.sh"
OUT="$BAKEOFF/model-buildoff-2026-08-22/strata-h2h-runs"
TS=$(date -u +%Y%m%dT%H%M%SZ)
RUN="$OUT/$TS-$CELL"; RRUN="strata-h2h/$TS-$CELL"     # remote dir is relative to sandbox $HOME
PORT_L=18180
BUDGET="${STRATA_REASONING_BUDGET:-12288}"
SSH=(ssh -o BatchMode=yes -o ConnectTimeout=20 -o ServerAliveInterval=30 claude-sandbox)
umask 077
mkdir -p "$RUN"
KEYF="$RUN/.strata-key"; TPID=""
say() { echo "[$(date +%T)] $*" | tee -a "$RUN/arm.txt"; }

cleanup() {
  [ -n "$TPID" ] && kill "$TPID" 2>/dev/null
  rm -f "$KEYF"
  "${SSH[@]}" "STRATA_RUN_DIR=\$HOME/$RRUN ~/strata/strata-serve.sh stop" >> "$RUN/arm.txt" 2>&1
  scp -q -o BatchMode=yes -r "claude-sandbox:$RRUN/." "$RUN/remote/" 2>>"$RUN/arm.txt" || say "WARN: could not copy remote run dir"
  # the run config copy holds the key -- never keep it on the Mac, delete it remotely too
  rm -f "$RUN/remote/strata-run.json"
  "${SSH[@]}" "rm -f \$HOME/$RRUN/strata-run.json" 2>/dev/null
  say "cleanup done"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

say "strata arm cell=$CELL budget=$BUDGET run=$RUN"
# 1. key -> file (stdout straight into the file; it never reaches a log or argv)
( "${SSH[@]}" 'cat ~/.config/strata/api_key' > "$KEYF" ) 2>>"$RUN/arm.txt"
[ -s "$KEYF" ] || { say "ABORT: could not fetch the Strata key (ssh claude-sandbox)"; exit 1; }
chmod 600 "$KEYF"

# 2. server (its guard writes preflight-guard.txt = what was resident on the card)
mkdir -p "$RUN/remote"
if ! "${SSH[@]}" "mkdir -p \$HOME/$RRUN && STRATA_RUN_DIR=\$HOME/$RRUN STRATA_REASONING_BUDGET=$BUDGET STRATA_WATCHDOG_S=${STRATA_WATCHDOG_S:-600} STRATA_PLE_IO=${STRATA_PLE_IO:-ram} STRATA_MAX_S=${STRATA_MAX_S:-14400} ~/strata/strata-serve.sh start" >> "$RUN/arm.txt" 2>&1; then
  say "ABORT: strata-serve.sh start failed (guard or load) -- see arm.txt / remote/preflight-guard.txt; last output: $(tail -n 3 "$RUN/arm.txt" | tr '\n' '|' | cut -c1-300)"
  exit 1
fi

# 3. tunnel
ssh -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -N \
    -L "127.0.0.1:$PORT_L:127.0.0.1:18080" claude-sandbox 2>>"$RUN/arm.txt" &
TPID=$!
for _ in $(seq 30); do curl -fs -m 5 "http://127.0.0.1:$PORT_L/health" >/dev/null 2>&1 && break; sleep 1; done
curl -fs -m 5 "http://127.0.0.1:$PORT_L/health" >/dev/null 2>&1 || { say "ABORT: tunnel to Strata not healthy"; exit 1; }
say "tunnel up 127.0.0.1:$PORT_L -> sandbox:18080"
# warm-up: the first request after load is cold (prefix prefill + expert paging) and outlasts the
# worker's 30 s tool-calling preflight; absorb it here so it never becomes a model result.
say "warm-up request (max 600 s)"
printf 'Authorization: Bearer %s\n' "$(cat "$KEYF")" > "$RUN/.hdr"; chmod 600 "$RUN/.hdr"
curl -s -m 600 -o /dev/null -H @"$RUN/.hdr" -H "Content-Type: application/json" \
  -d '{"model":"strata","messages":[{"role":"user","content":"Reply with the single word ready."}],"max_tokens":16}' \
  "http://127.0.0.1:$PORT_L/v1/chat/completions" >/dev/null 2>&1; WRC=$?; rm -f "$RUN/.hdr"; say "warm-up done rc=$WRC"

# 4. driver
H2H_ARM=strata H2H_CELLS="$CELL" H2H_ARM_URL="http://127.0.0.1:$PORT_L" H2H_KEY_FILE="$KEYF" \
  WORKER_PREFLIGHT_TIMEOUT_S="${WORKER_PREFLIGHT_TIMEOUT_S:-900}" STRATA_REASONING_BUDGET="$BUDGET" bash "$DRIVER" < /dev/null >> "$RUN/arm.txt" 2>&1
RC=$?
say "driver exit $RC"
exit "$RC"
