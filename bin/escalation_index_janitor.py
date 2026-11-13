#!/usr/bin/env python3
"""escalation_index_janitor -- tick ESCALATIONS.md / READY-TO-LAND.md rows whose
underlying item is RESOLVED, from the pipeline's own state, never from prose.

WHY (2026-10-05): 40 rows were open at once and every one of them was already
resolved somewhere else -- the slice was accepted (`done`), the chain landed, the
plan was produced by a later plan-gen round, the needs_opus job was resolved, the
bundle was no longer parked. Nothing ever ticked a row, so the SessionStart hook
reported 40 "stuck" items and a human had to re-derive each one by hand. That is
exactly the opposite of unattended: a worklist that only grows gets ignored.

WHAT CLOSES A ROW (each needs POSITIVE evidence read from a state file; an
unreadable or missing source closes NOTHING):
  slice rows (A/B)        the slice is `done`/`skipped`, or its plan is
                          human-cancelled (plan_cancel), or it was re-sliced into
                          its own live sub-plan `<plan>-<sid>`.
  job rows (C/D/F, hex id) the job's slice (from the row label) is done/skipped or
                          its plan cancelled; or the job is `done` in live queue
                          state; or it is GONE from a readable live queue state
                          (worklist rows -- failed/needs_opus/blocked -- are never
                          pruned, only `resolve`/`cancel` removes them, so absence
                          is a human/automation having handled it); or, for a
                          `plan-gen-<L>-rN` job, plan <L> now exists (a later round
                          produced it) or is cancelled.
  integration rows (E)     WHOLE-CHAIN gate: integration now `passed`/`staged`, or
                          plan cancelled. CHAIN STAGED: integration `landed` is set
                          or `passed` (incl. "already on main"), the row's stage
                          branch was superseded by a re-stage or retired (branch
                          gone), or plan cancelled. A still-`staged` row whose
                          stage git says is no longer a fast-forward of main is
                          RE-STAGED (--integrate; never writes main) and re-decided.
  BUNDLE PARKED (Q)        the bundle is no longer in the queue's `_bundle_parked`,
                          or it is a cancelled plan, or it is a `plan-gen-<L>` bundle
                          whose plan <L> now exists/is cancelled; while it IS parked,
                          only the NEWEST open row per bundle stays open (older rows
                          are superseded by it).
  BLOOMGAUGE CONTROL (Q)   (ollama-queue _control_alert, 2026-11-02) `darkbloom-drift`:
                          the queue state has no `_db_drift` (Darkbloom serves every
                          model queued jobs need). `bloomgauge-control-pick` /
                          `-manager`: the queue's BloomGauge control is no longer
                          'on', or the newest ledger result of that write kind is ok
                          and not older than the row (a write succeeded since).
                          `bloomgauge-control-log` (the activity log unreadable 30+
                          min): control not 'on', any write succeeded since, or the
                          tick logged "log: activity log readable again" since.
  INFRA (Q)                (ollama-queue _control_alert tag INFRA, 2026-11-02) `path-access`
                          (a path queued jobs open stayed held with the screen unlocked):
                          the queue state's `_path_blocked` is empty or absent.

HOW: a same-length, in-place `[ ]` -> `[x]` byte flip at the row's offset (re-read
and re-checked under the write), so a row another process appends concurrently is
never lost (the appenders do not lock). Every close is logged as one JSON line to
ESCALATIONS-AUTOCLOSE.jsonl beside the index (row, rule, evidence).

CLI:  escalation_index_janitor.py [--apply] [--index F] [--ready F]
      (default is a DRY RUN that prints what would close).
"""
from __future__ import annotations

import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import calendar
import json
import math
import os
import re
import sys
import time
from pathlib import Path

HOME = Path.home()
ESC_DIR = HOME / ".ollama-dispatch" / "escalations"
INDEX_MD = ESC_DIR / "ESCALATIONS.md"
READY_MD = ESC_DIR / "READY-TO-LAND.md"
RUNS_DIR = HOME / ".ollama-dispatch" / "slice-runs"
PLANS_DIR = HOME / ".ollama-dispatch" / "slice-plans"
QUEUE_STATE = queue_config.state_dir() / "ollama-queue-state.json"
LOG_NAME = "ESCALATIONS-AUTOCLOSE.jsonl"

ROW_RE = re.compile(r"^- \[ \] `(?P<plan>[^`]*)` \*\*(?P<who>[^*]+)\*\* (?P<src>[A-Z]) - "
                    r"(?P<ts>\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ) - ")
JOB_ID_RE = re.compile(r"^[0-9a-f]{12}$")
BRANCH_RE = re.compile(r"integrate/[A-Za-z0-9._/-]+?-[0-9a-f]{8}\b")
# decide() returns this rule prefix for a still-`staged` CHAIN STAGED row whose stage
# git says is no longer a fast-forward of main; run(apply=True) then re-stages the
# plan (--integrate: classify only, never writes main) and re-decides the row once.
RESTAGE_RULE = "restage:"
PLAN_GEN_RE = re.compile(r"^plan-gen-(?P<label>.+)-r\d+$")
SLICE_DONE = ("done", "skipped")
WORKLIST_STATUSES = ("failed", "needs_opus", "blocked", "done_unconverged", "escalated")
# ollama-queue CONTROL_ALERT_TAG rows: key -> the bloomgauge_control ledger actions whose
# success closes it (darkbloom-drift closes from queue state instead).
CONTROL_TAG = "BLOOMGAUGE CONTROL"
INFRA_TAG = "INFRA"
CONTROL_KEYS = {"bloomgauge-control-pick": ("pick",),
                "bloomgauge-control-manager": ("mgr_off", "mgr_on"),
                # the policy writes only while the activity log reads (bud log_ok), so any write
                # that succeeded since closes the log alert, as does the tick's CONTROL_LOG_BACK row
                "bloomgauge-control-log": ("pick", "mgr_off", "mgr_on")}
CONTROL_LOG_KEY = "bloomgauge-control-log"
CONTROL_MANAGER_KEY = "bloomgauge-control-manager"
# outcome statuses (bloomgauge_policy.TURN_ON_OK minus 'overridden') that show an answered Manager-on
# turned the Manager on: only these close the manager alert
CONTROL_TURN_ON_OK = ("active", "superseded")
CONTROL_LOG_BACK = "log: activity log readable again"   # bloomgauge_tick.LOG_BACK_REASON
CONTROL_TS_SLACK_S = 120.0   # the row is stamped when the queue pops the alert, after the write


def _load(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def label_base(label):
    """Strip auto-author-/auto-refine-/gate-/regate-/secondop-/esc-review- prefixes
    and trailing -rN/-cN/-esc decorations (same rule as dispatch-self-heal)."""
    b = re.sub(r"^(?:auto-(?:author|refine)-|gate-|regate-|secondop-)", "", str(label or ""))
    prev = None
    while prev != b:
        prev = b
        b = re.sub(r"-(?:r\d+|c\d+|esc)$", "", b)
    return b


class Facts:
    """Read-only view of the pipeline state the rules consult. Each accessor returns
    None for 'unknown' (unreadable), which every rule treats as 'do not close'."""

    def __init__(self, runs_dir=RUNS_DIR, plans_dir=PLANS_DIR, queue_state=QUEUE_STATE,
                 cancelled=None, bg_config=None):
        self.runs_dir = Path(runs_dir)
        self.plans_dir = Path(plans_dir)
        self._q = _load(queue_state)
        self._runs = {}
        self._cancelled = cancelled
        self._bg_config = bg_config        # dict (tests) or None = bloomgauge_control.config()
        self._agent_doc = None             # container: the status service's ledger answer (see _agent_ledger)
        self._bg = None

    # -- plans -------------------------------------------------------------
    def run(self, plan):
        if plan not in self._runs:
            st = _load(self.runs_dir / f"{plan}.json") if plan else None
            self._runs[plan] = st if isinstance(st, dict) else None
        return self._runs[plan]

    def plan_exists(self, plan):
        return bool(plan) and ((self.runs_dir / f"{plan}.json").is_file()
                               or (self.plans_dir / f"{plan}.slices.json").is_file())

    def is_cancelled(self, plan):
        if not plan:
            return None
        if self._cancelled is not None:
            return self._cancelled(plan)
        try:
            sys.path.insert(0, str(Path(__file__).resolve().parent))
            import plan_cancel
            return plan_cancel.cancelled(plan, runs_dir=self.runs_dir)
        except Exception:
            return None

    def slice_status(self, plan, sid):
        st = self.run(plan)
        if not st:
            return None
        s = (st.get("slices") or {}).get(sid) if isinstance(st.get("slices"), dict) else None
        return str(s.get("status") or "") if isinstance(s, dict) else None

    def plan_slice_for_label(self, label):
        """(plan, sid) for a job label of a slice plan, else (None, None). Longest
        plan-name prefix wins (sub-plans are `<plan>-<sid>`)."""
        base = label_base(label)
        try:
            stems = sorted((p.stem for p in self.runs_dir.glob("*.json")
                            if ".bak" not in p.name), key=len, reverse=True)
        except OSError:
            return None, None
        for plan in stems:
            if base.startswith(plan + "-"):
                sid = base[len(plan) + 1:]
                if self.slice_status(plan, sid) is not None:
                    return plan, sid
        return None, None

    # -- queue -------------------------------------------------------------
    def queue_ok(self):
        return isinstance(self._q, dict) and isinstance(self._q.get("jobs"), list)

    def job(self, jid):
        if not self.queue_ok():
            return None
        return next((j for j in self._q["jobs"] if j.get("id") == jid), None)

    def job_with_label(self, label):
        if not self.queue_ok():
            return None
        return next((j for j in self._q["jobs"] if j.get("label") == label), None)

    def parked_bundles(self):
        if not self.queue_ok():
            return None
        p = self._q.get("_bundle_parked")
        return p if isinstance(p, dict) else {}

    def db_drift(self):
        """Models the queue last alerted Darkbloom does not serve ([] = none), None unreadable."""
        if not self.queue_ok():
            return None
        d = self._q.get("_db_drift")
        return [str(x) for x in d] if isinstance(d, list) else []

    def path_blocked(self):
        """Paths a new process cannot open right now per the queue's path-access gate ([] = none),
        None unreadable."""
        if not self.queue_ok():
            return None
        d = self._q.get("_path_blocked")
        return [str(x) for x in d] if isinstance(d, list) else []

    # -- the queue's BloomGauge control (read-only) -------------------------
    def _bg_cfg(self):
        if self._bg is None:
            try:
                if self._bg_config is not None:
                    cfg = self._bg_config
                else:
                    sys.path.insert(0, str(Path(__file__).resolve().parent))
                    import bloomgauge_control
                    cfg = bloomgauge_control.config()
                self._bg = cfg if isinstance(cfg, dict) else False
            except Exception:
                self._bg = False
        return self._bg or None

    def _agent_ledger(self):
        """Container (QUEUE_AGENT_URL set): the Mac status service's /v1/bloomgauge-ledger, fetched once per Facts
        (one janitor run). The ledger and the control mode live on the Mac. False = agent unset, None = no answer
        (every rule then reads 'unreadable' and does not close), else the answer dict."""
        if not queue_config.agent_url():
            return False
        if self._agent_doc is None:
            doc = queue_config.agent_get("/v1/bloomgauge-ledger", timeout=5)
            self._agent_doc = doc if isinstance(doc, dict) and isinstance(doc.get("rows"), list) else {}
        return self._agent_doc or None

    def bg_control_mode(self):
        doc = self._agent_ledger()
        if doc is not False:
            return str(doc.get("mode") or "off") if doc else None
        cfg = self._bg_cfg()
        return str(cfg.get("mode") or "off") if cfg else None

    def bg_last_write(self, actions):
        """The newest phase-'result' ledger row (mode 'on') whose action is in `actions`;
        {} when there is none, None when the ledger cannot be read."""
        return self._bg_newest(lambda r: r.get("phase") == "result" and r.get("action") in actions)

    def bg_last_reason(self, reason):
        """The newest ledger row (mode 'on') whose reason is `reason`; {} when there is none,
        None when the ledger cannot be read."""
        return self._bg_newest(lambda r: r.get("reason") == reason)

    def bg_outcome(self, attempt_ts):
        """How BloomGauge settled the queue's answered Manager-on of attempt_ts: the newest phase
        'outcome' ledger row (mode 'on') for it (bloomgauge_control.record_outcome); {} while it is
        unsettled, None when the ledger cannot be read."""
        def same(r):
            try:
                return r.get("phase") == "outcome" and abs(float(r.get("attempt_ts")) - float(attempt_ts)) < 1e-6
            except (TypeError, ValueError):
                return False
        return self._bg_newest(same)

    def _bg_newest(self, keep):
        doc = self._agent_ledger()
        if doc is not False:
            if not doc or doc.get("ledger") == "unreadable":
                return None
            text = "\n".join(str(r) for r in doc["rows"])
        else:
            cfg = self._bg_cfg()
            if not cfg or not cfg.get("ledger_path"):
                return None
            try:
                text = Path(cfg["ledger_path"]).read_text()
            except FileNotFoundError:
                return {}
            except OSError:
                return None
        best, best_ts = {}, None
        for line in text.splitlines():
            try:
                r = json.loads(line)
                ts = float(r.get("ts"))
            except Exception:
                continue
            if not math.isfinite(ts) or r.get("mode") != "on" or not keep(r):
                continue
            if best_ts is None or ts >= best_ts:
                best, best_ts = r, ts
        return best

    # -- git (live; None = unknown -> never closes) -----------------------------
    @staticmethod
    def _git_rc(repo, *args):
        if not repo or not Path(repo).is_dir():
            return None
        try:
            import subprocess
            return subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                                  timeout=30).returncode
        except Exception:
            return None

    def branch_exists(self, repo, branch):
        rc = self._git_rc(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        return None if rc is None else (rc == 0)

    def stage_landable(self, repo, onto, branch):
        """True when `onto` is an ancestor of the staged branch (still a fast-forward),
        False when it is not (main moved), None when git cannot say."""
        if not onto or not branch:
            return None
        rc = self._git_rc(repo, "merge-base", "--is-ancestor", f"refs/heads/{onto}",
                          f"refs/heads/{branch}")
        return {0: True, 1: False}.get(rc)

    def restage(self, plan):
        """Re-run the slicer's STAGE half for `plan` (classify against the CURRENT
        default branch; writes no default branch, pushes nothing). Returns rc or None."""
        st = self.run(plan) or {}
        pp = st.get("plan_path")
        if not pp or not Path(pp).is_file():
            return None
        try:
            import subprocess
            rc = subprocess.run([sys.executable, str(Path(__file__).resolve().parent /
                                                     "ollama-dispatch-slice"), pp, "--integrate"],
                                capture_output=True, text=True, timeout=600).returncode
        except Exception:
            return None
        self._runs.pop(plan, None)          # re-read the re-staged record
        return rc


def decide(row, facts, newest_q_ts=None):
    """PURE given `facts`. (close: bool, rule: str) for one parsed open row."""
    plan, who, src = row["plan"], row["who"].strip(), row["src"]
    # BUNDLE PARKED (queue)
    if src == "Q" and who == "BUNDLE PARKED":
        parked = facts.parked_bundles()
        if parked is None:
            return False, "queue state unreadable"
        if facts.is_cancelled(plan):
            return True, f"plan {plan} is cancelled"
        m = PLAN_GEN_RE.match(plan + "-r0") if plan.startswith("plan-gen-") else None
        if m and (facts.is_cancelled(m.group("label")) or facts.plan_exists(m.group("label"))):
            return True, (f"plan-gen bundle {plan}: plan {m.group('label')} was produced "
                          f"or cancelled")
        if plan not in parked:
            return True, f"bundle {plan} is no longer parked in the queue"
        if newest_q_ts and row["ts"] < newest_q_ts.get(plan, row["ts"]):
            return True, (f"superseded by the newer open BUNDLE PARKED row for {plan} "
                          f"({newest_q_ts[plan]})")
        return False, f"bundle {plan} is still parked"
    # INFRA (queue, 2026-11-02): the queue's own infrastructure alerts. Positive evidence only.
    if src == "Q" and who == INFRA_TAG:
        if plan == "path-access":
            held = facts.path_blocked()
            if held is None:
                return False, "queue state unreadable"
            if not held:
                return True, "a new process opens every path queued jobs use (no _path_blocked in queue state)"
            return False, f"still held: {', '.join(held)}"
        return False, f"unknown {INFRA_TAG} key {plan}"
    # BLOOMGAUGE CONTROL (queue, 2026-11-02): the queue's BloomGauge control could not do its
    # job (ollama-queue _control_alert). Closes only on positive evidence, never on silence.
    if src == "Q" and who == CONTROL_TAG:
        if plan == "darkbloom-drift":
            drift = facts.db_drift()
            if drift is None:
                return False, "queue state unreadable"
            if not drift:
                return True, "Darkbloom serves every model queued jobs need (no _db_drift in queue state)"
            return False, f"Darkbloom still does not serve {', '.join(drift)}"
        acts = CONTROL_KEYS.get(plan)
        if acts is None:
            return False, f"unknown {CONTROL_TAG} key {plan}"
        mode = facts.bg_control_mode()
        if mode is None:
            return False, "BloomGauge control config unreadable"
        if mode != "on":
            return True, f"the queue's BloomGauge control is {mode}: it makes no BloomGauge writes"
        last = facts.bg_last_write(acts)
        if last is None:
            return False, "BloomGauge control ledger unreadable"
        try:
            row_t = calendar.timegm(time.strptime(row["ts"], "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError):
            return False, "row timestamp unreadable"
        if last.get("result") == "ok" and float(last["ts"]) >= row_t - CONTROL_TS_SLACK_S:
            when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(last["ts"])))
            if plan == CONTROL_MANAGER_KEY and last.get("action") == "mgr_on" and last.get("op_id"):
                # BloomGauge answers a Manager-on at once and turns the Manager on afterwards, or
                # blocks it (2026-11-03): only the settled outcome shows the change succeeded
                out = facts.bg_outcome(last["ts"])
                if out is None:
                    return False, "BloomGauge control ledger unreadable"
                st = out.get("status") if out else None
                if st not in CONTROL_TURN_ON_OK:
                    return False, (f"the Manager-on of {when} was answered but BloomGauge has not turned the "
                                   f"Manager on ({st or 'still under way'})")
                return True, f"a later mgr_on write turned the Manager on ({when}, {st})"
            return True, f"a later {last.get('action')} write succeeded ({when})"
        if plan == CONTROL_LOG_KEY:
            back = facts.bg_last_reason(CONTROL_LOG_BACK)
            if back and float(back["ts"]) >= row_t - CONTROL_TS_SLACK_S:
                when = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(float(back["ts"])))
                return True, f"the activity log read again ({when})"
            return False, "the activity log has not read again since the alert"
        return False, f"no {'/'.join(acts)} write has succeeded since the alert"
    # HARNESS GO awaiting the relevance review (source R, ollama-dispatch-auto
    # surface_go, 2026-10-05): closes once the coding dispatch for this exact label
    # is a queue row (the human gate was passed and it was enqueued), or the
    # worktree is gone (abandoned/pruned).
    if src == "R" and who == "HARNESS GO -- relevance review":
        wt = row.get("path")
        if wt and not Path(wt).exists():
            return True, f"worktree {wt} is gone"
        if not facts.queue_ok():
            return False, "queue state unreadable"
        if facts.job_with_label(plan) is not None:
            return True, f"coding dispatch --label {plan} is enqueued"
        return False, f"no coding dispatch for {plan} yet"
    # INTEGRATION (source E)
    if src == "E":
        if facts.is_cancelled(plan):
            return True, f"plan {plan} is cancelled"
        st = facts.run(plan)
        if not st:
            return False, f"plan state for {plan} unreadable"
        integ = st.get("integration") if isinstance(st.get("integration"), dict) else {}
        status = str(integ.get("status") or "")
        if who.startswith("CHAIN STAGED"):
            if integ.get("landed"):
                return True, f"chain landed @ {str(integ.get('landed'))[:12]}"
            # STALE READY ROWS (2026-10-05, chat-frontend-plan): the row stayed open a
            # day after main had absorbed the chain by hand -- only `landed` closed it,
            # and nothing re-checked the stage against main outside --execute.
            if status == "passed":
                return True, (f"integration passed: {str(integ.get('summary') or '')[:90]}"
                              or "integration passed")
            row_br = row.get("branch")
            cur_br = integ.get("integrate_branch")
            if row_br and cur_br and row_br != cur_br:
                return True, f"stage {row_br} superseded by re-stage {cur_br}"
            if row_br and status in ("stale", "failed") \
                    and facts.branch_exists(st.get("repo"), row_br) is False:
                return True, f"stage {row_br} retired (integration {status}, branch gone)"
            if status == "staged" and row_br and row_br == cur_br:
                landable = facts.stage_landable(st.get("repo"), integ.get("onto"), cur_br)
                if landable is False:
                    return False, (f"{RESTAGE_RULE} {integ.get('onto')} is no longer an "
                                   f"ancestor of {cur_br} (main moved under the stage)")
            return False, f"integration status {status or '?'}, not landed"
        if who.startswith("WHOLE-CHAIN"):
            if status in ("passed", "staged"):
                return True, f"integration gate is now {status}"
            return False, f"integration gate still {status or '?'}"
        return False, "unrecognised integration row"
    # JOB rows (a 12-hex job id)
    if JOB_ID_RE.match(who):
        p, sid = facts.plan_slice_for_label(plan)
        if p:
            if facts.is_cancelled(p):
                return True, f"plan {p} is cancelled"
            ss = facts.slice_status(p, sid)
            if ss in SLICE_DONE:
                return True, f"slice {p}/{sid} is {ss}"
        m = PLAN_GEN_RE.match(plan)
        if m:
            lab = m.group("label")
            if facts.is_cancelled(lab):
                return True, f"plan {lab} is cancelled"
            if facts.plan_exists(lab):
                return True, f"plan {lab} was produced (slice plan/run state exists)"
        if not facts.queue_ok():
            return False, "queue state unreadable"
        j = facts.job(who)
        if j is None:
            return True, (f"job {who} is gone from live queue state (worklist rows only "
                          f"leave via resolve/cancel)")
        if j.get("status") == "done":
            return True, f"job {who} is done"
        return False, f"job {who} is {j.get('status')}"
    # SLICE rows (A/B)
    if src in ("A", "B"):
        if facts.is_cancelled(plan):
            return True, f"plan {plan} is cancelled"
        ss = facts.slice_status(plan, who)
        if ss is None:
            return False, f"slice {plan}/{who} not found / unreadable"
        if ss in SLICE_DONE:
            return True, f"slice {plan}/{who} is {ss}"
        if facts.run(f"{plan}-{who}"):
            return True, f"slice re-sliced into its own live sub-plan {plan}-{who}"
        return False, f"slice {plan}/{who} is {ss}"
    return False, "no rule for this row shape"


def scan(text):
    """[(byte_offset_of_line, line, parsed_row)] for every OPEN parseable row."""
    out, off = [], 0
    for raw in text.encode("utf-8").split(b"\n"):
        line = raw.decode("utf-8", errors="replace")
        m = ROW_RE.match(line)
        if m:
            d = m.groupdict()
            tail = re.findall(r"`([^`]+)`", line)
            d["path"] = tail[-1] if len(tail) >= 2 else None
            bm = BRANCH_RE.search(line)
            d["branch"] = bm.group(0) if bm else None
            out.append((off, line, d))
        off += len(raw) + 1
    return out


def _flip(path, offset, expect_line):
    """In-place '[ ]' -> '[x]' at `offset`, only if the bytes there still are the
    expected open row. Same length: concurrent appends are untouched."""
    want = expect_line.encode("utf-8")
    with open(path, "r+b") as fh:
        fh.seek(offset)
        cur = fh.read(len(want))
        if cur != want or not cur.startswith(b"- [ ] "):
            return False
        fh.seek(offset + 3)
        fh.write(b"x")
        fh.flush()
        os.fsync(fh.fileno())
    return True


RESTAGE_ATTEMPTS_NAME = "index-janitor-restage-attempts.json"


def restage_unlandable_stages(facts, apply=False, attempts_path=None, out=print):
    """PLAN-SIDE re-check of every `staged` chain, independent of index rows.

    WHY (2026-10-06, pipeline-canary --hand-land): the row rules above re-stage a
    CHAIN STAGED row only once that row EXISTS. A chain merged into main by hand
    between watcher passes still read `staged`; the pass ran the janitor first (no
    row yet: nothing to re-check), THEN detected `staged` and posted a brand-new
    "ready to land" row + desktop notification for a chain main already contained.
    Re-checking the plan record itself, before detection, fixes the state at its
    source: the slicer's --integrate re-classifies it (e.g. "already on main" ->
    passed) and no false row is ever written.

    Only a stage git PROVES is no longer a fast-forward of `onto` (stage_landable is
    False) is touched; unknown (branch gone, repo missing) is left alone. Bounded:
    one attempt per (plan, onto tip, stage branch), recorded in `attempts_path`, so a
    re-stage that does not change the record is never retried every pass.
    Returns [(plan, rc)] re-staged (or that WOULD be when apply=False). Never raises."""
    done = []
    try:
        plans = sorted(p.stem for p in Path(facts.runs_dir).glob("*.json"))
    except Exception:
        return done
    tried = (_load(attempts_path) if attempts_path else None) or {}
    changed = False
    for plan in plans:
        try:
            st = facts.run(plan) or {}
            integ = st.get("integration") if isinstance(st.get("integration"), dict) else {}
            if str(integ.get("status") or "") != "staged" or integ.get("landed"):
                continue
            onto, br = integ.get("onto"), integ.get("integrate_branch")
            if facts.stage_landable(st.get("repo"), onto, br) is not False:
                continue
            if facts.is_cancelled(plan):
                continue
            tip = ""
            try:
                import subprocess
                tip = subprocess.run(["git", "-C", str(st.get("repo")), "rev-parse",
                                      f"refs/heads/{onto}"], capture_output=True,
                                     text=True, timeout=30).stdout.strip()
            except Exception:
                pass
            key = f"{plan}|{onto}@{tip[:12]}|{br}"
            if tried.get(plan) == key:
                continue
            if not apply:
                out(f"would re-stage {plan}: {onto} is no longer an ancestor of {br}")
                done.append((plan, None))
                continue
            rc = facts.restage(plan)
            tried[plan] = key
            changed = True
            out(f"re-staged {plan} (rc {rc}) -- {onto} is no longer an ancestor of {br} "
                f"(main moved under the stage, no index row yet)")
            done.append((plan, rc))
        except Exception as e:
            out(f"re-stage check error on {plan}: {type(e).__name__}: {e}")
    if changed and attempts_path:
        try:
            tmp = Path(str(attempts_path) + ".tmp")
            tmp.write_text(json.dumps(tried, indent=1, sort_keys=True))
            os.replace(tmp, attempts_path)
        except OSError:
            pass
    return done


def run(index_files=None, facts=None, apply=False, log_path=None, out=print):
    """Close every resolved row in `index_files`. Returns [(file, line, rule)] closed
    (or that WOULD close when apply=False). Never raises."""
    facts = facts or Facts()
    closed = []
    _files = [Path(f) for f in (index_files or (INDEX_MD, READY_MD))]
    restage_unlandable_stages(
        facts, apply=apply, out=out,
        attempts_path=(_files[0].parent / RESTAGE_ATTEMPTS_NAME) if _files else None)
    for f in index_files or (INDEX_MD, READY_MD):
        f = Path(f)
        try:
            text = f.read_text()
        except OSError:
            continue
        rows = scan(text)
        newest = {}
        for _o, _l, r in rows:
            if r["src"] == "Q" and r["who"] == "BUNDLE PARKED":
                newest[r["plan"]] = max(newest.get(r["plan"], ""), r["ts"])
        for off, line, r in rows:
            try:
                ok, rule = decide(r, facts, newest)
                if not ok and str(rule).startswith(RESTAGE_RULE):
                    if not apply:
                        out(f"would re-stage {r['plan']}: {line[:90]}  <- {rule}")
                        continue
                    rc = facts.restage(r["plan"])
                    out(f"re-staged {r['plan']} (rc {rc}) -- {rule}")
                    ok, rule = decide(r, facts, newest)
                    if not ok and str(rule).startswith(RESTAGE_RULE):
                        ok, rule = False, "re-stage did not change the record"
            except Exception as e:
                ok, rule = False, f"rule error {type(e).__name__}: {e}"
            if not ok:
                continue
            if apply and not _flip(f, off, line):
                continue
            closed.append((str(f), line, rule))
            out(("closed" if apply else "would close") + f": {line[:110]}  <- {rule}")
            if apply:
                try:
                    lp = Path(log_path) if log_path else f.parent / LOG_NAME
                    with lp.open("a") as fh:
                        fh.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                                 time.gmtime()),
                                             "file": str(f), "row": line[:600],
                                             "rule": rule}) + "\n")
                except OSError:
                    pass
    return closed


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="tick rows (default: dry run)")
    ap.add_argument("--index", action="append", help="index file(s) (default: both)")
    a = ap.parse_args(argv)
    res = run(a.index, apply=a.apply)
    print(f"{'closed' if a.apply else 'would close'} {len(res)} row(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
