#!/usr/bin/env python3
"""Regression: a human-cancelled (plan_cancel) unfinished bundle is "superseded", not "failed".
Run: python3 test-bundle-cancelled.py [--revert-check]  (revert uses the pre-fix .bak and must FAIL)"""
import glob, importlib.util, os, sys
here = os.path.dirname(os.path.abspath(__file__))
src = os.path.join(here, "bundle_view.py")
if "--revert-check" in sys.argv:
    src = sorted(glob.glob(os.path.join(here, "bundle_view.py.bak-*-cancelled")))[0]
from importlib.machinery import SourceFileLoader
spec = importlib.util.spec_from_loader("bv", SourceFileLoader("bv", src)); bv = importlib.util.module_from_spec(spec); spec.loader.exec_module(bv)
view = {"key": "p", "through": 5, "total": 9, "slices": [
    {"sid": "a", "phase": "done"}, {"sid": "b", "phase": "escalated", "attention": True}]}
rec = {"label": "p", "by": "ollama-dispatch-slice --cancel"}
try:
    assert bv.bundle_outcome(view, {}, rec)[0] == "superseded", "cancelled must be superseded"
    assert bv.bundle_outcome(view, {})[0] == "failed", "uncancelled failed bundle must still read failed"
    fin = {"key": "p", "through": 1, "total": 1, "slices": [{"sid": "a", "phase": "done"}]}
    assert bv.bundle_outcome(fin, {}, rec)[0] == "finished", "finished stays finished"
except (AssertionError, TypeError) as e:
    print("FAIL:", e); sys.exit(1)
print("PASS"); 

# --- second property: the live panel (needs_attention rows + load_view slice flags) ---
api_src = os.path.join(here, "ollama-queue-api.py")
if "--revert-check" in sys.argv:
    api_src = sorted(glob.glob(os.path.join(here, "ollama-queue-api.py.bak-*-cancelled")))[0]
sys.path.insert(0, here)
aspec = importlib.util.spec_from_loader("qa", SourceFileLoader("qa", api_src)); qa = importlib.util.module_from_spec(aspec); aspec.loader.exec_module(qa)
import tempfile, json, pathlib
with tempfile.TemporaryDirectory() as td:
    td = pathlib.Path(td)
    (td / "pc.json").write_text(json.dumps({"label": "pc", "order": ["a"], "slices": {"a": {"status": "escalated"}}}))
    (td / "pc.cancelled").write_text(json.dumps({"label": "pc", "by": "t"}))
    (td / "pn.json").write_text(json.dumps({"label": "pn", "order": ["a"], "slices": {"a": {"status": "escalated"}}}))
    ld = lambda p: bv.load_view(p, [], td, td, td, td / "h.json", td, [], lambda _p: False)
    try:
        assert not any(s["attention"] for s in ld("pc")["slices"]), "cancelled plan slices must not flag attention"
        assert any(s["attention"] for s in ld("pn")["slices"]), "uncancelled escalated slice must still flag"
    except (AssertionError, TypeError, KeyError) as e:
        print("FAIL:", e); sys.exit(1)
print("PASS (live view)")
