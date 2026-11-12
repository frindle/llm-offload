#!/usr/bin/env python3
"""Find TRUE-ANCESTOR regressions: a bug that was introduced after the code worked.

The diagnosis method needs criterion 2 (test passes at a pre-bug anchor), which
only exists when the code was once CORRECT. Most real bugs fail this: they were
born broken (introduced by the same commit that created the feature), so there is
no ancestor where the symptom is absent.

A candidate is a fix commit F where:
  - F removes a line L from a .py file (Python => importable, no JS/TSX render harness)
  - L was INTRODUCED by commit I, found with `git log -S`
  - I is strictly older than F's parent, and I is not F itself
  - I^ exists  => that is the pre-bug anchor
The gap (commits between I and F) is reported: a bug that lived a long time is a
better target than one fixed minutes later, because a same-day fix usually means
the feature was born broken.

Prints candidates ranked by how long the bug survived.
"""
import subprocess, sys
from pathlib import Path

ROOT = Path("/Users/user/Desktop/GitHub Projects")
REPOS = [r for r in sys.argv[1:]] or [
    "plex-automation", "machine-config", "network-bandwidth-monitor",
    "drivecam-backup", "teams-shifts-staffing-export", "vacation-planner",
    "meshtastic-bitchat-bridge", "mitm-control",
]
MAX_CHANGED = 15


def git(repo, *args):
    return subprocess.run(["git", "-C", str(ROOT / repo), *args],
                          capture_output=True, text=True).stdout


cands = []
for repo in REPOS:
    if not (ROOT / repo / ".git").exists():
        continue
    log = git(repo, "log", "--format=%H|%s", "-400", "--", "*.py")
    for line in log.splitlines():
        if "|" not in line:
            continue
        sha, subj = line.split("|", 1)
        if not any(w in subj.lower() for w in ("fix", "correct", "wrong", "broke",
                                               "regress", "revert", "bug")):
            continue
        # small, surgical fixes only
        stat = git(repo, "show", sha, "--numstat", "--format=", "--", "*.py").split()
        try:
            add, dele = int(stat[0]), int(stat[1])
        except Exception:
            continue
        if not (1 <= add <= MAX_CHANGED and 1 <= dele <= MAX_CHANGED):
            continue
        pyfile = stat[2] if len(stat) > 2 else ""
        # the lines this fix REMOVED are the buggy ones
        diff = git(repo, "show", sha, "--format=", "-U0", "--", pyfile)
        removed = [l[1:].strip() for l in diff.splitlines()
                   if l.startswith("-") and not l.startswith("---")
                   and len(l.strip()) > 25]
        for L in removed[:3]:
            intro = git(repo, "log", "-S", L, "--format=%H", "--", pyfile).split()
            if not intro:
                continue
            I = intro[-1] if len(intro) > 1 else intro[0]
            if I.startswith(sha[:12]):
                continue                      # born broken in this very commit
            # how many commits did the bug survive?
            gap = git(repo, "rev-list", "--count", f"{I}..{sha}", "--", pyfile).strip()
            anchor = git(repo, "rev-parse", f"{I}^").strip()
            if not anchor or not gap.isdigit() or int(gap) < 2:
                continue
            cands.append((int(gap), repo, sha[:9], I[:9], anchor[:9], pyfile, subj[:58]))
            break

cands.sort(reverse=True)
print(f"{'gap':>4}  {'repo':<26} {'fix':<10} {'introduced':<10} {'anchor':<10} file / subject")
for gap, repo, sha, I, anchor, f, subj in cands[:18]:
    print(f"{gap:>4}  {repo:<26} {sha:<10} {I:<10} {anchor:<10} {f}")
    print(f"{'':>4}  {'':<26} {subj}")
if not cands:
    print("no true-ancestor regressions found -- every fix repaired code that was born broken")
