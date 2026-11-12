#!/usr/bin/env bash
# PreToolUse (Write|Edit|NotebookEdit) HARD GATE: block hand-authoring of app
# source code. The authoring analog of dispatch-troubleshoot-gate.sh.
#
# The owner's rule (feedback_dispatch_coding_to_goose): qwen writes coding work by
# default, not Claude, UNLESS the edit is genuinely exempt (a physical/hardware
# step, applying an already-reviewed qwen fix, a true one-liner, or the dispatch
# mechanism itself). The prior guards were both too weak to stop the reflex:
#   - dispatch-handauthor-guard.sh only fired on files ending verify.test.ts, and
#     was non-blocking.
#   - dispatch-to-qwen-reminder.sh is PostToolUse -- it fires AFTER the code is
#     already written, so it can only nag, never prevent (2026-09-17: it "fired"
#     on a 400-line build.py that had already been hand-authored).
# A post-hoc nag cannot prevent the action. This BLOCKS the write itself.
#
# BLOCKS: a Write/Edit/NotebookEdit whose target is a CODE file inside a
#   non-exempt app repo (GitHub Projects/<repo>, or a src/lib/app/components/pages
#   dir). Deny -> the reflex is redirected to the dispatch pipeline.
#
# NOT blocked (pass through):
#   - fresh manual token ~/.claude/.qwen-manual (<6h; self-expires like the
#     troubleshoot gate's -- a stale token silently disables the gate otherwise)
#   - harness / mechanism paths (~/bin, ~/.claude, machine-config, dispatch
#     worktrees, .ollama-dispatch, bakeoff, scratchpad) -- that IS dispatch work
#   - non-code files: docs (.md/.txt), data/config (.json/.yaml/.toml/.csv),
#     markup/style (.html/.css/.svg/.xml) -- editing these is not "coding work"
#   - anything outside an app repo's source (a stray path with no repo/src signal)
#
# Bypass for a genuine exemption (reviewed qwen fix, one-liner, physical step):
#   touch ~/.claude/.qwen-manual   # self-expires in 6h; rm it when done.
input=$(cat)
python3 - "$input" <<'PY'
import json, os, re, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
tool = d.get("tool_name") or ""
if tool not in ("Write", "Edit", "NotebookEdit"):
    sys.exit(0)
ti = d.get("tool_input") or {}
fp = ti.get("file_path") or ti.get("notebook_path") or ""
if not fp:
    sys.exit(0)

# 0. Manual bypass token -> allow, but only if FRESH (same policy as the
#    troubleshoot gate: a stale token left from an earlier manual task would
#    silently disable this gate).
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

# 1. Exempt destinations: harness / dispatch-mechanism paths. Editing these IS
#    the dispatch machinery (or scratch), which is exempt.
EXEMPT = [
    "dispatch-worktrees", ".ollama-dispatch", ".claude/worktrees",
    "/bin/", "machine-config", "/.claude", "bakeoff", "scratchpad",
    "ollama-dispatch", "ollama-queue", "dispatch-cpu-runner", "local-llm-dispatch", "darkbloom", "ollama-queue-dashboard", "/tmp/", "/private/tmp/",
]
if any(tok in fp for tok in EXEMPT):
    sys.exit(0)

# 2. Is the target a CODE file? Only code authoring is gated -- docs, data,
#    config, and markup/style are not "coding work".
CODE_EXT = {
    ".py", ".js", ".jsx", ".ts", ".tsx", ".mjs", ".cjs", ".swift", ".go",
    ".rs", ".rb", ".java", ".kt", ".kts", ".scala", ".c", ".cc", ".cpp",
    ".h", ".hpp", ".m", ".mm", ".php", ".sh", ".bash", ".zsh", ".pl",
    ".lua", ".r", ".dart", ".ex", ".exs", ".vue", ".svelte",
}
ext = os.path.splitext(fp)[1].lower()
if ext not in CODE_EXT:
    sys.exit(0)

# 3. Is it inside an app repo's source? (Same signals as the troubleshoot gate.)
#    Signal A: an app repo under GitHub Projects (excluding the exempt ones).
#    Signal B: a common source dir (app/lib/components/src/pages).
APP_REPO = re.search(r"GitHub Projects(?:\.nosync)?/(?!machine-config|bakeoff)[A-Za-z0-9._-]+", fp)
SRC_DIRS = re.search(r"(?:^|/)(?:app|lib|components|src|pages)(?:/)", fp)
if not (APP_REPO or SRC_DIRS):
    sys.exit(0)

# 4. BLOCK.
reason = (
    "DISPATCH GATE (hard): you're about to HAND-AUTHOR code (" + os.path.basename(fp) + "), "
    "which is qwen's job by default (memory: feedback_dispatch_coding_to_goose). "
    "Maximize offload -- qwen writes the cases AND the fix; you write only the "
    "reference impl + review.\n\n"
    "DEFAULT = mini-swe (do not hand-write the file):\n"
    "  ~/bin/mini-spec --repo <path> --base origin/main   # writes <dir>/spec.json\n"
    "  ~/bin/mini-validate <dir>\n"
    "  ~/bin/mini-submit <dir>/spec.json --label <area-change> \\\n"
    "      --model qwen3.8:27b-q4_K_M --bundle <b>\n"
    "  Diagnose a failing/odd job with ~/bin/mini-triage.\n\n"
    "If this edit is genuinely EXEMPT (applying an already-reviewed qwen fix, a true\n"
    "one-liner, a physical/hardware step, or the dispatch mechanism itself):\n"
    "  touch ~/.claude/.qwen-manual   # self-expires in 6h; rm it when done, then retry.\n"
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
