#!/usr/bin/env bash
# PreToolUse (Bash) HARD GATE: block hand-rolling a dispatch worktree.
#
# The owner's rule (2026-09-12): "how do we stop doing that?" -- the recurring reflex
# is to `git worktree add` a scratch worktree by hand before a dispatch, instead
# of letting mini-swe (`~/bin/mini-spec --repo <path> --base origin/main`) cut + isolate
# it. Advisory memories have not changed the reflex, so this BLOCKS the manual
# `git worktree add` at its start.
#
# Why this is safe:
#   - mini-spec creates its worktree via a subprocess git call, which does NOT
#     pass through the Bash TOOL, so it is unaffected. Targets under
#     ~/.ollama-dispatch/worktrees, ~/.ollama-dispatch/mini-work, ~/dispatch-worktrees
#     and any command involving mini-spec are allowed.
#   - The harness's own EnterWorktree tool and Agent isolation:"worktree" are
#     separate tools, not `git worktree add`, so general isolation still works.
#   - Only the literal, hand-typed `git worktree add` is denied.
#
# Bypass (rare, legitimate manual worktree): `touch ~/.claude/.worktree-manual`
# (also honours the shared ~/.claude/.qwen-manual token); rm it when done.
input=$(cat)
python3 - "$input" <<'PY'
import json, os, re, sys
try:
    d = json.loads(sys.argv[1])
except Exception:
    sys.exit(0)
if (d.get("tool_name") or "") != "Bash":
    sys.exit(0)
cmd = (d.get("tool_input") or {}).get("command") or ""

# Bypass tokens.
for tok in ("~/.claude/.worktree-manual", "~/.claude/.qwen-manual"):
    if os.path.exists(os.path.expanduser(tok)):
        sys.exit(0)

# Only fire on a hand-typed `git worktree add`.
if not re.search(r"\bgit\b[^\n|;&]*\bworktree\s+add\b", cmd):
    sys.exit(0)

# Allow mini-swe flows.
home = os.path.expanduser("~")
if "mini-spec" in cmd:
    sys.exit(0)
for sub in ("/.ollama-dispatch/worktrees", "/.ollama-dispatch/mini-work", "/dispatch-worktrees"):
    for pre in (home, "~", "$HOME"):
        if pre + sub in cmd:
            sys.exit(0)

# Allow worktree management that is NOT creation (remove/prune/list/move/repair).
# (Those don't contain "worktree add", so they already fell through above.)

reason = (
    "DISPATCH WORKTREE GATE (hard): don't hand-roll a dispatch worktree with "
    "`git worktree add`. mini-swe cuts + isolates it for you:\n\n"
    "  ~/bin/mini-spec --repo <path> --base origin/main\n\n"
    "or make an isolated copy with `git clone --no-local <repo> ~/dispatch-worktrees/<name>`.\n"
    "Then mini-validate -> mini-submit <dir>/spec.json --label <area-change> "
    "--model qwen3.8:27b-q4_K_M --bundle <b>.\n\n"
    "If you genuinely need a manual worktree (not for a dispatch):\n"
    "  - prefer the EnterWorktree tool or Agent isolation:\"worktree\"; or\n"
    "  - touch ~/.claude/.worktree-manual   # then retry; rm it when done.\n"
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
