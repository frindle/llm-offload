#!/bin/bash
# Thin wrapper, NOT a fork: points the existing macstudio driver at the Bonsai
# llama-server that's ALREADY running on Unraid's GPU (203.0.113.33:8092, confirmed
# healthy 2026-09-19) instead of having the driver launch its own local Studio
# server. BONSAI_SKIP_LAUNCH=1 means a dead remote server is a hard abort here,
# not a fallback to a local launch -- this run is specifically testing Unraid's
# GPU, so silently falling back to Studio would produce a mislabeled datapoint.
set -euo pipefail
# Distinct backend label: the driver's already_done()/CSV resume matches on
# (model, BACKEND, rep), so this MUST differ from the default
# macstudio-llamaserver-ternary -- otherwise the Studio arm's existing rep-1 row
# would make the Unraid arm think rep 1 is already done and skip it, and any
# rows this run does write would falsely claim to be a Studio measurement.
export BONSAI_BACKEND="unraid-llamaserver-ternary"
export BONSAI_ENDPOINT="http://203.0.113.33"
export BONSAI_PORT="8092"
export BONSAI_SKIP_LAUNCH=1
exec "$(dirname "$0")/bakeoff-bonsai-ternary-macstudio.sh"
