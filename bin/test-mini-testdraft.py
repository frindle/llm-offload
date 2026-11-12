#!/usr/bin/env python3
"""mini-testdraft: kill switch, must-not-offload refusal, prompt content; and (if the bloom-ledger run dir exists on this box) the real
trial: the Claude-written test is ACCEPTed and a weak 'returns a dict' draft is REJECTed by the mutation score although V1-V7 pass it."""
import importlib.machinery, importlib.util, io, json, os, subprocess, sys, tempfile
from contextlib import redirect_stdout
from pathlib import Path
here = Path(__file__).resolve().parent
ld = importlib.machinery.SourceFileLoader("td", str(here / "mini-testdraft")); sp = importlib.util.spec_from_loader("td", ld); T = importlib.util.module_from_spec(sp); ld.exec_module(T)
tmp = Path(tempfile.mkdtemp())
os.environ["MINI_TESTDRAFT"] = "off"
buf = io.StringIO()
with redirect_stdout(buf):
    assert T.main(["prompt", "x"]) == 2
assert "disabled" in buf.getvalue()
del os.environ["MINI_TESTDRAFT"]
assert not T.guard({"baseline_from": "/x/wt-resell-money", "allowed": ["a.py"], "test_file": "t.py"}, False)
assert T.guard({"baseline_from": "/x/wt-resell-money", "allowed": ["a.py"], "test_file": "t.py"}, True)
assert T.guard({"baseline_from": "/x/wt-bloom-ledger", "allowed": ["ingest/x.py"], "test_file": "ingest/t.py"}, False)
base = tmp / "base"; base.mkdir(); (base / "calc.py").write_text("def add(a, b):\n    raise NotImplementedError\n")
(tmp / "ref").mkdir(); (tmp / "ref/calc.py").write_text("def add(a, b):\n    return a + b\n"); (tmp / "TASK.md").write_text("# TASK\nadd must add\n")
(tmp / "spec.json").write_text(json.dumps({"baseline_from": str(base), "allowed": ["calc.py"], "test_file": "test_calc.py", "test_cmd": "python3 -m pytest -q test_calc.py", "task": "TASK.md"}))
buf = io.StringIO()
with redirect_stdout(buf):
    assert T.main(["prompt", str(tmp / "spec.json")]) == 0
out = buf.getvalue()
assert "+    return a + b" in out and "add must add" in out and "test_calc.py" in out and "EVERY hunk" in out, out
R = Path.home() / ".ollama-dispatch/mini-runs/bloom-ledger-summary-build-core"
if (R / "spec.json").is_file() and (Path.home() / ".local/bin/mutmut").exists():
    wt = Path(json.loads((R / "spec.json").read_text())["baseline_from"])
    good = wt / "ingest/test_accounting_summary_core.py"
    if good.is_file():
        weak = tmp / "weak.py"
        weak.write_text(good.read_text().split("def test_month_summary_with_rates")[0] + "def test_a():\n    assert isinstance(SM.build(make(), '2026-10-31', 'month'), dict)\n\n\ndef test_b():\n    assert SM.build(make(), '2026-10-31', 'ytd') is not None\n")
        for draft, want in ((good, 0), (weak, 1)):
            p = subprocess.run([sys.executable, str(here / "mini-testdraft"), "check", str(R / "spec.json"), "--draft", str(draft), "--mutmut-pythonpath", "lib"], capture_output=True, text=True)
            assert p.returncode == want, (draft, p.stdout[-600:])
        print("  real trial: good ACCEPT, weak REJECT")
print("test-mini-testdraft ok")
