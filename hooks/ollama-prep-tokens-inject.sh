#!/bin/bash
# PreToolUse hook on Bash. Auto-injects --claude-prep-tokens into an
# ollama-worker.py dispatch that's missing it, computed via
# claude-token-cursor.py --since-checkpoint, BEFORE the command runs.
#
# WHY THIS REPLACES ollama-prep-tokens-reminder.sh (a PostToolUse hook):
# a reminder that fires AFTER dispatch is structurally too late -- by then
# the prep work already happened with no delta captured, and the flag can't
# be retroactively added to a command that already ran. Confirmed live
# 2026-08-28: the reminder fired correctly on every real dispatch all night
# and the flag was still never once actually used -- a correct-but-late
# nudge doesn't fix a missing-input problem. This fixes it structurally
# instead of relying on the reminder being acted on: PreToolUse hooks can
# rewrite the tool's input before it runs (hookSpecificOutput.updatedInput),
# so the flag gets added automatically, with no dependence on Claude
# remembering anything.
input="$(cat)"
cmd="$(printf '%s' "$input" | jq -r '.tool_input.command // empty')"

[ -n "$cmd" ] || exit 0

# Must be an actual invocation (python3/python immediately before a path
# ending in ollama-worker.py), not just the filename appearing as an
# argument to something else -- confirmed live 2026-08-28: a plain `grep
# ... ~/bin/ollama-worker.py | head -10` matched the old *ollama-worker.py*
# glob and got --claude-prep-tokens appended to `head`, breaking it
# ("head: unrecognized option"). This hook REWRITES the command, so a false
# positive here breaks real commands, not just prints a harmless note.
if [[ ! "$cmd" =~ python3?[[:space:]]+[^[:space:]]*ollama-worker\.py ]]; then
  exit 0
fi

case "$cmd" in
  *--claude-prep-tokens*) exit 0 ;;
esac

# Only inject when the ollama-worker.py invocation is the last thing in the
# command -- confirmed live 2026-08-28: a command like
# `... python3 ollama-worker.py ... & disown && echo done` got the flag
# appended to the very END of the whole string, landing on `echo` instead
# of the actual dispatch (harmless that time -- echo just printed extra
# text -- but the same blind-append into an ambiguous tail could corrupt a
# real trailing command). Rather than parse shell syntax to find the exact
# right insertion point, refuse to inject whenever anything looks like it
# follows the invocation: any of & ; | anywhere in the tail, or any line
# after the first that isn't a continued CLI flag (doesn't start with
# optional whitespace then a dash). A missed injection is fine (that one
# dispatch just has no prep-token comparison data); a corrupted command is
# not.
tail="${cmd#*ollama-worker.py}"
if [[ "$tail" == *"&"* || "$tail" == *";"* || "$tail" == *"|"* ]]; then
  exit 0
fi
is_safe=1
first_line=1
while IFS= read -r line; do
  if [ "$first_line" = 1 ]; then
    first_line=0
    continue  # first line is the tail end of the invocation's own arg list
  fi
  case "$line" in
    ""|[[:space:]]*-*) ;;  # blank, or whitespace then a flag -- fine
    *) is_safe=0; break ;;
  esac
done <<< "$tail"
[ "$is_safe" = 1 ] || exit 0

delta="$(python3 ~/bin/claude-token-cursor.py --since-checkpoint 2>/dev/null)"
case "$delta" in
  ''|*[!0-9]*) exit 0 ;;  # cursor script failed or gave non-numeric output -- don't inject garbage
esac

new_cmd="${cmd} \\
  --claude-prep-tokens ${delta}"

jq -n --arg cmd "$new_cmd" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    updatedInput: { command: $cmd }
  }
}'
