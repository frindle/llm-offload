#!/usr/bin/env python3
"""Live-validation ledger: fixes that SHIPPED but whose correctness can only be
confirmed against live data/hardware on the next real run.

The dispatch handoff board (handoff-emit.py) tracks a fix up to the moment it is
merged. But a whole class of fix is not actually DONE at merge: a scraper that
must read a real cross-origin iframe on the next order sync, a dashboard header
that must render correctly while real hardware sits idle. Those are "validated on
the next live sync" -- a promise that today lives only in a human's head and one
scattered vault line. A shipped-but-unvalidated fix then silently reads as done,
and if the live check quietly fails nothing surfaces it.

This is the sibling of the handoff board for exactly that gap. Same storage
neighbourhood (the ollama-handoff dir), same stdlib-only, degrade-never-throw
posture, same session-start surfacing.

STATES (a fix moves down this list):
  pending-fix  the FIX itself hasn't shipped yet (e.g. a diagnosis is still
               running). Recorded now so the live check isn't forgotten once it
               lands. Surfaced gently, like pending.
  pending      shipped; awaiting its live check. The default worklist.
  passed       the live observable held. Drops out of the default view.
  failed       the live observable did NOT hold -- a shipped fix that didn't
               actually work. Stays PROMINENTLY surfaced; this is the single most
               important thing the ledger exists to show.

ONE PIECE OF REAL STATE. live-validation.json is the whole record; there is no
derived folder. A corrupt/empty/missing file degrades to "no items" -- the tool
and the session-start hook must never throw into a session.

Usage:
  live-validation-ledger.py add --repo R --label L --what "..." --how "..." \
                                [--commit SHA] [--id ID] [--pending-fix]
  live-validation-ledger.py list [--json] [--all]
  live-validation-ledger.py pass <id> [--note "..."]
  live-validation-ledger.py fail <id> [--note "..."]
  live-validation-ledger.py note <id> "..."
  live-validation-ledger.py rm <id>
"""
import argparse, json, os, sys, time, uuid
from pathlib import Path

# Match handoff-emit.py: same dir, same HANDOFF_DIR override, so the two tools
# always live together and a consolidation move can't split one from the other.
OUT = Path(os.environ.get(
    "HANDOFF_DIR",
    Path.home() / "Desktop" / "GitHub Projects" / "ollama" / "ollama-handoff")
).expanduser()
STATE = OUT / "live-validation.json"

PENDING_STATES = ("pending", "pending-fix")
OPEN_STATES = ("pending", "pending-fix", "failed")  # what the default view shows


def _now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _load():
    """Whole ledger as {id: item}. Corrupt/empty/missing -> {} (never throw)."""
    try:
        data = json.loads(STATE.read_text())
        items = data.get("items") if isinstance(data, dict) else None
        return items if isinstance(items, dict) else {}
    except Exception:
        return {}


def _save(items):
    OUT.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"items": items}, indent=1, sort_keys=True))
    tmp.replace(STATE)  # atomic; a half-written ledger is the thing we can't afford


def _gen_id():
    return uuid.uuid4().hex[:12]


def _sorted(items, states):
    """Items in `states`, newest-first (by added_ts then id)."""
    rows = [it for it in items.values() if it.get("status") in states]
    return sorted(rows, key=lambda it: (it.get("added_ts") or "", it.get("id") or ""),
                  reverse=True)


def _group_by_repo(rows):
    groups = {}
    for it in rows:
        groups.setdefault(it.get("repo") or "(no repo)", []).append(it)
    return dict(sorted(groups.items()))


# ---- commands ---------------------------------------------------------------

def cmd_add(a):
    items = _load()
    jid = a.id or _gen_id()
    if jid in items:
        print(f"[live-val] id {jid} already exists; pick another or omit --id")
        return 2
    items[jid] = {
        "id": jid,
        "repo": a.repo,
        "commit": a.commit or "",
        "label": a.label,
        "what": a.what,
        "how": a.how,
        "added_ts": _now(),
        "status": "pending-fix" if a.pending_fix else "pending",
        "notes": [],
    }
    if a.note:
        items[jid]["notes"].append({"ts": _now(), "note": a.note})
    _save(items)
    print(f"[live-val] added {jid} [{items[jid]['status']}] {a.repo}: {a.label}")
    return 0


def _resolve(a, status, verb):
    items = _load()
    it = items.get(a.id)
    if not it:
        print(f"[live-val] no such id: {a.id}")
        return 2
    it["status"] = status
    it["resolved_ts"] = _now()
    if a.note:
        it["notes"].append({"ts": _now(), "note": a.note})
        it["resolution_note"] = a.note
    _save(items)
    print(f"[live-val] {verb}: {a.id} {it.get('label') or ''}"
          + (f" — {a.note}" if a.note else ""))
    return 0


def cmd_pass(a):
    return _resolve(a, "passed", "PASSED live validation")


def cmd_fail(a):
    return _resolve(a, "failed", "FAILED live validation")


def cmd_note(a):
    items = _load()
    it = items.get(a.id)
    if not it:
        print(f"[live-val] no such id: {a.id}")
        return 2
    it.setdefault("notes", []).append({"ts": _now(), "note": a.text})
    _save(items)
    print(f"[live-val] noted on {a.id}: {a.text}")
    return 0


def cmd_rm(a):
    items = _load()
    if items.pop(a.id, None) is None:
        print(f"[live-val] no such id: {a.id}")
        return 2
    _save(items)
    print(f"[live-val] removed {a.id}")
    return 0


def _fmt_item(it):
    L = [f"- `{it.get('id')}` **{it.get('label') or '(no label)'}**"
         f"  [{it.get('status')}]  _{(it.get('added_ts') or '')[:10]}_"]
    if it.get("commit"):
        L.append(f"    - commit: `{it['commit']}`")
    if it.get("what"):
        L.append(f"    - what must hold live: {it['what']}")
    if it.get("how"):
        L.append(f"    - how to check: {it['how']}")
    if it.get("resolution_note"):
        L.append(f"    - resolution: {it['resolution_note']}")
    for n in it.get("notes", []):
        L.append(f"    - note ({(n.get('ts') or '')[:10]}): {n.get('note')}")
    return L


def cmd_list(a):
    items = _load()
    if a.json:
        rows = list(items.values()) if a.all else \
            [it for it in items.values() if it.get("status") in OPEN_STATES]
        rows = sorted(rows, key=lambda it: (it.get("added_ts") or "", it.get("id") or ""),
                      reverse=True)
        print(json.dumps({
            "open": [r for r in rows if r.get("status") in OPEN_STATES],
            "all": sorted(items.values(),
                          key=lambda it: (it.get("added_ts") or "", it.get("id") or ""),
                          reverse=True) if a.all else None,
            "counts": {
                "pending": sum(1 for i in items.values() if i.get("status") == "pending"),
                "pending_fix": sum(1 for i in items.values() if i.get("status") == "pending-fix"),
                "failed": sum(1 for i in items.values() if i.get("status") == "failed"),
                "passed": sum(1 for i in items.values() if i.get("status") == "passed"),
            },
            "out_dir": str(OUT),
        }, indent=1))
        return 0

    failed = _sorted(items, ("failed",))
    pending = _sorted(items, PENDING_STATES)
    passed = _sorted(items, ("passed",)) if a.all else []

    L = ["# Live-validation ledger", "",
         "Shipped fixes whose correctness only a live run can confirm. "
         "`pass`/`fail` each once its live check is observed.", ""]

    # FAILED first and loudest: a shipped fix that didn't actually work.
    L += [f"## ❌ FAILED live validation ({len(failed)})", ""]
    if failed:
        for repo, rows in _group_by_repo(failed).items():
            L.append(f"### {repo}")
            for it in rows:
                L += _fmt_item(it)
            L.append("")
    else:
        L += ["_None failed._", ""]

    L += [f"## ⏳ Awaiting live validation ({len(pending)})", ""]
    if pending:
        for repo, rows in _group_by_repo(pending).items():
            L.append(f"### {repo}")
            for it in rows:
                L += _fmt_item(it)
            L.append("")
    else:
        L += ["_Nothing awaiting a live check._", ""]

    if a.all:
        L += [f"## ✅ Passed ({len(passed)})", ""]
        for repo, rows in _group_by_repo(passed).items():
            L.append(f"### {repo}")
            for it in rows:
                L += _fmt_item(it)
            L.append("")

    print("\n".join(L))
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")

    p = sub.add_parser("add", help="record a pending live-validation")
    p.add_argument("--repo", required=True)
    p.add_argument("--label", required=True)
    p.add_argument("--what", required=True, help="the observable that must hold live")
    p.add_argument("--how", required=True, help="how to check it (API pull, dashboard field, log line)")
    p.add_argument("--commit", default="", help="SHA of the shipped fix")
    p.add_argument("--id", default="", help="short id (generated if omitted)")
    p.add_argument("--pending-fix", action="store_true",
                   help="the FIX itself hasn't shipped yet (e.g. diagnosis still running)")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_add)

    p = sub.add_parser("list", help="show pending + failed (newest first, grouped by repo)")
    p.add_argument("--json", action="store_true")
    p.add_argument("--all", action="store_true", help="include passed/resolved items")
    p.set_defaults(func=cmd_list)

    p = sub.add_parser("pass", help="mark validated (live observable held)")
    p.add_argument("id")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_pass)

    p = sub.add_parser("fail", help="mark failed (stays prominently surfaced)")
    p.add_argument("id")
    p.add_argument("--note", default="")
    p.set_defaults(func=cmd_fail)

    p = sub.add_parser("note", help="append a note to an item")
    p.add_argument("id")
    p.add_argument("text")
    p.set_defaults(func=cmd_note)

    p = sub.add_parser("rm", help="remove an item")
    p.add_argument("id")
    p.set_defaults(func=cmd_rm)

    a = ap.parse_args()
    if not getattr(a, "cmd", None):
        ap.print_help()
        return 0
    try:
        return a.func(a)
    except Exception as e:
        # Never throw into a caller (or a session-start hook shelling out to us).
        print(f"[live-val] error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
