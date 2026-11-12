#!/usr/bin/env python3
"""Which of the swept anchors can actually be GRADED? Check before dispatching.

The sweep's filter -- "the fix removed a line introduced by a strictly earlier
commit" -- establishes that a pre-bug ancestor EXISTS. It does NOT establish that
a test can PASS at that ancestor, which is criterion 2 and the whole method.

The gap that matters: if the bug was introduced by the commit that ADDED the
feature, the feature does not exist at the anchor at all, so the model's test
ERRORS there instead of passing. passes_at_anchor is False and the case grades
REJECTED however good the diagnosis is. Queueing those measures the staging, not
the model.

Checks, all decidable:
  A. anchor != introduced                     (a row where they match is broken)
  B. anchor is a true ancestor of introduced
  C. the TARGET FILE exists at the anchor     (feature predates the bug)
  D. the SYMBOL SITE exists at the anchor     -- the fix's removed lines have
                                                 some context present at anchor
  E. the introducing commit is focused        (few files: a 30-file commit means
                                                 anchor..HEAD is full of unrelated
                                                 change and will grade LOOSE)
  F. the fix is single-purpose                (a subject naming 3 fixes is 3 bugs;
                                                 the symptom is not attributable)
"""
import re, subprocess, sys
from pathlib import Path

ROOT = Path("/Users/user/Desktop/GitHub Projects")
SRC = ROOT / "machine-config/diagnosis-offload/ANCHORED-REGRESSIONS.txt"


def git(repo, *a):
    return subprocess.run(["git", "-C", str(ROOT / repo), *a],
                          capture_output=True, text=True)


rows, cur = [], None
for line in SRC.read_text().splitlines()[1:]:
    m = re.match(r"\s*(\d+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s+(\S+)\s*$", line)
    if m:
        cur = dict(gap=int(m.group(1)), repo=m.group(2), fix=m.group(3),
                   intro=m.group(4), anchor=m.group(5), file=m.group(6), subj="")
        rows.append(cur)
    elif cur is not None and line.strip():
        cur["subj"] = line.strip()

MULTI = re.compile(r",|\band\b|;", re.I)

print(f"{'ok':<4}{'repo':<26}{'intro':<11}{'why not / notes'}")
usable = []
for r in rows:
    bad = []
    if r["anchor"].startswith(r["intro"][:9]) or r["intro"].startswith(r["anchor"][:9]):
        bad.append("anchor==introduced")
    else:
        anc = git(r["repo"], "merge-base", "--is-ancestor", r["anchor"], r["intro"])
        if anc.returncode != 0:
            bad.append("anchor not an ancestor")
        # C: target file present at the anchor?
        if git(r["repo"], "cat-file", "-e", f"{r['anchor']}:{r['file']}").returncode != 0:
            bad.append("TARGET FILE ABSENT AT ANCHOR (feature born here)")
        else:
            # FILE-EXISTS IS A PROXY FOR FEATURE-EXISTS, and it is too weak.
            # plex /plex-dupe-fix passed the file check (arr-webhook.py has
            # existed forever) while the ROUTE itself was added by the
            # introducing commit -- 0 occurrences at the anchor. A test hitting
            # it ERRORS at the anchor instead of passing, so criterion 2 fails
            # and the case grades REJECTED however good the diagnosis is.
            # Decide the real property: do the lines the fix TOUCHED have
            # meaningful ancestry at the anchor?
            hunk = git(r["repo"], "show", r["fix"], "--format=", "-U0",
                       "--", r["file"]).stdout
            toks = set()
            for ln in hunk.splitlines():
                if ln[:1] in "+-" and ln[1:2] not in "+-":
                    for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{5,}", ln[1:]):
                        toks.add(t)
            if toks:
                anc_src = git(r["repo"], "show",
                              f"{r['anchor']}:{r['file']}").stdout
                present = sum(1 for t in toks if t in anc_src)
                if present == 0:
                    bad.append(f"FEATURE ABSENT AT ANCHOR (none of {len(toks)} "
                               f"identifiers from the fix exist there)")
    # E: how wide is the introducing commit?
    n = git(r["repo"], "show", "--numstat", "--format=", r["intro"]).stdout.strip()
    nfiles = len([x for x in n.splitlines() if x.strip()])
    if nfiles > 6:
        bad.append(f"introducing commit touches {nfiles} files (anchor..HEAD noisy)")
    # F: single-purpose fix?
    if MULTI.search(r["subj"].split("--")[0]):
        bad.append("fix subject names multiple fixes (symptom not attributable)")
    if any("ruff" in r["subj"].lower() or "lint" in r["subj"].lower() for _ in [0]):
        bad.append("lint-only fix: no observable runtime symptom")

    mark = "OK  " if not bad else "--  "
    print(f"{mark}{r['repo']:<26}{r['intro'][:9]:<11}{'; '.join(bad) or r['subj'][:60]}")
    if not bad:
        usable.append(r)

print(f"\nUSABLE: {len(usable)} of {len(rows)}")
for r in usable:
    print(f"  {r['repo']}  intro={r['intro'][:9]} anchor={r['anchor'][:9]} "
          f"file={r['file']}\n      {r['subj'][:72]}")
