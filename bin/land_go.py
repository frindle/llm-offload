#!/usr/bin/env python3
"""land_go.py -- HARNESS GO automation: no human, no model. Touches ONLY the dispatch worktree
and the queue (never a product repo), so it is automatic in BOTH land modes.

The 'HARNESS GO -- relevance review' row used to say: READ the fixture, then
ollama-dispatch-draft --confirm, preflight --auto-seal, enqueue. ollama-dispatch-auto already
auto-continues when it is the driver (auto_continue_go); this module handles the rows that
PAUSED (older drivers, `--no-auto-continue`, relevance unmeasured) with the SAME measured
decision, made stricter by the necessity / wrong-solution-bank gates (verify-relevance.py,
commit 930d7f8):

  ENQUEUE   verdict GO, no blockers, verify RED on the baseline, relevance score >= MIN_SCORE
            with enough evidence mutants, necessity gate clean (no unnecessary hunk), wrong-bank
            clean (no surviving wrong solution)  ->  draft --confirm, preflight --auto-seal,
            enqueue --label X --bundle X
  REAUTHOR  anything else (below threshold, necessity/bank survivors, relevance or necessity
            NOT MEASURED, contract not proven)  ->  send back to the authoring loop via the
            driver's recorded argv + --resume-harness (never to a human); capped per label/day

The decision is pure (decide_go). Execution takes injectable runners so tests need no queue.
"""
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HOME = Path(os.environ.get("LAND_HOME_DIR") or Path.home())
BIN = Path(os.environ.get("LAND_BIN_DIR") or (HOME / "bin"))
DISPATCH = HOME / ".ollama-dispatch"
MIN_SCORE = 0.85
MIN_EVIDENCE = 3
REAUTHOR_CAP_PER_DAY = 3
TEST_TARGET_RE = re.compile(r"(\.|_)(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$|(^|/)tests?/", re.I)


def load_json(p, default=None):
    try:
        return json.loads(Path(p).read_text())
    except (OSError, ValueError):
        return default


def decide_go(state, harness=None, min_score=MIN_SCORE, min_evidence=MIN_EVIDENCE):
    """state = the worktree's .preflight-state.json (or preflight --json). Pure.
    -> {"action": "enqueue"|"reauthor", "reasons": [...], "numbers": {...}}"""
    harness = harness or {}
    why = []
    vr = (state or {}).get("verify_relevance") or {}
    nec, bank = vr.get("necessity"), vr.get("wrong_bank")
    nums = {"score": vr.get("score"), "threshold": vr.get("threshold"), "killed": vr.get("killed"),
            "evidence_mutants": vr.get("evidence_mutants"), "relevance_verdict": vr.get("verdict")}
    if (state or {}).get("verdict") != "GO":
        why.append(f"preflight verdict is {(state or {}).get('verdict')!r}, not GO")
    if (state or {}).get("blockers"):
        why.append("preflight has blockers: " + ", ".join(str(b.get("check") if isinstance(b, dict) else b)
                                                         for b in state["blockers"][:4]))
    creation = bool(harness.get("creation_task"))
    if (state or {}).get("verify_failed_at_baseline") is not True and not creation:
        why.append("fail-before not proven: verify did not fail on the untouched baseline")
    score, ev = vr.get("score"), vr.get("evidence_mutants") or 0
    target = str(harness.get("target") or "")
    test_target = bool(TEST_TARGET_RE.search(target))
    nec_ok = bool(nec) and nec.get("enabled", True) is not False and not nec.get("untested")
    nec_measured = nec_ok and (nec.get("checked") or 0) >= 1
    if score is None or ev < min_evidence:
        if test_target and not (vr.get("survivors") or []):
            pass   # a locked test-file target has no mutable product code: relevance N/A (as auto)
        elif nec_measured and not (nec.get("unnecessary")) and nec.get("necessary") == nec.get("checked"):
            pass   # no line-mutant evidence, but every added hunk is individually proven necessary
        else:
            why.append(f"relevance NOT measured ({ev} evidence mutants, score {score}; need >= {min_evidence} "
                       "or a clean per-hunk necessity result)")
    else:
        floor = max(min_score, float(vr.get("threshold") or 0))
        if score < floor:
            why.append(f"relevance score {score} below threshold {floor} ({vr.get('killed')}/{ev} killed)")
        if vr.get("verdict") not in ("relevant", "unproven", "low"):
            why.append(f"relevance verdict {vr.get('verdict')!r}")
        if vr.get("verdict") == "unproven" and not vr.get("truncated"):
            why.append("relevance verdict unproven for a reason other than the time budget: "
                       + str(vr.get("reason"))[:120])
    if nec is None:
        why.append("necessity gate NOT MEASURED (preflight state predates it) -- needs a fresh preflight")
    else:
        if nec.get("unnecessary"):
            why.append(f"necessity: {len(nec['unnecessary'])} unnecessary hunk(s)")
        if nec.get("untested"):
            why.append(f"necessity: {len(nec['untested'])} hunk(s) untested")
    if bank is None:
        why.append("wrong-solution bank NOT MEASURED (preflight state predates it) -- needs a fresh preflight")
    elif bank.get("survivors"):
        why.append(f"wrong-solution bank: {len(bank['survivors'])} wrong solution(s) pass the fixture")
    return {"action": "reauthor" if why else "enqueue", "reasons": why, "numbers": nums}


def _count_recent(ledger, label, kind, window=86400):
    n, now = 0, time.time()
    try:
        for ln in Path(ledger).read_text().splitlines():
            try:
                r = json.loads(ln)
            except ValueError:
                continue
            if r.get("label") == label and r.get("kind") == kind and now - r.get("epoch", 0) < window:
                n += 1
    except OSError:
        pass
    return n


def argv_record(label, runs_dir=None):
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(label or "")).strip("-")
    return load_json(Path(runs_dir or DISPATCH / "auto-runs") / "argv" / f"{safe}.json")


def _opt(argv, flag):
    for i, x in enumerate(argv):
        if x == flag and i + 1 < len(argv):
            return argv[i + 1]
        if x.startswith(flag + "="):
            return x.split("=", 1)[1]
    return None


def reauthor_cmd(label, runs_dir=None, amend=None):
    """Relaunch the recorded driver argv with --resume-harness (+ optional intent amendment)."""
    rec = argv_record(label, runs_dir)
    if not rec or not rec.get("argv"):
        return None, f"no recorded driver argv for {label!r}"
    av = [x for x in rec["argv"] if x not in ("--resume-harness", "--resume-author")]
    if amend:
        for i, x in enumerate(av):
            if x == "--intent" and i + 1 < len(av):
                av[i + 1] = av[i + 1] + "\n\nADJUDICATOR AMENDMENT: " + amend
                break
    return [sys.executable, str(BIN / "ollama-dispatch-auto"), *av, "--resume-author" if amend else "--resume-harness"], ""


def queue_has_label(label, runner):
    rc, out = runner([sys.executable, str(BIN / "ollama-queue.py"), "status"])
    return bool(re.search(r"(?m)^\[[a-z_ ]+\]\s+[0-9a-f]{6,}\s+" + re.escape(label) + r"(\s|$)", out or ""))


def capture(cmd, timeout=3600, cwd=None):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=cwd)
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, f"{type(e).__name__}: {e}"


def execute_go(wt, label, decision=None, runner=capture, spawner=None, ledger=None, runs_dir=None, dry=False):
    """Act on the decision. -> {"action":..., "ok":bool, "detail":...}. Logs to the ledger."""
    wt = Path(wt)
    st = load_json(wt / ".preflight-state.json") or {}
    hn = load_json(wt / ".dispatch-harness.json") or {}
    d = decision or decide_go(st, hn)
    out = {"label": label, "worktree": str(wt), "decision": d["action"], "reasons": d["reasons"],
           "numbers": d["numbers"], "ok": False, "detail": ""}
    ledger = Path(ledger or DISPATCH / "landing" / "go-ledger.jsonl")

    def log(kind, **kw):
        try:
            ledger.parent.mkdir(parents=True, exist_ok=True)
            with ledger.open("a") as fh:
                fh.write(json.dumps(dict(label=label, kind=kind, epoch=time.time(),
                                         at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **kw)) + "\n")
        except OSError:
            pass

    if d["action"] == "reauthor":
        if _count_recent(ledger, label, "reauthor") >= REAUTHOR_CAP_PER_DAY:
            out["detail"] = f"re-author cap ({REAUTHOR_CAP_PER_DAY}/day) reached: parked for the summary"
            log("reauthor-capped", reasons=d["reasons"])
            return out
        cmd, err = reauthor_cmd(label, runs_dir)
        if cmd is None:
            out["detail"] = err
            log("reauthor-failed", error=err, reasons=d["reasons"])
            return out
        out["detail"] = "relaunch: " + " ".join(cmd[:3]) + " ..."
        if dry:
            out["ok"] = True
            return out
        (spawner or _spawn)(cmd, str(DISPATCH / f"{label}.land-reauthor.log"))
        out["ok"] = True
        log("reauthor", reasons=d["reasons"], numbers=d["numbers"])
        return out

    # ENQUEUE: the same three steps auto_continue_go runs, re-checked after --confirm.
    if dry:
        out["ok"], out["detail"] = True, "would: draft --confirm; preflight --auto-seal; enqueue --label %s" % label
        return out
    if queue_has_label(label, runner):
        out["ok"], out["detail"] = True, "already enqueued (label present in the queue)"
        log("already-enqueued")
        return out
    if runner is capture and not (BIN / "ollama-dispatch-draft").is_file():
        out["detail"] = "old draft/preflight tools retired (use mini-swe)"
        log("tools-retired", detail=out["detail"])
        return out
    rc, o = runner([sys.executable, str(BIN / "ollama-dispatch-draft"), str(wt), "--confirm"])
    if rc != 0:
        out["detail"] = f"draft --confirm failed rc={rc}: {o[-200:]}"
        log("confirm-failed", detail=out["detail"])
        return out
    rec = argv_record(label) or {}
    av = rec.get("argv") or []
    cmd = [sys.executable, str(BIN / "ollama-dispatch-preflight"), str(wt), "--json", "--auto-seal",
           "--refimpl-cmd", "python3 refimpl.py"] + [f"--require={r}" for i, r in enumerate(av[1:], 1)
                                                      if av[i - 1] == "--require"]
    rc, o = runner(cmd)
    try:
        d2 = json.loads(o)
    except ValueError:
        d2 = {"verdict": "PARSE-ERROR"}
    again = decide_go(d2, hn)
    if again["action"] != "enqueue":
        out["decision"], out["detail"] = "reauthor", "re-check after --confirm was not clean: " + "; ".join(again["reasons"])[:300]
        log("recheck-not-clean", reasons=again["reasons"])
        return out
    d_def = load_json(DISPATCH / "defaults.json") or {}
    model = _opt(av, "--model") or d_def.get("model")
    host = _opt(av, "--host") or d_def.get("host")
    cmd = [sys.executable, str(BIN / "ollama-queue.py"), "enqueue", "--model", str(model), "--host", str(host),
           "--cwd", str(wt), "--task-file", str(wt / "TASK.md"), "--task-kind", "coding",
           "--verify", "bash verify.sh", "--label", label, "--bundle", rec.get("bundle") or label]
    rc, o = runner(cmd)
    if rc != 0:
        out["detail"] = f"enqueue refused rc={rc}: {o[-200:]}"
        log("enqueue-refused", detail=out["detail"])
        return out
    out["ok"], out["detail"] = True, "enqueued: " + o.strip()[-200:]
    log("enqueued", numbers=d["numbers"])
    return out


def _spawn(cmd, logpath):
    """Default spawner. Retired: the old auto driver it relaunched is gone (use mini-swe),
    so this only records the refusal in the log and launches nothing."""
    with open(logpath, "ab") as fh:
        fh.write(b"old auto driver retired -- nothing launched (use mini-swe)\n")
