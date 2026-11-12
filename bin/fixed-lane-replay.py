#!/usr/bin/env python3
"""fixed-lane-replay -- run the fixed repair lane over a corpus of PAST completed tasks
and compare convergence / wall time / tokens against the RECORDED open-loop outcome.

This is the measurement that gates enabling the lane by default (ship rule in
2026-10-09-fixed-lane-notes.md). It is a single process with JSON output and an exit
code, so it can be enqueued through the queue as an ordinary bundle step; use
--shard I/N to split a corpus across steps and --merge to combine shard reports.

Corpus (JSONL, one task per line):
  {"id": "...",
   "tier": "35b" | "9b",                       # which model tier the recording ran on
   "workspace": "/path/to/pristine/dir",       # copied to a temp dir per run, OR
   "repo": "/path/to/git/repo", "base_commit": "<sha>",   # cloned + checked out per run
   "overlay": {"relative/path": "content"},    # optional harness files written after checkout
   "targets": ["a.py"], "verify": "python3 check.py", "verify_timeout_s": 30,
   "spec": "...", "title": "...",
   "recorded": {"converged": true, "wall_s": 412.0, "tokens": 90311}}   # open-loop result
Never point `workspace`/`repo` at a live dispatch worktree: every run works on a COPY in a
temp dir, and the source is only read.

Arms: `--k 1 --k 4` runs the lane once per K value (fresh copy each time). Report per
arm and per tier: eligible n, converged rate, median wall, median tokens, versus the
recorded open-loop numbers on the SAME eligible subset, plus the ship/kill verdict:
  ship if  K=1 converge >= open loop AND median wall <= open-loop median / 2
        or K=4 converge >= open loop + 5 points
  kill if  K=1 is > 5 points worse AND K=4 does not recover it
  else     'inconclusive'
Usage:
  fixed-lane-replay.py --corpus C.jsonl --k 1 --k 4 [--model M] [--limit N] [--shard 0/2]
                       [--thinking off|on|low] [--out report.json] [--dry-run]
  fixed-lane-replay.py --merge a.json b.json --out merged.json
  fixed-lane-replay.py --selftest
"""
import argparse
import json
import os
import shutil
import statistics
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.realpath(__file__))
sys.path.insert(0, HERE)
import fixed_lane as fl   # noqa: E402


def load_corpus(path):
    out = []
    with open(path) as f:
        for n, line in enumerate(f, 1):
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            try:
                out.append(json.loads(line))
            except ValueError as e:
                raise SystemExit(f"{path}:{n}: bad JSON ({e})")
    return out


def prepare(entry, base_tmp):
    """Fresh private copy of the task's workspace. Returns the dir."""
    dst = tempfile.mkdtemp(prefix="flreplay-", dir=base_tmp)
    work = os.path.join(dst, "w")
    if entry.get("workspace"):
        shutil.copytree(entry["workspace"], work, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", ".git"))
    elif entry.get("repo") and entry.get("base_commit"):
        subprocess.run(["git", "clone", "-q", "--no-hardlinks", entry["repo"], work], check=True)
        subprocess.run(["git", "-C", work, "checkout", "-q", entry["base_commit"]], check=True)
    else:
        raise ValueError(f"{entry.get('id')}: needs `workspace` or `repo`+`base_commit`")
    for rel, content in (entry.get("overlay") or {}).items():
        p = os.path.join(work, rel)
        os.makedirs(os.path.dirname(p) or ".", exist_ok=True)
        with open(p, "w") as f:
            f.write(content)
    return work


def run_one(entry, k, model, thinking, base_tmp, lane_runner=None):
    work = prepare(entry, base_tmp)
    task = {"cwd": work, "targets": entry["targets"], "verify": entry["verify"],
            "verify_timeout_s": entry.get("verify_timeout_s"), "spec": entry.get("spec", ""),
            "title": entry.get("title", ""), "protected": entry.get("protected", [])}
    t0 = time.time()
    try:
        res = (lane_runner or fl.run_lane)(task, model=model, k=k, overrides={"thinking": thinking},
                                          log=lambda m: None)
    finally:
        shutil.rmtree(os.path.dirname(work), ignore_errors=True)
    return {"id": entry.get("id"), "tier": entry.get("tier", "unknown"), "k": k,
            "status": res["status"], "reason": res.get("reason"),
            "converged": res["status"] == fl.ST_PASSED, "wall_s": res.get("wall_s", time.time() - t0),
            "tokens": res.get("tokens", 0), "samples": len(res.get("samples") or []),
            "eligible": res["status"] != fl.ST_INELIGIBLE,
            "ineligible_reasons": (res.get("eligibility") or {}).get("reasons") if res["status"] == fl.ST_INELIGIBLE else None,
            "recorded": entry.get("recorded") or {}}


def _med(xs):
    xs = [x for x in xs if x is not None]
    return round(statistics.median(xs), 1) if xs else None


def summarize(rows):
    """rows -> {tier: {k: {...}}} + verdicts. Only eligible tasks with a recorded outcome count."""
    out = {}
    for tier in sorted({r["tier"] for r in rows}):
        tr = [r for r in rows if r["tier"] == tier]
        arms = {}
        for k in sorted({r["k"] for r in tr}):
            rs = [r for r in tr if r["k"] == k and r["eligible"] and r["recorded"]]
            n = len(rs)
            conv = sum(1 for r in rs if r["converged"])
            rec = sum(1 for r in rs if r["recorded"].get("converged"))
            arms[k] = {
                "n_eligible": n, "n_ineligible": sum(1 for r in tr if r["k"] == k and not r["eligible"]),
                "lane_converged": conv, "lane_rate": round(100 * conv / n, 1) if n else None,
                "open_converged": rec, "open_rate": round(100 * rec / n, 1) if n else None,
                "lane_median_wall_s": _med([r["wall_s"] for r in rs]),
                "open_median_wall_s": _med([r["recorded"].get("wall_s") for r in rs]),
                "lane_median_tokens": _med([r["tokens"] for r in rs]),
                "open_median_tokens": _med([r["recorded"].get("tokens") for r in rs]),
                "transport_errors": sum(1 for r in rs if r["status"] == "transport_error"),
            }
        out[tier] = {"arms": arms, "verdict": verdict(arms)}
    return out


def verdict(arms):
    a1, a4 = arms.get(1), arms.get(4)
    def ok(a):
        return a and a["n_eligible"] and a["lane_rate"] is not None
    if not ok(a1) and not ok(a4):
        return "no data"
    why = []
    ship = False
    if ok(a1):
        faster = (a1["lane_median_wall_s"] is not None and a1["open_median_wall_s"]
                  and a1["lane_median_wall_s"] <= a1["open_median_wall_s"] / 2)
        if a1["lane_rate"] >= a1["open_rate"] and faster:
            ship = True
            why.append("K=1 matches open-loop convergence at <= half the median wall time")
    if ok(a4) and a4["lane_rate"] >= a4["open_rate"] + 5:
        ship = True
        why.append("K=4 beats open loop by >= 5 points")
    if ship:
        return "SHIP: " + "; ".join(why)
    k1_bad = ok(a1) and a1["lane_rate"] < a1["open_rate"] - 5
    recovers = ok(a4) and a4["lane_rate"] >= a4["open_rate"] - 5
    if k1_bad and not recovers:
        return "KILL: K=1 more than 5 points worse and K=4 does not recover it"
    return "inconclusive: needs more eligible tasks or a closer look at the arm numbers"


def merge(paths):
    rows = []
    for p in paths:
        rows += json.load(open(p))["rows"]
    return {"rows": rows, "summary": summarize(rows)}


def selftest():
    def mk(tier, k, conv, wall, rec_conv, rec_wall):
        return {"id": "x", "tier": tier, "k": k, "status": "passed" if conv else "exhausted", "converged": conv,
                "wall_s": wall, "tokens": 10, "eligible": True,
                "recorded": {"converged": rec_conv, "wall_s": rec_wall, "tokens": 20}}
    rows = [mk("35b", 1, True, 40, True, 100) for _ in range(5)] + [mk("35b", 1, False, 40, False, 100) for _ in range(5)]
    s = summarize(rows)["35b"]
    assert s["verdict"].startswith("SHIP"), s
    rows = [mk("9b", 1, False, 90, True, 100) for _ in range(10)] + [mk("9b", 4, False, 300, True, 100) for _ in range(10)]
    assert summarize(rows)["9b"]["verdict"].startswith("KILL"), summarize(rows)
    rows = [mk("t", 1, True, 90, True, 100) for _ in range(4)] + [mk("t", 4, True, 90, False, 100) for _ in range(4)] + \
           [mk("t", 4, True, 90, True, 100) for _ in range(0)]
    assert summarize(rows)["t"]["verdict"].startswith("SHIP"), summarize(rows)
    # end-to-end with a stubbed lane over a tiny workspace
    d = tempfile.mkdtemp()
    ws = os.path.join(d, "ws")
    os.makedirs(ws)
    open(os.path.join(ws, "a.py"), "w").write("x = 1\n")
    entry = {"id": "t1", "tier": "9b", "workspace": ws, "targets": ["a.py"], "verify": "true",
             "recorded": {"converged": True, "wall_s": 50, "tokens": 5}}
    r = run_one(entry, 1, "m", "off", d, lane_runner=lambda t, **kw: {"status": "passed", "wall_s": 3.0, "tokens": 7,
                                                                     "samples": [1], "eligibility": {}})
    assert r["converged"] and r["wall_s"] == 3.0 and r["recorded"]["wall_s"] == 50
    assert os.path.exists(os.path.join(ws, "a.py")), "source workspace must be untouched"
    shutil.rmtree(d)
    print("SELFTEST OK")


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--corpus")
    ap.add_argument("--k", type=int, action="append")
    ap.add_argument("--model")
    ap.add_argument("--thinking", default="off", choices=("off", "on", "low"))
    ap.add_argument("--limit", type=int)
    ap.add_argument("--shard", help="I/N")
    ap.add_argument("--out")
    ap.add_argument("--tmp", help="parent dir for the per-run copies")
    ap.add_argument("--dry-run", action="store_true", help="only report eligibility per task")
    ap.add_argument("--merge", nargs="+")
    ap.add_argument("--selftest", action="store_true")
    a = ap.parse_args()
    if a.selftest:
        selftest()
        return 0
    if a.merge:
        rep = merge(a.merge)
    else:
        if not a.corpus:
            ap.error("--corpus required")
        entries = load_corpus(a.corpus)
        if a.shard:
            i, n = (int(x) for x in a.shard.split("/"))
            entries = [e for j, e in enumerate(entries) if j % n == i]
        if a.limit:
            entries = entries[:a.limit]
        ks = a.k or [1]
        base_tmp = a.tmp or tempfile.mkdtemp(prefix="flreplay-root-")
        rows = []
        for e in entries:
            for k in ks:
                if a.dry_run:
                    work = prepare(e, base_tmp)
                    ok, why = fl.lane_eligible({"cwd": work, "targets": e["targets"], "verify": e["verify"],
                                                "verify_timeout_s": e.get("verify_timeout_s"),
                                                "spec": e.get("spec", ""), "title": e.get("title", "")})
                    shutil.rmtree(os.path.dirname(work), ignore_errors=True)
                    rows.append({"id": e.get("id"), "tier": e.get("tier", "unknown"), "k": k, "eligible": ok,
                                 "status": "dry-run", "converged": False, "wall_s": 0, "tokens": 0,
                                 "ineligible_reasons": why, "recorded": e.get("recorded") or {}})
                    break
                try:
                    r = run_one(e, k, a.model, a.thinking, base_tmp)
                except Exception as ex:      # one bad corpus entry must not sink the replay
                    r = {"id": e.get("id"), "tier": e.get("tier", "unknown"), "k": k, "status": "replay_error",
                         "reason": str(ex)[:200], "converged": False, "wall_s": 0, "tokens": 0,
                         "eligible": False, "recorded": e.get("recorded") or {}}
                rows.append(r)
                print(f"[replay] {r['id']} K={k} -> {r['status']} {r['wall_s']}s {r['tokens']}tok", flush=True)
        rep = {"rows": rows, "summary": summarize(rows)}
    txt = json.dumps(rep, indent=1)
    if a.out:
        with open(a.out, "w") as f:
            f.write(txt)
    for tier, s in rep["summary"].items():
        print(f"== tier {tier}: {s['verdict']}")
        for k, arm in s["arms"].items():
            print(f"   K={k}: n={arm['n_eligible']} lane {arm['lane_rate']}% / open {arm['open_rate']}%  "
                  f"wall {arm['lane_median_wall_s']}s vs {arm['open_median_wall_s']}s  "
                  f"tokens {arm['lane_median_tokens']} vs {arm['open_median_tokens']}")
    if not a.out:
        print(txt)
    return 0


if __name__ == "__main__":
    sys.exit(main())
