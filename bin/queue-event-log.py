#!/usr/bin/env python3
"""Durable queue-event log. Appends one JSON line per terminal job transition to
<dir>/queue-events.jsonl (seq-numbered). Metadata only (id, label, state, verdict, short
reason, launcher); never job content.

Three sources, deduped per (id, state):
  1. `ollama-queue.py status` rows in a terminal state (failed/blocked/needs_opus/...: those
     stay listed until resolved), plus live rows paused on needs_info (a job asking a
     question) read from the queue state file.
  2. new <LOG_DIR>/<id>.done.json sidecars, written by the queue at every terminal
     transition and never pruned. This is the only reliable trace of a clean finish:
     RETAIN_DONE_RECENT=0 drops a done row on the next daemon tick, so the 10 s status poll
     almost never saw one (2026-11-02: 0 done/passed events ever logged, only failures).
  3. new rows of <LOG_DIR>/NON-PASS-GATES.md (gate-on-complete's non-pass verdicts), as
     state "gate_nonpass".
Each event carries launched_by / launched_by_session (live row, else sidecar) so delivery
can target the launching session (queue-events-deliver.py).

Env overrides (tests): QEL_DIR, QEL_STATUS_CMD (shell command printing status text),
QEL_INTERVAL, QEL_MAX_BYTES, QEL_LOG_DIR, QEL_STATE_PATH.
"""
import json, os, re, subprocess, sys, time

DIR = os.environ.get("QEL_DIR") or os.path.expanduser("~/.ollama-dispatch")
STATUS_CMD = os.environ.get("QEL_STATUS_CMD") or "python3 ~/bin/ollama-queue.py status"
MAX_BYTES = int(os.environ.get("QEL_MAX_BYTES") or 1_000_000)
LOG_DIR = os.environ.get("QEL_LOG_DIR") or os.path.expanduser("~/bin/ollama-queue-logs")
STATE_PATH = os.environ.get("QEL_STATE_PATH") or os.path.expanduser("~/bin/ollama-queue-state.json")
TERMINAL = {"failed", "blocked", "done", "passed", "pass", "needs_opus", "needs-review", "needs_review",
            "cancelled", "done_unconverged", "error"}
OVERLAP_S = 120        # sidecar re-scan window below the watermark (late or coarse mtimes)
KEEP_EMITTED = 2000    # dedupe memory, in job ids
LINE = re.compile(r"^\s*\[(\S+)\s*\]\s+(\S+)\s+(\S*)")
NONPASS = re.compile(r"^- `([^`]+)` \*\*([^*]+)\*\* code_high=(\d+)")
P = lambda n: os.path.join(DIR, n)


def parse(text):
    jobs = {}
    for ln in text.splitlines():
        m = LINE.match(ln)
        if not m:
            continue
        st, jid, label = m.groups()
        v = re.search(r"\bverdict=(\S+(?: \([^)]*\))?)", ln)
        r = re.search(r"\breason=(.{0,120}?)(?:\s{2}|$)", ln) or re.search(r"\bclass=(\S+)", ln)
        e = re.search(r"\bescalated=\[(\S+?)\]", ln)
        jobs[jid] = {"status": st, "label": label,
                     "verdict": (v.group(1)[:80] if v else None),
                     "reason": (r.group(1).strip()[:120] if r else (e.group(1) if e else None))}
    return jobs


def key(j):
    v = j["verdict"]
    return f'{j["status"]}|{"" if v in (None, "PENDING") else v}'


def event_state(j):
    v = (j["verdict"] or "").upper()
    if j["status"] == "done" and v.startswith("PASS"):
        return "passed"
    if j["status"] == "done" and v.startswith("NEEDS-REVIEW"):
        return "needs-review"    # a soft-gate-only mini-swe pass: always reviewed before it lands (mini-scorecard)
    return j["status"]


def norm_verdict(v):
    """First word of a verdict, upper-cased; '' for none/PENDING ('FAIL (mode=..)' -> 'FAIL')."""
    w = str(v or "").strip().split(" ", 1)[0].upper()
    return "" if w in ("", "NONE", "PENDING") else w


def write_atomic(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        f.write(data)
    os.replace(tmp, path)


def live_rows():
    try:
        return {j.get("id"): j for j in json.load(open(STATE_PATH)).get("jobs", [])
                if isinstance(j, dict) and j.get("id")}
    except Exception:
        return {}


def sidecar(jid):
    for p in (os.path.join(LOG_DIR, f"{jid}.done.json"), os.path.join(LOG_DIR, "archive", f"{jid}.done.json")):
        try:
            d = json.load(open(p))
            return d if isinstance(d, dict) else {}
        except Exception:
            continue
    return {}


def launcher(rec):
    out = {}
    lb = str((rec or {}).get("launched_by") or "")
    if lb:
        out["launched_by"] = lb[4:] if lb.startswith("uds:") else lb
    if (rec or {}).get("launched_by_session"):
        out["launched_by_session"] = str(rec["launched_by_session"])[:80]
    return out


FINISH_CLASSES = ("model", "timeout", "context")
REPLAY_FAIL = re.compile(r"\breplay (?:RED|no-ref|test-rejected)\b")


def finish_hint(jid, rec=None):
    """`~/bin/mini-finish <id>` when Claude can finish this failed job's kept attempt in place: a mini-swe-runner job that
    failed on the model or its budget (not the spec, infra or a continuation's replay) whose work dir is still there.
    Mirrors mini-finish's own first refusals; mini-finish re-checks everything when it runs."""
    full = dict(sidecar(jid))
    full.update({k: v for k, v in (rec or {}).items() if v is not None})
    if os.path.basename(str(full.get("runner") or "")) != "mini-swe-runner.py":
        return None
    if str(full.get("failure_class") or "").lower() not in FINISH_CLASSES:
        return None
    if REPLAY_FAIL.search(" ".join(str(full.get(k) or "") for k in ("runner_verdict", "failure_detail", "terminal_reason"))):
        return None
    if full.get("superseded_by") or full.get("continued_by"):
        return None
    cwd = os.path.expanduser(str(full.get("cwd") or ""))
    if not finishable_spec(full.get("task_file"), cwd):
        return None
    return f"~/bin/mini-finish {jid}" if cwd and os.path.isdir(cwd) else None


def finishable_spec(path, cwd=None):
    """True for a spec mini-finish takes (a chain, two-phase or diagnose, or an auto job that reached its fix stage: the
    runner left the derived spec.auto.json next to the work dir); a verify-mode or unreadable spec gets no hint."""
    try:
        with open(os.path.expanduser(str(path or ""))) as fh:
            sp = json.load(fh)
    except (OSError, ValueError):
        return False
    if isinstance(sp, dict) and sp.get("remote"):  # a remote-lane job: mini-finish refuses it (its tests run only on a CPU runner)
        return False
    if isinstance(sp, dict) and sp.get("mode") == "auto":
        return bool(cwd) and os.path.isfile(os.path.join(os.path.dirname(os.path.normpath(cwd)), "spec.auto.json"))
    return isinstance(sp, dict) and bool(sp.get("steps") or sp.get("mode") in ("two-phase", "diagnose"))


def failed_row(jid, j, row):
    """One unresolved failed row for the hook snapshot: id, label, reason, launcher, finish hint."""
    out = dict({"id": jid, "label": j["label"], "reason": j["reason"]}, **launcher(row))
    h = finish_hint(jid, row)
    if h:
        out["finish"] = h
    return out


def settled(k):
    """True for a status key in a terminal state or parked on a needs_info question."""
    return bool(k) and (k.split("|", 1)[0].lower() in TERMINAL or k.endswith("|needs_info"))


def is_dup(st, jid, state, nv, any_verdict=False):
    """An event (jid, state) is a duplicate once that state was emitted, unless it now
    carries a verdict not seen before (status path: PENDING -> FAIL). any_verdict=True
    (sidecar/gate paths): the state alone decides. The memory of a job is dropped when it
    is seen running again after a terminal/needs_info state (requeue, resume, an answered
    question), so its next outcome is a new event (poll_once)."""
    seen = (st.get("emitted") or {}).get(jid, {}).get(state)
    if seen is None:
        return False
    return any_verdict or nv == "" or nv in seen


def mark(st, jid, state, nv):
    em = st.setdefault("emitted", {})
    rec = em.pop(jid, {})          # re-insert = most recent
    rec.setdefault(state, [])
    if nv not in rec[state]:
        rec[state].append(nv)
    em[jid] = rec
    while len(em) > KEEP_EMITTED:
        em.pop(next(iter(em)))


def scan_sidecars(st):
    """[(jid, rec)] for <id>.done.json files not seen before, or rewritten since (a later
    terminal transition of a requeued job; is_dup still drops a plain re-write of the same
    outcome). First run (no watermark): set the watermark, emit nothing (no flood of
    history)."""
    wm = st.get("sc_wm")
    seen = st.get("sc_seen") or {}
    files = []
    try:
        with os.scandir(LOG_DIR) as it:
            for de in it:
                if de.name.endswith(".done.json"):
                    try:
                        files.append((de.name[:-10], de.stat().st_mtime))
                    except OSError:
                        pass
    except OSError:
        return []
    top = max([wm or 0.0] + [m for _, m in files])
    new = []
    if wm is not None:
        for jid, mt in files:
            if mt >= wm - OVERLAP_S and (jid not in seen or mt > seen[jid]):
                rec = sidecar(jid)
                if rec:
                    new.append((jid, rec))
    for jid, mt in files:
        if mt >= top - OVERLAP_S:
            seen[jid] = mt
    st["sc_wm"] = top
    st["sc_seen"] = {j: m for j, m in seen.items() if m >= top - OVERLAP_S}
    return new


def scan_nonpass(st):
    """New complete rows of NON-PASS-GATES.md since the stored byte offset."""
    p = os.path.join(LOG_DIR, "NON-PASS-GATES.md")
    try:
        size = os.path.getsize(p)
    except OSError:
        return []
    off = st.get("np_off")
    if off is None:
        st["np_off"] = size
        return []
    if size < off:          # truncated / replaced
        off = 0
    if size == off:
        return []
    with open(p, "rb") as f:
        f.seek(off)
        data = f.read(size - off)
    nl = data.rfind(b"\n")
    if nl < 0:
        return []
    st["np_off"] = off + nl + 1
    rows = []
    for ln in data[:nl].decode("utf-8", "replace").splitlines():
        m = NONPASS.match(ln)
        if m:
            rows.append({"id": m.group(1), "verdict": m.group(2), "code_high": int(m.group(3)),
                         "top": ln.split(" | ", 1)[1].strip() if " | " in ln else ""})
    return rows


def last_seq():
    best = 0
    for fn in ("queue-events.jsonl", "queue-events.jsonl.1"):
        try:
            with open(P(fn), "rb") as f:
                f.seek(0, 2)
                f.seek(max(0, f.tell() - 65536))
                for ln in f.read().splitlines():
                    try:
                        best = max(best, int(json.loads(ln).get("seq") or 0))
                    except Exception:
                        pass
        except OSError:
            pass
    return best


def poll_once():
    os.makedirs(DIR, exist_ok=True)
    try:
        out = subprocess.run(STATUS_CMD, shell=True, capture_output=True, text=True, timeout=60)
    except Exception:
        return 0
    if out.returncode != 0:
        return 0
    jobs = parse(out.stdout)
    live = live_rows()
    sp = P("queue-events.state.json")
    try:
        st = json.load(open(sp))
    except Exception:
        st = None
    first = st is None
    st = st or {"seq": last_seq(), "keys": {}}   # lost state: never reuse a seq
    emitted = []

    def emit(jid, label, state, verdict=None, reason=None, rec=None, bundle=None):
        st["seq"] += 1
        ev = {"seq": st["seq"], "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "id": jid,
              "label": label, "state": state}
        if verdict:
            ev["verdict"] = str(verdict)[:160]
        if reason:
            ev["reason"] = str(reason)[:120]
        if bundle:
            ev["bundle"] = str(bundle)[:60]
        ev.update(launcher(rec))
        if state == "failed":
            h = finish_hint(jid, rec)
            if h:
                ev["finish"] = h
        emitted.append(ev)
        mark(st, jid, state, norm_verdict(verdict))

    # 1. status rows (+ needs_info pauses from the live state)
    for jid, j in jobs.items():
        k = key(j)
        row = live.get(jid) or {}
        if j["status"] == "paused" and row.get("pause_reason") == "needs_info":
            k += "|needs_info"
        prev = st["keys"].get(jid)
        if not settled(k) and (prev is None or settled(prev)):
            # running again after an outcome (requeue/resume/answered question), or listed
            # again after it was gone: its next outcome is a new event. Only on that edge,
            # never on every running poll, so a stale "running" row read after the sidecar
            # was already emitted cannot re-arm a duplicate.
            (st.get("emitted") or {}).pop(jid, None)
        if prev != k and not first:
            if j["status"].lower() in TERMINAL:
                state, nv = event_state(j), norm_verdict(j["verdict"])
                if not is_dup(st, jid, state, nv):
                    emit(jid, j["label"], state, j["verdict"], j["reason"],
                         row or sidecar(jid), row.get("bundle"))
            elif k.endswith("|needs_info") and not is_dup(st, jid, "needs_info", "", True):
                emit(jid, j["label"], "needs_info", None, "job asks a question (NEEDS.md)",
                     row, row.get("bundle"))
        st["keys"][jid] = k
    st["keys"] = {i: st["keys"][i] for i in jobs}
    # 2. completion sidecars (the done rows the poll never sees)
    for jid, rec in scan_sidecars(st):
        status = str(rec.get("status") or "done")
        v = rec.get("runner_verdict")
        state = event_state({"status": status, "verdict": v})
        if is_dup(st, jid, state, norm_verdict(v), True):
            continue
        emit(jid, rec.get("label") or "", state, v,
             rec.get("terminal_reason") or rec.get("failure_class"), rec, rec.get("bundle"))
    # 3. gate non-pass verdicts
    for r in scan_nonpass(st):
        jid = r["id"]
        if is_dup(st, jid, "gate_nonpass", norm_verdict(r["verdict"])):
            continue
        rec = live.get(jid) or sidecar(jid)
        emit(jid, rec.get("label") or "", "gate_nonpass",
             f'{r["verdict"]} code_high={r["code_high"]}', r["top"], rec, rec.get("bundle"))
    lp = P("queue-events.jsonl")
    if emitted:
        try:
            if os.path.getsize(lp) > MAX_BYTES:
                os.replace(lp, lp + ".1")
        except OSError:
            pass
        lead = ""
        try:
            with open(lp, "rb") as f:
                f.seek(-1, 2)
                if f.read(1) != b"\n":
                    lead = "\n"        # a killed append left half a line: end it, or it swallows this batch
        except OSError:
            pass
        with open(lp, "a") as f:
            f.write(lead + "".join(json.dumps(e) + "\n" for e in emitted))
    write_atomic(sp, json.dumps(st))
    # snapshot for fast hook reads: unresolved failed rows
    write_atomic(P("queue-events.failed.json"), json.dumps(
        {"ts": time.time(), "failed": [failed_row(i, j, live.get(i) or {})
                                       for i, j in jobs.items() if j["status"] == "failed"]}))
    return len(emitted)


def main():
    if "--once" in sys.argv:
        print(poll_once())
        return
    interval = float(os.environ.get("QEL_INTERVAL") or 10)
    while True:
        try:
            poll_once()
        except Exception as e:
            print("poll error:", type(e).__name__, flush=True)
        time.sleep(interval)


if __name__ == "__main__":
    main()
