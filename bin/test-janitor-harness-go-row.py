#!/usr/bin/env python3
"""HARNESS GO rows in READY-TO-LAND.md close on positive evidence only
(escalation_index_janitor.py, 2026-10-05 "janitor READY default path").

ollama-dispatch-auto surface_go() writes a `HARNESS GO -- relevance review` row (source
R) into READY-TO-LAND.md when a harness converges. Nothing ever ticked it, so rows sat
open after the coding dispatch had been enqueued, or after the worktree was pruned.
The fix:
  * run() with no index_files scans BOTH default index files (ESCALATIONS.md and
    READY-TO-LAND.md),
  * a GO row closes once a queue row carries the coding label, or its worktree (the
    row's trailing `path`) is gone,
  * it stays open while neither holds, and while the queue state is unreadable.

Hermetic: temp index files, temp queue state, temp worktree. Touches nothing live.
Revert: JANITOR=/path/to/old_copy.py python3 this.py  -> FAIL."""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = Path(os.environ.get("JANITOR", HERE / "escalation_index_janitor.py"))
_ld = importlib.machinery.SourceFileLoader("eij_go", str(MOD))   # .bak paths too
spec = importlib.util.spec_from_loader("eij_go", _ld)
J = importlib.util.module_from_spec(spec)
_ld.exec_module(J)
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": want {want!r} got {got!r}"))
    if not ok:
        FAILS.append(name)


T = Path(tempfile.mkdtemp(prefix="eij-go-"))
(T / "runs").mkdir()
(T / "plans").mkdir()
wt = T / "wt-rt-x-getc"
wt.mkdir()
qs = T / "state.json"
J.INDEX_MD, J.READY_MD = T / "ESCALATIONS.md", T / "READY-TO-LAND.md"
J.INDEX_MD.write_text("# Escalations\n")


def row(label, path):
    return (f"- [ ] `{label}` **HARNESS GO -- relevance review** R - 2026-10-06T01:50:21Z - "
            f"harness reached GO (relevance killed 7/7, score 1.0); READ the fixture, then "
            f"ollama-dispatch-draft --confirm, enqueue - `{path}`\n")


def queue(jobs):
    qs.write_text(json.dumps({"jobs": jobs}))


def go(jobs, path=wt, label="rt-x-getc"):
    """Write one GO row to the default READY file, run the janitor with DEFAULT index
    files, return whether that row was closed."""
    J.READY_MD.write_text("# Ready to land\n\n" + row(label, path))
    if jobs is None:
        qs.write_text("{not json")
    else:
        queue(jobs)
    facts = J.Facts(runs_dir=T / "runs", plans_dir=T / "plans", queue_state=qs)
    J.run(facts=facts, apply=True, log_path=T / "log.jsonl", out=lambda *a: None)
    return J.READY_MD.read_text().count("- [x] `" + label + "`") == 1


author = {"id": "87ef87235fd3", "label": "auto-author-rt-x-getc", "status": "failed"}
coding = {"id": "abcdef123456", "label": "rt-x-getc", "status": "pending"}
chk("GO row stays open while only the AUTHOR job exists (not the coding dispatch)",
    go([author]), False)
chk("GO row closes once the coding dispatch --label is enqueued (default READY path)",
    go([author, coding]), True)
chk("GO row closes when its worktree is gone", go([author], path=T / "gone"), True)
chk("GO row stays open while the queue state is unreadable", go(None), False)
d = J.scan(row("rt-x-getc", wt))[0][2]
chk("the row's trailing `path` is parsed", d.get("path"), str(wt))
shutil.rmtree(T, ignore_errors=True)
print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
