#!/usr/bin/env python3
"""READY-TO-LAND rows must not outlive the stage they announce (2026-10-05,
chat-frontend-plan: row open a day after main absorbed the chain by hand).

escalation_index_janitor (run every watcher pass) now closes a CHAIN STAGED row when
the integration is `passed` (incl. "already on main"), when its stage branch was
superseded by a re-stage, or when it was retired (stale/failed + branch gone); and a
still-`staged` row whose stage git proves is no longer a fast-forward of main is
RE-STAGED (stubbed here -- tests never run the slicer) and re-decided.

Uses a REAL temp git repo for the live landability check. Never touches the live
index, slice-runs or queue.

Revert-test: JANITOR_SRC=<backup> python3 test-escalation-ready-stale-retire.py -> FAILs.
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SRC = Path(os.environ.get("JANITOR_SRC") or Path(__file__).resolve().parent / "escalation_index_janitor.py")
FAILS = []


def check(n, ok):
    print(("ok  " if ok else "FAIL") + ": " + n)
    if not ok:
        FAILS.append(n)


def g(repo, *a):
    return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout


def row(plan, br):
    return (f"- [ ] `{plan}` **CHAIN STAGED -- ready to land** E - 2026-10-04T12:36:17Z - "
            f"READY TO LAND -- the converged chain is STAGED for landing ({br} onto main; "
            f"main untouched) -- review - `/x/{plan}.md`\n")


def main():
    ld = SourceFileLoader("eij_t", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("eij_t", ld))
    ld.exec_module(m)
    with tempfile.TemporaryDirectory() as td:
        repo = os.path.join(td, "repo")
        os.makedirs(repo)
        g(repo, "init", "-q", "-b", "main")
        g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "base")
        g(repo, "branch", "integrate/slice-pa-aaaaaaaa")          # landable stage of pA
        g(repo, "branch", "integrate/slice-pb-bbbbbbbb")          # stage of pB ...
        g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "main moved")
        # ... main moved: pA's stage is now NOT a fast-forward either; give pA a fresh one
        g(repo, "branch", "integrate/slice-pa-cccccccc")
        runs = Path(td, "runs")
        runs.mkdir()

        def put(plan, integ):
            Path(runs, f"{plan}.json").write_text(json.dumps(
                {"label": plan, "repo": repo, "plan_path": f"/nope/{plan}.slices.json",
                 "slices": {"s1": {"status": "done"}}, "integration": integ}))

        put("pp", {"status": "passed", "summary": "already on main (redundant)", "landed": None,
                   "integrate_branch": "integrate/slice-pp-dddddddd"})
        put("pa", {"status": "staged", "onto": "main", "integrate_branch": "integrate/slice-pa-cccccccc"})
        put("pr", {"status": "stale", "onto": "main", "integrate_branch": "integrate/slice-pr-eeeeeeee"})
        put("pb", {"status": "staged", "onto": "main", "integrate_branch": "integrate/slice-pb-bbbbbbbb"})
        put("pk", {"status": "staged", "onto": "main", "integrate_branch": "integrate/slice-pa-cccccccc"})
        ready = Path(td, "READY-TO-LAND.md")
        ready.write_text(row("pp", "integrate/slice-pp-dddddddd")      # passed -> close
                         + row("pa", "integrate/slice-pa-aaaaaaaa")    # superseded -> close
                         + row("pr", "integrate/slice-pr-eeeeeeee")    # stale + branch gone -> close
                         + row("pb", "integrate/slice-pb-bbbbbbbb")    # staged but main moved -> restage
                         + row("pk", "integrate/slice-pa-cccccccc"))   # staged & landable -> stays

        restaged = []

        class F(m.Facts):
            def restage(self, plan):
                restaged.append(plan)
                if plan == "pb":    # the slicer would re-classify it: already on main
                    put("pb", {"status": "passed", "summary": "already on main (redundant)"})
                self._runs.pop(plan, None)
                return 0

        mk = lambda: F(runs_dir=runs, plans_dir=Path(td, "plans"),
                       queue_state=Path(td, "q.json"), cancelled=lambda p: False)
        has = all(hasattr(m.Facts, a) for a in ("stage_landable", "branch_exists", "restage"))
        check("Facts has live git checks + restage", has)
        if has:
            f0 = mk()
            check("live check: fresh stage is landable", f0.stage_landable(repo, "main", "integrate/slice-pa-cccccccc") is True)
            check("live check: stage under a moved main is NOT landable",
                  f0.stage_landable(repo, "main", "integrate/slice-pb-bbbbbbbb") is False)
        dry = m.run([ready], facts=mk(), apply=False, out=lambda s: None)
        check("dry run closes nothing on disk", ready.read_text().count("- [ ] ") == 5)
        check("dry run never re-stages", restaged == [])
        closed = m.run([ready], facts=mk(), apply=True, log_path=Path(td, "log.jsonl"), out=lambda s: None)
        txt = ready.read_text().splitlines()
        state = {l.split("`")[1]: l[:5] for l in txt}
        check("passed ('already on main') row closed", state.get("pp") == "- [x]")
        check("superseded-stage row closed", state.get("pa") == "- [x]")
        check("retired (stale, branch gone) row closed", state.get("pr") == "- [x]")
        check("moved-main stage was re-staged", restaged == ["pb"])
        check("re-staged row closed after re-decide", state.get("pb") == "- [x]")
        check("a still-landable staged row stays OPEN", state.get("pk") == "- [ ]")
        check("4 closes logged", len(closed) == 4)

        # ROWLESS STAGE (2026-10-06, pipeline-canary --hand-land): a chain main absorbed
        # by hand BEFORE any CHAIN STAGED row existed must be re-staged from the plan
        # record itself, so the watcher's later detection never posts a false row.
        g(repo, "checkout", "-q", "-b", "integrate/slice-ph-ffffffff")
        g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "ph work")
        g(repo, "checkout", "-q", "main")
        g(repo, "-c", "user.email=t@t", "-c", "user.name=t", "merge", "-q", "--no-ff",
          "-m", "hand land", "integrate/slice-ph-ffffffff")
        put("ph", {"status": "staged", "onto": "main", "integrate_branch": "integrate/slice-ph-ffffffff"})
        ready2 = Path(td, "esc2", "READY-TO-LAND.md")
        ready2.parent.mkdir()
        ready2.write_text("")
        restaged.clear()

        class F2(F):
            def restage(self, plan):
                restaged.append(plan)
                if plan == "ph":
                    put("ph", {"status": "passed", "summary": "already on main"})
                elif plan == "pk":      # main moved under pk too: slicer leaves it staged
                    pass
                self._runs.pop(plan, None)
                return 0
        mk2 = lambda: F2(runs_dir=runs, plans_dir=Path(td, "plans"),
                         queue_state=Path(td, "q.json"), cancelled=lambda p: False)
        m.run([ready2], facts=mk2(), apply=False, out=lambda s: None)
        check("rowless: dry run never re-stages", restaged == [])
        m.run([ready2], facts=mk2(), apply=True, out=lambda s: None)
        check("rowless: hand-landed staged chain re-staged with no index row",
              "ph" in restaged)
        check("rowless: its record is no longer `staged`",
              json.loads(Path(runs, "ph.json").read_text())["integration"]["status"] == "passed")
        n = len(restaged)
        m.run([ready2], facts=mk2(), apply=True, out=lambda s: None)
        check("rowless: an unchanged re-stage is not retried every pass (bounded)",
              len(restaged) == n)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED")
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
