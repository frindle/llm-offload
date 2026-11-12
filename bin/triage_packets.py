#!/usr/bin/env python3
"""triage_packets -- read-only TRIAGE HANDOFF over the failure ledger (the owner 2026-10-06).

NOT a headless model run. The ledger sweep writes ONE packet per distinct unresolved failure
SIGNATURE under ~/.ollama-dispatch/triage/<slug>.md plus index.json; a human-driven Opus/Claude
session picks them up via `triage-emit.py` (modeled on handoff-emit.py) and a SessionStart panel.

  refresh()      rebuild packets from failures.jsonl. FAIL-OPEN, never raises.
                 * new signature            -> packet written, status open
                 * signature already open   -> NOT re-emitted as a new item (packet text refreshed
                                               in place only if new rows joined it)
                 * acted, then NEW rows     -> reopened (history keeps what was tried)
                 * every row superseded     -> closed as superseded
                 operator-stop / cancelled are never packets.
  open_items()   [{id, signature, n_jobs, repeat, oldest, ...}] repeats first, then oldest
  act(ids, outcome, reason, commit, job, ref)   record FIXED|ESCALATED|WONTFIX; future packets
                 include this history
Real state: index.json only (history of outcomes); every packet is derived and rebuildable.
"""
import fcntl
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.path.expanduser("~"))
_LED = os.environ.get("FAILURE_LEDGER")
# A sandboxed ledger (tests) gets a sandboxed triage dir: never write the real one by accident.
DIR = Path(os.environ.get("TRIAGE_DIR") or (Path(_LED).parent / "triage" if _LED
                                            else HOME / ".ollama-dispatch" / "triage"))
INDEX = DIR / "index.json"
PLANS = Path(os.environ.get("TRIAGE_PLANS") or HOME / ".ollama-dispatch" / "slice-plans")
WORKTREES = Path(os.environ.get("TRIAGE_WORKTREES") or HOME / ".ollama-dispatch" / "worktrees")
OUTCOMES = ("FIXED", "ESCALATED", "WONTFIX")
SKIP = ("operator-stop", "cancelled")

FIX_STANDARD = """\
REQUIRED FIX STANDARD (non-negotiable)
- Fix the ROOT CAUSE, not this instance: no bare --retry-slice, no raised caps, no "it passed on retry".
- Fix the GENERATION side too (the plan generator / author prompt / scaffold that produced the
  defect), not only the one plan or worktree.
- Every file edit: back up first as <file>.bak-<UTC ts>-<tag>.
- Every pipeline-tooling change: a BEHAVIOURAL test AND a REVERT test that fails with the fix removed.
- Log the fix in the vault: Claude/Agent-Dispatch-Log-2026-10.md via REST
  (python3 ~/bin/vault_conf.py url|auth; header 'Authorization: <auth output>').
- NEVER work around a hook or auto-mode classifier denial: stop and report it. No queue daemon
  restart, no touching .qwen-manual, no hand-edits to a main checkout, no polling loops.
- Close the packet: python3 ~/bin/triage-emit.py --acted <id> --outcome FIXED|ESCALATED|WONTFIX
  --reason "..." [--commit SHA] [--job JOBID] [--ref TEXT]
"""


def _iso(t=None):
    return datetime.fromtimestamp(t or time.time(), timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def slugify(sig):
    return re.sub(r"[^A-Za-z0-9._-]+", "-", str(sig)).strip("-")[:80] or "unknown"


def _fl():
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import failure_ledger
    return failure_ledger


def _load_index():
    try:
        d = json.loads(INDEX.read_text())
        if isinstance(d.get("signatures"), dict):
            return d
    except Exception:
        pass
    return {"signatures": {}}


def _save_index(d):
    DIR.mkdir(parents=True, exist_ok=True)
    tmp = INDEX.with_suffix(".tmp")
    tmp.write_text(json.dumps(d, indent=1, sort_keys=True))
    os.replace(tmp, INDEX)


class _lock:
    def __enter__(self):
        DIR.mkdir(parents=True, exist_ok=True)
        self.f = open(DIR / ".lock", "w")
        fcntl.flock(self.f, fcntl.LOCK_EX)
        return self

    def __exit__(self, *a):
        try:
            fcntl.flock(self.f, fcntl.LOCK_UN)
        finally:
            self.f.close()


def _groups(rows):
    sup = {r.get("job_id") for r in rows if r.get("event") == "superseded"}
    g = {}
    for r in rows:
        if r.get("event") or not r.get("signature") or r["signature"] in SKIP:
            continue
        g.setdefault(r["signature"], []).append((r, r["job_id"] in sup))
    return g


def _tail(lines, n):
    return [str(x) for x in (lines or [])][-n:]


def _plan_slice(plan, sid):
    try:
        d = json.loads((PLANS / f"{plan}.slices.json").read_text())
        sl = d.get("slices", d) if isinstance(d, dict) else d
        it = sl.items() if isinstance(sl, dict) else [(s.get("id"), s) for s in sl]
        for k, s in it:
            if k == sid or (isinstance(s, dict) and s.get("id") == sid):
                return s
    except Exception:
        pass
    return None


def build_packet(sig, rows, entry):
    """Markdown for one signature. `rows` oldest-first ledger rows (superseded included)."""
    first, last = rows[0], rows[-1]
    plan, sid, bundle = last.get("plan"), last.get("slice"), last.get("bundle")
    L = [f"# TRIAGE: {sig}", "",
         f"- id: `{entry['slug']}`   status: **{entry['status']}**   opened: {entry.get('opened_ts')}"
         + (f"   reopened: {entry['reopened_ts']}" if entry.get("reopened_ts") else ""),
         f"- failed jobs with this signature: **{len(rows)}** (first {first.get('ts')}, last {last.get('ts')})",
         "- affected (plan / slice, jobs): " + ", ".join(
             "`%s / %s` x%d" % (k[0], k[1], n) for k, n in sorted(_where(rows).items(), key=lambda kv: (str(kv[0][0] or ""), str(kv[0][1] or "")))),
         f"- newest: bundle `{bundle}` plan `{plan}` slice `{sid}`", ""]
    L += ["## Why this needs eyes",
          "A repeated signature means the earlier fix (if any) did not address the cause, or the "
          "generation side still produces the defect." if len(rows) > 1 else
          "New signature: first time this failure shape is seen.", ""]
    if entry.get("history"):
        L.append("## What was already tried (prior triage outcomes -- do NOT repeat these)")
        for h in entry["history"]:
            L.append(f"- {h.get('ts')} **{h.get('outcome')}**: {h.get('reason')}"
                     + (f" commit={h['commit']}" if h.get("commit") else "")
                     + (f" job={h['job']}" if h.get("job") else "")
                     + (f" ref={h['ref']}" if h.get("ref") else "")
                     + f"  (covered {len(h.get('covered') or [])} job(s))")
        L.append("")
    L.append("## Jobs (oldest first)")
    for r, sup in [(r, s) for r, s in rows_with_sup(rows, entry)]:
        fp = r.get("fingerprint") or {}
        L.append(f"### {r['job_id']}  {r.get('label')}{'  [superseded]' if sup else ''}")
        L.append(f"- {r.get('ts')} round={r.get('round')} model={r.get('model')} host={r.get('host')} "
                 f"exit={r.get('exit')} verdict={r.get('verdict')} reason={r.get('reason')} class={r.get('class')}")
        L.append(f"- detail: {r.get('detail')}")
        L.append(f"- iterations {r.get('iterations')}/{r.get('iteration_cap')}, duration {r.get('duration_s')}s, "
                 f"tags {r.get('tags')}")
        L.append(f"- same_as: {r.get('same_as')} ({r.get('same_as_scope')})   same failing cases as: {r.get('same_cases_as')}")
        if fp:
            L.append(f"- fingerprint: tools={fp.get('tool_counts')} longest_readonly_streak={fp.get('longest_readonly_streak')} "
                     f"verified_after_last_edit={fp.get('verified_after_last_edit')}")
            L.append(f"  top_read={fp.get('top_read')}  top_written={fp.get('top_written')}  rewrites={fp.get('rewrites')}")
        for c in (r.get("failed_cases") or [])[:20]:
            L.append(f"- FAILED CASE: {c}")
        L.append("")
    chain, cur, byid = [], last, {r["job_id"]: r for r in rows}
    while cur and cur.get("same_as") and len(chain) < 20:
        chain.append(cur["same_as"])
        cur = byid.get(cur["same_as"])
    L += ["## Repeat chain (SAME_AS, newest to oldest)", "  " + (" -> ".join([last["job_id"]] + chain)), ""]
    L += ["## Latest verify output (tail)", "```"] + _tail(last.get("verify_tail"), 30) + ["```", ""]
    if len(rows) > 1:
        L += [f"## Previous failure's verify tail ({rows[-2]['job_id']})", "```"] + _tail(rows[-2].get("verify_tail"), 12) + ["```", ""]
    pf = PLANS / f"{plan}.slices.json" if plan else None
    wt = WORKTREES / f"wt-slice-{plan}-{sid}" if plan and sid else None
    paths = last.get("paths") or {}
    L.append("## Paths")
    for k, v in (("plan file", pf), ("worktree", wt), ("livelog (newest)", paths.get("livelog")),
                 ("run log", paths.get("runlog")), ("transcript", paths.get("transcript")),
                 ("harness check", HOME / "bin" / "auto-harness-check.py"),
                 ("failure ledger", _fl().LEDGER)):
        if v:
            L.append(f"- {k}: `{v}`" + ("" if Path(str(v)).exists() else "  (missing now)"))
    s = _plan_slice(plan, sid) if plan else None
    if s:
        L += ["", "## Slice plan entry (intent / pins)", "```json",
              json.dumps({k: s.get(k) for k in ("id", "intent", "must_contain", "files", "depends_on", "verify_shape")
                          if k in s}, indent=1)[:3000], "```"]
    try:
        if plan and sid:
            L += ["", "## Attempt counter (the one counter)", "- " + _fl().slice_counter(plan, sid)["text"]]
    except Exception:
        pass
    L += ["", "## Inspect / repro commands",
          f"- python3 ~/bin/qctl failures --by signature",
          f"- python3 ~/bin/qctl failures --job {last['job_id']}",
          f"- python3 ~/bin/qctl timeline {plan} {sid}" if plan and sid else "- python3 ~/bin/qctl failures --since 24h",
          f"- python3 ~/bin/failure_ledger.py show {last['job_id']}",
          f"- tail -n 120 '{paths.get('livelog', '<livelog>')}'", ""]
    L += ["## Suggested first steps",
          "1. Read the newest livelog and the run-log result section; confirm the fingerprint matches what you see.",
          "2. Decide: plan defect, pipeline/tool defect, model nonconvergence, or environment. Cite log lines.",
          "3. Check the PRIOR OUTCOMES above, then `git log`/`.bak-*` for the files you intend to change.",
          "4. Fix at the source (plan AND generator), prove with a behavioural + revert test, then retry the slice only if it will not wipe a converged harness.",
          "", FIX_STANDARD]
    return "\n".join(L) + "\n"


def _where(rows):
    w = {}
    for r in rows:
        k = (r.get("plan"), r.get("slice"))
        w[k] = w.get(k, 0) + 1
    return w


def rows_with_sup(rows, entry):
    sup = set(entry.get("_sup") or [])
    return [(r, r["job_id"] in sup) for r in rows]


def refresh():
    """Rebuild packets from the ledger. Returns {'opened':[], 'reopened':[], 'updated':[], 'closed':[]}.
    FAIL-OPEN."""
    out = {"opened": [], "reopened": [], "updated": [], "closed": []}
    try:
        fl = _fl()
        groups = _groups(fl.load_rows())
        with _lock():
            idx = _load_index()
            sigs = idx["signatures"]
            for sig, pairs in groups.items():
                rows = [r for r, _ in pairs]
                live = [r["job_id"] for r, s in pairs if not s]
                ids = [r["job_id"] for r in rows]
                e = sigs.get(sig)
                if e is None:
                    if not live:
                        continue            # born superseded: nothing to triage
                    e = sigs[sig] = {"slug": slugify(sig), "status": "open", "opened_ts": _iso(),
                                     "history": [], "job_ids": ids}
                    out["opened"].append(e["slug"])
                else:
                    covered = {j for h in e.get("history") or [] for j in h.get("covered") or []}
                    new_live = [j for j in live if j not in covered]
                    if e["status"] in ("acted", "superseded") and new_live:
                        e["status"] = "open"
                        e["reopened_ts"] = _iso()
                        out["reopened"].append(e["slug"])
                    elif e["status"] == "open" and not live:
                        e["status"] = "superseded"
                        out["closed"].append(e["slug"])
                    elif e["status"] == "open" and set(ids) != set(e.get("job_ids") or []):
                        out["updated"].append(e["slug"])
                    elif e["status"] != "open":
                        e["job_ids"] = ids
                        continue
                    e["job_ids"] = ids
                if e["status"] == "open":
                    e["_sup"] = [r["job_id"] for r, s in pairs if s]
                    DIR.mkdir(parents=True, exist_ok=True)
                    (DIR / (e["slug"] + ".md")).write_text(build_packet(sig, rows, e))
                    e.pop("_sup", None)
                    e["live_jobs"] = len(live)
            _save_index(idx)
    except Exception as ex:
        try:
            print(f"[triage-packets] refresh failed (ignored): {ex}", file=sys.stderr)
        except Exception:
            pass
    return out


def _epoch(ts):
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except Exception:
        return 0


def list_items(status="open"):
    idx = _load_index()
    items = []
    for sig, e in idx["signatures"].items():
        if status and e.get("status") != status:
            continue
        n = e.get("live_jobs") or len(e.get("job_ids") or [])
        items.append({"id": e["slug"], "signature": sig, "status": e.get("status"),
                      "n_jobs": n, "repeat": n >= 2, "opened_ts": e.get("opened_ts"),
                      "reopened_ts": e.get("reopened_ts"), "packet": str(DIR / (e["slug"] + ".md")),
                      "history": e.get("history") or []})
    items.sort(key=lambda i: (not i["repeat"], _epoch(i["opened_ts"])))
    return items


def open_summary():
    """Cheap, never raises: {'open': N, 'repeats': M, 'oldest': ts|None}."""
    try:
        it = list_items("open")
        return {"open": len(it), "repeats": sum(1 for i in it if i["repeat"]),
                "oldest": min((i["opened_ts"] for i in it), default=None)}
    except Exception:
        return {"open": 0, "repeats": 0, "oldest": None}


def act(ids, outcome, reason, commit=None, job=None, ref=None, unact=False):
    """Mark packets acted (or reopen with unact). Returns (ok, message)."""
    try:
        if not unact:
            if outcome not in OUTCOMES:
                return False, "outcome must be one of %s" % "/".join(OUTCOMES)
            if not (reason or "").strip():
                return False, "--reason is required"
            if outcome == "FIXED" and not (commit or job or ref):
                return False, "FIXED needs --commit, --job or --ref (what landed / which retry)"
        with _lock():
            idx = _load_index()
            by_slug = {e["slug"]: (s, e) for s, e in idx["signatures"].items()}
            for i in ids:
                if i not in by_slug:
                    return False, "unknown triage id: %s" % i
            for i in ids:
                sig, e = by_slug[i]
                if unact:
                    e["status"] = "open"
                    continue
                e["status"] = "acted"
                e.setdefault("history", []).append({
                    "ts": _iso(), "outcome": outcome, "reason": reason, "commit": commit,
                    "job": job, "ref": ref, "covered": list(e.get("job_ids") or [])})
            _save_index(idx)
        return True, "ok"
    except Exception as ex:
        return False, "error: %s" % ex
