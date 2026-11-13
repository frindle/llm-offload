#!/usr/bin/env python3
"""install-dispatch-log-hook.py [--uninstall] [--dry-run]

Switches the vault agent-dispatch log from "a reminder asks the model to write it" to "a hook writes it":
  1. ~/.claude/settings.json: adds `python3 -I ~/bin/dispatch-log-hook.py` (async) to the PostToolUse "Agent" entry
     and to SubagentStop.
  2. ~/.claude/hooks/vault-agent-dispatch-reminder.sh: drops the "log this dispatch now" text (job 1). Job 2, the
     durable-session marker for the Stop gate, stays.
  3. ~/.claude/hooks/vault-subagent-stop-reminder.sh: drops the "log the outcome in the dispatch log" half; the
     "file the findings in the project note" half stays.
Every file is backed up first (<file>.bak-dlog). Running it twice changes nothing. --uninstall restores the backups.
--dry-run prints what would change and writes nothing. HOME can be overridden for tests."""
import argparse, json, os, shutil, sys
from pathlib import Path

HOME = Path(os.environ.get("HOME"))
SETTINGS = HOME / ".claude" / "settings.json"
DISPATCH = HOME / ".claude" / "hooks" / "vault-agent-dispatch-reminder.sh"
STOP = HOME / ".claude" / "hooks" / "vault-subagent-stop-reminder.sh"
CMD = "python3 -I ~/bin/dispatch-log-hook.py"
ENTRY = {"type": "command", "command": CMD, "async": True}
BAK = ".bak-dlog"

DISPATCH_OLD_START = 'LOGNOTE="Claude/Agent-Dispatch-Log-$(date -u +%Y-%m).md"\nprintf \'%s\' "$input" | jq -c'
DISPATCH_NEW_TAIL = ("# JOB 1 retired 2026-11-13: ~/bin/dispatch-log-hook.py (PostToolUse Agent + SubagentStop, async)\n"
                     "# now writes the dispatch-log line itself, so this hook no longer asks the model to.\n"
                     "echo '{}'\n")
STOP_OLD = ("check its report and file BOTH (1) the outcome in \" + $ln + \" (monthly log; create the note if the month "
            "has none yet) and (2) the actual findings in the relevant project/topic vault note (Claude/Projects/"
            "<project>.md, Claude/Homelab/devices.md, etc.) -- the dispatch log alone is not sufficient.")
STOP_NEW = ("check its report and file the actual findings in the relevant project/topic vault note (Claude/Projects/"
            "<project>.md, Claude/Homelab/devices.md, etc.). The outcome line in \" + $ln + \" is written automatically "
            "by dispatch-log-hook.py; do not write it yourself.")


def backup(p, dry):
    b = p.with_name(p.name + BAK)
    if not b.exists() and not dry:
        shutil.copy2(p, b)


def new_settings(d):
    hooks = d.setdefault("hooks", {})
    changed = False
    post = hooks.setdefault("PostToolUse", [])
    agent = next((h for h in post if h.get("matcher") == "Agent"), None)
    if agent is None:
        agent = {"matcher": "Agent", "hooks": []}
        post.append(agent)
    if not any(x.get("command") == CMD for x in agent.setdefault("hooks", [])):
        agent["hooks"].append(dict(ENTRY))
        changed = True
    stop = hooks.setdefault("SubagentStop", [])
    if not stop:
        stop.append({"hooks": []})
    tgt = next((h for h in stop if not h.get("matcher")), stop[0])
    if not any(x.get("command") == CMD for x in tgt.setdefault("hooks", [])):
        tgt["hooks"].append(dict(ENTRY))
        changed = True
    return changed


def new_dispatch(s):
    if "JOB 1 retired" in s:
        return None
    i = s.find(DISPATCH_OLD_START)
    if i < 0:
        raise SystemExit("vault-agent-dispatch-reminder.sh: expected text not found; nothing changed")
    return s[:i] + DISPATCH_NEW_TAIL


def new_stop(s):
    if STOP_NEW in s:
        return None
    if STOP_OLD not in s:
        raise SystemExit("vault-subagent-stop-reminder.sh: expected text not found; nothing changed")
    return s.replace(STOP_OLD, STOP_NEW)


def install(dry):
    if not (HOME / "bin" / "dispatch-log-hook.py").exists():
        raise SystemExit("~/bin/dispatch-log-hook.py is missing")
    plan = []
    d = json.loads(SETTINGS.read_text())
    if new_settings(d):
        plan.append((SETTINGS, json.dumps(d, indent=2) + "\n"))
    for p, f in ((DISPATCH, new_dispatch), (STOP, new_stop)):
        t = f(p.read_text())          # check every file before writing any
        if t is not None:
            plan.append((p, t))
    if not plan:
        print("already installed; nothing to do")
        return
    for p, text in plan:
        print(("would update " if dry else "updated ") + str(p).replace(str(HOME), "~"))
        if not dry:
            backup(p, dry)
            tmp = p.with_name(p.name + ".tmp-dlog")
            tmp.write_text(text)
            shutil.copymode(p, tmp)
            os.replace(tmp, p)


def uninstall(dry):
    n = 0
    for p in (SETTINGS, DISPATCH, STOP):
        b = p.with_name(p.name + BAK)
        if b.exists():
            print(("would restore " if dry else "restored ") + str(p).replace(str(HOME), "~"))
            if not dry:
                shutil.copy2(b, p)
                b.unlink()
            n += 1
    if not n:
        print("no backups found; nothing to restore")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--uninstall", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    a = ap.parse_args()
    (uninstall if a.uninstall else install)(a.dry_run)


if __name__ == "__main__":
    main()
