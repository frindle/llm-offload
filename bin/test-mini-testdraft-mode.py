#!/usr/bin/env python3
"""Lever 1 live path: mini-submit accepts a test_draft job only with MINI_TESTDRAFT_MODE=on, refuses must-not-offload paths and weakened
accept commands; the runner's testdraft_accept maps check rc to PASS/FAIL."""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path
H = Path(__file__).resolve().parent
def load(n, p):
    loader = importlib.machinery.SourceFileLoader(n, str(p)); s = importlib.util.spec_from_loader(n, loader); m = importlib.util.module_from_spec(s); loader.exec_module(m); return m
import importlib.machinery
MS = load("mini_submit", H / "mini-submit")
R = load("mini_swe_runner", H / "mini-swe-runner.py")
fails = []
def ok(c, m):
    if not c: fails.append(m); print("FAIL", m)
class C: pass
def ctx(spec):
    c = C(); c.spec = spec; return c
good = {"baseline_from": "/x/wt", "allowed": ["lib/a.py"], "test_file": "lib/test_a.py", "phase_a_only": True,
        "draft_accept_cmd": "python3 /x/mini-testdraft check /x/spec.json --draft {draft}"}
os.environ.pop("MINI_TESTDRAFT_MODE", None)
try: MS.c3(ctx(good)); ok(False, "off by default")
except MS.Fail as f: ok("OFF" in f.reason, "off by default reason")
os.environ["MINI_TESTDRAFT_MODE"] = "on"
ok("test_draft enabled" in MS.c3(ctx(good)), "on accepted")
for bad, why in (({**good, "allowed": ["lib/resell_pnl.py"]}, "money"), ({**good, "test_file": "t/test_auth.py"}, "auth"),
                 ({**good, "draft_accept_cmd": "true {draft}"}, "not testdraft check"),
                 ({**good, "draft_accept_cmd": good["draft_accept_cmd"] + " --allow-unscored"}, "weakened")):
    try: MS.c3(ctx(bad)); ok(False, "must refuse " + why)
    except MS.Fail: pass
os.environ["MINI_TESTDRAFT"] = "off"
try: MS.c3(ctx(good)); ok(False, "kill switch")
except MS.Fail: pass
os.environ.pop("MINI_TESTDRAFT")
# runner: accept cmd rc -> PASS/FAIL, and the draft is kept beside the run
with tempfile.TemporaryDirectory() as d:
    cwd = Path(d, "w"); cwd.mkdir(); Path(cwd, "t.py").write_text("x")
    rd = Path(d, "run"); rd.mkdir()
    rc, line = R.testdraft_accept({"draft_accept_cmd": "grep -q x {draft}"}, str(cwd), "t.py", rd)
    ok(rc == R.RC_OK and line.startswith("PASS mode=test-draft"), f"accept -> PASS ({line})")
    rc, line = R.testdraft_accept({"draft_accept_cmd": "grep -q y {draft}"}, str(cwd), "t.py", rd)
    ok(rc == R.RC_FAIL and line.startswith("FAIL mode=test-draft"), "reject -> FAIL")
    rc, line = R.testdraft_accept({}, str(cwd), "t.py", rd)
    ok(rc == R.RC_FAIL, "no cmd -> FAIL")
    ok((rd / "draft_t.py").read_text() == "x", "draft kept")
print("OK" if not fails else f"{len(fails)} FAILED"); sys.exit(1 if fails else 0)
