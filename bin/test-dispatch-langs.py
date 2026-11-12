#!/usr/bin/env python3
"""test-dispatch-langs.py -- the up-front language refusal (dispatch_langs.py).

Pure checks, no worktree, no queue. The end-to-end proof that every entry point
honours these answers is test-pipeline-e2e.py (csharp / swift-exe cases); this
pins the decision table itself so a regression names the rule it broke.
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dispatch_langs as dl  # noqa: E402

fails = 0


def check(name, cond, note=""):
    global fails
    print(f"  {'ok  ' if cond else 'FAIL'} {name}" + (f" -- {note}" if note and not cond else ""))
    if not cond:
        fails += 1


# C# is a SUPPORTED flavour since 2026-09-27 (.NET 8 SDK at ~/.dotnet)
_have_dotnet = bool(dl.dotnet_path())
check("dotnet resolves (PATH, then ~/.dotnet/dotnet)", _have_dotnet, "no dotnet on this host")
if _have_dotnet:
    check("C# by --lang is supported", dl.support_error("csharp", "src/Calc.cs") is None,
          dl.support_error("csharp", "src/Calc.cs"))
    check("--lang cs / dotnet are aliases of csharp",
          dl.canon("cs") == "csharp" and dl.canon("dotnet") == "csharp"
          and dl.support_error("dotnet", "src/Calc.cs") is None)
    env = dl.dotnet_env(dl.dotnet_path())
    check("dotnet env: DOTNET_ROOT + telemetry opt-out + no node reuse",
          env.get("DOTNET_ROOT") and env.get("DOTNET_CLI_TELEMETRY_OPTOUT") == "1"
          and env.get("MSBUILDDISABLENODEREUSE") == "1", env)
e = dl.support_error("python", "Foo.cs")
check("a .cs target under the wrong --lang names --lang csharp", bool(e) and "--lang csharp" in e, e)
e = dl.support_error("fsharp", "src/A.fs")
check("F# is refused, with the reason", bool(e) and "F#/VB.NET" in e, e)
e = dl.support_error("python", "Form1.vb")
check("a .vb target is refused whatever --lang says", bool(e) and "F#/VB.NET" in e, e)
with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / "src").mkdir()
    (d / "src" / "Tray.cs").write_text("using System.Windows.Forms;\nclass T { }\n")
    (d / "src" / "Hook.cs").write_text('class H { [DllImport("user32.dll")] static extern int X(); }\n')
    (d / "src" / "Peer.cs").write_text("namespace P; public static class Peer { }\n")
    (d / "App.csproj").write_text("<Project><PropertyGroup><TargetFramework>net8.0-windows"
                                  "</TargetFramework><UseWindowsForms>true</UseWindowsForms>"
                                  "</PropertyGroup></Project>")
    if _have_dotnet:
        e = dl.support_error("csharp", "src/Tray.cs", repo=d)
        check("a WinForms C# file is refused up front", bool(e) and "Windows-only" in e, e)
        e = dl.support_error("csharp", "src/Hook.cs", repo=d)
        check("a user32 P/Invoke C# file is refused up front", bool(e) and "Windows-only" in e, e)
        check("a platform-neutral file in a net8.0-windows project is allowed "
              "(decided by the FILE, not the TFM -- clamshell's shape)",
              dl.support_error("csharp", "src/Peer.cs", repo=d) is None)
        (d / ".dispatch-harness.json").write_text(json.dumps({"target": "src/Tray.cs"}))
        e = dl.verify_toolchain_error("bash verify.sh", d)
        check("enqueue: a hand-assembled Windows-API C# dispatch is refused",
              bool(e) and "Windows-only" in e, e)
        (d / ".dispatch-harness.json").write_text(json.dumps({"target": "src/Peer.cs"}))
        check("enqueue: a platform-neutral C# dispatch is allowed",
              dl.verify_toolchain_error("bash verify.sh", d) is None)
e = dl.support_error("python", "update.sh")
check("lang/target mismatch refused, naming the right --lang",
      bool(e) and "--lang bash" in e, e)
check("python on a .py target is fine", dl.support_error("python", "app/x.py") is None)
check("ts on a .tsx target is fine", dl.support_error("ts", "app/page.tsx") is None)
check("js on a CommonJS .js target is fine", dl.support_error("js", "sidecar/src/a.js") is None)
check("bash on an extensionless script is fine", dl.support_error("bash", "bin/tool") is None)
check("an unknown flavour is refused", bool(dl.support_error("cobol", "x.cbl")))
check("a diagnosis is language-agnostic (never refused for its language)",
      dl.support_error("fsharp", "src/A.fs", task="diagnosis") is None)
check("unset target: only the language is checked", dl.support_error("python", None) is None)

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / ".dispatch-harness.json").write_text(json.dumps({"target": "src/Lib.fs"}))
    e = dl.verify_toolchain_error("bash verify.sh", d)
    check("enqueue: a scaffolded F# target is refused", bool(e) and "F#/VB.NET" in e, e)
    (d / ".dispatch-harness.json").write_text(json.dumps({"target": "app/x.py"}))
    (d / "verify.sh").write_text("#!/bin/bash\n# DISPATCH_FIXTURE_CMD: no-such-dotnet-xyz test\n")
    check("enqueue: verify.sh's recorded fixture command is read",
          dl.verify_toolchain_error("bash verify.sh", d) is None)
    (d / "verify.sh").write_text("#!/bin/bash\n# DISPATCH_FIXTURE_CMD: no-such-tool-xyz run\n")
    check("enqueue: an unknown tool is undecidable -> allowed, never refused blind",
          dl.verify_toolchain_error("bash verify.sh", d) is None)
    for v in ("source .venv/bin/activate && pytest -q", ".venv/bin/pytest -q", "uv run pytest"):
        check(f"enqueue: shell/venv verify allowed: {v}", dl.verify_toolchain_error(v, d) is None)
    (d / "verify.sh").write_text("#!/bin/bash\necho ok\n")
    check("enqueue: a plain bash verify is never refused blind",
          dl.verify_toolchain_error("bash verify.sh", d) is None)
    check("enqueue: an undecidable verify (./script) is allowed",
          dl.verify_toolchain_error("./run-tests.sh", d) is None)
check("enqueue: no cwd, a present tool -> allowed",
      dl.verify_toolchain_error("python3 -m pytest", None) is None)

print(f"--- {fails} failed ---")
print("DISPATCH_LANGS_OK" if not fails else "DISPATCH_LANGS_FAIL")
sys.exit(1 if fails else 0)
