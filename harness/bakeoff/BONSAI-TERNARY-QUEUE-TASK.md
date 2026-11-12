# Bonsai-2-27B PTQ1_0 ternary bake-off (queue runner job)

Runs the standalone Bonsai llama-server lifecycle runner via `bakeoff-runner.py`
as ONE Studio-lane queue job: start llama-server (PrismML patched binary) ->
health check -> bench the v10 cell-E bulk-codemod fixture -> score -> stop.

ALL GPU work goes through the queue (`feedback_all_gpu_work_through_queue`), and a
lifecycle job that holds the Studio lane for its whole duration is exactly the
serialization wanted here: nothing else touches the GPU while llama-server has the
ternary weights resident.

The runner is a SIBLING of `bakeoff-v10-bulk-macstudio.sh`, never a patch to it --
that driver is a pinned-results comparability artifact. Bonsai publishes no Q4_K_M
and the roster is pinned to Q4_K_M, so this can never be a quant-matched arm: it is
a separately-labelled standalone ternary datapoint
(`bonsai-2-27b-ternary-PTQ1_0` / `macstudio-llamaserver-ternary`) in its own CSV.

`ROSTER_ONLY` is set by the runner wrapper and is IGNORED by this driver (it has a
roster of exactly one model). `DRIVER=` below is the only line the wrapper reads.

RESUME: the driver skips any rep that already has a measurement row in
`model-buildoff-2026-08-22/results-v10-bulk-bonsai-ternary.csv`, so a re-queue
continues at the next missing rep instead of redoing rep 1 and overwriting its
transcript. An interrupted rep (preemption SIGTERM, daemon restart, dead
llama-server) writes NO row and the driver exits 4, so re-queueing is the
correct and only recovery action.

This file exists to satisfy the queue's `--task-file` requirement.

DRIVER=/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-bonsai-ternary-macstudio.sh
