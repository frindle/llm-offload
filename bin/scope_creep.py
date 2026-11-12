#!/usr/bin/env python3
"""scope_creep -- flag a landed/gated diff that widens the program's runtime surface.

INCIDENT (2026-10-12, plex-automation 4a454d6, job 463676923c97): a verify-green qagent job added a
30-second scheduler thread, an env var and a 121-line function to a 7000-line file.  Every existing check
passed or was never consulted:
  * `qagent land` (land_guard) only checks FILE-level scope + "VERIFIED" (verify before!=0, after==0);
    it never read the job's gate record (verdict=concerns, signoff_required, signoff_verdict=None,
    "4 hunks, 121 changed lines, cap 80", untrusted dirty baseline).
  * The change was spec-NAMED (the auto-authored TASK.md literally listed maintain_unlimited_upgrade_slots /
    UNLIMITED_UPGRADE_SLOTS), so any "is it mentioned in the task" test passes it.  Only "it adds a new
    runtime surface (thread / env var / route) AND nobody signed it off" catches it.

Two independent rules (both additive, both heuristic, tuned for few false positives):
  UNREQUESTED_SURFACE  a new thread / scheduler / env var / route whose name the spec never mentions.
  UNREQUESTED_BLOCK    a new top-level def/class >= MAX_UNNAMED_LINES lines whose name the spec never mentions
                       (a small unnamed helper is a normal refactor and passes).
  UNSIGNED_SURFACE     ANY new runtime surface (even spec-named) in a diff of >= SURFACE_LINES added lines
                       while the caller says the change is unsigned (gate not a clean pass, or signoff
                       required and not given, or no gate record at all).

Env (all read at call time):
  SCOPE_CREEP_DISABLE=1   kill switch: evaluate()/hooks do nothing at all (exact prior behaviour)
  SCOPE_CREEP_SHADOW      default 1: log-only event SCOPE-CREEP-WOULD-FLAG, no verdict change
  SCOPE_CREEP_ENFORCE     default 0: qagent land REFUSES (event SCOPE-CREEP-BLOCKED); only meaningful when not disabled
  SCOPE_CREEP_MAX_UNNAMED_LINES (40)   SCOPE_CREEP_SURFACE_LINES (80)
  SCOPE_CREEP_LOG         jsonl path (default ~/.ollama-dispatch/scope-creep.jsonl)
"""
from __future__ import annotations
import json, os, re, sys, time
from pathlib import Path

MAX_UNNAMED_LINES = 40
SURFACE_LINES = 80
BULK_LINES = 3000
EVENT_SHADOW = "SCOPE-CREEP-WOULD-FLAG"
EVENT_BLOCK = "SCOPE-CREEP-BLOCKED"

_SKIP_FILE = re.compile(r"(^|/)(tests?/|__tests__/|fixtures?/)|(^|/)test[_-][^/]*$|[_-]tests?\.[a-z]+$|\.test\.[a-z]+$|\.spec\.[a-z]+$|"
                        r"(^|/)(conftest\.py|TASK\.md|ANSWER\.md|verify\.sh|check_literals\.py|refimpl\.py|dispatch_env\.[a-z]+)$|"
                        r"\.(md|txt|json|ya?ml|lock)$", re.I)
_CODE_FILE = re.compile(r"\.(py|js|jsx|mjs|cjs|ts|tsx)$")
_DEF_PY = re.compile(r"^(?:async\s+)?def\s+(\w+)|^class\s+(\w+)")
_DEF_JS = re.compile(r"^(?:export\s+)?(?:default\s+)?(?:async\s+)?function\s*\*?\s*(\w+)|^(?:export\s+)?class\s+(\w+)")
_ENV = re.compile(r"""(?:os\.environ\.get|os\.getenv|os\.environ\[)\s*\(?\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]|process\.env\.([A-Za-z_][A-Za-z0-9_]*)|process\.env\[\s*['"]([A-Za-z_][A-Za-z0-9_]*)['"]""")
_THREAD = re.compile(r"threading\.(?:Thread|Timer)\s*\(|\b(?:Thread|Timer)\s*\(\s*(?:target|\d)|setInterval\s*\(|schedule\.every\b|BackgroundScheduler\s*\(|asyncio\.create_task\s*\(|multiprocessing\.Process\s*\(|new Worker\s*\(")
_TARGET = re.compile(r"target\s*=\s*([\w.]+)|setInterval\s*\(\s*([\w.]+)")
_ROUTE = re.compile(r"@\w+\.(?:route|get|post|put|delete|patch)\s*\(\s*['\"]([^'\"]+)|\.add_url_rule\s*\(\s*['\"]([^'\"]+)|\b(?:app|router|server)\.(?:get|post|put|delete|patch|use)\s*\(\s*['\"]([^'\"]+)")


def disabled() -> bool:
    return os.environ.get("SCOPE_CREEP_DISABLE", "0") not in ("", "0", "false", "no")


def shadow_on() -> bool:
    return (not disabled()) and os.environ.get("SCOPE_CREEP_SHADOW", "1") not in ("", "0", "false", "no")


def enforce_on() -> bool:
    return (not disabled()) and os.environ.get("SCOPE_CREEP_ENFORCE", "0") not in ("", "0", "false", "no")


def _int_env(name: str, d: int) -> int:
    try:
        return int(os.environ.get(name, d))
    except Exception:
        return d


def parse_diff(diff: str) -> dict:
    """{file: {'added': [str], 'removed': [str]}} for code files only (tests/docs/harness skipped)."""
    out: dict = {}
    cur = None
    for ln in (diff or "").splitlines():
        if ln.startswith("diff --git "):
            m = re.match(r"diff --git a/(.*?) b/(.*)$", ln)
            cur = m.group(2) if m else None
            if cur and (_SKIP_FILE.search(cur) or not _CODE_FILE.search(cur)):
                cur = None
            if cur:
                out.setdefault(cur, {"added": [], "removed": [], "new": False})
            continue
        if ln.startswith("new file mode") and cur:
            out[cur]["new"] = True
            continue
        if ln.startswith("+++ ") or ln.startswith("--- "):
            continue
        if cur is None:
            continue
        if ln.startswith("+"):
            out[cur]["added"].append(ln[1:])
        elif ln.startswith("-"):
            out[cur]["removed"].append(ln[1:])
    return out


def _first(m):
    return next((g for g in m.groups() if g), "")


def _scan(lines):
    items = []
    # top-level defs and their body length (lines until the next top-level added non-blank, non-indented line)
    defs = []
    for i, l in enumerate(lines):
        m = _DEF_PY.match(l) or _DEF_JS.match(l)
        if m:
            defs.append((i, _first(m)))
    for k, (i, name) in enumerate(defs):
        j = i + 1
        while j < len(lines):
            lj = lines[j]
            if lj.strip() and not lj[0].isspace() and lj[0] not in ")]}":
                break
            j += 1
        items.append({"kind": "def", "name": name, "lines": j - i})
    for l in lines:
        for m in _ENV.finditer(l):
            items.append({"kind": "env", "name": _first(m), "lines": 1})
        if _THREAD.search(l):
            t = _TARGET.search(l)
            items.append({"kind": "thread", "name": (_first(t) if t else "") or "<thread>", "lines": 1})
        for m in _ROUTE.finditer(l):
            items.append({"kind": "route", "name": _first(m), "lines": 1})
    return items


def surface_items(diff: str) -> tuple[list, int]:
    """New items (present on added lines, not already present on removed lines) + total added code lines."""
    files = parse_diff(diff)
    new, added_total = [], 0
    for f, d in files.items():
        added_total += sum(1 for l in d["added"] if l.strip())
        if d.get("new"):        # a brand-new file: its surface IS the deliverable (creation task); only edits to existing code widen scope
            continue
        old = {(i["kind"], i["name"]) for i in _scan(d["removed"])}
        seen = set()
        for it in _scan(d["added"]):
            key = (it["kind"], it["name"])
            if key in old or key in seen:
                continue
            seen.add(key)
            it["file"] = f
            new.append(it)
    return new, added_total


_ANON_WORDS = re.compile(r"interval|heartbeat|\bhb\b|timer|schedul|poll|every|background|thread|cron|periodic|daemon|keep-?alive|\bloop\b", re.I)


def _named(name: str, spec: str, kind: str = "") -> bool:
    if not name:
        return False
    if name == "<thread>":      # anonymous callback: named iff the task talks about a periodic/background mechanism at all
        return bool(_ANON_WORDS.search(spec))
    if kind == "route":         # '/run-bulk-search': any word of the path of >=4 chars in the task counts
        return any(len(w) >= 4 and w.lower() in spec.lower() for w in re.split(r"[^A-Za-z0-9]+", name))
    return name.split(".")[-1] in spec


def evaluate(diff: str, spec_text: str = "", unsigned: bool | None = None) -> dict:
    """Pure function.  unsigned: True/False/None(unknown -> no UNSIGNED_SURFACE rule).
    spec_text empty/blank = the task text is UNKNOWN: the UNREQUESTED_* rules abstain (cannot judge 'not named' without
    a spec); UNSIGNED_SURFACE still applies.  A diff > BULK_LINES added lines is a snapshot/import, not a dispatch: abstain."""
    spec = spec_text or ""
    items, added = surface_items(diff)
    if added > _int_env("SCOPE_CREEP_BULK_LINES", BULK_LINES):
        return {"flag": False, "reasons": [], "evidence": [], "added_code_lines": added, "surface": [], "abstain": "bulk"}
    have_spec = bool(spec.strip())
    max_unnamed = _int_env("SCOPE_CREEP_MAX_UNNAMED_LINES", MAX_UNNAMED_LINES)
    surf_lines = _int_env("SCOPE_CREEP_SURFACE_LINES", SURFACE_LINES)
    reasons, ev = [], []
    surface = [i for i in items if i["kind"] in ("thread", "env", "route")]
    for i in surface:
        if have_spec and not _named(i["name"], spec, i["kind"]):
            reasons.append("UNREQUESTED_SURFACE")
            ev.append(f"new {i['kind']} {i['name']!r} in {i['file']} is not named by the task")
    for i in items:
        if i["kind"] == "def" and i["lines"] >= max_unnamed and have_spec and not _named(i["name"], spec):
            reasons.append("UNREQUESTED_BLOCK")
            ev.append(f"new {i['name']}() {i['lines']} lines in {i['file']} is not named by the task")
    if surface and unsigned and added >= surf_lines:
        reasons.append("UNSIGNED_SURFACE")
        ev.append("new runtime surface (" + ", ".join(f"{i['kind']}:{i['name']}" for i in surface[:6]) +
                  f") in a {added}-line change with no clean gate pass / sign-off")
    return {"flag": bool(reasons), "reasons": sorted(set(reasons)), "evidence": ev[:10],
            "added_code_lines": added, "surface": [(i["kind"], i["name"]) for i in surface]}


def gate_unsigned(gate: dict | None) -> bool | None:
    """True when the gate record says: not a clean pass, or sign-off required and not given.  None = no record."""
    if not isinstance(gate, dict):
        return None
    if gate.get("signoff_required") and not gate.get("signoff_verdict"):
        return True
    if gate.get("verdict") not in ("pass", "pass-pending-review"):
        return True
    return False


def log_event(event: str, ctx: dict, res: dict) -> None:
    try:
        p = Path(os.environ.get("SCOPE_CREEP_LOG") or Path.home() / ".ollama-dispatch" / "scope-creep.jsonl")
        p.parent.mkdir(parents=True, exist_ok=True)
        rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "event": event, **ctx,
               "reasons": res["reasons"], "evidence": res["evidence"], "added_code_lines": res["added_code_lines"]}
        with p.open("a") as f:
            f.write(json.dumps(rec) + "\n")
    except Exception:
        pass


def check(diff: str, spec_text: str, gate: dict | None, ctx: dict) -> tuple[str | None, dict | None]:
    """Hook entry.  Returns (refusal_reason_or_None, result).  No-op (None, None) when disabled.
    Shadow: logs SCOPE-CREEP-WOULD-FLAG.  Enforce: logs SCOPE-CREEP-BLOCKED and returns a reason."""
    if disabled() or not (shadow_on() or enforce_on()):
        return None, None
    try:
        res = evaluate(diff, spec_text, gate_unsigned(gate))
    except Exception:
        return None, None
    if not res["flag"]:
        return None, res
    if enforce_on():
        log_event(EVENT_BLOCK, ctx, res)
        return (f"{EVENT_BLOCK}: " + "; ".join(res["reasons"]) + " -- " + " | ".join(res["evidence"][:3]) +
                " (SCOPE_CREEP_ENFORCE=1; SCOPE_CREEP_DISABLE=1 to bypass)"), res
    log_event(EVENT_SHADOW, ctx, res)
    return None, res


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("diff_file"); ap.add_argument("--spec"); ap.add_argument("--unsigned", action="store_true")
    a = ap.parse_args()
    r = evaluate(Path(a.diff_file).read_text(), Path(a.spec).read_text() if a.spec else "", a.unsigned or None)
    print(json.dumps(r, indent=1)); sys.exit(1 if r["flag"] else 0)
