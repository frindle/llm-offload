# llm-offload

Tooling for moving coding work off expensive Claude models onto local LLMs and cheaper Claude models (Haiku, Sonnet),
with no loss in quality, measured on hidden tests from real GitHub issues.

This repo is a one-way, published copy of one person's working setup: a job queue on a Mac, a GPU box running Ollama, a
CPU job runner on a home server, and Claude Code hooks that route work to them. It is meant for reading and borrowing
from, not as a turnkey install. Personal names, home paths, host names and LAN addresses are replaced with placeholders
(`the owner`, `/Users/user`, `unraid-host`, documentation addresses such as `192.0.2.x`) before anything is published.

## What is where

| Folder | What |
|---|---|
| `bin/` | The queue, workers, dispatch tools, test gate and their tests (the owner's `~/bin`, committed work only) |
| `dashboard/` | Queue API and web dashboard |
| `cpu-runner/` | Pull-based CPU job runner (Docker image, Unraid template, compose file). It runs jobs in an empty network namespace as an unprivileged user |
| `hooks/` | Claude Code hooks that steer work to the queue |
| `launchd/` | macOS launchd jobs that run the queue on the Mac |
| `config/` | Dispatch defaults and the model ladder |
| `docs/` | Design notes; `docs/state/` is copied from the live setup |
| `harness/` | The earlier bake-off harness (frozen, historical) |

## Images

| Image | Built from |
|---|---|
| `ghcr.io/frindle/llm-offload-cpu-runner` | `cpu-runner/` (see `cpu-runner/README.md` for install and update) |

## How this repo is updated

`bin/offload-repo-sync` builds every commit: it stages the sources listed in `MANIFEST.txt`, checks them for secrets,
scrubs and scans them for private information, runs the test suite on a separate test machine, and only then commits
and pushes (fast-forward only). Files that machines run straight from an image (`[runtime]` in the manifest) must be
clean before the scrub, so a scrubbed value can never end up inside a running container.

Issues and pull requests are welcome, but changes land through the live setup, so a merged pull request shows up here
with the next sync.

## License

MIT, see `LICENSE`. `harness/` keeps the licenses of the third-party code it contains, where noted.
