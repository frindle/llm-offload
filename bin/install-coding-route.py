#!/usr/bin/env python3
"""install-coding-route.py [--uninstall] [--dry-run]

Changes the coding-routing nudge in ~/.claude/hooks/dispatch-to-qwen-reminder.sh (PostToolUse on Edit|Write of a
code file). Before: "qwen should write coding work by default". After: bug fixes that start from an issue or a
symptom go to a Sonnet agent; the local model only gets work whose check exists before the job; small edits stay
direct. Why (measured 2026-11-13, ~/offload-research/results-2026-11-13/coding-lane-net.md): on the owner's own projects a
local fix cost at least $5.95 of Claude per landed fix vs about $2.3-3.8 via Sonnet, and about 3 in 10 reviewed local
passes hid a real bug the test missed.

Only the one additionalContext line changes, plus a dated comment. The file is backed up first (<file>.bak-route).
Running it twice changes nothing. --uninstall restores the backup. --dry-run prints what would change and writes
nothing. HOME can be overridden for tests."""
import argparse, os, shutil
from pathlib import Path

HOME = Path(os.environ.get("HOME"))
HOOK = HOME / ".claude" / "hooks" / "dispatch-to-qwen-reminder.sh"
BAK = ".bak-route"
OLD_PREFIX = '    additionalContext: ("DISPATCH REMINDER: just edited " + $path + " directly. Standing rule'
MARK = "ROUTING REMINDER"
# jq string inside a single-quoted bash argument: no single quotes, no backslashes; $ is literal in jq strings.
NEW_LINE = (
    '    additionalContext: ("ROUTING REMINDER: just edited " + $path + " directly. Coding routing since 2026-11-13 '
    "(measured; see ~/Desktop/Claude Plans/Local LLM Usage Plan.md): (1) a bug fix that starts from an issue, an error "
    "report or a symptom goes to a Sonnet agent (fixer-sonnet, or the ladder skill), not to the local model: on these "
    "projects a local fix cost at least $5.95 of Claude per landed fix vs about $2.3-3.8 via Sonnet, and about 3 in 10 "
    "local passes hid a real bug; (2) the local model (qwen-dispatch.sh) gets only work whose check exists before the "
    "job: an existing failing test to make pass, lint, type or format errors, or a refactor that existing tests "
    "cover; every 5th local pass gets a spot audit, and if more than 1 in 20 audited passes has a real bug, stop "
    "using the lane and tell the user; (3) edits under about 40 lines are cheaper done directly. Was this edit one "
    "of those? If it should have gone to Sonnet or the local lane, say so to the user rather than letting it pass "
    'silently.")\n')
COMMENT = ("# 2026-11-13: routing text changed by ~/bin/install-coding-route.py (issue-driven fixes -> Sonnet; local\n"
           "# model only with a check that exists before the job). Undo: python3 -I ~/bin/install-coding-route.py --uninstall\n")


def new_text(s):
    if MARK in s:
        return None
    lines = s.splitlines(keepends=True)
    hits = [i for i, l in enumerate(lines) if l.startswith(OLD_PREFIX)]
    if len(hits) != 1:
        raise SystemExit("dispatch-to-qwen-reminder.sh: expected reminder line not found exactly once; nothing changed")
    lines[hits[0]] = NEW_LINE
    j = next((i for i, l in enumerate(lines) if l.startswith('input="$(cat)"')), None)
    if j is None:
        raise SystemExit('dispatch-to-qwen-reminder.sh: expected input="$(cat)" line not found; nothing changed')
    lines.insert(j, COMMENT)
    return "".join(lines)


def install(dry):
    t = new_text(HOOK.read_text())
    if t is None:
        print("already installed; nothing to do")
        return
    print(("would update " if dry else "updated ") + str(HOOK).replace(str(HOME), "~"))
    if dry:
        return
    b = HOOK.with_name(HOOK.name + BAK)
    if not b.exists():
        shutil.copy2(HOOK, b)
    tmp = HOOK.with_name(HOOK.name + ".tmp-route")
    tmp.write_text(t)
    shutil.copymode(HOOK, tmp)
    os.replace(tmp, HOOK)


def uninstall(dry):
    b = HOOK.with_name(HOOK.name + BAK)
    if not b.exists():
        print("no backup found; nothing to restore")
        return
    print(("would restore " if dry else "restored ") + str(HOOK).replace(str(HOME), "~"))
    if not dry:
        shutil.copy2(b, HOOK)
        b.unlink()


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    (uninstall if a.uninstall else install)(a.dry_run)


if __name__ == "__main__":
    main()
