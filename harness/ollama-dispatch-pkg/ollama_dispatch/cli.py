"""ollama-dispatch -- one-command orchestrator for the verify-gated pipeline.

Collapses the chain (scaffold -> draft -> REVIEW -> preflight -> enqueue) into a
single entrypoint, pausing ONLY at the one irreducible human gate: reviewing the
drafted adversarial cases for RELEVANCE.

It INVOKES the pipeline modules; it does not re-implement them:
  ollama_dispatch.scaffold    worktree + TASK.md + verify.sh
  ollama_dispatch.draft       draft the cases with a local model
  ollama_dispatch.preflight   the both-ways GO/NO-GO gate
  ollama_dispatch.queue       put the job on the queue

Two load-bearing gates are made CONVENIENT, never SKIPPABLE:
  * the DRAFT_UNCONFIRMED marker is surfaced, never auto-confirmed;
  * a preflight NO-GO stops the chain and never enqueues.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import config, draft as draft_mod, preflight as preflight_mod, queue as queue_mod, scaffold as scaffold_mod


def _pause(dest: Path) -> None:
    print("\n" + "=" * 70, file=sys.stderr)
    print("PAUSE -- human review required.", file=sys.stderr)
    print(f"  1. Read the drafted cases:   {dest / 'verify.sh'}", file=sys.stderr)
    print("  2. Decide if they test the RIGHT property (relevance is irreducible).", file=sys.stderr)
    print(f"  3. Resume:  ollama-dispatch --confirm {dest}", file=sys.stderr)
    print("=" * 70, file=sys.stderr)


def setup(args) -> int:
    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    new_project = Path(args.new_project).expanduser().resolve() if args.new_project else None
    dest = (Path(args.dest).expanduser().resolve() if args.dest
            else (repo.parent / f"{repo.name}-{args.label}" if repo else new_project))

    scaffold_mod.scaffold(
        label=args.label, repo=repo, new_project=new_project, dest=dest,
        lang=args.lang, target=args.target or "", kind=args.kind or "symbol",
        symbol=args.symbol, entry_point=args.entry_point,
        defect=args.defect or "", property_=args.property_ or "",
        required=args.require, baseline_ref="HEAD",
    )

    if args.task == "diagnosis":
        # No cases/refimpl -> no human pause; gate with --no-refimpl and enqueue.
        return _gate_and_enqueue(args, dest, no_refimpl=True)

    # Code-fix: draft the cases, then STOP for review.
    draft_mod.draft(
        dest, model=args.model or config.DEFAULT_MODEL, host=args.host or "auto",
        rounds=3, max_iters=8, timeout=1800, drafter_cmd=args.drafter_cmd,
    )
    _pause(dest)
    return 0


def resume(args) -> int:
    dest = Path(args.confirm).expanduser().resolve()
    if not draft_mod.confirm(dest):
        return 1
    return _gate_and_enqueue(args, dest, no_refimpl=False)


def _gate_and_enqueue(args, dest: Path, *, no_refimpl: bool) -> int:
    r = preflight_mod.preflight(
        dest, verify_cmd="bash verify.sh", required=args.require,
        refimpl_cmd=args.refimpl_cmd, no_refimpl=no_refimpl,
    )
    for name, ok, detail in r.checks:
        print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if not ok else ""),
              file=sys.stderr)
    if not r.go:
        print("\nNO-GO -- not enqueuing. Fix the failing check above.", file=sys.stderr)
        return 2

    task_text = (dest / "TASK.md").read_text()
    job = queue_mod.enqueue(
        args.label, str(dest), task_text,
        model=args.model, host=args.host or "auto",
        verify="bash verify.sh", task_kind=args.task or "coding",
    )
    print(f"\nGO -- enqueued {job['id']} ({args.label}).", file=sys.stderr)
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="One-command verify-gated dispatch.")
    ap.add_argument("--confirm", metavar="WORKTREE",
                    help="Resume after reviewing drafted cases in WORKTREE.")
    ap.add_argument("--repo")
    ap.add_argument("--new-project", dest="new_project")
    ap.add_argument("--dest")
    ap.add_argument("--label")
    ap.add_argument("--lang", choices=scaffold_mod.LANGS, default="python")
    ap.add_argument("--target")
    ap.add_argument("--kind", choices=("symbol", "expression"))
    ap.add_argument("--symbol")
    ap.add_argument("--entry-point", dest="entry_point")
    ap.add_argument("--defect")
    ap.add_argument("--property", dest="property_")
    ap.add_argument("--task", choices=("coding", "diagnosis"), default="coding")
    ap.add_argument("--require", action="append", default=[])
    ap.add_argument("--refimpl-cmd", dest="refimpl_cmd")
    ap.add_argument("--drafter-cmd", dest="drafter_cmd")
    ap.add_argument("--model")
    ap.add_argument("--host")
    args = ap.parse_args(argv)

    if args.confirm:
        return resume(args)
    if not args.label or not (args.repo or args.new_project):
        ap.error("--label and one of --repo/--new-project are required for setup")
    return setup(args)


if __name__ == "__main__":
    raise SystemExit(main())
