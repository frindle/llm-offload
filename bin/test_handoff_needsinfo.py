#!/usr/bin/env python3
"""needs_info slice P1: handoff-emit renders a parked needs_info job with its question and the mini-answer hint (no queue, no network):
python3 ~/bin/test_handoff_needsinfo.py"""
import importlib.util
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sp = importlib.util.spec_from_file_location("handoff_emit_t", HERE / "handoff-emit.py")
H = importlib.util.module_from_spec(sp)
sp.loader.exec_module(H)
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f" (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


job = {"id": "nitest000001", "label": "x-fix", "status": "paused", "pause_reason": "needs_info", "pause_meta": {"question": "Which TTL does the\nvendor use?"}, "_live": True}
plain = {"id": "plain0000001", "label": "y", "status": "paused", "pause_reason": "reasoning_loop", "pause_meta": {}, "_live": True}
check("needs_answer returns the question for paused+needs_info", H.needs_answer(job), "Which TTL does the\nvendor use?")
check("needs_answer is None for other paused rows and other statuses", (H.needs_answer(plain), H.needs_answer({**job, "status": "running"})), (None, None))
check("a parked ask is still a pending (in flight) row", H.is_pending(job), True)
page = H.render(job, {}, "")
check("render shows NEEDS ANSWER with the question and the hint", "**NEEDS ANSWER**: Which TTL does the vendor use?" in page and 'mini-answer nitest000001 "..."' in page, True)
check("render of other rows has no NEEDS ANSWER", "NEEDS ANSWER" not in H.render(plain, {}, ""), True)
with tempfile.TemporaryDirectory() as d:
    H.OUT = Path(d)
    H.write_index([(job, {}, "pending/a.md"), (plain, {}, "pending/b.md")], [], {})
    idx = (Path(d) / "INDEX.md").read_text()
    rows = [ln for ln in idx.splitlines() if "pending/a.md" in ln or "pending/b.md" in ln]
    check("INDEX flags only the needs_info row", len(rows) == 2 and "NEEDS ANSWER" in rows[0] and "NEEDS ANSWER" not in rows[1], True)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAIL")
sys.exit(1 if FAILS else 0)
