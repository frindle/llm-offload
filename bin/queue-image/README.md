# queue-image: the job queue as a container

The queue that runs on the Mac today (`~/bin`, 8 launchd jobs) packaged for Unraid. One image, supervisord inside.
Nothing here has been built or run yet: no docker on the Mac, and no docker build in this branch's work.
Plan and contract: `Queue Docker Build.md` and `Queue to Unraid Docker.md` in the owner's plans folder (not in this repo).

## Files

| File | What it does |
|---|---|
| `Dockerfile` | python 3.14 slim bookworm plus git, tini, supervisor, procps, lsof, build tools, node 26 (copied from the official image), pnpm, tsx, uv, a mini-swe-agent venv, a repo-toolchain venv (`/opt/repo-toolchain/difvenv`, Python 3.12 web packages for pet-portrait-studio's check; pins in `requirements-difvenv.txt`), and Python 3.13 under `/opt/uv-python` for the graph job's own venv (the back-test's B5 test runner). User `queue` (PUID 99, PGID 100 by build arg). Code at `/opt/queue/bin`, `~/bin` links to it |
| `supervisord.conf` | one program per launchd job (8), with the plist's env, restart, stop and schedule rules |
| `PROCESS-MAP.md` | each plist mapped to its program, every env var and where it went, and what cannot run in Linux |
| `every.sh`, `throttle.sh` | replace launchd StartInterval / StartCalendarInterval and ThrottleInterval |
| `entrypoint.sh` | checks the volumes (writable, real filesystem, flock really excludes), makes dirs, writes git identity and the secret files (0600) from env, then execs supervisord under tini |
| `stage.sh` | copies the files listed in `MANIFEST` from a `~/bin` checkout into `build/bin/` (follows symlinks). Stops with a clear message if MANIFEST is missing or lists a missing file |
| `docker-compose.yml` | br0 macvlan, `${QUEUE_IP}` and `${QUEUE_MAC}` with no defaults, the five volumes, healthcheck, 90 s stop grace |
| `unraid-template.xml` | the same for the Unraid UI. Secrets are masked variables. IP and MAC are placeholders |
| `env.example` | every variable, with placeholders only |
| `github-workflow-image.yml` | becomes `.github/workflows/queue-image.yml` in `frindle/llm-offload` at the cutover: stages `bin/` plus `dashboard/src/*.py` through `stage.sh`, private-info scan of the staged files, static checks, build and push `ghcr.io/frindle/llm-offload-queue` (linux/amd64), smoke test of the pushed image |
| `MANIFEST`, `scan-private-info.py` | the files the image ships, and the scan (`--base build/bin --list MANIFEST`) |

## Volumes

All host paths are on the cache pool (`QUEUE_APPDATA`), never `/mnt/user`: flock and sqlite WAL need a real local filesystem.

| Container path | Holds | Notes |
|---|---|---|
| `/home/queue/state` | `QUEUE_STATE_DIR`: state JSON and its lock, daemon lock, queue logs | flock: local disk only |
| `/home/queue/.ollama-dispatch` | chain runs, escalations, decisions, CPU lane sqlite (WAL) and blobs, `hosts.json`, settings | the Mac's is 19 GB, most of it worktrees and venvs; see open question 2 |
| `/home/queue/dispatch-worktrees` | job worktrees | large; git needs symlinks |
| `/home/queue/Library/Logs` | API and supervisord logs | |
| `/config` (read-only) | `config.toml` (hosts, LAN addresses, names) | |

`HOME` (`/home/queue`) itself is not a volume. Everything written there (git identity, credential file, token files) is rebuilt from env on each start.

## Environment

Full list with placeholders: `env.example`. Secrets are masked in the Unraid template.

| Variable | Use |
|---|---|
| `QUEUE_IP`, `QUEUE_MAC`, `QUEUE_IMAGE`, `QUEUE_APPDATA` | compose only; no defaults |
| `TZ` | log-rotate time zone (04:17) |
| `GIT_AUTHOR_NAME`, `GIT_AUTHOR_EMAIL` | required; the container stops without them |
| `GITHUB_TOKEN` | clone and push; written to a 0600 credential file |
| `DARKBLOOM_BASE_URL`, `DARKBLOOM_API_KEY` | Darkbloom local API on the Mac |
| `QUEUE_AGENT_URL`, `QUEUE_AGENT_TOKEN` | Mac status service; token also written to `~/.config/ollama-queue/agent-token` |
| `QUEUE_API_TOKEN` | write-route token (env only) |
| `CPU_RUNNER_TOKEN` | written to `~/.config/dispatch-cpu-runner/token` |
| `OBSIDIAN_TOKEN` | written to `~/.config/ollama-worker/obsidian-token` |
| `TAVILY_API`, `BRAVE_SEARCH_API`, `OLLAMA_WEB_SEARCH` | worker search keys (env only) |
| `QUEUE_STANDBY` | `1` (image default): standby. Only the API (and docsum) start; the daemon, watchers and reaper stay stopped; every POST/PUT/DELETE answers 503 `{"error":"standby"}` except `/api/standby/*` (state import, back-test; write token, 404 outside standby); nothing notifies or posts to the Mac. Set `0` and restart the container to go live. |
| `QUEUE_CONFIG`, `QUEUE_STATE_DIR`, `MINI_BIN` | fixed in the image |

The daemon's `GATE_*` and `BONSAI_CTX` values are literal in `supervisord.conf`.

## Standby

With `QUEUE_STANDBY=1` the container serves read routes on a copy of the Mac state and acts on nothing. Differences from the Mac, by design: no retention loop and no CPU-lane reaper in the API, no daemon, no watchers, no notifications, no writes to the Mac status service. Load a state copy with `POST /api/standby/import` (tarball plus `X-Content-SHA256`; safe extraction; `?replace=1` keeps a backup). `queue-cutover export` does this from the Mac. Flip to live: set `QUEUE_STANDBY=0`, restart.

## Cutover day: the container's part

1. CI has built the image and pushed it to GHCR (scan passed). Pull it on Unraid.
2. Create the appdata dirs on the cache pool (`state`, `ollama-dispatch`, `dispatch-worktrees`, `logs`, `config`), owned 99:100.
3. Put `config.toml` in `config/`. Put `hosts.json` and the other `~/.ollama-dispatch` config files (see the parity review, section 3) in `ollama-dispatch/`.
4. Stop the Mac queue (daemon, API, watchers) and wait until the daemon has exited. Only one machine may own the job list.
5. Copy the state JSON and the carried keys (`_bundle_next`, `_bundle_accepted`, pinned group `docsum`) into `state/`. Copy `ollama-dispatch` data, including the CPU lane sqlite, with the Mac queue stopped.
6. Fill in `.env` or the template: IP, MAC, tokens, `DARKBLOOM_*`, `QUEUE_AGENT_*`. Start the container.
7. Check: `docker ps` shows healthy; `entrypoint.sh` printed no FATAL; `supervisorctl -c /etc/queue/supervisord.conf status` shows 9 programs RUNNING (in standby only queue-api and docsum-ui) (log-rotate waits for 04:17); the dashboard loads at `http://QUEUE_IP:7684/`.
8. Point the CPU runners at the new API URL (the runner container's `CPU_RUNNER_API`); they keep the same token.
9. Rollback: stop the container, start the Mac jobs again. The state copy on the Mac is untouched until you decide otherwise.

## Checks run on this branch (no docker)

`bash -n` on all scripts; `supervisord.conf` parsed with configparser (9 programs); compose and the workflow parse as YAML; the template parses as XML; `stage.sh` (fake MANIFEST: good, missing file, bad path, no MANIFEST), `every.sh` (interval, daily, TERM), `throttle.sh` and `entrypoint.sh` (secret files 0600, no secret in output, fails on missing identity or read-only state, with supervisord replaced by echo) run on claude-sandbox. `hadolint` is not installed, so the Dockerfile has not been linted.

## Open questions

1. ~~**Public repo layout.**~~ Settled 11-12: the image builds from `frindle/llm-offload` (`bin/` = the ~/bin files, `dashboard/src/` = the dashboard's real files; the workflow overlays them and runs `stage.sh`). At the cutover the public sync must stop excluding the MANIFEST files it drops today and check them as runtime files.
2. **Volume sizes and what to copy.** `~/.ollama-dispatch` is 19 GB on the Mac. Which parts to copy and how much space to give `dispatch-worktrees` is not decided.
3. **Where target repos live.** GitHub is the source of truth, but nothing says where clones go in the container. The contract lists no repos volume. Workers that expect `~/Desktop/GitHub Projects/<repo>` will not find one. Needs the "enqueue by repo and ref" work (parity gap 4) or a repos volume.
4. **Model host `unraid` and `studio`.** `GATE_PREGATE_HOST=unraid` needs a matching entry in the container's `hosts.json`. In code `studio` is `127.0.0.1:11434`, which is the container itself (parity gap 8, qd-config).
5. **Never-installed jobs.** `queue-deadman` and `worktree-cleanup` are not in the image (`docsum-ui` is, since 11-13: the 9th program). Run them or leave them out?
6. **Resource limits.** No CPU or memory limit is set. Worker builds (prisma, better-sqlite3, sharp) can be heavy. Pick limits, or accept none.
7. **mini-swe-agent pin.** The image installs `mini-swe-agent==2.4.6` from PyPI. The runner's setup phase builds its own mini venv from a lock file (lane_remote.py:205); if the container's workers should use that lock, say where it lives.
8. **Node and the per-repo toolchains are a first pass.** Not verified: each repo's real test command, prisma engine download inside the container, `sharp` on this base. Run `repo-toolchain-check` in the standby container (repos from `toolchain.repos`, token from `GITHUB_TOKEN`): it clones each repo, picks node or python steps from its files, and prints one PASS/FAIL/SKIP line each. Its node path is tested only through fixtures that have no dependencies (the sandbox has no npm), so prisma, better-sqlite3 and sharp are first proven by that run.
9. **Log growth.** Logs have no size cap, the same as launchd today. `log-rotate` deletes by age.
10. **`/opt/pivy/bin`** is on the daemon's Mac PATH and not in the image. Not known whether any worker needs it.
11. **Pool name.** The template defaults to `/mnt/cache/appdata/ollama-queue`; your pool name may differ.
12. **Not verified:** `procps` `ps -axo pid=,lstart=,command=` output matches what cpu-prefetch parses; `stat -f -c %T` reports `fuse*` for shfs.
