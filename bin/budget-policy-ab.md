# Budget-policy A/B (how to run it; NOT started)

Question: do iteration-budget *structure* policies beat the current flat cap (14/24), or is cap size the
lever? Evidence so far disagrees (local: 23-30% of flat-author jobs hit the cap, 160 converged on exactly
their last iteration, ~80% of capped jobs wrote in their last 3 iterations; outside: read windows, stale-
observation masking, visible budget, best-so-far checkpointing). So: an A/B, not a default change.

Policies live in `~/bin/worker_budget.py`, wired into `ollama-worker.py`, all OFF by default
(`machine-config/docs/budget-policies.md` for details):
`progress_extend, budget_visible, read_window, mask_stale_reads, prefetch_excerpt, checkpoint_revert,
read_streak_nudge` (`all` = every one).

## Arms

| arm | `Budget-policy:` value | tests |
|---|---|---|
| A  | `none` (arm line omitted) | baseline: flat cap, byte-identical prompts |
| B  | `progress_extend,budget_visible` | does a conditional +8/+12 (<=1.5x) convert capped-but-progressing jobs? |
| C  | `read_window,mask_stale_reads,prefetch_excerpt` | do cheaper/denser reads get the first write earlier? |
| D  | `all` | everything incl. `checkpoint_revert`, `read_streak_nudge` |

Hold constant per task: model (`qwen3.6-35b-a3b-vl-mtp-mxfp8`, what the originals ran), `--num-ctx`,
`--max-iters` (the original job's value), verify command, temperature profile. One variable: the arm.
Replicates: Darkbloom sampling is not deterministic, so run **2 reps per (task, arm)** minimum, otherwise
a single capped/converged flip is noise (the original jobs themselves are a 3rd, uncontrolled A sample;
do not pool them with the replays).

## 1. Pick the replay set

```
python3 ~/bin/budget-policy-ab-select.py -n 40 --json > /tmp/ab-set.json     # read-only
python3 ~/bin/budget-policy-ab-select.py -n 40                               # human view + why others were excluded
```
Today (2026-10-09, since 2026-09-12): 94 replayable authoring jobs (74 originally converged, 20 capped).
Take ~20 tasks stratified: ~10 that originally hit the cap (where the policies can matter) + ~10 that
converged (to show no harm / cost). Prefer distinct `cwd`s.

### Is replay feasible with existing tooling? Partly. What is exactly missing

Have: the task text (transcript `task`), start HEAD sha + the *names* of untracked files at start
(`worktree_start_snapshot`), verify cmd / max_iters / num_ctx (queue row), the scaffold files still sitting
in the original worktree.

Missing (so these were excluded by the selector, 3/4 of all authoring jobs):
1. **Contents of untracked files at job start.** The snapshot stores only `?? name`. Round-1 creation jobs
   are fine because the only untracked files are frozen scaffold (TASK.md, verify.sh, verify.test.*,
   refimpl.py, check_literals.py, ...), which still exist; continuation/refine jobs started with a
   half-written target whose bytes were never archived (130 + 297 excluded).
2. **Scaffold-at-start proof.** Scaffold files are untracked, so nothing proves they are unchanged since
   that job (a later refine can rewrite verify.test.ts). Replay uses the *current* scaffold; accept that, or
   diff it against the job's task text / `verify` expectations before trusting a pair.
3. **Reaped worktrees** (170) and **missing queue rows** (184, so verify/max_iters unknown).
4. The transcript does not store the queue job id; the selector joins by cwd + enqueue time.

The durable fix (not done here, `ollama-queue.py`/`cpu_stage.py` are owned by another agent): archive the
start tree (`git stash create` + a tar of untracked files) next to each transcript at launch.

## 2. Build one replay worktree per task (shared by all arms is WRONG; one per arm x rep)

For each selected row (`start_head`, `cwd`, `scaffold`):
```
R=~/.ollama-dispatch/worktrees/ab-<jobtag>-<arm>-r<rep>
git -C <repo-of-cwd> worktree add --detach "$R" <start_head>
for f in <scaffold...>; do cp -p "<cwd>/$f" "$R/$f"; done
# the target file(s) must NOT exist (round-1): confirm with: test ! -e "$R/<entry point>"
```
The repo of `cwd` is `git -C <cwd> rev-parse --git-common-dir`. Env parity first (node_modules etc.: same
`--setup` the original used; see feedback_worktree_env_parity). Then write the arm into the task:
```
cp "$R/AUTO-TASK.md" "$R/AUTO-TASK.md.orig"       # or TASK.md; whichever the original used
printf '\nBudget-policy: %s\n' "progress_extend,budget_visible" >> "$R/AUTO-TASK.md"   # arm B; omit for A
```
(The retired old authoring driver did the same append for fresh authoring rounds. Per-job env is
not available through the queue, so the **task-text `Budget-policy:` line is the passthrough**. The
daemon's own env `WORKER_BUDGET_POLICY` would flip every job, do not use it for an A/B.)

## 3. Enqueue as ONE bundle on the idle queue

```
B=ab-budget-$(date +%m%d)
for each (task, arm, rep):
  python3 ~/bin/ollama-queue.py enqueue --model qwen3.6-35b-a3b-vl-mtp-mxfp8 --host auto \
     --cwd "$R" --task-file "$R/AUTO-TASK.md" --task-kind coding --api openai \
     --num-ctx <row.num_ctx> --max-iters <row.max_iters> --verify '<row.verify>' \
     --label ab-<arm>-<jobtag>-r<rep> --bundle "$B" --no-split
```
Keep labels `ab-<arm>-...` so metrics can be grouped (the `budget_policy` metric field also records the
arm). Order inside the bundle: by *task* first, then arm, then rep (depth-first, minimizes model swaps; the
queue owns fit-routing; do not interleave other bundles: bundle -> completion). Start it when the queue is
idle (`ollama-queue.py status` shows no running/pending) and do not promote it over real work.
Expected size: 20 tasks x 4 arms x 2 reps = 160 jobs (~6-10 min each at 12-24 iterations): ~20-25 GPU
hours, so start with 8 tasks x 4 arms x 2 reps = 64 jobs.

## 4. Measure

Per job (from `dispatch-metrics.jsonl` rows with `task_preview`/`cwd`, filtered to label `ab-*`; fields:
`status`, `verify_passed`, `iterations`, `calls`, `sum_completion_tokens`, `budget_policy`,
`budget_base_cap`, `budget_extension_granted`, `budget_extension_refused`, `budget_reverts`,
`budget_reads_masked`, `budget_streak_nudges`, `budget_events`; GPU seconds = sum of `turn_log[].elapsed_s`):

| metric | definition |
|---|---|
| convergence rate | converged & verify_passed / jobs, per arm (Wilson 95% CI; 2 reps x N tasks) |
| iterations | mean/median iterations of converged jobs; cap-hit rate |
| GPU-seconds per converged job | total `sum(turn_log.elapsed_s)` over ALL jobs in the arm / converged jobs (failures still cost) |
| first-write iteration | first `write_file`/`edit_file` OK in the transcript (the mechanism claim) |
| extension yield (B, D) | granted / cap-hit, and converged-after-grant / granted |
| harm check | tasks that converged in A but not in the arm; revert count and whether a revert preceded a converge |

Decision rule (state before running): adopt an arm as default only if its convergence rate is >= A's and
GPU-s per converged job is lower, with at least 10 percentage points on capped-original tasks, in 2/2
reps on a majority of tasks. Otherwise keep the flat cap and ship only the single policies that moved a
mechanism metric (e.g. first-write iteration) without hurting convergence. Add a per-policy ablation arm
(one policy at a time) only for the arm that wins.

## Known limits of the instrumentation

- `progress_extend` grants +8 (+12 refine) but is clamped to 1.5x the original cap: with cap 14 that is
  +7, with cap 24 it is +8/+12 (to 32/36). One grant per process; a resumed process starts fresh.
- Verify "output differs" ignores durations/timestamps; for non-deterministic verifies it can over-grant.
- `checkpoint_revert` only restores files edited through write_file/edit_file (not shell-side edits) and
  only scores verifies it can count (recognized diagnostics, or `N failed`); unscorable runs never revert.
- `mask_stale_reads` changes only the prompt copy; the saved transcript keeps the full reads (so a
  `--resume` of a masked run re-masks from scratch: its read bookkeeping is not persisted).
- Research tasks ignore all policies (coding only).
