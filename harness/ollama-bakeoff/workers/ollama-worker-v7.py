#!/usr/bin/env python3
"""Standalone agentic coding dispatch against Ollama's native /api/chat,
bypassing opencode's CLI entirely.

Built 2026-08-21 because opencode's CLI has two confirmed harness bugs
that make it unreliable for real dispatch:
  1. It corrupts the user-turn prompt before sending it to the provider
     (partial/broken quote-escaping) -- causally proven, 0/7 vs 6/6
     tool-call success with/without the corruption. Filed upstream:
     https://github.com/anomalyco/opencode/issues/43923
  2. A separate infinite self-nudge loop in its `build` agent
     ("Continue if you have next steps..."), confirmed model-independent.
Both bugs live inside opencode's compiled binary and don't exist if
opencode isn't in the dispatch path -- hence this script talks to Ollama
directly and implements its own minimal tool-execution loop.

Termination is based purely on "the model's response has no more
tool_calls" -- never on injecting a self-generated "continue" prompt --
which is the structural fix for bug #2 above.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from datetime import datetime, timezone
from pathlib import Path

try:
    import trafilatura
except ImportError:
    trafilatura = None

DEFAULT_MODEL = "qwen3-14b-agentic"
DEFAULT_HOST = "http://192.0.2.82:11434"
DEFAULT_TEMPERATURE = 0.15
DEFAULT_NUM_CTX = 16384
DEFAULT_MAX_ITERS = 20
# 60s was tight: a warm `npm run build` measures ~10-30s, but a large edit that
# invalidates the Turbopack cache can exceed it, and the model then receives a
# spurious "command timed out" that looks like its own code hanging. Cheap
# insurance -- a real hang still terminates, just later.
BASH_TIMEOUT_S = 240
WEB_TIMEOUT_S = 20
WEB_FETCH_MAX_CHARS = 8000
DEFAULT_SEARXNG_HOST = "http://198.51.100.6:8080"
LOG_DIR = Path.home() / "bin" / "ollama-worker-logs"
UNRAID_OLLAMA_HOSTS = ("192.0.2.82",)  # substrings matched against --host to detect "this dispatch targets Unraid"
LAN_MOUNT_ROOT = "/Volumes/data"  # Unraid's SMB share, mounted here when available
COPY_HELPER = str(Path.home() / "bin" / "copy-ollama-model-from-unraid.py")

# Local hot cache: OLLAMA_MODELS normally points here (fast NVMe). The
# shared SMB store (mounted for both this Mac and Unraid) is the source of
# truth / cold storage -- confirmed live 2026-08-21 that Ollama's own
# model-load path over SMB hangs indefinitely regardless of mmap setting,
# while a plain file copy from the same share does not, so copying once
# and loading locally is the actual fix, not a network/client tuning one.
# Ollama's REAL model store on this Mac. Was `~/ollama-models-local` until
# 2026-08-22, which nothing ever read: OLLAMA_MODELS is unset in the running
# `ollama serve` process, so Ollama loads from its default `~/.ollama/models`.
#
# The consequence was a double SMB transfer for every model not already local:
# ensure_model_cached() copied blobs+manifest from the share into
# ~/ollama-models-local (invisible to Ollama), ensure_model_ready() then found
# the model still missing from /api/tags and ran copy-ollama-model-from-unraid.py,
# which copies from the SAME share again into ~/.ollama/models (its LOCAL_ROOT).
# That second copy is the one that ever worked; the first just accumulated --
# 83GB of it, across 19 blobs, none of them unique.
#
# Pointing this at the real store makes the first copy the only copy: blobs and
# manifest land where Ollama reads, so the model is visible immediately and the
# helper's per-blob "SKIP (already at destination)" makes the second pass free.
LOCAL_MODEL_CACHE = Path.home() / ".ollama" / "models"
SMB_MODEL_SOURCE = Path("/Volumes/data/ollama-models")
MODEL_PULL_LOG = Path.home() / "bin" / "ollama-model-pulls.log"
OBSIDIAN_URL = "http://192.0.2.20:27123"
OBSIDIAN_TOKEN_FILE = Path.home() / ".config" / "ollama-worker" / "obsidian-token"
# Env var first (if a caller's shell happens to have it), else the local
# file -- launchctl setenv only affects processes launched AFTER the
# setenv call, which proved unreliable for background-dispatched runs
# from an already-running shell, so the file is the primary path.
OBSIDIAN_TOKEN = os.environ.get("OBSIDIAN_TOKEN") or (
    OBSIDIAN_TOKEN_FILE.read_text().strip() if OBSIDIAN_TOKEN_FILE.exists() else ""
)
OBSIDIAN_DISPATCH_LOG_PATH = "Claude/Ollama-Dispatch-Log.md"

SYSTEM_PROMPT = """You are a focused coding agent. You have seven tools: \
list_files, read_file, write_file, edit_file, run_bash, web_search, and \
web_fetch. Use them to accomplish the task directly -- don't describe what \
you would do, actually call the tools.

ALWAYS start by calling list_files on '.' to see the project's real \
structure, then read the files that matter, BEFORE writing anything. Never \
guess a file path, and never assume a framework or directory layout -- \
discover it. The project's existing stack and conventions are whatever \
list_files and read_file actually show you, not what a project like this \
usually looks like. Prefer edit_file over write_file when changing an \
existing file.

If AGENTS.md, CLAUDE.md, README.md or CONTRIBUTING.md exist at the top \
level, read them before writing any code. They are written for you and \
state this project's conventions, which may deliberately differ from what \
you learned in training. Follow them over your own habits.

Before you CREATE a new file, read an existing file of the same kind in \
this project and match its idiom exactly -- its imports, its export style, \
its function signatures, its naming. A framework often has several valid \
styles from different versions; the only one that works here is the one \
already in use. If you are adding an API route, read an existing API route \
first. If you are adding a module or target, read how existing ones are \
declared. Your training data is likely to be older than this project.

web_search/web_fetch are for external documentation only (an unfamiliar \
third-party API). Never use them to learn about the project in front of \
you -- that is what list_files and read_file are for.

If you import or require a package, make sure it is actually a dependency \
of this project first -- check package.json (or the equivalent manifest) \
with read_file, and if it is missing either install it with run_bash or \
use something already available. Code that imports a package the project \
does not have will fail to build.

If a path you tried does not exist, do not try it again. Call list_files \
to find where the file actually is, and only use paths you have seen in a \
list_files result.

When the task is fully complete, respond with a final plain-text summary \
and no further tool calls. Keep file paths relative to the working \
directory. Do not ask clarifying questions; make reasonable assumptions \
and proceed."""

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List the files and subdirectories at a path. Use this FIRST to discover the project's structure before reading or writing anything. Pass '.' for the working directory.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Directory path relative to the working directory. Use '.' for the working directory itself."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file's contents. Returns up to 600 lines from the top by default; for a larger file, page through it with offset/limit, or use run_bash with `grep -n` to jump to the part you need instead of reading the whole thing.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the working directory."},
                    "offset": {"type": "integer", "description": "0-based line number to start reading from (default 0). Use the value the previous read reported to continue."},
                    "limit": {"type": "integer", "description": "Maximum number of lines to return (default 600)."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create a NEW file, or fully overwrite an existing one, with the given content. Creates parent directories as needed. For an existing file where you're only changing part of it, use edit_file instead -- write_file forces you to regenerate the entire file from scratch in one response, which is slow and error-prone for anything but a small or brand-new file.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the working directory."},
                    "content": {"type": "string", "description": "Full file content to write."},
                },
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace one exact occurrence of old_string with new_string in an existing file. Use this instead of write_file for any change to a file you didn't just create -- it only requires you to output the small changed region, not the whole file. old_string must match the file's current content exactly (including whitespace/indentation) and must be unique in the file; include enough surrounding context (a few lines before/after) to make it unique if the change itself is a short/common line.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Path relative to the working directory."},
                    "old_string": {"type": "string", "description": "Exact existing text to replace, unique within the file."},
                    "new_string": {"type": "string", "description": "Text to replace it with."},
                },
                "required": ["path", "old_string", "new_string"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command in the working directory and return stdout, stderr, and exit code.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run."},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (self-hosted SearXNG). Returns the top results as title/url/snippet.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query."},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch a URL and return its main readable text content (HTML stripped of nav/ads/scripts). Truncated if very long.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch."},
                },
                "required": ["url"],
            },
        },
    },
]


def render_manual_tools_block(tools: list) -> str:
    """Textual tool-schema injection for models whose Ollama chat template
    doesn't render the native `tools` API field into the prompt at all --
    confirmed 2026-08-21 for deepseek-r1 distills (incl. the community
    'MFDoom/deepseek-r1-tool-calling' build, which ships the exact same
    template gap): rendering the real Jinja template locally with `tools`
    populated proved it never appears in the output. Root cause per an
    Ollama maintainer (github.com/ollama/ollama/issues/8517): these distills
    don't emit the exact special-token sequence Ollama's native tool_calls
    parser requires. This sidesteps that parser entirely by describing the
    tools as plain text (Qwen/DeepSeek's own convention) and having
    extract_manual_tool_call() below parse the model's response ourselves."""
    lines = [json.dumps({"type": "function", "function": t.get("function", t)}) for t in tools]
    tools_xml = "\n".join(lines)
    valid_names = ", ".join(t.get("function", t)["name"] for t in tools)
    return f"""# Tools

You may call one function per response to assist with the user query. You are provided with function signatures within <tools></tools> XML tags:
<tools>
{tools_xml}
</tools>

For each function call, return ONLY a JSON object with function name and arguments within <tool_call></tool_call> XML tags -- nothing else, no other text:
<tool_call>
{{"name": <function-name>, "arguments": <args-json-object>}}
</tool_call>

CRITICAL: the "name" field must be EXACTLY one of these literal tool names:
  {valid_names}
Never put a file path, a filename, or anything else in "name" -- file paths always go inside "arguments".
Correct:   {{"name": "read_file", "arguments": {{"path": "app/page.tsx"}}}}
INCORRECT: {{"name": "app/page.tsx", "arguments": {{"path": "app"}}}}

When the task is fully done and no more function calls are needed, respond with a normal plain-text message and NOT a <tool_call> block."""


_VALID_JSON_ESCAPE_CHARS = set('"\\/bfnrtu')


def _repair_invalid_json_escapes(s: str) -> str:
    """Char-by-char (not regex-substitution) repair of a JSON-string
    candidate containing backslashes that aren't valid JSON escapes --
    e.g. a model copying a shell `find ... \\( -o ... \\)` snippet into a
    tool-call argument without doubling those backslashes for JSON.
    Confirmed live 2026-08-21 (qwen2.5-coder:14b) that a naive single-char
    lookahead regex (`re.sub(r'\\\\(?!...)', ...)`) double-counts an
    ALREADY-valid two-character escape like `\\.` (backslash-backslash
    then a literal dot) -- it inspects the second backslash of that valid
    pair in isolation, sees the dot after it, and wrongly "repairs" a
    correct escape. Walking left-to-right and consuming valid pairs whole
    avoids that: only a backslash NOT already paired with a valid escape
    char gets doubled, and the walk advances 2 chars over anything it
    correctly recognized as already-valid."""
    out = []
    i = 0
    n = len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n and s[i + 1] in _VALID_JSON_ESCAPE_CHARS:
            out.append(c)
            out.append(s[i + 1])
            i += 2
            continue
        if c == "\\":
            out.append("\\\\")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


_QWEN_FN_RE = re.compile(r'<function=([^>\s]+)\s*>(.*?)</function>', re.DOTALL)
_QWEN_PARAM_RE = re.compile(r'<parameter=([^>\s]+)\s*>(.*?)</parameter>', re.DOTALL)


def extract_qwen_xml_tool_calls(content: str) -> list:
    """Parse Qwen3-Coder's native XML tool-call dialect, e.g.
        <function=read_file><parameter=path>app/page.tsx</parameter></function>
    (optionally wrapped in <tool_call>...</tool_call>). Confirmed live
    2026-08-23 (qwen3-coder:30b, clamshell base r3): the model emitted
    <function=list_files> XML that NOTHING in the harness parsed -- the run
    executed zero calls in 7s and was scored as an inert model, when it was a
    harness dialect gap, not the model. Added 2026-08-24 as the v9 R2 instrument
    fix so that rep re-dispatches against a harness that can actually execute
    its calls. Parameter VALUES are captured as raw text (not JSON), which is
    cleaner than the JSON path for multi-line write_file content -- no escaping
    to get wrong."""
    calls = []
    for m in _QWEN_FN_RE.finditer(content):
        name = m.group(1).strip()
        args = {}
        for pm in _QWEN_PARAM_RE.finditer(m.group(2)):
            args[pm.group(1).strip()] = pm.group(2)
        calls.append({"name": name, "arguments": args})
    return calls


def extract_manual_tool_calls(content: str) -> list:
    """Find every {"name": ..., "arguments": ...} object in plain-text model
    output. Tag-agnostic on purpose -- confirmed live 2026-08-21 that
    deepseek-r1:14b is inconsistent about wrapping this in <tool_call>,
    <tools>, or no tag at all, but the JSON object itself came back
    correctly-schema'd (right field names) in every real test, unlike
    native tool_calls mode which hallucinated wrong field names.

    Returns a LIST, not just the first match -- confirmed live 2026-08-21
    that despite the system prompt saying "one function per response", the
    model sometimes emits several call objects back-to-back in a single
    response (e.g. write two files then run a command). Silently taking
    only the first one drops real, correctly-formed work the model already
    did -- confirmed as a real bug this way (a second file's write_file
    call was dropped, breaking the task) before this was made to scan the
    rest of the string instead of stopping at the first match."""
    # Pre-pass: normalize Python-style triple-quoted values into real JSON
    # strings. Confirmed live 2026-08-22 (deepseek-r1:32b, clamshell task):
    # the model emitted
    #     {"name": "write_file", "arguments": {"path": "Sources/Clamshell/
    #      ConfirmationBridge.swift", "content": """// swiftlint:disable all
    # -- a correct call, at a correct path, with `"""` where JSON needs `"`.
    # json.loads rejects it, the call was discarded, and the run scored
    # files=0 as though the model had done nothing. This must run BEFORE the
    # brace scan below: `"""` opens-then-closes-then-reopens a string as far
    # as the scanner is concerned, so the span would be found wrong too.
    #
    # Anchored on the closing `"""` being followed by the object's closing
    # braces, so a legitimate `"""` INSIDE the content (a Python docstring)
    # doesn't terminate the match early and truncate the file being written.
    content = re.sub(
        r'(:\s*)"""(.*?)"""(?=\s*\}\s*\})',
        lambda m: m.group(1) + json.dumps(m.group(2)),
        content,
        flags=re.DOTALL,
    )

    calls = []
    pos = 0
    while True:
        # Match either key order. Confirmed empirically that a model emitting
        # {"arguments": {...}, "name": "..."} was dropped entirely by the
        # name-then-arguments-only pattern -- it scores 0 while calling tools
        # correctly, which is the exact failure class this session kept
        # mistaking for model incompetence.
        m = re.search(
            r'\{.*?"name"\s*:.*?"arguments"\s*:|\{.*?"arguments"\s*:.*?"name"\s*:',
            content[pos:], re.DOTALL)
        if not m:
            break
        start = pos + m.start()
        depth = 0
        end = None
        # String-aware brace matching. A naive depth counter also counts
        # braces that appear INSIDE a JSON string literal -- i.e. inside
        # the file content a model is writing -- so any unbalanced brace
        # in generated code (a `}` in a comment, a truncated snippet)
        # ends the candidate at the wrong offset and the whole tool call
        # is silently dropped. Skip over string literals entirely.
        in_str = False
        esc = False
        for i in range(start, len(content)):
            c = content[i]
            if in_str:
                if esc:
                    esc = False
                elif c == '\\':
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == '{':
                depth += 1
            elif c == '}':
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            break
        candidate = content[start:end + 1]
        try:
            parsed = json.loads(candidate)
        except Exception:
            # Confirmed live 2026-08-21 (qwen2.5-coder:14b, model-bakeoff run):
            # models embedding a shell command inside the "arguments" string
            # sometimes copy shell-escape backslashes (e.g. `\(`, `\)` from a
            # `find ... \( -o ... \)` snippet) verbatim without doubling them,
            # producing a backslash followed by a character JSON doesn't
            # recognize as an escape (strict json.loads raises "Invalid
            # \escape" on this, unlike some lenient parsers). Repair by
            # doubling any backslash not already followed by a valid JSON
            # escape char, then retry once before giving up on this
            # candidate -- same spirit as _fix_literal_escapes above, but
            # for the opposite direction (under- rather than over-escaped).
            #
            # Second repair, confirmed live 2026-08-22 (deepseek-r1:32b,
            # clamshell ConfirmationBridge task): a model can escape its
            # quotes correctly (\"Clamshell\") while emitting REAL newline
            # characters inside the "content" string rather than \n. JSON
            # forbids raw control characters in string literals, so
            # json.loads raises and the tool call is thrown away -- the
            # worker then treats a write_file attempt as a final text
            # answer, stops, and `swift build` passes on an untouched tree
            # (exit=0, files_changed=0), which reads as a clean pass. Try
            # each repair alone and then combined before giving up.
            parsed = None
            for repair in (
                _repair_invalid_json_escapes,
                _escape_raw_control_chars,
                lambda s: _escape_raw_control_chars(_repair_invalid_json_escapes(s)),
            ):
                try:
                    parsed = json.loads(repair(candidate))
                    break
                except Exception:
                    continue
            if parsed is None:
                pos = end + 1
                continue
        if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
            calls.append(parsed)
            pos = end + 1
        else:
            # The balanced object parsed but isn't a tool call itself -- it's a
            # WRAPPER around one: {"tool_call": {...}}, {"function": {...}}.
            # Skipping to end+1 would step over the real call nested inside and
            # return nothing. Rescan from just inside this object instead, so
            # the inner call is found on the next pass.
            pos = start + 1
    # Fallback to the Qwen3-Coder XML dialect only when the JSON scan found
    # nothing -- a model emits one dialect or the other, never both, and the
    # brace scanner above cannot match XML (no {...} objects) so there is no
    # risk of double-counting. This is what lets qwen3-coder:30b's
    # <function=...> calls execute (v9 R2 fix, 2026-08-24).
    if not calls:
        calls = extract_qwen_xml_tool_calls(content)
    return calls


def _escape_raw_control_chars(s: str) -> str:
    """Escape literal newline/CR/tab characters appearing INSIDE a JSON
    string literal, leaving structural whitespace between tokens alone.
    JSON forbids raw control characters inside strings; some models emit
    them anyway when writing multi-line file content. Tracks string state
    so it never touches the JSON's own formatting."""
    out = []
    in_str = False
    esc = False
    for ch in s:
        if not in_str:
            out.append(ch)
            if ch == '"':
                in_str = True
            continue
        if esc:
            out.append(ch)
            esc = False
            continue
        if ch == '\\':
            out.append(ch)
            esc = True
            continue
        if ch == '"':
            out.append(ch)
            in_str = False
            continue
        out.append({'\n': '\\n', '\r': '\\r', '\t': '\\t'}.get(ch, ch))
    return ''.join(out)


def log(msg):
    print(msg, flush=True)


def resolve_path(cwd: Path, path: str) -> Path:
    p = (cwd / path).resolve()
    if cwd.resolve() not in p.parents and p != cwd.resolve():
        raise ValueError(f"path escapes working directory: {path}")
    return p


def tool_list_files(cwd: Path, args: dict) -> str:
    """List a directory. Added 2026-08-22 after probing showed BOTH 7B
    models reach for a listing tool that did not exist: qwen2.5-coder:7b
    called read_file({"path": "."}) (reading a directory as a file), and
    deepseek-r1:7b invented a `list_files` call outright and then
    hallucinated the directory contents. Without this, a small model's
    only route to discovery was guessing filenames -- which is exactly
    what produced the fabricated Flask/MERN stacks in the bake-off."""
    p = resolve_path(cwd, args.get("path") or ".")
    if not p.exists():
        return f"ERROR: path not found: {args.get('path')}"
    if not p.is_dir():
        return f"ERROR: not a directory: {args.get('path')} (use read_file for files)"
    skip = {".git", "node_modules", ".next", ".build", "__pycache__", ".venv"}
    entries = []
    try:
        for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
            if child.name in skip:
                entries.append(f"{child.name}/  (skipped: large/generated)")
            elif child.is_dir():
                entries.append(f"{child.name}/")
            else:
                entries.append(f"{child.name}  ({child.stat().st_size} bytes)")
    except Exception as e:
        return f"ERROR listing directory: {e}"
    if not entries:
        return "(empty directory)"
    return "\n".join(entries)


READ_FILE_MAX_LINES = 600


def tool_read_file(cwd: Path, args: dict) -> str:
    p = resolve_path(cwd, args["path"])
    if not p.exists():
        return f"ERROR: file not found: {args['path']} -- use list_files to see what exists."
    if p.is_dir():
        # Do not dead-end here: a bare "Is a directory" errno was what the
        # models hit before list_files existed, with no hint what to do next.
        return (f"ERROR: {args['path']} is a directory, not a file. "
                f"Listing it instead:\n" + tool_list_files(cwd, {"path": args["path"]}))
    try:
        text = p.read_text()
    except Exception as e:
        return f"ERROR reading file: {e}"

    # Windowed read (added 2026-08-24, v9 R1 instrument fix). Without a cap this
    # returned the ENTIRE file, and v8's debug cell measured the consequence
    # directly: arr-webhook.py is 228KB (~57k tokens) and one read_file filled
    # or overflowed the whole context window (65k, and past qwen2.5-coder's 32k
    # / qwen3-14b-agentic's 41k outright) -- 10/18 rows stalled `config_ceiling`
    # at 5-6 iterations having read nothing usable, and every nonzero
    # files_changed was junk. That cell was measuring "the file doesn't fit in
    # context", not debugging. A default 2000-line window (mirroring the
    # orchestrator's own Read tool) plus offset/limit paging turns that into
    # "read the part you need". Whole files that already fit are returned
    # verbatim, so behaviour for small files is unchanged.
    lines = text.splitlines()
    total = len(lines)
    try:
        offset = max(0, int(args.get("offset", 0) or 0))
    except (TypeError, ValueError):
        offset = 0
    try:
        limit = int(args.get("limit", READ_FILE_MAX_LINES) or READ_FILE_MAX_LINES)
    except (TypeError, ValueError):
        limit = READ_FILE_MAX_LINES
    if limit <= 0:
        limit = READ_FILE_MAX_LINES

    if offset == 0 and total <= limit:
        return text  # fits in one window -- return verbatim (preserves trailing newline)

    window = lines[offset:offset + limit]
    shown_end = offset + len(window)
    hint = (f"\n\n[read_file: showing lines {offset + 1}-{shown_end} of {total}. "
            f"Call read_file again with offset={shown_end} for the next window, "
            f"or use run_bash with `grep -n` to jump straight to a symbol instead "
            f"of paging the whole file.]")
    return "\n".join(window) + hint


def _fix_literal_escapes(content: str) -> str:
    """Some models (confirmed: devstral:24b) emit write_file content with
    literal two-character `\\n`/`\\t` sequences instead of real newline/tab
    bytes -- a generation quirk, not a JSON-parsing bug (Ollama's own
    tool_calls arguments field already decodes cleanly; the model just
    wrote backslash-n as text). Heuristic: only fix this when the content
    has ZERO real newlines but at least one literal `\\n` -- a file that
    already has real newlines mixed with a few genuinely-intended literal
    backslash sequences (e.g. a regex) is left alone, to avoid mangling
    correct output from models that don't have this quirk."""
    if "\n" not in content and "\\n" in content:
        content = (content.replace("\\r\\n", "\n").replace("\\n", "\n")
                          .replace("\\t", "\t").replace('\\"', '"'))
    return content


def tool_write_file(cwd: Path, args: dict) -> str:
    p = resolve_path(cwd, args["path"])
    p.parent.mkdir(parents=True, exist_ok=True)
    content = _fix_literal_escapes(args["content"])
    p.write_text(content)
    return f"OK: wrote {len(content)} bytes to {args['path']}"


def tool_edit_file(cwd: Path, args: dict) -> str:
    """Targeted find/replace, added 2026-08-22 after a real incident: a
    model (qwen3-coder-next) forced to re-emit a whole 762-line file via
    write_file to make one small change generated 35,000+ tokens without
    stopping (confirmed live via Ollama's own generation log -- steady
    ~43 tok/s the entire time, two separate context-window shifts along
    the way) before being killed. Long-verbatim-file reproduction is a
    known LLM failure mode; giving models a small-diff tool instead of
    forcing a full rewrite is the actual fix, not a longer timeout."""
    p = resolve_path(cwd, args["path"])
    if not p.exists():
        return f"ERROR: file not found: {args['path']}"
    try:
        content = p.read_text()
    except Exception as e:
        return f"ERROR reading file: {e}"
    old_string = _fix_literal_escapes(args["old_string"])
    new_string = _fix_literal_escapes(args["new_string"])
    count = content.count(old_string)
    if count == 0:
        return (f"ERROR: old_string not found in {args['path']} -- it must match the file's "
                f"current exact content, including whitespace/indentation. Re-read the file if unsure.")
    if count > 1:
        return (f"ERROR: old_string matches {count} locations in {args['path']} -- it must be "
                f"unique. Include more surrounding context (a line or two before/after) to disambiguate.")
    new_content = content.replace(old_string, new_string, 1)
    p.write_text(new_content)
    return f"OK: replaced 1 occurrence in {args['path']} ({len(new_content)} bytes total)"


def tool_run_bash(cwd: Path, args: dict) -> str:
    try:
        result = subprocess.run(
            args["command"], shell=True, cwd=cwd,
            capture_output=True, text=True, timeout=BASH_TIMEOUT_S,
        )
        return json.dumps({
            "exit_code": result.returncode,
            "stdout": result.stdout[-4000:],
            "stderr": result.stderr[-4000:],
        })
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {BASH_TIMEOUT_S}s"
    except Exception as e:
        return f"ERROR running command: {e}"


def tool_web_search(cwd: Path, args: dict, searxng_host: str) -> str:
    query = args["query"]
    url = f"{searxng_host}/search?" + urllib.parse.urlencode({"q": query, "format": "json"})
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return (f"ERROR: web_search failed ({e}). Is SearXNG deployed and reachable "
                f"at {searxng_host}?")
    results = (data.get("results") or [])[:5]
    if not results:
        return "No results found."
    lines = []
    for r in results:
        title = r.get("title", "")
        result_url = r.get("url", "")
        snippet = (r.get("content") or "")[:300]
        lines.append(f"- {title}\n  {result_url}\n  {snippet}")
    return "\n".join(lines)


def tool_web_fetch(cwd: Path, args: dict) -> str:
    url = args["url"]
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0 (ollama-worker)"})
    try:
        with urllib.request.urlopen(req, timeout=WEB_TIMEOUT_S) as resp:
            html = resp.read()
    except Exception as e:
        return f"ERROR: web_fetch failed: {e}"

    text = None
    if trafilatura is not None:
        text = trafilatura.extract(html, url=url, include_comments=False, include_tables=True)
    if not text:
        # Fallback: no trafilatura, or it found no extractable main content.
        text = html.decode("utf-8", errors="replace")
        text = f"[NOTE: main-content extraction unavailable/failed, this is raw decoded HTML]\n{text}"

    truncated = len(text) > WEB_FETCH_MAX_CHARS
    text = text[:WEB_FETCH_MAX_CHARS]
    if truncated:
        text += f"\n\n[TRUNCATED at {WEB_FETCH_MAX_CHARS} chars]"
    return text


def build_tool_impls(searxng_host: str) -> dict:
    return {
        "list_files": tool_list_files,
        "read_file": tool_read_file,
        "write_file": tool_write_file,
        "edit_file": tool_edit_file,
        "run_bash": tool_run_bash,
        "web_search": lambda cwd, args: tool_web_search(cwd, args, searxng_host),
        "web_fetch": lambda cwd, args: tool_web_fetch(cwd, args),
    }


def _manifest_path(models_root: Path, model: str) -> Path:
    # "qwen3-coder-next:q4_K_M" -> manifests/registry.ollama.ai/library/qwen3-coder-next/q4_K_M
    # "qwen3.6" -> .../qwen3.6/latest
    # "MFDoom/deepseek-r1-tool-calling:14b" -> .../registry.ollama.ai/MFDoom/deepseek-r1-tool-calling/14b
    #
    # Only OFFICIAL models live under "library/"; a namespaced (org/user)
    # model sits directly under the registry root. Hardcoding "library"
    # here silently cost MFDoom/deepseek-r1-tool-calling:14b both of its
    # build-off tasks on 2026-08-22 -- ensure_model_cached raised
    # "not found in SMB source" in 0s and the driver recorded exit=1,
    # files=0, which reads as a model failure rather than a harness bug.
    name, _, tag = model.partition(":")
    tag = tag or "latest"
    root = models_root / "manifests" / "registry.ollama.ai"
    if "/" in name:
        return root / Path(name) / tag
    return root / "library" / name / tag


def _manifest_digests(manifest_path: Path) -> list[str]:
    manifest = json.loads(manifest_path.read_text())
    digests = [manifest["config"]["digest"]] + [l["digest"] for l in manifest["layers"]]
    return [d.replace(":", "-") for d in digests]


def log_model_pull(model: str, size_bytes: int, duration_s: float) -> None:
    MODEL_PULL_LOG.parent.mkdir(parents=True, exist_ok=True)
    entry = {
        "ts": datetime.now(timezone.utc).isoformat(), "model": model,
        "size_gb": round(size_bytes / 1024**3, 2), "duration_s": round(duration_s, 1),
    }
    with MODEL_PULL_LOG.open("a") as f:
        f.write(json.dumps(entry) + "\n")
    log(f"[worker] logged model pull: {entry}")


def _normalize_model_name(m: str) -> str:
    n, _, t = m.partition(":")
    return f"{n}:{t or 'latest'}"


def _model_visible_to_local_ollama(model: str) -> bool:
    """Ask the local Ollama server directly whether it already has this
    model, regardless of which directory it's actually stored in. Added
    2026-08-21 after ensure_model_cached blindly copied a model's full
    blobs into LOCAL_MODEL_CACHE even though it was already sitting in
    Ollama's real (default, OLLAMA_MODELS-unset) directory the whole
    time -- wasted ~24GB of a redundant SMB copy before being caught."""
    try:
        req = urllib.request.Request("http://localhost:11434/api/tags")
        with urllib.request.urlopen(req, timeout=15) as resp:
            tags = json.loads(resp.read())
    except Exception:
        return False
    names = {_normalize_model_name(m.get("name", "")) for m in tags.get("models", []) if m.get("name")}
    return _normalize_model_name(model) in names


def ensure_model_cached(model: str) -> None:
    """Copy `model`'s blobs from the SMB source into LOCAL_MODEL_CACHE
    (OLLAMA_MODELS normally points here) if not already present. Confirmed
    live 2026-08-21: Ollama's own model-load path over SMB hangs
    indefinitely; a plain file copy from the same share does not -- so
    this, not client-side tuning, is the actual fix. No-op if the model is
    already cached locally (checked two ways: already visible to the
    running local Ollama server via /api/tags -- covers the case where
    OLLAMA_MODELS isn't actually pointed at LOCAL_MODEL_CACHE, e.g. a
    model placed directly in Ollama's default directory -- or already
    present in LOCAL_MODEL_CACHE's own manifest)."""
    if _model_visible_to_local_ollama(model):
        log(f"[worker] {model} already visible to local Ollama (/api/tags) -- skipping SMB copy entirely.")
        return
    local_manifest = _manifest_path(LOCAL_MODEL_CACHE, model)
    if local_manifest.exists():
        log(f"[worker] {model} already cached locally, skipping copy.")
        return
    source_manifest = _manifest_path(SMB_MODEL_SOURCE, model)
    if not source_manifest.exists():
        raise RuntimeError(f"model {model} not found in SMB source at {source_manifest}")

    digests = _manifest_digests(source_manifest)
    log(f"[worker] caching {model} locally ({len(digests)} blob(s)) -- this reads the full "
        f"model over SMB once, expect real time for a large model...")
    start = datetime.now(timezone.utc)
    total_size = 0
    (LOCAL_MODEL_CACHE / "blobs").mkdir(parents=True, exist_ok=True)
    for digest in digests:
        src = SMB_MODEL_SOURCE / "blobs" / digest
        dst = LOCAL_MODEL_CACHE / "blobs" / digest
        if not dst.exists():
            shutil.copyfile(src, dst)
        total_size += dst.stat().st_size
    local_manifest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source_manifest, local_manifest)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log(f"[worker] cached {model}: {total_size/1024**3:.1f}GB in {elapsed:.0f}s")
    log_model_pull(model, total_size, elapsed)


def evict_model(model: str) -> None:
    """Remove `model`'s blobs from LOCAL_MODEL_CACHE, but only blobs not
    referenced by any OTHER cached model's manifest -- safe even if models
    share layers. Used by --cleanup-after to avoid permanently consuming
    local disk for models only used occasionally."""
    local_manifest = _manifest_path(LOCAL_MODEL_CACHE, model)
    if not local_manifest.exists():
        log(f"[worker] {model} not in local cache, nothing to evict.")
        return
    this_digests = set(_manifest_digests(local_manifest))

    other_digests = set()
    manifests_root = LOCAL_MODEL_CACHE / "manifests"
    for other_manifest in manifests_root.rglob("*"):
        if not other_manifest.is_file() or other_manifest == local_manifest:
            continue
        try:
            other_digests.update(_manifest_digests(other_manifest))
        except Exception:
            continue

    freed = 0
    for digest in this_digests - other_digests:
        blob = LOCAL_MODEL_CACHE / "blobs" / digest
        if blob.exists():
            freed += blob.stat().st_size
            blob.unlink()
    local_manifest.unlink()
    log(f"[worker] evicted {model} from local cache, freed {freed/1024**3:.1f}GB")


def log_dispatch_to_obsidian(model: str, task: str, converged: bool, verify_passed, log_path: Path) -> None:
    """Best-effort append to the vault's dispatch log -- never raises,
    a logging failure must not fail the actual dispatch. Requires
    OBSIDIAN_TOKEN in the environment (set via launchctl setenv, never
    hardcoded in this file)."""
    if not OBSIDIAN_TOKEN:
        log("[worker] OBSIDIAN_TOKEN not set -- skipping vault dispatch log (task itself is unaffected).")
        return
    status = "converged" if converged else "DID NOT CONVERGE"
    verify_str = "PASSED" if verify_passed is True else "FAILED" if verify_passed is False else "not run"
    entry = (
        f"\n- **{datetime.now(timezone.utc).isoformat(timespec='seconds')}** "
        f"`{model}` -- {status}, verify {verify_str}\n"
        f"  task: {task[:200]}{'...' if len(task) > 200 else ''}\n"
        f"  transcript: `{log_path}`\n"
    )
    try:
        req = urllib.request.Request(
            f"{OBSIDIAN_URL}/vault/{urllib.parse.quote(OBSIDIAN_DISPATCH_LOG_PATH)}",
            data=entry.encode(), method="POST",
            headers={"Authorization": f"Bearer {OBSIDIAN_TOKEN}", "Content-Type": "text/markdown"},
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
        log("[worker] dispatch logged to Obsidian vault.")
    except Exception as e:
        log(f"[worker] vault logging failed (non-fatal): {e}")


CHAT_TIMEOUT_S = 1200
WARMUP_TIMEOUT_S = 900  # cold model load (weights off disk into RAM/VRAM) can take minutes, not seconds
CHAT_RETRIES = 2


def call_ollama(host: str, model: str, messages: list, temperature: float, num_ctx: int,
                 timeout: int = CHAT_TIMEOUT_S, tools: bool = True,
                 top_p: float = None, top_k: int = None, api_style: str = "ollama") -> dict:
    """api_style="openai" targets llama-server (or any OpenAI-compatible
    /v1/chat/completions endpoint) instead of Ollama's native /api/chat.
    Added 2026-08-22: confirmed live that Ollama's own chat-template
    validation has a real upstream bug ("no user query found in messages",
    github.com/ollama/ollama/issues/17778) that crashes qwen3.8/devstral
    even on trivial requests -- llama-server renders the GGUF's own embedded
    chat template directly and doesn't run Ollama's custom Go renderer code
    at all, so it doesn't hit this bug. Returns a response already
    normalized to Ollama's shape ({"message": {...}}) so callers don't need
    to know which backend actually served the request."""
    options = {"temperature": temperature, "num_ctx": num_ctx}
    if top_p is not None:
        options["top_p"] = top_p
    if top_k is not None:
        options["top_k"] = top_k

    if api_style == "openai":
        payload = {"model": model, "messages": messages, "stream": False,
                   "temperature": temperature}
        if top_p is not None:
            payload["top_p"] = top_p
        if tools:
            payload["tools"] = TOOLS
        url = f"{host}/v1/chat/completions"
    else:
        payload = {"model": model, "messages": messages, "stream": False, "options": options}
        if tools:
            payload["tools"] = TOOLS
        url = f"{host}/api/chat"

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    last_err = None
    for attempt in range(1, CHAT_RETRIES + 2):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = json.loads(resp.read())
                if api_style == "openai":
                    # Normalize {"choices": [{"message": {...}}]} to Ollama's
                    # {"message": {...}} shape so the rest of run_task's loop
                    # doesn't need to know which backend answered. Carry `usage`
                    # through -- the context guard reads usage.prompt_tokens on
                    # this path, and dropping it here left that guard silently
                    # dead on llama-server (Fable review 2026-08-23, follow-up).
                    return {"message": raw["choices"][0]["message"],
                            "usage": raw.get("usage") or {}}
                return raw
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt <= CHAT_RETRIES:
                log(f"[worker] {'llama-server' if api_style == 'openai' else 'Ollama'} request failed ({e}), retry {attempt}/{CHAT_RETRIES}...")
    raise RuntimeError(f"Chat request failed after retries: {last_err}") from last_err


def _try_lan_copy(host: str, model: str) -> bool:
    """Before falling back to a fresh registry pull, try copying the model
    over the LAN instead -- from Unraid's shared store if this dispatch
    targets Mac Studio's local Ollama, or from Mac Studio's local cache if
    this dispatch targets Unraid. Mirrors bakeoff-driver-buildoff.sh's
    ensure_model_available(), generalized here so it applies to any
    dispatch (ad-hoc or driver-orchestrated), not only driver-orchestrated
    ones -- confirmed live 2026-08-22 that a direct ollama-worker.py
    invocation skipped this safeguard entirely before this fix, since it
    previously only lived in the bash driver wrapping this function.
    Returns True if the copy succeeded (model is now present at `host`),
    False if the LAN path isn't available or the model isn't there either
    -- caller falls back to a fresh pull in that case."""
    if not os.path.ismount(LAN_MOUNT_ROOT):
        log(f"[worker] {LAN_MOUNT_ROOT} not mounted -- skipping LAN-copy path.")
        return False

    targets_unraid = any(h in host for h in UNRAID_OLLAMA_HOSTS)
    direction = "push" if targets_unraid else "pull"
    log(f"[worker] {model} not present at {host} -- trying LAN copy ({direction}) before a fresh pull...")

    try:
        result = subprocess.run(
            [sys.executable, COPY_HELPER, direction, model],
            capture_output=True, text=True, timeout=600,
        )
    except Exception as e:
        log(f"[worker] LAN copy failed to run: {e}")
        return False

    if result.returncode != 0:
        log(f"[worker] LAN copy unsuccessful ({direction} {model}): {(result.stdout or '').strip()} {(result.stderr or '').strip()}")
        return False

    log(f"[worker] LAN copy succeeded ({direction} {model}).")
    return True


def ensure_model_ready(host: str, model: str, temperature: float, num_ctx: int,
                        top_p: float = None, top_k: int = None, api_style: str = "ollama") -> None:
    """Confirm the model is pulled, then explicitly load it into memory with
    a generous timeout, fully separate from the main dispatch loop's
    request timeout. A cold model load (reading multi-GB weights off disk)
    can easily exceed a normal chat-request timeout on its own, before any
    real work even starts -- this was the actual cause of an earlier crash
    tonight (a plain 180s timeout on the very first request to a model
    that hadn't been loaded yet)."""
    if api_style == "openai":
        # llama-server loads exactly one model, given via -m at process
        # startup -- there's no /api/tags-style discovery or /api/pull, and
        # nothing to warm up (the model is either already resident because
        # the server started successfully, or the server isn't up at all).
        # A plain /health check is the whole story here.
        log(f"[worker] checking llama-server is up at {host}...")
        req = urllib.request.Request(f"{host}/health")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except Exception as e:
            raise RuntimeError(f"could not reach {host}/health: {e}") from e
        log(f"[worker] llama-server is up.")
        return

    log(f"[worker] checking {model} is pulled on {host}...")
    req = urllib.request.Request(f"{host}/api/tags")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            tags = json.loads(resp.read())
    except Exception as e:
        raise RuntimeError(f"could not reach {host}/api/tags: {e}") from e
    names = {m.get("name") for m in tags.get("models", [])}
    # Ollama's /api/tags always qualifies a tag-less model with ":latest"
    # (e.g. "qwen3-14b-agentic" is listed as "qwen3-14b-agentic:latest"),
    # but a caller passing the bare name (no ":" at all) never matches that
    # literally -- confirmed live 2026-08-21: this false "not present"
    # triggered a real /api/pull against the public registry for a
    # locally-built custom model with no upstream equivalent, which 500'd
    # and crashed the whole dispatch. Normalize both sides to name:tag
    # (defaulting a missing tag to "latest") before comparing.
    normalized_names = {_normalize_model_name(n) for n in names if n}
    if _normalize_model_name(model) not in normalized_names:
        # Before ever pulling fresh from the public registry, try a LAN copy
        # first. This safeguard already existed in bakeoff-driver-buildoff.sh
        # (ensure_model_available()) but only for driver-orchestrated runs --
        # confirmed live 2026-08-22 that any ad-hoc direct dispatch (calling
        # this script by hand, not through a driver) skipped it entirely,
        # since the check lived in bash wrapping this function rather than in
        # the function itself. Moving it here makes it apply universally.
        if not _try_lan_copy(host, model):
            log(f"[worker] {model} not present locally or on the LAN -- pulling fresh from the registry (this can take a while for a large model)...")
            pull_req = urllib.request.Request(
                f"{host}/api/pull",
                data=json.dumps({"model": model, "stream": False}).encode(),
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(pull_req, timeout=1800) as resp:
                resp.read()
            log(f"[worker] pull complete.")

    log(f"[worker] warming up {model} (loading into memory, up to {WARMUP_TIMEOUT_S}s)...")
    start = datetime.now(timezone.utc)
    call_ollama(host, model, [{"role": "user", "content": "ready"}], temperature, num_ctx,
                timeout=WARMUP_TIMEOUT_S, tools=False, top_p=top_p, top_k=top_k)
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log(f"[worker] {model} loaded and warm ({elapsed:.0f}s).")



def _restart_local_ollama(host: str) -> bool:
    """Restart Ollama when it is local and we manage it. Returns True if a restart
    was attempted. Never touches a remote host -- we do not own its lifecycle."""
    if "127.0.0.1" not in host and "localhost" not in host:
        log(f"[worker] {host} is remote; not restarting it")
        return False
    try:
        subprocess.run(["/opt/homebrew/bin/brew", "services", "restart", "ollama"],
                       capture_output=True, timeout=120)
        time.sleep(20)
        return True
    except Exception as exc:                                        # noqa: BLE001
        log(f"[worker] restart failed: {exc}")
        return False


def _load_failed_result(model, host, cwd, task, exc, warmup_s):
    """A model that never loaded produced no run. Record it as its own outcome so
    it is never scored as a model failure, and write a stub transcript so the CSV's
    transcript column is never empty."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = LOG_DIR / f"{ts}.json"
    try:
        path.write_text(json.dumps({
            "model": model, "host": host, "cwd": str(cwd), "task": task,
            "converged": False, "iterations": 0, "stop_reason": "load_failed",
            "warmup_s": round(warmup_s, 1), "error": f"{type(exc).__name__}: {exc}",
            "messages": [],
        }, indent=2))
    except Exception:                                               # noqa: BLE001
        pass
    log(f"[worker] full transcript written to {path}")
    log(f"[worker] LOAD FAILED after {warmup_s:.0f}s -- the model never loaded, so this "
        f"is NOT a model outcome. Do not score it as one.")
    # run_task's contract is an EXIT CODE (see sys.exit(run_task(...))): 0 converged,
    # 2 did-not-converge. 3 is free and means "the model never loaded" -- a distinct
    # signal so the driver never files this as a model outcome.
    return 3


def run_task(model, host, cwd, task, verify, max_iters, temperature, num_ctx, searxng_host,
             system_prompt_file=None, cleanup_after=False, manual_tools=False,
             top_p=None, top_k=None, api_style="ollama"):
    cwd = Path(cwd).resolve()
    cwd.mkdir(parents=True, exist_ok=True)
    tool_impls = build_tool_impls(searxng_host)
    system_prompt = SYSTEM_PROMPT
    if system_prompt_file:
        system_prompt = Path(system_prompt_file).read_text()
        log(f"[worker] using custom system prompt from {system_prompt_file}")
    if manual_tools:
        system_prompt = system_prompt + "\n\n" + render_manual_tools_block(TOOLS)
        log("[worker] --manual-tools: native tool_calls bypassed, using textual tool-schema "
            "injection + our own response parsing instead (see render_manual_tools_block).")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": task},
    ]

    log(f"[worker] model={model} host={host} cwd={cwd}")
    log(f"[worker] task: {task}")

    # Only manage the local cache when dispatching against this Mac's own
    # Ollama (Unraid's has its own independent GPU + storage, nothing to
    # copy there). Detected by host, not hardcoded to localhost only, so
    # a Mac Studio reached by LAN IP still gets cache management.
    is_local_ollama = api_style == "ollama" and host in ("http://localhost:11434", "http://127.0.0.1:11434")
    if is_local_ollama:
        ensure_model_cached(model)
    # BLOCKER B2 (Fable review 2026-08-23). v7 row 31 died HERE, not in the agent
    # loop: ensure_model_ready() raised an uncaught TimeoutError after 3 x 900s
    # (~2710s) and the loop never started -- so there was never a conversation to
    # transcribe, and the crash-safe transcript writing was never implicated. In
    # the CSV it appeared as exit=1, iters=0, transcript=none, indistinguishable
    # from a mid-run crash, and it silently ate 45 minutes at the tail of a round.
    #
    # Now: warmup failure is its own outcome (load_failed), it restarts the
    # inference server and retries once, and warmup time is reported separately
    # from loop time so 2710s of failed loading is never mistaken for 2710s of work.
    warmup_started = datetime.now(timezone.utc)
    warmup_s = 0.0
    try:
        ensure_model_ready(host, model, temperature, num_ctx, top_p=top_p, top_k=top_k,
                           api_style=api_style)
    except Exception as first_exc:                                  # noqa: BLE001
        log(f"[worker] WARMUP FAILED: {type(first_exc).__name__}: {first_exc}")
        if _restart_local_ollama(host):
            log("[worker] restarted inference server, retrying warmup once ...")
            try:
                ensure_model_ready(host, model, temperature, num_ctx, top_p=top_p,
                                   top_k=top_k, api_style=api_style)
            except Exception as second_exc:                         # noqa: BLE001
                log(f"[worker] WARMUP FAILED AGAIN: {second_exc}")
                return _load_failed_result(model, host, cwd, task, second_exc,
                                           (datetime.now(timezone.utc) - warmup_started).total_seconds())
        else:
            return _load_failed_result(model, host, cwd, task, first_exc,
                                       (datetime.now(timezone.utc) - warmup_started).total_seconds())
    warmup_s = (datetime.now(timezone.utc) - warmup_started).total_seconds()
    log(f"[worker] warmup_s={warmup_s:.0f}")

    converged = False
    repeated_failures = {}   # "tool:args" -> consecutive identical failures
    loop_break_notes = []    # corrective guidance to deliver next turn
    nudge_count = 0
    MAX_NUDGES = 3  # hard ceiling regardless of progress -- see below for the
    # condition that governs whether a nudge under that ceiling is actually sent.
    empty_retries = 0
    MAX_EMPTY_RETRIES = 3  # bug 11: bounded retries for thinking-only turns
    repeated_successes = {}  # bug 13: "tool:args" -> (result_hash, consecutive identical results)
    tool_called_since_last_nudge = True  # starts True so the first nudge is
    # always allowed regardless of history, matching the original "exactly one
    # nudge" design's unconditional first attempt.
    any_mutation_called = False  # specifically write_file/edit_file, not any tool --
    # confirmed live 2026-08-22 (qwen3.8:27b-q8_0, resell-tracker-photo-upload): a
    # model can do 18 iterations of pure read_file/run_bash exploration, narrate a
    # full implementation as prose on iteration 19, get cut off mid-sentence with no
    # tool call, and stop -- because the old `any_tool_called` flag was set True by
    # the very first read_file/run_bash call in iteration 1, permanently disarming
    # the corrective-nudge safeguard below for the rest of the run. Tracking mutation
    # calls specifically (not any tool call) is what the nudge actually needs to mean
    # "the model has made real progress," not "the model has done anything at all."
    #
    # Nudge count widened from a flat one-shot to a bounded counter, confirmed live
    # 2026-08-22 (devstral:24b, resell-tracker-photo-upload): the original single
    # nudge worked -- it produced a real tool call on the very next turn -- but the
    # model then relapsed into narration two iterations later with no nudges left to
    # correct it. That's meaningfully different from opencode's infinite self-nudge
    # bug (which re-injected the same generic message forever regardless of whether
    # the model ever responded to it): here each nudge was demonstrably producing
    # real forward progress, just not durably. The `tool_called_since_last_nudge`
    # gate is what preserves the original safety property -- a model that ignores a
    # nudge outright (zero tool calls afterward) does NOT get another one, so a
    # truly stuck model still stops after one unproductive nudge, same as before.
    # Only a model demonstrating it's actually listening gets the extra budget, and
    # even that is hard-capped at MAX_NUDGES so this can never become unbounded.
    # BLOCKER B2 (Fable review 2026-08-23): the transcript used to be written only
    # AFTER the loop, so a run that crashed left none at all -- which is exactly what
    # happened to the qwen3.8 HTTP 500 rows, forcing them to be scored from the
    # console log, whose tool results are truncated to 300 chars. v7's entire
    # measurement is transcript parsing, so a crash must never cost the transcript.
    # Written at the top of every iteration: a crash during iteration N still leaves
    # everything through N-1 on disk, even on SIGKILL.
    stop_reason = None
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    _ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    log_path = LOG_DIR / f"{_ts}.json"

    def _save_transcript(iters, did_converge):
        try:
            log_path.write_text(json.dumps({
                "model": model, "host": host, "cwd": str(cwd), "task": task,
                "converged": did_converge, "iterations": iters,
                "stop_reason": stop_reason, "messages": messages,
            }, indent=2))
        except Exception as exc:                       # never let logging kill a run
            log(f"[worker] transcript write failed: {exc}")

    # Log the path BEFORE the loop. The driver scrapes this line to record the
    # transcript in the CSV, and it was only printed at the END -- so any run killed
    # by the timeout watchdog recorded `transcript,none` despite the transcript
    # existing on disk (crash-safe writing puts it there from iteration 1). v7's
    # entire measurement is transcript parsing, so a timed-out run must not lose it.
    log(f"[worker] full transcript written to {log_path}")

    i = 0
    last_prompt_tokens = 0   # exact server-reported size of the last prompt
    # Token accounting accumulator -- see the TOKEN ACCOUNTING note below.
    tok_stats = {"prompt_tokens": 0, "output_tokens": 0, "prefill_ns": 0,
                 "decode_ns": 0, "total_ns": 0, "responses": 0}
    last_msg_count = 0       # len(messages) at the moment of that report
    for i in range(1, max_iters + 1):
        log(f"[worker] --- iteration {i}/{max_iters} ---")
        _save_transcript(i - 1, converged)
        # PREDICTIVE ceiling check (Fable review 2026-08-23, follow-up to B1).
        # The reactive check below sees the prompt size only AFTER a request
        # succeeds; the crashed qwen3.8 run had observed single-iteration jumps
        # of +12,205 tokens (49% -> 86%), so a large tool result landing when
        # the context sits just under 0.90 overflows the NEXT request before
        # any reactive check runs -- the exact 500 path again. Estimate the
        # pending prompt as (last exact count) + (chars of everything appended
        # since, at a conservative ~3 chars/token) and stop BEFORE sending.
        # Only the delta is estimated, so the error is bounded and the worst
        # case is stopping slightly early with a correctly-labeled outcome.
        if last_prompt_tokens:
            # Estimate from RAW content length, not json.dumps: dumps escapes every
            # newline and quote, inflating a code-heavy delta by 20-40%. Combined
            # with a 3 chars/token divisor this fired at 32% ACTUAL usage on
            # qwen2.5-coder:14b (measured 10,346/32,768, estimated 31,874) and
            # truncated a run that had ample headroom -- a guard against losing
            # measurements should not itself destroy them.
            delta_chars = sum(len(str(m.get("content", "") or ""))
                              + len(json.dumps(m.get("tool_calls") or []))
                              for m in messages[last_msg_count:])
            est = last_prompt_tokens + int(delta_chars / 3.5)
            # Require BOTH a high estimate and a measurement already past halfway.
            # A single huge delta from a low base is far more likely an estimation
            # artifact than a real overflow, and the reactive check still catches
            # the real thing on the next turn.
            if est >= num_ctx * 0.95 and last_prompt_tokens >= num_ctx * 0.5:
                log(f"[worker] CONTEXT CEILING (predicted): last measured "
                    f"{last_prompt_tokens}/{num_ctx}, next prompt estimated ~{est} -- "
                    f"stopping before the server truncates. This is a context-size "
                    f"outcome, NOT a model failure.")
                stop_reason = "context_ceiling"
                break
        resp = call_ollama(host, model, messages, temperature, num_ctx, tools=not manual_tools,
                            top_p=top_p, top_k=top_k, api_style=api_style)
        msg = resp.get("message", {})

        # BLOCKER B1 (Fable review 2026-08-23). The HTTP 500s that destroyed the
        # qwen3.8:27b-q8_0 photo-upload cell in TWO consecutive rounds are now a
        # proven mechanism, not a hypothesis: iteration 11 ended at 31,623/32,768
        # tokens, iteration 12 overflowed, Ollama's server-side truncation dropped
        # the conversation's ONLY user message, and the model's chat template then
        # rejected the list ("no user query found in messages"). Identical error on
        # Apple Silicon, so it is nothing to do with VRAM.
        #
        # The run must stop itself BEFORE the server truncates, and the outcome must
        # be recorded as a context ceiling -- which is a fact about the task's size,
        # not a failure by the model.
        # Re-sending reasoning back to the model inflates the prompt every turn and
        # is not part of the API contract on either path. Keep it out of history;
        # the bug-11 check below still reads it from the live response. "reasoning"
        # is a llama-server/proxy alias for "reasoning_content".
        # Appended BEFORE the ceiling check so a ceiling break never drops the
        # final assistant turn from the transcript (it may carry a tool call the
        # evidence parser needs).
        msg_for_history = {k: v for k, v in msg.items()
                           if k not in ("thinking", "reasoning_content", "reasoning")}
        messages.append(msg_for_history)

        # TOKEN ACCOUNTING -- added 2026-08-23 on Fable's "now or never" note.
        # These fields exist on every Ollama response and were being thrown away;
        # once a round finishes they cannot be recovered from the transcripts.
        # Why they matter more than they look:
        #   * tokens/s is the CLEAN host-degradation measure. s/iteration -- the
        #     number the entire B3 argument rests on -- confounds host state with
        #     how verbose a model is. Tokens/s does not.
        #   * the prefill/decode split is what will explain the repomap arm's
        #     real cost, since a 317-line map is pure prefill.
        #   * it is the only route to a cost-equivalence figure ("this run cost N
        #     minutes of Studio"), which is what the offload decision rests on.
        tok_prompt = (resp.get("prompt_eval_count")
                      or (resp.get("usage") or {}).get("prompt_tokens") or 0)
        tok_out = (resp.get("eval_count")
                   or (resp.get("usage") or {}).get("completion_tokens") or 0)
        tok_stats["prompt_tokens"] += tok_prompt
        tok_stats["output_tokens"] += tok_out
        # Durations are nanoseconds on Ollama's native path; absent on OpenAI-style.
        tok_stats["prefill_ns"] += resp.get("prompt_eval_duration") or 0
        tok_stats["decode_ns"] += resp.get("eval_duration") or 0
        tok_stats["total_ns"] += resp.get("total_duration") or 0
        tok_stats["responses"] += 1

        prompt_tokens = tok_prompt
        if prompt_tokens:
            last_prompt_tokens = prompt_tokens
            last_msg_count = len(messages) - 1  # size measured BEFORE this response was appended
            log(f"[worker] context: {prompt_tokens}/{num_ctx} tokens "
                f"({100.0 * prompt_tokens / max(num_ctx, 1):.0f}%)")
            if prompt_tokens >= num_ctx * 0.90:
                log(f"[worker] CONTEXT CEILING: {prompt_tokens}/{num_ctx} -- stopping "
                    f"cleanly before server-side truncation drops the user message. "
                    f"This is a context-size outcome, NOT a model failure.")
                stop_reason = "context_ceiling"
                break

        tool_calls = msg.get("tool_calls") or []
        content = (msg.get("content") or "").strip()
        if content:
            log(f"[worker] model: {content[:500]}")

        manual_call_this_turn = False
        if not tool_calls and content:
            # Universal fallback, not gated behind --manual-tools: confirmed
            # live 2026-08-21 that qwen2.5-coder:14b has a CORRECT Ollama
            # template (proper <tools> schema injection, explicit
            # instruction to respond with <tool_call>...</tool_call> and no
            # backticks) but the model itself still sometimes ignores that
            # format and wraps the same call in a ```json fence instead --
            # Ollama's native parser only recognizes the <tool_call> tag
            # form, so tool_calls came back empty even though the model's
            # intent was clearly a real tool call. extract_manual_tool_calls
            # is tag-agnostic (finds the JSON object regardless of
            # wrapper), so it catches this for ANY model as a safety net,
            # not just the templateless models --manual-tools exists for.
            parsed_calls = extract_manual_tool_calls(content)
            if parsed_calls:
                manual_call_this_turn = True
                tool_calls = [{"function": {"name": p.get("name"), "arguments": p.get("arguments", {})}}
                              for p in parsed_calls]
                log(f"[worker] fallback-parse: recovered {len(tool_calls)} tool call(s) that "
                    f"native tool_calls missed -- {[tc['function']['name'] for tc in tool_calls]}")

        # BUG 11 FOLLOW-UP (Fable review 2026-08-23, blocker B4): the original fix
        # read only msg["thinking"], which is Ollama's native field name.
        # llama-server's OpenAI-compatible path returns reasoning in
        # "reasoning_content" instead -- so on that path the turn still looked
        # empty and the run still converged. That path is the designated
        # route-around for qwen3.8, which is a thinking model, so the exact bug
        # fixed for this model came back on the route this model needs.
        reasoning = ((msg.get("thinking") or "") + (msg.get("reasoning_content") or "")
                     + (msg.get("reasoning") or "")).strip()

        if not tool_calls and not content and reasoning:
            # BUG 11 (found 2026-08-22 by adversarial audit; fixed for v7).
            #
            # Thinking models can return a turn with content:"" and the real
            # output sitting in msg["thinking"]. This loop only ever read
            # `content` and `tool_calls`, so such a turn was indistinguishable
            # from "the model has nothing more to say" and converged the run.
            #
            # It cost a real result. qwen3.8:27b-q8_0's v6 photo-upload run
            # had just enumerated the exact remaining work and announced
            # "I'll rewrite the component for mobile/iOS and harden the upload
            # route", then emitted content:"" with thinking:"Let's actually
            # make the changes...". The harness declared convergence at
            # iteration 23/30, and it was written up as "correct diagnosis, no
            # treatment" -- a verdict about the MODEL for something the
            # HARNESS did. The model never chose to stop.
            #
            # Deliberately NOT gated on tool_called_since_last_nudge or
            # any_mutation_called: this is not the "narrated instead of
            # acting" failure those gates exist for. The model is mid-plan and
            # its output simply was not read. Bounded by its own counter, so a
            # model that only ever thinks still terminates.
            if empty_retries < MAX_EMPTY_RETRIES:
                empty_retries += 1
                think_preview = reasoning[:300]
                log(f"[worker] empty content with non-empty thinking -- NOT converging "
                    f"(retry {empty_retries}/{MAX_EMPTY_RETRIES}). thinking: {think_preview}")
                messages.append({
                    "role": "user",
                    "content": "Your last response contained no visible output and no tool "
                               "call -- only internal reasoning, which is not delivered to "
                               "anyone. Emit the tool call you were about to make, now, as "
                               "a normal response.",
                })
                continue
            log(f"[worker] empty content with thinking, retry budget exhausted "
                f"({MAX_EMPTY_RETRIES}) -- converging.")

        if not tool_calls:
            # Confirmed live 2026-08-21 (devstral:24b): a model can narrate
            # code in a fenced block instead of calling write_file, even
            # with an explicit system-prompt instruction not to. Give
            # exactly ONE corrective nudge if the response looks like a
            # narrated/summarized non-answer instead of real tool use --
            # bounded, not a loop, structurally different from opencode's
            # confirmed infinite self-nudge bug (that one re-injected a
            # generic "continue" with no new information forever; this
            # injects a specific correction once).
            #
            # Originally only fired on a code fence with no tool call, but
            # confirmed live 2026-08-21 (qwen3-coder-next) that a model can
            # also just write a plain-English summary of the files it read
            # (no fence at all) and stop having done zero edits -- same
            # underlying failure (treating description as the deliverable),
            # so the trigger is now "no mutation has happened yet at all" (see
            # any_mutation_called above for why read-only tool calls don't count).
            if (nudge_count < MAX_NUDGES and tool_called_since_last_nudge
                    and not any_mutation_called):
                nudge_count += 1
                tool_called_since_last_nudge = False
                log(f"[worker] no tool call yet and none made this response -- "
                    f"sending corrective nudge {nudge_count}/{MAX_NUDGES} instead of "
                    f"accepting it as final.")
                messages.append({
                    "role": "user",
                    "content": "You have not made any actual changes yet -- describing, "
                               "printing, or summarizing the code/files does not save "
                               "anything. Call the appropriate tool (e.g. write_file) now "
                               "to actually make the change the task asked for.",
                })
                continue
            converged = True
            log("[worker] no tool calls in response -- treating as final answer, stopping.")
            break

        tool_called_since_last_nudge = True
        # Fable review 2026-08-23: empty_retries was a LIFETIME cap, never reset,
        # so four NON-consecutive thinking-only turns anywhere in a long run would
        # silently converge it mid-plan -- bug 11 again, at lower frequency. The
        # budget exists to stop a model that only ever thinks; a model that has
        # since made real progress is not that model.
        empty_retries = 0
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name")
            if name in ("write_file", "edit_file"):
                any_mutation_called = True
            raw_args = fn.get("arguments", {})
            if isinstance(raw_args, dict):
                args = raw_args
            else:
                # On the OpenAI-compatible path EVERY native tool call arrives as a
                # JSON string, so an unwrapped json.loads meant one malformed call
                # killed the entire run -- concentrated on exactly the llama-server
                # path qwen3.8 needs. Hand the error back to the model instead.
                try:
                    args = json.loads(raw_args or "{}")
                except Exception as e:
                    log(f"[worker] unparseable tool arguments for {name}: {e}")
                    messages.append({
                        "role": "user",
                        "content": f"Your {name} call had malformed JSON arguments "
                                   f"({e}). Re-issue it with valid JSON.",
                    })
                    continue
            tool_call_id = tc.get("id")
            sig = f"{name}:{json.dumps(args, sort_keys=True)}"
            impl = tool_impls.get(name)
            if repeated_failures.get(sig, 0) >= 3:
                # HARD BLOCK. Warning alone was not enough: confirmed live
                # 2026-08-22 (qwen2.5-coder:7b, v4) that after the advisory
                # loop-break message the model retried the same dead path
                # anyway, reaching 11 identical failures and burning the run.
                # Refusing to execute is the only thing that reliably forces
                # a different action.
                result = (f"REFUSED: you have already called {name} with these exact arguments "
                          f"3+ times and it failed every time. This path does not exist. Stop "
                          f"retrying it. Call list_files on '.' to see the real structure, and "
                          f"use only paths that appeared in a list_files result.")
            elif impl is None:
                result = f"ERROR: unknown tool {name}"
            else:
                try:
                    result = impl(cwd, args)
                except Exception as e:
                    result = f"ERROR: {e}"
            args_preview = json.dumps(args)[:200]
            result_preview = str(result)[:300]
            log(f"[worker] tool {name}({args_preview}) -> {result_preview}")

            # Loop detection. Confirmed live 2026-08-22 (qwen2.5-coder:7b,
            # photo-upload): the model burned ALL 30 iterations repeating
            #   read_file app/components/ProfitCard.tsx -> not found
            #   list_files app/components/            -> not found
            # over and over. `components/` is at the repo root, not under
            # `app/` -- it had that fact from its own earlier listing and
            # never re-oriented. Nothing intervened, so a single wrong guess
            # became a total loss (0 files written). A prior run of the SAME
            # model on the SAME task wrote 5 correct files; the difference in
            # outcome was one bad turn with no recovery, which is why the
            # run-to-run variance looked so implausibly large.
            failed = str(result).startswith("ERROR") or str(result).startswith("REFUSED")
            if failed:
                repeated_failures[sig] = repeated_failures.get(sig, 0) + 1
                if repeated_failures[sig] in (3, 6, 9):
                    log(f"[worker] loop-break: {name} has failed 3x with identical args -- injecting corrective guidance.")
                    loop_break_notes.append(
                        f"You have now called {name} with exactly these arguments 3 times and it has "
                        f"failed every time: {args_preview}. That path does not exist. Do NOT call it "
                        f"again. Call list_files on '.' and on the parent directory to find where the "
                        f"file actually lives, and use only paths you have seen in a list_files result."
                    )
            else:
                repeated_failures.pop(sig, None)
                # BUG 13 (same audit): loop detection tracked FAILURES only, so
                # a model repeating an identical SUCCEEDING call was invisible
                # to it. qwen2.5-coder:7b, after correctly complying with a
                # hard-refusal, spent its remaining 17 iterations calling
                # read_file("README.md") -- succeeding every time, re-injecting
                # 14,540 chars per call. That pushed the conversation past its
                # 32,768 window around iteration 21, so every later turn ran
                # with the TASK ITSELF truncated out of context. A capability
                # verdict was nearly written from a run whose back half could
                # no longer see what it had been asked to do.
                #
                # Detection asked "is this call failing repeatedly?" when the
                # question is "is this run making progress?". Cheapest correct
                # answer: stop paying the context cost for a result the model
                # has already been given verbatim.
                #
                # Keyed on (signature, RESULT), not signature alone. Keying on
                # the call alone would be wrong and actively harmful: the
                # normal edit loop is read_file(X) -> edit_file(X) ->
                # read_file(X), where the call is identical every time and the
                # result is different every time because the file changed.
                # Suppressing that would tell a model its own edit had not
                # landed. Only a call that returns the SAME bytes it already
                # returned is genuinely redundant.
                res_key = hashlib.sha1(str(result).encode("utf-8", "replace")).hexdigest()
                prev_key = repeated_successes.get(sig, (None, 0))[0]
                if prev_key == res_key:
                    repeated_successes[sig] = (res_key, repeated_successes[sig][1] + 1)
                else:
                    repeated_successes[sig] = (res_key, 1)
                if repeated_successes[sig][1] >= 3:
                    log(f"[worker] repeat-success suppression: {name} called with identical "
                        f"args {repeated_successes[sig][1]}x with identical results -- returning a stub "
                        f"instead of the full result to stop context burn.")
                    result = (f"UNCHANGED: you have already called {name} with exactly these "
                              f"arguments {repeated_successes[sig][1]} times and the result has not "
                              f"changed. It is already in this conversation above -- scroll up "
                              f"rather than calling it again. Do something different now.")
            if manual_call_this_turn:
                # role:"tool" combined with omitting the native `tools` API
                # field is an untested combination for this model -- a
                # plain user-role result message is what was actually
                # proven to work end-to-end live 2026-08-21, so stick with
                # that instead of assuming role:"tool" also works here.
                messages.append({"role": "user", "content": f"[tool result for {name}]: {result}"})
            elif api_style == "openai":
                # OpenAI-compatible tool-result messages are keyed back to
                # their call via tool_call_id -- confirmed required (tested
                # live against llama-server 2026-08-22) for the model to
                # correctly associate the result with its own call.
                tool_msg = {"role": "tool", "content": str(result)}
                if tool_call_id:
                    tool_msg["tool_call_id"] = tool_call_id
                messages.append(tool_msg)
            else:
                messages.append({"role": "tool", "content": str(result)})

        # Deliver any loop-break guidance accumulated this turn, as a plain
        # user message so it reaches models with no tool-role template.
        if loop_break_notes:
            messages.append({"role": "user", "content": "\n\n".join(loop_break_notes)})
            loop_break_notes = []

    if stop_reason == "context_ceiling":
        log(f"[worker] STOPPED AT CONTEXT CEILING after {i} iterations -- this is a "
            f"context-size outcome, not a model failure. Do not score it as one.")
    elif not converged:
        log(f"[worker] DID NOT CONVERGE after {max_iters} iterations -- stopping, output may be incomplete.")

    _save_transcript(i, converged)
    log(f"[worker] full transcript written to {log_path}")

    # One machine-parseable line so the driver never has to re-open transcripts.
    _dec_s = tok_stats["decode_ns"] / 1e9
    _pre_s = tok_stats["prefill_ns"] / 1e9
    log("[worker] tokens prompt={} output={} prefill_s={:.1f} decode_s={:.1f} "
        "out_tps={:.1f} responses={}".format(
            tok_stats["prompt_tokens"], tok_stats["output_tokens"], _pre_s, _dec_s,
            (tok_stats["output_tokens"] / _dec_s) if _dec_s > 0 else 0.0,
            tok_stats["responses"]))

    verify_passed = None  # None = not run, distinct from False = ran and failed
    if verify:
        log(f"[worker] running verify command: {verify}")
        # FABLE BLOCKER 3 (2026-08-23): this timeout was uncaught, so a verify
        # that overran raised TimeoutExpired and CRASHED the worker after the
        # loop had already completed -- destroying an otherwise good run and
        # recording it as verify-failed. Reachable by a CORRECT solution: the
        # clamshell task requires waiting out a real expiry window, and a model
        # that picked a plausible 2-5 minute window would blow 300s honestly.
        # The task text now dictates a few-second window, and this catch means
        # an overrun is recorded as its own outcome rather than a crash.
        try:
            result = subprocess.run(verify, shell=True, cwd=cwd, capture_output=True,
                                    text=True, timeout=300)
        except subprocess.TimeoutExpired:
            log("[worker] VERIFY TIMED OUT after 300s -- the verify command did not "
                "finish. This is NOT a model result: it says nothing about whether "
                "the work was correct. Do not score it as one.")
            verify_passed = None
            log_dispatch_to_obsidian(model, task, converged, verify_passed, log_path)
            return 0 if converged else 2
        log(f"[worker] verify stdout:\n{result.stdout}")
        if result.stderr:
            log(f"[worker] verify stderr:\n{result.stderr}")
        verify_passed = result.returncode == 0
        if verify_passed:
            log("[worker] VERIFY PASSED (exit 0).")
        else:
            log(f"[worker] VERIFY FAILED (exit {result.returncode}). Do not trust this output as-is.")
    else:
        log("[worker] No --verify command given. Output has NOT been verified -- "
            "build/test it before trusting it.")

    # Always logged, per standing instruction -- not conditional on
    # success, since a failed/non-converged dispatch is exactly the data
    # worth having a record of too.
    log_dispatch_to_obsidian(model, task, converged, verify_passed, log_path)

    if is_local_ollama and cleanup_after:
        evict_model(model)

    if verify and not verify_passed:
        return 1
    return 0 if converged else 2


def main():
    ap = argparse.ArgumentParser(description="Dispatch a coding task to a local Ollama model, bypassing opencode.")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--cwd", required=True)
    ap.add_argument("--task", required=True)
    ap.add_argument("--verify", default=None, help="Shell command to run after the loop completes, e.g. 'npm run build'")
    ap.add_argument("--max-iters", type=int, default=DEFAULT_MAX_ITERS)
    ap.add_argument("--temperature", type=float, default=DEFAULT_TEMPERATURE)
    ap.add_argument("--num-ctx", type=int, default=DEFAULT_NUM_CTX)
    ap.add_argument("--top-p", type=float, default=None,
                     help="Nucleus sampling cutoff. Added 2026-08-22: both Qwen's and DeepSeek-R1's "
                          "own model cards specifically warn against temperature=0 (greedy decoding) "
                          "-- documented to cause endless-repetition failures, confirmed live as the "
                          "root cause of a real runaway-generation crash tonight. Their recommended "
                          "settings pair temperature=0.6 with top_p=0.95. Omit to leave Ollama's "
                          "default (unset here).")
    ap.add_argument("--top-k", type=int, default=None,
                     help="Top-k sampling cutoff. Qwen's recommended setting is 20, paired with the "
                          "temperature/top_p above -- see --top-p's help for why this exists.")
    ap.add_argument("--searxng-host", default=DEFAULT_SEARXNG_HOST,
                     help="Self-hosted SearXNG instance for web_search (see deploy notes in the vault -- "
                          "not yet deployed as of 2026-08-21, web_search will error until it is).")
    ap.add_argument("--system-prompt-file", default=None,
                     help="Path to a file with a custom system prompt, replacing the default. "
                          "Needed for devstral, which produces zero tool calls on this Ollama build "
                          "without its own OpenHands-scaffold system prompt.")
    ap.add_argument("--cleanup-after", action="store_true",
                     help="Evict this model from the local disk cache after the task completes "
                          "(only blobs not shared by another cached model are removed). Trades "
                          "disk space for a repeated copy-from-SMB cost on the next dispatch of "
                          "this model -- omit to keep it cached (default, recommended when disk "
                          "space isn't tight).")
    ap.add_argument("--manual-tools", action="store_true",
                     help="Bypass Ollama's native tool_calls parsing entirely: inject the tool "
                          "schemas as plain text in the system prompt and parse the model's "
                          "response for a {\"name\":...,\"arguments\":...} object ourselves. "
                          "Needed for deepseek-r1 distills (confirmed 2026-08-21: their Ollama "
                          "template never renders the native `tools` field into the prompt at "
                          "all, and native tool_calls stays null even on the community "
                          "'MFDoom/deepseek-r1-tool-calling' build -- see Ollama-Dispatch-Log.md "
                          "and github.com/ollama/ollama/issues/8517). Confirmed working live "
                          "against deepseek-r1:14b across write_file/read_file tasks including "
                          "multi-turn continuation and correct termination.")
    ap.add_argument("--api", choices=["ollama", "openai"], default="ollama",
                     help="Which API shape --host speaks. 'ollama' (default) targets Ollama's "
                          "native /api/chat and /api/tags|pull for model management. 'openai' "
                          "targets an OpenAI-compatible /v1/chat/completions endpoint (llama-server, "
                          "etc.) instead -- no model pull/discovery is attempted (the server already "
                          "has exactly one model loaded via its own -m flag), just a /health check. "
                          "Added 2026-08-22 to route around a confirmed upstream Ollama bug "
                          "('no user query found in messages', github.com/ollama/ollama/issues/17778) "
                          "that crashes qwen3.8/some other models even on trivial requests -- "
                          "llama-server renders the GGUF's own embedded chat template directly and "
                          "doesn't run Ollama's custom Go renderer code, so it doesn't hit this bug.")
    args = ap.parse_args()

    sys.exit(run_task(
        args.model, args.host, args.cwd, args.task, args.verify,
        args.max_iters, args.temperature, args.num_ctx, args.searxng_host,
        args.system_prompt_file, args.cleanup_after, args.manual_tools,
        top_p=args.top_p, top_k=args.top_k, api_style=args.api,
    ))


if __name__ == "__main__":
    main()
