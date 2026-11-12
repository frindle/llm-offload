"""ollama-dispatch preflight -- one GO / NO-GO gate for a dispatch.

The steps that used to be a hand-run checklist become a program with exactly
one output: a verdict and the specific failing reason. A hand-run checklist has
one failure mode -- a step gets skipped, and the resulting failure is then
misread for rounds as model incapacity. Nearly every failure attributed to "the
model" turns out to be spec/harness/human.

Given a worktree with TASK.md + verify.sh (+ an optional reference impl), this
runs the whole proof and prints ONE verdict.

THE CHECKS (and the failure each one is scar tissue from)
---------------------------------------------------------
  worktree           scaffold present and is a real git worktree
  baseline-clean     no tracked modifications -- a both-ways proof against a
                     dirty tree proves nothing, and a reference impl left
                     applied hands the model the answer
  task-shape         TASK.md carries the sections a spec-complete task needs
  scaffold-complete  verify.sh is not still the SCAFFOLD_INCOMPLETE stub
  required-literals  every --require literal is actually present in TASK.md
  baseline-fails     verify.sh FAILS against the unmodified tree (proves it
                     discriminates -- a verify that passes at baseline certifies
                     nothing)
  refimpl-passes     with the reference impl applied, verify.sh PASSES (proves
                     the task is satisfiable and the verify is reachable)

A verify that both fails at baseline and passes under the refimpl is "proven
both ways". Only then is GO emitted. This tool owns MECHANICS, not relevance:
whether the authored cases test the RIGHT property is the human's irreducible
job (see draft.py).
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

REQUIRED_SECTIONS = ("# TASK", "## Defect", "## Property", "## How to verify")


class Result:
    def __init__(self):
        self.checks: list[tuple[str, bool, str]] = []

    def add(self, name: str, ok: bool, detail: str = "") -> bool:
        self.checks.append((name, ok, detail))
        return ok

    @property
    def go(self) -> bool:
        return all(ok for _, ok, _ in self.checks)


def _git(worktree: Path, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(worktree), *args],
                          capture_output=True, text=True)


def _is_worktree(worktree: Path) -> bool:
    return (worktree / ".git").exists() and _git(worktree, "rev-parse").returncode == 0


def _baseline_clean(worktree: Path) -> bool:
    out = _git(worktree, "status", "--porcelain").stdout
    # untracked scaffold files (TASK.md, verify.sh) are expected; tracked
    # modifications are not.
    for line in out.splitlines():
        if line and not line.startswith("??"):
            return False
    return True


def _run_verify(worktree: Path, verify_cmd: str) -> tuple[bool, str]:
    proc = subprocess.run(verify_cmd, shell=True, cwd=str(worktree),
                          capture_output=True, text=True)
    return proc.returncode == 0, (proc.stdout + proc.stderr)[-1500:]


def preflight(worktree: Path, *, verify_cmd: str, required: list[str],
              refimpl_cmd: str | None, no_refimpl: bool) -> Result:
    r = Result()
    task_file = worktree / "TASK.md"

    if not r.add("worktree", _is_worktree(worktree), "not a git worktree"):
        return r
    r.add("baseline-clean", _baseline_clean(worktree),
          "tree has tracked modifications")

    task_text = task_file.read_text() if task_file.exists() else ""
    r.add("task-shape", all(s in task_text for s in REQUIRED_SECTIONS),
          f"TASK.md missing one of {REQUIRED_SECTIONS}")

    verify_text = (worktree / "verify.sh").read_text() if (worktree / "verify.sh").exists() else ""
    r.add("scaffold-complete", "SCAFFOLD_INCOMPLETE" not in verify_text or "CASES_AUTHORED=1" in verify_text,
          "verify.sh is still the incomplete scaffold stub")

    missing = [lit for lit in required if lit not in task_text]
    r.add("required-literals", not missing, f"absent from TASK.md: {missing}")

    baseline_fail, base_out = _run_verify(worktree, verify_cmd)
    r.add("baseline-fails", not baseline_fail,
          "verify.sh PASSED at baseline -- it does not discriminate the change")

    if no_refimpl:
        r.add("refimpl-passes", True, "skipped (--no-refimpl)")
        return r

    if not refimpl_cmd:
        r.add("refimpl-passes", False, "no --refimpl-cmd given and --no-refimpl not set")
        return r

    # Apply reference impl, prove verify passes, then restore the clean tree.
    apply = subprocess.run(refimpl_cmd, shell=True, cwd=str(worktree),
                           capture_output=True, text=True)
    if apply.returncode != 0:
        r.add("refimpl-passes", False, f"refimpl-cmd failed: {apply.stderr[-400:]}")
        return r
    passed, ref_out = _run_verify(worktree, verify_cmd)
    r.add("refimpl-passes", passed,
          "verify.sh did not pass under the reference impl -- task unsatisfiable or verify unreachable")
    _git(worktree, "checkout", "--", ".")
    _git(worktree, "clean", "-fdq")
    return r


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="GO/NO-GO gate for a dispatch scaffold.")
    ap.add_argument("worktree", help="Worktree containing TASK.md + verify.sh.")
    ap.add_argument("--verify", default="bash verify.sh")
    ap.add_argument("--require", action="append", default=[], metavar="LITERAL")
    ap.add_argument("--require-file", help="File of --require literals, one per line.")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--refimpl-cmd", help="Shell command that writes the reference impl into the tree.")
    g.add_argument("--no-refimpl", action="store_true",
                   help="Diagnosis-style task with no reference impl (skips refimpl-passes).")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    required = list(args.require)
    if args.require_file:
        required += [l.strip() for l in Path(args.require_file).read_text().splitlines() if l.strip()]

    r = preflight(
        Path(args.worktree).expanduser().resolve(),
        verify_cmd=args.verify, required=required,
        refimpl_cmd=args.refimpl_cmd, no_refimpl=args.no_refimpl,
    )

    if args.json:
        import json as _json
        print(_json.dumps({"go": r.go, "checks": [
            {"name": n, "ok": ok, "detail": d} for n, ok, d in r.checks]}, indent=2))
    else:
        for name, ok, detail in r.checks:
            mark = "PASS" if ok else "FAIL"
            print(f"  [{mark}] {name}" + (f"  -- {detail}" if not ok else ""))
        print("\n==> " + ("GO" if r.go else "NO-GO"))
    return 0 if r.go else 2


if __name__ == "__main__":
    raise SystemExit(main())
