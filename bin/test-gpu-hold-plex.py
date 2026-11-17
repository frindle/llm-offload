"""GET /api/gpu-hold also holds while Plex is playing on a lane that shares its GPU (the plex-busy flag), so
plex-automation's llm_advisor (LLM_UNRAID_HOLD_URL) yields the Unraid card to Plex the way the queue's own launches do."""
import importlib.util
import json
import os
import sys
import tempfile
import time
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
_here = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, _here)
_spec = importlib.util.spec_from_file_location("oqa_gpu_hold", os.path.join(_here, "ollama-queue-api.py"))
api = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(api)
q = api.q

FAILS = []


def chk(name, cond, detail=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  -> %r" % (detail,)))
    if not cond:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp())
state = tmp / "state.json"
flag = tmp / "plex-busy"
q.STATE_PATH = str(state)
real_path, real_standby = q.plex_busy_path, api.queue_config.standby
q.plex_busy_path = lambda: flag
api.queue_config.standby = lambda: False
gx = {"id": 7, "status": "running", "job_kind": q.GPU_JOB_KIND, "runner": q.GPU_JOB_RUNNER, "lane": "unraid"}
try:
    state.write_text(json.dumps({"jobs": []}))
    chk("no flag, no job: no hold", api._gpu_hold_now("unraid") == (200, {"hold": False, "lane": "unraid"}),
        api._gpu_hold_now("unraid"))
    flag.write_text("")
    chk("fresh plex-busy flag: unraid held for Plex",
        api._gpu_hold_now("unraid") == (200, {"hold": True, "cause": "plex", "lane": "unraid"}), api._gpu_hold_now("unraid"))
    chk("a lane that does not share Plex's GPU is not held", api._gpu_hold_now("studio-db")[1]["hold"] is False,
        api._gpu_hold_now("studio-db"))
    old = time.time() - q.PLEX_BUSY_FRESH_S - 5
    os.utime(flag, (old, old))
    chk("stale flag (watcher down): no hold, fails open", api._gpu_hold_now("unraid")[1]["hold"] is False,
        api._gpu_hold_now("unraid"))
    os.utime(flag, None)
    state.write_text(json.dumps({"jobs": [gx]}))
    st, body = api._gpu_hold_now("unraid")
    chk("a running gpu-exclusive job is reported first (names the job)",
        st == 200 and body.get("cause") == "gpu-exclusive" and body.get("job") == 7, body)
    state.write_text("not json")
    chk("unreadable state: 503 even with the flag (the caller fails open)", api._gpu_hold_now("unraid")[0] == 503)
finally:
    q.plex_busy_path, api.queue_config.standby = real_path, real_standby

print("ALL PASS" if not FAILS else "%d FAILED: %s" % (len(FAILS), "; ".join(FAILS)))
sys.exit(1 if FAILS else 0)
