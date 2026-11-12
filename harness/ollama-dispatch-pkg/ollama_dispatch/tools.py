"""Tool schema and implementations for the agentic worker.

The worker exposes a small, fixed set of tools to the model and executes them
locally. Two call conventions are supported:

  1. Native `tool_calls` -- models that emit Ollama/OpenAI-style structured
     tool calls.
  2. Manual JSON-in-content -- models that cannot emit the special tool-call
     token reliably instead print `{"name": ..., "arguments": {...}}` objects
     in plain content; `extract_manual_tool_calls` recovers those.

Web search/fetch are optional and provider-agnostic: they are enabled only when
a search backend URL is configured via env (see web.py). With none configured
they return a clear "no backend" message rather than failing the run.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

# Char caps: token-agnostic budgets so no tokenizer is needed.
READ_FILE_MAX_CHARS = 16000
BASH_TIMEOUT_S = 240

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "List files and directories under a path (relative to the working dir).",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string", "description": "Directory to list. Default '.'."}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a text file. Large files are paginated; pass 'offset' to continue.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "offset": {"type": "integer", "description": "Character offset to start from."},
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "Create or overwrite a file with the given content.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                "required": ["path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "Replace an exact substring in a file. 'old' must match uniquely.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "old": {"type": "string"},
                    "new": {"type": "string"},
                },
                "required": ["path", "old", "new"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "run_bash",
            "description": "Run a shell command in the working dir and return its output.",
            "parameters": {
                "type": "object",
                "properties": {"command": {"type": "string"}},
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web (only if a search backend is configured).",
            "parameters": {
                "type": "object",
                "properties": {"query": {"type": "string"}},
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and extract the main text of a URL.",
            "parameters": {
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "request_more_iterations",
            "description": "Ask for more iterations when close to finishing but out of budget.",
            "parameters": {
                "type": "object",
                "properties": {"reason": {"type": "string"}, "count": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "task_complete",
            "description": "Signal the task is done. Provide a short summary of what changed.",
            "parameters": {
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
            },
        },
    },
]


def render_manual_tools_block(tools: list) -> str:
    """Instruction block appended to the system prompt for models that must
    emit tool calls as plain-text JSON rather than the native token."""
    lines = [
        "You have tools. To call one, output a single JSON object on its own line:",
        '  {"name": <tool-name>, "arguments": <args-object>}',
        "",
        'Correct:   {"name": "read_file", "arguments": {"path": "app/page.py"}}',
        'INCORRECT: {"name": "app/page.py", "arguments": {"path": "app"}}',
        "",
        "Available tools:",
    ]
    for t in tools:
        fn = t["function"]
        params = ", ".join((fn.get("parameters", {}).get("properties") or {}).keys())
        lines.append(f'  - {fn["name"]}({params}): {fn["description"]}')
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Manual tool-call extraction (for non-native models)
# --------------------------------------------------------------------------

def _repair_invalid_json_escapes(s: str) -> str:
    """A model often emits invalid backslash escapes inside JSON string
    values (e.g. a literal '\\d' from a regex). Escape any backslash that is
    not part of a valid JSON escape so json.loads can parse it."""
    return re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", s)


def _coerce_tool_call(obj: dict):
    """Normalize one candidate object into {"name": .., "arguments": {..}}."""
    if not isinstance(obj, dict) or "name" not in obj:
        return None
    args = obj.get("arguments", obj.get("parameters", {}))
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except json.JSONDecodeError:
            args = {}
    if not isinstance(args, dict):
        args = {}
    return {"name": str(obj["name"]).strip(), "arguments": args}


def extract_manual_tool_calls(content: str) -> list:
    """Find every {"name": .., "arguments": ..} object in plain-text content.

    Scans for balanced-brace JSON objects and keeps the ones that coerce to a
    tool call. Returns [] when the content is a plain answer with no calls.
    """
    calls = []
    if not content or '"name"' not in content:
        return calls
    depth = 0
    start = None
    for i, ch in enumerate(content):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    chunk = content[start : i + 1]
                    obj = None
                    try:
                        obj = json.loads(chunk)
                    except json.JSONDecodeError:
                        try:
                            obj = json.loads(_repair_invalid_json_escapes(chunk))
                        except json.JSONDecodeError:
                            obj = None
                    call = _coerce_tool_call(obj) if isinstance(obj, dict) else None
                    if call:
                        calls.append(call)
                    start = None
    return calls


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------

def resolve_path(cwd: Path, path: str) -> Path:
    """Resolve a tool-supplied path inside the working dir, refusing escapes."""
    p = (cwd / path).resolve() if not Path(path).is_absolute() else Path(path).resolve()
    root = cwd.resolve()
    if root not in p.parents and p != root:
        raise ValueError(f"path escapes working directory: {path}")
    return p


def tool_list_files(cwd: Path, args: dict) -> str:
    target = resolve_path(cwd, args.get("path", "."))
    if not target.exists():
        return f"(no such path: {args.get('path', '.')})"
    if target.is_file():
        return target.name
    entries = []
    for child in sorted(target.iterdir()):
        entries.append(child.name + ("/" if child.is_dir() else ""))
    return "\n".join(entries) if entries else "(empty directory)"


def tool_read_file(cwd: Path, args: dict, max_chars: int = READ_FILE_MAX_CHARS) -> str:
    target = resolve_path(cwd, args["path"])
    if not target.exists() or not target.is_file():
        return f"(no such file: {args['path']})"
    text = target.read_text(errors="replace")
    offset = int(args.get("offset", 0) or 0)
    chunk = text[offset : offset + max_chars]
    more = offset + max_chars < len(text)
    suffix = (
        f"\n\n[truncated at {max_chars} chars; call read_file again with "
        f"offset={offset + max_chars} for more]"
        if more
        else ""
    )
    return chunk + suffix


def tool_write_file(cwd: Path, args: dict) -> str:
    target = resolve_path(cwd, args["path"])
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(args.get("content", ""))
    return f"wrote {args['path']} ({len(args.get('content', ''))} chars)"


def tool_edit_file(cwd: Path, args: dict) -> str:
    target = resolve_path(cwd, args["path"])
    if not target.exists():
        return f"(no such file: {args['path']})"
    text = target.read_text()
    old = args.get("old", "")
    if old not in text:
        return "ERROR: 'old' substring not found; file unchanged."
    if text.count(old) > 1:
        return "ERROR: 'old' substring is not unique; make it longer. File unchanged."
    target.write_text(text.replace(old, args.get("new", ""), 1))
    return f"edited {args['path']}"


def tool_run_bash(cwd: Path, args: dict, timeout: int = BASH_TIMEOUT_S) -> str:
    cmd = args.get("command", "")
    if not cmd.strip():
        return "(empty command)"
    try:
        proc = subprocess.run(
            cmd, shell=True, cwd=str(cwd), capture_output=True, text=True, timeout=timeout
        )
    except subprocess.TimeoutExpired:
        return f"ERROR: command timed out after {timeout}s"
    out = (proc.stdout or "") + (proc.stderr or "")
    return f"[exit {proc.returncode}]\n{out}".strip()


def dispatch_tool(name: str, cwd: Path, args: dict, web=None) -> str:
    """Execute a tool by name and return its string result."""
    if name == "list_files":
        return tool_list_files(cwd, args)
    if name == "read_file":
        return tool_read_file(cwd, args)
    if name == "write_file":
        return tool_write_file(cwd, args)
    if name == "edit_file":
        return tool_edit_file(cwd, args)
    if name == "run_bash":
        return tool_run_bash(cwd, args)
    if name == "web_search":
        return web.search(args.get("query", "")) if web else "(no search backend configured)"
    if name == "web_fetch":
        return web.fetch(args.get("url", "")) if web else "(no fetch backend configured)"
    return f"(unknown tool: {name})"
