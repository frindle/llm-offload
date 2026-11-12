#!/usr/bin/env python3
"""test-pipeline-e2e.py -- hermetic END-TO-END smoke of the dispatch chain, per language.

WHY THIS EXISTS (2026-09-27)
----------------------------
Every stage of the dispatch pipeline had per-language unit tests, and none of
them exercised the CHAIN. The first TS auto run anyone made died at scaffold
rc=2 (ollama-dispatch-auto never passed --symbol), and fixing that exposed the
next break, and the next. A stage that is "supported" in isolation but has
never had its output fed to the next stage is not supported. This runs the REAL
tools, in order, on a tiny temp repo per language:

  auto (scaffold -> author via --drafter-cmd -> self-check -> preflight GO ->
        pause for review, fixture marked DRAFT_UNCONFIRMED)
  -> draft --confirm (marker cleared on the fixture verify.sh actually runs)
  -> preflight (both-ways + relevance) GO
  -> seal the harness + worker loop against a STUB /api/chat (no GPU, no queue)
  -> verify green on the worker's edit
  -> gate-on-complete (GATE_TEST_MODE: decidable gate only, no review enqueue)
  -> integrate (--dry-run: the product diff is exactly the target)

SANDBOXED: a temp HOME (so every Path.home() write -- worker logs,
dispatch-metrics.jsonl, queue state, preflight ledger, vault token lookup --
lands in the temp dir, never the real ~/bin or the vault), a copy of this bin/
as $HOME/bin, a temp git identity. Nothing is enqueued, no model is called.

  test-pipeline-e2e.py                 # every case
  test-pipeline-e2e.py ts-tsx bash     # just these
  test-pipeline-e2e.py --bin DIR ...   # run the chain from another bin/ (revert proofs)
  test-pipeline-e2e.py --keep          # keep the temp dir for inspection
"""
from __future__ import annotations

import argparse
import http.server
import json
import os
import pwd
import re
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
REAL_HOME = Path(pwd.getpwuid(os.getuid()).pw_dir)

# ---------------------------------------------------------------------------
# the per-language cases
# ---------------------------------------------------------------------------
# Every case is the same tiny task -- clamp a number into [lo, hi] -- so the
# only thing that varies is the LANGUAGE and the harness flavour.
PY_CASES = """\
    ("below lo clamps up", lambda: target.clamp(-5, 0, 3), 0),
    ("above hi clamps down", lambda: target.clamp(9, 0, 3), 3),
    ("inside is unchanged", lambda: target.clamp(2, 0, 3), 2),
    ("exactly hi stays hi", lambda: target.clamp(3, 0, 3), 3),
    ("exactly lo stays lo", lambda: target.clamp(0, 0, 3), 0),
"""

TS_CHK = """\
chk('below lo clamps up', fn(-5, 0, 3) === 0);
chk('above hi clamps down', fn(9, 0, 3) === 3);
chk('inside is unchanged', fn(2, 0, 3) === 2);
chk('exactly hi stays hi', fn(3, 0, 3) === 3);
chk('exactly lo stays lo', fn(0, 0, 3) === 0);
"""

NODE_TEST_BODY = """\
import {{ test }} from 'node:test';
import assert from 'node:assert/strict';
import {{ clamp }} from '{imp}';

test('below lo clamps up', () => {{ assert.equal(clamp(-5, 0, 3), 0); }});
test('above hi clamps down', () => {{ assert.equal(clamp(9, 0, 3), 3); }});
test('inside is unchanged', () => {{ assert.equal(clamp(2, 0, 3), 2); }});
test('exactly hi stays hi', () => {{ assert.equal(clamp(3, 0, 3), 3); }});
test('exactly lo stays lo', () => {{ assert.equal(clamp(0, 0, 3), 0); }});
"""

SWIFT_CASES = """\
    Case(desc: "below lo clamps up") { clamp(-5, 0, 3) == 0 },
    Case(desc: "above hi clamps down") { clamp(9, 0, 3) == 3 },
    Case(desc: "inside is unchanged") { clamp(2, 0, 3) == 2 },
    Case(desc: "exactly hi stays hi") { clamp(3, 0, 3) == 3 },
    Case(desc: "exactly lo stays lo") { clamp(0, 0, 3) == 0 },
"""

CS_CASES = """\
            chk("below lo clamps up", Calc.Clamp(-5, 0, 3), 0);
            chk("above hi clamps down", Calc.Clamp(9, 0, 3), 3);
            chk("inside is unchanged", Calc.Clamp(2, 0, 3), 2);
            chk("exactly hi stays hi", Calc.Clamp(3, 0, 3), 3);
            chk("exactly lo stays lo", Calc.Clamp(0, 0, 3), 0);
            chk("nonzero lo: below clamps to lo, not to 0", Calc.Clamp(1, 2, 5), 2);
"""

BASH_CASES = """\
chk "below lo clamps up"   "$(run_target -5 0 3)" "0"
chk "above hi clamps down" "$(run_target 9 0 3)"  "3"
chk "inside is unchanged"  "$(run_target 2 0 3)"  "2"
chk "exactly hi stays hi"  "$(run_target 3 0 3)"  "3"
chk "no args -> exit 2"    "$(run_target_rc)"     "2"
"""

TSCONFIG_ALIAS = json.dumps({"compilerOptions": {
    "target": "es2022", "module": "esnext", "moduleResolution": "bundler",
    "strict": True, "noEmit": True, "allowImportingTsExtensions": True,
    "skipLibCheck": True, "paths": {"@/*": ["./*"]}},
    "include": ["lib/**/*.ts", "app/**/*.ts"]}, indent=2) + "\n"
TSCONFIG_PLAIN = json.dumps({"compilerOptions": {
    "target": "es2022", "module": "esnext", "moduleResolution": "bundler",
    "strict": True, "noEmit": True, "allowImportingTsExtensions": True,
    "skipLibCheck": True}, "include": ["lib/**/*.ts"]}, indent=2) + "\n"

TS_FIX = "  if (x < lo) return lo;\n  if (x > hi) return hi;\n  return x;\n"
TS_FIX_Z = TS_FIX.replace("return x;", "return x + ZERO;")
TS_LIT = "if (x > hi) return hi;"
# slice 1 of the chain (lower bound only); slice 2 lands the upper bound on top
TS_MID = "  if (x < lo) return lo;\n  return x;\n"
TS_MID_Z = TS_MID.replace("return x;", "return x + ZERO;")
TS_LIT_LO = "if (x < lo) return lo;"

TS_TARGET = ("import { ZERO } from '@/lib/consts';\n\n"
             "export function clamp(x: number, lo: number, hi: number): number {\n"
             "  return x + ZERO;\n}\n")

CASES = {
    "python": dict(
        lang="python", target="app/calc.py",
        files={"app/calc.py": "def clamp(x, lo, hi):\n    return x\n"},
        old="    return x\n",
        new="    if x < lo:\n        return lo\n    if x > hi:\n        return hi\n    return x\n",
        literal="if x > hi:", relevance="relevant",
        mid="    if x < lo:\n        return lo\n    return x\n", lit_lo="if x < lo:"),
    # TS, tsx symbol runner (ev-dashboard shape: no `test` script), `@/` alias
    # import inside the target.
    "ts-tsx": dict(
        lang="ts", target="lib/calc.ts", node=True,
        intent_extra=" -- export function clamp(x: number, lo: number, hi: number): number",
        files={"lib/calc.ts": TS_TARGET, "lib/consts.ts": "export const ZERO = 0;\n",
               "tsconfig.json": TSCONFIG_ALIAS,
               "package.json": '{"name":"e2e-tsx","private":true,"type":"module"}\n'},
        old="  return x + ZERO;\n", new=TS_FIX_Z, literal=TS_LIT, relevance="relevant",
        mid=TS_MID_Z, lit_lo=TS_LIT_LO),
    # TS, node --test runner (resell-tracker shape: `test` script runs node
    # --experimental-strip-types --test), no path aliases, relative .ts imports.
    "ts-node": dict(
        lang="ts", target="lib/calc.ts", node=True, test_imp="./lib/calc.ts",
        files={"lib/calc.ts": ("export function clamp(x: number, lo: number, hi: number): number {\n"
                               "  return x;\n}\n"),
               "tsconfig.json": TSCONFIG_PLAIN,
               "package.json": json.dumps({"name": "e2e-node", "private": True, "type": "module",
                                           "scripts": {"test": "node --experimental-strip-types --test lib/*.test.ts"}}) + "\n"},
        old="  return x;\n", new=TS_FIX, literal=TS_LIT, relevance="relevant",
        mid=TS_MID, lit_lo=TS_LIT_LO),
    # TS, node --test runner WITH `@/` aliases and a Prisma-generated client
    # (resell-tracker proper): the generated client dir is gitignored, so the
    # worktree never has it and verify.sh must `prisma generate` it.
    "ts-node-prisma": dict(
        lang="ts", target="lib/calc.ts", node=True, prisma=True, test_imp="@/lib/calc",
        files={"lib/calc.ts": ("import { ZERO } from '@/app/generated/prisma/client';\n\n"
                               "export function clamp(x: number, lo: number, hi: number): number {\n"
                               "  return x + ZERO;\n}\n"),
               "prisma/schema.prisma": ('generator client {\n  provider = "prisma-client"\n'
                                        '  output   = "../app/generated/prisma"\n}\n\n'
                                        'datasource db {\n  provider = "sqlite"\n}\n'),
               ".gitignore": "/node_modules\n/app/generated/prisma\n",
               "tsconfig.json": TSCONFIG_ALIAS,
               "package.json": json.dumps({"name": "e2e-prisma", "private": True, "type": "module",
                                           "scripts": {"test": "node --experimental-strip-types --test lib/*.test.ts"}}) + "\n"},
        old="  return x + ZERO;\n", new=TS_FIX_Z, literal=TS_LIT, relevance="relevant",
        mid=TS_MID_Z, lit_lo=TS_LIT_LO),
    # TS expression kind: the property lives in an inline expression, no symbol.
    "ts-expr": dict(
        lang="ts", target="lib/view.ts", node=True, kind="expression", anchor="__clamp__",
        # the extractor evaluates the CONTENTS of the {...} around the anchor:
        # the JSX `{expr}` / template `${expr}` shape, not an object literal
        files={"lib/view.ts": ("export const view = (x: number, lo: number, hi: number) =>\n"
                               "  `value=${ /* __clamp__ */ x }`;\n"),
               "tsconfig.json": TSCONFIG_PLAIN,
               "package.json": '{"name":"e2e-expr","private":true,"type":"module"}\n'},
        old="/* __clamp__ */ x }", new="/* __clamp__ */ (x < lo ? lo\n    : x > hi ? hi\n    : x) }",
        literal="x > hi ? hi",
        mid="/* __clamp__ */ (x < lo ? lo\n    : x) }", lit_lo="x < lo ? lo"),
    # CommonJS JS in a SUB-PACKAGE (resell-tracker sidecar/ shape).
    "cjs": dict(
        lang="js", target="sidecar/src/calc.js", node=True, sub_pkg="sidecar",
        intent_extra=" -- export function clamp(x, lo, hi) (module.exports.clamp)",
        files={"sidecar/src/calc.js": ("'use strict';\n\nfunction clamp(x, lo, hi) {\n"
                                       "  return x;\n}\n\nmodule.exports = { clamp };\n"),
               "sidecar/package.json": json.dumps({"name": "e2e-sidecar", "private": True,
                                                   "type": "commonjs",
                                                   "scripts": {"test": "node test.js"}}) + "\n",
               "sidecar/test.js": "console.log('sidecar ok');\n",
               "package.json": '{"name":"e2e-cjs-root","private":true}\n',
               "tsconfig.json": TSCONFIG_PLAIN},
        old="  return x;\n", new=TS_FIX, literal=TS_LIT, relevance="relevant",
        mid=TS_MID, lit_lo=TS_LIT_LO),
    # Swift: a SwiftPM LIBRARY module (CLT toolchain, no XCTest).
    "swift": dict(
        lang="swift", target="Sources/Calc/Calc.swift", toolchain="swift",
        files={"Package.swift": ('// swift-tools-version: 5.9\nimport PackageDescription\n\n'
                                 'let package = Package(\n    name: "Calc",\n'
                                 '    platforms: [.macOS(.v13)],\n'
                                 '    targets: [\n        .target(name: "Calc"),\n    ]\n)\n'),
               "Sources/Calc/Calc.swift": ("public func clamp(_ x: Int, _ lo: Int, _ hi: Int) -> Int {\n"
                                           "    return x\n}\n"),
               ".gitignore": ".build/\n"},
        old="    return x\n",
        new="    if x < lo { return lo }\n    if x > hi { return hi }\n    return x\n",
        literal="if x > hi { return hi }",
        mid="    if x < lo { return lo }\n    return x\n", lit_lo="if x < lo { return lo }"),
    "bash": dict(
        lang="bash", target="scripts/clamp.sh",
        files={"scripts/clamp.sh": ('#!/usr/bin/env bash\n[ $# -eq 3 ] || { echo "usage: clamp X LO HI" >&2; exit 2; }\n'
                                    'x=$1; lo=$2; hi=$3\n'
                                    'echo "$x"\n')},
        old='echo "$x"\n',
        new='if [ "$x" -lt "$lo" ]; then x=$lo; elif [ "$x" -gt "$hi" ]; then x=$hi; fi; echo "$x"\n',
        literal='elif [ "$x" -gt "$hi" ]',
        mid='if [ "$x" -lt "$lo" ]; then x=$lo; fi; echo "$x"\n', lit_lo='if [ "$x" -lt "$lo" ]'),
    # clamshell's shape: the code lives in an EXECUTABLE target, which a SwiftPM
    # test/exe target cannot import -- refused up front, not a build loop.
    "swift-exe": dict(
        lang="swift", target="Sources/Calc/Calc.swift", toolchain="swift", refuse=True,
        files={"Package.swift": ('// swift-tools-version: 5.9\nimport PackageDescription\n\n'
                                 'let package = Package(\n    name: "Calc",\n'
                                 '    targets: [\n        .executableTarget(name: "Calc"),\n    ]\n)\n'),
               "Sources/Calc/Calc.swift": ("func clamp(_ x: Int, _ lo: Int, _ hi: Int) -> Int {\n"
                                           "    return x\n}\nprint(clamp(1, 0, 3))\n"),
               ".gitignore": ".build/\n"}),
    # C#/.NET (since 2026-09-27): a plain net8.0 library file; the scaffold's
    # DispatchTests/ console project compiles it directly (clamshell PeerTests'
    # shape) with the user-local SDK at ~/.dotnet -- not on PATH, found anyway.
    "csharp": dict(
        lang="csharp", target="src/Calc.cs", toolchain="dotnet",
        files={"src/Calc.cs": "namespace CalcLib;\n\npublic static class Calc\n{\n    public static int Clamp(int x, int lo, int hi)\n    {\n        return x;\n    }\n}\n",
               "Calc.csproj": ("<Project Sdk=\"Microsoft.NET.Sdk\">\n  <PropertyGroup>\n"
                               "    <TargetFramework>net8.0</TargetFramework>\n  </PropertyGroup>\n"
                               "</Project>\n"),
               ".gitignore": "bin/\nobj/\n"},
        old="        return x;\n",
        new="        if (x < lo) return lo;\n        if (x > hi) return hi;\n        return x;\n",
        literal="if (x > hi) return hi;",
        mid="        if (x < lo) return lo;\n        return x;\n", lit_lo="if (x < lo) return lo;"),
    # A C# file that calls Windows-only APIs (clamshell's WinForms tray / user32
    # hooks) cannot compile or run on the Mac: REFUSED up front, from the file.
    "csharp-win": dict(
        lang="csharp", target="src/Tray.cs", toolchain="dotnet", refuse=True,
        files={"src/Tray.cs": ("using System.Windows.Forms;\n\nnamespace App;\n\n"
                               "public static class Tray\n{\n    public static int Clamp(int x, int lo, int hi) => x;\n}\n"),
               "App.csproj": ("<Project Sdk=\"Microsoft.NET.Sdk\">\n  <PropertyGroup>\n"
                              "    <TargetFramework>net8.0-windows</TargetFramework>\n"
                              "    <UseWindowsForms>true</UseWindowsForms>\n  </PropertyGroup>\n</Project>\n")}),
    # F#/VB have no harness: refused up front AND at enqueue for a hand-built one.
    "fsharp": dict(
        lang="fsharp", target="src/Calc.fs", refuse=True, enqueue_refuse=True,
        files={"src/Calc.fs": "module Calc\nlet clamp x lo hi = x\n",
               "Calc.fsproj": "<Project Sdk=\"Microsoft.NET.Sdk\"></Project>\n"}),
}

INTENT = "clamp(x, lo, hi) must bound x into [lo, hi]"


# ---------------------------------------------------------------------------
# the DRAFTER (stands in for the authoring model; run by --drafter-cmd in wt)
# ---------------------------------------------------------------------------
def _fill_task(wt: Path, c: dict):
    t = (wt / "TASK.md").read_text()
    blocks = t.split("\n\n")
    out = []
    for b in blocks:
        if "TODO" not in b:
            out.append(b)
            continue
        if b.startswith("TODO -- state the CONFIRMED"):
            out.append("clamp returns x unchanged; observed: clamp(9, 0, 3) returned 9.")
        elif b.startswith("TODO -- ") and ":<line>" in b:
            out.append(f"{c['target']}:1")
        elif "Behaviour that must NOT change" in b:
            out.append("Behaviour that must NOT change:\n- a value already inside [lo, hi] "
                       "is returned unchanged.")
        elif b.startswith("- `TODO"):
            out.append(f"- `{c['literal']}`")
        elif b.startswith("TODO -- state the property"):
            out.append(INTENT)
        else:
            out.append(re.sub(r"TODO[^\n]*", "(n/a)", b))
    (wt / "TASK.md").write_text("\n\n".join(out))


def _refimpl(wt: Path, c: dict):
    (wt / "refimpl.py").write_text(
        "#!/usr/bin/env python3\nimport pathlib, sys\n"
        "wt = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.')\n"
        f"p = wt / {c['target']!r}\nt = p.read_text()\n"
        f"OLD = {c['old']!r}\nNEW = {c['new']!r}\n"
        "assert OLD in t, 'refimpl anchor not found'\n"
        "p.write_text(t.replace(OLD, NEW, 1))\nprint('refimpl applied')\n")


def _harness_fixture(wt: Path):
    """The fixture file(s) the scaffold emitted, read from verify.sh / the
    harness manifest -- the file verify.sh actually RUNS, never a guess."""
    man = json.loads((wt / ".dispatch-harness.json").read_text())
    return [f for f in man.get("authored", [])
            if f not in ("TASK.md", "verify.sh", "check_literals.py", "refimpl.py",
                         "Package.swift")
            and not f.endswith((".csproj", ".props", ".targets", ".gitignore"))]


SLICES = ("s1-lower", "s2-upper")


def slice_view(c: dict, sid: str) -> dict:
    """The case as ONE slice of the two-slice chain: s1 lands the lower bound
    (old -> mid), s2 the upper bound on top of it (mid -> new)."""
    c = dict(c)
    if sid == "s1-lower":
        c.update(new=c["mid"], literal=c["lit_lo"])
    else:
        c.update(old=c["mid"])
    return c


def _cases_for(block: str, sid) -> str:
    """s1's fixture cannot assert the upper bound (its refimpl does not land
    it, so both-ways would fail); s2 asserts everything, incl. s1's property."""
    if sid != "s1-lower":
        return block
    return "".join(l for l in block.splitlines(True)
                   if "above hi" not in l and "exactly hi" not in l)


def draft(case: str):
    c = CASES[case]
    wt = Path.cwd()
    # a slicer-chain slice worktree is wt-slice-<label>-<sid>
    sid = next((x for x in SLICES if wt.name.endswith("-" + x)), None)
    if sid:
        c = slice_view(c, sid)
    _fill_task(wt, c)
    _refimpl(wt, c)
    fx = _harness_fixture(wt)
    assert fx, f"no fixture in the harness manifest: {fx}"
    for rel in fx:
        p = wt / rel
        s = p.read_text()
        if rel.endswith(".py"):
            s = s.replace("CASES = [\n", "CASES = [\n" + _cases_for(PY_CASES, sid), 1)
        elif rel.endswith("verify_impl.mts"):
            s = s.replace("const CASES_AUTHORED = false;", _cases_for(TS_CHK, sid) + "const CASES_AUTHORED = true;", 1)
        elif rel.endswith("verify_impl.mjs"):   # expression kind
            s = s.replace("const PARAMS = [/* e.g. 'v', 'canEditLimit' */];",
                          "const PARAMS = ['x', 'lo', 'hi'];")
            s = s.replace("const CASES_AUTHORED = false;",
                          _cases_for(TS_CHK, sid).replace("fn(", "evalExpr(") + "const CASES_AUTHORED = true;", 1)
        elif rel.endswith(".test.ts") or rel.endswith(".test.mts"):
            s = _cases_for(NODE_TEST_BODY, sid).format(imp=c.get("test_imp", "./" + c["target"]))
        elif rel.endswith(".swift"):
            s = s.replace("let cases: [Case] = [\n", "let cases: [Case] = [\n" + _cases_for(SWIFT_CASES, sid), 1)
        elif rel.endswith(".cs"):
            s = s.replace("            bool CASES_AUTHORED = false;",
                          _cases_for(CS_CASES, sid) + "            bool CASES_AUTHORED = true;", 1)
        elif rel.endswith(".sh"):
            s = s.replace("CASES_AUTHORED=0", _cases_for(BASH_CASES, sid) + "CASES_AUTHORED=1", 1)
        else:
            raise SystemExit(f"drafter: no idiom for fixture {rel}")
        p.write_text(s)
    print(f"drafter[{case}]: filled TASK.md, refimpl.py, {fx}")


# ---------------------------------------------------------------------------
# stub /api/chat (the worker's model) -- applies the fix, then task_complete
# ---------------------------------------------------------------------------
class _Stub(http.server.BaseHTTPRequestHandler):
    fix = None      # (path, content)
    calls = 0

    def log_message(self, *a):
        pass

    def _send(self, obj):
        b = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_GET(self):
        m = {"name": "stub:latest", "model": "stub:latest", "size": 1, "size_vram": 1,
             "digest": "0", "details": {"parameter_size": "1B", "quantization_level": "Q4"}}
        self._send({"models": [m], "version": "0.0.0"})

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        if not self.path.startswith("/api/chat"):
            return self._send({"status": "success"})
        _Stub.calls += 1
        msgs = body.get("messages") or []
        if not body.get("tools"):
            return self._send({"message": {"role": "assistant", "content": "ok"}, "done": True})
        wrote = any(tc.get("function", {}).get("name") == "write_file"
                    for m in msgs if m.get("role") == "assistant"
                    for tc in (m.get("tool_calls") or []))
        if not wrote:
            path, content = _Stub.fix
            tc = {"function": {"name": "write_file", "arguments": {"path": path, "content": content}}}
        else:
            tc = {"function": {"name": "task_complete",
                               "arguments": {"summary": "clamped x into [lo, hi]"}}}
        self._send({"message": {"role": "assistant", "content": "", "tool_calls": [tc]},
                    "done": True, "prompt_eval_count": 10, "eval_count": 10})


def start_stub(fix):
    _Stub.fix = fix
    _Stub.calls = 0
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Stub)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


# ---------------------------------------------------------------------------
# sandbox + runner
# ---------------------------------------------------------------------------
class Env:
    def __init__(self, root: Path, src_bin: Path):
        self.root = root
        self.home = root / "home"
        self.bin = self.home / "bin"
        self.home.mkdir(parents=True)
        shutil.copytree(src_bin, self.bin, symlinks=True,
                        ignore=shutil.ignore_patterns("__pycache__", "node_modules",
                                                      "*.log", "ollama-queue-logs",
                                                      "ollama-worker-logs", "*.jsonl",
                                                      "ollama-queue-state.json*"))
        # the TS sidecars' deps (typescript/tsx) are gitignored in the repo; borrow
        # the installed copy read-only so the chain runs the same as from ~/bin.
        nm = None
        for cand in (src_bin / "ts-mutator" / "node_modules",
                     REAL_HOME / "bin" / "ts-mutator" / "node_modules"):
            if (cand / "typescript").is_dir():
                nm = cand
                break
        self.ts_nm = nm
        if nm:
            (self.bin / "ts-mutator" / "node_modules").symlink_to(nm)
        gc = self.home / ".gitconfig"
        gc.write_text("[user]\n\tname = e2e\n\temail = e2e@example.invalid\n"
                      "[init]\n\tdefaultBranch = main\n[advice]\n\tdetachedHead = false\n")
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("OLLAMA_", "GATE_", "DISPATCH_", "SLICE_"))}
        self.env.update({
            "HOME": str(self.home), "GIT_CONFIG_GLOBAL": str(gc),
            "GIT_CONFIG_NOSYSTEM": "1",
            "OLLAMA_DISPATCH_HOME": str(self.home / ".ollama-dispatch"),
            "OLLAMA_DISPATCH_AUTO_RUNS_DIR": str(self.home / ".ollama-dispatch" / "auto-runs"),
            "OLLAMA_PREFLIGHT_LEDGER": str(self.home / "ledger"),
            "OLLAMA_QUEUE_STATE": str(self.bin / "ollama-queue-state.json"),
            "GATE_TEST_MODE": "1", "GATE_BIN": str(self.bin),
            "npm_config_offline": "true", "NO_UPDATE_NOTIFIER": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })

    def run(self, cmd, cwd=None, timeout=900, extra=None):
        env = dict(self.env, **(extra or {}))
        p = subprocess.run(cmd, cwd=cwd, env=env, capture_output=True, text=True,
                           timeout=timeout)
        return p.returncode, p.stdout, p.stderr


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True,
                          env={**os.environ, "GIT_CONFIG_NOSYSTEM": "1",
                               "GIT_AUTHOR_NAME": "e2e", "GIT_AUTHOR_EMAIL": "e@x",
                               "GIT_COMMITTER_NAME": "e2e", "GIT_COMMITTER_EMAIL": "e@x"})


def make_repo(E: Env, name: str, c: dict) -> Path:
    r = E.root / f"repo-{name}"
    for rel, body in c["files"].items():
        p = r / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        if rel.endswith(".sh"):
            p.chmod(0o755)
    git(r, "init", "-q", "-b", "main")
    git(r, "add", "-A")
    git(r, "commit", "-qm", "init")
    if c.get("node"):
        nm = E.root / f"nm-{name}"
        (nm / ".bin").mkdir(parents=True)
        for pkg in ("typescript", "tsx", "esbuild", "@esbuild", "get-tsconfig",
                    "resolve-pkg-maps"):
            if E.ts_nm and (E.ts_nm / pkg).exists():
                (nm / pkg).symlink_to(E.ts_nm / pkg)
        for b in ("tsx", "tsc"):
            if E.ts_nm and (E.ts_nm / ".bin" / b).exists():
                (nm / ".bin" / b).symlink_to((E.ts_nm / ".bin" / b).resolve())
        if c.get("prisma"):
            # a FAKE prisma CLI: `prisma generate` writes the client the schema's
            # generator names. Real behaviour, no engines/network.
            fake = nm / ".bin" / "prisma"
            fake.write_text("#!/usr/bin/env bash\n[ \"$1\" = generate ] || exit 0\n"
                            "mkdir -p app/generated/prisma\n"
                            "printf 'export const ZERO = 0;\\n' > app/generated/prisma/client.ts\n"
                            "echo 'generated prisma client'\n")
            fake.chmod(0o755)
        (r / "node_modules").symlink_to(nm)
        if c.get("sub_pkg"):
            (r / c["sub_pkg"] / "node_modules").mkdir(parents=True, exist_ok=True)
        if c.get("prisma"):
            # the SOURCE checkout has its generated client (gitignored)
            (r / "app/generated/prisma").mkdir(parents=True, exist_ok=True)
            (r / "app/generated/prisma/client.ts").write_text("export const ZERO = 0;\n")
    return r


class Result:
    def __init__(self, case):
        self.case = case
        self.stages = []    # (stage, ok, note)

    def add(self, stage, ok, note=""):
        self.stages.append((stage, ok, note))
        print(f"    [{'ok  ' if ok else 'FAIL'}] {stage}" + (f" -- {note}" if note else ""),
              flush=True)
        return ok

    @property
    def ok(self):
        return all(s[1] for s in self.stages)


def tail(s, n=1500):
    s = (s or "").strip()
    return s[-n:]


def _dotnet_found():
    try:
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import dispatch_langs
        return bool(dispatch_langs.dotnet_path())
    except Exception:
        return False


def run_case(E: Env, name: str, keep_going=False) -> Result:
    c = CASES[name]
    R = Result(name)
    print(f"\n=== {name} (lang={c['lang']}) ===", flush=True)
    if c.get("toolchain") and not shutil.which(c["toolchain"]) \
            and not Path(f"/usr/bin/{c['toolchain']}").exists() \
            and not (c["toolchain"] == "dotnet" and _dotnet_found()):
        R.add("toolchain", True, f"SKIP: {c['toolchain']} not installed on this host")
        return R
    repo = make_repo(E, name, c)
    wt = E.root / f"wt-{name}"
    label = f"e2e-{name}"
    auto = [sys.executable, str(E.bin / "ollama-dispatch-auto"), "--repo", str(repo),
            "--target", c["target"], "--lang", c["lang"],
            "--intent", INTENT + c.get("intent_extra", ""), "--label", label,
            "--dest", str(wt), "--no-auto-slice", "--max-rounds", "2",
            "--model", "stub:latest", "--host", "studio", "--num-ctx", "65536",
            "--relevance-budget-s", "240",
            "--drafter-cmd", f"{shlex_q(sys.executable)} {shlex_q(str(E.bin / 'test-pipeline-e2e.py'))} --draft {name}"]
    if c.get("kind"):
        auto += ["--kind", c["kind"]]
    if c.get("anchor"):
        auto += ["--anchor", c["anchor"]]
    rc, out, err = E.run(auto, timeout=1800)
    log = out + err
    (E.root / f"{name}.auto.log").write_text(log)

    if c.get("refuse"):
        refused = rc != 0 and not wt.exists() and re.search(
            r"not supported|unsupported|refus", log, re.I) is not None
        R.add("auto: refused up front (no worktree, clear message)", refused,
              tail(log, 300).replace("\n", " | "))
        # the SCAFFOLD itself refuses too (the door a human uses directly)
        rc, out, err = E.run([sys.executable, str(E.bin / "ollama-dispatch-scaffold"),
                              "--repo", str(repo), "--target", c["target"], "--lang", c["lang"],
                              "--property", INTENT, "--label", label + "-sc",
                              "--dest", str(E.root / f"wt-{name}-sc")])
        R.add("scaffold: refused up front (exit 2, no worktree)",
              rc == 2 and not (E.root / f"wt-{name}-sc").exists() and "REFUSING" in out + err,
              f"rc={rc} " + tail(out + err, 200).replace("\n", " | "))
        if not c.get("enqueue_refuse"):
            return R
        # ...and a HAND-ASSEMBLED dispatch is refused at ENQUEUE (the queue is
        # sandboxed under the temp HOME: a regression enqueues into a throwaway
        # state file, never the live queue)
        hand = E.root / f"hand-{name}"
        hand.mkdir()
        (hand / "TASK.md").write_text("# TASK\nfix clamp\n")
        (hand / ".dispatch-harness.json").write_text(json.dumps({"target": c["target"]}))
        (hand / "verify.sh").write_text("#!/bin/bash\n# DISPATCH_FIXTURE_CMD: dotnet test\ndotnet test\n")
        state = E.bin / "ollama-queue-state.json"
        before = state.read_bytes() if state.exists() else b""
        rc, out, err = E.run([sys.executable, str(E.bin / "ollama-queue.py"), "enqueue",
                              "--task-file", str(hand / "TASK.md"), "--cwd", str(hand), "--label", label + "-q",
                              "--verify", "bash verify.sh", "--model", "stub:latest"])
        after = state.read_bytes() if state.exists() else b""
        R.add(f"queue enqueue: hand-assembled {c['lang']} dispatch refused, nothing queued",
              rc != 0 and "REFUSING enqueue" in out + err and before == after,
              f"rc={rc} " + tail(out + err, 300).replace("\n", " | "))
        return R

    # a failed auto leaves nothing downstream to test: stop this case even
    # under --keep-going (--keep-going continues to the NEXT case)
    if not R.add("auto: scaffold -> author -> preflight GO -> pause", rc == 0 and "GO reached" in log,
                 f"rc={rc}; " + tail(log, 1200).replace("\n", " | ")):
        return R
    fx = _harness_fixture(wt) if (wt / ".dispatch-harness.json").is_file() else []
    runs_fx = [f for f in fx if (wt / f).is_file()]
    # the fixture verify.sh runs carries the marker (native flavours: a comment
    # in the fixture + the machine-readable marker in TASK.md, which is all the
    # gate can read for a .swift/.sh file)
    task_marked = "DRAFT_UNCONFIRMED = True" in (wt / "TASK.md").read_text()
    marked = [f for f in runs_fx if "DRAFT_UNCONFIRMED" in (wt / f).read_text()
              or ("Model-drafted; NOT yet read by a human." in (wt / f).read_text()
                  and task_marked)]
    R.add("auto: the fixture verify.sh RUNS is the one marked unconfirmed",
          bool(runs_fx) and marked == runs_fx, f"fixture={runs_fx} marked={marked}")
    # ...and the PROPERTY the marker exists for: the gate refuses until a human
    # confirms (draft-confirmed FAIL on the auto output as it stands).
    rc, out, err = E.run([sys.executable, str(E.bin / "ollama-dispatch-preflight"), str(wt),
                          "--refimpl-cmd", "python3 refimpl.py", "--no-relevance", "--auto-seal",
                          "--json"], timeout=900)
    try:
        pre = json.loads(out[out.index("{"):])
    except Exception:
        pre = {}
    dc = [x for x in pre.get("checks") or [] if x.get("check") == "draft-confirmed"]
    R.add("gate refuses the unconfirmed draft (draft-confirmed FAIL)",
          pre.get("verdict") != "GO" and bool(dc) and dc[0].get("status") == "FAIL",
          f"verdict={pre.get('verdict')} draft-confirmed={dc[0].get('status') if dc else None}")
    # the harness must still be green after marking (a marker that breaks the
    # fixture's syntax fails the gate on syntax, not relevance)
    rc, out, err = E.run(["python3", "refimpl.py"], cwd=wt)
    vrc, vout, verr = E.run(["bash", "verify.sh"], cwd=wt)
    git(wt, "checkout", "--", c["target"])
    R.add("marked fixture still runs green under refimpl", vrc == 0 and "VERIFY_OK" in vout,
          tail(vout + verr, 600).replace("\n", " | "))

    # --- draft --confirm ------------------------------------------------------
    rc, out, err = E.run([sys.executable, str(E.bin / "ollama-dispatch-draft"), str(wt), "--confirm"])
    still = [f for f in runs_fx if "DRAFT_UNCONFIRMED" in (wt / f).read_text()]
    R.add("draft --confirm clears the marker", rc == 0 and not still,
          f"rc={rc} still-marked={still} " + tail(out + err, 400).replace("\n", " | "))

    # --- preflight (the gate the human re-runs before enqueue) ----------------
    rc, out, err = E.run([sys.executable, str(E.bin / "ollama-dispatch-preflight"), str(wt),
                          "--refimpl-cmd", "python3 refimpl.py", "--require", c["literal"],
                          "--auto-seal", "--relevance-budget-s", "240", "--json"], timeout=1200)
    try:
        pf = json.loads(out[out.index("{"):])
    except Exception:
        pf = {}
    (E.root / f"{name}.preflight.json").write_text(out + err)
    bl = [b.get("check") for b in pf.get("blockers") or []]
    if not R.add("preflight GO (both-ways + relevance)", pf.get("verdict") == "GO",
                 f"verdict={pf.get('verdict')} blockers={bl} "
                 f"relevance={(pf.get('verify_relevance') or {}).get('verdict')}") and not keep_going:
        return R
    st = git(wt, "status", "--porcelain").stdout.strip()
    # measured the way the QUEUE measures a launch baseline (its own function)
    rc, out, err = E.run([sys.executable, "-c",
        "import importlib.util,json,sys;s=importlib.util.spec_from_file_location('q',sys.argv[1]);"
        "q=importlib.util.module_from_spec(s);s.loader.exec_module(q);"
        "print(json.dumps(q.measure_baseline(sys.argv[2])))",
        str(E.bin / "ollama-queue.py"), str(wt)])
    try:
        lb = json.loads(out.strip().splitlines()[-1])
    except Exception:
        lb = None
    R.add("harness sealed (queue launch baseline dirty == 0)", bool(lb) and lb.get("dirty") == 0,
          f"{lb} {st.replace(chr(10), ' | ')} {tail(err, 200)}")
    want_rel = c.get("relevance")
    got_rel = (pf.get("verify_relevance") or {}).get("verdict")
    if want_rel:
        R.add(f"relevance mutators engaged (verdict {want_rel})", got_rel == want_rel,
              f"got {got_rel}: {(pf.get('verify_relevance') or {}).get('reason')}")
    base = git(wt, "rev-parse", "HEAD").stdout.strip()

    # --- worker loop against the stub model -----------------------------------
    orig = (wt / c["target"]).read_text()
    srv, url = start_stub((c["target"], orig.replace(c["old"], c["new"], 1)))
    try:
        task = (wt / "TASK.md").read_text()
        rc, out, err = E.run([sys.executable, str(E.bin / "ollama-worker.py"), "--direct-ok",
                              "--host", url, "--model", "stub:latest", "--cwd", str(wt),
                              "--task", task, "--verify", "bash verify.sh",
                              "--verify-failed-at-baseline", "--max-iters", "6",
                              "--task-kind", "coding", "--num-ctx", "32768"], timeout=900)
    finally:
        srv.shutdown()
    (E.root / f"{name}.worker.log").write_text(out + err)
    fixed = c["new"] in (wt / c["target"]).read_text()
    R.add("worker (stub model) converges, verify green", rc == 0 and fixed,
          f"rc={rc} chat_calls={_Stub.calls} target_fixed={fixed} " + tail(out + err, 500).replace("\n", " | "))
    vrc, vout, verr = E.run(["bash", "verify.sh"], cwd=wt)
    R.add("verify.sh green on the worker's edit", vrc == 0 and "VERIFY_OK" in vout,
          tail(vout + verr, 500).replace("\n", " | "))

    # --- gate-on-complete (decidable half; GATE_TEST_MODE) ---------------------
    gdir = E.root / f"gate-{name}"
    gdir.mkdir()
    rc, out, err = E.run([sys.executable, str(E.bin / "gate-on-complete.py"), "--job-id",
                          f"e2e{name}", "--job-label", label, "--cwd", str(wt),
                          "--task-file", str(wt / "TASK.md"), "--verify", "bash verify.sh",
                          "--verify-exit", "0", "--out-dir", str(gdir)], timeout=900)
    gj = sorted(gdir.glob("*.gate.json"))
    g = json.loads(gj[0].read_text()) if gj else {}
    (E.root / f"{name}.gate.log").write_text(out + err)
    gv = g.get("verdict")
    iss = [f"{i.get('check') or i.get('source')}:{(i.get('what') or i.get('msg') or '')[:80]}"
           for i in (g.get("issues") or [])]
    R.add("gate-on-complete: decidable gate passes", gv in ("pass", "pass-with-concerns",
                                                            "pass-pending-review")
          and not [i for i in (g.get("issues") or []) if i.get("severity") in ("high", "code_high")],
          f"verdict={gv} issues={iss[:4]}")

    # --- integrate (dry run): the product diff is exactly the target ------------
    git(wt, "add", "-A", "--", c["target"])
    git(wt, "commit", "-qm", "worker fix")
    rc, out, err = E.run([sys.executable, str(E.bin / "ollama-dispatch-integrate"), str(wt),
                          "--dry-run"], timeout=600)
    (E.root / f"{name}.integrate.log").write_text(out + err)
    R.add("integrate --dry-run: product diff == target only", rc == 0,
          f"rc={rc} " + tail(out + err, 500).replace("\n", " | "))
    if c.get("mid"):
        run_chain(E, name, c, R)
    return R


# ---------------------------------------------------------------------------
# the SLICER CHAIN: two dependent slices through the real slicer
# ---------------------------------------------------------------------------
# What is REAL: ollama-dispatch-slice --execute (chain worktree, per-slice AUTO
# authoring -> auto-confirm -> mechanical preflight gate -> queue enqueue ->
# harvest/land onto the chain branch -> integration staging at convergence),
# the worker, gate-on-complete, and the queue's own completion hook
# (_fire_gate_on_complete / _persist_job_completion).
# What STANDS IN: (1) the drafter, via $OLLAMA_DISPATCH_DRAFTER_CMD, because the
# slicer launches AUTO itself; (2) the queue DAEMON's launch + reap -- the harness
# runs the worker for the enqueued job and then marks its row done exactly as the
# reap does, never running a daemon (a sandbox daemon would probe real hosts);
# (3) the gate's review-model wait: GATE_TEST_MODE yields pass-pending-review,
# which the slicer lands only after GATE_VERDICT_WAIT_S on the LIVE verify.sh
# re-run, so the done sidecar is aged past that wait.
_REAP = r"""
import importlib.util, json, sys
s = importlib.util.spec_from_file_location('q', sys.argv[1])
q = importlib.util.module_from_spec(s); s.loader.exec_module(q)
jid, rc = sys.argv[2], int(sys.argv[3])
with q._Locked() as lock:
    st = lock.load()
    job = next(j for j in st['jobs'] if j.get('id') == jid)
    job.update(status='done' if rc == 0 else 'failed', exit_code=rc, pid=None, lane=None)
    lock.save(st)
q._fire_gate_on_complete(job)       # persists <id>.done.json, then Popens the gate
print(json.dumps({'label': job.get('label'), 'cwd': job.get('cwd')}))
"""


def _slice_state(E: Env, label: str) -> dict:
    p = E.home / ".ollama-dispatch" / "slice-runs" / f"{label}.json"
    return json.loads(p.read_text()) if p.is_file() else {}


def run_chain(E: Env, name: str, c: dict, R: Result):
    repo = make_repo(E, name + "-chain", c)
    label = f"e2e-{name}-chain"
    extra = c.get("intent_extra", "")
    plan = {"repo": str(repo), "target": c["target"], "lang": c["lang"], "label": label,
            "slices": [
                {"id": "s1-lower", "title": "lower bound",
                 "intent": "clamp(x, lo, hi) must return lo when x < lo" + extra,
                 "depends_on": [], "must_contain": [c["lit_lo"]]},
                {"id": "s2-upper", "title": "upper bound",
                 "intent": "clamp(x, lo, hi) must return hi when x > hi" + extra,
                 "depends_on": ["s1-lower"], "must_contain": [c["literal"]]}]}
    for k in ("kind", "anchor"):
        if c.get(k):
            plan[k] = c[k]
    pp = E.root / f"{label}.plan.json"
    pp.write_text(json.dumps(plan, indent=1))
    drafter = {"OLLAMA_DISPATCH_DRAFTER_CMD":
               f"{shlex_q(sys.executable)} {shlex_q(str(E.bin / 'test-pipeline-e2e.py'))} --draft {name}"}
    slicer = [sys.executable, str(E.bin / "ollama-dispatch-slice"), str(pp), "--execute",
              "--model", "stub:latest", "--host", "studio", "--num-ctx", "32768"]
    logs = E.root / f"{name}.chain.log"
    for sid in SLICES:
        rc, out, err = E.run(slicer, timeout=1800, extra=drafter)
        with logs.open("a") as f:
            f.write(out + err)
        s = (_slice_state(E, label).get("slices") or {}).get(sid) or {}
        if not R.add(f"chain {sid}: authored -> mechanical gate GO -> enqueued",
                     s.get("status") == "enqueued" and s.get("job_id"),
                     f"status={s.get('status')} "
                     f"{(s.get('escalation_reason') or s.get('failure_reason') or s.get('last_auto_error') or '')[:300]} "
                     + tail(out + err, 600).replace("\n", " | ")):
            return
        wt = Path(s["worktree"])
        orig = (wt / c["target"]).read_text()
        v = slice_view(c, sid)
        srv, url = start_stub((c["target"], orig.replace(v["old"], v["new"], 1)))
        try:
            wrc, wout, werr = E.run([sys.executable, str(E.bin / "ollama-worker.py"), "--direct-ok",
                                     "--host", url, "--model", "stub:latest", "--cwd", str(wt),
                                     "--task", (wt / "TASK.md").read_text(),
                                     "--verify", "bash verify.sh", "--verify-failed-at-baseline",
                                     "--max-iters", "6", "--task-kind", "coding",
                                     "--num-ctx", "32768"], timeout=900)
        finally:
            srv.shutdown()
        with logs.open("a") as f:
            f.write(wout + werr)
        if not R.add(f"chain {sid}: worker converges in the slice worktree", wrc == 0,
                     tail(wout + werr, 400).replace("\n", " | ")):
            return
        rc, out, err = E.run([sys.executable, "-c", _REAP, str(E.bin / "ollama-queue.py"),
                              s["job_id"], str(wrc)])
        gj = E.home / "bin" / "ollama-queue-logs" / f"{s['job_id']}.gate.json"
        dj = E.home / "bin" / "ollama-queue-logs" / f"{s['job_id']}.done.json"
        g = {}
        for _ in range(240):                 # the hook Popens the gate: wait for it
            try:
                g = json.loads(gj.read_text())
                if g.get("slice_autofeed"):
                    break
            except (OSError, ValueError):
                pass
            time.sleep(1)
        # the gate's re-arm decision: `advance` on a terminal pass; in
        # GATE_TEST_MODE the review is never enqueued, so the verdict stays
        # pass-pending-review and the correct decision is `skip` (the review
        # merge fires the advance in production). Anything else is a defect.
        af = (g.get("slice_autofeed") or {}).get("action")
        R.add(f"chain {sid}: completion hook -> done.json + gate -> slice-autofeed decision",
              dj.is_file() and (af == "advance" or (af == "skip" and
                                                    g.get("verdict") == "pass-pending-review")),
              f"verdict={g.get('verdict')} autofeed={g.get('slice_autofeed')} "
              f"done.json={dj.is_file()} {tail(out + err, 200)}")
        old_t = time.time() - 3 * 3600      # past GATE_VERDICT_WAIT_S (see header)
        if dj.is_file():
            os.utime(dj, (old_t, old_t))
    # the advance the gate would fire (--advance-detached == one --execute)
    rc, out, err = E.run(slicer, timeout=1800, extra=drafter)
    with logs.open("a") as f:
        f.write(out + err)
    st = _slice_state(E, label)
    stats = {k: v.get("status") for k, v in (st.get("slices") or {}).items()}
    R.add("chain: both slices landed (done) on the chain branch",
          stats == {"s1-lower": "done", "s2-upper": "done"}, f"{stats} " + tail(out + err, 500).replace("\n", " | "))
    cwt = Path(st.get("chain_worktree") or "/nonexistent")
    if cwt.is_dir():
        final = (cwt / c["target"]).read_text()
        changed = git(cwt, "diff", "--name-only", "main", "HEAD").stdout.split()
        n = git(cwt, "rev-list", "--count", "main..HEAD").stdout.strip()
        R.add("chain: tip carries BOTH properties, only the target committed",
              c["new"] in final and changed == [c["target"]],
              f"changed={changed} commits={n}")
    integ = st.get("integration") or {}
    ib = integ.get("integrate_branch")
    diff = git(repo, "diff", "--name-only", f"main..{ib}").stdout.split() if ib else []
    staged = (git(repo, "show", f"{ib}:{c['target']}").stdout if ib else "")
    R.add("chain: integration staged at convergence (code-only, target only, both properties)",
          str(integ.get("status")) == "staged" and diff == [c["target"]] and c["new"] in staged,
          f"integration={ {k: integ.get(k) for k in ('status', 'integrate_branch', 'verdict')} } diff={diff}")


def shlex_q(s):
    import shlex
    return shlex.quote(s)


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--draft":
        return draft(sys.argv[2])
    ap = argparse.ArgumentParser()
    ap.add_argument("cases", nargs="*")
    ap.add_argument("--bin", default=str(HERE))
    ap.add_argument("--keep", action="store_true")
    ap.add_argument("--keep-going", action="store_true")
    a = ap.parse_args()
    names = a.cases or list(CASES)
    bad = [n for n in names if n not in CASES]
    if bad:
        ap.error(f"unknown case(s) {bad}; known: {list(CASES)}")
    root = Path(tempfile.mkdtemp(prefix="pipeline-e2e-"))
    E = Env(root, Path(a.bin).resolve())
    # the drafter must be THIS harness even when --bin points elsewhere
    shutil.copy2(__file__, E.bin / "test-pipeline-e2e.py")
    print(f"sandbox: {root}  (bin under test: {a.bin})")
    results = [run_case(E, n, a.keep_going) for n in names]
    print("\n=== summary ===")
    for r in results:
        bad = [s[0] for s in r.stages if not s[1]]
        print(f"  {'PASS' if r.ok else 'FAIL'}  {r.case:<16}" + (f"  failed: {bad}" if bad else ""))
    if not a.keep and all(r.ok for r in results):
        shutil.rmtree(root, ignore_errors=True)
    else:
        print(f"(sandbox kept: {root})")
    ok = all(r.ok for r in results)
    print("PIPELINE_E2E_OK" if ok else "PIPELINE_E2E_FAIL")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
