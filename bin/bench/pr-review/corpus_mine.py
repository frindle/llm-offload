#!/usr/bin/env python3
"""Mine regression CANDIDATES from git history: a fix commit F whose removed lines were blamed to an earlier commit I.

Output is a JSONL of candidates (I, F, file, I-side line range). It is only a candidate list: a human reads both
sides and keeps an item when F really repairs a defect that I introduced. Read-only git commands only.

  corpus_mine.py REPO [--max-diff 400] [--limit 400] > cands.jsonl
"""
import argparse, json, re, subprocess, sys
from collections import defaultdict

FIXRE = re.compile(r"\b(fix|fixes|fixed|bug|regress|revert|repair|correct|broken|wrong|crash|leak)\b", re.I)
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")
CODE_EXT = (".py", ".ts", ".tsx", ".js", ".mjs", ".sh", ".swift", ".bash")


def git(repo, *a, check=False):
    p = subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, errors="replace")
    return p.stdout


def fix_commits(repo):
    out = git(repo, "log", "--no-merges", "--format=%H%x09%ct%x09%s")
    for ln in out.splitlines():
        h, ts, subj = ln.split("\t", 2)
        if FIXRE.search(subj):
            yield h, int(ts), subj


def removed_ranges(repo, fix):
    """{file: [(old_start, old_len)]} for the fix commit, code files only."""
    out = git(repo, "show", "--format=", "-U0", "--no-renames", fix)
    cur, res = None, defaultdict(list)
    for ln in out.splitlines():
        if ln.startswith("+++ "):
            cur = ln[6:] if ln.startswith("+++ b/") else None
        elif ln.startswith("@@") and cur and cur.endswith(CODE_EXT):
            m = HUNK.match(ln)
            if m and int(m.group(2) if m.group(2) is not None else 1) > 0:
                res[cur].append((int(m.group(1)), int(m.group(2) if m.group(2) is not None else 1)))
    return res


def blame_origin(repo, parent, path, start, n):
    out = git(repo, "blame", "--porcelain", "-L", f"{start},{start + n - 1}", parent, "--", path)
    origins = defaultdict(list)
    for ln in out.splitlines():
        m = re.match(r"^([0-9a-f]{40}) (\d+) (\d+)", ln)
        if m:
            origins[m.group(1)].append(int(m.group(2)))
    return origins


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo")
    ap.add_argument("--max-diff", type=int, default=400)
    ap.add_argument("--limit", type=int, default=400)
    a = ap.parse_args()
    n = 0
    for fix, fts, subj in fix_commits(a.repo):
        parent = git(a.repo, "rev-parse", fix + "^").strip()
        if not parent:
            continue
        for path, rngs in removed_ranges(a.repo, fix).items():
            for s, ln in rngs:
                if ln > 40:
                    continue
                for intro, lines in blame_origin(a.repo, parent, path, s, ln).items():
                    if intro == fix:
                        continue
                    pr = git(a.repo, "rev-list", "--parents", "-n1", intro).split()
                    if len(pr) != 2:      # root or merge commit
                        continue
                    size = sum(1 for x in git(a.repo, "show", "--format=", "-U0", intro).splitlines()
                               if x[:1] in "+-" and x[:3] not in ("+++", "---"))
                    if size == 0 or size > a.max_diff:
                        continue
                    isubj = git(a.repo, "log", "-1", "--format=%s", intro).strip()
                    print(json.dumps({"repo": a.repo, "intro": intro, "fix": fix, "file": path,
                                      "intro_lines": [min(lines), max(lines)], "intro_subject": isubj,
                                      "fix_subject": subj, "diff_lines": size}))
                    n += 1
                    if n >= a.limit:
                        return
if __name__ == "__main__":
    main()
