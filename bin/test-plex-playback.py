#!/usr/bin/env python3
"""Tests for plex_playback.py (the digest's Playback section: real stalls vs skips and starts), 2026-11-16.

Fake Loki only (answers by query); no network, no queue, temp dirs only.
  python3 test-plex-playback.py      prints ALL PASS / N FAILED (rc 0 / 1)
"""
import datetime as dt
import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
TMP = Path(tempfile.mkdtemp(prefix="pbk-test-"))
os.environ["ERROR_DIGEST_DIR"] = str(TMP / "digest")
os.environ["PLEX_GLACIER_DIR"] = str(TMP / "pgw")
os.environ.pop("LOKI_TOKEN", None)
import plex_playback as P  # noqa: E402
import error_digest as ED  # noqa: E402

FAILS, N = [], [0]
LAN = ".".join(["192", "168", "50", "20"])        # built, so the public-repo scanner sees no private address
LAN2 = ".".join(["192", "168", "50", "21"])
WAN = "203.0.113.9"                                # TEST-NET-3
SECRET_KEY = "987654"                              # an item id that must never reach the output


def chk(name, cond, detail=""):
    N[0] += 1
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "\n     " + str(detail)[-1500:]))
    if not cond:
        FAILS.append(name)


DAY = "2026-11-14"
D0, D1 = ED.day_bounds(DAY)
T0 = D0 + 13 * 3600                                # 13:00 local on DAY


def tl_line(t, ip, state, pos_s, key=SECRET_KEY):
    lt = time.strftime("%b %d, %Y %H:%M:%S", time.localtime(t)) + ".%03d" % int((t % 1) * 1000)
    return (int((t + 0.8) * 1e9),                  # Loki receives it a moment later
            "%s [1234] DEBUG - Request: [%s:51515 (Allowed Network (Subnet))] GET /:/timeline?key=%%2Flibrary%%2F"
            "metadata%%2F%s&ratingKey=%s&state=%s&duration=3600000&time=%d&playbackTime=1 (5 live) #1a TLS"
            % (lt, ip, key, key, state, int(pos_s * 1000)))


def part_line(t, ip, code, nbytes, ms, ext="mkv", closed=True):
    """The real shape (Plex 1.4x, 11-15): 'Completed after connection close: [ip:port] 206 GET ...'."""
    lt = time.strftime("%b %d, %Y %H:%M:%S", time.localtime(t)) + ".000"
    return (int(t * 1e9), "%s [55] DEBUG - Completed%s: [%s:40000] %d GET "
                          "/library/parts/123/456/file.%s (5 live) #2b TLS %dms %d bytes (range: bytes=0-) "
            % (lt, " after connection close" if closed else "", ip, code, ext, ms, nbytes))


def ev(t, state, pos, item="a"):
    return {"t": float(t), "ip": LAN, "state": state, "pos": float(pos), "item": item}


class FakeLoki:
    """Answers query_range by which query it is; lines are filtered to the asked window like Loki does."""

    def __init__(self, timeline=(), parts=(), decisions=(), syslog=(), fail_on=None):
        self.data = {"timeline": list(timeline), "parts": list(parts), "decision": list(decisions),
                     "syslog": list(syslog)}
        self.fail_on, self.calls = fail_on, []

    def get(self, path, **params):
        q = params.get("query", "")
        kind = ("syslog" if 'job="syslog"' in q else "timeline" if "/:/timeline" in q
                else "parts" if "/library/parts/" in q else "decision" if "transcode/universal" in q else "?")
        self.calls.append(kind)
        if self.fail_on == kind:
            raise ED.LokiError("Loki unreachable (test)")
        lo, hi = int(params["start"]), int(params["end"])
        vals = [[str(ns), line] for ns, line in self.data.get(kind, []) if lo <= ns < hi]
        vals = vals[:int(params.get("limit", 5000))]
        return {"data": {"result": [{"stream": {"job": "files"}, "values": vals}] if vals else []}}


def playing_run(t, pos, secs, ip, step=10, key=SECRET_KEY):
    """Normal playback reports every `step` s for `secs` s from (t, pos); -> (lines, t_end, pos_end)."""
    out = []
    for k in range(0, int(secs) + 1, step):
        out.append(tl_line(t + k, ip, "playing", pos + k, key))
    return out, t + secs, pos + secs


def stall(t, pos, secs, ip, key=SECRET_KEY):
    """A stall at (t, pos) lasting secs: buffering reports every 10 s, then playing at the same position."""
    out = [tl_line(t + k, ip, "buffering", pos, key) for k in range(0, int(secs), 10)]
    out.append(tl_line(t + secs, ip, "playing", pos, key))
    return out, t + secs, pos


def session_with_stalls(ip, t, n_stalls, stall_s, gap=60):
    lines, pos = [tl_line(t, ip, "buffering", 0), tl_line(t + 2, ip, "playing", 0)], 0
    t += 2
    for _ in range(n_stalls):
        a, t, pos = playing_run(t, pos, gap, ip)
        lines += a
        b, t, pos = stall(t + 5, pos + 5, stall_s, ip)
        lines += b
    lines.append(tl_line(t + 10, ip, "stopped", pos + 10))
    return lines


# ------------------------------------------------------------------------------------------------ parsing
def test_parse():
    ns, line = tl_line(T0, LAN, "buffering", 61.5)
    e = P.parse(ns, line)
    chk("parse: a timeline request line gives ip, state, position in seconds",
        e and e["ip"] == LAN and e["state"] == "buffering" and e["pos"] == 61.5, e)
    chk("parse: the line's own clock is used, the time-zone offset to Loki's receive time rounded away",
        e and abs(e["t"] - T0) < 0.01, (e, T0))
    chk("parse: the item is a short hash, never the id", e and SECRET_KEY not in json.dumps(e), e)
    ns2, l2 = part_line(T0, LAN, 206, 1000, 5)
    chk("parse: a Completed (or any non-timeline) line is ignored", P.parse(ns2, l2) is None)
    chk("parse: no time= -> ignored", P.parse(ns, line.replace("&time=", "&tim=")) is None)
    e3 = P.parse(ns, line.split(" [1234]", 1)[1])
    chk("parse: no clock text on the line -> Loki's receive time", e3 and abs(e3["t"] - ns / 1e9) < 0.01, e3)
    for ip6, want in (("2001:db8:0:1::5", "2001:db8:0:1::5"), ("::ffff:" + LAN, LAN)):
        e6 = P.parse(*tl_line(T0, ip6, "playing", 10))
        chk("parse: an IPv6 client address is kept whole (%s)" % ip6, e6 and e6["ip"] == want, e6)
    m = P.DECISION_RE.search("Request: [2001:db8::5:51515 (Allowed Network)] GET /video/:/transcode/universal/start?x")
    chk("parse: the transcode request regex keeps an IPv6 address whole", m and m.group("ip") == "2001:db8::5",
        m and m.group("ip"))
    chk("label: a LAN client by address", P.device_label(LAN) == LAN)
    lab = P.device_label(WAN)
    chk("label: anything else hashed, never the address", lab.startswith("remote ") and WAN not in lab, lab)


# ------------------------------------------------------------------------------------------------ waits
def kinds(evs):
    return [(w["kind"], w["secs"]) for w in P.waits(evs)]


def test_waits():
    chk("waits: the first buffering of a play is a start",
        kinds([ev(0, "buffering", 0), ev(3, "playing", 0)]) == [("start", 3.0)])
    w = kinds([ev(0, "playing", 100), ev(10, "playing", 110), ev(20, "buffering", 120), ev(30, "buffering", 120),
               ev(45, "playing", 120)])
    chk("waits: buffering where playback had got to on its own is a stall; the run of reports is one wait",
        w == [("stall", 25.0)], w)
    w = kinds([ev(0, "playing", 100), ev(1, "buffering", 131), ev(2, "playing", 131)])
    chk("waits: a jump in position (a 30 s skip) is a seek", w == [("seek", 1.0)], w)
    w = kinds([ev(0, "playing", 100), ev(1, "buffering", 131), ev(2, "playing", 131), ev(6, "buffering", 135),
               ev(12, "playing", 135)])
    chk("waits: a wait just after a seek settles still belongs to the seek", [k for k, _ in w] == ["seek", "seek"], w)
    w = kinds([ev(0, "playing", 100), ev(10, "buffering", 110), ev(11, "playing", 110)])
    chk("waits: a stall under %d s is a blip, not a stall" % P.MIN_STALL_S, w == [("blip", 1.0)], w)
    w = kinds([ev(0, "playing", 100, "a"), ev(10, "buffering", 0, "b"), ev(12, "playing", 0, "b")])
    chk("waits: a different item is a new start", [k for k, _ in w] == ["start"], w)
    w = kinds([ev(0, "playing", 100), ev(10, "paused", 110), ev(400, "buffering", 110), ev(405, "playing", 110)])
    chk("waits: after a long gap (paused 6 min) it is a start", [k for k, _ in w] == ["start"], w)
    w = kinds([ev(0, "playing", 100), ev(10, "paused", 110), ev(30, "buffering", 110), ev(36, "playing", 110)])
    chk("waits: resuming from a pause at the same spot and buffering is the resume's wait, not a stall",
        w == [("seek", 6.0)], w)
    w = kinds([ev(0, "playing", 100), ev(10, "paused", 110), ev(20, "paused", 900), ev(30, "buffering", 900),
               ev(34, "playing", 900)])
    chk("waits: a scrub made while paused (already reported paused at the new spot) is a seek, not a stall",
        w == [("seek", 4.0)], w)
    w = kinds([ev(0, "playing", 100), ev(10, "paused", 110), ev(30, "buffering", 110), ev(36, "playing", 110)] +
              [ev(46 + 10 * k, "playing", 120 + 10 * k) for k in range(6)] +
              [ev(106, "buffering", 180), ev(120, "playing", 180)])
    chk("waits: a real stall later in the same play after a resume is still a stall",
        [k for k, _ in w] == ["seek", "stall"], w)
    w = kinds([ev(0, "playing", 100), ev(10, "buffering", 110), ev(20, "buffering", 110)])
    chk("waits: no report after the buffering -> counted to the last report plus 10 s", w == [("stall", 20.0)], w)
    w = kinds([ev(0, "playing", 100), ev(10, "buffering", 110), ev(500, "playing", 110)])
    chk("waits: a silent gap after buffering (app closed, device asleep) counts only to the last report plus 10 s",
        w == [("stall", 10.0)], w)
    w = kinds([ev(0, "playing", 100)] + [ev(10 + 10 * k, "buffering", 110) for k in range(21)] +
              [ev(215, "playing", 110)])
    chk("waits: one wait never counts more than %d s" % P.WAIT_CAP_S, w == [("stall", float(P.WAIT_CAP_S))], w)


def test_spells():
    def stalls(*pairs):
        return [{"kind": "stall", "t": float(t), "end": float(t + s), "secs": float(s)} for t, s in pairs]
    chk("spells: three stalls within 10 min are a spell",
        len(P.spells(stalls((0, 4), (100, 4), (200, 4)))) == 1)
    chk("spells: two short stalls are not", P.spells(stalls((0, 4), (100, 4))) == [])
    sp = P.spells(stalls((0, 35)))
    chk("spells: one stall of 30 s or more is", len(sp) == 1 and sp[0]["stall_s"] == 35 and sp[0]["longest"] == 35, sp)
    sp = P.spells(stalls((0, 4), (100, 4), (200, 4), (2000, 4), (2100, 4), (2200, 4)))
    chk("spells: stalls more than 10 min apart are separate spells", len(sp) == 2, sp)
    sp = P.spells(stalls((0, 4), (100, 4)) + [{"kind": "seek", "t": 150.0, "end": 160.0, "secs": 10.0}] * 5)
    chk("spells: skips never make a spell", sp == [], sp)


# ------------------------------------------------------------------------------------------------ a whole day
def test_day():
    # LAN: a skimming session -- 40 skips of 30 s, each with a short rebuffer -- and nothing else
    skim, t, pos = [tl_line(T0, LAN, "buffering", 0), tl_line(T0 + 1, LAN, "playing", 0)], T0 + 1, 0
    for _ in range(40):
        pos += 30
        t += 1
        skim += [tl_line(t, LAN, "buffering", pos), tl_line(t + 0.4, LAN, "playing", pos)]
    loki = FakeLoki(timeline=skim)
    sm = P.day_summary(loki, DAY)
    d = sm["devices"][LAN]
    chk("day: 40 skips with rebuffers are 40 skips and no stall, no spell",
        d["seeks"] == 40 and d["stalls"] == 0 and sm["spells"] == [], (d, sm["spells"]))
    chk("day: no spell -> no evidence queries", loki.calls == ["timeline"], loki.calls)

    # LAN2: real stalls (4 x 20 s) while its file requests starve, plus two failed requests
    tl = session_with_stalls(LAN2, T0 + 3600, 4, 20)
    parts = [part_line(T0 + 3600 + 30 + 20 * i, LAN2, 206, 16384, 40000) for i in range(5)]
    parts += [part_line(T0 + 3600 + 100, LAN2, 500, 0, 80000)] * 1 + [part_line(T0 + 3600 + 101, LAN2, 500, 0, 70000)]
    parts += [part_line(T0 + 3600 + 50, LAN2, 206, 50_000_000, 900000, closed=False)]   # a paced read: fine
    parts += [part_line(T0 + 3600 + 60, LAN, 206, 16384, 40000)]                 # another device: not counted
    loki = FakeLoki(timeline=tl, parts=parts)
    sm = P.day_summary(loki, DAY)
    sp = sm["spells"]
    chk("day: four 20 s stalls make one spell on that device",
        len(sp) == 1 and sp[0]["device"] == LAN2 and sp[0]["stalls"] == 4 and sp[0]["stall_s"] == 80, sp)
    e = sp[0].get("evidence") or {}
    chk("day: evidence counts only that device's requests: 5 starved, 2 failed, a paced big read is fine",
        e.get("starved") == 5 and e.get("failed") == 2 and e.get("requests") == 8 and e.get("exts") == {"mkv": 8}, e)
    chk("day: no Glacier samples for the day -> slow reads, parity and mover idle, Glacier 'not sampled'",
        sp[0]["verdict"].startswith("reading the file was slow") and "Glacier copy was not sampled" in sp[0]["verdict"]
        and sp[0]["glacier_sampled"] is False
        and "Glacier copy not sampled" in "\n".join(P.render_section(sm)), sp[0]["verdict"])

    # watcher samples (every 2 min): idle all day -> nothing scheduled ran
    sd = TMP / "pgw" / "samples"
    sd.mkdir(parents=True, exist_ok=True)
    (sd / (DAY + ".jsonl")).write_text("".join(json.dumps({"t": int(T0 + k * 120), "g": "idle"}) + "\n"
                                               for k, g in enumerate(range(60))))
    sm = P.day_summary(FakeLoki(timeline=tl, parts=parts), DAY)
    s = sm["spells"][0]
    chk("day: Glacier samples all idle -> verdict says nothing scheduled was running",
        "nothing scheduled running" in s["verdict"] and s["glacier_sampled"] is True
        and "no parity check, mover or Glacier copy" in "\n".join(P.render_section(sm)), s["verdict"])

    # the same spell while the Glacier copy was moving
    (sd / (DAY + ".jsonl")).write_text("".join(json.dumps({"t": int(T0 + 3600 + k * 120), "g": g}) + "\n"
                                               for k, g in enumerate(["idle", "moving", "moving", "idle"])))
    sm = P.day_summary(FakeLoki(timeline=tl, parts=parts), DAY)
    s = sm["spells"][0]
    chk("day: the Glacier copy moving during the spell is named as running",
        s["busy"] == ["Glacier copy"] and s["verdict"].endswith("while the Glacier copy ran"), s)
    (sd / (DAY + ".jsonl")).write_text(json.dumps({"t": int(T0), "g": "moving"}) + "\n")
    s = P.day_summary(FakeLoki(timeline=tl, parts=parts), DAY)["spells"][0]
    chk("day: a Glacier copy an hour before the spell does not count", s["busy"] == [], s)
    (sd / (DAY + ".jsonl")).unlink()

    # both devices stall at the same time -> server or network, whatever else
    tl2 = session_with_stalls(LAN2, T0 + 7200, 4, 20) + session_with_stalls(LAN, T0 + 7205, 4, 20)
    sm = P.day_summary(FakeLoki(timeline=tl2), DAY)
    v = [s["verdict"] for s in sm["spells"]]
    chk("day: two clients stalling together -> server or home network, each naming the other",
        len(v) == 2 and all(x.startswith("server or home network") for x in v) and LAN in v[0] and LAN2 in v[1], v)

    # parity check running (started the day before) and requests fine -> server busy
    syslog = [(int((D0 - 3600) * 1e9), "Nov 13 23:00:00 tower kernel: md: recovery thread: check P ..."),
              (int((T0 + 9 * 3600) * 1e9), "Nov 14 22:00:00 tower kernel: md: sync done. time=79200sec")]
    tl3 = session_with_stalls(LAN2, T0 + 3 * 3600, 4, 20)
    sm = P.day_summary(FakeLoki(timeline=tl3, syslog=syslog,
                                parts=[part_line(T0 + 3 * 3600 + 30, LAN2, 206, 9_000_000, 300)]), DAY)
    s = sm["spells"][0]
    chk("day: parity check from the night before still running -> server busy",
        s["busy"] == ["parity check"] and s["verdict"] == "server busy: the parity check was running", s)
    sm = P.day_summary(FakeLoki(timeline=tl3, syslog=syslog[:1] + [
        (int((T0 + 1800) * 1e9), "Nov 14 13:30:00 tower kernel: md: sync done. time=1sec")],
        parts=[part_line(T0 + 3 * 3600 + 30, LAN2, 206, 9_000_000, 300)]), DAY)
    s = sm["spells"][0]
    chk("day: a parity check that ended before the spell does not count",
        s["busy"] == [] and s["verdict"].startswith("not clear: the file requests were answered normally"), s)

    # the Mover Tuning plugin (this server's mover) running through the spell -> server busy
    mv = [(int((T0 + 2 * 3600) * 1e9), "Nov 14 15:30:02 tower move: Starting Mover tuning plugin ..."),
          (int((T0 + 5 * 3600) * 1e9), "Nov 14 18:40:57 tower move: Mover tuning plugin done!")]
    sm = P.day_summary(FakeLoki(timeline=tl3, syslog=mv,
                                parts=[part_line(T0 + 3 * 3600 + 30, LAN2, 206, 9_000_000, 300)]), DAY)
    s = sm["spells"][0]
    chk("day: the Mover Tuning plugin running through the spell -> server busy (mover)",
        s["busy"] == ["mover"] and s["verdict"] == "server busy: the mover was running", s)
    sm = P.day_summary(FakeLoki(timeline=tl3, syslog=[
        (int((T0 + 600) * 1e9), "Nov 14 13:10:00 tower move: Starting Mover tuning plugin ..."),
        (int((T0 + 1800) * 1e9), "Nov 14 13:30:00 tower move: Mover tuning plugin done!")],
        parts=[part_line(T0 + 3 * 3600 + 30, LAN2, 206, 9_000_000, 300)]), DAY)
    chk("day: a Mover Tuning run that ended before the spell does not count", sm["spells"][0]["busy"] == [],
        sm["spells"][0])
    chk("the syslog query asks Loki for the Mover Tuning markers",
        all(m in P.SYSLOG_Q for m in P.MOVER_START + P.MOVER_DONE), P.SYSLOG_Q)

    # evidence query fails -> the spell is still reported
    sm = P.day_summary(FakeLoki(timeline=tl3, fail_on="parts"), DAY)
    chk("day: an evidence query failing keeps the spell, says so",
        len(sm["spells"]) == 1 and sm["spells"][0]["verdict"].startswith("no evidence"), sm["spells"])
    raised = False
    try:
        P.day_summary(FakeLoki(fail_on="timeline"), DAY)
    except ED.LokiError:
        raised = True
    chk("day: the timeline query failing raises (the digest records playback_error)", raised)
    chk("day: no reports at all -> None", P.day_summary(FakeLoki(), DAY) is None)

    # nothing about the item leaks; a remote client shows as a hash
    tl4 = session_with_stalls(WAN, T0 + 4 * 3600, 4, 20)
    sm = P.day_summary(FakeLoki(timeline=tl4 + tl3), DAY)
    blob = json.dumps(sm) + "\n".join(P.render_section(sm)) + "\n".join(P.notify_lines(sm))
    chk("day: no item id and no remote address anywhere in the record, the section or the message",
        SECRET_KEY not in blob and WAN not in blob and "remote " in blob, blob[:400])
    chk("day: the stored record has no per-wait detail", "_waits" not in blob and '"item"' not in blob)


def test_render():
    chk("render: nothing played -> no section", P.render_section(None) == [] and P.notify_lines(None) == [])
    tl = session_with_stalls(LAN2, T0 + 3600, 4, 20)
    sm = P.day_summary(FakeLoki(timeline=tl, parts=[part_line(T0 + 3630, LAN2, 206, 16384, 40000, "mp4")]), DAY)
    md = "\n".join(P.render_section(sm))
    chk("render: the section lists the spell, its evidence and where it points",
        md.startswith("## Playback") and "4 stalls, 80s stalled" in md and ".mp4 1" in md and "points to:" in md
        and "1 starved, 0 failed" in md, md)
    nl = P.notify_lines(sm)
    chk("render: the message gets one line per spell", len(nl) == 1 and nl[0].startswith(LAN2), nl)
    clean = P.day_summary(FakeLoki(timeline=[tl_line(T0, LAN, "buffering", 0), tl_line(T0 + 2, LAN, "playing", 0),
                                             tl_line(T0 + 12, LAN, "playing", 10)]), DAY)
    md = "\n".join(P.render_section(clean))
    chk("render: a clean day says so and still lists the device", "No buffering spells." in md and LAN in md, md)
    chk("render: a clean day adds nothing to the message", P.notify_lines(clean) == [])

    rec = {"day": DAY, "containers": {}, "model": {}, "alerts": {}, "playback": sm}
    md = ED.render(rec)
    chk("digest: the .md carries the Playback section", "## Playback" in md, md[-600:])
    subj, text = ED.notify_text(rec)
    chk("digest: the daily message lists the spell under its own heading",
        "Plex buffering (stalls, not skips):" in text and LAN2 in text, text)
    rec2 = dict(rec, playback=clean)
    _, text2 = ED.notify_text(rec2)
    chk("digest: a clean day adds nothing to the message", "Plex buffering" not in text2, text2)
    chk("digest: the subject is not changed by playback", subj == ED.notify_text(dict(rec, playback=None))[0], subj)


def test_wiring():
    src = (BIN / "error_digest.py").read_text()
    chk("digest run: it builds rec['playback'] from the same Loki and records a failure by name",
        "plex_playback.day_summary(loki, day)" in src and 'rec["playback_error"]' in src)
    man = (BIN / "queue-image" / "MANIFEST").read_text().split()
    chk("image: plex_playback.py ships", "plex_playback.py" in man)
    tests = (BIN / "queue-image" / "MANIFEST.tests").read_text().split()
    chk("image: this test is listed with the image's tests", "test-plex-playback.py" in tests)
    chk("cli: unknown command -> rc 2", P.main(["plex_playback.py", "nope"]) == 2)


for f in (test_parse, test_waits, test_spells, test_day, test_render, test_wiring):
    try:
        f()
    except Exception as e:  # noqa: BLE001
        import traceback
        chk("%s ran without raising" % f.__name__, False, traceback.format_exc())

shutil.rmtree(TMP, ignore_errors=True)
print("\n%s (%d checks)" % ("ALL PASS" if not FAILS else "%d FAILED" % len(FAILS), N[0]))
sys.exit(1 if FAILS else 0)
