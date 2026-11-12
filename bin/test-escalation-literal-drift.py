#!/usr/bin/env python3
"""Behavioural + revert test: escalation reviews get machine-checked spec literals,
and a review that misspells the spec's identifiers is flagged as drift.

Defect (2026-10-06, rt-egift-link-s1 s0-db-schema, review job 7a86d3e31041): the
local reviewer's grep returned `OrderEgiftLink`, yet its answer wrote `OrderEgmtLink`
~20 times and called it "the correct spelling". The context file gave it no ground
truth, and nothing audited the answer, so the coordinator could not tell whether the
misspelling lived in the code or in the review.

  python3 ~/bin/test-escalation-literal-drift.py
  WATCHER_FILE=<backup> python3 ~/bin/test-escalation-literal-drift.py   # must FAIL
No queue, worker or network is touched: fixtures are temp dirs.
"""
import importlib.util
import json
import os
import re
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

MOD = os.environ.get("WATCHER_FILE", str(Path.home() / "bin" / "dispatch-escalation-watcher.py"))
_l = SourceFileLoader("escwatch", MOD)
W = importlib.util.module_from_spec(importlib.util.spec_from_loader("escwatch", _l))
_l.exec_module(W)

FAILED, RAN = [], []


def check(name, cond, extra=""):
    RAN.append(name)
    print(("  PASS " if cond else "  FAIL ") + name + (f"  [{extra}]" if extra else ""))
    if not cond:
        FAILED.append(name)


TASK = """TASK: x

## Must contain

- in prisma/schema.prisma: `model OrderEgiftLink {`
- in prisma/schema.prisma: `OrderEgiftLink?`
- `upsertOrderEgiftLink`

## Scope
"""

# Verbatim shape of the 7a86d3e31041 answer: drifted prose over correct files.
DRIFT_REVIEW = """VERDICT: (a) -- already satisfied.
1. `prisma/schema.prisma` line 236 contains `egiftLink OrderEgmtLink?` (correct spelling)
2. lib/egiftLinkStore.ts exports `upsertOrderEgmtLink` correctly
"""
CLEAN_REVIEW = """VERDICT: (c) -- harness defect.
schema has `model OrderEgiftLink {`; the client accessor `prisma.orderEgiftLink` and
`upsertOrderEgiftLink` are present.
"""


def mk_wt(root, name, ident):
    wt = Path(root) / name
    (wt / "prisma").mkdir(parents=True)
    (wt / "lib").mkdir()
    (wt / "TASK.md").write_text(TASK)
    (wt / "check_literals.py").write_text("TARGET = pathlib.Path('lib/egiftLinkStore.ts')\n")
    (wt / "prisma" / "schema.prisma").write_text(
        "model Order {\n  egiftLink %s?\n}\nmodel %s {\n  id Int\n}\n" % (ident, ident))
    (wt / "lib" / "egiftLinkStore.ts").write_text(
        "export async function upsert%s() { return prisma.%s.upsert(); }\n"
        % (ident, ident[:1].lower() + ident[1:]))
    return wt


with tempfile.TemporaryDirectory() as td:
    good = mk_wt(td, "good", "OrderEgiftLink")
    bad = mk_wt(td, "bad", "OrderEgmtLink")
    logs = Path(td) / "logs"
    logs.mkdir()
    (logs / "01fdda651fba.done.json").write_text(json.dumps({"cwd": str(good)}))

    # 1. ground truth for a correct worktree
    fg = W.spec_literal_facts(good)
    counts = {(f, l): n for f, l, n in (fg or {}).get("literals", [])}
    check("facts: exact counts measured per pinned file",
          counts.get(("prisma/schema.prisma", "model OrderEgiftLink {")) == 1
          and counts.get(("prisma/schema.prisma", "OrderEgiftLink?")) == 1
          and counts.get(("lib/egiftLinkStore.ts", "upsertOrderEgiftLink")) == 1, counts)
    check("facts: correct worktree has NO near-miss", fg and fg["near"] == [], fg and fg["near"])

    # 2. a real misspelling in the files is reported as a near-miss (a real defect)
    fb = W.spec_literal_facts(bad)
    nb = {(h, w) for h, w, _r in (fb or {}).get("near", [])}
    check("facts: misspelled worktree reports OrderEgmtLink~OrderEgiftLink",
          ("OrderEgmtLink", "OrderEgiftLink") in nb, nb)
    check("facts: missing literal counted 0 in misspelled worktree",
          any(n == 0 for _f, _l, n in fb["literals"]))

    # 3. source-D job escalation (no slice record) resolves the job cwd -> context block
    W.QUEUE_LOGS = logs
    e = {"source": "D", "kind": "job", "plan": None, "slice_id": None,
         "job_id": "01fdda651fba", "label": "x", "signature": "needs_opus", "reason": "r"}
    check("worktree: source-D job resolves its cwd", W.escalation_worktree(e) == str(good))
    W._log_tail = lambda *a, **k: ""
    ctx = W.build_context(e)
    check("context: ground-truth block present for a job escalation",
          "machine-checked spec literals" in ctx and "O-r-d-e-r-E-g-i-f-t-L-i-n-k" in ctx
          and "NONE" in ctx, ctx[-600:])

    # 4. review drift: drifted prose over correct files is flagged; clean is not
    d = W.review_literal_drift(DRIFT_REVIEW, fg)
    check("drift: 7a86d3e31041-shaped review flagged",
          ("OrderEgmtLink", "OrderEgiftLink") in d and ("upsertOrderEgmtLink", "upsertOrderEgiftLink") in d, d)
    check("drift: clean review (incl. camel accessor) not flagged",
          W.review_literal_drift(CLEAN_REVIEW, fg) == [])
    check("drift: a misspelling that IS in the files is a finding, not drift",
          W.review_literal_drift(DRIFT_REVIEW, fb) == [])
    note = W.drift_note(d, fg)
    check("drift note names the drifted token and the machine counts",
          "OrderEgmtLink" in note and "= 1 exact" in note)

    # 5. a real answer file, if still present, is flagged against the correct tree
    ans = Path.home() / "bin" / "ollama-queue-logs" / "7a86d3e31041.answer.md"
    if ans.exists():
        check("drift: the live 7a86d3e31041 answer is flagged",
              bool(W.review_literal_drift(ans.read_text(), fg)))

# 6. prompt + wiring
check("prompt: spelling ground-truth rule", "GROUND TRUTH" in W.REVIEW_PROMPT
      and "never retyped from memory" in W.REVIEW_PROMPT)
check("prompt: converged-unlanded slice -> close by hand (old slicer retired), not skip",
      "retired" in W.REVIEW_PROMPT)
W.REVIEW_PROMPT.format(ctx="/x")  # must stay format-safe
src = Path(MOD).read_text()
ro = src[src.index("def run_once"):src.index("def review_bundle")]
check("run_once audits the review and holds a drifted diff from self-heal",
      "review_literal_drift(review" in ro and "_drift_in_patch" in ro
      and re.search(r"heal = \(None if _drift_in_patch", ro) is not None)

print("%d/%d passed" % (len(RAN) - len(FAILED), len(RAN)))
sys.exit(1 if FAILED else 0)
