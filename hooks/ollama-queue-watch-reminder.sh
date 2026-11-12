#!/bin/bash
# Nudges the session to arm its queue watcher: the Monitor tool running
# `queue-events-deliver.py --watch`, the only path that wakes an IDLE session when one
# of ITS queue jobs passes/fails/blocks/asks (queue-events-inject.sh covers active
# sessions at every tool call). The watcher exits by itself ~3 min after none of the
# session's jobs is in flight, so it is armed per batch of work, not kept alive forever:
#   - SessionStart (incl. /clear, which keeps the process and so its jobs): nudge only
#     if this session has jobs in flight.
#   - PostToolUse(Bash): nudge after a submit (ollama-queue.py enqueue / mini-submit).
# It only NUDGES (additionalContext) -- arming the Monitor is a tool call only the
# model can make; a hook cannot call it.
# Prior version backed up in ~/.ollama-dispatch/backups/queue-events-20261102/.
input="$(cat)"
event="$(printf '%s' "$input" | jq -r '.hook_event_name // empty')"

# DIAGNOSIS-ONLY HEADLESS SESSION -> self-skip (2026-09-24): a headless `claude -p`
# review runs with read-only --allowedTools and cannot arm a Monitor.
if [ -n "${CLAUDE_HEADLESS_DIAGNOSIS:-}" ]; then exit 0; fi

ARM_MSG='OLLAMA QUEUE WATCHER: this session has queue jobs in flight. Unless a watcher is already running in this session, arm the Monitor tool (timeout_ms 1800000, description "queue events for this session") with command:
python3 ~/bin/queue-events-deliver.py --watch
It prints one line per passed/done/failed/blocked/needs_info/gate_nonpass event for jobs THIS session launched (plus orphaned/unstamped ones nobody has seen), and exits by itself ~3 min after none of your jobs is in flight. On a 30-min expiry re-arm only if `python3 ~/bin/queue-events-deliver.py --inflight` prints a number above 0. On a failed row: triage, then run `python3 ~/bin/ollama-queue.py resolve <id>` and `curl -s -X POST http://127.0.0.1:7684/api/runs/<id>/clear` in the same step.'

emit() {
  jq -Rn --arg ev "$1" --arg msg "$ARM_MSG" '{
    hookSpecificOutput: { hookEventName: $ev, additionalContext: $msg }
  }'
}

case "$event" in
  SessionStart)
    n="$(python3 ~/bin/queue-events-deliver.py --inflight 2>/dev/null)"
    case "$n" in ''|0) ;; *) emit "SessionStart" ;; esac
    ;;
  PostToolUse|PreToolUse)
    cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"
    case "$cmd" in
      *ollama-queue.py*enqueue*|*mini-submit*) emit "$event" ;;
    esac
    ;;
esac
exit 0
