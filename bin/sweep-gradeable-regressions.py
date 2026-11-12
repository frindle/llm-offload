#!/usr/bin/env python3
"""How many GRADEABLE anchored regressions exist across every repo?

The first sweep answered "does a pre-bug ancestor exist" and produced 18
candidates, of which only 2 turned out gradeable. This one applies the filter that
actually predicts gradeability, learned from those 16 failures:

  1. fix commit is small and touches ONE python file
  2. fix subject is SINGLE-PURPOSE (no comma/and/semicolon listing several fixes)
     and is not a lint-only change (no runtime symptom to observe)
  3. the removed line was introduced by a STRICTLY EARLIER commit (a pre-bug
     ancestor exists at all)
  4. the introducing commit is FOCUSED (<= 6 files), or anchor..HEAD is so noisy
     that head-vs-anchor can flip for unrelated reasons
  5. THE FEATURE EXISTS AT THE ANCHOR -- the decisive one, and the one that killed
     plex /plex-dupe-fix. Not "the file exists" and not "identifiers from the diff
     exist" (get_json and isinstance exist everywhere). The named DEFINITION the
     fix modified must already be defined at the anchor, resolved by AST.

Criterion 5 is the born-broken test: a bug introduced by the commit that ADDED the
feature has no ancestor where a test can PASS, so it fails criterion 2 of the
grader however good the diagnosis is.
"""
import ast, re, subprocess, sys
from pathlib import Path

ROOT = Path("/Users/user/Desktop/GitHub Projects")
MULTI = re.compile(r",|\band\b|;", re.I)
LINT = re.compile(r"\bruff\b|\blint\b|\bflake8\b|\btypo\b|\bformat(ting)?\b", re.I)


def git(repo, *a, **kw):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True,
                          text=True, **kw)


def defs_at(repo, ref, rel):
    """Set of def/class names defined in `rel` at `ref`. None if undecidable."""
    r = git(repo, "show", f"{ref}:{rel}")
    if r.returncode != 0:
        return None
    try:
        tree = ast.parse(r.stdout)
    except SyntaxError:
        return None
    return {n.name for n in ast.walk(tree)
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))}


def enclosing_defs(repo, fix, rel):
    """Names of defs whose body the fix touched, at the fix's parent."""
    r = git(repo, "show", fix, "--format=", "-U0", "--", rel)
    lines = set()
    for m in re.finditer(r"^@@ -(\d+)(?:,(\d+))? ", r.stdout, re.M):
        s = int(m.group(1)); n = int(m.group(2) or 1)
        lines.update(range(s, s + max(n, 1)))
    src = git(repo, "show", f"{fix}^:{rel}")
    if src.returncode != 0 or not lines:
        return set()
    try:
        tree = ast.parse(src.stdout)
    except SyntaxError:
        return set()
    out = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if any(n.lineno <= ln <= (n.end_lineno or n.lineno) for ln in lines):
                out.add(n.name)
    return out


repos = [p.parent for p in ROOT.glob("*/.git")] + [p.parent for p in ROOT.glob("*/*/.git")]
repos = sorted({r for r in repos if r.is_dir()})
print(f"scanning {len(repos)} repos...\n", file=sys.stderr)

keep = []
for repo in repos:
    log = git(repo, "log", "--format=%H|%s", "-500", "--", "*.py").stdout
    for line in log.splitlines():
        if "|" not in line:
            continue
        sha, subj = line.split("|", 1)
        low = subj.lower()
        if not any(w in low for w in ("fix", "correct", "wrong", "broke", "regress", "bug")):
            continue
        if MULTI.search(subj.split("--")[0]) or LINT.search(subj):
            continue
        stat = git(repo, "show", sha, "--numstat", "--format=", "--", "*.py").stdout.split()
        if len(stat) != 3:                      # exactly ONE python file
            continue
        try:
            add, dele = int(stat[0]), int(stat[1])
        except ValueError:
            continue
        rel = stat[2]
        if not (1 <= add <= 15 and 1 <= dele <= 15):
            continue
        diff = git(repo, "show", sha, "--format=", "-U0", "--", rel).stdout
        removed = [l[1:].strip() for l in diff.splitlines()
                   if l.startswith("-") and not l.startswith("---") and len(l.strip()) > 25]
        for L in removed[:4]:
            intro = git(repo, "log", "-S", L, "--format=%H", "--", rel).stdout.split()
            if not intro:
                continue
            I = intro[-1]
            if I.startswith(sha[:12]):
                continue
            gap = git(repo, "rev-list", "--count", f"{I}..{sha}", "--", rel).stdout.strip()
            if not gap.isdigit() or int(gap) < 2:
                continue
            anchor = git(repo, "rev-parse", f"{I}^").stdout.strip()
            if not anchor:
                continue
            nfiles = len([x for x in git(repo, "show", "--numstat", "--format=", I)
                          .stdout.strip().splitlines() if x.strip()])
            if nfiles > 6:
                continue
            # CRITERION 5 -- the born-broken test, by AST not by grep
            want = enclosing_defs(repo, sha, rel)
            have = defs_at(repo, anchor, rel)
            if not want or have is None or not (want & have):
                continue
            keep.append((int(gap), repo.name, sha[:9], I[:9], anchor[:9], rel,
                         sorted(want & have)[:2], subj[:58]))
            break

keep.sort(reverse=True)
print(f"GRADEABLE ANCHORED REGRESSIONS: {len(keep)}\n")
for gap, repo, fix, I, anc, rel, syms, subj in keep:
    print(f"  gap={gap:<3} {repo}  fix={fix} intro={I} anchor={anc}")
    print(f"        file={rel}  symbol(s)={','.join(syms)}")
    print(f"        {subj}")
