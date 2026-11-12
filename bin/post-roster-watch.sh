#!/bin/bash
# Waits for the 9-model roster to finish, then does ONLY the mechanical,
# reversible post-roster steps. Deliberately does NOT dispatch any model:
# every dispatch task still needs a verifier written, and an unverified build
# is exactly what this programme refuses to trust.
#
# Log: ~/post-roster-watch.log      Stop: kill the PID in ~/post-roster-watch.pid
BASE="/Users/user/Desktop/GitHub Projects"
LOG="$HOME/post-roster-watch.log"
ROSTER="$BASE/.ollama-sandboxes/roster-final.log"
FIXED="/private/tmp/claude-501/-Users-user-Desktop-GitHub-Projects/6031e4e3-1c06-4ea4-b4cb-2f6d6aab23e9/scratchpad/guided-piv-setup.fixed.sh"
LIVE="$BASE/machine-config/bin/guided-piv-setup.sh"

echo $$ > "$HOME/post-roster-watch.pid"
say(){ echo "[$(date '+%m-%d %H:%M:%S')] $*" >> "$LOG"; }

say "watching for ROSTER COMPLETE"
# 16h ceiling at 60s -- longer than any plausible remaining runtime.
for _ in $(seq 1 960); do
  grep -q "ROSTER COMPLETE" "$ROSTER" 2>/dev/null && break
  sleep 60
done

if ! grep -q "ROSTER COMPLETE" "$ROSTER" 2>/dev/null; then
  say "gave up waiting -- roster still running. Nothing applied."
  exit 0
fi
say "ROSTER COMPLETE detected"

# 1. Scorecard snapshot BEFORE anything is touched.
say "--- scorecard (pre-change) ---"
python3 "$BASE/machine-config/benchmarks/scorecard.py" >> "$LOG" 2>&1

# 2. Apply the guided-piv-setup fix, but only if the seed patterns still match.
# seed_min2 is `sed 's/-lt 2 \]/-lt 1 ]/'`; if the fix moved or changed that
# line the review bench would silently degrade to SEED_NOOP on a re-run.
if [ -f "$FIXED" ]; then
  if grep -q -- '-lt 2 \]' "$FIXED"; then
    cp "$LIVE" "$LIVE.pre-fix-$(date '+%Y%m%d-%H%M%S')"
    cp "$FIXED" "$LIVE"
    if bash -n "$LIVE" 2>>"$LOG"; then
      say "applied guided-piv-setup fix (backup alongside, .pre-fix-*)"
    else
      say "SYNTAX ERROR after apply -- reverting"
      cp "$LIVE.pre-fix-"* "$LIVE" 2>/dev/null
    fi
  else
    say "SKIPPED guided-piv-setup fix: seed pattern '-lt 2 ]' no longer present"
  fi
else
  say "SKIPPED guided-piv-setup fix: staged copy missing (scratchpad cleared?)"
fi

# 3. Review rescore -- DRY RUN only. Writing needs the answer keys re-checked
# against the seeded diffs first, and that is a judgement call.
say "--- rescore-review.py (dry run) ---"
python3 "$BASE/machine-config/benchmarks/rescore-review.py" >> "$LOG" 2>&1

say "done. NOT applied: rescore --write, task-manifest additions, any model dispatch."
