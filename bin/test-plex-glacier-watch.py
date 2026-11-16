#!/usr/bin/env python3
"""Tests for plex_glacier_watch.py (Plex playback -> plex-busy flag for the Glacier mover), 2026-11-15.

Fake Loki object and fake notify poster; temp dirs only. No network, no queue.
  python3 test-plex-glacier-watch.py      prints ALL PASS / N FAILED (rc 0 / 1)
"""
import datetime as dt
import io
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import contextlib
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
TMP = Path(tempfile.mkdtemp(prefix="pgw-test-"))
os.environ["GLACIER_SHARE_DIR"] = str(TMP / "share")
os.environ["PLEX_GLACIER_DIR"] = str(TMP / "data")
os.environ["ERROR_DIGEST_DIR"] = str(TMP / "digest")
os.environ.pop("LOKI_TOKEN", None)
import plex_glacier_watch as W  # noqa: E402
import error_digest as ED  # noqa: E402

FAILS, N = [], [0]


def chk(name, cond, detail=""):
    N[0] += 1
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "\n     " + str(detail)[-1500:]))
    if not cond:
        FAILS.append(name)


def vec(**states):
    return {"data": {"resultType": "vector",
                     "result": [{"metric": {"state": k}, "value": [0, str(v)]} for k, v in states.items()]}}


class FakeLoki:
    def __init__(self, answer=None, exc=None):
        self.answer, self.exc, self.calls = answer, exc, []

    def get(self, path, **params):
        self.calls.append((path, params))
        if self.exc:
            raise self.exc
        return self.answer


class Poster:
    def __init__(self, ret=None):
        self.ret = {"channel": "pushover"} if ret is None else ret
        self.sent = []

    def __call__(self, path, body):
        self.sent.append((path, body))
        return self.ret or None


NOW = dt.datetime(2026, 11, 15, 23, 10).timestamp()     # a night tick (local time)
SHARE, DATA = Path(os.environ["GLACIER_SHARE_DIR"]), Path(os.environ["PLEX_GLACIER_DIR"])
BUSY = SHARE / "plex-busy"
STATUS = SHARE / "glacier-status"


def reset():
    for d in (SHARE, DATA, Path(os.environ["ERROR_DIGEST_DIR"])):
        shutil.rmtree(d, ignore_errors=True)


def run_tick(loki, now=NOW, post=None):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        s = W.tick(now=now, loki=loki, post=post or Poster(), gpu=lambda busy, say=print: {})
    return s, out.getvalue()


def status(t, state, detail="disk1 -> disk2"):
    SHARE.mkdir(parents=True, exist_ok=True)
    STATUS.write_text("%d %s %s\n" % (t, state, detail))


# ------------------------------------------------------------------------------------------------ parsing
c = W.parse_counts(vec(playing=12, buffering=3, foo=9))
chk("parse_counts: known states counted, unknown ignored", c == {"playing": 12, "buffering": 3, "paused": 0, "stopped": 0}, c)
bad = {"data": {"result": [{"metric": {"state": "playing"}, "value": [0, "x"]}, {"metric": {}}]}}
chk("parse_counts: bad values and missing labels ignored", W.parse_counts(bad)["playing"] == 0)
chk("parse_counts: None / empty answer -> zeros", W.parse_counts(None) == dict.fromkeys(W.STATES, 0))
groups = re.findall(r"\(\?P<(\w+)>", W.TIMELINE_Q)
chk("Loki query extracts only the state word (no client ids, no titles)", groups == ["state"], groups)
fl = FakeLoki(vec(playing=1))
W.plex_counts(fl, NOW)
p = fl.calls[0][1]
chk("plex_counts: instant query at the tick time over the 5-min window",
    fl.calls[0][0] == "/loki/api/v1/query" and p["time"] == str(int(NOW * 1e9)) and "[300s]" in p["query"], fl.calls)

reset()
chk("read_status: missing file -> None", W.read_status(STATUS, NOW) is None)
status(NOW - 60, "moving")
r = W.read_status(STATUS, NOW)
chk("read_status: fresh moving line -> active", r and r["state"] == "moving" and r["active"] and r["t"] == int(NOW - 60), r)
status(NOW - 4 * 3600, "moving")
chk("read_status: moving line older than 3 h -> not active (run died)", not W.read_status(STATUS, NOW)["active"])
status(NOW - 60, "done", "moved=4")
chk("read_status: done -> not active", not W.read_status(STATUS, NOW)["active"])
status(NOW + 3600, "moving")
chk("read_status: future timestamp -> not active", not W.read_status(STATUS, NOW)["active"])
STATUS.write_text("garbage\n")
chk("read_status: not our format -> None", W.read_status(STATUS, NOW) is None)
STATUS.write_text("1700000000 start phases=evict\n1700000100 YIELD plex\n")
chk("read_status: last line wins, state lower-cased", W.read_status(STATUS, 1700000200)["state"] == "yield")

# ------------------------------------------------------------------------------------------------ step()
cnt = lambda **k: dict(dict.fromkeys(W.STATES, 0), **k)
a, s = W.step(NOW, cnt(playing=5), None, None)
chk("step: playback, no flag -> flag since now", a == {"since": int(NOW), "seen": int(NOW), "play": 5, "buf": 0} and s["busy"], a)
b0 = {"since": int(NOW) - 900, "seen": int(NOW) - 120}
a, s = W.step(NOW, cnt(buffering=2), b0, None)
chk("step: continued playback keeps 'since', moves 'seen'", a["since"] == b0["since"] and a["seen"] == int(NOW), a)
a, s = W.step(NOW, cnt(paused=7, stopped=1), None, None)
chk("step: paused/stopped only is not playback", a is None and not s["busy"], (a, s))
a, s = W.step(NOW, cnt(), {"since": 1, "seen": int(NOW) - 100}, None)
chk("step: no playback, seen 100 s ago -> flag kept", a is not None and s["busy"])
a, s = W.step(NOW, cnt(), {"since": 1, "seen": int(NOW) - W.BUSY_CLEAR_S}, None)
chk("step: no playback for BUSY_CLEAR_S -> flag removed", a is None and not s["busy"])
b1 = {"since": 1, "seen": 2}
a, s = W.step(NOW, None, b1, None)
chk("step: Loki failed -> flag left exactly as is, sample says so", a is b1 and s.get("err") == "loki", s)
a, s = W.step(NOW, None, None, None)
chk("step: Loki failed, no flag -> nothing written", a is None and s.get("err") == "loki")

since = int(NOW) - 1200
busy = {"since": since, "seen": int(NOW) - 120}
st = lambda t, state="moving": {"t": int(t), "state": state, "detail": "", "active": True}
_, s = W.step(NOW, cnt(playing=20, buffering=4), busy, st(since - 60))
chk("step: buffering during a unit started before the flag -> contention", s.get("ev") == "contention", s)
_, s = W.step(NOW, cnt(playing=20, buffering=4), busy, st(since + W.GRACE_S - 1))
chk("step: unit started inside the grace period -> still contention", s.get("ev") == "contention", s)
_, s = W.step(NOW, cnt(playing=20, buffering=4), busy, st(since + W.GRACE_S + 5))
chk("step: unit started after flag + grace and Plex buffered -> ignored", s.get("ev") == "ignored", s)
_, s = W.step(NOW, cnt(playing=20), busy, st(since + 600))
chk("step: no buffering -> no event even when glacier moves", "ev" not in s, s)
_, s = W.step(NOW, cnt(playing=20, buffering=4), busy, st(since + 600, "yield"))
chk("step: glacier yielding -> buffering is not glacier's (no event)", "ev" not in s, s)
_, s = W.step(NOW, cnt(playing=20, buffering=4), busy, dict(st(since + 600), active=False))
chk("step: dead run's old status -> no event", "ev" not in s and s.get("g") == "moving-old", s)
_, s = W.step(NOW, cnt(buffering=3), None, st(NOW - 30))
chk("step: playback just began mid-unit -> contention, not ignored", s.get("ev") == "contention", s)
allowed = {"t", "busy", "g", "err", "play", "buf", "pause", "ev", "notice"}
chk("step: samples hold counts and states only", set(s) <= allowed, s)

# ------------------------------------------------------------------------------------------------ tick()
reset()
s, out = run_tick(FakeLoki(vec(playing=30)))
chk("tick: playback -> plex-busy written", BUSY.exists() and json.loads(BUSY.read_text())["since"] == int(NOW), out)
chk("tick: plex-busy is world-readable (glacier runs as root on the host, but keep it plain)",
    stat.S_IMODE(BUSY.stat().st_mode) == 0o644, oct(BUSY.stat().st_mode))
chk("tick: says 'set' once", "plex-busy set" in out, out)
day = dt.datetime.fromtimestamp(NOW).date().isoformat()
lines = (DATA / "samples" / (day + ".jsonl")).read_text().splitlines()
chk("tick: one sample appended for the day", len(lines) == 1 and json.loads(lines[0])["play"] == 30, lines)
s, out = run_tick(FakeLoki(vec(playing=30)), now=NOW + 120)
chk("tick: steady playback -> no log line, 'since' unchanged",
    out == "" and json.loads(BUSY.read_text())["since"] == int(NOW), out)
m0 = BUSY.stat().st_mtime_ns
s, out = run_tick(FakeLoki(exc=ED.LokiError("Loki unreachable (URLError)")), now=NOW + 240)
chk("tick: Loki down -> flag not touched, sample records it, no exception",
    BUSY.stat().st_mtime_ns == m0 and s.get("err") == "loki" and "no Plex counts" in out, (s, out))
s, out = run_tick(FakeLoki(vec()), now=NOW + 300)
chk("tick: playback just ended -> flag kept for now", BUSY.exists())
s, out = run_tick(FakeLoki(vec()), now=NOW + 240 + W.BUSY_CLEAR_S + 1)
chk("tick: BUSY_CLEAR_S after the last sighting -> flag removed", not BUSY.exists() and "cleared" in out, out)

reset()
SHARE.mkdir(parents=True)
BUSY.write_text("not json")
s, out = run_tick(FakeLoki(vec(playing=2)))
chk("tick: unreadable flag is rewritten as valid JSON", json.loads(BUSY.read_text())["seen"] == int(NOW))

reset()
SHARE.mkdir(parents=True)
BUSY.write_text(json.dumps({"since": int(NOW) - 1800, "seen": int(NOW) - 120}))
status(NOW - 300, "moving")
post = Poster()
s, out = run_tick(FakeLoki(vec(playing=25, buffering=6)), post=post)
chk("tick: glacier ignored the flag and Plex buffered -> one notice", len(post.sent) == 1 and s.get("notice") is True, post.sent)
path, body = post.sent[0]
chk("notice: through the agent's /v1/notify, one per night",
    path == "/v1/notify" and body["dedupe_key"] == "plex-glacier-ignored:2026-11-15" and body["dedupe_s"] == 86400, body)
chk("notice: fits the agent's limits (title <=120, body <=1000, no newlines)",
    len(body["title"]) <= 120 and len(body["body"]) <= 1000 and "\n" not in body["title"] + body["body"], body)
chk("notice: says nothing was stopped", "Nothing was stopped" in body["body"])
s, out = run_tick(FakeLoki(vec(playing=25, buffering=6)), now=NOW + 120, post=Poster(ret={}))
chk("tick: agent did not answer -> sample notice False", s.get("notice") is False, s)
status(NOW - 2400, "moving")
post = Poster()
s, out = run_tick(FakeLoki(vec(playing=25, buffering=6)), now=NOW + 240, post=post)
chk("tick: buffering in a unit begun before the flag -> counted, no notice", not post.sent and s.get("ev") == "contention", s)

chk("night_of: 01:30 belongs to the previous evening", W.night_of(dt.datetime(2026, 11, 16, 1, 30).timestamp()) == "2026-11-15")
chk("night_of: 23:00 belongs to the same evening", W.night_of(dt.datetime(2026, 11, 15, 23, 0).timestamp()) == "2026-11-15")

# ------------------------------------------------------------------------------------------------ summary
reset()
d = DATA / "samples"
d.mkdir(parents=True)
t0 = int(dt.datetime(2026, 11, 14, 22, 0).timestamp())
rows = [{"t": t0, "busy": False, "play": 0, "buf": 0, "pause": 0, "g": "moving"}]
rows += [{"t": t0 + 120 * k, "busy": True, "play": 20, "buf": 3, "pause": 0, "g": "moving", "ev": "contention"} for k in range(1, 6)]
rows += [{"t": t0 + 120 * k, "busy": True, "play": 20, "buf": 0, "pause": 0, "g": "yield"} for k in range(6, 16)]
rows += [{"t": t0 + 120 * 16, "busy": True, "play": 20, "buf": 5, "pause": 0, "g": "moving", "ev": "ignored", "notice": True}]
rows += [{"t": t0 + 120 * 17, "busy": True, "err": "loki", "g": "moving"}]
rows += [{"t": t0 + 120 * 17 + 7200, "busy": False, "play": 0, "buf": 0, "pause": 0, "g": "done-old"}]
(d / "2026-11-14.jsonl").write_text("\n".join(json.dumps(r) for r in rows) + "\nnot json\n")
sm = W.day_summary("2026-11-14")
chk("summary: buffering totals, split by glacier moving", sm["buf"] == 20 and sm["buf_moving"] == 20, sm)
chk("summary: minutes moving / waiting / playing", (sm["moving_min"], sm["yield_min"], sm["play_min"]) == (14, 20, 32), sm)
chk("summary: a 2-h gap counts at most 2.5 ticks", sm["busy_min"] == 32, sm)
chk("summary: events, notices, Loki errors", (sm["contention"], sm["ignored"], sm["notices"], sm["loki_errors"]) == (5, 1, 1, 1), sm)
txt = "\n".join(W.render_section(sm))
chk("render: headline numbers", "## Plex and Glacier" in txt and "buffering reports: 20 (20 while Glacier" in txt and "32 min" in txt, txt)
chk("render: the ignored case is bold with the notice", "**Glacier started copying while Plex was busy" in txt and "notice sent" in txt, txt)
chk("render: Loki gaps explained", "Loki did not answer on 1 of" in txt, txt)
(d / "2026-11-13.jsonl").write_text(json.dumps({"t": t0 - 86400, "busy": False, "play": 4, "buf": 0, "pause": 0}) + "\n")
txt = "\n".join(W.render_section(W.day_summary("2026-11-13")))
chk("render: no glacier-status seen -> says so (old script or no run)", "no glacier-status file seen" in txt, txt)
chk("summary: day without samples -> None, section empty", W.day_summary("2026-11-01") is None and W.render_section(None) == [])
chk("_hm: hours and minutes", W._hm(135) == "2 h 15 min" and W._hm(7) == "7 min")

# ------------------------------------------------------------------------------------------------ digest hook
rec = {"day": "2026-11-14", "containers": {}, "baseline": False}
plain = ED.render(rec)
chk("digest: no section without samples", "Plex and Glacier" not in plain)
rec["plex_glacier"] = sm
md = ED.render(rec)
chk("digest: section rendered before the container table",
    "## Plex and Glacier" in md and md.index("## Plex and Glacier") < md.index("## By container"), md)

os.environ["LOKI_URL"] = "http://127.0.0.1:9"
orig = (ED.build_day, ED.model_items)
ED.build_day = lambda loki, day, ddir, rules, say=print: {"day": day, "containers": {}, "baseline": False}
ED.model_items = lambda rec, per_container=8: {}
try:
    with contextlib.redirect_stdout(io.StringIO()):
        rc = ED.cmd_run(["--day", "2026-11-14", "--no-model"])
    out = (Path(os.environ["ERROR_DIGEST_DIR"]) / "digests" / "2026-11-14.md").read_text()
    js = json.loads((Path(os.environ["ERROR_DIGEST_DIR"]) / "digests" / "2026-11-14.json").read_text())
    chk("digest run: the day's summary is stored in the JSON and shown in the .md",
        rc == 0 and js.get("plex_glacier", {}).get("ignored") == 1 and "## Plex and Glacier" in out, (rc, out[:400]))
    with contextlib.redirect_stdout(io.StringIO()):
        ED.cmd_run(["--day", "2026-11-12", "--no-model"])
    js = json.loads((Path(os.environ["ERROR_DIGEST_DIR"]) / "digests" / "2026-11-12.json").read_text())
    chk("digest run: no samples -> no key", "plex_glacier" not in js, js)
finally:
    ED.build_day, ED.model_items = orig

buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    rc = W.main(["show", "--day", "2026-11-14"])
chk("cli show: prints the section", rc == 0 and "## Plex and Glacier" in buf.getvalue())
with contextlib.redirect_stdout(io.StringIO()):
    chk("cli: unknown command -> rc 2", W.main(["nope"]) == 2)

# ------------------------------------------------------------------------------------------------ GPU yield
class Ollama:
    def __init__(self, models=("qwen3:14b",), ps_fails=False, unload_fails=()):
        self.models, self.ps_fails, self.unload_fails, self.calls = list(models), ps_fails, set(unload_fails), []

    def __call__(self, url, body=None, timeout=15):
        self.calls.append((url, body))
        if url.endswith("/api/ps"):
            if self.ps_fails:
                raise OSError("down")
            return {"models": [{"name": m} for m in self.models]}
        if body and body.get("model") in self.unload_fails:
            raise OSError("refused")
        return {}


BUSYD = {"since": int(NOW), "seen": int(NOW)}
idle_state = {"jobs": [{"id": "a1", "status": "done", "lane": "unraid"},
                       {"id": "b2", "status": "running", "lane": "studio-db"}]}
busy_state = {"jobs": [{"id": "c3", "status": "running", "lane": "unraid"}]}
q = lambda **k: W.gpu_yield(**{"url": "http://ollama:11434", "say": lambda m: None, **k})
o = Ollama(("qwen3:14b", "gemma3:12b"))
r = q(busy=BUSYD, state=idle_state, http=o)
chk("gpu: Plex playing, no Unraid job -> every resident model unloaded with keep_alive 0",
    r == {"unl": ["qwen3:14b", "gemma3:12b"]} and [b for _, b in o.calls if b] ==
    [{"model": "qwen3:14b", "keep_alive": 0}, {"model": "gemma3:12b", "keep_alive": 0}], (r, o.calls))
o = Ollama()
chk("gpu: a running Unraid job is never interrupted -> nothing unloaded",
    q(busy=BUSYD, state=busy_state, http=o) == {"gpu": "job"} and o.calls == [], o.calls)
o = Ollama()
chk("gpu: Plex not playing -> nothing touched", q(busy=None, state=idle_state, http=o) == {} and o.calls == [])
chk("gpu: unreadable queue state -> nothing unloaded", q(busy=BUSYD, state="x", http=o) == {"gpu": "?"} and o.calls == [])
chk("gpu: Ollama unreachable -> nothing unloaded, no exception", q(busy=BUSYD, state=idle_state, http=Ollama(ps_fails=True)) == {"gpu": "?"})
chk("gpu: nothing resident -> quiet", q(busy=BUSYD, state=idle_state, http=Ollama(())) == {})
msgs = []
r = W.gpu_yield(busy=BUSYD, state=idle_state, url="http://o", http=Ollama(("a", "b"), unload_fails={"a"}), say=msgs.append)
chk("gpu: one unload failing -> logged, the other still unloaded", r == {"unl": ["b"]} and any("unload of a failed" in m for m in msgs), (r, msgs))
chk("gpu: no Unraid Ollama URL -> off", q(busy=BUSYD, state=idle_state, http=Ollama(), url="") == {})
os.environ["PLEX_GPU_YIELD"] = "0"
o = Ollama()
chk("gpu: PLEX_GPU_YIELD=0 -> off", q(busy=BUSYD, state=idle_state, http=o) == {} and o.calls == [])
os.environ.pop("PLEX_GPU_YIELD")
os.environ["PLEX_GPU_LANES"] = "unraid,gpu2"
chk("gpu: PLEX_GPU_LANES names more lanes", W.running_on({"jobs": [{"id": "z", "status": "running", "lane": "gpu2"}]}, W.gpu_lanes()) == ["z"])
os.environ.pop("PLEX_GPU_LANES")

reset()
calls = []
def fake_gpu(busy, say=print):
    calls.append(busy)
    return {"unl": ["qwen3:14b"]}
with contextlib.redirect_stdout(io.StringIO()):
    s = W.tick(now=NOW, loki=FakeLoki(vec(playing=5)), post=Poster(), gpu=fake_gpu)
chk("tick: the GPU step gets the new flag and its result lands in the sample",
    calls and calls[0]["since"] == int(NOW) and s.get("unl") == ["qwen3:14b"], (calls, s))
def bad_gpu(busy, say=print):
    raise RuntimeError("boom")
out = io.StringIO()
with contextlib.redirect_stdout(out):
    s = W.tick(now=NOW + 120, loki=FakeLoki(vec(playing=5)), post=Poster(), gpu=bad_gpu)
chk("tick: a failing GPU step is logged and the flag/sample still happen",
    "GPU step failed" in out.getvalue() and BUSY.exists() and s.get("play") == 5, (out.getvalue(), s))

d = DATA / "samples"
d.mkdir(parents=True, exist_ok=True)
t1 = int(dt.datetime(2026, 11, 10, 21, 0).timestamp())
gr = [{"t": t1 + 120 * k, "busy": True, "play": 9, "buf": 0, "pause": 0, "gpu": "job"} for k in range(0, 4)]
gr += [{"t": t1 + 480, "busy": True, "play": 9, "buf": 0, "pause": 0, "unl": ["qwen3:14b", "gemma3:12b"]}]
(d / "2026-11-10.jsonl").write_text("\n".join(json.dumps(r) for r in gr) + "\n")
sm2 = W.day_summary("2026-11-10")
txt = "\n".join(W.render_section(sm2))
chk("summary: unloads counted, minutes a running job kept the GPU", sm2["unloads"] == 2 and sm2["job_min"] == 8, sm2)
chk("render: GPU line", "Unraid GPU for Plex: 2 Ollama model unload(s)" in txt and "about 8 min" in txt, txt)

# ------------------------------------------------------------------------------------------------ queue side
import importlib.util  # noqa: E402
_qp = BIN / "ollama-queue.py"
if _qp.exists():
    _src = _qp.read_text()
    _ns = {}
    _start = _src.index("# --- PLEX GPU YIELD")
    _end = _src.index("def _candidate_lanes(")
    exec("import os, time\nfrom pathlib import Path\n" + _src[_start:_end], _ns)
    pf = SHARE / "plex-busy"
    SHARE.mkdir(parents=True, exist_ok=True)
    pf.write_text("{}")
    m = pf.stat().st_mtime
    chk("queue: fresh plex-busy -> unraid lane skipped with a reason",
        "Plex is playing" in (_ns["plex_gpu_wait"]("unraid", now=m + 60) or ""))
    chk("queue: other lanes (Darkbloom, CPU) never held", _ns["plex_gpu_wait"]("studio-db", now=m + 60) is None)
    chk("queue: flag 10 min old -> stale, unraid launches", _ns["plex_gpu_wait"]("unraid", now=m + 600) is None)
    pf.unlink()
    chk("queue: no flag -> unraid launches", _ns["plex_gpu_wait"]("unraid", now=m) is None)
    chk("queue: launch loop consults it and records wait code plex-gpu",
        "_pgw = plex_gpu_wait(name)" in _src and '"plex-gpu", "Plex is playing' in _src)
    _dm = (BIN / "queue-deadman.py").read_text()
    chk("deadman: a plex-gpu wait is a long hold, not a stall after 15 min", '"plex-gpu": 8 * 60.0' in _dm)

import configparser
import re as _re
_c = configparser.RawConfigParser(strict=True)
_c.read(BIN / "queue-image" / "supervisord.conf")
_progs = [x for x in _c.sections() if x.startswith("program:")]
_wfn = [int(x) for x in _re.findall(r"startswith\('program:'\)\]\)?;?\s*(?:assert len\(p\))?\s*==\s*(\d+)",
                                     (BIN / "queue-image" / "github-workflow-image.yml").read_text())]
chk("image: the watcher is a supervisord program", "program:plex-glacier-watch" in _progs, _progs)
chk("image: both of the build workflow's program counts (static check, image smoke) match supervisord.conf "
    "(11-15: the build failed twice on 10)", len(_wfn) == 2 and set(_wfn) == {len(_progs)}, (_wfn, len(_progs)))

shutil.rmtree(TMP, ignore_errors=True)
print("\n%s (%d checks)" % ("ALL PASS" if not FAILS else "%d FAILED" % len(FAILS), N[0]))
sys.exit(1 if FAILS else 0)
