# macOS pipe starvation: bash 5.3 heredoc deadlock (2026-10-08)

Symptom: canary stuck at `[worker] FAIL: SELFCHECK_HANG ... running bash verify.sh (baseline)`; orphaned
`bash verify.sh` at 0% CPU, stdout on `dispatch-nonet.*/nonet.cjs`. `sample <pid>` shows
`execute_disk_command > do_redirections > heredoc_write > write` -- the child never reaches `exec`.

Cause: when the kernel is short of pipe memory macOS hands out 512-byte pipes (check: a nonblocking write
loop on `os.pipe()` stops at 512). bash 5.3 writes a heredoc into a pipe before exec; a heredoc larger than
the pipe blocks forever. Trigger seen: a desktop app leaking ~2000 pipe fds
(`lsof | awk '$5=="PIPE"{c[$1" "$2]++}END{for(k in c)print c[k],k}' | sort -rn | head`). It is environmental
but any verify.sh on that machine hung, so it hit real jobs too, not just the canary.

Fixed (no heredoc >512 B on a path that runs under dispatch):
- `ollama-dispatch-scaffold` nonet guard: `printf '%s\n' ... > file` (marker `dispatch-nonet-guard v3`);
  `ollama-dispatch-auto` `migrate_verify_sh` upgrades v1/legacy AND v2 (heredoc) verify.sh in existing worktrees.
- `darkbloom-apply-wide-models.sh`: program read from the script's own body after `#@@PYBODY@@`.
- Tests: `test-selfcheck-hang.py` (no-heredoc + v2 migration), `test-darkbloom-apply.py` (static no-heredoc).
- `pipeline-canary.py` prints a WARNING at start when `pipe_capacity() < 4096`.

Still heredoc-fed (python programs; they WILL stall on a pipe-starved Mac, fix by moving the program to a
file/`-c`): `dispatch-ack-hook.sh` (2), `review-diff.sh`, `goose-darkbloom.sh`, `gate-finding-check-live-smoke.sh`,
`bloom-integration-livetest.sh`, `review-fixtures/run-*.sh`. Real fix is also to restart the leaking app.
