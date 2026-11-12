#!/usr/bin/env python3
"""needs_info S1: scorecard reads needs_info_asks from done.json, skips cancelled-while-parked, reports shadow counts (temp dirs only):
python3 ~/bin/test_mini_needsinfo_scorecard.py"""
import json, os, subprocess, sys, tempfile, time
from pathlib import Path
HERE = Path(__file__).resolve().parent
F = []
def check(n, got, want=True):
    ok = got == want
    print(("PASS " if ok else "FAIL ") + n + ("" if ok else f" (got {got!r}, want {want!r})"))
    if not ok: F.append(n)
with tempfile.TemporaryDirectory() as d:
    q = Path(d) / "q"; q.mkdir()
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    def done(i, **kw):
        rec = {"id": i, "label": "a-" + i, "runner": "mini-swe-runner.py", "persisted_at": ts, "active_s": 10, "status": "done", "runner_verdict": "PASS"}
        rec.update(kw); (q / f"{i}.done.json").write_text(json.dumps(rec))
    done("p1", needs_info_asks=1)
    done("p2")
    done("f1", status="failed", runner_verdict="FAIL", needs_info_asks=1)
    done("c1", status="cancelled", runner_verdict=None, needs_info_parked=True)
    sh = Path(d) / "shadow.jsonl"
    sh.write_text("\n".join(json.dumps({"accepted": a}) for a in (True, False, False)) + "\n")
    env = dict(os.environ, OFFLOAD_QUEUE_LOGS=str(q), OFFLOAD_HOME=d, MINI_NEEDS_INFO_SHADOW_LOG=str(sh))
    r = subprocess.run([sys.executable, str(HERE / "mini-scorecard"), "report", "--json"], capture_output=True, text=True, env=env)
    try:
        ni = json.loads(r.stdout)["needs_info"]
    except Exception:
        ni = {}
        print(r.stdout[-300:], r.stderr[-300:])
    check("parked-and-cancelled job is excluded from the window", ni.get("jobs"), 3)
    check("asked jobs counted from needs_info_asks", (ni.get("asked"), ni.get("asked_pass")), (2, 1))
    check("answered-then-PASS rate", ni.get("answered_pass_rate"), 0.5)
    check("shadow accept/reject counts", (ni.get("shadow_accept"), ni.get("shadow_reject")), (1, 2))
print("ALL PASS" if not F else f"{len(F)} FAIL"); sys.exit(1 if F else 0)
