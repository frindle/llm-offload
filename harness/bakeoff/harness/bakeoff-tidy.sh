#!/bin/bash
# Move finished bake-off material out of the GitHub Projects root into bakeoff/.
#
# WHY THIS IS A SCRIPT AND NOT A ONE-OFF: the root is also the WORKING
# directory for every round. Drivers source bakeoff-v8-lib.sh by ABSOLUTE
# path, worktrees live under bakeoff-build-*, and results append to
# model-buildoff-*/. Moving any of those mid-round breaks the round, silently,
# hours in. So this refuses to run while a driver is alive, and it never
# touches anything a live script references.
#
# Run it BETWEEN rounds. It is idempotent and it prints what it would do
# before doing it.
set -u
ROOT="/Users/user/Desktop/GitHub Projects"
DEST="$ROOT/bakeoff"
APPLY="${1:-}"

cd "$ROOT" || exit 1
mkdir -p "$DEST"

# ---- GUARD 1: no round may be running ------------------------------------
LIVE_DRIVER="$(pgrep -fl 'bakeoff-v[0-9].*\.sh' | grep -v pgrep | head -3)"
LIVE_WORKER="$(pgrep -f 'ollama-worker' | wc -l | tr -d ' ')"
if [ -n "$LIVE_DRIVER" ] || [ "$LIVE_WORKER" -gt 0 ]; then
  echo "REFUSING: a round appears to be running."
  [ -n "$LIVE_DRIVER" ] && echo "$LIVE_DRIVER" | sed 's/^/  driver: /'
  [ "$LIVE_WORKER" -gt 0 ] && echo "  workers: $LIVE_WORKER"
  echo "Tidying mid-round can break it. Re-run when the round is complete."
  exit 2
fi

# ---- Things that STAY in root, always ------------------------------------
# bakeoff-v8-lib.sh        -- the frozen instrument, sourced by absolute path
# bakeoff-build-*          -- live git worktrees (18GB); moving breaks them
# model-buildoff-*         -- results, appended to by every round
# bakeoff-fixtures         -- v10 fixture, its own git repo
# plex-automation-debugcell-- the repo the worktrees are checked out FROM
# bakeoff                  -- the destination itself
KEEP_RE='^(bakeoff|bakeoff-v8-lib\.sh|bakeoff-build-.*|bakeoff-fixtures|model-buildoff-.*|plex-automation-debugcell|bakeoff-tidy\.sh)$'

# ---- GUARD 2: never move a file another root script still references ------
referenced() {
  local name="$1"
  for s in "$ROOT"/bakeoff-v*.sh; do
    [ -f "$s" ] || continue
    case "$(basename "$s")" in "$name") continue ;; esac
    grep -qF -- "$name" "$s" 2>/dev/null && { echo "$(basename "$s")"; return 0; }
  done
  return 1
}

moved=0; skipped=0
for item in bakeoff-* *buildoff*; do
  [ -e "$item" ] || continue
  [[ "$item" =~ $KEEP_RE ]] && continue
  if by="$(referenced "$item")"; then
    echo "  SKIP  $item  (referenced by $by)"
    skipped=$((skipped+1)); continue
  fi
  if [ "$APPLY" = "--apply" ]; then
    mv "$item" "$DEST/" 2>/dev/null && { echo "  MOVED $item"; moved=$((moved+1)); } \
      || echo "  FAIL  $item"
  else
    echo "  would move $item"; moved=$((moved+1))
  fi
done

echo
if [ "$APPLY" = "--apply" ]; then
  echo "moved $moved, skipped $skipped. root now has $(ls -d bakeoff* *buildoff* 2>/dev/null | wc -l | tr -d ' ') bake-off entries."
else
  echo "$moved would move, $skipped skipped. Re-run with --apply to do it."
fi
