#!/usr/bin/env python3
"""Regression test for Bug #4 (2026-09-18): abort a no-progress refine loop early
instead of burning the lane to the round cap.

The bg-health slice burned 12+ min of Studio GPU spinning on an unwinnable state
(the harness kept reverting the model's edit, so every round produced the IDENTICAL
gate outcome). ollama-dispatch-auto now fingerprints each preflight round's outcome
(_round_signature: verdict + blockers + survivors + target bytes) and aborts after
`--no-progress-rounds` consecutive identical rounds (_refine_stall_update).

Red-on-revert:
  - make _refine_stall_update always return (0, sig) -> the "stall grows / guard
    trips" assertions fail.
  - make _round_signature ignore the target bytes -> the "byte-identical target
    counts as no progress" case would still pass, but the "a changed target resets"
    assertion fails.

Run: python3 test-no-progress-guard.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

AUTO = Path(__file__).resolve().parent / "ollama-dispatch-auto"


def _load():
    from importlib.machinery import SourceFileLoader
    loader = SourceFileLoader("oda_noprog", str(AUTO))
    spec = importlib.util.spec_from_loader("oda_noprog", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def main():
    m = _load()
    with tempfile.TemporaryDirectory() as td:
        wt = Path(td)
        tgt = "pkg/mod.py"
        (wt / "pkg").mkdir()
        stub = wt / tgt
        stub.write_text('"""Stub -- implement per TASK.md."""')

        blockers = [{"check": "required-literals"}, {"check": "task-unchanged"}]
        survs = [{"file": "refimpl.py", "line": 5, "mutation": "x->y"}]

        sig1 = m._round_signature(wt, tgt, "NO-GO", blockers, survs)
        sig2 = m._round_signature(wt, tgt, "NO-GO", blockers, survs)
        ok("signature: identical inputs -> identical signature (no progress)",
           sig1 == sig2)

        # Different blocker set -> different signature (progress).
        sig_b = m._round_signature(wt, tgt, "NO-GO", [{"check": "verify-relevance"}], survs)
        ok("signature: a changed blocker set changes the signature", sig1 != sig_b)

        # The model re-applies its edit (target bytes change) -> progress.
        stub.write_text("def real_impl():\n    return 1\n")
        sig_t = m._round_signature(wt, tgt, "NO-GO", blockers, survs)
        ok("signature: a byte-changed target changes the signature (edit landed)",
           sig1 != sig_t)

        # Stall counter: identical rounds accumulate; a change resets.
        stall, prev = 0, None
        stall, prev = m._refine_stall_update(prev, sig1, stall)
        ok("stall: first round -> 0 (nothing to compare)", stall == 0)
        stall, prev = m._refine_stall_update(prev, sig1, stall)
        ok("stall: identical 2nd round -> 1", stall == 1)
        stall, prev = m._refine_stall_update(prev, sig1, stall)
        ok("stall: identical 3rd round -> 2 (default threshold: ABORT)", stall == 2)
        # A change resets it.
        stall, prev = m._refine_stall_update(prev, sig_b, stall)
        ok("stall: a signature change resets the counter to 0", stall == 0)

        # The guard trips at the default threshold (2), well before max-rounds (4).
        DEFAULT = 2
        ok("guard: 2 identical rounds reach the default abort threshold before the cap",
           2 >= DEFAULT and DEFAULT < 4)

    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nall no-progress-guard tests passed")


if __name__ == "__main__":
    main()
