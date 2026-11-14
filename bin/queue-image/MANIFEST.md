# Queue image manifest: how it was made

`MANIFEST` lists the files the container needs, one path per line, relative to `~/bin`. `MANIFEST.tests` lists the tests for those files.

## How the list was found
1. Start from the 8 queue jobs: the daemon (`ollama-queue.py`), the api (`ollama-queue-api.py`, lives in the dashboard repo), `queue-event-log.py`, `cpu-prefetch.py`, `dispatch-ack-reconcile.py`, `dispatch-escalation-watcher.py`, `verify-orphan-reap.py`, `log-rotate`.
2. Follow the closure: `import` and `importlib` loads, `subprocess` calls of other `~/bin` scripts, files the workers exec, and the tool files `cpu_stage.py` ships to the CPU runner (`_TOOL_FILES`). This gave 128 files by script, plus a few hand-added ones (see below). 133 are listed.
3. Groups in `MANIFEST` carry a `# why` comment each.

## Left out, and why
- Mentioned only in docs or comments, never loaded by code: `bloom_power_lib`, `bloom_ledger`, `mini-bench`, `doc-summary`, `mini-answer`, `qwen-dispatch.sh`, `start-llama-server-bonsai.sh`.
- `graphify-lane`: it is not a queue job. It runs as its own launchd job.
- Tests are not in `MANIFEST`. They are in `MANIFEST.tests` (293 tests that name a manifest file, plus `test_queue_config.py`).

## Things to know
- Dashboard files (`ollama-queue-api.py`, `bundle_view.py` and the others) are symlinks into the dashboard repo, and `logq` points into the metrics stack. Copy the targets, not the links.
- `ts-mutator/` (node files that `cpu_stage.py` ships) is not tracked in git. Add it to the image by hand if CPU stages use it.
- Mac-only members are listed because code imports them: `bloomgauge_*`, `darkbloom-field-probe*`, `darkbloom-keepwarm.py`. They do nothing useful without the Mac. Open question: ship them or stub them.
- The alert script was renamed to `notify-operator.py` (its log is `notify-operator.jsonl`, its symbols `notify_operator`) so no name is in the code. Its old alert-dedupe history starts fresh.
- `QUEUE_API` clients still point at `127.0.0.1:7684`. That is for the later task.
- `log_rotate_lib.py` keeps a `handoff` path under `~/Desktop/GitHub Projects`. Open question for the image.

## What changed in code for the image
- `queue_config.py`: lookup order is the legacy env name, then `QUEUE_<KEY>`, then TOML (`$QUEUE_CONFIG` or `~/.config/ollama-queue/config.toml`), then the default. `state_dir()` is `$QUEUE_STATE_DIR` or `~/bin`. See `config.example.toml` for every key.
- State, locks, logs and live logs use `state_dir()`. Code stays at `~/bin`.
- Homelab addresses, domains, the operator name and `/Users/<name>` paths moved to config or `Path.home()`.
- `cpu_lane._loopback_base` also accepts exact IPs from `darkbloom.upstream_hosts`.

## Checks
- `scan-private-info.py --list MANIFEST ...` exits 0. Private patterns live outside git in `~/.config/ollama-queue/private-patterns.txt`. The allowlist is `scan-allow.txt`.
- `check-mac-config.py` prints old value, new value and SAME/DIFF for each moved value. Old values live outside git in `~/.config/ollama-queue/old-values.toml`.
