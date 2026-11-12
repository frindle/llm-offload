"""The agent's tool surface, and the parser that recovers tool calls models
emit as plain text.

Two things in this file are load-bearing for measurement validity:

1. `read_file` is CAPPED. An uncapped read is the single cheapest way to void a
   whole benchmark cell: one oversized file can consume an entire context
   window in one turn, after which every subsequent iteration is fighting for
   room and the run terminates at the iteration ceiling having learned nothing.
   That is an instrument defect that reads in the results table as model
   incompetence. The cap is configurable and the truncation is announced to the
   model, so it can decide to read a narrower slice.

2. `extract_manual_tool_calls` exists because several open-weight model families
   never emit the special-token sequence a native tool-call parser requires —
   their chat templates do not render the `tools` field into the prompt at all.
   Those models call tools perfectly well as JSON in the message body. A harness
   that only reads the structured field scores them as having done nothing,
   across every row, while their calls are sitting in plain sight in the
   transcript. That is the most expensive class of measurement bug available:
   it is silent, it is systematic, and it looks exactly like a finding.

The repairs in the parser are not defensive padding. Each one corresponds to an
observed emission that was otherwise discarded whole, turning real work into a
`files_changed=0` row.
"""
from __future__ import annotations

import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

SKIP_DIRS = {".git", "node_modules", ".next", ".build", "__pycache__",
             ".venv", "venv", "dist", "build", ".mypy_cache", ".pytest_cache"}

SYSTEM_PROMPT = """You are a focused coding agent. Use your tools to \
accomplish the task directly -- don't describe what you would do, actually \
call the tools.

ALWAYS start by calling list_files on '.' to see the project's real \
structure, then read the files that matter, BEFORE writing anything. Never \
guess a file path, and never assume a framework or directory layout -- \
discover it. The project's existing stack and conventions are whatever \
list_files and read_file actually show you, not what a project like this \
usually looks like. Prefer edit_file over write_file when changing an \
existing file.

If AGENTS.md, README.md or CONTRIBUTING.md exist at the top \
level, read them before writing any code. They state this project's \
conventions, which may deliberately differ from what you learned in \
training. Follow them over your own habits.

Before you CREATE a new file, read an existing file of the same kind in \
this project and match its idiom exactly -- its imports, its export style, \
its function signatures, its naming. A framework often has several valid \
styles from different versions; the only one that works here is the one \
already in use.

If you import a package, check that it is actually a dependency of this \
project first. Code that imports something the project does not have will \
fail to build.

If a path you tried does not exist, do not try it again. Call list_files \
to find where the file actually is, and only use paths you have seen in a \
list_files result.

When the task is fully complete, respond with a final plain-text summary \
and no further tool calls. Keep file paths relative to the working \
directory. Do not ask clarifying questions; make reasonable assumptions \
and proceed."""


def tool_schemas(web_enabled: bool) -> list[dict]:
    fns = [
        {
            "name": "list_files",
            "description": "List the files and subdirectories at a path. Use this FIRST to discover the project's structure before reading or writing anything. Pass '.' for the working directory.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Directory path relative to the working directory. Use '.' for the working directory itself."}},
                "required": ["path"]},
        },
        {
            "name": "read_file",
            "description": "Read a file's contents. Very large files are truncated; the result says so if it was.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path relative to the working directory."},
                "start_line": {"type": "integer", "description": "Optional 1-based first line to return."},
                "max_lines": {"type": "integer", "description": "Optional maximum number of lines to return."}},
                "required": ["path"]},
        },
        {
            "name": "write_file",
            "description": "Create a NEW file, or fully overwrite an existing one. Creates parent directories as needed. For an existing file where you are only changing part of it, use edit_file instead -- write_file forces you to regenerate the entire file in one response, which is slow and error-prone for anything but a small or brand-new file.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path relative to the working directory."},
                "content": {"type": "string", "description": "Full file content to write."}},
                "required": ["path", "content"]},
        },
        {
            "name": "edit_file",
            "description": "Replace one exact occurrence of old_string with new_string in an existing file. old_string must match the file's current content exactly, including whitespace, and must be unique in the file; include a line or two of surrounding context if the change itself is a common line.",
            "parameters": {"type": "object", "properties": {
                "path": {"type": "string", "description": "Path relative to the working directory."},
                "old_string": {"type": "string", "description": "Exact existing text to replace, unique within the file."},
                "new_string": {"type": "string", "description": "Text to replace it with."}},
                "required": ["path", "old_string", "new_string"]},
        },
        {
            "name": "run_bash",
            "description": "Run a shell command in the working directory and return stdout, stderr and exit code.",
            "parameters": {"type": "object", "properties": {
                "command": {"type": "string", "description": "Shell command to run."}},
                "required": ["command"]},
        },
    ]
    if web_enabled:
        fns += [
            {"name": "web_search",
             "description": "Search the web. Returns the top results as title/url/snippet. For external documentation only.",
             "parameters": {"type": "object", "properties": {
                 "query": {"type": "string", "description": "Search query."}}, "required": ["query"]}},
            {"name": "web_fetch",
             "description": "Fetch a URL and return its text content, truncated if long.",
             "parameters": {"type": "object", "properties": {
                 "url": {"type": "string", "description": "Full URL to fetch."}}, "required": ["url"]}},
        ]
    return [{"type": "function", "function": f} for f in fns]


def render_manual_tools_block(tools: list[dict]) -> str:
    """Textual tool-schema injection, for models whose chat template never
    renders the native `tools` field into the prompt.

    This sidesteps the backend's structured tool-call parser entirely: describe
    the tools as plain text and parse the model's reply here instead. Without
    it, an affected model has no way of knowing tools exist and will narrate
    imaginary work — which is not a fabrication finding, it is a harness bug.
    """
    lines = [json.dumps({"type": "function", "function": t.get("function", t)})
             for t in tools]
    valid = ", ".join(t.get("function", t)["name"] for t in tools)
    return f"""# Tools

You may call one function per response. You are provided with function \
signatures within <tools></tools> XML tags:
<tools>
{chr(10).join(lines)}
</tools>

For each function call, return ONLY a JSON object with the function name and \
arguments within <tool_call></tool_call> XML tags -- nothing else:
<tool_call>
{{"name": <function-name>, "arguments": <args-json-object>}}
</tool_call>

CRITICAL: the "name" field must be EXACTLY one of these literal tool names:
  {valid}
Never put a file path or anything else in "name" -- paths go inside "arguments".
Correct:   {{"name": "read_file", "arguments": {{"path": "src/main.py"}}}}
INCORRECT: {{"name": "src/main.py", "arguments": {{"path": "src"}}}}

When the task is fully done and no more function calls are needed, respond \
with a normal plain-text message and NOT a <tool_call> block."""


# ---------------------------------------------------------------------------
# Parsing tool calls out of plain text
# ---------------------------------------------------------------------------

_VALID_JSON_ESCAPES = set('"\\/bfnrtu')


def _repair_invalid_json_escapes(s: str) -> str:
    """Double any backslash that is not already a valid JSON escape.

    Models copying shell snippets into an argument string reproduce escapes
    like `\\(` verbatim without doubling them for JSON, and strict json.loads
    rejects the whole object.

    Walked character by character rather than done with a regex substitution:
    a single-character lookahead misreads an ALREADY-valid two-character escape
    such as `\\.` — it inspects the second backslash in isolation, sees an
    ordinary character after it, and "repairs" something that was correct. This
    consumes valid pairs whole so they are never re-examined.
    """
    out: list[str] = []
    i, n = 0, len(s)
    while i < n:
        c = s[i]
        if c == "\\" and i + 1 < n:
            if s[i + 1] in _VALID_JSON_ESCAPES:
                out.append(s[i:i + 2])
                i += 2
                continue
            out.append("\\\\")
            i += 1
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _escape_raw_control_chars(s: str) -> str:
    """Escape real newlines/tabs/returns appearing inside JSON string literals.

    A model can escape its quotes correctly and still emit literal newline
    bytes inside a `content` string. JSON forbids raw control characters in
    string literals, so the object fails to parse and the tool call is thrown
    away — after which the harness treats an attempted file write as the
    model's final answer, stops, and the verify command runs against an
    untouched tree. That combination reads as a clean pass with zero files
    changed, which is the worst possible failure: wrong, and quiet.
    """
    out: list[str] = []
    in_str = False
    esc = False
    for c in s:
        if in_str:
            if esc:
                out.append(c)
                esc = False
                continue
            if c == "\\":
                out.append(c)
                esc = True
                continue
            if c == '"':
                in_str = False
                out.append(c)
                continue
            if c == "\n":
                out.append("\\n")
            elif c == "\r":
                out.append("\\r")
            elif c == "\t":
                out.append("\\t")
            else:
                out.append(c)
            continue
        if c == '"':
            in_str = True
        out.append(c)
    return "".join(out)


_TRIPLE_QUOTE_RE = re.compile(r'(:\s*)"""(.*?)"""(?=\s*\}\s*\})', re.DOTALL)
_CALL_ANCHOR_RE = re.compile(
    r'\{.*?"name"\s*:.*?"arguments"\s*:|\{.*?"arguments"\s*:.*?"name"\s*:',
    re.DOTALL)


def extract_manual_tool_calls(content: str) -> list[dict]:
    """Find every {"name": ..., "arguments": ...} object in plain model output.

    Tag-agnostic on purpose: models are inconsistent about whether they wrap
    the object in <tool_call>, in a ```json fence, or in nothing at all, while
    the object itself comes back correctly schema'd. Matching on the object
    rather than the wrapper is what makes this robust.

    Returns a LIST. Despite instructions to emit one call per response, models
    routinely emit several back to back — write two files, then run a command.
    Taking only the first silently discards work the model actually did.
    """
    # Pre-pass: normalise Python-style triple-quoted values into JSON strings.
    # A model can emit a correct call, at a correct path, with `\"\"\"` where
    # JSON needs `"`. This must run BEFORE the brace scan, because to a
    # string-aware scanner `"""` opens, closes and reopens a string, so the
    # object's extent would be found wrong too. Anchored on the closing `"""`
    # being followed by the object's closing braces, so a legitimate `"""`
    # inside the content being written does not terminate the match early.
    content = _TRIPLE_QUOTE_RE.sub(
        lambda m: m.group(1) + json.dumps(m.group(2)), content)

    calls: list[dict] = []
    pos = 0
    while True:
        m = _CALL_ANCHOR_RE.search(content, pos)
        if not m:
            break
        start = m.start()

        # String-aware brace matching. A naive depth counter also counts braces
        # inside JSON string literals -- i.e. inside the file content the model
        # is writing -- so any unbalanced brace in generated code ends the
        # candidate at the wrong offset and the whole call is dropped.
        depth, end, in_str, esc = 0, None, False, False
        for i in range(start, len(content)):
            c = content[i]
            if in_str:
                if esc:
                    esc = False
                elif c == "\\":
                    esc = True
                elif c == '"':
                    in_str = False
                continue
            if c == '"':
                in_str = True
            elif c == "{":
                depth += 1
            elif c == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        if end is None:
            break

        candidate = content[start:end + 1]
        parsed = None
        try:
            parsed = json.loads(candidate)
        except Exception:                                            # noqa: BLE001
            for repair in (_repair_invalid_json_escapes,
                           _escape_raw_control_chars,
                           lambda s: _escape_raw_control_chars(
                               _repair_invalid_json_escapes(s))):
                try:
                    parsed = json.loads(repair(candidate))
                    break
                except Exception:                                    # noqa: BLE001
                    continue

        if isinstance(parsed, dict) and "name" in parsed and "arguments" in parsed:
            calls.append(parsed)
            pos = end + 1
        elif parsed is None:
            pos = end + 1
        else:
            # The object parsed but is a WRAPPER around the call --
            # {"tool_call": {...}} or {"function": {...}}. Skipping past it
            # would step over the real call nested inside, so rescan from just
            # inside this object instead.
            pos = start + 1
    return calls


# ---------------------------------------------------------------------------
# Implementations
# ---------------------------------------------------------------------------

def resolve_path(cwd: Path, path: str) -> Path:
    p = (cwd / (path or ".")).resolve()
    root = cwd.resolve()
    if p != root and root not in p.parents:
        raise ValueError(f"path escapes working directory: {path}")
    return p


def _fix_literal_escapes(content: str) -> str:
    """Some models emit file content with literal two-character `\\n` sequences
    instead of newline bytes — a generation quirk, not a parsing bug.

    Applied only when the content has ZERO real newlines and at least one
    literal `\\n`. A file that already contains real newlines alongside a few
    genuinely intended backslash sequences (a regex, say) is left alone; fixing
    those would mangle correct output from models that do not have the quirk.
    """
    if "\n" not in content and "\\n" in content:
        content = (content.replace("\\r\\n", "\n").replace("\\n", "\n")
                          .replace("\\t", "\t").replace('\\"', '"'))
    return content


class Toolbox:
    def __init__(self, cwd: Path, *, read_max_chars: int = 60000,
                 bash_timeout_s: int = 180, web: dict | None = None):
        self.cwd = Path(cwd)
        self.read_max_chars = int(read_max_chars)
        self.bash_timeout_s = int(bash_timeout_s)
        self.web = web or {"enabled": False}

    # -- discovery ----------------------------------------------------------
    def list_files(self, args: dict) -> str:
        p = resolve_path(self.cwd, args.get("path") or ".")
        if not p.exists():
            return f"ERROR: path not found: {args.get('path')}"
        if not p.is_dir():
            return f"ERROR: not a directory: {args.get('path')} (use read_file for files)"
        entries = []
        try:
            for child in sorted(p.iterdir(), key=lambda c: (not c.is_dir(), c.name)):
                if child.name in SKIP_DIRS:
                    entries.append(f"{child.name}/  (skipped: large/generated)")
                elif child.is_dir():
                    entries.append(f"{child.name}/")
                else:
                    entries.append(f"{child.name}  ({child.stat().st_size} bytes)")
        except Exception as e:                                       # noqa: BLE001
            return f"ERROR listing directory: {e}"
        return "\n".join(entries) if entries else "(empty directory)"

    def read_file(self, args: dict) -> str:
        p = resolve_path(self.cwd, args["path"])
        if not p.exists():
            return (f"ERROR: file not found: {args['path']} -- use list_files "
                    f"to see what exists.")
        if p.is_dir():
            # Do not dead-end on a bare "is a directory" errno with no hint
            # about what to do next; show the listing instead.
            return (f"ERROR: {args['path']} is a directory, not a file. "
                    f"Listing it instead:\n" + self.list_files({"path": args["path"]}))
        try:
            text = p.read_text(errors="replace")
        except Exception as e:                                       # noqa: BLE001
            return f"ERROR reading file: {e}"

        start = max(1, int(args.get("start_line") or 1))
        max_lines = args.get("max_lines")
        if start > 1 or max_lines:
            lines = text.splitlines()
            end = start - 1 + int(max_lines) if max_lines else len(lines)
            text = "\n".join(lines[start - 1:end])

        # THE CAP. An uncapped read of one oversized file is enough to consume
        # a whole context window in a single turn and void every iteration that
        # follows. Truncation is announced so the model can ask for a slice
        # rather than silently reasoning over a fragment it thinks is whole.
        if len(text) > self.read_max_chars:
            shown = text[:self.read_max_chars]
            n_lines = shown.count("\n") + 1
            return (shown + f"\n\n[TRUNCATED: this file is {len(text)} characters; "
                    f"only the first {self.read_max_chars} ({n_lines} lines) are "
                    f"shown. Re-read with start_line/max_lines, or use run_bash "
                    f"with grep, to see a specific region.]")
        return text

    # -- mutation -----------------------------------------------------------
    def write_file(self, args: dict) -> str:
        p = resolve_path(self.cwd, args["path"])
        p.parent.mkdir(parents=True, exist_ok=True)
        content = _fix_literal_escapes(args["content"])
        p.write_text(content)
        return f"OK: wrote {len(content)} bytes to {args['path']}"

    def edit_file(self, args: dict) -> str:
        """Targeted find/replace.

        This tool exists because forcing a model to re-emit an entire long file
        to change one line is a known pathology: a model asked to do that has
        been observed generating tens of thousands of tokens without stopping,
        through two context-window shifts, before being killed. Giving it a
        small-diff tool is the fix; a longer timeout is not.
        """
        p = resolve_path(self.cwd, args["path"])
        if not p.exists():
            return f"ERROR: file not found: {args['path']}"
        try:
            content = p.read_text()
        except Exception as e:                                       # noqa: BLE001
            return f"ERROR reading file: {e}"
        old = _fix_literal_escapes(args["old_string"])
        new = _fix_literal_escapes(args["new_string"])
        count = content.count(old)
        if count == 0:
            return (f"ERROR: old_string not found in {args['path']} -- it must match "
                    f"the file's current exact content, including whitespace. "
                    f"Re-read the file if unsure.")
        if count > 1:
            return (f"ERROR: old_string matches {count} locations in {args['path']} "
                    f"-- it must be unique. Include a line or two of surrounding "
                    f"context to disambiguate.")
        p.write_text(content.replace(old, new, 1))
        return f"OK: replaced 1 occurrence in {args['path']}"

    def run_bash(self, args: dict) -> str:
        try:
            r = subprocess.run(args["command"], shell=True, cwd=self.cwd,
                               capture_output=True, text=True,
                               timeout=self.bash_timeout_s)
            return json.dumps({"exit_code": r.returncode,
                               "stdout": r.stdout[-4000:],
                               "stderr": r.stderr[-4000:]})
        except subprocess.TimeoutExpired:
            return f"ERROR: command timed out after {self.bash_timeout_s}s"
        except Exception as e:                                       # noqa: BLE001
            return f"ERROR running command: {e}"

    # -- web (off by default) ------------------------------------------------
    def web_search(self, args: dict) -> str:
        endpoint = (self.web or {}).get("search_endpoint") or ""
        if not endpoint:
            return "ERROR: web_search is not configured."
        url = endpoint.rstrip("/") + "/search?" + urllib.parse.urlencode(
            {"q": args["query"], "format": "json"})
        try:
            req = urllib.request.Request(url, headers={"Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=self.web.get("timeout_s", 30)) as resp:
                data = json.loads(resp.read())
        except Exception as e:                                       # noqa: BLE001
            return f"ERROR: web_search failed ({e})."
        results = (data.get("results") or [])[:5]
        if not results:
            return "No results found."
        return "\n".join(
            f"- {r.get('title','')}\n  {r.get('url','')}\n  {(r.get('content') or '')[:300]}"
            for r in results)

    def web_fetch(self, args: dict) -> str:
        limit = int((self.web or {}).get("fetch_max_chars", 20000))
        try:
            req = urllib.request.Request(args["url"], headers={"User-Agent": "adh"})
            with urllib.request.urlopen(req, timeout=self.web.get("timeout_s", 30)) as resp:
                text = resp.read().decode("utf-8", errors="replace")
        except Exception as e:                                       # noqa: BLE001
            return f"ERROR: web_fetch failed: {e}"
        if len(text) > limit:
            text = text[:limit] + f"\n\n[TRUNCATED at {limit} chars]"
        return text

    def dispatch(self, name: str, args: dict) -> str:
        fn = {
            "list_files": self.list_files, "read_file": self.read_file,
            "write_file": self.write_file, "edit_file": self.edit_file,
            "run_bash": self.run_bash,
            "web_search": self.web_search, "web_fetch": self.web_fetch,
        }.get(name)
        if fn is None:
            return (f"ERROR: unknown tool {name!r}. Valid tools: list_files, "
                    f"read_file, write_file, edit_file, run_bash.")
        try:
            return fn(args or {})
        except KeyError as e:
            return f"ERROR: {name} is missing required argument {e}"
        except ValueError as e:
            return f"ERROR: {e}"
