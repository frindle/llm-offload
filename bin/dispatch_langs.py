"""dispatch_langs -- the ONE record of which languages the local dispatch pipeline
can carry end to end, and the up-front refusal for the ones it cannot.

WHY (2026-09-27): support was declared per stage and never across the chain, so
an unsupported or mismatched language did not fail at the door -- it failed
mid-chain, after a worktree, an authoring job, or a GPU slot had been spent:
  * `--lang python --target Foo.cs` built a Python harness around a C# file and
    looped on "target does not parse";
  * a C#/.NET task has no toolchain at all on the dispatch hosts (no `dotnet`);
  * a SwiftPM target inside an EXECUTABLE target cannot be imported by the
    scaffold's executable test target, so `swift run <M>Tests` died every run.
Every entry point (scaffold, ollama-dispatch-auto, ollama-queue.py enqueue)
asks this module first and refuses with the reason, before anything is created.

Proven end to end by bin/test-pipeline-e2e.py (one case per language).
"""
from __future__ import annotations

import json
import os
import pwd
import re
import shlex
import shutil
from pathlib import Path

# canonical --lang -> target suffixes that flavour's harness can drive
LANG_EXTS = {
    "python": (".py",),
    "typescript": (".ts", ".tsx", ".mts", ".cts", ".js", ".jsx", ".mjs", ".cjs"),
    "bash": (".sh", ".bash", ""),        # scripts are often extensionless
    "swift": (".swift",),
    "go": (".go",),
    "rust": (".rs",),
    "java": (".java",),
    "kotlin": (".kt",),
    "csharp": (".cs",),
}

LANG_ALIASES = {"ts": "typescript", "js": "typescript", "javascript": "typescript",
                "sh": "bash", "shell": "bash", "golang": "go", "rs": "rust",
                "kt": "kotlin", "py": "python",
                "cs": "csharp", "c#": "csharp", "dotnet": "csharp", ".net": "csharp",
                "f#": "fsharp", "vb": "fsharp"}

# Languages the owner's repos use that the pipeline deliberately does NOT carry, and
# why. Refused up front (scaffold/auto/enqueue) -- never a mid-chain crash.
# C# LEFT this table on 2026-09-27: the .NET 8 SDK is installed user-local at
# ~/.dotnet and the csharp flavour (a plain net8.0 console test project that
# compiles the target file directly, clamshell PeerTests' shape) is proven end to
# end. What stays refused is the part that genuinely cannot run on a Mac: code
# that calls Windows-only APIs (see csharp_windows_refusal) and non-C# .NET
# sources the harness does not model.
UNSUPPORTED = {
    "fsharp": ("F#/VB.NET", (".fs", ".fsproj", ".vb", ".vbproj", ".xaml"),
               "the dispatch C# flavour compiles C# sources only; F#/VB/XAML have "
               "no harness. Hand this change to a Claude agent."),
}
_UNSUPPORTED_EXT = {e: k for k, (_, exts, _) in UNSUPPORTED.items() for e in exts}

# toolchain each flavour's verify.sh actually invokes. Absolute paths first:
# the queue daemon runs under launchd's PATH.
TOOLCHAINS = {
    "swift": ("/usr/bin/swift", "swift"),
    "go": ("/opt/homebrew/bin/go", "/usr/local/go/bin/go", "go"),
    "rust": ("/opt/homebrew/bin/cargo", "cargo"),
    "java": ("/usr/bin/javac", "javac"),
    "kotlin": ("/opt/homebrew/bin/kotlinc", "kotlinc"),
    "typescript": ("/opt/homebrew/bin/node", "/usr/local/bin/node", "node"),
    # PATH first, then the user-local SDK (dotnet-install.sh, no sudo). The
    # REAL home, not $HOME: the e2e/sandboxed runs point HOME at a temp dir.
    "csharp": ("dotnet", "/usr/local/share/dotnet/dotnet", "/opt/homebrew/bin/dotnet",
               "__REAL_HOME__/.dotnet/dotnet"),
}


def real_home() -> str:
    try:
        return pwd.getpwuid(os.getuid()).pw_dir
    except (KeyError, OSError):
        return os.path.expanduser("~")


def dotnet_path() -> str | None:
    """Absolute path of a WORKING dotnet: PATH, then the standard installs, then
    the user-local ~/.dotnet/dotnet. None when there is none."""
    return _which(TOOLCHAINS["csharp"])


def dotnet_env(dotnet: str) -> dict:
    """The env every dotnet invocation in the pipeline runs under. The queue
    daemon runs under launchd's bare PATH/env, so verify.sh sets these itself:
    DOTNET_ROOT (a user-local SDK is not found without it), no telemetry, no
    first-run banner, and no MSBuild node reuse / shared compiler server -- a
    lingering VBCSCompiler/MSBuild node would outlive the job and hold the
    worktree open."""
    return {"DOTNET_ROOT": str(Path(dotnet).resolve().parent),
            "DOTNET_CLI_TELEMETRY_OPTOUT": "1", "DOTNET_NOLOGO": "1",
            "DOTNET_SKIP_FIRST_TIME_EXPERIENCE": "1",
            "MSBUILDDISABLENODEREUSE": "1", "DOTNET_CLI_USE_MSBUILD_SERVER": "0",
            "NUGET_PACKAGES": os.path.join(real_home(), ".nuget", "packages")}


# Windows-only surface a Mac cannot compile or run. Matched in the TARGET FILE:
# the csharp harness compiles that file for plain net8.0 (clamshell's PeerTests
# pattern), so what decides runnability is what the file itself calls -- a
# platform-neutral file inside a net8.0-windows project is fine (PeerProtocol.cs
# is exactly that), a file using WinForms or user32 P/Invoke is not.
_WIN_API_RE = re.compile(
    r"^\s*using\s+(?:static\s+)?(?:System\.Windows(?:\.Forms)?|System\.Drawing|"
    r"Microsoft\.Win32|Windows\.(?:Graphics|Media|UI|Win32|Devices|Storage)|"
    r"System\.Management|Vortice|SharpDX)\b"
    r"|\[(?:DllImport|LibraryImport)\s*\(\s*\"(?:user32|kernel32|gdi32|dxgi|d3d11|"
    r"shell32|ole32|advapi32|winmm|dwmapi|shcore|mfplat|ntdll)(?:\.dll)?\""
    r"|\bnew\s+(?:Form|NotifyIcon|ContextMenuStrip)\s*\("
    r"|\bRegistry\.(?:CurrentUser|LocalMachine|ClassesRoot)\b",
    re.M)
_WIN_PROJECT_RE = re.compile(
    r"<TargetFrameworks?>[^<]*-windows|<Use(?:WindowsForms|WPF)>\s*true", re.I)


def owning_csproj(repo, target):
    """The nearest *.csproj at or above the target's directory, or None."""
    try:
        d = (Path(repo) / target).parent
        root = Path(repo).resolve()
        while True:
            hits = sorted(d.glob("*.csproj"))
            if hits:
                return hits[0]
            if d.resolve() == root or d.parent == d:
                return None
            d = d.parent
    except OSError:
        return None


def csharp_windows_refusal(repo, target) -> str | None:
    """Why a C# target cannot be dispatched on this Mac, or None."""
    if not repo or not target:
        return None
    p = Path(repo) / target
    try:
        text = p.read_text(errors="replace") if p.is_file() else ""
    except OSError:
        text = ""
    m = _WIN_API_RE.search(text)
    if m:
        proj = owning_csproj(repo, target)
        where = f" (project {proj.name})" if proj else ""
        return (f"{target}{where} uses a Windows-only API (`{m.group(0).strip()[:60]}`): "
                f"the C# harness compiles and runs the target on the Mac for plain "
                f"net8.0, where WinForms/WPF, System.Drawing, the registry and Win32 "
                f"P/Invoke (user32/kernel32/...) do not exist. Move the logic under test "
                f"into a platform-neutral file (as clamshell's PeerTests does for the "
                f"peer protocol) and dispatch against that, or hand this change to a "
                f"Claude agent / build it on the Windows box.")
    return None


def canon(lang: str | None) -> str:
    l = (lang or "python").strip().lower()
    return LANG_ALIASES.get(l, l)


def _which(cands) -> str | None:
    for c in cands:
        c = c.replace("__REAL_HOME__", real_home())
        if os.path.isabs(c):
            if os.access(c, os.X_OK):
                return c
        else:
            path = os.environ.get("PATH", "") + os.pathsep + "/opt/homebrew/bin:/usr/local/bin"
            hit = shutil.which(c, path=path)
            if hit:
                return hit
    return None


def toolchain_missing(lang: str) -> str | None:
    """The missing tool's name, or None when the flavour's toolchain is present."""
    lang = canon(lang)
    cands = TOOLCHAINS.get(lang)
    if not cands:
        return None
    return None if _which(cands) else cands[-1]


def support_error(lang: str | None, target: str | None, task: str | None = None,
                  repo=None) -> str | None:
    """Why this (lang, target) cannot be dispatched, or None if it can.

    A diagnosis (`task == "diagnosis"`) is read-only: it writes DIAGNOSIS.md and
    never builds the target, so only the language-agnostic checks apply."""
    lang = canon(lang)
    ext = Path(target or "").suffix.lower()
    unsup = UNSUPPORTED.get(lang) or UNSUPPORTED.get(_UNSUPPORTED_EXT.get(ext, ""))
    if unsup and task != "diagnosis":
        name, _exts, why = unsup
        return (f"{name} is not supported by the local dispatch pipeline "
                f"(--lang {lang}, target {target or '-'}): {why}")
    if task == "diagnosis":
        return None
    if lang not in LANG_EXTS:
        return (f"--lang {lang!r} is not a dispatch flavour; supported: "
                f"{', '.join(sorted(LANG_EXTS))}")
    if target and ext not in LANG_EXTS[lang]:
        want = next((l for l, exts in LANG_EXTS.items() if ext in exts and ext), None)
        hint = f" -- pass --lang {want}" if want else ""
        return (f"--target {target} ({ext or 'no extension'}) is not a {lang} file; "
                f"the {lang} harness would parse/run it as {lang} and fail on every "
                f"iteration{hint}")
    miss = toolchain_missing(lang)
    if miss:
        return (f"the {lang} toolchain (`{miss}`) is not installed on this host, and "
                f"verify.sh runs here -- every verify would die on command-not-found")
    if lang == "csharp":
        return csharp_windows_refusal(repo, target)
    return None


def verify_toolchain_error(verify: str | None, cwd) -> str | None:
    """Enqueue-time check for a hand-assembled dispatch: does the verify's own
    toolchain exist here, and is the scaffolded target a supported language?
    None when fine or undecidable (never refuse blind)."""
    cwd = Path(cwd) if cwd else None
    # 1. the scaffold's manifest names the target
    if cwd is not None:
        try:
            man = json.loads((cwd / ".dispatch-harness.json").read_text())
            tgt = man.get("target") if isinstance(man, dict) else None
        except (OSError, ValueError):
            tgt = None
        if tgt:
            k = _UNSUPPORTED_EXT.get(Path(tgt).suffix.lower())
            if k:
                return support_error(k, tgt)
            if Path(tgt).suffix.lower() == ".cs":
                why = csharp_windows_refusal(cwd, tgt)
                if why:
                    return why
    # 2. the verify command's head, and verify.sh's recorded fixture command
    cmds = []
    if verify:
        cmds.append(verify)
    if cwd is not None and verify and re.search(r"\bverify\.sh\b", verify):
        try:
            m = re.search(r"^#\s*DISPATCH_FIXTURE_CMD:\s*(.+)$",
                          (cwd / "verify.sh").read_text(errors="replace"), re.M)
            if m:
                cmds.append(m.group(1))
        except OSError:
            pass
    for c in cmds:
        try:
            toks = shlex.split(c)
        except ValueError:
            continue
        toks = [t for t in toks if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", t)]
        if not toks:
            continue
        # Only a KNOWN flavour toolchain (swift/go/cargo/javac/kotlinc/node/
        # dotnet) is judged. Anything else -- `source .venv/bin/activate`, a
        # relative `.venv/bin/pytest`, `uv run ...` -- is undecidable from here
        # (it depends on the job cwd / shell state), so it is ALLOWED.
        head = toks[0]
        base = os.path.basename(head)
        lang = next((l for l, cands in TOOLCHAINS.items()
                     if base in {os.path.basename(x) for x in cands}), None)
        if lang is None or ("/" in head and not os.path.isabs(head)):
            continue
        if not _which([head] + list(TOOLCHAINS[lang])):
            if lang in UNSUPPORTED:
                return support_error(lang, None)
            return (f"the verify runs `{head}` (the {lang} toolchain), which is not "
                    f"installed on this host (the worker runs verify locally) -- every "
                    f"iteration would die on command-not-found")
    return None
