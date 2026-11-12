#!/bin/bash
# SessionStart: surface SHIPPED-BUT-UNVALIDATED fixes -- ones whose correctness
# only a live run can confirm (a scraper that must read a real page, a dashboard
# header validated "on the next sync"). Sibling of session-start-handoff-panel.sh:
# the handoff board tracks a fix to merge, this tracks it to its LIVE check.
#
# FAILED items (a shipped fix that live data proved wrong) are the loud case and
# always surface. Pending items are a gentle reminder. Zero open items -> silent,
# no noise. Read-only: the ledger's `list --json` writes nothing.
# Degrades to silence on any error -- it must never throw into a session.
set -euo pipefail
command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$HOME/bin/live-validation-ledger.py" ] || exit 0

msg="$(python3 "$HOME/bin/live-validation-ledger.py" list --json 2>/dev/null | python3 -c '
import sys, json
try:
    d = json.load(sys.stdin)
    op = d.get("open") or []
    failed  = [i for i in op if i.get("status") == "failed"]
    pending = [i for i in op if i.get("status") in ("pending", "pending-fix")]
    if not failed and not pending:
        sys.exit(0)  # silent
    lines = ["LIVE-VALIDATION: %d pending, %d FAILED (python3 ~/bin/live-validation-ledger.py list; mark with pass|fail <id> --note)." % (len(pending), len(failed))]
    for i in failed[:3]:
        lines.append("  FAILED [%s] %s (%s): %s" % (i.get("id",""), i.get("label",""), i.get("repo",""), str(i.get("what",""))[:160]))
    print("\n".join(lines))
except SystemExit:
    raise
except Exception:
    pass  # any parse trouble -> emit nothing
' 2>/dev/null || true)"

# No open items (or any failure above) -> stay silent.
[ -n "$msg" ] || exit 0

jq -n --arg m "$msg" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $m
  }
}'
exit 0
