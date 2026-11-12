#!/usr/bin/env python3
"""Scorer for the photo/greenfield cell. Replaces `verify_passed`, which cannot
tell building the feature from doing nothing.

WHY THIS EXISTS
---------------
The photo cell's preflight and verify are the same command (`npm run build`) and
the preflight must PASS on the pristine tree. So the baseline builds, a model
that changes nothing passes verify, and two shipped routing verdicts were
inflated by exactly that. Fable's ruling: a cell whose verify passes pristine
may not use `verify_passed` as its success metric; it needs a separate scorer.

WHAT IT SCORES
--------------
The task asks for four things. Each is a separate, independently-checkable
requirement, so each is scored separately rather than collapsed into a verdict:

  1. an upload control        -- a file input / FormData / multipart handler
  2. an API route             -- a new route handler, not just a UI edit
  3. order association        -- the upload is tied to a specific order
  4. storage                  -- the image is actually persisted

Score is requirements-met / 4, plus `built` (did the tree still compile).

WHAT IT DELIBERATELY DOES NOT CLAIM
-----------------------------------
That the feature WORKS. This is a floor, exactly like the fixture checker: it
detects presence, not correctness, and a model could satisfy all four with code
that never runs. It exists to replace a metric that scored ZERO as a pass, not
to pass judgement on quality. Report it as `requirements_present`, never as
`passes`.

READS THE ARCHIVE, NOT THE WORKTREE. The worktree is reset before the next run,
so scoring must happen against `$STEM.diff` + `$STEM.newfiles`. The latter only
exists for runs after the untracked-content fix -- older runs have no recoverable
deliverable at all, which is why Fable voided them.
"""
import argparse
import re
import sys
from pathlib import Path

REQUIREMENTS = {
    "upload_control": [
        r'type=["\']file["\']', r"\bFormData\b", r"multipart/form-data",
        r"\.files\b", r"<input[^>]*file",
    ],
    "api_route": [
        r"export\s+async\s+function\s+(POST|PUT)\b",
        r"app/api/[^\s]*route\.(ts|tsx|js)",
        r"NextResponse", r"\breq\.formData\(",
    ],
    "order_association": [
        r"\borderId\b", r"\border\.id\b", r"orders/\[id\]", r"\bconnect:\s*\{\s*id",
    ],
    "storage": [
        r"\bwriteFile\b", r"\bcreateWriteStream\b", r"prisma\.\w*[Aa]ttachment",
        r"prisma\.\w*[Pp]hoto", r"\bBuffer\.from\(", r"\.upload\(",
    ],
}


def score_artifact(diff_path: Path):
    # MISSING and EMPTY are different facts and must not collapse.
    #   file absent  -> unscoreable: we cannot know what the model did.
    #   file present but 0 bytes -> the model changed NOTHING. That is 0/4, a
    #                               real measurement, and it is the exact row
    #                               that scored a PASS under `verify_passed`.
    # Treating the second as unscoreable would quietly drop the very rows this
    # scorer exists to catch.
    if not diff_path.is_file():
        return None
    text = ""
    for p in (diff_path, diff_path.with_suffix(".newfiles")):
        if p.is_file():
            text += p.read_text(encoding="utf-8", errors="replace")
    # Only look at ADDED lines in the diff portion, plus all new-file content;
    # otherwise a pattern already present in the baseline scores for free.
    added = []
    in_newfiles = False
    for line in text.splitlines():
        if line.startswith("=== UNTRACKED FILE CONTENTS"):
            in_newfiles = True
        if in_newfiles or (line.startswith("+") and not line.startswith("+++")):
            added.append(line)
    body = "\n".join(added)
    met = {}
    for req, pats in REQUIREMENTS.items():
        met[req] = any(re.search(p, body, re.IGNORECASE) for p in pats)
    return met, len(body)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("artifact", help="path to the run's .diff (its .newfiles is read too)")
    a = ap.parse_args()
    r = score_artifact(Path(a.artifact))
    if r is None:
        print("no artifact -- unscoreable (pre-dates the untracked-content fix)")
        return 2
    met, size = r
    n = sum(met.values())
    print(f"requirements_present {n}/4  (evidence {size} bytes)")
    for k, v in met.items():
        print(f"   {'YES' if v else ' no'}  {k}")
    print("\nFLOOR ONLY: detects presence, not correctness. Never report as 'passes'.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
