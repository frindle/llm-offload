#!/usr/bin/env python3
"""Cause -> owner table for the dispatch pipeline (one place that says WHICH mechanism
owns a failure cause, what state it must leave, and which mechanisms may not undo it).

WHY (2026-10-09). A failure gets fixed by one mechanism (gate, refresh_stale_base,
harness tree-dirt restore, handoff autoclear) and is then undone or contradicted by
another, because nothing recorded the ownership. Pregate vs regate verdicts flipped on
26 of 49 regated jobs. This table is that record; `lint()` keeps it honest against the
code, and `attribute_flip()` maps a real pregate->regate flip onto it.

Cause keys are "<namespace>:<name>":
  failure_class:*  ollama-queue.py classify_failure / _stamp_failure_class
  attempt:*        ollama-dispatch-auto classify_author_failure (ATTEMPT_CLASSES)
  gate_authority:* gate-on-complete.py payload["gate_authority"]
  fix_class:*      gate-on-complete.py autofix_classify / autofix_consider class
  harness_repair:* dispatch-self-heal.py HARNESS_REPAIR_CAUSES: a parked slice whose kept,
                   converged harness is blocked only by verify-relevance (2026-10-10)
  harness_admission:* harness_admission.py ADMISSION_CAUSES: why admit() refused to enqueue an
                   author/refine/coding job against a defective harness (2026-10-10)
  verdict_signal:* gate.json evidence that a verdict was set by a mechanism other
                   than the reviewer (used by attribute_flip)

CLI: python3 pipeline_cause_owners.py [--bin DIR] [--logs DIR]   (lint + live flip scan)
"""
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
Q, A, G, H, C = ("ollama-queue.py", "ollama-dispatch-auto", "gate-on-complete.py",
                 "handoff-emit.py", "completeness.py")
S, SL = "dispatch-self-heal.py", "ollama-dispatch-slice"
HA = "harness_admission.py"


def _o(file, func):
    return {"file": file, "func": func}


def _c(owner, leaves, forbid=(), note=""):
    return {"owner": owner, "leaves": list(leaves),
            "forbidden_undo": [_o(*f) for f in forbid], "note": note}


# State invariants are phrased as checkable facts about the world AFTER the owner ran.
CAUSES = {
    # ---- queue failure_class (one classifier, derived once at reap) -----------------
    "failure_class:harness": _c(_o(Q, "classify_failure"),
        ["failure_class=harness stamped once on the row and carried to done.json",
         "never relabelled stale-base / model afterwards",
         "job NOT counted against the model (no sample/ladder escalation on it)"],
        forbid=[(Q, "_stamp_progress")]),
    "failure_class:context": _c(_o(Q, "classify_failure"),
        ["failure_class=context (scaffold defect, not model incapacity)",
         "no model-side nsample escalation (NSAMPLE_CLASSES excludes it)"]),
    "failure_class:spec": _c(_o(Q, "classify_failure"),
        ["failure_class=spec; job parked via the spec-defect path, not re-run unchanged",
         "authoring rounds never get spec for unfinished TODO placeholders (that is model)",
         "an authoring round whose END-OF-RUN stage check is still red on a CONTENT rule (STAGE_FAIL: scaffold guard / no "
         "test cases / missing Must-contain literal), or whose staged job wrote another stage's file, is spec (stage contract "
         "or harness check), never model: no nsample / escalation, a plain requeue repeats it (2026-10-13)"],
        forbid=[(Q, "refresh_stale_base")]),
    "failure_class:model": _c(_o(Q, "classify_failure"),
        ["failure_class=model only when harness ran and spec was runnable",
         "unrelated base drift (touching none of the task's files) stays model"]),
    "failure_class:exploration-exhausted": _c(_o(Q, "classify_failure"),
        ["authoring round at its iteration cap, TODO placeholders unfilled, every tool call a read/diagnostic",
         "NOT a model verdict: excluded from NSAMPLE_CLASSES (re-sampling a too-big target repeats the paging)",
         "the fix is upstream: harness_admission oversize-reslice refuses the s1/s2 enqueue on a huge target"]),
    "failure_class:operator": _c(_o(Q, "classify_failure"),
        ["operator class never consumes an attempt (NON_ATTEMPT_CLASSES)"]),
    "failure_class:stale-base": _c(_o(Q, "refresh_stale_base"),
        ["worktree HEAD fast-forwarded/rebased onto base BEFORE launch, baseline stamped after",
         "failed rebase aborted: tree exactly as found (fail-open)",
         "resumed/continuation jobs' own tree never rewritten",
         "classifier relabels model->stale-base only when drift touches the task's files"],
        forbid=[(A, "restore_authoring_tree_to_base")],
        note="classify side: _stamp_failure_class; fix side: refresh_stale_base at launch"),
    "failure_class:nonconvergence_progressing": _c(_o(Q, "_stamp_progress"),
        ["only for authoring jobs with terminal_reason=nonconvergence and real progress",
         "still model-side for NSAMPLE purposes"]),
    # ---- authoring attempt classes (dispatch-auto owns the refine ladder) -----------
    **{f"attempt:{n}": _c(_o(A, "classify_author_failure"),
        ["same class twice = same wall; should_escalate keys on it",
         "attempt history recorded per RUN, not per bundle"])
       for n in ("refimpl-fails-own-tests", "syntax-error", "baseline-passes",
                 "stage-check-failed", "no-progress", "iteration-cap", "exploration-exhausted", "verify-missing",
                 "contaminated", "other", "harness-env", "stage-contract")},
    # ---- gate authority (who set the final verdict) ---------------------------------
    "gate_authority:studio-27b-regate": _c(_o(G, "merge_review"),
        ["regate verdict is final: pregate payload keeps pregate_verdict, regate='done'",
         "pregate-tier filters (uncorroborated / ungrounded-line drops) never applied to it"],
        forbid=[(G, "autofix_classify")],
        note="escalation enqueued by _escalate_regate"),
    "gate_authority:pregate-terminal-verify-exit": _c(_o(G, "merge_review"),
        ["deterministic verify failure is terminal: regate='not-warranted ...'",
         "no review can overturn it"]),
    "gate_authority:pregate-terminal-lowscope": _c(_o(G, "merge_review"),
        ["low input/scope only: regate not-warranted, no Studio-lane regate queued"]),
    "gate_authority:review-missing-terminal": _c(_o(G, "_resolve_missing_review"),
        ["missing review recorded as not_checked, never silently a pass"]),
    # ---- gate fix classes (autofix_classify decides, autofix_consider acts) ---------
    **{f"fix_class:{n}": _c(_o(G, "autofix_classify"),
        ["class + action + reasons recorded on the payload (auto_fix_*)"])
       for n in ("clean", "gate-error", "unattributable", "verify-red", "reviewer-code-high",
                 "no-product-change", "code-high", "scope", "undecidable",
                 "grader-suspect-source-text", "grader-suspect-relevance",
                 "grader-suspect-unproven")},
    "fix_class:harness-refine": _c(_o(G, "harness_refine_apply"),
        ["harness refine applied inline by the authoring loop; gate does not requeue it"]),
    **{f"fix_class:{n}": _c(_o(G, "autofix_consider"),
        ["action none/escalate recorded; the gate never requeues or escalates it"],
        forbid=[(G, "autofix_round")])
       for n in ("authoring-orchestrator-owned", "investigation-no-code",
                 "plangen-orchestrator-owned", "model-fallback", "grader-suspect",
                 "ladder-exhausted")},
    # ---- parked-slice in-place harness repair (dispatch-self-heal owns the action) ---
    **{f"harness_repair:{n}": _c(_o(S, "heal_harness_repair"),
        ["ONE repair job (auto-refine-<plan>-<sid>-r10N, slice bundle, slice's local model) is "
         "enqueued in the KEPT worktree; none while one is in flight (ledger + queue label scan)",
         "at most HARNESS_REPAIR_MAX attempts per slice (self-heal.json repairs.n), then the "
         "existing park + one deduped alert (final_rung)",
         "edits confined to the fixture file(s): a scope violation is reverted and counts as failed",
         "the worktree is never deleted and the gate is never bypassed: success = VERIFY_OK AND the "
         "relevance gate no longer blocks, then the existing --regate re-enters the slice",
         "no survivors named / not a verify-relevance NO-GO -> nothing here, existing ladder applies"],
        forbid=[(S, "_retry_with_notes"), (SL, "retry_slice")],
        note="sweep side: harness_repair_sweep (dispatch-self-heal.py --job-sweep, every watcher pass)")
       for n in ("converged-nogo-verify-relevance", "review-c-relevance-survivors")},
    # ---- harness admission gate (harness_admission.admit owns the refusal) ------------
    **{f"harness_admission:{n}": _c(_o(HA, "admit"),
        ["NO author/refine/continuation/coding job is enqueued: admit() returns ok=False with this "
         "named cause and an ADMISSION_REFUSED-marked message (own marker, NOT SPEC_DEFECT: never auto-sliced, no further model round)",
         "state recorded outside the worktree (fingerprint-keyed); cleared ONLY by the harness changing "
         "(repair path: heal_harness_repair / --retry-slice / --regate) and re-passing the gate",
         "repair jobs (-r10N) are exempt from refusal but the next enqueue re-runs the whole gate"],
        forbid=[(A, "resolve_runaway")],
        note="enqueue sites: ollama-dispatch-auto _dispatch_model_inner + auto_continue_go, "
             "ollama-dispatch-slice confirm_and_enqueue, ollama-queue.py cmd_enqueue (backstop)")
       for n in ("fixture-untraceable", "baseline-not-red", "refimpl-unapplicable", "refimpl-syntax",
                 "contract-broken", "flaky-fixture", "fixture-type-mismatch",
                 "oversize-reslice", "reslice-required", "repeated-defect", "refine-no-change")},
    "harness_admission:frozen-harness-mutated": _c(_o(HA, "enforce_freeze"),
        ["a COMPLETE harness (TASK.md + fixture + refimpl.py, none a stub/placeholder) admitted with a MEASURED-green "
         "contract is snapshotted (incl. verify.sh / check_literals.py) with the fingerprint recorded at snapshot time; "
         "a non-staged auto-author job (-cN/-esc/-fresh) that modified it has the files restored and the event logged",
         "staged -s1/-s2/-s3, refine and repair rounds are NEVER reverted; a same-path re-scaffold or a removed "
         "worktree clears the state + .snap instead of restoring it"]),
    "harness_admission:refusal-respec": _c(_o(SL, "bound_stale_worktree_retry"),
        ["an ADMISSION_REFUSED failure spends the slice's re-spec budget (respec_on_cap, RESPEC_CAP) exactly ONCE and "
         "is a NON-ATTEMPT (author_attempts / author_launches_total handed back); is_spec_defect is False for it",
         "budget spent -> ESCALATED with a repair hint (--retry-slice / --regate); the removed worktree takes its "
         "admission state with it"],
        note="single owner of the refusal re-spec budget; auto only stops the rounds (NO_MORE_ROUNDS_PREFIXES)"),
    # ---- verdict signals (evidence the verdict was set by a non-reviewer mechanism) --
    "verdict_signal:untrusted-baseline": _c(_o(Q, "seal_prev_round_baseline"),
        ["continuation round launches from a CLEAN, ATTRIBUTABLE baseline (prev round sealed)",
         "tracked target at base for auto-author jobs (restore_authoring_tree_to_base)",
         "gate's launch-baseline guard NOT loosened; harness residue not called UNTRUSTED"],
        forbid=[(G, "effective_launch_baseline")],
        note="detector: gate-on-complete.apply_baseline sets payload['untrusted'] -> pass downgraded"),
}

# gate.json evidence -> the cause that produced it (attribute_flip candidates).
SIGNALS = (
    ("gate_authority:studio-27b-regate", lambda d: d.get("gate_authority") == "studio-27b-regate"),
    ("gate_authority:pregate-terminal-verify-exit",
     lambda d: d.get("gate_authority") == "pregate-terminal-verify-exit"),
    ("gate_authority:pregate-terminal-lowscope",
     lambda d: d.get("gate_authority") == "pregate-terminal-lowscope"),
    ("gate_authority:review-missing-terminal",
     lambda d: d.get("gate_authority") == "review-missing-terminal"),
    ("verdict_signal:untrusted-baseline", lambda d: bool(d.get("untrusted"))),
)


def owner_key(cause):
    o = CAUSES[cause]["owner"]
    return f"{o['file']}:{o['func']}"


def attribute_flip(gate):
    """gate = parsed *.gate.json. Returns {"flip": bool, "causes": [...], "owners": [...],
    "status": "owned" | "unowned" | "double-owned" | "no-flip"}."""
    pre, fin = gate.get("pregate_verdict"), gate.get("verdict")
    if pre is None or pre == fin:
        return {"flip": False, "causes": [], "owners": [], "status": "no-flip"}
    causes = [c for c, pred in SIGNALS if pred(gate)]
    # A tier's gate_authority has the final word on the verdict; a verdict_signal (e.g. a
    # dirty launch baseline) is an INPUT the authority already folded into its issues, so
    # it is a contributor, not a second owner. It owns the flip only when no authority did.
    authority = [c for c in causes if c.startswith("gate_authority:")]
    contributors = [c for c in causes if c.startswith("verdict_signal:")] if authority else []
    causes_owning = authority or causes
    owners = sorted({owner_key(c) for c in causes_owning})
    status = "unowned" if not owners else ("owned" if len(owners) == 1 else "double-owned")
    return {"flip": True, "pre": pre, "final": fin, "causes": causes, "owners": owners,
            "contributors": sorted({owner_key(c) for c in contributors}), "status": status}


# ---------------------------------------------------------------------------- lint
def _read(bin_dir, name):
    try:
        return (Path(bin_dir) / name).read_text(errors="replace")
    except OSError:
        return ""


def _slice(src, start_pat, end_pat=r"\n(?=def |class |# ---)"):
    m = re.search(start_pat, src)
    if not m:
        return ""
    e = re.search(end_pat, src[m.end():])
    return src[m.start(): m.end() + (e.start() if e else len(src))]


def referenced_causes(bin_dir):
    """Causes the CODE emits, extracted from the source (not from this table)."""
    q, a, g = _read(bin_dir, Q), _read(bin_dir, A), _read(bin_dir, G)
    out = set()
    # failure_class: returns inside the two classifiers + explicit stamps.
    for fn in ("classify_failure", "classify_runner_failure"):
        body = _slice(q, rf"\ndef {fn}\(")
        out |= {f"failure_class:{x}" for x in re.findall(r'return "([a-z_-]+)",', body)}
    out |= {f"failure_class:{x}" for x in re.findall(r'job\["failure_class"\] = "([\w-]+)"', q)}
    out |= {f"failure_class:{x}" for x in re.findall(r'\n\s+cls = "([\w-]+)"', _slice(q, r"\ndef _stamp_failure_class\("))}
    out |= {f"failure_class:{x}" for x in re.findall(r'"(nonconvergence_progressing)"', _slice(q, r"\ndef _stamp_progress\("))}
    # attempt classes
    m = re.search(r"ATTEMPT_CLASSES = \(([^)]*)\)", a)
    if m:
        out |= {f"attempt:{x}" for x in re.findall(r'"([\w-]+)"', m.group(1))}
    out |= {f"attempt:{x}" for x in re.findall(r'return "([\w-]+)"', _slice(a, r"\ndef classify_author_failure\("))}
    # gate authority
    out |= {f"gate_authority:{x}" for x in re.findall(r'\["gate_authority"\] = "([\w-]+)"', g)}
    # in-place harness repair causes (dispatch-self-heal.py)
    m = re.search(r"HARNESS_REPAIR_CAUSES = \(([^)]*)\)", _read(bin_dir, S))
    if m:
        out |= {f"harness_repair:{x}" for x in re.findall(r'"([\w-]+)"', m.group(1))}
    m = re.search(r"ADMISSION_CAUSES = \(([^)]*)\)", _read(bin_dir, HA))
    if m:
        out |= {f"harness_admission:{x}" for x in re.findall(r'"([\w-]+)"', m.group(1))}
    # fix classes
    body = _slice(g, r"\ndef autofix_classify\(", r"\ndef autofix_round\(")
    out |= {f"fix_class:{x}" for x in re.findall(r'\{"class": "([\w-]+)"', body)}
    body = _slice(g, r"\ndef harness_refine_apply\(", r"\ndef autofix_classify\(")
    out |= {f"fix_class:{x}" for x in re.findall(r'\{"class": "([\w-]+)"', body)}
    body = _slice(g, r"\ndef autofix_consider\(", r"\n(?=def )")
    out |= {f"fix_class:{x}" for x in re.findall(r'auto_fix_class"\] = "([\w-]+)"', body)}
    out |= {f"fix_class:{x}" for x in re.findall(r'dec = \{"class": "([\w-]+)"', body)}
    return out


def _has_func(src, func):
    return bool(re.search(rf"^\s*(?:async )?def {re.escape(func)}\(|^{re.escape(func)}\s*=", src, re.M))


def lint(bin_dir=HERE):
    """Problems (list of str); [] = table and code agree. Checks: every cause the code
    emits is in CAUSES; every CAUSES key is still emitted (or is a verdict_signal whose
    detector exists); every owner / forbidden_undo function exists; invariants non-empty."""
    problems = []
    seen = referenced_causes(bin_dir)
    for c in sorted(seen - set(CAUSES)):
        problems.append(f"cause referenced in code but missing from table: {c}")
    for c in sorted(set(CAUSES) - seen):
        if c.startswith("verdict_signal:"):
            continue
        problems.append(f"table cause not found in code (stale?): {c}")
    for c, spec in CAUSES.items():
        if not spec["leaves"]:
            problems.append(f"{c}: no state invariants")
        for kind, o in [("owner", spec["owner"])] + [("forbidden_undo", f) for f in spec["forbidden_undo"]]:
            if not _has_func(_read(bin_dir, o["file"]), o["func"]):
                problems.append(f"{c}: {kind} {o['file']}:{o['func']} does not exist")
        if spec["owner"] in spec["forbidden_undo"]:
            problems.append(f"{c}: owner listed as forbidden to undo itself")
    for c, _ in SIGNALS:
        if c not in CAUSES:
            problems.append(f"signal maps to unknown cause {c}")
    return problems


def scan_flips(logs_dir):
    res = []
    for f in sorted(Path(logs_dir).glob("*.gate.json")):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        r = attribute_flip(d)
        if r["flip"]:
            r["job"] = d.get("job_id") or f.name.split(".")[0]
            res.append(r)
    return res


def main(argv):
    bin_dir, logs = HERE, HERE / "ollama-queue-logs"
    if "--bin" in argv:
        bin_dir = Path(argv[argv.index("--bin") + 1])
    if "--logs" in argv:
        logs = Path(argv[argv.index("--logs") + 1])
    probs = lint(bin_dir)
    for p in probs:
        print("LINT", p)
    flips = scan_flips(logs)
    by = {}
    for r in flips:
        by.setdefault(r["status"], []).append(r["job"])
    print(f"causes={len(CAUSES)} lint_problems={len(probs)} flips={len(flips)} "
          + " ".join(f"{k}={len(v)}" for k, v in sorted(by.items())))
    for k in ("unowned", "double-owned"):
        for j in by.get(k, []):
            print(f"  {k}: {j}")
    return 1 if probs else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
