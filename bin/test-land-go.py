#!/usr/bin/env python3
"""Behaviour tests for land_go.py: the HARNESS GO decision + execution."""
import json, os, sys, tempfile
from pathlib import Path
BIN = Path(os.environ.get("LAND_BIN_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(BIN))
import land_go as L

FAILS = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ((" -- " + str(extra)[:400]) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)

def state(**kw):
    vr = {"verdict": "relevant", "score": 0.95, "threshold": 0.8, "killed": 19, "evidence_mutants": 20, "survivors": [], "truncated": False,
          "necessity": {"enabled": True, "checked": 3, "necessary": 3, "unnecessary": [], "untested": []},
          "wrong_bank": {"enabled": True, "variants": [{}], "survivors": []}}
    vr.update(kw.pop("vr", {}))
    s = {"verdict": "GO", "blockers": [], "verify_failed_at_baseline": True, "verify_relevance": vr}
    s.update(kw)
    return s

H = {"target": "lib/a.ts", "creation_task": False}
d = L.decide_go(state(), H)
check("clean GO + relevance + necessity + bank -> ENQUEUE", d["action"] == "enqueue" and not d["reasons"], d)
check("score below threshold -> REAUTHOR", L.decide_go(state(vr={"score": 0.7, "killed": 14}), H)["action"] == "reauthor")
check("score exactly at the strict floor passes", L.decide_go(state(vr={"score": 0.85}), H)["action"] == "enqueue")
check("record threshold above our floor wins", L.decide_go(state(vr={"score": 0.86, "threshold": 0.9}), H)["action"] == "reauthor")
check("unnecessary hunk -> REAUTHOR", L.decide_go(state(vr={"necessity": {"checked": 3, "necessary": 2, "unnecessary": [{"f": 1}], "untested": []}}), H)["action"] == "reauthor")
check("wrong-solution survivor -> REAUTHOR", L.decide_go(state(vr={"wrong_bank": {"variants": [{}], "survivors": ["x"]}}), H)["action"] == "reauthor")
s = state(); del s["verify_relevance"]["necessity"]
d = L.decide_go(s, H)
check("necessity NOT MEASURED (old preflight state) -> REAUTHOR, never silently enqueue", d["action"] == "reauthor" and any("necessity" in r for r in d["reasons"]), d)
s = state(); del s["verify_relevance"]["wrong_bank"]
check("wrong-bank NOT MEASURED -> REAUTHOR", L.decide_go(s, H)["action"] == "reauthor")
check("verify not failing at baseline -> REAUTHOR", L.decide_go(state(verify_failed_at_baseline=False), H)["action"] == "reauthor")
check("creation task exempt from fail-before", L.decide_go(state(verify_failed_at_baseline=False), {"creation_task": True, "target": "a.py"})["action"] == "enqueue")
check("NO-GO verdict -> REAUTHOR", L.decide_go(state(verdict="NO-GO"), H)["action"] == "reauthor")
check("blockers -> REAUTHOR", L.decide_go(state(blockers=[{"check": "x"}]), H)["action"] == "reauthor")
d = L.decide_go(state(vr={"score": None, "evidence_mutants": 0, "killed": 0, "verdict": "unproven", "necessity": {"checked": 0, "necessary": 0, "unnecessary": [], "untested": []}}), H)
check("0/0 relevance with nothing per-hunk measured -> REAUTHOR (not measured)", d["action"] == "reauthor" and any("NOT measured" in r for r in d["reasons"]), d)
d = L.decide_go(state(vr={"score": None, "evidence_mutants": 0, "killed": 0, "verdict": "unproven"}), {"target": "lib/a.test.ts"})
check("locked test-file target: relevance N/A", d["action"] == "enqueue", d)
d = L.decide_go(state(vr={"score": None, "evidence_mutants": 0, "verdict": "unproven", "necessity": {"checked": 2, "necessary": 2, "unnecessary": [], "untested": []}}), H)
check("no line-mutants but every hunk proven necessary -> ENQUEUE", d["action"] == "enqueue", d)
check("unproven for a non-budget reason -> REAUTHOR", L.decide_go(state(vr={"verdict": "unproven", "reason": "refimpl not green", "truncated": False}), H)["action"] == "reauthor")
check("unproven only because the time budget truncated -> ENQUEUE", L.decide_go(state(vr={"verdict": "unproven", "truncated": True}), H)["action"] == "enqueue")

# ---------- execution with injected runners
with tempfile.TemporaryDirectory() as td:
    td = Path(td)
    wt = td / "wt"; wt.mkdir()
    (wt / ".preflight-state.json").write_text(json.dumps(state()))
    (wt / ".dispatch-harness.json").write_text(json.dumps(H))
    (wt / "TASK.md").write_text("t")
    runs = td / "auto-runs"; (runs / "argv").mkdir(parents=True)
    (runs / "argv" / "lab.json").write_text(json.dumps({"label": "lab", "bundle": "bun", "argv": ["--repo", "/r", "--require", "foo", "--label", "lab", "--model", "m1", "--host", "h1"]}))
    L.DISPATCH = td; L.BIN = Path("/x/bin")
    cmds = []
    def runner(cmd, **kw):
        cmds.append(cmd)
        if "status" in cmd: return 0, "[running ] abc123456789 other-label foo\n"
        if "ollama-dispatch-preflight" in cmd[1]: return 0, json.dumps(state())
        return 0, "ok"
    r = L.execute_go(wt, "lab", runner=runner, ledger=td / "led.jsonl", runs_dir=runs)
    names = [Path(c[1]).name if len(c) > 1 else c[0] for c in cmds]
    check("ENQUEUE ran draft --confirm, preflight --auto-seal, then enqueue (in order)",
          r["ok"] and names == ["ollama-queue.py", "ollama-dispatch-draft", "ollama-dispatch-preflight", "ollama-queue.py"], (r, names))
    pf = next(c for c in cmds if "ollama-dispatch-preflight" in c[1]); eq = cmds[-1]
    check("preflight got --auto-seal and the recorded --require", "--auto-seal" in pf and "--require=foo" in pf, pf)
    check("enqueue is coding, bundled, with the recorded model/host", eq[eq.index("--bundle") + 1] == "bun" and eq[eq.index("--model") + 1] == "m1" and eq[eq.index("--host") + 1] == "h1" and "--label" in eq, eq)
    cmds.clear()
    def runner_dupe(cmd, **kw):
        cmds.append(cmd)
        return 0, "[queued  ] abc123456789 lab foo\n"
    r = L.execute_go(wt, "lab", runner=runner_dupe, ledger=td / "led.jsonl", runs_dir=runs)
    check("label already in the queue -> no second enqueue", r["ok"] and "already" in r["detail"] and len(cmds) == 1, (r, cmds))
    # recheck after confirm not clean -> no enqueue
    cmds.clear()
    def runner_bad(cmd, **kw):
        cmds.append(cmd)
        if "status" in cmd: return 0, ""
        if "ollama-dispatch-preflight" in cmd[1]: return 1, json.dumps(state(verdict="NO-GO"))
        return 0, "ok"
    r = L.execute_go(wt, "lab", runner=runner_bad, ledger=td / "led.jsonl", runs_dir=runs)
    check("re-check after --confirm not GO -> no enqueue, routed back", not r["ok"] and r["decision"] == "reauthor" and not any("enqueue" in c for c in cmds), (r, cmds))
    # reauthor path
    (wt / ".preflight-state.json").write_text(json.dumps(state(vr={"score": 0.5})))
    spawned = []
    r = L.execute_go(wt, "lab", runner=runner, spawner=lambda c, lp: spawned.append(c), ledger=td / "led2.jsonl", runs_dir=runs)
    check("below threshold -> driver relaunched with --resume-harness (no human)", r["ok"] and spawned and spawned[0][-1] == "--resume-harness" and "--label" in spawned[0], (r, spawned))
    for _ in range(4):
        r = L.execute_go(wt, "lab", runner=runner, spawner=lambda c, lp: spawned.append(c), ledger=td / "led2.jsonl", runs_dir=runs)
    check("re-author is capped per label per day", not r["ok"] and "cap" in r["detail"], r)
    r = L.execute_go(wt, "nolab", runner=runner, spawner=lambda c, lp: spawned.append(c), ledger=td / "led3.jsonl", runs_dir=runs)
    check("no recorded argv -> reported, not crashed", not r["ok"] and "no recorded" in r["detail"], r)
    n = len(spawned)
    r = L.execute_go(wt, "lab", runner=runner, spawner=lambda c, lp: spawned.append(c), ledger=td / "led4.jsonl", runs_dir=runs, dry=True)
    check("dry run changes nothing", r["ok"] and len(spawned) == n)
    (runs / "argv" / "lab2.json").write_text(json.dumps({"label": "lab2", "argv": ["--intent", "do X", "--label", "lab2"]}))
    cmd, _ = L.reauthor_cmd("lab2", runs, amend="use the clamp")
    check("re-spec relaunch appends the amendment to --intent and uses --resume-author",
          cmd[-1] == "--resume-author" and "ADJUDICATOR AMENDMENT: use the clamp" in cmd[cmd.index("--intent") + 1] and cmd[cmd.index("--intent") + 1].startswith("do X"), cmd)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
