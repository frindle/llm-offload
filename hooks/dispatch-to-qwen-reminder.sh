#!/bin/bash
# PostToolUse hook on Edit|Write. Fires when Claude edits a real code file
# directly, to prompt a check: should this have gone to qwen first?
#
# WHY: standing project rule (memory feedback_dispatch_coding_to_goose,
# "UFN lets have qwen do everything we can have it do") kept getting
# overridden by Claude's own in-the-moment confidence -- caught doing this
# directly at least twice in one session (a ttyd/Cloudflare plist setup, then
# a Rivian session-length constant) despite the standing rule, each time only
# because the owner asked "would you have dispatched that if I didn't remind you?"
# A memory-based rule only works if Claude remembers to apply it; a hook fires
# regardless. This is a NUDGE, not a block -- plenty of edits are legitimately
# exempt (physical/hardware steps, applying an already-reviewed qwen-produced
# fix, genuine one-liners, orchestration/dispatch-mechanism code itself where
# the bootstrapping is awkward) and Claude's own judgment still decides.
# 2026-11-13: routing text changed by ~/bin/install-coding-route.py (issue-driven fixes -> Sonnet; local
# model only with a check that exists before the job). Undo: python3 -I ~/bin/install-coding-route.py --uninstall
input="$(cat)"
# subagent guard: these reminders are for the main session only
[ -n "$(printf '%s' "$input" | jq -r '.agent_id // empty' 2>/dev/null)" ] && exit 0
path="$(printf '%s' "$input" | jq -r '.tool_input.file_path // .tool_response.filePath // empty')"

[ -n "$path" ] || exit 0

case "$path" in
  /tmp/*|/private/tmp/*|*/scratchpad/*) exit 0 ;;
esac

case "$path" in
  *.ts|*.tsx|*.js|*.jsx|*.py|*.sh|*.swift|*.go|*.rs|*.rb) ;;
  *) exit 0 ;;
esac

jq -n --arg path "$path" '{
  hookSpecificOutput: {
    hookEventName: "PostToolUse",
    additionalContext: ("ROUTING REMINDER: just edited " + $path + " directly. Coding routing since 2026-11-13 (measured; see ~/Desktop/Claude Plans/Local LLM Usage Plan.md): (1) a bug fix that starts from an issue, an error report or a symptom goes to a Sonnet agent (fixer-sonnet, or the ladder skill), not to the local model: on these projects a local fix cost at least $5.95 of Claude per landed fix vs about $2.3-3.8 via Sonnet, and about 3 in 10 local passes hid a real bug; (2) the local model (qwen-dispatch.sh) gets only work whose check exists before the job: an existing failing test to make pass, lint, type or format errors, or a refactor that existing tests cover; every 5th local pass gets a spot audit, and if more than 1 in 20 audited passes has a real bug, stop using the lane and tell the user; (3) edits under about 40 lines are cheaper done directly. Was this edit one of those? If it should have gone to Sonnet or the local lane, say so to the user rather than letting it pass silently.")
  }
}'
