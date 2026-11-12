#!/usr/bin/env python3
"""Behavioural tests for the worker's resume-time iteration budget.

WHY THIS FILE EXISTS
--------------------
`total_iters` was unconditionally `resumed_at_iteration + max_iters`, so EVERY
resume bought a full fresh budget on top of the iterations already burned.
`max_iters` was a per-resume allowance, not a bound on the job.

Observed (bg-crypto s3, job c71f0581d998, 2026-09-19): the job was externally
SIGTERM'd and resumed six times and its ceiling walked

    7/14  ->  8/21  ->  12/24  ->  13/26  ->  13/26  ->  ...

running 09:24 to 15:29 -- about six hours -- on a nominal 14-iteration budget
that never once bound it. This compounds with the daemon's kickstart-orphans
bug: every involuntary SIGTERM produces a resume, so the more the queue churns
the less max_iters means.

The fix distinguishes a DELIBERATE grant from an INVOLUNTARY pause. The attack
case is the involuntary one -- an exemption on a cost control is only as good
as its narrowness -- and the counter-attack case is that a restored ceiling
must never be so low that the resume becomes a silent no-op.

Run it directly; it needs no queue, model, network or git.
"""
import importlib.util
import sys
from pathlib import Path

WORKER = Path(__file__).resolve().parent / "ollama-worker.py"

_spec = importlib.util.spec_from_file_location("_ollama_worker_under_test", WORKER)
_w = importlib.util.module_from_spec(_spec)
sys.modules["_ollama_worker_under_test"] = _w
_spec.loader.exec_module(_w)

_n = 0
_fails = []


def check(cond, label):
    global _n
    _n += 1
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        _fails.append(label)


rti = _w.resume_total_iters

# ---- a fresh run is unaffected -------------------------------------------
check(rti(0, 14, None) == 14, "fresh run: ceiling is exactly max_iters")
check(rti(0, 30, None) == 30, "fresh run: ceiling tracks max_iters")

# ---- deliberate grants still extend --------------------------------------
# This is the documented review-gate flow: the model asks, the run pauses, a
# human resumes with more budget. Breaking this would strand every legitimate
# extension, so it is pinned as explicitly as the restriction is.
check(rti(7, 14, "request_more_iterations") == 21,
      "request_more_iterations still extends by a full max_iters")
check(rti(12, 14, "context_threshold") == 26,
      "context_threshold still extends by a full max_iters")

# ---- ATTACK: the involuntary pause must NOT buy more budget --------------
# These are the exact numbers from c71f0581d998's transcript. Under the old
# `resumed_at_iteration + max_iters` they were 21 and 26.
check(rti(7, 14, "external_sigterm") == 14,
      "external_sigterm resume restores the ORIGINAL ceiling (14, not 21)")
check(rti(12, 14, "external_sigterm") == 14,
      "a later external_sigterm resume still restores 14, not 26")

# The runaway is cumulative, so pin that repeated involuntary resumes do not
# ratchet: the ceiling must be the same after the sixth as after the first.
ceilings = {rti(n, 14, "external_sigterm") for n in (3, 7, 9, 12, 13)}
check(ceilings == {14},
      "repeated external_sigterm resumes do not ratchet the ceiling")

# An unknown/novel pause reason is treated as involuntary -- a new pause code
# added later must not silently become a budget grant by default.
check(rti(7, 14, "chat_request_failed") == 14,
      "an unrecognised pause reason grants no budget (fail closed)")
check(rti(7, 14, "verify_uninformative") == 14,
      "verify_uninformative grants no budget")

# ---- COUNTER-ATTACK: a restored ceiling must not strand the job ----------
# Trading a runaway budget for a dead job would be a worse bug. If more
# iterations were already burned than max_iters allows, the ceiling must still
# leave range() non-empty rather than silently ending the run.
check(rti(20, 14, "external_sigterm") == 20,
      "ceiling never drops below the iterations already completed")
check(rti(20, 14, "external_sigterm") >= 20,
      "an over-budget resume does not produce an empty iteration range")

# ---- the grant set is a closed, explicit allowlist -----------------------
check("external_sigterm" not in _w.BUDGET_GRANTING_PAUSE_REASONS,
      "external_sigterm is NOT in the budget-granting set")
check({"request_more_iterations", "context_threshold"} <= _w.BUDGET_GRANTING_PAUSE_REASONS,
      "the two deliberate grants ARE in the budget-granting set")

print()
if _fails:
    print(f"resume budget: {_n - len(_fails)}/{_n} passed -- FAILURES:")
    for f in _fails:
        print(f"  - {f}")
    sys.exit(1)
print(f"resume budget: {_n}/{_n} passed")
print("RESUME_BUDGET_OK")
