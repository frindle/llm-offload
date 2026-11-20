#!/usr/bin/env python3
"""pr_review_score.py -- score a PR-review backtest from its artifacts. Deterministic: file + line window.

  pr_review_score.py --run NAME [--run NAME2 ...] [--corpus DIR] [--window 5] [--json OUT] [--handcheck N]
  pr_review_score.py --self-check

A finding "hits" a regression when its quoted line resolves (inside the item's diff) to a line of the key's file within
key_start-W .. key_end+W. Findings the harness marked REAL_DEFECT or UNSURE count as flagged (both are shown to a human
in the report); NOT_A_DEFECT never counts. Only real, merged-then-fixed regressions and real clean changes are scored.
Injection obedience is not scored: no real case exists in history.
"""
import argparse, json, os, re, statistics, sys, tempfile
from collections import defaultdict
from pathlib import Path

HUNK = re.compile(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@")
FLAG = ("REAL_DEFECT", "UNSURE")


def diff_lines(diff):
    """{path: [(new_no, kind, text)]}; a removed line carries the new-side number of the line after it."""
    out, path, o, n = defaultdict(list), None, 0, 0
    for l in diff.splitlines():
        if l.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*) b/(.*)$", l)
            path = m.group(2) if m else None
        elif l.startswith("+++ b/"):
            path = l[6:]
        elif l.startswith("--- ") or l.startswith("+++ "):
            continue
        elif (m := HUNK.match(l)):
            o, n = int(m.group(1)), int(m.group(2))
        elif path and l[:1] == "+":
            out[path].append((n, "+", l[1:].strip())); n += 1
        elif path and l[:1] == "-":
            out[path].append((n, "-", l[1:].strip())); o += 1
        elif path and l[:1] == " ":
            out[path].append((n, " ", l[1:].strip())); n += 1; o += 1
    return out


def locate(finding, dl):
    """(path, line) for a finding, or (path, None) when its quote cannot be placed."""
    path = finding.get("path") or ""
    rows = dl.get(path)
    if rows is None:
        cand = [p for p in dl if p.endswith(path) or path.endswith(p)] if path else []
        rows, path = (dl[cand[0]], cand[0]) if cand else (None, path)
    q = next((x.strip() for x in (finding.get("quote") or "").splitlines() if x.strip()), "")
    q = q[1:].strip() if q[:1] in "+-" and q[1:2] == " " else q
    if rows is None or not q:
        return path, None
    for kinds in ("+", "-", " "):
        for n, k, t in rows:
            if k == kinds and t == q:
                return path, n
    return path, None


def hit(path, line, keys, w, file_only=False):
    for k in keys:
        if k["file"] == path or k["file"].endswith(path) or path.endswith(k["file"]):
            if file_only:
                return True
            if line is not None and k["lines"][0] - w <= line <= k["lines"][1] + w:
                return True
    return False


def score_job(run_json, diff, keys, w, flag=FLAG):
    dl = diff_lines(diff)
    fs = [f for f in (run_json.get("findings") or []) if f.get("verdict") in flag]
    placed = [(f,) + locate(f, dl) for f in fs]
    hits = [1 for f, pa, ln in placed if hit(pa, ln, keys, w)]
    fonly = any(hit(pa, ln, keys, w, True) for f, pa, ln in placed)
    return {"n_flag": len(fs), "n_hit": len(hits), "hit": bool(hits), "file_hit": fonly,
            "unplaced": sum(1 for f, pa, ln in placed if ln is None)}


def load(run_dirs, corpus):
    items = json.loads((corpus / "items.json").read_text())
    keys = json.loads((corpus / "keys.json").read_text())
    rows = []
    for rd in run_dirs:
        for j in json.loads((rd / "jobs.json").read_text()):
            res = rd / "results" / str(j.get("job_id"))
            res = res / "work" if (res / "work").is_dir() else res      # the queue puts the runner's output under work/
            rj = res / "run.json"
            row = {"item": j["item"], "model": j["model"], "repeat": j["repeat"], "job": j.get("job_id"), "state": "missing"}
            if rj.exists():
                try:
                    row["run"] = json.loads(rj.read_text()); row["state"] = "ok"
                except ValueError:
                    row["state"] = "parse_fail"
            elif (res / "report.md").exists():
                row["run"] = {"findings": [], "elapsed_s": 0}; row["state"] = "ok"   # all-generated PASS report
            elif (res / "skipped.json").exists():
                row["state"] = "skipped"
            elif (res.parent / "done.json").exists():
                row["state"] = "failed"          # finished but produced no report: counted as a parse/crash failure
            rows.append(row)
    return items, keys, rows


def pct(a, b):
    return round(100.0 * a / b, 1) if b else None


def p95(xs):
    xs = sorted(xs)
    return xs[min(len(xs) - 1, int(round(0.95 * (len(xs) - 1))))] if xs else None


def aggregate(rows, keys, corpus, w=5, flag=FLAG):
    per = defaultdict(list)
    for r in rows:
        k = keys[r["item"]]
        if r["state"] == "ok":
            diff = (corpus / "items" / r["item"] / "diff.patch").read_text()
            r["score"] = score_job(r["run"], diff, k["keys"], w, flag)
            st = r["run"].get("stats") or {}
            r["pf"] = bool(st.get("review_parse_fail"))
        per[r["model"]].append(r)
    res = {}
    for m, rs in per.items():
        ok = [r for r in rs if r["state"] == "ok"]
        reg = [r for r in ok if keys[r["item"]]["type"] == "regression"]
        cln = [r for r in ok if keys[r["item"]]["type"] == "clean"]
        d = {"jobs": len(rs), "ok": len(ok), "missing": sum(1 for r in rs if r["state"] != "ok"),
             "recall": pct(sum(r["score"]["hit"] for r in reg), len(reg)),
             "recall_file_only": pct(sum(r["score"]["file_hit"] for r in reg), len(reg)),
             "false_alarm_per_clean": round(sum(r["score"]["n_flag"] for r in cln) / len(cln), 3) if cln else None,
             "clean_flagged_pct": pct(sum(1 for r in cln if r["score"]["n_flag"]), len(cln)),
             "precision": pct(sum(r["score"]["n_hit"] for r in ok), sum(r["score"]["n_flag"] for r in ok)),
             "parse_fail_pct": pct(sum(1 for r in ok if r["pf"]) + sum(1 for r in rs if r["state"] in ("parse_fail", "failed")), len(rs))}
        for dim in ("class", "lang", "size"):
            g = defaultdict(list)
            for r in reg:
                g[keys[r["item"]][dim]].append(r["score"]["hit"])
            d["recall_by_" + dim] = {x: [pct(sum(v), len(v)), len(v)] for x, v in sorted(g.items())}
        byi = defaultdict(list)
        for r in ok:
            byi[r["item"]].append(r)
        agree, nn = 0, 0
        for it, v in byi.items():
            if len(v) < 2:
                continue
            ys = [(r["score"]["hit"] if keys[it]["type"] == "regression" else r["score"]["n_flag"] > 0) for r in v]
            agree += max(sum(ys), len(ys) - sum(ys)); nn += len(ys)
        d["consistency"] = pct(agree, nn)
        t = [r["run"].get("elapsed_s", 0) for r in ok if r["run"].get("elapsed_s")]
        d["median_s"] = round(statistics.median(t), 1) if t else None
        d["p95_s"] = round(p95(t), 1) if t else None
        d["gpu_min"] = round(sum(t) / 60, 1)
        res[m] = d
    return res, per


def item_outcome(rs, keys):
    """Per item majority outcome across repeats: regression -> hit, clean -> flagged."""
    byi = defaultdict(list)
    for r in rs:
        if r["state"] == "ok":
            k = keys[r["item"]]
            byi[r["item"]].append(r["score"]["hit"] if k["type"] == "regression" else r["score"]["n_flag"] > 0)
    return {i: sum(v) * 2 > len(v) for i, v in byi.items()}


def ensemble(per, keys):
    out = {}
    ms = sorted(per)
    oc = {m: item_outcome(per[m], keys) for m in ms}
    for i, a in enumerate(ms):
        for b in ms[i + 1:]:
            its = set(oc[a]) & set(oc[b])
            reg = [x for x in its if keys[x]["type"] == "regression"]
            cl = [x for x in its if keys[x]["type"] == "clean"]
            out[f"{a} + {b}"] = {"recall": pct(sum(oc[a][x] or oc[b][x] for x in reg), len(reg)),
                                 "clean_flagged_pct": pct(sum(oc[a][x] or oc[b][x] for x in cl), len(cl)),
                                 "recall_both_agree": pct(sum(oc[a][x] and oc[b][x] for x in reg), len(reg)),
                                 "clean_flagged_both_pct": pct(sum(oc[a][x] and oc[b][x] for x in cl), len(cl))}
    return out


def bar(d):
    ok = (d["recall"] or 0) >= 70 and (d["false_alarm_per_clean"] if d["false_alarm_per_clean"] is not None else 9) <= 0.3 \
        and (d["consistency"] or 0) >= 80
    return "PASS" if ok else "FAIL"


def self_check():
    """Build a tiny synthetic corpus + results in a temp dir and assert the exact numbers."""
    diff = ("diff --git a/a.py b/a.py\n--- a/a.py\n+++ b/a.py\n@@ -10,3 +10,4 @@ def f():\n x = 1\n-y = 2\n+y = 3\n+z = 4\n w = 5\n")
    dl = diff_lines(diff)
    assert locate({"path": "a.py", "quote": "z = 4"}, dl) == ("a.py", 12), locate({"path": "a.py", "quote": "z = 4"}, dl)
    assert locate({"path": "a.py", "quote": "y = 2"}, dl) == ("a.py", 11)       # removed line -> next new line
    assert locate({"path": "a.py", "quote": "nope"}, dl)[1] is None
    keys = [{"file": "a.py", "lines": [11, 11]}]
    run = {"findings": [{"path": "a.py", "quote": "z = 4", "verdict": "REAL_DEFECT"},
                        {"path": "a.py", "quote": "y = 3", "verdict": "NOT_A_DEFECT"}], "elapsed_s": 10}
    s = score_job(run, diff, keys, 5)
    assert s["hit"] and s["n_flag"] == 1 and s["n_hit"] == 1
    far = [{"file": "a.py", "lines": [40, 41]}]
    assert not score_job(run, diff, far, 5)["hit"] and score_job(run, diff, far, 5)["file_hit"]
    assert not score_job({"findings": [{"path": "a.py", "quote": "z = 4", "verdict": "NOT_A_DEFECT"}]}, diff, keys, 5)["hit"]
    with tempfile.TemporaryDirectory() as td:
        c, rd = Path(td) / "c", Path(td) / "run"
        for iid in ("r1", "r2", "c1"):
            (c / "items" / iid).mkdir(parents=True)
            (c / "items" / iid / "diff.patch").write_text(diff)
        (c / "items.json").write_text(json.dumps({i: {"intent": "t"} for i in ("r1", "r2", "c1")}))
        K = {"r1": {"type": "regression", "class": "x", "lang": "python", "size": "tiny", "keys": keys},
             "r2": {"type": "regression", "class": "x", "lang": "python", "size": "tiny", "keys": far},
             "c1": {"type": "clean", "class": "lock", "lang": "python", "size": "tiny", "keys": []}}
        (c / "keys.json").write_text(json.dumps(K))
        jobs, n = [], 0
        for it in K:
            for rep in (1, 2, 3):
                n += 1
                jid = "j%d" % n
                jobs.append({"item": it, "model": "M", "repeat": rep, "job_id": jid})
                (rd / "results" / jid).mkdir(parents=True)
                fl = run["findings"] if (it != "r2" and not (it == "r1" and rep == 3)) else []
                if it == "c1":
                    fl = [{"path": "a.py", "quote": "z = 4", "verdict": "UNSURE"}] if rep == 1 else []
                (rd / "results" / jid / "run.json").write_text(json.dumps({"findings": fl, "elapsed_s": 10 * rep, "stats": {}}))
        (rd / "jobs.json").write_text(json.dumps(jobs))
        items, keys_, rows = load([rd], c)
        res, per = aggregate(rows, keys_, c)
        m = res["M"]
        assert m["recall"] == pct(2, 6), m                  # r1 hits twice of 3, r2 never
        assert m["false_alarm_per_clean"] == round(1 / 3, 3), m
        assert m["clean_flagged_pct"] == pct(1, 3), m
        assert m["precision"] == pct(2, 3), m          # 3 flagged findings (2 on r1, 1 on c1), 2 hit
        assert m["consistency"] == pct(2 + 3 + 2, 9), m     # r1 2/3, r2 3/3, c1 2/3
        assert m["median_s"] == 20.0 and m["gpu_min"] == round(180 / 60, 1), m
        assert m["recall_by_class"]["x"] == [pct(2, 6), 6]
        assert bar(m) == "FAIL"
    print("self-check OK")
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--run", action="append")
    ap.add_argument("--corpus", default=os.environ.get("PRR_CORPUS", "~/bench/pr-review-corpus"))
    ap.add_argument("--window", type=int, default=5)
    ap.add_argument("--json")
    ap.add_argument("--handcheck", type=int, default=0, help="print N random hit/miss rows for a human reading")
    ap.add_argument("--self-check", action="store_true")
    a = ap.parse_args(argv)
    if a.self_check:
        return self_check()
    corpus = Path(a.corpus).expanduser()
    items, keys, rows = load([corpus / "runs" / r for r in a.run], corpus)
    res, per = aggregate(rows, keys, corpus, a.window)
    ens = ensemble(per, keys)
    for m, d in res.items():
        d["bar"] = bar(d)
    out = {"models": res, "ensembles": ens}
    if a.json:
        Path(a.json).write_text(json.dumps(out, indent=1))
    for m, d in sorted(res.items()):
        print(f"{m:42s} recall {d['recall']}% FA/clean {d['false_alarm_per_clean']} prec {d['precision']}% cons {d['consistency']}% "
              f"pf {d['parse_fail_pct']}% med {d['median_s']}s p95 {d['p95_s']}s gpu-min {d['gpu_min']} ok {d['ok']}/{d['jobs']} {d['bar']}")
    if a.handcheck:
        import random
        rr = random.Random(11)
        pool = [r for rs in per.values() for r in rs if r["state"] == "ok" and keys[r["item"]]["type"] == "regression"]
        for r in rr.sample(pool, min(a.handcheck, len(pool))):
            print("HANDCHECK", r["model"], r["item"], "matcher:", "HIT" if r["score"]["hit"] else "MISS", "job", r["job"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
