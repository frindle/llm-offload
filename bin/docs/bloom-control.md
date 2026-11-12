# bloom-control: session-aware Darkbloom set (plan sleepy-dijkstra s15)

Files: `bloom_control.py` (state machine + `session_decision`, `set_models`), `ollama-queue.py` (`bloom_queue_sync`, runs each daemon tick), `claude-active-touch.sh` (activity beacon hook, NOT registered in settings.json: needs the owner's sign-off).

## Behaviour
- Pair = Qwen3.5-9B + qwen3.6-35b-a3b-vl-mtp-mxfp8. Gemma = gemma-4-26b-qat-4bit.
- Queue work: pair held (unchanged legacy behaviour).
- No queue work, Claude active (beacon `~/.ollama-dispatch/claude-active` touched < 30 min ago): pair HELD, not released.
- Idle >= 30 min: release, Gemma added back.
- Activity must be sustained `debounce_s` (60s) before the pair is grabbed (hysteresis; one stray tool call does nothing).
- At most one set change per `switch_min_gap_s` (600s).
- No set change while a job is running (restart would kill it).
- Change method: `darkbloom switch --model ... --timeout drain_s` first (no restart); wait `switch_wait_s` (120s) for the set to be served; otherwise fall back to `darkbloom start`.

## Kill switches / knobs (env)
- `BLOOM_CONTROL=0`: whole hook off.
- `BLOOM_IDLE_GEMMA=0`: pre-s15 behaviour (release the moment the queue is done, restart only, no switch).
- `BLOOMCTL_ACTIVITY_FILE`, `BLOOMCTL_IDLE_S` (1800), `BLOOMCTL_DEBOUNCE_S` (60), `BLOOMCTL_SWITCH_MIN_GAP_S` (600), `BLOOMCTL_SWITCH_WAIT_S` (120).

## State (`bloom-control-state.json`)
mode, phase (switching/ready/release-pending/failed), gemma_removed, act_first (start of the current activity run), last_switch_at.

## Tests
- `test-bloom-control.py` scenarios `session-aware`, `session-aware-queue` (fake darkbloom shim handles start + switch; `switch_rc` file forces a switch failure).
- `test-idle-driverless-hold.py` (queue done + active Claude keeps the pair; 30 min quiet releases).
- Canary seam `sessionaware` (`pipeline-canary.py --prove --only sessionaware`: 5 reverts, all RED).

## Activation
The running queue daemon has the old code until restarted. Restart only when no job is running. Register the hook only after sign-off.
