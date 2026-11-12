#!/usr/bin/env python3
"""Behavioural test for queue-abort using a stubbed ollama-queue.py (never touches the live queue).

Usage: python3 test-queue-abort.py [path/to/queue-abort]
Prints QUEUE_ABORT_OK when every check passes.
"""
import json, os, subprocess, sys, tempfile
from pathlib import Path

QA = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("queue-abort"))
td = Path(tempfile.mkdtemp(prefix="qa-test-"))
STATE, CALLS, STUB = td / "state.json", td / "calls.txt", td / "stub-queue.py"
STUB.write_text('''
import json, os, sys
st = json.load(open(os.environ["QA_STATE"]))
av = sys.argv[1:]
open(os.environ["QA_CALLS"], "a").write(" ".join(av) + "\\n")
if av[0] == "status":
    for j in st:
        print("[%-9s] %s  %-30s qwen3.8:27b host=studio   pid=%s  exit=None" % (j["s"], j["id"], j["l"], j.get("pid")))
elif av[0] == "cancel" and "--force" in av:
    jid = av[-1]
    if os.environ.get("QA_STOP") == "die":
        for j in st:
            if j["id"] == jid: j["s"], j["pid"] = "failed", None
        json.dump(st, open(os.environ["QA_STATE"], "w"))
elif av[0] == "cancel" and os.environ.get("QA_FAILCANCEL"):
    sys.exit("[queue] boom")
''')
ok = True
def check(name, got, want):
    global ok
    good = got == want
    ok &= good
    print(("PASS " if good else "FAIL ") + name + ("" if good else f"  got={got!r} want={want!r}"))

def J(i, s, l, pid=None): return {"id": i, "s": s, "l": l, "pid": pid}
DEAD = 2**22 + 12345  # not a live pid
def run(jobs, *args, env=None):
    STATE.write_text(json.dumps(jobs)); CALLS.write_text("")
    e = dict(os.environ, QUEUE_ABORT_QUEUE_PY=str(STUB), QA_STATE=str(STATE), QA_CALLS=str(CALLS),
             QUEUE_ABORT_POLL="0.05", **(env or {}))
    p = subprocess.run([sys.executable, str(QA), *args], capture_output=True, text=True, env=e,
                       stdin=subprocess.DEVNULL, timeout=60)
    calls = [c for c in CALLS.read_text().splitlines() if not c.startswith("status")]
    return p.returncode, p.stdout, calls

A, B, C, D = "a" * 12, "b" * 12, "c" * 12, "d" * 12
base = [J(A, "pending", "bo-v12-debug"), J(B, "running", "bo-v12-bulk", os.getpid()),
        J(C, "needs_opus", "other-job"), J(D, "done", "old-thing")]

rc, out, calls = run(base, A)
check("id resolve + pending -> cancel, rc0", (rc, calls), (0, [f"cancel {A}"]))
rc, out, calls = run(base, "other-")
check("label prefix single match, needs_opus -> resolve", (rc, calls), (0, [f"resolve {C}"]))
rc, out, calls = run(base, "bulk", env={"QA_STOP": "die"})
check("running -> cancel --force, waits, STOPPED rc0", (rc, calls, "STOPPED" in out),
      (0, [f"cancel --force {B}"], True))
rc, out, calls = run(base, "bulk", "--wait", "0.3")
check("wait timeout -> STILL RUNNING + pid, rc1", (rc, "STILL RUNNING" in out, f"pid={os.getpid()}" in out),
      (1, True, True))
rc, out, calls = run(base, D)
check("already-terminal by id -> no calls, rc0", (rc, calls, "already terminal" in out), (0, [], True))
rc, out, calls = run(base, "old-")
check("terminal matched by pattern -> skipped, no calls, rc0", (rc, calls), (0, []))
rc, out, calls = run(base, "nomatch")
check("no match -> rc1", (rc, calls), (1, []))
rc, out, calls = run(base, "bo-v12")
check("2 targets without --yes -> refused rc2, no calls", (rc, calls), (2, []))
rc, out, calls = run([J(A, "pending", "bo-v12-debug"), J(C, "pending", "bo-v12-x")], "bo-v12", "--yes")
check("2 targets --yes -> both cancelled", (rc, sorted(calls)), (0, sorted([f"cancel {A}", f"cancel {C}"])))
rc, out, calls = run(base, "bo-v12", "--dry-run")
check("dry-run lists targets, no calls, rc0", (rc, calls, A in out and B in out), (0, [], True))
many = [J(f"{i:012x}", "pending", f"mass-{i}") for i in range(11)]
rc, out, calls = run(many, "mass-")
check(">10 without --yes refused", (rc, calls), (2, []))
rc, out, calls = run(many, "mass-", "--yes")
check(">10 with --yes acts on all", (rc, len(calls)), (0, 11))
kids = [J(A, "pending", "bo-v12"), J(B, "pending", "auto-fix-bo-v12-r1"), J(C, "pending", "regate-bo-v12"),
        J(D, "pending", "auto-refine-bo-v12-r1"), J("e" * 12, "pending", "gate-unrelated"),
        J("f" * 12, "pending", "esc-review-bo-v12")]
rc, out, calls = run(kids, "bo-v12", "--children", "--yes")
check("--children includes auto-fix/regate/auto-refine/esc-review, excludes unrelated",
      (rc, sorted(calls)), (0, sorted(f"cancel {x}" for x in (A, B, C, D, "f" * 12))))
rc, out, calls = run(kids, "bo-v12", "--yes")
check("no --children -> only the label's own match", (rc, sorted(calls)),
      (0, sorted(f"cancel {j['id']}" for j in kids if "bo-v12" in j["l"])))
rc, out, calls = run(kids, "bo-v12", "--bundle", "nomatch")
check("--bundle filter excludes", rc, 1)
rc, out, calls = run(base, A, env={"QA_FAILCANCEL": "1"})
check("cancel failure -> rc1", rc, 1)
rc, out, calls = run([J(B, "running", "r", DEAD)], B, env={"QA_STOP": "die"})
check("dead-pid running stops cleanly", rc, 0)
print("QUEUE_ABORT_OK" if ok else "QUEUE_ABORT_FAILED")
sys.exit(0 if ok else 1)
