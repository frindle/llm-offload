#!/usr/bin/env python3
"""pr_review_integrity.py -- does the PR-review corpus leak its own answer key? (bench-integrity.py equivalent for it)

  pr_review_integrity.py [--corpus DIR] [--jobs jobs.json ...]
Exit 0 = clean, 1 = problems. Checks, all on what a model could see:
  ids neutral (i + 6 hex) and not derived from content; items.json holds no key fields; the corpus dir is mode 700, keys.json
  is not world-readable and the corpus lives outside every git repo; no intent (PR title) or job label contains an
  answer-key phrase (the same list bench-integrity.py uses) or the fix commit's subject; the key's file/class never appears
  in a label. The diffs themselves are real history and are not rewritten.
"""
import argparse, importlib.util, json, os, re, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve()
_spec = None
for cand in (HERE.parents[2] / "bench-integrity.py", HERE.parents[1] / "bench-integrity.py"):
    if cand.exists():
        _spec = importlib.util.spec_from_file_location("bench_integrity", cand)
        break
LEAK = None
if _spec:
    m = importlib.util.module_from_spec(_spec)
    _spec.loader.exec_module(m)
    LEAK = m._LEAK
if LEAK is None:
    LEAK = re.compile(r"\b(seeded bug|planted (bug|defect)|answer key|ground.?truth|this is the bug|bug is here)\b", re.I)
KEYFIELDS = {"type", "class", "keys", "summary", "tags", "source", "lines", "file"}


def check(corpus, jobs_files=()):
    problems, notes = [], []
    items = json.loads((corpus / "items.json").read_text())
    keys = json.loads((corpus / "keys.json").read_text())
    if set(items) != set(keys):
        problems.append("items.json and keys.json disagree on ids")
    for iid, meta in items.items():
        if not re.fullmatch(r"i[0-9a-f]{6}", iid):
            problems.append(f"{iid}: id is not neutral")
        if KEYFIELDS & set(meta):
            problems.append(f"{iid}: items.json carries key fields {sorted(KEYFIELDS & set(meta))}")
        if LEAK.search(meta.get("intent", "")):
            problems.append(f"{iid}: intent contains an answer-key phrase")
        k = keys.get(iid, {})
        fx = (k.get("summary") or "").strip().lower()
        if fx and len(fx) > 12 and fx in meta.get("intent", "").lower():
            problems.append(f"{iid}: intent repeats the fix subject")
        if not (corpus / "items" / iid / "diff.patch").exists():
            problems.append(f"{iid}: diff.patch missing")
        extra = [p.name for p in (corpus / "items" / iid).iterdir() if p.name != "diff.patch"]
        if extra:
            problems.append(f"{iid}: extra files visible in the item dir: {extra}")
    mode = (os.stat(corpus).st_mode & 0o777)
    if mode & 0o077:
        problems.append(f"corpus dir mode is {oct(mode)}, want 700")
    if (os.stat(corpus / "keys.json").st_mode & 0o007):
        problems.append("keys.json is world-readable")
    r = subprocess.run(["git", "-C", str(corpus), "rev-parse", "--is-inside-work-tree"], capture_output=True, text=True)
    if r.stdout.strip() == "true":
        problems.append("corpus sits inside a git work tree")
    for jf in jobs_files:
        for j in json.loads(Path(jf).read_text()):
            lab = j.get("label", "")
            if LEAK.search(lab):
                problems.append(f"label {lab}: answer-key phrase")
            kk = keys.get(j.get("item"), {})
            for bad in (kk.get("class") or "", kk.get("type") or ""):
                if bad and bad in lab.replace("bakeoff-prr", ""):
                    problems.append(f"label {lab}: carries the item's class/type")
    notes.append(f"{len(items)} items, {sum(1 for v in keys.values() if v['type']=='regression')} regressions, "
                 f"{sum(1 for v in keys.values() if v['type']=='clean')} clean; corpus mode {oct(mode)}")
    return problems, notes


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", default=os.environ.get("PRR_CORPUS", "~/bench/pr-review-corpus"))
    ap.add_argument("--jobs", action="append", default=[])
    a = ap.parse_args(argv)
    problems, notes = check(Path(a.corpus).expanduser(), a.jobs)
    for n in notes:
        print("note:", n)
    for p in problems:
        print("PROBLEM:", p)
    print("integrity:", "CLEAN" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
