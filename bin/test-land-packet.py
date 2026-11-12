#!/usr/bin/env python3
"""Behaviour tests for land_packet.py: diff-scope facts, risk tiers, repo checks, packet build."""
import json, os, subprocess, sys, tempfile
from pathlib import Path
BIN = Path(os.environ.get("LAND_BIN_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(BIN))
import land_packet as P

FAILS = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ((" -- " + str(extra)[:400]) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)

def mkdiff(path, removed=(), added=()):
    body = "".join(f"-{l}\n" for l in removed) + "".join(f"+{l}\n" for l in added)
    return f"diff --git a/{path} b/{path}\n--- a/{path}\n+++ b/{path}\n@@ -1,{len(removed)} +1,{len(added)} @@\n{body}"

# ---------- analyze_diff / risk
f = P.analyze_diff(mkdiff("lib/a.test.ts", removed=["expect(x).toBe(1)", "expect(y).toBe(2)"], added=["expect(x).toBe(1)"]))
check("removed assertions in a test file == tests weakened", f["tests_weakened"], f)
f = P.analyze_diff(mkdiff("lib/a.test.ts", added=["it.skip('x', () => {})"]))
check("added .skip == tests weakened", f["tests_weakened"], f)
f = P.analyze_diff(mkdiff("lib/a.test.ts", added=["it('x', () => { expect(1).toBe(1) })"]))
check("purely additive test is NOT weakened and is test_only_additive", not f["tests_weakened"] and f["test_only_additive"], f)
f = P.analyze_diff(mkdiff("src/k.py", added=["AWS = 'AKIAABCDEFGHIJKLMNOP'"]))
check("secret scan flags an AWS key id", f["secrets"], f)
f = P.analyze_diff(mkdiff("src/k.py", added=["password = 'hunter2hunter2hunter2'"]))
check("secret scan flags password literal", f["secrets"], f)
f = P.analyze_diff(mkdiff(".github/workflows/ci.yml", added=["run: x"]))
check("deny-list: CI workflow", f["deny_hits"] == [".github/workflows/ci.yml"], f)
f = P.analyze_diff(mkdiff("a.py", added=["x=1"]) + mkdiff("b.py", added=["y=1"]), declared_scope=["a.py"])
check("file outside declared scope listed", f["outside_scope"] == ["b.py"], f)
f = P.analyze_diff(mkdiff("a.py", added=["x=1"]) + mkdiff("TASK.md", added=["scaffold"]), declared_scope=["a.py"])
check("scaffold files are not 'outside scope'", f["outside_scope"] == [], f)

r = P.risk(P.analyze_diff(mkdiff("README.md", added=["hi"])))
check("docs-only is T0 and low risk", r["tier"] == "T0" and r["score"] < 5, r)
r = P.risk(P.analyze_diff(mkdiff("src/a.py", added=["x=1"] * 30)))
check("small logic change is T1", r["tier"] == "T1" and not r["auto_blockers"], r)
r = P.risk(P.analyze_diff(mkdiff("src/a.py", added=["x=1"] * 300)))
check("300 LOC is T2 and over the size cap", r["tier"] == "T2" and any("size cap" in x for x in r["auto_blockers"]), r)
r = P.risk(P.analyze_diff(mkdiff("src/a.py", added=["x=1"]), deps_added=["left-pad (dependencies)"]))
check("new dependency is an auto-blocker and raises risk", any("new dependency" in x for x in r["auto_blockers"]) and r["score"] >= 20, r)
r2 = P.risk(P.analyze_diff(mkdiff("src/a.py", added=["x=1"])), concerns=True)
check("a CONCERNS verdict raises risk", r2["score"] > P.risk(P.analyze_diff(mkdiff("src/a.py", added=["x=1"])))["score"])
check("revert command: single commit", P.revert_command("/r", "main", ["abc"]).startswith("git -C /r revert --no-edit abc && git -C /r push origin main:main"))
check("revert command: range", "abc^..def" in P.revert_command("/r", "main", ["abc", "def"]))

# ---------- real repo: build_packet
def git(repo, *a): return subprocess.run(["git", "-C", repo, *a], capture_output=True, text=True, check=True).stdout.strip()
with tempfile.TemporaryDirectory() as td:
    repo = str(Path(td) / "repo"); os.makedirs(repo)
    git(repo, "init", "-q", "-b", "main"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    Path(repo, "app.py").write_text("def f():\n    return 1\n"); Path(repo, "test_app.py").write_text("import app\nassert app.f() == 1\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base")
    git(repo, "checkout", "-q", "-b", "integrate/x")
    Path(repo, "app.py").write_text("def f():\n    return 2\n"); Path(repo, "test_app.py").write_text("import app\nassert app.f() == 2\n")
    git(repo, "commit", "-qam", "integrate: app.py"); git(repo, "checkout", "-q", "main")
    ran = []
    def runner(cmd, cwd=None, timeout=0): ran.append((cmd, cwd)); return 0, "ok"
    cand = {"chain": "c1", "kind": "integration", "repo": repo, "branch": "integrate/x", "declared_scope": ["app.py", "test_app.py"],
            "gate": {"verdict": "pass", "job_id": "j1", "verify_exit_reported": 0, "verify_failed_at_baseline": True,
                     "verify_relevance": {"verdict": "relevant", "score": 0.9, "threshold": 0.8, "killed": 9, "evidence_mutants": 10, "survivors": [],
                                          "necessity": {"checked": 2, "necessary": 2, "unnecessary": [], "untested": []},
                                          "wrong_bank": {"variants": [1], "survivors": []}},
                     "issues": [{"severity": "high", "file": "app.py", "line": 2, "what": "`return 9` is wrong", "source": "review"},
                                {"severity": "low", "file": "app.py", "line": 2, "what": "`return 2` hard-coded", "source": "review"}]}}
    pk = P.build_packet(cand, {}, runner=runner)
    check("packet is awaiting_final_review with no blockers", pk["state"] == "awaiting_final_review" and not pk["blockers"], pk["blockers"])
    check("ungrounded 'return 9' finding dropped, real one kept", pk["findings_stats"]["dropped"] == 1 and pk["findings_stats"]["kept"] == 1, pk["findings_stats"])
    check("repo checks used the repo's own test command (pytest)", pk["checks"]["status"] == "pass" and any(("pytest" in c or "test_*.py" in c) for c, _ in ran), ran)
    check("checks ran in a throw-away worktree, not the repo", all(cwd and cwd != repo for _, cwd in ran), ran)
    check("throw-away worktree removed", "land-wt-" not in git(repo, "worktree", "list"))
    check("revert command names the staged commit", pk["revert"]["command"].count(pk["commits"][0]) == 1 and "push origin main:main" in pk["revert"]["command"])
    check("relevance + necessity + bank numbers carried", pk["relevance"]["score"] == 0.9 and pk["relevance"]["necessity"]["necessary"] == 2 and pk["relevance"]["wrong_bank"]["survivors"] == 0, pk["relevance"])
    check("main untouched by building the packet", git(repo, "rev-parse", "main") == pk["base_tip"])
    md = P.render_md(pk)
    check("markdown names approve/reject/revert", "ollama-land approve c1" in md and "ollama-land reject c1" in md and "revert --no-edit" in md)
    # failing change, passing base -> blocker
    def red(cmd, cwd=None, timeout=0): return (1, "boom") if "return 2" in Path(cwd, "app.py").read_text() else (0, "ok")
    pk = P.build_packet(cand, {}, runner=red)
    check("red on the branch + green on base == FAIL blocker", pk["checks"]["status"] == "fail" and pk["state"] == "needs_rework", pk["checks"])
    def red_both(cmd, cwd=None, timeout=0): return 1, "boom"
    pk = P.build_packet(cand, {}, runner=red_both)
    check("red on base too == preexisting, not blamed on the change", pk["checks"]["status"] == "preexisting" and pk["state"] == "awaiting_final_review", pk["checks"])
    n = [0]
    def flaky(cmd, cwd=None, timeout=0): n[0] += 1; return (1, "x") if n[0] == 1 else (0, "ok")
    pk = P.build_packet(cand, {}, runner=flaky)
    check("one red then green is a flake, not a failure", pk["checks"]["status"] == "pass", pk["checks"])
    def tmo(cmd, cwd=None, timeout=0): return 124, "timeout"
    pk = P.build_packet(cand, {}, runner=tmo)
    check("timeout is 'unverified', raises risk, does not read as green", pk["checks"]["status"] == "unverified" and pk["risk"]["auto_blockers"], pk["checks"])
    # gate verdicts
    c2 = json.loads(json.dumps(cand)); c2["gate"]["verdict"] = "concerns"
    pk = P.build_packet(c2, {}, runner=runner)
    check("CONCERNS without adjudication is a blocker", any("CONCERNS" in b for b in pk["blockers"]), pk["blockers"])
    c2["adjudication"] = {"decision": "accept", "reasons": ["fp"]}
    pk = P.build_packet(c2, {}, runner=runner)
    check("CONCERNS + accepting adjudication is not a blocker", pk["state"] == "awaiting_final_review", pk["blockers"])
    c3 = json.loads(json.dumps(cand)); c3["gate"]["verdict"] = "fail"
    check("gate FAIL is a blocker", P.build_packet(c3, {}, runner=runner)["state"] == "needs_rework")
    c4 = json.loads(json.dumps(cand)); c4["gate"]["verify_relevance"]["necessity"]["unnecessary"] = [{"file": "app.py"}]
    check("necessity gate survivor blocks", P.build_packet(c4, {}, runner=runner)["state"] == "needs_rework")
    c5 = json.loads(json.dumps(cand)); c5["gate"]["verify_relevance"]["wrong_bank"]["survivors"] = ["wrong passes"]
    check("wrong-bank survivor blocks", P.build_packet(c5, {}, runner=runner)["state"] == "needs_rework")
    # stale: main moves
    git(repo, "checkout", "-q", "main"); Path(repo, "other.txt").write_text("x"); git(repo, "add", "-A"); git(repo, "commit", "-qm", "main moves")
    pk = P.build_packet(cand, {}, runner=runner)
    check("main moved on => stale blocker", any("moved on" in b for b in pk["blockers"]) and pk["state"] == "needs_rework", pk["blockers"])
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
