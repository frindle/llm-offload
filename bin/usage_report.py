#!/usr/bin/env python3
"""usage_report.py -- automatic weekly Claude-usage report (offload plan step 6; the owner 2026-10-15).

Measures the same way as plan sections 2b/2c (session logs, messages deduped by id, price-weighted
tokens: input 1, cache write 1.25, cache read 0.1, output 5; pipeline-upkeep tag from the paths each
message's tool calls touched, carried forward through the session) and reports, weekly:
  median main-turn context, upkeep (pipeline-tagged) share, cache-read share, subagent share,
  weighted tokens per landed commit, and the trend vs the previous 7-day window.

WHERE IT RUNS (no new Mac service, no Unraid SSH):
  * extract  (Mac, cheap, inside the existing queue daemon's tick via usage_report_sched.py): scans
             ~/.claude/projects/**/*.jsonl and `git log`, writes a slim digest (one tiny record per
             message, no content/paths). Parsing only, no metrics.
  * compute  (UNRAID): the digest + this file are shipped as a job to the existing dispatch-cpu-runner
             over the existing CPU-lane API (cpu_job.submit_cpu_job); the runner computes every metric
             and prints JSON. Stdlib only. The runner has no network and holds no secrets.
  * publish  (Mac, same tick): appends the result to the history JSON and writes the vault note over
             the vault REST (URL/token via vault_conf.py; token read from its existing file, never argv).

  usage_report.py run [--dry-run] [--now EPOCH] [--local]   extract -> compute (Unraid) -> publish
  usage_report.py extract OUT.jsonl.gz [--now EPOCH]
  usage_report.py compute --digest FILE [--now EPOCH]       prints metrics JSON (the Unraid side)
  usage_report.py --self-test
--dry-run prints the report and writes nothing. --local computes on this machine instead of Unraid
(manual debugging only; the scheduled path never falls back to the Mac).
"""
import argparse
import gzip
import io
import json
import os
import re
import statistics
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HOME = Path.home()
BIN = Path(__file__).resolve().parent
LOGS = HOME / ".claude" / "projects"
STATE_DIR = HOME / ".ollama-dispatch" / "usage-report"
HISTORY = STATE_DIR / "history.json"
REPOS_ROOT = HOME / "Desktop" / "GitHub Projects"
VAULT_NOTE = "Claude/Reference/Claude-Weekly-Usage-Report.md"
W = {"i": 1.0, "w": 1.25, "r": 0.1, "o": 5.0}
DAY = 86400
WINDOW = 7 * DAY
PIPELINE_RE = re.compile(r"ollama-|\.ollama-dispatch|canary|gate|darkbloom|dispatch-", re.I)
MAX_STR = 2000


# ------------------------------------------------------------------ extract (Mac)
def _iso(ts):
    try:
        from datetime import datetime
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def _strings(o, out, depth=0):
    if depth > 4:
        return
    if isinstance(o, str):
        out.append(o[:MAX_STR])
    elif isinstance(o, dict):
        for v in o.values():
            _strings(v, out, depth + 1)
    elif isinstance(o, list):
        for v in o[:20]:
            _strings(v, out, depth + 1)


def scan_logs(since, logs=LOGS):
    """Return per-message records [{t,s,sub,i,w,r,o,p}] (p = this message's own path hit: 1 pipeline,
    0 non-pipeline path, None no paths); deduped by message id (last usage wins, paths unioned)."""
    msgs = {}
    for f in logs.rglob("*.jsonl"):
        try:
            if f.stat().st_mtime < since:
                continue
        except OSError:
            continue
        parts = f.relative_to(logs).parts
        sub = 1 if "subagents" in parts else 0
        sess = parts[1] if sub and len(parts) > 2 else f.stem
        try:
            fh = open(f, errors="replace")
        except OSError:
            continue
        with fh:
            for ln in fh:
                if '"type":"assistant"' not in ln and '"type": "assistant"' not in ln:
                    continue
                try:
                    d = json.loads(ln)
                except ValueError:
                    continue
                m = d.get("message") or {}
                u = m.get("usage")
                if d.get("type") != "assistant" or not isinstance(u, dict):
                    continue
                t = _iso(d.get("timestamp") or "")
                if t is None or t < since:
                    continue
                mid = m.get("id") or d.get("requestId") or d.get("uuid")
                key = (sess, mid)
                strs = []
                for b in m.get("content") or []:
                    if isinstance(b, dict) and b.get("type") == "tool_use":
                        _strings(b.get("input"), strs)
                hit = None
                if strs:
                    hit = 1 if any(PIPELINE_RE.search(s) for s in strs) else 0
                rec = msgs.get(key)
                if rec is None:
                    rec = msgs[key] = {"t": t, "s": sess, "sub": sub, "p": None}
                rec.update(i=int(u.get("input_tokens") or 0), w=int(u.get("cache_creation_input_tokens") or 0),
                           r=int(u.get("cache_read_input_tokens") or 0), o=int(u.get("output_tokens") or 0))
                if hit is not None:
                    rec["p"] = max(rec["p"] or 0, hit) if rec["p"] is not None else hit
    return list(msgs.values())


def tag_records(recs):
    """Carry the tag forward through each session in time order -> adds g (1 = pipeline upkeep)."""
    by = {}
    for r in recs:
        by.setdefault(r["s"], []).append(r)
    for rs in by.values():
        rs.sort(key=lambda r: r["t"])
        cur = 0
        for r in rs:
            if r["p"] is not None:
                cur = r["p"]
            r["g"] = cur
            r.pop("p", None)
    return recs


def landed_commits(since, root=REPOS_ROOT):
    seen = {}
    if not root.is_dir():
        return []
    for d in sorted(root.iterdir()):
        n = d.name
        if n.endswith(".nosync") or re.search(r" \d+$", n) or not (d / ".git").exists():
            continue
        try:
            out = subprocess.run(["git", "log", "--no-merges", "--since=@%d" % int(since), "--format=%H %ct"],
                                 cwd=d, capture_output=True, text=True, timeout=60).stdout
        except Exception:
            continue
        for ln in out.splitlines():
            p = ln.split()
            if len(p) == 2:
                seen.setdefault(p[0], int(p[1]))
    return [{"c": h, "t": t} for h, t in seen.items()]


def extract(out_path, now):
    since = now - 2 * WINDOW - DAY
    recs = tag_records(scan_logs(since))
    commits = landed_commits(since)
    with gzip.open(out_path, "wt") as fh:
        for r in recs:
            fh.write(json.dumps(r, separators=(",", ":")) + "\n")
        for c in commits:
            fh.write(json.dumps(c, separators=(",", ":")) + "\n")
    return len(recs), len(commits)


# ------------------------------------------------------------------ compute (Unraid)
def _wt(r):
    return sum(r[k] * W[k] for k in W)


def window_metrics(recs, commits, lo, hi):
    rs = [r for r in recs if lo <= r["t"] < hi]
    tot = sum(_wt(r) for r in rs)
    main = [r for r in rs if not r["sub"]]
    ctx = [r["i"] + r["r"] + r["w"] for r in main]
    nc = sum(1 for c in commits if lo <= c["t"] < hi)

    def share(x):
        return round(x / tot, 4) if tot else None
    return {
        "messages": len(rs), "main_messages": len(main), "commits": nc,
        "weighted_tokens": round(tot),
        "median_main_context": int(statistics.median(ctx)) if ctx else None,
        "upkeep_share": share(sum(_wt(r) for r in rs if r["g"])),
        "cache_read_share": share(sum(r["r"] * W["r"] for r in rs)),
        "subagent_share": share(sum(_wt(r) for r in rs if r["sub"])),
        "tokens_per_commit": round(tot / nc) if nc else None,
    }


def compute(digest_path, now):
    recs, commits = [], []
    with gzip.open(digest_path, "rt") as fh:
        for ln in fh:
            d = json.loads(ln)
            (commits if "c" in d else recs).append(d)
    cur = window_metrics(recs, commits, now - WINDOW, now)
    prev = window_metrics(recs, commits, now - 2 * WINDOW, now - WINDOW)
    return {"generated": int(now), "window_days": 7, "current": cur, "previous": prev}


# ------------------------------------------------------------------ report
def _fmt(k, v):
    if v is None:
        return "n/a"
    if k.endswith("_share"):
        return "%.1f%%" % (v * 100)
    return "{:,}".format(int(v))


def _trend(k, c, p):
    if c is None or p is None or p == 0:
        return "n/a"
    if k.endswith("_share"):
        return "%+.1f pts" % ((c - p) * 100)
    return "%+.0f%%" % ((c - p) / p * 100)


ROWS = [("median_main_context", "Median main-turn context (tokens)", "lower"),
        ("upkeep_share", "Pipeline-upkeep share of usage", "lower"),
        ("cache_read_share", "Cache-read share of usage", "info"),
        ("subagent_share", "Subagent share of usage", "info"),
        ("tokens_per_commit", "Weighted tokens per landed commit", "lower"),
        ("weighted_tokens", "Total weighted tokens", "info"),
        ("commits", "Landed commits", "info")]


def render(res):
    from datetime import datetime, timezone
    end = datetime.fromtimestamp(res["generated"], timezone.utc).strftime("%Y-%m-%d")
    c, p = res["current"], res["previous"]
    lines = ["## Week ending %s" % end, "",
             "| Metric | This week | Previous week | Trend |", "|---|---|---|---|"]
    for k, label, _ in ROWS:
        lines.append("| %s | %s | %s | %s |" % (label, _fmt(k, c[k]), _fmt(k, p[k]), _trend(k, c[k], p[k])))
    lines += ["", "Baseline (plan 2b, 30 days): upkeep about 65%, cache reads about 65% of weighted cost. "
              "Target: upkeep toward 15-25%. Method: session logs, deduped by message id, weights "
              "input 1 / cache write 1.25 / cache read 0.1 / output 5; upkeep tag from touched paths, carried "
              "forward per session (approximate). Computed on Unraid by the CPU runner.", ""]
    return "\n".join(lines)


# ------------------------------------------------------------------ orchestration (Mac)
def compute_on_unraid(digest_path, now):
    sys.path.insert(0, str(BIN))
    import cpu_job
    api = os.environ.get("CPU_RUNNER_API")
    if not api:
        import cpu_dispatch
        api = cpu_dispatch.DEFAULT_API
    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q", td], check=True)
        Path(td, "README").write_text("usage-report carrier\n")
        env = dict(os.environ, GIT_AUTHOR_NAME="u", GIT_AUTHOR_EMAIL="u@x", GIT_COMMITTER_NAME="u",
                   GIT_COMMITTER_EMAIL="u@x")
        subprocess.run(["git", "-C", td, "add", "-A"], check=True)
        subprocess.run(["git", "-C", td, "commit", "-qm", "carrier"], check=True, env=env)
        tools = {"bin/usage_report.py": str(Path(__file__).resolve()),
                 "bin/digest.jsonl.gz": Path(digest_path).read_bytes()}
        r = cpu_job.submit_cpu_job(
            td, 'python3 "$JOB_TOOLS/bin/usage_report.py" compute --digest "$JOB_TOOLS/bin/digest.jsonl.gz" --now %d' % now,
            timeout_s=300, stage="usage-report", label="weekly usage report", bundle_id="usage-report",
            tools=tools, api=api, claim_timeout_s=1800, fallback=False)
    if r.exit_code != 0 or r.ran_on != "runner":
        raise RuntimeError("runner compute failed exit=%s: %s" % (r.exit_code, (r.stderr_tail or "")[-300:]))
    return json.loads(r.stdout_tail.strip().splitlines()[-1])


def load_history(path=HISTORY):
    try:
        return json.loads(Path(path).read_text())
    except Exception:
        return []


def save_history(res, path=HISTORY):
    h = load_history(path)
    h = [e for e in h if e.get("generated_day") != time.strftime("%Y-%m-%d", time.gmtime(res["generated"]))]
    h.append(dict(res, generated_day=time.strftime("%Y-%m-%d", time.gmtime(res["generated"]))))
    Path(path).parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(str(path) + ".tmp")
    tmp.write_text(json.dumps(h[-104:], indent=1))
    os.replace(tmp, path)


def vault_write(md, week_end):
    """Create the note on first run (PUT), then POST-append each week (204)."""
    import urllib.request
    import urllib.error
    sys.path.insert(0, str(BIN))
    import vault_conf
    url = vault_conf.vault_url() + "/vault/" + VAULT_NOTE
    auth = subprocess.run([sys.executable, str(BIN / "vault_conf.py"), "auth"], capture_output=True,
                          text=True).stdout.strip()

    def call(method, body):
        req = urllib.request.Request(url, data=body.encode(), method=method,
                                     headers={"Authorization": auth, "Content-Type": "text/markdown"})
        return urllib.request.urlopen(req, timeout=30).status
    try:
        req = urllib.request.Request(url, headers={"Authorization": auth})
        existing = urllib.request.urlopen(req, timeout=30).read().decode()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
        existing = None
    if existing is not None and ("## Week ending %s" % week_end) in existing:
        return "already-present"
    if existing is None:
        head = "# Claude weekly usage report\n\nWritten automatically every week by the queue daemon; " \
               "metrics computed on Unraid. History: ~/.ollama-dispatch/usage-report/history.json\n\n"
        return call("PUT", head + md)
    return call("POST", "\n" + md)


def run(dry, now, local=False):
    with tempfile.TemporaryDirectory() as td:
        dg = os.path.join(td, "digest.jsonl.gz")
        nm, nc = extract(dg, now)
        res = compute(dg, now) if local else compute_on_unraid(dg, now)
    md = render(res)
    if dry:
        print(md)
        print("(dry-run: extracted %d messages, %d commits; nothing written)" % (nm, nc))
        return res
    save_history(res)
    st = vault_write(md, time.strftime("%Y-%m-%d", time.gmtime(res["generated"])))
    print(md)
    print("history updated; vault write: %s" % st)
    return res


# ------------------------------------------------------------------ self-test
def self_test():
    import shutil
    ok = fails = 0

    def chk(name, got, want):
        nonlocal ok, fails
        if got == want:
            ok += 1
        else:
            fails += 1
            print("FAIL %s: got %r want %r" % (name, got, want))
    td = Path(tempfile.mkdtemp())
    try:
        now = 1_800_000_000.0
        logs = td / "logs" / "proj"
        (logs / "S1" / "subagents").mkdir(parents=True)

        def line(mid, ts, i, w, r, o, tool=None):
            from datetime import datetime, timezone
            c = [{"type": "tool_use", "input": {"file_path": tool}}] if tool else [{"type": "text", "text": "x"}]
            return json.dumps({"type": "assistant", "timestamp": datetime.fromtimestamp(ts, timezone.utc).isoformat(),
                               "message": {"id": mid, "content": c, "usage": {
                                   "input_tokens": i, "cache_creation_input_tokens": w,
                                   "cache_read_input_tokens": r, "output_tokens": o}}}) + "\n"
        t = now - DAY
        (logs / "S1.jsonl").write_text(
            line("a", t, 10, 0, 1000, 10, "/x/ollama-queue.py")          # pipeline
            + line("a", t, 10, 0, 1000, 20)                              # same id twice -> deduped, last wins
            + line("b", t + 10, 10, 0, 3000, 10)                         # inherits pipeline tag
            + line("c", t + 20, 10, 0, 500, 10, "/x/app/page.tsx")       # product
            + line("old", now - 30 * DAY, 99, 99, 99, 99))               # outside the scan window
        (logs / "S1" / "subagents" / "agent-1.jsonl").write_text(line("s", t + 30, 100, 0, 0, 0, "/x/page.tsx"))
        for f in (td / "logs").rglob("*.jsonl"):
            os.utime(f, (now, now))
        recs = tag_records(scan_logs(now - 15 * DAY, td / "logs"))
        chk("dedupe+window", sorted(r["i"] + r["r"] for r in recs), [100, 510, 1010, 3010])
        g = {(r["sub"], r["r"]): r["g"] for r in recs}
        chk("tag a", g[(0, 1000)], 1)
        chk("tag carried", g[(0, 3000)], 1)
        chk("tag product", g[(0, 500)], 0)
        chk("subagent own tag", g[(1, 0)], 0)
        dg = td / "d.jsonl.gz"
        with gzip.open(dg, "wt") as fh:
            for r in recs:
                fh.write(json.dumps(r) + "\n")
            fh.write(json.dumps({"c": "h1", "t": t}) + "\n")
            fh.write(json.dumps({"c": "h2", "t": now - 10 * DAY}) + "\n")
        res = compute(str(dg), now)
        c = res["current"]
        chk("messages", c["messages"], 4)
        chk("commits", c["commits"], 1)
        chk("median ctx", c["median_main_context"], 1010)
        tot = (10 + 1000 * .1 + 20 * 5) + (10 + 300 + 50) + (10 + 50 + 50) + 100
        chk("weighted", c["weighted_tokens"], round(tot))
        chk("upkeep", c["upkeep_share"], round(((10 + 100 + 100) + (10 + 300 + 50)) / tot, 4))
        chk("subagent", c["subagent_share"], round(100 / tot, 4))
        chk("per commit", c["tokens_per_commit"], round(tot))
        chk("prev window commits", res["previous"]["commits"], 1)
        md = render(res)
        chk("render has table", "| Median main-turn context (tokens) | 1,010 |" in md, True)
        h = td / "h.json"
        save_history(res, h)
        save_history(res, h)
        chk("history idempotent per day", len(load_history(h)), 1)
        chk("trend n/a", _trend("upkeep_share", 0.5, None), "n/a")
        chk("trend pts", _trend("upkeep_share", 0.5, 0.6), "-10.0 pts")
    finally:
        shutil.rmtree(td, ignore_errors=True)
    print("usage_report self-test: %d ok, %d failed" % (ok, fails))
    return 1 if fails else 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--self-test", action="store_true")
    sp = ap.add_subparsers(dest="cmd")
    r = sp.add_parser("run")
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--now", type=float)
    r.add_argument("--local", action="store_true")
    e = sp.add_parser("extract")
    e.add_argument("out")
    e.add_argument("--now", type=float)
    c = sp.add_parser("compute")
    c.add_argument("--digest", required=True)
    c.add_argument("--now", type=float)
    a = ap.parse_args(argv)
    if a.self_test:
        return self_test()
    now = (getattr(a, "now", None) or time.time())
    if a.cmd == "run":
        run(a.dry_run, now, a.local)
    elif a.cmd == "extract":
        print("%d messages, %d commits" % extract(a.out, now))
    elif a.cmd == "compute":
        print(json.dumps(compute(a.digest, now)))
    else:
        ap.print_help()
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
