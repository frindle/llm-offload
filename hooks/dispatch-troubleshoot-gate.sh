#!/usr/bin/env bash
# PreToolUse (Bash|Grep|Glob) HARD GATE: block hand-troubleshooting of app source.
#
# The owner's rule (2026-09-08): qwen should do the troubleshooting, not Claude, unless
# The owner explicitly says to do it manually. Advisory reminders don't reliably fire
# (proven this session), so this BLOCKS the reflex at its start: investigation-
# shaped Bash (recursive greps / source-file reads) into an app repo's source is
# denied unless a dispatch is clearly the vehicle or the owner has authorised manual.
#
# Extended (2026-09-17): the Grep and Glob TOOLS are also gated. Those tools ARE
# a recursive search -- there is no "investigation shape" sub-test for them: a
# Grep/Glob whose target is non-exempt app source is inherently a spelunk and is
# denied. Single targeted Read-tool reads stay allowed (Read is NOT gated).
#
# It gates the READ phase specifically -- writing the dispatch spec still needs to
# know entry points, so single targeted reads are allowed; it's the spelunk
# (grep -r/rg across app source, chained cat/sed/head/tail of source files) that
# is blocked.
#
# NOT blocked (pass through):
#   - anything when the manual token ~/.claude/.qwen-manual exists
#   - paths under a dispatch worktree (~/dispatch-worktrees, ~/.ollama-dispatch,
#     .claude/worktrees) or harness paths (~/bin, ~/.claude, machine-config) --
#     that IS dispatch / mechanism work, which is exempt
#   - curl/API data pulls, git, ls, queue status, and other non-investigation cmds
#   - a single-file read (one sed/cat/head/tail target) -- spec research is allowed
#
# Bypass: `touch ~/.claude/.qwen-manual` (I do this only when the owner says "manual"),
# and `rm` it when the manual task is done.
input=$(cat)
python3 - "$input" <<'PY'
import json, os, re, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
tool = d.get("tool_name") or ""
if tool not in ("Bash", "Grep", "Glob"):
    sys.exit(0)
ti = d.get("tool_input") or {}
cmd = ti.get("command") or ""

# 0. Manual bypass token -> allow everything, BUT only if FRESH. A token left
#    behind from an earlier manual task silently disables this whole gate until
#    someone notices -- exactly what happened 2026-09-14: a Sep-12 token killed
#    the gate for an entire session of hand-grepping. Treat a token older than
#    MANUAL_TTL_SECS as stale: delete it and do NOT bypass.
_tok = os.path.expanduser("~/.claude/.qwen-manual")
MANUAL_TTL_SECS = 6 * 3600
if os.path.exists(_tok):
    import time
    if time.time() - os.path.getmtime(_tok) <= MANUAL_TTL_SECS:
        sys.exit(0)
    try:
        os.remove(_tok)
    except OSError:
        pass

# Build the probe string that the EXEMPT + touches_source tests inspect.
#   - Bash: the raw command.
#   - Grep/Glob: the search target -- its `path` param (or the hook's cwd when
#     `path` is absent), plus `pattern`/`glob` so a src-dir named in those is
#     caught too. The leading space after path lets SRC_DIRS match a path that
#     ends in .../app with no trailing slash.
if tool == "Bash":
    probe = cmd
else:
    _path = ti.get("path") or d.get("cwd") or ""
    _pat = ti.get("pattern") or ""
    _glob = ti.get("glob") or ""
    probe = " ".join([_path, _glob, _pat])

low = probe

# 1. Exempt destinations: dispatch worktrees + harness/mechanism paths. If the
#    command/target references any of these, it's dispatch/mechanism work -> allow.
EXEMPT = [
    "dispatch-worktrees", ".ollama-dispatch", ".claude/worktrees",
    "/bin/", "machine-config", "/.claude", "bakeoff", "scratchpad",
    "ollama-dispatch", "ollama-queue", "dispatch-cpu-runner", "local-llm-dispatch", "darkbloom", "ollama-queue-dashboard",
]
if any(tok in low for tok in EXEMPT):
    sys.exit(0)

# 2. Is this command even touching an app repo's SOURCE?
#    Signal A: names an app repo under GitHub Projects (not the exempt ones).
#    Signal B: greps/reads common source dirs (app/ lib/ components/ src/ pages/).
APP_REPO = re.search(r"GitHub Projects(?:\.nosync)?/(?!machine-config|bakeoff)[A-Za-z0-9._-]+", probe)
SRC_DIRS = re.search(r"(?:^|\s|/)(?:app|lib|components|src|pages)(?:/|\s|$)", probe)
touches_source = bool(APP_REPO) or bool(SRC_DIRS)
if not touches_source:
    sys.exit(0)

# 3. Investigation shape?
#    Grep/Glob: the TOOL is itself a recursive search of source -- there is no
#    shape sub-test, so a non-exempt source target falls straight through to the
#    BLOCK below. (Read-tool single reads are not gated at all.)
#    Bash: only investigation-shaped commands are blocked --
#    - recursive/multi-file search: grep -r / -rn / -rln, ripgrep `rg`
#    - reading source: cat/sed -n/head/tail/awk/less  (count targets; a single
#      targeted read for spec research is allowed, a spelunk of many is not)
if tool == "Bash":
    # The -r flag must be its OWN argv token (preceded by whitespace, a run of
    # short-flag letters, ending at whitespace/end). `-[A-Za-z]*r` alone also
    # matched the "-r" inside hyphenated words such as a path segment
    # "sync-reservations", false-blocking a single-file grep (2026-09-23).
    recursive = bool(re.search(
        r"\bgrep\b[^|;&]*(?:^|\s)-[A-Za-z]*r[A-Za-z]*(?=\s|$)|\brg\b", cmd))
    # A `cat > f` / `cat >> f` (heredoc or redirect) is a file WRITE, not a
    # read: creating a file that merely NAMES a src/ path is file-creation, not
    # a spelunk. Strip write-redirect cats before counting so a heredoc build
    # (e.g. `cat > app/x.py <<EOF ...`) isn't misread as source-reading and
    # false-blocked. A `cat file > out` (read of file, write to out) has a token
    # between cat and `>`, so it is NOT stripped and still counts as a read.
    _reads = re.sub(r"\bcat\b\s*>>?", " ", cmd)
    _reads = re.sub(r"\|\s*(?:head|tail)(?:\s+-[A-Za-z0-9]+(?:\s+\d+)?)*\s*(?=$|[|;&)])", " ", _reads)
    read_tools = re.findall(r"\b(?:cat|sed\s+-n|head|tail|awk|less)\b", _reads)
    many_reads = len(read_tools) >= 2 or ("*" in cmd and read_tools)  # globbed reads count as many

    # 2026-11: read-only searching/grepping of app source via Bash is no longer
    # hard-blocked (hand-authoring edits stay gated by the handauthor gate).
    # Grep/Glob TOOL calls remain gated.
    sys.exit(0)

# 4. BLOCK.
_via = {
    "Grep": "a Grep of application source (the Grep tool IS a recursive search)",
    "Glob": "a Glob over application source (the Glob tool IS a recursive search)",
}.get(tool, "hand-troubleshooting of application source")
reason = (
    "DISPATCH GATE (hard): this is " + _via + ", "
    "which is qwen's job by default (the owner, 2026-09-08). Maximize offload -- "
    "qwen writes the cases AND the fix; you write only the reference impl + review.\n\n"
    "DEFAULT = mini-swe. Do not hand-grep, do not hand-scaffold:\n"
    "  1. Confirm the SYMPTOM only (a data/API pull or ONE targeted read is fine).\n"
    "  2. ~/bin/mini-spec --repo <path> --base origin/main   # writes <dir>/spec.json\n"
    "     ~/bin/mini-validate <dir>\n"
    "     ~/bin/mini-submit <dir>/spec.json --label <area-change> \\\n"
    "        --model qwen3.8:27b-q4_K_M --bundle <b>\n"
    "  3. Diagnose a failing/odd job with ~/bin/mini-triage.\n\n"
    "If the owner explicitly said to do it MANUALLY this time:\n"
    "  touch ~/.claude/.qwen-manual   # then retry; rm it when the manual task is done.\n"
)
print(json.dumps({
    "hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": reason,
    }
}))
PY
exit 0