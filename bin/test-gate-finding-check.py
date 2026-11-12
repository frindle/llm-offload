#!/usr/bin/env python3
"""Regression test: GATE FINDING CHECK (unattended-readiness item 4, 2026-10-04).

The authoritative re-gate reviewer flagged correct code and, on a GREEN verify,
parked slices as needs_opus. gate-on-complete now turns each reviewer finding into
an executed reproducer (gate_finding_check.py) before it may park.

FIXTURES ARE THE REAL CASES (test-fixtures-gate-finding-check/cases/<job>/): each
holds the job's real <id>.gate.json, <id>.diff and regate report.md, plus base/ (git
archive of its launch baseline) and head/ (baseline + the gated diff) -- exactly the
code the reviewer saw. repros/ holds HAND-WRITTEN reproducers (not produced by the
reviewing model: the verifier's executor is validated against ground truth, not
against the model family that made the claims):
  false positives (Main overrode by hand):
    a330fff2d326  s4b  "build_request mutates the input messages list"     -> lands
    a0be91760fb5  s4b  same slice, earlier run                             -> STILL PARKS
                       (a surviving relevance mutant 3 lines from its second
                        finding: the fixture does not pin that code; fail-closed)
    ef2868d21945  s6c  "'queued' silently dropped" + uncertain enqueue row -> lands
    9414e0cf848e  aw-sched-runner-s11 "SeatsAeroClient NameError"          -> lands
  true positives (a later commit fixed exactly what the reviewer said):
    813d71882974  bg-brokers-s1  non-str id -> AttributeError, contract says ValueError
                  (fixed by broker-guard c94ba6d)                          -> PARKS, confirmed
    2f87ad7a79c2  chat-frontend s6  enqueue ignores session_id/message_index
                  (fixed by dashboard-chat 7181305); relevance UNPROVEN    -> PARKS (plan none);
                  executor confirms it under a counterfactual 'relevant'
  "concerns, findings: none listed":
    c63772e0b710  uncertain medium "crashes if the session file is missing" -- literally
                  true -> CONFIRMED -> still parks, now WITH a reproducer
    b1b085738769  relevance LOW (the spec-defect case)                      -> PARKS (plan none)
  pre-existing: ee33616e78f9's real stores_image defect, run on a330's trees -> advisory

Usage: GATE=<gate-on-complete.py> GFC=<gate_finding_check.py> CRA=<code-review-agent.py>
       SLICE=<ollama-dispatch-slice> python3 this.py
Revert check: point them at the .bak files (GFC at a missing path) -> every test FAILs.
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import traceback
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
FX = Path(os.environ.get("FX") or (HERE / "test-fixtures-gate-finding-check"))
BIN = Path.home() / "bin"
GATE = Path(os.environ.get("GATE", BIN / "gate-on-complete.py"))
GFC = Path(os.environ.get("GFC", BIN / "gate_finding_check.py"))
CRA = Path(os.environ.get("CRA", BIN / "code-review-agent.py"))
SLICE = Path(os.environ.get("SLICE", BIN / "ollama-dispatch-slice"))
sys.path.insert(0, str(BIN))   # sibling modules the loaded scripts import

TMP = Path(tempfile.mkdtemp(prefix="test-gfc-"))
os.environ["GATE_TEST_MODE"] = "1"                      # no real enqueue/notify
os.environ["GATE_ADVISORY_LOG"] = str(TMP / "ADVISORY-CAVEATS.md")
os.environ["OLLAMA_DISPATCH_HOME"] = str(TMP / "dispatch-home")
os.environ.pop("GATE_FINDING_CHECK", None)

results = []          # (test name, ok)
_cur = {"fails": 0}


def chk(name, actual, expected):
    ok = actual == expected
    print(("    ok   - " if ok else "    FAIL - ") + name
          + ("" if ok else f": expected {expected!r} got {actual!r}"))
    _cur["fails"] += 0 if ok else 1


def test(fn):
    _cur["fails"] = 0
    print(f"[{fn.__name__}]")
    try:
        fn()
        ok = _cur["fails"] == 0
    except Exception as e:
        print("    FAIL - raised " + "".join(traceback.format_exception_only(type(e), e)).strip())
        ok = False
    results.append((fn.__name__, ok))
    print(("  PASS " if ok else "  FAIL ") + fn.__name__)
    return fn


def load(path, name):
    loader = importlib.machinery.SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


_mods = {}


def mod(key):
    if key not in _mods:
        path, name = {"gfc": (GFC, "gate_finding_check"), "goc": (GATE, "goc_under_test"),
                      "cra": (CRA, "cra_under_test"), "slice": (SLICE, "slice_under_test")}[key]
        m = load(path, name)
        if key == "goc":
            m._GFC_MOD = mod("gfc")   # one module object (raises on the .bak: no _GFC_MOD)
            if not hasattr(m, "merge_finding_check"):
                raise AttributeError("gate-on-complete has no finding check")
        _mods[key] = m
    return _mods[key]


def case(job):
    return FX / "cases" / job


def payload(job):
    return json.loads((case(job) / f"{job}.gate.json").read_text())


def diff_files(job):
    return [ln[6:].strip() for ln in (case(job) / f"{job}.diff").read_text().splitlines()
            if ln.startswith("+++ b/")]


def repro(name):
    return (FX / "repros" / f"{name}.py").read_text()


def run_case(job, src, cited, timeout=None, tree=None):
    g = mod("gfc")
    h = g.run_reproducer(tree or case(job) / "head", src, cited, timeout=timeout)
    b = (g.run_reproducer(case(job) / "base", src, cited, timeout=timeout)
         if h.get("kind") == "fail" else None)
    return g.classify_row(h, b), h, b


# ------------------------------------------------------------------ plan()

@test
def test_plan_real_cases():
    g = mod("gfc")
    want = {"a330fff2d326": ("check", [0]), "ef2868d21945": ("check", [0, 1]),
            "9414e0cf848e": ("check", [0]), "813d71882974": ("check", [0, 1]),
            "c63772e0b710": ("check", [0]),
            "a0be91760fb5": ("none", []),     # survivor at :292, finding at :295
            "2f87ad7a79c2": ("none", []),     # relevance unproven (the TP keeps parking)
            "b1b085738769": ("none", [])}     # relevance low (the spec-defect case)
    for job, (act, rows) in want.items():
        p = g.plan(payload(job), diff_files(job))
        chk(f"{job}: plan {act} {rows}", (p["action"], p["rows"]), (act, rows))


@test
def test_plan_guards_fail_closed():
    g = mod("gfc")
    base = payload("a330fff2d326")
    df = diff_files("a330fff2d326")
    chk("baseline: a330 is checked", g.plan(base, df)["action"], "check")

    def mut(**kw):
        p = json.loads(json.dumps(base))
        p.update(kw)
        return g.plan(p, df)["action"]
    chk("verify red -> none", mut(verify_exit_reported=1), "none")
    chk("verify exit unknown -> none", mut(verify_exit_reported=None), "none")
    chk("job exit nonzero -> none", mut(job_exit_code=1), "none")
    chk("verify green AT BASELINE -> none", mut(verify_failed_at_baseline=False), "none")
    chk("pre-gate authority -> none", mut(gate_authority="pregate-terminal-lowscope"), "none")
    chk("untrusted baseline -> none", mut(untrusted=["dirty"]), "none")
    chk("UNPROVEN review -> none", mut(review_verdict="UNPROVEN (token cap)"), "none")
    chk("relevance unproven -> none", mut(verify_relevance={"verdict": "unproven"}), "none")
    chk("relevance missing -> none", mut(verify_relevance=None), "none")
    chk("survivor within 5 lines of the finding -> none",
        mut(verify_relevance={"verdict": "relevant",
                              "survivors": [{"file": "dashboard_chat.py", "line": 288}]}), "none")
    chk("survivor 6+ lines away -> still checked",
        mut(verify_relevance={"verdict": "relevant",
                              "survivors": [{"file": "dashboard_chat.py", "line": 291}]}), "check")
    chk("verdict pass -> none", mut(verdict="pass"), "none")
    extra = base["issues"] + [{"severity": "high", "category": "code", "source": "verify-exit",
                               "file": "x.py", "line": 1, "what": "verify failed"}]
    chk("a non-reviewer finding present -> none", mut(issues=extra), "none")
    many = [dict(base["issues"][0], line=10 * k) for k in range(1, 6)]
    chk("more than GATE_FC_MAX_ROWS findings -> none", mut(issues=many), "none")
    chk("finding on a file outside the diff (fail) -> none",
        mut(issues=[dict(base["issues"][0], file="other.py")]), "none")
    chk("finding on a non-checkable (.tsx) file (fail) -> none",
        mut(issues=[dict(base["issues"][0], file="static/app.tsx")]), "none")


@test
def test_plan_concerns_advisory():
    g = mod("gfc")
    p = payload("c63772e0b710")
    chk("c637 concerns, uncertain row, .py in diff -> check (not waved through)",
        g.plan(p, diff_files("c63772e0b710"))["action"], "check")
    q = json.loads(json.dumps(p))
    # .tsx stays "not mechanically checkable" (needs a JSX transform); plain .js/.ts ARE checkable since 2026-10-14
    q["issues"][0]["file"] = "static/chat.tsx"
    chk("concerns made only of uncertain non-high caveats, not checkable -> advisory",
        g.plan(q, ["static/chat.tsx"])["action"], "advisory")
    q["issues"][0]["uncertain"] = False
    chk("...but a CERTAIN concern is not advisory -> none", g.plan(q, ["static/chat.tsx"])["action"], "none")


# --------------------------------------------------------------- executor

@test
def test_executor_false_positives_unconfirmed():
    for name, job, cited in [("a330fff2d326-0", "a330fff2d326", "dashboard_chat.py"),
                             ("ef2868d21945-0", "ef2868d21945", "dashboard_chat.py"),
                             ("ef2868d21945-1", "ef2868d21945", "dashboard_chat.py"),
                             ("9414e0cf848e-0", "9414e0cf848e", "src/scheduled_runner.py"),
                             ("813d71882974-1", "813d71882974", "broker_guard/brokers.py"),
                             ("a0be91760fb5-0", "a0be91760fb5", "dashboard_chat.py"),
                             ("b1b085738769-0", "b1b085738769", "dashboard_chat.py")]:
        r, h, _ = run_case(job, repro(name), cited)
        chk(f"{name}: FP reproducer passes on the gated tree -> unconfirmed",
            (r["outcome"], h.get("touched_cited")), ("unconfirmed", True))


@test
def test_executor_true_positives_confirmed():
    r, h, b = run_case("813d71882974", repro("813d71882974-0"), "broker_guard/brokers.py")
    chk("813d (TP, fixed by c94ba6d): confirmed", r["outcome"], "confirmed")
    chk("813d: the failure is raised FROM the product code", (h.get("exc"), h.get("where")),
        ("AttributeError", "broker_guard/brokers.py:27"))
    chk("813d: baseline does not fail the same way", (b or {}).get("kind") != "fail"
        or (b.get("exc"), b.get("where")) != (h.get("exc"), h.get("where")), True)
    r, h, b = run_case("2f87ad7a79c2", repro("2f87ad7a79c2-0"), "dashboard_chat.py")
    chk("2f87 (TP, fixed by 7181305): confirmed", r["outcome"], "confirmed")
    r, h, b = run_case("c63772e0b710", repro("c63772e0b710-0"), "dashboard_chat.py")
    chk("c637 uncertain caveat is literally true: confirmed (parks with a reproducer)",
        (r["outcome"], h.get("exc")), ("confirmed", "FileNotFoundError"))


@test
def test_executor_pre_existing():
    r, h, b = run_case("a330fff2d326", repro("ee33616e78f9-preexisting-on-a330"),
                       "dashboard_chat.py")
    chk("ee33's real stores_image defect on a330's trees: fails on head AND base -> pre-existing",
        (h.get("kind"), (b or {}).get("kind"), r["outcome"]), ("fail", "fail", "pre-existing"))


@test
def test_executor_negative_controls():
    j, f = "a330fff2d326", "dashboard_chat.py"
    ctrl = {
        "assert False without touching the cited code": (
            "import dashboard_chat\ndef reproduce():\n    assert False, 'claim'\n", "no-reproducer"),
        "a typo in the reproducer (NameError in its own frame)": (
            "import dashboard_chat\ndef reproduce():\n    dashboard_chat.build_request('p', [], [], [])\n"
            "    asert_x(1)\n", "no-reproducer"),
        "syntax error": ("def reproduce(:\n    pass\n", "no-reproducer"),
        "no reproduce() defined": ("import dashboard_chat\n", "no-reproducer"),
        "sleeps past the budget": (
            "import time, dashboard_chat\ndef reproduce():\n    time.sleep(30)\n", "error"),
        "exits the interpreter": ("import os\ndef reproduce():\n    os._exit(0)\n", "error"),
        "missing third-party dep at import": (
            "import no_such_module_xyz\ndef reproduce():\n    pass\n", "error"),
    }
    for name, (src, want) in ctrl.items():
        r, _, _ = run_case(j, src, f, timeout=5)
        chk(f"control: {name} -> {want}", r["outcome"], want)
    r, h, _ = run_case(j, "import dashboard_chat\ndef reproduce():\n    assert True\n", f, timeout=5)
    chk("control: vacuous pass is unconfirmed AND flagged vacuous",
        (r["outcome"], "vacuous" in r["why"]), ("unconfirmed", True))


@test
def test_executor_isolated_copy():
    g = mod("gfc")
    before = sorted(str(p.relative_to(case("a330fff2d326"))) for p in case("a330fff2d326").rglob("*"))
    src = ("import os, dashboard_chat\nopen('dashboard_chat.py','w').write('x')\n"
           "open(os.path.expanduser('~/gfc-probe'),'w').write('x')\n"
           "def reproduce():\n    dashboard_chat\n")
    g.run_reproducer(case("a330fff2d326") / "head", src, "dashboard_chat.py", timeout=10)
    after = sorted(str(p.relative_to(case("a330fff2d326"))) for p in case("a330fff2d326").rglob("*"))
    chk("the fixture tree is untouched (runs in a throwaway copy)", after, before)
    chk("HOME is a scratch dir (no ~/gfc-probe in the real home)",
        (Path.home() / "gfc-probe").exists(), False)


# --------------------------------------------------------------- end to end

class A:
    def __init__(self, **kw):
        self.__dict__.update(dict(out_dir=None, job_id="t", cwd=".", task_file="", verify=""))
        self.__dict__.update(kw)


def stage(job):
    """A fresh out_dir holding the real gate.json/diff/regate report for `job`."""
    od = TMP / f"od-{job}-{len(list(TMP.iterdir()))}"
    od.mkdir()
    shutil.copy(case(job) / f"{job}.gate.json", od / f"{job}.gate.json")
    shutil.copy(case(job) / f"{job}.diff", od / f"{job}.diff")
    rg = od / f"{job}-regate"
    rg.mkdir()
    shutil.copy(case(job) / "regate-report.md", rg / "report.md")
    return od, rg


def patched_goc(enqueue_ok=True, materialize_ok=True):
    goc, g = mod("goc"), mod("gfc")
    calls = {"enqueue": [], "finalize": []}

    def fake_enqueue(argv):
        calls["enqueue"].append(argv)
        return (True, "queued") if enqueue_ok else (False, "queue refused")

    def fake_materialize(job_cwd, base_commit, diff_path, dest):
        if not materialize_ok:
            return False, "job worktree gone"
        job = Path(diff_path).stem
        for t in ("base", "head"):
            if (dest / t).exists():
                shutil.rmtree(dest / t)
            shutil.copytree(case(job) / t, dest / t)
        return True, "ok"

    def fake_finalize(parent, payload, gate_json, out_dir):
        calls["finalize"].append(json.loads(json.dumps(payload)))
        return 0
    goc._fc_enqueue = fake_enqueue
    g.materialize = fake_materialize
    goc._finalize_review = fake_finalize
    goc._slice_ctx = lambda *a, **k: None
    goc._job_field = lambda jid, key: "chat-fixes" if key == "bundle" else None
    return goc, g, calls


def regate(job, **kw):
    goc, g, calls = patched_goc(**kw)
    od, rg = stage(job)
    rc = goc.merge_review(A(job_label=f"regate-{job}", cwd=str(rg), out_dir=str(od)), od,
                          prefix="regate-", authoritative=True)
    return goc, g, calls, od, json.loads((od / f"{job}.gate.json").read_text()), rc


def verifier_done(goc, od, job, rows, raw=None):
    """Simulate the runner: refute.json from HAND-WRITTEN reproducers (rows: idx->name)."""
    cwd = od / f"{job}-fcheck"
    issues = json.loads((od / f"{job}.gate.json").read_text())["issues"]
    if raw is None:
        raw = {"rows": [{"idx": n, "file": issues[n]["file"], "line": issues[n]["line"],
                         "reproducer": repro(name) if name else None,
                         "problem": None if name else "model declined: not observable"}
                        for n, name in rows.items()], "model": "hand-written"}
    if raw is not False:
        (cwd / "refute.json").write_text(json.dumps(raw))
    goc.merge_finding_check(A(job_label=f"secondop-{job}-fcheck", cwd=str(cwd),
                              out_dir=str(od), job_id="fc1"), od)
    return json.loads((od / f"{job}.gate.json").read_text())


@test
def test_e2e_false_positive_lands():
    job = "a330fff2d326"
    goc, g, calls, od, p, rc = regate(job)
    chk("regate merge: verdict parked as PENDING, not fail",
        p["verdict"], "pending-finding-check")
    chk("regate merge: the terminal block (autofix/park/advance) is DEFERRED",
        calls["finalize"], [])
    argv = calls["enqueue"][0] if calls["enqueue"] else []
    chk("verifier enqueued as secondop-<id>-fcheck (the hook-routed, non-barrier kind)",
        argv[argv.index("--label") + 1] if "--label" in argv else None, f"secondop-{job}-fcheck")
    chk("verifier enqueued through the queue with code-review-agent --runner, in the parent bundle",
        ("--runner" in argv and argv[argv.index("--runner") + 1].endswith("code-review-agent.py"),
         argv[argv.index("--bundle") + 1] if "--bundle" in argv else None), (True, "chat-fixes"))
    task = json.loads((od / f"{job}-fcheck" / "task.json").read_text())
    chk("task carries mode=refute + the reviewer's FULL finding text",
        (task["mode"], "mutates" in task["rows"][0]["detail"]), ("refute", True))
    p = verifier_done(goc, od, job, {0: "a330fff2d326-0"})
    chk("unconfirmed -> verdict pass, finding moved to advisory_caveats",
        (p["verdict"], [a.get("finding_check") for a in p.get("advisory_caveats", [])]),
        ("pass", ["unconfirmed"]))
    chk("terminal block ran exactly once, on the new verdict",
        [f["verdict"] for f in calls["finalize"]], ["pass"])
    chk("autofix no longer parks it", goc.autofix_classify(p)["action"], "none")
    log = Path(os.environ["GATE_ADVISORY_LOG"]).read_text()
    chk("advisory log row written", f"**{job}** unconfirmed" in log, True)
    verifier_done(goc, od, job, {}, raw={"rows": [
        {"idx": 0, "file": "dashboard_chat.py", "line": 284, "reproducer": "def reproduce():\n    assert False\n"}]})
    chk("a re-fired hook is a no-op (idempotent: no second decision)",
        (len(calls["finalize"]), json.loads((od / f"{job}.gate.json").read_text())["verdict"]), (1, "pass"))


@test
def test_e2e_true_positive_parks_with_reproducer():
    job = "813d71882974"
    goc, g, calls, od, p, rc = regate(job)
    chk("813d pending while verified", p["verdict"], "pending-finding-check")
    p = verifier_done(goc, od, job, {0: "813d71882974-0", 1: "813d71882974-1"})
    chk("confirmed TP keeps verdict fail", p["verdict"], "fail")
    kept = [i for i in p["issues"] if i.get("confirmed_by_reproducer")]
    chk("the confirmed finding carries its reproducer", len(kept) == 1
        and Path(kept[0]["confirmed_by_reproducer"]).is_file(), True)
    chk("the refuted sibling finding is advisory, not a second park reason",
        [a["line"] for a in p.get("advisory_caveats", [])], [19])
    chk("autofix still ESCALATES (parks) it", goc.autofix_classify(p)["action"], "escalate")
    chk("finding_check outcome recorded", p["finding_check"]["outcome"], "confirmed")


@test
def test_e2e_concerns_true_caveat_parks():
    job = "c63772e0b710"
    goc, g, calls, od, p, rc = regate(job)
    p = verifier_done(goc, od, job, {0: "c63772e0b710-0"})
    chk("c637 uncertain caveat CONFIRMED -> restored to the reviewer's severity",
        (p["issues"][0]["severity"], p["issues"][0]["uncertain"]),
        (p["issues"][0].get("reviewer_severity"), False))
    chk("c637 still not landed (verdict concerns/fail, not pass)", p["verdict"] != "pass", True)


@test
def test_e2e_untouched_cases_park_as_today():
    for job in ("2f87ad7a79c2", "b1b085738769", "a0be91760fb5"):
        goc, g, calls, od, p, rc = regate(job)
        chk(f"{job}: no verifier enqueued", calls["enqueue"], [])
        chk(f"{job}: verdict unchanged and terminal block ran (today's park)",
            ([f["verdict"] for f in calls["finalize"]], p["finding_check"]["plan"]),
            ([payload(job)["verdict"]], "none"))


@test
def test_e2e_fail_safe_paths_park():
    job = "a330fff2d326"
    g = mod("gfc")
    q = payload(job)
    snap = json.dumps(q, sort_keys=True)
    chk("apply_outcome with an ERROR row changes nothing (defence in depth)",
        (g.apply_outcome(q, {0: {"outcome": "error", "why": "x"},
                             1: {"outcome": "unconfirmed", "why": "y"}}),
         json.dumps(q, sort_keys=True) == snap), ("error", True))
    goc, g, calls, od, p, rc = regate(job, materialize_ok=False)
    chk("materialize failure -> verdict fail stands, terminal block runs",
        ([f["verdict"] for f in calls["finalize"]], p["finding_check"]["status"]), (["fail"], "error"))
    goc, g, calls, od, p, rc = regate(job, enqueue_ok=False)
    chk("enqueue failure -> verdict fail stands, terminal block runs",
        ([f["verdict"] for f in calls["finalize"]], p["finding_check"]["status"]),
        (["fail"], "enqueue-failed"))
    goc, g, calls, od, p, rc = regate(job)
    p = verifier_done(goc, od, job, {}, raw=False)
    chk("verifier produced no refute.json -> restored to fail (park)",
        (p["verdict"], p["finding_check"]["status"]), ("fail", "error"))
    goc, g, calls, od, p, rc = regate(job)
    p = verifier_done(goc, od, job, {}, raw={"rows": [
        {"idx": 0, "file": "dashboard_chat.py", "line": 284, "reproducer": None,
         "problem": "model call failed: ConnectionError: refused"}]})
    chk("model unreachable -> error -> park", p["verdict"], "fail")
    goc, g, calls, od, p, rc = regate(job)
    p = verifier_done(goc, od, job, {}, raw={"rows": []})
    chk("verifier returned no row for the finding -> park", p["verdict"], "fail")
    goc, g, calls, od, p, rc = regate(job)
    p = verifier_done(goc, od, job, {}, raw={"rows": [
        {"idx": 0, "file": "other.py", "line": 1, "reproducer": "def reproduce():\n    pass\n"}]})
    chk("verifier result for a DIFFERENT finding -> park", p["verdict"], "fail")
    goc, g, calls, od, p, rc = regate(job)
    p = verifier_done(goc, od, job, {0: None})
    chk("model DECLINED to write one (none within budget) -> unconfirmed, lands",
        p["verdict"], "pass")


@test
def test_e2e_timeout_parks():
    job = "a330fff2d326"
    g = mod("gfc")
    old = g.FC_REPRO_TIMEOUT_S
    g.FC_REPRO_TIMEOUT_S = 3
    try:
        goc, g, calls, od, p, rc = regate(job)
        p = verifier_done(goc, od, job, {}, raw={"rows": [
            {"idx": 0, "file": "dashboard_chat.py", "line": 284,
             "reproducer": "import time, dashboard_chat\ndef reproduce():\n    time.sleep(60)\n"}]})
        chk("reproducer timeout -> error -> verdict fail stands (park)",
            (p["verdict"], p["finding_check"]["outcome"]), ("fail", "error"))
    finally:
        g.FC_REPRO_TIMEOUT_S = old


@test
def test_e2e_crash_in_consider_keeps_verdict():
    goc, g, calls = patched_goc()
    od, rg = stage("a330fff2d326")
    real = g.plan
    g.plan = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        goc.merge_review(A(job_label="regate-a330fff2d326", cwd=str(rg), out_dir=str(od)), od,
                         prefix="regate-", authoritative=True)
    finally:
        g.plan = real
    chk("a crash in the check leaves verdict fail and runs the terminal block",
        [f["verdict"] for f in calls["finalize"]], ["fail"])
    # A crash AFTER the verdict was flipped to pending (here: the record cannot be
    # serialised) must restore the re-gate's verdict, never strand it as pending.
    goc, g, calls = patched_goc()
    od, rg = stage("a330fff2d326")
    realv = g.FC_PENDING_VERDICT
    g.FC_PENDING_VERDICT = object()
    try:
        goc.merge_review(A(job_label="regate-a330fff2d326", cwd=str(rg), out_dir=str(od)), od,
                         prefix="regate-", authoritative=True)
    finally:
        g.FC_PENDING_VERDICT = realv
    chk("a crash after the pending flip restores verdict fail and runs the terminal block",
        [f["verdict"] for f in calls["finalize"]], ["fail"])


@test
def test_off_switch():
    goc, g, calls = patched_goc()
    goc.FINDING_CHECK_MODE = "off"
    try:
        od, rg = stage("a330fff2d326")
        goc.merge_review(A(job_label="regate-a330fff2d326", cwd=str(rg), out_dir=str(od)), od,
                         prefix="regate-", authoritative=True)
    finally:
        goc.FINDING_CHECK_MODE = "live"
    chk("GATE_FINDING_CHECK=off: no enqueue, verdict fail, terminal block ran",
        (calls["enqueue"], [f["verdict"] for f in calls["finalize"]]), ([], ["fail"]))


@test
def test_main_routes_fcheck_label():
    goc = mod("goc")
    seen = []
    goc.merge_finding_check = lambda a, od: seen.append(("fc", a.job_label)) or 0
    goc.merge_second_opinion = lambda a, od: seen.append(("so", a.job_label)) or 0
    for lab in ("secondop-a330fff2d326-fcheck", "secondop-a330fff2d326"):
        sys.argv = ["gate-on-complete.py", "--job-id", "x", "--job-label", lab, "--cwd", str(TMP),
                    "--out-dir", str(TMP)]
        goc.main()
    chk("fcheck label -> merge_finding_check; plain secondop -> merge_second_opinion", seen,
        [("fc", "secondop-a330fff2d326-fcheck"), ("so", "secondop-a330fff2d326")])
    _mods.pop("goc", None)   # monkeypatched: reload for anything after this


# ------------------------------------------------------------- runner side

class FakeModel:
    model = "fake"

    def __init__(self, replies):
        self.replies, self.users = list(replies), []

    def chat_json(self, system, user, schema, num_predict=0):
        self.users.append(user)
        r = self.replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r


@test
def test_runner_refute_mode():
    cra = mod("cra")
    od = TMP / "runner"
    od.mkdir(exist_ok=True)
    spec = {"mode": "refute", "head_tree": str(case("a330fff2d326") / "head"),
            "diff": str(case("a330fff2d326") / "a330fff2d326.diff"), "intent": "s4b",
            "rows": [{"idx": 0, "severity": "high", "file": "dashboard_chat.py", "line": 284,
                      "what": "mutates the input messages list", "detail": "### [HIGH] mutates"}]}
    (od / "task.json").write_text(json.dumps(spec))
    fake = FakeModel([{"reproducer": "def reproduce(:", "explanation": "bad"},
                      {"reproducer": repro("a330fff2d326-0"), "explanation": "ok"}])
    cra.Model = lambda *a, **k: fake
    sys.argv = ["code-review-agent.py", "--model", "m", "--host", "h", "--num-ctx", "8192",
                "--cwd", str(od), "--task-file", str(od / "task.json")]
    rc = cra.main()
    out = json.loads((od / "refute.json").read_text())
    chk("--mode refute via the task file -> refute.json, rc 0", (rc, len(out["rows"])), (0, 1))
    chk("a non-compiling reply is retried once, the good one kept",
        (out["rows"][0]["attempts"], out["rows"][0]["reproducer"] == repro("a330fff2d326-0")),
        (2, True))
    chk("the prompt carries the cited code and the claim",
        "def build_request" in fake.users[0] and "### [HIGH] mutates" in fake.users[0], True)
    fake2 = FakeModel([ConnectionError("refused")])
    cra.Model = lambda *a, **k: fake2
    cra.main()
    out = json.loads((od / "refute.json").read_text())
    chk("an unreachable model is recorded as a runner error (-> park, not land)",
        str(out["rows"][0]["problem"]).startswith("model call failed"), True)


# ------------------------------------------------------------------ slicer

@test
def test_slicer_never_lands_a_lost_check():
    sl = mod("slice")
    chk("pending finding check -> wait", sl.coding_land_route("pending-finding-check", 60), "wait")
    chk("lost finding check -> ESCALATE, never land on the live verify alone",
        sl.coding_land_route("pending-finding-check", sl.GATE_VERDICT_WAIT_S), "escalate")
    chk("untimeable -> escalate", sl.coding_land_route("pending-finding-check", None), "escalate")
    chk("unchanged: no verdict past the wait still lands",
        sl.coding_land_route(None, sl.GATE_VERDICT_WAIT_S), "land")


@test
def test_real_materialize_matches_fixture():
    """The production materialize() rebuilds the reviewed tree from the real repo."""
    g = load(GFC, "gfc_fresh")      # unpatched copy
    p = payload("a330fff2d326")
    cwd, head = p.get("job_cwd"), (p.get("launch_baseline") or {}).get("head")
    if not (cwd and Path(cwd).is_dir()):
        print(f"    SKIP - job worktree {cwd} gone; materialize() verified at staging time")
        chk("module exposes materialize", callable(g.materialize), True)
        return
    dest = TMP / "mat-a330"
    ok, why = g.materialize(cwd, head, case("a330fff2d326") / "a330fff2d326.diff", dest)
    chk(f"materialize ok ({why})", ok, True)
    chk("head/dashboard_chat.py identical to the fixture",
        (dest / "head" / "dashboard_chat.py").read_text()
        == (case("a330fff2d326") / "head" / "dashboard_chat.py").read_text(), True)


if __name__ == "__main__":
    try:
        bad = [n for n, ok in results if not ok]
        print(f"\n{len(results) - len(bad)}/{len(results)} tests passed")
        if bad:
            print("FAILED: " + ", ".join(bad))
        sys.exit(1 if bad else 0)
    finally:
        shutil.rmtree(TMP, ignore_errors=True)
