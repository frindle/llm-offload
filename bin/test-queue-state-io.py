#!/usr/bin/env python3
"""test-queue-state-io.py -- queue state-file I/O safety as a canary seam.

WHY (adversarial canary review 2026-10-13): with _Locked.save writing the state file IN PLACE (a kill
mid-write leaves torn JSON -> next load quarantines it and every bundle vanishes) and _Locked.load
quarantining on an OSError (the 2026-09-21 EMFILE incident: a valid 551KB state moved aside three
times in 90s), the full canary stayed 240/240 green. These rules were pinned only in
`ollama-queue.py --self-test`, which the canary does not run (and which is red at baseline).

Usage: python3 ~/bin/test-queue-state-io.py [--queue PATH]     Prints STATE_IO_OK."""
import argparse, errno, json, os, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--queue", default=str(Path(__file__).resolve().parent / "ollama-queue.py"))
A = ap.parse_args()
T = Path(tempfile.mkdtemp(prefix="stateio-"))
os.environ["HOME"] = str(T)
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
q = SourceFileLoader("q_stateio", A.queue).load_module()
q.STATE_PATH = T / "state.json"
q.LOCK_PATH = T / "state.lock"
fails = 0


def chk(name, ok, detail=""):
    global fails
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": {str(detail)[:300]}"))
    fails += 0 if ok else 1


GOOD = {"jobs": [{"id": "a1", "status": "running"}]}
q.STATE_PATH.write_text(json.dumps(GOOD))
corrupt = lambda: sorted(p.name for p in T.glob("*corrupt*"))

# 1. a process killed mid-save (simulated: the final rename never happens) leaves the OLD state intact
_real_replace = q.os.replace
q.os.replace = lambda *a, **k: (_ for _ in ()).throw(KeyboardInterrupt("killed mid-save"))
try:
    with q._Locked() as lk:
        lk.save({"jobs": [{"id": "b2", "status": "pending"}] * 50})
except KeyboardInterrupt:
    pass
finally:
    q.os.replace = _real_replace
chk("save is tmp + atomic rename: a kill before the rename leaves the old state byte-intact",
    json.loads(q.STATE_PATH.read_text()) == GOOD, q.STATE_PATH.read_text()[:120])
with q._Locked() as lk:
    lk.save({"jobs": [{"id": "c3"}]})
chk("a completed save replaces the state", json.loads(q.STATE_PATH.read_text()) == {"jobs": [{"id": "c3"}]})

# 2. an OSError on READ (EMFILE / EIO) is not corruption: it propagates, nothing is quarantined
q.STATE_PATH.write_text(json.dumps(GOOD))
_real_read = Path.read_text


def _boom(self, *a, **k):
    if self == q.STATE_PATH:
        raise OSError(errno.EIO, "I/O error (injected)")
    return _real_read(self, *a, **k)


Path.read_text = _boom
raised = False
try:
    with q._Locked() as lk:
        lk.load()
except OSError:
    raised = True
finally:
    Path.read_text = _real_read
chk("OSError on read PROPAGATES (never 'starting empty')", raised)
chk("...and the valid state is NOT quarantined", q.STATE_PATH.exists() and not corrupt()
    and json.loads(q.STATE_PATH.read_text()) == GOOD, corrupt())

# 3. bytes that do not parse ARE quarantined (moved aside, never deleted) and load starts empty
q.STATE_PATH.write_text('{"jobs": [{"id": "a1", "sta')
with q._Locked() as lk:
    st = lk.load()
chk("torn JSON -> quarantined to *.corrupt-*.json, load returns empty", st == {"jobs": []} and len(corrupt()) == 1, (st, corrupt()))
chk("...the torn bytes are preserved in the quarantine file",
    corrupt() and _real_read(T / corrupt()[0]).startswith('{"jobs": [{"id": "a1"'))

# 4. two quarantines in the SAME SECOND must not overwrite each other (the first torn state is the
#    only copy of those bundles). Freeze the clock so the second is identical, as in the incident.
for _f in list(T.glob("*corrupt*")):
    _f.unlink()
_real_time = q.time.time
q.time.time = lambda: 1790000000.5
try:
    q.STATE_PATH.write_text('{"jobs": [{"id": "first-torn')
    with q._Locked() as lk:
        lk.load()
    q.STATE_PATH.write_text('{"jobs": [{"id": "second-torn')
    with q._Locked() as lk:
        lk.load()
finally:
    q.time.time = _real_time
_kept = sorted(_real_read(T / n) for n in corrupt())
chk("two same-second quarantines are BOTH preserved (distinct names, neither overwritten)",
    len(_kept) == 2 and _kept[0].startswith('{"jobs": [{"id": "first-torn') and _kept[1].startswith('{"jobs": [{"id": "second-torn'), (corrupt(), _kept))
chk("...and the names keep the corrupt-<epoch> shape", all(n.startswith("ollama-queue-state.corrupt-1790000000") and n.endswith(".json") for n in corrupt()), corrupt())
# a pre-existing file of the would-be name (another process, same pid reuse) is never clobbered either
_pre = q._quarantine_path(1790000001)
_pre.write_text("PRE-EXISTING")
q._QUARANTINE_SEQ[0] -= 1                              # force the same candidate name again
_again = q._quarantine_path(1790000001)
chk("an existing quarantine file is never reused as a target", _again != _pre and not _again.exists(), (_pre, _again))
_pre.unlink()

print("STATE_IO_OK" if not fails else f"FAILED: {fails}")
sys.exit(1 if fails else 0)
