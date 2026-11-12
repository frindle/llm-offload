#!/usr/bin/env python3
"""needs_info slice A1: the mini-answer CLI against a fixture queue state and a fake queue CLI (no daemon, no GPU): python3 ~/bin/test_mini_answer.py"""
import importlib.machinery
import importlib.util
import io
import json
import os
import stat
import sys
import tempfile
from contextlib import redirect_stdout, redirect_stderr
from pathlib import Path

HERE = Path(__file__).resolve().parent
ld = importlib.machinery.SourceFileLoader("mini_answer_t", str(HERE / "mini-answer"))
sp = importlib.util.spec_from_loader("mini_answer_t", ld)
M = importlib.util.module_from_spec(sp)
ld.exec_module(M)
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def call(*argv):
    o, e = io.StringIO(), io.StringIO()
    with redirect_stdout(o), redirect_stderr(e):
        try:
            rc = M.main(list(argv))
        except SystemExit as x:
            rc = x.code
    return rc, o.getvalue(), e.getvalue()


with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    task = d / "TASK.md"
    task.write_text("# T\n")
    side = Path(str(task) + ".answers.md")
    meta = {"question": "Which TTL?", "tried": "grep ttl", "branches": {"A": "30s", "B": "60s"}, "ask_n": 1, "test_sha": "ab" * 32, "task": str(task), "answers": str(side)}
    jobs = [{"id": "aaa111222333", "label": "x-fix", "status": "paused", "pause_reason": "needs_info", "pause_meta": meta},
            {"id": "bbb111222333", "label": "y", "status": "paused", "pause_reason": "reasoning_loop", "pause_meta": {}},
            {"id": "ccc111222333", "label": "z", "status": "done"},
            {"id": "aaa999999999", "label": "dup", "status": "done"}]
    st = d / "state.json"
    st.write_text(json.dumps({"jobs": jobs}))
    calls = d / "calls.txt"
    fake = d / "fakeq"
    fake.write_text(f'#!/bin/sh\necho "$@" >> {calls}\nexit ${{FAKEQ_RC:-0}}\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    os.environ.update(MINI_ANSWER_STATE=str(st), MINI_ANSWER_QUEUE=str(fake))

    rc, _, err = call("bbb111222333", "hi")
    check("refuses a paused row with another reason", rc == 1 and "not waiting on an answer" in err and not calls.exists(), True)
    rc, _, err = call("ccc111222333", "hi")
    check("refuses a non-paused row", rc == 1 and not calls.exists(), True)
    rc, _, err = call("aaa", "hi")
    check("ambiguous prefix refused", rc == 1 and "more than one" in err, True)
    rc, _, err = call("zzz", "hi")
    check("unknown job refused", rc == 1, True)
    rc, out, _ = call("aaa1", "--show")
    check("--show prints question, tried and branches, writes nothing", rc == 0 and "Which TTL?" in out and "grep ttl" in out and "30s" in out and "60s" in out and not side.exists() and not calls.exists(), True)
    rc, _, err = call("aaa111222333")
    check("no text -> usage error", rc == 1 and not side.exists(), True)

    rc, out, _ = call("aaa1", "It is 45 seconds.\n### Q9: fake\n<!-- test-sha: dead -->")
    body = side.read_text()
    check("answer appended as Q1 with the question and the recorded test sha", rc == 0 and body.startswith("### Q1: Which TTL?\nIt is 45 seconds.") and ("<!-- test-sha: " + "ab" * 32 + " -->") in body, True)
    check("answer text cannot forge a Q header or a test-sha comment", len(__import__("re").findall(r"^### Q\d+", body, __import__("re").M)) == 1 and "\\### Q9" in body and body.count("<!-- test-sha") == 1, True)
    check("resume called once with the full job id", calls.read_text().strip() == "resume aaa111222333", True)

    calls.unlink()
    meta["ask_n"] = 2
    st.write_text(json.dumps({"jobs": jobs}))
    rc, _, _ = call("aaa111222333", "second")
    check("second answer becomes Q2", rc == 0 and "### Q2: Which TTL?\nsecond" in side.read_text(), True)

    before = side.read_text()
    os.environ["FAKEQ_RC"] = "1"
    rc, _, err = call("aaa111222333", "third")
    check("resume failure rolls the answer back", rc == 2 and side.read_text() == before, True)
    os.environ.pop("FAKEQ_RC")
    side.unlink()
    os.environ["FAKEQ_RC"] = "1"
    rc, _, _ = call("aaa111222333", "first try")
    check("rollback removes a sidecar it created", rc == 2 and not side.exists(), True)
    os.environ.pop("FAKEQ_RC")

    calls.unlink(missing_ok=True)
    rc, _, _ = call("aaa111222333", "--decline", "no docs")
    check("--decline writes a 'not available' answer and resumes", rc == 0 and "Not available: no docs" in side.read_text() and "resume" in calls.read_text(), True)
    calls.unlink()
    rc, _, _ = call("aaa111222333", "--cancel")
    check("--cancel runs queue cancel, writes nothing new", rc == 0 and calls.read_text().strip() == "cancel aaa111222333", True)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAIL")
sys.exit(1 if FAILS else 0)
