#!/bin/bash
# PostToolUse hook on Bash. Fires when a Bash command dispatches
# ~/bin/ollama-worker.py without --claude-prep-tokens, to catch a missing
# data point before it silently accumulates.
#
# WHY: added 2026-08-28 alongside claude-token-cursor.py and ollama-worker.py's
# --claude-prep-tokens flag, which together let dispatch-metrics.jsonl compare
# Claude's own prep-token cost against the local model's generation-token cost
# per dispatch -- but the flag requires manually running claude-token-cursor.py
# before and after prep and computing a delta, which is easy to forget (same
# failure mode as the qwen-dispatch-reminder hook: a memory-based rule only
# works if Claude remembers to apply it every time; a hook fires regardless).
# This is a NUDGE, not a block -- a dispatch without the flag still runs and
# still gets logged, just without that one comparison field for this run.
input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

[ -n "$cmd" ] || exit 0

case "$cmd" in
  *ollama-worker.py*) ;;
  *) exit 0 ;;
esac

case "$cmd" in
  *--claude-prep-tokens*) exit 0 ;;
esac

jq -n '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: "TOKEN-TRACKING REMINDER: just dispatched ollama-worker.py without --claude-prep-tokens. This dispatch will still log to dispatch-metrics.jsonl on convergence, but without a Claude-side prep-token figure to compare against the local model'\''s generation tokens for this task. Run claude-token-cursor.py before starting prep and again right before dispatching, pass the delta as --claude-prep-tokens next time -- unless this dispatch genuinely had no meaningful Claude-side investigation/prep to measure (e.g. a trivial re-dispatch or retry of an already-prepped task)."
  }
}'
