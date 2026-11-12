#!/usr/bin/env python3
"""loc_score.py -- score localizer arms against the files/functions changed by PASSING fixes, plus the gate test.

Runs on the Mac but only PARSES: unified diffs by regex, base-repo files with ast.parse. Never imports or executes
anything from a task repo. Reads only $R/bench/<id>/repo/ (never hidden/, meta*.json) and the first three fields of
$R/grade*/results.txt (arm task PASS:|FAIL:).

  loc_score.py [--arms a,b] [--out DIR] [--truth FILE] [--json] [--stub ARM]
  loc_score.py --selftest         stub arm ranking the truth files first must give hit@1 = 100%

Per-arm output files: ~/offload-research/localize/out/<arm>/<task>.json (see loc_submit.py for the schema).
"""
import argparse
import ast
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

R = Path("/private/tmp/claude-501/-Users-user-Desktop-GitHub-Projects-claude-offload-nosync/"
         "bc93b00c-1a09-40bf-8add-2df5a1fcac65/scratchpad/real")
LOC = Path.home() / "offload-research" / "localize"
DOC_SUFFIX = {".md", ".rst", ".txt", ".toml", ".cfg", ".ini", ".yml", ".yaml", ".json"}
KS = (1, 3, 5, 10)


# ---- same rules as b2check.py ----
def is_test(f):
    p = Path(f)
    return (p.name.startswith("test_") or p.name.endswith("_test.py") or p.name == "conftest.py"
            or any(part in ("tests", "test", "testing") for part in p.parts[:-1]))


def is_doc(f):
    p = Path(f)
    return p.suffix in DOC_SUFFIX or p.parts[0] in ("docs", "doc", "changelog", "changes", "news") or \
        p.name.upper().startswith(("CHANGELOG", "CHANGES", "HISTORY", "NEWS", "README"))


def is_source(f):
    return not is_test(f) and not is_doc(f)


# ---- patch parsing ----
HUNK = re.compile(r"^@@ -(\d+)(?:,(\d+))? \+(\d+)(?:,(\d+))? @@")


def parse_patch(text):
    """-> {path: {"old": set(old line numbers removed), "ins": [old line number AFTER which text was inserted],
    "new_file": bool}}  (path = old-side path, falling back to new-side)."""
    files, cur = {}, None
    old_path = None
    lines = text.splitlines()
    i = 0
    while i < len(lines):
        ln = lines[i]
        if ln.startswith("diff --git "):
            cur = None
            old_path = None
        elif ln.startswith("--- "):
            p = ln[4:].split("\t")[0].strip()
            old_path = None if p == "/dev/null" else re.sub(r"^a/", "", p)
            new_file = old_path is None
            nxt = lines[i + 1] if i + 1 < len(lines) else ""
            if nxt.startswith("+++ "):
                q = nxt[4:].split("\t")[0].strip()
                newp = None if q == "/dev/null" else re.sub(r"^b/", "", q)
                path = old_path or newp
                if path:
                    cur = files.setdefault(path, {"old": set(), "ins": [], "new_file": new_file})
                i += 1
        elif cur is not None and (m := HUNK.match(ln)):
            o = int(m.group(1))
            i += 1
            while i < len(lines) and not lines[i].startswith(("@@", "diff --git ")):
                t = lines[i]
                if t.startswith("-") and not t.startswith("--- "):
                    cur["old"].add(o)
                    o += 1
                elif t.startswith("+") and not t.startswith("+++ "):
                    cur["ins"].append(o)  # inserted before old line o
                elif t.startswith("\\"):
                    pass
                else:
                    o += 1
                i += 1
            continue
        i += 1
    return files


# ---- enclosing defs via ast ----
def defs_of(src):
    """-> list of (qualname, start, end) for every def/class, outermost first."""
    try:
        tree = ast.parse(src)
    except (SyntaxError, ValueError):
        return []
    out = []

    def walk(node, prefix):
        for ch in ast.iter_child_nodes(node):
            if isinstance(ch, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                q = f"{prefix}.{ch.name}" if prefix else ch.name
                start = min([ch.lineno] + [d.lineno for d in ch.decorator_list])
                out.append((q, start, ch.end_lineno))
                walk(ch, q)
            else:
                walk(ch, prefix)
    walk(tree, "")
    return out


def innermost(defs, *lines):
    best = None
    for q, s, e in defs:
        if all(s <= l <= e for l in lines) and (best is None or (e - s) < (best[2] - best[1])):
            best = (q, s, e)
    return best


def patch_truth(task, patch_text):
    """-> (files:set, funcs:set of (path, qualname, start, end), missing:list)"""
    files, funcs, missing = set(), set(), []
    for path, info in parse_patch(patch_text).items():
        if not is_source(path):
            continue
        base = R / "bench" / task / "repo" / path
        if not base.is_file():
            missing.append(path)  # new file: nothing to retrieve at baseline
            continue
        files.add(path)
        if path.endswith(".py"):
            defs = defs_of(base.read_text("utf-8", "replace"))
            for l in info["old"]:
                d = innermost(defs, l)
                if d:
                    funcs.add((path, *d))
            for l in info["ins"]:
                d = innermost(defs, l - 1, l) or innermost(defs, l)
                if d:
                    funcs.add((path, *d))
    return files, funcs, missing


def load_truth(truth_file):
    per = defaultdict(list)
    for ln in Path(truth_file).read_text().splitlines():
        if ln.strip():
            t, p = ln.split(None, 1)
            per[t].append(p.strip())
    truth = {}
    for t, paths in per.items():
        fs, fns, miss = [], [], []
        for p in paths:
            f, fn, m = patch_truth(t, Path(p).read_text("utf-8", "replace"))
            fs.append(f)
            fns.append(fn)
            miss += m
        truth[t] = {
            "files_len": set().union(*fs), "files_strict": set.intersection(*fs) if fs else set(),
            "funcs_len": set().union(*fns), "funcs_strict": set.intersection(*fns) if fns else set(),
            "npatches": len(paths), "new_files": sorted(set(miss)),
        }
    return truth


# ---- arm outputs ----
def load_arm(out, arm):
    res = {}
    for f in sorted((Path(out) / arm).glob("*.json")):
        try:
            d = json.loads(f.read_text())
            res[f.stem] = d
        except Exception:  # noqa: BLE001
            pass
    return res


def ranked_files(d):
    seen, out = set(), []
    for p in (d or {}).get("files", []):
        if p not in seen:
            seen.add(p)
            out.append(p)
    return out


def func_hit(funcs, truth_funcs, k):
    """any of the arm's top-k functions matches a truth function: same path and (same short name, or the arm's span
    lies inside the truth def's span)."""
    for fn in funcs[:k]:
        for (p, q, s, e) in truth_funcs:
            if fn.get("path") != p:
                continue
            short = fn.get("symbol", "").split(".")[-1]
            if short and short == q.split(".")[-1]:
                return True
            a, b = fn.get("start"), fn.get("end")
            if isinstance(a, int) and isinstance(b, int) and s <= a and b <= e:
                return True
    return False


def score_arm(res, truth, kind):
    """kind = 'len' or 'strict'. -> metrics dict over tasks that have truth AND an arm output."""
    tasks = [t for t in truth if truth[t][f"files_{kind}"] and t in res]
    n = len(tasks)
    m = {"n": n, "tasks": tasks}
    for k in KS:
        m[f"hit@{k}"] = sum(any(f in truth[t][f"files_{kind}"] for f in ranked_files(res[t])[:k]) for t in tasks)
    rec = []
    for t in tasks:
        tf = truth[t][f"files_{kind}"]
        rec.append(len(tf & set(ranked_files(res[t])[:5])) / len(tf))
    m["recall@5"] = sum(rec) / n if n else 0.0
    rr = []
    for t in tasks:
        tf = truth[t][f"files_{kind}"]
        r = next((i + 1 for i, f in enumerate(ranked_files(res[t])) if f in tf), None)
        rr.append(1 / r if r else 0.0)
    m["mrr"] = sum(rr) / n if n else 0.0
    ft = [t for t in tasks if truth[t][f"funcs_{kind}"] and res[t].get("functions")]
    m["fn_n"] = len(ft)
    for k in (5, 10):
        m[f"fn_hit@{k}"] = sum(func_hit(res[t]["functions"], truth[t][f"funcs_{kind}"], k) for t in ft)
    return m


# ---- gate test ----
def load_graded():
    """-> list of (gradedir, arm, task, verdict, patch path)"""
    out = []
    for rf in sorted(R.glob("grade*/results.txt")):
        for ln in rf.read_text("utf-8", "replace").splitlines():
            f = ln.split()[:3]  # only the first three fields
            if len(f) == 3 and f[2] in ("PASS:", "FAIL:"):
                arm, task, v = f
                pp = rf.parent / arm / f"{task}.patch"
                out.append((rf.parent.name, arm, task, v[:-1], pp))
    return out


def gate(res, graded):
    """flag@k: top-k files include a SOURCE file the patch did not touch. -> {k: {PASS:(flag,n), FAIL:(flag,n)}},
    plus per-patch rows."""
    stats = {k: {"PASS": [0, 0], "FAIL": [0, 0]} for k in (1, 2, 3)}
    rows = []
    for gdir, arm, task, verdict, pp in graded:
        if task not in res or not pp.is_file():
            continue
        touched = set(parse_patch(pp.read_text("utf-8", "replace")))
        top = [f for f in ranked_files(res[task])]
        row = {"gdir": gdir, "arm": arm, "task": task, "verdict": verdict, "flags": {}}
        for k in (1, 2, 3):
            extra = [f for f in top[:k] if is_source(f) and f not in touched]
            row["flags"][k] = extra
            stats[k][verdict][1] += 1
            stats[k][verdict][0] += bool(extra)
        rows.append(row)
    return stats, rows


def fmt_pct(a, b):
    return f"{a}/{b} = {100 * a / b:.0f}%" if b else "n/a"


def report(arms_res, truth, graded):
    lines = []
    for kind in ("len", "strict"):
        lines.append(f"\n### File/function metrics, {'lenient (union of passing patches)' if kind == 'len' else 'strict (files changed by every passing patch)'}\n")
        lines.append("| arm | n | hit@1 | hit@3 | hit@5 | hit@10 | recall@5 | MRR | fn n | fn hit@5 | fn hit@10 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|---|")
        for arm, res in arms_res.items():
            m = score_arm(res, truth, kind)
            n = m["n"]
            lines.append(f"| {arm} | {n} | " + " | ".join(fmt_pct(m[f"hit@{k}"], n) for k in KS)
                         + f" | {m['recall@5']:.2f} | {m['mrr']:.2f} | {m['fn_n']} | "
                         + " | ".join(fmt_pct(m[f"fn_hit@{k}"], m["fn_n"]) for k in (5, 10)) + " |")
    lines.append("\n### Gate test (flag@k = top-k files include a source file the patch did not touch)\n")
    lines.append("| arm | k | catch rate on FAIL patches | false-alarm rate on PASS patches |")
    lines.append("|---|---|---|---|")
    allrows = {}
    for arm, res in arms_res.items():
        stats, rows = gate(res, graded)
        allrows[arm] = rows
        for k in (1, 2, 3):
            lines.append(f"| {arm} | {k} | {fmt_pct(*stats[k]['FAIL'])} | {fmt_pct(*stats[k]['PASS'])} |")
    return "\n".join(lines), allrows


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--arms", default="")
    ap.add_argument("--out", default=str(LOC / "out"))
    ap.add_argument("--truth", default=str(LOC / "truth-sources.txt"))
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    truth = load_truth(a.truth)
    graded = load_graded()
    if a.selftest:
        stub = {t: {"files": sorted(v["files_len"]) + ["zzz_other.py"], "functions": [
            {"path": p, "symbol": q, "start": s, "end": e} for (p, q, s, e) in sorted(v["funcs_len"])]}
            for t, v in truth.items() if v["files_len"]}
        m = score_arm(stub, truth, "len")
        print(f"selftest: tasks with truth={m['n']} hit@1={m['hit@1']}/{m['n']} hit@10={m['hit@10']}/{m['n']} "
              f"fn_hit@5={m['fn_hit@5']}/{m['fn_n']}")
        bad = stub and {t: {"files": ["zzz_other.py"] + sorted(v["files_len"])} for t, v in truth.items()}
        m2 = score_arm(bad, truth, "len")
        print(f"selftest (truth ranked 2nd): hit@1={m2['hit@1']}/{m2['n']} hit@3={m2['hit@3']}/{m2['n']}")
        ok = m["hit@1"] == m["n"] and m2["hit@1"] == 0 and m2["hit@3"] == m2["n"]
        print("OK" if ok else "FAILED")
        sys.exit(0 if ok else 1)
    arms = [x for x in a.arms.split(",") if x] or sorted(p.name for p in Path(a.out).iterdir() if p.is_dir())
    arms_res = {arm: load_arm(a.out, arm) for arm in arms}
    text, rows = report(arms_res, truth, graded)
    print(text)


if __name__ == "__main__":
    main()
