#!/usr/bin/env python3
"""Deliver queue events (<dir>/queue-events.jsonl, written by com.example.queue-event-log) to
the Claude session that should act on them.

  (default)   hook backend (PostToolUse/UserPromptSubmit via queue-events-inject.sh): read
              the hook JSON on stdin, print hook output JSON when there is something new.
  --watch     Monitor-tool backend: one line per new event for this session. This is the
              only path that wakes an IDLE session. It exits by itself once none of this
              session's jobs has been in flight for QEW_IDLE_EXIT_S (default 180 s), so a
              session with no queue work is not woken every 30 min just to re-arm.
  --inflight  print how many live jobs this session launched (the reminder hook's test).

Targeting. A job stamped launched_by=<this session's messaging socket> belongs to this
session. An event with no launcher, or whose launcher socket is gone, is shown once, to
whichever session sees it first (someone must). A job launched by another live session is
left to that session.
Cursors (highest seq considered): per session <dir>/queue-events.sessions/<sid>, per process
<dir>/queue-events.sessions/sock-<pid> (a /clear keeps the process, so the new session
resumes where the old one stopped instead of skipping its own jobs' events), and the global
<dir>/queue-events.cursor (highest unowned event shown anywhere). Hook and watch mode share
them, so an event the Monitor delivered is not injected again: a session starts from the
higher of its own and its process's cursor, and the read-advance runs under a short
best-effort lock (<dir>/queue-events.lock) so a hook firing while the watcher delivers cannot
show one event twice. A hook call from inside a subagent (hook input carries agent_id)
delivers nothing and advances nothing: the events stay for the main session.
Env: QEL_DIR, QEL_STATE_PATH, QEL_LOG_DIR, QEL_STATUS_CMD (fallback only), QEW_IDLE_EXIT_S,
QEW_POLL_S, QEW_LOCK_WAIT_S.
"""
import contextlib, fcntl, json, os, re, subprocess, sys, time

DIR = os.environ.get("QEL_DIR") or os.path.expanduser("~/.ollama-dispatch")
STATE_PATH = os.environ.get("QEL_STATE_PATH") or os.path.expanduser("~/bin/ollama-queue-state.json")
LOG_DIR = os.environ.get("QEL_LOG_DIR") or os.path.expanduser("~/bin/ollama-queue-logs")
P = lambda n: os.path.join(DIR, n)
NOT_IN_FLIGHT = {"done", "passed", "pass", "needs-review", "failed", "blocked", "needs_opus", "cancelled",
                 "done_unconverged", "error", "resolved"}
HEADER = ("QUEUE EVENTS for this session (passed/done: review the result; passed with review=: this pass is in the 1-in-5 "
          "quality sample (the only check on false PASSes): before landing it, run the `review=` command shown and do the review it "
          "describes in THIS session or a Sonnet subagent of it (no API, no `claude -p`), then record the verdict with the "
          "`mini-scorecard review add` line it prints; needs-review: the tests pass but a soft gate failed (verdict= names it): "
          "never land it unreviewed: do the same review= review, starting with that gate's REVIEW.md row, fix what it found in "
          "place, then record the verdict; an auto job (verdict mode=auto: the model diagnosed the bug and wrote the test "
          "and the fix) is always reviewed: its event prints the packet under the event line (review=inline); read the test "
          "first, then the diff, then run the one step the packet ends with, `~/bin/mini-auto review <id> ok|reject --note ...` "
          "(records the verdict; ok also lands it); failed with finish=: finish the "
          "local model's kept attempt yourself, in THIS session (no subagent, no API, no `claude -p`): run the "
          "`~/bin/mini-finish <id>` shown, edit the files it lists in place, then `~/bin/mini-finish <id> --check` "
          "in the background; its PASS queues any remaining chain steps locally and resolves + clears the job; "
          "other failed/blocked/gate_nonpass: triage, then `python3 ~/bin/ollama-queue.py resolve <id>` and "
          "`curl -s -X POST http://127.0.0.1:7684/api/runs/<id>/clear` in the same step; "
          "needs_info: answer the job's NEEDS.md):")


def rd_int(path):
    try:
        return int(open(path).read().strip() or 0)
    except Exception:
        return None


def wr(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    open(tmp, "w").write(text)
    os.replace(tmp, path)


def norm_sock(s):
    s = str(s or "").strip()
    if s.startswith("uds:"):
        s = s[4:]
    if s.startswith("/private/tmp/"):
        s = s[len("/private"):]
    return s


def sock_pid(s):
    m = re.search(r"/(\d+)\.sock$", s or "")
    return m.group(1) if m else None


def my_socket():
    """This session's messaging socket: the env the queue stamps launched_by from, else the
    first ancestor process that owns a /tmp/cc-socks/<pid>.sock (hooks may lack the env)."""
    s = os.environ.get("CLAUDE_CODE_MESSAGING_SOCKET")
    if s:
        return norm_sock(s)
    cands = [os.environ["CLAUDE_PID"]] if os.environ.get("CLAUDE_PID") else []
    p = os.getppid()
    for _ in range(8):
        if p <= 1:
            break
        cands.append(str(p))
        try:
            p = int(subprocess.run(["ps", "-o", "ppid=", "-p", str(p)], capture_output=True,
                                   text=True, timeout=2).stdout.strip() or 0)
        except Exception:
            break
    for c in cands:
        if os.path.exists(f"/tmp/cc-socks/{c}.sock"):
            return f"/tmp/cc-socks/{c}.sock"
    return None


def classify(e, me, bridge, alive):
    """'yours' | 'unstamped' | 'orphan' (launcher gone) | 'other' (another live session)."""
    lb = norm_sock(e.get("launched_by"))
    if not lb:
        return "unstamped"
    if me and lb == me:
        ls = e.get("launched_by_session")
        if bridge and ls and ls != bridge:
            return "orphan"            # pid reuse: same socket path, different session
        return "yours"
    if lb not in alive:
        alive[lb] = os.path.exists(lb)
    return "other" if alive[lb] else "orphan"


def parse_lines(data):
    out = []
    for ln in data.decode("utf-8", "replace").splitlines():
        try:
            e = json.loads(ln)
        except Exception:
            continue
        if isinstance(e, dict) and isinstance(e.get("seq"), int) and e.get("state"):
            out.append(e)
    return out


def log_ino():
    try:
        return os.stat(P("queue-events.jsonl")).st_ino
    except OSError:
        return None


def read_events():
    out = []
    for _ in range(3):
        ino = log_ino()
        for fn in ("queue-events.jsonl.1", "queue-events.jsonl"):
            try:
                out += parse_lines(open(P(fn), "rb").read())
            except OSError:
                pass
        if log_ino() == ino:
            break                      # else the log rotated between the two reads: read both again
    return out                         # (a repeated record is dropped by deliver())


class Tail:
    """Incremental reader of queue-events.jsonl that survives rotation (-> .1)."""

    def __init__(self):
        self.path, self.ino, self.off = P("queue-events.jsonl"), None, 0

    def read(self, retry=2):
        try:
            stt = os.stat(self.path)
        except OSError:
            return []
        out = []
        if self.ino is not None and (stt.st_ino != self.ino or stt.st_size < self.off):
            try:
                with open(self.path + ".1", "rb") as f:   # finish the rotated file
                    f.seek(self.off)
                    out += parse_lines(f.read())
            except OSError:
                pass
            self.off = 0
        self.ino = stt.st_ino
        if stt.st_size > self.off:
            with open(self.path, "rb") as f:
                if retry and os.fstat(f.fileno()).st_ino != stt.st_ino:
                    return out + self.read(retry - 1)   # rotated between the stat and the open
                f.seek(self.off)
                data = f.read(stt.st_size - self.off)
            nl = data.rfind(b"\n")
            if nl >= 0:
                self.off += nl + 1
                out += parse_lines(data[:nl])
        return out


_SCORECARD = []


def scorecard():
    """mini-scorecard (an extensionless script next to this file), loaded once per process."""
    if not _SCORECARD:
        from importlib.machinery import SourceFileLoader
        import importlib.util
        path = os.environ.get("QEL_SCORECARD") or os.path.join(os.path.dirname(os.path.abspath(__file__)), "mini-scorecard")
        ld = SourceFileLoader("mini_scorecard", path)
        m = importlib.util.module_from_spec(importlib.util.spec_from_loader("mini_scorecard", ld))
        ld.exec_module(m)
        _SCORECARD.append(m)
    return _SCORECARD[0]


def job_rec(jid):
    """The queue's finished-job record of jid (live or archived), or None."""
    for p in (os.path.join(LOG_DIR, f"{jid}.done.json"), os.path.join(LOG_DIR, "archive", f"{jid}.done.json")):
        try:
            with open(p) as fh:
                rec = json.load(fh)
            return rec if isinstance(rec, dict) else None
        except (OSError, ValueError):
            continue
    return None


AUTO_PKT_CAP = 24000


def auto_packet(e):
    """The REVIEW-AUTO.md packet of a passed auto job (mini-auto), printed under its event line so the review costs the session
    no extra call: the reviewer reads the test and the diff right there and answers with one `mini-auto review`. The packet's
    file list and review asks are replaced by the closing line. None for anything else; never raises."""
    jid = str(e.get("id") or "")
    if e.get("state") not in ("passed", "needs-review") or not re.fullmatch(r"[\w.-]+", jid):
        return None
    try:
        rec = job_rec(jid)
        if not rec or "mini-swe-runner" not in str(rec.get("runner") or "") or scorecard().job_kind(rec) != "auto":
            return None
        work = os.path.expanduser(str(rec.get("cwd") or "")).rstrip("/")
        pkt = os.path.join(os.path.dirname(work), "REVIEW-AUTO.md")
        if not work or not os.path.isfile(pkt):
            return None
        with open(pkt, errors="replace") as fh:
            txt = fh.read(AUTO_PKT_CAP + 1)
    except Exception:
        return None
    body = re.split(r"(?m)^## Files[ \t]*$", txt)[0].rstrip()
    if len(body) > AUTO_PKT_CAP:
        body = body[:AUTO_PKT_CAP] + f"\n[... cut at {AUTO_PKT_CAP} chars; the whole packet: {pkt}]"
    return (f"----- {pkt} -----\n{body}\n----- end of packet -----\n"
            f"AUTO REVIEW {jid}: the model wrote the test AND the fix, so the test is the only oracle. Test first: every assertion is "
            f"what the Problem asks, with the exact values. Then the diff: the minimal root-cause fix, nothing unasked changed. One step "
            f"records the verdict and, on ok, lands the test + fix (uncommitted) in the repo the job came from: "
            f"`~/bin/mini-auto review {jid} ok --note \"<one line>\"` or `~/bin/mini-auto review {jid} reject --note \"<what is wrong>\"` "
            f"(a reject lands nothing: fix it yourself). Work dir: {work}")


def review_hint(e):
    """`~/bin/mini-scorecard review brief <id>` when this pass is in the quality review: a passed mini-swe fix job that
    mini-scorecard's hash sample picks (about 1 in 5; docs/offload-increase-plan.md section 5), or any needs-review pass
    (a soft gate failed: always reviewed). Never raises: an unreadable record means no hint, and `mini-scorecard sample list`
    still lists the job."""
    jid = str(e.get("id") or "")
    if e.get("state") not in ("passed", "needs-review") or not re.fullmatch(r"[\w.-]+", jid):
        return None
    try:
        rec = job_rec(jid)
        if not isinstance(rec, dict) or "mini-swe-runner" not in str(rec.get("runner") or ""):
            return None                # not an offload job (checked before loading the scorecard)
        if scorecard().review_pick(rec)[0]:
            return f"~/bin/mini-scorecard review brief {jid}"
    except Exception:
        pass
    return None


def fmt(e, cls):
    tag = {"yours": "yours", "unstamped": "no launcher", "orphan": "launcher gone"}[cls]
    extra = " ".join(f"{k}={e[k]}" for k in ("verdict", "reason") if e.get(k))
    fin = f" finish=`{e['finish']}`" if e.get("finish") else ""
    pkt = auto_packet(e)
    rv = None if pkt else review_hint(e)
    rev = f" review=`{rv}`" if rv else (" review=inline (packet below)" if pkt else "")
    line = f'#{e["seq"]} {e["state"]} ({tag}): {e.get("label") or "?"} [{e.get("id")}] {extra}'.rstrip() + fin + rev
    return line + ("\n" + pkt if pkt else "")


def session_paths(sid, me):
    """(session cursor or None when the session id is unknown, process cursor or None)."""
    sdir = P("queue-events.sessions")
    pid = sock_pid(me)
    return (os.path.join(sdir, sid) if sid else None,
            os.path.join(sdir, f"sock-{pid}") if pid else None)


def owner_cursor(e):
    """Highest seq the launcher's process has considered (its process cursor; 0 when unknown)."""
    pid = sock_pid(norm_sock(e.get("launched_by")))
    return (rd_int(os.path.join(P("queue-events.sessions"), f"sock-{pid}")) or 0) if pid else 0


@contextlib.contextmanager
def cursor_lock():
    """Exclusive lock around one cursor read-advance, best effort: when it cannot be had
    within QEW_LOCK_WAIT_S (default 2 s; a wedged holder) or the lock file cannot be opened,
    proceed unlocked. A rare duplicate line beats a hung hook or a lost event."""
    f = None
    try:
        os.makedirs(DIR, exist_ok=True)
        f = open(P("queue-events.lock"), "a")
        end = time.time() + float(os.environ.get("QEW_LOCK_WAIT_S") or 2)
        while True:
            try:
                fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.time() >= end:
                    f.close()
                    f = None
                    break
                time.sleep(0.05)
    except (OSError, ValueError):
        if f:
            f.close()
        f = None
    try:
        yield
    finally:
        if f:
            f.close()                  # closing the descriptor releases the flock


def deliver(events, sid, me, bridge):
    """Core shared by hook and watch mode. Returns (lines, new_session, shown_ids)."""
    with cursor_lock():
        spath, kpath = session_paths(sid, me)
        g = rd_int(P("queue-events.cursor")) or 0
        s = rd_int(spath) if spath else None
        k = rd_int(kpath) if kpath else None
        new_session = s is None
        # Higher of the session and process cursors: after /clear a watcher still armed under
        # the old session id and the new session's hook share this process, so neither may
        # re-deliver what the other already showed. Neither known: the global cursor.
        known = [c for c in (s, k) if c is not None]
        base = max(known) if known else g
        lines, shown, alive, top, gtop, seen = [], set(), {}, base, g, set()
        for e in sorted(events, key=lambda x: x["seq"]):
            n = e["seq"]
            if n <= base or (n, e.get("id"), e["state"]) in seen:
                continue               # already delivered, or the same record logged twice (writer replay)
            seen.add((n, e.get("id"), e["state"]))
            top = max(top, n)
            cls = classify(e, me, bridge, alive)
            if cls == "other":
                continue
            if cls != "yours":
                if n <= g:
                    continue           # an unowned event another session already showed
                gtop = max(gtop, n)
                if cls == "orphan" and n <= owner_cursor(e):
                    continue           # its launcher already got it before it exited
            lines.append(fmt(e, cls))
            shown.add(e.get("id"))
        if spath and (s is None or top > s):
            wr(spath, str(top))
        if kpath and (k is None or top > k):
            wr(kpath, str(top))
        if gtop > g:
            wr(P("queue-events.cursor"), str(gtop))
    return lines, new_session, shown


def failed_rows():
    try:
        d = json.load(open(P("queue-events.failed.json")))
        if time.time() - d["ts"] < 120:
            return d["failed"]
    except Exception:
        pass
    import importlib.util
    spec = importlib.util.spec_from_file_location("qel", os.path.expanduser("~/bin/queue-event-log.py"))
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    cmd = os.environ.get("QEL_STATUS_CMD") or "python3 ~/bin/ollama-queue.py status"
    try:
        t = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return []
    live = m.live_rows()
    return [m.failed_row(i, j, live.get(i) or {}) for i, j in m.parse(t).items() if j["status"] == "failed"]


def hook():
    try:
        inp = json.load(sys.stdin)
    except Exception:
        inp = {}
    if inp.get("agent_id"):
        return                         # a subagent's tool call: leave the events for the main session
    sid = "".join(c for c in str(inp.get("session_id") or "x") if c.isalnum() or c in "-_")[:80] or "x"
    ev_name = inp.get("hook_event_name") or "PostToolUse"
    me, bridge = my_socket(), os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID")
    lines, new_session, shown = deliver(read_events(), sid, me, bridge)
    if new_session:
        alive = {}
        for f in failed_rows():
            cls = classify(f, me, bridge, alive)
            if f["id"] not in shown and cls != "other":
                lines.append(f'UNRESOLVED failed ({cls}): {f["label"]} [{f["id"]}] {f.get("reason") or ""}'.rstrip()
                             + (f' finish=`{f["finish"]}`' if f.get("finish") else ""))
        sdir = P("queue-events.sessions")
        try:
            for fn in os.listdir(sdir):
                p = os.path.join(sdir, fn)
                if time.time() - os.path.getmtime(p) > 7 * 86400:
                    os.unlink(p)
        except OSError:
            pass
    if lines:
        print(json.dumps({"hookSpecificOutput": {"hookEventName": ev_name,
                                                 "additionalContext": HEADER + "\n" + "\n".join(lines)}}))


def _sidecar_launcher(jid):
    try:
        return norm_sock(json.load(open(os.path.join(LOG_DIR, f"{jid}.done.json"))).get("launched_by"))
    except Exception:
        return ""


def inflight(me):
    """Live, non-terminal jobs this session launched (gate/regate/secondop children of its
    jobs included). -1 when the queue state is unreadable."""
    if not me:
        return 0
    try:
        jobs = [j for j in json.load(open(STATE_PATH)).get("jobs", []) if isinstance(j, dict)]
    except Exception:
        return -1
    by_id = {j.get("id"): j for j in jobs}
    n = 0
    for j in jobs:
        if str(j.get("status") or "") in NOT_IN_FLIGHT:
            continue
        lb = norm_sock(j.get("launched_by"))
        if not lb:
            m = re.match(r"^(?:gate|regate|secondop)-([0-9a-f]{6,})$", str(j.get("label") or ""))
            if m:
                par = by_id.get(m.group(1))
                lb = norm_sock(par.get("launched_by")) if par else _sidecar_launcher(m.group(1))
        if lb == me:
            n += 1
    return n


def watch():
    me, bridge = my_socket(), os.environ.get("CLAUDE_CODE_BRIDGE_SESSION_ID")
    if not me:
        print("queue watcher: no session messaging socket found; nothing to target", file=sys.stderr)
        return 2
    # No session id in the env: process cursor only (a shared "x" session file would let one
    # session's watcher advance past another session's own events).
    sid = "".join(c for c in os.environ.get("CLAUDE_CODE_SESSION_ID", "") if c.isalnum() or c in "-_")[:80] or None
    idle_exit = float(os.environ.get("QEW_IDLE_EXIT_S") or 180)
    poll = float(os.environ.get("QEW_POLL_S") or 2)
    tail, last_busy, last_check = Tail(), time.time(), 0.0
    while True:
        evs = tail.read()
        if evs:
            lines, _, _ = deliver(evs, sid, me, bridge)
            for ln in lines:
                print("queue " + ln, flush=True)
        now = time.time()
        if now - last_check >= max(poll, 10):
            last_check = now
            if inflight(me) != 0:      # -1 (unreadable) counts as busy
                last_busy = now
        if now - last_busy >= idle_exit:
            return 0
        time.sleep(poll)


if __name__ == "__main__":
    if "--watch" in sys.argv:
        sys.exit(watch())
    if "--inflight" in sys.argv:
        print(inflight(my_socket()))
    else:
        hook()
