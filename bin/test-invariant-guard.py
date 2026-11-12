#!/usr/bin/env python3
"""Fixture harness for invariant-guard.py.

Covers the relocation-suppressor false-positive class (2026-09-04) alongside the
case the guard exists for (payout902). Each fixture is a unified diff plus a
`fires` expectation: True = a finding must be emitted, False = it must be
suppressed. The two REAL false positives from machine-config 1d4db91 are pulled
from git at runtime so the harness tracks the actual hunks, not a paraphrase.
"""
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import importlib.util

spec = importlib.util.spec_from_file_location(
    "invariant_guard", str(Path(__file__).resolve().parent / "invariant-guard.py"))
ig = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ig)


# --- synthetic fixtures -------------------------------------------------------

# (a) payout902 shape: a documented guard deleted, literal reappears NOWHERE.
#     MUST still fire -- this is the whole reason the guard exists.
PAYOUT902 = r"""diff --git a/src/sync.ts b/src/sync.ts
--- a/src/sync.ts
+++ b/src/sync.ts
@@ -40,7 +40,7 @@ export async function sync() {
   const overdue = await prisma.sale.findMany({
     where: {
       // Once paid (salePriceSynced=true), never re-stamp overdueAt --
       // otherwise the cleared badge re-appears on the next sync.
-      salePriceSynced: false,
       overdueAt: { not: null },
+      // recompute overdueAt every pass now
     },
   });
"""

# (b) relocation into a branch: the WHOLE removed line reappears verbatim in an
#     added else-branch line. MUST be suppressed.
RELOC_BRANCH = r"""diff --git a/app/run.py b/app/run.py
--- a/app/run.py
+++ b/app/run.py
@@ -10,8 +10,11 @@ def run(cfg):
     # The retry budget must never be recomputed mid-flight.
-    result = expensive_call(payload, retries=3)
+    if cfg.fast:
+        result = expensive_call(payload, retries=1)
+    else:
+        result = expensive_call(payload, retries=3)
     return result
"""

# (c) extract-into-variable: a QUOTED literal from the removed line reappears in
#     a different added line (the FP#1 shape, synthesised). MUST be suppressed.
RELOC_EXTRACT = r"""diff --git a/app/enqueue.py b/app/enqueue.py
--- a/app/enqueue.py
+++ b/app/enqueue.py
@@ -20,7 +20,9 @@ def enqueue(a):
     # Studio work goes through the queue, never a direct API call.
+    verify_cmd = "python3 draft-check.py"
     cmd = [QUEUE, "enqueue",
-           "--verify", "python3 draft-check.py",
+           "--verify", verify_cmd,
            "--label", a.label]
"""

# (d) negative control: a removed guard whose only reappearing fragment is the
#     bare word `false` (below _RELOC_MIN, quoted-only match not triggered).
#     MUST still fire -- proves short/common fragments are not treated as moves.
SHORT_FRAGMENT = r"""diff --git a/app/auth.py b/app/auth.py
--- a/app/auth.py
+++ b/app/auth.py
@@ -5,6 +5,7 @@ def check(u):
     # Never grant admin on an unverified account.
-    allow = u.verified and u.role == "adm"
+    log.info("verified=%s", u.verified)
     return False
"""

# (e) extend-in-place: a field ADDED to an object/select literal. The closing
#     `}` shifts right so the removed line's whole form does not reappear
#     verbatim -- _relocated misses it. This is the 2026-09-09 BFMR FP class
#     (shipped-flip: select {cancelled} -> {cancelled, bfmrStatus}). MUST be
#     suppressed by _extended_in_place.
EXTEND_IN_PLACE = r"""diff --git a/app/api/bfmr/submit.ts b/app/api/bfmr/submit.ts
--- a/app/api/bfmr/submit.ts
+++ b/app/api/bfmr/submit.ts
@@ -30,7 +30,7 @@ export async function submit() {
   const res = await prisma.orderBfmrLink.findMany({
     // The terminal-status set must never silently drop a status.
-    select: { cancelled: true },
+    select: { cancelled: true, bfmrStatus: true },
     where: { orderId },
   });
"""

# (f) revert-test control for the extend suppressor: a REAL guard deletion whose
#     removed line ALSO ends in a closing delimiter, but whose core reappears
#     nowhere. Proves _extended_in_place is bounded -- it does not blanket-
#     suppress every removed line that happens to end in `}`. MUST still fire.
CLOSER_DELETE = r"""diff --git a/app/gate.ts b/app/gate.ts
--- a/app/gate.ts
+++ b/app/gate.ts
@@ -12,7 +12,7 @@ function gate(order) {
     // Always keep the paid-lock branch; never remove it.
-    if (allPaid) { lockOrder(order) }
+    log.info("gate evaluated for order")
     return order
"""

SYNTHETIC = [
    ("payout902 (guard deleted, no reappearance)", PAYOUT902, True),
    ("relocation into branch (whole line moved)", RELOC_BRANCH, False),
    ("extract-into-variable (quoted literal moved)", RELOC_EXTRACT, False),
    ("short-fragment control (only `false` reappears)", SHORT_FRAGMENT, True),
    ("extend-in-place (field added to object literal)", EXTEND_IN_PLACE, False),
    ("closer-delete control (ends in `}`, no reappearance)", CLOSER_DELETE, True),
]


REAL_DIFF = (Path(__file__).resolve().parent / "test-fixtures" / "invariant-guard"
             / "machine-config-1d4db91-ollama-dispatch-draft.diff")


def real_hunks():
    """FP#1 (run_drafter) and FP#2 (mark_drafted) from the actual 1d4db91 diff.

    SELF-CONTAINED (2026-10-05): this used to run `git -C ~/bin/.. show 1d4db91`,
    which only worked when ~/bin's parent was the machine-config checkout -- from
    ~/bin it is $HOME, git printed nothing, and the suite died with StopIteration.
    The real hunks are vendored verbatim (git show 1d4db91 -- bin/ollama-dispatch-draft
    in machine-config) beside this test; a missing/edited fixture FAILS loudly."""
    try:
        diff = REAL_DIFF.read_text()
    except OSError as e:
        raise SystemExit(f"FAIL: vendored real-hunk fixture missing: {REAL_DIFF} ({e})")
    # Split the file diff into per-hunk diffs so each FP is scored in isolation
    # against the guard (each needs the +++ header to attribute a filename).
    header = "diff --git a/bin/ollama-dispatch-draft b/bin/ollama-dispatch-draft\n" \
             "--- a/bin/ollama-dispatch-draft\n+++ b/bin/ollama-dispatch-draft\n"
    hunks, cur = [], None
    for ln in diff.splitlines(keepends=True):
        if ln.startswith("@@"):
            if cur is not None:
                hunks.append(cur)
            cur = ln
        elif cur is not None:
            cur += ln
    if cur is not None:
        hunks.append(cur)
    fp1 = next((h for h in hunks if '"--verify", "python3 draft-check.py",' in h), None)
    fp2 = next((h for h in hunks if "if DRAFT_MARK not in new_body:" in h), None)
    if fp1 is None or fp2 is None:
        raise SystemExit(f"FAIL: {REAL_DIFF} no longer holds the two real 1d4db91 hunks")
    return [
        # FP#1: relocation -- literal moved into else-branch. MUST be suppressed.
        ("REAL FP#1 run_drafter (verify_cmd relocation)", header + fp1, False),
        # FP#2: generalization DRAFT_MARK->mark. Left advisory by design: the
        # token does not reappear verbatim, so it STILL fires (documented gap).
        ("REAL FP#2 mark_drafted (DRAFT_MARK->mark, advisory)", header + fp2, True),
    ]


def run():
    cases = list(SYNTHETIC) + real_hunks()
    fails = 0
    for name, diff, should_fire in cases:
        found = ig.check(diff)
        fired = len(found) > 0
        ok = fired == should_fire
        verb = "FIRES" if fired else "suppressed"
        want = "FIRE" if should_fire else "suppress"
        mark = "ok  " if ok else "FAIL"
        print(f"  [{mark}] {name}: {verb} (want {want})")
        if not ok:
            fails += 1
            for v in found:
                print(f"          -> removed: {v['removed']}")
    print(f"\n  {len(cases) - fails}/{len(cases)} passed")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(run())
