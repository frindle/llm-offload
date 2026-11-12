#!/usr/bin/env python3
"""test-relevance-io-adapter.py -- the preflight relevance NO-GO loop on real-I/O adapters.

chat-frontend-plan s5-model-call (2026-10-04) ran 14 author/refine jobs: every mutant
in `_default_http` (a urlopen wrapper reached only as `http_func or _default_http`)
survived a HERMETIC fixture by design, the site read UNEXERCISED -> LOW -> NO-GO, and
the refine loop's no-progress guard never tripped because refine edits shifted the
survivors' line numbers. Two fixes, both tested here:

  verify-relevance.py   a `## Real-I/O adapters` declaration in TASK.md excludes a
                        function's mutants ONLY when it validates (calls a real-I/O
                        primitive, referenced only as an injectable default, thin);
                        exclusions/rejections/undeclared candidates are REPORTED.
  ollama-dispatch-auto  a line/target-independent survivor-SET signature escalates
                        after N identical rounds with the slicer's NO PROGRESS marker.

REVERT tests: a copy of verify-relevance.py with the evidence filter removed keeps the
declared run LOW; the old line-keyed signature never stalls on the s5 shape.
Hermetic: temp git repos only; URL is 127.0.0.1:9 (discard port, nothing listens) so a
mutant routing to the real adapter gets connection-refused, never a live server; no queue.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
FAILS = []


def check(name, got, want):
    ok = got == want
    print(f"{'PASS' if ok else 'FAIL'}: {name}" + ("" if ok else f"  got={got!r} want={want!r}"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    spec = importlib.util.spec_from_file_location(name, path)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def load_script(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


vr = load(BIN / "verify-relevance.py", "vr_live")

BASE = 'import json\nURL = "http://127.0.0.1:9/v1/chat/completions"\n'
ADAPTER = '''import urllib.error
import urllib.request


def _default_http(url, body, timeout):
    req = urllib.request.Request(url, data=body, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
'''
CALLER = '''

def call_model(body, http_func=None):
    try:
        status, raw = (http_func or _default_http)(URL, body, 60)
    except Exception as e:
        return {"status": "error", "text": type(e).__name__}
    if status != 200:
        return {"status": "error", "text": "http " + str(status)}
    return {"status": "done", "text": json.loads(raw)["choices"][0]["message"]["content"]}
'''
FIXED = BASE + "\n\n" + ADAPTER + CALLER
FIXTURE = '''import json, sys
sys.dont_write_bytecode = True
import mod
fails = 0
def ok(name, cond):
    global fails
    if not cond:
        fails += 1
        print("FAIL", name)
calls = []
def fake(status, raw=b"", exc=None):
    def f(url, body, timeout):
        calls.append((url, body, timeout))
        if exc:
            raise exc
        return status, raw
    return f
good = json.dumps({"choices": [{"message": {"content": "hi"}},
                               {"message": {"content": "no"}}]}).encode()
r = mod.call_model(b"B", fake(200, good))
ok("done", r == {"status": "done", "text": "hi"})
ok("args", calls[-1] == ("http://127.0.0.1:9/v1/chat/completions", b"B", 60))
ok("500", mod.call_model(b"B", fake(500)) == {"status": "error", "text": "http 500"})
ok("201", mod.call_model(b"B", fake(201, good)) == {"status": "error", "text": "http 201"})
ok("raise", mod.call_model(b"B", fake(0, exc=OSError("x"))) == {"status": "error", "text": "OSError"})
print("VERIFY_OK" if not fails else "FAILED")
sys.exit(1 if fails else 0)
'''


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)


def make_tree(tmp, fixed=FIXED):
    wt = Path(tmp)
    git(wt, "init", "-q")
    git(wt, "config", "user.email", "t@t")
    git(wt, "config", "user.name", "t")
    (wt / "mod.py").write_text(BASE)
    git(wt, "add", "mod.py")
    git(wt, "commit", "-qm", "base")
    (wt / "mod.py").write_text(fixed)
    (wt / "test_fixture.py").write_text(FIXTURE)
    return wt, git(wt, "diff", "-U0").stdout


def measure(mod, wt, diff, adapters):
    return mod.measure_applied(wt, "python3 test_fixture.py", diff, literals=[],
                               max_mutants=80, budget_s=300, io_adapters=adapters)


# ---- 1. structural validation (unit) -------------------------------------
res = vr.python_io_adapters(FIXED, ["_default_http"])
check("1a s5-shape adapter accepted", list(res["accepted"]), ["_default_http"])
check("1b call_model is NOT an adapter candidate", "call_model" in res["candidates"], False)
res = vr.python_io_adapters(FIXED, [])
check("1c undeclared adapter is only a CANDIDATE", (list(res["accepted"]), list(res["candidates"])),
      ([], ["_default_http"]))
res = vr.python_io_adapters(FIXED, ["call_model"])
check("1d declaring a logic function is REJECTED (no I/O primitive)",
      "no real-I/O primitive" in res["rejected"].get("call_model", ""), True)
direct = FIXED.replace("(http_func or _default_http)(URL, body, 60)",
                       "_default_http(URL, body, 60) if http_func is None else http_func(URL, body, 60)")
res = vr.python_io_adapters(direct, ["_default_http"])
check("1e adapter called DIRECTLY is rejected", "DIRECT call" in res["rejected"].get("_default_http", ""),
      True)
fat = FIXED.replace("        return e.code, e.read()\n",
                    "        return e.code, e.read()\n" + "".join(
                        f"    x{i} = {i}\n" for i in range(12)))
res = vr.python_io_adapters(fat, ["_default_http"])
check("1f fat adapter (logic inside) is rejected", "not thin" in res["rejected"].get("_default_http", ""),
      True)
check("1g TASK.md declaration parsed",
      vr.declared_io_adapters("# T\n## Must contain\n- `call_model`\n\n## Real-I/O adapters\n"
                              "- `_default_http` -- urlopen wrapper\n\n## Scope\n- `x`\n"),
      ["_default_http"])
check("1h no section -> nothing declared", vr.declared_io_adapters("## Must contain\n- `abc`\n"), [])

# ---- 2. end-to-end measure_applied ---------------------------------------
with tempfile.TemporaryDirectory() as tmp:
    wt, diff = make_tree(tmp)
    r0 = measure(vr, wt, diff, [])
    unex_in_adapter = [s for s in r0["unexercised_sites"]
                       if 5 <= int(s.split(":")[1]) <= 14]
    check("2a UNDECLARED: verdict LOW (the s5 NO-GO, reproduced)", r0["verdict"], "low")
    check("2b UNDECLARED: unexercised sites are in the adapter", bool(unex_in_adapter), True)
    check("2c UNDECLARED: survivors tagged io_adapter_candidate",
          r0["io_adapter_candidate_survivors"], ["_default_http"])
    check("2d UNDECLARED: reason names the remedy", "## Real-I/O adapters" in r0["reason"], True)
    check("2e UNDECLARED: nothing excluded", r0["io_adapter_excluded"], 0)
    check("2f tree restored", (wt / "mod.py").read_text(), FIXED)

    r1 = measure(vr, wt, diff, ["_default_http"])
    check("2g DECLARED: verdict relevant", r1["verdict"], "relevant")
    check("2h DECLARED: adapter mutants excluded (>0) and reported",
          (r1["io_adapter_excluded"] > 0,
           [e["name"] for e in r1["io_adapters"]["excluded"]]), (True, ["_default_http"]))
    check("2i DECLARED: reason logs the exclusion", "EXCLUDED from evidence" in r1["reason"], True)
    check("2j DECLARED: call-site mutants still counted (no survivor outside adapter)",
          [s for s in r1["survivors"] if s.get("func") != "_default_http"], [])
    check("2k DECLARED: excluded mutants are not in the evidence count",
          r1["evidence_mutants"] + r1["io_adapter_excluded"] <= r1["generated"], True)

    rr = measure(vr, wt, diff, ["call_model"])
    check("2l BOGUS declaration rejected, still LOW", (rr["verdict"],
          [x["name"] for x in rr["io_adapters"]["rejected"]]), ("low", ["call_model"]))
    check("2m rejected declaration is in the reason", "REJECTED" in rr["reason"], True)

    # REVERT: the evidence filter removed -> the declaration no longer helps
    src = (BIN / "verify-relevance.py").read_text()
    needle = "evidence = [m for m in mutants if m.literal_preserving and not m.io_adapter]"
    check("2n evidence filter present once", src.count(needle), 1)
    rev = Path(tmp) / "vr_reverted.py"
    rev.write_text(src.replace(needle, "evidence = [m for m in mutants if m.literal_preserving]"))
    vrr = load(rev, "vr_reverted")
    r2 = measure(vrr, wt, diff, ["_default_http"])
    check("2o REVERT: without the filter the declared run is LOW again", r2["verdict"], "low")

# ---- 3. auto: survivor-SET stall cap -------------------------------------
auto = load_script(BIN / "ollama-dispatch-auto", "oda_io")
blk = [{"check": "verify-relevance"}]


def sv(line, mut="return-none", orig="return resp.status, resp.read()", func="_default_http"):
    return {"file": "mod.py", "line": line, "mutation": mut, "original": orig, "func": func,
            "io_adapter_candidate": func}


rounds = [[sv(9), sv(11, "stmt-delete", "return e.code, e.read()")],
          [sv(15), sv(17, "stmt-delete", "return e.code, e.read()")],    # refine shifted lines
          [sv(21), sv(23, "stmt-delete", "return e.code,  e.read()")]]   # + whitespace
st, prev, tripped_at = 0, None, None
ost, oprev, old_trip = 0, None, None
with tempfile.TemporaryDirectory() as tmp:
    (Path(tmp) / "mod.py").write_text("x")
    for i, survs in enumerate(rounds, 1):
        st, prev = auto._refine_stall_update(prev, auto._survivor_set_signature("NO-GO", blk, survs), st)
        if st >= 2 and tripped_at is None:
            tripped_at = i
        # the OLD line-keyed guard on the same rounds (REVERT comparison)
        (Path(tmp) / "mod.py").write_text(f"x{i}")      # refine edited the target
        ost, oprev = auto._refine_stall_update(
            oprev, auto._round_signature(Path(tmp), "mod.py", "NO-GO", blk, survs), ost)
        if ost >= 2 and old_trip is None:
            old_trip = i
check("3a identical survivor set (lines shifted) trips at round 3", tripped_at, 3)
check("3b REVERT: the old line/target-keyed guard never trips on it", old_trip, None)
check("3c a changed mutation resets", auto._survivor_set_signature("NO-GO", blk, [sv(9)])
      != auto._survivor_set_signature("NO-GO", blk, [sv(9, "const-int")]), True)
check("3d no survivors -> no signature", auto._survivor_set_signature("NO-GO", blk, []), None)
msg = auto._survivor_stall_message(3, rounds[-1])
check("3e message carries the slicer's NO PROGRESS marker",
      msg.startswith("[auto] NO PROGRESS for 3 consecutive rounds"), True)
check("3f message names the adapter remedy", "## Real-I/O adapters" in msg, True)
asrc = (BIN / "ollama-dispatch-auto").read_text()
i_sig = asrc.find("_ssig = _survivor_set_signature(verdict, blockers, survs)")
# The cap test became `if rnd >= _max_rounds:` when refine_round_cap() started
# stretching the cap once survivors are known; accept either spelling.
i_cap = max(asrc.find("if rnd == a.max_rounds:"), asrc.find("if rnd >= _max_rounds:"))
check("3g loop computes the survivor-set stall BEFORE the round cap", 0 < i_sig < i_cap, True)
slc = load_script(BIN / "ollama-dispatch-slice", "ods_io")
with tempfile.TemporaryDirectory() as tmp:
    for f in ("verify.sh", "refimpl.py", "test_fixture.py"):
        (Path(tmp) / f).write_text("ok\n")
    (Path(tmp) / "TASK.md").write_text("# T\n## Must contain\n- `call_model`\n")
    authored = slc.harness_authored(tmp)
    got = slc.converged_harness_preflight_nogo(msg + "\n    verdict=NO-GO  blockers=['verify-relevance']",
                                               tmp)
    check("3h slicer escalates (keeps worktree, no re-author) on that output",
          bool(got) if authored else "harness_authored() false for stub tree", True)

# ---- 4. refine prompt -----------------------------------------------------
p = auto.refine_prompt("mod.py", rounds[0], [], "python")
check("4a refine prompt tells the model to declare the adapter",
      "## Real-I/O adapters\n- `_default_http`" in p, True)
p2 = auto.refine_prompt("mod.py", [dict(rounds[0][0], io_adapter_candidate=None)], [], "python")
check("4b no adapter paragraph without a candidate", "REAL-I/O ADAPTER" in p2, False)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
