#!/usr/bin/env python3
"""Does this bench leak its own answer key?

Item 6, written 2026-08-31. Two failures motivate it, both mine:

  1. `dflash`: an answer key asserted a NEGATIVE about the world ("this does not
     exist") that I never checked. It did exist -- arXiv 2602.06036, ICML 2026 --
     and the wrong key INVERTED two results: my harness failed the question and
     scored as an exemplary negative, while a model that answered correctly
     scored 0.00 as fabrication. A key that is wrong is worse than no key.
  2. A planted bug described in a commit message, docstring or task text is an
     answer key handed to the model under test. It measures reading, not review.

Both are mechanically checkable. Run it before trusting any bench number.

Usage: bench-integrity.py <tasks.json> [--fixtures DIR]
Exit 0 = clean, 1 = integrity problems found.
"""
import argparse, json, re, sys
from pathlib import Path

# Words that mark a claim about the world rather than about the code in hand.
_NEGATIVE = re.compile(
    r"\b(does not exist|doesn't exist|no such|never existed|is not real|"
    r"nonexistent|non-existent|fabricat|invented|made up|hallucinat)\b", re.I)
# Accept any phrasing that names HOW the key was established. The first version
# missed "PROVEN BY EXECUTION" and flagged an already-verified fixture -- a
# checker that only recognises its author's favourite wording produces noise.
_SOURCED = re.compile(r"(https?://|arxiv|doi:|github\.com|VERIFIED|verified by|"
                      r"proven by|differential execution|by execution|confirmed|"
                      r"verification method)", re.I)
# Words that give away that something was deliberately planted.
# Deliberately NARROW. The first version matched bare "deliberately" and
# "the bug", and immediately produced 7 false positives on this very bench --
# every hit was innocuous prose carried over from the real scripts the fixtures
# were cut from ("-e deliberately NOT set", "deliberately not disabled by this
# script"). A leak detector that fires on ordinary English is worse than none:
# it manufactures integrity problems and trains you to ignore it. Require
# phrasing that only an answer key would use.
_LEAK = re.compile(
    r"\b(seeded bug|planted (bug|defect)|the seeded|intentionally (broken|wrong|"
    r"buggy|introduced)|deliberately (broken|wrong|buggy|introduced|inverted)|"
    r"answer key|ground.?truth|this is the bug|bug is here|inject(ed)? a (bug|defect))\b",
    re.I)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("tasks")
    ap.add_argument("--fixtures", default=None)
    a = ap.parse_args()
    tasks_path = Path(a.tasks).expanduser()
    d = json.loads(tasks_path.read_text())
    tasks = d.get("tasks", d if isinstance(d, list) else [])
    fixdir = Path(a.fixtures).expanduser() if a.fixtures else tasks_path.parent / "fixtures"

    problems, notes = [], []
    seeded = [t for t in tasks if t.get("kind") == "seeded-bug"]
    decoys = [t for t in tasks if t.get("kind", "").startswith("benign")]
    notes.append(f"{len(tasks)} task(s): {len(seeded)} seeded, {len(decoys)} decoy(s)")

    for t in tasks:
        tid = t.get("id", "?")
        gt = t.get("ground_truth", "") or ""

        # 1. an unsourced negative claim about the world
        if _NEGATIVE.search(gt) and not _SOURCED.search(gt):
            problems.append(f"{tid}: ground truth asserts a NEGATIVE about the world "
                            f"with no source. You cannot prove a universal negative "
                            f"from absence of evidence -- cite something or restate "
                            f"it as 'we found none'. (the dflash failure)")

        # 2. seeded bugs must state how the key was established
        if t.get("kind") == "seeded-bug" and gt and not _SOURCED.search(gt):
            notes.append(f"{tid}: ground truth cites no verification method "
                         f"(prefer 'VERIFIED BY DIFFERENTIAL EXECUTION: ...')")

        # 3. the answer key must not be inside anything the model reads
        # ONLY what the model actually reads. In review mode that is the .diff;
        # the .sh/.py/.ts source files are inputs to `diff`, never shown to the
        # model. Scanning them was the other half of the false-positive bug --
        # a checker must model the real input surface, not the directory.
        for fname in (f"{tid}.diff",):
            f = fixdir / fname
            if not f.exists():
                continue
            body = f.read_text()
            hit = _LEAK.search(body)
            if hit:
                problems.append(f"{tid}: the fixture the model READS ({fname}) contains "
                                f"{hit.group(0)!r} -- that is the answer key. It measures "
                                f"reading comprehension, not review.")

        # 4. every decoy must name what it is a decoy for, or it is just a clean file
        if t.get("kind", "").startswith("benign") and not t.get("decoy_for"):
            notes.append(f"{tid}: decoy has no `decoy_for` -- an UNMATCHED decoy does "
                         f"not test discrimination, only quietness")

    # 5. structural: matched pairs are what make an FP rate meaningful
    unmatched = [t["id"] for t in seeded
                 if not any(x.get("decoy_for") == t["id"] for x in decoys)]
    if unmatched:
        notes.append(f"{len(unmatched)} seeded bug(s) have no matched decoy: "
                     f"{', '.join(unmatched[:6])} -- a detection without a matched "
                     f"decoy cannot distinguish reasoning from pattern-matching")

    print("=== bench integrity ===")
    for n in notes:
        print(f"  note: {n}")
    for p in problems:
        print(f"  PROBLEM: {p}")
    print(f"\n  VERDICT: {'CLEAN' if not problems else f'{len(problems)} integrity problem(s)'}")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
