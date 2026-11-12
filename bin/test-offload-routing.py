#!/usr/bin/env python3
import importlib.util, sys, json, tempfile
from pathlib import Path
s = importlib.util.spec_from_file_location("r", Path(__file__).resolve().parent / "offload_routing.py"); R = importlib.util.module_from_spec(s); s.loader.exec_module(R)
f = []
def ok(c, m):
    if not c: f.append(m); print("FAIL", m)
E = {}
ok(R.route("research", env=E)["lane"] == "claude", "off by default")
E = {"OFFLOAD_LOCAL_RESEARCH": "1"}
d = R.route("research", True, env=E); ok(d["lane"] == "darkbloom" and d["model"] and not d["applied"], "on+working: model by job, dry-run")
d = R.route("research", False, env=E); ok(d["model"] is None and d["decided_by"] == "bloomgauge-manager", "idle: Manager decides")
ok(R.route("research", True, env={**E, "OFFLOAD_ROUTING_DRYRUN": "0"})["applied"], "dryrun 0 applies")
ok(R.route("research", True, ["lib/resell_pnl.py"], env=E)["lane"] == "claude", "money stays on claude")
ok(R.route("research", True, env={**E, "OFFLOAD_ROUTING": "off"})["lane"] == "claude", "kill switch")
ok(R.route("diagnose", env=E)["lane"] == "claude", "other kinds need own flag")
src = {"a.py": "def f(x):\n    return x  +  1\n"}
ok(R.quote_gate("it does `return x + 1` here", src)[0], "quote exists (whitespace-normalised)")
ok(not R.quote_gate("it does `return x + 2` here", src)[0], "fabricated quote rejected")
ok(not R.quote_gate("no quotes at all", src)[0], "no quote rejected for research")
ok(R.diagnosis_gate({"repro_cmd": "x", "expect": "boom"}, run=lambda c, w: (1, "AssertionError boom"))[0], "failing repro accepted")
ok(not R.diagnosis_gate({"repro_cmd": "x", "expect": "boom"}, run=lambda c, w: (0, "ok"))[0], "passing repro rejected")
ok(not R.diagnosis_gate({"repro_cmd": "x", "expect": "boom"}, run=lambda c, w: (1, "other"))[0], "wrong symptom rejected")
ok(not R.diagnosis_gate({"claim": "x"})[0], "no repro rejected")
with tempfile.TemporaryDirectory() as t:
    (Path(t) / "x.done.json").write_text(json.dumps({"runner": "/b/mini-swe-runner.py", "active_s": 100, "runner_verdict": "PASS", "model": "m"}))
    (Path(t) / "sp" / "a").mkdir(parents=True); (Path(t) / "sp" / "a" / "spec.json").write_text("{}")
    e = R.gpu_estimate(["m"], Path(t) / "sp", t); ok(e["specs"] == 1 and e["models"]["m"]["median_pass_s"] == 100, "estimate math")
print("OK" if not f else f"{len(f)} FAILED"); sys.exit(1 if f else 0)
