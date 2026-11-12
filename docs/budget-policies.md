# Worker budget policies (2026-10-09)

Switchable agent-budget policies for `ollama-worker.py`, implemented in `bin/worker_budget.py`
(pure state machine + helpers) with gated seams in the worker. **Default is OFF: with no policy the
worker's prompts, tool results and metrics are byte-identical to before.** They exist for the A/B in
`bin/budget-policy-ab.md` (flat-author jobs hit the 14/24 cap 23-30% of the time; whether budget size or
budget structure is the lever is unsettled, so this is a measurement switch, not a default change).

## The switch

Resolution order: `--budget-policy name[,name...]` on the worker (an explicit `none` wins) >
a `Budget-policy: a,b` line in the task text (per-job passthrough through the queue with no queue change;
`ollama-dispatch-auto --budget-policy X` appends it to AUTO-TASK.md) > env `WORKER_BUDGET_POLICY` (daemon-wide,
do not use for an A/B). `all` = every policy. Unknown names: hard error on the CLI, logged + ignored
from task/env. Coding runs only (research ignores). Logged as `BUDGET POLICY active (...)`; recorded in
`dispatch-metrics.jsonl` as `budget_policy` (list), `budget_base_cap`, plus the fields below.

| policy | behaviour | metrics |
|---|---|---|
| `progress_extend` | at the cap grant ONE extension (+8; +12 for `# REFINE TASK`) iff a write/edit landed in the last 3 iterations AND the last verify output differs from the previous one; refuse on 3 identical verify tails, no verify seen, or past 1.5x the original cap (grant is clamped: cap 14 -> +7). Decided at end-of-iteration and again in the loop condition (silent-stop `continue` paths). Notice appended to the transcript. | `budget_extension_granted`, `budget_extension_refused`, `budget_events` |
| `budget_visible` | `[Budget: iteration i of N, R left; extension available (+8 ...)/no extension available.]` after EVERY turn (replaces the every-3rd-turn note) | - |
| `read_window` | read_file pages are >=100 lines (default when `length` omitted/0, and a floor for smaller requests) | - |
| `mask_stale_reads` | read_file results older than the last 3 tool turns -> `[read PATH lines a-b of N; omitted -- re-read if you need it]` when the file was not edited since; prompt copy only, transcript intact | `budget_reads_masked` |
| `prefetch_excerpt` | first prompt gets the target's first 40 lines + outline (or `PATH:A-B` lines), <=6000 chars. Specs: `--prefetch-excerpt`, `Prefetch:` lines in the task / TASK.md / AUTO-TASK.md, else the task's declared entry/required files | - |
| `checkpoint_revert` | tracks the best verify score (fewest failures; own-verify runs + harness verify), snapshots the touched files at each new best; after 3 consecutive edit rounds that each worsen the score, restores the best files and says so | `budget_reverts` |
| `read_streak_nudge` | after 5 (and 10) consecutive read-only turns with no write: "write a first draft now" (no tool_choice forcing) | `budget_streak_nudges` |

## Tests and canary

`bin/test-worker-budget-policy.py` (unit + real `run_task` with a stubbed model, one script per policy,
default-off byte-identity). Canary seams `budgetpolicy` (+ shared `budgetvisible`, `budgetreadwindow`,
`budgetmask`, `budgetprefetch`, `budgetrevert`, `budgetstreak`), each red-on-revert via
`pipeline-canary.py --prove --only <seam>`.

## Live-file note

`ollama-worker.py` is spawned per job: edit atomically (temp file, test, `os.replace`). `worker_budget.py`
is imported at worker start (a failed import leaves `_bb=None` and the worker runs unchanged). No daemon
restart is needed for any of this.
