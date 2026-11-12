#!/usr/bin/env python3
"""End-to-end behaviour tests for ollama-land: modes, packets, approve/reject/revert, post-land
revert, breaker, AUTO gating, kill switch, shadow period, summary. Real git repos + a bare origin;
no network, no queue, no model (claude/runner/spawner are injected)."""
import importlib.machinery, importlib.util, json, os, subprocess, sys, tempfile, time
from pathlib import Path
BIN = Path(os.environ.get("LAND_BIN_DIR") or Path(__file__).resolve().parent)
sys.path.insert(0, str(BIN))

FAILS = []
def check(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ((" -- " + str(extra)[:500]) if (extra and not cond) else ""))
    if not cond:
        FAILS.append(name)

def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True, check=True).stdout.strip()

TD = tempfile.mkdtemp(prefix="land-e2e-")
os.environ["LAND_HOME_DIR"] = TD
os.environ["LAND_ROOT"] = f"{TD}/.ollama-dispatch"
os.environ["LAND_QLOGS"] = f"{TD}/qlogs"
os.environ["LAND_BIN_DIR"] = str(BIN)
loader = importlib.machinery.SourceFileLoader("ollama_land", str(BIN / "ollama-land"))
spec = importlib.util.spec_from_loader("ollama_land", loader)
L = importlib.util.module_from_spec(spec); loader.exec_module(L)
ROOT = Path(os.environ["LAND_ROOT"]); (ROOT / "escalations").mkdir(parents=True); (ROOT / "slice-runs").mkdir(); Path(TD, "qlogs").mkdir()

def new_repo(name):
    origin = Path(TD) / f"{name}.git"; repo = Path(TD) / name
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    repo.mkdir(); git(repo, "init", "-q", "-b", "main"); git(repo, "config", "user.email", "t@t"); git(repo, "config", "user.name", "t")
    (repo / "app.py").write_text("def f():\n    return 1\n"); (repo / "test_app.py").write_text("import app\nassert app.f() == 1\n")
    git(repo, "add", "-A"); git(repo, "commit", "-qm", "base"); git(repo, "remote", "add", "origin", str(origin)); git(repo, "push", "-q", "origin", "main")
    return repo, origin

def stage(repo, branch, files, msg="integrate: x"):
    git(repo, "checkout", "-q", "-b", branch)
    for p, t in files.items():
        (repo / p).write_text(t)
    git(repo, "add", "-A"); git(repo, "commit", "-qm", msg); git(repo, "checkout", "-q", "main")

def register(label, repo, branch, verdict="pass", extra_gate=None, scope=None):
    g = {"verdict": verdict, "job_id": "j_" + label, "verify_exit_reported": 0, "verify_failed_at_baseline": True, "issues": [],
         "verify_relevance": {"verdict": "relevant", "score": 0.95, "threshold": 0.8, "killed": 9, "evidence_mutants": 10, "survivors": [],
                              "necessity": {"checked": 1, "necessary": 1, "unnecessary": [], "untested": []}, "wrong_bank": {"variants": [], "survivors": []}}}
    g.update(extra_gate or {})
    (Path(TD) / "qlogs" / f"j_{label}.gate.json").write_text(json.dumps(g))
    (ROOT / "slice-runs" / f"{label}.json").write_text(json.dumps({
        "label": label, "repo": str(repo), "plan_path": None,
        "slices": {"s1": {"status": "done", "job_id": "j_" + label}},
        "integration": {"status": "staged", "integrate_branch": branch, "onto": "main", "scope": scope or ["app.py", "test_app.py"]}}))

def ready_row(label, who="HARNESS GO -- relevance review"):
    with (ROOT / "escalations" / "READY-TO-LAND.md").open("a") as fh:
        fh.write(f"- [ ] `{label}` **{who}** R - 2026-10-09T00:00:00Z - something - `{TD}/wt-{label}`\n")

def runner_ok(cmd, cwd=None, timeout=0): return 0, "ok"

# ============ mode ============
check("no mode file == REVIEW", L.get_mode() == "REVIEW")
(ROOT / "land-mode").write_text("banana\n"); check("garbage mode file == REVIEW (fail safe)", L.get_mode() == "REVIEW")
(ROOT / "land-mode").write_text("REVIEW\n"); check("REVIEW file == REVIEW", L.get_mode() == "REVIEW")
(ROOT / "land-mode").write_text("AUTO\n"); check("AUTO file == AUTO", L.get_mode() == "AUTO")
(ROOT / "land-mode").unlink()

# ============ REVIEW: scan never moves a product repo ============
repo, origin = new_repo("prod")
stage(repo, "integrate/c1", {"app.py": "def f():\n    return 2\n", "test_app.py": "import app\nassert app.f() == 2\n"})
register("c1", repo, "integrate/c1"); ready_row("c1", "CHAIN STAGED -- ready to land")
main0, origin0 = git(repo, "rev-parse", "main"), git(origin, "rev-parse", "main")
res = L.scan(runner=runner_ok)
check("scan builds the packet and parks it awaiting_final_review", ("c1", "awaiting_final_review") in res["packets"], res)
check("REVIEW: local main untouched by scan", git(repo, "rev-parse", "main") == main0)
check("REVIEW: origin main untouched by scan", git(origin, "rev-parse", "main") == origin0)
check("REVIEW: scan landed nothing", res["auto_landed"] == [])
P = L.load_packet("c1")
check("packet.md and packet.json written under landing/<chain>/", (ROOT / "landing/c1/packet.md").is_file() and (ROOT / "landing/c1/packet.json").is_file())
check("packet records the shadow decision (AUTO would not: shadow period)", P["auto_would"]["eligible"] is False and any("shadow" in r for r in P["auto_would"]["reasons"]), P.get("auto_would"))
sm = L.summary()
check("summary names the awaiting chain and the commands", "1 chain(s) awaiting FINAL REVIEW" in sm and "ollama-land show c1" in sm, sm)
os.environ["LAND_ROOT"]  # noqa
res = L.scan(runner=runner_ok)
check("second scan is idempotent (no rebuild of an unchanged packet)", L.load_packet("c1")["built_epoch"] == P["built_epoch"])

# approve a harness-go chain is refused
ready_row("gochain"); (Path(TD) / "wt-gochain").mkdir(); (Path(TD) / "wt-gochain/.preflight-state.json").write_text("{}")
Pg = L.go_packet("gochain", str(Path(TD) / "wt-gochain"), L.load_cfg(), execute=False); L.save_packet(Pg)
try:
    L.approve("gochain"); ok = False
except L.LandError as e:
    ok = "not landed" in str(e)
check("approve refuses a harness-go chain", ok)

# ============ approve (the ONE merge) ============
r = L.approve("c1", by="main", runner=runner_ok, handoff=lambda c, **k: (0, "ok"))
check("approve lands: origin main == staged tip", git(origin, "rev-parse", "main") == P["branch_tip"] and r["state"] == "landed", r["state"])
check("post-land verification ran and passed", r["post_land"]["status"] == "pass")
check("approve closed the READY-TO-LAND row with the action recorded",
      "- [x] `c1`" in (ROOT / "escalations/READY-TO-LAND.md").read_text() and "ollama-land landed" in (ROOT / "escalations/READY-TO-LAND.md").read_text())
check("no AI attribution in the landed commit message", "Co-Authored" not in git(repo, "log", "-1", "--format=%B", "main"))
check("main decision recorded in the ledger for the shadow comparison", any(x.get("event") == "main-decision" and x.get("decision") == "approve" for x in L.read_ledger()))
try:
    L.approve("c1"); ok = False
except L.LandError as e:
    ok = "landed" in str(e)
check("a landed chain cannot be approved twice", ok)
r = L.revert_chain("c1")
check("revert undoes the landing on origin (content restored)", r["state"] == "reverted" and "return 1" in git(origin, "show", "main:app.py"), r["state"])
check("summary silent when nothing awaits", L.summary() == "" or "awaiting" not in L.summary())

# ============ post-land regression -> automatic revert + breaker ============
repo2, origin2 = new_repo("prod2")
for n in (1, 2):
    stage(repo2, f"integrate/r{n}", {f"mod{n}.py": f"X = {n}\n"})
    register(f"r{n}", repo2, f"integrate/r{n}")
    L.scan(runner=runner_ok)
def runner_red_after_land(cmd, cwd=None, timeout=0):
    # green on the base, red on a tree that contains the landed module
    return (1, "regression") if cwd and any(Path(cwd).glob("mod*.py")) else (0, "ok")
r = L.approve("r1", runner=runner_red_after_land, handoff=lambda c, **k: (0, "ok"))
check("post-land RED (green on base) -> automatically reverted and marked rejected", r["state"] == "rejected" and r["post_land"]["auto_revert"]["ok"], r["post_land"])
check("the revert is pushed: origin has no mod1.py", subprocess.run(["git", "-C", str(origin2), "cat-file", "-e", "main:mod1.py"], capture_output=True).returncode != 0)
check("one revert does not open the breaker", not L.breaker_state(str(repo2)).get("open"))
# r2 was built on the old main -> stale after r1's land+revert moved main
L.scan(runner=runner_ok)
stage(repo2, "integrate/r3", {"mod3.py": "X = 3\n"}); register("r3", repo2, "integrate/r3")
L.scan(runner=runner_ok)
try:
    r = L.approve("r3", runner=runner_red_after_land, handoff=lambda c, **k: (0, "ok"))
except L.LandError as e:
    r = None
check("second consecutive revert opens the per-repo breaker", L.breaker_state(str(repo2)).get("open") is True, L.breaker_state(str(repo2)))
stage(repo2, "integrate/r4", {"mod4.py": "X = 4\n"}); register("r4", repo2, "integrate/r4"); L.scan(runner=runner_ok)
try:
    L.approve("r4", runner=runner_ok); ok = False
except L.LandError as e:
    ok = "breaker" in str(e)
check("breaker open -> approve refused", ok)
check("summary reports the open breaker", "BREAKER OPEN" in L.summary(), L.summary())
L.main(["breaker", "clear", str(repo2)]) if False else L.jsave(L.breaker_path(str(repo2)), {"consecutive_reverts": 0, "open": False})

# pre-existing red is NOT reverted
repo3, origin3 = new_repo("prod3")
stage(repo3, "integrate/p1", {"m.py": "X=1\n"}); register("p1", repo3, "integrate/p1"); L.scan(runner=runner_ok)
r = L.approve("p1", runner=lambda c, cwd=None, timeout=0: (1, "red everywhere"), handoff=lambda c, **k: (0, "ok"))
check("red on the base too -> landed, NOT reverted (pre-existing)", r["state"] == "landed" and r["post_land"]["status"] == "preexisting", r["post_land"])

# ============ stale ============
repo4, origin4 = new_repo("prod4")
stage(repo4, "integrate/s1", {"m.py": "X=1\n"}); register("s1", repo4, "integrate/s1"); L.scan(runner=runner_ok)
(repo4 / "other.txt").write_text("x"); git(repo4, "add", "-A"); git(repo4, "commit", "-qm", "main moves"); head = git(repo4, "rev-parse", "main")
try:
    L.approve("s1", runner=runner_ok); ok = False
except L.LandError as e:
    ok = "STALE" in str(e)
check("main moved after the packet was built -> approve refuses (STALE) and lands nothing", ok and git(repo4, "rev-parse", "main") == head and L.load_packet("s1")["state"] == "stale")

# ============ reject ============
repo5, origin5 = new_repo("prod5")
stage(repo5, "integrate/j1", {"m.py": "X=1\n"}); register("j1", repo5, "integrate/j1"); ready_row("j1", "CHAIN STAGED"); L.scan(runner=runner_ok)
m5 = git(origin5, "rev-parse", "main")
r = L.reject("j1", "wrong approach")
check("reject records the reason, closes the row, merges nothing",
      r["state"] == "rejected" and r["rejection"]["reason"] == "wrong approach" and git(origin5, "rev-parse", "main") == m5
      and "- [x] `j1`" in (ROOT / "escalations/READY-TO-LAND.md").read_text())

# ============ AUTO gating ============
def docs_chain(name, label, verdict="pass", extra_gate=None):
    r_, o_ = new_repo(name)
    stage(r_, f"integrate/{label}", {"README.md": "# doc\nmore\n"}, msg="integrate: README.md")
    register(label, r_, f"integrate/{label}", verdict=verdict, extra_gate=extra_gate, scope=["README.md"])
    return r_, o_
L.jsave(ROOT / "landing/config.json", {"min_shadow_agreements": 0, "auto_risk_threshold": 50, "auto_max_tier": "T0", "hold_labels": []})
ra, oa = docs_chain("auto1", "a1")
res = L.scan(runner=runner_ok)   # still REVIEW
check("REVIEW mode never auto-lands even a perfectly eligible T0 chain", res["auto_landed"] == [] and git(oa, "rev-parse", "main") == git(ra, "rev-parse", "main") and L.load_packet("a1")["state"] == "awaiting_final_review")
(ROOT / "land-mode").write_text("AUTO\n")
(ROOT / "landing").mkdir(exist_ok=True); (ROOT / "landing/KILL").write_text("stop")
res = L.scan(runner=runner_ok)
check("AUTO + kill-switch file present -> nothing lands", res["auto_landed"] == [] and L.load_packet("a1")["state"] == "awaiting_final_review")
(ROOT / "landing/KILL").unlink()
L.jsave(ROOT / "landing/config.json", {"min_shadow_agreements": 5, "auto_risk_threshold": 50, "auto_max_tier": "T0", "hold_labels": []})
check("AUTO is held by the shadow period until enough agreements", L.scan(runner=runner_ok)["auto_landed"] == [])
L.jsave(ROOT / "landing/config.json", {"min_shadow_agreements": 0, "auto_risk_threshold": 3, "auto_max_tier": "T0", "hold_labels": []})
P = L.load_packet("a1"); P["rebuild"] = True; L.save_packet(P)
check("AUTO: risk above the threshold -> not eligible", L.auto_eligibility(L.load_packet("a1"), L.load_cfg(), "AUTO", kill=False, breaker_open=False, shadow=(9, 0))[0] is False or L.load_packet("a1")["risk"]["score"] < 3)
L.jsave(ROOT / "landing/config.json", {"min_shadow_agreements": 0, "auto_risk_threshold": 50, "auto_max_tier": "T0", "hold_labels": []})
ok_, why = L.auto_eligibility(L.load_packet("a1"), L.load_cfg(), "AUTO", kill=False, breaker_open=True, shadow=(9, 0))
check("AUTO: open breaker -> not eligible", not ok_ and any("breaker" in w for w in why), why)
ok_, why = L.auto_eligibility(L.load_packet("a1"), L.load_cfg(), "AUTO", kill=False, breaker_open=False, shadow=(9, 1))
check("AUTO: a shadow false-eligible (AUTO would, main rejected) blocks", not ok_ and any("REJECTED" in w for w in why), why)
res = L.scan(runner=runner_ok)
check("AUTO + all gates open -> the same approve step lands it by itself", res["auto_landed"] == ["a1"] and git(oa, "rev-parse", "main") == L.load_packet("a1")["branch_tip"], res)
check("AUTO landing is recorded as by=auto with post-land check", L.load_packet("a1")["landed"]["by"] == "auto" and L.load_packet("a1")["post_land"]["status"] == "pass", L.load_packet("a1")["post_land"])
# a non-docs T1 chain is above auto_max_tier T0
rb, ob = new_repo("auto2"); stage(rb, "integrate/a2", {"m.py": "X=1\n"}); register("a2", rb, "integrate/a2")
res = L.scan(runner=runner_ok)
check("AUTO: T1 chain above auto_max_tier=T0 is not landed", "a2" not in res["auto_landed"] and L.load_packet("a2")["state"] == "awaiting_final_review")
# CONCERNS accepted only by an LLM can never auto-land
rc_, oc_ = docs_chain("auto3", "a3", verdict="concerns")
cl = lambda prompt, **k: (0, 'VERDICT: {"decision":"accept","reasons":["fp"]}', "")
res = L.scan(runner=runner_ok, claude=cl)
P3 = L.load_packet("a3")
check("CONCERNS + accept -> land-ready with the adjudication attached", P3["state"] == "awaiting_final_review" and P3["adjudication"]["decision"] == "accept", P3.get("blockers"))
check("...but a CONCERNS chain is never AUTO-eligible (LLM may flag, never approve)", "a3" not in res["auto_landed"] and not L.auto_eligibility(P3, L.load_cfg(), "AUTO", kill=False, breaker_open=False, shadow=(99, 0))[0])
(ROOT / "land-mode").unlink()

# ============ adjudication: re-run / re-spec executed automatically ============
rd, od = docs_chain("adj", "a4", verdict="concerns")
(ROOT / "auto-runs/argv").mkdir(parents=True, exist_ok=True); (ROOT / "auto-runs/argv/a4.json").write_text(json.dumps({"label": "a4", "argv": ["--intent", "x", "--label", "a4"]}))
sp = []
cl2 = lambda prompt, **k: (0, 'VERDICT: {"decision":"re-run","reasons":["bad attempt"]}', "")
L.scan(runner=runner_ok, claude=cl2, spawner=lambda c, lp: sp.append(c))
P4 = L.load_packet("a4")
check("adjudicated re-run: driver relaunched automatically and chain parked needs_rework", P4["state"] == "needs_rework" and sp and sp[0][-1] == "--resume-harness", (P4["state"], sp))
check("no human step: nothing was merged", git(od, "rev-parse", "main") == git(rd, "rev-parse", "main"))

# ============ ingest rules ============
def gate_file(job, **kw):
    g = {"verdict": "pass", "job_label": "feat-x", "job_cwd": str(Path(TD)), "task_intent": "implement x"}; g.update(kw)
    (Path(TD) / "qlogs" / f"{job}.gate.json").write_text(json.dumps(g))
gate_file("ig1"); check("ingest accepts a standalone passing dispatch", L.ingest("ig1") and L.ingest("ig1")["chain"] == "feat-x")
gate_file("ig2", verdict="fail"); check("ingest ignores a failed gate", L.ingest("ig2") is None)
gate_file("ig3", job_label="auto-author-foo-c1"); check("ingest ignores harness authoring jobs", L.ingest("ig3") is None)
gate_file("ig4", job_label="auto-refine-foo-r1"); check("ingest ignores refine jobs", L.ingest("ig4") is None)
gate_file("ig5", task_intent="REFINE TASK -- close the gaps"); check("ingest ignores refine-intent jobs", L.ingest("ig5") is None)
gate_file("j_c1"); check("ingest ignores a slice sub-job (it lands via its chain)", L.ingest("j_c1") is None)

# ============ harness-go through scan: hold + dry ============
L.jsave(ROOT / "landing/config.json", {"hold_labels": ["held1"]})
for lab in ("held1",):
    w = Path(TD) / f"wt-{lab}"; w.mkdir(exist_ok=True); (w / ".preflight-state.json").write_text("{}")
    ready_row(lab)
P = L.go_packet("held1", str(Path(TD) / "wt-held1"), L.load_cfg(), execute=True)
check("hold_labels: packet only, nothing executed", P["state"] == "go_held" and "executed" not in P)
# ---- isolated eligibility reasons: every refusal tested with ONE thing wrong, all else open
BASEP = {"kind": "integration", "state": "awaiting_final_review", "blockers": [], "repo": str(Path(TD) / "norepo"),
         "risk": {"score": 0, "tier": "T0", "auto_blockers": []}, "checks": {"status": "pass"}, "gate": {"verdict": "pass"}}
def elig(P, **kw):
    a = dict(kill=False, breaker_open=False, shadow=(99, 0)); a.update(kw)
    return L.auto_eligibility(P, L.load_cfg(), "AUTO", **a)
ok_, why = elig(BASEP); check("control: fully open synthetic T0/pass packet IS eligible", ok_, why)
import copy
P_ = copy.deepcopy(BASEP); P_["risk"]["tier"] = "T1"; ok_, why = elig(P_)
check("only the tier is wrong (T1 > auto_max_tier T0) -> refused for the tier", not ok_ and any("tier" in w for w in why), why)
P_ = copy.deepcopy(BASEP); P_["gate"]["verdict"] = "concerns"; ok_, why = elig(P_)
check("only the verdict is wrong (CONCERNS) -> refused for the verdict", not ok_ and any("gate verdict" in w for w in why), why)
P_ = copy.deepcopy(BASEP); P_["checks"]["status"] = "fail"; ok_, why = elig(P_)
check("only the repo checks are wrong -> refused for the checks", not ok_ and any("checks" in w for w in why), why)
P_ = copy.deepcopy(BASEP); P_["risk"]["score"] = 99; ok_, why = elig(P_)
check("only the risk is wrong -> refused for the risk", not ok_ and any("risk" in w for w in why), why)
ok_, why = elig(BASEP, kill=True); check("only the kill switch -> refused for it", not ok_ and any("kill" in w for w in why), why)
ok_, why = elig(BASEP, shadow=(0, 0)); check("only the shadow count -> refused for it", not ok_ and any("shadow" in w for w in why), why)
print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
