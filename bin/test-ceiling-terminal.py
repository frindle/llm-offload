#!/usr/bin/env python3
"""A job paused on context_threshold that is already at its host's safe ceiling is TERMINAL: failed, class=context,
with a reason. It is not parked-and-rechecked every tick (7930 "leaving paused" lines in the 2026-11 audit)."""
import importlib.util as u, os, sys, tempfile
from pathlib import Path

os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ["HOME"] = tempfile.mkdtemp(prefix="ceil-")
sp = u.spec_from_file_location("q_ceil", Path(__file__).resolve().parent / "ollama-queue.py")
q = u.module_from_spec(sp)
sp.loader.exec_module(q)
td = tempfile.mkdtemp()
q.LOG_DIR = Path(td)
fails = []


def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c:
        fails.append(n)


job = {"id": "ceil00000001", "label": "x-change", "status": "paused", "pause_reason": "context_threshold",
       "num_ctx": 65536, "max_iters": 40, "host_pref": "studio", "auto_resume_count": 0, "model": "m"}
d = q._auto_resume_decide(job, 65536, False)
chk("at the ceiling: decision is fail_ctx", d[0] == "fail_ctx")
chk("the reason is clear", "safe ceiling" in d[1] and "context_ceiling" in d[2])
chk("apply reports a change", q._auto_resume_apply(job, d) is True)
chk("status failed", job["status"] == "failed")
chk("class=context, terminal_reason set", job["failure_class"] == "context" and job["terminal_reason"] == "context_ceiling")
chk("failure_detail carries the reason", "safe ceiling" in job["failure_detail"])
chk("pause marker cleared", job["pause_reason"] is None)
chk("not re-checked: a failed row is skipped", q._auto_resume_decide(job, 65536, False)[0] == "skip")
chk("durable record carries the class", (Path(td) / "ceil00000001.done.json").exists()
    and '"context"' in (Path(td) / "ceil00000001.done.json").read_text())
j2 = {"id": "ceil00000002", "label": "y", "status": "paused", "pause_reason": "context_threshold", "num_ctx": 32768,
      "max_iters": 40, "host_pref": "studio", "auto_resume_count": 0}
chk("below the ceiling it still bumps", q._auto_resume_decide(j2, 131072, False)[0] == "bump_ctx")
j3 = {"id": "ceil00000003", "label": "z", "status": "pending", "pause_reason": "context_threshold", "num_ctx": 32768,
      "max_iters": 40, "host_pref": "unraid", "auto_resume_count": 0}
d3 = q._auto_resume_decide(j3, None, False)
chk("unknown ceiling (None) parks, never fail_ctx", d3[0] == "park")
chk("known ceiling below current still bumps", q._auto_resume_decide(j3, 65536, False)[0] == "bump_ctx")
print("FAIL: " + ", ".join(fails) if fails else "CEILING_OK")
sys.exit(1 if fails else 0)
