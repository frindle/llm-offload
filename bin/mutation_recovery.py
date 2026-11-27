#!/usr/bin/env python3
"""mutation_recovery -- end an attempt that keeps failing to edit, with advice that names the way out.

A pure function over the runner's step list [(command, output)], so it plugs into failfast_reason (the
runner's one loop-breaker) instead of keeping a second counter of its own.

A failed edit is either
  * an anchored_edit refusal (`EDIT-REFUSED[kind]` in the output of an `anchored_edit.py edit` call), or
  * a shell patch command (`git apply`, `patch`) whose output says the hunks did not apply.
The first stale-tag refusal after a good state is EXPECTED (the file moved under the model, which is
what the tag is for) and gets one free retry. Every other failure counts. A successful edit
(`EDIT-OK`, or a patch command that applied) resets both the count and
the free retry. This check runs on every mini run, not only with MINI_ANCHORED_EDITS=1, so the reset matters. At MAX_FAILED failures the attempt ends.
"""
import re

MAX_FAILED = 3

_EDIT_CALL = re.compile(r"anchored_edit(?:\.py)?\s+edit\b")
_PATCH_CALL = re.compile(r"(?:^|[;&|(])\s*(?:git\s+apply|patch)\b")
_PATCH_FAIL = re.compile(r"patch failed|does not apply|Hunk #\d+ FAILED|\.rej\b|corrupt patch|malformed patch|can't find file to patch|"
                         r"Only garbage was found", re.I)
_REFUSED = re.compile(r"EDIT-REFUSED\[(\w+)\]")


def classify(command, output):
    """'ok' | 'stale' | 'refused' | 'patch' | None for one step (None: not an edit attempt)."""
    command, output = command or "", output or ""
    if _EDIT_CALL.search(command):
        m = _REFUSED.search(output)
        if m:
            return "stale" if m.group(1) == "stale" else "refused"
        return "ok" if "EDIT-OK" in output else None
    if _PATCH_CALL.search(command):
        return "patch" if _PATCH_FAIL.search(output) else "ok"   # a patch that applied is a good edit too
    return None


def advice(kinds):
    out = []
    if "stale" in kinds or "refused" in kinds:
        out.append("view the file again (`anchored_edit.py view FILE`) and redo the edit from the new tag and line numbers; "
                   "anchor only on lines the view printed")
    if "patch" in kinds:
        out.append("do not repeat a shell patch command (git apply / patch); make the change with `anchored_edit.py edit` "
                   "or a small exact-string replace instead")
    return "; ".join(out) or "re-read the file before editing again"


def mutation_reason(steps):
    """Why this attempt should end now because its edits keep failing, or None."""
    failed, free_used, kinds = 0, False, []
    for command, output in steps:
        k = classify(command, output)
        if k == "ok":
            failed, free_used, kinds = 0, False, []
        elif k == "stale" and not free_used:
            free_used = True            # the one free retry on an expected stale-tag error
        elif k in ("stale", "refused", "patch"):
            failed += 1
            kinds.append(k)
    if failed >= MAX_FAILED:
        return f"fail-fast: {failed} edits failed since the last good edit. Advice: {advice(set(kinds))}"
    return None
