#!/usr/bin/env python3
"""Tests for mini-graph lookups and the pre-submit freshness warning (plain python; PASS/FAIL lines; exit 1 on any FAIL)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("mini_graph", HERE / "mini_graph.py")
mg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mg)
fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  {detail}"))
    fails += not cond


def cli(cwd, *argv, env=None):
    e = {k: v for k, v in os.environ.items() if k not in ("MINI_GRAPH", "MINI_GRAPH_JSON")}
    e.update(env or {})
    return subprocess.run([sys.executable, str(HERE / "mini-graph"), *argv], cwd=cwd, capture_output=True, text=True, env=e)


with tempfile.TemporaryDirectory() as td:
    wt, gp, run = mg._fixture(td)
    G = mg.load(gp)
    L = lambda *a: mg.lookup(G, wt, *a)
    out = L("callers", ["src/a.js", "f1"])
    check("callers", out[0].startswith("callers of src/a.js:f1 (2)") and "src/a.js:2 f2" in out and "src/b.js:2 g" in out, out)
    out = L("callees", ["src/a.js", "f2"])
    check("callees", "src/a.js:1 f1" in out and "src/a.js:3 f3" in out, out)
    out = L("where", ["f2"])
    check("where", out == ["where f2 (1):", "src/a.js:2 f2"], out)
    out = L("neighbors", ["src/a.js"])
    check("neighbors importers", "  src/b.js (imports_from)" in out and "  test-a.js (imports_from)" in out, out)
    check("neighbors peers", any("src/a.js" not in x and "src/b.js:g" in x for x in out) or "community peers:" in out, out)
    check("missing fn", "no function" in L("callers", ["src/a.js", "nope"])[0])
    # STALE path
    (wt / "src/a.js").write_text("function f1() { return 1; }\n")
    for c, a in (("callers", ["src/a.js", "f1"]), ("callees", ["src/a.js", "f2"]), ("neighbors", ["src/a.js"])):
        check(f"stale {c}", L(c, a) == ["STALE: graph not trusted for src/a.js, use grep"], L(c, a))
    check("stale where marks hit", L("where", ["f2"])[1].endswith("[STALE: verify with grep]"), L("where", ["f2"]))
    check("fresh file still served", L("neighbors", ["src/b.js"])[0] == "neighbors of src/b.js:")
    run("checkout", "--", "src/a.js")
    # truncation
    big = json.loads(gp.read_text())
    for i in range(80):
        big["nodes"].append({"id": f"x{i}", "label": f"helper{i}()", "source_file": "src/a.js", "source_location": f"L{10 + i}", "_callable": True, "community": 1})
        big["links"].append({"source": f"x{i}", "target": "a_f1", "relation": "calls"})
    gp2 = gp.parent / "big.json"
    gp2.write_text(json.dumps(big))
    (gp.parent / "manifest.json").exists()
    G2 = mg.load(gp2)
    out = mg.lookup(G2, wt, "callers", ["src/a.js", "f1"])
    check("truncation", len(out) == 40 and out[-1].startswith("... (") and "more lines cut" in out[-1], (len(out), out[-1]))
    # CLI end to end
    r = cli(wt, "callers", "src/a.js", "f1", env={"MINI_GRAPH_JSON": str(gp)})
    check("cli callers via MINI_GRAPH_JSON", r.returncode == 0 and "src/b.js:2 g" in r.stdout, r.stdout + r.stderr)
    r = cli(wt, "--graph", str(gp), "where", "g")
    check("cli --graph where", "src/b.js:2 g" in r.stdout, r.stdout + r.stderr)
    r = cli(wt, "callers", "src/a.js", "f1", env={"MINI_GRAPH": str(gp)})
    check("cli MINI_GRAPH=*.json", "callers of" in r.stdout, r.stdout)
    r = cli(wt, "callers", str(wt / "src/a.js"), "f1", env={"MINI_GRAPH_JSON": str(gp)})
    check("cli absolute path", "callers of src/a.js:f1" in r.stdout, r.stdout)
    # no-graph case: fixture graph is outside wt/../graphify-out, so locate fails
    r = cli(wt, "where", "f1")
    check("no graph", r.returncode == 0 and r.stdout.strip() == "no graph for this repo: use grep", r.stdout + r.stderr)
    before = sorted(p.name for p in Path(td).rglob("*") if ".git" not in p.parts)
    # submit warning
    check("warn fresh is empty", mg.submit_warning(G, wt, ["src/a.js", "src/b.js"]) == "")
    check("warn no files", mg.submit_warning(G, wt, []) == "")
    check("warn absent", mg.submit_warning(None, wt, ["src/a.js"]).startswith("WARNING: no graphify graph") and "graphify update ." in mg.submit_warning(None, wt, ["src/a.js"]))
    (wt / "src/a.js").write_text("function f1() { return 1; }\n")
    w = mg.submit_warning(G, wt, ["src/a.js", "src/b.js", "unlisted.js"], wtroot="/repo")
    check("warn stale count + command", w.startswith("WARNING: graph stale for 2 of 3") and w.endswith("cd /repo && graphify update .") and "\n" not in w, w)
    # mini-submit wiring: graph_warning(steps) locates the graph via the spec `graph` key
    import importlib.machinery
    ld = importlib.machinery.SourceFileLoader("mini_submit_t", str(HERE / "mini-submit"))
    ms = importlib.util.module_from_spec(importlib.util.spec_from_loader("mini_submit_t", ld))
    ld.exec_module(ms)
    step = {"baseline_from": str(wt), "allowed": ["src/a.js", "src/b.js"], "graph": str(gp)}
    w = ms.graph_warning([step])
    check("submit warns on stale", w.startswith("WARNING: graph stale for 1 of 2") and "graphify update ." in w, w)
    os.environ["MINI_GRAPH_WARN"] = "off"
    check("MINI_GRAPH_WARN=off silences", ms.graph_warning([step]) == "")
    del os.environ["MINI_GRAPH_WARN"]
    run("checkout", "--", "src/a.js")
    check("submit silent when fresh", ms.graph_warning([step]) == "")
    check("submit warns when graph missing", ms.graph_warning([{**step, "graph": str(gp) + ".nope"}]).startswith("WARNING: no graphify graph"))
    check("submit skips specs without allowed", ms.graph_warning([{"baseline_from": str(wt), "allowed": []}]) == "")
    check("read-only", before == sorted(p.name for p in Path(td).rglob("*") if ".git" not in p.parts))

sys.exit(1 if fails else 0)
