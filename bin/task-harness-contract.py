#!/usr/bin/env python3
"""Does the TASK specify every interface the TEST HARNESS depends on?

Three dispatch failures in one day, all the same shape: the harness invoked the
script under test through an interface the task never named, so the model built
something reasonable, every check failed identically, and it read as the model
being incapable.

  1. harness called `--job-id`;         task never said the job id was a flag
     -> model made it positional, every run died at argv
  2. harness set OLLAMA_QUEUE_LOGS;     task never named it
     -> model hardcoded the documented default, every run died at "no gate record"
  3. harness ran verify.sh each round;  task never told the model to
     -> it ran the verify once in 17 tool calls and steered off nothing

The task and the harness are ONE CONTRACT. Drift between them is a spec bug that
presents as a model failure, and nothing was checking it. This checks it.

Usage: task-harness-contract.py --task TASK.md --harness fixture.py [--harness verify.sh]
Exit 0 = every interface the harness uses is named in the task.
"""
import argparse, re, sys
from pathlib import Path

# Flags the RUNNER owns, not the script under test -- naming these in the task
# would be noise.
IGNORE_FLAGS = {"--job-id-does-not-exist"}
IGNORE_ENV = {"PATH", "HOME", "PWD", "TMPDIR"}


# Flags belonging to tools the harness drives for SETUP, not to the script under
# test. Without this the checker reports git's --bare/--list and a planted
# --force as unspecified, which is noise that would train people to ignore it.
EXTERNAL = {"git", "gh", "bash", "sh", "chmod", "mkdir"}


def interfaces(text: str):
    env = {m.group(1) for m in re.finditer(r"""env\[["']([A-Z][A-Z0-9_]+)["']\]""", text)}
    env |= {m.group(1) for m in re.finditer(r"""environ\[["']([A-Z][A-Z0-9_]+)["']\]""", text)}
    env |= {m.group(1) for m in re.finditer(r"""\b([A-Z][A-Z0-9_]{3,})=\S""", text)}

    # Flags are taken ONLY from argv-shaped list literals that are not driving an
    # external tool. A regex over every quoted --flag cannot tell "an argument to
    # the script under test" from "an argument to git", and the difference is the
    # whole point of the check.
    flags = set()
    try:
        import ast as _ast
        tree = _ast.parse(text)
        for n in _ast.walk(tree):
            if not isinstance(n, (_ast.List, _ast.Tuple)):
                continue
            items = [e.value for e in n.elts
                     if isinstance(e, _ast.Constant) and isinstance(e.value, str)]
            if not items or items[0] in EXTERNAL:
                continue
            flags |= {i for i in items if i.startswith("--")}
    except SyntaxError:
        # A shell harness (verify.sh with embedded python). Fall back to a text
        # scan, but only inside SQUARE-BRACKET argv lists -- a flag named in a
        # tuple is almost always an assertion ABOUT forbidden flags, not a flag
        # being passed. Without this the checker flags the ("--force", "-f")
        # inside the never-merges assertion as an unspecified interface.
        for line in text.splitlines():
            if any(re.search(rf"\b{t}\b", line) for t in EXTERNAL):
                continue
            for lst in re.findall(r"\[([^\]]*)\]", line):
                flags |= set(re.findall(r"""["'](--[a-z][a-z0-9-]+)["']""", lst))
    return env - IGNORE_ENV, flags - IGNORE_FLAGS


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--harness", action="append", required=True)
    a = ap.parse_args()
    task = Path(a.task).read_text()
    env, flags = set(), set()
    for h in a.harness:
        try:
            e, f = interfaces(Path(h).read_text())
        except Exception as ex:
            print(f"ERROR: cannot read harness {h}: {ex}")
            return 2
        env |= e; flags |= f

    missing_env = sorted(v for v in env if v not in task)
    missing_flags = sorted(v for v in flags if v not in task)
    for v in missing_env:
        print(f"UNSPECIFIED ENV: the harness sets {v} but the task never names it")
    for v in missing_flags:
        print(f"UNSPECIFIED FLAG: the harness passes {v} but the task never names it")
    if not (missing_env or missing_flags):
        print(f"contract OK: {len(env)} env var(s) and {len(flags)} flag(s), all specified")
        return 0
    print("\nThe model will build against the TASK, not the harness. Anything the "
          "harness needs and the task omits fails every check identically, which "
          "reads as incapacity rather than as the spec gap it is.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
