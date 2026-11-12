#!/bin/bash
# SessionStart panel: how many failure signatures need triage. Read-only, fail-open, <1s.
# Mirrors session-start-handoff-panel.sh. Draft settings snippet: ~/.ollama-dispatch/proposals/triage/
if [ -n "${CLAUDE_HEADLESS_DIAGNOSIS:-}" ]; then exit 0; fi
command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$HOME/bin/triage-emit.py" ] || exit 0
read -r n rep <<<"$(python3 "$HOME/bin/triage-emit.py" --json 2>/dev/null | python3 -c '
import sys, json
try:
    s = json.load(sys.stdin)["summary"]; print(s["open"], s["repeats"])
except Exception:
    print(0, 0)
' 2>/dev/null || echo "0 0")"
case "$n" in ''|*[!0-9]*) n=0 ;; esac
case "$rep" in ''|*[!0-9]*) rep=0 ;; esac
[ "$n" -ge 1 ] || exit 0
msg="FAILURE TRIAGE PANEL: $n failure signature(s) need triage ($rep repeated; oldest, repeats first): python3 ~/bin/triage-emit.py --json ; then show <id>. Fix the root cause per the packet's REQUIRED FIX STANDARD, then close it with triage-emit.py --acted <id> --outcome FIXED|ESCALATED|WONTFIX --reason \"...\"."
python3 -c 'import json,sys; print(json.dumps({"hookSpecificOutput":{"hookEventName":"SessionStart","additionalContext":sys.argv[1]}}))' "$msg"
exit 0
