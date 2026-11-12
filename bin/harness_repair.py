#!/usr/bin/env python3
"""In-place HARNESS REPAIR helpers (2026-10-10).

THE GAP. A slice ends "HARNESS CONVERGED, PREFLIGHT NO-GO -- worktree KEPT" with the
only blocker `verify-relevance` (the fixture's assertions are satisfied by BOTH the
baseline and the reference implementation, so mutants of the fix survive). The
escalation review answers verdict (c) harness defect and "proposed fix: none
written". Nothing acted: a human had to run the relevance gate, strengthen
test_fixture.py IN PLACE, re-run preflight --auto-seal and `--regate`.

dispatch-self-heal.py (heal_harness_repair / harness_repair_sweep) is the OWNER of
that action; this module holds the pieces that must be shared with the repair job's
own acceptance check (harness-repair-verify.py) and unit-tested without a queue:

  survey()         run the preflight (the relevance gate's only authority) --json and
                   extract the SURVIVING mutants + the blocker set
  survey_clean()   True only when the relevance gate reports verdict `relevant` (no blocker)
  repair_prompt()  the job's task text: exact survivors + the review's reasoning
  scope_violations() paths a repair touched that are not the slice's fixture file(s)

Nothing here weakens the gate: the repair job's success check IS the gate.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
PREFLIGHT = BIN / "ollama-dispatch-preflight"

# Files a repair may NEVER edit (mirrors dispatch-self-heal.PROTECTED_HARNESS plus the
# reference impl: moving the refimpl to dodge a mutant would weaken the proof).
PROTECTED = {"TASK.md", "verify.sh", "check_literals.py", "Package.swift", "REPO_MAP.md",
             "fix.patch", ".dispatch-harness.json", "auto-harness-check.py", "refimpl.py",
             "dispatch_env.py", "DispatchTests.csproj", "Directory.Build.props",
             "Directory.Build.targets", "Directory.Packages.props", ".gitignore"}
FALLBACK_FIXTURES = ("test_fixture.py", "verify.test.ts", "verify.test.mts",
                     "Tests/verifyTests.swift", "DispatchTests/VerifyImpl.cs")
# Untracked residue the harness itself leaves around; never a scope violation.
NOISE_RE = re.compile(
    r"(^|/)(__pycache__|node_modules|\.venv|\.pytest_cache|\.mypy_cache)(/|$)|\.pyc$|"
    r"(^|/)(auto-harness-check\.py|AUTO-TASK\.md|\.refine-guard\.json|\.preflight-state\.json|"
    r"relevance-signoff\.json|\.hand-harness|\.draft-unconfirmed)$")


def _load(p):
    try:
        return json.loads(Path(p).read_text())
    except Exception:
        return None


def fixture_files(wt, target=None):
    """PURE-ish. The slice's fixture file(s) a repair may edit: the manifest's
    `fixture` / `authored` names that are fixtures, never a protected file or the
    target."""
    man = _load(Path(wt) / ".dispatch-harness.json") or {}
    names = []
    if man.get("fixture"):
        names.append(str(man["fixture"]))
    names += [a for a in (man.get("authored") or []) if a in FALLBACK_FIXTURES]
    if not names:
        names = [f for f in FALLBACK_FIXTURES if (Path(wt) / f).is_file()]
    tgt = target or man.get("target")
    out = []
    for n in names:
        if n and n not in out and Path(n).name not in PROTECTED and n != tgt:
            out.append(n)
    return out


def parse_preflight_json(text):
    """PURE. The JSON object a `preflight --json` run printed (stdout may carry a
    banner before it), else None."""
    text = text or ""
    i = text.find("{")
    while i != -1:
        try:
            obj, _ = json.JSONDecoder().raw_decode(text[i:])
            if isinstance(obj, dict) and "verdict" in obj:
                return obj
        except ValueError:
            pass
        i = text.find("{", i + 1)
    return None


def survey_from_payload(data):
    """PURE. {parsed, verdict, relevance_blocked, survivors, other_blockers, reason}
    from a parsed preflight --json payload (None = unparseable)."""
    if not isinstance(data, dict):
        return {"parsed": False, "verdict": None, "relevance_blocked": False,
                "survivors": [], "other_blockers": [], "reason": "preflight printed no JSON"}
    blockers = [b for b in (data.get("blockers") or []) if isinstance(b, dict)]
    rel = [b for b in blockers if b.get("id") == "verify-relevance"
           or b.get("check") == "verify-relevance" or b.get("name") == "verify-relevance"]
    other = [b for b in blockers if b not in rel]
    vr = data.get("verify_relevance") or {}
    surv = [{k: sv.get(k) for k in ("file", "line", "mutation", "class", "snippet", "original",
                                    "io_adapter_candidate")}
            for sv in (vr.get("survivors") or []) if isinstance(sv, dict)]
    return {"parsed": True, "verdict": data.get("verdict"), "relevance_blocked": bool(rel),
            "survivors": surv, "other_blockers": [
                {k: b.get(k) for k in ("id", "check", "name", "message", "detail")} for b in other],
            "relevance_verdict": vr.get("verdict"),
            "reason": "; ".join(str(b.get("message") or b.get("detail") or "")[:200] for b in rel)}


def survey(wt, requires=(), run=subprocess.run, preflight=None, timeout=3000):
    """Run the relevance gate (the preflight, with --auto-seal exactly as the manual
    repair did) on the kept worktree and return survey_from_payload(...). Never raises."""
    pf = str(preflight or PREFLIGHT)
    cmd = [pf, str(wt), "--auto-seal", "--refimpl-cmd", "python3 refimpl.py %s" % wt,
           *["--require=%s" % r for r in requires], "--json"]
    try:
        r = run(cmd, capture_output=True, text=True, timeout=timeout)
    except Exception as e:
        return {"parsed": False, "verdict": None, "relevance_blocked": False, "survivors": [],
                "other_blockers": [], "reason": "preflight did not run: %s: %s" % (type(e).__name__, e)}
    out = survey_from_payload(parse_preflight_json(getattr(r, "stdout", "") or ""))
    if not out["parsed"]:
        out["reason"] += " (rc=%s) %s" % (getattr(r, "returncode", "?"),
                                         (getattr(r, "stderr", "") or "")[-200:])
    return out


def survey_clean(sv):
    """PURE. The relevance gate no longer blocks: parsed, NO verify-relevance blocker and
    the gate's own verdict is exactly "relevant". The gate is the authority -- a
    "relevant" verdict may still list a survivor it deems tolerable (e.g. an equivalent
    mutant under the threshold); demanding zero would fail a repair `--regate` would
    pass. UNPROVEN / low / unparseable are never clean."""
    return bool(sv.get("parsed")) and not sv.get("relevance_blocked") \
        and sv.get("relevance_verdict") == "relevant"


def repair_prompt(fixture, target, survivors, review_text="", other_blockers=(), task_text=None,
                  hints=None, line_map=None):
    """PURE. The repair job's task. `hints` carries the refine loop's own wording
    (SURVIVOR_KILL_RULE / REFINE_WORK_RULE / MOCK_FIDELITY_HINT from ollama-dispatch-auto)
    when importable; `line_map` the refimpl location of each survivor."""
    hints = hints or {}
    L = ["# HARNESS REPAIR -- strengthen the fixture IN PLACE (no re-authoring)\n",
         "The harness for this slice CONVERGED but the mutation-relevance gate says the fixture "
         "does not pin the property: the mutants below, each a wrong variant of the reference "
         "implementation, all still PASS your fixture. An escalation review diagnosed this as a "
         "HARNESS defect (the fixture's assertions are satisfied by both the baseline and a wrong "
         "build).\n",
         "## YOUR ONLY EDIT",
         "Edit `%s` and NOTHING else. `refimpl.py`, `TASK.md`, `verify.sh`, `check_literals.py`, "
         "the target `%s` and every other file are FROZEN -- do not touch them, and do not weaken "
         "or delete any existing case. Add the minimal adversarial case(s) to `%s` that FAIL when "
         "the reference implementation is changed as each survivor shows (fixture data must make "
         "the compared values DIFFER, e.g. distinct names/ids, so a mutant that swaps one for the "
         "other is caught).\n" % (fixture, target, fixture)]
    for k in ("SURVIVOR_KILL_RULE", "REFINE_WORK_RULE"):
        if hints.get(k):
            L.append(str(hints[k]).strip() + "\n")
    L.append("## Surviving mutants (exact output of the relevance gate)")
    lm = line_map or {}
    for sv in survivors:
        key = (sv.get("file"), sv.get("line"))
        m = lm.get(key) or {}
        orig = (m.get("original") or sv.get("original") or "").strip()
        snip = (sv.get("snippet") or "").strip()
        L.append("- `%s:%s` mutation %s%s%s -- went UNNOTICED" % (
            sv.get("file"), sv.get("line"), sv.get("mutation"),
            (" (original `%s`)" % orig) if orig else "",
            (" mutated to `%s`" % snip) if snip else ""))
    if other_blockers:
        L.append("\n## Other gate blockers (informational; you cannot fix them by editing the fixture)")
        for b in other_blockers:
            L.append("- %s: %s" % (b.get("id") or b.get("check") or b.get("name"),
                                   str(b.get("message") or "")[:200]))
    if review_text:
        L.append("\n## The escalation review's reasoning (read it; it names why the fixture is blind)")
        L.append("```\n%s\n```" % review_text.strip()[-2200:])
    if hints.get("MOCK_FIDELITY_HINT"):
        L.append("\n" + str(hints["MOCK_FIDELITY_HINT"]).strip())
    L.append("\n## Done means")
    L.append("`python3 auto-harness-check.py` prints VERIFY_OK AND the relevance gate reports no "
             "surviving mutant (the job's verify runs both; it prints the remaining survivors if "
             "any). Re-run it after every edit.")
    return "\n".join(L) + "\n"


def changed_paths(wt, base_head, run=subprocess.run):
    """Tracked paths changed since `base_head` (committed seals included) plus untracked
    non-noise files."""
    out = set()
    def _g(*a):
        r = run(["git", "-C", str(wt), *a], capture_output=True, text=True, timeout=120)
        return [l for l in (r.stdout or "").splitlines() if l.strip()]
    try:
        if base_head:
            out |= set(_g("diff", "--name-only", base_head))
        out |= set(_g("diff", "--name-only"))
        out |= set(_g("diff", "--name-only", "--cached"))
        out |= set(_g("ls-files", "--others", "--exclude-standard"))
    except Exception:
        pass
    return sorted(p for p in out if not NOISE_RE.search(p))


def scope_violations(wt, base_head, allowed, run=subprocess.run):
    """Paths a repair changed that are not in `allowed` (the slice's fixture file names)."""
    al = set(allowed)
    return [p for p in changed_paths(wt, base_head, run=run) if p not in al]


def main(argv=None):
    """Self-check CLI: `harness_repair.py --survey WT [--require=L ...]` prints the survey."""
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "--survey":
        reqs = [a.split("=", 1)[1] for a in argv[2:] if a.startswith("--require=")]
        print(json.dumps(survey(argv[1], reqs), indent=1))
        return 0
    print(__doc__)
    return 0


if __name__ == "__main__":
    sys.exit(main())
