#!/bin/bash
# Launch the Unraid-hosted Goose chat front end.
#
# operator 2026-10-01 (supersedes the chat-through-the-queue build; see
# goose-darkbloom.sh.bak-unraidchat):
#   CHAT  -> Unraid Ollama, hard-bound, model kept warm, num_ctx pinned to the pregate's
#            confirmed-safe value. Never queues behind Studio work.
#   WORK  -> still 100% through ~/bin/ollama-queue.py, dispatched FROM the chat by the
#            `queue_dispatch` tool in ~/bin/goose-queue-mcp.py. Every investigation /
#            code change / research task is still a studio-db dashboard row.
#
#   goose -> ~/bin/goose-queue-proxy.py (127.0.0.1:8011) -> <ollama.unraid_url>/api/chat
#         \-> ~/bin/goose-queue-mcp.py (stdio MCP) -> ollama-queue.py enqueue -> studio-db
#
# The name is kept for muscle memory and so nothing that calls it breaks. This script
# still reads NO Darkbloom API key -- the key rotates on every Darkbloom restart and
# nothing in the chat path needs it now that chat does not touch Darkbloom at all.
# ~/.darkbloom/local.json is untouched; the queue's own runners still use it.
#
# Only ENV is overridden here; ~/.config/goose/config.yaml holds the trimmed extension
# set (goose reads env before the config file, so OPENAI_HOST here wins).
set -u

PORT="${GOOSE_QUEUE_PORT:-8011}"
PROXY="$HOME/bin/goose-queue-proxy.py"
# Which chat backend. Validated HERE as well as in the proxy: a typo would otherwise
# start a proxy that binds the port and then 503s every turn, and the only clue would be
# in a log file. No default-on-typo -- that is the silent fallback this design forbids.
BACKEND="${GOOSE_CHAT_BACKEND:-darkbloom}"
case "$BACKEND" in
  darkbloom|unraid) ;;
  *)
    echo "[goose-chat] GOOSE_CHAT_BACKEND='$BACKEND' is not a backend." >&2
    echo "[goose-chat] Use 'darkbloom' (Qwen3.5-9B, local provider) or 'unraid'" >&2
    echo "[goose-chat] (qwen3:14b @ num_ctx 6144). Refusing to pick one for you." >&2
    exit 2
    ;;
esac
export GOOSE_CHAT_BACKEND="$BACKEND"
RUNDIR="$HOME/.cache/goose-queue"
PIDFILE="$RUNDIR/proxy.pid"
LOGFILE="$RUNDIR/proxy.log"
mkdir -p "$RUNDIR"

started_proxy=0

listening() {
  # Loopback-only check, no extra deps: python can tell us if the port answers.
  python3 - "$PORT" <<'EOF' >/dev/null 2>&1
import socket, sys
s = socket.socket()
s.settimeout(0.75)
try:
    s.connect(("127.0.0.1", int(sys.argv[1])))
finally:
    s.close()
EOF
}

stop_proxy() {
  # Stop the proxy WITH goose -- but only the one we started. If the port was already
  # answering when we launched (another goose session, or a hand-started proxy), leave
  # it alone: killing it would break that session.
  if [ "$started_proxy" = "1" ] && [ -f "$PIDFILE" ]; then
    pid="$(cat "$PIDFILE" 2>/dev/null)"
    if [ -n "${pid:-}" ]; then
      kill "$pid" 2>/dev/null
    fi
    rm -f "$PIDFILE"
  fi
}
trap stop_proxy EXIT INT TERM

if listening; then
  echo "[goose-unraid-chat] chat proxy already listening on 127.0.0.1:$PORT" >&2
else
  echo "[goose-unraid-chat] starting chat proxy on 127.0.0.1:$PORT (log: $LOGFILE)" >&2
  nohup python3 "$PROXY" --port "$PORT" --backend "$BACKEND" --pidfile "$PIDFILE" >>"$LOGFILE" 2>&1 &
  started_proxy=1
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    listening && break
    sleep 0.25
  done
  if ! listening; then
    echo "[goose-unraid-chat] FAILED to start the chat proxy; see $LOGFILE" >&2
    echo "[goose-unraid-chat] refusing to launch goose: it would bypass the ctx clamp." >&2
    exit 1
  fi
fi

# Point goose at the proxy. Not at Unraid directly: Ollama's own OpenAI shim cannot be
# given num_ctx or keep_alive, which are the two settings this whole setup turns on.
export OPENAI_HOST="http://127.0.0.1:$PORT"
export OPENAI_BASE_PATH="v1/chat/completions"
# The proxy needs no credential (plain Ollama, on the LAN, no auth), but goose's openai
# provider refuses to start without one.
export OPENAI_API_KEY="unraid-ollama-no-key-needed"
# The model name and context budget goose ADVERTISES/PLANS against, per backend. The
# proxy ignores the client's model id and serves its own hard-bound one, but keeping
# them equal stops goose's UI lying about which model is answering.
#
# GOOSE_CONTEXT_LIMIT must stay at or under what the backend actually serves. Go over it
# and goose cheerfully builds a prompt the server truncates from the FRONT -- silently
# dropping the system prompt and the tool definitions, leaving a model that has
# forgotten it can dispatch. That failure looks like "the model got dumb", not like a
# config error, which is why it is pinned here rather than left to a default.
case "$BACKEND" in
  darkbloom)
    # Qwen3.5-9B on the local provider. MEASURED 2026-10-01 (queued ctxprobe-9b-* jobs):
    # processes >=149,574 prompt tokens without truncating, exact recall verified at
    # 18.5k / 36.9k / 74.8k at every needle depth. 32768 is therefore a comfortable
    # BUDGET, not the model's limit.
    CHAT_MODEL="${GOOSE_CHAT_DARKBLOOM_MODEL:-Qwen3.5-9B}"
    DEFAULT_CTX=32768
    ;;
  unraid)
    # qwen3:14b at the pregate's UNRAID_CONFIRMED_SAFE_CTX value. 6144 is a HARD GPU
    # limit (12GB 3080, ~10.3GB usable, no SSH to recover an OOM), so the budget leaves
    # room for the answer rather than filling the window.
    CHAT_MODEL="${GOOSE_UNRAID_MODEL:-qwen3:14b}"
    DEFAULT_CTX="${GOOSE_UNRAID_NUM_CTX:-6144}"
    ;;
esac
export GOOSE_PROVIDER="openai"
export GOOSE_MODEL="$CHAT_MODEL"
export GOOSE_CONTEXT_LIMIT="${GOOSE_CONTEXT_LIMIT:-$DEFAULT_CTX}"
export GOOSE_INPUT_LIMIT="${GOOSE_INPUT_LIMIT:-$GOOSE_CONTEXT_LIMIT}"
# A warm model answers an interactive turn in seconds; a cold one pays a load first.
# Generous but finite -- a hang should surface, not look like thinking.
export OPENAI_TIMEOUT="${OPENAI_TIMEOUT:-900}"
export GOOSE_STREAM_TIMEOUT="${GOOSE_STREAM_TIMEOUT:-900}"

echo "[goose-chat] backend=$BACKEND model=$CHAT_MODEL context=$GOOSE_CONTEXT_LIMIT" >&2

exec /opt/homebrew/bin/goose "$@"
