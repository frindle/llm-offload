"""issue_poke: wake the GitHub issue poller now (a GitHub Actions runner on the LAN calls this when an issue is labelled).

  POST /api/issue-poke   {"repo": "OWNER/NAME", "issue": 123}   -> 202 {"ok": true, "queued": true}

The route only validates, authenticates and writes a trigger file (atomic, mode 0600) in the queue state dir. It never runs
anything itself. The loop (`python3 issue_poke.py loop`, one supervisord program) runs `mini-issue-poller` as soon as a
trigger is newer than the last tick's start (never closer than MIN_GAP_S to the previous start: bursts coalesce into one
tick) and, with no poke, every BACKSTOP_S. Ticks run one at a time, and the poller's own lock stays. A poke that arrives
during a tick is not lost: it is newer than that tick's start, so one more tick follows.

Auth: "Authorization: Bearer <token>"; the token is the file ISSUE_POKE_TOKEN_FILE (default ~/.config/mini-issue-poller/
poke-token, written by entrypoint.sh from the env ISSUE_POKE_TOKEN), or the env ISSUE_POKE_TOKEN itself. It works on this
route only. No token (or one shorter than 24 characters) = 503, never open. The repo owner must be the poller's owner
(config.json "owner", default below). The poke carries no authority beyond "run a tick now": the tick re-reads GitHub and
applies every poller rule (owner-authored, label, daily cap, one search per tick). Nothing from the body is logged but the
repo name and issue number, which the validator has already restricted to safe characters.
"""
import hmac
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path

ROUTE = "/api/issue-poke"
MAX_BODY = 1024
MIN_TOKEN_LEN = 24
BACKSTOP_S = 60
MIN_GAP_S = 5
TICK_TIMEOUT_S = 3600
TRIGGER_NAME = "issue-poke.trigger"
DEFAULT_OWNER = "frindle"
REPO_RE = re.compile(r"\A([A-Za-z0-9][A-Za-z0-9-]{0,38})/([A-Za-z0-9_.-]{1,100})\Z")
HERE = Path(os.path.realpath(__file__)).parent


def token():
    t = (os.environ.get("ISSUE_POKE_TOKEN") or "").strip()
    if not t:
        f = (os.environ.get("ISSUE_POKE_TOKEN_FILE") or "~/.config/mini-issue-poller/poke-token").strip()
        try:
            t = Path(f).expanduser().read_text().strip()
        except OSError:
            t = ""
    return t if len(t) >= MIN_TOKEN_LEN else ""


def owner():
    o = (os.environ.get("ISSUE_POKE_OWNER") or "").strip()
    if o:
        return o
    home = Path(os.environ.get("POLLER_HOME") or Path.home() / ".ollama-dispatch" / "issue-poller")
    try:
        d = json.loads((home / "config.json").read_text())
        if isinstance(d, dict) and str(d.get("owner") or "").strip():
            return str(d["owner"]).strip()
    except (OSError, ValueError):
        pass
    return DEFAULT_OWNER


def state_dir():
    d = os.environ.get("QUEUE_STATE_DIR")
    if d:
        return Path(d)
    import queue_config
    return Path(queue_config.state_dir())


def auth(headers, tok=None):
    """-> None when allowed, else (status, body)."""
    tok = token() if tok is None else tok
    if not tok:
        return 503, {"error": "E_OFF", "detail": "issue poke is off (no ISSUE_POKE_TOKEN on the queue)"}
    got = (headers.get("Authorization") or "") if headers is not None else ""
    if got[:7].lower() != "bearer " or not hmac.compare_digest(got[7:].strip().encode(), tok.encode()):
        return 401, {"error": "E_AUTH"}
    return None


def _body(raw, own):
    try:
        d = json.loads(raw or b"{}")
    except ValueError:
        return None, (400, {"error": "E_BODY", "detail": "body is not JSON"})
    if not isinstance(d, dict):
        return None, (400, {"error": "E_BODY", "detail": "body must be a JSON object"})
    repo, n = d.get("repo"), d.get("issue")
    m = REPO_RE.match(repo) if isinstance(repo, str) else None
    if not m:
        return None, (400, {"error": "E_BODY", "detail": "repo: OWNER/NAME"})
    if m.group(1).lower() != own.lower():
        return None, (403, {"error": "E_OWNER", "detail": "repo is not under the configured owner"})
    if isinstance(n, bool) or not isinstance(n, int) or not 0 < n < 10 ** 9:
        return None, (400, {"error": "E_BODY", "detail": "issue: a positive integer"})
    return {"repo": repo, "issue": n}, None


def write_trigger(base, rec, now=None):
    base = Path(base)
    base.mkdir(parents=True, exist_ok=True)
    tmp = base / (TRIGGER_NAME + ".tmp-%d" % os.getpid())
    fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps({**rec, "at": time.time() if now is None else now}) + "\n")
    os.replace(tmp, base / TRIGGER_NAME)


def handle(method, path, headers, body, base=None, tok=None, own=None):
    """-> (status, json-able). Only POST on ROUTE."""
    if str(path or "").split("?", 1)[0].rstrip("/") != ROUTE:
        return 404, {"error": "E_NOT_FOUND"}
    if method != "POST":
        return 405, {"error": "E_METHOD"}
    bad = auth(headers, tok)
    if bad:
        return bad
    rec, err = _body(body, owner() if own is None else own)
    if err:
        return err
    try:
        write_trigger(state_dir() if base is None else base, rec)
    except OSError:
        return 500, {"error": "E_INTERNAL"}
    return 202, {"ok": True, "queued": True}


# ---------------------------------------------------------------------------------------------------- the loop
def trigger_mtime_ns(base):
    try:
        return (Path(base) / TRIGGER_NAME).stat().st_mtime_ns
    except OSError:
        return 0


def run_loop(base, cmd, backstop=BACKSTOP_S, min_gap=MIN_GAP_S, stop=None, clock=time.time, sleep=time.sleep,
             poll=1.0, timeout=TICK_TIMEOUT_S, log=print):
    """Run `cmd` (one poller tick) on a poke or every `backstop` seconds, one at a time, at least `min_gap` apart."""
    stop = stop or threading.Event()
    last_start = None            # clock() when the last tick started
    last_start_ns = 0            # wall time (ns) of that start: a trigger newer than it has not been served
    while not stop.is_set():
        now = clock()
        poked = trigger_mtime_ns(base) > last_start_ns
        due = last_start is None or now - last_start >= backstop
        gap_ok = last_start is None or now - last_start >= min_gap
        if (poked and gap_ok) or due:
            last_start, last_start_ns = now, time.time_ns()
            try:
                p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
                log(f"tick rc={p.returncode}")
            except Exception as e:                       # a failing tick never stops the loop
                log(f"tick error {type(e).__name__}")
            continue
        sleep(poll)


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    if a[:1] != ["loop"]:
        print("usage: issue_poke.py loop", file=sys.stderr)
        return 64
    import queue_config
    if queue_config.standby():                           # a standby container acts on nothing
        print("standby: issue poller loop idle", flush=True)
        while True:
            time.sleep(3600)
    cmd = (os.environ.get("ISSUE_POKE_TICK_CMD") or f"{sys.executable} {HERE / 'mini-issue-poller'}").split()
    stop = threading.Event()
    import signal
    for s in (signal.SIGTERM, signal.SIGINT):
        signal.signal(s, lambda *_: stop.set())
    run_loop(state_dir(), cmd, stop=stop, log=lambda m: print(m, flush=True))
    return 0


if __name__ == "__main__":
    sys.exit(main())
