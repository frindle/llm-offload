# Section D notes (queue to Docker, Mac as client)

Done: upload/result API (qd-dash src/queue_upload.py + ollama-queue-api.py), client packer (queue_remote.py),
mini-submit remote branch (submit_remote), CLI forwarding (queue_remote_cli.py, hook in ollama-queue.py main()).

Not done:
- mini-finish and gate-on-complete still read local state/.done.json/livelogs. When remote they need
  queue_remote.fetch_result(base, headers, job_id, dest) into a local mirror dir, then read from it.
  mini-finish already has MINI_FINISH_API; start there.
- CLI subcommands not forwarded (exit with a message when remote): results, plan-add/clear, focus, accept-bundle,
  promote-group, run-now, unpin-group, bundle, move-group, nsample, ttc, escalate, retag-bundles, needs-opus.
  Routes exist for promote-group, run-now, move-group in the API; add them to queue_remote_cli.forward().
- resume --force, promote --preempt/--take-focus, cancel --plan are refused remotely (the API routes take no such options).
- Spec path keys other than baseline_from, ref, task (top level and steps[]) are not shipped; mini-submit warns.
- Local SUBMIT-OK receipt is not written in remote mode (the API _enqueue never verified one).
- Hook text: in ~/.claude/hooks/ollama-queue-watch-reminder.sh and ~/bin/CLAUDE.md replace
  `curl -s -X POST http://127.0.0.1:7684/api/runs/<id>/clear` with `python3 ~/bin/ollama-queue.py clear-card <id>`
  and keep `python3 ~/bin/ollama-queue.py resolve <id>`. Not edited here (hooks are the operator's).
  resolve and clear-card are two different actions: resolve removes the job's state row (remote: POST
  /api/jobs/<id>/resolve, same guards as the CLI: 404 unknown, 409 pending/running); clear-card archives the dashboard
  run card (POST /api/runs/<id>/clear, the same call locally and remotely, with the API token when one is set).
- Container needs the runner (mini-swe-runner.py) in its bin and QUEUE_API_TOKEN set. Upload caps: [upload] max_mb
  (512) and max_unpacked_mb (2048) in config.toml, or QUEUE_UPLOAD_MAX_MB / QUEUE_UPLOAD_MAX_UNPACKED_MB.

## Bundle 6 left
Done in Bundle 6 (qd-submit + qd-dash): promote-group / run-now / move-group forwarding (queue_remote_cli.py), spec path keys
(queue_remote.py PATH_KEY_PATHS / CMD_KEY_PATHS, queue_upload.py cmd_refs), mini-finish remote mode (mini-finish,
mini_finish_remote.py; server ?scope=run in queue_upload.build_run_tar and a runner_args allowlist in queue_upload.RUNNER_FLAGS,
appended to the runner argv in ollama-queue.py next to "--task-file").
Not done:
- ~~Test-draft specs cannot be submitted remotely~~ Done (bundle 8, qd-submit 8c904fd): queue_remote.plan_draft ships the original spec, rewrites draft_accept_cmd to the queue's mini-testdraft via cmd_refs, refuses weakened gates; mini-finish finishes draft jobs remotely (check job, DRAFT-CHECK files fetched). Old text: draft_accept_cmd names Mac-only tools (an interpreter, mini-testdraft, the original
  spec) that lie in no shipped tree, so mini-submit refuses them (queue_remote.check_paths). mini-finish refuses draft jobs when
  remote (mini-finish build_ctx, "test-draft job"). Fix: ship the accept script as a tree and rewrite it as a cmd_ref.
- ~~mini-finish remote continuation validates on the Mac~~ Done (bundle 8): a validate job (--validate-spec in RUNNER_FLAGS) runs V1-V8, C5, C6 on the queue; mini-submit skips C5/C6 when VALIDATION.md carries the queue marker. Remaining local C-checks and caveats: NOTES-gaps-left.md. Old text: the continuation still runs mini-validate and mini-submit's C0-C6 on the Mac (mini-finish continuation(),
  mini-submit main); those execute the spec's tests locally, as a remote mini-submit already did before Bundle 6. Moving V1-V8 to the
  queue (a validate job through the same runner_args route) is the clean fix.
- ~~mini-finish remote: the check job's card stays on the dashboard~~ Done (qd-submit 9d12845, test qd-dash d837022):
  remote_runner clears the check job's card after its result is fetched; on a failed clear it prints the clear-card command.
- mini-finish remote: a mirror is fetched once; a re-fetch (the run dir changed on the queue) needs the mirror deleted by hand
  (the fetch warning says so). ~~Larger runs lose files silently~~ Done: build_run_tar lists the files over the cap
  (upload.run_max_mb, default 128) in _queue/skipped.txt and never ships a _queue dir of the run's own; the fetch warns, and
  --check refuses a partial mirror unless MINI_FINISH_ALLOW_PARTIAL=1 (1 of 434 Mac run dirs was over 128 MB on 11-12).
- ~~results / needs-opus forwarding~~ Done: both read the queue over its API when remote (API_NATIVE): `results` via the
  new token-gated GET /api/results?verdict=&label=&limit= (server and CLI share q._filter_results; -v notes the paths are on
  the queue host), `needs-opus` via /api/state (queue_config.load_queue_state). OLLAMA_QUEUE_FORCE_LOCAL=1 keeps both local.
- Hook text edits (ollama-queue-watch-reminder.sh, ~/bin/CLAUDE.md curl clear -> `clear-card`; the owner's, at cutover) and the
  SUBMIT-OK receipt in remote mode: unchanged from the list above.
- ~~plan-add / plan-clear / focus / accept-bundle / unpin-group / bundle / nsample / ttc / escalate / retag-bundles forwarding,
  resume --force and promote --preempt / --take-focus flags, qctl remote mode~~ Done (Bundle 7 K): POST /api/cli runs an
  allowlisted ollama-queue.py or qctl command on the queue host (queue_remote_cli.passthrough); escalate --gate-output FILE goes
  inline (last 4000 chars). Remote differs from local only here: output comes back after the command ends (no live streaming,
  180 s cap, 256 KB per stream), `--cwd` / `--next-command` paths are the queue host's, and a plan-add --cwd or escalate path
  that exists only on the Mac is just a string there.

- Bundle 8 also closed: docsum_proxy upstream is configurable (docsum.upstream / QUEUE_DOCSUM_UPSTREAM); no loopback studio host inside the container (queue_config.studio_url). Open items: NOTES-gaps-left.md.
