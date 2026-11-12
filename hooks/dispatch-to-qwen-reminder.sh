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
input="$(cat)"
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
    additionalContext: ("DISPATCH REMINDER: just edited " + $path + " directly. Standing rule (memory feedback_dispatch_coding_to_goose): UFN, qwen should write coding work by default, not Claude -- was this edit actually exempt (physical/hardware step, applying an already-reviewed qwen fix, a genuine one-liner, or the dispatch mechanism itself), or should this have gone through qwen-dispatch.sh first? (Advisory size rule of thumb, never a block: delegation costs ~600-900 cloud tokens of overhead, so one-liners and edits under ~40 lines are cheaper done directly; anything bigger dispatches.) If it was not exempt and nothing was dispatched, say so to the user rather than letting it pass silently.")
  }
}'
