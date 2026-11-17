#!/usr/bin/env python3
"""plex_playback.py -- the error digest's "Playback" section: real stalls per Plex client, with evidence (2026-11-16).

Plex clients report their state to the server every ~10 s and on every change (GET /:/timeline ... state=...&time=<ms>).
A "buffering" report alone means little: a client buffers after every start and after every skip or seek. This module
reads one day of those reports from Loki ({job="files", app="plex"}, Plex Media Server.log at debug) and sorts every
buffering wait by what came just before it:

  start   the first report of a play (or the first after a stop / a different item / a gap)
  seek    the position jumped (the viewer skipped or scrubbed); the wait that follows is expected
  stall   the position had been moving on normally and playback stopped to buffer: what a viewer calls "buffering"
          (a wait shorter than MIN_STALL_S, or one starting within SETTLE_S of a start or seek, is not a stall)

A device has a SPELL when its stalls cluster: at least SPELL_STALLS stalls or SPELL_STALL_S seconds of stall within
SPELL_GAP_S of each other. Slow starts (> SLOW_START_S) and slow seeks (> SLOW_SEEK_S) are counted, not flagged.

For each spell, the evidence comes from the same Loki: what the device fetched (file type, direct play or transcode,
"starved" file requests -- under 1 MB in 10 s or more, which no healthy read does -- and failed ones), whether other
clients stalled at the same time, whether a parity check or the mover was running (syslog), and whether the Glacier
copy was moving (plex_glacier_watch.py's samples, in the container only). A one-line verdict
says which way the evidence points; it is a pointer, not proof. (Per-request MB/s is not used: a playing client reads
an open range at its own pace, so a slow rate is normal there.)

No titles and no item ids leave this module: items are only told apart internally. LAN clients are shown by address,
others as "remote <hash>". Read-only: it only queries Loki.

  python3 plex_playback.py show [--day YYYY-MM-DD]                 the section for a day (default yesterday)
  python3 plex_playback.py scan --from YYYY-MM-DD [--to YYYY-MM-DD]  one line per day and every spell (back-test)
"""
import calendar
import datetime as dt
import hashlib
import json
import re
import sys
import time

PAGE = 5000
LINE_CAP = 60000            # timeline reports per day; past it the day is summarised from what was read ("capped")
SAME_PLAY_GAP_S = 120       # reports further apart than this start a new play
SEEK_TOL_S = 10             # position off the expected one by more than this: a seek
WAIT_CAP_S = 120            # one wait never counts for more than this (a client that vanished mid-buffer)
SPELL_GAP_S = 600           # stalls this close together belong to one spell
SPELL_STALLS = 3            # a spell: this many stalls ...
SPELL_STALL_S = 30          # ... or this many seconds stalled
SLOW_START_S = 15
SLOW_SEEK_S = 8
MIN_STALL_S = 3             # shorter waits in normal playback are blips nobody notices
SETTLE_S = 10               # a wait this soon after a start or seek wait ended still belongs to it
STARVED_BYTES = 1 << 20     # a file request that delivered less than this ...
STARVED_MS = 10000          # ... in at least this long was starved
SYSLOG_LOOKBACK_S = 4 * 86400

TIMELINE_Q = '{job="files",app="plex"} |~ "Request: \\\\[[^]]+\\\\] GET /:/timeline\\\\?"'
LINE_RE = re.compile(r"^(?P<lt>[A-Z][a-z]{2} \d{1,2}, \d{4} \d\d:\d\d:\d\d(?:\.\d+)?)?.*?Request: \[(?P<ip>[0-9a-fA-F.:]+?):\d+"
                     r"[^\]]*\] GET /:/timeline\?(?P<q>\S+)")
SYSLOG_Q = ('{job="syslog"} |~ "md: recovery thread: check|md: recovery thread: exit status|md: sync done|'
            'mdcmd \\\\([0-9]+\\\\): nocheck|mover: started|mover: finished"')


# ---------------------------------------------------------------------------------------------------- parsing
def _param(q, k):
    m = re.search(r"(?:^|&)" + k + r"=([^&]*)", q)
    return m.group(1) if m else None


def _line_time(lt, loki_s):
    """The line's own clock time (ms precision) moved into unix seconds. Plex writes local time; the offset to the
    Loki receive time is a time-zone offset (a multiple of 15 min) plus a small lag, so it is rounded away."""
    if not lt:
        return loki_s
    try:
        base, _, frac = lt.partition(".")
        naive = calendar.timegm(time.strptime(base, "%b %d, %Y %H:%M:%S")) + (float("0." + frac) if frac else 0.0)
    except ValueError:
        return loki_s
    return naive + round((loki_s - naive) / 900.0) * 900


def parse(loki_ns, line):
    """-> {"t", "ip", "state", "pos", "item"} for one timeline request line, or None. pos is seconds; item is a short
    hash used only to tell plays apart (never shown)."""
    m = LINE_RE.search(line)
    if not m:
        return None
    q = m.group("q")
    state, pos = _param(q, "state"), _param(q, "time")
    if not state or pos is None:
        return None
    try:
        pos = int(pos) / 1000.0
    except ValueError:
        return None
    key = _param(q, "ratingKey") or _param(q, "key") or ""
    return {"t": _line_time(m.group("lt"), int(loki_ns) / 1e9), "ip": m.group("ip"), "state": state, "pos": pos,
            "item": hashlib.sha1(key.encode()).hexdigest()[:8]}


def device_label(ip):
    if re.match(r"^(10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.)", ip):
        return ip
    return "remote " + hashlib.sha1(ip.encode()).hexdigest()[:4]


# ---------------------------------------------------------------------------------------------------- the waits
def waits(events):
    """events: one device's reports, oldest first. -> [wait], each {"kind", "t", "end", "secs", "pos", "item"}:
    a run of buffering reports up to the next report in another state. kind: start, seek, stall or blip."""
    out, i, n = [], 0, len(events)
    settled = None                 # (item, end) of the last start or seek wait
    while i < n:
        e = events[i]
        if e["state"] != "buffering":
            i += 1
            continue
        prev = events[i - 1] if i else None
        if (prev is None or prev["state"] == "stopped" or prev["item"] != e["item"]
                or e["t"] - prev["t"] > SAME_PLAY_GAP_S):
            kind = "start"
        else:
            expect = prev["pos"] + (e["t"] - prev["t"] if prev["state"] == "playing" else 0.0)
            kind = "seek" if abs(e["pos"] - expect) > SEEK_TOL_S else "stall"
            if kind == "stall" and settled and settled[0] == e["item"] and e["t"] - settled[1] <= SETTLE_S:
                kind = "seek"
        j = i
        while j + 1 < n and events[j + 1]["state"] == "buffering" and events[j + 1]["item"] == e["item"] \
                and events[j + 1]["t"] - events[j]["t"] <= SAME_PLAY_GAP_S:
            j += 1
        nxt = events[j + 1] if j + 1 < n else None
        if nxt is not None and nxt["t"] - e["t"] <= WAIT_CAP_S:
            secs = nxt["t"] - e["t"]
        else:
            secs = min(WAIT_CAP_S, max(events[j]["t"] - e["t"], 0.0) + 10.0)
        if kind == "stall" and secs < MIN_STALL_S:
            kind = "blip"
        if kind in ("start", "seek"):
            settled = (e["item"], e["t"] + secs)
        out.append({"kind": kind, "t": e["t"], "end": e["t"] + secs, "secs": round(secs, 1), "pos": e["pos"],
                    "item": e["item"]})
        i = j + 1
    return out


def play_seconds(events):
    s = 0.0
    for a, b in zip(events, events[1:]):
        if a["state"] == "playing" and a["item"] == b["item"]:
            s += min(b["t"] - a["t"], 60.0)
    return s


def spells(ws):
    """Clusters of stalls -> [{"start", "end", "stalls", "stall_s", "longest"}] that pass the spell bar."""
    st = [w for w in ws if w["kind"] == "stall"]
    groups, cur = [], []
    for w in st:
        if cur and w["t"] - cur[-1]["end"] > SPELL_GAP_S:
            groups.append(cur)
            cur = []
        cur.append(w)
    if cur:
        groups.append(cur)
    out = []
    for g in groups:
        secs = sum(w["secs"] for w in g)
        if len(g) >= SPELL_STALLS or secs >= SPELL_STALL_S:
            out.append({"start": g[0]["t"], "end": max(w["end"] for w in g), "stalls": len(g),
                        "stall_s": round(secs), "longest": round(max(w["secs"] for w in g))})
    return out


def summarize(events_by_ip):
    """{ip: [events]} -> {label: device summary}."""
    out = {}
    for ip, evs in events_by_ip.items():
        evs = sorted(evs, key=lambda e: e["t"])
        ws = waits(evs)
        plays = sum(1 for w in ws if w["kind"] == "start") or len({e["item"] for e in evs if e["state"] == "playing"})
        k = {kind: [w for w in ws if w["kind"] == kind] for kind in ("start", "seek", "stall")}
        out[device_label(ip)] = {
            "ip": ip, "plays": plays, "play_min": round(play_seconds(evs) / 60.0),
            "stalls": len(k["stall"]), "stall_s": round(sum(w["secs"] for w in k["stall"])),
            "blips": sum(1 for w in ws if w["kind"] == "blip"),
            "seeks": len(k["seek"]), "slow_seeks": sum(1 for w in k["seek"] if w["secs"] > SLOW_SEEK_S),
            "slow_starts": sum(1 for w in k["start"] if w["secs"] > SLOW_START_S),
            "buffering_reports": sum(1 for e in evs if e["state"] == "buffering"),
            "spells": spells(ws), "_waits": ws}
    return out


# ---------------------------------------------------------------------------------------------------- Loki reads
def _range(loki, q, start, end, cap):
    """[(ns, line)] for [start, end), oldest first, deduplicated; (lines, capped)."""
    got, seen, lo, hi = [], set(), int(start * 1e9), int(end * 1e9)
    while lo < hi and len(got) < cap:
        d = loki.get("/loki/api/v1/query_range", query=q, start=str(lo), end=str(hi), limit=str(PAGE),
                     direction="forward")
        page = sorted((int(ts), line) for s in (d.get("data") or {}).get("result") or [] for ts, line in
                      s.get("values") or [])
        fresh = [x for x in page if x not in seen]
        for x in fresh:
            seen.add(x)
        got += fresh
        if len(page) < PAGE or not fresh:
            break
        lo = page[-1][0]                       # inclusive: lines sharing the last timestamp are kept by `seen`
    return got[:cap], len(got) >= cap


def timeline_events(loki, start, end):
    lines, capped = _range(loki, TIMELINE_Q, start, end, LINE_CAP)
    by_ip = {}
    for ns, line in lines:
        e = parse(ns, line)
        if e:
            by_ip.setdefault(e["ip"], []).append(e)
    return by_ip, capped


PART_RE = re.compile(r"Completed[^\[]*\[(?P<ip>[0-9a-fA-F.:]+?):\d+\] (?P<code>\d{3}) GET "
                     r"/library/parts/\d+/\d+/file\.(?P<ext>[A-Za-z0-9]+)\S*.*? (?P<ms>\d+)ms (?P<bytes>\d+) bytes")
DECISION_RE = re.compile(r"Request: \[(?P<ip>[0-9a-fA-F.:]+?):\d+[^\]]*\] GET /video/:/transcode/universal/"
                         r"(?P<what>decision|start)\S*")


def device_evidence(loki, ip, start, end):
    """What the device fetched in [start, end): file types, direct play or not, starved and failed requests."""
    pad = 120
    ipq = ip.replace(".", "\\\\.")
    q = ('{job="files",app="plex"} |~ "Completed[^\\\\[]*\\\\[%s:[0-9]+\\\\] [0-9]+ GET /library/parts/"' % ipq)
    parts, _ = _range(loki, q, start - pad, end + pad, 20000)
    exts, n, total, starved, failed = {}, 0, 0, 0, 0
    for _, line in parts:
        m = PART_RE.search(line)
        if not m or m.group("ip") != ip:
            continue
        n += 1
        b, ms = int(m.group("bytes")), int(m.group("ms"))
        total += b
        exts[m.group("ext").lower()] = exts.get(m.group("ext").lower(), 0) + 1
        if int(m.group("code")) >= 500:
            failed += 1
        elif b < STARVED_BYTES and ms >= STARVED_MS:
            starved += 1
    q2 = ('{job="files",app="plex"} |~ "Request: \\\\[%s:[0-9]+[^]]*\\\\] GET /video/:/transcode/universal/'
          '(decision|start)"' % ipq)
    dec, _ = _range(loki, q2, start - 3600, end + pad, 2000)
    mode = None
    for _, line in dec:
        m = DECISION_RE.search(line)
        if not m or m.group("ip") != ip:
            continue
        s = m.group(0)
        dp, ds = _param(s.split("?", 1)[-1], "directPlay"), _param(s.split("?", 1)[-1], "directStream")
        mode = "direct play" if dp == "1" else "direct stream" if ds == "1" else "transcode"
    span = max(end - start + 2 * pad, 1)
    return {"requests": n, "req_per_min": round(n / (span / 60.0), 1), "mb": round(total / 1e6),
            "exts": exts, "starved": starved, "failed": failed, "mode": mode}


def server_intervals(loki, start, end):
    """{"parity": [(a, b)], "mover": [(a, b)]} overlapping [start, end) from syslog markers (b None = still running)."""
    lines, _ = _range(loki, SYSLOG_Q, start - SYSLOG_LOOKBACK_S, end, 20000)
    out = {"parity": [], "mover": []}
    open_ = {"parity": None, "mover": None}
    for ns, line in lines:
        t = ns / 1e9
        if "recovery thread: check" in line:
            open_["parity"] = open_["parity"] or t
        elif ("sync done" in line or "exit status" in line or "nocheck" in line) and open_["parity"]:
            out["parity"].append((open_["parity"], t))
            open_["parity"] = None
        elif "mover: started" in line:
            open_["mover"] = open_["mover"] or t
        elif "mover: finished" in line and open_["mover"]:
            out["mover"].append((open_["mover"], t))
            open_["mover"] = None
    for k, a in open_.items():
        if a:
            out[k].append((a, None))
    return {k: [(a, b) for a, b in v if (b is None or b > start) and a < end] for k, v in out.items()}


def _overlaps(iv, a, b):
    return any(x < b and (y is None or y > a) for x, y in iv)


def glacier_moving(day):
    """Sample times at which the Glacier copy was moving (plex_glacier_watch samples), or None when the day has no
    samples at all (before the watcher ran, or outside the container): then the section says it was not sampled."""
    try:
        import plex_glacier_watch
        samples = plex_glacier_watch.load_samples(day)
    except Exception:  # noqa: BLE001 - no watcher here: same as no samples
        return None
    return [s["t"] for s in samples if s.get("g") == "moving"] if samples else None


def _running(busy):
    return " and ".join("the " + b for b in busy)


# ---------------------------------------------------------------------------------------------------- the verdict
def _idle(glacier_known):
    return ("nothing scheduled running (parity check, mover, Glacier copy)" if glacier_known
            else "no parity check or mover running (the Glacier copy was not sampled)")


def verdict(ev, others, busy, glacier_known=True):
    """One line saying which way the evidence points. ev: device_evidence; others: labels of other clients that
    stalled in the same spell; busy: ["parity check", "mover", "Glacier copy"] that ran during it; glacier_known:
    False when there were no Glacier samples for the day."""
    if others:
        return "server or home network: %s stalled at the same time" % ", ".join(others)
    bad = []
    if ev.get("failed"):
        bad.append("%d failed" % ev["failed"])
    if ev.get("starved"):
        bad.append("%d got under 1 MB in 10 s or more" % ev["starved"])
    if bad:
        why = ("while %s ran" % _running(busy) if busy
               else "with %s: a busy or waking disk, or this device's link" % _idle(glacier_known))
        return "reading the file was slow: of its file requests, %s, %s" % (" and ".join(bad), why)
    if busy:
        return "server busy: %s %s running" % (_running(busy), "was" if len(busy) == 1 else "were")
    if ev.get("mode") == "transcode":
        return "not clear: a transcode (the server reads the file itself, so there are no file requests to check)"
    if ev.get("requests"):
        return "not clear: the file requests were answered normally and nothing else was busy (the device itself?)"
    return "not clear: no file requests seen from the device in the spell"


# ---------------------------------------------------------------------------------------------------- one day
def day_summary(loki, day, evidence=True):
    """The section's data for one local calendar day, or None when no client reported at all."""
    import error_digest                                      # day_bounds lives there; imported late (no cycle)
    start, end = error_digest.day_bounds(day)
    by_ip, capped = timeline_events(loki, start, end)
    if not by_ip:
        return None
    devs = summarize(by_ip)
    sm = {"day": day, "capped": capped, "devices": {}, "spells": []}
    busy_iv, moving = None, None
    for label, d in sorted(devs.items()):
        sm["devices"][label] = {k: v for k, v in d.items() if not k.startswith("_") and k not in ("ip", "spells")}
        for sp in d["spells"]:
            others = sorted(o for o, od in devs.items() if o != label and any(
                w["kind"] == "stall" and sp["start"] - 60 <= w["t"] <= sp["end"] + 60 for w in od["_waits"]))
            rec = dict(sp, device=label)
            if evidence:
                try:
                    if busy_iv is None:
                        busy_iv, moving = server_intervals(loki, start, end), glacier_moving(day)
                    busy = [name for k, name in (("parity", "parity check"), ("mover", "mover"))
                            if _overlaps(busy_iv[k], sp["start"], sp["end"])]
                    if any(sp["start"] - 240 <= t <= sp["end"] + 240 for t in moving or ()):
                        busy.append("Glacier copy")
                    ev = device_evidence(loki, d["ip"], sp["start"], sp["end"])
                    rec.update(evidence=ev, others=others, busy=busy, glacier_sampled=moving is not None,
                               verdict=verdict(ev, others, busy, moving is not None))
                except Exception as e:  # noqa: BLE001 - the spell is reported without its evidence
                    rec.update(others=others, verdict="no evidence (%s)" % type(e).__name__)
            sm["spells"].append(rec)
    sm["spells"].sort(key=lambda s: s["start"])
    return sm


def _hm(t):
    return time.strftime("%H:%M", time.localtime(t))


def render_section(sm):
    """Markdown lines for the digest; [] when no client played anything."""
    if not sm or not sm.get("devices"):
        return []
    out = ["## Playback", ""]
    sp = sm.get("spells") or []
    if sp:
        out.append("- %d buffering spell%s (stalls of %ds or more in normal playback, not skips or starts):"
                   % (len(sp), "" if len(sp) == 1 else "s", MIN_STALL_S))
        for s in sp:
            out.append("  - %s, %s to %s: %d stall%s, %ss stalled (longest %ss)" % (
                s["device"], _hm(s["start"]), _hm(s["end"]), s["stalls"], "" if s["stalls"] == 1 else "s",
                s["stall_s"], s["longest"]))
            ev = s.get("evidence")
            if ev:
                ex = ", ".join(".%s %d" % kv for kv in sorted(ev["exts"].items(), key=lambda kv: -kv[1])) or "none"
                out.append("    file requests: %s (%s); %s/min; %s MB; %d starved, %d failed" % (
                    ex, ev["mode"] or "mode not logged", ev["req_per_min"], f"{ev['mb']:,}", ev["starved"],
                    ev["failed"]))
                out.append("    also: %s; %s" % (
                    "others stalling: " + ", ".join(s["others"]) if s.get("others") else "no other client stalled",
                    "running: " + ", ".join(s["busy"]) if s.get("busy") else
                    "no parity check, mover or Glacier copy" if s.get("glacier_sampled", True) else
                    "no parity check or mover; Glacier copy not sampled"))
            out.append("    points to: %s" % s.get("verdict", "not checked"))
    else:
        out.append("- No buffering spells.")
    rows = sorted(sm["devices"].items(), key=lambda kv: -kv[1]["play_min"])
    for label, d in rows:
        bits = ["%d play%s, ~%d min" % (d["plays"], "" if d["plays"] == 1 else "s", d["play_min"])]
        bits.append("%d stall%s (%ss)" % (d["stalls"], "" if d["stalls"] == 1 else "s", d["stall_s"]))
        if d["seeks"]:
            bits.append("%d skip%s%s" % (d["seeks"], "" if d["seeks"] == 1 else "s",
                                          ", %d slow" % d["slow_seeks"] if d["slow_seeks"] else ""))
        if d["slow_starts"]:
            bits.append("%d slow start%s" % (d["slow_starts"], "" if d["slow_starts"] == 1 else "s"))
        out.append("- %s: %s" % (label, "; ".join(bits)))
    if sm.get("capped"):
        out.append("- (more than %s reports that day; counted from the first ones)" % f"{LINE_CAP:,}")
    out.append("")
    return out


def notify_lines(sm):
    """Short lines for the daily message: only spells (nothing when playback was clean)."""
    out = []
    for s in (sm or {}).get("spells") or []:
        out.append("%s %s-%s: %d stalls, %ss; points to %s" % (s["device"], _hm(s["start"]), _hm(s["end"]),
                                                               s["stalls"], s["stall_s"], s.get("verdict", "?")))
    return out


# ---------------------------------------------------------------------------------------------------- CLI
def _loki():
    import error_digest
    url, tok = error_digest.loki_settings()
    return error_digest.Loki(url, tok)


def main(argv):
    cmd = argv[1] if len(argv) > 1 else ""

    def arg(k, default=None):
        return argv[argv.index(k) + 1] if k in argv and argv.index(k) + 1 < len(argv) else default

    if cmd == "show":
        day = arg("--day") or (dt.date.today() - dt.timedelta(days=1)).isoformat()
        sm = day_summary(_loki(), day)
        print("\n".join(render_section(sm)) if sm else "no Plex timeline reports for %s" % day)
        return 0
    if cmd == "scan":
        a = dt.date.fromisoformat(arg("--from") or (dt.date.today() - dt.timedelta(days=14)).isoformat())
        b = dt.date.fromisoformat(arg("--to") or (dt.date.today() - dt.timedelta(days=1)).isoformat())
        loki, ev = _loki(), "--no-evidence" not in argv
        while a <= b:
            sm = day_summary(loki, a.isoformat(), evidence=ev)
            if not sm:
                print("%s  no reports" % a)
            else:
                ds = sm["devices"]
                print("%s  %d device(s): %s%s" % (a, len(ds), "; ".join(
                    "%s %dmin %d stall/%ss %d skip %d slowstart" % (k, d["play_min"], d["stalls"], d["stall_s"],
                                                                 d["seeks"], d["slow_starts"])
                    for k, d in sorted(ds.items())), " CAPPED" if sm["capped"] else ""))
                for s in sm["spells"]:
                    print("    SPELL %s %s-%s %d stalls %ss longest %ss -> %s" % (
                        s["device"], _hm(s["start"]), _hm(s["end"]), s["stalls"], s["stall_s"], s["longest"],
                        s.get("verdict", "-")))
                    if s.get("evidence"):
                        print("          %s" % json.dumps(s["evidence"], sort_keys=True))
            a += dt.timedelta(days=1)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
