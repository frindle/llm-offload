#!/usr/bin/env python3
"""harness-audit-replay: replay the STATIC wave-3 audit gates (A traceability, B prose constants,
C flake lint) over every preserved dispatch worktree that has TASK.md + a fixture, and report the
fire rate per gate.  Read-only (git show / file reads only).  The dynamic gates (C 3x verify,
D trivial battery, E revalidate) execute verifies and are measured by preflight itself.

    python3 harness-audit-replay.py [--root DIR] [--json] [--limit N]

Use this BEFORE promoting any gate from WARN to blocking: a gate may only block when its fire rate
on known-good (GO / landed) harnesses is ~0 and on known-bad (escalated) harnesses is meaningful."""
import argparse, importlib.util, json, subprocess, sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
sp = importlib.util.spec_from_file_location("hag", BIN / "harness_audit_gates.py")
g = importlib.util.module_from_spec(sp); sp.loader.exec_module(g)


def sh(wt, *a):
    r = subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(Path.home() / ".ollama-dispatch/worktrees"))
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    a = ap.parse_args()
    rows, fire = [], {"spec-traceability": 0, "prose-constants": 0, "flake-lint": 0}
    n = 0
    for wt in sorted(Path(a.root).glob("*/")):
        fxn = next((x for x in ("test_fixture.py", "verify.test.ts", "verify.test.mts") if (wt / x).is_file()), None)
        if not fxn or not (wt / "TASK.md").is_file():
            continue
        if a.limit and n >= a.limit:
            break
        n += 1
        try:
            man = json.loads((wt / ".dispatch-harness.json").read_text())
        except (OSError, ValueError):
            man = {}
        tgt = man.get("target")
        lang = man.get("lang") or ("python" if fxn.endswith(".py") else "ts")
        task, fx = (wt / "TASK.md").read_text(errors="replace"), (wt / fxn).read_text(errors="replace")
        base = sh(wt, "show", f"HEAD:{tgt}") if tgt else ""
        res = {}
        if tgt:
            res["spec-traceability"] = g.untraceable_assertions(fx, task, base, tgt, lang)
        res["prose-constants"] = g.constant_conflicts(task, fx)
        res["flake-lint"] = g.flake_lint(fx, fxn)
        for k, v in res.items():
            if v:
                fire[k] += 1
        rows.append({"worktree": wt.name, "fixture": fxn, **{k: len(v) for k, v in res.items()},
                     "first": {k: (v[0].get("token") or v[0].get("detail", ""))[:60] for k, v in res.items() if v}})
    out = {"scanned": n, "fired_worktrees": fire,
           "rate": {k: (round(v / n, 3) if n else 0) for k, v in fire.items()}, "rows": rows}
    if a.json:
        print(json.dumps(out, indent=1))
    else:
        print(f"scanned {n} worktrees with TASK.md + fixture")
        for k, v in fire.items():
            print(f"  {k:20s} fired on {v:4d}  ({out['rate'][k]:.1%})")
        for r in rows[:15]:
            hit = {k: r.get(k) for k in fire if r.get(k)}
            if hit:
                print(" ", r["worktree"], hit, r["first"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
