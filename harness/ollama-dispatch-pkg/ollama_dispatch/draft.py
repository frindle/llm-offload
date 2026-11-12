"""ollama-dispatch draft -- have a local model DRAFT the adversarial cases.

  scaffold   emits everything except the judgment
  draft      drafts the judgment part, and PROVES it mechanically (this file)
  preflight  gates it
  <you>      decide whether it is RELEVANT                <- irreducible

WHAT THE GATE CAN AND CANNOT DO
-------------------------------
The gate mechanically catches a verify that does not discriminate, a task that
is unsatisfiable, and a verify that goes green while a required literal is
absent. It does NOT catch a verify that discriminates, asserts every literal,
and tests the WRONG property. A drafted case like `is_safe({"count": 99}, True)`
can fail at baseline, satisfy the refimpl, and pass the ENTIRE gate while testing
nothing about the boundary the task is actually about. Irrelevance is what a
weak drafter produces most often.

So this tool does not remove the human. It changes the job from AUTHORING cases
to REVIEWING drafted ones -- cheaper, same judgment -- and it ENFORCES that the
review happened: drafts carry a DRAFT_UNCONFIRMED marker, the gate FAILs on it,
and only `--confirm` removes it. A pipeline that is green because nobody looked
is the failure this whole toolchain exists to prevent.

THE LOOP IS SEPARABLE FROM THE MODEL, DELIBERATELY
--------------------------------------------------
`--drafter-cmd` injects any command in place of the model, so the retry logic is
provable without a GPU (the same reason preflight takes `--refimpl-cmd`).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

from . import config

DRAFT_MARKER = "DRAFT_UNCONFIRMED"

DRAFT_PROMPT = """\
You are authoring adversarial test cases for a code-fix task. Read TASK.md in
the working directory. Then edit verify.sh: replace the empty case list with
cases that (a) FAIL against the current (unmodified) code and (b) will PASS once
the Property in TASK.md holds. Set CASES_AUTHORED=1. Prefer boundary inputs.
Leave the line `# {marker}` at the top of verify.sh -- a human must confirm your
cases before they are trusted. When done, call task_complete.
""".format(marker=DRAFT_MARKER)


def _ensure_marker(worktree: Path) -> None:
    vpath = worktree / "verify.sh"
    text = vpath.read_text()
    if DRAFT_MARKER not in text:
        lines = text.splitlines()
        insert_at = 1 if lines and lines[0].startswith("#!") else 0
        lines.insert(insert_at, f"# {DRAFT_MARKER}")
        vpath.write_text("\n".join(lines) + "\n")


def draft(worktree: Path, *, model: str, host: str, rounds: int, max_iters: int,
          timeout: int, drafter_cmd: str | None) -> bool:
    """Run the drafter (model or injected command) until verify.sh looks authored.

    Returns True if a draft was produced (still UNCONFIRMED). The human then
    reviews and runs with --confirm.
    """
    for attempt in range(1, rounds + 1):
        print(f"[draft] round {attempt}/{rounds}", file=sys.stderr)
        if drafter_cmd:
            proc = subprocess.run(drafter_cmd, shell=True, cwd=str(worktree),
                                  capture_output=True, text=True, timeout=timeout)
            print(proc.stdout[-1000:], file=sys.stderr)
        else:
            cmd = [
                sys.executable, "-m", "ollama_dispatch.worker",
                "--task", DRAFT_PROMPT, "--cwd", str(worktree),
                "--model", model, "--host", host,
                "--max-iters", str(max_iters), "--chat-timeout", str(timeout),
            ]
            subprocess.run(cmd)

        verify_text = (worktree / "verify.sh").read_text()
        if "CASES_AUTHORED=1" in verify_text:
            _ensure_marker(worktree)
            print(f"[draft] cases drafted -- {DRAFT_MARKER}. REVIEW verify.sh, then "
                  f"re-run with --confirm.", file=sys.stderr)
            return True
    print("[draft] drafter did not produce authored cases within the round budget.",
          file=sys.stderr)
    return False


def confirm(worktree: Path) -> bool:
    """Remove the DRAFT_UNCONFIRMED marker after a human review."""
    vpath = worktree / "verify.sh"
    text = vpath.read_text()
    if DRAFT_MARKER not in text:
        print(f"[draft] no {DRAFT_MARKER} marker -- nothing to confirm.", file=sys.stderr)
        return False
    lines = [l for l in text.splitlines() if l.strip() != f"# {DRAFT_MARKER}"]
    vpath.write_text("\n".join(lines) + "\n")
    print("[draft] confirmed -- marker removed. Run preflight next.", file=sys.stderr)
    return True


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Draft adversarial cases with a local model.")
    ap.add_argument("worktree")
    ap.add_argument("--confirm", action="store_true",
                    help="Remove DRAFT_UNCONFIRMED after you reviewed the cases.")
    ap.add_argument("--rounds", type=int, default=3)
    ap.add_argument("--model", default=config.DEFAULT_MODEL)
    ap.add_argument("--host", default="auto")
    ap.add_argument("--max-iters", type=int, default=8)
    ap.add_argument("--timeout", type=int, default=1800)
    ap.add_argument("--drafter-cmd", help="Command to inject in place of the model (for testing).")
    args = ap.parse_args(argv)

    wt = Path(args.worktree).expanduser().resolve()
    if args.confirm:
        return 0 if confirm(wt) else 1
    ok = draft(wt, model=args.model, host=args.host, rounds=args.rounds,
               max_iters=args.max_iters, timeout=args.timeout, drafter_cmd=args.drafter_cmd)
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
