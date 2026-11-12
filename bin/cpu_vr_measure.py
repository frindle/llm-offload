#!/usr/bin/env python3
"""cpu_vr_measure.py -- runs verify-relevance.measure_applied on the CURRENT worktree and prints the
record as JSON. This is the remote half of ollama-dispatch-preflight's relevance stage (CPU lane):
the runner executes it next to a shipped copy of the tree; the preflight parses the JSON back.

    cpu_vr_measure.py <args.json>      args: {"verify": str, "diff_text": str, "kw": {...}}

`mutants` (every mutant's full source) is dropped from the record: the preflight discards it too
(it slims the record before storing), and the sign-off artifact that needs it runs locally.
"""
import importlib.util
import json
import sys
from pathlib import Path

spec = json.load(open(sys.argv[1]))
vr = Path(__file__).resolve().parent / "verify-relevance.py"
sp = importlib.util.spec_from_file_location("verify_relevance", vr)
mod = importlib.util.module_from_spec(sp)
sp.loader.exec_module(mod)
kw = dict(spec.get("kw") or {})
rec = mod.measure_applied(Path.cwd(), spec["verify"], spec["diff_text"], **kw)
rec.pop("mutants", None)
sys.stdout.write(json.dumps(rec, default=str))
