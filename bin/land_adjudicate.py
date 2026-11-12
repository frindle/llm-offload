#!/usr/bin/env python3
"""land_adjudicate.py -- CONCERNS / disputed verdict adjudication by a headless Sonnet call.

Replaces the human `ollama-queue.py accept-bundle` read-and-decide for a chain whose gate ended
CONCERNS (or whose reviewers disagree). A `claude -p --model sonnet` call returns exactly one of

    accept    the concerns are non-blocking / reviewer false positives (flags land-ready, NEVER lands)
    re-run    the spec is fine, the attempt was bad: run the coding dispatch again
    re-spec   the spec/harness is the defect: amend the intent and re-author

and this module EXECUTES re-run / re-spec automatically (relaunching the driver's recorded argv,
land_go.reauthor_cmd). `accept` only attaches the adjudication; in REVIEW mode the chain then
waits for the final reviewer, in AUTO mode a CONCERNS chain stays auto-ineligible (research (f):
an LLM may demote or flag, never approve). Research: 2026-10-09-autoland-research.md (b),(c),(e).

Anti-flip-flop: the decision is cached by hash(inputs, model, prompt version) and a recorded
decision changes only when the inputs change (new diff / new findings). Caps (research (e)
step 5): re-run <= 2, re-spec <= 1 per task; the same diff hash twice => re-spec, not re-run.
A refusal / usage-limit / unparseable answer is `unavailable` -- never an implicit accept.
"""
import hashlib
import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import land_go  # noqa: E402

PROMPT_VERSION = "adj-v1"
MODEL = os.environ.get("LAND_ADJ_MODEL", "sonnet")
DECISIONS = ("accept", "re-run", "re-spec")
RERUN_CAP, RESPEC_CAP = 2, 1

PROMPT = """You adjudicate ONE finished local-LLM code change whose automated gate ended CONCERNS.
You have no tools and must not ask questions. Decide from the evidence below only.

Choose exactly one:
  accept    - every concern is non-blocking or a reviewer false positive; the change matches the task
  re-run    - the task/harness is sound but this attempt is wrong or incomplete; run the model again
  re-spec   - the task or harness is the defect (ambiguous, unsatisfiable, tests the wrong property)

Rules: a concern backed by a quoted line that exists in the diff outweighs one that is not. Do not
accept when a grounded BLOCKING finding, a failing repo check, or an unnecessary-hunk/wrong-solution
survivor is listed. When unsure between accept and re-run, choose re-run.

Answer with ONE line, nothing after it:
VERDICT: {"decision":"accept|re-run|re-spec","reasons":["..."],"amendment":"<for re-spec: the text to append to the task intent, else empty>"}

=== EVIDENCE ===
%s
"""


def evidence(packet, task_intent="", diff_text="", max_diff=24000):
    g = packet.get("gate") or {}
    slim = {
        "chain": packet.get("chain"), "gate_verdict": g.get("verdict"), "review_verdict": g.get("review_verdict"),
        "task_intent": (task_intent or "")[:3000],
        "relevance": packet.get("relevance"), "checks": (packet.get("checks") or {}).get("status"),
        "grounded_findings": [{k: f.get(k) for k in ("file", "line", "rule", "severity", "what", "blocking", "quote_checked")}
                              for f in (packet.get("findings") or [])][:20],
        "dropped_as_ungrounded": len(packet.get("findings_dropped") or []),
        "risk": packet.get("risk"), "facts": {k: (packet.get("facts") or {}).get(k) for k in
                                              ("n_files", "loc", "outside_scope", "tests_weakened", "new_deps")},
        "blockers": packet.get("blockers"),
    }
    return json.dumps(slim, indent=1, default=str) + "\n\n--- diff (truncated) ---\n" + (diff_text or "")[:max_diff]


def input_hash(ev, model=MODEL):
    return hashlib.sha256(f"{PROMPT_VERSION}\0{model}\0{ev}".encode()).hexdigest()


def run_claude(prompt, timeout=420, model=MODEL):
    env = dict(os.environ, CLAUDE_HEADLESS_DIAGNOSIS="1")
    try:
        r = subprocess.run(["claude", "-p", "--model", model, "--tools", "", "--no-session-persistence"],
                           input=prompt, capture_output=True, text=True, timeout=timeout, env=env)
        return r.returncode, r.stdout or "", r.stderr or ""
    except (OSError, subprocess.TimeoutExpired) as e:
        return 127, "", f"{type(e).__name__}: {e}"


def parse_answer(text):
    """-> dict(decision, reasons, amendment) or None. Requires the VERDICT: line (a usage-limit
    refusal arrives on stdout looking like an answer; without the marker it is not one)."""
    for ln in reversed((text or "").strip().splitlines()):
        m = re.match(r"^\s*VERDICT:\s*(\{.*\})\s*$", ln)
        if m:
            try:
                d = json.loads(m.group(1))
            except ValueError:
                return None
            dec = str(d.get("decision") or "").strip().lower().replace("_", "-").replace("rerun", "re-run").replace("respec", "re-spec")
            if dec in DECISIONS:
                return {"decision": dec, "reasons": [str(x)[:300] for x in (d.get("reasons") or [])][:6],
                        "amendment": str(d.get("amendment") or "")[:2000]}
    return None


def adjudicate(packet, task_intent="", diff_text="", cache_dir=None, history=None, claude=run_claude, model=MODEL):
    """-> adjudication dict {decision|'unavailable', reasons, amendment, model, input_hash, cached, forced}"""
    ev = evidence(packet, task_intent, diff_text)
    h = input_hash(ev, model)
    cp = Path(cache_dir) / f"{h}.json" if cache_dir else None
    if cp and cp.is_file():
        try:
            rec = json.loads(cp.read_text())
            rec["cached"] = True
            return rec
        except (OSError, ValueError):
            pass
    rc, out, err = claude(PROMPT % ev, model=model) if claude is run_claude else claude(PROMPT % ev)
    ans = parse_answer(out)
    if ans is None:
        return {"decision": "unavailable", "reasons": [f"no parseable VERDICT (rc={rc}): {(out or err)[:160]!r}"],
                "amendment": "", "model": model, "input_hash": h, "cached": False}
    rec = dict(ans, model=model, input_hash=h, cached=False, at=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
    # policy guards the model cannot talk its way past: a grounded blocker / red check forbids accept
    if rec["decision"] == "accept" and (packet.get("blockers") and any(
            b for b in packet["blockers"] if "CONCERNS" not in b)):
        rec.update(decision="re-run", forced="accept overridden: mechanical blocker present")
    # no-progress: same diff already re-run once -> re-spec
    hist = history or []
    same = [x for x in hist if x.get("diff_sha256") == packet.get("diff_sha256") and x.get("executed") == "re-run"]
    if rec["decision"] == "re-run" and same:
        rec.update(decision="re-spec", forced="same diff re-run already tried: re-run -> re-spec")
    if cp:
        try:
            cp.parent.mkdir(parents=True, exist_ok=True)
            tmp = cp.with_name(cp.name + f".tmp{os.getpid()}")
            tmp.write_text(json.dumps(rec))
            os.replace(tmp, cp)
        except OSError:
            pass
    return rec


def execute(adj, label, history=None, spawner=None, runs_dir=None, dry=False):
    """Carry out re-run / re-spec. -> {"executed": 're-run'|'re-spec'|'accept'|None, "ok": bool, "detail": str}"""
    d = adj.get("decision")
    hist = history or []
    n_rerun = sum(1 for x in hist if x.get("executed") == "re-run")
    n_respec = sum(1 for x in hist if x.get("executed") == "re-spec")
    if d == "accept":
        return {"executed": "accept", "ok": True, "detail": "flagged land-ready with the adjudication attached"}
    if d not in ("re-run", "re-spec"):
        return {"executed": None, "ok": False, "detail": "no actionable decision: " + "; ".join(adj.get("reasons") or [])}
    if d == "re-run" and n_rerun >= RERUN_CAP:
        d = "re-spec"
    if d == "re-spec" and n_respec >= RESPEC_CAP:
        return {"executed": None, "ok": False, "detail": f"re-run/re-spec budget exhausted ({n_rerun} re-run, {n_respec} re-spec): parked, one digest line"}
    cmd, err = land_go.reauthor_cmd(label, runs_dir, amend=(adj.get("amendment") or "see adjudication reasons: "
                                                            + "; ".join(adj.get("reasons") or [])) if d == "re-spec" else None)
    if cmd is None:
        return {"executed": None, "ok": False, "detail": err}
    if dry:
        return {"executed": d, "ok": True, "detail": "would run: " + " ".join(cmd[:3]) + " ...", "cmd": cmd}
    (spawner or land_go._spawn)(cmd, str(land_go.DISPATCH / f"{label}.land-{d}.log"))
    return {"executed": d, "ok": True, "detail": "relaunched " + d, "cmd": cmd[:3]}
