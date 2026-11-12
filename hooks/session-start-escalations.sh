#!/bin/bash
# SessionStart: surface OPEN DISPATCH ESCALATIONS -- slices/jobs the scheduled
# watcher (com.example.dispatch-escalation-watcher) parked as stuck, together with
# the headless review verdict it already produced.
#
# WHY THIS EXISTS. The watcher runs from launchd, so a stuck slice is detected,
# recorded and diagnosed even with ZERO sessions open (that is the overnight
# case). But a file nobody opens is not a notification. This hook is the other
# half: the next session to start -- at any hour, with nothing to re-arm -- is
# told what is open and what the review already concluded.
#
# Sibling of session-start-live-validation.sh, same contract: zero open items ->
# completely silent, read-only, degrades to silence on any error, never throws
# into a session.
set -euo pipefail

# Homebrew bash 5.3 hangs on this script's heredocs (confirmed 2026-10-02: 0.03s under
# /bin/bash, >15s under /opt/homebrew/bin/bash; hung copies piled up for 6+ hours).
# Re-run under the system bash whenever we were started by another one.
if [ "${BASH:-}" != "/bin/bash" ] && [ -x /bin/bash ] && [ -z "${ESC_HOOK_REEXEC:-}" ]; then
  ESC_HOOK_REEXEC=1 exec /bin/bash "$0" "$@"
fi

# DIAGNOSIS-ONLY HEADLESS SESSION -> self-skip (2026-09-24). The escalation review
# this marker flags is spawned BY the watcher that writes ESCALATIONS.md, so injecting
# the open-escalation backlog into it hands the review a list containing the very item
# it was spawned to diagnose, plus ~17 others it must not touch. Nothing here is
# actionable without a human, and the recursion invites the agent to start triaging
# the queue instead of answering its one assigned question.
if [ -n "${CLAUDE_HEADLESS_DIAGNOSIS:-}" ]; then exit 0; fi

IDX="$HOME/.ollama-dispatch/escalations/ESCALATIONS.md"
READY="$HOME/.ollama-dispatch/escalations/READY-TO-LAND.md"
command -v jq >/dev/null 2>&1 || exit 0

# LAND QUEUE (2026-10-09, ollama-land): finished chains that already have a computed landing
# packet and wait for FINAL REVIEW by the main session. Read-only (the tool never merges from
# here); silent when nothing waits or when the tool is not installed.
land="$(python3 "$HOME/bin/ollama-land" summary 2>/dev/null || true)"

if [ ! -f "$IDX" ] && [ ! -f "$READY" ]; then
  [ -n "$land" ] || exit 0
  jq -n --arg m "$land" '{hookSpecificOutput: {hookEventName: "SessionStart", additionalContext: $m}}'
  exit 0
fi

msg="$(python3 - "$IDX" "$READY" <<'PY' 2>/dev/null || true
import sys, pathlib
def open_rows(path):
    try:
        return [l for l in pathlib.Path(path).read_text().splitlines()
                if l.startswith("- [ ] ")]
    except Exception:
        return []
p = pathlib.Path(sys.argv[1])
rows = open_rows(p)
ready = open_rows(sys.argv[2])
if not rows and not ready:
    sys.exit(0)
import re
URG = re.compile(r"RED|URGENT|CRITICAL|FAIL", re.I)
out = []
if rows:
    out.append("DISPATCH ESCALATIONS: %d open (index: %s; review verdicts are on the rows, diagnosis only)." % (len(rows), p))
    for r in [r for r in rows if URG.search(r)][-3:]:
        out.append("  URGENT " + r[len("- [ ] "):][:200])
if ready:
    out.append("READY TO LAND: %d staged chain(s) (%s)." % (len(ready), sys.argv[2]))
    for r in [r for r in ready if URG.search(r)][-3:]:
        out.append("  URGENT " + r[len("- [ ] "):][:200])
print("\n".join(out))
PY
)"

if [ -n "$land" ]; then
  msg="${land}${msg:+

}${msg}"
fi
[ -n "$msg" ] || exit 0

jq -n --arg m "$msg" '{
  hookSpecificOutput: {
    hookEventName: "SessionStart",
    additionalContext: $m
  }
}'
exit 0
