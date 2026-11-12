#!/usr/bin/env python3
"""Local escalation ladder (offload plan step 9). PURE: no I/O except env reads.

Ordered rungs: harness -> resume -> sampling -> second_model -> reslice -> claude (needs_opus).
Rungs with no mechanism yet (second_model, reslice) are listed and skipped with a reason.
Budget: the queue's per-task GPU cap (ttc_task_cap_s) is the source; the ladder only ever
lowers it (min with HARD_CEILING_S) and never raises it. Ships SHADOW (log only): the caller
audits decide()'s result and applies nothing. Kill switch: ESCALATION_LADDER=0."""
import os

RUNGS = ("harness", "resume", "sampling", "second_model", "reslice", "claude")
HARD_CEILING_S = 7200.0          # ~2h absolute per-task ceiling
NOT_IMPLEMENTED = {"second_model": "no second-local-model mechanism yet",
                   "reslice": "no automatic re-slice mechanism yet"}


def enabled(env=None):
    env = os.environ if env is None else env
    return str(env.get("ESCALATION_LADDER", "1")).strip() != "0"


def effective_cap_s(task_cap_s):
    """Never raises the existing cap; adds the hard ceiling."""
    if task_cap_s is None:
        return HARD_CEILING_S
    return min(float(task_cap_s), HARD_CEILING_S)


def harness_verdict(admission):
    """Wrap an existing harness_admission result ({ok, cause, message, ...}) or a job-side
    refusal string. -> (ok, cause). None/empty = no evidence of a bad harness (ok)."""
    if not admission:
        return True, None
    if isinstance(admission, str):
        bad = "ADMISSION_REFUSED" in admission or "HARNESS ADMISSION REFUSED" in admission
        return (not bad), ("admission-refused" if bad else None)
    if admission.get("ok") is False:
        return False, admission.get("cause") or "admission-refused"
    return True, None


def decide(cont_decision, active_s=0.0, task_cap_s=None, admission=None, rungs_done=(),
           sampling_mode="off"):
    """-> {"rung", "action", "skipped": [{"rung","reason"}], "cap_s"}.
    cont_decision is continuation_decide()'s tuple. rungs_done lists rungs already spent."""
    cap = effective_cap_s(task_cap_s)
    skipped = []
    done = set(rungs_done or ())

    def skip(r, why):
        skipped.append({"rung": r, "reason": why})

    for r in RUNGS:
        if r == "harness":
            ok, cause = harness_verdict(admission)
            if not ok:
                return {"rung": "harness", "action": "repair_harness", "cause": cause,
                        "skipped": skipped, "cap_s": cap}
            skip(r, "harness ok")
        elif float(active_s or 0) >= cap and r != "claude":
            skip(r, f"budget spent ({float(active_s or 0):.0f}s >= {cap:.0f}s)")
        elif r in done:
            skip(r, "already used")
        elif r == "resume":
            if cont_decision and cont_decision[0] == "continue":
                return {"rung": r, "action": "resume", "extra_iterations": cont_decision[1],
                        "skipped": skipped, "cap_s": cap}
            skip(r, cont_decision[1] if cont_decision and len(cont_decision) > 1 else "no decision")
        elif r == "sampling":
            if sampling_mode == "live":
                return {"rung": r, "action": "best_of_k", "skipped": skipped, "cap_s": cap}
            skip(r, f"nsample mode is {sampling_mode}")
        elif r in NOT_IMPLEMENTED:
            skip(r, NOT_IMPLEMENTED[r])
        elif r == "claude":
            return {"rung": r, "action": "needs_opus", "skipped": skipped, "cap_s": cap}
    return {"rung": "claude", "action": "needs_opus", "skipped": skipped, "cap_s": cap}
