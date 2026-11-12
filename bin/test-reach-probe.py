#!/usr/bin/env python3
"""REACH PROBE tests (2026-10-10, aw-codec-floor s3). Synthetic target + fixture reproducing the
empty-stub early return and a recorder that is never invoked:
  1. reach_probe.probe_inplace names the stub that returned set(), the target line of the early
     return, the helper that returned False, and the never-invoked recorder stub.
  2. the generated auto-harness-check.py embeds the REACH PROBE block when the refimpl fails ALL K
     cases, and does NOT when only some cases fail.
  3. the block is <= 25 lines; a JS fixture / missing refimpl / passing fixture -> no block (fail-open).
Run: python3 ~/bin/test-reach-probe.py   (--revert-check is done via pipeline-canary seam)."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(os.path.expanduser("~/bin"))
if "--bin" in sys.argv:
    BIN = Path(sys.argv[sys.argv.index("--bin") + 1])
_sp = importlib.util.spec_from_file_location("reach_probe", BIN / "reach_probe.py")
rp = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(rp)
SRC = os.environ.get("AUTO_SRC") or str(BIN / "ollama-dispatch-auto")
ld = SourceFileLoader("oda_rp", SRC)
oda = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_rp", ld))
ld.exec_module(oda)
FAILS = []


def chk(name, cond, detail=""):
    print(("ok   - " if cond else "FAIL - ") + name + ("" if cond else "  " + str(detail)[:600]))
    if not cond:
        FAILS.append(name)


TARGET = '''def get_titles():
    return {"real title"}


def matches(name, titles):
    return any(t in name for t in titles)


def record_activity(cat, msg):
    raise RuntimeError("real recorder")


def handle(name):
    titles = get_titles()
    if not matches(name, titles):
        return None
    record_activity("hit", name)
    return "done"
'''
FIXTURE = '''import importlib.util, sys
spec = importlib.util.spec_from_file_location("target", "target.py")
target = importlib.util.module_from_spec(spec)
spec.loader.exec_module(target)
rec = []

def _patch():
    target.get_titles = lambda: set()
    target.record_activity = lambda c, m: rec.append((c, m))

def case_a():
    _patch(); rec.clear()
    target.handle("real title here")
    assert len(rec) == 1, "expected 1 activity, got %d" % len(rec)
    return 'ok'

def case_b():
    _patch(); rec.clear()
    target.handle("another real title")
    assert len(rec) == 1, "expected 1 activity, got %d" % len(rec)
    return 'ok'

CASES = [("case a", case_a), ("case b", case_b)]

def main():
    fails = 0
    for desc, thunk in CASES:
        try:
            got = thunk()
        except Exception as e:
            print("  FAIL {} -- raised {}: {}".format(desc, type(e).__name__, e)); fails += 1; continue
    print("  {}/{} case(s) passed".format(len(CASES) - fails, len(CASES)))
    return 1 if fails else 0

if __name__ == "__main__":
    sys.exit(main())
'''
REFIMPL = "open('target.py','a').write('\\n# refimpl applied\\n')\n"


def build(fixture=FIXTURE, verify=None, target=None):
    wt = tempfile.mkdtemp(prefix="reachp-")
    sh = lambda *a: subprocess.run(a, cwd=wt, check=True, capture_output=True)
    sh("git", "init", "-q", ".")
    sh("git", "config", "user.email", "t@t"); sh("git", "config", "user.name", "t")
    Path(wt, "target.py").write_text(target or TARGET)
    Path(wt, "TASK.md").write_text("## Must contain\n- `# refimpl applied`\n\nFix `handle`. Only edit `target.py`; do not edit verify.sh\n")
    Path(wt, "refimpl.py").write_text(REFIMPL)
    Path(wt, "test_fixture.py").write_text(fixture)
    Path(wt, "verify.sh").write_text(verify or "cd \"$(dirname \"$0\")\"\npython3 test_fixture.py && echo VERIFY_OK || exit 1\n")
    Path(wt, ".dispatch-harness.json").write_text('{"target": "target.py"}')
    sh("git", "add", "-A"); sh("git", "commit", "-q", "-m", "base")
    return wt


wt = build()
subprocess.run([sys.executable, "refimpl.py"], cwd=wt, check=True)       # refimpl applied, like in the check
out = rp.probe_inplace(wt, "target.py", "test_fixture.py")
print(out)
chk("block present with marker", out.startswith(rp.MARK), out)
chk("<= 25 lines", 0 < len(out.splitlines()) <= 25, len(out.splitlines()))
chk("names the function under test", "handle()" in out, out)
chk("names the early return line + source", "returned None at target.py:16 `return None`" in out, out)
chk("names the stub that returned an empty set", "get_titles" in out and "set()" in out and "STUB" in out, out)
chk("names the helper that returned False", "matches" in out and "False" in out, out)
chk("names the never-invoked recorder stub", "NEVER invoked" in out and "record_activity" in out, out)
chk("hint tells the author the guard exited early", "HINT:" in out, out)
chk("worktree untouched by the probe (inplace leaves files alone)", Path(wt, "target.py").read_text().count("# refimpl applied") == 1)

# copy mode: refimpl applied only in the copy; the real tree stays at baseline
wt2 = build()
out2 = rp.probe_copy(wt2, "target.py", "test_fixture.py")
chk("copy mode produces the block", out2.startswith(rp.MARK) and "get_titles" in out2, out2)
chk("copy mode leaves the real tree at baseline", "# refimpl applied" not in Path(wt2, "target.py").read_text())

# fail-open
chk("no fixture -> empty", rp.probe_inplace(tempfile.mkdtemp(), "target.py", "test_fixture.py") == "")
good = FIXTURE.replace("target.get_titles = lambda: set()", "target.get_titles = lambda: {'real title'}")
wt3 = build(good)
chk("passing fixture -> copy mode emits nothing", rp.probe_copy(wt3, "target.py", "test_fixture.py") == "")
wt4 = build()
Path(wt4, "refimpl.py").unlink()
chk("no refimpl -> copy mode empty", rp.probe_copy(wt4, "target.py", "test_fixture.py") == "")

# --- through the REAL generated auto-harness-check.py ---------------------------------------
def hc(wt):
    oda.write_harness_check(Path(wt), "python", requires=["# refimpl applied"])
    r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt, capture_output=True, text=True)
    return r.returncode, r.stdout + r.stderr


wt5 = build()
rc, o = hc(wt5)
chk("harness-check: K-of-K failure -> REACH PROBE embedded", rc != 0 and rp.MARK in o and "get_titles" in o and "set()" in o, o[-1500:])
chk("harness-check: probe sits AFTER the hint (survives a tail cut)", o.rfind(rp.MARK) > o.find("COMMON CAUSE") > 0, o[-1500:])
chk("harness-check: tree back at baseline", "# refimpl applied" not in Path(wt5, "target.py").read_text())
half = FIXTURE.replace("target.get_titles = lambda: set()", "target.get_titles = lambda: {'real title'}").replace(
    'target.handle("another real title")', 'target.handle("zzz")').replace(
    'CASES = [("case a", case_a), ("case b", case_b)]',
    'def case_c():\n    _patch(); rec.clear()\n    target.handle("zzz2")\n    assert len(rec) == 1, "expected 1 activity, got %d" % len(rec)\n    return "ok"\n\n'
    'CASES = [("case a", case_a), ("case b", case_b), ("case c", case_c)]')
wt6 = build(half)
rc, o = hc(wt6)
chk("harness-check: only SOME cases fail -> no probe (not the K-of-K condition)", rc != 0 and "2 of 3 failed" in o and rp.MARK not in o, o[-900:])

# --- the slicer's RE-SPEC note carries the probe to the re-authoring stage ----------------------
_sl = SourceFileLoader("ods_rp", str(BIN / "ollama-dispatch-slice"))
sl = importlib.util.module_from_spec(importlib.util.spec_from_loader("ods_rp", _sl))
_sl.exec_module(sl)
wt7 = build()
st = {"intent": "do the thing", "author_launches_total": 1}
chk("respec applied", sl.respec_on_cap(st, "ERROR: auto failed", 1, 5, wt=wt7) is True)
chk("respec intent carries the REACH PROBE (names the empty stub + the unreached recorder)",
    rp.MARK in st["intent"] and "get_titles" in st["intent"] and "record_activity" in st["intent"], st["intent"][-900:])
chk("respec probe did not touch the real worktree", "# refimpl applied" not in Path(wt7, "target.py").read_text())
st2 = {"intent": "do the thing"}
chk("respec without a worktree still works, no probe", sl.respec_on_cap(st2, "ERROR: x", 1, 5) is True and rp.MARK not in st2["intent"])
wt8 = build(FIXTURE.replace("target.get_titles = lambda: set()", "target.get_titles = lambda: {'real title'}"))
st3 = {"intent": "i"}
sl.respec_on_cap(st3, "ERROR: x", 1, 5, wt=wt8)
chk("respec on a PASSING fixture adds no probe", rp.MARK not in st3["intent"])

# --- exception from a wrong-TYPE stub (list where the real helper returns a dict) -------------
TARGET_X = '''def get_all_torrents():
    resp = {"result": {}}
    return resp.get("result", {})


def record_activity(cat, msg):
    raise RuntimeError("real recorder")


def handle(name):
    torrents = get_all_torrents()
    for h, info in torrents.items():
        if info["name"] == name:
            record_activity("hit", name)
            return h
    return None
'''
FIXTURE_X = FIXTURE.replace("target.get_titles = lambda: set()", "target.get_all_torrents = lambda: [{'hash': 'h1', 'name': 'real title here'}]"
                            ).replace("target.record_activity = lambda c, m: rec.append((c, m))", "target.record_activity = lambda c, m: rec.append((c, m))")
wtx = build(FIXTURE_X, target=TARGET_X)
subprocess.run([sys.executable, "refimpl.py"], cwd=wtx, check=True)
ox = rp.probe_inplace(wtx, "target.py", "test_fixture.py")
print(ox)
chk("exc: HINT headlines the raised exception + type", "HINT: case raised AttributeError: 'list' object has no attribute 'items'" in ox, ox)
chk("exc: names the failing target line + source", "target:12 `for h, info in torrents.items():`" in ox, ox)
chk("exc: names the stub, its fixture line and the returned type", "stub get_all_torrents (fixture:" in ox and "returned list" in ox, ox)
chk("exc: says what the op needs (mapping)", ".items() => mapping (dict)" in ox, ox)
chk("exc: shows the REAL function's return shape (dict)", "real get_all_torrents (target:1) returns dict" in ox, ox)
chk("exc: <= 25 lines", len(ox.splitlines()) <= 25, len(ox.splitlines()))
chk("exc: guard HINT is NOT used when an exception was raised", "never reached" not in ox and "the guard at" not in ox, ox)
chk("no exception -> still the guard/stub hint (regression)", "HINT: case raised" not in out and "HINT:" in out)
o_hc = hc(wtx) if False else None

print("\n%d failure(s)" % len(FAILS))
if not FAILS:
    print("REACH_PROBE_OK")
sys.exit(1 if FAILS else 0)
