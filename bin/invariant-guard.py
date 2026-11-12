#!/usr/bin/env python3
"""Did this diff delete code that an adjacent comment says must stay?

THE CASE THIS EXISTS FOR (resell-payout902, 2026-08-31). A dispatch removed
`salePriceSynced: false` from a Prisma where-clause. Directly above it sat:

    // Once paid (salePriceSynced=true), never re-stamp overdueAt --
    // otherwise the cleared badge re-appears on the next sync.

The model left that comment in place, added its own below it rationalising the
change, and deleted the guard the comment protects. Typecheck passed. A verify
asserting "touches the sync path" passed. Nothing in the gate looks at whether a
change contradicts a stated invariant, so a documented, deliberately-placed
guard came out silently.

THE SIGNAL IS DECIDABLE and needs no model: a removed line whose immediately
preceding context is a comment carrying invariant language ("never", "always",
"must not", "otherwise ...", "once ..."). That comment is there precisely
because someone expected a future reader to want to remove the line under it.

DELIBERATELY NOT A HARD FAIL. Code under such a comment is sometimes legitimately
removed -- the invariant can be genuinely obsolete, and a checker that blocks
that trains people to ignore it. It reports HIGH so a human reads that hunk
first, and the gate keeps it category=input so it can never condemn code on its
own. Same rule as every other check here: loud, not blocking.
"""
import argparse, json, re, sys
from pathlib import Path

# Language that marks a comment as stating a RULE rather than describing code.
# TIGHTENED after a false-positive sweep over 13 real diffs. The first version
# fired on comments that merely CONTAIN the words -- "Was never actually set
# here" (describing history) and "what still has to be decided" (describing a
# TODO). Neither states a rule. A prohibition is what matters, so:
#   - "never" is rejected when it is preceded by was/were/is/are, which turns it
#     from a prohibition into a description of the past.
#   - "has to" / "needs to" / "required to" are gone entirely: too weak, and
#     they produced the only other false positive.
#   - "otherwise" is gone as a STANDALONE trigger -- it supports a rule but does
#     not state one. The real payout902 comment still fires, on "never re-stamp".
# Result: 1 true positive, 0 false positives across the same 13 diffs.
_INVARIANT = re.compile(
    r"\b(?<!was )(?<!were )(?<!is )(?<!are )never\b"
    r"|\balways\b|\bmust not\b|\bmust never\b|\bmustn't\b"
    r"|\bdo not\b|\bdon't\b|\binvariant\b"
    # "only ever" needs a MODAL in front. Second false-positive sweep, 2026-09-02,
    # on a real dispatch (Diplomat 08c288f0):
    #     "# hours in. SEED_DAYS only ever guarded the soft path."
    # DESCRIBES pre-fix behaviour -- and changing that behaviour was the task.
    # So the guard fired HIGH on the intended fix and capped an otherwise-clean,
    # both-ways-proven job at 'concerns'. Removing the line under it WAS the fix.
    # Same discriminator the `never` clause already uses: modality separates a
    # rule from a description. "must/should/may/can only ever" prohibits;
    # "only ever guarded" narrates. Narrower than downgrading ALL
    # comment-anchored hits, which would gut a check whose every true positive
    # lives in a comment.
    r"|\b(?:must|should|shall|may|can)\s+only\s+ever\b", re.I)
_COMMENT = re.compile(r"^\s*(//|#|/\*|\*|--|<!--)")
_WINDOW = 4          # how far above a removal a governing comment can sit

# RELOCATION SUPPRESSOR. Fourth false-positive class, found on the first
# two-tier-gate dispatch (draft-mjs-phase3, machine-config 1d4db91). A model
# moved `"--verify", "python3 draft-check.py",` into an else-branch as
# `verify_cmd = "python3 draft-check.py"`; the guard read the `-` line as a
# deletion and fired HIGH under the unrelated comment four lines up ("Studio
# work goes through the queue, never a direct API call"). Nothing was removed --
# the code was relocated. The signal is decidable and needs no model: a removed
# line whose SIGNIFICANT LITERAL reappears verbatim in an added (`+`) line of the
# SAME hunk is a move/extract-into-variable/move-into-branch, not a deletion.
#
# What counts as a "significant literal" is chosen to stay SAFE against the
# payout902 case this guard exists for -- there the deleted `salePriceSynced:
# false` never reappears, so nothing here can match and the guard still fires:
#   - the removed line's own stripped text (minus a trailing comma), when it
#     reappears verbatim inside an added line -- catches a whole line moved into
#     a branch; and
#   - each QUOTED string literal in the removed line -- catches a value lifted
#     out into a variable and re-passed (payout902 carries no quoted literal).
# Both are gated on _RELOC_MIN characters: a bare `false`/`id`/`""` reappearing
# somewhere is too common to trust and could mask a real deletion, so short
# fragments are never treated as evidence of relocation. Matching is VERBATIM,
# never fuzzy -- a token GENERALIZED rather than moved (DRAFT_MARK -> mark) does
# not reappear verbatim and is deliberately left to fire (advisory): a fuzzy
# token-substitution match would risk false-negatives on real removals.
_QUOTED = re.compile(r"""(['"`])((?:\\.|(?!\1).)*)\1""")
_RELOC_MIN = 6       # shortest fragment whose verbatim reappearance we trust


def _reloc_literals(removed: str):
    """Distinctive fragments of a removed line; verbatim reappearance in an added
    line means the code moved, not vanished. Empty when the line has no fragment
    long/distinctive enough to trust (e.g. payout902's `salePriceSynced: false`
    yields its whole-line form only, which never reappears)."""
    lits = []
    whole = removed.strip().rstrip(",").strip()
    if len(whole) >= _RELOC_MIN:
        lits.append(whole)
    for m in _QUOTED.finditer(removed):
        body = m.group(2)
        if len(body) >= _RELOC_MIN:
            lits.append(body)
    return lits


def _relocated(removed: str, added: list[str]) -> bool:
    """True if a significant literal of the removed line reappears verbatim in
    any added line of the same hunk -- a relocation, not a deletion."""
    lits = _reloc_literals(removed)
    if not lits:
        return False
    return any(lit in a for lit in lits for a in added)


# EXTEND-IN-PLACE SUPPRESSOR. Fifth false-positive class, found 2026-09-09 on two
# real BFMR dispatches (shipped-flip `select: { cancelled: true }` ->
# `{ cancelled: true, bfmrStatus: true }`, and order-900 rollup). A field was
# ADDED to an object/select literal in place, so the closing `}` shifted right
# and the removed line's whole form (`... true }`) no longer reappears verbatim --
# _relocated misses it and the guard fired HIGH under an unrelated invariant
# comment. The signal is still decidable: strip the removed line's TRAILING run
# of closing delimiters ( } ) ] > and spaces/commas), and if what remains
# reappears as a prefix-substring of an added line that then CONTINUES and
# re-closes with the same delimiter, the line was extended, not deleted.
# SAFE vs payout902: `salePriceSynced: false` has no trailing closer to strip, so
# core == whole and this never matches it -- it still fires. A token GENERALIZED
# (totalPayout -> paidPayout) doesn't reappear even as a prefix, so it is still
# left advisory, exactly as the verbatim-relocation comment above intends.
_CLOSERS = "}])>"


def _extended_in_place(removed: str, added: list[str]) -> bool:
    """True if the removed line was extended in place: its content minus a
    trailing run of closing delimiters reappears as a prefix of an added line
    that continues past it and re-closes with the same delimiter."""
    whole = removed.strip().rstrip(",;").strip()
    core = whole.rstrip(_CLOSERS + " \t,;")
    if core == whole:            # nothing closing was stripped -> not this class
        return False
    if len(core) < _RELOC_MIN:
        return False
    closer = whole[len(core):].strip()
    if not closer:
        return False
    for a in added:
        pos = a.find(core)
        if pos < 0:
            continue
        tail = a[pos + len(core):]
        if tail.strip() and closer[0] in tail:
            return True
    return False


def hunks(diff: str):
    """(file, header, [(kind, text)]) per hunk. kind in {' ', '-', '+'}."""
    f, cur, lines = None, None, []
    for ln in diff.splitlines():
        # File headers FIRST, and they close the open hunk. `--- a/path` starts
        # with '-' and was being collected as a removed LINE, which attributed a
        # file header to the previous file's hunk and reported it as deleted
        # code. Caught in the false-positive sweep, not by a fixture.
        if ln.startswith("diff --git ") or ln.startswith("--- "):
            if cur is not None:
                yield f, cur, lines
            cur, lines = None, []
            continue
        if ln.startswith("+++ b/"):
            f = ln[6:].strip()
            continue
        if ln.startswith("@@"):
            if cur is not None:
                yield f, cur, lines
            cur, lines = ln, []
            continue
        if cur is not None and ln[:1] in (" ", "-", "+"):
            lines.append((ln[0], ln[1:]))
    if cur is not None:
        yield f, cur, lines


def check(diff: str) -> list[dict]:
    out = []
    for f, header, lines in hunks(diff):
        added = [t for (k, t) in lines if k == "+"]
        for i, (kind, text) in enumerate(lines):
            if kind != "-" or not text.strip():
                continue
            # Walk back over the hunk for a governing comment. Context AND added
            # lines both count: the payout902 case left the original comment as
            # context and inserted its own rationalisation under it, so a scan
            # that only looked at context lines would still have found it, and
            # one that only looked at removals would not.
            for j in range(max(0, i - _WINDOW), i):
                k, t = lines[j]
                if k == "-":
                    continue
                if _COMMENT.match(t) and _INVARIANT.search(t):
                    # Suppress a relocation: if this removed line's significant
                    # literal reappears verbatim in an added line of the hunk,
                    # the code moved, it was not deleted. SAFE vs payout902,
                    # whose deleted literal reappears nowhere. This removed line
                    # is explained -- stop looking for a governing comment.
                    if _relocated(text, added) or _extended_in_place(text, added):
                        break
                    out.append({
                        "file": f or "?", "comment": t.strip()[:150],
                        "removed": text.strip()[:150],
                        "hunk": header.strip()[:60]})
                    break
    # De-duplicate: one finding per (file, comment) -- a multi-line removal under
    # one comment is ONE concern, not five, and a check that reports it five
    # times is the crying-wolf failure this whole gate is built to avoid.
    seen, uniq = set(), []
    for o in out:
        key = (o["file"], o["comment"])
        if key not in seen:
            seen.add(key); uniq.append(o)
    return uniq


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--diff", required=True)
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    found = check(Path(a.diff).expanduser().read_text())
    if a.json:
        print(json.dumps({"violations": found}, indent=1))
        return 0
    print("=== invariant guard: did the diff remove code a comment protects? ===")
    if not found:
        print("  No removed line sits under a comment stating an invariant.")
        return 0
    for v in found:
        print(f"  [{v['file']}]  {v['hunk']}")
        print(f"      comment says: {v['comment']}")
        print(f"      but removed:  {v['removed']}")
    print(f"\n  {len(found)} removal(s) under a documented invariant. ADVISORY: the "
          f"rule may be obsolete, but a comment written to stop exactly this "
          f"deserves a human read before the change lands.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
