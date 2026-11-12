#!/usr/bin/env python3
"""The focus log must describe the idle-lane backfill as it really is (2026-10-05).

Backfill admits only a job stamped with the COMMITTED bundle's tag (focus_skips_job),
but the daemon logged "BACKFILLING an idle lane with one job from another bundle" --
an operator watching a regate of another bundle sit pending was told it could run.

Checks: backfill_focus_note() names the committed bundle and "only", never "another
bundle"; it is empty when backfill is off; it agrees with focus_skips_job (a job of
another bundle IS skipped under backfill, a same-bundle one is not); and the daemon
loop uses it (the old text is gone from the source).

Revert-test: QUEUE_SRC=<backup> python3 test-queue-backfill-focus-note.py -> FAILs.
"""
import importlib.machinery
import importlib.util
import os
import sys
from pathlib import Path

os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
SRC = Path(os.environ.get("QUEUE_SRC") or Path(__file__).resolve().parent / "ollama-queue.py")
FAILS = []


def chk(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


def load():
    loader = importlib.machinery.SourceFileLoader("q_bfnote", str(SRC))
    spec = importlib.util.spec_from_loader("q_bfnote", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main():
    m = load()
    fn = getattr(m, "backfill_focus_note", None)
    chk("backfill_focus_note exists", callable(fn))
    if callable(fn):
        on = fn(True, "rt-egift-link-s1")
        chk("note names the committed bundle", "rt-egift-link-s1" in on)
        chk("note says committed-bundle ONLY", "only" in on)
        chk("note never claims another bundle may run", "another bundle" not in on)
        chk("no note when backfill is off", fn(False, "rt-egift-link-s1") == "")
    # The note must agree with the actual launch rule.
    other = {"id": "x", "bundle": "chat-frontend-plan"}
    same = {"id": "y", "bundle": "rt-egift-link-s1"}
    fs = m.focus_skips_job
    chk("rule: another bundle's job is skipped under backfill",
        fs(other, "chat-frontend-plan", "rt-egift-link-s1", True, "rt-egift-link-s1",
           set(), None, None, backfill_ok=True) is True)
    chk("rule: committed bundle's stamped job is admitted under backfill",
        fs(same, "other-key", "rt-egift-link-s1", True, "rt-egift-link-s1",
           set(), None, None, backfill_ok=True) is False)
    src = SRC.read_text()
    chk("daemon loop no longer prints the old 'from another bundle' text",
        "BACKFILLING an idle lane with one job from another" not in src)
    chk("daemon loop uses backfill_focus_note", "+ backfill_focus_note(_backfill_ok, _commit_key)" in src)
    if FAILS:
        print(f"\n{len(FAILS)} FAILED")
        sys.exit(1)
    print("\nALL PASS")


if __name__ == "__main__":
    main()
