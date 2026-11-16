#!/usr/bin/env python3
"""plex_glacier_watch -- tell the Glacier disk mover when Plex is streaming, and record how the two got along.

  plex_glacier_watch.py tick             one pass (queue container: supervisord `every.sh 120`)
  plex_glacier_watch.py show [--day D]   that day's summary (default today), as the digest prints it

Why (the operator 2026-11-15): Glacier moves (plex-automation tiering/glacier-mover.sh: rsync copy, sha256 verify, delete)
made Plex buffer. On 11-15 13:00-14:20 one stream had 10-30% of its timeline reports in "buffering" while disk1
was being emptied; outside moves it is 2% or less.

One tick (reads Loki and two small files; it never restarts, rescans, signals or kills anything):
  1. counts the Plex "timeline state" reports of the last 5 min (Plex Media Server log at DEBUG, Loki
     {job="files",app="plex"}); only the state word is read, never client ids or titles.
  2. playing or buffering seen -> (re)write <share>/plex-busy; nothing seen for BUSY_CLEAR_S after the last
     sighting -> remove it. glacier-mover.sh checks plex-busy before each unit and waits while it is fresh (mtime
     under 10 min). A stale or missing file means go: a stopped watcher or a Loki outage never stalls the mover.
  3. reads <share>/glacier-status, one line "<epoch> <state> <detail>" written by glacier-mover.sh
     (state: start | moving | yield | done | stopped | failed).
  3b. Plex gets the GPU (the operator 2026-11-15, "Plex should get the GPU over ollama"): while plex-busy is up,
     ollama-queue.py starts no new job on the Unraid GPU lane (plex_gpu_wait, wait code plex-gpu), and this tick
     unloads the Unraid Ollama's resident models (keep_alive 0) once no queue job runs there. A running job is
     never stopped. PLEX_GPU_YIELD=0 turns both off; PLEX_GPU_LANES (default unraid) names the lanes.
  4. appends one sample (counts, flag, Glacier state, unloads; no names) to <data>/samples/<day>.jsonl (35 days).
  5. Plex buffered while Glacier was copying a unit it STARTED after plex-busy had been up for GRACE_S (so it
     did not wait as agreed): one notice to the operator per night through the Mac agent. Buffering during a unit that was
     already running when playback began is expected and only counted.

<share> = GLACIER_SHARE_DIR or ~/state/glacier (host: appdata/ollama-queue/state/glacier, an existing bind mount).
<data>  = PLEX_GLACIER_DIR or <dispatch dir>/plex-glacier. Loki settings as error_digest.py (LOKI_URL, LOKI_TOKEN).
The nightly error digest adds a "Plex and Glacier" section from the samples (day_summary, render_section).
"""
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

HERE = os.path.dirname(os.path.realpath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

WINDOW_S = 300            # look-back of one tick's Loki count
TICK_S = 120              # supervisord interval; samples further apart than 2.5 ticks count as a gap
BUSY_CLEAR_S = 300        # remove plex-busy this long after the last tick that saw playback
GLACIER_STALE_S = 600     # glacier-mover.sh ignores a plex-busy older than this (documented here, enforced there)
GRACE_S = 180             # a unit started this long after plex-busy appeared should not have started
STATUS_MAX_AGE_S = 3 * 3600   # a "moving" line older than this is a run that died without writing its exit line
KEEP_DAYS = 35
STATES = ("playing", "buffering", "paused", "stopped")
ACTIVE = ("start", "moving", "yield")
TIMELINE_Q = ('sum by (state) (count_over_time({job="files",app="plex"} |~ "reporting timeline state" '
              '| regexp "reporting timeline state (?P<state>[a-z]+)" [%ds]))')


# ---------------------------------------------------------------------------------------------------- paths
def share_dir():
    return Path(os.environ.get("GLACIER_SHARE_DIR") or Path.home() / "state" / "glacier").expanduser()


def data_dir():
    p = os.environ.get("PLEX_GLACIER_DIR")
    if p:
        return Path(p).expanduser()
    try:
        import queue_config
        return Path(queue_config.dispatch_dir()) / "plex-glacier"
    except Exception:
        return Path.home() / ".ollama-dispatch" / "plex-glacier"


# ---------------------------------------------------------------------------------------------------- inputs
def parse_counts(data):
    """Loki vector answer -> {state: count} for the four timeline states (others ignored)."""
    out = dict.fromkeys(STATES, 0)
    for r in ((data or {}).get("data") or {}).get("result") or []:
        st = (r.get("metric") or {}).get("state")
        if st in out:
            try:
                out[st] += int(float(r["value"][1]))
            except (KeyError, IndexError, TypeError, ValueError):
                pass
    return out


def plex_counts(loki, at, window=WINDOW_S):
    """{state: count} of Plex timeline reports in (at - window, at]. Raises error_digest.LokiError."""
    return parse_counts(loki.get("/loki/api/v1/query", query=TIMELINE_Q % int(window), time=str(int(at * 1e9))))


def read_status(path, now):
    """glacier-status -> {"t", "state", "detail", "active"} or None (missing / unreadable / not our format)."""
    try:
        line = Path(path).read_text(errors="replace").strip().splitlines()[-1]
        t, state, *rest = line.split(None, 2)
        t = int(float(t))
    except (OSError, IndexError, ValueError):
        return None
    state = state.lower()
    active = state in ACTIVE and 0 <= now - t <= STATUS_MAX_AGE_S
    return {"t": t, "state": state, "detail": (rest[0] if rest else "")[:120], "active": active}


def read_busy(path):
    try:
        d = json.loads(Path(path).read_text())
        return d if isinstance(d, dict) and isinstance(d.get("since"), (int, float)) else None
    except (OSError, ValueError):
        return None


# ---------------------------------------------------------------------------------------------------- decision
def step(now, counts, busy, status):
    """Pure decision for one tick.
    counts: {state: n} or None (Loki failed); busy: current plex-busy content or None; status: read_status() or None.
    -> (busy_after, sample). busy_after is the dict to write, None to remove, or busy itself to leave as is."""
    sample = {"t": int(now), "busy": busy is not None}
    if status:
        sample["g"] = status["state"] if status["active"] else (status["state"] + "-old")
    if counts is None:
        sample["err"] = "loki"
        return busy, sample               # leave it: a fresh flag goes stale by itself, glacier then goes on
    play, buf = counts.get("playing", 0), counts.get("buffering", 0)
    sample.update(play=play, buf=buf, pause=counts.get("paused", 0))
    if play or buf:
        after = {"since": int(busy["since"]) if busy else int(now), "seen": int(now), "play": play, "buf": buf}
    elif busy and now - float(busy.get("seen", busy["since"])) >= BUSY_CLEAR_S:
        after = None
    else:
        after = busy
    sample["busy"] = after is not None
    if buf and status and status["active"] and status["state"] == "moving":
        if busy and status["t"] >= busy["since"] + GRACE_S:
            sample["ev"] = "ignored"      # glacier began this unit well after the flag was up: it did not wait
        else:
            sample["ev"] = "contention"   # the unit was already running when playback began: expected, counted
    return after, sample


# ---------------------------------------------------------------------------------------------------- effects
def _write_atomic(path, text, mode=0o644):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text)
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def apply_busy(path, before, after):
    if after is None:
        if before is not None:
            try:
                Path(path).unlink()
            except FileNotFoundError:
                pass
    elif after is not before:
        _write_atomic(path, json.dumps(after) + "\n")


def append_sample(ddir, sample, now):
    d = Path(ddir) / "samples"
    d.mkdir(parents=True, exist_ok=True)
    day = dt.datetime.fromtimestamp(now).date().isoformat()
    with open(d / (day + ".jsonl"), "a") as f:
        f.write(json.dumps(sample, separators=(",", ":")) + "\n")
    if dt.datetime.fromtimestamp(now).minute < 3:       # prune about once an hour
        cut = (dt.date.fromisoformat(day) - dt.timedelta(days=KEEP_DAYS)).isoformat()
        for p in d.glob("*.jsonl"):
            if p.stem < cut:
                try:
                    p.unlink()
                except OSError:
                    pass


def night_of(now):
    """The night a moment belongs to: before noon counts as the previous evening's night."""
    t = dt.datetime.fromtimestamp(now)
    return (t.date() - dt.timedelta(days=1 if t.hour < 12 else 0)).isoformat()


def send_notice(now, sample, post=None):
    """One notice per night when glacier started a unit while plex-busy was up and Plex buffered. -> True if sent."""
    title = "Glacier did not wait for Plex"
    body = ("Plex buffered while Glacier copied a unit it started after the plex-busy flag was up (%d buffering "
            "reports in 5 min). The installed glacier-mover.sh may predate the plex-busy check: recopy it from "
            "plex-automation. Nothing was stopped." % sample.get("buf", 0))
    msg = {"title": title, "body": body, "dedupe_key": "plex-glacier-ignored:" + night_of(now), "dedupe_s": 86400}
    if post is None:
        import queue_config
        post = queue_config.agent_post
    return post("/v1/notify", msg) is not None


# ---------------------------------------------------------------------------------------------------- GPU yield
# The operator 2026-11-15: "Plex should get the GPU over ollama". While plex-busy is up, ollama-queue.py starts no new job
# on PLEX_GPU_LANES (plex_gpu_wait) and this unloads whatever the Unraid Ollama still holds once no queue job runs
# there. A running job is never stopped; its model goes on the first tick after it ends.
def gpu_yield_on():
    return os.environ.get("PLEX_GPU_YIELD", "1").strip().lower() not in ("0", "off", "false", "no")


def gpu_lanes():
    return tuple(x.strip() for x in os.environ.get("PLEX_GPU_LANES", "unraid").split(",") if x.strip())


def ollama_url():
    import queue_config
    return queue_config.get_str("ollama.unraid_url", "", env="OLLAMA_UNRAID_URL").rstrip("/")


def running_on(state, lanes):
    """Ids of queue jobs running on `lanes`, or None when the state could not be read."""
    if not isinstance(state, dict):
        return None
    return [j.get("id") for j in state.get("jobs") or []
            if isinstance(j, dict) and j.get("status") == "running" and j.get("lane") in lanes]


def _http_json(url, body=None, timeout=15):
    import urllib.request
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method="POST" if body is not None else "GET",
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read()
    return json.loads(raw) if raw.strip() else {}


def gpu_yield(busy, state=None, url=None, http=None, say=print):
    """While Plex is playing, unload the Unraid Ollama's resident models when no queue job runs there.
    -> sample fields: {} when idle or off; {"gpu": "job"} while a job finishes; {"gpu": "?"} when the queue state
    or Ollama cannot be read (nothing unloaded); {"unl": [names]} after asking Ollama to unload them."""
    if busy is None or not gpu_yield_on():
        return {}
    url = ollama_url() if url is None else url
    if not url:
        return {}
    if state is None:
        import queue_config
        state = queue_config.load_queue_state()
    running = running_on(state, gpu_lanes())
    if running is None:
        return {"gpu": "?"}
    if running:
        return {"gpu": "job"}
    http = http or _http_json
    try:
        names = [m.get("name") or m.get("model") for m in (http(url + "/api/ps").get("models") or [])]
    except Exception:
        return {"gpu": "?"}
    unl = []
    for n in names:
        if not n:
            continue
        try:   # keep_alive 0: Ollama unloads it once any request still using it finishes
            http(url + "/api/generate", {"model": n, "keep_alive": 0}, timeout=60)
            unl.append(n)
        except Exception as e:
            say("plex-glacier-watch: unload of %s failed (%s)" % (n, type(e).__name__))
    if unl:
        say("plex-glacier-watch: Plex is playing; unloaded %s from the Unraid GPU" % ", ".join(unl))
        return {"unl": unl}
    return {}


def tick(now=None, loki=None, post=None, say=print, gpu=None):
    now = time.time() if now is None else now
    share, ddir = share_dir(), data_dir()
    share.mkdir(parents=True, exist_ok=True)
    busy_path = share / "plex-busy"
    busy = read_busy(busy_path)
    if busy is None and busy_path.exists():
        busy = {"since": int(busy_path.stat().st_mtime), "seen": int(busy_path.stat().st_mtime)}  # unreadable: rewrite
    status = read_status(share / "glacier-status", now)
    counts = None
    try:
        if loki is None:
            import error_digest
            loki = error_digest.Loki(*error_digest.loki_settings(), timeout=30)
        counts = plex_counts(loki, now)
    except Exception as e:                 # LokiError, no URL: logged, flag left to go stale
        say("plex-glacier-watch: no Plex counts (%s)" % (str(e)[:160] or type(e).__name__))
    after, sample = step(now, counts, busy, status)
    apply_busy(busy_path, busy, after)
    try:
        sample.update((gpu or gpu_yield)(after, say=say))
    except Exception as e:                 # never let the GPU step stop the flag or the sample
        say("plex-glacier-watch: GPU step failed (%s)" % type(e).__name__)
    if sample.get("ev") == "ignored":
        sample["notice"] = bool(send_notice(now, sample, post))
    append_sample(ddir, sample, now)
    if (after is None) != (busy is None):
        say("plex-glacier-watch: plex-busy %s" % ("set" if after else "cleared"))
    return sample


# ---------------------------------------------------------------------------------------------------- summary
def load_samples(day, ddir=None):
    p = Path(ddir or data_dir()) / "samples" / (day + ".jsonl")
    out = []
    try:
        for line in p.read_text().splitlines():
            try:
                s = json.loads(line)
            except ValueError:
                continue
            if isinstance(s, dict) and isinstance(s.get("t"), (int, float)):
                out.append(s)
    except OSError:
        pass
    return sorted(out, key=lambda s: s["t"])


def day_summary(day, ddir=None):
    """Totals for one calendar day from its samples, or None when there are none. Minutes are estimates: each
    sample stands for the time since the previous one, at most 2.5 ticks."""
    ss = load_samples(day, ddir)
    if not ss:
        return None
    cap = TICK_S * 2.5
    sm = {"samples": len(ss), "play_min": 0.0, "busy_min": 0.0, "moving_min": 0.0, "yield_min": 0.0,
          "buf": 0, "buf_moving": 0, "contention": 0, "ignored": 0, "notices": 0, "loki_errors": 0,
          "status_seen": False, "unloads": 0, "job_min": 0.0}
    prev = None
    for s in ss:
        dt_s = min(s["t"] - prev, cap) if prev is not None else TICK_S
        prev = s["t"]
        mins = max(dt_s, 0) / 60.0
        g = s.get("g")
        sm["status_seen"] |= g is not None
        if s.get("err"):
            sm["loki_errors"] += 1
            continue
        if s.get("play") or s.get("buf"):
            sm["play_min"] += mins
        if s.get("busy"):
            sm["busy_min"] += mins
        if g == "moving":
            sm["moving_min"] += mins
            sm["buf_moving"] += s.get("buf", 0)
        elif g == "yield":
            sm["yield_min"] += mins
        sm["buf"] += s.get("buf", 0)
        ev = s.get("ev")
        if ev in ("contention", "ignored"):
            sm[ev] += 1
        sm["notices"] += 1 if s.get("notice") else 0
        sm["unloads"] += len(s.get("unl") or ())
        if s.get("gpu") == "job":
            sm["job_min"] += mins
    for k in ("play_min", "busy_min", "moving_min", "yield_min", "job_min"):
        sm[k] = round(sm[k])
    return sm


def _hm(m):
    m = int(m)
    return "%d min" % m if m < 60 else "%d h %02d min" % (m // 60, m % 60)


def render_section(sm):
    """Markdown lines for the digest; [] when there is nothing to say."""
    if not sm:
        return []
    out = ["## Plex and Glacier", ""]
    out.append("- Plex playing: about %s; buffering reports: %d (%d while Glacier was moving files)"
               % (_hm(sm["play_min"]), sm["buf"], sm["buf_moving"]))
    if sm["status_seen"]:
        out.append("- Glacier: moving about %s, waiting for Plex about %s" % (_hm(sm["moving_min"]), _hm(sm["yield_min"])))
    else:
        out.append("- Glacier: no glacier-status file seen (no run, or the installed glacier-mover.sh predates the "
                   "plex-busy check)")
    if sm["ignored"]:
        out.append("- **Glacier started copying while Plex was busy and Plex buffered: %d check(s)%s**"
                   % (sm["ignored"], ", notice sent" if sm["notices"] else ""))
    if sm["contention"]:
        out.append("- Plex buffered during a unit that was already copying when playback began: %d check(s) "
                   "(expected; Glacier waits before its next unit)" % sm["contention"])
    if sm["unloads"] or sm["job_min"]:
        out.append("- Unraid GPU for Plex: %d Ollama model unload(s); a queue job already running kept the GPU about %s"
                   % (sm["unloads"], _hm(sm["job_min"])))
    if sm["loki_errors"]:
        out.append("- Loki did not answer on %d of %d checks (the flag goes stale then, and Glacier carries on)"
                   % (sm["loki_errors"], sm["samples"]))
    out.append("")
    return out


# ---------------------------------------------------------------------------------------------------- cli
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0 if argv else 2
    if argv[0] == "tick":
        tick()
        return 0
    if argv[0] == "show":
        day = argv[argv.index("--day") + 1] if "--day" in argv[:-1] else dt.date.today().isoformat()
        lines = render_section(day_summary(day))
        print("\n".join(lines) if lines else "no samples for %s" % day)
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
