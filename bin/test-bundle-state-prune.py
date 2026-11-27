#!/usr/bin/env python3
"""prune_stale_bundle_state: per-bundle history (_bundle_next, _fairshare, _bundle_fail_streak, _bundle_flaps) of
bundles with no live job and untouched for 3 days is dropped; live, recent, and lane-holding bundles are kept;
the daemon prunes on its first tick and then hourly."""
import importlib.util as u, os, sys, tempfile, time
from pathlib import Path

os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
BIN = Path(__file__).resolve().parent
os.environ["HOME"] = tempfile.mkdtemp(prefix="bsp-")
sp = u.spec_from_file_location("q_prune", BIN / "ollama-queue.py")
q = u.module_from_spec(sp)
sp.loader.exec_module(q)
fails = []


def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c:
        fails.append(n)


now = time.time()
DAY = 86400
iso = lambda t: time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t))
jobs = [{"id": "l1", "label": "live-job", "bundle": "b-live", "status": "pending", "enqueued_at": iso(now)},
        {"id": "r1", "label": "recent-job", "bundle": "b-recent", "status": "done", "launched_at": iso(now - DAY)}]
pk = q._plan_key_map(jobs)
K = {n: pk(j) for n, j in (("live", jobs[0]), ("recent", jobs[1]))}
old = now - 10 * DAY
state = {"jobs": jobs,
         "_bundle_next": {K["live"]: {"at": old}, "old-1": {"at": old}, "new-1": {"at": now - 3600}, "nostamp": {}},
         "_fairshare": {"old-2": {"c": 1.0, "t": old}, "new-1": {"c": 1.0, "t": now - 60}},
         "_bundle_fail_streak": {"old-1": 3, K["recent"]: 2, "streak-only": 1},
         "_bundle_flaps": {"old-3": [old], "new-1": [now - 10]}}
n = q.prune_stale_bundle_state(state, now)
chk("old entries with no live job are dropped", "old-1" not in state["_bundle_next"]
    and "old-2" not in state["_fairshare"] and "old-3" not in state["_bundle_flaps"]
    and "old-1" not in state["_bundle_fail_streak"])
chk("a live bundle keeps its entry even when old", K["live"] in state["_bundle_next"])
chk("a recently touched bundle is kept (own stamp)", "new-1" in state["_bundle_next"] and "new-1" in state["_fairshare"]
    and "new-1" in state["_bundle_flaps"])
chk("a bundle with a recent job row is kept", state["_bundle_fail_streak"].get(K["recent"]) == 2)
chk("an undated entry is kept the first time (aged from first sight)", "nostamp" in state["_bundle_next"]
    and "streak-only" in state["_bundle_fail_streak"])
chk("count = entries removed (4)", n == 4)
chk("undated entries age from first sight and go after 3 days",
    q.prune_stale_bundle_state(state, now + 4 * DAY) >= 2 and "nostamp" not in state["_bundle_next"])
state["_bundle_commit"] = {"key": "held"}
state["_bundle_next"]["held"] = {"at": old}
q.prune_stale_bundle_state(state, now)
chk("a committed bundle is never dropped", "held" in state["_bundle_next"])
# cadence: first call prunes, a second within the hour does not, one after the hour does
q._BUNDLE_PRUNE_AT[0] = 0.0
s2 = {"jobs": [], "_bundle_next": {"x": {"at": old}}}
chk("first tick prunes", q.maybe_prune_bundle_state(s2, now) == 1)
s2["_bundle_next"]["y"] = {"at": old}
chk("not again within the hour", q.maybe_prune_bundle_state(s2, now + 60) == 0 and "y" in s2["_bundle_next"])
chk("again after the hour", q.maybe_prune_bundle_state(s2, now + 3700) == 1)
chk("never raises on junk", q.prune_stale_bundle_state({"jobs": None, "_bundle_next": 5}, now) == 0)
jp = [{"id": "p1", "label": "planned-job", "bundle": "b-planned", "status": q.PLANNED_STATUS},
      {"id": "e1", "label": "esc-job", "bundle": "b-esc", "status": q.ESCALATION_STATUS}]
pkp = q._plan_key_map(jp)
s3 = {"jobs": jp, "_bundle_next": {pkp(jp[0]): {"at": old}, pkp(jp[1]): {"at": old}, "gone": {"at": old}}}
q.prune_stale_bundle_state(s3, now)
chk("planned and needs_opus bundles are protected", pkp(jp[0]) in s3["_bundle_next"] and pkp(jp[1]) in s3["_bundle_next"]
    and "gone" not in s3["_bundle_next"])
print("FAIL: " + ", ".join(fails) if fails else "BUNDLE_PRUNE_OK")
sys.exit(1 if fails else 0)
