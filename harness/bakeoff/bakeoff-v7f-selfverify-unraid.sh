#!/bin/bash
# v7e — does the token-recall finding GENERALIZE, and is it RELIABLE?
# Unraid only, qwen2.5-coder:7b only. Safe to run beside v6.1: this uses the
# *-unraid worktrees, v6.1 uses *-macstudio.
#
# WHAT v7d ESTABLISHED, AND WHAT IT DID NOT
# Every failure recorded for this model is "right concept, wrong exact
# identifier" -- app/components/OrderForm.tsx, ECPrivateKey, capture="camera".
# v7d tested that on ONE attribute in ONE React file:
#     requirement removed  (notoken)    2/2 clean pass, 12-13s, 5 iters
#     token withheld                    0/4 valid values (3x capture="camera",
#                                       1x ;capture=camera buried in accept=)
#     token supplied       (exacttoken) 0/2 INVALID values -- but only 1/2
#                                       actually made the edit; e2 silently
#                                       skipped the requirement entirely
#
# So two things are still open, and they are the two that matter for routing:
#
#   ARM 1 (e3-e5, resell-tracker): RELIABILITY. Supplying the token clearly
#   stops the model emitting a WRONG value. It is not established that it
#   makes the model DO the edit -- e2 did requirements 1+2 and quietly
#   dropped 3. n=2 cannot separate "usually works" from "coin flip". Three
#   more runs takes the arm to n=5.
#
#   ARM 2 (s1-s4, clamshell): GENERALIZATION. The whole finding rests on one
#   attribute in one TSX file. If it is real it should reproduce in a
#   different language, a different repo, and a different API surface. This
#   model invented `ECPrivateKey` in Swift during v6 -- exactly the same
#   shape as capture="camera" -- so CryptoKit is the natural cross-check:
#       s1/s2  withheld -- "sign with P-256 using CryptoKit", no names given
#       s3/s4  supplied -- P256.Signing.PrivateKey / .signature(for:) /
#                          .rawRepresentation spelled out
#   PREDICTION: withheld invents a plausible non-existent type and fails to
#   compile; supplied compiles. If supplied ALSO fails, the finding is
#   specific to the React task and the routing rule must not be generalised.
#
# The Swift task is deliberately TINY -- one function in one new file that
# joins the existing Clamshell target. The full ConfirmationBridge task is far
# beyond this model and would confound token recall with everything else.
set -uo pipefail
export RUN_TAG=v7f
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v7b-nosearch-lib.sh"

HOST="http://192.0.2.82:11434"
BACKEND="unraid"
M="qwen2.5-coder:7b"
BASELINE_clamshell="8803d67"

echo "=== $RUN_TAG UNRAID START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

# ---------- ARM 1: reliability of the supplied-token result ----------

# ---------- ARM 2: does it generalise to Swift/CryptoKit? ----------
TASK_SWIFT_WITHHELD='Add a new file `Sources/Clamshell/SignatureUtil.swift` to this Swift package.

It should define a public enum `SignatureUtil` with one public static function that signs a block of `Data` using a P-256 (ECDSA) private key and returns the raw signature bytes as `Data`. Use Apple'"'"'s built-in CryptoKit framework -- do not add any package dependency.

Write only that one file. Do not modify Package.swift; a file placed in Sources/Clamshell/ already belongs to the existing Clamshell target.

When you are done, respond with a short written summary (no further tool calls).

Your first response must contain a tool call, not just text.'

TASK_SWIFT_SUPPLIED='Add a new file `Sources/Clamshell/SignatureUtil.swift` to this Swift package.

It should define a public enum `SignatureUtil` with one public static function that signs a block of `Data` using a P-256 (ECDSA) private key and returns the raw signature bytes as `Data`. Use Apple'"'"'s built-in CryptoKit framework -- do not add any package dependency.

Use exactly these CryptoKit APIs -- these are the real names and any variation will not compile:
  - the key type is `P256.Signing.PrivateKey`
  - you sign by calling `try privateKey.signature(for: data)`
  - that returns a `P256.Signing.ECDSASignature`, whose raw bytes are `.rawRepresentation`

Write only that one file. Do not modify Package.swift; a file placed in Sources/Clamshell/ already belongs to the existing Clamshell target.

When you are done, respond with a short written summary (no further tool calls).

Your first response must contain a tool call, not just text.'

# The ONLY difference from v7e. v7e proved the model never compiles its own
# work: s3/s4 made ZERO run_bash calls, wrote the file, and stopped -- so a
# missing `import Foundation` survived to the final verify even though the
# compiler would have named it instantly. That is the multiplier on every
# other failure: an invented API or a missing import is trivially recoverable
# IF you read the error, and permanent if you never look.
#
# qwen3.8:27b-q8_0 -- the one model that completed a task -- did the opposite:
# it ran swift build repeatedly, read the errors, went to the SDK
# .swiftinterface when stuck, and executed its own self-test.
#
# QUESTION: is the verify loop a capability this model lacks, or just a habit
# it does not have? If instructing it to compile makes it self-correct, that
# is a large routing win and a one-line change to any dispatch prompt. If it
# still will not, the deficit is structural.
VERIFY_INSTR='

IMPORTANT: after writing the file, run `swift build` using the run_bash tool and read the output. If it does not compile, fix the errors and build again. Do not finish until `swift build` succeeds.'

run_swift_arm() {
  local SUF="$1" TASK_TEXT="$2"
  local SLUG="qwen2.5-coder-7b-unraid"
  local TASK_NAME="clamshell-p256-sign${SUF}"
  local WT_DIR="$WT_BASE/clamshell/$SLUG"
  local LOG="$OUTDIR/$SLUG-$TASK_NAME-${RUN_TAG}-$BACKEND.log"
  local TAG="${RUN_TAG}-$BACKEND"
  local VERIFY="swift build"

  git -C "$WT_DIR" reset --hard "$BASELINE_clamshell" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  echo "[$TAG] preflight: '$VERIFY' on pristine clamshell/$SLUG ..." >> "$DRIVER_LOG"
  if ! ( cd "$WT_DIR" && eval "$VERIFY" >/dev/null 2>&1 ); then
    echo "[$TAG] ABORT: pristine-tree verify FAILED" >> "$DRIVER_LOG"
    echo "$M,$BACKEND,$TASK_NAME,ABORT_PREFLIGHT,na,0,false,0,no,no,no,0,0,32768" >> "$RESULTS_CSV"
    return
  fi
  git -C "$WT_DIR" reset --hard "$BASELINE_clamshell" >/dev/null 2>&1
  git -C "$WT_DIR" clean -fd >/dev/null 2>&1

  local START_TS; START_TS=$(date +%s)
  echo "[$TAG] START: $M / $TASK_NAME @ $(date '+%Y-%m-%d %H:%M:%S')" >> "$DRIVER_LOG"

  python3 "$WORKER" --model "$M" --host "$HOST" --cwd "$WT_DIR" \
    --task "$TASK_TEXT" --verify "$VERIFY" \
    --max-iters 30 --num-ctx 32768 --temperature 0.2 --top-p 0.95 --top-k 20 \
    > "$LOG" 2>&1 &
  local WPID=$!
  ( sleep 900 && kill -TERM "$WPID" 2>/dev/null ) & local WATCH=$!
  wait "$WPID" 2>/dev/null; local EXIT=$?
  kill "$WATCH" 2>/dev/null; wait "$WATCH" 2>/dev/null

  local DUR ITERS VPASS FILES REALAPI FAKEAPI
  DUR=$(( $(date +%s) - START_TS ))
  ITERS=$(grep -ac 'iteration' "$LOG" 2>/dev/null); ITERS=${ITERS:-0}
  VPASS="no"; grep -aq "VERIFY PASSED" "$LOG" 2>/dev/null && VPASS="yes"
  FILES=$(git -C "$WT_DIR" status --porcelain -uall 2>/dev/null | wc -l | tr -d ' ')
  # The measurement that matters: did it use the REAL API, or invent one?
  REALAPI="no"; grep -q 'P256\.Signing\.PrivateKey' "$WT_DIR/Sources/Clamshell/SignatureUtil.swift" 2>/dev/null && REALAPI="yes"
  FAKEAPI="no"; grep -qE 'ECPrivateKey|P256\.PrivateKey|P256\.KeyPair|ECDSAPrivateKey' "$WT_DIR/Sources/Clamshell/SignatureUtil.swift" 2>/dev/null && FAKEAPI="yes"

  cp "$WT_DIR/Sources/Clamshell/SignatureUtil.swift" "$OUTDIR/$SLUG-$TASK_NAME-${RUN_TAG}.swift" 2>/dev/null
  git -C "$WT_DIR" diff "$BASELINE_clamshell" > "$OUTDIR/$SLUG-$TASK_NAME-${RUN_TAG}.diff" 2>/dev/null

  echo "[$TAG] DONE: $M / $TASK_NAME exit=$EXIT verify=$VPASS dur=${DUR}s files=$FILES real_api=$REALAPI fake_api=$FAKEAPI iters=$ITERS" >> "$DRIVER_LOG"
  echo "$M,$BACKEND,$TASK_NAME,$EXIT,$VPASS,$DUR,false,$FILES,$REALAPI,$FAKEAPI,na,0,$ITERS,32768" >> "$RESULTS_CSV"
}

run_swift_arm "-b1-withheld-verify" "$TASK_SWIFT_WITHHELD$VERIFY_INSTR"
run_swift_arm "-b2-withheld-verify" "$TASK_SWIFT_WITHHELD$VERIFY_INSTR"
run_swift_arm "-b3-supplied-verify" "$TASK_SWIFT_SUPPLIED$VERIFY_INSTR"
run_swift_arm "-b4-supplied-verify" "$TASK_SWIFT_SUPPLIED$VERIFY_INSTR"

unload_model "$M" "$HOST"
echo "=== $RUN_TAG UNRAID COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
