#!/usr/bin/env python3
"""The research "Every web_fetch call in this session has failed" nudge must not fire on
a research job that verified against LOCAL files (2026-10-06).

esc-review jobs (24123dd86140, 77d808c3984a, a580c8793aa7, ~10 earlier) read the
escalation file + worktree with read_file/list_files, made zero web_fetch calls, and
were told their sources were unread; each final answer argued with the harness instead
of diagnosing. should_stamp_unverified already exempted local reads (8d690b764cec); the
nudge and the DID-NOT-CONVERGE banner did not.

Tests the REAL decision function and its wiring in ollama-worker.py (WORKER env var
points it at a backup to prove the suite goes RED there)."""
import importlib.util, os, re, sys

WORKER = os.environ.get("WORKER", "/Users/user/bin/ollama-worker.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("  ok   " if ok else "  FAIL ") + name + ("" if ok else f": got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


from importlib.machinery import SourceFileLoader
_ld = SourceFileLoader("ow_nudge", WORKER)
spec = importlib.util.spec_from_loader("ow_nudge", _ld)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
f = getattr(m, "should_send_zero_fetch_nudge", None)
check("decision function exists", callable(f), True)
if callable(f):
    check("research, 0 fetch, local reads -> NO nudge", f("research", False, 3, False, False), False)
    check("research, 0 fetch, 0 reads -> nudge", f("research", False, 0, False, False), True)
    check("research, fetched -> no nudge", f("research", True, 0, False, False), False)
    check("already sent -> no nudge", f("research", False, 0, False, True), False)
    check("facts_provided -> no nudge", f("research", False, 0, True, False), False)
    check("coding -> no nudge", f("coding", False, 0, False, False), False)
src = open(WORKER).read()
i = src.find('"content": "Every web_fetch call in this session has failed')
pre = src[max(0, i - 700):i]
check("nudge site gated by should_send_zero_fetch_nudge", "should_send_zero_fetch_nudge(" in pre, True)
j = src.find("without ever completing a verified answer, and no web_fetch call")
pre2 = src[max(0, j - 700):j]
check("DID-NOT-CONVERGE banner honours local reads",
      bool(re.search(r"should_stamp_unverified\([^)]*local_read_count", pre2)), True)
print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
