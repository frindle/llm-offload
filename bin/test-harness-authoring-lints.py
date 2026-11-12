#!/usr/bin/env python3
"""Harness-authoring defect lints (2026-10-11): REFIMPL_SWALLOWS_EXCEPTION, JS_FIXTURE_LOAD_TRAP,
EVALUATE_NOT_SELF_CONTAINED, REAL_TIME_POLLING_FIXTURE (+ the makePage fake-DOM false-positive fix lives in
test-dispatch-env-fixture.py).
  test-harness-authoring-lints.py                 # run -> HARNESS_AUTHORING_LINTS_OK
  test-harness-authoring-lints.py --revert-check  # disable each rule; every one must go RED
"""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("HG_SRC") or HERE / "dispatch_harness_gates.py")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[:300]))
    if not ok:
        FAILS.append(name)


def codes(fs):
    return sorted({f["code"] for f in fs})


def main():
    spec = importlib.util.spec_from_file_location("hg_a", SRC)
    hg = importlib.util.module_from_spec(spec)
    sys.modules["hg_a"] = hg
    spec.loader.exec_module(hg)
    # ---- 1 refimpl swallow
    sw = hg.refimpl_swallow_findings
    BAD1 = "import sys\ntry:\n    out = target.session.get_all()\nexcept:\n    out = None\n    pass\n"
    BAD1 = "def run(target):\n    try:\n        return target.session.get_all()\n    except Exception:\n        pass\n"
    BAD2 = "def run(t):\n    try:\n        return t.compute(1)\n    except:\n        return []\n"
    BAD3 = "def run(t):\n    for x in t.items:\n        try:\n            t.handle(x)\n        except (NameError, AttributeError):\n            continue\n"
    check("swallow: `except Exception: pass` around attribute access -> finding", codes(sw(BAD1)) == [hg.REFIMPL_SWALLOWS_EXCEPTION], sw(BAD1))
    check("swallow: bare except returning [] -> finding", len(sw(BAD2)) == 1, sw(BAD2))
    check("swallow: (NameError, AttributeError) continue -> finding", len(sw(BAD3)) == 1, sw(BAD3))
    check("swallow near-miss: specific exception", sw("def f(t):\n    try:\n        return t.x.y()\n    except KeyError:\n        return None\n") == [])
    check("swallow near-miss: broad except that re-raises", sw("def f(t):\n    try:\n        return t.x.y()\n    except Exception as e:\n        raise RuntimeError('bad') from e\n") == [])
    check("swallow near-miss: broad except that exits loudly", sw("import sys\ndef f(t):\n    try:\n        return t.x.y()\n    except Exception:\n        print('boom')\n        sys.exit(1)\n") == [])
    check("swallow near-miss: pure file I/O cleanup", sw("import os\nfrom pathlib import Path\ndef f(p):\n    try:\n        Path(p).unlink()\n    except Exception:\n        pass\n") == [])
    check("swallow near-miss: records failure in a variable", sw("def f(t):\n    err = None\n    try:\n        t.go()\n    except Exception as e:\n        err = e\n") == [])
    check("swallow: unparsable refimpl -> no crash", sw("def (:\n") == [])
    # ---- 2 JS fixture load traps
    jt = hg.js_fixture_trap_findings
    T1 = "const { installFakeBrowser } = require('./dispatch-env.ts');\nimport { test, mock } from 'node:test';\nmock.module('./lib.js', { exports: {} });\n"
    T2 = "import { test } from 'node:test';\nglobalThis.mock.module('x', { exports: {} });\n"
    T3 = "import assert from 'node:assert';\ntest('a', () => { mock.fn(); });\n"
    GOOD = ("import { test, mock } from 'node:test';\nimport Module from 'node:module';\nconst o = Module._load;\n"
            "mock.module('./lib.js', { exports: {} });\ntest('a', async () => { const t = await import('./x.js'); });\n")
    check("trap: require('.ts') + mock.module -> finding", any("require()" in f["message"] for f in jt("v.test.ts", T1)), jt("v.test.ts", T1))
    check("trap: globalThis.mock -> finding", any("globalThis.mock" in f["message"] for f in jt("v.test.ts", T2)), jt("v.test.ts", T2))
    check("trap: test/mock used but never imported -> findings", len(jt("v.test.ts", T3)) == 2, jt("v.test.ts", T3))
    check("trap near-miss: well-formed recipe fixture", jt("v.test.ts", GOOD) == [], jt("v.test.ts", GOOD))
    check("trap near-miss: require('.ts') WITHOUT mock.module (plain ESM-under-tsx load)", jt("v.test.ts", "import { test } from 'node:test';\nconst e = require('./dispatch-env.ts');\ntest('a', () => {});\n") == [])
    check("trap near-miss: t.mock.module (context mock) is not the bare global", jt("v.test.ts", "import { test } from 'node:test';\ntest('a', async (t) => { t.mock.module('x', { exports: {} }); });\n") == [])
    check("trap near-miss: comment mentioning globalThis.mock / require('x.ts')", jt("v.test.ts", "import { test } from 'node:test';\n// globalThis.mock.module is wrong; require('./a.ts') too\ntest('a', () => {});\n") == [])
    check("trap near-miss: vitest globals imported", jt("v.test.ts", "import { test, vi } from 'vitest';\ntest('a', () => {});\n") == [])
    check("trap near-miss: `test` bound via const destructure", jt("v.test.js", "const { test, mock } = require('node:test');\ntest('a', () => { mock.fn(); });\n") == [])
    # ---- 3 evaluate self-containment
    ev = hg.evaluate_closure_findings
    E1 = ("function extractCard(html) { return html.length; }\n"
          "function extractDetailInBrowser() { const h = document.body.innerHTML; return extractCard(h); }\n"
          "async function f(page) { return page.evaluate(extractDetailInBrowser); }\n")
    E2 = ("const sleep = ms => new Promise(r => setTimeout(r, ms));\n"
          "async function f(page) { return page.evaluate(async () => { await sleep(10); return 1; }); }\n")
    E3 = ("function norm(s) { return s.trim(); }\n"
          "async function f(page) { return page.waitForFunction(() => norm(document.title) === 'x'); }\n")
    OK1 = ("function extractCard(html) { return html.length; }\n"
           "function extractDetailInBrowser() { function extractCard2(h) { return h.length; } return extractCard2(document.body.innerHTML); }\n"
           "async function f(page) { const d = await page.evaluate(extractDetailInBrowser); return extractCard(d); }\n")
    OK2 = ("function norm(s) { return s.trim(); }\n"
           "async function f(page) { const t = await page.evaluate(() => document.title); return norm(t); }\n")
    OK3 = ("function norm(s) { return s.trim(); }\n"
           "async function f(page) { const n = await page.evaluate((norm) => norm('x'), 1); const sleep = 1; return page.evaluate(() => document.title).then(() => norm(n)); }\n")
    check("evaluate: named fn calling module helper -> finding", codes(ev("t.js", E1)) == [hg.EVALUATE_NOT_SELF_CONTAINED] and "extractCard(" in ev("t.js", E1)[0]["message"], ev("t.js", E1))
    check("evaluate: inline async callback calling module helper -> finding", len(ev("t.js", E2)) == 1, ev("t.js", E2))
    check("evaluate: waitForFunction callback -> finding", len(ev("t.js", E3)) == 1, ev("t.js", E3))
    check("evaluate near-miss: helper re-declared INSIDE the serialized fn", ev("t.js", OK1) == [], ev("t.js", OK1))
    check("evaluate near-miss: helper called OUTSIDE the evaluate callback (expression arrow)", ev("t.js", OK2) == [], ev("t.js", OK2))
    check("evaluate near-miss: helper passed as a parameter name shadows the module one", ev("t.js", OK3) == [], ev("t.js", OK3))
    # ---- 4 real-time polling
    rt = hg.real_time_polling_findings
    POLLJS = "async function poll(page) { for (let i = 0; i < 16; i++) { if (await ok(page)) return true; await page.waitForTimeout(500); } }\n"
    FXREAL = "import { test } from 'node:test';\ntest('a', async () => { await poll(page); });\n"
    FXVIRT = "import { test } from 'node:test';\nconst real = globalThis.setTimeout;\nglobalThis.setTimeout = (f, ms, ...a) => real(f, Math.ceil(ms / 50), ...a);\ntest('a', async () => {});\n"
    check("polling: polling target + fixture without virtual timers -> finding", codes(rt("verify.test.ts", FXREAL, POLLJS)) == [hg.REAL_TIME_POLLING_FIXTURE], rt("verify.test.ts", FXREAL, POLLJS))
    check("polling: fixture really sleeping 2s -> finding", len(rt("verify.test.ts", "test('a', async () => { await new Promise(r => setTimeout(r, 2000)); });\n", "")) == 1)
    check("polling: python time.sleep(2) in the fixture -> finding", len(rt("test_fixture.py", "import time\ndef test_a():\n    time.sleep(2)\n", "")) == 1)
    check("polling: python polling target, unpatched fixture -> finding",
          len(rt("test_fixture.py", "def test_a():\n    assert poll() == 1\n", "def poll():\n    for i in range(16):\n        if ok():\n            return 1\n        time.sleep(0.5)\n")) == 1)
    check("polling near-miss: compressed timers installed", rt("verify.test.ts", FXVIRT, POLLJS) == [])
    check("polling near-miss: no polling in the code under test", rt("verify.test.ts", FXREAL, "export function add(a, b) { return a + b; }\n") == [])
    check("polling near-miss: a 10 ms sleep in the fixture", rt("verify.test.ts", "test('a', async () => { await new Promise(r => setTimeout(r, 10)); });\n", "") == [])
    check("polling near-miss: python fixture patches time.sleep", rt("test_fixture.py", "from unittest import mock\ndef test_a():\n    with mock.patch('time.sleep'):\n        poll()\n", "def poll():\n    while True:\n        time.sleep(1)\n") == [])
    check("polling near-miss: single sleep, no loop, fixture unpatched", rt("verify.test.ts", FXREAL, "async function f(){ await page.waitForTimeout(500); }\n") == [])
    # ---- wiring
    hlspec = importlib.util.spec_from_file_location("hl_a", HERE / "harness_lint.py")
    hl = importlib.util.module_from_spec(hlspec)
    hlspec.loader.exec_module(hl)
    with tempfile.TemporaryDirectory() as d:
        wt = Path(d)
        (wt / "refimpl.py").write_text(BAD1)
        (wt / "verify.test.ts").write_text(T3)
        (wt / "TASK.md").write_text("# T\n## Required change\nx\n## Must contain\n- `a.py`: `def poll`\n")
        got = codes(hg.harness_authoring_findings(wt, E1))
        check("harness_authoring_findings aggregates all lints", {hg.REFIMPL_SWALLOWS_EXCEPTION, hg.JS_FIXTURE_LOAD_TRAP, hg.EVALUATE_NOT_SELF_CONTAINED} <= set(got), got)
        check("spec_defects (preflight admission) carries them",
              any(f["code"] == hg.REFIMPL_SWALLOWS_EXCEPTION for f in hg.spec_defects(wt, target="a.py", creation=False, task_text=(wt / "TASK.md").read_text())))
        check("harness_lint.run_lint(quick) reports them (stage entry)",
              any(f["code"] == hg.JS_FIXTURE_LOAD_TRAP for f in hl.run_lint(wt, quick=True, target="a.py")))
        (wt / "refimpl.py").write_text("def run(t):\n    try:\n        return t.x.y()\n    except KeyError:\n        return None\n")
        (wt / "verify.test.ts").write_text(GOOD)
        check("healthy worktree -> no authoring finding", hg.harness_authoring_findings(wt, "") == [])
    au = (HERE / "ollama-dispatch-auto").read_text()
    check("authoring prompt carries the JS recipes (import node:test, Module._load, self-contained evaluate, compressed timers)",
          all(s in au for s in ("JS_FIXTURE_RECIPES_RULE", "Module as any)._load", "SELF-CONTAINED", "Math.ceil(ms / 50)"))
          and "import_rule += MOCK_MODULE_RULE + JS_FIXTURE_RECIPES_RULE" in au)
    check("refimpl authoring section carries the no-swallow hygiene rule", "what it deleted, even though its own verify may go green.\n{REFIMPL_HYGIENE_RULE}" in au)
    print("HARNESS_AUTHORING_LINTS_OK" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTATIONS = [
    ("swallow lint emits nothing", "            if h.lineno in seen:\n                continue\n", "            continue\n"),
    ("swallow ignores bare/broad names", "            if not (names & ({\"<bare>\"} | _BROAD_EXC)) or not _handler_swallows(h):\n                continue\n", "            continue\n"),
    ("globalThis.mock not flagged", "    if re.search(r\"\\b(?:globalThis|global)\\s*\\.\\s*mock\\b\", s):", "    if False:"),
    ("require(.ts)+mock.module not flagged", "    if has_mm and re.search(", "    if False and re.search("),
    ("unimported test/mock not flagged", "        if use_re.search(s) and not _binds(s, ident) and", "        if False and not _binds(s, ident) and"),
    ("evaluate helper check inert", "            if re.search(r\"(?<![\\w$.])\" + re.escape(h) + r\"\\s*\\(\", body) and (label, h) not in seen:", "            if False:"),
    ("evaluate local shadowing ignored", "        for h in sorted(helpers - local - _JS_SKIP", "        for h in sorted(helpers - _JS_SKIP"),
    ("real-sleep branch inert", "            if ms >= 1000 and not re.search(", "            if False and not re.search("),
    ("polling + no-virtual branch inert", "    if msg is None and poll and not virt:", "    if False:"),
    ("not wired into harness_authoring_findings (evaluate)", "        out += evaluate_closure_findings(\"target\", target_text)", "        pass"),
    ("not wired into spec_defects", "    out += harness_authoring_findings(wt)\n", ""),
]


def revert_check():
    bad, src = 0, SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name} ({src.count(old)})"
        with tempfile.NamedTemporaryFile("w", suffix="-hg.py", delete=False, dir=str(HERE)) as fh:
            fh.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "HG_SRC": fh.name}, capture_output=True, text=True, timeout=300)
        os.unlink(fh.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
