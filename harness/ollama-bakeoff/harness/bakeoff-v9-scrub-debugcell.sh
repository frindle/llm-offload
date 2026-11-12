#!/bin/bash
# v9 R1 blocker fix: history-free debug-cell baseline. Fable-ruled 2026-08-24.
#
# WHY: plex-automation is a PUBLIC repo. The debug cell's planted defect lives at
# 3f5e4ff ([A-Za-z0-9] -> [A-Za-z]) on branch bakeoff-v8-debug-baseline @ bc08ae2.
# A model with run_bash can read the answer key out of `git log`, `git log -p` or
# `git log -S` -- BOTH the commit messages and the diff leak it. v8 rows are not
# contaminated (all 19 transcripts grepped, zero git-archaeology), but v9 R1 is
# NO-GO until the debug cell is served from a repo with no such history.
#
# WHAT: build a single-commit, no-remote baseline repo from the bc08ae2 TREE
# (same files, including the planted defect in the working tree -- that is the
# point), re-point the lib, and re-provision every debug-cell worktree from it.
#
# Idempotent: safe to re-run. Rebuilds the clean repo from scratch each time.
#
# Modes:
#   (no args)            run the scrub
#   --retire-old-branch  SEPARATE step, run ONLY after a full preflight has
#                        verified against the new baseline. Deletes the old
#                        poisoned branch; the bundle keeps it reconstructable.
set -euo pipefail

SRC="/Users/user/Desktop/GitHub Projects/plex-automation"
CLEAN="/Users/user/Desktop/GitHub Projects/plex-automation/plex-automation-debugcell"
LIB="/Users/user/Desktop/GitHub Projects/bakeoff/harness/bakeoff-v8-lib.sh"
ARCHIVE="/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-private-archive"
WT_BASE="/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-build-2026-08-22"
PYBIN="/Users/user/bin/plex-bakeoff-venv/bin/python"
OLD_SHA="bc08ae2"; DEFECT_SHA="3f5e4ff"; OLD_BRANCH="bakeoff-v8-debug-baseline"

die() { echo "FATAL: $*" >&2; exit 1; }
say() { echo "[scrub] $*"; }

# ---------------------------------------------------------------------------
# --retire-old-branch : the deferred cleanup step (AMENDMENT 2)
#
# Fable's note said to run `git branch -D bakeoff-v8-debug-baseline` after the
# scrub verifies. As written that FAILS: the main plex-automation checkout is
# currently ON that branch, and git refuses to delete a checked-out branch. So
# this is a separate, explicit mode that checks out main first, and it is NOT
# part of the main scrub run -- deleting the old branch before a preflight has
# actually passed against the new baseline would burn the fallback.
# ---------------------------------------------------------------------------
if [ "${1:-}" = "--retire-old-branch" ]; then
  [ -f "$ARCHIVE/$OLD_BRANCH.bundle" ] || die "no bundle at $ARCHIVE/$OLD_BRANCH.bundle -- run the scrub first"
  git -C "$SRC" bundle verify "$ARCHIVE/$OLD_BRANCH.bundle" >/dev/null 2>&1 \
    || die "bundle does not verify -- refusing to delete the only copy"
  if ! git -C "$SRC" diff --quiet || ! git -C "$SRC" diff --cached --quiet; then
    die "$SRC has uncommitted changes -- refusing to switch branches"
  fi
  if [ "$(git -C "$SRC" rev-parse --abbrev-ref HEAD)" = "$OLD_BRANCH" ]; then
    say "main checkout is on $OLD_BRANCH; switching to main first"
    git -C "$SRC" checkout -q main || die "could not check out main"
  fi
  git -C "$SRC" branch -D "$OLD_BRANCH" || die "branch -D failed"
  say "RETIRED $OLD_BRANCH (recoverable from $ARCHIVE/$OLD_BRANCH.bundle)"
  git -C "$SRC" worktree prune
  exit 0
fi

# ---------------------------------------------------------------------------
# 0. archive the poisoned branch before touching anything
# ---------------------------------------------------------------------------
mkdir -p "$ARCHIVE"
git -C "$SRC" rev-parse --verify "$OLD_BRANCH" >/dev/null 2>&1 \
  || die "$OLD_BRANCH not found in $SRC -- has this already been retired?"
git -C "$SRC" bundle create "$ARCHIVE/$OLD_BRANCH.bundle" "$OLD_BRANCH"
git -C "$SRC" bundle verify "$ARCHIVE/$OLD_BRANCH.bundle" >/dev/null \
  || die "bundle failed verification"
say "archived $OLD_BRANCH -> $ARCHIVE/$OLD_BRANCH.bundle"

# ---------------------------------------------------------------------------
# 1. tear down the OLD worktrees BEFORE rebuilding (AMENDMENT 1, part 1)
#
# A git worktree does NOT have its own object store -- its .git is a file
# pointing back at the parent repo. So every one of the six debug-cell
# worktrees can reach 3f5e4ff through $SRC today. Re-pointing the lib's
# baseline SHA alone would NOT fix this: the worktrees would still resolve
# `git log` against the poisoned parent. They must be removed and re-created
# from the clean repo. Fable's script left this as a comment; this does it.
#
# The slug list is derived from what actually exists on disk rather than
# hardcoded, so the roster can't silently drift out of sync with the script.
# ---------------------------------------------------------------------------
SLUGS=()
if [ -d "$WT_BASE/plex-automation" ]; then
  while IFS= read -r d; do SLUGS+=("$(basename "$d")"); done \
    < <(find "$WT_BASE/plex-automation" -mindepth 1 -maxdepth 1 -type d | sort)
fi
[ ${#SLUGS[@]} -gt 0 ] || die "no existing debug-cell worktrees under $WT_BASE/plex-automation -- refusing to guess the roster"
say "debug-cell roster (${#SLUGS[@]}): ${SLUGS[*]}"

for s in "${SLUGS[@]}"; do
  WT="$WT_BASE/plex-automation/$s"
  # --force because these trees are intentionally dirty between runs; we are
  # rebuilding them from a pinned SHA anyway, so there is nothing to preserve.
  git -C "$SRC" worktree remove --force "$WT" 2>/dev/null || rm -rf "$WT"
done
git -C "$SRC" worktree prune
say "removed ${#SLUGS[@]} old worktrees"

# ---------------------------------------------------------------------------
# 2. build the clean single-commit, no-remote baseline repo
# ---------------------------------------------------------------------------
rm -rf "$CLEAN"; mkdir -p "$CLEAN"
git -C "$SRC" archive "$OLD_SHA" | tar -x -C "$CLEAN"
git -C "$CLEAN" init -q
git -C "$CLEAN" add -A
git -C "$CLEAN" -c user.name=frindle -c user.email=69526085+frindle@users.noreply.github.com \
  commit -qm "Import project"
NEW_SHA=$(git -C "$CLEAN" rev-parse --short HEAD)
say "clean baseline built: $NEW_SHA"

# ---------------------------------------------------------------------------
# 3. leak assertions on the clean repo
# ---------------------------------------------------------------------------
[ -z "$(git -C "$CLEAN" remote)" ] || die "clean repo has a remote"
[ "$(git -C "$CLEAN" rev-list --count HEAD)" = 1 ] || die "expected exactly 1 commit"

# the clean tree must be byte-identical to the v8 baseline tree, or the
# v8/v9 comparability claim is void (catches export-ignore and add -A drops)
[ "$(git -C "$SRC" rev-parse "$OLD_SHA^{tree}")" = "$(git -C "$CLEAN" rev-parse "HEAD^{tree}")" ] \
  || die "tree mismatch: clean baseline is not byte-identical to $OLD_SHA"

# the defect object must not exist here AT ALL
if git -C "$CLEAN" cat-file -e "$DEFECT_SHA" 2>/dev/null; then
  die "defect object $DEFECT_SHA is reachable in the clean repo"
fi

# no diagnostic prose anywhere in history or commit messages
if git -C "$CLEAN" log --all -p \
     | grep -qE 'alphabetic names|digit-bearing group cases|Tighten release-group'; then
  die "leak-grep found diagnostic text in clean history"
fi

# the tree must still be BROKEN -- the inverted preflight. If the repro passes
# here, the planted defect did not survive the archive and the cell is void.
if ( cd "$CLEAN" && "$PYBIN" test_release_group_numeric.py >/dev/null 2>&1 ); then
  die "repro PASSES on pristine tree -- planted defect missing, cell is void"
fi
say "leak assertions pass; repro correctly FAILS on pristine tree"

# ---------------------------------------------------------------------------
# 4. re-point the lib
# ---------------------------------------------------------------------------
sed -i '' -E "s/BASELINE_plex_automation=\"[0-9a-f]+\"/BASELINE_plex_automation=\"$NEW_SHA\"/" "$LIB"
grep -q "BASELINE_plex_automation=\"$NEW_SHA\"" "$LIB" || die "lib re-point failed"
say "lib re-pointed: BASELINE_plex_automation=$NEW_SHA"

# ---------------------------------------------------------------------------
# 5. re-provision worktrees FROM THE CLEAN REPO (AMENDMENT 1, part 2)
#
# Detached at the new baseline, matching how the old ones were attached
# (all six were `bc08ae2 (detached HEAD)`). The lib's _one_task_v8 does
# `git -C "$WT_DIR" reset --hard $BASE_SHA`, which now resolves inside the
# clean repo -- this is AMENDMENT 3: lib:450's RUN_VERIFY does
# `git checkout $BASELINE_plex_automation -- <test files>`, and that SHA only
# exists in the clean repo, so the worktree MUST live there or every debug
# run aborts. Amendments 1 and 3 are the same fix.
# ---------------------------------------------------------------------------
for s in "${SLUGS[@]}"; do
  git -C "$CLEAN" worktree add -q --detach "$WT_BASE/plex-automation/$s" "$NEW_SHA"
done
say "re-provisioned ${#SLUGS[@]} worktrees from $CLEAN"

# ---------------------------------------------------------------------------
# 6. mechanical per-worktree leak-grep -- the pre-dispatch check Fable asked for
#
# This is the assertion that actually matters at dispatch time: prove that a
# model sitting IN the worktree, running git itself, cannot reach the answer key.
# ---------------------------------------------------------------------------
for s in "${SLUGS[@]}"; do
  WT="$WT_BASE/plex-automation/$s"
  [ -d "$WT" ] || die "worktree missing after provisioning: $WT"
  ( cd "$WT" && git cat-file -e "$DEFECT_SHA" 2>/dev/null ) \
    && die "worktree $s can reach $DEFECT_SHA"
  N=$( cd "$WT" && git rev-list --count --all )
  [ "$N" = 1 ] || die "worktree $s sees $N commits, expected 1"
  ( cd "$WT" && git log --all -p \
      | grep -qE 'alphabetic names|digit-bearing group cases|Tighten release-group' ) \
    && die "worktree $s leaks diagnostic text"
  [ -z "$( cd "$WT" && git remote )" ] || die "worktree $s has a remote"
done
say "per-worktree leak-grep passes for all ${#SLUGS[@]} worktrees"

echo
echo "SCRUB OK"
echo "  baseline : $OLD_SHA -> $NEW_SHA"
echo "  repo     : $CLEAN (1 commit, no remote)"
echo "  worktrees: ${#SLUGS[@]} re-provisioned from the clean repo"
echo "  bundle   : $ARCHIVE/$OLD_BRANCH.bundle"
echo
echo "NEXT: v9 pre-dispatch checklist items 1-9 (esp. 5 fresh worktrees, 6 inverted"
echo "      preflight, 8 parity checksum). Only after a full preflight passes:"
echo "      bash bakeoff-v9-scrub-debugcell.sh --retire-old-branch"
