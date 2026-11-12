#!/usr/bin/env python3
"""triage-emit: failure-signature triage handoff (modeled on handoff-emit.py). Read-only
packets under ~/.ollama-dispatch/triage/; only --acted/--unact write (index.json).

  triage-emit.py [--json]                      open items, repeats first then oldest
  triage-emit.py --all [--json]               include acted/superseded
  triage-emit.py show <id>                     print the packet
  triage-emit.py --acted <id>... --outcome FIXED|ESCALATED|WONTFIX --reason "..." [--commit SHA] [--job ID] [--ref TXT]
  triage-emit.py --unact <id>...               reopen
  triage-emit.py --refresh                     force a rebuild (the ledger sweep also does this)
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import triage_packets as tp


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("cmd", nargs="*")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--refresh", action="store_true")
    ap.add_argument("--acted", nargs="+")
    ap.add_argument("--unact", nargs="+")
    ap.add_argument("--outcome")
    ap.add_argument("--reason")
    ap.add_argument("--commit")
    ap.add_argument("--job")
    ap.add_argument("--ref")
    a = ap.parse_args(argv)
    try:
        import failure_ledger
        failure_ledger.sweep()          # also refreshes packets
    except Exception:
        pass
    tp.refresh()
    if a.acted or a.unact:
        ok, msg = tp.act(a.acted or a.unact, a.outcome, a.reason, a.commit, a.job, a.ref,
                         unact=bool(a.unact))
        print(msg, file=sys.stdout if ok else sys.stderr)
        return 0 if ok else 2
    if a.cmd[:1] == ["show"] and len(a.cmd) > 1:
        p = tp.DIR / (a.cmd[1] + ".md")
        if not p.exists():
            print("no packet: %s" % a.cmd[1], file=sys.stderr)
            return 1
        sys.stdout.write(p.read_text())
        return 0
    items = tp.list_items(None if a.all else "open")
    if a.json:
        print(json.dumps({"open": [i for i in items if i["status"] == "open"],
                          "other": [i for i in items if i["status"] != "open"],
                          "summary": tp.open_summary()}, indent=1))
        return 0
    if not items:
        print("no failure signatures need triage")
        return 0
    for i in items:
        print("%-10s %-44s x%-2d %s%s\n           %s" % (
            i["status"], i["id"], i["n_jobs"], "REPEAT " if i["repeat"] else "",
            i["opened_ts"], i["packet"]))
    return 0


if __name__ == "__main__":
    sys.exit(main())
