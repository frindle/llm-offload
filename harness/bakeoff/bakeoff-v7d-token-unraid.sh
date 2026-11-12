#!/bin/bash
# v7d — isolating WHY qwen2.5-coder:7b fails. Unraid only, one model.
#
# THE CLAIM BEING TESTED
# Every failure recorded for this model has the same shape: right concept,
# wrong exact identifier.
#     v6/v7  app/components/OrderForm.tsx   (real dir: top-level components/)
#     v6     ECPrivateKey                   (real: P256.Signing.PrivateKey)
#     v7c    capture="camera"               (valid: "user" | "environment")
# The last one is the cleanest evidence: THREE independent runs (r3, r4, p1)
# each produced the identical wrong value and each broke the build on it.
# Meanwhile the runs that skipped that requirement (v7b, r2) both compiled.
#
# If the claim holds, the model is not "bad at editing" -- it is precise and
# competent right up until it must recall a token it does not have, which is
# the one failure mode a caller can design around.
#
# THREE ARMS, all pathonly (file named, contents withheld), 2 runs each:
#   x1/x2  plain pathonly    -- replication; p1 was the only valid run after
#                               p2 was discarded as an infrastructure artifact
#                               (ensure_model_ready threw a urllib connection
#                               error before iteration 1). n=1 is not a result.
#   n1/n2  notoken           -- requirements 1+2 only, no token to recall.
#                               PREDICTION: clean pass, build green.
#   e1/e2  exacttoken        -- all three requirements, but the exact value
#                               capture="environment" is handed over.
#                               PREDICTION: passes where the plain arm failed.
#
# Falsifiable both ways. If notoken still breaks the build, the "wrong token"
# story is wrong and the model is simply unreliable at editing. If exacttoken
# still produces capture="camera", the model cannot follow an explicit literal
# instruction, which is a much worse finding than not knowing the value.
set -uo pipefail
export RUN_TAG=v7d
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v7b-nosearch-lib.sh"

HOST="http://192.0.2.82:11434"
BACKEND="unraid"
M="qwen2.5-coder:7b"
SLUG="qwen2.5-coder-7b-unraid"

echo "=== $RUN_TAG UNRAID START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

for a in x1 x2; do
  MODE=pathonly   SUFFIX="-$a" run_model_v7b "$M" "$SLUG" 32768 900 no no "$BACKEND" "$HOST"
done
for a in n1 n2; do
  MODE=notoken    SUFFIX="-$a" run_model_v7b "$M" "$SLUG" 32768 900 no no "$BACKEND" "$HOST"
done
for a in e1 e2; do
  MODE=exacttoken SUFFIX="-$a" run_model_v7b "$M" "$SLUG" 32768 900 no no "$BACKEND" "$HOST"
done

unload_model "$M" "$HOST"
echo "=== $RUN_TAG UNRAID COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"
