#!/usr/bin/env python3
"""named_files_chars (ollama-queue ctx estimate) must not count harness scaffold files.

AUTO-TASK.md names auto-harness-check.py (~75kB) in every authoring task; counting it
pushed a 330kB-target slice over the ctx-gate ceiling and the enqueue was refused.
Real named target files must still count.
"""
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("oq_scaffold", HERE / "ollama-queue.py")
oq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(oq)

fails = []


def check(name, cond, detail=""):
    if not cond:
        fails.append(f"{name}: {detail}")


with tempfile.TemporaryDirectory() as td:
    d = Path(td)
    (d / "app.py").write_text("x" * 1000)
    (d / "auto-harness-check.py").write_text("y" * 75000)
    (d / "check_literals.py").write_text("z" * 6000)
    task = "Edit app.py. Run auto-harness-check.py and check_literals.py, see ./auto-harness-check.py"
    n = oq.named_files_chars(task, d)
    check("scaffold excluded, real target counted", n == 1000, n)
    check("scaffold alone counts 0", oq.named_files_chars("auto-harness-check.py", d) == 0)
    check("real file alone still counted", oq.named_files_chars("app.py", d) == 1000)

print("FAIL: " + "; ".join(fails) if fails else "ALL PASS")
sys.exit(1 if fails else 0)
