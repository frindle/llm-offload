# CPU lane (Unraid CPU runner, queue side)

Phase 6 of the dispatch pipeline. CPU-only stages (baseline/final verify, preflight both-ways
verify, relevance mutation, harness self-check, slicer scaffolding) run on the Unraid
`dispatch-cpu-runner` container (repo `dispatch-cpu-runner`) so they stop idling the GPU lanes.

## Moving parts (all in `bin/`)
- `cpu_lane.py` durable job store (sqlite WAL + payload blobs under `~/.ollama-dispatch/cpu-jobs/`),
  the `/api/cpu/*` routes, lease reaper, token handling. CLI: `cpu_lane.py token|ip|api-url|summary`.
- `ollama-queue-api.py` (dashboard API, :7684, binds 0.0.0.0) mounts the routes (`do_GET/POST/PUT/DELETE`
  -> `_cpu_lane`), starts the reaper at boot, serves a read-only `GET /api/cpu-lane` feed and the
  "CPU lane" dashboard section (counts, queue depth, running jobs with runner id, recent durations).
- `ollama-queue.py` `bundle_commit_status(cpu_wait=)` / `bundle_commit_step` / `cpu_outstanding_by_bundle()`.
- `cpu_dispatch.py` `run_cpu_stage(...)` (what the stages call) + vendored `cpu_job.py` client.
- Tests: `test-cpu-lane-api.py`, `test-cpu-lane-queue.py` (canary seams `cpuapi`, `cpulane`, both proven
  red on revert with `pipeline-canary.py --prove --only cpuapi --only cpulane`), and in the runner repo
  `tests/test_queue_integration.py` (real agent + real API + wrapper).

## Security model
`/api/cpu/*` enforces its own bearer token (`~/.config/dispatch-cpu-runner/token`, 0600, constant-time
compare, created automatically, never logged or returned) because the runner reaches the API over the
LAN. Every route except `GET /api/cpu/health` needs it. Requests that carry Cloudflare/proxy headers
(`Cf-*`, `X-Forwarded-*`, `Forwarded`, `X-Real-IP`) or a public Host name get 403 even with a valid
token, so the tunnel can never reach these routes. The dashboard feed `/api/cpu-lane` has the same trust
as the rest of the dashboard and exposes ids/labels/durations only (never cmd, env, token).
Residual risk: any LAN host can reach :7684 (as before) and can brute-force the token (64 hex chars,
0.25 s penalty per failure) -- keep the token secret; rotate by rewriting the file (re-read on change).
macOS may ask once to allow incoming connections for python.

## How the lanes are released
A CPU stage is not a queue row, so it never occupies a lane. The only thing that held the GPU was the
BUNDLE COMMITMENT: a committed bundle that is merely waiting between steps keeps every other bundle
out. `run_cpu_stage(bundle_id=<queue bundle key>)` makes the stage visible (remote job row, or a
`local` marker when it falls back to a local subprocess). Each daemon tick `_apply_bundle_commit` reads
`cpu_lane.outstanding_by_bundle()` and passes `cpu_wait` to `bundle_commit_status`; a bundle with no
running row, no live row and an outstanding stage is `waiting`: the commitment is released (parked
kind `cpu_wait`, NO alert), the next bundle commits and launches, and the waiting bundle is
re-committed FIRST (parked bundles resume before any new bundle) when the result lands and it has a
row again. A running or pending row of the bundle keeps it `working` as before. Orphans (a crashed
caller) stop counting after `timeout_s + 30 min` (remote) or `timeout_s + 2 min` (local marker).
A gate-on-complete hook alone still holds its bundle (the nobounce policy: it may enqueue a regate);
only the CPU stage inside it yields, once that stage goes through `run_cpu_stage`.

## Pipeline wiring (DONE 2026-10-08): `cpu_stage.run_stage`
One wrapper, `~/bin/cpu_stage.py`, used by every stage below. Signature: `run_stage(wt, cmd, timeout_s, stage,
bundle_id, local_fn, *, tools, env, cwd_rel, check, gzb64)`. `local_fn` is the caller's ORIGINAL code, so the
fallback is byte-for-byte the pre-lane behaviour. Every call logs
`[cpu-lane] stage=.. bundle=.. path=runner|local reason=.. snap=.. exit=.. wall=..` to stderr and
`~/.ollama-dispatch/cpu-lane.log` (JSONL).

| stage | file | runs where |
|---|---|---|
| `baseline-verify`, `refimpl-verify` (both-ways) | ollama-dispatch-preflight (`sh_lane`) | runner if eligible |
| relevance mutants (preflight) | ollama-dispatch-preflight (`measure_relevance_lane`, driver `cpu_vr_measure.py`) | runner; local when a relevance sign-off is set |
| relevance (gate) | gate-on-complete.py (`_relevance_run`) | runner |
| `harness-check` | ollama-dispatch-auto (`_cpu_lane_run`, `resume_harness_check`), ollama-dispatch-slice (`_lane_stage`) | runner |
| `merge-verify`, `resolve-verify` | ollama-dispatch-slice (`_verify_sh_lane`) | runner |

**Deliberately local:** in-job `run_bash` (latency); `ollama-dispatch-scaffold` (no whole-stage verify there; it
does the env bootstrap `npm ci` / prisma generate / node_modules link, which needs network); the worker's own
in-job verify; every non-node worktree (about 230 of 300 live ones: Mac python 3.14 vs runner 3.11).

**Eligibility (`cpu_stage.eligibility`)** -> local when: no package.json or no package-lock.json; the command
matches curl/wget/pip install/git push/fetch/osascript/keychain; it names a host-only path (/Users, $HOME, ~/ except
the shipped tools); `npx --yes X` with X not in the lockfile (tsc maps to typescript; tsx is shipped as a prebuilt
linux-x64 tarball); verify.sh uses sqlite3 while prisma files are modified (the runner image has no sqlite3).

**Install / network decision.** The agent installs deps in a separate phase WITH network, cached by lockfile hash
(`/cache/nm/<key>`); the job itself runs in an empty netns as uid 10001 with env allow-list
`VERIFY_*,DISPATCH_*,TEST_*,NODE_ENV,CI,TZ,LANG,LC_*,PRISMA_*,DATABASE_URL`. So the stage never installs: a stage
that needs network or secrets is ineligible and stays local. Cache miss costs one `npm ci` (about +85s once per lockfile).

**Kill switch.** `CPU_LANE=0` (env) or `~/.ollama-dispatch/cpu-lane.disabled` (file): local_fn runs directly, no
runner contact, no marker.

**Tree-lock / stale snapshot.** Callers keep holding the worktree tree lock around the call. The job carries
`HEAD sha : sha256(diff)` (env `DISPATCH_SNAPSHOT`, label `stage@snap`); after the runner returns the snapshot is
recomputed and a mismatch discards the remote result and re-runs locally ("STALE SNAPSHOT").

**Result parity.** exit code, stdout and stderr tails pass through unchanged (VERIFY_OK markers and
flight-check parsing read the same text); big JSON (relevance) travels gzip+base64 and a JSON-verdict `check`
vetoes an unusable result (-> local).

**Lane holding.** A remote job is a pending CPU row => `bundle_commit_status(cpu_wait=)` => "waiting" => the
bundle holds no GPU lane. A LOCAL fallback registers NO marker by default (the bundle keeps its lane exactly as
before, bundle-to-completion; the canary's foreign-bundle invariant depends on this). Opt in with
`CPU_LANE_LOCAL_MARKER=1`.

**Fidelity caveats:** python 3.11 on the runner (non-node stages stay local), node 22 vs the Mac's 26, no sqlite3 in
the image (suggest adding it to the Dockerfile), prisma-client generate lines differ in output.
**Runner repo fix (needs image rebuild + Unraid update):** agent.py now chowns the cache dir to the job user and
fails loudly when the node_modules cache fill `cp` fails (a root-owned /cache/nm made it fail silently; the pipeline
fell back local, correctly).

**Tests:** `~/bin/test-cpu-stage.py` (CPU_STAGE_TEST_OK); canary seam `cpustage` (`pipeline-canary.py --prove --only
cpustage`, 6 revert proofs); the canary sandbox points the lane at a counting stub API that reports no runner, and
checks the real jobs.sqlite is untouched and no stage went remote.

## Operating
- Check: `python3 ~/bin/cpu_lane.py summary`; dashboard "CPU lane" section.
- Runner offline => `run_cpu_stage` runs locally at once (no 3-minute wait) and registers a local marker.
- Force local: `CPU_DISPATCH_REMOTE=0`.
- Prune: rows 7 days (`CPU_LANE_PRUNE_DAYS`), payload blobs of finished jobs 1 day (`CPU_LANE_BLOB_DAYS`).

**Test-suite notes (2026-10-08):** `ollama-dispatch-auto._harness_check_output(wt, verify_cmd, target=None, bundle=None)` now takes an optional 4th `bundle` arg (the chain key, for the lane release); test stubs of it must accept it. `ollama-dispatch-slice` marks the lane call site with `# VERIFY-SANDBOX` (the sandbox env is passed to `run_stage`; `local_fn` carries its own), keeping `test-verify-sandbox-guard.py` counts honest. `test-selfcheck-hang.py` runs ~20 min (16 mutants x ~70s) -- it is slow, not hung.
