#!/usr/bin/env python3
"""handoff-autoclear.py -- keep the handoff panel near 0 with nobody reading it.

Flow per finished job on the panel (`handoff-emit.py --json` -> "complete"):
  1. HARD GUARDS (always first, never overridden by any rule or model answer):
       * awaiting_signoff                          -> keep
       * the job is still live in the queue (any non-terminal status, incl. needs_opus,
         escalated, blocked, held) or a live job uses its worktree -> keep
       * code_high findings / untrusted            -> keep
       * worktree hand-locked (.hand-harness)      -> keep
       * a landable deliverable (status done, gate PASS, files > 0): land/accept
         decisions belong to the auto-land agent  -> keep
  2. MECHANICAL RULES (no LLM):
       superseded  : a later job in the same worktree finished done/exit 0
       handled     : a self-heal ledger round/re-spec already acted on this job
       no-deliverable : a finished authoring round (auto-author-/auto-refine-) with no files
  2b. ITS LAUNCHER IS LIVE -> keep, no Sonnet: the queue stamped launched_by=<a Claude
     session's messaging socket>, the socket still exists and the job finished less than
     LAUNCHER_GRACE_S ago. queue-events-deliver.py hands that session the job's event (its
     hook: triage, then resolve + clear, which marks the row acted here too) and leaves
     another live session's jobs to it, so a Sonnet triage would repeat the session's.
     Past the grace, or once the session is gone, the job goes on to step 3.
  3. SONNET BATCH (`claude -p --model sonnet --tools ""`, headless, no tools) classifies
     the remainder as clear | keep | followup. Cached by hash of the digest (never
     re-asked), >= 20 min apart, <= 8 calls/day, <= 25 jobs per call. Its answers are
     re-checked against the guards; a malformed/unknown answer keeps the job.
  4. FOLLOW-UPS become queue jobs (`ollama-queue.py enqueue`, research kind, bundle
     handoff-followups, <= 3/day, label-deduped); the original is then cleared with the
     job id in the reason.
Clearing = `handoff-emit.py --acted <ids> --reason ...` (the existing separate tool).

Kill switch: file ~/.ollama-dispatch/handoff-autoclear/OFF or HANDOFF_AUTOCLEAR=off
(nothing acts). `--dry-run` prints the plan. `--auto` is what the watcher pass runs.
"""
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config
import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

BIN = Path.home() / "bin"
EMIT = BIN / "handoff-emit.py"
QUEUE = BIN / "ollama-queue.py"
STATE = queue_config.state_dir() / "ollama-queue-state.json"
LOGS = queue_config.state_dir() / "ollama-queue-logs"
HEAL_LEDGER = Path.home() / ".ollama-dispatch" / "escalations" / "self-heal.json"
WORK = Path.home() / ".ollama-dispatch" / "handoff-autoclear"
KILL = WORK / "OFF"
LLM_CACHE = WORK / "llm-cache.json"
LLM_STAMP = WORK / "llm-calls.json"
LOG = WORK / "decisions.jsonl"

LIVE = {"pending", "running", "queued", "paused", "held", "scheduled", "planned",
        "needs_opus", "escalated", "blocked", "enqueued"}
LLM_MIN_GAP_S = 20 * 60
LLM_MAX_PER_DAY = 8
LLM_BATCH = 25
FOLLOWUP_MAX_PER_DAY = 3
# 2026-11-05: the per-job hook runs this pass seconds after a job ends, ahead of the session that launched it, so all
# five Sonnet calls that day were on one live session's bench failures (two also queued research follow-ups) that the
# session was triaging anyway. A live launcher gets this long to act before the panel's own triage takes the job back.
LAUNCHER_GRACE_S = 3 * 3600
MERGE_WORDS = ("landed", "land in", "in main", "to main", "deployed", "shipped to", "merg")
PROMPT_VERSION = "hac-1"


def _load(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def _save(p, obj):
    p = Path(p)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=1))
    os.replace(tmp, p)


def killed(env=None, kill=None):
    env = os.environ if env is None else env
    return str(env.get("HANDOFF_AUTOCLEAR", "on")).lower() == "off" or Path(kill or KILL).exists()


def log(rec, path=None):
    try:
        p = Path(path or LOG)
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a") as f:
            f.write(json.dumps(dict(rec, ts=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))) + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- guards (pure)
def guard(row, live_by_id, live_cwds, handlocked=None):
    """Reason string if the row must NOT be cleared, else None."""
    if row.get("awaiting_signoff"):
        return "awaiting sign-off"
    jid = row.get("id")
    lj = live_by_id.get(jid)
    if lj is not None:
        return "still live in the queue (%s)" % lj.get("status")
    if (row.get("status") or "").lower() in LIVE:
        return "status %s is not terminal" % row.get("status")
    cwd = row.get("cwd")
    if cwd and live_cwds.get(cwd) and live_cwds[cwd] - {jid}:
        return "a live job still uses the worktree (%s)" % sorted(live_cwds[cwd] - {jid})[0]
    if row.get("code_high"):
        return "code-high findings"
    if row.get("untrusted"):
        return "untrusted"
    hl = handlocked if handlocked is not None else (bool(cwd) and Path(cwd, ".hand-harness").exists())
    if hl:
        return "worktree hand-locked"
    if (row.get("status") == "done" and (row.get("gate") or "") == "PASS"
            and int(row.get("files") or 0) > 0):
        return "landable deliverable (the auto-land agent owns land/accept)"
    return None


def live_maps(state_jobs):
    live_by_id, live_cwds = {}, {}
    for j in state_jobs or []:
        if (j.get("status") or "").lower() in LIVE and j.get("id"):
            live_by_id[j["id"]] = j
            if j.get("cwd"):
                live_cwds.setdefault(j["cwd"], set()).add(j["id"])
    return live_by_id, live_cwds


# ------------------------------------------------------------ mechanical rules
def _passed(rec):
    try:
        return rec.get("status") == "done" and int(rec.get("exit_code")) == 0
    except (TypeError, ValueError):
        return False


def mechanical(row, sidecars_by_cwd, heal_ledger):
    """(reason|None). PURE given its inputs. Only called for rows the guards allowed."""
    jid, cwd = row.get("id"), row.get("cwd")
    me_ts = (sidecars_by_cwd.get("__ts__") or {}).get(jid, 0)
    for sc in sidecars_by_cwd.get(cwd, []):
        if sc.get("id") != jid and _passed(sc) and (sc.get("_ts") or 0) >= me_ts:
            return "superseded: a later job %s in the same worktree finished done/exit 0" % sc["id"]
    jl = (heal_ledger or {}).get("jobs") or {}
    for key, ch in (jl.get("chains") or {}).items():
        for r in (ch.get("rounds") or []) + (ch.get("respecs") or []):
            if r.get("for") == jid:
                return "handled: self-heal already acted (%s -> %s)" % (
                    r.get("kind") or "round", r.get("job") or r.get("label"))
    lbl = str(row.get("label") or "")
    if (lbl.startswith(("auto-author-", "auto-refine-")) and row.get("status") == "done"
            and int(row.get("files") or 0) == 0):
        return "no deliverable: finished authoring round (the harness lives in its worktree)"
    if _TEST_DRAFT.fullmatch(lbl) and not (row.get("status") == "done" and (row.get("gate") or "") == "PASS"):
        # a mini-testdraft REJECT is a recorded measurement, not a stuck job: the runner already fed the check's findings
        # back as retries, and on a REJECT Claude owns the test. A research follow-up here re-diagnoses a settled verdict
        # on the serial Studio lane (2026-11-02: two such jobs, one built on a since-fixed gate false positive).
        return "test-draft reject: the draft check's verdict is the outcome; Claude owns the test on a REJECT"
    return None


_TEST_DRAFT = re.compile(r".+-test-draft(?:-r\d+)?")   # mini-testdraft's label convention: <area>-test-draft[-rN]


def _sidecar(row, sidecars_by_cwd):
    return next((s for s in sidecars_by_cwd.get(row.get("cwd"), []) if s.get("id") == row.get("id")), None)


def launcher_waiting(sidecar, now=None, exists=os.path.exists):
    """(reason|None) for step 2b: the job's launching Claude session is live and the job is younger than LAUNCHER_GRACE_S,
    so that session triages it. Reads the job's .done.json sidecar: launched_by (the socket the queue stamped) and _ts (the
    sidecar's mtime, i.e. when the job finished). No stamp, no finish time or an unreadable one -> None (step 3 as before)."""
    sc = sidecar or {}
    sock = str(sc.get("launched_by") or "").strip()
    if not sock:
        return None
    try:
        fin = float(sc.get("_ts") or 0)
    except (TypeError, ValueError):
        return None
    age = (time.time() if now is None else now) - fin
    if fin <= 0 or not 0 <= age < LAUNCHER_GRACE_S or not exists(sock):
        return None
    return "launcher live: the session at %s launched it (finished %dm ago) and triages it itself; grace %dh" % (
        sock, age // 60, LAUNCHER_GRACE_S // 3600)


def sidecars_index(logs=None):
    out, ts = {}, {}
    d = Path(logs or LOGS)
    for base in (d, d / "archive"):
        try:
            files = list(base.glob("*.done.json"))
        except OSError:
            continue
        for f in files:
            rec = _load(f)
            if not isinstance(rec, dict) or not rec.get("id"):
                continue
            try:
                rec["_ts"] = f.stat().st_mtime
            except OSError:
                rec["_ts"] = 0
            ts[rec["id"]] = rec["_ts"]
            out.setdefault(rec.get("cwd"), []).append(rec)
    out["__ts__"] = ts
    return out


# ----------------------------------------------------------------- LLM (Sonnet)
SYSTEM = """You triage FINISHED local-model dispatch jobs on a handoff panel. For each job
decide ONE of:
  clear    -- routine/obsolete/superseded; nothing for a human or the pipeline to do
  keep     -- anything unclear, a warning, a failure with no visible follow-up, or you are unsure
  followup -- real work remains that nothing else will do; give a one-paragraph self-contained
              "task" for a read-only research/diagnosis job
Rules: when unsure choose keep. Never choose clear for a job with a sign-off, a live
continuation or an unlanded deliverable. Answer ONLY with a JSON array, one object per
job: [{"id": "...", "decision": "clear|keep|followup", "reason": "<=160 chars", "task": "(followup only)"}]"""


try:
    from docsum_private import is_private_label
except ImportError:  # script copied elsewhere (tests): same definition, kept in step by test_docsum_ui_consumers
    import re as _re_dp

    def is_private_label(label):
        return _re_dp.fullmatch(r"docsum-[0-9a-f]{8}", str(label or "")) is not None


def is_private(row):   # private-document jobs (docsum-<8hex>): never digested, classified by claude, or followed up
    return is_private_label((row or {}).get("label"))


def digest(row, state_job, sidecar, page_head):
    sj = state_job or {}
    sc = sidecar or {}
    return {"id": row["id"], "label": row.get("label"), "status": row.get("status"),
            "gate": row.get("gate"), "files": row.get("files"),
            "failure_class": sj.get("failure_class") or sc.get("failure_class"),
            "failure_detail": str(sj.get("failure_detail") or sc.get("failure_detail") or "")[:200],
            "terminal_reason": sj.get("terminal_reason") or sc.get("terminal_reason"),
            "report_head": page_head[:700]}


def llm_hash(digests):
    return hashlib.sha256((PROMPT_VERSION + "\0" + json.dumps(digests, sort_keys=True)).encode()).hexdigest()[:24]


def run_claude(prompt, timeout=420, model="sonnet"):
    env = dict(os.environ, CLAUDE_HEADLESS_DIAGNOSIS="1")
    try:
        r = subprocess.run(["claude", "-p", "--model", model, "--tools", "", "--no-session-persistence"],
                           input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout or ""
    except (OSError, subprocess.TimeoutExpired):
        return 127, ""


def parse_answers(text, allowed_ids):
    m = re.search(r"\[.*\]", text or "", re.S)
    if not m:
        return {}
    try:
        arr = json.loads(m.group(0))
    except ValueError:
        return {}
    out = {}
    for d in arr if isinstance(arr, list) else []:
        if not isinstance(d, dict) or d.get("id") not in allowed_ids:
            continue
        dec = str(d.get("decision") or "").strip().lower()
        if dec in ("clear", "keep", "followup") and str(d.get("reason") or "").strip():
            out[d["id"]] = {"decision": dec, "reason": str(d["reason"]).strip()[:160],
                            "task": str(d.get("task") or "").strip()[:1500]}
    return out


def llm_classify(digests, now=None, claude=run_claude, cache_path=None, stamp_path=None):
    """{id: answer}. Cached by input hash; rate limited. Returns ({}, why) when it did not run."""
    now = time.time() if now is None else now
    h = llm_hash(digests)
    cache = _load(cache_path or LLM_CACHE, {}) or {}
    if h in cache:
        return cache[h], "cached"
    st = _load(stamp_path or LLM_STAMP, {}) or {}
    calls = [c for c in st.get("calls", []) if now - c < 86400]
    if calls and now - max(calls) < LLM_MIN_GAP_S:
        return {}, "rate limit: last call %ds ago" % (now - max(calls))
    if len(calls) >= LLM_MAX_PER_DAY:
        return {}, "daily cap %d" % LLM_MAX_PER_DAY
    calls.append(now)
    _save(stamp_path or LLM_STAMP, {"calls": calls})
    rc, out = claude(SYSTEM + "\n\nJOBS:\n" + json.dumps(digests, indent=1))
    ans = parse_answers(out, {d["id"] for d in digests})
    if ans:
        cache[h] = ans
        if len(cache) > 300:
            for k in list(cache)[:100]:
                cache.pop(k, None)
        _save(cache_path or LLM_CACHE, cache)
        return ans, "asked"
    return {}, "no usable answer (rc=%s)" % rc


# -------------------------------------------------------------------- actions
def emit_json():
    return json.loads(subprocess.run([sys.executable, str(EMIT), "--json"], capture_output=True,
                                     text=True, check=True, timeout=300).stdout)


def clear(ids, reason, runner=None):
    assert not any(w in reason.lower() for w in MERGE_WORDS), reason
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=300))
    r = run([sys.executable, str(EMIT), "--acted", *ids, "--reason", reason])
    return r.returncode, ((r.stdout or "") + (r.stderr or "")).strip()


def enqueue_followup(row, task, runner=None, today=None):
    """Enqueue one read-only research job for a follow-up. -> job id | None."""
    st = _load(WORK / "followups.json", {}) or {}
    day = today or time.strftime("%Y-%m-%d", time.gmtime())
    if len([x for x in st.get("log", []) if x.get("day") == day]) >= FOLLOWUP_MAX_PER_DAY:
        return None
    if is_private(row):
        return None
    label = "handoff-followup-%s" % row["id"]
    if any(x.get("label") == label for x in st.get("log", [])):
        return None
    tf = WORK / ("%s.task.md" % label)
    WORK.mkdir(parents=True, exist_ok=True)
    tf.write_text("Follow-up from the handoff panel for job %s (%s). READ-ONLY diagnosis: do not "
                  "edit product code.\n\n%s\n" % (row["id"], row.get("label"), task))
    run = runner or (lambda argv: subprocess.run(argv, capture_output=True, text=True, timeout=120))
    r = run([sys.executable, str(QUEUE), "enqueue", "--model", os.environ.get(
        "HANDOFF_FOLLOWUP_MODEL", "qwen3.6-35b-a3b-vl-mtp-mxfp8"), "--host", "studio",
        "--task-kind", "research", "--cwd", str(Path.home()), "--allow-unisolated",
        "--allow-no-verify", "--task-file", str(tf), "--label", label,
        "--bundle", "handoff-followups"])
    m = re.search(r"enqueued\s+([0-9a-f]{6,})", (r.stdout or ""))
    if not m:
        return None
    st.setdefault("log", []).append({"day": day, "label": label, "job": m.group(1), "for": row["id"]})
    st["log"] = st["log"][-200:]
    _save(WORK / "followups.json", st)
    return m.group(1)


# ----------------------------------------------------------------------- main
def plan(data, state_jobs, sidecars, heal_ledger, use_llm=True, claude=run_claude, now=None,
         page_reader=None, handlocked=None, sock_exists=os.path.exists):
    """PURE-ish. -> {"clear": [(row, reason)], "followup": [(row, task, reason)],
    "keep": [(row, why)], "llm": why}"""
    live_by_id, live_cwds = live_maps(state_jobs)
    by_id = {j.get("id"): j for j in state_jobs or []}
    out = {"clear": [], "followup": [], "keep": [], "llm": "not run"}
    rest = []
    acted_ids = set((data.get("acted") or {}).keys())
    for row in data.get("complete", []):
        if row.get("eval_arm") or is_private(row):
            continue
        if row.get("id") in acted_ids:
            continue            # idempotent: a mark that exists is never re-written
        g = guard(row, live_by_id, live_cwds, handlocked=handlocked)
        if g:
            out["keep"].append((row, g))
            continue
        why = mechanical(row, sidecars, heal_ledger)
        if why:
            out["clear"].append((row, why))
            continue
        lw = launcher_waiting(_sidecar(row, sidecars), now=now, exists=sock_exists)
        if lw:
            out["keep"].append((row, lw))
        else:
            rest.append(row)
    if rest and use_llm:
        ds = []
        for row in [r for r in rest if not is_private(r)][:LLM_BATCH]:
            sc = _sidecar(row, sidecars)
            head = ""
            if page_reader:
                try:
                    head = page_reader(row)
                except OSError:
                    head = ""
            ds.append(digest(row, by_id.get(row["id"]), sc, head))
        ans, out["llm"] = llm_classify(ds, now=now, claude=claude)
        for row in rest:
            a = ans.get(row["id"])
            if not a or a["decision"] == "keep":
                out["keep"].append((row, "llm: %s" % (a["reason"] if a else "no answer")))
            elif a["decision"] == "clear":
                out["clear"].append((row, "sonnet-triage: " + a["reason"]))
            else:
                out["followup"].append((row, a["task"], a["reason"]))
    else:
        for row in rest:
            out["keep"].append((row, "no mechanical rule%s" % ("" if use_llm else " (llm off)")))
    return out


RESURFACE_LEDGER = WORK / "cleared-ids.json"


def note_resurfaced(ids, ledger_path=None, logger=None):
    """Record every id we clear; return (and log) the ids we had ALREADY cleared that
    came back. A cleared id coming back means its acted mark was erased by something
    else (2026-10-09: 217 ids cleared 5x each over 16h) -- that must be loud and
    countable, not an invisible loop. Returns the resurfaced ids."""
    path = Path(ledger_path or RESURFACE_LEDGER)
    ids = list(dict.fromkeys(ids))
    led = _load(path, {}) or {}
    back = [i for i in ids if i in led]
    now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    for i in ids:
        e = led.get(i) or {"first": now, "n": 0}
        e["n"] = int(e.get("n", 0)) + 1
        e["last"] = now
        led[i] = e
    if len(led) > 5000:
        for k in sorted(led, key=lambda k: led[k].get("last", ""))[:1000]:
            led.pop(k, None)
    _save(path, led)
    if back:
        (logger or log)({"action": "resurfaced", "ids": back[:50], "n": len(back),
                         "reason": "previously auto-cleared ids are back on the panel: "
                                   "their acted mark was erased (see acted.restore.log)"})
    return back


def apply(p, clearer=clear, follow=enqueue_followup, ledger_path=None):
    done = []
    groups = {}
    note_resurfaced([row["id"] for row, _r in p["clear"]], ledger_path=ledger_path)
    seen_ids = set()
    for row, reason in p["clear"]:
        if row["id"] in seen_ids:
            continue
        seen_ids.add(row["id"])
        groups.setdefault("auto-triage: " + reason.split(":")[0].strip()[:40] + " -- " + reason[:140], []).append(row["id"])
    for row, task, reason in p["followup"]:
        if not task:
            continue
        jid = follow(row, task)
        if jid:
            groups.setdefault("auto-triage: follow-up queued as %s (%s)" % (jid, reason[:100]), []).append(row["id"])
    for reason, ids in groups.items():
        for k in range(0, len(ids), 10):
            rc, msg = clearer(ids[k:k + 10], reason)
            log({"action": "clear", "ids": ids[k:k + 10], "reason": reason, "rc": rc})
            done += ids[k:k + 10] if rc == 0 else []
    return done


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    ap.add_argument("--auto", action="store_true", help="act (what the watcher pass runs)")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-llm", action="store_true")
    a = ap.parse_args()
    if killed():
        print("kill switch set (%s / HANDOFF_AUTOCLEAR=off): not acting" % KILL)
        return 0
    data = emit_json()
    out_dir = Path(data.get("out_dir") or "")
    st = _load(STATE, {}) or {}
    jobs = st.get("jobs") or []
    jobs = list(jobs.values()) if isinstance(jobs, dict) else jobs
    p = plan(data, jobs, sidecars_index(), _load(HEAL_LEDGER, {}), use_llm=not a.no_llm,
             page_reader=lambda r: (out_dir / r["page"]).read_text(errors="replace")[:1200])
    print("clear=%d followup=%d keep=%d llm=%s" % (len(p["clear"]), len(p["followup"]),
                                                   len(p["keep"]), p["llm"]))
    for row, why in p["clear"]:
        print("  clear %s %s -- %s" % (row["id"], row.get("label"), why))
    for row, why in p["keep"]:
        print("  keep  %s %s -- %s" % (row["id"], row.get("label"), why))
    if a.dry_run or not a.auto:
        print("(dry run; --auto to act)")
        return 0
    done = apply(p)
    print("cleared %d" % len(done))
    return 0


if __name__ == "__main__":
    sys.exit(main())
