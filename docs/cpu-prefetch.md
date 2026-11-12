# CPU prefetch (start queued work's CPU prep early; the GPU never waits on CPU)

Added 2026-10-09. Tool: `~/bin/cpu-prefetch.py` (+ launchd `com.example.cpu-prefetch`, one pass per minute).
Tests: `test-cpu-prefetch.py` (marker `CPU_PREFETCH_OK`); canary seam `cpuprefetch`, 13 revert proofs
(`python3 ~/bin/pipeline-canary.py --prove --only cpuprefetch`). No queue-daemon change, no daemon restart.

## What was established (code read; line numbers as of this commit)

**Who launches drivers today.** Nobody launches them ahead of need.
- First driver: an operator or agent runs `ollama-dispatch-auto ...` (or `ollama-dispatch-slice <plan> --execute`)
  by hand. `do_auto` (`ollama-dispatch-auto:6156`) then runs scaffold + prechecks (CPU) -> author the harness
  (GPU row, driver polls) -> `_preflight_loop` (`:6449`: harness self-check, preflight, relevance = CPU) ->
  enqueue the implementation row (GPU) -> poll -> refine rounds.
- Continuations of a plain chain are the SAME process (it polls its own rows; chain state in
  `auto-runs/<bundle>.json`, `chain_state_write` `:410`). A driver that exits (park, crash, stale-code
  re-exec) is only restarted by `dispatch-self-heal.resume_auto_driver` (`dispatch-self-heal.py:1731`,
  after a PASSED needs_opus continuation, pid-reuse safe via `_driver_alive` `:1686`) or by hand with
  `--resume-harness` (argv recorded by `record_argv` `:5913`).
- Sliced plans: the slicer launches one `ollama-dispatch-auto` per slice (`ollama-dispatch-slice:4905`), each
  cut off the previous slice's LANDED tree, so slice N+1 genuinely depends on slice N. The queue daemon only
  fires the slicer's `--advance-detached` (`ollama-queue.py:3358 _bundle_kick`, condition `:3514`) for the
  COMMITTED bundle after it idled `BUNDLE_KICK_AFTER_S` (`:1722`) -- i.e. while it already owns the lanes.

**What can run before its GPU predecessor finishes.** Per piece: CPU0 (scaffold, spec/baseline prechecks) has
no GPU dependency; author (GPU) -> CPU1 (self-check, preflight, relevance) needs the authored harness;
implement (GPU) -> CPU2 (gate/relevance hook). Across pieces: independent bundles and the first piece of each
bundle are independent of everything in flight; step N+1 of a chain/slice plan is NOT (it is cut from N's
landed tree). So the early-startable set is: queued independent work that has no driver yet, and an authored
harness whose driver is gone (the rt-walmart-cancel-import `--resume-harness` case).

**Why the GPU idled.** Measured on the live queue (lane `studio-db`, last 13.4 h, lane occupancy from
`launched_at` -> log mtime): 8.47 h idle in 26 jobs, 7.87 h of it (18 gaps) with the next job not yet enqueued
(1.38 h same-bundle follow-on rounds, 6.49 h cross-bundle). Only ONE driver was ever in flight, launched
reactively, so while it prepared its next row nothing else was ready. (Some of that idle had nothing
else queued at all -- no mechanism can fill that; it is the addressable ceiling, not a promise.)
a7c2ba3 already releases the lanes of a bundle in a CPU-only step (`bundle_commit_status` `ollama-queue.py:2361`,
`CHAIN_CPU_ONLY_STEP_RE` `:1268`, parked kind `cpu_wait` `:2605`); that lets OTHER bundles backfill but only
helps if another bundle HAS a ready row, which needs a second driver already prepping.

**CPU lane limits.** Unraid runner `concurrency: 2` (`cpu_lane.py:59`); `cpu_stage.eligibility` (`cpu_stage.py:241`)
keeps non-node projects, network/secret/host-path stages and `node:test mock.module` fixtures local
(`cpu_stage.py:266`, runner is node 22); `auto-harness-check.py` ships to the runner. Local fallback registers no
marker by default. Mac: 16 cpus shared with the owner's interactive work and the GPU job's `run_bash`.

## The mechanism: an explicit backlog + a one-minute scheduler pass

Why a separate watcher, not a daemon pass: drivers are already launched by out-of-daemon actors
(self-heal, agents) as detached processes; the daemon is a 19k-line single process whose restart is gated and
that owns GPU lane decisions only; a standalone pass is deployable/revertable (`launchctl bootout`) with no
restart, and fails closed (queue state unreadable -> does nothing). Why an explicit backlog: argv records
outlive their work (rows get pruned, chains end `exit 0`), so discovering "unfinished" work from them would
resurrect landed work. An entry is the operator's statement "this is queued".

```
cpu-prefetch.py add --label L [--bundle B] [--priority N] -- <ollama-dispatch-auto argv>   # kind=start
cpu-prefetch.py add --label L --resume        # authored harness, driver gone (argv record + --resume-harness)
cpu-prefetch.py status | hold L | unhold L | cancel L | requeue L | --once [--dry-run]
~/.ollama-dispatch/prefetch/{backlog/,holds.txt,config.json,decisions.jsonl,launch.lock}
kill switch: touch ~/.ollama-dispatch/cpu-prefetch.disabled
```

**Admission.** `slots = min(max_prep - drivers_advancing, target_ready - (ready_bundles + drivers_advancing))`
(defaults 2 / 3), zero when Mac 1-min load >= 75% of cpus or the CPU lane has >= 2 running+pending remote
stages. `ready_bundles` = bundles with a running or runnable pending MODEL row (what the GPU can start now);
`drivers_advancing` = alive drivers (command verified, pid-reuse safe) in chain phase `advancing`, plus ones
this tool spawned in the last 10 min that have not written a record. So prep happens only while the runway
is short, never beyond `max_prep`, and never touches the GPU: if prep is still running when the GPU frees,
the queue backfills with what IS ready (unchanged); a finished prep is enqueued by its own driver at once.

**Order** (when the GPU will need them): bundle the queue resumes first (parked `cpu_wait`/`yielded`) or has
committed, then `kind=resume` (GPU authoring already spent), then priority, then FIFO.

**Safety gates** (each skip is named in `decisions.jsonl`/`--dry-run`): entry hold, `holds.txt`; bundle parked
in the queue (anything but benign `cpu_wait`/`yielded`); a `needs_opus` or human-held row of the bundle;
an unchecked backticked row for the label/bundle in `READY-TO-LAND.md`/`ESCALATIONS.md` (held HARNESS-GO /
READY-TO-LAND chains such as rt-giftcard-ocr-ingest, rt-costco-receipt-attach); superseded/accepted bundle;
slicer-owned bundle (unfinished slices in `slice-runs/<bundle>.json`); a live driver for the label (ps by
`--label` AND chain record, command-verified); worktree with a held `dispatch-tree.lock` (non-blocking flock
probe), a `.hand-harness`, or a live queue row whose cwd is the worktree; `kind=resume` needs TASK.md +
refimpl.py + verify.sh. Launch is under `launch.lock` (flock): decide + spawn + persist `launched` are atomic,
an entry launches once, a vanished driver reconciles to `done`/`failed` and is never auto-relaunched
(`requeue` is explicit). Launched like self-heal does: detached `start_new_session`, log
`auto-runs/logs/<label>-prefetch-<ts>.log`.

## Feeding the backlog (2026-10-09, what registers and what must not)

Registration is explicit and conservative; `cpu-prefetch.register()` is the one entry point (idempotent, never raises,
never resurrects a done/failed/cancelled entry; `requeue` or `force=True` re-arms). The launch-time gates above stay the
single authority on whether a registered entry may START.

| New unit of work | Registers? | How |
|---|---|---|
| (a) new `ollama-dispatch-auto` run (bundle intent exists, nothing running yet) | YES, on request | `ollama-dispatch-auto <same args> --prefetch` registers the invocation (kind=start) and exits without running a driver. A plain launch registers nothing (it is already running; nothing to prefetch). Needs `--label`. |
| (b) `dispatch-self-heal.resume_auto_driver` after a passed needs_opus continuation | NO normally; YES only if its immediate launch raised | The resume stays IMMEDIATE: it is a stuck chain whose GPU authoring is already spent, it is capped (2/label/day, 6/day) and a prefetch admission/headroom wait could only make it wait longer. If `launch` raises, the converged harness is handed to the backlog as kind=resume (`deferred:registered`) instead of being lost. |
| (c) driver died with an authored harness | Operator-vetted | `cpu-prefetch.py candidates` lists argv-recorded runs with an authored harness, no live driver and no `exit 0` chain record (ADVISORY: many have landed; vet each), then `cpu-prefetch.py add --label L --resume`. Never auto-registered: records outlive their work. |
| (d) slicer-plan slice N+1 | NEVER | N+1 is cut from N's landed tree; the slicer owns the bundle (`slicer_owns` also blocks at launch). |

Never started regardless of registration: held READY-TO-LAND/HARNESS-GO labels (rt-giftcard-ocr-ingest, rt-costco-receipt-attach),
parked / needs_opus bundles, chains that ended `exit 0` (entry terminal, record gate). Tests: `test-prefetch-register.py`
(`PREFETCH_REGISTER_OK`), canary seam `prefetchfeed` (6 revert proofs).

## Operating
- Deploy: already installed (`~/Library/LaunchAgents/com.example.cpu-prefetch.plist`, StartInterval 60). Nothing
  to restart: not the queue daemon, not the API. After editing the script, nothing either (each pass is a fresh process).
- Pause: `touch ~/.ollama-dispatch/cpu-prefetch.disabled`. Remove entirely: `launchctl bootout gui/$(id -u)/com.example.cpu-prefetch`.
- Tune: `prefetch/config.json` `{"max_prep":2,"target_ready":3,"load_frac":0.75,"lane_busy_max":2}`.
- Usage pattern: instead of launching drivers one by one as the GPU drains, `add` the whole backlog; the pass
  keeps the runway at `target_ready` bundles.
- Known limits: the runway is an approximation from queue rows (not model-time remaining); a slice plan's own
  step N+1 is still owned by the slicer (dependency); kind=start entries run scaffold, which needs network
  (`npm ci`) and stays local by design.
