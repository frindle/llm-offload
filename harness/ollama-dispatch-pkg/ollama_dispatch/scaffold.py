"""ollama-dispatch scaffold -- emit a dispatch scaffold, minus the judgment.

Every dispatch needs the same five things: a git worktree, a spec-complete
TASK.md, a verify.sh that discriminates the change, an optional reference
implementation, and a place to author adversarial cases. Hand-typing them is
how a required section goes missing -- and the resulting failure then gets
misread as model incapacity. This generates them.

THE ONE THING IT DELIBERATELY DOES NOT DO
------------------------------------------
It does not author the adversarial assertions, and it does NOT emit a scaffold
that passes the gate. The emitted case list is EMPTY and verify.sh exits 1 with
SCAFFOLD_INCOMPLETE until a human fills it in. Picking inputs that separate "did
the job" from "made the test go green" is the one judgment this whole toolchain
exists to protect; a generator that emitted a gate-passing scaffold would let an
operator skip exactly that step.

Pipeline:  scaffold -> author the adversarial cases -> gate (preflight) -> enqueue
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

LANGS = ("python", "typescript", "javascript", "swift", "go", "rust")

TASK_TEMPLATE = """\
# TASK: {label}

## Defect (confirmed symptom, and how it was observed)
{defect}

## Property (what the change must satisfy)
{property}

## Target
- File: {target}
- Change site: {entry_point}
- Kind: {kind}{symbol_line}

## Constraints
- Make the smallest change that satisfies the Property.
- Do not modify tests, fixtures, or verify.sh.
- The change must make verify.sh pass (`bash verify.sh` exits 0).

## Must contain
{required}

## How to verify
Run `bash verify.sh` from the worktree root. It must exit 0 only when the
Property holds for every adversarial case.
"""

VERIFY_TEMPLATE = """\
#!/usr/bin/env bash
# verify.sh -- discriminating check for: {label}
#
# THIS SCAFFOLD IS INCOMPLETE ON PURPOSE.
# The adversarial case list below is EMPTY. Until a human authors cases that
# separate "did the job" from "made the test go green", this exits non-zero and
# the preflight gate will NO-GO by name. That NO-GO is the tool working.
set -euo pipefail

# --- adversarial cases (AUTHOR THESE) -------------------------------------
# Each case must FAIL against the unmodified baseline and PASS once the
# Property holds. Prefer boundary inputs over convenient ones.
CASES_AUTHORED=0   # set to 1 once you have added real cases below

if [ "$CASES_AUTHORED" -ne 1 ]; then
  echo "SCAFFOLD_INCOMPLETE: author adversarial cases in verify.sh, then set CASES_AUTHORED=1" >&2
  exit 1
fi

# Example shape (delete and replace with real cases):
#   {run_hint}

echo "verify: all cases passed"
"""


def _run(cmd: list, cwd: str | None = None) -> str:
    return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, check=True).stdout


def _make_worktree(repo: Path, dest: Path, label: str, baseline_ref: str) -> None:
    """Cut a git worktree off `repo` at `baseline_ref` on a new branch."""
    branch = f"dispatch/{label}"
    subprocess.run(
        ["git", "-C", str(repo), "worktree", "add", "-b", branch, str(dest), baseline_ref],
        check=True,
    )


def _new_project(dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "-C", str(dest), "init", "-q"], check=True)


def run_hint_for(lang: str, target: str) -> str:
    if lang == "python":
        return f'python -c "import sys; ..." || exit 1   # exercise {target}'
    if lang in ("typescript", "javascript"):
        return f"node --test  ||  npx tsx verify-impl.ts   # exercise {target}"
    if lang == "swift":
        return "swift test"
    if lang == "go":
        return "go test ./..."
    if lang == "rust":
        return "cargo test"
    return f"# run the check that exercises {target}"


def scaffold(*, label: str, repo: Path | None, new_project: Path | None, dest: Path,
             lang: str, target: str, kind: str, symbol: str | None,
             entry_point: str | None, defect: str, property_: str,
             required: list[str], baseline_ref: str) -> Path:
    if repo:
        _make_worktree(repo, dest, label, baseline_ref)
    elif new_project:
        dest = new_project
        _new_project(dest)
    else:
        raise ValueError("one of repo or new_project is required")

    symbol_line = f"\n- Symbol: {symbol}" if symbol else ""
    required_block = (
        "\n".join(f"- `{r}`" for r in required)
        if required
        else "- (none specified)"
    )
    task_md = TASK_TEMPLATE.format(
        label=label, defect=defect or "(TODO: state the confirmed symptom)",
        property=property_ or "(TODO: state what the change must satisfy)",
        target=target or "(TODO)", entry_point=entry_point or "(TODO: file:line)",
        kind=kind, symbol_line=symbol_line, required=required_block,
    )
    verify_sh = VERIFY_TEMPLATE.format(
        label=label, run_hint=run_hint_for(lang, target or "the target"),
    )

    (dest / "TASK.md").write_text(task_md)
    vpath = dest / "verify.sh"
    vpath.write_text(verify_sh)
    vpath.chmod(0o755)

    print(f"scaffolded {dest}", file=sys.stderr)
    print(f"  TASK.md   -- fill any (TODO) fields", file=sys.stderr)
    print(f"  verify.sh -- author adversarial cases, then set CASES_AUTHORED=1", file=sys.stderr)
    print(f"Next: author cases -> `ollama-dispatch-preflight {dest}` -> enqueue", file=sys.stderr)
    return dest


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Scaffold a verify-gated dispatch.")
    ap.add_argument("--label", required=True)
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--repo", help="Existing repo -- a git worktree is cut from it.")
    src.add_argument("--new-project", dest="new_project", help="Fresh project dir (git init'd).")
    ap.add_argument("--dest", help="Where to place the worktree (default: sibling of repo).")
    ap.add_argument("--lang", choices=LANGS, default="python")
    ap.add_argument("--target", help="File the change lives in.")
    ap.add_argument("--kind", choices=("symbol", "expression"), default="symbol")
    ap.add_argument("--symbol", help="Function/symbol under change (kind=symbol).")
    ap.add_argument("--entry-point", dest="entry_point", help="file:line of the change site.")
    ap.add_argument("--defect", help="The CONFIRMED symptom + how you observed it.")
    ap.add_argument("--property", dest="property_", help="What the change must satisfy.")
    ap.add_argument("--require", action="append", default=[], metavar="LITERAL",
                    help="A literal the correct fix must introduce (repeatable).")
    ap.add_argument("--baseline-ref", default="HEAD", help="Tree-ish to branch the worktree from.")
    args = ap.parse_args(argv)

    repo = Path(args.repo).expanduser().resolve() if args.repo else None
    new_project = Path(args.new_project).expanduser().resolve() if args.new_project else None
    if args.dest:
        dest = Path(args.dest).expanduser().resolve()
    elif repo:
        dest = repo.parent / f"{repo.name}-{args.label}"
    else:
        dest = new_project

    scaffold(
        label=args.label, repo=repo, new_project=new_project, dest=dest,
        lang=args.lang, target=args.target or "", kind=args.kind, symbol=args.symbol,
        entry_point=args.entry_point, defect=args.defect or "", property_=args.property_ or "",
        required=args.require, baseline_ref=args.baseline_ref,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
