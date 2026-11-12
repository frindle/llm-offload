#!/usr/bin/env python3
"""Decode-throughput trend vs ollama server uptime, from the worker logs we already keep.

WHY THIS EXISTS
---------------
A widely-cited Apple-Silicon local-inference finding (oMLX) reports decode
throughput decaying ~3.2x over ~10 hours of uptime (66 -> 20 tok/s) from Metal
allocator fragmentation, with scheduled process recycling every 6 hours as the
production remedy. Before adopting a restart cadence here, the claim was checked
against this machine's own data -- and did NOT reproduce. Restarting ollama is
not free (it evicts the resident model and forces a cold reload, against the
keep_alive=24h residency policy), so this script exists to make the check
repeatable rather than a one-off argument.

Run it again before anyone re-proposes scheduled restarts.

WHAT IT CONTROLS FOR
--------------------
Raw tok/s is not comparable across samples, so the aggregate alone would be
misleading:
  * MODEL -- different models decode at different speeds. Filtered to one.
  * GENERATION SIZE -- tok/s varies systematically with how much was generated
    in that iteration (and with how much context preceded it). Bucketed, and the
    trend is fitted inside a single bucket.
Without both controls a busy day of short generations can masquerade as decay.

USAGE
    python3 ollama-throughput-trend.py [--model M] [--since-epoch N]
Server start epoch defaults to the running ollama's actual start time.
"""
import argparse
import collections
import glob
import json
import os
import re
import statistics
import subprocess

LOGS = os.path.expanduser("~/bin/ollama-queue-logs")
ITER = re.compile(r"iteration (\d+)/\d+ generated (\d+) tokens in ([0-9.]+)s \(([0-9.]+) tok/s\)")


def server_start_epoch():
    """Epoch seconds when the running ollama started, or None."""
    try:
        pid = subprocess.run(["pgrep", "-x", "ollama"], capture_output=True,
                             text=True).stdout.split()
        if not pid:
            return None
        out = subprocess.run(["ps", "-o", "lstart=", "-p", pid[0]],
                             capture_output=True, text=True).stdout.strip()
        import time as _t
        return _t.mktime(_t.strptime(out, "%a %b %d %H:%M:%S %Y"))
    except Exception:
        return None


def models():
    m = {}
    for f in glob.glob(os.path.join(LOGS, "*.done.json")) + \
             glob.glob(os.path.join(LOGS, "archive", "*.done.json")):
        try:
            d = json.load(open(f))
        except Exception:
            continue
        m[d.get("id")] = d.get("model")
    return m


def bucket(tok):
    if tok >= 8192:
        return "8192(cap)"
    if tok >= 4096:
        return "4k-8k"
    if tok >= 1024:
        return "1k-4k"
    return "<1k"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default="qwen3.8:27b-q4_K_M")
    ap.add_argument("--since-epoch", type=float, default=None)
    a = ap.parse_args()
    start = a.since_epoch or server_start_epoch()
    if not start:
        raise SystemExit("could not determine ollama start time; pass --since-epoch")

    mdl, rows = models(), []
    for f in glob.glob(os.path.join(LOGS, "*.log")):
        jid = os.path.basename(f).split("-")[0]
        if mdl.get(jid) != a.model:
            continue
        mt = os.path.getmtime(f)
        if mt < start:
            continue
        for _it, tok, _sec, tps in ITER.findall(open(f, errors="replace").read()):
            rows.append(((mt - start) / 3600.0, int(tok), float(tps)))

    if not rows:
        raise SystemExit("no samples for %s since %s" % (a.model, start))
    print("model=%s  samples=%d  uptime span=%.1fh"
          % (a.model, len(rows), max(h for h, _, _ in rows)))

    g = collections.defaultdict(lambda: collections.defaultdict(list))
    for h, tok, tps in rows:
        g[bucket(tok)][int(h // 24)].append(tps)
    print("\ngen-size   | median tok/s by uptime DAY")
    for b in ("8192(cap)", "4k-8k", "1k-4k", "<1k"):
        parts = ["d%d: %5.2f (n=%d)" % (d, statistics.median(v), len(v))
                 for d, v in sorted(g[b].items()) if len(v) >= 5]
        if parts:
            print("%10s | %s" % (b, "   ".join(parts)))

    # Least-squares slope inside ONE bucket -- the tightest control available.
    tight = [(h, t) for h, tok, t in rows if 1024 <= tok < 4096]
    if len(tight) > 30:
        n = len(tight)
        mx = sum(h for h, _ in tight) / n
        my = sum(t for _, t in tight) / n
        num = sum((h - mx) * (t - my) for h, t in tight)
        den = sum((h - mx) ** 2 for h, _ in tight)
        slope = num / den if den else 0.0
        print("\n1k-4k bucket: n=%d  slope=%+.4f tok/s per HOUR of uptime" % (n, slope))
        print("  over 10h: %+.2f tok/s   (the fragmentation claim implies about -4.6/h)" % (10 * slope))
        print("  VERDICT:", "no decay -- scheduled restarts NOT warranted"
              if slope > -0.5 else "DECAY PRESENT -- revisit scheduled recycling")


if __name__ == "__main__":
    main()
