#!/usr/bin/env python3
"""Ollama job handoff: what's in flight, and what's finished but not acted on.

The owner's model is WORKFLOW STATE, not gate verdict:

    pending/   jobs still in flight (queued or running)
    complete/  jobs the MODEL finished that WE have not acted on yet --
               reviewed and merged/integrated/deployed
    acted on   removed from complete/, permanently

The distinction that matters, and the thing the first version got wrong: a job
whose GATE PASSED is not "handled". resell-bfmr-oneclick gates clean and is
still unmerged -- finished by the model, untouched by us. So "acted on" is
ORTHOGONAL to the gate verdict, and only an explicit mark removes a job from
complete/. The gate columns stay, because they are how you decide what to act
on next; they just don't decide it for you.

ONE PIECE OF REAL STATE, DELIBERATELY. Everything else here is derived and the
folder can be deleted and rebuilt with --all. "Acted on" cannot be derived --
no upstream system records whether a human integrated a diff -- so it lives in
acted.json. That is not a second source of truth about job state; it is the
only source of a different fact.

A PLAIN `rm` STICKS. Deleting a page from complete/ is the obvious gesture and
it must not be undone by the next rebuild, so a rebuild treats "this job was in
the manifest and its file is now gone" as an acted-on mark and records it. The
manifest is what makes that safe: with no manifest (fresh folder, first run)
absence means "never written", not "deleted", so nothing is silently marked.

Usage:
  handoff-emit.py --all              # rebuild everything
  handoff-emit.py --job-id <id>      # refresh one job + the index (hook path)
  handoff-emit.py --acted <id> [...] # mark acted on -> drops out of complete/
  handoff-emit.py --unact <id> [...] # undo that
"""
import argparse, json, re, subprocess, sys, time
import os
import fcntl
import contextlib
from pathlib import Path

BIN = Path.home() / "bin"
LOGS = BIN / "ollama-queue-logs"
STATE = BIN / "ollama-queue-state.json"
# HANDOFF_DIR overrides this. The path was hardcoded, so consolidating the
# GitHub Projects folder on 2026-09-02 silently split the tool from its data:
# the next run would have RECREATED an empty dir at the old location and written
# there, while 100 acted entries sat in the real one. Nothing would have errored
# -- it would just have quietly kept two records. Same shape as every other
# "reads as fine while pointing at the wrong artifact" bug in this harness.
OUT = Path(os.environ.get(
    "HANDOFF_DIR",
    Path.home() / "Desktop" / "GitHub Projects" / "ollama" / "ollama-handoff")
).expanduser()
ACTED = OUT / "acted.json"
MANIFEST = OUT / ".manifest.json"

IN_FLIGHT = {"pending", "running", "queued", "paused"}

# A --reason that CLAIMS a merge/landing/deploy but carries no --commit to verify
# is exactly how a job that never landed (d31d96d23b29) got cleared as "merged to
# main". "merg" alone is ambiguous -- it also matches "merging" used in its
# data/code sense (e.g. "no field merging"), which is not a landed-to-main claim.
# Only treat it as a merge claim when paired with a destination token; the other
# words are specific enough to stand alone.
_MERGE_WORDS = ("landed", "land in", "in main", "to main", "deployed", "shipped to")
_MERGE_STEM = "merg"
_DEST_WORDS = ("main", "master", "default branch", "trunk", "production", "prod")


def claims_merge_without_commit(reason):
    """True if `reason` (free text) asserts the work reached a real target branch."""
    reason_l = reason.lower()
    return (any(w in reason_l for w in _MERGE_WORDS)
            or (_MERGE_STEM in reason_l and any(d in reason_l for d in _DEST_WORDS)))


# AUTOMATED CLEARING (2026-10-12, LLM-only pipeline): the panel is processed per job with no
# human (handoff-autoclear.py: mechanical rules + a Sonnet batch + follow-up queue jobs). Its
# callers run with HANDOFF_AUTOMATED=1, and under it `--acted` REFUSES -- before any write, and
# whatever the caller's classifier said -- (1) a job whose sign-off is required and unapproved
# (no --override-signoff for automation), (2) a job still LIVE in the queue (incl. needs_opus /
# escalated / blocked / held: something or someone still needs it), (3) a job whose worktree
# another live job uses. A merge/landed claim still needs `--merged ID --commit SHA --repo NAME`
# (claims_merge_without_commit above applies to automation exactly as to a human).
AUTOMATED_LIVE_STATES = {"pending", "running", "queued", "paused", "held", "scheduled", "planned",
                         "needs_opus", "escalated", "blocked", "enqueued"}
AUTOCLEAR = Path(__file__).resolve().parent / "handoff-autoclear.py"


def automated_mode(env=None):
    return str((os.environ if env is None else env).get("HANDOFF_AUTOMATED", "")).lower() in ("1", "on", "true")


def automated_refusal(jid, rows):
    """PURE. Why automation must not clear `jid`, else None. `rows` = queue rows (live state)."""
    row = next((r for r in rows if r.get("id") == jid), None)
    if row is None:
        return None
    if str(row.get("status") or "").lower() in AUTOMATED_LIVE_STATES:
        return "%s is still live in the queue (%s): something still needs it" % (jid, row.get("status"))
    cwd = row.get("cwd")
    if cwd:
        for o in rows:
            if o.get("id") != jid and o.get("cwd") == cwd and \
                    str(o.get("status") or "").lower() in AUTOMATED_LIVE_STATES:
                return "%s: live job %s still uses its worktree" % (jid, o.get("id"))
    return None


def kick_autoclear(job_id=None, tool=None, popen=None, now=None, stamp=None):
    """Per-job automatic processing: a finished job's panel refresh (--job-id, the completion
    hook path) starts ONE detached `handoff-autoclear.py --auto` so the item is classified when
    the job ends, not on the next watcher pass. Debounced 45s via a stamp file; never under the
    verify sandbox or a redirected HANDOFF_DIR; HANDOFF_AUTO_PROCESS=off disables. Returns
    'started' | 'skipped:<why>'."""
    env = os.environ
    if str(env.get("HANDOFF_AUTO_PROCESS", "on")).lower() == "off":
        return "skipped:HANDOFF_AUTO_PROCESS=off"
    if env.get("DISPATCH_VERIFY_SANDBOX") == "1" or env.get("HANDOFF_DIR"):
        return "skipped:sandbox/redirected HANDOFF_DIR"
    if tool is None:
        # only the REAL install kicks processing: a canary / replay sandbox runs a COPY of this
        # script with HOME redirected (and no DISPATCH_VERIFY_SANDBOX in the gate-hook env), and
        # must not spawn a detached Sonnet-backed pass that competes with the replay
        try:
            import pwd
            real_bin = Path(pwd.getpwuid(os.getuid()).pw_dir).resolve() / "bin"
            if Path(__file__).resolve().parent != real_bin:
                return "skipped:not the installed copy (sandbox/replay)"
        except Exception:       # noqa: BLE001 -- advisory
            return "skipped:cannot resolve the installed copy"
    tool = Path(tool or AUTOCLEAR)
    if not tool.exists():
        return "skipped:no handoff-autoclear.py"
    stamp = Path(stamp or (OUT / ".autoclear.stamp"))
    now = time.time() if now is None else now
    try:
        if now - float(stamp.read_text().strip()) < 45:
            return "skipped:debounced"
    except (OSError, ValueError):
        pass
    try:
        stamp.parent.mkdir(parents=True, exist_ok=True)
        stamp.write_text(str(now))
        (popen or subprocess.Popen)([sys.executable, str(tool), "--auto"], stdin=subprocess.DEVNULL,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                    start_new_session=True, env=dict(env, HANDOFF_AUTOMATED="1"))
    except Exception as e:      # noqa: BLE001 -- advisory
        return "skipped:%s" % type(e).__name__
    return "started"


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    # Atomic write (temp file + os.replace, same idiom as ollama-queue.py's
    # _Locked.save): a plain write_text() is not atomic, so a reader with no
    # lock of its own (e.g. the --json dashboard read path) could observe a
    # truncated/partial acted.json mid-write. os.replace is a single syscall,
    # so a concurrent reader always sees either the old or the new file whole.
    tmp = p.with_suffix(p.suffix + f".tmp{os.getpid()}")
    tmp.write_text(json.dumps(obj, indent=1, sort_keys=True))
    os.replace(tmp, p)


ACTED_LASTGOOD = ACTED.with_name(ACTED.name + ".lastgood")
ACTED_RESTORE_LOG = ACTED.with_name("acted.restore.log")


def _load_acted_strict():
    """acted.json as a dict. A MISSING file is a fresh folder ({}); an UNREADABLE or
    CORRUPT one is an error, never "empty". ROOT CAUSE class (2026-10-09, handoff-
    autoclear cleared the same 217 ids five times over 16h; acted.json held nothing
    older than the last clear): `_load` swallows every exception into `{}`, and
    `_locked_acted` then saved `{}`+one new entry over the real file -- one transient
    read failure silently erased every acted mark. Fall back to the last-good copy,
    else refuse (raise) so nothing is written over a file we could not read."""
    try:
        txt = ACTED.read_text()
    except FileNotFoundError:
        return {}
    try:
        obj = json.loads(txt)
        if isinstance(obj, dict):
            return obj
        raise ValueError("acted.json is not a JSON object")
    except ValueError as exc:
        err = exc
    except Exception as exc:  # pragma: no cover
        err = exc
    lg = _load(ACTED_LASTGOOD, None)
    if isinstance(lg, dict) and lg:
        _log_restore("acted.json unreadable (%s); using last-good" % err, list(lg))
        return dict(lg)
    raise RuntimeError("acted.json unreadable and no last-good copy: %s" % err)


def _log_restore(why, ids):
    try:
        with open(ACTED_RESTORE_LOG, "a") as f:
            f.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                "why": why, "n": len(ids), "ids": ids[:20],
                                "pid": os.getpid(), "ppid": os.getppid(),
                                "argv": sys.argv[:6]}) + "\n")
    except OSError:
        pass


def _restore_missing(acted, log=True):
    """Merge back every acted mark the last-good copy has that `acted` lost. The ONLY
    legitimate removal is `--unact`, and that path rewrites last-good in the same
    locked cycle, so a key present in last-good but absent from the live file was
    erased by something else (truncation, a stale writer, a failed read). Marks are
    permanent by contract (see module docstring), so put them back. Opt out with
    HANDOFF_ACTED_NO_RESTORE=1. Returns the ids restored."""
    if os.environ.get("HANDOFF_ACTED_NO_RESTORE") == "1":
        return []
    lg = _load(ACTED_LASTGOOD, None)
    if not isinstance(lg, dict):
        return []
    lost = [k for k in lg if k not in acted]
    for k in lost:
        acted[k] = lg[k]
    if lost and log:
        _log_restore("acted.json lost %d mark(s) that last-good holds; restored" % len(lost), lost)
    return lost


def _save_acted(acted):
    _save(ACTED, acted)
    _save(ACTED_LASTGOOD, acted)


@contextlib.contextmanager
def _locked_acted():
    """One locked read-modify-write cycle on acted.json (mirrors ollama-queue.py's
    _Locked idiom). ROOT CAUSE (2026-09-21, live this session: `--acted` marks for
    4 job ids -- cc6448e24e43, ee1202495db4, e29a8ccc8c9d, 19d3e6c66ec8 -- were
    confirmed persisted immediately, then found MISSING from acted.json on the next
    poll, minutes later, with no `rm` or `--unact` involved). Multiple processes
    (dispatch-ack-hook.sh, gate.py, gate-on-complete.py, ollama-queue.py) all call
    this file with a bare `_load` + `_save` and NO locking: two concurrent
    read-modify-write cycles race, and whichever saves last silently clobbers the
    other's addition with a stale copy that never saw it. Yields the loaded dict;
    the caller mutates it in place and the lock releases (and the file saves) on
    context exit -- every scattered `acted = _load(ACTED, {})` / `_save(ACTED,
    acted)` pair in main() goes through this instead.
    2026-10-09: the load is strict (unreadable != empty) and heals marks erased by
    an outside writer from the last-good copy (see _restore_missing)."""
    lock_path = ACTED.with_suffix(ACTED.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            acted = _load_acted_strict()
            _restore_missing(acted)
            yield acted
            _save_acted(acted)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)


def run_bash(command):
    import subprocess
    try:
        result = subprocess.run(command, shell=True, capture_output=True, text=True)
        return result
    except Exception as e:
        return type('obj', (object,), {'exit_code': 1, 'stdout': '', 'stderr': str(e)})()


def gate_for(job_id): return _load(LOGS / f"{job_id}.gate.json", {})


def done_for(job_id): return _load(LOGS / f"{job_id}.done.json", {})


_QUEUE_MOD = "unloaded"


def _queue_mod():
    """Lazily load ollama-queue.py (hyphenated -> not importable by name) so we can
    reuse its read-time answer recovery. Cached; fail-OPEN to None so a load error
    just means 'no fallback', never a handoff crash."""
    global _QUEUE_MOD
    if _QUEUE_MOD != "unloaded":
        return _QUEUE_MOD
    _QUEUE_MOD = None
    try:
        import importlib.util
        p = BIN / "ollama-queue.py"
        spec = importlib.util.spec_from_file_location("ollama_queue", p)
        if spec and spec.loader:
            m = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(m)
            _QUEUE_MOD = m
    except Exception:
        _QUEUE_MOD = None
    return _QUEUE_MOD


def answer_for(job_id, done):
    """(answer_path, answer_preview) for a research/diagnosis job. The recorded
    done.json fields win; when a pre-Fix-2 job lacks them, recover from the durable
    .log/transcript sidecars via the queue module's single source of truth. Coding
    jobs get (None, None). Best-effort and non-raising."""
    ap, ans = done.get("answer_path"), done.get("answer")
    if ap or ans:
        return ap, ans
    mod = _queue_mod()
    if mod is None:
        return None, None
    try:
        return mod._recover_answer_at_read(job_id, done, str(LOGS))
    except Exception:
        return None, None


_VERDICT_RE = re.compile(r"^##\s*VERDICT:\s*([A-Za-z]+)", re.M)


def _report_verdict(job_id, kind):
    """First `## VERDICT: X` line from a reviewer's report.md (kind: review|regate)."""
    try:
        txt = (LOGS / f"{job_id}-{kind}" / "report.md").read_text()
    except Exception:
        return None
    m = _VERDICT_RE.search(txt)
    return m.group(1).upper() if m else None


def final_verdict(job_id, g, job=None):
    """Authoritative FINAL gate verdict (the owner: "the final pass or the final fail").

    The regate (Studio 27B) is the final authority when it ran, so its report
    wins over the pre-gate. We surface the reviewer's narrative `## VERDICT:`
    (PASS/FAIL/CONCERNS) rather than gate.json's `verdict`, which only ever
    carries the pre-gate machine classification (e.g. "concerns").

    "not-run (enqueued separately)" is NOT a verdict -- it only means the review
    was queued as its own gate- job and hasn't merged yet. When a real reviewer
    verdict is absent, fall back to the job's own honest enqueue-time preflight
    reading (recorded by ollama-queue.py) rather than showing that ambiguous
    string, which reads like a failure. "not-run" survives only for a job that
    genuinely ran no preflight (no preflight field)."""
    gate_v = None
    if g:
        # ESCALATING (2026-09-11, job fd7b1d7cd7d6): a re-gate is IN FLIGHT, so the
        # verdict is NOT final. Do NOT fall through to the pre-gate reviewer's
        # report verdict here -- it can say PASS while the machine verdict is
        # 'concerns' and the authoritative Studio 27B is still running, which read
        # on the dashboard as a terminal PASS on a job that was actually escalating.
        # Show the escalating state and the pre-gate machine verdict that triggered
        # it, so the row reflects the OVERALL state, never a premature PASS.
        if g.get("regate") == "pending":
            _pg = g.get("pregate_verdict") or g.get("verdict") or "concerns"
            return f"escalating -> re-gate (pre-gate: {_pg})"
        if g.get("regate") == "done":
            gate_v = _report_verdict(job_id, "regate")
        if not gate_v:
            gate_v = _report_verdict(job_id, "review")
        if not gate_v:
            gate_v = (g.get("regate_verdict") or g.get("review_verdict")
                      or g.get("verdict"))
        # OVERALL-VERDICT FLOOR: never let a reviewer's narrative PASS mask a
        # machine verdict of fail/concerns. The machine `verdict` folds BOTH the
        # decidable findings and the reviewer's rows (merge_review recomputes it),
        # so a reviewer report saying PASS while `verdict` is fail/concerns means
        # a DECIDABLE finding (a red verify, a scope/completeness flag) the diff
        # reviewer never spoke to. Surface the non-pass so it can't read as clean.
        _mv = str(g.get("verdict") or "")
        if (str(gate_v).upper() == "PASS"
                and _mv in ("fail", "concerns")):
            gate_v = f"{_mv} (reviewer said PASS; decidable finding stands)"
        # Same floor for an INCONCLUSIVE reviewer over a machine FAIL (2026-10-05,
        # s3fix): 16 replay-endorse s3 authoring jobs with a RED verify showed as
        # "UNPROVEN" because the reviewer hit its token cap -- which read as a
        # review-path problem, not the deterministic failure it was.
        elif (gate_v and _mv == "fail"
                and str(gate_v).upper() not in ("FAIL", "PASS")
                and not str(gate_v).lower().startswith(("fail", "not-run"))):
            gate_v = f"fail (reviewer {gate_v}; decidable finding stands)"
    # A real reviewer verdict (PASS/FAIL/CONCERNS) always wins.
    if gate_v and not str(gate_v).startswith("not-run"):
        return gate_v
    # No real verdict yet: prefer the honest preflight reading if we have one.
    pf = (job or {}).get("preflight")
    if pf:
        return pf
    return gate_v or "-"
def diff_for(job_id):
    try:
        return (LOGS / f"{job_id}.diff").read_text()
    except Exception:
        return ""


def diff_files(diff):
    return sorted({m.group(1) for m in re.finditer(r"^\+\+\+ b/(.+)$", diff, re.M)
                   if m.group(1) != "/dev/null"})


def slug(s):
    return re.sub(r"[^a-zA-Z0-9._-]+", "-", (s or "job")).strip("-")[:60] or "job"


try:
    from docsum_private import is_private_label
except ImportError:  # script copied elsewhere (tests): same definition, kept in step by test_docsum_ui_consumers
    import re as _re_dp

    def is_private_label(label):
        return _re_dp.fullmatch(r"docsum-[0-9a-f]{8}", str(label or "")) is not None

def load_jobs():
    """Live queue rows UNION every job with a gate record.

    Neither source alone is enough: the queue state holds only in-flight jobs
    (3 rows against 21 completed dispatches on disk), and gate records only
    exist once a job has finished. Pending work comes from the first, finished
    work from the second.
    """
    live = {}
    jobs = _load(STATE, {}).get("jobs") or []
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    for j in jobs:
        if j.get("id"):
            # _live marks "this row came from the LIVE queue state". Load-bearing for
            # is_pending: the queue PURGES completed jobs, so a job reconstructed from
            # its gate record carries whatever status was frozen at gate time -- and if
            # that was "running", nothing ever corrects it. Four bake-off arms sat in
            # "Pending -- in flight" indefinitely against an empty queue that way.
            live[j["id"]] = dict(j, _live=True)
    out = dict(live)
    for gf in sorted(LOGS.glob("*.gate.json")):
        jid = gf.name[: -len(".gate.json")]
        job = out.get(jid) or {"id": jid}
        g = gate_for(jid)
        for src, dst in (("job_label", "label"), ("job_model", "model"),
                         ("job_cwd", "cwd"), ("job_verify", "verify"),
                         ("job_status", "status"), ("job_exit_code", "exit_code"),
                         ("job_preflight", "preflight")):
            if not job.get(dst) and g.get(src) is not None:
                job[dst] = g[src]
        if not job.get("label"):
            logs = sorted(LOGS.glob(f"{jid}-*.log"))
            job["label"] = logs[0].name[len(jid) + 1:-4] if logs else jid
        if not job.get("status"):
            job["status"] = "unknown"
        job["_mtime"] = gf.stat().st_mtime
        out[jid] = job
    # Some jobs are not DISPATCH work -- there is nothing to act on, integrate, or
    # merge, so they must never sit on the handoff pending/complete worklist:
    #   - image generation (Pet Portrait Studio et al.), and
    #   - pipeline-internal jobs: a `draft-` case-drafting run and the `gate-`/`regate-`
    #     review jobs are steps IN a dispatch, not dispatches themselves. (A draft that
    #     picked up a spurious gate record -- fixed in the queue, but old records
    #     linger -- was showing up here as a stale "failed" the panel could never
    #     clear, which is exactly the "not keeping up" drift.)
    _INTERNAL_PREFIXES = ("pet-", "draft-", "gate-", "regate-")
    def _is_non_dispatch(j):
        label = str(j.get("label", ""))
        return (str(j.get("model", "")).lower() == "image"
                or label.startswith(_INTERNAL_PREFIXES) or is_private_label(label))
    # Bug #8 (2026-09-18): a failed run whose fixed retry has been enqueued is
    # superseded -- it folds off the handoff panel (the retry carries the work now;
    # the never-pruned sidecar keeps the audit trail).
    def _is_superseded(j):
        return bool(j.get("superseded_by") or j.get("superseded"))
    return [j for j in out.values()
            if not _is_non_dispatch(j) and not _is_superseded(j)]


def needs_answer(job):
    """The question of a mini-swe job parked on a NEEDS.md ask (paused, pause_reason needs_info), else None. The owner answers with `mini-answer`."""
    if (job.get("status") or "").lower() == "paused" and job.get("pause_reason") == "needs_info":
        return str((job.get("pause_meta") or {}).get("question") or "(question unavailable)")
    return None


def is_pending(job):
    # In flight means SOMETHING IS EXECUTING IT, which requires a row in the live
    # queue state -- a status string alone only says what was true when it was last
    # written. A purged job's frozen "running" is not evidence of a running process.
    return bool(job.get("_live")) and (job.get("status") or "").lower() in IN_FLIGHT


def page_name(job):
    ts = (job.get("enqueued_at") or "")[:10] or (
        time.strftime("%Y-%m-%d", time.localtime(job["_mtime"])) if job.get("_mtime") else "undated")
    return f"{ts}-{slug(job.get('label'))}-{job.get('id')}.md"


def render(job, gate, diff):
    files = diff_files(diff)
    L = [f"# {job.get('label') or job.get('id')}", "",
         f"- **job id**: `{job.get('id')}`",
         f"- **status**: `{job.get('status')}`" +
         (f"  (exit {job['exit_code']})" if job.get("exit_code") is not None else "")]
    if gate:
        L.append(f"- **gate**: `{final_verdict(job.get('id'), gate, job)}`")
        c = gate.get("counts") or {}
        if c:
            L.append(f"- **gate counts**: {c.get('code_high',0)} code-high, "
                     f"{c.get('code',0)} code, {c.get('input',0)} input")
        for nc in gate.get("not_checked") or []:
            L.append(f"- **NOT CHECKED**: {nc}")
        for ut in gate.get("untrusted") or []:
            L.append(f"- **UNTRUSTED**: {ut}")
    if needs_answer(job):
        L.append(f"- **NEEDS ANSWER**: {' '.join(needs_answer(job).split())[:400]} — `mini-answer {job.get('id')} \"...\"` "
                 f"(`mini-answer {job.get('id')} --show` for what it tried)")
    for key, label in (("model", "model"), ("cwd", "cwd"), ("verify", "verify")):
        if job.get(key):
            L.append(f"- **{label}**: `{job[key]}`")
    if not is_pending(job):
        L.append(f"- **files changed** ({len(files)}): " +
                 (", ".join(f"`{f}`" for f in files) or "_none recorded_"))
    L += ["", f"_Acted on? `python3 ~/bin/handoff-emit.py --acted {job.get('id')}`_", ""]
    if gate and gate.get("issues"):
        L += ["## Gate findings", "", "| sev | where | what |", "|---|---|---|"]
        for i in gate["issues"]:
            loc = (f"{i.get('file','')}:{i.get('line')}"
                   if i.get("file") and i.get("line") else (i.get("file") or "-"))
            L.append(f"| {i.get('severity','?')} | `{loc}` | {str(i.get('what','')).replace('|', chr(92)+'|')} |")
        L.append("")
    # RESEARCH/DIAGNOSIS ANSWER (Fix 2): these jobs deliver a written finding, not a
    # diff. The queue persists the worker's final answer to <id>.answer.md (+ ANSWER.md
    # in the cwd) and records answer_path/answer in <id>.done.json. Surface the preview
    # and point at the durable file so the finding isn't lost the way it was twice before.
    _done = done_for(job.get("id"))
    # Recorded fields win; a pre-Fix-2 job's answer is recovered from the .log/
    # transcript sidecars so EVERY research/diagnosis job surfaces, not just new ones.
    _ans_path, _ans = answer_for(job.get("id"), _done)
    if _ans or _ans_path:
        L += ["## Answer (research/diagnosis)", ""]
        if _ans_path:
            L.append(f"_Full answer: `{_ans_path}`_")
            L.append("")
        if _ans:
            L += ["> " + ln for ln in str(_ans).splitlines()] + [""]
    if is_pending(job):
        L += ["_In flight -- no diff or gate record until it finishes._", ""]
    elif diff.strip():
        L += ["## Diff", "", "```diff", diff.rstrip(), "```", ""]
    elif _ans or _ans_path:
        pass  # research/diagnosis job: the Answer section above IS the deliverable
    else:
        L += ["## Diff", "", "_No diff was captured for this job._", ""]
    return "\n".join(L)


def reconcile_removals(jobs, acted):
    """A page that was written and is now gone was deleted on purpose."""
    manifest = _load(MANIFEST, None)
    if manifest is None:
        return acted, []          # no manifest = fresh folder; infer nothing
    newly = []
    by_id = {j["id"]: j for j in jobs}
    for jid, rel in manifest.items():
        if jid in acted or jid not in by_id:
            continue
        if rel.startswith("complete/") and not (OUT / rel).exists():
            acted[jid] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                          "how": "removed from complete/",
                          "label": by_id[jid].get("label")}
            newly.append(jid)
    return acted, newly


# Every prefix here must be specific enough that a REAL coding label can never
# start with it -- hiding a genuine deliverable is far worse than showing an
# extra measurement arm. That is why this is not simply "rt-": `rt-sync-window-fix`
# is real work, `rt-qwen38` is an arm. Extended 2026-09-02 after ~35 scored arms
# piled into the action buckets: pa2-/bo2-/automerge-ladder- were all missing.
EVAL_LABEL_PREFIXES = ("bo-", "bo2-", "pa2-", "rt-qwen", "rt2-qwen",
                       "automerge-ladder-", "bakeoff-", "canary-", "gate-",
                       # Diagnosis CALIBRATION/EVAL runs measure whether the method
                       # works; they produce a DIAGNOSIS.json we read once and score,
                       # not a change to integrate. Deliberately NOT a bare "diag-":
                       # a production diagnosis of a bug we actually need explained
                       # IS a deliverable and must stay visible, and a real coding
                       # label like diag-panel-fix must never be hidden.
                       "diag-calib-", "diag-eval-")


def _label_is_eval(label):
    """The single definition of 'this is a measurement arm, not a deliverable'.
    Both the INDEX writer and the --json read path go through it, because they
    disagreed once already: the filter was added to write_index only, so INDEX.md
    reported 0 awaiting action while the dashboard -- which renders --json -- still
    listed all 38 arms. Two renderings of one truth need one predicate."""
    return (label or "").startswith(EVAL_LABEL_PREFIXES)


def signoff_blocks_acting(jid, label):
    """Refuse to mark a job acted-on while a REQUIRED sign-off is unapproved.

    Marking acted removes a job from every bucket (rebuild() skips acted ids
    before it computes awaiting_signoff), so `--acted` silently overrode the
    sign-off gate entirely -- the one action the gate exists to hold back was
    also the action that erased it. Measured 2026-09-02: all 15
    required-and-unapproved jobs were invisible, because every one had been
    marked acted.

    An EVAL ARM is exempt: bake-off arms edit harness files inside a scratch
    worktree purely to be measured, so the basename rule flags them while there
    is nothing to integrate and nothing to sign off on.

    Returns a refusal string, or None if acting is allowed.
    """
    if _label_is_eval(label):
        return None
    so = _load(Path(__file__).parent / "signoff.json", {}).get(jid) or {}
    if so.get("required") and so.get("verdict") != "approve":
        return (f"{jid} ({label or '?'}) REQUIRES sign-off from "
                f"{so.get('reviewer') or 'a reviewer'} and none is recorded "
                f"(verdict={so.get('verdict')!r}; {so.get('reason') or 'no reason recorded'}). "
                f"Record it with:  signoff.py --verdict approve {jid}\n"
                f"    To clear it WITHOUT sign-off, pass --override-signoff "
                f"\"<why>\" -- that override is recorded.")
    return None


def _is_eval_arm(row):
    """A row is (job, gate, rel). Bake-off/eval arms are MEASUREMENT runs: they have
    no deliverable to integrate, so they belong in neither action bucket."""
    return _label_is_eval((row[0] or {}).get("label"))


# How many acted entries each INDEX section renders. acted.json stays the full
# append-only record; the INDEX is a DASHBOARD and had grown to 16KB rendering
# all 100, which is what "flooded" meant -- 0 pending and 0 awaiting action, but
# every historical entry still on the page. Truncation is stated explicitly
# rather than silent: a list that stops without saying so reads as complete,
# which is the same failure as a check that does not say it did not run.
ACTED_INDEX_LIMIT = int(os.environ.get("HANDOFF_ACTED_LIMIT", "15"))


def _capped(rows, label):
    """Render at most ACTED_INDEX_LIMIT rows, and SAY when older ones are hidden."""
    if ACTED_INDEX_LIMIT <= 0 or len(rows) <= ACTED_INDEX_LIMIT:
        return rows, []
    hidden = len(rows) - ACTED_INDEX_LIMIT
    return rows[:ACTED_INDEX_LIMIT], [
        "", f"_+{hidden} older {label} entr{'y' if hidden == 1 else 'ies'} not shown "
            f"(showing the {ACTED_INDEX_LIMIT} most recent). Full record: `acted.json`._"]


def write_index(pending, complete, acted):
    def table(rs):
        out = ["| job | status | gate | files | flags |", "|---|---|---|---:|---|"]
        for j, g, rel in rs:
            n = len(diff_files(diff_for(j["id"])))
            nc = len(g.get("not_checked") or [])
            flags = " ".join(filter(None, [
                f"{nc} unchecked" if nc else "",
                "UNTRUSTED" if g.get("untrusted") else "",
                "AWAITING SIGN-OFF" if j.get("awaiting_signoff") else "",
                "NEEDS ANSWER (mini-answer)" if needs_answer(j) else ""])) or "-"
            out.append(f"| [{j.get('label') or j['id']}]({rel}) | `{j.get('status')}` | "
                       f"`{final_verdict(j['id'], g, j)}` | {n} | {flags} |")
        return out

    L = ["# Ollama job handoff", "",
         "- **pending/** — still in flight.",
         "- **complete/** — the model finished it; **we haven't acted on it yet** "
         "(reviewed + merged/integrated/deployed).",
         "",
         "A gate `pass` does **not** mean handled — plenty of clean jobs are still "
         "waiting to be merged. Only marking a job acted-on clears it:",
         "", "```", "python3 ~/bin/handoff-emit.py --acted <job-id>", "```", "",
         "Deleting a page from `complete/` by hand does the same thing and sticks. "
         "Everything else is derived — rebuild any time with `--all`.", "",
         "", ""]

    # write_index receives ROWS -- (job_dict, gate_dict, rel_path) tuples built
    # by rebuild() -- not bare job dicts. The sign-off bucket originally called
    # j.get("awaiting_signoff") straight on the tuple, which raised
    # AttributeError and broke every --all rebuild. It shipped because the
    # stage-1 verify only GREPPED for the bucket heading; a grep proves the code
    # mentions a feature, never that the feature runs. Unpack explicitly.
    # The eval-arm exclusion has to apply to BOTH action buckets. It was originally
    # applied only to the regular rows, so a scored arm that happened to carry
    # awaiting_signoff sailed past it -- which is exactly how 11 bake-off arms came
    # to sit in "Awaiting sign-off" as though a human owed them a decision.
    _awaiting_all = [r for r in complete if (r[0] or {}).get("awaiting_signoff")]
    _regular_all = [r for r in complete if not (r[0] or {}).get("awaiting_signoff")]
    awaiting_rows = [r for r in _awaiting_all if not _is_eval_arm(r)]
    regular_rows = [r for r in _regular_all if not _is_eval_arm(r)]
    # Counted separately per bucket, and always PRINTED: a filter that hides work
    # silently is the failure mode this whole index exists to prevent.
    _hidden_signoff = len(_awaiting_all) - len(awaiting_rows)
    _hidden_eval = len(_regular_all) - len(regular_rows)

    L += [f"## Awaiting sign-off ({len(awaiting_rows)})" +
          (f" · {_hidden_signoff} eval arm(s) hidden" if _hidden_signoff else ""), ""]
    L += table(awaiting_rows) if awaiting_rows else ["_Nothing waiting on sign-off._"]

    L += ["", f"## Complete — awaiting action ({len(regular_rows)})" +
          (f" · {_hidden_eval} eval arm(s) hidden" if _hidden_eval else ""), ""]
    L += table(regular_rows) if regular_rows else ["_Nothing waiting on us._"]
    
    L += ["", f"## Pending — in flight ({len(pending)})", ""]
    # `pending` is ALREADY a list of (job, gate, rel) rows, exactly like
    # awaiting_rows and regular_rows above. Re-wrapping it as if its items were
    # dicts raised "tuple indices must be integers" and broke every render that
    # had a job in flight -- the THIRD instance of this same tuple/dict
    # confusion, and the one my harness missed because its fixtures only ever
    # populated the awaiting and complete buckets. Pass the rows straight
    # through, like the two lines above it.
    L += table(pending) if pending else ["_Nothing running._"]
    
    # Add sections for merged and resolved jobs
    merged_jobs = []
    resolved_jobs = []
    unspecified_jobs = []
    
    for jid, meta in sorted(acted.items(), key=lambda kv: kv[1].get("ts", ""), reverse=True):
        if "merged" in meta:
            if meta["merged"] is True:
                merged_jobs.append((jid, meta))
            elif meta["merged"] is False:
                resolved_jobs.append((jid, meta))
        else:
            unspecified_jobs.append((jid, meta))
    
    if merged_jobs:
        L += ["", "## Merged into code", ""]
        _rows, _more = _capped(merged_jobs, "merged")
        for jid, meta in _rows:
            repo = meta.get("repo", "")
            commit = meta.get("commit", "")
            verified = meta.get("verified", False)
            status = "UNVERIFIED" if not verified else ""
            L.append(f"- `{jid}` {meta.get('label') or ''} — {meta.get('ts','')} ({status})")
            if repo and commit:
                L.append(f"  - Repo: {repo}, Commit: {commit}")
        L += _more
        L.append("")
    
    if resolved_jobs:
        L += ["", "## Resolved (no merge)", ""]
        _rows, _more = _capped(resolved_jobs, "resolved")
        for jid, meta in _rows:
            reason = meta.get("reason", "")
            L.append(f"- `{jid}` {meta.get('label') or ''} — {meta.get('ts','')} ({reason})")
        L += _more
        L.append("")
    
    if unspecified_jobs:
        L += ["", "## Acted on (unspecified)", "",
              "_Cleared. Listed so the record survives the page being removed._", ""]
        _rows, _more = _capped(unspecified_jobs, "acted")
        for jid, meta in _rows:
            L.append(f"- `{jid}` {meta.get('label') or ''} — {meta.get('ts','')} ({meta.get('how','')})")
        L += _more
    L.append("")
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "INDEX.md").write_text("\n".join(L))


def collect(acted):
    """The pending / complete split, as data. READ-ONLY.

    Split out from rebuild() so the dashboard can poll it without writing
    anything: a GET that rebuilt the folder on every poll would rewrite 20-odd
    files a few times a minute, and -- far worse -- would run the hand-delete
    reconciliation on a schedule, turning a transient missing file into a
    permanent acted-on mark. Reads never mutate.
    """
    pending, complete = [], []
    signoffs = _load(Path(__file__).parent / "signoff.json", {})
    
    for j in sorted(load_jobs(),
                    key=lambda x: (x.get("enqueued_at") or "", x.get("_mtime", 0)),
                    reverse=True):
        # Filter out internal review runners (gate- pre-gate, regate- Studio re-gate)
        # and measurement jobs (bakeoff-, fit-) -- none are deliverable work
        if j.get("label", "").startswith(("gate-", "regate-", "bakeoff-", "fit-")) or is_private_label(j.get("label")):
            continue
        # A PLANNED placeholder (ollama-queue.PLANNED_STATUS) is a worklist promise,
        # not a dispatch -- no diff, no gate, nothing to review. is_pending() reads
        # it as False (not in IN_FLIGHT) so it fell into `complete`, showing up as a
        # 0-file "needs review" row forever with nothing to act on. Confirmed live
        # 2026-09-21 after fixing the missing-placeholder bug (see
        # ollama-queue.py cmd_plan_add / ollama-dispatch-slice publish_dag): the two
        # republished esim-global rows showed up here immediately.
        if (j.get("status") or "").lower() == "planned":
            continue
            
        jid = j["id"]
        if jid in acted:
            continue
        g = gate_for(jid)
        n = len(diff_files(diff_for(jid)))
        nc = len(g.get("not_checked") or [])
        
        # Check if job requires sign-off and hasn't been approved yet
        awaiting_signoff = False
        if (signoffs.get(jid, {}).get("required", False) and 
            signoffs.get(jid, {}).get("verdict") != "approve"):
            awaiting_signoff = True
        
        row = {"id": jid, "label": j.get("label") or jid,
               "status": j.get("status"), "gate": final_verdict(jid, g, j) or "-",
               "files": n, "not_checked": nc,
               "untrusted": bool(g.get("untrusted")),
               "code_high": (g.get("counts") or {}).get("code_high", 0),
               "model": j.get("model"), "cwd": j.get("cwd"),
               "awaiting_signoff": awaiting_signoff,
               "needs_info": needs_answer(j) is not None, "question": needs_answer(j)}
        sub = "pending" if is_pending(j) else "complete"
        row["page"] = f"{sub}/{page_name(j)}"
        (pending if sub == "pending" else complete).append(row)
    return pending, complete


def rebuild(only_id=None):
    jobs = load_jobs()
    lock_path = ACTED.with_suffix(ACTED.suffix + ".lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with open(lock_path, "a+") as lf:
        fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
        try:
            acted = _load_acted_strict()
            _healed = _restore_missing(acted)
            acted, newly = reconcile_removals(jobs, acted)
            if newly or _healed:
                _save_acted(acted)
        finally:
            fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
    if newly:
        print(f"[handoff] {len(newly)} page(s) removed by hand -> marked acted on")

    pending, complete, manifest = [], [], {}
    for j in sorted(jobs, key=lambda x: (x.get("enqueued_at") or "", x.get("_mtime", 0)), reverse=True):
        # Filter out internal review runners (gate- pre-gate, regate- Studio re-gate)
        # and measurement jobs (bakeoff-, fit-) -- none are deliverable work
        if j.get("label", "").startswith(("gate-", "regate-", "bakeoff-", "fit-")) or is_private_label(j.get("label")):
            continue
        # A PLANNED placeholder (ollama-queue.PLANNED_STATUS) is a worklist promise,
        # not a dispatch -- no diff, no gate, nothing to review. is_pending() reads
        # it as False (not in IN_FLIGHT) so it fell into `complete`, showing up as a
        # 0-file "needs review" row forever with nothing to act on. Confirmed live
        # 2026-09-21 after fixing the missing-placeholder bug (see
        # ollama-queue.py cmd_plan_add / ollama-dispatch-slice publish_dag): the two
        # republished esim-global rows showed up here immediately.
        if (j.get("status") or "").lower() == "planned":
            continue
            
        jid = j["id"]
        if jid in acted:
            continue
        g, d = gate_for(jid), diff_for(jid)
        sub = "pending" if is_pending(j) else "complete"
        rel = f"{sub}/{page_name(j)}"
        # Check sign-off status for the complete bucket
        signoffs = _load(Path(__file__).parent / "signoff.json", {})
        awaiting_signoff = False
        if (signoffs.get(jid, {}).get("required", False) and 
            signoffs.get(jid, {}).get("verdict") != "approve"):
            awaiting_signoff = True
        
        row = {"id": jid, "label": j.get("label") or jid,
               "status": j.get("status"), "gate": final_verdict(jid, g, j) or "-",
               "files": len(diff_files(d)), "not_checked": len(g.get("not_checked") or []),
               "untrusted": bool(g.get("untrusted")),
               "code_high": (g.get("counts") or {}).get("code_high", 0),
               "model": j.get("model"), "cwd": j.get("cwd"),
               "awaiting_signoff": awaiting_signoff}
        # Stamp the flag onto the JOB dict -- that is what gets appended below
        # and handed to write_index. The `row` built here was discarded, so
        # awaiting_signoff was computed correctly and then thrown away and the
        # bucket rendered 0 while signoff.json held a required/no-verdict entry.
        # Dead code, invisible to a grep-level check.
        j["awaiting_signoff"] = awaiting_signoff
        row["page"] = rel
        (pending if sub == "pending" else complete).append((j, g, rel))
        page = OUT / rel
        if only_id is None or jid == only_id:
            page.parent.mkdir(parents=True, exist_ok=True)
            page.write_text(render(j, g, d))
        # MANIFEST RECORDS ONLY PAGES THAT EXIST. In --job-id mode (the hook
        # path) every other job is skipped, so recording them regardless would
        # put a page in the manifest that was never written -- and the next
        # rebuild reads "in the manifest, file absent" as a deliberate hand
        # delete and marks it acted on. That would silently retire live work
        # the moment any new job appeared between two hook runs.
        if page.exists():
            manifest[jid] = rel

    # Sweep pages whose job moved folders (pending -> complete) or was acted on,
    # so a stale copy can't sit there looking like live work.
    keep = set(manifest.values())
    for sub in ("pending", "complete"):
        for f in (OUT / sub).glob("*.md") if (OUT / sub).exists() else []:
            if f"{sub}/{f.name}" not in keep:
                f.unlink()
    _save(MANIFEST, manifest)
    write_index(pending, complete, acted)
    return len(pending), len(complete), len(acted)


def _github_slug(base):
    """'owner/name' of the repo's origin when it is a GitHub remote, else None."""
    try:
        r = subprocess.run(["git", "-C", str(base), "remote", "get-url", "origin"],
                           capture_output=True, text=True, timeout=15)
    except (OSError, subprocess.SubprocessError):
        return None
    m = re.search(r"github\.com[:/]([^/\s]+)/([^/\s]+?)(?:\.git)?/?$", (r.stdout or "").strip())
    return f"{m.group(1)}/{m.group(2)}" if (r.returncode == 0 and m) else None


def _commit_in_default_branch_remote(base, commit):
    """READ-ONLY GitHub check (gh compare API): True when `commit` is an ancestor of
    origin's main/master, False when it exists there but is not, None when it cannot
    be resolved (no GitHub origin, no gh, unknown SHA). Never writes the local repo."""
    slug = _github_slug(base)
    if not slug:
        return None
    seen_false = False
    for br in ("main", "master"):
        try:
            r = subprocess.run(["gh", "api", f"repos/{slug}/compare/{br}...{commit}",
                                "--jq", ".status"], capture_output=True, text=True, timeout=45)
        except (OSError, subprocess.SubprocessError):
            return None
        st = (r.stdout or "").strip()
        if r.returncode != 0 or not st:
            continue
        if st in ("behind", "identical"):
            return True
        seen_false = True
    return False if seen_false else None


def _commit_in_default_branch(repo, commit):
    """True iff `commit` is an ancestor of the repo's default branch (main/master,
    local or origin/*) -- i.e. it actually LANDED, not merely that the SHA exists
    (a dispatch-worktree branch commit exists but is NOT merged; that ambiguity is
    how d31d96d23b29 got a false 'merged to main' record). Returns None when the
    repo or commit cannot be resolved here (the caller decides what to do).

    A local clone that has not FETCHED the landing (live 2026-10-02: bfmr
    replace-tracking landed from a separate clone, e692a48 unknown to the Desktop
    repo) is not evidence either way, so anything short of a local True falls back
    to the read-only GitHub check before answering."""
    local = _commit_in_default_branch_local(repo, commit)
    if local is True:
        return True
    base = Path(repo) if repo else None
    if base is not None and not base.exists():
        base = Path.home() / "Desktop" / "GitHub Projects" / str(repo)
    if base is not None and base.exists():
        remote = _commit_in_default_branch_remote(base, commit)
        if remote is not None:
            return remote
    return local


def _commit_in_default_branch_local(repo, commit):
    try:
        base = Path(repo) if repo else None
        if base is not None and not base.exists():
            base = Path.home() / "Desktop" / "GitHub Projects" / str(repo)
        if base is None or not base.exists():
            return None
        if subprocess.run(["git", "-C", str(base), "cat-file", "-e", commit + "^{commit}"],
                          capture_output=True, timeout=30).returncode != 0:
            return None
        for ref in ("main", "master", "origin/main", "origin/master"):
            if subprocess.run(["git", "-C", str(base), "rev-parse", "--verify", "--quiet", ref],
                              capture_output=True, timeout=15).returncode != 0:
                continue
            if subprocess.run(["git", "-C", str(base), "merge-base", "--is-ancestor", commit, ref],
                              capture_output=True, timeout=30).returncode == 0:
                return True
        return False
    except (OSError, subprocess.SubprocessError):
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job-id")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--acted", nargs="+", metavar="JOB_ID")
    ap.add_argument("--unact", nargs="+", metavar="JOB_ID")
    ap.add_argument("--merged", metavar="JOB_ID")
    ap.add_argument("--commit", metavar="SHA")
    ap.add_argument("--repo", metavar="NAME")
    ap.add_argument("--reason", metavar="REASON")
    ap.add_argument("--allow-unverified-merge", action="store_true",
                    help="Record a merge/landed claim WITHOUT the ancestor-of-default-branch "
                         "check (squash/rebase gave a different SHA, or the repo is not "
                         "checked out here). Without it, a merge claim whose SHA is not an "
                         "ancestor of main/master is REFUSED (see job d31d96d23b29).")
    ap.add_argument("--override-signoff", metavar="WHY", default=None,
                    help=("clear a job whose REQUIRED sign-off is unapproved. The "
                          "reason is recorded in acted.json. Without this, --acted "
                          "refuses such a job instead of silently erasing the gate."))
    ap.add_argument("--auto-process", action="store_true",
                    help="after the rebuild, run the automatic panel processing NOW (handoff-autoclear "
                         "--auto: mechanical rules, Sonnet batch, follow-up queue jobs, clears via "
                         "--acted --reason) under HANDOFF_AUTOMATED=1 guards")
    ap.add_argument("--json", action="store_true",
                    help="print the pending/complete split as JSON and write "
                         "nothing -- the dashboard's read path")
    a = ap.parse_args()
    # ROOT CAUSE (2026-09-21): `--acted`/`--unact` are `nargs="+"`, which only
    # splits on WHITESPACE between separate argv tokens -- `--acted id1,id2,id3`
    # (one comma-joined shell argument, the natural way to type a bulk mark, and
    # what this session's own sweep passes did repeatedly) parses as ONE literal
    # string "id1,id2,id3" and gets written to acted.json as a single garbage
    # key that no real job id ever matches. Every job in that batch then kept
    # resurfacing as "unreviewed" on the next poll -- the exact symptom earlier
    # misdiagnosed this session as a concurrent-write race on an unlocked file
    # (a real bug, separately fixed via _locked_acted, but not what caused THIS
    # resurfacing). Splitting each token on commas here fixes it at the one
    # place both flags are consumed, without changing the CLI contract for
    # anyone already passing space-separated or single ids.
    if a.acted:
        a.acted = [jid for tok in a.acted for jid in tok.split(",") if jid]
    if a.unact:
        a.unact = [jid for tok in a.unact for jid in tok.split(",") if jid]

    if a.json:
        # Read path: never raises on a bad file (the dashboard polls it) and never
        # writes, but DOES overlay last-good so a lost mark cannot resurface a row.
        acted = _load(ACTED, {})
        if not isinstance(acted, dict):
            acted = {}
        _restore_missing_view = dict(acted)
        _restore_missing(_restore_missing_view, log=False)
        acted = _restore_missing_view
        pending, complete = collect(acted)
        # Same exclusion the INDEX applies, on the dashboard's read path. The
        # dashboard iterates data.complete / data.pending straight into its tables,
        # so filtering HERE makes it agree with INDEX.md without the renderer
        # changing at all. Nothing is dropped: every row keeps an eval_arm flag and
        # the excluded rows are still delivered under their own keys, so a consumer
        # that wants to show or count arms still can.
        for r in pending + complete:
            r["eval_arm"] = _label_is_eval(r.get("label"))
        ev_pending = [r for r in pending if r["eval_arm"]]
        ev_complete = [r for r in complete if r["eval_arm"]]
        print(json.dumps({"pending": [r for r in pending if not r["eval_arm"]],
                          "complete": [r for r in complete if not r["eval_arm"]],
                          "eval_arms_pending": ev_pending,
                          "eval_arms_complete": ev_complete,
                          "hidden_eval_counts": {"pending": len(ev_pending),
                                                 "complete": len(ev_complete)},
                          "acted": acted, "out_dir": str(OUT)}, indent=1))
        return 0

    # ROOT CAUSE (2026-10-06): the merge-claim --reason refusal used to run AFTER
    # this block had already written every --acted id to acted.json, so a
    # "REFUSED ... exit 2" still cleared the job (reproduced: `--acted X --reason
    # "landed in main"` -> REFUSED, X in acted.json). Refuse BEFORE any write.
    if (a.reason and a.acted and claims_merge_without_commit(a.reason)
            and not a.commit and not a.allow_unverified_merge):
        print("[handoff] REFUSED: the --reason claims this job merged/landed/deployed, but no "
              "--commit was given to verify it. A free-text merge claim is how a job that "
              "never landed (d31d96d23b29) got cleared as 'merged to main'. Use "
              "`--merged <id> --commit SHA --repo NAME` so the SHA is checked against the "
              "default branch, or pass --allow-unverified-merge to record this reason as-is. "
              "Nothing was marked acted.")
        return 2

    if a.acted or a.unact:
        labels = {j["id"]: j.get("label") for j in load_jobs()}
        # Check EVERY id before writing ANY of them: a bulk clear that marks half
        # the list and then refuses leaves the caller guessing what landed.
        if automated_mode() and a.acted:
            if a.override_signoff:
                print("[handoff] REFUSED (automated): --override-signoff is never available to automation. "
                      "Nothing was marked acted.")
                return 2
            _rows = _load(STATE, {}).get("jobs") or []
            _rows = list(_rows.values()) if isinstance(_rows, dict) else _rows
            _auto_bad = [r_ for r_ in (automated_refusal(j, _rows) for j in a.acted) if r_]
            if _auto_bad:
                for r_ in _auto_bad:
                    print(f"[handoff] REFUSED (automated): {r_}")
                print("[handoff] nothing was marked acted.")
                return 2
        _blocked = [b for b in (signoff_blocks_acting(j, labels.get(j))
                                for j in (a.acted or [])) if b]
        if _blocked and not a.override_signoff:
            for b in _blocked:
                print(f"[handoff] REFUSED: {b}")
            print(f"[handoff] {len(_blocked)} job(s) blocked by required sign-off; "
                  f"nothing was marked acted.")
            return 2
        with _locked_acted() as acted:
            for jid in a.acted or []:
                entry = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                         "how": "marked with --acted", "label": labels.get(jid)}
                if a.override_signoff and signoff_blocks_acting(jid, labels.get(jid)):
                    entry["how"] = "marked with --acted (SIGN-OFF OVERRIDDEN)"
                    entry["signoff_override_reason"] = a.override_signoff
                    print(f"[handoff] SIGN-OFF OVERRIDDEN for {jid}: {a.override_signoff}")
                if a.reason:
                    # Folded in here (2026-10-06): a separate later block used to
                    # REPLACE this entry, erasing the sign-off override audit.
                    entry["how"] += f" (reason: {a.reason})"
                    entry["merged"] = False
                    entry["reason"] = a.reason
                acted[jid] = entry
                print(f"[handoff] acted on: {jid} {labels.get(jid) or ''}")
            for jid in a.unact or []:
                if acted.pop(jid, None):
                    print(f"[handoff] un-acted: {jid} {labels.get(jid) or ''}")
                else:
                    print(f"[handoff] {jid} was not marked acted on")

    # Handle --merged flag
    if a.merged and a.commit:
        labels = {j["id"]: j.get("label") for j in load_jobs()}

        # Does the commit actually exist? Recorded, never enforced.
        #
        # Two bugs to avoid here, both of which shipped in the first version:
        #   1. it called run_bash(), which does not exist in this file -- a
        #      NameError swallowed by a bare `except: pass`, so `verified` was
        #      False for EVERY commit including real ones. A check that can
        #      only ever fail is not a check.
        #   2. it interpolated the repo path into a shell string, and the real
        #      path contains a space ("GitHub Projects"), so even with a real
        #      shell it would have split the argument.
        # subprocess with a list argv fixes both, and the except now narrows to
        # the failures that are genuinely expected.
        # ENFORCED (2026-09-10): a merge claim is only recorded as verified when the
        # commit is actually an ANCESTOR OF THE DEFAULT BRANCH -- not merely that the
        # SHA exists (a dispatch-worktree branch commit exists but has not landed).
        # If it is not an ancestor, REFUSE rather than record a false merged=True,
        # unless --allow-unverified-merge acknowledges a squash/rebase/remote-only case.
        landed = _commit_in_default_branch(a.repo, a.commit)
        if landed is not True and not a.allow_unverified_merge:
            if landed is False:
                print(f"[handoff] REFUSED: {a.commit} exists in {a.repo} but is NOT an ancestor "
                      f"of its default branch (main/master) -- it has not actually landed. This "
                      f"is exactly the false 'merged to main' record this check exists to prevent "
                      f"(job d31d96d23b29). If it merged via squash/rebase (different SHA) or the "
                      f"repo is not checked out here, pass --allow-unverified-merge.")
            else:
                print(f"[handoff] REFUSED: could not verify {a.commit} landed in {a.repo} (repo "
                      f"or commit not resolvable here). Pass --allow-unverified-merge to record "
                      f"it without the check.")
            return 2
        verified = landed is True

        # Mark as merged (this implies acted-on)
        with _locked_acted() as acted:
            acted[a.merged] = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                "how": "merged into code",
                "label": labels.get(a.merged),
                "merged": True,
                "commit": a.commit,
                "repo": a.repo,
                "verified": verified
            }
            print(f"[handoff] merged: {a.merged} -> {a.repo}@{a.commit} (verified: {verified})")

            # Also mark as acted on if not already done
            if a.merged not in acted:
                acted[a.merged] = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                  "how": "marked with --acted (merged)", "label": labels.get(a.merged)}
    
    # --acted --reason is recorded inside the --acted block above (and a merge-claim
    # reason is refused before any write) -- see the 2026-10-06 notes there.

    p, c, k = rebuild(None if (a.all or a.acted or a.unact) else a.job_id)
    print(f"[handoff] pending={p} complete(awaiting action)={c} acted={k} -> {OUT/'INDEX.md'}")
    if a.auto_process:
        env = dict(os.environ, HANDOFF_AUTOMATED="1")
        r = subprocess.run([sys.executable, str(AUTOCLEAR), "--auto"], capture_output=True, text=True,
                           timeout=900, env=env)
        for ln in (r.stdout or "").splitlines():
            if ln.strip():
                print("[handoff] auto-process: " + ln.strip())
    elif a.job_id and not (a.all or a.acted or a.unact):
        print("[handoff] auto-process: " + kick_autoclear(a.job_id))
    return 0


if __name__ == "__main__":
    sys.exit(main())
