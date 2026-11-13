#!/usr/bin/env python3
"""Stop main-session reminder hooks from firing inside subagents.

Why: these PostToolUse/PreToolUse hooks tell the session to log to the vault or
to re-route a code edit. Inside a subagent (fixer-haiku, fixer-sonnet, bundle
agents) that text is off-task: subagents can't file to the vault and can't
re-route their own brief. Transcripts 10-15..11-13: the commit reminder landed
in 502 subagent runs, the outside-repo-write reminder in 312, the git-push
reminder in 322. Hook payloads carry agent_id only inside a subagent, so the
guard exits early when it is set. The SubagentStop reminder is left alone on
purpose (it is the one meant for subagents).

Run: python3 ~/bin/install-subagent-hook-guard.py        (installs, then self-tests)
     python3 ~/bin/install-subagent-hook-guard.py --undo (restores the backups)
"""
import os
import shutil
import subprocess
import sys

HOOKS = os.path.expanduser("~/.claude/hooks")
TARGETS = [
    "dispatch-to-qwen-reminder.sh",
    "vault-substantial-commit-reminder.sh",
    "vault-bash-wrote-outside-repo.sh",
    "vault-git-push-reminder.sh",
    "vault-outside-repo-reminder.sh",
    "vault-apidocs-commit-reminder.sh",
    "vault-apidocs-mirror-reminder.sh",
    "vault-background-launch-reminder.sh",
]
ANCHOR = 'input="$(cat)"'
GUARD = ('# subagent guard: these reminders are for the main session only\n'
         '[ -n "$(printf \'%s\' "$input" | jq -r \'.agent_id // empty\' 2>/dev/null)" ] && exit 0')
BAK = ".bak-subagentguard"


def run_hook(path, payload):
    r = subprocess.run(["bash", path], input=payload, capture_output=True, text=True, timeout=20)
    return r.returncode, r.stdout


def main():
    undo = "--undo" in sys.argv
    changed = []
    for name in TARGETS:
        p = os.path.join(HOOKS, name)
        if undo:
            if os.path.exists(p + BAK):
                shutil.copy2(p + BAK, p)
                print("restored", name)
            continue
        text = open(p, encoding="utf-8").read()
        if "subagent guard" in text:
            print("already guarded", name)
            continue
        lines = text.split("\n")
        idx = next((i for i, l in enumerate(lines) if l.strip() == ANCHOR), None)
        if idx is None:
            print("SKIP (no input line)", name)
            continue
        shutil.copy2(p, p + BAK)
        lines[idx + 1:idx + 1] = GUARD.split("\n")
        open(p, "w", encoding="utf-8").write("\n".join(lines))
        if subprocess.run(["bash", "-n", p]).returncode != 0:
            shutil.copy2(p + BAK, p)
            print("SYNTAX ERROR, restored", name)
            continue
        changed.append(name)
        print("guarded", name)
    if undo:
        return

    # Self-test on the routing hook: main-session payload still gets the reminder,
    # a subagent payload gets nothing.
    q = os.path.join(HOOKS, "dispatch-to-qwen-reminder.sh")
    main_payload = '{"tool_name":"Edit","tool_input":{"file_path":"/Users/x/proj/app.py"}}'
    sub_payload = ('{"tool_name":"Edit","agent_id":"a1","agent_type":"fixer-sonnet",'
                   '"tool_input":{"file_path":"/Users/x/proj/app.py"}}')
    _, out_main = run_hook(q, main_payload)
    _, out_sub = run_hook(q, sub_payload)
    ok = "ROUTING REMINDER" in out_main and out_sub.strip() == ""
    print("self-test:", "PASS" if ok else "FAIL",
          "(main session gets reminder: %s, subagent silent: %s)"
          % ("ROUTING REMINDER" in out_main, out_sub.strip() == ""))
    if not ok:
        print("undo with: python3 ~/bin/install-subagent-hook-guard.py --undo")
        sys.exit(1)


if __name__ == "__main__":
    main()
