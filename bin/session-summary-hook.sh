#!/usr/bin/env bash
# session-summary-hook.sh — SessionEnd hook for the session indexer.
# Indexes ONLY the just-ended session into the live vault SessionIndex/.
# Claude Code passes SessionEnd data as JSON on stdin (session_id, transcript_path,
# cwd, ...). We derive --project-dir from transcript_path so this works for ANY
# project, not just github-projects (fixes the indexer's hardcoded default).
# Never blocks session teardown (always exits 0).
set -uo pipefail

INDEXER="$HOME/bin/session-summary-index.py"
LOG="$HOME/.claude/session-summary-index.log"
payload="$(cat 2>/dev/null || true)"

field() { # $1 = json key; jq if present, else grep fallback
  local k="$1" v=""
  if command -v jq >/dev/null 2>&1; then
    v="$(printf '%s' "$payload" | jq -r --arg k "$k" '.[$k] // empty' 2>/dev/null || true)"
  fi
  if [ -z "$v" ]; then
    v="$(printf '%s' "$payload" | grep -o "\"$k\"[[:space:]]*:[[:space:]]*\"[^\"]*\"" | head -1 | sed 's/.*"\([^"]*\)"$/\1/' || true)"
  fi
  printf '%s' "$v"
}

sid="$(field session_id)"
tpath="$(field transcript_path)"

if [ -z "$sid" ]; then
  echo "$(date -u +%FT%TZ) no session_id in SessionEnd payload; skipping" >>"$LOG"
  exit 0
fi

args=(--session "$sid" --write --via-shim)
if [ -n "$tpath" ] && [ -d "$(dirname "$tpath")" ]; then
  args+=(--project-dir "$(dirname "$tpath")")
fi

/usr/bin/env python3 "$INDEXER" "${args[@]}" >>"$LOG" 2>&1 || {
  echo "$(date -u +%FT%TZ) indexer failed for $sid" >>"$LOG"
  exit 0
}
exit 0
