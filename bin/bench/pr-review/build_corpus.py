#!/usr/bin/env python3
"""build_corpus.py -- turn a curation file into the PRIVATE pr-review corpus. Real git history only.

  build_corpus.py CURATION.json --out ~/bench/pr-review-corpus [--seed N]

CURATION.json (private, never committed):
  {"regressions": [{"repo","intro","fix","class","ranges":{"file":[[a,b],...]}, "summary"}],
   "clean": [{"repo","commit","tag"}]}

Every item is the full diff of a commit that really landed (`git show`). A regression's key comes from the FIX: the
intro-side lines it removed or changed (comment, blank and docstring lines are dropped from the key). Nothing is
generated, mutated or invented. Output (mode 700):
  items/<id>/diff.patch    what a model sees, with a neutral shuffled id
  items.json               {id: {"intent", "n"}}            -- safe to hand to the runner
  keys.json                {id: {"type","class","lang","size","tags","keys","summary","source"}} -- NEVER given to a model
"""
import argparse, json, os, random, re, subprocess, sys
from pathlib import Path

LANG = {".py": "python", ".ts": "ts-js", ".tsx": "ts-js", ".js": "ts-js", ".mjs": "ts-js", ".jsx": "ts-js",
        ".sh": "bash", ".bash": "bash", ".swift": "swift", ".md": "docs", ".json": "config", ".yml": "config",
        ".yaml": "config", ".toml": "config"}
COMMENT = re.compile(r"^\s*(#|//|\*|/\*|\*/|\"\"\"|''')")


def git(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, errors="replace").stdout


def size_tier(n):
    return "tiny" if n < 30 else "medium" if n < 250 else "large"


def code_runs(lines, a, b):
    """Contiguous runs inside [a,b] (1-based) that are real code: not blank, not comment, not docstring fence."""
    runs, cur = [], None
    for n in range(a, min(b, len(lines)) + 1):
        t = lines[n - 1]
        if not t.strip() or COMMENT.match(t):
            cur = None
            continue
        if cur is None:
            cur = [n, n]
            runs.append(cur)
        else:
            cur[1] = n
    return runs


def patch_lang(diff):
    files = re.findall(r"^diff --git a/(\S+)", diff, re.M)
    langs = {LANG.get(os.path.splitext(f)[1], "other") for f in files}
    langs.discard("other")
    return sorted(langs)[0] if len(langs) == 1 else ("mixed" if langs else "other"), files


def make_item(repo, sha):
    diff = git(repo, "show", "--format=", "--no-renames", "-p", sha)
    subj = git(repo, "show", "-s", "--format=%s", sha).strip()
    return diff, subj


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("curation")
    ap.add_argument("--out", default=os.path.expanduser("~/bench/pr-review-corpus"))
    ap.add_argument("--seed", type=int, default=1119)
    a = ap.parse_args(argv)
    cur = json.loads(Path(a.curation).read_text())
    out = Path(a.out).expanduser()
    (out / "items").mkdir(parents=True, exist_ok=True)
    os.chmod(out, 0o700)
    rng = random.Random(a.seed)
    entries = []
    merged = {}
    for r in cur.get("regressions", []):   # one diff, two later fixes -> ONE item holding both keys
        m = merged.setdefault((r["repo"], r["intro"]), dict(r, ranges={}, summary=[], fixes=[]))
        for f, rs in r["ranges"].items():
            m["ranges"].setdefault(f, []).extend(rs)
        m["summary"].append(r["summary"]); m["fixes"].append(r["fix"][:10])
    for r in merged.values():
        r["summary"] = " | ".join(r["summary"])
        diff, subj = make_item(r["repo"], r["intro"])
        keys = []
        for f, rs in r["ranges"].items():
            src = git(r["repo"], "show", f'{r["intro"]}:{f}').splitlines()
            for lo, hi in rs:
                for x, y in (code_runs(src, lo, hi) or []):
                    keys.append({"file": f, "lines": [x, y]})
        if not keys:
            print("skip (no code lines in key):", r["intro"][:8], file=sys.stderr)
            continue
        lang, files = patch_lang(diff)
        entries.append((diff, subj, {"type": "regression", "class": r["class"], "lang": lang, "keys": keys,
                                     "summary": r["summary"], "tags": r.get("tags", []),
                                     "source": {"intro": r["intro"][:10], "fix": ",".join(r["fixes"])}}))
    for c in cur.get("clean", []):
        diff, subj = make_item(c["repo"], c["commit"])
        lang, files = patch_lang(diff)
        entries.append((diff, subj, {"type": "clean", "class": c["tag"], "lang": lang, "keys": [], "summary": "",
                                     "tags": [c["tag"]], "source": {"commit": c["commit"][:10]}}))
    rng.shuffle(entries)
    items, keys = {}, {}
    used = set()
    for diff, subj, k in entries:
        while True:
            iid = "i" + "%06x" % rng.randrange(1 << 24)
            if iid not in used:
                used.add(iid)
                break
        n = sum(1 for l in diff.splitlines() if l[:1] in "+-" and l[:3] not in ("+++", "---"))
        k["size"] = size_tier(n)
        k["changed_lines"] = n
        d = out / "items" / iid
        d.mkdir(parents=True, exist_ok=True)
        (d / "diff.patch").write_text(diff)
        items[iid] = {"intent": subj, "chars": len(diff)}
        keys[iid] = k
    (out / "items.json").write_text(json.dumps(items, indent=1, sort_keys=True))
    (out / "keys.json").write_text(json.dumps(keys, indent=1, sort_keys=True))
    os.chmod(out / "keys.json", 0o600)
    print(f"{len(items)} items: " + ", ".join(f"{t}={sum(1 for v in keys.values() if v['type']==t)}" for t in ("regression", "clean")))


if __name__ == "__main__":
    main()
