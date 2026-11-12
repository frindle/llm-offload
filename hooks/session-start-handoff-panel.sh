#!/bin/bash
# SessionStart: surface the ollama handoff/awaiting-action panel backlog at the
# TOP of the session so it gets folded into planning, not deferred to Stop.
# The owner 2026-09-09: "we need to ensure you remember" — front-loading the nudge is
# the forcing function (the handoff-passdown SessionStart inject is what actually
# gets acted on). Read-only: handoff-emit.py --json writes nothing.
# Fires whenever there is ANY actionable backlog (real dispatches always count;
# eval cr: arms only past a threshold so a routine bench run doesn't nag).
set -euo pipefail

# DIAGNOSIS-ONLY HEADLESS SESSION -> self-skip.
# THE BUG THIS FIXES (2026-09-24): dispatch-escalation-watcher.py spawns its
# integration/escalation reviews as a headless `claude -p` from launchd, read-only
# --allowedTools, nobody at the keyboard. This hook fired there too and told that
# agent to "process the handoff panel EARLY ... then clear the processed with
# handoff-emit.py --acted", which it can neither approve nor execute. At least 2 of
# the ~18 integration reviews that night came back "I could not process the handoff
# panel this session" INSTEAD of the diagnosis they were spawned to produce -- a whole
# review round trip lost to a nudge aimed at a human.
# Any hook that asks for an interactive action self-skips when this marker is set.
if [ -n "${CLAUDE_HEADLESS_DIAGNOSIS:-}" ]; then exit 0; fi

command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$HOME/bin/handoff-emit.py" ] || exit 0

read -r total real <<<"$(python3 "$HOME/bin/handoff-emit.py" --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    comp = [j for j in d.get("complete", []) if j.get("status") in ("done", "failed")]
    real = [j for j in comp if not str(j.get("label","")).startswith("cr:") or j.get("awaiting_signoff")]
    print(len(comp), len(real))
except Exception:
    print(0, 0)
' 2>/dev/null || echo "0 0")"
case "$total" in ''|*[!0-9]*) total=0 ;; esac
case "$real"  in ''|*[!0-9]*) real=0 ;; esac

# PIPELINE E2E (2026-10-06): a pipeline edit with no green canary after it is a
# visible FAIL here, not a silent skip. --status is read-only and takes <1s.
e2e=""
if [ -f "$HOME/bin/pipeline-canary.py" ]; then
  e2e="$(python3 "$HOME/bin/pipeline-canary.py" --status 2>/dev/null)" && e2e=""
  e2e="$(printf '%s\n' "$e2e" | cut -c1-230 | head -2)"  # step4 trim: one-line-ish; full text via the command
fi

# Surface if there is any non-eval work awaiting action, or the eval pile is large.
panel=""
if [ "$real" -ge 1 ] || [ "$total" -ge 15 ]; then
  panel="HANDOFF PANEL: $total unprocessed ($real real). See: python3 ~/bin/handoff-emit.py --json (clear with --acted; never blanket-clear awaiting_signoff)."
fi
if [ -n "$panel" ] || [ -n "$e2e" ]; then
  jq -n --arg p "$panel" --arg e "$e2e" '{
    hookSpecificOutput: {
      hookEventName: "SessionStart",
      additionalContext: ([$e, $p] | map(select(. != "")) | join("\n"))
    }
  }'
fi
exit 0
