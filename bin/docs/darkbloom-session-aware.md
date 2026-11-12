# Plan change: session-aware Darkbloom model set (amends tender-tiger §11–14)

## Context
The owner (2026-10-15): while we're working, Darkbloom should serve only the models we use, so network jobs for Gemma don't overlap with our queue work. After 30 min idle, go back to serving Gemma for earnings. This replaces the "35B+9B pinned, Gemma off" rule (memory `project_darkbloom_two_model_pin`, 2026-10-13) with a schedule driven by activity.

## Behaviour
- **Active** (a Claude Code prompt or tool call in the last 30 min, OR any queued or running local job): the advertised set is the pair only (`qwen3.6-35b-a3b-vl-mtp-mxfp8` + `Qwen3.5-9B`).
- **Idle** (neither of those for 30 min): all three advertised (pair + `gemma-4-26b-qat-4bit`). The provider handles eviction; the memory gate keeps two resident.
  *Update 2026-10-24:* idle is now the Autopilot-selected set (`selected_models` in provider.toml) minus gpt-oss, primary (35B) then `Qwen3.5-9B` first (`idle_models()` in `bloom_control.py`). The denylist is env `BLOOMCTL_EXCLUDE_MODELS` (default `gpt-oss-20b`, case-insensitive, any `gpt-oss` substring). An empty or unparseable list falls back to the old pair + Gemma list. `BLOOM_IDLE_AUTOPILOT=0` forces that old list; `BLOOM_IDLE_GEMMA=0` is still full legacy. The active/hold path stays the pair only.
- Each switch is a restart (Autopilot stays enrolled per §13b; `darkbloom switch` is broken, #1469). A restart takes about 1–6 min, so the first local job after idle waits that long. The queue treats it as an infra wait, never a failure.

## Implementation (reuses existing code; no new Mac services)
1. **Activity signal:** a tiny UserPromptSubmit and PostToolUse hook in `~/.claude/hooks/` that touches `~/.ollama-dispatch/claude-active`. A hook, not a daemon.
2. **Decision:** extend `bloom_queue_sync` in `~/bin/ollama-queue.py` (~line 5980), which already runs in the daemon loop. It holds while the queue has work or the activity file is newer than 30 min. Otherwise, after 30 min it releases to the idle set.
3. **Mechanics:** in `~/bin/bloom_control.py`, add an idle three-model set to the existing restart-based path (`desired_toml`, `_wait_for_start`, the flock, idempotence). `hold_for_queue` gets back to the pair. `release_to_bloom` stops being a no-op and moves to the idle set. Gemma is already in the list at line 124. The kill switches stay (`BLOOM_CONTROL=0`, plus a new `BLOOM_IDLE_GEMMA=0` that keeps today's behaviour).
4. **Guards:** never restart with a job running; hysteresis so a stray hook ping doesn't flap; at most one switch per 10 min.
5. Update memory `project_darkbloom_two_model_pin`, `docs/bloom-control.md`, and tender-tiger §13b/§14. Container migration: the hook becomes an HTTP ping later, and the logic doesn't change.

## Scheduling
Dispatched through the pipeline (qwen implements, Claude writes the tests) after the arm C revert/re-flip, as part of the 11:01 run. The daemon restart that activates it happens only at idle.

## Verification
- Unit tests in `test-bloom-control.py` / `test-idle-driverless-hold.py`: active→pair, idle 30m→three, queue work blocks idle, no restart with a running job, hysteresis, kill switch.
- Prove the new canary seam red-on-revert (`pipeline-canary.py --prove`).
- Live: let the system sit idle 30 min and confirm `/v1/models` lists three. Then send a prompt and confirm the pair is back, measuring the restart time (doubles as §14 measurement 1).
