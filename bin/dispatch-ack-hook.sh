#!/usr/bin/env bash
# dispatch-ack-hook.sh -- post-commit acknowledgement of a merged dispatch.
#
# WHY: the ollama handoff "awaiting action" bucket is cleared ONLY by an explicit
# `handoff-emit.py --acted <id>`, and the signoff shadow->live flip is gated on a
# calibration corpus (`signoff.py --agreement`) that needs a HUMAN verdict paired
# with each shadow decision. Hand-merging dispatched fixes did neither, so the
# bucket piled up AND the corpus stayed empty (0/0 pairs). This hook closes both:
# when a commit's message names a dispatch job-id that is still awaiting action,
# the merge IS the human's approval -- so record `--verdict approve` (builds the
# corpus) and `--acted` (clears the bucket).
#
# CALIBRATION CAVEAT: a merge only ever happens on an ACCEPTED dispatch, so this
# hook records approves only. It samples the conservative direction (harness held
# / human approved) but NEVER the dangerous one (harness approved / human rejected)
# -- a rejected dispatch is never merged, so the hook never fires on it. Full
# calibration still needs rejects recorded by hand (`signoff.py --verdict reject`).
# Do not read a clean --agreement built solely from this hook as license to flip.
#
# Safe no-op on any commit whose message names no awaiting-action job-id. Never
# fails the commit (post-commit cannot abort anyway); all mutations are local Mac
# state (handoff/signoff ledgers), never repo content.
set -uo pipefail

HANDOFF="$HOME/bin/handoff-emit.py"
SIGNOFF="$HOME/bin/signoff.py"
command -v python3 >/dev/null 2>&1 || exit 0
[ -f "$HANDOFF" ] && [ -f "$SIGNOFF" ] || exit 0

msg=$(git log -1 --pretty=%B 2>/dev/null) || exit 0
sha=$(git rev-parse --short HEAD 2>/dev/null) || exit 0

# candidate 12-hex job-id tokens (matches both a "Dispatch-Job: <id>" trailer and
# the loose "(qwen <id>)" convention already in our merge messages)
cands=$(printf '%s\n' "$msg" | grep -oiE '[0-9a-f]{12}' | tr 'A-F' 'a-f' | sort -u)
[ -n "$cands" ] || exit 0

# ids currently awaiting action (the complete/ bucket). Only these are eligible --
# so a stray hex in a message can only ever clear something that genuinely needs it.
known=$(python3 "$HANDOFF" --json 2>/dev/null \
  | python3 -c "import sys,json
try: d=json.load(sys.stdin)
except Exception: sys.exit(0)
b=d.get('complete') or d.get('awaiting_action') or []
print('\n'.join((j.get('id') or '') for j in b if isinstance(j,dict)))" 2>/dev/null) || exit 0
[ -n "$known" ] || exit 0

# SAME-REPO REQUIREMENT (2026-09-19). The original rule was "the message names an
# awaiting-action id, therefore this commit merged it", justified as safe because
# "a stray hex can only ever clear something that genuinely needs it". That
# reasoning is wrong: being in the awaiting-action bucket means the job needs
# ATTENTION, not that THIS commit resolved it. A commit that mentions a job id in
# order to talk ABOUT it -- a bug report, an investigation, a harness fix the job
# exposed -- was recorded as that job's human approval.
#
# That is not hypothetical. An audit of every sign-off this hook ever wrote found
# 6 of 6 were false: each was a machine-config commit to bin/gate-on-complete.py,
# bin/gate.py, bin/ollama-worker.py, bin/ollama-dispatch-auto or bin/ollama-queue.py
# that merely NAMED a slice job whose deliverable lives in another repo entirely.
# The hook had never once recorded a genuine merge. Worse, these approvals are fed
# to the signoff calibration corpus (`signoff.py --agreement`) that gates the
# shadow->live flip of autonomous sign-off, and they all land in the "human
# approved" direction -- the direction that argues FOR flipping it live.
#
# So require what "merged" actually means: the commit must be in the SAME
# repository as the job's own working tree. Landing a dispatch's work into the
# repo it came from still acks exactly as before; a harness commit that merely
# mentions the id no longer can. Unknown cwd => fail closed, since that is no
# evidence at all.
# Identity must be computed the SAME way on both sides. A dispatch's work is
# landed from a linked worktree as often as from the main checkout, and
# `--show-toplevel` returns the WORKTREE path there -- so comparing a toplevel
# against a main-repo path would refuse exactly the genuine merges this hook
# exists to record. Resolve both sides through --git-common-dir (which maps any
# worktree back to its main repo) and prefer the origin URL.
_repo_id_for_dir() {  # $1 = any dir inside a repo/worktree -> repo identity, or nothing
  python3 - "$1" <<'PY' 2>/dev/null
import os, subprocess, sys
cwd = sys.argv[1]
if not cwd or not os.path.isdir(cwd):
    sys.exit(0)
try:
    common = subprocess.run(["git", "rev-parse", "--path-format=absolute",
                             "--git-common-dir"], cwd=cwd, capture_output=True,
                            text=True, timeout=10)
    if common.returncode != 0:
        sys.exit(0)
    main = os.path.dirname(common.stdout.strip())   # .../repo/.git -> .../repo
    url = subprocess.run(["git", "config", "--get", "remote.origin.url"], cwd=main,
                         capture_output=True, text=True, timeout=10)
    print(url.stdout.strip() or main)
except Exception:
    sys.exit(0)
PY
}

_here_id=$(_repo_id_for_dir "$PWD")

_repo_id_for_job() {  # $1 = job id -> prints the job's repo identity, or nothing
  _jcwd=$(python3 - "$1" <<'PY' 2>/dev/null
import json, sys
jid = sys.argv[1]
try:
    print((json.load(open(f"/Users/user/bin/ollama-queue-logs/{jid}.done.json")) or {}).get("cwd") or "")
except Exception:
    pass
PY
)
  [ -n "$_jcwd" ] || return 0          # unknown tree -> no evidence -> no ack
  _repo_id_for_dir "$_jcwd"
}

for id in $cands; do
  printf '%s\n' "$known" | grep -qx "$id" || continue
  _job_id_url=$(_repo_id_for_job "$id")
  if [ -z "$_job_id_url" ]; then
    echo "[dispatch-ack] $id NOT acked: cannot locate the job's working tree, so there is no evidence this commit landed it."
    continue
  fi
  if [ "$_job_id_url" != "$_here_id" ]; then
    echo "[dispatch-ack] $id NOT acked: this commit is in '${_here_id}' but the job's work lives in '${_job_id_url}'. Naming a job id is not merging it."
    continue
  fi
  # the merge is the human accept -> record it, then clear the bucket entry
  python3 "$SIGNOFF" --verdict approve "$id" --conditions "merged $sha (dispatch-ack hook)" >/dev/null 2>&1 || true
  python3 "$HANDOFF" --acted "$id" >/dev/null 2>&1 || true
  echo "[dispatch-ack] $id acknowledged as merged in $sha (verdict=approve, cleared from awaiting-action)"
done

# Reconcile-on-push: the message-parse loop above only fires when the commit
# names the job-id. It misses a merged fix whose message omitted the id (or a
# fast-forward merge that makes no commit on the default branch) and can never
# see a read-only research job (which never commits). The reconciler catches
# both by branch-merge state + task_kind instead of the message. Safe (acks
# only), quiet when idle, never fails the commit.
RECONCILE="$HOME/bin/dispatch-ack-reconcile.py"
[ -x "$RECONCILE" ] && python3 "$RECONCILE" --quiet 2>/dev/null || true
exit 0
