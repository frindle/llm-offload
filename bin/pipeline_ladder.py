#!/usr/bin/env python3
"""pipeline_ladder.py -- the TIER LADDER that replaces "park for a human" (2026-10-12).

The operator: the dispatch pipeline runs UNATTENDED, LLM-only. A needs_opus row used to end at
`_park_final` ("parking for human") or at self-heal's `skip:not an auto-author job`. This
module is the escalation path every such row now walks, with a NAMED exit and an ACTION
at every rung -- never a human.

    tier 1  local reviewer      the watcher's review verdict (a/b/c/d), used only if GROUNDED
    tier 2  Sonnet headless     `claude -p --model sonnet` adjudicates a row tier 1 could not
                                (no verdict / UNVERIFIED / capped). Its findings are GROUNDED
                                too: file:line + a quote a script finds in the file or in the
                                job's diff; ungrounded findings are DROPPED.
    tier 3  Opus                ONLY for a pipeline-TOOLING root cause (verdict "t"). Spend is
                                capped; unless LADDER_OPUS=on it writes a tooling request
                                (tooling-requests.jsonl) for the coordinator agent instead.

Actions (executed, not suggested), each recorded in the ledger + decisions.jsonl and the
escalation index rows are ticked with the action by the caller (close_index_rows):
    retired:superseded     a NEWER attempt of the same task family exists (its owner moved on)
    closed:satisfied       verdict a, proven by the job's own verify (or grounded evidence)
    heal-continuation:ID   verdict b/c/d: one more round with the GROUNDED findings attached
                           (d -> the bigger model)
    respec:...             rounds spent: relaunch from the recorded dispatch-auto argv
    ladder-exit:REASON     nothing left to try: a NAMED reason (EXIT_REASONS), job resolved,
                           recorded in ledger["ladder"]["terminal"] -- the row is closed with
                           the reason, never left parked
Never touched: awaiting_signoff / user_hold / hand-locked worktrees, rows named in
ladder-protect.json (an agent is working them), and the NEWEST attempt of a family whose
launching agent session is still alive (that agent owns the next step).

Pure functions + injectable hooks so the test drives every rung hermetically.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import sys
import time
from pathlib import Path

HOME = Path.home()
DISPATCH_DIR = HOME / ".ollama-dispatch"
ESC_DIR = DISPATCH_DIR / "escalations"
HEAL_LEDGER = ESC_DIR / "self-heal.json"
PROTECT_FILE = ESC_DIR / "ladder-protect.json"
TOOLING_REQUESTS = ESC_DIR / "tooling-requests.jsonl"
QUEUE_PY = Path(__file__).resolve().parent / "ollama-queue.py"

MIN_AGE_S = 10 * 60            # a fresh row is left to the watcher's review + heal first
SONNET_MAX_PER_JOB = 2
CONTINUE_MAX_PER_FAMILY = 2
RESPEC_MAX_PER_FAMILY = 1
ENQUEUE_MAX_PER_DAY = 4
SONNET_MAX_PER_DAY = 12
OPUS_MAX_PER_DAY = 1

EXIT_REASONS = (
    "superseded",               # a newer attempt of the same family exists
    "satisfied",                # already satisfied (verified)
    "no-runnable-spec",         # nothing to continue/re-spec mechanically (no verify / argv)
    "ungrounded-diagnosis",     # tier 1 and tier 2 both produced no grounded finding
    "tooling-root-cause",       # a pipeline-tooling defect: handed to tier 3
    "ladder-budget-spent",      # continuation + re-spec budgets spent
)

_FAMILY_TS = re.compile(r"-\d{6}$")
_FAMILY_SUFFIX = re.compile(r"(?:\s*\[auto-fix r\d+\]|-rs\d+|-c\d+|-esc)+$")
_FAMILY_DIGITS = re.compile(r"(?<=[A-Za-z])\d+$")


def _load(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _save(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp%d" % os.getpid())
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, p)


def _utc(ts=None):
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts) if ts else time.gmtime())


def _utc_day(ts=None):
    return time.strftime("%Y-%m-%d", time.gmtime(ts) if ts else time.gmtime())


def _parse_ts(s):
    if isinstance(s, (int, float)):
        return float(s)
    try:
        from datetime import datetime
        return datetime.fromisoformat(str(s).replace("Z", "+00:00")).timestamp()
    except (ValueError, TypeError):
        return 0.0


# --------------------------------------------------------------------------- families
def job_family(label):
    """PURE. One key per TASK, shared by its attempts: `qagent-rt-sidecar-nl-numlock4-152438`
    and `qagent-rt-sidecar-nl-numlock2-121335` are both `rt-sidecar-nl-numlock`;
    `amazon [auto-fix r2]` is `amazon`. Distinct tasks (s1a-lib vs s1c-lq) stay distinct."""
    s = str(label or "")
    s = re.sub(r"^qagent-", "", s)
    s = _FAMILY_SUFFIX.sub("", s)
    s = _FAMILY_TS.sub("", s)
    s = _FAMILY_DIGITS.sub("", s)
    return s


def _enq(j):
    return _parse_ts(j.get("enqueued_at") or j.get("created_at") or 0)


def newer_attempt(job, jobs):
    """PURE. The newest row of the same family (and bundle-less / same-repo kind) that was
    enqueued AFTER `job`, else None. The caller of an older attempt launched the newer one:
    the older row is dead weight."""
    fam = job_family(job.get("label"))
    if not fam:
        return None
    mine = _enq(job)
    best = None
    for o in jobs or []:
        if o.get("id") == job.get("id") or job_family(o.get("label")) != fam:
            continue
        # an auto-fix round and its parent share a label family; only SAME-KIND rows count
        if _label_kind(o.get("label")) != _label_kind(job.get("label")):
            continue
        if _enq(o) > mine and (best is None or _enq(o) > _enq(best)):
            best = o
    return best


def _label_kind(label):
    s = str(label or "")
    if s.startswith("qagent-"):
        return "qagent"
    if "[auto-fix" in s:
        return "autofix"
    if s.startswith("auto-author-"):
        return "author"
    return "other"


# --------------------------------------------------------------------------- protection
def socket_alive(path, timeout=0.5):
    """Is the launching agent session's socket still accepting connections?"""
    if not path or not os.path.exists(path):
        return False
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except OSError:
            pass


def protected_by_file(job, now, path=None):
    """A row an agent has explicitly claimed: ladder-protect.json
    {"claims": [{"match": "arr-webhook", "until": "<iso>", "by": "a5040d", "why": ".."}]}.
    `match` is a substring of the label or bundle. Expired claims are ignored."""
    cfg = _load(path or PROTECT_FILE, {}) or {}
    hay = "%s %s" % (job.get("label") or "", job.get("bundle") or "")
    for c in cfg.get("claims") or []:
        m = str(c.get("match") or "")
        if not m or m not in hay:
            continue
        if c.get("until") and _parse_ts(c["until"]) < now:
            continue
        return "claimed by %s: %s" % (c.get("by") or "an agent", c.get("why") or m)
    return None


def protect_reason(job, jobs, now, alive=socket_alive, protect_path=None, hand_lock=".hand-harness",
                   min_age=MIN_AGE_S):
    """PURE-ish. Why the ladder must NOT act on this row right now, else None."""
    if job.get("awaiting_signoff"):
        return "awaiting_signoff"
    if job.get("user_hold"):
        return "user_hold"
    cwd = job.get("cwd")
    if cwd and Path(cwd, hand_lock).exists():
        return "worktree hand-locked"
    c = protected_by_file(job, now, protect_path)
    if c:
        return c
    esc_at = _parse_ts((job.get("escalation") or {}).get("escalated_at"))
    if esc_at and now - esc_at < min_age:
        return "escalated %ds ago: the watcher's review + heal go first" % (now - esc_at)
    for o in jobs or []:
        if o.get("id") != job.get("id") and o.get("status") in ("running", "pending", "paused", "queued") \
                and cwd and o.get("cwd") == cwd:
            return "live job %s uses this worktree" % o.get("id")
    # the launching agent session still alive owns the NEWEST attempt of its family
    sock = job.get("launched_by")
    if sock and newer_attempt(job, jobs) is None and alive(str(sock)):
        return "owned by the live agent session %s (newest attempt of its task)" % sock
    return None


# --------------------------------------------------------------------------- grounding
def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def ground_findings(findings, roots, diff_text="", verify_claim=None):
    """-> (kept, dropped). A finding {file, line, quote} is KEPT only when a script finds the
    quote at that file:line (+-2) in a file under `roots`, or verbatim in the job's diff.
    Everything else is DROPPED with a reason: an LLM's claim is a hypothesis until a script
    has seen the text (the hasLink/hashLink class of hallucination)."""
    if verify_claim is None:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import escalation_grounding as eg
        verify_claim = eg.verify_claim
    nd = _norm(diff_text)
    kept, dropped = [], []
    for f in findings or []:
        if not isinstance(f, dict):
            dropped.append({"finding": f, "code": "malformed"})
            continue
        try:
            ok, code = verify_claim(f, roots)
        except Exception:
            ok, code = False, "error"
        if not ok:
            q = _norm(f.get("quote"))
            if q and len(q.replace(" ", "")) >= 8 and q in nd:
                ok, code = True, "ok-in-diff"
        (kept if ok else dropped).append(dict(f, code=code))
    return kept, dropped


# --------------------------------------------------------------------------- tier 2
def sonnet_call(prompt, timeout=420):
    env = dict(os.environ, CLAUDE_HEADLESS_DIAGNOSIS="1")
    r = subprocess.run(["claude", "-p", "--model", "sonnet", "--tools", "", "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
    return (r.stdout or "") + ("" if r.returncode == 0 else "\n[rc=%s] %s" % (r.returncode, (r.stderr or "")[-200:]))


def opus_call(prompt, timeout=900):
    env = dict(os.environ, CLAUDE_HEADLESS_DIAGNOSIS="1")
    r = subprocess.run(["claude", "-p", "--model", "opus", "--tools", "", "--no-session-persistence"],
                       input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
    return r.stdout or ""


_REFUSAL = re.compile(r"(usage limit|rate limit|session limit|limit reached|try again|overloaded|"
                      r"credit|not logged in|invalid api key|\[rc=)", re.I)


def is_refusal(text):
    t = (text or "").strip()
    return (not t) or (len(t) < 400 and bool(_REFUSAL.search(t)))


def parse_json_block(text):
    """PURE. The first balanced {...} object in `text` that parses, else None."""
    t = text or ""
    if "</think>" in t:
        t = t.rsplit("</think>", 1)[1]
    n = len(t)
    for i in range(n):
        if t[i] != "{":
            continue
        depth, instr, esc = 0, False, False
        for j in range(i, n):
            c = t[j]
            if instr:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    instr = False
                continue
            if c == '"':
                instr = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    try:
                        obj = json.loads(t[i:j + 1])
                        if isinstance(obj, dict):
                            return obj
                    except ValueError:
                        pass
                    break
    return None


def job_context(job, diff_runner=None, max_diff=6000):
    """The evidence handed to tier 2: task text, escalation reason, repair history, the
    worktree's own diff and the log tail. Bounded."""
    esc = job.get("escalation") or {}
    parts = ["JOB %s label=%s bundle=%s model=%s exit=%s" % (
        job.get("id"), job.get("label"), job.get("bundle"), job.get("model"), job.get("exit_code")),
        "ESCALATION REASON: %s" % str(esc.get("reason") or "")[:1800],
        "FAILURE: class=%s detail=%s terminal_reason=%s" % (
            job.get("failure_class"), str(job.get("failure_detail"))[:300], job.get("terminal_reason"))]
    tf = job.get("task_file")
    if tf:
        try:
            parts.append("TASK (head):\n" + Path(tf).read_text(errors="replace")[:3000])
        except OSError:
            pass
    diff = ""
    cwd = job.get("cwd")
    if cwd and Path(cwd).is_dir():
        try:
            run = diff_runner or (lambda: subprocess.run(
                ["git", "-C", cwd, "diff", "HEAD"], capture_output=True, text=True, timeout=60).stdout)
            diff = run() or ""
        except Exception:
            diff = ""
    if diff:
        parts.append("WORKTREE DIFF (head):\n" + diff[:max_diff])
    lp = job.get("log_path")
    if lp:
        try:
            parts.append("LOG TAIL:\n" + Path(lp).read_text(errors="replace")[-1500:])
        except OSError:
            pass
    return "\n\n".join(parts), diff


ADJ_PROMPT = """You are the tier-2 adjudicator of an unattended local-LLM coding pipeline. A job
failed and escalated; nobody will read your answer -- a script acts on it. Decide WHY and WHAT
TO DO, from the evidence below ONLY.

verdict codes:
  a  the work is already satisfied (the target behaviour exists / the verify would pass)
  b  the SPEC/TASK is wrong or unsatisfiable (names a file/literal that does not exist, a frozen
     test cannot cover a required new path, scope forbids a file the task needs)
  c  the HARNESS/fixture/verify is defective (mocks the wrong thing, asserts the wrong value)
  d  the MODEL is incapable (valid spec and harness, repeated genuine failure)
  t  a bug in the PIPELINE TOOLING itself (queue, worker, qagent, scaffold, gate) not this task

GROUNDING (mandatory): every claim must be a finding {"file": path, "line": N, "quote": "text
copied VERBATIM from that file or from the diff"}. A script verifies each quote; findings it
cannot find are DROPPED, and a verdict with no surviving finding is discarded.

Answer with ONE JSON object and nothing else:
{"verdict": "a|b|c|d|t", "why": "<=3 sentences", "findings": [{"file": "", "line": 0, "quote": ""}],
 "fix_notes": "concrete instructions for the next author round (b/c/d), else empty"}

EVIDENCE:
%s
"""


def adjudicate(job, review_text="", sonnet=None, roots=None, verify_claim=None, diff_runner=None):
    """Tier 2. -> dict {ok, verdict, why, findings(kept), dropped, fix_notes, tier:'sonnet',
    raw} or {ok: False, unavailable: True} when the call was refused / empty (the caller waits;
    a refusal never spends a rung)."""
    ctx, diff = job_context(job, diff_runner)
    if review_text:
        ctx += "\n\nLOCAL REVIEWER (tier 1) said (UNVERIFIED unless it cites file:line + quote):\n" + review_text[-2500:]
    out = (sonnet or sonnet_call)(ADJ_PROMPT % ctx[:14000])
    if is_refusal(out):
        return {"ok": False, "unavailable": True, "raw": (out or "")[:200], "tier": "sonnet"}
    obj = parse_json_block(out)
    if not obj or str(obj.get("verdict", "")).lower() not in ("a", "b", "c", "d", "t"):
        return {"ok": False, "unavailable": False, "raw": (out or "")[:200], "tier": "sonnet",
                "malformed": True}
    rts = list(roots or [])
    if job.get("cwd"):
        rts.insert(0, str(job["cwd"]))
    rts.append(str(Path.home() / "bin"))
    kept, dropped = ground_findings(obj.get("findings") or [], rts, diff, verify_claim)
    return {"ok": True, "verdict": str(obj["verdict"]).lower(), "why": str(obj.get("why") or "")[:600],
            "findings": kept, "dropped": dropped, "fix_notes": str(obj.get("fix_notes") or "")[:1500],
            "tier": "sonnet", "raw": (out or "")[:200]}


def grounded_verdict(adj):
    """PURE. The verdict a script may act on: the adjudication's own when at least one finding
    survived grounding, else 'unverified'."""
    if not adj or not adj.get("ok"):
        return "unverified"
    if adj["verdict"] in ("b", "c", "d", "t", "a") and not adj.get("findings"):
        return "unverified"
    return adj["verdict"]


# --------------------------------------------------------------------------- ledger
def _ledger(path):
    led = _load(path, {}) or {}
    return led, led.setdefault("ladder", {"jobs": {}, "log": [], "terminal": []})


def _record(path, jid, **kw):
    led, lad = _ledger(path)
    rec = lad["jobs"].setdefault(jid, {})
    rec.update(kw)
    rec["updated_at"] = _utc()
    _save(path, led)
    return rec


def _count_family(lad, fam, kind):
    return sum(1 for e in lad.get("log") or [] if e.get("family") == fam and e.get("kind") == kind)


def _count_day(lad, kind, now):
    d = _utc_day(now)
    return sum(1 for e in lad.get("log") or [] if e.get("day") == d and e.get("kind") == kind)


def _log(path, job, kind, detail, now, **extra):
    led, lad = _ledger(path)
    lad["log"] = (lad.get("log") or [])[-400:] + [dict({"day": _utc_day(now), "at": _utc(now), "job": job.get("id"),
                                                       "family": job_family(job.get("label")), "kind": kind,
                                                       "detail": str(detail)[:400]}, **extra)]
    _save(path, led)


# --------------------------------------------------------------------------- the ladder
def exit_job(job, reason, why, hooks, ledger_path, now):
    """The named exit: resolve the row, record the reason (+ why) in the ledger's `terminal`
    list. NOT a park -- the row is closed WITH the reason, and any re-spec has been tried."""
    assert reason in EXIT_REASONS, reason
    led, lad = _ledger(ledger_path)
    lad.setdefault("terminal", []).append({"job": job.get("id"), "label": job.get("label"),
                                           "family": job_family(job.get("label")), "reason": reason,
                                           "why": str(why)[:500], "at": _utc(now)})
    lad["terminal"] = lad["terminal"][-200:]
    lad["jobs"].setdefault(job.get("id"), {}).update({"exit": reason, "exit_why": str(why)[:500],
                                                      "updated_at": _utc(now)})
    _save(ledger_path, led)
    try:
        hooks["resolve"](job.get("id"))
    except Exception:
        pass
    return "ladder-exit:%s -- %s" % (reason, " ".join(str(why).split())[:200])


def write_tooling_request(job, adj, path=None, now=None):
    p = Path(path or TOOLING_REQUESTS)
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a") as fh:
        fh.write(json.dumps({"at": _utc(now), "job": job.get("id"), "label": job.get("label"),
                             "why": adj.get("why"), "findings": adj.get("findings"),
                             "status": "open"}) + "\n")


def continuation_task(job, adj, review_text, esc_dir):
    """The next round's TASK = the job's own task + the GROUNDED findings (never the raw
    ungrounded review)."""
    try:
        prev = Path(str(job.get("task_file"))).read_text(errors="replace")
    except OSError:
        prev = ""
    fnd = "\n".join("- %s:%s `%s`" % (f.get("file"), f.get("line"), str(f.get("quote"))[:200])
                    for f in (adj.get("findings") or []))
    notes = ("## LADDER FINDINGS -- fix these FIRST (tier=%s, each verified against the files)\n"
             "Diagnosis: %s\n%s\n\n%s\n\n---\n" % (adj.get("tier"), adj.get("why"), fnd, adj.get("fix_notes") or ""))
    p = Path(esc_dir) / "ladder" / ("%s-TASK.txt" % job.get("id"))
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(notes + prev)
    return str(p)


def process_job(job, jobs, hooks, ledger_path=None, now=None, review_text="", cfg=None):
    """Walk ONE needs_opus row down the ladder. Returns one word-string:
    'protected:..' | 'wait:..' | 'retired:..' | 'closed:..' | 'heal-continuation:ID' |
    'respec:..' | 'ladder-exit:REASON ..'. All side effects go through `hooks`:
      enqueue(argv)->(id,out)  resolve(jid)->bool  respec(job,review,why,reslice)->str
      sonnet(prompt)->str  opus(prompt)->str  run_verify(job)->(rc,tail)  alive(path)->bool
      local_verdict(job,review)->(verdict|None, grounded: bool, findings)  esc_dir"""
    ledger_path = ledger_path or HEAL_LEDGER
    now = time.time() if now is None else now
    cfg = cfg or {}
    jid = job.get("id")
    why = protect_reason(job, jobs, now, hooks.get("alive", socket_alive), hooks.get("protect_path"),
                         min_age=cfg.get("min_age", MIN_AGE_S))
    if why:
        return "protected:%s" % why
    fam = job_family(job.get("label"))
    nw = newer_attempt(job, jobs)
    if nw is not None:
        try:
            hooks["resolve"](jid)
        except Exception:
            pass
        _log(ledger_path, job, "retire", "superseded by %s" % nw.get("id"), now)
        _record(ledger_path, jid, exit="superseded", superseded_by=nw.get("id"))
        return "retired:superseded by newer attempt %s (%s)" % (nw.get("id"), nw.get("status"))
    led, lad = _ledger(ledger_path)
    rec = lad["jobs"].get(jid) or {}

    # ---- tier 1: the local reviewer, only when its verdict is grounded
    verdict, source, findings, adj = None, "local", [], None
    lv = hooks["local_verdict"](job, review_text) if hooks.get("local_verdict") else (None, False, [])
    if lv and lv[0] in ("a", "b", "c", "d") and lv[1]:
        verdict, findings = lv[0], list(lv[2] or [])
        adj = {"ok": True, "verdict": verdict, "findings": findings, "why": "local reviewer (grounded)",
               "fix_notes": "", "tier": "local"}
    # ---- tier 2: Sonnet for everything tier 1 could not ground
    if verdict is None:
        if int(rec.get("sonnet_calls") or 0) >= SONNET_MAX_PER_JOB:
            return exit_job(job, "ungrounded-diagnosis",
                            "local reviewer and %d Sonnet adjudications produced no grounded finding"
                            % SONNET_MAX_PER_JOB, hooks, ledger_path, now)
        if _count_day(lad, "sonnet", now) >= SONNET_MAX_PER_DAY:
            return "wait:Sonnet daily cap %d reached" % SONNET_MAX_PER_DAY
        adj = adjudicate(job, review_text, hooks.get("sonnet"), hooks.get("roots"),
                         hooks.get("verify_claim"), hooks.get("diff_runner"))
        if adj.get("unavailable"):
            return "wait:Sonnet unavailable (%s) -- the rung is not spent" % adj.get("raw", "")[:80]
        _log(ledger_path, job, "sonnet", "verdict=%s kept=%d dropped=%d" % (
            adj.get("verdict"), len(adj.get("findings") or []), len(adj.get("dropped") or [])), now)
        _record(ledger_path, jid, sonnet_calls=int(rec.get("sonnet_calls") or 0) + 1,
                last_adjudication={k: adj.get(k) for k in ("verdict", "why", "tier")} if adj.get("ok") else {"malformed": True})
        verdict = grounded_verdict(adj)
        findings = adj.get("findings") or []
        source = "sonnet"
        if verdict == "unverified":
            if int(rec.get("sonnet_calls") or 0) + 1 < SONNET_MAX_PER_JOB:
                return "wait:Sonnet verdict UNVERIFIED (0 grounded finding) -- not acted on; one more adjudication allowed"
            return exit_job(job, "ungrounded-diagnosis",
                            "tier 2 verdict %s had no grounded finding after %d calls (%d dropped)" % (
                                adj.get("verdict"), SONNET_MAX_PER_JOB, len(adj.get("dropped") or [])),
                            hooks, ledger_path, now)

    # ---- act on a GROUNDED verdict
    if verdict == "t":
        write_tooling_request(job, adj, hooks.get("tooling_path"), now)
        opus_note = "request written to tooling-requests.jsonl for the coordinating agent"
        if hooks.get("opus") and _count_day(lad, "opus", now) < OPUS_MAX_PER_DAY:
            _log(ledger_path, job, "opus", "tier-3 tooling root cause", now)
            try:
                out = hooks["opus"]("PIPELINE-TOOLING ROOT CAUSE. Find the defect in the pipeline tool, name the "
                                    "file:line and the minimal fix.\n\n" + job_context(job)[0][:9000]
                                    + "\n\nTier-2 finding: %s" % json.dumps(adj.get("findings"))[:2000])
                opus_note = "tier-3 Opus answered (%d chars)" % len(out or "")
                with Path(hooks.get("tooling_path") or TOOLING_REQUESTS).open("a") as fh:
                    fh.write(json.dumps({"at": _utc(now), "job": jid, "tier3": (out or "")[:6000]}) + "\n")
            except Exception as e:      # noqa: BLE001
                opus_note = "tier-3 Opus failed: %s" % type(e).__name__
        return exit_job(job, "tooling-root-cause", "%s; %s" % (adj.get("why"), opus_note), hooks, ledger_path, now)

    if verdict == "a":
        rc, tail = (hooks["run_verify"](job) if hooks.get("run_verify") and job.get("verify") else (None, "no verify"))
        if rc == 0 or (rc is None and findings):
            try:
                hooks["resolve"](jid)
            except Exception:
                pass
            _log(ledger_path, job, "close", "satisfied rc=%s" % rc, now)
            _record(ledger_path, jid, exit="satisfied")
            return "closed:already satisfied (%s)" % ("verify rc=0" if rc == 0 else "grounded evidence, no verify command")
        verdict = "b"          # (a) not proven by the job's own verify: treat as a spec problem

    # b / c / d -> one more round with the findings (needs a runnable job), else re-spec
    can_run = bool(job.get("cwd") and job.get("task_file") and job.get("verify") and job.get("model"))
    n_cont = _count_family(lad, fam, "continue")
    if can_run and n_cont < CONTINUE_MAX_PER_FAMILY and _count_day(lad, "continue", now) < ENQUEUE_MAX_PER_DAY \
            and not any(o.get("status") in ("pending", "running", "paused", "queued") and o.get("cwd") == job.get("cwd")
                        for o in jobs or [] if o.get("id") != jid):
        model, host = str(job["model"]), str(job.get("host_pref") or "studio")
        if verdict == "d" and cfg.get("bigger_model"):
            model, host = str(cfg["bigger_model"]), str(cfg.get("bigger_host") or host)
        tf = continuation_task(job, adj, review_text, hooks.get("esc_dir") or ESC_DIR)
        label = "%s-lc%d" % (re.sub(r"-lc\d+$", "", str(job.get("label"))), n_cont + 1)
        argv = ["--model", model, "--host", host, "--cwd", str(job["cwd"]), "--task-file", tf,
                "--task-kind", "coding", "--verify", str(job["verify"]),
                "--max-iters", str(job.get("max_iters") or 24), "--label", label, "--continues", str(jid)]
        if job.get("num_ctx"):
            argv += ["--num-ctx", str(job["num_ctx"])]
        if job.get("bundle"):
            argv += ["--bundle", str(job["bundle"])]
        new_id, out = hooks["enqueue"](argv)
        if new_id:
            _log(ledger_path, job, "continue", "verdict %s (%s) -> %s" % (verdict, source, new_id), now)
            _record(ledger_path, jid, continued_by=new_id, verdict=verdict, tier=source)
            return "heal-continuation:%s (tier %s verdict %s, %d grounded finding(s))" % (new_id, source, verdict, len(findings))
        return "wait:continuation enqueue failed: %s" % " ".join(str(out).split())[-120:]

    # budget spent (or nothing runnable): re-spec once per family, else the named exit
    if _count_family(lad, fam, "respec") < RESPEC_MAX_PER_FAMILY and hooks.get("respec"):
        notes = "ladder verdict %s (%s): %s" % (verdict, source, (adj or {}).get("why", ""))
        try:
            act = hooks["respec"](job, notes + "\n" + ((adj or {}).get("fix_notes") or ""),
                                  "ladder: continuation budget spent" if can_run else "ladder: no runnable spec",
                                  verdict == "d")
        except Exception as e:     # noqa: BLE001
            act = "skip:respec error %s" % type(e).__name__
        if str(act).startswith("respec:"):
            _log(ledger_path, job, "respec", act, now)
            _record(ledger_path, jid, respec=act)
            return act
    reason = "ladder-budget-spent" if can_run else "no-runnable-spec"
    return exit_job(job, reason, "verdict %s (%s): %s; continuations=%d/%d respecs=%d/%d" % (
        verdict, source, (adj or {}).get("why", ""), n_cont, CONTINUE_MAX_PER_FAMILY,
        _count_family(lad, fam, "respec"), RESPEC_MAX_PER_FAMILY), hooks, ledger_path, now)


def sweep(jobs, hooks, ledger_path=None, now=None, review_for=None, cfg=None, only=None, dry_run=False):
    """Every watcher pass: walk every needs_opus row the normal heal path has not already
    got pending. -> [(job_id, action)]. `dry_run` reports protection/supersession only."""
    now = time.time() if now is None else now
    out = []
    for j in list(jobs or []):
        if j.get("status") != "needs_opus" or (only and j.get("id") not in only):
            continue
        if dry_run:
            why = protect_reason(j, jobs, now, hooks.get("alive", socket_alive), hooks.get("protect_path"))
            nw = newer_attempt(j, jobs)
            out.append((j.get("id"), ("protected:%s" % why) if why else
                        ("would-retire:superseded by %s" % nw.get("id")) if nw else "would-adjudicate"))
            continue
        review = review_for(j) if review_for else ""
        try:
            out.append((j.get("id"), process_job(j, jobs, hooks, ledger_path, now, review, cfg)))
        except Exception as e:     # noqa: BLE001 -- one bad row never stops the pass
            out.append((j.get("id"), "error:%s: %s" % (type(e).__name__, str(e)[:120])))
    return out


if __name__ == "__main__":
    print("pipeline_ladder is a library; use: dispatch-self-heal.py --ladder-sweep [--dry-run]")
