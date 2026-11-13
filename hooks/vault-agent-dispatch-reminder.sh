#!/bin/bash
# PostToolUse hook on the Agent tool. Two jobs.
#
# JOB 1 -- remind the model to log every dispatch to Claude/Agent-Dispatch-Log-YYYY-MM.md
# (monthly file) in the same turn. A pure-behavioral version of this rule failed 6 times in a
# row (see memory/feedback_vault_writes_not_happening.md).
#
# JOB 2 -- mark the session DURABLE for the Stop gate in
# vault-staleness-reminder.sh. The gate only fires when something durable
# happened and nothing reached the vault, and its only durable signal was
# `git commit`/`git push` in vault-write-tracker.sh. That misses whole
# categories: on 2026-08-25 the two most valuable results of the session -- a
# claim-vs-verify calibration that found a fabricated completion, and a
# falsified network hypothesis -- involved no commit at all, so the gate could
# never have fired on either. Dispatching an agent is a mechanical, unambiguous
# signal that real work happened; it needs no judgement and no LLM to decide.
input="$(cat)"

STATE_DIR="$HOME/.claude/state"
mkdir -p "$STATE_DIR" 2>/dev/null
sid="$(printf '%s' "$input" | jq -r '.session_id // empty' 2>/dev/null | tr -cd 'A-Za-z0-9._-')"
[ -n "$sid" ] && date +%s > "$STATE_DIR/vault-sess-$sid-durable" 2>/dev/null

# JOB 1 retired 2026-11-13: ~/bin/dispatch-log-hook.py (PostToolUse Agent + SubagentStop, async)
# now writes the dispatch-log line itself, so this hook no longer asks the model to.
echo '{}'
