#!/usr/bin/env python3
"""Prove the BASELINE-BROKEN guard is REACHABLE, not merely present.

A grep cannot distinguish a live guard from an inert one -- that is exactly how
this shipped: `getattr(args, "verify_failed_at_baseline", False)` where `args`
is the tool-call dict returns the default False unconditionally, so the refuse
branch was unreachable while reading perfectly in review.

So this checks the AST, not the text: the value must arrive as a run_task
PARAMETER and be read as a bare local at both sites, and no site may read it
off `args` at all.
"""
import ast, sys
from pathlib import Path

FLAG = "verify_failed_at_baseline"
src = Path(sys.argv[1]).read_text()
tree = ast.parse(src)
fails = []

run_task = next((n for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "run_task"), None)
if run_task is None:
    print("FAIL: no run_task"); sys.exit(1)

# 1. the value arrives as a parameter
params = {a.arg for a in run_task.args.args + run_task.args.kwonlyargs}
if FLAG not in params:
    fails.append(f"run_task does not take {FLAG} -- the value cannot reach the guard")

# 2. no getattr(args, FLAG, ...) survives anywhere
for n in ast.walk(tree):
    if (isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id == "getattr"
            and len(n.args) >= 2 and isinstance(n.args[1], ast.Constant)
            and n.args[1].value == FLAG):
        fails.append(f"line {n.lineno}: still reads {FLAG} via getattr on a dict -- inert")

# 3. the guard sites read the bare local
reads = [n.lineno for n in ast.walk(run_task)
         if isinstance(n, ast.Name) and n.id == FLAG and isinstance(n.ctx, ast.Load)]
if len(reads) < 2:
    fails.append(f"expected >=2 local reads of {FLAG} in run_task, found {len(reads)} at {reads}")

# 4. main() actually passes it through
main = next((n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef) and n.name == "main"), None)
passed = main is not None and any(
    isinstance(c, ast.Call) and isinstance(c.func, ast.Name) and c.func.id == "run_task"
    and any(k.arg == FLAG for k in c.keywords) for c in ast.walk(main))
if not passed:
    fails.append(f"main() does not pass {FLAG} into run_task")

# 5. demonstrate the bug class this replaces, so the reason is testable too
d = {FLAG: True}
if getattr(d, FLAG, False) is not False:
    fails.append("environment assumption wrong: getattr on a dict should return the default")

for f in fails:
    print("FAIL:", f)
print(f"--- {'GUARD REACHABLE' if not fails else str(len(fails)) + ' problem(s)'} ---")
sys.exit(1 if fails else 0)
