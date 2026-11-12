#!/usr/bin/env python3
"""needs_info queue slices Q1-Q3 (pure functions of ollama-queue.py; no daemon, no state file): python3 ~/bin/test_mini_needsinfo_queue.py"""
import importlib.util
import json
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sp = importlib.util.spec_from_file_location("oq_ni", HERE / "ollama-queue.py")
Q = importlib.util.module_from_spec(sp)
sp.loader.exec_module(Q)
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


G = Q.PAUSED_STRANDED_GRACE_S
job = {"id": "ni0000000001", "label": "x-fix", "status": "paused", "pause_reason": "needs_info", "pause_meta": {"question": "q?"},
       "resume_transcript": "/tmp/x.needs-info.json"}
for idle in (G + 1, G * 100):
    d = Q._stranded_pause_decide(dict(job), idle)
    check(f"Q1 needs_info idle {idle}s -> surface (never resume)", d[0], "surface")
check("Q1 surface text carries the mini-answer hint", "mini-answer ni0000000001" in Q._stranded_pause_decide(dict(job), G + 1)[1], True)
check("Q1 even with the resume count at 0 and many idle hours: never 'resume'", all(Q._stranded_pause_decide({**job, "stranded_resume_count": n}, G * 50)[0] != "resume" for n in (0, 1, 2, 5)), True)
check("Q1 under the grace -> skip", Q._stranded_pause_decide(dict(job), G - 1)[0], "skip")
check("Q1 other unknown reasons are still auto-resumed", Q._stranded_pause_decide({**job, "pause_reason": "reasoning_loop"}, G + 1)[0], "resume")
j2 = dict(job)
check("Q1 surfaced once per episode", (Q._stranded_pause_apply(j2, ("surface", "m")), Q._stranded_pause_apply(j2, ("surface", "m"))), (True, False))

check("Q2 paused+needs_info is not 'incomplete'", Q._counts_as_incomplete(dict(job)), False)
check("Q2 other paused rows still incomplete", Q._counts_as_incomplete({**job, "pause_reason": "reasoning_loop"}), True)
check("Q2 gate_preempt still excluded", Q._counts_as_incomplete({**job, "pause_reason": Q.GATE_PREEMPT_REASON}), False)
check("Q2 pending still incomplete", Q._counts_as_incomplete({"status": "pending"}), True)

with tempfile.TemporaryDirectory() as d:
    m = Path(d, "work.needs-info.json")
    m.write_text(json.dumps({"pause_reason": "needs_info", "pause_meta": {"question": "q?", "task": "/t/TASK.md", "ask_n": 1}}))
    log = Path(d, "job.log")
    log.write_text(f"[mini-runner] PAUSED: x\n[worker] RESUMABLE TRANSCRIPT: {m}\n")
    rt = Q._parse_resume_transcript(str(log))
    reason, meta = Q._read_pause_info(rt)
    check("Q3 reap: the marker gives pause_reason needs_info and the meta", (reason, meta.get("question"), meta.get("task")), ("needs_info", "q?", "/t/TASK.md"))
    check("Q3 exit 3 is exempt from the bundle failure streak", Q._streak_exempt({"status": "failed", "exit_code": 3}), True)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAIL")
sys.exit(1 if FAILS else 0)
