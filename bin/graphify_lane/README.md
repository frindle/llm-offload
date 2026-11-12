# graphify lane

Keeps every project's code graph (`<project>/graphify-out/graph.json`) fresh. Changed projects are rebuilt on the
Unraid CPU runner (`unraid-cpu-1`) through the queue; the Mac builds a project itself only when it has to.

- `~/bin/graphify-lane`: the Mac side (stdlib Python, run with `/opt/homebrew/bin/python3 -I`). launchd job
  `com.example.graphify-all` runs it every 30 min: `graphify-lane --quiet --log ~/Library/Logs/graphify-lane.log`.
- `scan.py`: graphify's own file list for a project (its `detect()` under graphify's python, plus the import-resolution
  files such as package.json / tsconfig / lock files). Same ignore and sensitive-file rules as a real `graphify update`.
- `graphify_job.sh`: runs on the runner only. `setup` makes a venv with the Mac's exact graphify pins
  (`uv pip freeze` of the Mac's graphify tool venv, cached in the runner's `/tmp/graphify-lane/venv-<sha>`); `run`
  restores the Mac mtimes, runs `graphify update <name>` (AST only, `GRAPHIFY_BACKEND=none`, no network) and returns
  the graphify-out files.
- `~/bin/configs/graphify-lane.json`: which folders are projects, runner name, timeouts, size cap.

## Flow per project
1. Scan. A project is stale when a code or import-resolution file is newer than the last refresh
   (`graphify-out/.lane-stamp`, else graph.json's mtime), the set of code files changed, the graphify version changed,
   or there is no graph yet. Doc-only changes don't count (`graphify update` only redoes the code layer).
2. Payload: the scanned files, `.graphifyignore`/`.graphifyrc`, and the graphify-out state (graph.json, manifest,
   labels, semantic marker...) under `<name>/` in a temp git repo, sent in archive mode. Any file that looks like it
   holds a secret (key headers, GitHub/Anthropic/OpenAI/Slack/AWS/Google/HF tokens, quoted key=value secrets) keeps the
   project on the Mac; so does a payload over 400 MB.
3. The job is first sent as a plain job. If the runner has no graphify venv yet it exits 75 and the lane resends it
   once with setup (a setup job counts against the runner's lane limit of 2).
4. Install: the artifacts are untrusted (tar `filter="data"`, whitelisted names only). graph.json must parse, have
   nodes under `<name>/`, no absolute or runner paths, and keep at least half of its semantic (doc/concept) nodes.
   graphify's own `backup_if_protected` runs on the Mac before a protected graph is replaced; graph.json is swapped in
   last; `.graphify_build_state.json` is dropped (graphify re-derives it).
5. Mac fallback (`taskpolicy -b nice -n 10 graphify update <name>`) when the runner heartbeat is stale, no runner
   claims the job within 30 min, the job/setup fails, a check fails, a secret is suspected, or the payload is too big.
   `--mac-only` forces it.

## Checks done (2026-11-12)
Relocation test (lane payload -> git archive -> extract elsewhere -> mtime restore -> update -> install) against a
Mac-side update of the same edit: identical node and edge sets, semantic nodes kept, no absolute paths, identical
manifest keys, on MS-Shifts-To-ICS, ollama-queue-dashboard and plex-automation. Script:
scratchpad `lane/reloc_test.py` (session 57408d61).

## Commands
    graphify-lane --dry-run          # what is stale and where it would build
    graphify-lane --only bin,.claude # just these
    graphify-lane --mac-only --force # rebuild on the Mac (still incremental)
    tail ~/Library/Logs/graphify-lane.log
