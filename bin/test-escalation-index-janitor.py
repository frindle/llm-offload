#!/usr/bin/env python3
"""escalation_index_janitor (2026-10-05): ESCALATIONS.md rows auto-tick ONLY on
positive evidence of resolution, never on missing evidence.

Hermetic: temp slice-runs / slice-plans / queue-state / index; touches nothing live,
enqueues nothing. Asserts both ways for every rule, plus the fail-closed guards
(unreadable queue state, unreadable plan state) and the concurrency property (a row
appended after the scan survives the in-place flip).

Usage:  python3 ~/bin/test-escalation-index-janitor.py
Revert: JANITOR=/path/to/reverted_copy.py python3 this.py   -> must print FAIL
"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = Path(os.environ.get("JANITOR", HERE / "escalation_index_janitor.py"))
spec = importlib.util.spec_from_file_location("eij", str(MOD))
J = importlib.util.module_from_spec(spec)
spec.loader.exec_module(J)

fails = 0


def chk(name, got, want):
    global fails
    ok = got == want
    print(("ok   - " if ok else "FAIL - ") + name + ("" if ok else f": want {want!r} got {got!r}"))
    fails += 0 if ok else 1


T = Path(tempfile.mkdtemp(prefix="eij-"))
runs, plans = T / "runs", T / "plans"
runs.mkdir()
plans.mkdir()


def plan(label, slices, integ=None):
    st = {"label": label, "slices": {k: {"status": v} for k, v in slices.items()}}
    if integ is not None:
        st["integration"] = integ
    (runs / f"{label}.json").write_text(json.dumps(st))


plan("pA", {"s1-done": "done", "s2-esc": "escalated", "s3-skip": "skipped", "s4-sub": "escalated"})
plan("pA-s4-sub", {"x1": "pending"})                # live sub-plan of pA/s4-sub
plan("pStaged", {"s1": "done"}, {"status": "staged"})
plan("pLanded", {"s1": "done"}, {"status": "passed", "landed": "abc123def4567"})
plan("pIntFail", {"s1": "done"}, {"status": "failed"})
plan("pCanc", {"s1": "escalated"})
(plans / "pGen.slices.json").write_text("{}")       # plan-gen produced pGen
cancelled = {"pCanc"}
qstate = T / "q.json"
qstate.write_text(json.dumps({
    "jobs": [{"id": "aaaaaaaaaaaa", "status": "needs_opus", "label": "pA-s2-esc"},
             {"id": "bbbbbbbbbbbb", "status": "done", "label": "oneoff"},
             {"id": "cccccccccccc", "status": "failed", "label": "oneoff2"},
             {"id": "dddddddddddd", "status": "failed", "label": "plan-gen-pNone-r1"}],
    "_bundle_parked": {"bParked": {"why": "x"}}}))


def facts(q=qstate):
    return J.Facts(runs_dir=runs, plans_dir=plans, queue_state=q,
                   cancelled=lambda p: {"label": p} if p in cancelled else None)


TS = "2026-10-04T10:00:00Z"


def row(plan_, who, src, ts=TS, tail="verdict - `/x.md`"):
    return f"- [ ] `{plan_}` **{who}** {src} - {ts} - {tail}"


CASES = [
    # (name, row, expect_close)
    ("slice done -> close", row("pA", "s1-done", "A"), True),
    ("slice skipped -> close", row("pA", "s3-skip", "B"), True),
    ("slice still escalated -> OPEN", row("pA", "s2-esc", "A"), False),
    ("slice re-sliced into live sub-plan -> close", row("pA", "s4-sub", "A"), True),
    ("slice of cancelled plan -> close", row("pCanc", "s1", "A"), True),
    ("unknown plan slice -> OPEN", row("pNope", "s1", "A"), False),
    ("job row, slice done -> close", row("pA-s1-done", "eeeeeeeeeeee", "D"), True),
    ("job row, auto-refine label of done slice -> close",
     row("auto-refine-pA-s1-done-r2", "ffffffffffff", "D"), True),
    ("job row, slice escalated + job needs_opus -> OPEN", row("pA-s2-esc", "aaaaaaaaaaaa", "D"), False),
    ("job row, non-slice job done -> close", row("oneoff", "bbbbbbbbbbbb", "D"), True),
    ("job row, non-slice job failed -> OPEN", row("oneoff2", "cccccccccccc", "D"), False),
    ("job row, job gone from live state -> close", row("oneoff3", "999999999999", "D"), True),
    ("plan-gen job, plan produced -> close", row("plan-gen-pGen-r1", "dddddddddddd", "D"), True),
    ("plan-gen job, no plan + job failed -> OPEN", row("plan-gen-pNone-r1", "dddddddddddd", "D"), False),
    ("CHAIN STAGED, not landed -> OPEN", row("pStaged", "CHAIN STAGED -- ready to land", "E"), False),
    ("CHAIN STAGED, landed -> close", row("pLanded", "CHAIN STAGED -- ready to land", "E"), True),
    ("WHOLE-CHAIN failed -> OPEN", row("pIntFail", "WHOLE-CHAIN integration gate", "E"), False),
    ("WHOLE-CHAIN now staged -> close", row("pStaged", "WHOLE-CHAIN integration gate", "E"), True),
    ("integration of cancelled plan -> close", row("pCanc", "WHOLE-CHAIN integration gate", "E"), True),
    ("BUNDLE PARKED, no longer parked -> close", row("bGone", "BUNDLE PARKED", "Q"), True),
    ("BUNDLE PARKED, still parked (newest) -> OPEN", row("bParked", "BUNDLE PARKED", "Q"), False),
    ("BUNDLE PARKED, plan-gen bundle whose plan exists -> close",
     row("plan-gen-pGen", "BUNDLE PARKED", "Q"), True),
]
rows = [r for _n, r, _e in CASES]
# an OLDER duplicate park of a still-parked bundle -> superseded -> close
old_dup = row("bParked", "BUNDLE PARKED", "Q", ts="2026-10-04T09:00:00Z")
closed_ln = "- [x] `pA` **s2-esc** A - 2026-10-01T00:00:00Z - already closed - `/y.md`"
idx = T / "ESCALATIONS.md"
idx.write_text("\n".join([closed_ln, old_dup] + rows) + "\n")

res = J.run([idx], facts=facts(), apply=True, log_path=T / "log.jsonl", out=lambda *_: None)
after = idx.read_text().splitlines()
for (name, r, want) in CASES:
    closed_r = "- [x] " + r[len("- [ ] "):]
    state = closed_r in after
    chk(name, state, want)
chk("older duplicate of a still-parked bundle -> close (superseded)",
    ("- [x] " + old_dup[6:]) in after, True)
chk("already-closed row untouched", closed_ln in after, True)
chk("row count unchanged (in-place flip, nothing dropped)", len(after), len(CASES) + 2)
chk("every close is logged", len((T / "log.jsonl").read_text().splitlines()), len(res))

# FAIL-CLOSED: unreadable queue state closes no Q/job row that depends on it
idx2 = T / "E2.md"
idx2.write_text("\n".join([row("bGone", "BUNDLE PARKED", "Q"),
                           row("oneoff3", "999999999999", "D")]) + "\n")
J.run([idx2], facts=facts(q=T / "missing.json"), apply=True, log_path=T / "l2", out=lambda *_: None)
chk("unreadable queue state -> BUNDLE row stays OPEN",
    idx2.read_text().splitlines()[0].startswith("- [ ] "), True)
chk("unreadable queue state -> vanished-job row stays OPEN",
    idx2.read_text().splitlines()[1].startswith("- [ ] "), True)

# FAIL-CLOSED: corrupt plan state -> slice row stays open
(runs / "pBroken.json").write_text("{not json")
idx3 = T / "E3.md"
idx3.write_text(row("pBroken", "s1", "A") + "\n")
J.run([idx3], facts=facts(), apply=True, log_path=T / "l3", out=lambda *_: None)
chk("corrupt plan state -> slice row stays OPEN", idx3.read_text().startswith("- [ ] "), True)

# CONCURRENCY: a row appended between scan and flip survives; a row CHANGED under
# us is not flipped.
idx4 = T / "E4.md"
r_done = row("pA", "s1-done", "A")
idx4.write_text(r_done + "\n")
orig_flip = J._flip


def flip_with_append(path, off, line):
    with open(path, "a") as fh:
        fh.write(row("pA", "s2-esc", "A", tail="appended concurrently - `/z.md`") + "\n")
    return orig_flip(path, off, line)


J._flip = flip_with_append
J.run([idx4], facts=facts(), apply=True, log_path=T / "l4", out=lambda *_: None)
J._flip = orig_flip
l4 = idx4.read_text().splitlines()
chk("concurrent append survives the flip", len(l4) == 2 and "appended concurrently" in l4[1], True)
chk("...and the resolved row was flipped", l4[0].startswith("- [x] "), True)
idx5 = T / "E5.md"
idx5.write_text(r_done + "\n")
chk("row changed under us -> not flipped", J._flip(idx5, 0, r_done.replace("s1-done", "s1-dXne")), False)

# DRY RUN changes nothing
idx6 = T / "E6.md"
idx6.write_text(r_done + "\n")
J.run([idx6], facts=facts(), apply=False, out=lambda *_: None)
chk("dry run leaves the file untouched", idx6.read_text(), r_done + "\n")

# WATCHER WIRING: run_once's index_janitor() ticks rows against the watcher's own
# (overridable) SLICE_RUNS and index paths; the kill switch disables it.
WATCHER = Path(os.environ.get("WATCHER", HERE / "dispatch-escalation-watcher.py"))
try:
    from importlib.machinery import SourceFileLoader
    _ld = SourceFileLoader("wtch", str(WATCHER))
    _sp = importlib.util.spec_from_loader("wtch", _ld)
    W = importlib.util.module_from_spec(_sp)
    _ld.exec_module(W)
except Exception as e:
    W = None
    print(f"(could not load watcher {WATCHER}: {e})")
widx, wready = T / "W-ESC.md", T / "W-READY.md"
widx.write_text(row("pA", "s1-done", "A") + "\n" + row("pA", "s2-esc", "A") + "\n")
wready.write_text(row("pLanded", "CHAIN STAGED -- ready to land", "E") + "\n")
fn = getattr(W, "index_janitor", None) if W else None
chk("watcher defines index_janitor()", callable(fn), True)
if callable(fn):
    W.SLICE_RUNS, W.INDEX_MD, W.READY_MD = runs, widx, wready
    os.environ["DISPATCH_INDEX_JANITOR"] = "off"
    fn()
    chk("kill switch off -> nothing ticked", widx.read_text().count("- [x] "), 0)
    os.environ.pop("DISPATCH_INDEX_JANITOR")
    fn()
    wl = widx.read_text().splitlines()
    chk("watcher pass ticks the done slice row", wl[0].startswith("- [x] "), True)
    chk("watcher pass leaves the escalated slice row open", wl[1].startswith("- [ ] "), True)
    chk("watcher pass ticks the landed READY-TO-LAND row",
        wready.read_text().startswith("- [x] "), True)
    src = WATCHER.read_text()
    chk("run_once calls index_janitor() on a non-dry pass",
        bool(__import__("re").search(r"if not dry_run:\n(?:\s+\w+\(\)\n)*\s+index_janitor\(\)", src)),
        True)

print("\nALL PASS" if not fails else f"\n{fails} FAIL")
sys.exit(1 if fails else 0)
