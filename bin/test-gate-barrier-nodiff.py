#!/usr/bin/env python3
"""The gate-firing-this-tick hold only applies when a finished job will really be gated: research, --runner, image,
draft and chain-step jobs have no code diff, no gate row is coming, so other jobs are not held for them."""
import importlib.util as u, os, sys, tempfile
from pathlib import Path

os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
os.environ["HOME"] = tempfile.mkdtemp(prefix="gbn-")
sp = u.spec_from_file_location("q_gb", Path(__file__).resolve().parent / "ollama-queue.py")
q = u.module_from_spec(sp)
sp.loader.exec_module(q)
fails = []


def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c:
        fails.append(n)


nxt = {"id": "n1", "label": "auto-author-login", "status": "pending", "model": "qwen3.8:27b-q4_K_M"}
coding = {"id": "c1", "label": "auth-fix", "status": "done", "model": "qwen3.8:27b-q4_K_M"}
research = dict(coding, id="r1", label="bakeoff-x", task_kind="research")
runner = dict(coding, id="r2", label="lane-bakeoff-judge", runner="bakeoff-runner.py")
image = dict(coding, id="i1", model="image")
draft = dict(coding, id="d1", label="draft-verify")
step = dict(coding, id="s1", chain="c", chain_final=False)
gate = dict(coding, id="g1", label="gate-abc", runner="gate-on-complete.py")
chk("a gated coding job still holds others", q._gate_firing_this_tick_hold(nxt, [coding]) is True)
for name, j in (("research", research), ("runner", runner), ("image", image), ("draft", draft), ("chain step", step)):
    chk(f"a finished {name} job (no code diff) does not hold others", q._gate_firing_this_tick_hold(nxt, [j]) is False)
chk("a finished gate row still holds (it can escalate to a regate)", q._gate_firing_this_tick_hold(nxt, [gate]) is True)
chk("a mix holds when any job will be gated", q._gate_firing_this_tick_hold(nxt, [research, coding]) is True)
chk("nothing finished: no hold", q._gate_firing_this_tick_hold(nxt, []) is False)
print("FAIL: " + ", ".join(fails) if fails else "GATE_BARRIER_OK")
sys.exit(1 if fails else 0)
