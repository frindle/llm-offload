"""Pure decisions for queue-driven BloomGauge control: the queue turns the Manager off while its
work runs (pinning the model it needs) and hands the Manager back once the queue has been idle
for cfg idle_s (Claude session activity is ignored). Design: queue_bloomgauge_control_design.md (2026-10-31).

No I/O here: every input is a plain dict, every output is a plain dict. bloomgauge_control.py does
the reads/writes, bloomgauge_tick.py wires them together.

Lease (the queue's claim on BloomGauge): None (IDLE) or
  {state: HELD|DRAIN|HANDBACK, since, target, prior_manager_on, idle_since?, mgr_off_at?, picked?, picked_at?,
   mgr_on_seen_at?, yielded_at?}; the tick adds heartbeat (the time of the tick that saved it), daemon_pid and mode.
Decision: {action: none|wait|yield|mgr_off|mgr_on|pick, model, reason}.

Rate limits (2026-11-02): BloomGauge admits at most APP_PICKS_PER_H picks/restarts and APP_MGR_PER_H
Manager changes per rolling hour (in memory, on its own monotonic clock) and answers 429 over that.
Every write therefore waits: on the hourly budget (own ledger + other agents in the activity log), on
a failed write (FAIL_BACKOFF_S by streak; a 429 at least RATE_LIMITED_MIN_S), on a switch in progress,
and for yield_s after a manual change. Wait reasons are constant strings (the ledger dedupes on them).
No write, and no lease taken, while the activity log is unreadable (bud log_ok not True, 2026-11-02):
a manual change could not be seen then, so acting could fight the owner.
While the queue holds the lease and has work, a Manager that BloomGauge turned back on by itself
(never a manual change: that yields) is turned off again with the same waits (_reassert_step).

Owner first (2026-11-02, Fable review #2/#8): every manual change the tick sees is also written to the
ledger as a {phase: 'manual', manual_at} row, so it outlives the activity log's window. With no lease,
the queue turns an off Manager back on only when it left it off itself (bud 'leak': its newest
successful Manager change was a Manager-off and no manual change came after it). A manual change
while the queue holds the Manager off (the lease's prior_manager_on, Manager still off) keeps the
lease, marked yielded_at: no changes for yield_s, then the usual hand-back, so the Manager is never
left off and unowned.
"""
import math
import re

APP_PICKS_PER_H = 2   # BloomGauge agent_access.CHANGES_PER_HOUR (model switches + restarts)
APP_MGR_PER_H = 6     # BloomGauge agent_access.SETTINGS_PER_HOUR (Manager on/off, follow Recommended)
HOUR = 3600.0
PICK_WHATS = ("change-model", "restart")
MGR_WHATS = ("manager", "follow")
MGR_ACTIONS = ("mgr_off", "mgr_on")
# mode.change 'why' values BloomGauge's own logic writes (bloom_events.py): never a person or an agent.
# 'pick-done' is the app restoring the Manager after a pick it already logged as 'pick'.
APP_WHYS = ("blocked", "startup", "recovery", "auto", "autopilot", "pick-done")
UI_CHANGE_WHATS = ("manager", "follow", "manual")   # control.request rows that change the Manager or the model
# Causal matching of activity-log rows to the queue's own attempt rows. The attempt row is written
# BEFORE the call (GET + GET + POST, 25 s timeout each), so the app's row lands 0-75 s later (seen:
# 0.5-12.4 s); its 'at' is whole seconds, so it can read up to 1 s early.
MATCH_LATE_S = 180.0
MATCH_EARLY_S = 5.0
PAIR_S = 2.0          # an agent's manager/restart request is also logged as a control.request in the same second
# What BloomGauge saw of an own write, read from its ledger result row (_outcomes): agent_mcp raises
# ValueError/TypeError before sending anything, and 'not answering' for a refused connection or a timeout.
NOT_SENT_RE = re.compile(r"^(ValueError|TypeError):")
NO_ANSWER_RE = re.compile(r"not answering")
MGR_FAIL_BACKOFF_S = (120.0, 300.0, 900.0)     # wait after the 1st, 2nd, 3rd+ failed Manager change in a row
PICK_FAIL_BACKOFF_S = (300.0, 1800.0, 3600.0)  # wait after the 1st, 2nd, 3rd+ failed pick in a row
RATE_LIMITED_MIN_S = 900.0                     # BloomGauge said 429 (its window differs from ours): at least this
RATE_LIMITED_RE = re.compile(r"at most \d+ times an hour")   # agent_mcp's text for a 429
ALERT_STREAK = 3      # failed writes of one kind in a row that raise one queue alert
ALERT_PHASE = "alert"  # ledger rows {phase: 'alert', kind: pick|manager}: the alert raised for a failure streak
REASSERT_GRACE_S = MATCH_LATE_S   # a Manager change of the queue's own can take this long to show in the view
REASSERT_CONFIRM_S = 60.0         # the Manager must read on this long before the queue turns it off again:
                                  # BloomGauge logs a request when it is answered, so a manual change can
                                  # show in the view a moment before its activity-log row does
REASSERT_KEEP_MGR = 1             # Manager changes a re-assert leaves in the hourly budget, so the hand-back can run
MANUAL_PHASE = "manual"           # ledger rows {phase: 'manual', manual_at}: manual changes the tick has seen
YIELD_REASON = "yielded-to-owner: manual Manager change"
YIELD_KEEP_REASON = "yielded-to-owner: manual change; lease kept, Manager handed back after yield_s"
# A Manager-on is BloomGauge's operation, not a switch (optimizer_control.py, beta 2026-11-02): the POST
# is answered at once with the operation {id: the request's requestId, status 'pending'}, which then runs
# (pending/starting/waiting; up to 3 x INTENT_SECONDS = 1800 s over a running Darkbloom) and ends 'active'
# (Manager on), 'blocked' (block(): it could not turn on; the Manager stays Manual) or 'cancelled' (a
# Manual choice or a provider-control admission). The operation keeps its last status until the next
# one replaces it. 2026-11-03: a hand-back turn-on blocked at 21:31 ("Could not verify this command. Check
# Darkbloom on the Mac before retrying.") read as mid-change for 12 h, and the queue, which had dropped
# its lease once the POST was answered, never noticed: the Manager stayed Manual all night.
OP_RUNNING = ("pending", "starting", "waiting")
OP_SETTLED = ("active", "blocked", "cancelled")   # any other status reads as mid-change
OUTCOME_PHASE = "outcome"         # ledger rows {phase: 'outcome', attempt_ts, op_id, status}: how a turn-on ended
# outcomes that are not a failed change: turned on ('active'; 'superseded': a newer operation did), or
# 'overridden' (a manual Manager change after the request cancelled or replaced it: the owner's call,
# which decide yields to; the janitor closes the manager alert only on the first two)
TURN_ON_OK = ("active", "superseded", "overridden")
TURN_ON_WAIT_S = 3 * 600.0 + 60.0  # a turn-on still running this long after its write counts as failed
TURN_ON_ERR = "turn-on "           # err text of a turn-on BloomGauge answered but did not complete (_fail_run)
TURN_ON_BACKOFF_S = (900.0, 1800.0, 3600.0)   # a turn-on BloomGauge blocked: retrying at once cannot help


def _num(v):
    """PURE. v as a finite float when it is an int/float (not a bool), else None."""
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        return None
    v = float(v)
    return v if math.isfinite(v) else None


def read_view(mc, oc, access):
    """PURE. Flatten GET /api/model-control (mc), /api/optimizer/control (oc) and
    /api/agent-access (access) into {agent_on, manager_on, can_enable, blocker, current, serving,
    switching, op_id, op_status, op_detail}. switching is True while BloomGauge is mid-change (a
    switch, a pending provider operation, an operation still running: any status but OP_SETTLED, or a
    provider that is neither running nor stopped): the queue never writes then. A settled operation
    (active, blocked, cancelled) is not a change in progress: it keeps its status until the next one.
    op_id/op_status/op_detail: the newest Manager-on operation (None/None/'' when there is none)."""
    mc = mc if isinstance(mc, dict) else {}
    oc = oc if isinstance(oc, dict) else {}
    access = access if isinstance(access, dict) else {}
    auto = oc.get("automatic") if isinstance(oc.get("automatic"), dict) else {}
    op = oc.get("operation") if isinstance(oc.get("operation"), dict) else {}
    pc = mc.get("providerControl") if isinstance(mc.get("providerControl"), dict) else {}
    blocker = auto.get("blocker") if isinstance(auto.get("blocker"), dict) else {}
    agent_on = bool(access.get("enabled")) and isinstance(access.get("keyPath"), str)
    manager_on = auto.get("mode") not in (None, "", "manual")
    can_enable = bool(auto.get("canEnable"))
    blocker_code = blocker.get("code")
    current = mc.get("currentModel") or oc.get("currentModel")
    serving = [m for m in (mc.get("servingModels") or []) if isinstance(m, str)]
    switching = False
    if mc.get("switching"):
        switching = True
    if mc.get("switchingBy"):
        switching = True
    if pc.get("operationPending"):
        switching = True
    op_status = op.get("status") if isinstance(op.get("status"), str) else None
    if op_status is not None and op_status not in OP_SETTLED:
        switching = True
    if pc.get("status") not in ("running", "stopped", None):
        switching = True
    op_id = op.get("id") if isinstance(op.get("id"), str) and op.get("id") else None
    return {
        "agent_on": agent_on, "manager_on": manager_on,
        "can_enable": can_enable, "blocker": blocker_code,
        "current": current, "serving": serving, "switching": switching,
        "op_id": op_id, "op_status": op_status, "op_detail": " ".join(str(op.get("detail") or "").split())[:200],
    }


def _row_kind(etype, f):
    """PURE. What one activity-log row (event etype, fields f) did, for matching against own attempts:
    'pick', 'mgr_on', 'mgr_off', 'mgr' (a Manager change, direction not logged), 'ui' (a UI-only
    change the queue never makes: a manual pick or follow), or None (a row the budget ignores,
    including every mode.change BloomGauge makes on its own: why in APP_WHYS)."""
    what = f.get("what")
    if etype == "agent.request":
        return "pick" if what in PICK_WHATS else ("mgr" if what in MGR_WHATS else None)
    if etype == "control.request":
        if what == "manager":
            return {"yes": "mgr_on", "no": "mgr_off"}.get(f.get("on"), "mgr")
        return "ui" if what in UI_CHANGE_WHATS else None
    if etype == "mode.change":
        why = f.get("why")
        if why in APP_WHYS:
            return None
        if why == "pick":
            return "pick"
        return {"on": "mgr_on", "off": "mgr_off"}.get(f.get("to"), "mgr")
    return None


def _fits(kind, action):
    """PURE. Can an own attempt of this action have caused a row of this kind?"""
    if kind == "pick":
        return action == "pick"
    if kind == "mgr":
        return action in MGR_ACTIONS
    return kind in MGR_ACTIONS and action == kind


def _agrees(outcome, result):
    """PURE. Can an own attempt with this outcome (_outcomes) have logged a request row with this result?
    BloomGauge logs ok for a request it answered ok and refused/failed for one it answered with an error,
    so an answered attempt only matches a row of the same kind of answer; an unanswered one ('unknown')
    matches any row, an unsent one ('none') none."""
    if outcome == "none":
        return False
    if outcome not in ("ok", "failed") or result not in ("ok", "failed", "refused"):
        return True
    return (outcome == "ok") == (result == "ok")


def _caused(kind, etype, row_kind, f):
    """PURE. Can a request row (etype, its _row_kind, fields f) have caused a mode.change of this kind?
    A Manager on/off: a control.request of that direction (or of no logged direction). A pick
    (why 'pick'): an agent's pick (agent.request change-model/restart) or the owner's manual pick
    (control.request what 'manual')."""
    if kind == "pick":
        return row_kind == "pick" if etype == "agent.request" else f.get("what") == "manual"
    if etype != "control.request" or row_kind not in ("mgr_on", "mgr_off", "mgr"):
        return False
    return kind == "mgr" or row_kind in (kind, "mgr")


def _other_usage(events, att, hour):
    """PURE. Read BloomGauge's manager activity log rows (events: [{at, event, fields}]) against the
    queue's own 'attempt' rows (att; each may carry 'outcome', see _outcomes, else 'unknown').
    events None (log unreadable) -> (1, 1, None): one other-agent pick and one other-agent Manager
    change assumed.

    Request rows (agent.request, control.request; one own write logs one of each) are matched one to
    one, per row type: rows oldest first, each takes the NEWEST unused own attempt that could have
    caused it (_fits) whose ts lies in [at - MATCH_LATE_S, at + MATCH_EARLY_S] and whose outcome agrees
    with the row's result (_agrees: a write BloomGauge refused or failed never takes an ok row, so
    someone else's change made just after it is not taken for the queue's). An unmatched
    agent.request newer than hour whose what is change-model/restart (a pick) or manager/follow (a
    Manager change) is another agent's and counts. An unmatched control.request that changes the
    Manager or the model (UI_CHANGE_WHATS, result ok) with no agent.request within PAIR_S is a UI
    click: a manual change.

    A mode.change whose why is not BloomGauge's own (APP_WHYS) is chained to the request that caused
    it (2026-11-02 simulated run, test_bgc_sim.py: a failed own Manager-off took another agent's
    Manager-off for its own): the oldest unclaimed request row that can have caused it (_caused),
    answered ok (else failed: a change can land before an error), at or after the previous
    mode.change and within [at - MATCH_LATE_S, at + MATCH_EARLY_S]. The queue's own when that row is
    matched to an own attempt, else manual (another agent's change is manual too: the queue yields
    rather than spend its 2-an-hour picks fighting it). A Manager-on with no such row may be a turn-on
    BloomGauge finished late (it waits for readiness, up to TURN_ON_WAIT_S; 2026-11-03): then the
    NEWEST unclaimed ok row that can have caused it, at or after the previous mode.change and within
    [at - TURN_ON_WAIT_S, at - MATCH_LATE_S) (a newer request replaces a pending one, so a manual 'on'
    clicked after the queue's still counts as manual). With no such row it falls back to the newest
    unused own attempt that fits in that window (any outcome but 'none'), for a Manager-on also an
    own Manager-on answered ok at or after the previous mode.change and up to TURN_ON_WAIT_S earlier;
    none -> manual.
    manual_at is the newest manual 'at' (any age), None when there is none. Rows with a non-numeric
    'at' are skipped.
    -> (other_picks, other_mgr, manual_at)."""
    if events is None:
        return (1, 1, None)
    own = []
    for a in att or []:
        ts = _num(a.get("ts")) if isinstance(a, dict) else None
        if ts is not None:
            own.append((ts, a.get("action"), a.get("outcome") or "unknown"))
    reqs = {"agent.request": [], "control.request": []}
    flips = []
    for e in events:
        if not isinstance(e, dict) or e.get("event") not in ("agent.request", "control.request", "mode.change"):
            continue
        at = _num(e.get("at"))
        f = e.get("fields") if isinstance(e.get("fields"), dict) else {}
        kind = _row_kind(e["event"], f)
        if at is None:
            continue
        if e["event"] == "mode.change":
            flips.append((at, kind))   # kind None: BloomGauge's own change (it still moves the Manager)
        elif kind is not None:
            reqs[e["event"]].append({"at": at, "kind": kind, "f": f, "etype": e["event"], "own": False, "claimed": False})
    agent_at = [r["at"] for r in reqs["agent.request"]]
    other_picks = other_mgr = 0
    manual = []
    for etype, rs in reqs.items():
        rs.sort(key=lambda r: r["at"])
        used = set()
        for r in rs:
            at, kind, f = r["at"], r["kind"], r["f"]
            best = None
            for i, (ts, action, outcome) in enumerate(own):
                if (i in used or not _fits(kind, action) or not (at - MATCH_LATE_S <= ts <= at + MATCH_EARLY_S)
                        or not _agrees(outcome, f.get("result"))):
                    continue
                if best is None or ts > own[best][0]:
                    best = i
            if best is not None:
                used.add(best)
                r["own"] = True
            elif etype == "agent.request":
                if at > hour:
                    if kind == "pick":
                        other_picks += 1
                    else:
                        other_mgr += 1
            elif (f.get("what") in UI_CHANGE_WHATS and f.get("result") in ("ok", None)
                    and not any(abs(at - x) <= PAIR_S for x in agent_at)):
                manual.append(at)
    allreqs = sorted(reqs["agent.request"] + reqs["control.request"], key=lambda r: r["at"])
    used = set()
    prev = None
    for at, kind in sorted(flips, key=lambda x: x[0]):
        lo = at - MATCH_LATE_S if prev is None else max(at - MATCH_LATE_S, prev)
        prev_at, prev = prev, at
        if kind is None:
            continue
        cause = None
        for ok in (True, False):
            for r in allreqs:
                if r["claimed"] or not (lo <= r["at"] <= at + MATCH_EARLY_S) or not _caused(kind, r["etype"], r["kind"], r["f"]):
                    continue
                if (r["f"].get("result") in ("ok", None)) if ok else (r["f"].get("result") == "failed"):
                    cause = r
                    break
            if cause is not None:
                break
        if cause is None and kind == "mgr_on":
            # a turn-on BloomGauge finished late (its readiness wait: up to TURN_ON_WAIT_S after the
            # request) logs its mode.change past MATCH_LATE_S: the newest unclaimed Manager-on answered ok
            # since the previous mode.change caused it (a newer request replaces a pending one)
            wide = at - TURN_ON_WAIT_S if prev_at is None else max(at - TURN_ON_WAIT_S, prev_at)
            for r in allreqs:
                if (not r["claimed"] and wide <= r["at"] < lo and r["f"].get("result") in ("ok", None)
                        and _caused(kind, r["etype"], r["kind"], r["f"])):
                    cause = r
        if cause is not None:
            cause["claimed"] = True
            if not cause["own"]:
                manual.append(at)
            continue
        best = None
        for i, (ts, action, outcome) in enumerate(own):
            if i in used or outcome == "none" or not _fits(kind, action):
                continue
            if not (at - MATCH_LATE_S <= ts <= at + MATCH_EARLY_S):
                # the late turn-on with its request row gone from the log: an own Manager-on answered ok
                if not (kind == "mgr_on" and outcome == "ok" and ts < at - MATCH_LATE_S
                        and ts >= at - TURN_ON_WAIT_S and (prev_at is None or ts >= prev_at)):
                    continue
            if best is None or ts > own[best][0]:
                best = i
        if best is not None:
            used.add(best)
        else:
            manual.append(at)
    return (other_picks, other_mgr, max(manual) if manual else None)


def _outcomes(ledger, own):
    """PURE. The own attempt rows (own), each copied with 'outcome': what BloomGauge saw of that write,
    from its result row (same ts and action, phase 'result', mode 'on'; paired in ledger order):
    'ok' (answered ok), 'failed' (answered with an error: its log rows say refused or failed),
    'none' (never sent: the client refused the arguments, NOT_SENT_RE), 'unknown' (no answer,
    NO_ANSWER_RE: refused connection or a timeout, so it may have run; or no result row)."""
    results = {}
    for r in ledger or []:
        if isinstance(r, dict) and r.get("phase") == "result" and r.get("mode") == "on":
            results.setdefault((_num(r.get("ts")), r.get("action")), []).append(r)
    out = []
    for a in own:
        q = results.get((_num(a.get("ts")), a.get("action")))
        r = q.pop(0) if q else None
        if r is None:
            o = "unknown"
        elif r.get("result") == "ok":
            o = "ok"
        else:
            err = str(r.get("err") or "")
            o = "none" if NOT_SENT_RE.match(err) else ("unknown" if NO_ANSWER_RE.search(err) else "failed")
        out.append(dict(a, outcome=o))
    return out


def _settled(ledger):
    """PURE. How BloomGauge settled the queue's accepted Manager-ons: {attempt ts: (status, detail)} from
    the phase OUTCOME_PHASE rows (mode 'on'; the newest row per attempt wins)."""
    out = {}
    for r in ledger or []:
        if isinstance(r, dict) and r.get("phase") == OUTCOME_PHASE and r.get("mode") == "on":
            at = _num(r.get("attempt_ts"))
            if at is not None:
                out[at] = (str(r.get("status") or "unknown"), str(r.get("detail") or ""))
    return out


def turn_on_err(status, detail):
    """PURE. The failure text of a turn-on BloomGauge answered but did not complete (TURN_ON_ERR prefix)."""
    return (TURN_ON_ERR + str(status) + (": " + " ".join(str(detail).split()) if detail else ""))[:160]


def _turned_on(r, settled):
    """PURE. False when result row r is an accepted Manager-on that BloomGauge settled as not turned on
    (an outcome row whose status is not in TURN_ON_OK); True otherwise (still pending counts as done)."""
    if r.get("action") != "mgr_on":
        return True
    out = settled.get(_num(r.get("ts")))
    return out is None or out[0] in TURN_ON_OK


def _fail_run(ledger, actions):
    """PURE. The current run of the queue's failed own writes of these actions: [(ts, err)], newest
    first, from the newest phase 'result' rows (mode 'on', ordered by ts) that failed in a row; []
    when the newest result was ok or there is none. An accepted Manager-on that BloomGauge then blocked
    (or cancelled, or never completed: _settled) failed too, its err turn_on_err(status, detail)."""
    settled = _settled(ledger)
    res = []
    for i, r in enumerate(ledger or []):
        if not isinstance(r, dict) or r.get("phase") != "result" or r.get("mode") != "on" or r.get("action") not in actions:
            continue
        ts = _num(r.get("ts"))
        if ts is not None:
            res.append((ts, i, r))
    res.sort(key=lambda x: (x[0], x[1]))
    run = []
    for ts, _i, r in reversed(res):
        if r.get("result") == "ok" and not _turned_on(r, settled):
            run.append((ts, turn_on_err(*settled[ts])))
            continue
        if r.get("result") != "failed":
            break
        run.append((ts, str(r.get("err") or "")))
    return run


def _fail_backoff(ledger, actions, table):
    """PURE. (streak, retry_at, err) for the queue's own writes of these actions. streak = how many
    of the newest phase 'result' rows (mode 'on', ordered by ts) failed in a row (_fail_run); retry_at = the
    newest failure's ts + table[min(streak, len(table)) - 1], and at least RATE_LIMITED_MIN_S after
    it when the error was BloomGauge's hourly limit (429); err = that failure's text.
    (0, 0.0, None) when the newest result was ok or there is none."""
    run = _fail_run(ledger, actions)
    if not run:
        return 0, 0.0, None
    streak, (last_ts, err) = len(run), run[0]
    wait = table[min(streak, len(table)) - 1]
    if RATE_LIMITED_RE.search(err):
        wait = max(wait, RATE_LIMITED_MIN_S)
    if err.startswith(TURN_ON_ERR):
        # a blocked turn-on: BloomGauge asks for a look before a retry, so space retries out (from the write's ts)
        wait = max(wait, TURN_ON_BACKOFF_S[min(streak, len(TURN_ON_BACKOFF_S)) - 1])
    return streak, last_ts + wait, err


def _alerted(ledger, actions, kind):
    """PURE. True when the current run of failed writes of these actions (_fail_run) already has a
    phase ALERT_PHASE row of this kind (pick|manager) at or after its first failure: the owner was
    alerted to this streak, by the daemon or by the stale-lease guard (2026-11-02)."""
    run = _fail_run(ledger, actions)
    if not run:
        return False
    first = run[-1][0]
    for r in ledger or []:
        if isinstance(r, dict) and r.get("phase") == ALERT_PHASE and r.get("kind") == kind:
            ts = _num(r.get("ts"))
            if ts is not None and ts >= first:
                return True
    return False


def _kept_manual_at(ledger):
    """PURE. The newest manual_at the ledger keeps (phase MANUAL_PHASE rows, any mode), None when
    there is none."""
    kept = None
    for r in ledger or []:
        if isinstance(r, dict) and r.get("phase") == MANUAL_PHASE:
            at = _num(r.get("manual_at"))
            if at is not None and (kept is None or at > kept):
                kept = at
    return kept


def _own_off_at(ledger):
    """PURE. The ts of the queue's newest SUCCESSFUL Manager change (phase 'result', mode 'on',
    result ok, action in MGR_ACTIONS; newest by ts, then ledger order) when it was a Manager-off,
    else None. A failed change is skipped: it did not move the Manager (as far as the queue knows),
    and skipping it lets a failed turn-on be retried. So is an accepted Manager-on that BloomGauge
    settled as not turned on (_turned_on: blocked, cancelled, never completed)."""
    settled = _settled(ledger)
    best = None
    for i, r in enumerate(ledger or []):
        if (not isinstance(r, dict) or r.get("phase") != "result" or r.get("mode") != "on"
                or r.get("action") not in MGR_ACTIONS or r.get("result") != "ok" or not _turned_on(r, settled)):
            continue
        ts = _num(r.get("ts"))
        if ts is not None and (best is None or (ts, i) >= best[:2]):
            best = (ts, i, r.get("action"))
    return best[0] if best is not None and best[2] == "mgr_off" else None


def _turn_on(ledger):
    """PURE. The queue's accepted Manager-on that BloomGauge has not settled yet: {ts, op_id} when the
    newest own Manager-change result row (phase 'result', mode 'on', action in MGR_ACTIONS; newest by
    ts, then ledger order) is an ok mgr_on carrying an op_id (its operation's id, from the POST's answer)
    with no outcome row (_settled) for its ts; else None. An older turn-on no longer matters once a newer
    Manager change was made."""
    best = None
    for i, r in enumerate(ledger or []):
        if (not isinstance(r, dict) or r.get("phase") != "result" or r.get("mode") != "on"
                or r.get("action") not in MGR_ACTIONS):
            continue
        ts = _num(r.get("ts"))
        if ts is not None and (best is None or (ts, i) >= best[:2]):
            best = (ts, i, r)
    if best is None:
        return None
    ts, _i, r = best
    op_id = r.get("op_id")
    if r.get("action") != "mgr_on" or r.get("result") != "ok" or not isinstance(op_id, str) or not op_id:
        return None
    if ts in _settled(ledger):
        return None
    return {"ts": ts, "op_id": op_id}


def turn_on_outcome(pending, view, now, manual_at=None):
    """PURE. How BloomGauge settled the queue's accepted Manager-on (pending: _turn_on's {ts, op_id}),
    read from its operation (view: read_view's op_id/op_status/op_detail, manager_on, switching).
    manual_at: the newest manual Manager change (budget's), to tell the owner's cancel from a failure.
    -> (status, detail); status None while it is still under way (or there is nothing to settle):
    - its operation (op_id matches): running (OP_RUNNING, or a status this code does not know) -> None
      until TURN_ON_WAIT_S after the write, then 'timeout'; 'active' -> 'active'; 'blocked' -> 'blocked',
      detail the operation's text; 'cancelled' -> 'overridden' when a manual change came after the
      request (MATCH_EARLY_S slack), else 'cancelled' (detail the operation's text);
    - another operation, or none (BloomGauge lost it, or a newer one replaced it): while BloomGauge is
      mid-change -> None (then 'timeout' as above); Manager on -> 'superseded'; a manual change after
      the request -> 'overridden'; else 'lost'.
    TURN_ON_OK ('active', 'superseded', 'overridden') is not a failed change; the rest count as one."""
    if not isinstance(pending, dict) or not isinstance(view, dict):
        return None, ""
    ts = _num(pending.get("ts"))
    age = now - ts if ts is not None else 0.0
    late = ("timeout", f"BloomGauge was still turning the Manager on {int(age)} s after the request")
    m = _num(manual_at)
    manual = m is not None and ts is not None and m >= ts - MATCH_EARLY_S
    overridden = ("overridden", "a manual Manager change after the request")
    if view.get("op_id") == pending.get("op_id"):
        st = view.get("op_status")
        if st == "active":
            return "active", ""
        if st == "cancelled" and manual:
            return overridden
        if st in ("blocked", "cancelled"):
            return st, view.get("op_detail") or ""
        return (None, "") if age < TURN_ON_WAIT_S else late
    if view.get("switching"):
        return (None, "") if age < TURN_ON_WAIT_S else late
    if view.get("manager_on"):
        return "superseded", "another operation turned the Manager on"
    if manual:
        return overridden
    return "lost", "BloomGauge no longer shows this turn-on and the Manager is Manual"


def budget(ledger, events, now, cfg):
    """PURE. What the queue may still spend this hour, the last manual Manager change, and the
    failure backoff.
    ledger: the queue's own rows; only phase 'attempt' rows of mode 'on' count, whatever their
    result (BloomGauge counts a change before it runs, so a refusal burns budget); own picks are
    action 'pick', own Manager changes mgr_off/mgr_on, both only when ts is within the last hour.
    Other agents' use and manual_at come from _other_usage(events, _outcomes(ledger, attempts), now - HOUR).
    BloomGauge's own automatic switches never count.
    picks_left = max(0, min(cfg picks_per_h (default 2) - own picks, APP_PICKS_PER_H - picks_used));
    mgr_left = max(0, min(cfg mgr_per_h (default 4) - own Manager changes, APP_MGR_PER_H - mgr_used));
    *_used = own + other. pick_fails/pick_retry_at and mgr_fails/mgr_retry_at: _fail_backoff of the
    own picks (PICK_FAIL_BACKOFF_S) and Manager changes (MGR_FAIL_BACKOFF_S); a retry waits until
    *_retry_at. log_ok: the activity log was read (events not None), so manual_at can be trusted.
    manual_at is the newer of the log's (manual_log_at) and the ledger's kept one (manual_kept_at,
    _kept_manual_at): the tick writes a manual row whenever manual_log_at is newer, so a manual change
    still counts after it scrolls out of the log. leak: the queue left the Manager off itself, the
    only case an ownerless turn-on may fix (own_off_at = _own_off_at(ledger) is set and manual_at is
    None or not after it).
    -> {picks_left, mgr_left, picks_used, mgr_used, manual_at, manual_log_at, manual_kept_at,
    pick_fails, mgr_fails, pick_alerted, mgr_alerted, pick_retry_at, mgr_retry_at, log_ok, own_off_at, leak,
    turn_on}. *_alerted: the current failure streak of that kind already raised its alert (_alerted).
    turn_on: the queue's accepted Manager-on BloomGauge has not settled yet (_turn_on), None when none."""
    own = [r for r in (ledger or []) if isinstance(r, dict) and r.get("phase") == "attempt" and r.get("mode") == "on"]
    hour = now - HOUR
    own_p = sum(1 for r in own if r.get("action") == "pick" and (_num(r.get("ts")) or 0.0) > hour)
    own_m = sum(1 for r in own if r.get("action") in MGR_ACTIONS and (_num(r.get("ts")) or 0.0) > hour)
    other_p, other_m, manual_log_at = _other_usage(events, _outcomes(ledger, own), hour)
    manual_kept_at = _kept_manual_at(ledger)
    manual_at = max((x for x in (manual_log_at, manual_kept_at) if x is not None), default=None)
    own_off_at = _own_off_at(ledger)
    leak = own_off_at is not None and (manual_at is None or manual_at <= own_off_at)
    picks_used = own_p + other_p
    mgr_used = own_m + other_m
    picks_per_h = cfg.get("picks_per_h", 2)
    mgr_per_h = cfg.get("mgr_per_h", 4)
    picks_left = max(0, min(picks_per_h - own_p, APP_PICKS_PER_H - picks_used))
    mgr_left = max(0, min(mgr_per_h - own_m, APP_MGR_PER_H - mgr_used))
    pick_fails, pick_retry_at, _pe = _fail_backoff(ledger, ("pick",), PICK_FAIL_BACKOFF_S)
    mgr_fails, mgr_retry_at, _me = _fail_backoff(ledger, MGR_ACTIONS, MGR_FAIL_BACKOFF_S)
    return {"picks_left": picks_left, "mgr_left": mgr_left, "picks_used": picks_used, "mgr_used": mgr_used,
            "manual_at": manual_at, "manual_log_at": manual_log_at, "manual_kept_at": manual_kept_at,
            "pick_fails": pick_fails, "mgr_fails": mgr_fails,
            "pick_alerted": _alerted(ledger, ("pick",), "pick"), "mgr_alerted": _alerted(ledger, MGR_ACTIONS, "manager"),
            "pick_retry_at": pick_retry_at, "mgr_retry_at": mgr_retry_at, "log_ok": events is not None,
            "own_off_at": own_off_at, "leak": leak, "turn_on": _turn_on(ledger)}


def _pick_step(lease, view, target, now, bud):
    """PURE. The queue holds the lease and wants model target served alone. Wait (lease returned
    unchanged) when lease 'picked' == target and 'picked_at' is less than an hour (HOUR) before now
    ("pick: already sent for this model this hour"), when bud 'log_ok' is not True ("pick: activity
    log unreadable": a manual pick could not be seen), when view 'switching' ("pick: BloomGauge is
    mid-change"), when bud 'picks_left' <= 0 ("pick: pick budget used this hour"), or before bud
    'pick_retry_at' after a failed pick ("pick: last pick failed; backing off"). Otherwise
    pick: a NEW lease dict (input not mutated) with picked=target, picked_at=now, and the decision
    {action 'pick', model target, reason "hold: target is not the sole served model"}.
    Every decision carries model=target. -> (decision, lease)."""
    lease = lease if isinstance(lease, dict) else {}
    view = view if isinstance(view, dict) else {}
    bud = bud if isinstance(bud, dict) else {}
    picked_at = _num(lease.get("picked_at"))
    if lease.get("picked") == target and picked_at is not None and (now - picked_at) < HOUR:
        return {"action": "wait", "model": target,
                "reason": "pick: already sent for this model this hour"}, lease
    if bud.get("log_ok") is not True:
        return {"action": "wait", "model": target,
                "reason": "pick: activity log unreadable"}, lease
    if view.get("switching"):
        return {"action": "wait", "model": target,
                "reason": "pick: BloomGauge is mid-change"}, dict(lease)
    if bud.get("picks_left", 0) <= 0:
        return {"action": "wait", "model": target,
                "reason": "pick: pick budget used this hour"}, lease
    if now < (_num(bud.get("pick_retry_at")) or 0.0):
        return {"action": "wait", "model": target,
                "reason": "pick: last pick failed; backing off"}, lease
    new_lease = dict(lease)
    new_lease["picked"] = target
    new_lease["picked_at"] = now
    return {"action": "pick", "model": target,
            "reason": "hold: target is not the sole served model"}, new_lease


def _unseen(lease):
    """PURE. lease without mgr_on_seen_at: a copy when it had one, else lease itself."""
    if isinstance(lease, dict) and "mgr_on_seen_at" in lease:
        lease = dict(lease)
        lease.pop("mgr_on_seen_at")
    return lease


def _reassert_step(lease, view, target, now, bud):
    """PURE. The queue holds the lease and has work, but the Manager is on. A manual change never
    gets here (decide yields first), so BloomGauge turned it back on itself (an APP_WHYS
    mode.change, e.g. 'recovery' when a timed Manual run ends) or a hand-back the queue counted as
    failed landed after all. Turn it off again, with the acquire's waits. Wait:
    - within REASSERT_GRACE_S of the lease's since/mgr_off_at (the queue's own change may not show);
    - until the Manager has read on for REASSERT_CONFIRM_S: the lease's mgr_on_seen_at is set on the
      first such tick, and again when its heartbeat (the last tick) is more than REASSERT_CONFIRM_S
      old; decide drops it whenever the Manager reads off or BloomGauge cannot be read. By then the
      activity-log row of a manual change has landed, and decide yields instead;
    - while the activity log was unreadable (bud log_ok not True: a manual change cannot be told
      apart), while BloomGauge is mid-change, while bud mgr_left is REASSERT_KEEP_MGR or less (the
      hand-back always has a change left), and before bud mgr_retry_at.
    The write sets mgr_off_at=now and prior_manager_on=True (the Manager was on, so the hand-back
    turns it on again) and drops mgr_on_seen_at. Wait decisions keep the lease (a first sighting
    adds mgr_on_seen_at). -> (decision, lease)."""
    last = max(_num(lease.get("mgr_off_at")) or 0.0, _num(lease.get("since")) or 0.0)
    seen = _num(lease.get("mgr_on_seen_at"))
    beat = _num(lease.get("heartbeat"))
    wait = None
    if now - last < REASSERT_GRACE_S:
        wait = "hold: Manager on; the queue's own change may not show yet"
    elif seen is None or seen > now or (beat is not None and now - beat > REASSERT_CONFIRM_S):
        lease = dict(lease, mgr_on_seen_at=now)
        wait = "hold: Manager on; confirming it stays on"
    elif now - seen < REASSERT_CONFIRM_S:
        wait = "hold: Manager on; confirming it stays on"
    elif bud.get("log_ok") is not True:
        wait = "hold: Manager on; activity log unreadable"
    elif view.get("switching"):
        wait = "hold: Manager on; BloomGauge is mid-change"
    elif bud.get("mgr_left", 0) <= REASSERT_KEEP_MGR:
        wait = "hold: Manager on; Manager budget used this hour (one kept for the hand-back)"
    elif now < (_num(bud.get("mgr_retry_at")) or 0.0):
        wait = "hold: Manager on; last Manager change failed; backing off"
    if wait:
        return {"action": "wait", "model": target, "reason": wait}, lease
    new = dict(lease, mgr_off_at=now, prior_manager_on=True)
    new.pop("mgr_on_seen_at", None)
    return {"action": "mgr_off", "model": target, "reason": "hold: Manager came back on; Manager off"}, new


def _yielded(lease, now, cfg):
    """PURE. True while a lease kept on a yield (yielded_at, the manual change's time) is within
    cfg yield_s (default 7200) of it: the queue makes no change then."""
    y = _num(lease.get("yielded_at")) if isinstance(lease, dict) else None
    return y is not None and now - y < cfg.get("yield_s", 7200)


def busy_step(lease, view, work, now, cfg, bud):
    """PURE. Queue work exists. IDLE: wait while the activity log is unreadable (bud log_ok not True:
    a recent manual change could not be seen, and a lease taken now would be handed back over it),
    back off for cfg yield_s after a manual Manager change, else take the lease (Manager off when it
    is on, budget, no switch in progress and no failure backoff (bud mgr_retry_at) permitting).
    HELD/DRAIN: stay or go back to HELD (new work cancels a pending hand-back). A lease kept on a
    yield (_yielded) makes no change: Manager off -> hold with no pick; Manager on -> released (the
    owner's state stands, nothing to hand back). Otherwise, when the Manager is on anyway, turn it
    off again (_reassert_step, 2026-11-02); else, with cfg allow_picks, pick the target model when
    it is not the sole served model (once per model per hour, within the pick budget).
    -> (decision, lease)."""
    target = work.get("target")
    # No lease: IDLE case
    if lease is None:
        if bud.get("log_ok") is not True:
            return {"action": "wait", "model": None, "reason": "acquire: activity log unreadable"}, None
        manual_at = bud.get("manual_at")
        if manual_at is not None and now - manual_at < cfg.get("yield_s", 7200):
            return {"action": "wait", "model": None, "reason": "backoff: manual Manager change less than 2h ago"}, None
        new = {"state": "HELD", "since": now, "target": target, "prior_manager_on": bool(view["manager_on"])}
        if not view["manager_on"]:
            return {"action": "none", "model": target, "reason": "acquire: Manager already off; holding"}, new
        if view["switching"]:
            return {"action": "wait", "model": None, "reason": "acquire: BloomGauge is mid-change"}, None
        if bud.get("mgr_left", 0) <= 0:
            return {"action": "wait", "model": None, "reason": "acquire: Manager budget used this hour"}, None
        if now < (_num(bud.get("mgr_retry_at")) or 0.0):
            return {"action": "wait", "model": None, "reason": "acquire: last Manager change failed; backing off"}, None
        new["mgr_off_at"] = now
        return {"action": "mgr_off", "model": target, "reason": "acquire: queue work; Manager off"}, new
    # With a lease: work on a copy
    lease = dict(lease)
    reason = "held"
    if lease["state"] != "HELD":
        lease["state"] = "HELD"
        lease.pop("idle_since", None)
        reason = "drain cancelled: new queue work"
    if target is not None:
        lease["target"] = target
    if _yielded(lease, now, cfg):
        if view["manager_on"]:
            return {"action": "none", "model": target, "reason": "yielded: Manager on again; lease released"}, None
        return {"action": "none", "model": target, "reason": "held: yielded to a manual change; no changes for yield_s"}, lease
    if view["manager_on"]:
        return _reassert_step(lease, view, target, now, bud)
    if cfg.get("allow_picks") and target:
        if not (view["current"] == target and view["serving"] == [target]):
            return _pick_step(lease, view, target, now, bud)
    return {"action": "none", "model": target, "reason": reason}, lease


def idle_step(lease, view, now, cfg, bud):
    """PURE. No queue work. HELD -> DRAIN (idle timer starts). DRAIN/HANDBACK: keep the lease
    while the queue has been idle less than cfg idle_s (default 900). Claude sessions no longer delay it (queue idle only). Then hand back: Manager on when it was on before or cfg handback is
    'always'; never within cfg yield_s of the manual change a kept lease yielded to (_yielded),
    while the activity log is unreadable (bud log_ok not True: a manual change
    since the lease was taken could not be seen), while BloomGauge is mid-change, while it cannot
    enable (blocker), with no Manager budget left, or before bud mgr_retry_at after a failed
    Manager change (state HANDBACK, retried next tick). Release without a write when the
    Manager is already on or was off before (handback 'prior'). New queue work cancels a pending
    hand-back (busy_step).
    The Manager-on keeps the lease (HANDBACK): BloomGauge answers it at once and turns the Manager on
    afterwards, or blocks it (2026-11-03). While that turn-on is under way (bud turn_on: the tick settles
    it first, bloomgauge_tick.settle) the lease waits; once it went active the Manager reads on and the
    lease is released; a blocked one counts as a failed change (the backoff, then a retry).
    -> (decision, lease)."""
    if lease is None:
        return {"action": "none", "model": None, "reason": "idle"}, None
    state = lease.get("state")
    if state == "HELD":
        lease = dict(lease)
        lease["state"] = "DRAIN"
        lease["idle_since"] = now
        return {"action": "none", "model": None, "reason": "drain: queue idle, hand-back timer started"}, lease
    # DRAIN or HANDBACK: check idle timer
    idle_s = cfg.get("idle_s", 900)
    if now - lease.get("idle_since", 0) < idle_s:
        return {"action": "none", "model": None, "reason": "drain: queue idle less than idle_s"}, lease
    # Handback decision
    prior_on = lease.get("prior_manager_on")
    handback = cfg.get("handback", "always")
    if not (prior_on or handback == "always"):
        return {"action": "none", "model": None, "reason": "handback: Manager was off before; released"}, None
    if bud.get("turn_on") is not None:
        # the queue's own turn-on is under way (its operation reads the Manager as on meanwhile)
        return {"action": "wait", "model": None, "reason": "handback: BloomGauge is turning the Manager on"}, dict(lease, state="HANDBACK")
    # Manager already on -> release without write
    if view.get("manager_on"):
        return {"action": "none", "model": None, "reason": "handback: Manager already on; released"}, None
    # Set HANDBACK state
    lease = dict(lease)
    lease["state"] = "HANDBACK"
    if _yielded(lease, now, cfg):
        return {"action": "wait", "model": None, "reason": "handback: manual change less than yield_s ago"}, lease
    if bud.get("log_ok") is not True:
        return {"action": "wait", "model": None, "reason": "handback: activity log unreadable"}, lease
    # Check mid-change
    if view.get("switching"):
        return {"action": "wait", "model": None, "reason": "handback: BloomGauge is mid-change"}, lease
    # Check can_enable
    if not view.get("can_enable"):
        blocker = view.get("blocker") or "Manager cannot be enabled"
        return {"action": "wait", "model": None, "reason": f"handback blocked: {blocker}"}, lease
    # Check budget
    if bud.get("mgr_left", 0) <= 0:
        return {"action": "wait", "model": None, "reason": "handback: Manager budget used this hour"}, lease
    if now < (_num(bud.get("mgr_retry_at")) or 0.0):
        return {"action": "wait", "model": None, "reason": "handback: last Manager change failed; backing off"}, lease
    # All clear: turn Manager on; the lease stays until BloomGauge has turned it on
    return {"action": "mgr_on", "model": None, "reason": "handback: queue idle for idle_s; Manager on"}, lease


def _yield_floor(lease, now, cfg):
    """PURE. A manual change newer than this is one the lease yields to: the later of the lease's since
    and yielded_at. A corrupt lease (bloomgauge_control.lease_load) has lost both, and its since (the
    file's mtime) can be later than a manual change it had yielded to. busy_step takes no lease within
    yield_s of a manual change, so any manual change in the last yield_s came while the lease was
    held: a corrupt lease's since counts as no later than now - yield_s (2026-11-02, from the
    simulated run: a corrupt lease let the queue pick over the owner's pick)."""
    since = _num(lease.get("since")) or 0.0
    if lease.get("corrupt"):
        since = min(since, now - cfg.get("yield_s", 7200))
    return max(since, _num(lease.get("yielded_at")) or 0.0)


def decide(lease, view, work, now, cfg, bud):
    """PURE. One control step. App down (view None) or agent changes off: wait, lease kept. A
    manual Manager change after the lease was taken (and after the yielded_at of a kept lease;
    _yield_floor, which also covers a corrupt lease):
    yield, nothing sent (never fight the owner). The lease is dropped, except when the queue had
    turned the Manager off (prior_manager_on) and it still reads off: then it is kept with
    yielded_at = the manual change's time (Fable review #8, 2026-11-02; busy_step/idle_step make no
    change for yield_s, then hand back as usual), so a late own row read as manual, or an owner
    who changed something else, never leaves the Manager off with nobody to turn it back on.
    Else busy_step when work['busy'], idle_step otherwise. The lease's
    mgr_on_seen_at (_reassert_step) is dropped whenever BloomGauge cannot be read, agent changes
    are off or the Manager reads off. -> (decision, lease)."""
    if view is None:
        return {"action": "wait", "model": None, "reason": "app-down: BloomGauge not answering"}, _unseen(lease)
    if not view.get("agent_on"):
        return {"action": "wait", "model": None, "reason": "control-unavailable: agent changes off"}, _unseen(lease)
    manual_at = _num(bud.get("manual_at"))
    if lease is not None and manual_at is not None and manual_at > _yield_floor(lease, now, cfg):
        if lease.get("prior_manager_on") and not view.get("manager_on"):
            kept = dict(lease, yielded_at=manual_at)
            kept.pop("mgr_on_seen_at", None)
            return {"action": "yield", "model": None, "reason": YIELD_KEEP_REASON}, kept
        return {"action": "yield", "model": None, "reason": YIELD_REASON}, None
    if lease is not None:
        lease = dict(lease)
        if not view.get("manager_on"):
            lease.pop("mgr_on_seen_at", None)
    if work.get("busy"):
        return busy_step(lease, view, work, now, cfg, bud)
    return idle_step(lease, view, now, cfg, bud)


def ownerless_step(view, idle_for, cfg, bud, now=None):
    """PURE. Returns {action, model, reason} for the no-lease, queue-idle case: turn a Manager that is
    off back on once the queue has been idle for cfg idle_s, ONLY when the queue left it off itself
    (bud leak, see budget: its newest successful Manager change was a Manager-off with no manual
    change after it; Fable review #2, 2026-11-02). An owner's off, however old, is never undone
    here, and after the turn-on succeeds the newest change is a Manager-on, so it never repeats.
    Then the same waits as a hand-back (activity log unreadable, mid-change, blocker, Manager
    budget, and, when now is given, the failure backoff)."""
    if not isinstance(view, dict) or not view.get("agent_on") or view.get("manager_on"):
        return {"action": "none", "model": None, "reason": "ownerless idle: nothing to do"}
    if idle_for < cfg.get("idle_s", 900):
        return {"action": "none", "model": None, "reason": "ownerless idle: queue idle less than idle_s"}
    if bud.get("turn_on") is not None:
        return {"action": "wait", "model": None, "reason": "ownerless idle: BloomGauge is turning the Manager on"}
    if not bud.get("leak"):
        return {"action": "none", "model": None, "reason": "ownerless idle: Manager off, not by the queue; left to its owner"}
    if bud.get("log_ok") is not True:
        return {"action": "wait", "model": None, "reason": "ownerless idle: activity log unreadable"}
    if view.get("switching"):
        return {"action": "wait", "model": None, "reason": "ownerless idle: BloomGauge is mid-change"}
    if not view.get("can_enable"):
        return {"action": "wait", "model": None, "reason": "ownerless idle blocked: " + str(view.get("blocker") or "Manager cannot be enabled")}
    if bud.get("mgr_left", 0) <= 0:
        return {"action": "wait", "model": None, "reason": "ownerless idle: Manager budget used this hour"}
    if now is not None and now < (_num(bud.get("mgr_retry_at")) or 0.0):
        return {"action": "wait", "model": None, "reason": "ownerless idle: last Manager change failed; backing off"}
    return {"action": "mgr_on", "model": None, "reason": "ownerless idle: manager off, queue idle >= idle_s -> manager on"}

