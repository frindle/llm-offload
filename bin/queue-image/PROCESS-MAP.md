# Process map: launchd plists to supervisord

Source: the 9 queue plists in `~/Library/LaunchAgents` (read only, 2026-11-12) and `supervisord.conf` here.
`every.sh` and `throttle.sh` replace launchd's interval and throttle keys (see the header of `supervisord.conf`).

| launchd job | Program and args | Schedule | supervisord program | Log (plist -> container) |
|---|---|---|---|---|
| ollama-queue-daemon | `python3 ~/bin/ollama-queue.py run`, cwd `~/bin` | KeepAlive, RunAtLoad, ThrottleInterval 30, ExitTimeOut 60 | `queue-daemon`: `throttle.sh 30 python3 ...`, stopsignal TERM, stopwaitsecs 60 | `~/bin/ollama-queue-daemon.log` -> `$QUEUE_STATE_DIR/ollama-queue-daemon.log` |
| ollama-queue-api | `python3 .../ollama-queue-api.py` (a link into the dashboard repo; stage.sh copies the real file) | KeepAlive, RunAtLoad | `queue-api` | `/tmp/ollama-queue-api.log` -> `~/Library/Logs/ollama-queue-api.log` |
| queue-event-log | `python3 ~/bin/queue-event-log.py` | KeepAlive, RunAtLoad, ThrottleInterval 10 | `queue-event-log`: `throttle.sh 10 ...` | `~/bin/ollama-queue-logs/queue-event-log.log` -> `$QUEUE_STATE_DIR/ollama-queue-logs/` |
| cpu-prefetch | `python3 ~/bin/cpu-prefetch.py --once` | StartInterval 60, RunAtLoad | `cpu-prefetch`: `every.sh 60 ...` | same name under `ollama-queue-logs/` |
| dispatch-ack-reconcile | `python3 ~/bin/dispatch-ack-reconcile.py` | StartInterval 300, RunAtLoad | `dispatch-ack-reconcile`: `every.sh 300 ...` | same |
| dispatch-escalation-watcher | `python3 ~/bin/dispatch-escalation-watcher.py --once` | StartInterval 300, RunAtLoad | `dispatch-escalation-watcher`: `every.sh 300 ...` | same |
| verify-orphan-reap | `python3 ~/bin/verify-orphan-reap.py --apply`, Nice 10 | StartInterval 120, RunAtLoad | `verify-orphan-reap`: `every.sh 120 nice -n 10 ...` | same |
| log-rotate | `python3 ~/bin/log-rotate.py --apply`, Nice 10 | StartCalendarInterval 04:17 daily (no RunAtLoad) | `log-rotate`: `every.sh at 04:17 nice -n 10 ...` | `~/.ollama-dispatch/log-rotate.out` (unchanged; that dir is a volume) |

Interpreter: every plist uses `/opt/homebrew/bin/python3`, which is Python 3.14.7 today. The image uses `python:3.14-slim-bookworm`.
`~/bin` in the container is a symlink to `/opt/queue/bin` (the staged code), so the paths above stay the same.

## Environment variables

Every key of every plist's EnvironmentVariables, and where it went.

| Var | Plist(s) | Value in plist | In the container |
|---|---|---|---|
| GATE_AUTOFIX_MODE | daemon | live | literal in `supervisord.conf` (queue-daemon). Code default is `shadow` (gate-on-complete.py:166), so it must stay set |
| GATE_PREGATE_HOST | daemon | unraid | literal. It names a host in `hosts.json`; the container's `~/.ollama-dispatch/hosts.json` must define `unraid` (open question 4) |
| GATE_PREGATE_MODEL | daemon | qwen3:14b | literal (same as code default, gate-on-complete.py:98) |
| GATE_PREGATE_NUM_CTX | daemon | 6144 | literal (same as default, :99) |
| GATE_TWO_TIER | daemon | 1 | literal (same as default, :106) |
| BONSAI_CTX | daemon | 65536 | literal. Code default is 32768 (ollama-queue.py:5137), so it must stay set |
| HOME | daemon | the Mac home folder | `HOME=/home/queue` (Dockerfile ENV) |
| USER | daemon | the Mac user | `USER=queue` (Dockerfile ENV) |
| PATH | daemon | `~/.local/bin`, homebrew, `/usr/local/bin`, system, `/opt/pivy/bin` | image PATH: `/home/queue/.local/bin:/usr/local/bin:/usr/bin:/bin`. `/opt/pivy/bin` is not in the image (not part of the queue; if a worker needs it, say so) |
| PATH | event-log, escalation-watcher | `~/.local/bin`, homebrew, system | same image PATH |
| PATH | cpu-prefetch, ack-reconcile, verify-orphan-reap, log-rotate | homebrew, system | same image PATH |
| (none) | queue-api | no EnvironmentVariables | inherits the container env |

The three "same as default" values are kept literal anyway, so the conf matches the plist and a future default change cannot alter the lane.
No env value in a plist is homelab-specific except `GATE_PREGATE_HOST=unraid` (a name), so `env.example` holds none of them.

Env the container adds (not in any plist): `QUEUE_CONFIG`, `QUEUE_STATE_DIR`, `MINI_BIN`, `PYTHONUNBUFFERED`, `PYTHONPYCACHEPREFIX`, `TZ`, the secrets and addresses in `env.example`.
`OLLAMA_DISPATCH_VIA_QUEUE` is deliberately not set: the daemon stamps it on the workers it starts (ollama-worker.py:10430-10431), and a global value would defeat that guard.

## What cannot run as on the Mac

| Item | Why | What replaces it |
|---|---|---|
| launchd itself (KeepAlive, StartInterval, StartCalendarInterval, Nice, ThrottleInterval, ExitTimeOut) | no launchd in Linux | supervisord plus `every.sh` and `throttle.sh` (table above) |
| `osascript` desktop notification in the escalation watcher (dispatch-escalation-watcher.py:2176) | no macOS | the call is wrapped in try/except, so it silently does nothing. Real notify goes through the Mac agent (qd-agent branch) |
| `claude -p` in the escalation watcher | the review path now runs on a local model through the queue (watcher header, :1850), no Claude CLI call was found in the file; no Claude login in the container | none needed; if a code path still shells out to `claude`, it will fail soft. Not verified end to end |
| verify-orphan-reap and cpu-prefetch watching Mac processes | `ps` and `lsof` in the container only see the container | they still run and watch the container's own jobs. Orphans of Mac-side jobs are not seen. This is the same scope the daemon has after the move |
| `ps -axo pid=,lstart=,command=` and `ps -axww -o ...` | BSD flags | procps-ng accepts both (installed in the image). Not run on the Mac; check on claude-sandbox in the parity tests |
| `lsof -a -d cwd -Fpn` | macOS lsof | Debian `lsof` takes the same flags |
| macOS keychain (`security`), `vm_stat`, `sysctl hw.memsize`, screen-lock | no macOS | env secrets (entrypoint.sh) and the Mac agent (qd-agent branch) |
| `/opt/homebrew/bin/python3` | no Homebrew | `python3` is 3.14 on PATH |

## Jobs not in this map

| Job | Where it lives after the move |
|---|---|
| sandbox-slots (`~/.darkbloom/sandbox-slots.sh`, every 30 s) | stays on the Mac: it produces `sandbox-slots.log` from Darkbloom's own files |
| unraid-influx (`unraid_influx.py --once`, every 30 s, polls 127.0.0.1:7684) | stays on the Mac as a client; repoint its QUEUE_URL at the container (qd-config) |
| queue-deadman.plist, worktree-cleanup.plist, docsum-ui.plist (in `~/bin/launchd`, never installed) | not in the image. Decision needed (open question 5) |
| queue-agent | new, Mac side (qd-agent branch) |
