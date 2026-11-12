# Moving ~/bin tooling into this repo

Why: ~/bin is outside any project, unversioned, and the permission classifier treats edits there as modifying
shared resources. A real repo gives history, review, and normal project-level permissions.

Cutover (do when no agent is editing ~/bin and the queue is idle):
1. `scripts/sync-from-bin.sh` (pull latest live edits), review `git diff`, commit.
2. Stop nothing; `scripts/install.sh --dry-run`, then `scripts/install.sh` (backs up replaced files as `.pre-repo-*`).
3. Daemons/launchd entries keep their `~/bin/...` paths; they now follow symlinks into the repo.
4. Edit in the repo from then on; `python3 bin/ollama-queue.py --self-test` before restart.
Not moved: runtime state, logs, caches, personal tools (renderers, Obsidian, claude trackers) that stay in machine-config.
