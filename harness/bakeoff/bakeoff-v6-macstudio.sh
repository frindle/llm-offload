#!/bin/bash
# v6 bake-off, Mac Studio — FULL matrix, all 12 model-backends.
#
# WHY v6 EXISTS (the one change that matters):
# v5 measured every model while withholding context the project had written
# FOR them. resell-tracker/AGENTS.md line 4 says verbatim: "This version has
# breaking changes -- APIs, conventions, and file structure may all differ
# from your training data. Read the relevant guide in
# node_modules/next/dist/docs/ before writing any code." The harness never
# mentioned AGENTS.md or CLAUDE.md (grep count was 0), while 26 existing
# app/api/ route directories showed the correct idiom and the Next.js docs
# sat readable on disk. deepseek-r1:32b failed by writing Pages Router idiom
# (export default handler / NextApiRequest) into an App Router route.ts --
# a version-recency dialect error, one convention away from a working
# feature, with the answer available three different ways and none surfaced.
#
# SYSTEM_PROMPT now instructs models to (a) read AGENTS.md/CLAUDE.md/README/
# CONTRIBUTING if present, and (b) read an existing file of the same kind
# before creating a new one and match its idiom exactly.
#
# v5 results are a FLOOR, not a ceiling -- every model was handicapped
# equally, so the relative ranking held, but absolute verdicts were
# pessimistic. v6 re-measures everything with the handicap removed.
#
# Running EVERYTHING (the owner's call), including models v5 already disqualified,
# so the dataset is complete and internally comparable rather than a mix of
# v5 and v6 rows.
#
# Carried forward from v5, unchanged:
#   * per-model ctx: deepseek family 131072 (Mac Studio holds the KV cache);
#     qwen family 32768; devstral 65536 (its proven setting)
#   * --manual-tools for deepseek-r1/MFDoom (ollama#8517 template gap)
#   * temp 0.2 for qwen + devstral, 0.6 for deepseek (DeepSeek's own 0.5-0.7)
#   * qwen3-coder-next gets TIMEOUT 3600: 52.2GB resident, ~7.5 min/iteration,
#     so 1800s only ever allowed ~4 iterations
#   * pristine-tree preflight; iteration count and /api/ps recorded; ABORT
#     rows written rather than silent skips
set -uo pipefail
source "/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-v2-lib.sh"

HOST="http://localhost:11434"
BACKEND="macstudio"

echo "=== $RUN_TAG MACSTUDIO START @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"

# --- RUN FIRST (the owner's call): these two are the ONLY models that ever used
# llama-server, so they decide whether we still need a second backend at all.
# Front-loaded so that answer arrives in minutes instead of hours.
#
# devstral:24b -- previously run via llama-server with --api openai after being
# wrongly declared non-viable on Ollama. Runs native here: capabilities
# ['completion','tools'] with a real template confirmed. temp 0.2 is its
# proven setting.
run_model "devstral:24b"                        "devstral-24b"                        65536   1800  no     no   "$BACKEND" "$HOST"
unload_model "devstral:24b" "$HOST"

# qwen3.8:27b-q8_0 -- previously crashed on Ollama with "no user query found in
# messages" (HTTP 500, ~215ms, 9 times = 3 crashes x 3 retries). The vault's
# root-cause entry attributes that to ollama-worker.py's OWN message-list
# building, not to Ollama -- so this run tests whether that bug is actually
# fixed. If it 500s again in ~215ms, the bug is live; that is the signal, and
# it is worth knowing early rather than after eight other models.
run_model "qwen3.8:27b-q8_0"                    "qwen3.8-27b-q8_0"                    32768   1800  no     no   "$BACKEND" "$HOST"
unload_model "qwen3.8:27b-q8_0" "$HOST"

#          model                                slug                                  ctx     tmo   manual nudge
run_model "deepseek-r1:32b"                     "deepseek-r1-32b"                     131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:32b" "$HOST"

run_model "deepseek-r1:14b"                     "deepseek-r1-14b"                     131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:14b" "$HOST"

run_model "MFDoom/deepseek-r1-tool-calling:14b" "MFDoom-deepseek-r1-tool-calling-14b" 131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "MFDoom/deepseek-r1-tool-calling:14b" "$HOST"

run_model "qwen2.5-coder:14b"                   "qwen2.5-coder-14b"                   32768   1800  no     no   "$BACKEND" "$HOST"
unload_model "qwen2.5-coder:14b" "$HOST"

run_model "qwen3-coder-next:q4_K_M"             "qwen3-coder-next-q4_K_M"             32768   3600  no     no   "$BACKEND" "$HOST"
unload_model "qwen3-coder-next:q4_K_M" "$HOST"

run_model "deepseek-r1:32b-qwen-distill-q8_0"   "deepseek-r1-32b-qwen-distill-q8_0"   131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:32b-qwen-distill-q8_0" "$HOST"

run_model "qwen2.5-coder:7b"                    "qwen2.5-coder-7b-macstudio"          32768   1800  no     no   "$BACKEND" "$HOST"
unload_model "qwen2.5-coder:7b" "$HOST"

run_model "deepseek-r1:7b"                      "deepseek-r1-7b-macstudio"            131072  1800  yes    yes  "$BACKEND" "$HOST"
unload_model "deepseek-r1:7b" "$HOST"

# --- the three that were never in any prior matrix ---

# deepseek-r1:70b -- not in local Ollama; ensure_model_cached copies it from
# the shared archive (confirmed present: library/deepseek-r1/70b).
# deepseek-r1:70b -- REMOVED from v6. Not a model verdict: it cannot be fairly
# tested on this hardware. iogpu.wired_limit_mb=0 caps the GPU working set at
# ~75% of 64GB (~48GB); 70b weights are ~40-42.5GB and KV at 32768 adds ~10.2GB
# (~0.31MB/token f16) plus ~1.5GB buffers => ~53-55GB against a 48GB ceiling,
# so it partially offloads to CPU and crawls. Max fully-resident context is
# roughly 12k tokens, which a manual-tools deepseek transcript exceeds -- that
# would trigger context shift and produce a harness-induced failure.
#
# It IS staged in ~/.ollama/models, so it can be run separately without another
# 42.5GB copy if `sudo sysctl iogpu.wired_limit_mb=57344` is applied first
# (verify /api/ps shows 100% GPU on a warmup before trusting any result).
# Record as "untestable on 64GB at usable context", which is itself the routing
# finding: this model needs a bigger box.

# qwen3-14b-agentic -- never tested; the original attempt died when the LAN
# mount dropped. Native tool-calling ASSUMED (unverified). If it emits zero
# tool calls, that is a template gap, not the model -- retry with
# --manual-tools before recording any verdict.
run_model "qwen3-14b-agentic"                   "qwen3-14b-agentic"                   32768   1800  no     no   "$BACKEND" "$HOST"
unload_model "qwen3-14b-agentic" "$HOST"

echo "=== $RUN_TAG MACSTUDIO COMPLETE @ $(date '+%Y-%m-%d %H:%M:%S') ===" >> "$DRIVER_LOG"