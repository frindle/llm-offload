#!/usr/bin/env python3
"""failure_ledger -- append-only ledger of terminal-failed queue jobs, with a
FINGERPRINT of what the job actually did and a normalized SIGNATURE of why it failed.

WHY (2026-10-06, rt-egift-link-s1-s4: ~14 author jobs): every cause had to be rebuilt by
hand from livelogs / transcripts / attempts.json; the dashboard said "attempt 14" while the
slicer said "3/8 jobs" so no cap ever bit; nobody could tell whether failure N repeated
failure N-1.  This module is the one place that answers those questions.

  ~/.ollama-dispatch/failures.jsonl   one JSON row per terminal-failed job (idempotent by
                                      job_id), plus tiny {"event": "superseded"} rows
  record_job(job_id)                  append one job's row            (FAIL-OPEN, never raises)
  sweep()                             backfill/append every terminal-failed job not yet
                                      recorded, oldest first          (FAIL-OPEN, never raises)
  slice_counter(run_label, sid, ...)  THE attempt counter (author jobs lifetime + window,
                                      attempts, last signature, same-cause) used by the
                                      slicer's status/budget AND the dashboard
  signature_hint(run_label, sid)      one-line prompt hint when a signature repeated
  load_rows()                         every ledger row (current + rotated files)

Standalone on purpose: the queue daemon cannot be restarted to pick up new code, so the
sweep reads queue state + <id>.done.json + livelogs from disk and is called from auto, the
slicer and qctl; ollama-queue.py's finalize path also calls record_job (live after the next
daemon restart).  Every public function swallows its own errors: a ledger problem must never
break the queue, auto or the slicer.

  failure_ledger.py sweep | show [JOB] | counter RUN SID | selftest
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
BIN = Path(os.environ.get("FAILURE_LEDGER_BIN") or HOME / "bin")
STATE_DIR = Path(os.environ.get("FAILURE_LEDGER_BIN") or queue_config.state_dir())  # the override still wins (tests); else the queue state dir
QUEUE_LOGS = Path(os.environ.get("FAILURE_LEDGER_QUEUE_LOGS") or STATE_DIR / "ollama-queue-logs")
LIVELOGS = Path(os.environ.get("FAILURE_LEDGER_LIVELOGS") or STATE_DIR / "ollama-queue-livelogs")
STATE = Path(os.environ.get("FAILURE_LEDGER_STATE") or STATE_DIR / "ollama-queue-state.json")
LEDGER = Path(os.environ.get("FAILURE_LEDGER") or HOME / ".ollama-dispatch" / "failures.jsonl")
ROTATE_BYTES = 8 * 1024 * 1024

# Defaults mirror ollama-dispatch-slice (MAX_AUTHOR_JOBS, AUTHOR_JOB_LIFETIME_FACTOR,
# MAX_AUTHOR_ATTEMPTS); callers pass the live values.
DEFAULT_JOB_BUDGET = 8
DEFAULT_LIFETIME_FACTOR = 3
DEFAULT_ATTEMPT_CAP = 5

_ANSI = re.compile(r"\x1b\[[0-9;]*m")
_SECRET = re.compile(
    r"(sk-[A-Za-z0-9_\-]{12,}|ghp_[A-Za-z0-9]{20,}|xox[abp]-[A-Za-z0-9\-]{10,}|"
    r"(?i:bearer)\s+[A-Za-z0-9._\-]{16,}|"
    r"(?i:(?:password|passwd|secret|token|api[_-]?key|authorization)\s*[=:]\s*)[^\s'\",;]{6,})")
_VERIFY_CMD = re.compile(r"auto-harness-check|verify\.sh|--test\b|npm (?:run )?test|pytest|node --test|tsx --test")
_HARNESS_SRC = ("auto-harness-check.py",)
NO_REPEAT = ("operator-stop", "cancelled")


def redact(text):
    return _SECRET.sub("[REDACTED]", text) if isinstance(text, str) else text


def _redact_deep(o):
    if isinstance(o, str):
        return redact(o)
    if isinstance(o, list):
        return [_redact_deep(x) for x in o]
    if isinstance(o, dict):
        return {k: _redact_deep(v) for k, v in o.items()}
    return o


# --------------------------------------------------------------------------- label parsing
_PREFIX = re.compile(r"^(?:needs-opus-auto-|needs-opus-|auto-author-|auto-refine-|auto-fix-)+")


def parse_label(label, bundle=None):
    """(kind, run, slice) from a queue label.
    kind: author | c1 | c2 | esc | refine | gate | secondop | preflight | job"""
    label = str(label or "")
    if label.startswith("gate-"):
        return "gate", bundle, None
    if label.startswith("secondop-"):
        return "secondop", bundle, None
    kind = "job"
    if label.startswith("auto-refine-"):
        kind = "refine"
    elif label.startswith("auto-author-"):
        kind = "author"
    elif "preflight" in label:
        kind = "preflight"
    core = _PREFIX.sub("", label)
    m = re.search(r"-(c\d+|esc)$", core)
    if m:
        core = core[:m.start()]
        if kind == "author":
            kind = m.group(1)
    m = re.search(r"-r(\d+)$", core)
    if m and kind == "refine":
        core = core[:m.start()]
    run = bundle
    sid = None
    if bundle and core.startswith(bundle + "-"):
        sid = core[len(bundle) + 1:]
    elif bundle and core == bundle:
        sid = None
    else:
        sid = core or None
    return kind, run, sid


# --------------------------------------------------------------------------- livelog fingerprint
_CALL = re.compile(r"^\[(\d\d):(\d\d):(\d\d)\]\s+\[\w+\]\s+-> calling (\w+)\((.*)\)\s*$")
_ITER = re.compile(r"iteration (\d+)/(\d+)")
_PATH = re.compile(r"""path=(?:'([^']*)'|"([^"]*)")""")


def fingerprint_livelog(path):
    """Everything the livelog says the model DID. Pure parse; {} when unreadable."""
    try:
        text = _ANSI.sub("", Path(path).read_text(errors="replace"))
    except Exception:
        return {}
    calls = []          # (iteration, tool, file|None, cmd|None, seq)
    it = 0
    cap = None
    first_t = last_t = None
    day = 0
    prev_s = None
    for ln in text.splitlines():
        m = _ITER.search(ln)
        if m and "──" in ln:
            it, cap = int(m.group(1)), int(m.group(2))
            continue
        m = _CALL.match(ln)
        if not m:
            continue
        s = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + int(m.group(3))
        if prev_s is not None and s < prev_s - 3600:
            day += 86400
        prev_s = s
        t = s + day
        first_t = t if first_t is None else first_t
        last_t = t
        tool, args = m.group(4), m.group(5)
        pm = _PATH.search(args)
        f = (pm.group(1) or pm.group(2)) if pm else None
        cmd = None
        if tool == "run_bash":
            cm = re.search(r"command=(?:'([^']*)'|\"([^\"]*)\")", args)
            cmd = (cm.group(1) or cm.group(2)) if cm else args[:120]
        calls.append((it, tool, f, cmd, len(calls)))
    if not calls and cap is None:
        return {}
    counts, reads, writes, rewrites = {}, {}, {}, {}
    for _it, tool, f, _c, _s in calls:
        counts[tool] = counts.get(tool, 0) + 1
        if tool == "read_file" and f:
            reads[f] = reads.get(f, 0) + 1
        if tool in ("write_file", "edit_file") and f:
            writes[f] = writes.get(f, 0) + 1
        if tool == "write_file" and f:
            rewrites[f] = rewrites.get(f, 0) + 1
    edit_iters = {c[0] for c in calls if c[1] in ("write_file", "edit_file")}
    max_it = max([c[0] for c in calls] + [it]) if (calls or it) else 0
    streak = best = 0
    for i in range(1, max_it + 1):
        if i in edit_iters:
            streak = 0
        else:
            streak += 1
            best = max(best, streak)
    last_edit = max((c[4] for c in calls if c[1] in ("write_file", "edit_file")), default=None)
    verified_after = None
    if last_edit is not None:
        verified_after = any(c[1] == "run_bash" and c[3] and _VERIFY_CMD.search(c[3])
                             for c in calls if c[4] > last_edit)
    top = lambda d: sorted(d.items(), key=lambda kv: (-kv[1], kv[0]))[:5]
    return {
        "iterations": max_it, "iteration_cap": cap,
        "tool_counts": counts,
        "top_read": top(reads), "top_written": top(writes),
        "rewrites": {k: v for k, v in rewrites.items() if v > 1},
        "longest_readonly_streak": best,
        "verified_after_last_edit": verified_after,
        "duration_s": (last_t - first_t) if (first_t is not None and last_t is not None) else None,
        "reads": reads,
    }


# A job whose worker never ran an iteration (queue: class=harness, "worker produced no iterations")
# usually died on something outside the model's code: Darkbloom held for sandbox work, a dropped
# connection, a GPU out of memory, a model name the host doesn't have, a document that can't be read.
# Its livelog's LAST error line says which. (cause, kind, regex) -- first match per line wins, and the
# scan runs from the end of the log, so the fatal error counts, not an earlier one the worker survived.
_FATAL = (
    ("darkbloom-unreachable", "infra", re.compile(r"darkbloom unreachable")),
    ("provider-capacity", "infra", re.compile(r"Provider capacity is temporarily unavailable")),
    ("provider-error", "infra", re.compile(r"Internal inference failure")),
    ("gpu-oom", "env", re.compile(r"\bout of memory\b")),
    ("model-not-found", "env", re.compile(r"""model ['"][^'"]+['"] not found""")),
    ("token-repeat-limit", "model", re.compile(r"token repeat limit")),
    ("transport-error", "infra", re.compile(
        r"^(?:\[[^\]]*\]\s*)*(?:(?:\w+\.)*(?:TimeoutError|RemoteDisconnected|ConnectionResetError"
        r"|ConnectionRefusedError|URLError)|(?:\w+\.)+timeout):"
        r"|<urlopen error .*(?:timed out|Connection refused|Connection reset)")),
    ("docsum", None, re.compile(r"\[docsum\] job=\S+ FAIL code=(E_[A-Z_]+)")),
)
_DOCSUM_INPUT = {"E_SCANNED_NO_OCR", "E_NOT_PDF", "E_TOO_BIG", "E_TOO_MANY_PAGES", "E_NOT_FOUND",
                 "E_BAD_UPLOAD", "E_SCENARIO_TOO_LONG"}
_DOCSUM_INFRA = {"E_TIMEOUT", "E_EXTRACT_TIMEOUT"}
# Transient infra causes: nothing in the job or its code to fix, so no triage packet (the queue's
# Darkbloom wait / requeue is the remedy). env/model/input causes still get one.
TRANSIENT = ("darkbloom-unreachable", "provider-capacity", "provider-error", "transport-error")


def livelog_fatal(path, tail_n=60):
    """{cause, kind, line} for the last fatal error in the livelog's tail, {} when none / unreadable.
    PURE parse. kind: infra | env | model | input | harness."""
    try:
        text = _ANSI.sub("", Path(path).read_text(errors="replace"))
    except Exception:
        return {}
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()][-tail_n:]
    for ln in reversed(lines):
        if ln.startswith(("raise ", 'File "')):
            continue        # traceback source lines quote the message text without being the error
        for cause, kind, rx in _FATAL:
            m = rx.search(ln)
            if not m:
                continue
            if cause == "docsum":
                code = m.group(1)
                cause = "docsum:" + code
                kind = "input" if code in _DOCSUM_INPUT else ("infra" if code in _DOCSUM_INFRA else "harness")
            return {"cause": cause, "kind": kind, "line": redact(ln)[:200]}
    return {}


# --------------------------------------------------------------------------- run-log scrape
def scrape_runlog(path, tail_n=40):
    """FAILED CASE lines, last N lines of the worker's verify output, literal-cap/lint
    findings and the transcript path, from the worker's per-job .log."""
    out = {"failed_cases": [], "verify_tail": [], "lint": [], "transcript": None}
    try:
        text = _ANSI.sub("", Path(path).read_text(errors="replace"))
    except Exception:
        return out
    lines = text.splitlines()
    # The worker echoes the whole task prompt first; its prose mentions ENOENT, "Must contain"
    # lists, lint names... Only the RESULT part of the log (from the last verify/terminal
    # marker, else the last 200 lines) may feed the lint/env detectors.
    res_from = None
    for i, ln in enumerate(lines):
        if ("[worker] verify stdout:" in ln or "[worker] running verify command" in ln
                or "DID NOT CONVERGE" in ln):
            res_from = i if res_from is None else min(res_from, i)
    result_lines = lines[res_from:] if res_from is not None else lines[-200:]
    for ln in result_lines:
        s = ln.strip()
        if s.startswith("FAILED CASE:"):
            out["failed_cases"].append(s[len("FAILED CASE:"):].strip())
        elif re.match(r"^not ok \d+ - ", s):
            out["failed_cases"].append(s)
        m = re.search(r"full transcript written to (\S+\.json)", s)
        if m:
            out["transcript"] = m.group(1)
        if "Must contain` lists" in s or "plan pins only" in s:
            out["lint"].append("literal-cap")
        if "PLAN SUSPECT" in s or "PLAN LINT" in s:
            out["lint"].append("plan-suspect")
        if "mock.module" in s and ("lint" in s.lower() or "ignored" in s.lower()):
            out["lint"].append("mock-module-lint")
        if ("SELFCHECK_HANG" in s or "self-check timed out" in s
                or "verify command timed out after" in s or "VERIFY TIMED OUT" in s):
            out["lint"].append("selfcheck-hang")
        if "HARNESS-ENV" in s:
            out["lint"].append("harness-env")
        if "ENOENT" in s or "command not found" in s or "Cannot find module" in s:
            out["lint"].append("env-missing")
    idx = None
    for i, ln in enumerate(lines):
        if "[worker] verify stdout:" in ln:
            idx = i
    seg = lines[idx + 1:] if idx is not None else lines
    seg = [l for l in seg if not l.startswith("[worker] ")] or seg
    out["verify_tail"] = seg[-tail_n:]
    # de-dup keeping order, case names only once
    seen, fc = set(), []
    for c in out["failed_cases"]:
        c = re.sub(r"^not ok \d+ - ", "", c)
        if c not in seen:
            seen.add(c)
            fc.append(c)
    out["failed_cases"] = fc
    out["lint"] = sorted(set(out["lint"]))
    return out


# --------------------------------------------------------------------------- signature
def classify(rec, fp, scrape):
    """(primary signature, [all matching tags]) -- PURE. Priority order, most specific
    pipeline defect first, 'model-nonconvergence-other' only when nothing explains it."""
    tags = []
    reason = str((rec or {}).get("terminal_reason") or "")
    detail = str((rec or {}).get("failure_detail") or "")
    cls = str((rec or {}).get("failure_class") or "")
    lint = set((scrape or {}).get("lint") or [])
    reads = (fp or {}).get("reads") or {}
    rewrites = (fp or {}).get("rewrites") or {}
    if reason in ("force_stopped", "cancelled") or cls == "operator" or "force stopped" in detail:
        tags.append("operator-stop")
    # The worker never ran an iteration and its livelog ends on a known outside cause.
    fatal = (fp or {}).get("fatal") or {}
    if fatal.get("cause") and not (fp or {}).get("iterations"):
        tags.append(fatal["cause"])
    # Phase 3 worker_robust named exit reasons (ollama-worker.py TERMINAL REASON).
    if reason == "repeated_format_error":
        tags.append("format-error-loop")
    if reason == "loop_detected":
        tags.append("loop-detected")
    if reason == "stop_gate_failed":
        tags.append("stop-gate-failed")
    if reason == "reasoning_runaway":
        tags.append("reasoning-runaway")
    if reason in ("error_loop", "monologue_loop", "alternation_loop", "nav_loop",
                  "repeat_call_loop", "output_cap_review", "tool_fanout_loop", "read_past_eof_loop"):
        tags.append(reason.replace("_", "-"))     # worker_robust named loop exits (2026-10-09)
    if reason == "fixed_lane_exhausted":
        tags.append("fixed-lane-exhausted")
    if reason == "fixed_lane_apply_failed":
        tags.append("fixed-lane-apply-failed")
    if reason == "fixed_lane_transport_error":
        tags.append("fixed-lane-transport-error")   # infra: retry-safe, not a model verdict
    if reason == "write_thrash":
        top = max(rewrites.items(), key=lambda kv: kv[1])[0] if rewrites else None
        tags.append("write-thrash" + (":" + top if top else ""))
    if "selfcheck-hang" in lint:
        tags.append("selfcheck-hang")     # a verify/self-check wedged (killed at its wall)
    if "literal-cap" in lint:
        tags.append("literal-cap")
    if "plan-suspect" in lint:
        tags.append("plan-suspect")
    if lint & {"harness-env", "env-missing"} and reason != "write_thrash":
        tags.append("env")
    for f, n in sorted(reads.items(), key=lambda kv: -kv[1]):
        base = os.path.basename(f)
        if base in _HARNESS_SRC and n >= 3:
            tags.append("read-loop:" + base)
            break
        if n >= 4:
            tags.append("read-loop:" + base)
            break
    for f, n in sorted(rewrites.items(), key=lambda kv: -kv[1]):
        if n >= 3:
            tags.append("rewrite-loop:" + os.path.basename(f))
            break
    if (fp or {}).get("verified_after_last_edit") is False and (fp or {}).get("iterations"):
        tags.append("edit-never-reverified")
    # mini-swe-runner states its own verdict in detail ("RESULT: <gate>=FAIL ... class=INFRA" or
    # "FAIL bad-cwd ..."). A gate failure or an infra refusal is the runner's/pipeline's, not the
    # model's; keep it out of the model-nonconvergence bucket so triage fixes the right thing.
    # NOTE: runner-gate:<x> stays a TAG only. It is NOT budget-free: a gate that fails repeatedly may be the
    # model's fault or a runner-gate suspect, so mini-submit still counts it (budget_failures drops only runner-infra).
    if "class=INFRA" in detail or "bad-cwd" in detail:
        tags.append("runner-infra")
    else:
        gm = re.search(r"\b([a-z_]+)=FAIL\b", detail)
        if gm and "mini-swe-runner" in detail:
            tags.append("runner-gate:" + gm.group(1))
    if reason in ("nonconvergence", "") and not tags:
        tags.append("model-nonconvergence-other")
    if not tags:
        tags.append(reason or cls or "unknown")
    return tags[0], tags


# --------------------------------------------------------------------------- ledger io
def _iso(ts=None):
    return datetime.fromtimestamp(ts if ts else time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def note_event(kind, **fields):
    """Append a non-job {"event": kind, ...} row (e.g. a self-check watchdog hit that failed no
    queue job). FAIL-OPEN; returns True if written."""
    try:
        row = {"event": kind, "ts": _iso()}
        row.update(fields)
        _append(row)
        return True
    except Exception:
        return False


def budget_failures(rows):
    """PURE. The rows that count against a task family's budget (mini-submit C8): drops pipeline/infra failures
    (tagged runner-infra, or class=INFRA / bad-cwd in the detail). runner-gate:* rows are KEPT."""
    out = []
    for r in rows or []:
        tags = r.get("tags") or []
        detail = str(r.get("detail") or "")
        if "runner-infra" in tags or "class=INFRA" in detail or "bad-cwd" in detail:
            continue
        out.append(r)
    return out


def _ledger_files():
    files = sorted(LEDGER.parent.glob(LEDGER.name + ".*[0-9]"))
    return files + ([LEDGER] if LEDGER.exists() else [])


def load_rows():
    rows = []
    for p in _ledger_files():
        try:
            for ln in p.read_text().splitlines():
                if ln.strip():
                    try:
                        rows.append(json.loads(ln))
                    except Exception:
                        pass
        except Exception:
            pass
    return rows


def _append(row):
    import fcntl
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    lock = LEDGER.with_name(LEDGER.name + ".lock")
    with open(lock, "a") as lf:
        fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            if LEDGER.exists() and LEDGER.stat().st_size > ROTATE_BYTES:
                LEDGER.rename(LEDGER.with_name(LEDGER.name + "." + time.strftime("%Y%m%d%H%M%S")))
            with open(LEDGER, "a") as f:
                f.write(json.dumps(_redact_deep(row), ensure_ascii=False, sort_keys=True) + "\n")
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


def _read_json(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


_STATE_CACHE = {"mtime": None, "path": None, "jobs": {}}
_LABEL_CACHE = {}          # job_id -> (label, persisted_at) for sidecars already read


def _state_jobs():
    try:
        mt = STATE.stat().st_mtime
    except Exception:
        mt = None
    if _STATE_CACHE["mtime"] == mt and _STATE_CACHE["path"] == str(STATE) and mt is not None:
        return _STATE_CACHE["jobs"]
    jobs = _state_jobs_uncached()
    _STATE_CACHE.update(mtime=mt, path=str(STATE), jobs=jobs)
    return jobs


def _state_jobs_uncached():
    st = _read_json(STATE) or {}
    jobs = st.get("jobs") if isinstance(st, dict) else None
    return {j.get("id"): j for j in (jobs or []) if isinstance(j, dict) and j.get("id")}


def _job_record(job_id, state_jobs=None):
    done = _read_json(QUEUE_LOGS / f"{job_id}.done.json") or {}
    sj = (state_jobs if state_jobs is not None else _state_jobs()).get(job_id) or {}
    rec = dict(done)
    for k, v in sj.items():           # live state is fresher than the frozen sidecar
        if v is not None:
            rec[k] = v
    return rec


def _is_terminal_failed(rec):
    if not rec:
        return False
    if rec.get("status") == "failed":
        return True
    return bool(rec.get("terminal_reason") and rec.get("status") not in ("done", "running", "pending"))


def build_row(job_id, rec=None):
    rec = rec if rec is not None else _job_record(job_id)
    label = rec.get("label")
    bundle = rec.get("bundle")
    kind, run, sid = parse_label(label, bundle)
    live = rec.get("live_log_path") or next(iter(LIVELOGS.glob(f"{job_id}-*.livelog")), None)
    runlog = rec.get("log_path") or next(iter(QUEUE_LOGS.glob(f"{job_id}-*.log")), None)
    fp = fingerprint_livelog(live) if live else {}
    if live and not fp.get("iterations"):
        fatal = livelog_fatal(live)
        if fatal:
            fp["fatal"] = fatal
    sc = scrape_runlog(runlog) if runlog else {"failed_cases": [], "verify_tail": [], "lint": [], "transcript": None}
    sig, tags = classify(rec, fp, sc)
    cause_kind = (fp.get("fatal") or {}).get("kind") if sig == (fp.get("fatal") or {}).get("cause") else None
    fpub = {k: v for k, v in fp.items() if k != "reads"}
    ts = rec.get("persisted_at")
    if not ts:
        try:
            ts = _iso(Path(QUEUE_LOGS / f"{job_id}.done.json").stat().st_mtime)
        except Exception:
            ts = _iso()
    row = {
        "ts": ts, "job_id": job_id, "label": label, "bundle": bundle,
        "task_file": rec.get("task_file"),
        "plan": run, "slice": sid, "round": kind,
        "model": rec.get("model"), "host": rec.get("lane") or rec.get("host_pref"),
        "exit": rec.get("exit_code"), "verdict": "FAIL",
        "reason": rec.get("terminal_reason"), "class": rec.get("failure_class"),
        "detail": rec.get("failure_detail"),
        "iterations": fp.get("iterations"), "iteration_cap": fp.get("iteration_cap") or rec.get("max_iters"),
        "duration_s": rec.get("active_s") if rec.get("active_s") else fp.get("duration_s"),
        "continues": rec.get("continues"),
        "fingerprint": fpub,
        "failed_cases": sc["failed_cases"][:40], "verify_tail": sc["verify_tail"],
        "lint": sc["lint"],
        "paths": {"livelog": str(live) if live else None, "runlog": str(runlog) if runlog else None,
                  "transcript": sc.get("transcript") or rec.get("resume_transcript")},
        "signature": sig, "tags": tags, "cause_kind": cause_kind,
    }
    return row


def _link_same_as(row, existing):
    """SAME_AS: the most recent EARLIER row with the same signature -- same slice first,
    then any slice. Also flags an identical failing-case set."""
    if str(row.get("signature") or "") in NO_REPEAT:
        row["same_as"] = row["same_as_scope"] = row["same_cases_as"] = None
        return          # an operator stop is not a cause that "repeats"
    prior = [r for r in existing if r.get("job_id") != row["job_id"] and r.get("signature")
             and str(r.get("ts") or "") <= str(row["ts"] or "")]
    same = [r for r in prior if r["signature"] == row["signature"]]
    in_slice = [r for r in same if row.get("slice") and r.get("slice") == row.get("slice")
                and r.get("plan") == row.get("plan")]
    pick = (in_slice or same or [None])[-1]
    row["same_as"] = pick["job_id"] if pick else None
    row["same_as_scope"] = ("slice" if in_slice else "other-slice") if pick else None
    cs = set(row.get("failed_cases") or [])
    row["same_cases_as"] = None
    if cs:
        for r in reversed(prior):
            if r.get("slice") == row.get("slice") and set(r.get("failed_cases") or []) == cs:
                row["same_cases_as"] = r["job_id"]
                break


def record_job(job_id, rec=None):
    """Append one terminal-failed job (idempotent). FAIL-OPEN: returns True if a row was
    written, False otherwise; never raises."""
    try:
        rows = load_rows()
        if any(r.get("job_id") == job_id and not r.get("event") for r in rows):
            return False
        rec = rec if rec is not None else _job_record(job_id)
        if not _is_terminal_failed(rec):
            return False
        row = build_row(job_id, rec)
        _link_same_as(row, [r for r in rows if not r.get("event")])
        _append(row)
        return True
    except Exception as e:
        try:
            print(f"[failure-ledger] record {job_id} failed (ignored): {e}", file=sys.stderr)
        except Exception:
            pass
        return False


def sweep():
    """Backfill every terminal-failed job not yet in the ledger, oldest first, and note
    newly superseded ones. FAIL-OPEN. Returns the number of rows appended."""
    n = 0
    try:
        sj = _state_jobs()
        rows = load_rows()
        known = {r.get("job_id") for r in rows if not r.get("event")}
        sup_known = {r.get("job_id") for r in rows if r.get("event") == "superseded"}
        ids = set(sj)
        try:
            ids |= {p.name[:-len(".done.json")] for p in QUEUE_LOGS.glob("*.done.json")}
        except Exception:
            pass
        cands = []
        for jid in ids:
            rec = _job_record(jid, sj)
            if _is_terminal_failed(rec) and jid not in known:
                ts = rec.get("persisted_at") or rec.get("launched_at") or rec.get("enqueued_at") or ""
                cands.append((str(ts), jid, rec))
        for _ts, jid, rec in sorted(cands):
            if record_job(jid, rec):
                n += 1
        for jid, j in sj.items():
            if j.get("superseded") and jid in known | {c[1] for c in cands} and jid not in sup_known:
                try:
                    _append({"event": "superseded", "job_id": jid, "ts": j.get("superseded_at") or _iso(),
                             "superseded_by": j.get("superseded_by")})
                except Exception:
                    pass
    except Exception as e:
        try:
            print(f"[failure-ledger] sweep failed (ignored): {e}", file=sys.stderr)
        except Exception:
            pass
    try:        # triage handoff packets (read-only, fail-open); see triage_packets.py
        import triage_packets
        triage_packets.refresh()
    except Exception:
        pass
    return n


# --------------------------------------------------------------------------- the ONE counter
def author_label_re(run_label, sid):
    """Labels of a slice's author/refine queue jobs -- the single definition (the slicer's
    budget regex is this one)."""
    r, s = re.escape(str(run_label)), re.escape(str(sid))
    return re.compile(r"^(?:auto-author-%s-%s(?:-c\d+|-esc)?|auto-refine-%s-%s-r\d+)$" % (r, s, r, s))


def lifetime_job_ids(run_label, sid):
    """Every author/refine job id ever queued for this slice -- from the queue state plus the
    never-pruned <id>.done.json sidecars, so neither queue pruning nor a --retry-slice state
    reset can un-count one. FAIL-OPEN (returns [])."""
    try:
        rx = author_label_re(run_label, sid)
        out = {}
        for jid, j in _state_jobs().items():
            if rx.match(str(j.get("label") or "")):
                out[jid] = str(j.get("enqueued_at") or j.get("persisted_at") or "")
        for p in QUEUE_LOGS.glob("*.done.json"):
            jid = p.name[:-len(".done.json")]
            if jid in out:
                continue
            key = (str(QUEUE_LOGS), jid)
            if key not in _LABEL_CACHE:          # sidecars are write-once: read each file once
                d = _read_json(p) or {}
                _LABEL_CACHE[key] = (str(d.get("label") or ""), str(d.get("persisted_at") or ""))
            lbl, pat = _LABEL_CACHE[key]
            if rx.match(lbl):
                out[jid] = pat
        return [j for j, _t in sorted(out.items(), key=lambda kv: (kv[1], kv[0]))]
    except Exception:
        return []


def slice_counter(run_label, sid, state_slice=None, job_budget=DEFAULT_JOB_BUDGET,
                  lifetime_factor=DEFAULT_LIFETIME_FACTOR, attempt_cap=DEFAULT_ATTEMPT_CAP,
                  extra_ids=()):
    """THE attempt counter. `jobs_lifetime` is immutable history; `jobs_window` is the
    lifetime minus what a HUMAN retry forgave (state_slice['author_jobs_at_retry']). Both the
    slicer (status text + budget enforcement) and the dashboard call this."""
    ids = list(lifetime_job_ids(run_label, sid))
    for j in (extra_ids or ()):
        if j not in ids:
            ids.append(j)
    ss = state_slice or {}
    for j in (ss.get("author_job_ids") or []):
        if j not in ids:
            ids.append(j)
    lifetime = len(ids)
    window = max(0, lifetime - int(ss.get("author_jobs_at_retry") or 0))
    life_cap = lifetime_factor * job_budget
    attempts = int(ss.get("author_attempts") or 0)
    last_sig = same_as = None
    try:
        mine = [r for r in load_rows() if not r.get("event") and r.get("job_id") in set(ids)]
        if mine:
            last = sorted(mine, key=lambda r: str(r.get("ts")))[-1]
            last_sig, same_as = last.get("signature"), last.get("same_as")
    except Exception:
        pass
    over = lifetime >= life_cap or window >= job_budget
    text = f"author jobs {window}/{job_budget} (lifetime {lifetime}/{life_cap}) - attempts {attempts}/{attempt_cap}"
    if last_sig:
        text += f" - last cause: {last_sig}"
        if same_as:
            text += f" (same cause as {same_as})"
    return {"jobs_window": window, "jobs_lifetime": lifetime, "job_budget": job_budget,
            "lifetime_cap": life_cap, "attempts": attempts, "attempt_cap": attempt_cap,
            "last_signature": last_sig, "same_as": same_as, "at_cap": over, "text": text, "job_ids": ids}


# --------------------------------------------------------------------------- prompt feedback
_HINTS = {
    "read-loop": "the last rounds spent most iterations READING {x}; do not read it -- edit refimpl.py/TASK.md/the fixture and RUN the self-check",
    "rewrite-loop": "the last rounds re-wrote {x} from scratch again and again; make small edit_file changes and re-run the self-check after each one",
    "edit-never-reverified": "the last rounds ended on an edit that was never re-run; run `python3 auto-harness-check.py` immediately after every edit",
    "literal-cap": "the last rounds were rejected for a Must-contain list longer than the plan allows; keep only the plan's literals plus at most 8",
    "write-thrash": "the last rounds thrashed writing {x}; make one complete write, then fix only what the self-check lists",
    "format-error-loop": "the last rounds could not emit a parseable tool call; send ONE complete tool call per turn and write long files in several smaller pieces",
    "loop-detected": "the last rounds repeated the same reads/writes without progress; act on what you already read -- make an edit, then run the self-check",
    "stop-gate-failed": "the last rounds called task_complete while a required file or Must-contain literal was still missing; check every Must-contain literal is present before finishing",
    "reasoning-runaway": "the last rounds spent whole turns thinking with no tool call; decide in a sentence or two and make the tool call",
    "model-nonconvergence-other": "the last rounds ran out of iterations without a clear single cause; read the FAILED CASE lines below and fix the ONE shared cause first",
}


def signature_hint(run_label, sid):
    """One line for the next author prompt when the SAME signature appeared in two of the
    slice's last failures. '' otherwise. FAIL-OPEN."""
    try:
        ids = set(lifetime_job_ids(run_label, sid))
        rows = sorted([r for r in load_rows() if not r.get("event") and r.get("job_id") in ids],
                      key=lambda r: str(r.get("ts")))
        if len(rows) < 2 or rows[-1].get("signature") != rows[-2].get("signature"):
            return ""
        sig = rows[-1]["signature"]
        base, _, x = sig.partition(":")
        tpl = _HINTS.get(base)
        if not tpl:
            return ""
        return ("## Repeat-failure hint (failure ledger)\nThe previous two failed rounds of THIS slice had the same cause "
                f"(`{sig}`): " + tpl.format(x=x or "a file") + ".\n")
    except Exception:
        return ""


# --------------------------------------------------------------------------- read tools (qctl)
def _parse_since(s):
    m = re.fullmatch(r"(\d+)\s*([smhd])", str(s or "").strip())
    if not m:
        return None
    return time.time() - int(m.group(1)) * {"s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def _epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0.0


def select_rows(bundle=None, slice_id=None, since=None, job=None):
    cut = _parse_since(since) if since else None
    out = []
    for r in load_rows():
        if r.get("event"):
            continue
        if job and r.get("job_id") != job:
            continue
        if bundle and bundle not in (r.get("bundle"), r.get("plan")):
            continue
        if slice_id and r.get("slice") != slice_id:
            continue
        if cut and _epoch(r.get("ts")) < cut:
            continue
        out.append(r)
    return sorted(out, key=lambda r: str(r.get("ts")))


def render_failures(rows, by=None):
    if not rows:
        return "no failures recorded for that selection"
    if by:
        key = {"signature": lambda r: r.get("signature") or "?", "slice": lambda r: f"{r.get('plan') or '-'}/{r.get('slice') or '-'}",
               "bundle": lambda r: r.get("bundle") or r.get("plan") or "-"}[by]
        groups = {}
        for r in rows:
            groups.setdefault(key(r), []).append(r)
        lines = [f"{'COUNT':>5}  {by.upper():46} LAST JOB       LAST TS"]
        for k, rs in sorted(groups.items(), key=lambda kv: -len(kv[1])):
            lines.append(f"{len(rs):>5}  {k[:46]:46} {rs[-1]['job_id']}  {rs[-1].get('ts')}")
        return "\n".join(lines)
    lines = [f"{'TS':20} {'JOB':12} {'ROUND':8} {'SLICE':22} {'ITER':>6}  SIGNATURE (same cause as)"]
    for r in rows:
        it = f"{r.get('iterations') or '-'}/{r.get('iteration_cap') or '-'}"
        same = f"  <- {r['same_as']}" if r.get("same_as") else ""
        lines.append(f"{str(r.get('ts'))[:19]:20} {r['job_id']:12} {str(r.get('round') or '-'):8} {str(r.get('slice') or r.get('label'))[:22]:22} {it:>6}  {r.get('signature')}{same}")
    return "\n".join(lines)


def render_job(r):
    fp = r.get("fingerprint") or {}
    L = [f"job        {r['job_id']}  ({r.get('label')})", f"when       {r.get('ts')}   round={r.get('round')}  plan={r.get('plan')}  slice={r.get('slice')}",
         f"model/host {r.get('model')} / {r.get('host')}   exit={r.get('exit')} verdict={r.get('verdict')}",
         f"reason     {r.get('reason')}  class={r.get('class')}  {r.get('detail') or ''}",
         f"iterations {r.get('iterations')}/{r.get('iteration_cap')}   duration_s={r.get('duration_s')}   continues={r.get('continues')}",
         f"SIGNATURE  {r.get('signature')}   all tags: {', '.join(r.get('tags') or [])}",
         f"SAME AS    {r.get('same_as') or '-'} ({r.get('same_as_scope') or '-'})   same failing cases as: {r.get('same_cases_as') or '-'}",
         f"tools      {fp.get('tool_counts')}", f"top read   {fp.get('top_read')}", f"top written{fp.get('top_written')}",
         f"rewrites   {fp.get('rewrites')}   longest read-only streak: {fp.get('longest_readonly_streak')}",
         f"verified after last edit: {fp.get('verified_after_last_edit')}   lint: {r.get('lint')}",
         f"paths      {r.get('paths')}", "FAILED CASES:"]
    L += [f"  - {c}" for c in (r.get("failed_cases") or [])] or ["  (none captured)"]
    L.append("last verify output:")
    L += ["  | " + x for x in (r.get("verify_tail") or [])[-40:]]
    return "\n".join(L)


def timeline(run_label, sid):
    """Chronological story of one slice: every author/refine job (ok or failed), its gate
    verdict, second-opinion, with signatures. [(ts, text)]"""
    ev = []
    ledger = {r["job_id"]: r for r in load_rows() if not r.get("event")}
    for jid in lifetime_job_ids(run_label, sid):
        rec = _job_record(jid)
        ts = str(rec.get("persisted_at") or rec.get("launched_at") or rec.get("enqueued_at") or "")
        kind = parse_label(rec.get("label"), rec.get("bundle"))[0]
        r = ledger.get(jid)
        if r:
            txt = (f"{kind:8} {jid} FAILED {r.get('reason')} {r.get('iterations')}/{r.get('iteration_cap')} it -> {r.get('signature')}"
                   + (f"  (same cause as {r['same_as']})" if r.get("same_as") else ""))
            if r.get("failed_cases"):
                txt += f"  cases: {'; '.join(r['failed_cases'][:3])}"
        else:
            txt = f"{kind:8} {jid} {rec.get('status') or '?'}"
            if rec.get("superseded"):
                txt += f" (superseded by {rec.get('superseded_by')})"
        ev.append((ts, txt))
        g = _read_json(QUEUE_LOGS / f"{jid}.gate.json")
        if g:
            ev.append((str(g.get("ts") or ts), f"gate     {jid} verdict={g.get('verdict')} review={g.get('review_verdict')}"))
        so = QUEUE_LOGS / f"{jid}-secondop" / "report.md"
        if so.exists():
            ev.append((_iso(so.stat().st_mtime), f"secondop {jid} report {so.name}"))
    return sorted(ev, key=lambda e: e[0])


def render_timeline(run_label, sid, state_slice=None):
    ev = timeline(run_label, sid)
    c = slice_counter(run_label, sid, state_slice)
    head = f"{run_label}/{sid}: {c['text']}"
    return head + "\n" + ("\n".join(f"{ts[:19]:20} {tx}" for ts, tx in ev) if ev else "(no author jobs found)")


def _core_label(label):
    core = _PREFIX.sub("", str(label or ""))
    core = re.sub(r"-(?:c\d+|esc)$", "", core)
    return re.sub(r"-r\d+$", "", core)


def signature_hint_for_label(base_label):
    """signature_hint for auto's own label (a.label == the slicer's `<run>-<sid>`): the same
    two-in-a-row rule over the ledger rows whose label reduces to it. '' otherwise."""
    try:
        rows = sorted([r for r in load_rows() if not r.get("event") and _core_label(r.get("label")) == str(base_label)],
                      key=lambda r: str(r.get("ts")))
        if len(rows) < 2 or rows[-1].get("signature") != rows[-2].get("signature"):
            return ""
        sig = rows[-1]["signature"]
        if sig in NO_REPEAT:
            return ""
        base, _, x = sig.partition(":")
        tpl = _HINTS.get(base)
        if not tpl:
            return ""
        return ("\n## Repeat-failure hint (failure ledger)\nThe previous two failed rounds of THIS slice had the same cause "
                f"(`{sig}`): " + tpl.format(x=x or "a file") + ".\n")
    except Exception:
        return ""


def author_job_ids_for(run_label, sid):
    return lifetime_job_ids(run_label, sid)


def _main(argv):
    cmd = argv[1] if len(argv) > 1 else "sweep"
    if cmd == "sweep":
        print(f"appended {sweep()} row(s) -> {LEDGER}")
    elif cmd == "show":
        for r in load_rows():
            if r.get("event") or (len(argv) > 2 and r.get("job_id") != argv[2]):
                continue
            print(json.dumps(r, indent=1) if len(argv) > 2 else
                  f"{r.get('ts')} {r.get('job_id')} {r.get('label')} {r.get('signature')} same_as={r.get('same_as')}")
    elif cmd == "counter" and len(argv) > 3:
        c = slice_counter(argv[2], argv[3])
        print(c["text"])
    else:
        print(__doc__)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv))
