#!/bin/bash
# Launch llama-server for Bonsai-2-27B ternary (PTQ1_0) using PrismML's PATCHED
# llama.cpp fork, exposing the OpenAI-protocol endpoint that ollama-worker.py
# talks to via `--api openai`.
#
# WHY A PATCHED BINARY: stock ollama (0.32.14) CANNOT read these weights. The
# GGUF uses custom ggml tensor type 143 (PTQ1_0, "1.75 bpw ternary, group 128");
# 142 for the PQ2_0 sibling. Stock ggml has no block-size entry for those ids, so
# its byte-size computation for output.weight (5120 x 248320) overflows and
# /api/show and /api/generate both fail with:
#     tensor "output.weight" size overflow
# The architecture is NOT the problem -- the GGUF reports arch `qwen35`, the same
# arch our working qwen3.8:27b-q4_K_M reports. Only the quant is unsupported.
#
# The binary is the project's OWN prebuilt macOS arm64 release -- NOT a local
# build. Nothing is compiled on the owner's Mac, and a Linux sandbox cannot produce a
# macOS/Metal arm64 binary anyway (and there is no full Xcode here to compile
# Metal shaders -- CLT only).
#
# Binary provenance:
#   repo    github.com/PrismML-Eng/llama.cpp
#   release prism-b10685-7dffb15  (newest release that actually ships a macOS
#           arm64 asset -- the newer prism-b10687-5d80cff advertises one in its
#           release body but uploaded only 3 cudart zips)
#   asset   llama-prism-b10685-7dffb15-bin-macos-arm64.tar.gz
#
# The weights are the blob ollama already pulled for
# hf.co/prism-ml/Ternary-Bonsai-2-27B-gguf:PTQ1_0 -- reused directly so there is
# only one copy on disk. `ollama rm` of that tag WILL delete this blob.
#
# Usage:
#   start-llama-server-bonsai.sh          # Metal (-ngl 99), the real arm
#   BONSAI_NGL=0 start-llama-server-bonsai.sh   # CPU-only, for protocol checks
#                                         # that must not contend for the GPU
# Stop:
#   kill "$(cat /tmp/llama-server-bonsai.pid)"
set -euo pipefail

BIN="/Users/user/opt/llama-prism/llama-prism-b10685-7dffb15/llama-server"
MODEL_BLOB="/Volumes/NVMe-Models/ollama-models/blobs/sha256-53107f530aa52eb00912263ab1ee29bd199261c87cd7b4ad4ca1318c1fe33ee3"
PORT="${BONSAI_PORT:-8092}"
NGL="${BONSAI_NGL:-99}"
CTX="${BONSAI_CTX:-32768}"
LOG="/Users/user/Desktop/GitHub Projects/ollama/llama-server-tests/llama-server-bonsai-$(date +%Y%m%dT%H%M%S).log"

if [ ! -x "$BIN" ]; then
  echo "ERROR: patched llama-server not found at $BIN" >&2
  echo "  re-download: gh release download prism-b10685-7dffb15 --repo PrismML-Eng/llama.cpp \\" >&2
  echo "      --pattern 'llama-prism-b10685-7dffb15-bin-macos-arm64.tar.gz'" >&2
  echo "  then untar into /Users/user/opt/llama-prism and xattr -dr com.apple.quarantine ." >&2
  exit 1
fi
if [ ! -f "$MODEL_BLOB" ]; then
  echo "ERROR: Bonsai PTQ1_0 blob not found at $MODEL_BLOB" >&2
  echo "  re-pull: ollama pull hf.co/prism-ml/Ternary-Bonsai-2-27B-gguf:PTQ1_0" >&2
  exit 1
fi
mkdir -p "$(dirname "$LOG")"

# --parallel 1: one slot, so --ctx-size is the whole context the worker's
# --num-ctx assumes, not divided across unused slots (the devstral lesson).
"$BIN" \
  -m "$MODEL_BLOB" \
  --port "$PORT" \
  --ctx-size "$CTX" \
  --parallel 1 \
  -ngl "$NGL" \
  --jinja \
  > "$LOG" 2>&1 &

SERVER_PID=$!
echo "$SERVER_PID" > /tmp/llama-server-bonsai.pid
echo "[start-llama-server-bonsai] PID $SERVER_PID port $PORT ngl=$NGL ctx=$CTX"
echo "[start-llama-server-bonsai] log: $LOG"

for i in $(seq 1 300); do
  if curl -s -m 2 "http://localhost:$PORT/health" 2>/dev/null | grep -q "ok"; then
    echo "[start-llama-server-bonsai] up and healthy after ${i}s"
    exit 0
  fi
  if ! kill -0 "$SERVER_PID" 2>/dev/null; then
    echo "ERROR: llama-server exited during load -- see $LOG" >&2
    tail -20 "$LOG" >&2
    exit 1
  fi
  sleep 1
done

echo "ERROR: llama-server did not become healthy within 300s -- check $LOG" >&2
exit 1
