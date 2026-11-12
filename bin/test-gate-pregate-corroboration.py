#!/usr/bin/env python3
"""Regression test for Bug #14 (2026-09-18): the Unraid 14B PRE-gate reviewer has
fabricated defects. Two hardenings in gate-on-complete.py:

1. DIFF CORROBORATION: a pre-gate reviewer code finding that cites a file NOT in the
   actual diff is spurious (it is not about this change) and is dropped before it can
   drive an escalation. _diff_changed_files() parses the touched paths; _file_in_diff()
   matches a finding's cited file by exact path or basename.

2. (prompt) the authoritative regate context now forces the 27B to RE-DERIVE each
   claimed behavior rather than accept it because the quoted code exists -- not unit-
   testable here, asserted only that the instruction text is present.

Red-on-revert: make _file_in_diff always return True -> the "unrelated file is dropped"
assertion fails (nothing is ever dropped).

Run: python3 test-gate-pregate-corroboration.py
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

GATE = Path(__file__).resolve().parent / "gate-on-complete.py"


def _load():
    spec = importlib.util.spec_from_file_location("goc_t", GATE)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


failures = []


def ok(name, cond):
    print(f"  {'ok  ' if cond else 'FAIL'} {name}")
    if not cond:
        failures.append(name)


def main():
    m = _load()
    with tempfile.TemporaryDirectory() as td:
        diff = Path(td) / "x.diff"
        diff.write_text(
            "diff --git a/src/formfill.py b/src/formfill.py\n"
            "--- a/src/formfill.py\n+++ b/src/formfill.py\n@@\n+ changed line\n")
        files = m._diff_changed_files(diff)
        ok("diff parse finds the changed file", "src/formfill.py" in files)
        ok("finding on a file IN the diff is kept",
           m._file_in_diff("src/formfill.py", files) is True)
        ok("finding cited by bare basename is kept",
           m._file_in_diff("formfill.py", files) is True)
        ok("finding on a file NOT in the diff is dropped (revert-test bites here)",
           m._file_in_diff("other/unrelated.py", files) is False)
        ok("a finding with no file cited is kept (cannot disprove)",
           m._file_in_diff("", files) is True)
        ok("a missing diff yields an empty set (skip corroboration, keep findings)",
           m._diff_changed_files(Path(td) / "nope.diff") == set())

    # The authoritative-regate instruction hardening is present in the source.
    src = GATE.read_text()
    ok("regate context forces behavioral re-derivation (not code-presence)",
       "re-derive" in src.lower() and "unverified" in src.lower())

    if failures:
        print(f"\n{len(failures)} FAILED: {failures}")
        sys.exit(1)
    print("\nall pregate-corroboration tests passed")


if __name__ == "__main__":
    main()
