#!/bin/bash
# First-pass review of a real change, dispatched to a local model via the queue.
#
#   review-diff.sh                     # working tree vs HEAD
#   review-diff.sh HEAD~3              # against a ref
#   review-diff.sh main...feature      # a branch's changes
#   review-diff.sh --commit <sha>      # ONE commit's own change
#   review-diff.sh --pr 42             # a GitHub PR
#
# Env: MODEL (default qwen3-coder-30b-ctx64k:latest), HOST (studio|unraid|auto).
#
# Claude review before GitHub remains the gate -- this is triage that runs first
# and costs nothing, not a replacement for it (standing policy, Review-Bench-v5).
set -uo pipefail
MODEL="${MODEL:-qwen3-coder-30b-ctx64k:latest}"
HOST="${HOST:-studio}"
OUT="${OUT:-$(mktemp -d /tmp/review-XXXXXX)}"
mkdir -p "$OUT"

if [ "${1:-}" = "--pr" ]; then
  PR="${2:?--pr needs a number}"
  gh pr diff "$PR" > "$OUT/change.diff" || { echo "gh pr diff failed"; exit 1; }
  INTENT="$(gh pr view "$PR" --json title,body -q '.title + "\n\n" + .body' 2>/dev/null | head -c 1500)"
elif [ "${1:-}" = "--commit" ]; then
  # Review ONE commit's own change. Added 2026-08-31 after this script silently
  # produced 12 useless dispatches: a bare `review-diff.sh <sha>` runs
  # `git diff <sha>`, which is the WORKING TREE vs that sha -- a huge cumulative
  # diff, not that commit -- and every job got HEAD's commit message as its
  # intent regardless of what was being reviewed. Both bugs were invisible until
  # I dumped the enqueued task.json files and saw all 12 share one intent line.
  SHA="${2:?--commit needs a sha}"
  git diff "${SHA}^" "$SHA" > "$OUT/change.diff" || { echo "bad sha $SHA"; exit 1; }
  INTENT="$(git log -1 --pretty=%B "$SHA" 2>/dev/null | head -c 1500)"
else
  REF="${1:-}"
  if [ -n "$REF" ]; then
    git diff "$REF" > "$OUT/change.diff"
    # the intent must describe the diff being reviewed, not whatever HEAD says
    INTENT="$(git log --pretty=%B "$REF"..HEAD 2>/dev/null | head -c 1500)"
    [ -n "$INTENT" ] || INTENT="Cumulative changes since $REF."
  else
    git diff HEAD > "$OUT/change.diff"
    INTENT="$(git log -1 --pretty=%B 2>/dev/null | head -c 1500)"
  fi
fi

[ -s "$OUT/change.diff" ] || { echo "empty diff -- nothing to review"; exit 0; }
LINES=$(grep -cE '^[+-][^+-]' "$OUT/change.diff" || true)
echo "reviewing $LINES changed line(s) -> $OUT"

python3 - "$OUT" "$INTENT" <<'PY'
import json,sys,pathlib
out,intent=sys.argv[1],sys.argv[2]
pathlib.Path(out,"task.json").write_text(json.dumps({
  "mode":"review","diff":str(pathlib.Path(out,"change.diff")),
  "intent":intent.strip() or "No stated intent.",
  "context":"Real change under review. Report only defects this change introduces."},indent=2))
PY

python3 ~/bin/ollama-queue.py enqueue --model "$MODEL" --host "$HOST" \
  --num-ctx 32768 --cwd "$OUT" --task-file "$OUT/task.json" \
  --runner ~/bin/code-review-agent.py --label "review:$(basename "$OUT")"

echo
echo "when it completes:  cat $OUT/report.md"
