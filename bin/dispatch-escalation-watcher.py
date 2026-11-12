#!/usr/bin/env python3
"""Scheduled watcher that gets a STUCK slice/job in front of a reviewer.

THE GAP THIS CLOSES
-------------------
2026-09-19: slice s4-email-confirm-return-key of bg-actions-s1-item re-dispatched
itself 11 times against a property that was already satisfied at the chain tip.
Nothing stopped it and nothing told anyone; a human happened to notice. The
slicer's own guard (MAX_IDENTICAL_AUTHOR_FAILURES in ollama-dispatch-slice) now
parks such a slice in ESCALATED after 2 identical failures -- but ESCALATED is
just a word in a JSON file. Nobody reads it at 3am.

This watcher is the delivery half. It runs from launchd
(com.example.dispatch-escalation-watcher, StartInterval 300) so there is nothing to
re-arm, and it works with ZERO open Claude sessions:

  1. DETECT   -- three independent sources, so a loop that dodges one is caught
                 by another (see SOURCES below).
  2. RECORD   -- a durable, never-pruned context file under
                 ~/.ollama-dispatch/escalations/ with everything a reviewer needs
                 (plan, slice, reason, attempts, worktree, job log tails).
  3. REVIEW   -- spawn a STANDALONE headless `claude -p` on that context file.
                 This is the no-open-session answer: `claude -p` does not attach
                 to, resume, or require any existing session, tmux window or ttyd
                 tunnel -- launchd can start one on a locked, unattended machine.
                 Its verdict lands next to the context file as <stem>.review.md.
                 The review agent is given a READ-ONLY tool allowlist and is told
                 in the prompt to diagnose and recommend, never to edit product
                 code or re-enqueue anything. A stuck loop must not be "fixed" by
                 an unsupervised agent at 3am; it must be UNDERSTOOD by one, so a
                 human or the coordinator can act in seconds instead of hours.
  4. SURFACE  -- append to ESCALATIONS.md (the index the SessionStart hook reads,
                 so the next session that opens is told), and fire a local
                 desktop notification. A chain STAGED for landing is not stuck:
                 its "READY TO LAND" notice goes to READY-TO-LAND.md beside it
                 (same row format, same ping, NOT counted by the hook/qctl).

WHY NOT ANOTHER Monitor/watch loop: those die with the session that armed them,
and someone has to remember to re-arm. That is precisely how last night ran
unattended. launchd survives logout and reboot.

NOISE DISCIPLINE. Every escalation is deduped in a durable ledger keyed by
identity + signature, so a slice parked in ESCALATED is announced exactly ONCE,
not every 5 minutes. Routine failures, routine done jobs and needs-review rows
are NOT escalations and are never announced -- only the three sources below.

SOURCES
  A. slice status == 'escalated' in any slice-runs/*.json.
     The slicer's own no-progress guard. Highest confidence.
  B. author_attempts >= ATTEMPTS_ESCALATE on a slice that is still not satisfied.
     Catches a loop whose failure SIGNATURE keeps changing (which resets the
     slicer's identical-failure streak and would otherwise loop forever).
  C. a queue job RUNNING longer than STUCK_JOB_MINUTES.
     Catches a single wedged worker (the read_thrash class) independently of the
     slicer, including for non-sliced dispatches.
  D. a queue job parked as `needs_opus`.
     The queue's own escalation lane -- a state literally named for wanting an
     Opus reviewer, which nothing was actually fetching one for.
  E. a plan whose WHOLE-CHAIN integration gate failed (2026-09-20).
     Every slice of the plan passed its own adversarial verify, and then the
     modules did not fit together: an import broke, a cross-module call no
     longer binds to its callee's signature, or the target repo's own suite
     went red. That gate (ollama-dispatch-slice's `integration` record, written
     by dispatch-integration-check.py) is fail-closed, so the chain is NOT
     reported ready to integrate -- but, exactly like source A, that is just a
     word in a JSON file until something announces it. A whole plan being
     silently un-integratable is the most expensive thing on this list.

Everything here is advisory and fail-open: this tool must never be able to break
a dispatch. Exceptions are contained per-source.

    --once        one pass (what launchd runs)
    --dry-run     detect + print, write nothing, spawn nothing
    --self-test   offline unit tests
"""
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

DISPATCH_DIR = Path.home() / ".ollama-dispatch"
SLICE_RUNS = DISPATCH_DIR / "slice-runs"
ESC_DIR = DISPATCH_DIR / "escalations"
LEDGER = ESC_DIR / "ledger.json"
INDEX_MD = ESC_DIR / "ESCALATIONS.md"
# Ready-to-land notices (a STAGED chain) are not escalations: nothing is stuck and no
# review runs. They used to be "- [ ] " rows in INDEX_MD, which the SessionStart hook
# counts as "stuck slice/job(s)" (2026-10-03, phase-2 item d). They go here instead;
# the desktop ping is unchanged. qctl/the hook read only INDEX_MD.
READY_MD = ESC_DIR / "READY-TO-LAND.md"
QUEUE_PY = Path.home() / "bin" / "ollama-queue.py"
QUEUE_LOGS = Path.home() / "bin" / "ollama-queue-logs"
RUNS_API = "http://127.0.0.1:7684/api/runs"
AUTO_RUNS = DISPATCH_DIR / "auto-runs"
WORKTREES = DISPATCH_DIR / "worktrees"
# The model review of an escalation is a BOUNDED job: a "<400 words" answer needs ~1K output
# tokens, so the cap is generous for a verdict plus a small diff yet ends a runaway in ~1.5 min
# instead of the profile's 32768 (job 2ac42be77bcc, 2026-10-09: 5+ min of one unbounded generation).
# role=review = the card's NON-thinking reviewer sampling (a reviewer has nothing to think for
# 32K tokens about; thinking content is what ran away).
ESC_REVIEW_MAX_TOKENS = 6144
ESC_REVIEW_ROLE = "review"

# --- thresholds (judgment, with the evidence that set them) ------------------
# ATTEMPTS_ESCALATE: s4 burned 11 authoring attempts. Two identical failures is
# already enough for the slicer to park a slice; this source exists for the case
# where the signature CHANGES each round, so it must be loose enough to tolerate
# a genuine retry-and-recover (attempt 2 succeeding is normal) and tight enough
# to fire in minutes, not hours. 4 attempts ~= 15-25 min of authoring on this
# hardware: past any honest retry, far short of an overnight window.
ATTEMPTS_ESCALATE = 4
# STUCK_JOB_MINUTES: the longest HEALTHY converging authoring dispatch observed
# in ollama-queue-logs is well under an hour; the BFMR read_thrash run spun for
# far longer producing nothing. 90 min never fires on a healthy job and still
# catches a wedge inside the first two hours of an overnight window.
STUCK_JOB_MINUTES = 90
# POLL: matches the com.example.dispatch-ack-reconcile precedent.
POLL_SECONDS = 300
REVIEW_TIMEOUT = 1800
REVIEW_MODEL = "local"  # retired: reviews are queue jobs (REVIEW_QUEUE_MODEL)
LOG_TAIL_LINES = 80
# A blocked review (session/usage limit, not logged in, CLI never ran the
# prompt) is an INFRA failure, not a verdict. It is retried on a later pass --
# but not forever: after this many blocked attempts on the same escalation the
# key stays announced and the index row says the review never ran, so a human
# sees it instead of the watcher burning a CLI spawn every POLL_SECONDS.
REVIEW_BLOCKED_MAX_ATTEMPTS = 8

SATISFIED = ("done", "skipped")
UNSATISFIED_LOOPABLE = ("pending", "failed", "enqueued", "blocked")

REVIEW_TOOLS = [
    "Read", "Grep", "Glob",
    "Bash(git log:*)", "Bash(git diff:*)", "Bash(git show:*)", "Bash(git status:*)",
    "Bash(python3 /Users/user/bin/ollama-queue.py results:*)",
    "Bash(python3 /Users/user/bin/ollama-queue.py status:*)",
]


# --------------------------------------------------------------------------
# detection
# --------------------------------------------------------------------------
def _load_json(p):
    try:
        with open(p) as fh:
            return json.load(fh)
    except Exception:
        return None


def plan_files(slice_runs=SLICE_RUNS):
    """Live plan state files only. `.bak*` snapshots and archive/ are history:
    escalating off a stale backup would announce something already resolved."""
    d = Path(slice_runs)
    if not d.is_dir():
        return []
    out = []
    for p in sorted(d.iterdir()):
        if p.is_dir() or p.suffix != ".json":
            continue
        if ".bak" in p.name or p.name.endswith(".advance.log"):
            continue
        out.append(p)
    return out


def _plan_cancelled(label):
    """plan_cancel.cancelled(label) -- a human-cancelled plan (or a sub-plan of
    one) is never reviewed or self-healed. Never raises."""
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import plan_cancel
        return plan_cancel.cancelled(label, runs_dir=SLICE_RUNS)
    except Exception:
        return None


def subplan_names(slice_runs=SLICE_RUNS):
    """Stems of every live plan-state file, used to spot a SUPERSEDED escalation."""
    return {p.stem for p in plan_files(slice_runs)}


def is_superseded(plan, sid, known_plans):
    """An ESCALATED slice that was already acted on by re-slicing it into its own
    sub-plan (`<plan>-<slice_id>.json`) is NOT stuck -- the escalation was
    resolved and the slice's status field is simply the last thing written.
    Without this check the watcher's first real pass announced 5 historical,
    long-resolved escalations (bg-actions/s1-item, bg-crypto/s1-crypto,
    bg-eraser/s1-invoke + s2-verify, bg-state/s1-init-db), every one of which has
    a live sub-plan beside it. A watcher that cries wolf on install gets muted,
    which would leave exactly the gap it was built to close."""
    return bool(plan) and bool(sid) and ("%s-%s" % (plan, sid)) in (known_plans or set())


_AUTHOR_LABEL_PREFIXES = ("auto-author-", "auto-refine-", "auto-fix-", "auto-")
_LIVE_ROW_STATUSES = ("running", "queued", "pending", "scheduled", "held", "paused", "planned")


def has_active_author_row(plan, sid, queue_rows):
    """True when the queue holds a LIVE (pending/running/held...) authoring/refine row for
    this slice -- i.e. a replacement is already healing it. `queue_rows` = parse_queue_status
    dicts. esc-review-/gate-/regate- rows never count (a review is not a replacement)."""
    stem = "%s-%s" % (plan, sid)
    for r in queue_rows or ():
        lab = str(r.get("label") or "")
        st = str(r.get("status") or "").strip().lower()
        if st in _LIVE_ROW_STATUSES and lab.startswith(_AUTHOR_LABEL_PREFIXES) and stem in lab:
            return True
    return False


def detect_slice_escalations(state, plan, known_plans=None, queue_rows=None):
    """Sources A and B over one loaded slice-run state. Pure; no I/O.
    `queue_rows` (optional, parse_queue_status dicts): source B (attempt-count heuristic) is
    SUPPRESSED while a live authoring row for the slice exists -- the cumulative attempt count
    is not reset by a re-spec/resolve, so 2026-10-09 aw-codec-floor/s2 was reviewed while its
    replacement job sat pending (the old failed rows already resolved)."""
    found = []
    slices = (state or {}).get("slices") or {}
    if not isinstance(slices, dict):
        return found
    for sid, s in slices.items():
        if not isinstance(s, dict):
            continue
        status = str(s.get("status") or "")
        attempts = s.get("author_attempts") or 0
        try:
            attempts = int(attempts)
        except Exception:
            attempts = 0
        if status == "escalated":
            if is_superseded(plan, sid, known_plans):
                continue
            found.append({
                "source": "A",
                "kind": "slice",
                "plan": plan,
                "slice_id": sid,
                "signature": str(s.get("escalation_reason") or s.get("author_failure_sig") or "")[:200],
                "reason": str(s.get("escalation_reason") or "slice parked in ESCALATED"),
                "slice": s,
            })
        elif status in UNSATISFIED_LOOPABLE and attempts >= ATTEMPTS_ESCALATE:
            if queue_rows is not None and has_active_author_row(plan, sid, queue_rows):
                continue
            found.append({
                "source": "B",
                "kind": "slice",
                "plan": plan,
                "slice_id": sid,
                "signature": "attempts>=%d" % ATTEMPTS_ESCALATE,
                "reason": ("%d authoring attempts with status=%s and no convergence "
                           "(failure signature keeps changing, so the slicer's "
                           "identical-failure guard never trips)" % (attempts, status)),
                "slice": s,
            })
    return found


# Integration-gate statuses that mean "this chain is NOT certified". `passed` is
# the only clean one; `skipped` is the debug escape hatch and is deliberately
# announced too, because a gate that was turned off and forgotten is exactly how
# an un-integratable chain gets reported as done.
INTEGRATION_BAD = ("failed", "unavailable", "skipped")
# `staged` (2026-09-23) is the slicer's "the chain integrates cleanly onto the
# default branch and a scratch `integrate/<slug>-<sha8>` branch holds the result;
# nothing has been written to main". That is NOT a defect and gets NO headless
# review (there is nothing to diagnose) -- but it IS the one event the operator
# has to act on for the work to land, so it is surfaced on the index and the
# desktop exactly once per staged commit, with the landing command on the row.
INTEGRATION_STAGED = "staged"


def detect_vanished_jobs(state, plan, queue_ids, has_sidecar):
    """Source F (2026-09-23, cc-waitlist-r2 s1 / e1b1a6ceea18). Pure given its
    inputs. An ENQUEUED slice whose coding job is in neither the live queue table
    nor any completion sidecar: no completion event will ever fire for it, so the
    chain sits ENQUEUED forever. The slicer's sweep now re-gates it (bounded); this
    source puts the fact on the index so the queue's lost job is not invisible.
    NO review is spawned (there is nothing to diagnose in the code). An EMPTY
    queue table is 'unknown' (daemon down) and announces nothing."""
    if not queue_ids:
        return []
    found = []
    slices = (state or {}).get("slices") or {}
    if not isinstance(slices, dict):
        return found
    for sid, s in slices.items():
        if not isinstance(s, dict) or str(s.get("status") or "") != "enqueued":
            continue
        jid = str(s.get("job_id") or "")
        if not jid or jid in queue_ids or has_sidecar(jid):
            continue
        found.append({
            "source": "F",
            "kind": "slice",
            "plan": plan,
            "slice_id": sid,
            "no_review": True,
            "signature": "vanished:%s" % jid,
            "reason": ("coding job %s VANISHED: not in the queue table and no completion "
                       "sidecar. The slicer sweep re-gates the intact harness "
                       "(bounded, then escalates); if this row persists, check why the "
                       "queue dropped the job." % jid),
            "slice": s,
        })
    return found


def detect_integration_escalations(state, plan):
    """Source E over one loaded slice-run state. Pure; no I/O.

    FORWARD/BACKWARD COMPATIBLE BY CONSTRUCTION: a plan state written by the
    previous slicer has no `integration` key at all, and no key means "the gate
    never ran on this chain", which is NOT an escalation -- an in-flight chain
    from before the gate existed must not be announced retroactively. Only an
    explicit bad status speaks -- plus `staged`, announced as a no-review
    ready-to-land notice (see INTEGRATION_STAGED)."""
    integ = (state or {}).get("integration")
    if not isinstance(integ, dict):
        return []
    status = str(integ.get("status") or "")
    if status == INTEGRATION_STAGED:
        sc = str(integ.get("staged_commit") or "")[:12]
        return [{
            "source": "E",
            "kind": "integration-staged",
            "plan": plan,
            "slice_id": None,
            "no_review": True,
            # Keyed by the staged commit: re-staging the same tip is silent; a
            # chain that moved and re-staged is a new landing decision.
            "signature": "integration:staged:%s" % (sc or "?"),
            "reason": ("the converged chain is STAGED for landing (%s onto %s; main "
                       "untouched) -- review the scratch branch, then: "
                       "(old slicer retired -- use mini-swe)"
                       % (integ.get("integrate_branch") or "integrate/?",
                          integ.get("onto") or "default branch")),
            "integration": integ,
        }]
    if status not in INTEGRATION_BAD:
        return []
    summary = str(integ.get("summary") or "")
    stages = integ.get("failed_stages") or []
    return [{
        "source": "E",
        "kind": "integration",
        "plan": plan,
        "slice_id": None,
        # The signature is the STAGE SET, not the prose: a chain re-checked after
        # a partial fix (fewer stages red) is a genuinely different state and is
        # announced again, while re-running the same failing gate every 5 minutes
        # stays silent.
        "signature": "integration:%s:%s" % (status, ",".join(sorted(map(str, stages)))),
        "reason": ("the whole-chain integration gate is %s: %s"
                   % (status, summary or "no summary recorded")),
        "integration": integ,
    }]


# private doc-summary jobs (label docsum-<8hex>): their dirs hold user documents, never review/escalate them
try:
    from docsum_private import is_private_label
except ImportError:  # script copied elsewhere (tests): same definition, kept in step by test_docsum_ui_consumers
    import re as _re_dp

    def is_private_label(label):
        return _re_dp.fullmatch(r"docsum-[0-9a-f]{8}", str(label or "")) is not None




def drop_private_jobs(found):
    """Escalations about private doc-summary jobs are discarded before any context file or `claude -p` review exists."""
    return [e for e in found if not is_private_label(e.get("label"))]


_STATUS_RE = re.compile(
    r"^\[(?P<st>[a-z_]+)\s*\]\s+(?P<id>[0-9a-f]{6,})\s+(?P<label>\S+)(?P<rest>.*)$")


def parse_queue_status(text):
    """Rows from `ollama-queue.py status`. The id is field 3, NOT field 2:
    the status column is bracketed and space-padded (`[running ] <id> ...`), a
    gotcha that silently turned an earlier drain loop into a no-op."""
    rows = []
    for line in (text or "").splitlines():
        m = _STATUS_RE.match(line.strip())
        if m:
            rows.append({"status": m.group("st"), "id": m.group("id"),
                         "label": m.group("label"), "rest": m.group("rest")})
    return rows


def _job_started_epoch(job_id):
    """Best-effort launch time: the queue state row, else the log file's mtime."""
    st = _load_json(DISPATCH_DIR.parent / ".ollama-queue" / "state.json")
    for base in (Path.home() / ".ollama-queue" / "state.json",
                 Path.home() / "bin" / "ollama-queue-state.json"):
        st = st or _load_json(base)
    if isinstance(st, dict):
        for j in st.get("jobs") or []:
            if isinstance(j, dict) and j.get("id") == job_id:
                for k in ("started_at", "launched_at", "start_time"):
                    v = j.get(k)
                    if isinstance(v, (int, float)):
                        return float(v)
                    if isinstance(v, str):
                        try:
                            return time.mktime(time.strptime(v[:19], "%Y-%m-%dT%H:%M:%S"))
                        except Exception:
                            pass
    for p in sorted(QUEUE_LOGS.glob("%s-*.log" % job_id)) if QUEUE_LOGS.is_dir() else []:
        try:
            return p.stat().st_ctime
        except Exception:
            pass
    return None


def detect_stuck_jobs(now=None, status_text=None):
    """Sources C and D, off one `ollama-queue.py status` read.

    D is the queue's OWN escalation lane: a job the repair loop gave up on and
    parked as `needs_opus` (e.g. esim-global-s1-parse-global 13a235b378c0,
    'untrusted baseline: launch baseline was dirty'). That state is literally
    named for wanting an Opus reviewer and nothing was fetching one -- rows just
    sat in the queue. It is a terminal state, so it needs no time threshold."""
    now = now or time.time()
    if status_text is None:
        try:
            status_text = subprocess.run([sys.executable, str(QUEUE_PY), "status"],
                                         capture_output=True, text=True, timeout=60).stdout
        except Exception:
            return []
    out = status_text
    found = []
    for row in parse_queue_status(out):
        if is_private_label(row["label"]):
            continue
        if row["status"] == "needs_opus":
            found.append({
                "source": "D",
                "kind": "job",
                "plan": None,
                "slice_id": None,
                "job_id": row["id"],
                "label": row["label"],
                "signature": "needs_opus",
                "reason": "queue parked this job as needs_opus: %s"
                          % row["rest"].strip()[:300],
            })
            continue
        if row["status"] != "running":
            continue
        started = _job_started_epoch(row["id"])
        if started is None:
            continue
        mins = (now - started) / 60.0
        if mins >= STUCK_JOB_MINUTES:
            found.append({
                "source": "C",
                "kind": "job",
                "plan": None,
                "slice_id": None,
                "job_id": row["id"],
                "label": row["label"],
                "signature": "running>=%dm" % STUCK_JOB_MINUTES,
                "reason": "job has been RUNNING for %.0f minutes (threshold %d)"
                          % (mins, STUCK_JOB_MINUTES),
            })
    return found


def detect_needs_info_jobs(jobs=None):
    """Source G: a mini-swe job PARKED on a NEEDS.md question (status paused,
    pause_reason needs_info). Notice only (no_review): nothing to diagnose, no Opus
    session, and no auto-answer/auto-resume -- only the owner answers, with `mini-answer`.
    One finding per job and ask number (the ask number is spelled as a letter in the
    signature so _norm_sig's digit masking keeps a second ask distinct)."""
    if jobs is None:
        try:
            jobs = (_load_json(Path.home() / "bin" / "ollama-queue-state.json") or {}).get("jobs") or []
        except Exception:
            return []
    out = []
    for j in jobs:
        if not isinstance(j, dict) or j.get("status") != "paused" or j.get("pause_reason") != "needs_info":
            continue
        if is_private_label(j.get("label")):
            continue
        meta = j.get("pause_meta") or {}
        try:
            n = max(1, min(int(meta.get("ask_n") or 1), 25))
        except Exception:
            n = 1
        q = " ".join(str(meta.get("question") or "(question unavailable)").split())[:200]
        out.append({
            "source": "G", "kind": "job", "plan": None, "slice_id": None,
            "job_id": j.get("id"), "label": j.get("label"),
            "signature": "needs_info:ask" + chr(96 + n),
            "no_review": True,
            "reason": 'job %s is PARKED waiting for your answer: "%s" -- reply: mini-answer %s "..."'
                      % (j.get("label"), q, j.get("id")),
        })
    return out


def _norm_sig(sig):
    """Free-text escalation reasons embed job ids, hashes, counts and timestamps, so the
    same parked slice produced a NEW key (6 for one slice, 2026-10-01) and was
    re-announced + re-reviewed each time. Mask the volatile tokens and cap the length so
    the key tracks the reason's SHAPE, not its details."""
    t = re.sub(r"[0-9a-f]{7,}", "#", str(sig or "").lower())
    t = re.sub(r"\d+", "#", t)
    return re.sub(r"\s+", " ", t).strip()[:80]


def esc_key(e):
    """Dedup identity. Includes the signature so a slice that escalates AGAIN for
    a genuinely different reason is announced again, while the same parked slice
    stays silent forever after the first announcement."""
    if e.get("kind") == "job":
        return "job:%s:%s" % (e.get("job_id"), _norm_sig(e.get("signature")))
    if e.get("kind") in ("integration", "integration-staged"):
        return "integration:%s:%s" % (e.get("plan"), _norm_sig(e.get("signature")))
    return "slice:%s:%s:%s" % (e.get("plan"), e.get("slice_id"), _norm_sig(e.get("signature")))


def _subject(e):
    """What this escalation is ABOUT, for a row/notification title. A whole-chain
    escalation names no slice and no job -- it is about the plan itself -- and
    without this it rendered as a bare '?' in the index and 'None' in the
    desktop notification."""
    if e.get("slice_id"):
        return e["slice_id"]
    if e.get("job_id"):
        return e["job_id"]
    if e.get("kind") == "integration":
        return "WHOLE-CHAIN integration gate"
    if e.get("kind") == "integration-staged":
        return "CHAIN STAGED -- ready to land"
    return "?"


def new_escalations(found, ledger):
    seen = set((ledger or {}).get("announced") or [])
    return [e for e in found if esc_key(e) not in seen]


# --------------------------------------------------------------------------
# NUMBERED RERUNS WITH A VISIBLE CAUSE (the owner, 2026-10-01)
# --------------------------------------------------------------------------
# _norm_sig masks every digit and hex id, which is what stops one parked slice from
# being announced six times -- but it ALSO swallowed a genuinely worse
# re-escalation ("2 new failures" -> "9 new failures"): same masked key, so never
# seen again. So: keep the masked key for dedup, and track the RAW signature
# alongside it. Each raw change is a numbered rerun with a one-line "what changed";
# only a WORSENING (or a new job with a different outcome class) is re-announced.
#
# THE RE-ANNOUNCE RULE (deliberately conservative -- the failure mode we are
# fixing is noise, so every ambiguous case stays silent and merely increments N):
#   1. Not alignable (the two raw signatures tokenize to different lengths)  -> NO.
#   2. Only noise changed (hex ids >= 7 chars, clock times, ISO timestamps) AND
#      rule 4 does not apply                                                -> NO,
#      and that does not even count as a rerun.
#   3. A number in a BADNESS context (its +/-2-token window names failures /
#      errors / regressions / attempts / blocked / stuck / missing) strictly
#      INCREASED                                                            -> YES.
#   4. The triggering job id changed AND the outcome class changed           -> YES.
#   5. Anything else (a number moved in a context we cannot read as badness,
#      a word changed, a count went DOWN)                                    -> NO.
RERUN_WORSE_WHEN_UP = frozenset("""
failure failures fail failed fails error errors erroring regression regressions
new attempt attempts attempted blocked stuck missing unresolved conflict conflicts
timeout timeouts crash crashes broken rejected
""".split())

_RE_HEXID = re.compile(r"^[0-9a-f]{7,}$")
_RE_CLOCK = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?(am|pm)?$")
_RE_ISOTS = re.compile(r"^\d{4}-\d{2}-\d{2}([t ][\d:.+\-z]*)?$")
_RE_INT = re.compile(r"\d+")

_OUTCOME_PATTERNS = (
    ("no-tree-change", r"no tree change|0 new failures|empty diff"),
    ("verify-failed", r"verify failed|verify_failed"),
    ("iteration-cap", r"iteration cap|hit the cap|max.iters"),
    ("stuck", r"running>=|running for"),
    ("vanished", r"vanish|disappear|no such job"),
    ("needs-opus", r"needs.opus"),
    ("blocked", r"blocked|parked|cascade"),
    ("staged", r"staged|ready to land"),
)


def _tok_core(t):
    """PURE. A token stripped of the punctuation that rides along in free text."""
    return str(t).strip().strip("`'\"(),.;:[]{}<>").lower()


def _tok_is_noise(t):
    """PURE. A token whose change can NEVER mean a worse escalation: a job id /
    sha, a wall-clock time, an ISO timestamp."""
    c = _tok_core(t)
    return bool(_RE_HEXID.match(c) or _RE_CLOCK.match(c) or _RE_ISOTS.match(c))


def _first_int(t):
    m = _RE_INT.search(str(t))
    return int(m.group(0)) if m else None


def _badness_context(toks, i):
    """PURE. The words within 2 tokens of position i that mean 'more is worse',
    including the non-digit text of the token itself ("failures=2")."""
    out = set()
    for j in range(max(0, i - 2), min(len(toks), i + 3)):
        for w in re.split(r"[^a-z]+", _tok_core(toks[j])):
            if w in RERUN_WORSE_WHEN_UP:
                out.add(w)
    return out


def sig_delta(old_raw, new_raw):
    """PURE. Compare two RAW escalation signatures that already share a masked
    esc_key (so they are the same SHAPE). Returns a dict:
      alignable  -- the two tokenize to the same length (else we refuse to read it)
      changed    -- [(old_tok, new_tok), ...] in order
      noise_only -- every change is a hex id / clock / timestamp
      worse      -- a badness-context number strictly increased
      summary    -- one line, "2 -> 9 (new failure(s))", changed tokens only
    """
    a, b = str(old_raw or "").split(), str(new_raw or "").split()
    out = {"alignable": len(a) == len(b) and bool(a), "changed": [],
           "noise_only": True, "worse": False, "summary": ""}
    if not out["alignable"]:
        out["noise_only"] = False
        out["summary"] = "the wording changed (not token-aligned): %r -> %r" % (
            str(old_raw or "")[:60], str(new_raw or "")[:60])
        return out
    bits = []
    for i, (x, y) in enumerate(zip(a, b)):
        if x == y:
            continue
        out["changed"].append((x, y))
        if _tok_is_noise(x) and _tok_is_noise(y):
            continue
        out["noise_only"] = False
        ctx = _badness_context(b, i)
        ox, oy = _first_int(x), _first_int(y)
        if ox is not None and oy is not None and oy > ox and ctx:
            out["worse"] = True
            bits.append("%s -> %s (%s)" % (x, y, " ".join(sorted(ctx))))
        else:
            bits.append("%s -> %s" % (x, y))
    out["summary"] = "; ".join(bits)[:220]
    return out


def outcome_class(e):
    """PURE. A COARSE outcome class for an escalation: two escalations in the same
    class are 'the same kind of bad'. Used only to decide whether a NEW triggering
    job is worth re-announcing, so coarse is the point."""
    fc = str((e or {}).get("failure_class") or "").strip().lower()
    sig = ("%s %s" % ((e or {}).get("signature") or "",
                      (e or {}).get("reason") or "")).lower()
    for name, pat in _OUTCOME_PATTERNS:
        if re.search(pat, sig):
            return "%s/%s" % (name, fc) if fc else name
    return fc or "other"


def rerun_assess(prior_rec, e):
    """PURE. Is this ALREADY-ANNOUNCED escalation a numbered rerun, and is it worth
    putting in front of a human again? Returns (rerun_dict_or_None, reannounce).

    rerun_dict: {n, changed, worse, note, job_id, prev_job_id, class, prev_class,
                 raw, prev_raw}. `note` is the one line that goes on the index row
                 and at the top of the context file."""
    prior_rec = prior_rec or {}
    new_raw = str(e.get("signature") or "")
    old_raw = str(prior_rec.get("raw_signature") or "")
    n_prev = int(prior_rec.get("escalation_n") or 1)
    prev_job = prior_rec.get("rerun_job_id")
    prev_cls = prior_rec.get("rerun_class")
    cls = outcome_class(e)
    if not old_raw or old_raw == new_raw:
        return None, False
    d = sig_delta(old_raw, new_raw)
    # A NEW triggering job whose outcome CLASS differs is a re-announce trigger in
    # its own right (failure_class can flip from `model` to `harness` with the reason
    # text unchanged), so it is checked BEFORE the noise short-circuit -- a job id is
    # a hex token, i.e. "noise", and would otherwise swallow exactly that case.
    new_job_diff_class = bool(e.get("job_id") and e["job_id"] != prev_job
                              and prev_cls and cls != prev_cls)
    if d["noise_only"] and not new_job_diff_class:
        # Timestamps/hashes only. Record the newer raw signature so a LATER real
        # change is diffed against what we actually last saw -- but this is not a
        # rerun and nobody is told.
        return {"n": n_prev, "noise": True, "raw": new_raw, "changed": [],
                "worse": False, "note": None, "job_id": e.get("job_id"),
                "prev_job_id": prev_job, "class": cls, "prev_class": prev_cls,
                "prev_raw": old_raw}, False
    n = n_prev + 1
    reannounce = bool(d["worse"] or new_job_diff_class)
    why = ("WORSENING" if d["worse"] else
           ("new job, different outcome class" if new_job_diff_class else
            "no worse -- logged, not re-announced"))
    note = "↻ ESCALATION #%d (%s) -- what changed: %s" % (
        n, why, d["summary"] or "a new triggering job in a different outcome class")
    if e.get("job_id"):
        note += " | triggering job: %s" % e["job_id"]
        if prev_job and prev_job != e["job_id"]:
            note += " (was %s)" % prev_job
    if prev_cls and cls != prev_cls:
        note += " | outcome class: %s -> %s" % (prev_cls, cls)
    return {"n": n, "noise": False, "raw": new_raw, "prev_raw": old_raw,
            "changed": ["%s -> %s" % (x, y) for x, y in d["changed"]],
            "worse": bool(d["worse"]), "note": note[:400],
            "job_id": e.get("job_id"), "prev_job_id": prev_job,
            "class": cls, "prev_class": prev_cls}, reannounce


def rerun_tag(e):
    """PURE. The short marker for an index row / desktop notification. '' at #1."""
    rr = (e or {}).get("rerun") or {}
    try:
        n = int(rr.get("n") or 0)
    except (TypeError, ValueError):
        n = 0
    return ("↻ #%d " % n) if n > 1 else ""


def rerun_line(e, line):
    """PURE. One human line with the rerun number in FRONT and the cause appended.
    Used for the index row and the desktop notification, so both say "#3 ... what
    changed" without anyone opening a file. Unchanged at #1."""
    rr = (e or {}).get("rerun") or {}
    return "%s%s%s" % (rerun_tag(e), line,
                       (" -- %s" % rr["note"]) if rr.get("note") else "")


def rerun_banner(e):
    """PURE. The lines that go at the TOP of the context/review file so a human
    reads the CAUSE of the rerun before anything else. [] at #1."""
    rr = (e or {}).get("rerun") or {}
    if not rr or int(rr.get("n") or 1) <= 1:
        return []
    L = ["> %s" % (rr.get("note") or ("↻ ESCALATION #%s" % rr.get("n"))),
         ">",
         "> - previous reason: %s" % (str(rr.get("prev_raw") or "?")[:300]),
         "> - this reason:     %s" % (str(rr.get("raw") or "?")[:300])]
    if rr.get("changed"):
        L.append("> - changed tokens:  %s" % "; ".join(rr["changed"])[:300])
    if not rr.get("worse"):
        L.append("> - (not read as a worsening: re-surfaced for another reason)")
    L.append("")
    return L


def track_reruns(found, ledger, ledger_path, persist=True):
    """Fold every ALREADY-ANNOUNCED escalation's raw signature into the ledger.
    Returns (ledger, revived) where `revived` are the escalations whose key was
    un-announced because the change was a worsening (or a new job with a different
    outcome class) -- each carries e['rerun'] so the row/context/notification can
    show the number and the cause. Contained: a failure here can never lose an
    escalation, it just means no rerun metadata this pass."""
    announced = set((ledger or {}).get("announced") or [])
    revived, seen = [], set()
    for e in found or []:
        k = esc_key(e)
        if k not in announced or k in seen:
            continue
        seen.add(k)
        try:
            rr, reann = rerun_assess((ledger.get("records") or {}).get(k), e)
        except Exception as ex:                      # never break a watcher pass
            print("  WARN rerun tracking failed for %s (%r)" % (k, ex))
            continue
        if not rr:
            continue
        rec = dict((ledger.get("records") or {}).get(k) or {})
        rec.update({"raw_signature": rr["raw"], "escalation_n": rr["n"],
                    "rerun_job_id": rr.get("job_id") or rec.get("rerun_job_id"),
                    "rerun_class": rr.get("class")})
        if rr.get("note"):
            rec["rerun_note"] = rr["note"]
            rec["rerun_at"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        if not persist:
            # --dry-run: SAY what would happen, write nothing. The ledger is the
            # thing that makes an escalation durable; a dry run that mutated it
            # would silently consume the very change it was asked to preview.
            if rr.get("note"):
                print("  WOULD RERUN #%d %s %s -- %s"
                      % (rr["n"], "RE-ANNOUNCE" if reann else "(log only)", k,
                         rr["note"]))
            if reann:
                e["rerun"] = rr
                revived.append(e)
            continue
        if reann:
            e["rerun"] = rr
            ledger = _ledger_unannounce(ledger_path, k, rec)
            revived.append(e)
            print("  RERUN #%d RE-ANNOUNCED %s -- %s" % (rr["n"], k, rr["note"]))
        else:
            ledger = _merge_ledger(ledger_path, k, rec)
            if rr.get("note"):
                print("  RERUN #%d logged (not re-announced) %s -- %s"
                      % (rr["n"], k, rr["note"]))
    return ledger, revived


# --------------------------------------------------------------------------
# context + delivery
# --------------------------------------------------------------------------
def _log_tail(job_id, lines=LOG_TAIL_LINES):
    if not job_id or not QUEUE_LOGS.is_dir():
        return ""
    paths = sorted(QUEUE_LOGS.glob("%s-*.log" % job_id))
    if not paths:
        return ""
    try:
        return "\n".join(paths[-1].read_text(errors="replace").splitlines()[-lines:])
    except Exception:
        return ""


_QMOD = []


def _queue_module():
    """ollama-queue.py loaded once (its chain/bundle resolvers), or None."""
    if _QMOD:
        return _QMOD[0]
    try:
        import importlib.machinery as _im
        import importlib.util as _iu
        ld = _im.SourceFileLoader("_oq_for_watcher", str(Path(__file__).resolve().parent / "ollama-queue.py"))
        sp = _iu.spec_from_loader(ld.name, ld)
        m = _iu.module_from_spec(sp)
        ld.exec_module(m)
        _QMOD.append(m)
    except Exception:
        _QMOD.append(None)
    return _QMOD[0]


_STAGE_PREFIX_RE = re.compile(r"^(?:auto-(?:author|refine)-|needs-opus-auto-|needs-opus-|gate-|regate-|secondop-|esc-review-|plan-gen-)+")


def _base_label(label):
    m = _queue_module()
    try:
        if m is not None:
            return m.default_bundle_from_label(label) or ""
    except Exception:
        pass
    s = _STAGE_PREFIX_RE.sub("", str(label or "")).strip()
    prev = None
    while prev != s:
        prev, s = s, re.sub(r"-(?:s|r|c)\d+$", "", s)
    return s.strip("-")


def _is_wt(p):
    try:
        return bool(p) and (Path(str(p)) / "TASK.md").is_file()
    except OSError:
        return False


def resolve_job_evidence(e, state_path=None, auto_runs=None, worktrees=None, dispatch_dir=None):
    """The evidence a bare JOB row (source C/D, no plan: e.g. the synthetic
    `needs-opus-auto-<label>` placeholder ollama-dispatch-auto parks, host=? pid=None) never
    carried into its context file (2026-10-09, 444c051f2538: a 499-byte stub and a model review
    that spent 7 min looking for evidence that was never written).

    Everything is derived from the LABEL: the queue row (cwd, bundle) -> the label's base ->
    the driver's chain record auto-runs/<bundle>.json (`runs[<base>]`: parked.reason / .log /
    .worktree, the round job ids, outcome) -> the worktree wt-<base> -> the driver log
    <base>-auto.log. Returns a dict with only the keys that RESOLVED to something real;
    {} when nothing did. Pure reads, never raises."""
    out = {}
    try:
        state_path = Path(state_path) if state_path else Path.home() / "bin" / "ollama-queue-state.json"
        ar = Path(auto_runs) if auto_runs else AUTO_RUNS
        wts = Path(worktrees) if worktrees else WORKTREES
        dd = Path(dispatch_dir) if dispatch_dir else DISPATCH_DIR
        row = {}
        try:
            for j in (json.loads(state_path.read_text()).get("jobs") or []):
                if j.get("id") == e.get("job_id"):
                    row = j
                    break
        except (OSError, ValueError, AttributeError):
            row = {}
        label = e.get("label") or row.get("label")
        base = _base_label(label)
        rec, chain_file = None, None
        cands = []
        if row.get("bundle") or e.get("bundle"):
            cands.append(ar / ("%s.json" % (e.get("bundle") or row.get("bundle"))))
        try:
            cands += sorted(ar.glob("*.json"))
        except OSError:
            pass
        for f in cands:
            if f.name.endswith(".attempts.json"):
                continue
            d = _load_json(f)
            r = ((d or {}).get("runs") or {}).get(base) if isinstance(d, dict) and base else None
            if isinstance(r, dict):
                rec, chain_file = r, f
                break
        if rec:
            out["chain_file"] = str(chain_file)
            for k in ("bundle", "phase", "outcome", "started_at", "updated_at"):
                if rec.get(k):
                    out[k] = rec[k]
            if rec.get("rounds"):
                out["rounds"] = [str(x) for x in rec["rounds"]]
            pk = rec.get("parked") if isinstance(rec.get("parked"), dict) else {}
            if pk.get("reason"):
                out["parked_reason"] = str(pk["reason"])
        elif row.get("bundle"):
            out["bundle"] = row["bundle"]
        pk = (rec or {}).get("parked") if isinstance((rec or {}).get("parked"), dict) else {}
        for c in (pk.get("worktree"), row.get("cwd"), wts / ("wt-%s" % base) if base else None):
            if _is_wt(c):
                out["worktree"] = str(c)
                break
        for c in (pk.get("log"), dd / ("%s-auto.log" % base) if base else None):
            try:
                if c and Path(str(c)).is_file():
                    out["driver_log"] = str(c)
                    break
            except OSError:
                pass
    except Exception:
        return {}
    return out


def _file_tail(path, lines=LOG_TAIL_LINES):
    try:
        return "\n".join(Path(path).read_text(errors="replace").splitlines()[-lines:])
    except Exception:
        return ""


def _clip_lines(text, width=300):
    return "\n".join(l if len(l) <= width else l[:width] + " ...[clipped]" for l in str(text or "").splitlines())


def driver_evidence_lines(ev, tail_rounds=2):
    """PURE-ish markdown for resolve_job_evidence(); [] when there is nothing to show."""
    if not ev:
        return []
    L = ["## driver evidence (resolved from the label by the watcher)", ""]
    for k, lab in (("bundle", "bundle"), ("chain_file", "chain record (driver state)"),
                   ("phase", "driver phase"), ("outcome", "driver outcome"),
                   ("worktree", "worktree (TASK.md, verify.sh, check_literals.py live here)"),
                   ("driver_log", "driver log")):
        if ev.get(k):
            L.append("- %s: `%s`" % (lab, ev[k]))
    if ev.get("rounds"):
        L.append("- rounds (job ids, oldest first): %s" % ", ".join("`%s`" % r for r in ev["rounds"]))
    if ev.get("parked_reason"):
        L += ["", "### park reason (full)", "```", ev["parked_reason"][:2500], "```"]
    for jid in (ev.get("rounds") or [])[-tail_rounds:]:
        t = _clip_lines(_log_tail(jid, 30))
        if t:
            L += ["", "### last lines of round job %s (failure signature)" % jid, "```", t, "```"]
    if ev.get("driver_log"):
        t = _clip_lines(_file_tail(ev["driver_log"], 40))
        if t:
            L += ["", "### driver log tail", "```", t, "```"]
    L.append("")
    return L


# Headings whose presence means the context carries evidence the review task tells the
# reviewer to read (plan state / slice record / worktree literals / driver evidence / log tail /
# integration records). A context with NONE of them is a stub: nothing for a model to diagnose.
EVIDENCE_HEADINGS = ("## plan\n", "## slice record", "## log tail", "## driver evidence",
                     "## machine-checked spec literals", "## whole-chain integration gate",
                     "## chain staged for landing", "## chain was STAGED")


def context_has_evidence(body):
    """PURE. (ok, missing_description). ok when the rendered context contains at least one
    evidence block; otherwise a model review would only be asked to explore for evidence the
    context never held."""
    b = str(body or "")
    if any(("\n" + h) in ("\n" + b) for h in EVIDENCE_HEADINGS):
        return True, ""
    return False, "plan state / worktree / driver log / spec literals"


NO_EVIDENCE_MARK = "NO EVIDENCE AVAILABLE"


def no_evidence_section(e):
    return ("\n## %s\n\nThe watcher could not resolve any of: plan state, worktree, driver log, "
            "round failure signatures, machine-checked spec literals for this escalation "
            "(job `%s`, label `%s`). No model review was spawned -- there is nothing for it to "
            "read; a review would only explore.\n" % (NO_EVIDENCE_MARK, e.get("job_id"), e.get("label")))


def no_evidence_review(e, fc=None, ev=None):
    """The deterministic (no LLM) review for an evidence-less context: a first-line marker
    (escalation_verdict.is_no_verdict_review -> self-heal refuses to act on it), then the
    machine-resolved verdict via escalation_verdict.resolve (failure_class > reason keywords >
    default), always low confidence."""
    if ev is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import escalation_verdict as ev
    vd = ev.resolve("", fc, e.get("reason"), key_parts=(esc_key(e), NO_EVIDENCE_MARK))
    return ("%s -- the escalation context carried no evidence (no plan state, worktree, driver "
            "log or literals could be resolved from the label). No model review was run.\n\n"
            "VERDICT: %s -- (machine-resolved from %s: %s; confidence low; NO ACTION TAKEN, "
            "the escalation stays open for a human)\n"
            % (NO_EVIDENCE_MARK, vd["verdict"], vd["source"], vd["why"]))


GIT_TIMEOUT_RC, GIT_NOT_RUN_RC = 124, 127   # never a git exit code (git uses 0, 1 and 128)


def _git(repo, *args, timeout=30):
    """(rc, stdout). Never raises -- a panel must render even on a broken repo. A git that did not
    answer in `timeout` seconds is GIT_TIMEOUT_RC and one that could not start (or no repo) is
    GIT_NOT_RUN_RC, never 1: `rev-parse --verify --quiet` and `merge-base --is-ancestor` exit 1 for
    "no", so a stalled git must not read as a deleted branch or a divergence (2026-11-02: while the
    screen is locked, launchd's git can sit in the kernel on ~/Desktop access; dispatch-ack-reconcile
    read the same stall as a landed branch)."""
    if not repo:
        return GIT_NOT_RUN_RC, ""
    try:
        p = subprocess.Popen(["git", "-C", str(repo), *args], stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    except OSError:
        return GIT_NOT_RUN_RC, ""
    try:
        out, _err = p.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        # not subprocess.run: after its kill it waits for the child without a limit, and a git
        # inside the kernel's access check only dies when that call returns
        p.kill()
        try:
            p.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return GIT_TIMEOUT_RC, ""
    return p.returncode, (out or "").strip()


def _git_why(rc):
    return "did not answer" if rc == GIT_TIMEOUT_RC else "exit %s" % rc


def integration_freshness(integ, repo):
    """LIVE re-check of an integration record against the CURRENT default branch, run
    at PANEL-BUILD time. Returns (lines, stale_reason) -- stale_reason is None when the
    record still describes the repo as it is now.

    THE BUG THIS EXISTS FOR (2026-09-24). This panel printed
    `- base commit compared against: <integ["base_rev"]>` -- a key NOTHING has ever
    written. Every escalation ever emitted therefore said `none`, and 14/14 Opus
    integration reviews read that (correctly, as stated) as "this gate has no
    merge-base" and concluded the gate was broken rather than the code. The merge-base
    was in fact always computed; it was simply never captured (now fixed in
    ollama-dispatch-slice's integrate_chain, which records base_rev/onto_tip).

    The deeper defect was tense. This panel reprinted a STORED verdict in the present
    tense with nothing re-tested: eleven `conflict-semantic` records and one
    `staged`/clean record, all frozen at 2026-09-23T22:41:15Z, were re-announced as
    live truth for ~26h -- including "the whole chain replays cleanly onto the default
    branch ... land it with --land-integration" for rt-costco-always-sites, a stage
    main had diverged from since 00:23:51Z and whose landing would have deleted four
    live files. A panel that repeats a cached verdict without re-testing it against the
    tree that exists NOW is not evidence, it is a transcript. So: re-check here, and
    say plainly when the stored verdict can no longer be trusted."""
    L, stale = [], None
    dref = integ.get("onto") or "main"
    base = integ.get("base_rev") or integ.get("classified_base")
    L.append("- base commit compared against: `%s`"
             % (base or "(NOT RECORDED -- this record predates base capture; "
                        "re-run `--integrate` to get a verdict with a recorded base)"))
    rc, cur = _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % dref)
    cur = cur if rc == 0 and cur else None
    was = integ.get("onto_tip")
    L.append("- %s tip WHEN CLASSIFIED: `%s`" % (dref, was or "(not recorded)"))
    L.append("- %s tip RIGHT NOW: `%s`" % (dref, cur or ("(git did not answer: not re-checked)"
                                                         if rc == GIT_TIMEOUT_RC else "(could not resolve)")))
    if cur and was and cur != was:
        rc2, n = _git(repo, "rev-list", "--count", "%s..%s" % (was, cur))
        stale = ("%s has advanced %s commit(s) since this verdict was computed"
                 % (dref, n if rc2 == 0 and n else "?"))
    elif cur and not was:
        stale = ("the tip this was classified against was never recorded, so the "
                 "verdict cannot be proven current")
    br = integ.get("integrate_branch")
    if br and cur:
        rc3, new = _git(repo, "rev-parse", "--verify", "--quiet", "refs/heads/%s" % br)
        if rc3 == 1:     # git's own "no such ref"
            L.append("- staged branch `%s`: GONE (deleted, or landed and cleaned up)" % br)
        elif rc3 != 0 or not new:
            L.append("- staged branch `%s`: NOT CHECKED (git %s)" % (br, _git_why(rc3)))
        else:
            rca = _git(repo, "merge-base", "--is-ancestor", cur, new)[0]
            rcb = _git(repo, "merge-base", "--is-ancestor", new, cur)[0]
            anc, back = rca == 0, rcb == 0
            L.append("- staged branch `%s` @ `%s`" % (br, new))
            if rca not in (0, 1) or rcb not in (0, 1):
                L.append("- is CURRENT %s an ancestor of the stage? NOT CHECKED (git %s)"
                         % (dref, _git_why(rca if rca not in (0, 1) else rcb)))
                return L, stale
            L.append("- is CURRENT %s an ancestor of the stage (i.e. still a "
                     "fast-forward)? **%s**" % (dref, "yes" if anc else "NO"))
            if not anc:
                rc4, miss = _git(repo, "rev-list", "--count", "%s..%s" % (new, cur))
                rc5, dels = _git(repo, "diff", "--name-only", "--diff-filter=D",
                                 "%s..%s" % (cur, new))
                names = [x for x in dels.splitlines() if x.strip()] if rc5 == 0 else []
                stale = ("%s and the staged branch have DIVERGED (neither is an "
                         "ancestor of the other); %s commit(s) on %s are missing from "
                         "the stage%s"
                         % (dref, miss if rc4 == 0 else "?", dref,
                            (" and landing it would DELETE: %s" % ", ".join(names[:8]))
                            if names else ""))
                if names:
                    L.append("- files the stage would DELETE from %s: %s"
                             % (dref, ", ".join("`%s`" % x for x in names[:8])))
                if not back:
                    L.append("- neither branch is an ancestor of the other: this is a "
                             "genuine divergence, not a stale label")
    return L, stale


def build_context(e, state=None, evidence=None):
    """The never-pruned record. Written BEFORE the review is spawned, so an
    escalation is durable even if the review agent fails to start."""
    L = []
    L.append("# DISPATCH ESCALATION -- needs eyes")
    L.append("")
    # NUMBERED RERUN, AT THE TOP (the owner 2026-10-01: "the ability to somehow see what
    # caused the rerun? maybe have it put at the top of the log window"). First thing
    # under the title, before any other context, so the reviewer reads the CAUSE
    # first: escalation #N, what changed old -> new, and the triggering job.
    L.extend(rerun_banner(e))
    L.append("- detected: %s" % time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    L.append("- source: %s" % e.get("source"))
    L.append("- reason: %s" % e.get("reason"))
    if e.get("plan"):
        L.append("- plan: `%s`" % e["plan"])
        L.append("- plan state: `%s`" % (SLICE_RUNS / ("%s.json" % e["plan"])))
    if e.get("slice_id"):
        L.append("- slice: `%s`" % e["slice_id"])
    if e.get("job_id"):
        L.append("- job: `%s` (%s)" % (e["job_id"], e.get("label", "")))
    L.append("")
    if isinstance(state, dict):
        L.append("## plan")
        # WHICH FILE TO READ (2026-10-03): this block used to list `repo` and `target`
        # side by side, and the replay-endorse s4b review read <repo>/<target> -- the
        # untouched default branch -- instead of the chain tip, then "refuted" the
        # escalation against the wrong code. Name the code under review explicitly
        # and mark `repo` as the base it is NOT.
        _cwt = state.get("chain_worktree")
        _tgt = state.get("target")
        _swt = None
        if e.get("slice_id") and isinstance(state.get("slices"), dict):
            _swt = (state["slices"].get(e["slice_id"]) or {}).get("worktree")
        if _tgt and (_swt or _cwt):
            L.append("- **code under review**: `%s` -- read THIS file, not the one "
                     "under `repo`" % os.path.join(_swt or _cwt, _tgt))
            if _swt:
                L.append("- slice worktree: `%s`" % _swt)
        for k in ("label", "repo", "target", "lang", "chain_branch", "chain_worktree"):
            if state.get(k):
                _note = (" (base repo, default branch -- NOT the code under review; "
                         "slices land on chain_branch)" if k == "repo" and (_swt or _cwt)
                         else "")
                L.append("- %s: `%s`%s" % (k, state[k], _note))
        L.append("")
    integ = e.get("integration") or {}
    _repo = (state or {}).get("repo") if isinstance(state, dict) else None
    _fresh_lines, _stale = ([], None)
    if integ and isinstance(integ, dict) and integ.get("onto"):
        try:
            _fresh_lines, _stale = integration_freshness(integ, _repo)
        except Exception as _exc:                       # never kill run_once (see below)
            _fresh_lines, _stale = (["- freshness re-check FAILED: %r" % (_exc,)], None)
    if integ and e.get("kind") == "integration-staged" and not _stale:
        L.append("## chain staged for landing (nothing written to the default branch)")
        L.append("")
        L.append("Every slice converged and the whole chain replays cleanly onto the "
                 "default branch -- RE-VERIFIED against its current tip just now, not "
                 "taken from the stored verdict. The result sits on a scratch branch; "
                 "landing is an explicit operator step (fast-forward + push), never "
                 "automatic.")
        L.append("")
        L += _fresh_lines
        for k in ("integrate_branch", "staged_commit", "onto", "chain_tip", "verdict"):
            if integ.get(k):
                L.append("- %s: `%s`" % (k, integ[k]))
        files = integ.get("files")
        if files:
            # `files` is a {path: status} DICT -- ollama-dispatch-slice writes the
            # integration record that way (rec["files"] = per). A dict has no [:20];
            # the KeyError that raised here (2026-09-23T22:42Z, aw-sched-runner's
            # staged record) killed run_once() mid-loop on EVERY pass for ~22h, so
            # every escalation ordered after it in the pass -- bo-O-qwen36-35b-a3b-
            # studio s1's vacuous-gate park among them -- was never announced. A
            # list (older/hand-written records, the old self-test fixture) renders
            # too.
            names = ([("%s (%s)" % (p, s)) if s else str(p) for p, s in files.items()]
                     if isinstance(files, dict) else [str(f) for f in files])
            L.append("- files: %s" % ", ".join("`%s`" % f for f in names[:20]))
        L.append("")
        L.append("Review, then land with:")
        L.append("")
        L.append("    git -C <repo> diff %s..%s" % (integ.get("onto") or "main",
                                                   integ.get("integrate_branch") or "integrate/<slug>"))
        L.append("    (old slicer retired -- use mini-swe)")
        L.append("")
    elif integ and e.get("kind") == "integration-staged":
        # STALE STAGE. Same source-E escalation, but the live re-check says the stored
        # "clean / ready to land" is no longer true. This branch exists so that state is
        # never rendered with the landing instructions (2026-09-24, rt-costco-always-
        # sites: the panel said "replays cleanly ... land it" for a stage that had
        # diverged from main and would have deleted four live files).
        L.append("## chain was STAGED, but the default branch has MOVED -- NOT landable")
        L.append("")
        L.append("**Do NOT land this.** Every slice converged and this chain WAS staged "
                 "cleanly, but that verdict was computed against an older tip of the "
                 "default branch and is no longer true of the repo as it is now:")
        L.append("")
        L.append("> %s" % _stale)
        L.append("")
        L += _fresh_lines
        L.append("")
        L.append("- stored status: `%s` (recorded %s)" % (integ.get("status"),
                                                          integ.get("at") or "?"))
        L.append("- stored verdict: `%s` -- CACHED, not current"
                 % (integ.get("verdict") or "?"))
        L.append("")
        L.append("This is NOT a code defect and NOT a conflict to resolve by hand: the "
                 "stage simply needs rebuilding on the current tip. Re-stage (this also "
                 "retires the old scratch branch, whose name is keyed on the chain head "
                 "and would otherwise block the re-stage):")
        L.append("")
        L.append("    (old slicer retired -- use mini-swe)")
        L.append("")
        L.append("Inspect the divergence first if you want it in front of you:")
        L.append("")
        L.append("    git -C <repo> diff %s..%s" % (integ.get("onto") or "main",
                                                   integ.get("integrate_branch") or "integrate/<slug>"))
        L.append("")
    elif integ:
        L.append("## whole-chain integration gate")
        L.append("")
        L.append("Every slice of this plan passed its OWN adversarial verify. What "
                 "failed is the chain as a WHOLE -- cross-module wiring, or the "
                 "target repo's own suite. Per-slice fixtures cannot see this.")
        L.append("")
        L.append("- status: `%s`" % integ.get("status"))
        L.append("- recorded: %s" % (integ.get("at") or "(unknown)"))
        L.append("- failed stages: %s" % (", ".join(integ.get("failed_stages") or [])
                                          or "(none recorded)"))
        L += _fresh_lines or ["- base commit compared against: `%s`"
                             % (integ.get("base_rev") or "none")]
        if _stale:
            # The cached-conflict shape: eleven of these were re-announced for ~26h as
            # live semantic conflicts against a main that no longer existed, several for
            # chains whose content had by then LANDED and been hardened further.
            L.append("")
            L.append("**READ THIS BEFORE DIAGNOSING THE CODE.** The verdict below is "
                     "CACHED, and the repo has changed under it: %s. A conflict against "
                     "a tip that no longer exists is not evidence of a conflict now -- "
                     "the chain's content may well have LANDED on the default branch "
                     "since (possibly hardened further there), which would make the "
                     "honest verdict `redundant`. Re-compute BEFORE reasoning about the "
                     "code:" % _stale)
            L.append("")
            L.append("    (old slicer retired -- use mini-swe)")
            L.append("")
            L.append("If the re-run still reports a conflict, THEN diagnose it below.")
        L.append("- summary: %s" % (integ.get("summary") or ""))
        rp = integ.get("report_path")
        if rp:
            L.append("- full report: `%s`" % rp)
            rep = _load_json(rp)
            for st_ in ((rep or {}).get("stages") or []):
                if st_.get("status") in ("fail", "pre_existing"):
                    L.append("")
                    L.append("### stage `%s` -- %s" % (st_.get("name"), st_.get("status")))
                    L.append(st_.get("detail") or "")
                    if st_.get("evidence"):
                        L.append("```")
                        L.append(str(st_["evidence"])[:4000])
                        L.append("```")
        L.append("")
        L.append("Re-check after a fix with (re-stages the converged chain; the "
                 "previous `--integration-check` lever never existed in the slicer):")
        L.append("")
        L.append("    (old slicer retired -- use mini-swe)")
        L.append("")
    s = e.get("slice") or {}
    # BARE JOB ROW (source C/D, no plan): resolve the driver's evidence from the label --
    # chain record, worktree, driver log, last rounds' failure tails. Without it the file is
    # a stub (2026-10-09, 444c051f2538) and the review has nothing to read.
    _jev = {}
    if e.get("job_id") and not e.get("plan") and not isinstance(state, dict):
        _jev = evidence if evidence is not None else resolve_job_evidence(e)
        L += driver_evidence_lines(_jev)
    _lwt = escalation_worktree(e, state, evidence=_jev)
    if _lwt:
        L += literal_facts_lines(spec_literal_facts(_lwt))
    if s:
        L.append("## slice record")
        L.append("```json")
        L.append(json.dumps(s, indent=2)[:6000])
        L.append("```")
        L.append("")
    job_ids = [j for j in (s.get("job_id"), s.get("author_job_id"), e.get("job_id")) if j]
    for jid in dict.fromkeys(job_ids):
        tail = _log_tail(jid)
        if tail:
            L.append("## log tail -- %s" % jid)
            L.append("```")
            L.append(tail)
            L.append("```")
            L.append("")
    return "\n".join(L) + "\n"


# --------------------------------------------------------------------------
# MACHINE-CHECKED SPEC LITERALS (2026-10-06, rt-egift-link-s1 s0-db-schema, review job
# 7a86d3e31041). The local reviewer ran `grep -n OrderEgiftLink` and got back the
# CORRECT bytes, then wrote `OrderEgmtLink` in its own prose ~20 times and called that
# "the correct spelling" -- a transcription drift in the model's output, not in the
# files. Its verdict leaned on spelling claims it could not reproduce, and the
# coordinator could not tell whether the misspelling was in the code or in the review.
# Fix, both sides: (1) the context file carries the exact expected identifiers with
# their measured counts (and any near-miss spellings actually present in the files),
# computed here, so the reviewer has ground truth to copy instead of recall; (2) the
# finished review is audited for near-miss spellings of those identifiers that occur
# in NO checked file -- such a review is flagged in the index and its diff is held.
_IDENT_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _must_contain(task_text):
    """(file_or_None, literal) pairs from TASK.md's `## Must contain` block -- the
    same parse check_literals.py uses (a line's `in <path>:` pins its literals)."""
    m = re.search(r"##+\s*Must contain[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text or "",
                  re.S | re.I)
    out = []
    for line in (m.group(1).splitlines() if m else []):
        if not re.match(r"\s*-\s", line):
            continue
        fm = re.search(r"(?:^|\s)in\s+([^\s:`]+)\s*:", line)
        for lm in re.finditer(r"`([^`\n]{1,200})`", line):
            if "TODO" not in lm.group(1):
                out.append((fm.group(1) if fm else None, lm.group(1)))
    return out


def _near(h, w):
    """PURE. `h` is a near-miss spelling of `w`: same first 3 letters, length within
    2, SequenceMatcher ratio >= 0.8, case-insensitively DIFFERENT, and neither a
    substring of the other (so `orderEgiftLink` / `upsertOrderEgiftLink` are not
    near-misses of `OrderEgiftLink`). Same rule as gate-on-complete's
    _near_miss_feedback, so the gate and the review audit agree."""
    import difflib
    hl, wl = h.lower(), w.lower()
    return (hl != wl and abs(len(h) - len(w)) <= 2 and hl[:3] == wl[:3]
            and wl not in hl and hl not in wl
            and difflib.SequenceMatcher(None, hl, wl).ratio() >= 0.8)


def spec_literal_facts(worktree):
    """Measure every Must-contain literal in a slice worktree. Returns None when there
    is no TASK.md, else {"literals": [(file, literal, exact_count)], "wants": [ident],
    "file_idents": set, "near": [(have, want, file)]}. Read-only; never raises."""
    try:
        wt = Path(str(worktree))
        task = wt / "TASK.md"
        if not task.is_file():
            return None
        lits = _must_contain(task.read_text(errors="replace"))
        if not lits:
            return None
        target = None
        cl = wt / "check_literals.py"
        if cl.is_file():
            tm = re.search(r"^TARGET\s*=\s*pathlib\.Path\(['\"]([^'\"]+)['\"]\)",
                           cl.read_text(errors="replace"), re.M)
            target = tm.group(1) if tm else None
        files = {}
        for f, _l in lits:
            rel = f or target
            if rel and rel not in files:
                p = wt / rel
                files[rel] = p.read_text(errors="replace") if p.is_file() else ""
        out = {"literals": [], "wants": [], "file_idents": set(), "near": []}
        for f, l in lits:
            rel = f or target
            out["literals"].append((rel, l, files.get(rel, "").count(l) if rel else 0))
            out["wants"] += [t for t in _IDENT_RE.findall(l) if len(t) >= 6]
        out["wants"] = list(dict.fromkeys(out["wants"]))
        for rel, body in files.items():
            ids = set(_IDENT_RE.findall(body))
            out["file_idents"] |= ids
            for h in sorted(ids):
                for w in out["wants"]:
                    if _near(h, w):
                        out["near"].append((h, w, rel))
        return out
    except Exception:
        return None


def escalation_worktree(e, state=None, logs=None, evidence=None):
    """The worktree holding the code under review: the slice record's worktree, the
    plan state's for that slice, else (source D/C job rows carry none) the job's own
    cwd from its persisted queue record -- only when that cwd has a TASK.md."""
    s = e.get("slice") if isinstance(e.get("slice"), dict) else {}
    wt = s.get("worktree")
    if not wt and isinstance(state, dict) and e.get("slice_id"):
        wt = ((state.get("slices") or {}).get(e["slice_id"]) or {}).get("worktree")
    if not wt and e.get("job_id"):
        logs = Path(logs) if logs else QUEUE_LOGS
        for name in ("%s.done.json" % e["job_id"], "%s.json" % e["job_id"]):
            rec = _load_json(logs / name)
            if isinstance(rec, dict) and rec.get("cwd"):
                wt = rec["cwd"]
                break
    if wt and (Path(str(wt)) / "TASK.md").is_file():
        return str(wt)
    # a bare job row whose queue record is gone/placeholder: the label-resolved worktree
    # (chain record parked.worktree / queue row cwd / wt-<base>) -- see resolve_job_evidence.
    if e.get("job_id") and not (state and e.get("slice_id")):
        ev = evidence if evidence is not None else resolve_job_evidence(e)
        if ev.get("worktree"):
            return ev["worktree"]
    return None


def _spelled(tok):
    return "-".join(tok)


def literal_facts_lines(facts):
    """The context-file section rendering spec_literal_facts()."""
    if not facts:
        return []
    L = ["## machine-checked spec literals (GROUND TRUTH -- copy spellings from here)",
         "",
         "Measured by the watcher with an exact byte search of the worktree files "
         "just now. When you state an identifier's spelling, COPY it from this "
         "block or from raw tool output; never retype it from memory. If your "
         "reading disagrees with this block, this block is right.",
         ""]
    for rel, l, n in facts["literals"]:
        L.append("- `%s` in `%s`: **%d** exact occurrence(s)" % (l, rel or "(target)", n))
    for w in facts["wants"]:
        L.append("- identifier `%s` is spelled %s (%d chars)" % (w, _spelled(w), len(w)))
    if facts["near"]:
        L.append("")
        L.append("NEAR-MISS spellings PRESENT in the files (these are real defects):")
        for h, w, rel in facts["near"]:
            L.append("- `%s` (%s) in `%s` -- spec spelling is `%s`" % (h, _spelled(h), rel, w))
    else:
        L.append("- near-miss spellings of these identifiers present in the files: NONE")
    L.append("")
    return L


def review_literal_drift(review, facts):
    """PURE. [(token, want)] for identifier tokens in the REVIEW text that are
    near-misses of a spec identifier yet occur in NO checked file -- the reviewer's
    own transcription drift. A near-miss that IS in a file is a real finding, not
    drift, and is not reported here."""
    if not facts or not review:
        return []
    out = []
    for t in dict.fromkeys(_IDENT_RE.findall(review)):
        if len(t) < 6 or t in facts["file_idents"]:
            continue
        for w in facts["wants"]:
            if _near(t, w):
                out.append((t, w))
                break
    return out


def drift_note(drift, facts):
    """Markdown appended to a drifted review."""
    L = ["", "---", "## WATCHER LITERAL AUDIT -- review text is UNRELIABLE on identifiers", "",
         "This review spells spec identifiers in ways that occur in NO worktree file, "
         "so its spelling claims are the reviewer's own transcription drift, not the "
         "code's. Trust the machine check below, not the prose above.", ""]
    for t, w in drift:
        L.append("- review wrote `%s` (%s); spec identifier is `%s` (%s); `%s` occurs in "
                 "no checked file" % (t, _spelled(t), w, _spelled(w), t))
    for rel, l, n in facts["literals"]:
        L.append("- machine check: `%s` in `%s` = %d exact" % (l, rel or "(target)", n))
    return "\n".join(L) + "\n"


REVIEW_PROMPT = """A dispatch slice/job has been parked as ESCALATED by the
scheduled escalation watcher because it is not making progress. Nobody is at the
keyboard -- you were started headlessly by launchd.

Read the escalation context file at {ctx}

Your job is DIAGNOSIS ONLY. In order:
1. Read the context file, the referenced plan state JSON, and the referenced
   worktree/log evidence. Read the actual data; do not reason from priors.
2. Decide which of these it is, and say WHICH and WHY with evidence:
   (a) the slice is already satisfied at the chain tip, so no honest both-ways
       proof exists and it should be SKIPPED;
   (b) the task spec (intent / must_contain / verify_shape) is wrong or
       under-specified and needs re-authoring;
   (c) a harness/dispatch defect (suspect the harness before model incapacity,
       but only conclude harness-defect if the evidence supports it);
   (d) genuine model incapacity on this slice.
3. Recommend the single concrete next action, with the exact command if there
   is one. NOT (a): a slice whose work is correct and VERIFY_OK in its OWN
   worktree but is not yet on the chain tip -- skipping it would discard the
   deliverable; recommend closing the slice by hand (old slicer retired -- use mini-swe).

IDENTIFIER SPELLING. If the context file has a "machine-checked spec literals"
block, it is GROUND TRUTH for which identifiers are present and how they are
spelled. Any spelling claim you make must be COPIED from that block or from raw
tool output, never retyped from memory; if your reading disagrees with the
block, the block is right. Your review is audited against it afterwards.

HARD CONSTRAINTS. Do NOT edit any product code. Do NOT enqueue, re-enqueue,
retry, cancel or skip anything. Do NOT modify any slice-plan or queue state
file. You are writing a recommendation for a human/coordinator to act on; an
unsupervised 3am agent that "fixes" a stuck loop is how loops get worse.

Answer in under 400 words, plain markdown, starting with a one-line verdict of
the form: VERDICT: <a|b|c|d> -- <one sentence>.

GROUNDING (mandatory; a script verifies it -- escalation_grounding.py, policy v1).
Every causal claim behind your verdict must cite `path:LINE` AND quote the exact text
from that line in backticks, on the same line, e.g.
  - refimpl.py:42 `session = target.session` -- reads an attribute the fixture never sets
The script opens the cited file (absolute path, or relative to the worktree named in the
context file) and drops any claim whose quote is not within 2 lines of LINE. A verdict
with NO surviving claim is marked UNVERIFIED and the pipeline will NOT act on it. Never
quote from memory: copy from a tool result you just saw.
Also RUN the cheapest reproducing command ONCE (e.g. `python3 auto-harness-check.py`, or
`bash verify.sh`, or the refimpl against the fixture) and record, verbatim, before the
verdict is finalised:
REPRO CMD: <the command>
REPRO OUTPUT:
```
<its output, including the FIRST traceback / exception if there is one>
```
If that output shows an exception, your claims must name it: a claim such as "every case
returns (0, [])" is checked against the real first exception (a swallowed NameError behind
a bare `except` is the usual cause), not accepted on prose.

MACHINE-CHECKABLE OUTPUT (mandatory, the pipeline ACTS on it with nobody reading
this file). Your FIRST line is the VERDICT line -- do not narrate your plan or
"let me analyze" first; read the evidence, then write the answer. Your LAST line
is exactly one line of JSON (no code fence):
ESC_RESULT: {{"verdict": "<a|b|c|d>", "confidence": "<high|medium|low>", "why": "<one sentence, <=200 chars>"}}
The action taken is driven by that verdict: a = close/resume, b = re-spec the
task, c = harness repair round, d = bigger model / re-slice.

OPTIONAL, AND ONLY IF YOU ARE CONFIDENT: after the prose, you may PROPOSE a fix
as a unified diff in a single fenced ```diff block. For a slice, write paths
relative to the slice WORKTREE named in the context file. A diff that touches ONLY
the slice's harness proof (its fixture / test file, or refimpl.py) is applied
AUTOMATICALLY by the self-heal step and then re-gated: preflight + the mutation-
relevance gate decide GO, so a wrong fix simply re-escalates. Any diff that touches
the target code, TASK.md, verify.sh or check_literals.py is NEVER applied -- it
waits for a human. Either way it is never committed by you, so do not write it as
though it were already landed.
  * Use `git apply`-compatible format: a/ and b/ prefixes, real @@ hunk headers,
    correct context lines taken from the file you actually read.
  * Diff exactly ONE logical fix. A diff that also reformats, renames or
    "tidies" surrounding code is worse than no diff.
  * Emit NO diff block at all if you are guessing, if the right fix is a spec
    re-authoring rather than a code edit, or if you could not read the file you
    would be patching. A missing proposal costs nothing; a plausible-looking
    wrong one costs a human's trust in every future one.
"""


REVIEW_BLOCKED_PREFIX = "REVIEW BLOCKED"

# The CLI's own refusal phrasings. Deliberately NARROW: a real review that
# merely discusses "limits" must never match. Observed live 2026-09-22 on job
# 3ca17c902224: stdout was exactly
#   "You've hit your session limit - resets 7:30pm (America/Los_Angeles)"
# which sailed through the old "empty stdout == failure" check and was written
# out as a completed review with "proposed fix: none written".
_REFUSAL_SIGS = [
    (re.compile(r"you'?(?:ve|ve| have) hit your (?:session|usage|weekly) limit", re.I),
     "session limit"),
    (re.compile(r"(?:claude )?(?:ai )?(?:usage|session|rate) limit reached", re.I),
     "usage limit reached"),
    (re.compile(r"\b(?:session|usage|weekly) limit\b[^\n]{0,80}\bresets?\b", re.I),
     "session limit"),
    (re.compile(r"credit balance is too low", re.I), "credit balance"),
    (re.compile(r"\b(?:invalid api key|authentication_error|oauth token has expired)\b", re.I),
     "auth failure"),
    (re.compile(r"(?:please run|run) /login\b", re.I), "not logged in"),
    (re.compile(r"api error: 5\d\d\b", re.I), "upstream API error"),
]
_RESETS_RE = re.compile(r"resets?\s+([^\n()]{1,40}(?:\([^)\n]{1,40}\))?)", re.I)


def review_blocked_reason(stdout, rc=0, stderr=""):
    """Short reason string if this `claude -p` result is a REFUSAL / infra failure
    rather than a real review; None if it is a real review.

    THE BUG THIS EXISTS FOR: headless `claude -p` answers a session/usage-limit
    refusal on STDOUT, non-empty and exit-0-looking. Trusting non-empty stdout
    therefore records "the reviewer looked and proposed nothing" for a review
    that never ran, and the job rots in needs_opus because nothing retries it.

    GUARD AGAINST MISFIRE: a genuine review is required by REVIEW_PROMPT to
    carry a `VERDICT:` line, so the presence of one is treated as proof the
    prompt really ran and NOTHING is ever classified as blocked after that --
    a legitimate review that happens to discuss rate limits stays a review."""
    txt = (stdout or "").strip()
    err = (stderr or "").strip()
    if not txt:
        return "no output from the review CLI (rc=%s)%s" % (
            rc, (": " + err[-300:]) if err else "")
    if "VERDICT:" in txt.upper():
        return None
    head = txt[:600] + "\n" + err[:600]
    for rx, label in _REFUSAL_SIGS:
        if rx.search(head):
            m = _RESETS_RE.search(head)
            return "%s%s -- the review never ran" % (
                label, (", resets %s" % m.group(1).strip()) if m else "")
    if rc != 0:
        return ("review CLI exited rc=%s and produced no VERDICT -- the review "
                "never ran%s" % (rc, (": " + err[-300:]) if err else ""))
    return None


def is_review_blocked(review):
    """True for a review text spawn_review marked as an infra failure (retryable),
    as opposed to a real review that happened to propose nothing."""
    return str(review or "").lstrip().startswith(REVIEW_BLOCKED_PREFIX)


# A SESSION LIMIT IS A CLOCK, NOT A COUNT (2026-09-25).
#
# THE BUG. A blocked review is retried on the very next watcher pass, bounded only
# by REVIEW_BLOCKED_MAX_ATTEMPTS. A session-limit refusal does not clear with
# retries -- it clears at a time the refusal itself states. So `arr-codec-floor`
# s4-pack-vs-pack burned all 8 attempts inside ~40 minutes (02:12 -> 02:49 on
# 2026-09-25) against a limit that reset hours later, wrote 8 escalation files, and
# then GAVE UP permanently: the slice got no review at all, even though a single
# attempt after the reset would have worked. 24 escalation files across 11 plans and
# 4 days are this class (2026-09-19/21/22/25); two rows had to be re-diagnosed by
# hand after a limit refusal was filed as a real review.
#
# THE FIX. The refusal already tells us when it clears -- review_blocked_reason()
# parses it ("session limit, resets 7:30pm (America/Los_Angeles)"). Wait for that
# time instead of spending the budget on the clock, and do NOT count a wait as an
# attempt: the attempt budget then measures real failures (a broken CLI, a bad
# token), which is what it was for. A refusal with no parseable reset time keeps the
# old count-bounded behaviour, so nothing becomes unbounded.
_CLOCK_RE = re.compile(r"\b(\d{1,2})(?::(\d{2}))?\s*([ap])\.?m\.?", re.I)
_TZ_RE = re.compile(r"\(([A-Za-z]+(?:/[A-Za-z_+\-]+)+)\)")
# Never wait longer than this on one parse, however the clock reads. A bad parse
# must degrade to "retry soon", never to "never look at this again".
REVIEW_BLOCKED_MAX_WAIT_S = 26 * 3600


def review_blocked_until(text, now=None):
    """Epoch seconds to wait until before re-spawning a review, or None when the
    refusal names no reset time. PURE apart from reading the clock.

    `text` is the blocked review (or just its reason line). The reset is a wall
    clock in a named zone, so it is resolved in THAT zone -- the watcher runs under
    launchd, whose TZ is not reliably the account's."""
    m = _CLOCK_RE.search(str(text or ""))
    if not m:
        return None
    hour = int(m.group(1)) % 12
    if m.group(3).lower() == "p":
        hour += 12
    minute = int(m.group(2) or 0)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    now = time.time() if now is None else now
    tz = None
    tzm = _TZ_RE.search(str(text or ""))
    if tzm:
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(tzm.group(1))
        except Exception:
            tz = None
    try:
        from datetime import datetime, timedelta
        base = datetime.fromtimestamp(now, tz) if tz else datetime.fromtimestamp(now)
        target = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
        if target.timestamp() <= now:
            # The stated clock time has already passed today, so it means tomorrow.
            target = (base + timedelta(days=1)).replace(
                hour=hour, minute=minute, second=0, microsecond=0)
        until = target.timestamp()
    except Exception:
        return None
    if until <= now:
        return None
    return min(until, now + REVIEW_BLOCKED_MAX_WAIT_S)


def review_blocked_wait(ledger, key, now=None):
    """Seconds still to wait before this key's review may be re-spawned, or 0.
    Never raises, and never returns a wait longer than REVIEW_BLOCKED_MAX_WAIT_S --
    a corrupt ledger value cannot silence an escalation forever."""
    now = time.time() if now is None else now
    try:
        until = float(((ledger or {}).get("records") or {})
                      .get(key, {}).get("review_blocked_until") or 0)
    except (TypeError, ValueError):
        return 0
    return int(min(until - now, REVIEW_BLOCKED_MAX_WAIT_S)) if until > now else 0


INTEGRATION_REVIEW_PROMPT = """A whole SLICE PLAN finished -- every slice passed
its own adversarial verify -- and then the WHOLE-CHAIN integration gate failed.
Nobody is at the keyboard; you were started headlessly by launchd.

Read the escalation context file at {ctx}

This is NOT a stuck-slice diagnosis. Each slice was gated in isolation against
its own fixture; what broke is the chain as a WHOLE -- cross-module wiring, or
the target repo's own suite. Your job is DIAGNOSIS ONLY. In order:
1. Read the context file and the referenced integration report JSON, then read
   the actual source in the chain worktree it names. Read the code; do not
   reason from priors.
2. Decide which of these it is, and say WHICH and WHY with file:line evidence:
   (a) a REAL cross-module defect -- two slices agreed on different shapes for
       the same seam (a signature, a return shape, a module name). Name both
       sides and which one is wrong.
   (b) a pre-existing repo failure that the baseline differential should have
       discounted but did not -- i.e. a defect in the GATE, not the code.
   (c) an environment problem in the chain worktree (a missing dependency, a
       missing node_modules, a venv the checker could not find).
   (d) a plan-level gap -- the seam was never sliced by anyone, so no slice was
       ever responsible for it.
3. Recommend the single concrete next action, with the exact command if there
   is one (a `--retry-slice`, a new slice plan for the seam, or a hand fix in
   the chain worktree followed by `--integration-check`).

HARD CONSTRAINTS. Do NOT edit any product code. Do NOT enqueue, re-enqueue,
retry, cancel or skip anything. Do NOT modify any slice-plan, queue or
integration state file, and NEVER flip an integration verdict by hand -- a gate
edited to pass is exactly the dishonest bypass this pipeline forbids.

Answer in under 400 words, plain markdown, starting with a one-line verdict of
the form: VERDICT: <a|b|c|d> -- <one sentence>.

OPTIONAL, AND ONLY IF YOU ARE CONFIDENT: after the prose you may PROPOSE a fix
as a unified diff in a single fenced ```diff block, under the same rules as any
other proposal here -- one logical fix, real context lines from a file you
actually read, and no diff at all if you are guessing.
"""


# Marker this watcher exports into the headless review's environment. SessionStart
# hooks that ask for an INTERACTIVE action self-skip on it (currently
# session-start-handoff-panel.sh, session-start-escalations.sh and
# ollama-queue-watch-reminder.sh in ~/.claude/hooks, mirrored in machine-config's
# claude-hooks/).
#
# THE BUG THIS FIXES (2026-09-24). The reviews below are `claude -p` started by
# launchd with read-only --allowedTools and nobody at the keyboard, but every
# SessionStart hook fired in them as if a human were present. The handoff-panel hook
# injected "OLLAMA HANDOFF PANEL: N completed jobs unprocessed ... process this EARLY
# ... then clear the processed with handoff-emit.py --acted", which a headless
# read-only session can neither approve nor run. At least 2 of the ~18 integration
# reviews that night answered "I could not process the handoff panel this session"
# INSTEAD of the diagnosis they were spawned to produce. The escalations hook was
# worse than useless here: it hands the review the open-escalation list that contains
# the very item under review. A nudge nobody can act on is a derail, not a reminder.
HEADLESS_MARKER = "CLAUDE_HEADLESS_DIAGNOSIS"


def review_env():
    """The spawned review's environment: ours plus the diagnosis-only marker. Built as
    a copy so nothing here mutates this process's env."""
    return dict(os.environ, **{HEADLESS_MARKER: "1"})


QUEUE = Path.home() / "bin" / "ollama-queue.py"
REVIEW_QUEUE_MODEL = os.environ.get("ESC_REVIEW_MODEL", "qwen3.6-35b-a3b-vl-mtp-mxfp8")
_STATUS_ROW_RE = re.compile(r"^\[([a-z_]+)\s*\]\s+([0-9a-f]{6,})\s+(\S+)", re.M)
_ACTIVE_STATES = ("running", "queued", "pending", "scheduled", "held")


def _queue_rows():
    """(state, id, label) for every row `ollama-queue.py status` prints."""
    r = subprocess.run([sys.executable, str(QUEUE), "status"], capture_output=True,
                       text=True, timeout=60)
    return _STATUS_ROW_RE.findall(r.stdout or "")


_CTX_TS_RE = re.compile(r"^\d{8}T\d{6}Z-")


def _sibling_review(stem, rows):
    """(job_id, sibling_stem) of an ACTIVE esc-review for the same subject as `stem`
    but a different context file (different leading timestamp), else None. Only
    untruncated labels count, so the sibling's stem -- and thus its output file --
    is recoverable exactly."""
    if not _CTX_TS_RE.match(stem):
        return None
    subject = _CTX_TS_RE.sub("", stem)
    for st, jid, lab in rows:
        if st not in _ACTIVE_STATES or not lab.startswith("esc-review-"):
            continue
        sib = lab[len("esc-review-"):]
        if sib == stem or len(sib) >= 60 or not _CTX_TS_RE.match(sib):
            continue
        if _CTX_TS_RE.sub("", sib) == subject:
            return jid, sib
    return None


def spawn_review(ctx_path, repo=None, timeout=REVIEW_TIMEOUT, model=REVIEW_QUEUE_MODEL,
                 prompt=None, bundle=None, max_tokens=ESC_REVIEW_MAX_TOKENS):
    """Diagnosis review as a QUEUE JOB on the local Darkbloom lane (2026-10-01: the
    old `claude -p --model opus` spawn exhausted the weekly Claude limit). The job is
    a read-only research dispatch; its final answer is captured to a file next to the
    context file. A deterministic label dedups: a pass that finds the job still active
    waits on it instead of enqueueing another. Returns the review text, or a
    REVIEW BLOCKED string (retryable infra failure, never a verdict); never raises."""
    ctx_path = Path(ctx_path)
    stem = ctx_path.stem
    label = "esc-review-" + re.sub(r"[^A-Za-z0-9._-]+", "-", stem)[-60:]
    out = ESC_DIR / ("%s.local-review.txt" % stem)

    def blocked(msg):
        return ("%s (%s)\n\nThe local review did NOT run -- this is an infra failure, "
                "not a verdict. The escalation stays open and the watcher will retry "
                "it on a later pass.\n" % (REVIEW_BLOCKED_PREFIX, msg))
    note = ""
    try:
        all_rows = _queue_rows()
        rows = [r for r in all_rows if r[2] == label]
        active = [r for r in rows if r[0] in _ACTIVE_STATES]
        jid = active[0][1] if active else None
        if jid is None:
            # 2026-10-06: the label carries the context file's TIMESTAMP, so a second
            # context file for the SAME subject (job 0fb392936848: one at 020328Z after
            # a BLOCKED review, one at 023840Z from source D) enqueued a second review
            # while the first was still pending -- two GPU reviews of one job. A review
            # already active for the same subject is waited on and reused instead.
            sib = _sibling_review(stem, all_rows)
            if sib:
                jid, sib_stem = sib
                out = ESC_DIR / ("%s.local-review.txt" % sib_stem)
                note = ("(reused review job %s of the earlier context file %s.md for the "
                        "same subject; no duplicate review was enqueued)\n\n" % (jid, sib_stem))
        if jid is None:
            try:
                out.unlink()
            except OSError:
                pass
            task = ESC_DIR / ("%s.local-review.task.md" % stem)
            task.write_text(
                (prompt or REVIEW_PROMPT).format(ctx=ctx_path)
                + "\n\nOUTPUT: your FINAL ANSWER is the complete review text, starting "
                  "with the VERDICT: line (and any ```diff block). Use the read-only "
                  "file tools to read the context file and evidence first. The context "
                  "file names every path you need as an absolute path: read each file AT "
                  "MOST ONCE, never list '.' or $HOME, and answer as soon as you have read "
                  "it. Your output is hard-capped; a long answer is cut off and discarded.\n")
            # cwd is ALWAYS $HOME: the worker refuses a --capture-final-as path outside its
            # cwd, and both the escalations dir and every dispatch worktree live under it.
            cwd = str(Path.home())
            r = subprocess.run(
                [sys.executable, str(QUEUE), "enqueue", "--model", model, "--host", "studio",
                 "--task-kind", "research", "--cwd", cwd, "--allow-unisolated",
                 "--allow-no-verify", "--task-file", str(task), "--label", label,
                 "--allow-duplicate-label", "--capture-final-as", str(out),
                 "--role", ESC_REVIEW_ROLE, "--max-tokens", str(int(max_tokens))]
                + (["--bundle", str(bundle)] if bundle else []),
                capture_output=True, text=True, timeout=120)
            m = re.search(r"enqueued\s+([0-9a-f]{6,})", r.stdout or "")
            if not m:
                return blocked("could not enqueue: %s" % ((r.stdout + r.stderr)[-300:]))
            jid = m.group(1)
        deadline = time.time() + timeout
        while time.time() < deadline:
            state = {i: s for s, i, _l in _queue_rows()}.get(jid)
            if state not in _ACTIVE_STATES:
                break
            time.sleep(20)
        else:
            return blocked("still queued/running after %ss; job %s keeps going and the "
                           "next pass will collect it" % (timeout, jid))
        txt = out.read_text().strip() if out.exists() else ""
        if "</think>" in txt:  # the model's inline reasoning must not be filed as the review
            txt = txt.rsplit("</think>", 1)[1].strip()
        why = review_blocked_reason(txt, 0 if txt else 1, "" if txt else "no output")
        if why:
            return blocked("%s; job %s" % (why, jid)) + (
                "\nRaw output:\n```\n%s\n```\n" % (txt or "(empty)")[:2000])
        return (txt + "\n\n" + note.strip() + "\n") if note else txt
    except Exception as exc:
        return blocked("could not run: %s" % exc)


PATCH_RE = re.compile(r"```(?:diff|patch)\s*\n(.*?)```", re.S | re.I)


def extract_patch(review):
    """The proposed diff out of the review's fenced ```diff block, or None.

    Only a block that actually looks like a unified diff is returned -- a model
    will happily fence prose, a shell transcript or a single '...' under ```diff,
    and handing that to `git apply` produces a confusing error instead of an
    honest "no proposal". If several blocks are present we take the LONGEST,
    since the extra ones are usually an illustrative fragment quoted earlier."""
    best = None
    for m in PATCH_RE.finditer(review or ""):
        body = m.group(1)
        if not body.strip():
            continue
        has_hunk = "\n@@" in ("\n" + body) or body.lstrip().startswith("@@")
        has_file = ("--- " in body and "+++ " in body) or "diff --git " in body
        if not (has_hunk and has_file):
            continue
        if best is None or len(body) > len(best):
            best = body
    if best is None:
        return None
    return best if best.endswith("\n") else best + "\n"


def validate_patch(patch, repo):
    """`git apply --check` the proposal in *repo*. Returns (ok, detail).

    A patch is only ever WRITTEN OUT if it passes this. A .patch file sitting
    beside an escalation is an implicit promise that the one-line apply command
    next to it will work; a file that fails the moment someone runs it is the
    "looks wired while telling you nothing" failure the whole gate exists to
    prevent, and it costs more trust than emitting nothing would have.

    NOTE: --check only, never an apply. Nothing in this module applies, stages
    or commits a proposal -- see the self-test that asserts exactly that."""
    if not patch:
        return False, "no diff block in the review"
    if not repo or not Path(str(repo)).is_dir():
        return False, "no repo/worktree recorded for this escalation, cannot verify the diff"
    tmp = None
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".patch", delete=False) as fh:
            fh.write(patch)
            tmp = fh.name
        # --recount: a model-written diff routinely miscounts its @@ header
        # line counts (live 2026-09-27, sidecar-bfmr s2: a correct fixture diff
        # refused as "corrupt patch at line 22"). The content lines are what
        # matter; git infers the counts from them. The emitted apply command
        # carries the same flag, so the check and the apply agree.
        r = subprocess.run(["git", "apply", "--check", "--recount", "--verbose", tmp],
                           cwd=str(repo), capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return True, "git apply --check OK in %s" % repo
        return False, ("git apply --check REJECTED it: %s"
                       % ((r.stderr or r.stdout or "").strip()[-300:]))
    except Exception as exc:
        return False, "could not check the diff (%s)" % exc
    finally:
        if tmp:
            try:
                os.unlink(tmp)
            except OSError:
                pass


def write_proposal(patch, repo, out_path):
    """Write a VALIDATED proposal + its one-line apply command. Returns the
    apply command, or None when there is nothing trustworthy to offer.

    Propose, never apply (the owner 2026-09-19). The evidence for that split is in
    this pipeline's own history: of the first three live verdicts two were
    'harness defect' and the owner wanted to review one himself, so the triage class
    is not a safe auto-trigger; and bg-brokers is the counterexample that
    settles it -- a change that was CORRECT PER SPEC silently dropped real
    behaviour (dedupe), which only a human reading the chain tip caught. An
    agent that applies its own diff at 3am cannot catch that class at all."""
    ok, detail = validate_patch(patch, repo)
    if not ok:
        return None, detail
    out = Path(out_path)
    header = (
        "# PROPOSED FIX -- NOT APPLIED, NOT COMMITTED, NOT REVIEWED BY A HUMAN.\n"
        "# Generated headlessly by dispatch-escalation-watcher. It passed\n"
        "#   git apply --check --recount\n"
        "# in %s, which proves only that it APPLIES -- not that it is correct.\n"
        "# Read the diff and the .review.md beside it before applying.\n"
        "#\n# apply:  git -C %s apply --recount %s\n#\n" % (repo, repo, out)
    )
    try:
        out.write_text(header + patch)
    except Exception as exc:
        return None, "could not write proposal (%s)" % exc
    return "git -C %s apply --recount %s" % (repo, out), detail


_INFERRED_VERDICT_RES = (
    re.compile(r"\b(?:my |the )?verdict is:?\s*(?:\(([abcd])\)|([abcd])\b)[^\n]*", re.I),
    re.compile(r"\(([abcd])\) is the (?:most likely |correct |right )?diagnosis[^\n]*", re.I),
)


def verdict_line(review, reason=None):
    """The VERDICT line, or an honest placeholder -- NEVER the review's first
    line as a fallback. The review agent sometimes opens with a throwaway line
    ("The file search I started in the background has now finished..."), and the
    first real pass put exactly that into the index where a verdict belongs. An
    index row that reads like a verdict but isn't one is worse than a blank: it
    is the "looks wired while telling you nothing" failure the gate exists to
    prevent. Matches VERDICT: anywhere in the line, since a model may bold it."""
    for l in (review or "").splitlines():
        s = l.strip().lstrip("*# ").strip()
        if "VERDICT:" in s.upper():
            i = s.upper().index("VERDICT:")
            return s[i:].replace("**", "").strip()
    # #15 (2026-10-03): 31 index rows read only "(no VERDICT line ...)" -- reviews that
    # ran out mid-reasoning ("So my verdict is (c) - a harness/dispatch defect.") never
    # wrote the line. Surface the LAST explicit classification, marked as inferred,
    # else the escalation's own reason, so the row still says what is wrong.
    last = None
    for rx in _INFERRED_VERDICT_RES:
        for m in rx.finditer(review or ""):
            if last is None or m.start() > last.start():
                last = m
    if last is not None:
        return ("verdict (inferred, no VERDICT line): %s"
                % last.group(0).replace("**", "").strip()[:240])
    base = "(no VERDICT line -- read the review file)"
    if reason:
        return "%s -- escalation reason: %s" % (base, str(reason).strip()[:240])
    return base


CLASSIFY_PROMPT = """An automated dispatch escalation was reviewed, but the review ended
WITHOUT a usable verdict. Read the file at {ctx}: it holds the escalation context and,
at the end, the tail of that earlier review. Do not re-investigate from scratch and do
not narrate. Output EXACTLY two lines and nothing else:
VERDICT: <a|b|c|d> -- <one sentence>
ESC_RESULT: {{"verdict": "<a|b|c|d>", "confidence": "<high|medium|low>", "why": "<<=200 chars>"}}
a = not a real failure / already satisfied; b = the task spec is wrong or under-specified;
c = harness/dispatch defect; d = genuine model incapacity. Pick the single best fit.
"""


def _verdict_mod():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import escalation_verdict
    return escalation_verdict


def ensure_verdict(e, ctx, stem, review, rerun=None, ev=None):
    """A review with no usable verdict is never left: re-run ONE classify-only review
    (local queue job, cheap), then fall back to the machine resolution (mechanical
    failure_class > reason keywords > default b) and WRITE that verdict line so the row,
    the self-heal ladder and a human all read the same thing. Contained: never raises."""
    try:
        if ev is None:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import escalation_verdict as ev
        if is_review_blocked(review) or ev.has_verdict(review) or ev.is_no_verdict_review(review):
            return review
        cfg = ev.load_config()
        key = esc_key(e)
        led = _load_json(LEDGER) or {}
        rec = (led.get("records") or {}).get(key) or {}
        if int(rec.get("verdict_reruns") or 0) < int(cfg["rerun_review_max"]):
            _merge_ledger(LEDGER, key, dict(rec, verdict_reruns=int(rec.get("verdict_reruns") or 0) + 1))
            ctx2 = ESC_DIR / ("%s-classify.md" % stem)
            try:
                ctx2.write_text(Path(ctx).read_text() + "\n\n## Earlier review (tail, no verdict)\n\n"
                                + (review or "")[-2500:] + "\n")
                new = (rerun or spawn_review)(ctx2, prompt=CLASSIFY_PROMPT,
                                              bundle=review_bundle(e))
            except Exception:
                new = ""
            if new and not is_review_blocked(new) and ev.has_verdict(new):
                return (review or "").rstrip() + "\n\n---\nclassify-only re-run:\n" + new.strip() + "\n"
        fc = e.get("failure_class")
        if not fc and e.get("job_id"):
            try:
                st = json.loads((Path.home() / "bin" / "ollama-queue-state.json").read_text())
                fc = next((j.get("failure_class") for j in st.get("jobs") or []
                           if j.get("id") == e["job_id"]), None)
            except Exception:
                fc = None
        vd = ev.resolve(review, fc, e.get("reason"), key_parts=(key,))
        return ((review or "").rstrip() + "\n\nVERDICT: %s -- (machine-resolved from %s: %s)\n"
                % (vd["verdict"], vd["source"], vd["why"]))
    except Exception:
        return review



def _merge_ledger(path, key, record):
    """Re-read, add one key, write. Returns the merged ledger.

    Not a lock -- a lock cannot help across a launchd fire and a hand run anyway
    -- but it makes the write additive, so the loser of a race keeps the winner's
    entries instead of erasing them. Erasing them is what caused a second context
    file and a second Opus review for the same job."""
    cur = _load_json(path) or {"announced": [], "records": {}}
    ann = cur.setdefault("announced", [])
    if key not in ann:
        ann.append(key)
    cur.setdefault("records", {})[key] = record
    try:
        Path(path).write_text(json.dumps(cur, indent=1))
    except Exception:
        pass
    return cur


def _ledger_unannounce(path, key, record=None):
    """Drop *key* from `announced` (keeping/updating its record) so the NEXT pass
    re-detects and re-reviews this escalation.

    This is the retry path for a review that never ran. It reuses the existing
    announce-once mechanism rather than inventing a retry queue: the launchd
    watcher re-detects the same parked needs_opus job every POLL_SECONDS, and
    `announced` is the only thing that was suppressing it. The record survives
    un-announcing, so it carries the blocked-attempt counter."""
    cur = _load_json(path) or {"announced": [], "records": {}}
    cur["announced"] = [k for k in (cur.get("announced") or []) if k != key]
    if record is not None:
        cur.setdefault("records", {})[key] = record
    try:
        Path(path).write_text(json.dumps(cur, indent=1))
    except Exception:
        pass
    return cur


def _blocked_attempts(ledger, key):
    try:
        return int(((ledger or {}).get("records") or {}).get(key, {})
                   .get("review_blocked_attempts") or 0)
    except Exception:
        return 0


def notify_desktop(title, msg):
    try:
        subprocess.run(["osascript", "-e",
                        'display notification %s with title %s'
                        % (json.dumps(msg[:200]), json.dumps(title[:80]))],
                       capture_output=True, timeout=15)
    except Exception:
        pass


def append_index(e, ctx_path, verdict_line, index_md=None, apply_cmd=None, checked=False):
    """The index the SessionStart hook reads: the next session that opens -- at
    any hour, with no re-arming -- is told this is open.

    apply_cmd, when present, is a VALIDATED proposal's one-liner. It is put on
    the row so the next session sees the option without opening anything, and
    it is phrased as an offer, never as something already done."""
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    who = e.get("slice_id") or e.get("job_id") or _subject(e)
    # NUMBERED RERUN: "<U+21BB> #3 " in front of the verdict, and the one-line cause
    # appended, so the row itself says this is the 3rd time and WHY it came back --
    # no need to open the context file to find that out.
    verdict_line = rerun_line(e, verdict_line)
    # checked=True: a verified auto-skip already retired it -- recorded, not open.
    row = ("- [%s] `%s` **%s** %s - %s - %s - `%s`%s\n"
           % ("x" if checked else " ", e.get("plan") or e.get("label") or "-", who, e.get("source"),
              stamp, verdict_line[:400], ctx_path,
              (" - PROPOSED FIX (unapplied, read it first): `%s`" % apply_cmd)
              if apply_cmd else ""))
    idx = Path(index_md or INDEX_MD)
    try:
        idx.parent.mkdir(parents=True, exist_ok=True)
        with idx.open("a") as fh:
            fh.write(row)
    except Exception:
        pass


def notice_route(e, index_md=None, ready_md=None):
    """PURE. (index file, row text) for a no-review notice. Only a STAGED chain is a
    ready-to-land notice and goes to READY_MD; every other no-review event (source F:
    a coding job VANISHED from the queue) is a real problem and stays an open row in
    INDEX_MD -- labelled as a notice, never as "READY TO LAND" (it used to be)."""
    if e.get("kind") == "integration-staged":
        return Path(ready_md or READY_MD), "READY TO LAND -- %s" % e.get("reason", "")
    return Path(index_md or INDEX_MD), "NOTICE (no review) -- %s" % e.get("reason", "")


def open_escalations(index_md=INDEX_MD):
    """Unchecked rows in the index. Read by the SessionStart hook."""
    try:
        return [l for l in Path(index_md).read_text().splitlines()
                if l.startswith("- [ ] ")]
    except Exception:
        return []


# --------------------------------------------------------------------------
ESC_KEEP_PER_SLUG = 5


def _queue_live_paths(state=None):
    """Every task_file / capture_final_as a LIVE queue row still points at. Never raises."""
    out = set()
    try:
        st = json.loads(Path(state or (Path.home() / "bin" / "ollama-queue-state.json")).read_text())
        for j in st.get("jobs") or []:
            if j.get("status") in _ACTIVE_STATES:
                for k in ("task_file", "capture_final_as"):
                    if j.get(k):
                        out.add(str(j[k]))
    except Exception:
        pass
    return out


def prune_escalations(esc_dir=None, keep=ESC_KEEP_PER_SLUG, protect=None):
    """Retention cap. Files are `<UTC ts>-<slug>[.review.md|.local-review.*]`; one slice
    once accumulated 1063 near-identical sets (18MB). Keep the newest `keep` sets per
    slug, delete older ones. Returns the number of files removed; never raises.

    2026-10-06 (0fb392936848): a set whose review job is still PENDING in the queue is
    never pruned. The watcher re-escalated the same job every pass (8 sets) while
    reusing the first pass's pending review (_sibling_review); pass 6 pruned that set's
    .local-review.task.md and the review row then failed at launch ("No such file"),
    so the subject never got a review. `protect` = paths live rows reference (default:
    read from the queue state)."""
    esc_dir = Path(esc_dir or ESC_DIR)
    n = 0
    try:
        live = set(protect) if protect is not None else _queue_live_paths()
        sets = {}
        for f in esc_dir.glob("[0-9]*T[0-9]*Z-*"):
            m = re.match(r"^(\d{8}T\d{6}Z)-(.+?)(\.review\.md|\.local-review\..*|\.md)$", f.name)
            if m:
                sets.setdefault(m.group(2), {}).setdefault(m.group(1), []).append(f)
        for slug, by_ts in sets.items():
            for ts in sorted(by_ts)[:-keep] if len(by_ts) > keep else []:
                if any(str(f) in live for f in by_ts[ts]):
                    continue   # a live queue row still reads/writes this set
                for f in by_ts[ts]:
                    f.unlink(missing_ok=True)
                    n += 1
    except Exception:
        pass
    return n


def retire_sweep(run=subprocess.run, tool=None):
    """Resolve needs_opus/escalated/failed rows whose slice is now done/skipped
    (the write happens in dispatch-self-heal.py, never here)."""
    tool = Path(tool) if tool else SELF_HEAL
    try:
        if tool.exists():
            r = run([sys.executable, str(tool), "--retire-sweep"],
                    capture_output=True, text=True, timeout=300)
            for l in (r.stdout or "").splitlines():
                if l.strip() and l.strip() != "retired 0":
                    print("  " + l)
    except Exception as exc:
        print("  retire-sweep error: %s" % exc)
    # JOB SWEEP (2026-10-05, rt-bg-commitments-fix 95234578bc40): a needs_opus NON-slice
    # row whose continuation PASSED is resolved (its D row and its bundle's PARKED row
    # then close via index_janitor below); a failed capped heal round gets the next one.
    # Local-model only -- this never spawns Opus/claude -p. See dispatch-self-heal.py.
    try:
        if tool.exists():
            r = run([sys.executable, str(tool), "--job-sweep"],
                    capture_output=True, text=True, timeout=300)
            for l in (r.stdout or "").splitlines():
                if l.strip():
                    print("  " + l)
    except Exception as exc:
        print("  job-sweep error: %s" % exc)
    # HYGIENE SWEEP (2026-10-06): superseded / launch-failed esc-review rows are
    # retired through the queue's own verbs (cancel --automated / resolve), capped
    # and alerting -- see dispatch-self-heal.py hygiene_sweep.
    try:
        if tool.exists():
            r = run([sys.executable, str(tool), "--hygiene-sweep"],
                    capture_output=True, text=True, timeout=300)
            for l in (r.stdout or "").splitlines():
                if l.strip():
                    print("  " + l)
    except Exception as exc:
        print("  hygiene-sweep error: %s" % exc)


HANDOFF_AUTOCLEAR = Path(__file__).resolve().parent / "handoff-autoclear.py"


def handoff_autoclear(run=subprocess.run, tool=None):
    """Keep the handoff panel near 0 without a human (2026-10-09): every pass, the
    mechanical rules clear routine finished jobs; the (rate-limited, kill-switched)
    Sonnet batch classifier handles the rest. NEVER touches awaiting_signoff or an item a
    live job/agent still needs (enforced inside the tool). Contained."""
    tool = Path(tool) if tool else HANDOFF_AUTOCLEAR
    _prev = os.environ.get("HANDOFF_AUTOMATED")
    os.environ["HANDOFF_AUTOMATED"] = "1"      # handoff-emit's automated-clear guards apply
    try:
        if tool.exists():
            r = run([sys.executable, str(tool), "--auto"], capture_output=True, text=True,
                    timeout=900)
            for l in (r.stdout or "").splitlines():
                if l.strip():
                    print("  handoff-autoclear " + l.strip())
    except Exception as exc:
        print("  handoff-autoclear error: %s" % exc)
    finally:
        if _prev is None:
            os.environ.pop("HANDOFF_AUTOMATED", None)
        else:
            os.environ["HANDOFF_AUTOMATED"] = _prev


def index_janitor(index_files=None):
    """Tick index rows whose item is RESOLVED in pipeline state (slice done/skipped,
    plan cancelled, chain landed, job resolved/done, bundle unparked) -- see
    escalation_index_janitor.py. 2026-10-05: 40 open rows, every one already resolved
    elsewhere; nothing ever ticked a row. Contained: a janitor failure never stops the
    pass. Kill switch: DISPATCH_INDEX_JANITOR=off."""
    if os.environ.get("DISPATCH_INDEX_JANITOR", "on").lower() == "off":
        return []
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import escalation_index_janitor as _eij
        files = index_files or (INDEX_MD, READY_MD)
        # SLICE_RUNS read at CALL time (the end-to-end harness overrides it).
        facts = _eij.Facts(runs_dir=SLICE_RUNS,
                           plans_dir=Path(SLICE_RUNS).parent / "slice-plans")
        return _eij.run(files, facts=facts, apply=True,
                        out=lambda s: print("  index-janitor " + s))
    except Exception as exc:
        print("  index-janitor error: %s" % exc)
        return []


def drop_continued_jobs(found, sh=None, jobs=None, sidecars=None):
    """Drop source-D (needs_opus) escalations whose job is ALREADY continued -- a live
    or passed continuation (dispatch-self-heal.continuation_outcome) or a `-vN` re-run
    in the same bundle. 2026-10-06: 77d808c3984a got a 30-min GPU review queued at
    05:30Z although its relaunch 24123dd86140 was enqueued; reviewing a superseded
    round diagnoses a run nobody will act on. If the continuation later FAILS, the
    row stops being dropped and is reviewed then. Never raises (returns `found`)."""
    try:
        if sh is None:
            import importlib.util as _ilu
            _s = _ilu.spec_from_file_location("dsh_cont", str(SELF_HEAL))
            sh = _ilu.module_from_spec(_s)
            _s.loader.exec_module(sh)
        jobs = jobs if jobs is not None else sh._queue_jobs()
        sc = sidecars if sidecars is not None else sh._all_sidecars()
        led = sh._load(sh.HEAL_LEDGER) or {}
        by_id = {j.get("id"): j for j in jobs}
        keep = []
        for e in found:
            j = by_id.get(e.get("job_id")) if e.get("source") == "D" else None
            if j is not None:
                oc, cid = sh.continuation_outcome(j, jobs, led, sidecars=sc)
                if oc not in ("passed", "live"):
                    oc, cid = sh._sibling_version(j, jobs + sc)
                if oc in ("passed", "live"):
                    print("  D %s (%s): continued by %s (%s) -- not reviewed"
                          % (e.get("job_id"), e.get("label"), cid, oc))
                    continue
            keep.append(e)
        return keep
    except Exception:
        return found


WORKTREE_REAP = Path(__file__).resolve().parent / "dispatch-worktree-reap"
WORKTREE_REAP_STAMP = Path.home() / ".ollama-dispatch" / "worktree-reap.last"
WORKTREE_REAP_EVERY_S = 24 * 3600


def worktree_reap_daily(run=subprocess.run, now=None, stamp=None, tool=None):
    """Once a day, run the content-verified worktree reaper (--apply). It only removes
    a worktree whose every change is byte-identical on origin/main, that changed
    nothing real, or whose dir is gone -- and keeps anything a queue row, slice run,
    run-status row or the handoff panel still references. It had last run 2026-09-27
    (no schedule existed); 342 worktrees had piled up. Returns True if it ran."""
    stamp = Path(stamp or WORKTREE_REAP_STAMP)
    tool = Path(tool or WORKTREE_REAP)
    now = time.time() if now is None else now
    try:
        if not tool.exists():
            return False
        if stamp.exists() and now - stamp.stat().st_mtime < WORKTREE_REAP_EVERY_S:
            return False
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)) + "\n")
        os.utime(stamp, (now, now))
        r = run([sys.executable, str(tool), "--apply"], capture_output=True, text=True,
                timeout=1200)
        tail = [l for l in (r.stdout or "").splitlines() if "REAPED" in l or "reaped" in l]
        print("  worktree-reap: rc=%s %s" % (r.returncode, " | ".join(tail[:3])))
        return True
    except Exception as exc:
        print("  worktree-reap error: %s" % exc)
        return False


CLEANUP_TOOL = Path(__file__).resolve().parent / "worktree_cleanup.py"
CLEANUP_STAMP = Path.home() / ".ollama-dispatch" / "worktree-cleanup.last"


def worktree_cleanup_daily(run=subprocess.run, now=None, stamp=None, tool=None):
    """Once a day, run the git-history based worktree sweep (worktree_cleanup.py sweep --auto).
    --auto means it only DELETES when ~/.ollama-dispatch/worktree-cleanup.enabled exists;
    otherwise it dry-runs and logs what it would remove. Returns True if it ran."""
    stamp = Path(stamp or CLEANUP_STAMP)
    tool = Path(tool or CLEANUP_TOOL)
    now = time.time() if now is None else now
    try:
        if not tool.exists():
            return False
        if stamp.exists() and now - stamp.stat().st_mtime < WORKTREE_REAP_EVERY_S:
            return False
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)) + "\n")
        os.utime(stamp, (now, now))
        r = run([sys.executable, str(tool), "sweep", "--auto"], capture_output=True, text=True,
                timeout=1800)
        n = sum(1 for l in (r.stdout or "").splitlines() if l.startswith(("REMOVED", "WOULD REMOVE")))
        print("  worktree-cleanup: rc=%s %d removable/removed" % (r.returncode, n))
        return True
    except Exception as exc:
        print("  worktree-cleanup error: %s" % exc)
        return False


def handle_no_evidence(e, body, ctx, stem, render_failed=False, esc_dir=None, index_md=None):
    """The pre-spawn evidence gate. True when the context carries NO evidence (and the render did
    not fail): the context file gets a `NO EVIDENCE AVAILABLE` section, a deterministic (no LLM)
    review + verdict line is recorded, the index row is written, and the caller must NOT spawn a
    model review or self-heal. False when a review should proceed."""
    if render_failed:
        return False
    ok, _missing = context_has_evidence(body)
    if ok:
        return False
    esc_dir = Path(esc_dir) if esc_dir else ESC_DIR
    try:
        Path(ctx).write_text(body.rstrip("\n") + "\n" + no_evidence_section(e))
    except Exception:
        pass
    fc = e.get("failure_class")
    if not fc and e.get("job_id"):
        try:
            fc = next((j.get("failure_class") for j in json.loads(
                (Path.home() / "bin" / "ollama-queue-state.json").read_text()).get("jobs") or []
                if j.get("id") == e["job_id"]), None)
        except Exception:
            fc = None
    try:
        nrev = no_evidence_review(e, fc)
    except Exception as nx:
        nrev = ("%s -- no evidence in the context; no model review was run "
                "(deterministic verdict failed: %r).\n" % (NO_EVIDENCE_MARK, nx))
    try:
        (esc_dir / ("%s.review.md" % stem)).write_text(nrev)
    except Exception:
        pass
    vl = "%s -- %s: no model review run, no action taken, escalation stays open" % (
        verdict_line(nrev, e.get("reason")), NO_EVIDENCE_MARK)
    append_index(e, ctx, vl, index_md or INDEX_MD)
    notify_desktop("Dispatch escalation (no evidence): %s" % _subject(e), rerun_line(e, vl))
    print("  NO EVIDENCE %s -> %s (no review spawned, no action)" % (esc_key(e), ctx))
    return True


def handle_no_verdict_review(e, ctx, review, index_md=None):
    """A review that is a NAMED STOP, not a verdict (REVIEW CAPPED from a worker bound): True when
    handled -- the index row says why, and the caller must not classify, patch, self-heal or
    re-review. The escalation stays open."""
    if not _verdict_mod().is_no_verdict_review(review):
        return False
    first = (str(review).strip().splitlines() or ["REVIEW CAPPED"])[0].strip()[:240]
    append_index(e, ctx, "%s -- no model verdict, no action taken, escalation stays open" % first,
                 index_md or INDEX_MD)
    notify_desktop("Dispatch review capped: %s" % _subject(e), rerun_line(e, first))
    print("  REVIEW CAPPED %s (%s) -> %s" % (esc_key(e), first, ctx))
    return True


def run_once(dry_run=False):
    if not dry_run:
        prune_escalations()
        retire_sweep()
        index_janitor()
        worktree_reap_daily()
        worktree_cleanup_daily()
        handoff_autoclear()
    found = []
    # SLICE_RUNS is passed explicitly, never left to the def-time default: the
    # end-to-end harness overrides these module globals, and a default bound at
    # import time would silently keep reading the real directory.
    known = subplan_names(SLICE_RUNS)
    # ONE queue read per pass, shared by sources C/D (stuck/needs_opus) and F
    # (vanished). Unreadable -> empty text -> F sees an empty table and stays silent.
    try:
        _qtext = subprocess.run([sys.executable, str(QUEUE_PY), "status"],
                                capture_output=True, text=True, timeout=60).stdout
    except Exception:
        _qtext = ""
    try:
        _qids = {r["id"] for r in parse_queue_status(_qtext)}
    except Exception:
        _qids = set()

    def _has_sidecar(jid):
        for base in (QUEUE_LOGS, QUEUE_LOGS / "archive"):
            for suf in (".done.json", ".gate.json"):
                if (base / (jid + suf)).is_file():
                    return True
        return False
    for p in plan_files(SLICE_RUNS):
        st = _load_json(p)
        if isinstance(st, dict) and _plan_cancelled(p.stem):
            continue    # a HUMAN cancel is terminal: nothing to review or self-heal
        if isinstance(st, dict):
            try:
                found += detect_slice_escalations(
                    st, p.stem, known,
                    queue_rows=(parse_queue_status(_qtext) if _qtext else None))
            except Exception:
                pass
            # Contained separately: source E must not be able to take source A
            # down with it, and vice versa (the per-source containment rule in
            # this file's header).
            try:
                found += detect_integration_escalations(st, p.stem)
            except Exception:
                pass
            try:
                found += detect_vanished_jobs(st, p.stem, _qids, _has_sidecar)
            except Exception:
                pass
    try:
        found += detect_stuck_jobs(status_text=_qtext or None)
    except Exception:
        pass
    try:
        found += detect_needs_info_jobs()
    except Exception:
        pass
    found = drop_private_jobs(found)
    try:
        found = drop_continued_jobs(found)
    except Exception:
        pass

    ledger = _load_json(LEDGER) or {"announced": [], "records": {}}
    fresh = new_escalations(found, ledger)
    # NUMBERED RERUNS. An escalation whose masked key is already announced is NOT
    # new -- but its RAW reason may have got worse. track_reruns numbers every raw
    # change in the ledger and un-announces only the ones worth re-surfacing, which
    # then join `fresh` carrying e['rerun'] (number + what changed + cause).
    try:
        ledger, _revived = track_reruns(found, ledger, LEDGER,
                                        persist=not dry_run)
        fresh = fresh + _revived
    except Exception as _ex:
        print("  WARN rerun tracking pass failed (%r) -- escalations unaffected"
              % (_ex,))
    # Same escalation twice in ONE pass (a job listed twice, or two sources
    # agreeing) must produce one context file and one review, not two.
    _seen_keys, _uniq = set(), []
    for e in fresh:
        k = esc_key(e)
        if k not in _seen_keys:
            _seen_keys.add(k)
            _uniq.append(e)
    fresh = _uniq

    print("# escalation-watcher: %d detected, %d new" % (len(found), len(fresh)))
    if dry_run:
        for e in fresh:
            print("  NEW %s  %s" % (esc_key(e), e.get("reason")))
        return 0

    ESC_DIR.mkdir(parents=True, exist_ok=True)
    for e in fresh:
        # WAIT OUT A SESSION LIMIT INSTEAD OF SPENDING THE BUDGET ON IT
        # (2026-09-25). See review_blocked_until(). Checked BEFORE the context file is
        # written: this used to sit after it, so a rate-limited reviewer still emitted
        # a fresh ~17KB context file every 5 minutes (1063 files / 18MB for ONE slice,
        # 2026-10-01). Nothing is written, spawned or counted; the key is already
        # un-announced (the blocked branch below did that), so a later pass retries.
        _wait = review_blocked_wait(ledger, esc_key(e))
        if _wait > 0 and not e.get("no_review"):
            print("  REVIEW DEFERRED %s -- the reviewer is rate-limited for another "
                  "%dm (%s); no file written, not spawning, not counting an attempt."
                  % (esc_key(e), _wait // 60,
                     ((ledger.get("records") or {}).get(esc_key(e)) or {})
                     .get("review_blocked_reason") or "session limit"))
            continue
        state = _load_json(SLICE_RUNS / ("%s.json" % e["plan"])) if e.get("plan") else None
        stem = "%s-%s" % (time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()),
                          re.sub(r"[^A-Za-z0-9]+", "-",
                                 "%s-%s" % (e.get("plan") or "job",
                                            e.get("slice_id") or e.get("job_id")
                                            or ("integration" if e.get("kind") == "integration" else ""))).strip("-"))
        ctx = ESC_DIR / ("%s.md" % stem)
        # PER-ENTRY CONTAINMENT (2026-09-24). The detectors are contained per
        # source, but the announce loop was not: one record the context builder
        # could not render took the rest of the pass down with it, every 5 min,
        # and the ledger never learned about the entries behind it. A context
        # file that says "the builder failed, here is the reason line" still gets
        # the escalation in front of a reviewer; a traceback gets it in front of
        # nobody.
        _render_failed = False
        try:
            body = build_context(e, state)
        except Exception as ex:
            _render_failed = True
            body = ("# %s\n\n- key: `%s`\n- reason: %s\n\n(context builder failed: %r "
                    "-- the escalation is real, the rendering is not; see the plan "
                    "state file)\n" % (_subject(e), esc_key(e), e.get("reason"), ex))
            print("  WARN %s: build_context failed (%r) -- announcing with a minimal "
                  "context" % (esc_key(e), ex))
        ctx.write_text(body)

        # Ledger BEFORE the review: a crashed/slow review must never cause the
        # same escalation to be re-announced (and re-spawned) every 5 minutes.
        # RE-READ and MERGE, never blind-write. Two passes CAN overlap (a manual
        # run beside the launchd one, or a launchd fire while a slow review is
        # still going), and a blind write clobbers whatever the other pass
        # announced -- which re-announces it, and re-spawns its Opus review, on
        # the next tick. Observed live on the first real pass: job 813d71882974
        # got two context files and two reviews.
        # Carry EVERY review_blocked_* field across the rewrite. _merge_ledger replaces
        # the whole record, and this used to carry only the attempt counter: the
        # `review_blocked_until` the blocked branch stored was destroyed here before
        # review_blocked_wait() could read it, so the cooldown never fired and an Opus
        # review was spawned on every 5-minute pass for days (root cause of the
        # 2026-10-01 weekly-limit exhaustion; no live ledger record had an `until`).
        _prior_rec = dict((ledger.get("records") or {}).get(esc_key(e)) or {})
        _rec = {k: v for k, v in _prior_rec.items() if k.startswith("review_blocked_")}
        _rec.update({"context": str(ctx),
                     "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                     "source": e.get("source"), "reason": e.get("reason")})
        # NUMBERED RERUNS: the RAW signature (the masked one is the key) plus the
        # counter, so the NEXT pass can diff against what was actually announced
        # here. A first announcement is #1.
        _rr = e.get("rerun") or {}
        _rec.update({"raw_signature": str(e.get("signature") or ""),
                     "escalation_n": int(_rr.get("n")
                                         or _prior_rec.get("escalation_n") or 1),
                     "rerun_job_id": e.get("job_id") or _prior_rec.get("rerun_job_id"),
                     "rerun_class": outcome_class(e)})
        if _rr.get("note"):
            _rec["rerun_note"] = _rr["note"]
        ledger = _merge_ledger(LEDGER, esc_key(e), _rec)

        # NO-REVIEW NOTICES (a chain staged for landing): nothing to diagnose, so
        # no Claude session is spent. Index row + desktop ping, and done.
        if e.get("no_review"):
            _idx, _line = notice_route(e)
            append_index(e, ctx, _line, _idx)
            notify_desktop("Dispatch: %s" % _subject(e), rerun_line(e, _line))
            print("  NOTICE %s -> %s" % (esc_key(e), ctx))
            continue

        # PRE-SPAWN EVIDENCE GATE (2026-10-09, the owner: "how was it missing the required
        # checks and still queued?"). The review task tells the reviewer to read plan state /
        # worktree / log evidence; a context without ANY of it (a bare job row whose evidence
        # could not be resolved from its label) used to be reviewed anyway -- the model
        # explored for 7 min and ran away. No evidence -> no model review: record
        # `NO EVIDENCE AVAILABLE` + a deterministic (no LLM) verdict, take NO action (the
        # marker line is refused by self-heal), keep the escalation open. A render failure is
        # exempt: its minimal context still points the reviewer at the plan state file.
        if handle_no_evidence(e, body, ctx, stem, render_failed=_render_failed):
            continue

        # (the session-limit deferral now runs at the TOP of this loop, before the
        # context file is written -- see "WAIT OUT A SESSION LIMIT" above.)
        # But _merge_ledger above just re-ANNOUNCED this key, so a deferral cannot
        # be the thing that un-announces it; the blocked branch below does.
        review = spawn_review(
            ctx, repo=(state or {}).get("repo"),
            prompt=(INTEGRATION_REVIEW_PROMPT if e.get("kind") == "integration"
                    else REVIEW_PROMPT),
            bundle=review_bundle(e))
        # GROUNDING POLICY STAMP (the watcher stamps, never the model): only a review spawned
        # under the grounded prompt is enforced; the stamp is what self-heal keys on. A
        # blocked / capped review is not a verdict and is not stamped.
        if (e.get("kind") != "integration" and not is_review_blocked(review)
                and not _verdict_mod().is_no_verdict_review(review)):
            try:
                import escalation_grounding as _eg
                review = _eg.stamp(review)
            except Exception:
                pass
        try:
            (ESC_DIR / ("%s.review.md" % stem)).write_text(review + "\n")
        except Exception:
            pass

        # BLOCKED REVIEW == INFRA FAILURE, NOT A VERDICT.
        # A session/usage-limit refusal used to be written out as a normal
        # completed review with "proposed fix: none written" (live: job
        # 3ca17c902224, 2026-09-22) -- a false negative that left the job parked
        # in needs_opus forever because nothing knew to try again. Never run
        # extract_patch/write the proposal footer on it, and un-announce the key
        # so the next watcher pass re-reviews it. Bounded, so a permanently
        # broken CLI surfaces to a human instead of respawning forever.
        if is_review_blocked(review):
            why = review.splitlines()[0].strip()
            # The CLOCK is the primary gate: a refusal that NAMES its reset time sets
            # review_blocked_until, and the loop-top deferral keeps every pass off the
            # reviewer until then. Every spawn that reaches here is a REAL failed
            # attempt, so count it regardless -- the cap is the backstop. It used to
            # skip the count when an `until` parsed, which (with the carry-over bug)
            # made REVIEW_BLOCKED_MAX_ATTEMPTS unreachable and the retry unbounded.
            until = review_blocked_until(review)
            attempts = _blocked_attempts(ledger, esc_key(e)) + 1
            rec = dict((ledger.get("records") or {}).get(esc_key(e)) or {})
            rec.update({"context": str(ctx),
                        "review_blocked_attempts": attempts,
                        "review_blocked_reason": why,
                        "review_blocked_at": time.strftime(
                            "%Y-%m-%dT%H:%M:%SZ", time.gmtime())})
            if until:
                rec["review_blocked_until"] = until
            else:
                rec.pop("review_blocked_until", None)
            retryable = attempts < REVIEW_BLOCKED_MAX_ATTEMPTS
            if retryable:
                ledger = _ledger_unannounce(LEDGER, esc_key(e), rec)
            else:
                ledger = _merge_ledger(LEDGER, esc_key(e), rec)
                append_index(e, ctx, "%s -- NO REVIEW after %d attempts (%s)"
                             % (REVIEW_BLOCKED_PREFIX, attempts, why), INDEX_MD)
            _when = ("waiting %dm for the reset (no attempt spent)"
                     % (max(0, int(until - time.time())) // 60) if until
                     else ("attempt %d/%d" % (attempts, REVIEW_BLOCKED_MAX_ATTEMPTS)))
            notify_desktop(
                "Dispatch review BLOCKED: %s" % (e.get("slice_id") or e.get("job_id")),
                "%s (%s)%s" % (why, _when,
                               "" if retryable else " -- giving up, see index"))
            print("  REVIEW BLOCKED %s (%s, %s) -> %s"
                  % (esc_key(e), _when,
                     "will retry" if retryable else "GIVING UP", ctx))
            continue

        # CAPPED / NO-VERDICT REVIEW (worker exit reason output_cap_review / repeat_call_loop):
        # a named stop, not a verdict -- nothing classified, patched, self-healed or re-reviewed.
        if handle_no_verdict_review(e, ctx, review, INDEX_MD):
            continue

        # PROPOSE, NEVER APPLY. The review is still read-only (REVIEW_TOOLS has
        # no Edit/Write and is not widened for this); the proposal arrives as
        # TEXT in the review, and the only thing done with it here is
        # `git apply --check`. A diff that does not apply is NOT written out.
        review = ensure_verdict(e, ctx, stem, review)
        vline = verdict_line(review, e.get("reason"))
        # GROUNDING CHECK: verify every cited file:line + quote; record what was dropped on
        # the review file and make UNVERIFIED visible on the index row. Advisory here -- the
        # ACTION gate lives in dispatch-self-heal.py (it re-derives the same report).
        try:
            import escalation_grounding as _eg
            if _eg.enforced(review):
                _gr = _eg.assess(review, Path(ctx).read_text(errors="replace"))
                with (ESC_DIR / ("%s.review.md" % stem)).open("a") as fh:
                    fh.write(_eg.report_md(_gr))
                if _gr["status"] == "UNVERIFIED":
                    vline = "[%s] %s" % (_eg.label(_gr), vline)
        except Exception:
            pass
        repo = (state or {}).get("repo")
        # A slice's harness lives in its OWN worktree (fixture/refimpl are not in
        # the plan repo at all), so a slice proposal is validated there.
        _swt = (e.get("slice") or {}).get("worktree") if e.get("kind") == "slice" else None
        if _swt and Path(str(_swt)).is_dir():
            repo = _swt
        apply_cmd, patch_note = None, "no fix proposed"
        patch = extract_patch(review)
        # LITERAL AUDIT: a review that misspells the spec's own identifiers (in a
        # way no worktree file does) cannot be trusted on spelling; flag it in the
        # index, and hold any diff that carries a drifted token (self-heal would
        # otherwise apply it).
        _lwt = escalation_worktree(e, state)
        _lfacts = spec_literal_facts(_lwt) if _lwt else None
        _drift = review_literal_drift(review, _lfacts)
        _drift_in_patch = bool(patch) and any(t in patch for t, _w in _drift)
        if _drift:
            try:
                with (ESC_DIR / ("%s.review.md" % stem)).open("a") as fh:
                    fh.write(drift_note(_drift, _lfacts))
            except Exception:
                pass
            vline = "[REVIEW LITERAL DRIFT: %s -- trust the machine check] %s" % (
                ", ".join("`%s`~`%s`" % d for d in _drift[:3]), vline)
        if _drift_in_patch:
            patch, patch_note = None, "held: the diff carries a drifted identifier spelling"
        if patch:
            apply_cmd, patch_note = write_proposal(
                patch, repo, ESC_DIR / ("%s.patch" % stem))
        try:
            with (ESC_DIR / ("%s.review.md" % stem)).open("a") as fh:
                fh.write("\n---\nproposed fix: %s\n"
                         % (("VALIDATED -- apply by hand with:\n\n    %s\n" % apply_cmd)
                            if apply_cmd else ("none written (%s)" % patch_note)))
        except Exception:
            pass
        # SELF-HEAL (2026-09-26). A harness-side slice escalation is acted on
        # instead of parked: dispatch-self-heal.py applies a reviewed HARNESS-ONLY
        # diff in the slice worktree and --regates (the relevance gate still decides
        # GO), or --retry-slices an authoring-class failure. Bounded per slice, every
        # action logged to the context file. The writes happen in that tool, never
        # in this module (see the structural read-only checks in self_test).
        heal = (None if _drift_in_patch
                else self_heal(e, ctx, ESC_DIR / ("%s.review.md" % stem)))
        if heal:
            vline = "%s [self-heal: %s]" % (vline, heal)
        append_index(e, ctx, vline, INDEX_MD, apply_cmd=apply_cmd,
                     checked=heal_retired_it(heal))
        notify_desktop("Dispatch escalation: %s" % _subject(e), rerun_line(e, vline))
        print("  ESCALATED %s -> %s%s" % (esc_key(e), ctx,
                                          (" (self-heal: %s)" % heal) if heal else ""))
    return 0


SELF_HEAL = Path(__file__).resolve().parent / "dispatch-self-heal.py"


def review_bundle(e):
    """PURE. The bundle an escalation review job joins (the owner 2026-10-02: every job
    belongs to a bundle; an esc-review is a child of what it reviews): the job's own
    bundle, else the slice plan, else None (the queue derives one)."""
    if e.get("bundle") or e.get("plan"):
        return e.get("bundle") or e.get("plan")
    if e.get("job_id"):
        try:
            st = json.loads((Path.home() / "bin" / "ollama-queue-state.json").read_text())
            for j in st.get("jobs") or []:
                if j.get("id") == e["job_id"]:
                    if j.get("bundle"):
                        return j["bundle"]
                    # a bundle-less row (needs-opus-auto-X placeholder, pruned parent): resolve the REAL
                    # bundle from the chain record / label, never leave it to the queue's job-<id> slug
                    # (2026-10-09: that stranded an idle lane and held an unrelated bundle).
                    b = _queue_label_bundle(j.get("label") or e.get("label"))
                    if b:
                        return b
        except Exception:
            pass
    if e.get("label"):
        return _queue_label_bundle(e.get("label"))
    return None


def _queue_label_bundle(label):
    """The queue's own resolver (chain auto-runs record, else the default bundle of the label)."""
    if not label:
        return None
    try:
        m = _queue_module()
        if m is None:
            return None
        return m.chain_bundle_for_label(label) or m.default_bundle_from_label(label) or None
    except Exception:
        return None


# An ACTION that handles the escalation by itself (2026-10-09): the row is recorded [x]
# with the action in it. If the action later fails, a NEW escalation row is raised.
ACTED_PREFIXES = ("skip-verified:", "closed:", "respec:", "heal-continuation:", "retired:",
                  "resumed:", "ladder-exit:")


def heal_retired_it(heal):
    """PURE. A VERIFIED auto-skip retired the slice: the index row is written [x]."""
    return str(heal or "").startswith(ACTED_PREFIXES)


def self_heal(e, ctx, review_path, run=subprocess.run, tool=None):
    """Hand a source-A slice escalation to dispatch-self-heal.py. Returns its
    one-word outcome ('regate' / 'retry' / 'skip:...') or None when not applicable.
    Advisory and contained: a broken self-heal never breaks the watcher pass."""
    if e.get("source") == "A" and e.get("kind") == "slice":
        target = ["--plan-label", str(e.get("plan")), "--slice", str(e.get("slice_id"))]
    elif e.get("source") == "D" and e.get("label"):
        # needs_opus JOB: of a slice -> only the VERIFIED already-satisfied skip applies;
        # a NON-slice auto-author job -> the bounded job ladder (heal_job, b/c verdicts).
        target = ["--job-label", str(e.get("label"))] + (
            ["--job-id", str(e.get("job_id"))] if e.get("job_id") else [])
    else:
        return None
    tool = Path(tool) if tool else SELF_HEAL
    if not tool.exists():
        return None
    try:
        # 2400s: a verified skip runs the slice verify twice (tip + reverted base)
        r = run([sys.executable, str(tool)] + target + ["--context", str(ctx),
                 "--review", str(review_path)],
                capture_output=True, text=True, timeout=2400)
        out = [l for l in (r.stdout or "").splitlines() if l.strip()]
        for l in out[:-1]:
            print("  " + l)
        return out[-1].strip() if out else "error: rc=%s %s" % (
            r.returncode, (r.stderr or "").strip()[-160:])
    except Exception as exc:
        return "error: %s" % exc


# --------------------------------------------------------------------------
def self_test():
    import tempfile
    ok = 0

    def check(name, cond):
        nonlocal ok
        print(("  PASS " if cond else "  FAIL ") + name)
        if cond:
            ok += 1
        else:
            self_test.failed = True

    self_test.failed = False
    print("dispatch-escalation-watcher self-test")

    # --- source A
    st = {"slices": {"s1": {"status": "done"},
                     "s4": {"status": "escalated", "escalation_reason": "2 identical failures"}}}
    a = detect_slice_escalations(st, "p")
    check("A fires on escalated only", len(a) == 1 and a[0]["slice_id"] == "s4")
    check("A carries the reason", "identical" in a[0]["reason"])

    # --- supersede rule (the false-positive that the first real pass surfaced)
    st = {"slices": {"s1-item": {"status": "escalated"}}}
    check("A is silent when a sub-plan already superseded the escalation",
          detect_slice_escalations(st, "bg-actions", {"bg-actions",
                                                      "bg-actions-s1-item"}) == [])
    check("A still fires when no sub-plan exists",
          len(detect_slice_escalations(st, "bg-actions", {"bg-actions"})) == 1)
    check("supersede needs an EXACT plan-slice match",
          not is_superseded("bg-actions", "s1-item", {"bg-actions-s1-item-x"}))
    check("supersede tolerates a missing plan set",
          not is_superseded("bg-actions", "s1-item", None))

    # --- source B (the signature-keeps-changing loop)
    st = {"slices": {"s5": {"status": "pending", "author_attempts": ATTEMPTS_ESCALATE}}}
    b = detect_slice_escalations(st, "p")
    check("B fires at threshold", len(b) == 1 and b[0]["source"] == "B")
    st = {"slices": {"s5": {"status": "pending", "author_attempts": ATTEMPTS_ESCALATE - 1}}}
    check("B silent below threshold", detect_slice_escalations(st, "p") == [])
    st = {"slices": {"s5": {"status": "done", "author_attempts": 99}}}
    _bst = {"slices": {"s2-x": {"status": "pending", "author_attempts": 9}}}
    check("B fires with no queue info", len(detect_slice_escalations(_bst, "aw")) == 1)
    check("B suppressed while an auto-author replacement is pending", detect_slice_escalations(
        _bst, "aw", queue_rows=[{"status": "pending", "label": "auto-author-aw-s2-x-s1"}]) == [])
    check("B NOT suppressed by an esc-review row or a finished author row", len(detect_slice_escalations(
        _bst, "aw", queue_rows=[{"status": "pending", "label": "esc-review-2026-aw-s2-x"},
                                {"status": "failed", "label": "auto-author-aw-s2-x-s1"}])) == 1)
    check("B never fires on a satisfied slice", detect_slice_escalations(st, "p") == [])
    st = {"slices": {"s5": {"status": "skipped", "author_attempts": 99}}}
    check("B never fires on a skipped slice", detect_slice_escalations(st, "p") == [])
    st = {"slices": {"s5": {"status": "pending", "author_attempts": "4"}}}
    check("B tolerates a string attempts count", len(detect_slice_escalations(st, "p")) == 1)
    check("B tolerates junk attempts",
          detect_slice_escalations({"slices": {"s": {"status": "pending",
                                                    "author_attempts": "x"}}}, "p") == [])

    # --- malformed state must never throw
    check("non-dict slices tolerated", detect_slice_escalations({"slices": []}, "p") == [])
    check("string slice tolerated", detect_slice_escalations({"slices": {"s": "x"}}, "p") == [])
    check("empty state tolerated", detect_slice_escalations({}, "p") == [])
    check("None state tolerated", detect_slice_escalations(None, "p") == [])

    # --- queue status parsing (the field-3 gotcha that no-op'd an earlier loop)
    rows = parse_queue_status(
        "[running ] 13a62c046cc0 auto-author-bg-actions-s1-item-s5-broker-id  qwen3.8:27b\n"
        "[done    ] fe325b2dec96 auto-author-bg-actions-s1-item\n"
        "garbage line\n")
    check("status parse finds 2 rows", len(rows) == 2)
    check("status parse takes the ID not the bracket",
          rows[0]["id"] == "13a62c046cc0" and rows[0]["status"] == "running")
    check("status parse keeps the label",
          rows[0]["label"].startswith("auto-author-bg-actions"))
    check("status parse skips junk", all(r["id"] for r in rows))

    # --- source D (the queue's own escalation lane)
    stxt = ("[needs_opus] 13a235b378c0 esim-global-s1-parse-global  escalated=[other] "
            "untrusted baseline: launch baseline was dirty\n"
            # A SYNTHETIC id, not a real one. This fixture used 13a62c046cc0,
            # which is a genuine job whose log file still sits in
            # ~/bin/ollama-queue-logs -- so _job_started_epoch resolved it off
            # real disk, source C fired on a 28-hour-old log, and this D-only
            # assertion failed on this machine (and only on this machine). A
            # self-test that reads live state is not a self-test.
            "[running ] ffffffffffff auto-author-synthetic-never-on-disk\n"
            "[done    ] fe325b2dec96 auto-author-bg-actions-s1-item\n")
    d = detect_stuck_jobs(status_text=stxt)
    check("D fires on needs_opus", len(d) == 1 and d[0]["source"] == "D")
    class _SH:
        HEAL_LEDGER = "/nonexistent"
        _load = staticmethod(lambda p: None)
        @staticmethod
        def continuation_outcome(j, jobs, led, sidecars=None):
            return ("live", "c2") if j["id"] == "a1" else (None, None)
        @staticmethod
        def _sibling_version(j, rows):
            return ("passed", "v2") if j["id"] == "b1" else (None, None)
    _f = [{"source": "D", "job_id": "a1"}, {"source": "D", "job_id": "b1"},
          {"source": "D", "job_id": "c1"}, {"source": "A", "job_id": None}]
    _kept = drop_continued_jobs(_f, sh=_SH, jobs=[{"id": "a1"}, {"id": "b1"}, {"id": "c1"}],
                                sidecars=[])
    with tempfile.TemporaryDirectory() as _td:
        _st, _tl = Path(_td) / "last", Path(_td) / "reap"
        _tl.write_text("x")
        _rc = []
        _rr = lambda argv, **k: _rc.append(argv) or type("R", (), {"returncode": 0, "stdout": ""})()
        _a = worktree_reap_daily(run=_rr, now=1000.0, stamp=_st, tool=_tl)
        _b = worktree_reap_daily(run=_rr, now=1000.0 + 3600, stamp=_st, tool=_tl)
        _c = worktree_reap_daily(run=_rr, now=1000.0 + 25 * 3600, stamp=_st, tool=_tl)
        check("worktree reaper runs (--apply) at most once a day",
              (_a, _b, _c, len(_rc), _rc[0][-1]) == (True, False, True, 2, "--apply"))
    check("D is not reviewed while its continuation is live / its -vN re-run passed",
          [e.get("job_id") for e in _kept] == ["c1", None])
    check("D takes the job id", d[0]["job_id"] == "13a235b378c0")
    check("D carries the queue's own escalation text", "untrusted baseline" in d[0]["reason"])
    check("D does not fire on done or short-running rows",
          all(x["source"] == "D" for x in d))

    # --- verdict-line extraction (the first live pass put a chatty preamble
    #     line into the index where a verdict belongs)
    check("verdict line is found anywhere in the review",
          verdict_line("some preamble\nVERDICT: a -- already satisfied\nmore")
          == "VERDICT: a -- already satisfied")
    check("a bolded/marked-up verdict still parses",
          verdict_line("**VERDICT: c -- harness defect**").startswith("VERDICT: c"))
    check("NO first-line fallback -- a chatty preamble never becomes the verdict",
          verdict_line("The file search I started in the background has now finished.\n"
                       "It isn't needed.") == "(no VERDICT line -- read the review file)")
    check("empty review yields the placeholder, not an exception",
          verdict_line("") == "(no VERDICT line -- read the review file)"
          and verdict_line(None) == "(no VERDICT line -- read the review file)")

    # --- blocked review (the false negative on job 3ca17c902224, 2026-09-22:
    #     a session-limit refusal on stdout was filed as a completed review)
    LIMIT = "You've hit your session limit · resets 7:30pm (America/Los_Angeles)"
    check("a session-limit refusal is NOT a review",
          (review_blocked_reason(LIMIT) or "").startswith("session limit"))
    check("the reset time is carried into the reason",
          "7:30pm" in (review_blocked_reason(LIMIT) or ""))
    check("empty stdout is still a blocked review",
          review_blocked_reason("", 1, "boom") is not None)
    check("a real verdict is never classified as blocked",
          review_blocked_reason("VERDICT: d -- genuine model incapacity") is None)
    check("a real review that DISCUSSES limits is not blocked",
          review_blocked_reason("VERDICT: c -- harness defect\nThe worker hit its "
                                "context limit; usage limit reached in the log.") is None)
    check("a non-zero rc with no VERDICT is blocked",
          review_blocked_reason("some chatter", 1) is not None)
    check("prose without a verdict and rc=0 is left alone (not over-eager)",
          review_blocked_reason("I read the context file and here are notes.") is None)
    check("is_review_blocked keys off the marker",
          is_review_blocked(REVIEW_BLOCKED_PREFIX + " (session limit)")
          and not is_review_blocked("VERDICT: a -- fine"))

    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "l.json"
        _merge_ledger(lp, "kB", {"context": "/c"})
        u = _ledger_unannounce(lp, "kB", {"review_blocked_attempts": 2})
        eb = {"kind": "job", "job_id": "kB", "signature": "s"}
        check("un-announce makes the escalation eligible again",
              "kB" not in u["announced"] and new_escalations([eb], u) == [eb])
        check("un-announce keeps the attempt counter", _blocked_attempts(u, "kB") == 2)
        check("attempt counter tolerates a missing record", _blocked_attempts(u, "nope") == 0)

    # --- ledger merge (the race that produced two reviews for job 813d71882974)
    with tempfile.TemporaryDirectory() as td:
        lp = Path(td) / "l.json"
        _merge_ledger(lp, "k1", {"a": 1})
        # simulate the OTHER pass writing concurrently, then us merging again
        lp.write_text(json.dumps({"announced": ["kOTHER"], "records": {"kOTHER": {}}}))
        m = _merge_ledger(lp, "k2", {"a": 2})
        check("merge KEEPS the other pass's entry instead of clobbering it",
              "kOTHER" in m["announced"] and "k2" in m["announced"])
        check("merge does not duplicate a key it already holds",
              _merge_ledger(lp, "k2", {"a": 3})["announced"].count("k2") == 1)
        check("merge survives an unreadable ledger",
              "k9" in _merge_ledger(Path(td) / "nope" / "l.json", "k9", {})["announced"])

    # --- dedup
    e1 = {"kind": "slice", "plan": "p", "slice_id": "s4", "signature": "sig"}
    e2 = {"kind": "slice", "plan": "p", "slice_id": "s4", "signature": "OTHER"}
    e3 = {"kind": "job", "job_id": "abc", "signature": "running>=90m"}
    led = {"announced": [esc_key(e1)]}
    check("already-announced is suppressed", new_escalations([e1], led) == [])
    check("a NEW signature re-announces", new_escalations([e2], led) == [e2])
    check("job keys are distinct from slice keys", esc_key(e3) != esc_key(e1))
    check("an empty ledger announces everything", len(new_escalations([e1, e3], {})) == 2)
    check("re-detecting the same thing twice in one pass keys identically",
          esc_key(e1) == esc_key(dict(e1)))

    # --- source E: the whole-chain integration gate -------------------------
    _fail = {"integration": {"status": "failed", "summary": "signature: 2 calls",
                             "failed_stages": ["signature"]}}
    E = detect_integration_escalations(_fail, "bg-orchestrator")
    check("a FAILED integration gate escalates", len(E) == 1 and E[0]["source"] == "E")
    check("...naming the plan, not a slice",
          E[0]["plan"] == "bg-orchestrator" and E[0]["slice_id"] is None)
    check("...and its reason carries the summary a reviewer needs",
          "signature: 2 calls" in E[0]["reason"])
    check("a PASSED integration gate is silent",
          detect_integration_escalations({"integration": {"status": "passed"}}, "p") == [])
    check("a gate that was SKIPPED by the escape hatch still escalates -- a gate "
          "turned off and forgotten is how an un-integratable chain reads as done",
          len(detect_integration_escalations({"integration": {"status": "skipped"}}, "p")) == 1)
    check("an UNAVAILABLE checker escalates (a gate that could not run has not passed)",
          len(detect_integration_escalations({"integration": {"status": "unavailable"}}, "p")) == 1)
    check("BACK-COMPAT: a plan state with NO integration key is never escalated "
          "(every chain written before this gate existed has none)",
          detect_integration_escalations({"slices": {}}, "p") == [])
    check("a non-dict integration value is tolerated, not crashed on",
          detect_integration_escalations({"integration": "failed"}, "p") == [])
    check("an empty/None state is tolerated",
          detect_integration_escalations(None, "p") == []
          and detect_integration_escalations({}, "p") == [])
    check("an unknown status is not escalated (only the declared bad ones speak)",
          detect_integration_escalations({"integration": {"status": "running"}}, "p") == [])
    _led = {"announced": [esc_key(E[0])]}
    check("the same failing gate is announced ONCE, not every 5 minutes",
          new_escalations(E, _led) == [])
    _partial = detect_integration_escalations(
        {"integration": {"status": "failed", "summary": "x", "failed_stages": ["tests"]}},
        "bg-orchestrator")
    check("...but a DIFFERENT set of failing stages re-announces (the chain moved)",
          new_escalations(_partial, _led) == _partial)
    check("an integration escalation is keyed by plan, distinctly from a slice one",
          esc_key(E[0]).startswith("integration:bg-orchestrator:"))
    check("it has a legible subject for the index row and the notification",
          _subject(E[0]) == "WHOLE-CHAIN integration gate")
    _ictx = build_context(E[0], {"label": "bg-orchestrator", "repo": "/r",
                                 "chain_worktree": "/wt"})
    check("its context file explains that the per-slice gates all PASSED",
          "passed its OWN adversarial verify" in _ictx)
    check("...and points the re-check at mini-swe, never at the retired slicer's levers (2026-11-02)",
          "old slicer retired -- use mini-swe" in _ictx and "--integrate" not in _ictx and "--integration-check\n" not in _ictx)
    # `files` is the DICT the slicer really writes ({path: status}); the old list
    # fixture let build_context's `[:20]` pass here while crashing every live pass
    # from 2026-09-23T22:42Z on (KeyError: slice(None, 20, None)).
    _stg = detect_integration_escalations(
        {"integration": {"status": "staged", "staged_commit": "abcdef1234567890",
                         "integrate_branch": "integrate/p-abcdef12", "onto": "main",
                         "files": {"src/x.py": "integrated", "src/y.py": "clean"}}}, "p")
    check("a STAGED chain is announced as a ready-to-land notice", len(_stg) == 1
          and _stg[0]["kind"] == "integration-staged" and _stg[0]["no_review"] is True)
    check("...keyed by the staged commit (re-staging the same tip is silent)",
          _stg[0]["signature"] == "integration:staged:abcdef123456")
    check("...and its reason names no retired landing lever",
          "old slicer retired -- use mini-swe" in _stg[0]["reason"] and "--land-integration" not in _stg[0]["reason"])
    check("...with a legible subject", _subject(_stg[0]) == "CHAIN STAGED -- ready to land")
    _sctx = build_context(_stg[0], {"label": "p", "repo": "/r"})
    check("its context names the scratch branch and the diff to review (the land lever is retired)",
          "integrate/p-abcdef12" in _sctx and "git -C <repo> diff main..integrate/p-abcdef12" in _sctx
          and "--land-integration" not in _sctx and "nothing written to the default branch" in _sctx)
    check("...and renders the slicer's {path: status} files DICT (the 09-23 crash)",
          "`src/x.py (integrated)`" in _sctx and "`src/y.py (clean)`" in _sctx)
    _stg_list = dict(_stg[0], integration=dict(_stg[0]["integration"], files=["a.py"]))
    check("a legacy files LIST still renders",
          "`a.py`" in build_context(_stg_list, {"label": "p"}))
    _stg_bad = dict(_stg[0], integration=dict(_stg[0]["integration"], files=42))
    _bad_raised = False
    try:
        build_context(_stg_bad, {"label": "p"})
    except Exception:
        _bad_raised = True
    check("an unrenderable record raises in build_context (so containment is what saves the pass)",
          _bad_raised)
    check("run_once contains a build_context failure per entry instead of dying mid-pass",
          "context builder failed" in __import__("inspect").getsource(run_once)
          and "ctx.write_text(body)" in __import__("inspect").getsource(run_once))

    # --- (2026-09-24) THE PANEL RE-CHECKS, IT DOES NOT REPLAY -------------------------
    # Two live failure shapes, both from records frozen at 2026-09-23T22:41:15Z and
    # re-announced for ~26h as present-tense truth:
    #   (1) rt-costco-always-sites: `staged`/clean rendered as "replays cleanly ... land
    #       it with --land-integration" after main had diverged from the stage, which
    #       would have deleted four live files.
    #   (2) eleven conflict-semantic records (bg-actions, bg-alert, bg-detection,
    #       bg-escalation, bg-health, bg-profile, bg-serpwatch, bg-state, bg-webui,
    #       scheduler, playwright_checks) rendered as live conflicts, several for chains
    #       whose content had since LANDED on main.
    # Both panels also said "base commit compared against: `none`" on EVERY escalation
    # ever emitted, because integ["base_rev"] was a key nothing wrote.
    _fr = tempfile.mkdtemp(prefix="esc-freshness-")
    try:
        _R = os.path.join(_fr, "repo")
        os.makedirs(_R)

        def _fg(*a):
            return subprocess.run(["git", "-C", _R, *a], capture_output=True, text=True)
        _fg("init", "-q", "-b", "main")
        _fg("config", "user.email", "t@t")
        _fg("config", "user.name", "t")
        Path(_R, "keep.py").write_text("x = 1\n")
        _fg("add", ".")
        _fg("commit", "-q", "-m", "base")
        _fbase = _fg("rev-parse", "HEAD").stdout.strip()
        # a code-only stage built on that exact tip, the way integrate_chain builds it
        _fg("checkout", "-q", "-b", "integrate/p-stage", _fbase)
        Path(_R, "new.py").write_text("y = 2\n")
        _fg("add", ".")
        _fg("commit", "-q", "-m", "staged code-only")
        _fstage = _fg("rev-parse", "HEAD").stdout.strip()
        _fg("checkout", "-q", "main")
        _frec = {"status": "staged", "verdict": "clean", "onto": "main",
                 "onto_tip": _fbase, "base_rev": _fbase, "chain_tip": "c0",
                 "staged_commit": _fstage, "integrate_branch": "integrate/p-stage",
                 "files": {"new.py": "clean"}, "at": "2026-09-24T00:00:00+00:00"}
        _fe = {"source": "E", "kind": "integration-staged", "plan": "p",
               "reason": "staged, ready to land", "integration": _frec}
        _ctx_ok = build_context(_fe, {"label": "p", "repo": _R})
        check("a stage still sitting on the CURRENT tip renders as ready-to-land",
              "Review, then land with:" in _ctx_ok and "RE-VERIFIED" in _ctx_ok
              and "NOT landable" not in _ctx_ok)
        check("...and the panel PRINTS the base it was compared against, never `none`",
              ("- base commit compared against: `%s`" % _fbase) in _ctx_ok
              and "compared against: `none`" not in _ctx_ok)
        check("...and shows the classified-vs-now tips side by side",
              "tip WHEN CLASSIFIED" in _ctx_ok and "tip RIGHT NOW" in _ctx_ok)
        # main moves on underneath the stage -- the rt-costco shape exactly.
        Path(_R, "landed_later.py").write_text("z = 3\n")
        _fg("add", ".")
        _fg("commit", "-q", "-m", "an unrelated chain lands on main")
        _ctx_stale = build_context(_fe, {"label": "p", "repo": _R})
        check("REPRO rt-costco-always-sites: once main moves, the SAME staged record is "
              "NO LONGER rendered as ready-to-land",
              "Review, then land with:" not in _ctx_stale)
        check("...it says plainly that the stage is not landable and must not be landed",
              "NOT landable" in _ctx_stale and "Do NOT land this" in _ctx_stale
              and "MOVED" in _ctx_stale)
        check("...names the file that landing the stale stage would DELETE",
              "landed_later.py" in _ctx_stale)
        check("...marks the stored verdict CACHED rather than current",
              "CACHED" in _ctx_stale)
        check("...and asks for a re-stage instead of a landing",
              "needs rebuilding on the current tip" in _ctx_stale and "old slicer retired -- use mini-swe" in _ctx_stale)
        check("...and reports the divergence as genuine (neither side an ancestor)",
              "genuine divergence" in _ctx_stale)
        _fnow = _fg("rev-parse", "main").stdout.strip()
        _fconf = {"status": "failed", "verdict": "conflict-semantic", "onto": "main",
                  "onto_tip": _fbase, "base_rev": _fbase,
                  "failed_stages": ["classify:conflict-semantic"],
                  "summary": "verdict conflict-semantic: STOP -> human",
                  "at": "2026-09-23T22:41:15+00:00"}
        _ctx_conf = build_context({"source": "E", "kind": "integration", "plan": "p",
                                   "reason": "integration failed",
                                   "integration": _fconf}, {"label": "p", "repo": _R})
        check("REPRO the 11 false conflicts: a conflict computed against an older tip is "
              "labelled CACHED, not reprinted as present tense",
              "CACHED" in _ctx_conf and "READ THIS BEFORE DIAGNOSING THE CODE" in _ctx_conf)
        check("...it tells the reviewer to re-compute BEFORE reasoning about the code",
              "Re-compute BEFORE reasoning about the code" in _ctx_conf
              and "old slicer retired -- use mini-swe" in _ctx_conf)
        check("...and names `redundant` as the verdict an already-landed chain should get",
              "redundant" in _ctx_conf)
        check("...while a conflict whose tip has NOT moved stays a live verdict",
              "CACHED" not in build_context(
                  {"source": "E", "kind": "integration", "plan": "p", "reason": "x",
                   "integration": dict(_fconf, onto_tip=_fnow)},
                  {"label": "p", "repo": _R}))
        check("freshness re-check is read-only: main is where the panel found it",
              _fg("rev-parse", "main").stdout.strip() == _fnow
              and _fg("rev-parse", "integrate/p-stage").stdout.strip() == _fstage)
        check("a record with no recorded base is called out as unprovable, not silently ok",
              "NOT RECORDED" in build_context(
                  {"source": "E", "kind": "integration-staged", "plan": "p", "reason": "x",
                   "integration": dict(_frec, base_rev=None, onto_tip=None)},
                  {"label": "p", "repo": _R}))
    finally:
        __import__("shutil").rmtree(_fr, ignore_errors=True)

    # --- (2026-09-24) A HEADLESS REVIEW IS NOT NAGGED ---------------------------------
    # At least 2 of the ~18 integration reviews spawned that night answered "I could not
    # process the handoff panel this session" instead of diagnosing anything. Proving the
    # marker is exported is not enough -- these checks run the real hook scripts BOTH
    # ways against a synthetic HOME, so a guard that stopped biting would fail here.
    check("the headless review exports the diagnosis-only marker",
          review_env()[HEADLESS_MARKER] == "1")
    check("...built as a copy, so this process's own environment is untouched",
          HEADLESS_MARKER not in os.environ)
    _have_jq = subprocess.run(["bash", "-c", "command -v jq"],
                              capture_output=True).returncode == 0
    check("jq is available (every one of these hooks needs it to emit)", _have_jq)
    _hd = next((d for d in (Path(__file__).resolve().parent.parent / "claude-hooks",
                            Path.home() / ".claude" / "hooks")
                if (d / "session-start-handoff-panel.sh").is_file()), None)
    check("the SessionStart hooks are findable (repo copy, else installed copy)",
          _hd is not None)
    if _hd and _have_jq:
        _hh = tempfile.mkdtemp(prefix="esc-hookhome-")
        try:
            os.makedirs(os.path.join(_hh, "bin"))
            # Backlog the handoff-panel hook will consider worth nagging about.
            Path(_hh, "bin", "handoff-emit.py").write_text(
                "import json\n"
                "print(json.dumps({'complete': [{'status': 'done', 'label': 'real-job'}]}))\n")
            # An in-flight job, so the queue-watch hook wants a Monitor armed.
            Path(_hh, "bin", "ollama-queue.py").write_text(
                "print('[running ] abc123 some-label')\n")
            # the hook now counts in-flight jobs with queue-events-deliver.py --inflight
            Path(_hh, "bin", "queue-events-deliver.py").write_text("print(1)\n")
            _esc = Path(_hh, ".ollama-dispatch", "escalations")
            _esc.mkdir(parents=True)
            (_esc / "ESCALATIONS.md").write_text(
                "# escalations\n- [ ] `bg-actions` WHOLE-CHAIN integration gate\n")

            def _hook(script, marker, event="SessionStart"):
                env = dict(os.environ, HOME=_hh)
                env.pop(HEADLESS_MARKER, None)
                if marker:
                    env[HEADLESS_MARKER] = "1"
                cp = subprocess.run(["bash", str(_hd / script)],
                                    input=json.dumps({"hook_event_name": event}),
                                    capture_output=True, text=True, env=env, timeout=60)
                return cp.returncode, (cp.stdout or "").strip()

            for _script, _needle, _why in (
                    ("session-start-handoff-panel.sh", "HANDOFF PANEL",
                     "the panel nudge that actually derailed the reviews"),
                    ("session-start-escalations.sh", "DISPATCH ESCALATIONS",
                     "the open-escalation list, which would contain the item under review"),
                    ("ollama-queue-watch-reminder.sh", "QUEUE WATCHER",
                     "an instruction to arm a Monitor a read-only session cannot arm")):
                _rc_on, _out_on = _hook(_script, marker=False)
                check("REPRO %s nags an ordinary session (%s)" % (_script, _why),
                      _rc_on == 0 and _needle in _out_on)
                _rc_off, _out_off = _hook(_script, marker=True)
                check("...and %s is SILENT once the diagnosis-only marker is set" % _script,
                      _rc_off == 0 and _out_off == "")
        finally:
            __import__("shutil").rmtree(_hh, ignore_errors=True)
    check("a PASSED (landed) record stays silent",
          detect_integration_escalations({"integration": {"status": "passed",
                                                          "landed": "abc"}}, "p") == [])

    # --- source F: a coding job that vanished from the queue -------------------
    _vst = {"slices": {"s1": {"status": "enqueued", "job_id": "gone1"},
                       "s2": {"status": "enqueued", "job_id": "live2"},
                       "s3": {"status": "enqueued", "job_id": "fin3"},
                       "s4": {"status": "pending", "job_id": None}}}
    F = detect_vanished_jobs(_vst, "cc-waitlist-r2", {"live2", "zzz"},
                             lambda j: j == "fin3")
    check("F fires for the ENQUEUED slice whose job is in neither table nor sidecar",
          [e["slice_id"] for e in F] == ["s1"] and F[0]["source"] == "F")
    check("...as a no-review notice keyed by the vanished id",
          F[0]["no_review"] is True and F[0]["signature"] == "vanished:gone1")
    check("...whose reason names the job and the auto-heal",
          "gone1" in F[0]["reason"] and "re-gates" in F[0]["reason"])
    check("F is silent when the queue table is EMPTY (daemon down is not 'vanished')",
          detect_vanished_jobs(_vst, "p", set(), lambda j: False) == [])
    check("F ignores a job with a completion sidecar and a job still in the table",
          not any(e["slice_id"] in ("s2", "s3") for e in F))
    check("a staged-integration notice keys under the plan's integration namespace",
          esc_key({"kind": "integration-staged", "plan": "p", "signature": "integration:staged:abc"})
          .startswith("integration:p:"))

    # --- context file
    ctx = build_context({"source": "A", "plan": "bg-actions-s1-item", "slice_id": "s4",
                         "reason": "2 identical failures",
                         "slice": {"status": "escalated", "intent": "do the thing"}},
                        {"repo": "/tmp/r", "label": "bg"})
    check("context names the plan", "bg-actions-s1-item" in ctx)
    check("context names the slice", "s4" in ctx)
    check("context carries the reason", "2 identical failures" in ctx)
    check("context carries the slice record", "do the thing" in ctx)
    check("context points at the plan state file", "slice-runs" in ctx)

    # --- review prompt guardrails (an unsupervised agent must not act)
    p = REVIEW_PROMPT.format(ctx="/x")
    check("review prompt forbids product edits", "Do NOT edit any product code" in p)
    check("review prompt forbids re-enqueue", "re-enqueue" in p)
    check("review prompt forbids state edits", "slice-plan or queue state" in p)
    check("review prompt asks for a VERDICT line", "VERDICT:" in p)
    check("review tools are read-only",
          all(t in ("Read", "Grep", "Glob") or t.startswith("Bash(git ")
              or "ollama-queue.py results" in t or "ollama-queue.py status" in t
              for t in REVIEW_TOOLS))

    # --- index
    # (tempfile imported at the top of self_test)
    with tempfile.TemporaryDirectory() as td:
        idx = Path(td) / "E.md"
        idx.write_text("- [ ] open one\n- [x] closed one\n")
        check("index lists only unchecked rows", open_escalations(idx) == ["- [ ] open one"])
        check("missing index is empty, not an error", open_escalations(Path(td) / "nope.md") == [])

    # --- plan file filtering
    with tempfile.TemporaryDirectory() as td:
        for n in ("a.json", "a.json.bak-intentfix", "b.advance.log", "c.json"):
            (Path(td) / n).write_text("{}")
        (Path(td) / "archive").mkdir()
        names = [p.name for p in plan_files(td)]
        check("plan scan takes live .json only", names == ["a.json", "c.json"])

    # --- propose-never-apply -------------------------------------------------
    GOOD = ("--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n-old\n+new\n ctx\n")
    check("no diff block -> no proposal", extract_patch("VERDICT: c -- nope") is None)
    check("prose fenced as diff is NOT taken as a patch",
          extract_patch("```diff\nI would change the loader.\n```") is None)
    check("a real unified diff is extracted",
          (extract_patch("VERDICT: d\n```diff\n" + GOOD + "```\n") or "").strip()
          == GOOD.strip())
    check("the LONGEST diff block wins over a quoted fragment",
          "new" in (extract_patch("```diff\n--- a/x\n+++ b/x\n@@ -1 +1 @@\n-a\n```\n"
                                  "```diff\n" + GOOD + "```") or ""))
    ok_v, why = validate_patch(GOOD, None)
    check("a patch with no repo recorded is REFUSED, not written", not ok_v and "repo" in why)

    with tempfile.TemporaryDirectory() as td:
        subprocess.run(["git", "init", "-q", td], check=True)
        (Path(td) / "f.py").write_text("old\nctx\n")
        subprocess.run(["git", "-C", td, "add", "f.py"], check=True)
        out = Path(td) / "p.patch"
        cmd, why = write_proposal(GOOD, td, out)
        check("an applicable diff is validated and written", cmd is not None and out.exists())
        check("the written proposal is labelled NOT APPLIED",
              "NOT APPLIED" in out.read_text() and "apply:" in out.read_text())
        check("the file on disk is UNCHANGED -- proposing never applies",
              (Path(td) / "f.py").read_text() == "old\nctx\n")
        # THE BUG THIS GUARDS: a diff that does not apply must leave NO .patch
        # file. A .patch beside an escalation is an implicit promise that the
        # apply command next to it works; one that fails on first use is worse
        # than no proposal at all.
        bad = Path(td) / "bad.patch"
        cmd2, why2 = write_proposal(
            "--- a/f.py\n+++ b/f.py\n@@ -1,2 +1,2 @@\n-NOT-THE-CONTENT\n+new\n ctx\n",
            td, bad)
        check("a diff that does NOT apply is refused and written nowhere",
              cmd2 is None and not bad.exists() and "REJECTED" in why2)
        # A model-written diff whose @@ counts are WRONG but whose content is
        # right (live: sidecar-bfmr s2 "corrupt patch at line 22") is accepted,
        # and the emitted apply command carries --recount so it works too.
        mis = Path(td) / "mis.patch"
        cmd3, why3 = write_proposal(
            "--- a/f.py\n+++ b/f.py\n@@ -1,5 +1,9 @@\n-old\n+new\n ctx\n", td, mis)
        check("a diff with miscounted @@ line counts is accepted (--recount): " + why3,
              cmd3 is not None and "--recount" in cmd3)

    # --- self-heal hand-off (2026-09-26)
    _calls = []

    class _R2:
        returncode, stderr = 0, ""
        stdout = "# self-heal p/s1: patch+regate -- applied\nregate\n"

    def _fake(argv, **kw):
        _calls.append(argv)
        return _R2()
    _ea = {"source": "A", "kind": "slice", "plan": "p", "slice_id": "s1"}
    with tempfile.TemporaryDirectory() as _pd:
        for _i in range(9):
            (Path(_pd) / ("2026100%dT000000Z-plan-s1.md" % (_i + 1))).write_text("x")
            (Path(_pd) / ("2026100%dT000000Z-plan-s1.review.md" % (_i + 1))).write_text("x")
        _removed = prune_escalations(_pd, keep=3, protect=set())
        check("retention cap keeps the newest 3 sets per slug, removes the rest",
              _removed == 12 and len(list(Path(_pd).iterdir())) == 6)
    with tempfile.TemporaryDirectory() as _pd:
        for _i in range(9):
            (Path(_pd) / ("2026100%dT000000Z-job-x.md" % (_i + 1))).write_text("x")
            (Path(_pd) / ("2026100%dT000000Z-job-x.local-review.task.md" % (_i + 1))).write_text("x")
        _live = str(Path(_pd) / "20261001T000000Z-job-x.local-review.task.md")
        _removed = prune_escalations(_pd, keep=3, protect={_live})
        check("retention cap never prunes a set a PENDING review row still reads",
              Path(_live).exists() and _removed == 10)
    check("esc_key is stable across volatile ids/counts in the reason",
          esc_key({"kind": "slice", "plan": "p", "slice_id": "s", "signature": "job abc123def456 failed 3 times at 12:01"})
          == esc_key({"kind": "slice", "plan": "p", "slice_id": "s", "signature": "job 999888777666 failed 7 times at 14:55"}))
    check("a source-A slice escalation is handed to self-heal",
          self_heal(_ea, "/c.md", "/r.md", run=_fake, tool=__file__) == "regate"
          and "--slice" in _calls[0] and "s1" in _calls[0])
    _calls.clear()
    check("a job / integration escalation is NOT self-healed",
          self_heal({"source": "C", "kind": "job", "job_id": "j"}, "/c", "/r",
                    run=_fake, tool=__file__) is None and not _calls)
    check("a missing self-heal tool is a quiet no-op",
          self_heal(_ea, "/c", "/r", run=_fake, tool="/no/such/tool") is None)

    def _boom(*a, **k):
        raise RuntimeError("x")
    check("a crashing self-heal is contained",
          str(self_heal(_ea, "/c", "/r", run=_boom, tool=__file__)).startswith("error"))

    src = Path(__file__).read_text()
    check("the review agent is never granted write tools",
          not any(t.split("(")[0] in ("Edit", "Write", "NotebookEdit") for t in REVIEW_TOOLS))
    # Every git subprocess this module runs, as a list of argv tokens. The
    # guarantee is structural, not stylistic: a future edit that adds a real
    # `git apply` / `git commit` / `git add` anywhere in the module body fails
    # this check, so "propose, never apply" cannot be quietly walked back.
    body = src.split("def self_test")[0]
    argvs = re.findall(r'subprocess\.run\(\s*\[([^\]]*)\]', body, re.S)
    gits = [re.findall(r'"([^"]+)"', a) for a in argvs if '"git"' in a]
    check("the only direct git argv is `git apply --check`, plus the read-only _git helper",
          gits and all(g[:3] == ["git", "apply", "--check"] or g[:2] == ["git", "-C"]
                       for g in gits))
    check("nothing here commits, stages or applies for real",
          not any(t in ("commit", "add", "checkout", "reset", "push")
                  for g in gits for t in g))
    # _git() takes its subcommand as an ARGUMENT, so the argv scan above cannot see
    # what it runs. Scan the call sites instead, against a read-only allowlist. Added
    # 2026-09-24 with the integration freshness re-check: that re-check reads the
    # TARGET repo (rev-parse / merge-base / rev-list / diff) while building a panel, so
    # "this watcher never writes a repo it is reporting on" has to stay structural --
    # a panel builder that mutated the repo under review would be far worse than the
    # stale verdict it was added to catch.
    _GIT_READONLY = {"rev-parse", "merge-base", "rev-list", "diff", "cat-file",
                     "log", "show", "status", "symbolic-ref", "name-rev"}
    _subs = re.findall(r'_git\(\s*[^,()]+,\s*"([a-z][a-z-]*)"', body)
    check("the freshness re-check only ever runs READ-ONLY git subcommands",
          _subs and set(_subs) <= _GIT_READONLY)

    # --- NUMBERED RERUNS WITH A VISIBLE CAUSE (the owner 2026-10-01) --------------
    # Grounded in the real chain off ~/bin/ollama-queue-state.json:
    # dbcf30f84454 -> d96a71500b9b -> f059db0bce62 (bundle sidecar-bfmr-login-nudge).
    # THE BUG: these two reasons share a masked esc_key, so the second was silently
    # swallowed even though the failure count TRIPLED.
    _SIG_A = ("verify failed: 2 new failure(s) attributable to this diff "
              "(job d96a71500b9b) at 12:01")
    _SIG_B = ("verify failed: 9 new failure(s) attributable to this diff "
              "(job f059db0bce62) at 14:55")
    _SIG_NOISE = ("verify failed: 2 new failure(s) attributable to this diff "
                  "(job f059db0bce62) at 15:30")
    check("the masked key still collapses the whole family (no spam)",
          esc_key({"kind": "slice", "plan": "p", "slice_id": "s", "signature": _SIG_A})
          == esc_key({"kind": "slice", "plan": "p", "slice_id": "s", "signature": _SIG_B})
          == esc_key({"kind": "slice", "plan": "p", "slice_id": "s",
                      "signature": _SIG_NOISE}))
    _dn = sig_delta(_SIG_A, _SIG_NOISE)
    check("a job id + clock change is NOISE ONLY, never a worsening",
          _dn["noise_only"] and not _dn["worse"] and len(_dn["changed"]) == 2)
    _dw = sig_delta(_SIG_A, _SIG_B)
    check("2 -> 9 new failures IS read as a worsening, with the tokens diffed",
          _dw["worse"] and not _dw["noise_only"] and "2 -> 9" in _dw["summary"])
    check("a count going DOWN is not a worsening",
          not sig_delta(_SIG_B, _SIG_A)["worse"])
    check("a number with no badness context is not a worsening",
          not sig_delta("stopped at iteration 5 of 24",
                        "stopped at iteration 9 of 24")["worse"])
    check("un-alignable wording is never read as a worsening (conservative)",
          not sig_delta("2 new failures", "9 new failures in 3 files")["worse"])
    # rerun_assess: the ledger-level decision.
    _e_noise = {"kind": "slice", "plan": "p", "slice_id": "s", "job_id": "f059db0bce62",
                "signature": _SIG_NOISE, "reason": _SIG_NOISE}
    _e_worse = {"kind": "slice", "plan": "p", "slice_id": "s", "job_id": "f059db0bce62",
                "signature": _SIG_B, "reason": _SIG_B}
    _rec0 = {"raw_signature": _SIG_A, "escalation_n": 1,
             "rerun_job_id": "d96a71500b9b", "rerun_class": outcome_class(_e_worse)}
    _rr_n, _re_n = rerun_assess(_rec0, _e_noise)
    check("NOISE-ONLY: not re-announced, and not even counted as a rerun",
          _re_n is False and _rr_n["noise"] and _rr_n["n"] == 1 and not _rr_n["note"])
    check("NOISE-ONLY: the newer raw signature is still remembered",
          _rr_n["raw"] == _SIG_NOISE)
    _rr_w, _re_w = rerun_assess(_rec0, _e_worse)
    check("WORSENING: re-announced as escalation #2 with a 'what changed' line",
          _re_w is True and _rr_w["n"] == 2 and _rr_w["worse"]
          and "what changed" in _rr_w["note"] and "2 -> 9" in _rr_w["note"]
          and "WORSENING" in _rr_w["note"])
    check("WORSENING: the note names the triggering job and the one it replaced",
          "f059db0bce62" in _rr_w["note"] and "d96a71500b9b" in _rr_w["note"])
    check("an unchanged raw signature is not a rerun at all",
          rerun_assess({"raw_signature": _SIG_B, "escalation_n": 2}, _e_worse)
          == (None, False))
    check("a new job with a DIFFERENT outcome class is re-announced",
          rerun_assess({"raw_signature": "verify failed: 2 new failure(s) (job "
                                         "d96a71500b9b)", "escalation_n": 1,
                        "rerun_job_id": "d96a71500b9b",
                        "rerun_class": "iteration-cap"},
                       {"kind": "slice", "plan": "p", "slice_id": "s",
                        "job_id": "f059db0bce62",
                        "signature": "verify failed: 2 new failure(s) (job "
                                     "f059db0bce62)"})[1] is True)
    check("...but the SAME job re-reporting the same class is not",
          rerun_assess({"raw_signature": "verify failed: 2 new failure(s) (job "
                                         "d96a71500b9b)", "escalation_n": 1,
                        "rerun_job_id": "f059db0bce62",
                        "rerun_class": "verify-failed"},
                       {"kind": "slice", "plan": "p", "slice_id": "s",
                        "job_id": "f059db0bce62",
                        "signature": "verify failed: 2 new failure(s) (job "
                                     "f059db0bce62)"})[1] is False)
    # A DIFFERENT SHAPE is a different masked key -- still a plain new escalation,
    # untouched by any of this.
    check("a different-shape reason is still a NEW escalation (unchanged behaviour)",
          len(new_escalations(
              [{"kind": "slice", "plan": "p", "slice_id": "s", "signature": _SIG_A},
               {"kind": "slice", "plan": "p", "slice_id": "s",
                "signature": "the slice worktree vanished"}],
              {"announced": [esc_key({"kind": "slice", "plan": "p", "slice_id": "s",
                                      "signature": _SIG_A})]})) == 1)
    # track_reruns, end to end against a real ledger file.
    with tempfile.TemporaryDirectory() as _td:
        _lp = Path(_td) / "ledger.json"
        _k = esc_key(_e_worse)
        _led = {"announced": [_k], "records": {_k: dict(_rec0)}}
        _lp.write_text(json.dumps(_led))
        _led2, _rev = track_reruns([dict(_e_noise)], _led, _lp)
        check("track_reruns: a noise-only pass stays silent and stays announced",
              not _rev and _k in (_led2.get("announced") or [])
              and _led2["records"][_k]["escalation_n"] == 1
              and _led2["records"][_k]["raw_signature"] == _SIG_NOISE)
        _e2 = dict(_e_worse)
        _led3, _rev2 = track_reruns([_e2], _led2, _lp)
        check("track_reruns: a worsening un-announces the key so it is re-reviewed",
              len(_rev2) == 1 and _k not in (_led3.get("announced") or []))
        check("track_reruns: the revived escalation carries its number + cause",
              _rev2[0] is _e2 and _e2["rerun"]["n"] == 2
              and "2 -> 9" in _e2["rerun"]["note"])
        check("track_reruns: an escalation that was never announced is left alone",
              track_reruns([{"kind": "slice", "plan": "p", "slice_id": "zz",
                             "signature": _SIG_A}], _led3, _lp)[1] == [])
        # --dry-run must not consume the change it is previewing.
        _dlp = Path(_td) / "dry.json"
        _dled = {"announced": [_k], "records": {_k: dict(_rec0)}}
        _dlp.write_text(json.dumps(_dled))
        _, _drev = track_reruns([dict(_e_worse)], _dled, _dlp, persist=False)
        check("track_reruns: persist=False writes NOTHING but still reports the rerun",
              len(_drev) == 1 and _drev[0]["rerun"]["n"] == 2
              and json.loads(_dlp.read_text()) == _dled)
        # The CAUSE is at the TOP of the context file, and on the index row.
        _ctx = build_context(_e2, None)
        _first = [l for l in _ctx.splitlines()[1:] if l.strip()][0]
        check("the rerun cause is the FIRST thing in the context file after the title",
              _first.startswith("> ↻ ESCALATION #2") and "what changed" in _first)
        # "previous" is the LAST SEEN raw reason, which the noise pass above already
        # advanced to _SIG_NOISE -- that is the point of remembering it on a noise-only
        # change, so the diff a human reads is against reality, not against #1.
        check("the context file shows the old reason AND the new one",
              "previous reason: " + _SIG_NOISE in _ctx
              and "this reason:     " + _SIG_B in _ctx)
        _idx = Path(_td) / "ESCALATIONS.md"
        append_index(_e2, "/c.md", "VERDICT: b -- model defect", _idx)
        _row = _idx.read_text()
        check("the index row carries the #N badge and the cause",
              "↻ #2 VERDICT: b" in _row and "2 -> 9" in _row)
        append_index({"kind": "slice", "plan": "p", "slice_id": "s"},
                     "/c.md", "VERDICT: b -- model defect", _idx)
        check("a first-time escalation's row is UNCHANGED (no badge, no cause)",
              "↻" not in _idx.read_text().splitlines()[1])
        check("a first-time escalation gets no banner either",
              rerun_banner({"kind": "slice"}) == []
              and rerun_banner({"rerun": {"n": 1}}) == [])

    print("%d checks passed" % ok)
    print("SELF_TEST_OK" if not self_test.failed else "SELF_TEST_FAILED")
    return 0 if not self_test.failed else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args()
    if a.self_test:
        return self_test()
    rc = run_once(dry_run=a.dry_run)
    stall_pass(dry_run=a.dry_run)
    return rc


STALL_DETECT = Path(__file__).resolve().parent / "dispatch-stall-detect.py"


def stall_pass(dry_run=False, run=subprocess.run, tool=None):
    """Mandate item 3 (2026-10-02): every pass, dispatch-stall-detect.py acts on a
    plan idle >30 min with the lane idle, or alerts ONCE. Contained: a broken stall
    pass never breaks the watcher pass; the writes happen in that tool."""
    tool = Path(tool) if tool else STALL_DETECT
    if not tool.exists():
        return None
    try:
        r = run([sys.executable, str(tool)] + (["--dry-run"] if dry_run else []),
                capture_output=True, text=True, timeout=2700)
        for l in (r.stdout or "").splitlines():
            if l.strip():
                print("  " + l)
        return r.returncode
    except Exception as exc:
        print("  # stall-detect failed: %s" % exc)
        return None


if __name__ == "__main__":
    sys.exit(main())
