#!/usr/bin/env python3
"""needs_info W1: the watcher surfaces a parked needs_info job as a no-review notice (never reviewed, never auto-resumed):
python3 ~/bin/test_watcher_needsinfo.py"""
import importlib.util, sys
from pathlib import Path
HERE = Path(__file__).resolve().parent
sp = importlib.util.spec_from_file_location("dew_t", HERE / "dispatch-escalation-watcher.py")
W = importlib.util.module_from_spec(sp); sp.loader.exec_module(W)
F = []
def check(n, got, want=True):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f" (got {got!r}, want {want!r})"))
    if not ok: F.append(n)
jobs = [{"id": "ni0000000001", "label": "x-fix", "status": "paused", "pause_reason": "needs_info", "pause_meta": {"question": "Which TTL?", "ask_n": 1}},
        {"id": "pl0000000002", "label": "y", "status": "paused", "pause_reason": "reasoning_loop"},
        {"id": "ru0000000003", "label": "z", "status": "running", "pause_reason": "needs_info"}]
f = W.detect_needs_info_jobs(jobs)
check("only the paused needs_info row is found", [e["job_id"] for e in f], ["ni0000000001"])
e = f[0]
check("no_review, source G, kind job", (e["no_review"], e["source"], e["kind"]), (True, "G", "job"))
check("reason carries the question and the mini-answer hint", 'Which TTL?' in e["reason"] and 'mini-answer ni0000000001' in e["reason"], True)
j2 = [dict(jobs[0], pause_meta={"question": "Which TTL?", "ask_n": 2})]
check("a second ask has a distinct dedup key", W.esc_key(e) != W.esc_key(W.detect_needs_info_jobs(j2)[0]), True)
check("same ask keeps the same key", W.esc_key(e), W.esc_key(W.detect_needs_info_jobs(jobs)[0]))
check("no_review events are not sent to self-heal", W.self_heal(e, "c", "r"), None)
check("notice route is an INDEX notice", W.notice_route(e)[0] == Path(W.INDEX_MD), True)
print("ALL PASS" if not F else f"{len(F)} FAIL"); sys.exit(1 if F else 0)
