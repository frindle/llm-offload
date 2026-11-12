# bloom-control: queue <-> BloomGauge hand-off (event-driven, zero lag)

Replaces the 10-minute idle timer of `bloom-idle-switch.py` (now a retired shim).

## Behaviour
- **Queue has work** (empty -> non-empty runnable transition, seen by the daemon tick): `bloom_control.hold_for_queue()`
  - BloomGauge -> **Manual** (`POST /api/optimizer/control set-automatic enabled:false`, fresh `controlVersion`)
  - Darkbloom auto-update **off** (remembered, restored on release)
  - `provider.toml`: `max_model_slots=2`, `idle_timeout_mins=0`, `preload_models=[pair]` (canonical case; fixes `qwen3.5-9b` vs `Qwen3.5-9B`)
  - `darkbloom start --model Qwen3.5-9B --model qwen3.6-35b-a3b-vl-mtp-mxfp8 --idle-timeout 0 --local-endpoint --timeout <drain>`
    (skipped when the pair is already served; `--timeout` = `BLOOMCTL_DRAIN_S`, default 60 s; `BLOOMCTL_FORCE=1` adds `--force`)
  - waits for `/health` + `/v1/models` + `loaded-models.json` to equal exactly the pair.
- **Queue done** (no lane rows, no gate-on-complete hook, no slicer/chain driver, no bundle `bundle_commit_status` calls "working"):
  `release_to_bloom()` -> `release-pin` + `set-automatic enabled:true`, auto-update re-enabled. No timer.
- Launches wait as **infra-wait** (never a failure) while state phase is `switching` (`bloom_control_hold` via `bloom_idle_hold`).
- While busy and held, the daemon re-verifies the pair every 60 s (drift guard) and re-holds if BloomGauge/anything moved it.

## Safety
- One flock serialises all callers; a second caller re-reads state and no-ops (no double switch).
- BloomGauge's control API is undocumented: every response is shape-checked (`controlVersion` str, `automatic.mode` in `on|manual`).
  On mismatch/outage the Bloom half is skipped (**DEGRADED**: `darkbloom start` only), `state.degraded=true`, daemon prints `!!!`.
- A failed hold/release retries after 60 s (error backoff only) and never blocks the queue beyond the normal `/v1/models` infra-wait.
- Bundles that are parked, user-held or fit-held no longer hold lanes or Darkbloom (`focus_drop_parked`, `fit_hold` in `bundle_commit_status`).

## Turn it OFF
`export BLOOM_CONTROL=0` (daemon env) **or** `touch ~/.ollama-dispatch/bloom-control.disabled` (takes effect next tick, no restart).
Then run `python3 ~/bin/bloom_control.py release` once if BloomGauge was left on Manual.
Remove the file / unset the var to turn it back on (default ON).

## Files
`bin/bloom_control.py` (module + CLI: `state | hold | release [--dry-run]`), queue block "BLOOM CONTROL" in `bin/ollama-queue.py`,
state `~/.ollama-dispatch/bloom-control-state.json`, log `~/.ollama-dispatch/bloom-control.log`,
tests `bin/test-bloom-control.py` (canary seam `bloomctl`, 8 revert proofs), live runbook `bin/bloom-integration-livetest.sh`,
config normaliser `bin/darkbloom-apply-wide-models.sh --pair-config`.
Keepwarm stays off (not needed with pair-only).

## Canary fixture (2026-10-08)
`pipeline-canary.py` runs the queue daemon with this hook ON, so the sandbox must look like a served pair:
`BLOOMCTL_PAIR=<stub model>`, a sandbox `provider.toml` (`enabled_models`), `loaded-models.json`, and a stub
BloomGauge (`BLOOMCTL_BLOOM_URL`). Before this, the sandbox had no `provider.toml`, so every pair-model job sat in
the 900s `HOLD FAILED` infra-wait (canary 75/90 in 22 min, jobs stuck pending), and the hook reached the REAL
BloomGauge on 127.0.0.1:8765 (an isolation leak; outer `BLOOM*` env is now stripped too). Check:
"bloom_control hook held the stub pair". Bisect: `BLOOM_CONTROL=0` gave 89/90 in 121s. Production note: if the
pair cannot be served the queue infra-waits (by design) for `BLOOMCTL_WAIT_S` (900s), retrying every 60s.
