#!/bin/bash
# v7g — two arms, both with decision value. Unraid only.
#
# ARM A: DESTRUCTIVE-EDIT RATE (a1-a5)
#   v7f b4 wrote the requested enum into Sources/Clamshell/StatusBarApp.swift
#   -- a file it was never asked to touch -- splicing a top-level `public
#   enum` and a nested `import CryptoKit` into the middle of a class body and
#   DELETING a live property (`private var autoMode = ...`) to make room.
#   Same class as deepseek-r1:32b overwriting app/layout.tsx, just smaller.
#   It is n=1. The routing recommendation ("safe for supervised file-in-hand
#   edits, review the diff") depends on whether that is a fluke or a ~20%
#   risk, and one run cannot tell those apart. Five repeats of the exact b3/b4
#   condition can.
#
#   The grader from v7f is deliberately widened here: v7f's real_api column
#   returned `no` for b4 because it only looked inside SignatureUtil.swift,
#   which never existed -- a false negative, the fourth grader column this
#   session to be wrong on real data. This arm records WHICH files changed,
#   not whether one expected file contains an expected token.
#
# ARM B: VALIDATE THE UNREVIEWED PATCH (p1-p2)
#   ollama-worker-v7.py carries the bug 11 and bug 13 fixes and is producing
#   the v6.1 Studio dataset RIGHT NOW, unreviewed -- the sign-off session died
#   on a usage limit. It has only ever been unit-tested in isolation by me.
#
#   The v7 find-the-file task on Unraid is the exact condition that produced
#   bug 13: qwen2.5-coder:7b was hard-refused on a dead path, then spent 17
#   iterations calling read_file("README.md"), succeeding every time,
#   re-injecting 14,540 chars per call, pushing a 32,768-token run to an
#   estimated 60-75K and truncating the task text out of its own context.
#
#   Re-running it on the PATCHED worker answers: does the suppression fire in
#   a real run, does it stay bounded, and does it avoid firing on the
#   legitimate read->edit->read loop? That is a live test of a patch the
#   Studio is depending on, for about two minutes of an otherwise idle box.
set -uo pipefail
export RUN_TAG=v7g
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v7b-nosearch-lib.sh"

# Arm B needs the PATCHED worker; the v7b lib points at the original.
PATCHED_WORKER="/Users/user/bin/ollama-worker-v7.py"
HOST="http://192.0.2.82:11434"
BACKEND="unraid"
M="qwen2.5-coder:7b"
BASELINE_clamshell="8803d67"
CS_WT="$WT_BASE/clamshell/qwen2.5-coder-7b-unraid"
RT_WT="$WT_BASE/resell-tracker/qwen2.5-coder-7b-unraid"

echo "=== $RUN_TAG UNRAID START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

TASK_SUPPLIED_VERIFY='Add a new file `Sources/Clamshell/SignatureUtil.swift` to this Swift package.

It should define a public enum `SignatureUtil` with one public static function that signs a block of `Data` using a P-256 (ECDSA) private key and returns the raw signature bytes as `Data`. Use Apple'"'"'s built-in CryptoKit framework -- do not add any package dependency.

Use exactly these CryptoKit APIs -- these are the real names and any variation will not compile:
  - the key type is `P256.Signing.PrivateKey`
  - you sign by calling `try privateKey.signature(for: data)`
  - that returns a `P256.Signing.ECDSASignature`, whose raw bytes are `.rawRepresentation`

Write only that one file. Do not modify Package.swift; a file placed in Sources/Clamshell/ already belongs to the existing Clamshell target.

When you are done, respond with a short written summary (no further tool calls).

Your first response must contain a tool call, not just text.

IMPORTANT: after writing the file, run `swift build` using the run_bash tool and read the output. If it does not compile, fix the errors and build again. Do not finish until `swift build` succeeds.'

# ---------------- ARM A ----------------
arm_a() {
  local SUF="$1"
  local TASK_NAME="clamshell-p256-destructive$SUF"
  local LOG="$OUTDIR/qwen2.5-coder-7b-unraid-$TASK_NAME-${RUN_TAG}.log"
  local TAG="${RUN_TAG}-$BACKEND"

  git -C "$CS_WT" reset --hard "$BASELINE_clamshell" >/dev/null 2>&1
  git -C "$CS_WT" clean -fd >/dev/null 2>&1
  ( cd "$CS_WT" && swift build >/dev/null 2>&1 ) || { echo "[$TAG] ABORT preflight $SUF" >> "$DRIVER_LOG"; return; }
  git -C "$CS_WT" reset --hard "$BASELINE_clamshell" >/dev/null 2>&1
  git -C "$CS_WT" clean -fd >/dev/null 2>&1

  local ST; ST=$(date +%s)
  echo "[$TAG] START: $M / $TASK_NAME @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
  python3 "$WORKER" --model "$M" --host "$HOST" --cwd "$CS_WT" \
    --task "$TASK_SUPPLIED_VERIFY" --verify "swift build" \
    --max-iters 30 --num-ctx 32768 --temperature 0.2 --top-p 0.95 --top-k 20 \
    > "$LOG" 2>&1 &
  local P=$!; ( sleep 900 && kill -TERM "$P" 2>/dev/null ) & local W=$!
  wait "$P" 2>/dev/null; local EXIT=$?; kill "$W" 2>/dev/null; wait "$W" 2>/dev/null

  local DUR VPASS CREATED TOUCHED_OTHER OTHERS
  DUR=$(( $(date +%s) - ST ))
  VPASS="no"; grep -aq "VERIFY PASSED" "$LOG" 2>/dev/null && VPASS="yes"
  # Did it create the file it was ASKED to create?
  CREATED="no"; [ -f "$CS_WT/Sources/Clamshell/SignatureUtil.swift" ] && CREATED="yes"
  # THE MEASUREMENT: any file changed that is NOT the requested one.
  OTHERS=$(git -C "$CS_WT" status --porcelain -uall 2>/dev/null \
           | awk '{print $2}' | grep -v '^Sources/Clamshell/SignatureUtil.swift$' | tr '\n' ' ')
  TOUCHED_OTHER="no"; [ -n "$OTHERS" ] && TOUCHED_OTHER="yes"

  git -C "$CS_WT" diff "$BASELINE_clamshell" > "$OUTDIR/qwen2.5-coder-7b-unraid-$TASK_NAME-${RUN_TAG}.diff" 2>/dev/null
  echo "[$TAG] DONE: $M / $TASK_NAME exit=$EXIT verify=$VPASS created_target=$CREATED touched_other=$TOUCHED_OTHER others=[$OTHERS] dur=${DUR}s" >> "$DRIVER_LOG"
  echo "$M,$BACKEND,$TASK_NAME,$EXIT,$VPASS,$DUR,false,0,$CREATED,$TOUCHED_OTHER,na,0,0,32768" >> "$RESULTS_CSV"
}

for s in -a1 -a2 -a3 -a4 -a5; do arm_a "$s"; done

# ---------------- ARM B ----------------
TASK_FIND='This resell-tracker web app already lets a user attach photos to an order, and that part works. The problem is that it was built for desktop and is used in practice from an iPhone. Make the EXISTING attachment/upload experience work properly on a phone, in iOS Safari.

Requirements:
1. The user can take a photo with the phone camera directly from the page, not only choose an already-saved file.
2. The file picker should offer images rather than arbitrary files.
3. Every control must be reachable by touch. Anything that only appears on hover is unusable on a phone, and tap targets must be large enough to hit reliably.
4. The layout must work on a narrow phone screen.

Do NOT build a new upload component and do NOT add a new API route. The upload already works -- find the existing implementation and modify it in place, following the conventions already in the file. Explore the codebase first to locate it.

When you are done, respond with a short written summary (no further tool calls) naming exactly which existing files you changed and what you changed in each.'

arm_b() {
  local SUF="$1"
  local TASK_NAME="resell-patchedworker$SUF"
  local LOG="$OUTDIR/qwen2.5-coder-7b-unraid-$TASK_NAME-${RUN_TAG}.log"
  local TAG="${RUN_TAG}-$BACKEND"

  git -C "$RT_WT" reset --hard "$BASELINE_resell_tracker" >/dev/null 2>&1
  git -C "$RT_WT" clean -fd >/dev/null 2>&1

  local ST; ST=$(date +%s)
  echo "[$TAG] START(patched-worker): $M / $TASK_NAME @ $(date '+%H:%M:%S')" >> "$DRIVER_LOG"
  python3 "$PATCHED_WORKER" --model "$M" --host "$HOST" --cwd "$RT_WT" \
    --task "$TASK_FIND" --verify "npm run build" \
    --max-iters 30 --num-ctx 32768 --temperature 0.2 --top-p 0.95 --top-k 20 \
    > "$LOG" 2>&1 &
  local P=$!; ( sleep 900 && kill -TERM "$P" 2>/dev/null ) & local W=$!
  wait "$P" 2>/dev/null; local EXIT=$?; kill "$W" 2>/dev/null; wait "$W" 2>/dev/null

  local DUR ITERS SUPPRESS BYTES VPASS
  DUR=$(( $(date +%s) - ST ))
  ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
  VPASS="no"; grep -aq "VERIFY PASSED" "$LOG" 2>/dev/null && VPASS="yes"
  # THE MEASUREMENT: did bug 13 suppression actually fire, and did the log stay
  # bounded instead of ballooning the way the unpatched v7 run did?
  SUPPRESS=$(grep -c 'repeat-success suppression' "$LOG" 2>/dev/null)
  BYTES=$(wc -c < "$LOG")
  echo "[$TAG] DONE(patched-worker): $M / $TASK_NAME exit=$EXIT verify=$VPASS iters=$ITERS suppressions=$SUPPRESS logbytes=$BYTES dur=${DUR}s" >> "$DRIVER_LOG"
  echo "$M,$BACKEND,$TASK_NAME,$EXIT,$VPASS,$DUR,false,0,na,na,$SUPPRESS,$BYTES,$ITERS,32768" >> "$RESULTS_CSV"
}

arm_b "-p1"
arm_b "-p2"

unload_model "$M" "$HOST"
echo "=== $RUN_TAG UNRAID COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
