#!/usr/bin/env bash
# batch-merge-and-sync.sh -- one batched, idempotent pass over the pending merges + mirror syncs.
#
#   bash ~/bin/batch-merge-and-sync.sh --dry-run     # read-only: prints exactly what a real run would do
#   bash ~/bin/batch-merge-and-sync.sh               # real run (stops at the first failure)
#   bash ~/bin/batch-merge-and-sync.sh --only dashboard|mc|llmd|runner   # one section (default: all, in this order)
#
# Sections (order matters: the dashboard tests get linked into ~/bin before ~/bin is mirrored):
#   dashboard  ollama-queue-dashboard: commit + push the live dashboard patches and their tests
#   mc         machine-config: mirror every ~/bin file that differs / is new, commit, push
#   llmd       local-llm-dispatch: land fix/singleflight-orphan-driver + fix/driver-killed-not-harness
#              (fast-forward) and fix/stalled-bundle-lane-hold (cherry-picked onto them, one known
#              keep-both conflict in bin/ollama-queue.py), add the tests to MANIFEST.txt, push main
#   runner     dispatch-cpu-runner: confirm feat/runner-caps-python is on main (push if not), watch CI,
#              then PRINT the Unraid commands (this script never touches Unraid or Docker)
#
# Never edits a running script, never restarts the queue daemon, never touches ~/.claude/.qwen-manual,
# adds no attribution lines to any commit message it creates.
set -euo pipefail

DRY=0; ONLY=all
while [ $# -gt 0 ]; do
  case "$1" in
    --dry-run) DRY=1;;
    --only) shift; ONLY="${1:-}";;
    -h|--help) sed -n 2,20p "$0"; exit 0;;
    *) echo "unknown arg: $1" >&2; exit 2;;
  esac; shift
done
case "$ONLY" in all|dashboard|mc|llmd|runner) ;; *) echo "--only must be dashboard|mc|llmd|runner" >&2; exit 2;; esac

P="$HOME/Desktop/GitHub Projects"
DASH="$P/ollama-queue-dashboard"; MC="$P/machine-config"; LLMD="$P/local-llm-dispatch"; RUN="$P/dispatch-cpu-runner"
SCRATCH=/private/tmp/claude-501/batch-merge
NOSYNC_PID_PAT='nosync\.py'

say()  { printf '\n==> %s\n' "$*"; }
note() { printf '    %s\n' "$*"; }
die()  { printf '\nSTOP: %s\n' "$*" >&2; exit 1; }
# mut: a mutating command. Printed always; executed only on a real run.
mut()  { if [ "$DRY" = 1 ]; then printf '    [dry-run] would run: %s\n' "$*"; else printf '    + %s\n' "$*"; "$@"; fi; }
want() { [ "$ONLY" = all ] || [ "$ONLY" = "$1" ]; }

[ "$DRY" = 1 ] && echo "*** DRY RUN: read-only, nothing is committed, pushed, copied or merged ***"

# ---------------------------------------------------------------------------------------------
say "0. preflight: iCloud .nosync migration"
migrating=0
if pgrep -f "$NOSYNC_PID_PAT" >/dev/null 2>&1; then
  migrating=1
  note "migration job is STILL RUNNING (pid $(pgrep -f "$NOSYNC_PID_PAT" | tr '\n' ' ')); last log lines:"
  tail -3 /private/tmp/claude-501/nosync/nosync.log 2>/dev/null | sed 's/^/      /' || true
else
  note "migration job not running"
fi
repo_ready() {   # $1 = repo path. OK if it is a symlink to *.nosync, or (migration finished) a plain dir with no .nosync twin
  local r="$1"
  if [ -L "$r" ]; then case "$(readlink "$r")" in *.nosync) return 0;; esac; fi
  [ "$migrating" = 1 ] && return 1
  [ -e "$r.nosync" ] && return 1          # half-switched: twin exists but the link does not
  return 0
}
need=()
want dashboard && need+=("$DASH"); want mc && need+=("$MC"); want llmd && need+=("$LLMD"); want runner && need+=("$RUN")
notready=()
for r in "${need[@]}"; do
  if repo_ready "$r"; then note "ready:   $(basename "$r")"; else note "NOT READY (not yet a symlink to .nosync, migration pending): $(basename "$r")"; notready+=("$r"); fi
done
if [ ${#notready[@]} -gt 0 ]; then
  if [ "$DRY" = 1 ]; then
    note "(dry-run continues; a real run would refuse here -- wait for the migration, then rerun)"
  else
    die "migration still switching: ${notready[*]##*/}. Wait for nosync.py to finish (tail -f /private/tmp/claude-501/nosync/nosync.log), then rerun."
  fi
fi
command -v gh >/dev/null || die "gh not found"

default_branch() { git -C "$1" symbolic-ref --short refs/remotes/origin/HEAD 2>/dev/null | sed 's#^origin/##'; }
# iCloud left a conflict-copy ref ('refs/heads/master 2', an invalid ref name) in machine-config; it makes
# `git fetch`/`push` fail with "bad object". Any ref file with a space in its name whose commit is already
# reachable from a real branch is moved aside (never deleted); anything else stops the run.
heal_bad_refs() {
  local repo="$1" f sha
  while IFS= read -r f; do
    [ -n "$f" ] || continue
    sha=$(tr -d '[:space:]' < "$f")
    if git -C "$repo" cat-file -e "$sha^{commit}" 2>/dev/null && [ -n "$(git -C "$repo" for-each-ref --contains "$sha" --format=x refs/heads refs/remotes 2>/dev/null | head -1)" ]; then
      note "invalid ref file '${f#$repo/.git/}' -> $sha is reachable from real branches"
      mkdir -p "$SCRATCH/moved-refs"; mut mv "$f" "$SCRATCH/moved-refs/$(basename "$repo")--$(echo "${f#$repo/.git/}" | tr ' /' '__')"
    else
      die "invalid ref '${f#$repo/.git/}' points at $sha which is not reachable from any branch -- inspect by hand"
    fi
  done < <(find "$repo/.git/refs" -name '* *' -type f 2>/dev/null)
}
clean_fetch() {
  heal_bad_refs "$1"
  if [ "$DRY" = 1 ]; then git -C "$1" fetch -q origin 2>/dev/null || note "(dry-run: fetch skipped/failed, using existing remote-tracking refs)"
  else git -C "$1" fetch -q origin; fi
}

# ---------------------------------------------------------------------------------------------
if want dashboard; then
say "1. ollama-queue-dashboard: commit + push live dashboard patches"
cd "$DASH"
clean_fetch "$DASH"
BR=$(git rev-parse --abbrev-ref HEAD); [ "$BR" = main ] || die "dashboard is on '$BR', expected main"
[ "$(git rev-list --count HEAD..origin/main)" = 0 ] || die "dashboard main is behind origin/main; pull first"
note "no CI workflow in this repo (.github absent); gate = its own tests, run below against the real ~/bin layout"
DASH_FILES=(src/bundle_view.py src/ollama-queue-api.py tests/test-dashboard-b.py \
            tests/test-gate-held-not-parked.py tests/test-cpu-lane-local.py tests/test-heal-nonterminal.py)
for f in "${DASH_FILES[@]}"; do [ -f "$f" ] || die "missing $f"; done
python3 -c 'import ast,sys; [ast.parse(open(f).read(), f) for f in sys.argv[1:]]' src/bundle_view.py src/ollama-queue-api.py
for t in test-gate-held-not-parked.py test-heal-nonterminal.py test-cpu-lane-local.py test-dashboard-b.py; do
  note "test: $t"
  ( cd tests && OLLAMA_QUEUE_NO_NOTIFY=1 PYTHONDONTWRITEBYTECODE=1 timeout 300 python3 "$t" >"$SCRATCH.dash.$t.out" 2>&1 ) \
    || { tail -15 "$SCRATCH.dash.$t.out"; die "dashboard test failed: $t"; }
done
note "dashboard tests: all pass"
git status -s -- "${DASH_FILES[@]}" | sed 's/^/      /'
if git diff --quiet -- "${DASH_FILES[@]}" && [ -z "$(git ls-files --others --exclude-standard -- "${DASH_FILES[@]}")" ]; then
  note "nothing to commit (already committed)"
else
  mut git add -- "${DASH_FILES[@]}"
  mut git commit -q -m "dashboard: gate-held rows are waits not parks (isGateHeld/jobAttentionKind/liveNow); heal-pending jobs read escalation-review not failed; whose-job wait phrase; local-stage CPU lane rows + why-local; tests"
fi
if [ "$(git rev-list --count origin/main..HEAD)" != 0 ] || [ "$DRY" = 1 ]; then mut git push origin main; else note "already pushed"; fi
# the dashboard's other tests are symlinked into ~/bin; give the new one the same treatment so machine-config mirrors it
if [ ! -e "$HOME/bin/test-gate-held-not-parked.py" ]; then
  mut ln -s "$DASH/tests/test-gate-held-not-parked.py" "$HOME/bin/test-gate-held-not-parked.py"
fi
note "left alone on purpose (not committed): ANSWER.md, graphify-out/, *.bak-* files, .gitattributes"
fi

# ---------------------------------------------------------------------------------------------
if want mc; then
say "2. machine-config: mirror ~/bin -> bin/, commit, push"
cd "$MC"
clean_fetch "$MC"
MCB=$(default_branch "$MC"); MCB=${MCB:-master}
note "machine-config's default branch is '$MCB' (there is no 'main' here)"
[ "$(git rev-parse --abbrev-ref HEAD)" = "$MCB" ] || die "machine-config is not on $MCB"
[ "$(git rev-list --count HEAD..origin/$MCB)" = 0 ] || die "machine-config is behind origin/$MCB; pull first"
note "pre-existing unrelated dirt (NOT staged by this script):"; git status -s | grep -v ' bin/\| LaunchAgents/' | sed 's/^/      /' || true

changed=0
sync_file() {   # $1 = file name in ~/bin
  local f="$1" dst="$MC/bin/$1"
  if [ ! -e "$dst" ]; then note "NEW  $f"; elif ! cmp -s "$HOME/bin/$f" "$dst"; then note "MOD  $f"; else return 0; fi
  changed=$((changed+1))
  mut cp -p "$HOME/bin/$f" "$dst"
}
cd "$HOME/bin"
for f in *; do
  [ -f "$f" ] || continue
  case "$f" in
    *.bak*|*.orig|*.log|*.lock|*.corrupt*|*backup*|*.pre-*|*.retired-*|*.pyc|.DS_Store) continue;;
    gRPCServerCLI|llama-swap|ollama-queue-state.json|signoff.json|vault-embeddings.json) continue;;
    ollama-worker-v7.py|ollama-worker-v8.py) continue;;     # frozen for the bake-off; the owner's call, never blanket-synced
  esac
  [ -s "$f" ] || { note "skip zero-byte file (stray, not mirrored): $f"; continue; }
  sync_file "$f"
done
for d in test-fixtures test-fixtures-gate-finding-check \
         test-fixtures-gate-verify-exit test-fixtures-live-artifacts test-fixtures-review-false-code-high \
         test-fixtures-slice-plan-context test-fixtures-source-text-harness review-fixtures verify-claims; do
  [ -d "$HOME/bin/$d" ] || continue
  n=$(rsync -a --dry-run --itemize-changes --exclude='*.bak*' --exclude=__pycache__ --exclude=node_modules "$HOME/bin/$d" "$MC/bin/" | grep -c '^[<>]f' || true)
  if [ "$n" -gt 0 ]; then note "DIR  $d: $n file(s) differ"; changed=$((changed+n))
    mut rsync -a --exclude='*.bak*' --exclude=__pycache__ --exclude=node_modules "$HOME/bin/$d" "$MC/bin/"; fi
done
for f in "$HOME"/bin/ts-mutator/*.mjs "$HOME"/bin/ts-mutator/package*.json; do
  [ -f "$f" ] || continue; b=$(basename "$f"); cmp -s "$f" "$MC/bin/ts-mutator/$b" 2>/dev/null && continue
  note "MOD  ts-mutator/$b"; changed=$((changed+1)); mut cp -p "$f" "$MC/bin/ts-mutator/$b"
done
for f in "$HOME"/Library/LaunchAgents/com.example*.plist; do
  [ -f "$f" ] || continue; b=$(basename "$f"); cmp -s "$f" "$MC/LaunchAgents/$b" 2>/dev/null && continue
  note "LA   $b"; changed=$((changed+1)); mut cp -p "$f" "$MC/LaunchAgents/$b"
done
note "$changed file(s) to mirror"

if [ "$DRY" = 0 ]; then
  cd "$MC"
  git add -- bin LaunchAgents
  if git diff --cached --quiet; then
    note "nothing staged: machine-config already in sync"
  else
    # secrets rule: none inline, ever. Scan only the ADDED lines of what is staged.
    if git diff --cached -U0 | grep '^+[^+]' | grep -iE '(api[_-]?key|passw(or)?d|secret|bearer|token)["'"'"']?[[:space:]]*[:=][[:space:]]*["'"'"'][A-Za-z0-9_./+=-]{20,}["'"'"']|-----BEGIN [A-Z ]*PRIVATE KEY|gh[pousr]_[A-Za-z0-9]{30,}|sk-[A-Za-z0-9]{30,}|AKIA[0-9A-Z]{16}' ; then
      git reset -q; die "possible secret in the staged diff (lines above); unstaged everything. Review, then rerun."
    fi
    git diff --cached --stat | tail -1
    git commit -q -m "machine-config: sync bin/ from ~/bin (cause-owners, auto stop reasons, single-flight, cpu lane/stage/prefetch, harness lint, gate-settle yield, worker robustness, queue/dashboard/gate/handoff fixes + tests)"
    git push origin "$MCB"
  fi
else
  note "[dry-run] would then: git add -- bin LaunchAgents; secret-scan the staged diff; commit; git push origin $MCB"
fi
fi

# ---------------------------------------------------------------------------------------------
if want llmd; then
say "3. local-llm-dispatch: land the three fix branches, MANIFEST, push main"
cd "$LLMD"
clean_fetch "$LLMD"
B1=fix/singleflight-orphan-driver; S1=01eced4
B2=fix/driver-killed-not-harness;  S2=fb05f4f
B3=fix/stalled-bundle-lane-hold;   S3=f08c305
tip() { git rev-parse --short=7 "$1"; }
[ "$(tip "origin/$B1")" = "$S1" ] || die "$B1 moved (now $(tip origin/$B1), expected $S1) -- re-inspect"
[ "$(tip "origin/$B2")" = "$S2" ] || die "$B2 moved (now $(tip origin/$B2), expected $S2) -- re-inspect"
[ "$(tip "$B3")" = "$S3" ]        || die "$B3 moved (now $(tip $B3), expected $S3) -- re-inspect"
note "origin/main = $(tip origin/main). NOTE this checkout's local main is behind AND dirty (it holds SYNC.sh output); it is deliberately not touched."
note "$B1 and $B2 are a linear chain on top of origin/main (fast-forward). $B3 is based on the OLD main f57ce5e:"
note "  git merge-tree vs the chain -> conflicts in: $(git merge-tree --write-tree --name-only "origin/$B2" "$B3" 2>/dev/null | sed -n '2,/^$/p' | tr '\n' ' ')"
note "  (adjacent additions in bin/ollama-queue.py bundle-commit sweep; resolution = keep both sides; the live ~/bin file already has both)"
note "live ~/bin/ollama-queue.py already contains every added line of all three branches; $B2's change is live too (its constant sits in a different place)."

if [ "$DRY" = 1 ]; then
  note "[dry-run] would: worktree $SCRATCH/llmd from origin/main; ff-merge $B1 then $B2; cherry-pick $B3 (auto keep-both on ollama-queue.py);"
  note "[dry-run]        git mv bin/test-slice-single-flight.py tests/; add 2 tests to MANIFEST.txt; run-tests.sh on the 5 relevant tests; commit; git push origin HEAD:main"
  # rehearse what is rehearsable read-only: the conflict set is exactly the one expected file
  conf=$(git merge-tree --write-tree --name-only "origin/$B2" "$B3" 2>/dev/null | sed -n '2,/^$/p' | grep -v '^$' || true)
  [ "$conf" = "bin/ollama-queue.py" ] || die "unexpected conflict set: '$conf'"
  note "conflict set verified = only bin/ollama-queue.py"
else
  WT="$SCRATCH/llmd"; mkdir -p "$SCRATCH"
  git worktree remove --force "$WT" 2>/dev/null || true; rm -rf "$WT"; git worktree prune
  mut git worktree add -q -B batch-merge-llmd "$WT" origin/main
  cd "$WT"
  for b in "origin/$B1" "origin/$B2"; do
    if git merge-base --is-ancestor "$b" HEAD; then note "already in main: $b"; else mut git merge -q --ff-only "$b"; fi
  done
  if git cherry HEAD "$B3" | grep -q '^+'; then
    if ! git cherry-pick "$B3" >/dev/null 2>&1; then
      bad=$(git diff --name-only --diff-filter=U | tr '\n' ' ')
      [ "$bad" = "bin/ollama-queue.py " ] || { git cherry-pick --abort || true; die "cherry-pick of $B3 conflicts in unexpected files: $bad"; }
      python3 - <<'PY'
import re
p = "bin/ollama-queue.py"
s = open(p).read()
pat = re.compile(r"<<<<<<< [^\n]*\n(.*?)=======\n(.*?)>>>>>>> [^\n]*\n", re.S)
s2 = pat.sub(lambda m: m.group(1) + m.group(2), s)
assert "<<<<<<<" not in s2 and ">>>>>>>" not in s2 and len(pat.findall(s)) == 1, "unexpected conflict shape"
open(p, "w").write(s2)
PY
      python3 -m py_compile bin/ollama-queue.py
      rm -rf bin/__pycache__
      git add bin/ollama-queue.py
      GIT_EDITOR=true git cherry-pick --continue >/dev/null
      note "cherry-pick resolved (keep both sides)"
    fi
  else note "already in main: $B3 (patch-equivalent)"; fi
  # tests live in tests/ per MANIFEST.txt convention ('tests|~/bin/test-*.py'); the branch put one in bin/
  [ -f bin/test-slice-single-flight.py ] && [ ! -e tests/test-slice-single-flight.py ] && git mv bin/test-slice-single-flight.py tests/
  python3 - <<'PY'
import re
lines = open("MANIFEST.txt").read().split("\n")
for name in ("test-slice-single-flight.py", "test-stalled-bundle-lane-release.py"):
    ent = "tests|~/bin/" + name
    if ent in lines: continue
    idx = [i for i, l in enumerate(lines) if l.startswith("tests|")]
    pos = next((i for i in idx if lines[i] > ent), idx[-1] + 1)
    lines.insert(pos, ent)
open("MANIFEST.txt", "w").write("\n".join(lines))
PY
  git add MANIFEST.txt
  git diff --cached --quiet || git commit -q -m "MANIFEST: track test-slice-single-flight.py and test-stalled-bundle-lane-release.py (tests/)"
  chmod +x run-tests.sh
  note "sandboxed tests (fake HOME, never touches live tree)"
  ./run-tests.sh test-slice-single-flight.py test-auto-park-escalate.py test-stalled-bundle-lane-release.py \
                 test-queue-bundle-no-bounce.py test-gate-settle-yield.py | tee "$SCRATCH/llmd-tests.txt"
  grep -q 'failed=0' "$SCRATCH/llmd-tests.txt" || die "llmd tests failed; nothing pushed (worktree kept at $WT)"
  mut git push origin HEAD:main
  cd "$LLMD"; git worktree remove --force "$WT"; git branch -D batch-merge-llmd >/dev/null 2>&1 || true
  note "pushed. Superseded branches ($B1, $B2, $B3) are now ancestors/equivalents of origin/main; delete at leisure."
fi
fi

# ---------------------------------------------------------------------------------------------
# Historical step (early 11-2026 batch). Since 11-12 the runner is published by offload-repo-sync to frindle/llm-offload
# (image ghcr.io/frindle/llm-offload-cpu-runner); frindle/dispatch-cpu-runner is retired, so this step no longer applies.
if want runner; then
say "4. dispatch-cpu-runner: feat/runner-caps-python -> main, CI, Unraid commands"
cd "$RUN"
clean_fetch "$RUN"
SHA=$(git rev-parse feat/runner-caps-python)
if git merge-base --is-ancestor "$SHA" origin/main; then
  note "feat/runner-caps-python ($(git rev-parse --short "$SHA")) is ALREADY on origin/main -- no push needed"
else
  mut git push origin feat/runner-caps-python:main
fi
note "image workflow runs for $(git rev-parse --short "$SHA"):"
gh run list -R frindle/dispatch-cpu-runner --workflow image -L 3 --json databaseId,status,conclusion,headSha,event \
   --jq '.[] | "      run \(.databaseId)  \(.headSha[0:7])  \(.status)/\(.conclusion)  \(.event)"'
RID=$(gh run list -R frindle/dispatch-cpu-runner --workflow image --commit "$SHA" -L 1 --json databaseId --jq '.[0].databaseId // empty')
[ -n "$RID" ] || { [ "$DRY" = 1 ] && { note "[dry-run] no run found yet for that sha"; RID=""; } || die "no image run found for $SHA (just pushed? wait a few seconds and rerun)"; }
if [ -n "$RID" ]; then
  st=$(gh run view "$RID" -R frindle/dispatch-cpu-runner --json status,conclusion --jq '.status+"/"+(.conclusion//"")')
  case "$st" in
    completed/success) note "CI green (run $RID)";;
    completed/*) die "image workflow for $SHA finished $st -- DO NOT pull the image";;
    *) if [ "$DRY" = 1 ]; then note "[dry-run] CI is $st; a real run would: gh run watch $RID --exit-status"
       else mut gh run watch "$RID" -R frindle/dispatch-cpu-runner --exit-status || die "CI failed -- DO NOT pull the image"; fi;;
  esac
fi
cat <<'EOF'

    ---- ONLY AFTER CI IS GREEN (above): run these in the UNRAID SHELL (the Mac API key cannot updateContainer; do not use update.sh / the Docker tab) ----
        cd /mnt/user/appdata/dispatch-cpu-runner && docker compose pull && docker compose up -d

    ---- then verify, back on the Mac ----
      # (there is no /api/cpu/runners route; the runner list with its advertised caps is in /api/cpu-lane)
      curl -s localhost:7684/api/cpu-lane | python3 -c 'import sys,json; [print(r) for r in json.load(sys.stdin)["runners"]]'
          -> expect a runner entry with "caps" showing node 26 and python 3.14 (caps are only present while fresh; wait ~1 min after up -d)
      tail -n 20 ~/.ollama-dispatch/cpu-lane.log | grep -c '"path": "runner"'
          -> after the next CPU stage, lines read "path": "runner" instead of "path": "local" / "ineligible: ... node 22 ..."
EOF
fi

say "done$([ "$DRY" = 1 ] && echo ' (dry-run: nothing changed)')"
