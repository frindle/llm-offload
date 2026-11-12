#!/usr/bin/env python3
"""Fix C (2026-10-03): escalation label + review context point at the right thing.

  * slicer run summary carries each slice's TRUE reason (escalation_kind), pinned on
    replay-endorse's real reason texts -- it used to stamp s3 AND s4b "relevance NO-GO"
  * watcher build_context names <slice_worktree or chain_worktree>/<target> as the code
    under review and marks `repo` as the base -- s4b's self-heal review read the
    testproject MAIN file and "refuted" the escalation against the wrong code
  * no worktree -> context unchanged

Usage: python3 ~/bin/test-escalation-label-context.py
Revert: SLICE=~/bin/ollama-dispatch-slice.bak-esclabel
        WATCHER=~/bin/dispatch-escalation-watcher.py.bak-esclabel python3 this.py -> FAIL
"""
import contextlib
import importlib.util
import io
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLICE = Path(os.environ.get("SLICE", HERE / "ollama-dispatch-slice"))
WATCHER = Path(os.environ.get("WATCHER", HERE / "dispatch-escalation-watcher.py"))
fails = 0


def chk(name, actual, expected):
    global fails
    ok = actual == expected
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": expected {expected!r} got {actual!r}"))
    fails += 0 if ok else 1


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


# ---------------- C (slicer summary) -----------------------------------------
sl = load(SLICE, "slice_t")
# Real reason texts (pinned: the live chain keeps moving). s3/s4b as escalated at
# 2026-10-04T01:56Z (BUNDLE PARKED row), s3 again after attempt 4 converged.
REASONS = {
    "s3-endorsement-selection": ("authoring has now been launched 6 times for this slice without "
                                 "ever producing a complete harness. The failure reason kept CHANGING"),
    "s4b-integrate-collapse": ("vacuous must_contain gate: every must_contain literal ('per-reservation "
                               "collapse', 'reservationId', 'smallest id', 'selectCanonicalBfmrLinks') "
                               "is ALREADY present"),
    "s3-after-attempt-4": ("coding job 9800001065f0 converged (queue done, its verify green) but the "
                           "gate's verdict is 'concerns' (code_high=0, review=PASS WITH CAVEATS)"),
}
ek = getattr(sl, "escalation_kind", lambda r: "ABSENT")
labels = {sid: ek(r) for sid, r in REASONS.items()}
chk("C: s3 after attempt 4 labelled as a gate verdict",
    "gate verdict" in labels["s3-after-attempt-4"], True)
chk("C: s4b labelled as a vacuous-literal plan defect",
    "vacuous must_contain" in labels.get("s4b-integrate-collapse", ""), True)
chk("C: s3 labelled as an authoring budget, not relevance",
    "authoring" in labels.get("s3-endorsement-selection", "") and
    "relevance" not in labels.get("s3-endorsement-selection", ""), True)
src = SLICE.read_text()
chk("C: summary no longer stamps every slice 'relevance NO-GO'",
    "ESCALATED to the Claude gate (relevance NO-GO)" in src, False)
chk("C: a real relevance NO-GO still reads as one",
    "relevance" in (sl.escalation_kind("mechanical relevance gate (preflight) NO-GO -- x")
                    if hasattr(sl, "escalation_kind") else ""), True)

# ---------------- C (watcher context) ----------------------------------------
w = load(WATCHER, "watch_t")
st = {"label": "replay-endorse", "repo": "/base/repo", "target": "lib/x.ts",
      "chain_worktree": "/wt/chain", "slices": {"s4b": {"worktree": None}}}
with contextlib.redirect_stdout(io.StringIO()):
    ctx = w.build_context({"source": "B", "reason": "r", "plan": "replay-endorse",
                           "slice_id": "s4b"}, st)
ctx = ctx if isinstance(ctx, str) else str(ctx)
if not ("lib/x.ts" in ctx):  # build_context may return a path; read it
    try:
        ctx = Path(ctx).read_text()
    except Exception:
        pass
chk("C: watcher names <chain_worktree>/<target> as the code under review",
    "code under review**: `/wt/chain/lib/x.ts`" in ctx, True)
chk("C: watcher marks repo as the base, NOT the code under review",
    "`/base/repo` (base repo" in ctx, True)
st["slices"]["s4b"]["worktree"] = "/wt/s4b"
with contextlib.redirect_stdout(io.StringIO()):
    ctx2 = w.build_context({"source": "B", "reason": "r", "plan": "replay-endorse",
                            "slice_id": "s4b"}, st)
chk("C: a slice with its own worktree points at THAT worktree",
    "code under review**: `/wt/s4b/lib/x.ts`" in str(ctx2), True)
with contextlib.redirect_stdout(io.StringIO()):
    ctx3 = w.build_context({"source": "Q", "reason": "r"}, {"label": "bg", "repo": "/r"})
chk("C: no chain/worktree -> repo line unchanged (no false 'base' note)",
    "- repo: `/r`\n" in str(ctx3), True)

print(f"\n{'ALL PASS' if not fails else str(fails) + ' FAIL'}")
sys.exit(1 if fails else 0)
