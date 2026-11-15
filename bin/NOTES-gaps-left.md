# Queue move: gaps left (after bundle 9)

Checked against qd-submit 8c904fd and qd-dash ade581a, from Queue Docker Parity Review section 9 and the Queue Docker Build status table.
Closed and not listed: review gaps 1, 3, 4, 5, 6, 7, 8 (now with the container guard), 9 (chat on darkbloom_endpoint), 10 (container side), 11, 13, 14, 15.

## Code work left
- Hold policy (review gap 2): model_hold() fails open (no view or a failing view = not held), cpu_lane.py:329-345. Plan said fail-closed; this is a policy call for the operator, not a code gap. Not changed.
- Runner-sha skew after a remote validate: mini_validate_remote keeps the queue's `- runner: ... sha=` line, so mini-submit C3 fails closed if the image's mini-swe-runner.py differs from ~/bin's (mini_validate_remote.py:65-80). Fix is a build rule (image runner = ~/bin runner at release), no code.
- MINI_RECEIPT_ENFORCE (ollama-queue.py:7864) ships False. A remote submit writes no local SUBMIT-OK.json (mini-submit:572); the upload route has no receipt check (the API token is the control). If the flag is ever turned on, uploaded jobs need an exemption in cmd_enqueue.
- Remote mode runs mini-submit C0, C1, C2, C3 (hash compare only), C4, C7, C8 locally; none runs project test code (C0 = the runner's own --self-test). C5 and C6 run in the queue's validate job. Hand-run `mini-validate` stays local by design.
- Hand-written clients that still default to 127.0.0.1:7684: mini-finish:129 (MINI_FINISH_API), batch-merge-and-sync.sh:311, qctl:8 (comment). mini-finish remote mode uses queue_api(); the default only applies with no QUEUE_API.
- mini-finish remote: a mirror is fetched once; a changed run dir needs the mirror deleted by hand (fetch warning says so).
- Check and validate jobs use host "auto" and the failed job's model, like the existing mini-finish check jobs. Whether that can change which lane runs them is a question for the operator.
- MANIFEST.tests (291 files) still holds personal names, IPs and /Users paths: decide leave-out or scrub before the public repo takes tests.
- bundle_view.py comments hold a personal name (scrub in the image repo's copy only).
- Docsum UI is now the 9th supervisord program (bundle 9): leave docsum.upstream unset in the container (default 127.0.0.1:7690 is the in-container page). Its PDFs come through its own upload route. Set docsum.upstream only to keep using the Mac's page.
- No studio host is needed inside the container for queue code. If the operator wants the studio lane there, set ollama.studio_url to a non-loopback URL; a loopback value now removes the host (queue_config.studio_url, ollama-worker.py SEEDED_OLLAMA_HOSTS and _normalize_ollama_hosts, bakeoff-runner.resolve_host).

## Operator step at cutover
- Hook text: ollama-queue-watch-reminder.sh and ~/bin/CLAUDE.md say `curl -X POST .../api/runs/<id>/clear`; change to `ollama-queue.py clear-card <id>` (resolve already forwards).
- Seed the container with the Mac's queue-events.* so seq continues; stop the Mac event-log and escalation-watcher jobs.
- Copy only the small live state from ~/.ollama-dispatch (CPU lane sqlite and blobs, queue-events files, escalations, hosts.json, chain-run records); skip worktrees, mini-work, node_modules-cache, mini-venv, retired.
- hosts.json in the container: `unraid` entry yes; no `studio` entry unless it has a real URL (see above).
- Target-repo clones: no repos volume; enqueue is by upload, so only jobs that clone need one.
- Create the dashboard-chat GitHub remote (review gap 9) if the chat is to ship.
- Build the image rule: queue image runner sha = ~/bin runner sha (see runner-sha skew above).
- Set QUEUE_API_TOKEN, queue.api_url on the Mac, unraid_url, studio_url in /config/config.toml (docsum.upstream stays unset). `queue-cutover switch-clients` edits queue.api_url and only lists the plists that set QUEUE_API; those need a by-hand change.

## Parity test (needs a real container)
- pipeline-watch reads slice-runs/ and the log dir's sidecars; ollama-diagnosis-followup reads log-dir sidecars: neither is mirrored to the Mac.
- bloom_power_lib's queue windows use job log mtimes (remote: skipped, metrics rows only).
- darkbloom-keepwarm state stays on the Mac, so the container sees none and fails open. If the queue acts differently, serve it through an agent route.
- docsum_proxy forwarding to a different host (stub-tested only).
- procps `ps -axo ...lstart` against cpu-prefetch's parser; `stat -f -c %T` on Unraid's shfs (the entrypoint refuses fuse).
- Per-repo toolchains (prisma, better-sqlite3 native build, pnpm, sharp) in the image (review gap 12); test #12.
- Remote draft job end to end: queue-side mini-testdraft on the shipped spec/ref/baseline trees (cmd_refs resolve; tested to the package and server allowlist only).
- Remote validate job end to end: --validate-spec runs mini-validate, C5, C6 in the queue's runner (validate_spec_job in mini-swe-runner.py; tested through the stub only).
- SUBMIT-OK receipt in remote mode: no local receipt, queue relies on the token (see above).

## Bundle 9 (standby, back-test, cutover)
Closed by code and sandbox tests (details in the commits):
- S1 standby: QUEUE_STANDBY=1 starts only queue-api and docsum-ui; every state-changing route answers 503; /api/standby/import (safe tar extraction, sha256, backup, ?replace=1) and /api/standby/backtest.
- S2 offload-repo-sync --bin-ref/--branch publishes a build branch (queue-pre) without touching main; the workflow builds it as `standby` and `pre-<sha>`.
- S3 parity-ci.sh runs parity checks 2-6, 11, 13, 15, 16 plus the standby checks in the built image. Checks 6 and 16 are reduced stand-ins (they say so); 3-5 use a fake status service.
- S4 queue-cutover (export, switch-clients, fallback; --dry-run; idempotent; logs) and the drain/export routes. Set QUEUE_STANDBY=0 and restart the container before the fallback routes work.
- S5 repo-toolchain-check: detects node or python from the repo's files, one PASS/FAIL/SKIP per repo. Its node path is only fixture-tested (no npm on the sandbox).
- S6 docsum-ui as the 9th program; upload and delete answer 503 in standby and while draining.
- S7 queue-backtest B1-B7 with a planted difference caught per part.

Where standby or remote differs from the Mac:
- The standby API has no retention loop and no CPU-lane reaper (they live with the daemon and are stopped); the first live start runs them.
- Standby shows state as imported; leases and sticky-focus timers do not advance.
- While a drain marker exists the daemon launches nothing: `if queue_config.draining(): _order = []` in ollama-queue.py cmd_run (right after _order is computed). It changes nothing when the marker is absent. Question for the operator: is that the right place (it also pauses sticky-focus bookkeeping)?
- docsum-ui's rename route still works in standby (it only renames a file in the container's private folder).

Not covered or only reduced:
- queue-backtest case files (b3/b4/b5 jsonl, b6 routes and recorded outputs, b7 recorded files) must be made from the real history by the operator step; the harness and its tests use made-up cases. B1 compares only what the sidecar records: bundle (label rule) and the context ceiling; lane, model and retry choice, and priority are reported "not comparable" with the reason. B2 builds provider-log history from the recorded hold start/release pairs in infra-incidents.jsonl and checks the edges (10 s tolerance), not every renewal. B4 and B5 need a real model and the owner's go.
- The `queue-image` tests now shipped in the image (test-queue-standby.py, test-queue-api-auth.py, test-repo-toolchain.py, test-docsum-container.py, test-queue-backtest.py) are listed in MANIFEST; decide if the public repo should carry them.
- offload-test-gate tests the dashboard repo at HEAD, not at --dash-ref (open question: gate the ref instead?).
- Parity items 1, 7-10, 12, 14, 17, 18 and everything that needs real docker, Actions, Unraid or the Mac (launchd, Darkbloom, status service) are untested here.

## B4, B5 and the toolchain venvs (case builders written, not yet run against real servers)
- B4 cases (10) come from `build_b4_cases.py` (scratch, not in this repo): 8 mini-swe-runner two-phase jobs on Darkbloom (4 PASS, 2 FAIL class=model, 2 NEEDS-REVIEW) and 2 ollama-worker jobs with `bash verify.sh` (PASS). Each case runs `b4_run.py`, which uses `QUEUE_BIN` code, waits up to 30 min while the live Mac queue API shows a running job on the lane ("not comparable: lane busy"), and reports a Darkbloom hold, 503 or refused connection as "not comparable: Darkbloom busy". part_run takes `not_comparable`, `env_pass`, per-case logs and `raw_verdict`; all tested with fake servers.
- No real "Unraid Ollama" job exists since 10-30: the 15 `host_pref=unraid` jobs are code-review-agent benchmarks. The two Unraid cases are real worker jobs recorded on Darkbloom (host_pref studio), re-run on the Unraid lane with `QUEUE_B4_UNRAID_MODEL` (default gemma4:12b-it-q4_K_M): the lane and model are a substitution, so a verdict difference there is weaker evidence.
- No real CPU-lane model job exists: the lane runs deterministic stages (audit-determinism, baseline-verify, harness-check, merge-verify, refimpl-verify, relevance ...) whose inputs are not retained. Closest real types: those stages replayed from a recorded job's worktree, or a mini-swe job sent to the runner with `remote` (lane_remote). Not built.
- Bases for B4 are the job work dir's HEAD (the baseline as launched), without `ref/` (the reference solution) and without files the secret scan flags (e.g. a test with token-like strings), listed in each case.json under `base.dropped_private`; a dropped test file can change a suite result.
- B5: the 53 graph jobs are CPU-lane jobs in jobs.sqlite, which is off limits; what the Mac recorded is `graphify-lane.log` (whole-graph "N nodes, M edges", trimmed to the last days) and each project's `graphify-out/graph.json`, `.lane-stamp`. The case compares the code layer (nodes with file_type code, links between them, source files) because the whole-graph counts include LLM-made document nodes. Edges are compared within 1 percent: the Mac grows graphs by incremental updates, a fresh build resolves 6-19 more test-to-fixture links (checked on bloom-ledger: the fresh graph holds every Mac edge). Each tolerated gap is listed in the result.
- The image had no Python 3.13 for graph jobs: `uv python install 3.13` puts one under `/opt/uv-python`. The graph job builds its own venv from the Mac's pins (`uv pip freeze` of the Mac's graphify tool venv, shipped with each job), as on the Unraid runner; no graphify venv is baked into the image.
- Gap closed: B3 case faff8839d36c (pet-portrait-studio) needs only fastapi, httpx, PIL and python-multipart (no torch or diffusers). `/opt/repo-toolchain/difvenv` (Python 3.12, `requirements-difvenv.txt`, the Mac venv's own pins) is mapped from `/Volumes/NVMe-Models/image-models/.difvenv/bin/python` in the case `path_map`. Passed on the sandbox with an equal venv. The image build itself is not run here.
