# claude-offload

Everything for offloading work to local models: the ollama queue, mini-swe, gates, docsum, BloomGauge/Darkbloom helpers, vault helpers.
Real location is `~/bin` (launchd cannot run jobs from ~/Desktop, so it cannot live there); `GitHub Projects/claude-offload` is a symlink to it. All plists, hooks and settings use `~/bin/...`.

## Only mini-swe
Old dispatch tools (ollama-dispatch*, qagent, slicer) are removed. Do not recreate them.
Flow: `mini-spec --repo <path> --base origin/main` -> `mini-validate` -> `mini-submit <dir>/spec.json --label <area-change> --model qwen3.8:27b-q4_K_M --bundle <b>` -> `mini-triage <jobid>` on failure.
In TASK.md's `## Rule coverage`, list every rule of Required change, bad and missing input included. End each line with the frozen test that asserts it, in backticks (`mini-spec --lint` and mini-validate V8 check this).
Job names: `area-change[-step]`, never project-fix-uN. After a failed job: `python3 ollama-queue.py resolve <id>` and `POST http://127.0.0.1:7684/api/runs/<id>/clear` in the same step.

## Layout
- `ollama-queue.py` queue daemon/CLI; `qctl` operator CLI; `mini-*` mini-swe tools; `gate-on-complete.py`, `ollama-land` landing.
- `dispatch-*.py` watchers/self-heal; `worktree_cleanup.py` (dormant until `~/.ollama-dispatch/worktree-cleanup.enabled`).
- `docsum_*` document summarizer (local only, never read user documents).
- `vault-log-append --base-note <note> "<text>"` vault logging (never the vault MCP).
- `docs/` design notes; `launchd/` plists.
- State lives OUTSIDE this folder: `~/.ollama-dispatch` (worktrees, mini-work, mini-runs, logs); symlink `state` points there.

## Rules
- No AI attribution in commits. I commit and push; origin is archived so commits stay local.
- GitHub copy: `frindle/llm-offload` (PUBLIC; clone at `~/Desktop/GitHub Projects.nosync/llm-offload`). It also carries the dashboard and CPU runner checkouts, and CI builds the runner image (`ghcr.io/frindle/llm-offload-cpu-runner`) that Unraid pulls. After committing here, or in the dashboard or runner checkout, or changing a hook, plist or config listed in its `MANIFEST.txt`, run `offload-repo-sync`: it copies HEAD and those files one-way, runs the secret guard, scrubs private info (rules outside git in `~/.config/llm-offload/`), scans what is left, runs the test gate, commits and pushes. A scan hit means a new scrub rule, a `.private-allow` line for a known fake, or (for `[runtime]` files such as `cpu-runner/`) a fix at the source. Never edit the copy's synced folders. The old repos (claude-offload, local-llm-dispatch, ollama-queue-dashboard, dispatch-cpu-runner, ollama-bakeoff) are retired: never push to them.
- Never create/remove `~/.claude/.qwen-manual`. Bulk work goes to the queue, reasoning stays with Claude.
- Never print secrets (Keychain, tokens, TL passkey).

## Darkbloom / BloomGauge: hands off
- BloomGauge runs Darkbloom on the M4. While jobs wait, the queue's own BloomGauge control turns the Manager off and picks the model; it hands back after 15 min idle. Mode file: `~/.ollama-dispatch/bloomgauge-control.mode`.
- BloomGauge allows 2 model changes and 6 Manager changes an hour, so the control waits on purpose. A pending job or `INFRA WAIT: darkbloom not serving ...` is normal: leave it, it launches once the model is served.
- Never run `darkbloom` commands or BloomGauge write tools (set_manager, pick_model, restart) by hand: they fight the control and spend its hourly budget. Reads (GET, activity log) are fine.
- `BLOOMGAUGE CONTROL` rows in ESCALATIONS.md are for the owner and close by themselves once the condition clears.
