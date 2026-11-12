#!/usr/bin/env python3
"""mini-spec log_intake_refusal: off by default, logs non-local-ready intake only when MINI_LOG_REFUSALS=1, never raises."""
import importlib.machinery, importlib.util, json, os, tempfile
from pathlib import Path
here = Path(__file__).resolve().parent
ld = importlib.machinery.SourceFileLoader("ms", str(here / "mini-spec")); sp = importlib.util.spec_from_loader("ms", ld); M = importlib.util.module_from_spec(sp); ld.exec_module(M)
log = Path(tempfile.mkdtemp()) / "sub" / "r.jsonl"
os.environ["MINI_REFUSAL_LOG"] = str(log)
os.environ.pop("MINI_LOG_REFUSALS", None)
M.log_intake_refusal("/x/run-a", {"class": "needs-split", "reasons": ["3 target functions"]}, False)
assert not log.exists(), "default OFF"
os.environ["MINI_LOG_REFUSALS"] = "1"
M.log_intake_refusal("/x/run-a", {"class": "local-ready"}, False)
assert not log.exists(), "local-ready is not a refusal"
M.log_intake_refusal("/x/run-a", {"class": "needs-split", "reasons": ["3 target functions"]}, True)
r = json.loads(log.read_text().splitlines()[0])
assert r["run"] == "run-a" and r["class"] == "needs-split" and r["overridden"] is True and r["reasons"] == ["3 target functions"], r
os.environ["MINI_REFUSAL_LOG"] = "/proc/nope/x/r.jsonl"
M.log_intake_refusal("/x/run-b", {"class": "not-local"}, False)  # unwritable path: swallowed
print("test-mini-refusal-log ok")
