# Bonsai-2-27B PTQ1_0 ternary bake-off (queue runner job) -- UNRAID GPU ARM

Same job as `BONSAI-TERNARY-QUEUE-TASK.md`, but `DRIVER=` points at
`bakeoff-bonsai-ternary-unraid.sh`, a thin wrapper that sets `BONSAI_ENDPOINT`/
`BONSAI_PORT`/`BONSAI_SKIP_LAUNCH=1` so the driver talks to the Bonsai
llama-server ALREADY running on Unraid's GPU (203.0.113.33:8092, PrismML patched
binary, confirmed healthy) instead of launching its own local Studio server.
This is a deliberate second, separately-labelled datapoint testing the same
ternary model on Unraid's 12GB CUDA card rather than Studio's Metal/unified
memory -- not a routing fix for the Studio arm, which still exists and still
runs against its own local server via the original task file.

Runs the standalone Bonsai llama-server lifecycle runner via `bakeoff-runner.py`
as ONE queue job: health-check the remote server -> bench the v10 cell-E
bulk-codemod fixture -> score. The remote server's lifecycle (start/stop) is
NOT owned by this job -- BONSAI_SKIP_LAUNCH=1 means a dead remote server is a
hard abort, not a fallback to a local launch, since a local fallback would
silently mislabel a Studio run as an Unraid datapoint.

ALL GPU work goes through the queue (`feedback_all_gpu_work_through_queue`).
Enqueue this with `--host unraid` so the queue's lane-occupancy serialization
protects the Unraid GPU for the run's duration, even though the actual
inference happens on an external process the queue's own VRAM-residency
guards can't see directly.

`ROSTER_ONLY` is set by the runner wrapper and is IGNORED by this driver (it has a
roster of exactly one model). `DRIVER=` below is the only line the wrapper reads.

RESUME: the driver skips any rep that already has a measurement row in
`model-buildoff-2026-08-22/results-v10-bulk-bonsai-ternary.csv`, so a re-queue
continues at the next missing rep instead of redoing rep 1 and overwriting its
transcript. An interrupted rep (preemption SIGTERM, daemon restart, dead
llama-server) writes NO row and the driver exits 4, so re-queueing is the
correct and only recovery action.

This file exists to satisfy the queue's `--task-file` requirement.

DRIVER=/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-bonsai-ternary-unraid.sh
