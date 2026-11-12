#!/usr/bin/env python3
"""handoff-autoclear step 2b (2026-11-05): a finished job whose launching Claude session is still live goes to that
session, not to the Sonnet batch. The per-job hook runs the pass seconds after a job ends, so before this every failure
of a live session got a Sonnet triage (and sometimes a research follow-up) on top of the session's own. Pinned with
plan() on fake panel rows and sidecars, under a temp HOME (no real queue, panel, cache or claude):
  * a live launcher inside the grace: kept with a 'launcher live' reason, and not sent to the LLM
  * the same job past the grace, with a gone socket, with no launched_by, with no finish time or a finish time in the
    future: sent to the LLM exactly as before
  * the grace boundary: GRACE - 1 s waits, GRACE does not
  * when every candidate's launcher is live the LLM is not called at all
  * a mechanical rule still clears a live launcher's superseded job, and the guards still come first
  * with the default socket check, a real socket file counts as live and a deleted one as gone
AUTOCLEAR=<path> points at a file under test (revert-check: the copy before 2b must FAIL).
Run: python3 test-handoff-autoclear-launcher.py      prints ALL PASS / N FAILED (rc 0 / 1)
"""
import importlib.util
import json
import os
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
AC = Path(os.environ.get("AUTOCLEAR") or HERE / "handoff-autoclear.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main():
    td = Path(tempfile.mkdtemp(prefix="hac-launcher-"))
    os.environ["HOME"] = str(td)          # WORK, the LLM cache and the call stamps resolve under it at import
    ac = load(AC, "ac_launcher_t")
    if not str(ac.WORK).startswith(str(td)):
        print("FAIL - the module's WORK dir is not under the temp HOME; refusing to run (%s)" % ac.WORK)
        return 1
    grace = getattr(ac, "LAUNCHER_GRACE_S", None)
    chk("LAUNCHER_GRACE_S exists and is a few hours", isinstance(grace, int) and 3600 <= grace <= 12 * 3600, True)
    grace = grace if isinstance(grace, int) else 3 * 3600

    now = 2_000_000_000.0
    live, gone = str(td / "live.sock"), str(td / "gone.sock")

    def row(i, **kw):
        return dict({"id": i, "label": "bench-real-" + i, "status": "failed", "gate": "FAIL", "files": 0,
                     "cwd": "/w/" + i}, **kw)

    def sc(i, cwd=None, **kw):
        return dict({"id": i, "cwd": cwd or "/w/" + i, "status": "failed", "exit_code": 1}, **kw)

    rows = [row("a1"), row("b1"), row("c1"), row("d1"), row("e1"), row("f1"), row("g1"),
            row("h1", awaiting_signoff=True), row("i1"), row("j1")]
    scs = [sc("a1", launched_by=live, _ts=now - 600),
           sc("b1", launched_by=live, _ts=now - grace - 1),         # past the grace
           sc("c1", launched_by=gone, _ts=now - 600),               # the session is gone
           sc("d1", _ts=now - 600),                                 # never stamped
           sc("e1", launched_by=live),                              # no finish time
           sc("f1", launched_by=live, _ts=now + 120),               # finish time in the future (clock skew)
           sc("g1", launched_by=live, _ts=now - 600),
           sc("g2", cwd="/w/g1", status="done", exit_code=0, _ts=now - 300),   # supersedes g1
           sc("h1", launched_by=live, _ts=now - 600),
           sc("i1", launched_by=live, _ts=now - grace + 1),         # 1 s inside the grace
           sc("j1", launched_by=live, _ts=now - grace)]             # exactly at the grace: no longer waits
    sidecars = {"__ts__": {s["id"]: s.get("_ts", 0) for s in scs}}
    for s in scs:
        sidecars.setdefault(s["cwd"], []).append(s)

    prompts = []

    def fake_claude(prompt, timeout=420, model="sonnet"):
        prompts.append(prompt)
        ids = [d["id"] for d in json.loads(prompt.split("JOBS:\n", 1)[1])]
        return 0, json.dumps([{"id": i, "decision": "keep", "reason": "fake keep"} for i in ids])

    def plan(data, **kw):
        try:
            return ac.plan(data, [], sidecars, {}, use_llm=True, claude=fake_claude, now=now, **kw)
        except TypeError as e:
            print("FAIL - plan() does not take the step-2b arguments: %s" % e)
            FAILS.append("plan signature")
            return {"clear": [], "followup": [], "keep": [], "llm": "error"}

    p = plan({"complete": rows, "acted": {}}, sock_exists=lambda s: s == live)
    keep = {r["id"]: why for r, why in p["keep"]}
    chk("a live launcher inside the grace is kept as 'launcher live'", keep.get("a1", "").startswith("launcher live"), True)
    chk("1 s inside the grace still waits", keep.get("i1", "").startswith("launcher live"), True)
    chk("a guard still comes first (sign-off)", keep.get("h1"), "awaiting sign-off")
    chk("a mechanical rule still clears a live launcher's superseded job",
        [(r["id"], why.split(":")[0]) for r, why in p["clear"]], [("g1", "superseded")])
    asked = sorted(d["id"] for pr in prompts for d in json.loads(pr.split("JOBS:\n", 1)[1]))
    chk("exactly the past-grace / gone / unstamped / no-time / future-time / at-grace jobs reach the LLM",
        asked, ["b1", "c1", "d1", "e1", "f1", "j1"])
    chk("those come back as LLM keeps", sorted(i for i, why in keep.items() if why == "llm: fake keep"),
        ["b1", "c1", "d1", "e1", "f1", "j1"])

    prompts.clear()
    p2 = plan({"complete": [row("a1"), row("i1")], "acted": {}}, sock_exists=lambda s: s == live)
    chk("only live launchers left: the LLM is not called", (len(prompts), p2["llm"]), (0, "not run"))

    # the default socket check: a real file is live, a deleted one is gone
    Path(live).write_text("")
    w = ac.launcher_waiting({"launched_by": live, "_ts": now - 60}, now=now)
    chk("default check: an existing socket file waits", bool(w) and w.startswith("launcher live"), True)
    Path(live).unlink()
    chk("default check: a deleted socket does not", ac.launcher_waiting({"launched_by": live, "_ts": now - 60}, now=now), None)
    chk("junk finish time does not wait", ac.launcher_waiting({"launched_by": live, "_ts": "soon"}, now=now,
                                                             exists=lambda s: True), None)
    chk("no sidecar does not wait", ac.launcher_waiting(None, now=now, exists=lambda s: True), None)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
