# queue-agent (Mac status service)

Small stdlib service on the Mac. The queue runs in Docker on Unraid after the cutover. This service tells the
container the few things only the Mac knows. Code: `queue-agent.py`. Tests: `test_queue_agent.py` (run on
claude-sandbox only). Launchd template: `LaunchAgents-staged/com.example.queue-agent.plist` (never loaded from here).

## Config and auth

- Config: TOML at `$QUEUE_CONFIG`, default `~/.config/ollama-queue/config.toml`, table `[queue_agent]`
  (a flat `"queue_agent.bind"` key also works). The service reads it with tomllib itself.
  - `bind` (default `127.0.0.1`; the Mac's real config says its ethernet IP), `port` (default 7691)
  - `notify_per_hour` (default 30)
  - `mirror_url` (empty = mirror off), `mirror_interval_s` (default 15), `mirror_token_file`
    (default `~/.config/ollama-queue/queue-api-token`)
- Token: `~/.config/ollama-queue/agent-token`. Without it the service refuses to start unless bind is loopback.
  Bearer compare is constant time (`hmac.compare_digest`). `/health` is open. Everything else needs
  `Authorization: Bearer <token>`.
- Code location: the folder of `queue-agent.py` (override `QUEUE_AGENT_CODE_DIR`). It must sit beside
  `ollama-queue.py`, `cpu_lane.py`, `infra_incidents.py`, `lane_plan.py`, `notify-operator.py` (all in ~/bin).
  They are imported by path, lazily, once. Import failure gives a JSON 503, never a traceback.
- All responses are JSON with `generated_at`. Errors: `{"error", "status", "generated_at"}`.

## Endpoints and sources of truth

| Endpoint | What | Source of truth |
|---|---|---|
| `GET /health` | `ok`, mirror status | none |
| `GET /v1/hold[?model=M]` | hold state | below |
| `GET /v1/mac` | `screen_locked`, `memory`, `load_avg` | below |
| `GET /v1/lane-plan` | vendor-file facts, pid | below |
| `GET /v1/darkbloom-status` | raw `darkbloom status` text (`{"text": ...}`), run with a timeout, cached 10 s | the queue container, when `QUEUE_AGENT_URL` is set |
| `GET /v1/darkbloom-model-configs` | `config.json` of each model in Darkbloom's HF cache, keyed by cache folder name (`{"configs": {...}}`); model metadata only | the queue container (Darkbloom window size and second-slot memory check), fetched outside the state lock, kept an hour |
| `GET /v1/bloomgauge-ledger` | BloomGauge control mode plus the last 256 KB of `bloomgauge-control.jsonl` (`rows` are raw JSON lines; `ledger` = ok / missing / unreadable) | `escalation_index_janitor` (closes `bloomgauge-control-*` alerts), same bearer auth |
| `POST /v1/notify` | notification | below |
| `POST /v1/bloomgauge-tick` | runs the queue's BloomGauge control step here (body `{busy, why, target, ack, epoch}`; answer `{epoch, mode, wanted, started, running, last_step_at, alerts}`) | the queue container's `bloomgauge_hook`, when `QUEUE_AGENT_URL` is set |
| `GET /v1/bloomgauge-tick` | the same answer without running a step | the container's `_bloomgauge_control_mode` on a cold cache |

### /v1/hold (gap 2)

It calls the queue's own code, not a copy of it.

- `view` = `ollama-queue.py:6052 darkbloom_view()`. It reads the last `DB_LOG_TAIL_BYTES` (65536, :5941) of
  `~/.darkbloom/provider.log` (env `DARKBLOOM_PROVIDER_LOG`, :5934) through `darkbloom_down_cause_text` (:5967),
  and the last line of `~/.darkbloom/sandbox-slots.log` (env `DARKBLOOM_SLOTS_LOG`, :5936) through
  `_sandbox_slots_jobs` (:6038). Keys: `cause`, `hold`, `lease_s`, `renewals`, `cache_path`, `log_age_s`, `sandbox_jobs`,
  `at`, `unreadable`, plus `hold_since` and `locked` (below).
- `hold`, `cause` = `cpu_lane.py:322-340 model_hold(fresh=True)` run on that same view. `cause` is cut to 120 chars
  and empty when not held. This is the answer the CPU lane gives runners (503 `sandbox_hold`).
- `hold_wait` (only with `?model=`) = `ollama-queue.py:6087 darkbloom_hold_wait(model, now, view=view)`: the INFRA
  WAIT note, or null.
- `hold_since`: the queue daemon keeps it in `_DB_TRANS` while priming every tick (:6177-6185). The agent keeps the
  same transition itself: set on the first held view, cleared on the first readable not-held view. It resets if the
  agent restarts.
- `locked`: set on the view only when the cause is `model-cache-unreadable`, from `infra_incidents.screen_locked()`
  (same as `prime_darkbloom_view`, :6171).
- `inputs`: path, `exists`, `mtime`, `age_s`, `size` for `provider_log` and `sandbox_slots_log`. Contents are not sent.
- `freshness_s`: `hold_stale` 180 (:5942), `view_fresh` 120 (:5943), `slots_fresh` 300 (:5946).
  Server clock `generated_at` and the view's `at` come from the Mac clock.
- `sandbox_hold_wait_enabled`: the `SANDBOX_HOLD_WAIT` kill switch as the Mac sees it (:5947).
- `policy`: always `"fail-open"`. An unreadable log is not held (`unreadable: true`). The agent being down or
  answering 503 must also mean not held in the container. That keeps today's policy. Not changed here.

Container side. Use `view` as the `_DB_VIEW` replacement and call `darkbloom_hold_wait(dbm, now, view=view)`.
Before that, rewrite `view["at"]` to the container's receive time, because `darkbloom_hold_wait` compares `at` with
the local clock (:6094). The `log_age_s` inside the view was already computed on the Mac, so skew does not touch it.

Inputs the queue reads that are NOT served: `daemon-state.json` `addon_hold` is not read by any code in `~/bin`
(grep, 2026-11-12). The plan mentioned it; today's hold comes only from the two logs above.

### /v1/mac (gap 11)

- `screen_locked`: `infra_incidents.py:90 screen_locked()` (ioreg, cached 30 s). True, False or null.
- `memory`: `sysctl -n hw.memsize` and `vm_stat`, same arithmetic as `ollama-queue-api.py:5097-5138`
  (`_parse_vm_stat`, `_studio_system_mem_gb`): used = total minus free, inactive, speculative and purgeable
  pages. Fields: `used_gb`, `total_gb`, `page_size`, `pages` (free, active, inactive, speculative, purgeable,
  wired). All null on a non-Mac.
- `load_avg`: `os.getloadavg()`.
- Note: this is a re-implementation of `_parse_vm_stat`, because importing the 11.6k-line API file starts too much.
  The test `test_memory_used_arithmetic` pins the formula. If the API one changes, change this one.

### /v1/lane-plan (gap 6)

`lane_plan.py:183-190` file list and `:328-330` pid liveness (`take_snapshot`, :345-460). Keys:
- `files`: path, exists, mtime, age_s, size for local.json, daemon-state.json, loaded-models.json, provider.pid,
  provider.toml, hosts.json, bonsai-host. (`model_profiles.yaml` ships in the image, not served.)
- `facts`: `local_json` (base_url, `has_api_key` bool, pid, version, updated_at), `daemon_state` (pid,
  start_time_micros, version, advertised_models), `loaded_models` (parsed), `provider_toml_served` (the
  enabled/preload model ids), `provider_pid` (pid, `alive` from `os.kill(pid, 0)`), `hosts`, `bonsai_url`.
- `snapshot`: `lane_plan.take_snapshot(...).public()`, the exact object the planner builds. It never holds the key.
- The api key is never served. After the cutover the container gets the key from env (`DARKBLOOM_API_KEY`).

Container side: lane_plan in the container needs a `Sources`-like input from this endpoint. That wiring belongs to
the config/image branches. Until it exists, the planner must stay in its default SHADOW mode.

### POST /v1/notify (gap 11)

Body: `{"title", "body", "url"?, "dedupe_key"?, "dedupe_s"?}` (`message` is accepted for `body`).
- Limits: title 1-120 chars, one line; body 1-1000 chars; no control characters (newline allowed in body); url must be
  http(s), at most 300; dedupe_key 1-120 chars of `[\w.:/@+-]`; dedupe_s 1..604800 (default 3600); request body at most 8 KB.
- Runs `notify-operator.py notify()` in-process (argv lists only; no shell; its osascript text is escaped there).
  Pushover when the Keychain has the tokens, else desktop, always one line in `notify-operator.jsonl`.
- Dedupe is notify-owner's own (`notify-operator.py:35-52`, `~/.ollama-dispatch/notify-dedupe.json`). Result
  `channel` can be `deduped`.
- Rate limit: 30 accepted requests per sliding hour (config `notify_per_hour`), then 429 with `Retry-After`.
  Invalid requests do not count.

## Mirror (gap 10)

Off unless `queue_agent.mirror_url` is set. One background thread pulls every `mirror_interval_s` and writes the files
the hooks read today. Auth to the container: Bearer token from `mirror_token_file` (the container's `QUEUE_API_TOKEN`).

Files written on the Mac (all under `$QEL_DIR`, default `~/.ollama-dispatch`):
- `queue-events.jsonl`: one JSON line per event, `seq` ascending. Read by `queue-events-deliver.py` (hook script
  `~/.claude/hooks/queue-events-inject.sh`). Writer today: `queue-event-log.py:345-362`. The agent appends only events with
  `seq` above the file's highest seq, ends a half line first, rotates to `.1` above 1,000,000 bytes (same as the writer).
- `queue-events.failed.json`: `{"ts", "failed": [...]}`, read at `queue-events-deliver.py:365`, written at
  `queue-event-log.py:364`.
- `escalations/ESCALATIONS.md` and `escalations/READY-TO-LAND.md`: whole-file atomic replace when the text changed.
  Read by `~/.claude/hooks/session-start-escalations.sh` (rows start with `- [ ] `). Writer today:
  `dispatch-escalation-watcher.py:93,98`. Only these two names are ever written. A null file in the response is
  left alone (never deleted).

### Container-side routes the mirror needs (do not exist yet; the dashboard branch builds them)

Both are GET, need the Bearer token, return JSON.

`GET /api/mirror/events?after=<seq>&limit=<n>` (limit at most 200, default 200)
```json
{"generated_at": 1760000000.0,
 "events": [{"seq": 41, "ts": "2026-11-12T10:00:00+0000", "id": "job", "label": "x", "state": "done",
             "verdict": "...", "launched_by": "...", "launched_by_session": "..."}],
 "last_seq": 41,
 "failed": {"ts": 1760000000.0, "failed": [{"id": "job"}]}}
```
`events` are the container's `queue-events.jsonl` lines with `seq > after`, oldest first, same fields the writer emits.
`failed` is the current `queue-events.failed.json` content (or null).

`GET /api/mirror/escalations`
```json
{"generated_at": 1760000000.0,
 "files": {"ESCALATIONS.md": "- [ ] ...\n", "READY-TO-LAND.md": null}}
```
`files` holds the full text of the two files in the container's `.ollama-dispatch/escalations/`, null when absent.

Rules for cutover:
- Copy the Mac's `queue-events.jsonl*` and `queue-events.state.json` into the container first. The writer never
  reuses a seq (`queue-event-log.py:278-279`); if the container started at seq 1 the Mac would ignore its events
  because they are below the Mac file's highest seq.
- Stop the Mac's `com.example.queue-event-log` and the Mac escalation watcher at the same time, or two writers race
  on the same files.
- Not mirrored (still need an answer from the other branches): `ollama-land summary`, `handoff-emit.py --json`,
  the per-session cursors under `queue-events.sessions/` (these stay on the Mac, the hook writes them),
  `ollama-queue-watch-reminder.sh --inflight` (reads the queue state file: needs the API).

## What this service does not do

No Darkbloom proxy, no key, no launchctl. It reads files, runs `darkbloom status` (read-only, with a timeout), runs
the notify path, and runs the queue's BloomGauge control step (below).

### BloomGauge control step (2026-11-12)
The step (`bloomgauge_tick.tick`) talks to the BloomGauge app on the Mac's 127.0.0.1, so for a queue in the container it
runs here. The container's `bloomgauge_hook` POSTs `{busy, why, target}` from a thread at most every 10 s (never from the
tick itself); this service keeps `bloomgauge_tick`'s own throttle (one step per `tick_s`, never two at once), with the
Mac clock. Its mode (`bloomgauge_control.config()`: env `QUEUE_BLOOMGAUGE_CONTROL` over `~/.ollama-dispatch/bloomgauge-control.mode`)
is the authority, also for `/v1/bloomgauge-ledger`; if a queue plist ever sets that env, this service's plist needs it too.
Alerts the step raises are kept (at most 50) until the container acks them (`ack` = highest `seq` seen, within the
service's `epoch`; a restart starts the numbers again), are written by the container through `_control_alert` (the
mirror brings the ESCALATIONS.md row back), and go through the Mac notify path at once (deduped per key for an hour;
the container has no desktop). The lease is saved with `daemon_pid` null: this service outlives the queue, so the
stale-lease guard (`bloomgauge_lease_guard.py`, Mac launchd) goes by the heartbeat alone, which stops when the
container stops posting. At most one queue may run the step: a Mac queue kept as a backup stays stopped while the
container is primary (control 'off' is not enough there: with a lease present, 'off' still hands the Manager back).

### Container readers (A2/A3)
With `QUEUE_AGENT_URL` set the queue reads: provider.log and sandbox-slots.log via `/v1/hold` (`view`, clock reset on receipt);
provider.toml and loaded-models.json via `/v1/lane-plan` `facts` (cached 10 s, never fetched under the state lock);
`darkbloom status` via `/v1/darkbloom-status`. The janitor reads the ledger via `/v1/bloomgauge-ledger` once per run.
An agent that does not answer reads as "no file" (fail-open; the janitor does not close). Unset = today's file paths.
Not served: keepwarm state (keepwarm stays on the Mac; the container sees none) and `model_cache_directory` (opt-in, off by default).
