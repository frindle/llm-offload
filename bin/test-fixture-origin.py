#!/usr/bin/env python3
"""FIXTURE-ORIGIN tests (2026-10-10e, rt-costco-receipt-attach s3). Synthetic node:test TAP + python
traceback whose top frame is in the FIXTURE:
  1. fixture_origin.analyze emits the <=12 line block (file:line, source, caller in target, stub hint)
  2. a throw whose top frame is in the TARGET / refimpl, or internal-only frames, emits nothing
  3. the generated auto-harness-check.py appends the block LAST (after hints) and stashes it
  4. the slicer's respec_on_cap folds the stashed block into the re-spec intent
Run: python3 ~/bin/test-fixture-origin.py"""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(os.path.expanduser("~/bin"))
if "--bin" in sys.argv:
    BIN = Path(sys.argv[sys.argv.index("--bin") + 1])
_sp = importlib.util.spec_from_file_location("fixture_origin", BIN / "fixture_origin.py")
fo = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(fo)
ld = SourceFileLoader("oda_fo", os.environ.get("AUTO_SRC") or str(BIN / "ollama-dispatch-auto"))
oda = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_fo", ld))
ld.exec_module(oda)
FAILS = []


def chk(name, cond, detail=""):
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else "  " + str(detail)[:700]))
    if not cond:
        FAILS.append(name)


wt = tempfile.mkdtemp(prefix="fxo-")
FX = """import { mock } from 'node:test';
// Returns a single order or an array of orders depending on how many matches exist.
function findManyStub(_a) {
  return 1;
}
mock.module('@/lib/m', { exports: { pick: (orders) => orders.length === 1 ? 1 : orders.map((o) => o.id) } });
"""
Path(wt, "verify.test.ts").write_text(FX)
Path(wt, "route.ts").write_text("export async function POST() {\n  return pick(rows);\n}\n")
TAP = """not ok 2 - ambiguous
  ---
  error: 'orders.map is not a function'
  code: 'ERR_TEST_FAILURE'
  name: 'TypeError'
  stack: |-
    pick (%s/verify.test.ts:6:60)
    POST (%s/route.ts:2:10)
    async Test.run (node:internal/test_runner/test:1404:7)
  ...
1..2
# fail 1
""" % (wt, wt)
b = fo.analyze(TAP, wt, ["verify.test.ts"], "route.ts")
print(b)
chk("TAP: FIXTURE-ORIGIN headline", b.startswith("FIXTURE-ORIGIN: the failure was raised inside the fixture's own code at verify.test.ts:6"), b)
chk("TAP: source line from disk", "orders.map((o) => o.id)" in b, b)
chk("TAP: caller in the target", "called from route.ts:2 (POST)" in b, b)
chk("TAP: do NOT change refimpl.py", "do NOT change refimpl.py" in b and "wrong shape" in b, b)
chk("TAP: names the mixed-shape stub", "findManyStub" in b and "single order or an array" in b, b)
TAP_ASSERT = """not ok 3 - skips linked
  ---
  error: 'Expected values to be strictly equal'
  code: 'ERR_ASSERTION'
  name: 'AssertionError'
  expected: 1
  actual: 0
  operator: 'strictEqual'
  stack: |-
    TestContext.<anonymous> (%s/verify.test.ts:6:10)
    async Test.run (node:internal/test_runner/test:1404:7)
  ...
1..3
# fail 1
""" % (wt,)
chk("TAP: an AssertionError raised at a fixture assert line is NOT a fixture-origin defect (live false positive, Costco f639e05d655c)",
    fo.analyze(TAP_ASSERT, wt, ["verify.test.ts"], "route.ts") == "", fo.analyze(TAP_ASSERT, wt, ["verify.test.ts"], "route.ts"))
chk("TAP: <= 12 lines", 0 < len(b.splitlines()) <= 12, len(b.splitlines()))
tgt_top = TAP.replace("pick (%s/verify.test.ts:6:60)" % wt, "pick (%s/route.ts:2:10)" % wt)
chk("TAP: top frame in target -> nothing", fo.analyze(tgt_top, wt, ["verify.test.ts"], "route.ts") == "")
chk("TAP: internal-only frames -> nothing", fo.analyze(TAP.replace(wt + "/verify.test.ts:6:60", "node:internal/x:1:1"), wt, ["verify.test.ts"], "route.ts") == "")
chk("garbage / empty -> nothing (fail-open)", fo.analyze("", wt) == "" and fo.analyze(None) == "" and fo.analyze("stack: |-\n   ((((", wt) == "")
chk("missing source file -> nothing (fail-open)", fo.analyze(TAP.replace(wt + "/verify.test.ts", "/nonexistent/verify.test.ts"), None, ["verify.test.ts"], "route.ts") == "")

PYFX = "import target\n\n\ndef stub():\n    return [1]\n\n\ndef case():\n    return stub().items()\n"
Path(wt, "test_fixture.py").write_text(PYFX)
TB = """Traceback (most recent call last):
  File "%s/target.py", line 9, in handle
    return fx.case()
  File "%s/test_fixture.py", line 9, in case
    return stub().items()
AttributeError: 'list' object has no attribute 'items'
""" % (wt, wt)
# python traceback: innermost frame is the fixture (call came from the target)
b2 = fo.analyze(TB, wt, ["test_fixture.py"], "target.py")
print(b2)
chk("PY: FIXTURE-ORIGIN at test_fixture.py:9", "at test_fixture.py:9 `return stub().items()`" in b2 and "called from target.py:9 (handle)" in b2, b2)
chk("PY: names the offending object", "`stub`" in b2 or "list" in b2, b2)
TB2 = TB.replace("test_fixture.py", "target.py").replace("line 9, in case", "line 20, in other")
chk("PY: innermost frame in target -> nothing", fo.analyze(TB2, wt, ["test_fixture.py"], "target.py") == "")

# --- through the generated auto-harness-check.py -----------------------------------------
w2 = tempfile.mkdtemp(prefix="fxo2-")
sh = lambda *a: subprocess.run(a, cwd=w2, check=True, capture_output=True)
sh("git", "init", "-q", "."); sh("git", "config", "user.email", "t@t"); sh("git", "config", "user.name", "t")
Path(w2, "target.py").write_text("x = 1\n")
Path(w2, "TASK.md").write_text("## Must contain\n- `# refimpl applied`\n\nFix. Only edit `target.py`; do not edit verify.sh\n")
Path(w2, "refimpl.py").write_text("open('target.py','a').write('\\n# refimpl applied\\n')\n")
Path(w2, "test_fixture.py").write_text("import sys\nsys.exit(0)\n")
Path(w2, "verify.sh").write_text("cd \"$(dirname \"$0\")\"\ncat tap.txt\nexit 1\n")
Path(w2, ".dispatch-harness.json").write_text('{"target": "target.py"}')
Path(w2, "verify.test.ts").write_text(FX)
Path(w2, "tap.txt").write_text(TAP.replace(wt, w2))
sh("git", "add", "-A"); sh("git", "commit", "-q", "-m", "base")
oda.write_harness_check(Path(w2), "python", requires=["# refimpl applied"])
r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=w2, capture_output=True, text=True)
o = r.stdout + r.stderr
chk("harness-check: block embedded", r.returncode != 0 and "FIXTURE-ORIGIN:" in o, o[-1500:])
chk("harness-check: block is at the very END (survives the ledger tail cut)", o.rstrip().splitlines()[-1].strip().startswith("The target/refimpl code is NOT"), o[-900:])
chk("harness-check: stashed for the slicer", "FIXTURE-ORIGIN" in fo.read_stash(w2), fo.read_stash(w2))

# --- slicer re-spec folds it in ------------------------------------------------------------
_sl = SourceFileLoader("ods_fo", str(BIN / "ollama-dispatch-slice"))
sl = importlib.util.module_from_spec(importlib.util.spec_from_loader("ods_fo", _sl))
_sl.exec_module(sl)
st = {"intent": "do it", "author_launches_total": 1}
chk("respec applied", sl.respec_on_cap(st, "ERROR: auto failed", 1, 5, wt=w2) is True)
chk("respec intent carries FIXTURE-ORIGIN", "FIXTURE-ORIGIN:" in st["intent"] and "findManyStub" in st["intent"], st["intent"][-700:])
fo.stash(w2, "")
st2 = {"intent": "do it"}
sl.respec_on_cap(st2, "ERROR: x", 1, 5, wt=w2)
chk("cleared stash -> no block in re-spec", "FIXTURE-ORIGIN" not in st2["intent"])

print("FIXTURE_ORIGIN_OK" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
