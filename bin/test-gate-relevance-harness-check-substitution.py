#!/usr/bin/env python3
"""Regression test (2026-09-26): the gate's post-completion relevance measurement
must not use `python3 auto-harness-check.py` as the mutation-testing --verify for
an authoring/refine job.

auto-harness-check.py resets its declared target to git HEAD before doing
anything, then runs its own self-contained baseline/refimpl/verify/revert cycle.
That is correct for its documented job (certifying the AUTHORED harness is
satisfiable), but it makes it structurally blind when handed to verify-relevance.py
as the discriminator for mutating the model's ACTUAL diff: the mutation lives on
disk as an uncommitted edit to the target, and the reset wipes it before anything
is measured. Confirmed directly: running it on a clean, completely-unfixed
baseline still prints VERIFY_OK. Every mutant "survives" by construction, which
produced the spurious LOW/0.0 relevance verdict on job 5843edc8a589
(arr-deluge-false-supersede-915) and contributed to the "no tracked diff" verdict
on 1c94b432dcaa (arr-sonarr-new-request-priority) -- both escalated as needs-eyes
on exactly this pattern.

Fix: relevance_verify_cmd() in gate-on-complete.py substitutes `bash verify.sh`
(the actual authored fixture) whenever the job is auto-author-/auto-refine-
labeled and its own --verify is the harness self-check -- mirroring the
substitution advance_to_coding() already makes when sealing the fixture for the
real coding job. This calls the REAL function, not a re-implementation of it.

Red-on-revert: replace relevance_verify_cmd's body with `return verify` (the
pre-fix behaviour) and checks 1-2 fail -- the harness self-check is returned
unsubstituted for an authoring-labeled job.

Run: python3 test-gate-relevance-harness-check-substitution.py
"""
import importlib.util
import sys
from pathlib import Path

GATE = Path(__file__).resolve().parent / "gate-on-complete.py"

FAILED = []


def check(name, actual, expected):
    ok = actual == expected
    print(("PASS" if ok else "FAIL") + f": {name}" + ("" if ok else f" (got {actual!r}, want {expected!r})"))
    if not ok:
        FAILED.append(name)


def _load():
    spec = importlib.util.spec_from_file_location("goc_relevance_test", GATE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def main():
    m = _load()
    f = m.relevance_verify_cmd

    check("authoring job's harness-check verify is substituted with bash verify.sh",
          f("auto-author-arr-deluge-false-supersede-915", "python3 auto-harness-check.py"),
          "bash verify.sh")

    check("refine job's harness-check verify is substituted with bash verify.sh",
          f("auto-refine-arr-sonarr-new-request-priority-r1", "python3 auto-harness-check.py"),
          "bash verify.sh")

    check("a real coding job's own bash verify.sh is passed through unchanged",
          f("arr-deluge-false-supersede-915", "bash verify.sh"),
          "bash verify.sh")

    check("a non-authoring label with the harness-check string is passed through unchanged",
          f("some-other-job", "python3 auto-harness-check.py"),
          "python3 auto-harness-check.py")

    check("an authoring job with no verify at all stays None (measure_relevance abstains)",
          f("auto-author-x", None),
          None)

    if FAILED:
        print(f"\n{len(FAILED)} check(s) failed: {FAILED}")
        sys.exit(1)
    print("\nall checks passed")


if __name__ == "__main__":
    main()
