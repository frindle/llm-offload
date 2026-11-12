#!/usr/bin/env python3
"""test-harness-admission.py -- the ONE harness admission gate (harness_admission.py) and its wiring
(2026-10-10). Language-agnostic: every rule is exercised on a TS (node:test) and a python fixture.

  A. size cap: > MAX_TARGET_FILES target files or an oversize harness => oversize-reslice (re-slice)
  B. spec-traceability is BLOCKING (strict tier) with an allowlist (spec-ok, assertion prose, ISO dates)
  C. refimpl that cannot apply (anchor missing) => refimpl-unapplicable; contract-broken via preflight
  D. baseline must be RED for an assertion reason (green / crash both refused)
  E. tiers: strict refuses at once; authoring tier repairs ONCE (advisory) then refuses a recurrence
  F. FREEZE: an admitted harness is snapshotted; a job that edits it is restored
  G. reslice-required: note_stop => further identical rounds refused (repair label exempt, fp change clears)
  H. wiring: ollama-dispatch-auto._dispatch_model_inner never enqueues a refused job; record_stop of a
     runaway stop records reslice; queue cmd_enqueue backstop refuses; slice + auto_continue_go call admit
HA_SRC / AUTO_SRC / QUEUE_SRC / SLICE_SRC override the files under test (revert proofs)."""
import importlib.util, json, os, re, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
HA = Path(os.environ.get("HA_SRC") or HERE / "harness_admission.py")
AUTO = Path(os.environ.get("AUTO_SRC") or HERE / "ollama-dispatch-auto")
QUEUE = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
SLICE = Path(os.environ.get("SLICE_SRC") or HERE / "ollama-dispatch-slice")
FAILS = []
TMP = Path(tempfile.mkdtemp(prefix="ha-test-"))
os.environ["HARNESS_ADMISSION_DIR"] = str(TMP / "adm")
sys.path.insert(0, str(HERE))


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def guard(name, fn):
    try:
        fn()
    except Exception as e:
        print(f"FAIL - {name}  (raised {type(e).__name__}: {e})")
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    argv, sys.argv = sys.argv, [str(path)]
    try:
        ld.exec_module(mod)
    finally:
        sys.argv = argv
    return mod


ha = load(HA, "harness_admission_under_test")
n_wt = [0]


def git(wt, *a):
    subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)


def mk(lang="ts", target="lib/x.ts", base="export function f(a){ return a + 1 }\n", fixture=None,
       refimpl=None, verify="#!/bin/bash\necho 'AssertionError: expected 3'\nexit 1\n", task=None):
    n_wt[0] += 1
    wt = TMP / f"wt{n_wt[0]}"
    (wt / Path(target).parent).mkdir(parents=True)
    git(TMP, "init", "-q", str(wt))
    (wt / target).write_text(base)
    git(wt, "add", "-A")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    fxn = "verify.test.ts" if lang == "ts" else "verify_fixture.py"
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": target, "fixture": fxn}))
    (wt / "TASK.md").write_text(task or f"# t\nMake f return 2.\n\n## Must contain\n- `return 2`\n\nOnly edit `{target}`; do not edit `verify.sh`.\n")
    (wt / fxn).write_text(fixture if fixture is not None else
                          ("import test from 'node:test'\ntest('f', () => { assert.equal(f(1), 2) })\n" if lang == "ts"
                           else "assert f(1) == 2\n"))
    (wt / "verify.sh").write_text(verify)
    if refimpl is not None:
        (wt / "refimpl.py").write_text(refimpl)
    return wt


def causes(res):
    return sorted({d["cause"] for d in res["defects"]})


GOOD_REF = 'OLD = "return a + 1"\nNEW = "return 2"\nt = open("lib/x.ts").read()\nassert OLD in t\n'
BAD_REF = 'OLD = "return a + 99"\nNEW = "return 2"\nt = open("lib/x.ts").read()\nassert OLD in t\n'
NOPRE = lambda cmd, wt: {"checks": []}


def run_all():
    for lang, tgt in (("ts", "lib/x.ts"), ("py", "lib/x.py")):
        base = "export function f(a){ return a + 1 }\n" if lang == "ts" else "def f(a):\n    return a + 1\n"
        # ---- tier mapping
        chk("tier -s1 none", ha.tier_for_label("auto-author-x-s1"), None)
        chk("tier -s2 none", ha.tier_for_label("auto-author-x-s2-r1"), None)
        chk("tier -s3 stage-refimpl", ha.tier_for_label("auto-author-x-s3"), "stage-refimpl")
        chk("tier -c1 authoring", ha.tier_for_label("auto-author-x-c1"), "authoring")
        chk("tier refine", ha.tier_for_label("auto-refine-x-r1"), "refine")
        chk("tier repair", ha.tier_for_label("auto-refine-p-s3-r101"), "repair")
        chk("tier coding strict", ha.tier_for_label("rt-foo-s3-update"), "strict")

        # ---- clean harness -> admitted
        wt = mk(lang, tgt, base, refimpl=GOOD_REF.replace("lib/x.ts", tgt))
        r = ha.admit(wt, "rt-clean-s1", target=tgt, preflight_runner=NOPRE)
        chk(f"[{lang}] clean harness admitted", (r["ok"], r["defects"]), (True, []))

        # ---- A. size
        wt = mk(lang, tgt, base, task=f"x\nOnly edit `a/1.ts`, `a/2.ts`, `a/3.ts`, `a/4.ts`.\n## Must contain\n- `return 2`\n")
        r = ha.admit(wt, "auto-author-big-c1", target=tgt, preflight_runner=NOPRE)
        chk(f"[{lang}] 5 target files => oversize-reslice refused even at authoring tier",
            (r["ok"], r["cause"]), (False, "oversize-reslice"))
        wt = mk(lang, tgt, base, fixture="// pad\n" + "x" * 70000)
        r = ha.admit(wt, "rt-big-s1", target=tgt)
        chk(f"[{lang}] oversize harness bytes refused", (r["ok"], r["cause"]), (False, "oversize-reslice"))

        # ---- B. traceability
        fx_bad = ("import test from 'node:test'\ntest('f', () => { assert.equal(f(1), 'zzqq-unspecified-literal') })\n"
                  if lang == "ts" else "assert f(1) == 'zzqq-unspecified-literal'\n")
        wt = mk(lang, tgt, base, fixture=fx_bad)
        r = ha.admit(wt, "rt-trace-s1", target=tgt)
        chk(f"[{lang}] untraceable literal BLOCKS a strict (coding) enqueue", (r["ok"], r["cause"]), (False, "fixture-untraceable"))
        fx_ok = ("test('f', () => { assert.equal(f(1), 'zzqq-unspecified-literal') }) // spec-ok\n" if lang == "ts"
                 else "assert f(1) == 'zzqq-unspecified-literal'  # spec-ok\n")
        wt = mk(lang, tgt, base, fixture=fx_ok)
        chk(f"[{lang}] spec-ok allowlists the line", ha.admit(wt, "rt-trace-s1", target=tgt)["ok"], True)
        fx_msg = ("test('f', () => { assert.equal(f(1), 2, 'expected linked++') })\n" if lang == "ts"
                  else "assert f(1) == 2, 'expected linked++'\n")
        wt = mk(lang, tgt, base, fixture=fx_msg)
        chk(f"[{lang}] assertion-message prose ('expected linked++') is allowlisted", ha.admit(wt, "rt-trace-s1", target=tgt)["ok"], True)

        # ---- C. refimpl unapplicable / contract
        wt = mk(lang, tgt, base, refimpl=BAD_REF.replace("lib/x.ts", tgt))
        r = ha.admit(wt, "rt-ref-s1", target=tgt, preflight_runner=NOPRE)
        chk(f"[{lang}] refimpl anchor absent => refimpl-unapplicable (strict refuses)", (r["ok"], r["cause"]), (False, "refimpl-unapplicable"))
        wt = mk(lang, tgt, base, refimpl=GOOD_REF.replace("lib/x.ts", tgt))
        bad_pf = lambda cmd, w: {"checks": [{"id": "refimpl-passes", "status": "FAIL", "message": "fixture case 'MUST NOT link' fails with refimpl"}]}
        r = ha.admit(wt, "rt-con-s1", target=tgt, preflight_runner=bad_pf)
        chk(f"[{lang}] fixture failing WITH refimpl => contract-broken", (r["ok"], r["cause"]), (False, "contract-broken"))

        # ---- D. baseline
        wt = mk(lang, tgt, base, verify="#!/bin/bash\necho VERIFY_OK\nexit 0\n")
        r = ha.admit(wt, "rt-base-s1", target=tgt)
        chk(f"[{lang}] verify green at baseline refused", (r["ok"], r["cause"]), (False, "baseline-not-red"))
        wt = mk(lang, tgt, base, verify="#!/bin/bash\necho \"SyntaxError: unexpected token\"\nexit 1\n")
        r = ha.admit(wt, "rt-base-s1", target=tgt)
        chk(f"[{lang}] verify crash (wrong reason) at baseline refused", (r["ok"], r["cause"]), (False, "baseline-not-red"))

        # ---- E. authoring tier: repair once, refuse recurrence; stage-refimpl skips refimpl checks
        wt = mk(lang, tgt, base, fixture=fx_bad)
        r1 = ha.admit(wt, "auto-author-x-c1", target=tgt)
        chk(f"[{lang}] authoring tier: first sighting is ADVISORY (model may repair once)", (r1["ok"], bool(r1["advisory"])), (True, True))
        chk(f"[{lang}] ...advisory text is appended to the prompt", "HARNESS ADMISSION DEFECTS" in ha.advisory_prompt(r1), True)
        r2 = ha.admit(wt, "auto-author-x-c2", target=tgt)
        chk(f"[{lang}] authoring tier: SAME defect set again => repeated-defect refusal", (r2["ok"], r2["cause"]), (False, "repeated-defect"))
        r3 = ha.admit(wt, "auto-author-x-c3", target=tgt)
        chk(f"[{lang}] ...and the state now says reslice-required for every later identical round", (r3["ok"], r3["cause"]), (False, "reslice-required"))
        chk(f"[{lang}] ...but a self-heal repair label is exempt", ha.admit(wt, "auto-refine-p-s3-r101", target=tgt)["ok"], True)
        (wt / ("verify.test.ts" if lang == "ts" else "verify_fixture.py")).write_text(fx_ok)
        chk(f"[{lang}] ...and a changed (repaired) harness re-passes the gate", ha.admit(wt, "auto-author-x-c4", target=tgt)["ok"], True)

        # ---- F. freeze
        wt = mk(lang, tgt, base, refimpl=GOOD_REF.replace("lib/x.ts", tgt))
        ha.admit(wt, "rt-frz-s1", target=tgt, preflight_runner=NOPRE)
        fxn = "verify.test.ts" if lang == "ts" else "verify_fixture.py"
        orig = (wt / fxn).read_text()
        (wt / fxn).write_text("// model rewrote the fixture\n")
        (wt / "TASK.md").write_text("changed")
        restored = ha.enforce_freeze(wt)
        chk(f"[{lang}] enforce_freeze restores modified harness files", (sorted(restored), (wt / fxn).read_text() == orig),
            (sorted([fxn, "TASK.md"]), True))
        chk(f"[{lang}] enforce_freeze is a no-op on an unadmitted harness", ha.enforce_freeze(mk(lang, tgt, base, fixture=fx_bad)), [])

        # ---- G. note_stop / backstop
        wt = mk(lang, tgt, base)
        chk("backstop allows a clean harness", ha.backstop_refusal(wt, "auto-author-x-c1"), None)
        ha.note_stop(wt, "repeated_signature", "identical harness-level failure signature")
        r = ha.admit(wt, "auto-author-x-c1", target=tgt)
        chk(f"[{lang}] runaway stop => next identical author round refused (reslice-required)", (r["ok"], r["cause"]), (False, "reslice-required"))
        chk(f"[{lang}] ...also a refine round", ha.admit(wt, "auto-refine-x-r1", target=tgt)["cause"], "reslice-required")
        chk("backstop refuses the recorded harness for author labels", bool(ha.backstop_refusal(wt, "auto-author-x-c9")), True)
        chk("backstop refuses refine labels too", bool(ha.backstop_refusal(wt, "auto-refine-x-r1")), True)
        chk("backstop exempts repair labels", ha.backstop_refusal(wt, "auto-refine-x-r102"), None)
        chk("backstop ignores non-harness labels", ha.backstop_refusal(wt, "esc-review-foo"), None)
        (wt / "TASK.md").write_text((wt / "TASK.md").read_text() + "\nmore\n")
        chk("backstop clears when the harness fingerprint changes", ha.backstop_refusal(wt, "auto-author-x-c9"), None)


guard("core", run_all)


# ---- H. wiring ----------------------------------------------------------------------------
def wiring():
    m = load(AUTO, "auto_under_test")
    m.AUTO_RUNS_DIR = TMP / "runs"
    decisions = []
    m.log_decision = lambda kind, a, decision, detail="", persist=True, **x: decisions.append((kind, decision, x))
    m.salvage_round = lambda *a, **k: None
    enq = []
    m.capture = lambda cmd, **k: (enq.append(cmd), (1, "", "must not be called"))[1]
    m._stop_for_hand = lambda *a, **k: None
    a = SimpleNamespace(label="w", bundle=None, lang="python", drafter_cmd=None, author_max_iters=3, model="m",
                        host="h", num_ctx=1, max_tokens=0, timeout=5, target="lib/x.py", require=[],
                        _author_rounds_run=0, _chain_last_job=None, budget_policy=None)
    wt = mk("py", "lib/x.py", "def f(a):\n    return a + 1\n", refimpl=BAD_REF.replace("lib/x.ts", "lib/x.py"))
    ok, why = m._dispatch_model_inner(wt, "p", "auto-refine-w-r1", "true", a)
    chk("auto: refine job against a defective harness NOT enqueued", (ok, enq, why.startswith(m.ADMISSION_ROUTE_PREFIX), "SPEC_DEFECT: " in why), (False, [], True, False))
    chk("auto: stop_reason admission_refused recorded", any(x.get("stop_reason") == "admission_refused" for _, _, x in decisions), True)
    # a runaway stop (record_stop) marks the harness reslice-required for the queue backstop
    wt2 = mk("py", "lib/x.py", "def f(a):\n    return a + 1\n")
    a2 = SimpleNamespace(**dict(vars(a), _admission_wt=wt2))
    m.record_stop(a2, "repeated_signature", "identical", kind="spec-defect")
    chk("auto: record_stop(repeated_signature) => admission reslice state", bool(ha.backstop_refusal(wt2, "auto-author-w-c1")), True)
    wt3 = mk("py", "lib/x.py", "def f(a):\n    return a + 1\n")
    a3 = SimpleNamespace(**dict(vars(a), _admission_wt=wt3))
    m.record_stop(a3, "external_kill", "driver exited 143", kind="round-stop")
    chk("auto: an external kill does NOT mark the harness for re-slice", ha.backstop_refusal(wt3, "auto-author-w-c1"), None)
    # queue backstop (refusal path only: nothing is enqueued)
    env = dict(os.environ, HARNESS_ADMISSION_DIR=str(TMP / "adm"), HOME=str(TMP))
    tf = TMP / "t.md"
    tf.write_text("x")
    p = subprocess.run([sys.executable, str(QUEUE), "enqueue", "--model", "m", "--host", "studio", "--cwd", str(wt2),
                        "--task-file", str(tf), "--verify", "true", "--label", "auto-author-w-c1"],
                       capture_output=True, text=True, env=env)
    chk("queue cmd_enqueue backstop refuses (rc!=0, named cause, no 'enqueued')",
        (p.returncode != 0, "reslice-required" in (p.stdout + p.stderr), "enqueued " in p.stdout), (True, True, False))
    # call sites
    src = AUTO.read_text()
    chk("auto_continue_go calls admit before the coding enqueue",
        bool(re.search(r"_ha\(\)\.admit\(wt, a\.label.*?\n.*?\n.*?\n.*?\n.*?\n\s+cmd = \[sys\.executable, str\(QUEUE\), \"enqueue\"", src, re.S)), True)
    chk("slice confirm_and_enqueue calls admit before the coding enqueue",
        bool(re.search(r"_ha\.admit\(wt, f\"\{st\['label'\]\}-\{sid\}\".*?\[\"python3\", QUEUE, \"enqueue\"", SLICE.read_text(), re.S)), True)


guard("wiring", wiring)

# ---- I. real Costco s3 harness (copied files) + fail-closed dynamic path ------------------------
def costco():
    FX = HERE / "test-fixtures-harness-admission"
    import shutil
    wt = TMP / "costco"
    shutil.copytree(FX / "costco" / "base", wt)
    git(TMP, "init", "-q", str(wt))
    git(wt, "add", "-A")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    for f in ("TASK.md", "refimpl.py", "verify.test.ts", "verify.sh", ".dispatch-harness.json"):
        shutil.copy2(FX / "costco" / f, wt / f)
    real_out = (FX / "costco-s3-selfcheck.out").read_text()
    base_red = lambda cmd, cwd, t: (1, "AssertionError: expected 1 to equal 2")
    # classifier
    chk("classify: REAL Costco s3 self-check output (SyntaxError in the TARGET) => refimpl-syntax", ha.classify_selfcheck_failure(real_out)[0], "refimpl-syntax")
    chk("classify: plain failing cases => contract-broken", ha.classify_selfcheck_failure("  FAIL: the reference impl does not make verify.sh print VERIFY_OK\n  FAILED CASE: not ok 1 - x")[0], "contract-broken")
    chk("classify: SyntaxError in patched target => refimpl-syntax",
        ha.classify_selfcheck_failure("  FAIL: refimpl.py WROTE source that does not parse -- SyntaxError in route.ts")[0], "refimpl-syntax")
    chk("classify: import anchor not found => refimpl-unapplicable",
        ha.classify_selfcheck_failure("  FAIL: refimpl.py errored: AssertionError: refimpl import anchor not found")[0], "refimpl-unapplicable")
    chk("classify: linkReceiptToOrder anchor not found => refimpl-unapplicable",
        ha.classify_selfcheck_failure("assert OLD in t, 'linkReceiptToOrder anchor not found'")[0], "refimpl-unapplicable")
    # dynamic: self-check script present => it is THE contract test, whatever preflight would say
    (wt / "auto-harness-check.py").write_text("# stub marker\n")
    sc_real = lambda cmd, cwd, t: (1, real_out)
    sc_bad = lambda cmd, cwd, t: (1, "  FAIL: the reference impl does not make verify.sh print VERIFY_OK\n  FAILED CASE: not ok 9 - POST: no transactionBarcode")
    sc_syn = lambda cmd, cwd, t: (1, "  FAIL: refimpl.py WROTE source that does not parse -- SyntaxError")
    sc_ok = lambda cmd, cwd, t: (0, "VERIFY_OK: harness discriminates and is satisfiable")
    sc_hang = lambda cmd, cwd, t: (124, "")

    def sc_raise(cmd, cwd, t):
        raise OSError("boom")
    def fresh(fn):
        ha.state_path(wt).unlink(missing_ok=True)
        return ha.admit(wt, "rt-costco-s3-update-post-route", runner=base_red, selfcheck_runner=fn)
    r = fresh(sc_bad)
    chk("costco: failing self-check refuses a coding enqueue (contract-broken among the defects)", (r["ok"], "contract-broken" in causes(r)), (False, True))
    r = fresh(sc_real)
    chk("costco: the REAL s3 self-check output is refused with refimpl-syntax", (r["ok"], "refimpl-syntax" in causes(r)), (False, True))
    r = fresh(sc_syn)
    chk("costco: SyntaxError in the patched target is a NAMED defect (refimpl-syntax)", (r["ok"], "refimpl-syntax" in causes(r)), (False, True))
    for nm, fn in (("hang/timeout", sc_hang), ("runner exception", sc_raise)):
        r = fresh(fn)
        chk(f"costco: dynamic path {nm} is FAIL-CLOSED (defect, not ok)", (r["ok"], "contract-broken" in causes(r)), (False, True))
        if fn is sc_hang:
            chk("costco: the hang is reported as 'could not be measured' (not as a plain failing case)",
                any("could not be measured" in d["detail"] for d in r["defects"]), True)
    r = fresh(sc_ok)
    chk("costco: passing self-check adds no contract defect", "contract-broken" in causes(r) or "refimpl-syntax" in causes(r), False)
    # the s3 stage label: refimpl-side defects are no longer skipped; first sighting advisory, recurrence refused
    ha.state_path(wt).unlink(missing_ok=True)
    r1 = ha.admit(wt, "auto-author-rt-costco-s3-update-post-route-s3", runner=base_red, selfcheck_runner=sc_syn)
    chk("costco s3 label: refimpl-syntax detected (advisory on first sighting)", (r1["ok"], "refimpl-syntax" in causes(r1), bool(r1["advisory"])), (True, True, True))
    r2 = ha.admit(wt, "auto-author-rt-costco-s3-update-post-route-s3-r1", runner=base_red, selfcheck_runner=sc_syn)
    chk("costco s3 label: identical defect again => refused repeated-defect", (r2["ok"], r2["cause"]), (False, "repeated-defect"))
    # preflight fallback without a self-check script: unmeasurable is a defect
    (wt / "auto-harness-check.py").unlink()
    r = ha.admit(wt, "rt-costco-x", runner=base_red, preflight_runner=lambda cmd, w: {"checks": []}) if False else None
    d = ha.contract_defects(wt, preflight_runner=lambda cmd, w: (_ for _ in ()).throw(RuntimeError("no preflight")))
    chk("preflight fallback crash is a defect (never fail-open)", [x["cause"] for x in d], ["contract-broken"])
    # gate-internal exceptions surface as defects
    orig = ha.traceability_findings
    ha.traceability_findings = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit import failed"))
    try:
        d = ha.collect(wt, dynamic=False, runner=base_red)
        chk("exception inside a static check => defect, not silently ok",
            any(x["cause"] == "fixture-untraceable" and "could not run" in x["detail"] for x in d), True)
    finally:
        ha.traceability_findings = orig
    # baseline timeout is a defect
    d = ha.baseline_findings(wt, "app/api/costco/receipts/route.ts", runner=lambda c, w, t: (124, ""))
    chk("baseline verify timeout => defect", bool(d), True)


guard("costco", costco)


# ---- K. BASE-ONLY static checks (an in-progress author's edits to the target must not matter) ---
def base_only():
    FX = HERE / "test-fixtures-harness-admission"
    import shutil
    wt = TMP / "costco-mod"
    shutil.copytree(FX / "costco" / "base", wt)
    git(TMP, "init", "-q", str(wt))
    git(wt, "add", "-A")
    git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    for f in ("TASK.md", "refimpl.py", "verify.test.ts", "verify.sh", ".dispatch-harness.json"):
        shutil.copy2(FX / "costco" / f, wt / f)
    tgt = "app/api/costco/receipts/route.ts"
    clean = ha.refimpl_static_findings(wt, tgt)
    chk("base-only: pristine worktree, real refimpl anchors match HEAD => no static findings", clean, [])
    # the previous round's model edits (REAL modified route.ts from the live s3 worktree)
    shutil.copy2(FX / "costco" / "modified" / "route.ts", wt / tgt)
    chk("fixture sanity: route.ts really differs from HEAD", ha._refimpl_applied(wt, tgt), True)
    chk("base-only: target edited after HEAD by an author => anchors still match HEAD => no findings",
        ha.refimpl_static_findings(wt, tgt), [])
    import dispatch_harness_gates as hg
    chk("fixture sanity: the WORKING-TREE check WOULD have false-positived here", bool(hg.refimpl_anchor_findings(wt, tgt)), True)
    res = ha.collect(wt, dynamic=False, runner=lambda c, w, t: (1, "AssertionError: expected 1 to equal 2"))
    chk("base-only: collect() on the edited tree has no refimpl-unapplicable", "refimpl-unapplicable" in [d["cause"] for d in res], False)
    # genuinely stale anchor still refuses
    ref = (wt / "refimpl.py").read_text()
    stale = ref.replace("async function linkReceiptToOrder(", "async function linkReceiptToOrderGONE(", 1)
    chk("fixture sanity: stale mutation changed refimpl", stale != ref, True)
    (wt / "refimpl.py").write_text(stale)
    f = ha.refimpl_static_findings(wt, tgt)
    chk("base-only: a genuinely stale anchor is still refused", any("GONE" in x for x in f), True)
    (wt / "refimpl.py").write_text(ref)
    # baseline on a dirty tree is measured in a HEAD scratch copy, not on the model's edits
    seen = []
    def rec(cmd, cwd, t):
        seen.append(str(cwd)); return (1, "AssertionError: expected 1 to equal 2")
    d = ha.baseline_findings(wt, tgt, runner=rec)
    chk("base-only: dirty-tree baseline is measured in a scratch copy, not the live tree",
        (len(seen) == 1 and seen[0] != str(wt), d), (True, []))
    # HTTP status allowlist
    fx_lines = ["  assert.strictEqual(res.status, 200);", "  assert.strictEqual(res.total, 200);"]
    for tk in ("[1, 3]", "[-2, 0]", "[4, 4]", "(1, 2)"):
        chk(f"allowlist: tiny-int worked-example string {tk!r}", ha._allowlisted({"token": tk, "line": 1}), True)
    for tk in ("[1000, 3]", "abc [1, 3]", "2026"):
        chk(f"allowlist: {tk!r} is NOT a tiny-int example (stays a finding)", ha._allowlisted({"token": tk, "line": 1}), False)
    chk("allowlist: HTTP status 200 on a status line", ha._allowlisted({"token": "200", "line": 1}, fx_lines), True)
    chk("allowlist: bare 200 NOT on a status line stays a finding", ha._allowlisted({"token": "200", "line": 2}, fx_lines), False)
    chk("allowlist: non-status number 777 on a status line stays a finding", ha._allowlisted({"token": "777", "line": 1}, ["res.status, 777"]), False)
    t = ha.traceability_findings(wt, tgt)
    chk("costco: the real fixture's 200s (res.status) are no longer untraceable", [x for x in t if x["token"] == "200"], [])


guard("base_only", base_only)


def scratch_ignored():
    wt = mk()
    (wt / ".gitignore").write_text(".dispatch-harness.json\ngen/\n")
    git(wt, "add", ".gitignore"); git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "ign")
    (wt / ".dispatch-harness.json").write_text('{"target": "lib/x.ts"}')
    (wt / "gen").mkdir(); (wt / "gen" / "client.ts").write_text("export const c = 1\n")
    sc, err = ha.scratch_copy(wt, apply_refimpl=False)
    try:
        chk("scratch copy carries git-IGNORED harness manifest + generated code",
            (sc is not None and (sc / ".dispatch-harness.json").is_file(), (sc / "gen" / "client.ts").is_file()), (True, True))
    finally:
        ha.drop_scratch(sc)


guard("scratch_ignored", scratch_ignored)


# ---- L. INTERACTIONS (2026-10-10k): freeze x staged authoring, refusal x respec, repair x admit, fp scope, attempt classes
def interactions():
    tgt = "lib/x.ts"
    base = "export function f(a){ return a + 1 }\n"
    def admitted():
        wt = mk("ts", tgt, base, refimpl=GOOD_REF)
        r = ha.admit(wt, "rt-frz-s1", target=tgt, preflight_runner=NOPRE)
        assert r["ok"], r
        return wt
    # --- freeze x staged authoring
    wt = admitted()
    st = ha.load_state(wt)
    chk("L1 snapshot taken for a COMPLETE harness whose contract was measured green", (bool(st.get("frozen")), st.get("frozen_fp") == st.get("fp")), (True, True))
    (wt / "refimpl.py").write_text(GOOD_REF + "# model edit\n")
    for lab in ("auto-author-x-s1", "auto-author-x-s2-r1", "auto-author-x-s3", "auto-author-x-s3-r2", "auto-refine-x-r1", "rt-x-r1001"):
        chk(f"L1 staged/refine/repair label {lab}: real authoring is NOT reverted", (ha.enforce_freeze(wt, lab), (wt / "refimpl.py").read_text().endswith("# model edit\n")), ([], True))
    chk("L1 a continuation round (-c1) on the admitted harness IS restored", (ha.enforce_freeze(wt, "auto-author-x-c1"), (wt / "refimpl.py").read_text() == GOOD_REF), (["refimpl.py"], True))
    wt = admitted()
    st = ha.load_state(wt); st["fp"] = "deadbeef"; ha.save_state(wt, st)
    (wt / "refimpl.py").write_text(GOOD_REF + "# x\n")
    chk("L1 st.fp != fingerprint recorded at snapshot time => nothing restored", ha.enforce_freeze(wt, "auto-author-x-c1"), [])
    # contract not measured / incomplete harness => no snapshot at all
    wt = mk("ts", tgt, base, refimpl=GOOD_REF)
    ha.admit(wt, "rt-nodyn-s1", target=tgt, dynamic=False)
    chk("L1 dynamic=False (contract not measured) => admitted but NOT frozen", bool(ha.load_state(wt).get("frozen")), False)
    wt = mk("ts", tgt, base, refimpl=GOOD_REF)
    (wt / "TASK.md").write_text("# t\nTODO -- fill in\n\n## Must contain\n- `return 2`\n")
    ha.admit(wt, "rt-todo-s1", target=tgt, preflight_runner=NOPRE)
    chk("L1 placeholder TASK.md => NOT frozen even with a green contract", bool(ha.load_state(wt).get("frozen")), False)
    wt = mk("ts", tgt, base, refimpl="# TODO -- the exact text to replace\n")
    ha.admit(wt, "rt-stub-s1", target=tgt, preflight_runner=NOPRE)
    chk("L1 stub refimpl => NOT frozen", bool(ha.load_state(wt).get("frozen")), False)
    # same-path re-scaffold: stub files again + stale snapshot => state cleared, nothing restored
    wt = admitted()
    (wt / "refimpl.py").write_text("# TODO -- the exact text to replace\n")
    chk("L1 same-path RE-SCAFFOLD (stub refimpl again): freeze does not restore, state cleared",
        (ha.enforce_freeze(wt, "auto-author-x-c1"), ha.state_path(wt).exists(), (wt / "refimpl.py").read_text().startswith("# TODO")), ([], False, True))
    # worktree removal clears state + snapshot (slice remove_worktree)
    wt = admitted()
    sl = load(SLICE, "slice_ha_inter")
    chk("L1 precondition: state + snapshot exist", (ha.state_path(wt).exists(), ha._snap_dir(wt).exists()), (True, True))
    sl.remove_worktree(str(TMP), str(wt))
    chk("L1 slice remove_worktree clears admission state AND the .snap dir", (ha.state_path(wt).exists(), ha._snap_dir(wt).exists()), (False, False))
    # --- round 0 (unstaged legacy label) against the untouched scaffold: nothing to admit, no dynamic cost
    wt = mk("ts", tgt, base, refimpl="# TODO -- the exact text to replace\n")
    (wt / "TASK.md").write_text("# t\nTODO -- fill in the intent\n\n## Must contain\n- `TODO -- literal`\n")
    calls = []
    r = ha.admit(wt, "auto-author-pcanary-s1-lower", target=tgt, runner=lambda c, w, t: (calls.append(c), (0, "VERIFY_OK"))[1],
                 preflight_runner=lambda c, w: (calls.append(c), {"checks": []})[1])
    chk("L6 round-0 label on the untouched scaffold: admitted without running ANY check (no lane-gap latency)", (r["ok"], calls, ha.harness_started(wt)), (True, [], False))
    r = ha.admit(wt, "rt-x", target=tgt, dynamic=False, runner=lambda c, w, t: (1, "AssertionError: x"))
    chk("L6 a STRICT (coding) admit still gates the scaffold", r["tier"], "strict")
    (wt / "refimpl.py").write_text(GOOD_REF)
    chk("L6 once refimpl.py is real the harness counts as started", ha.harness_started(wt), True)
    # --- fingerprint scope x reslice / history
    wt = admitted()
    fp0 = ha.fingerprint(wt)
    (wt / "verify.sh").write_text((wt / "verify.sh").read_text() + "# tweak\n")
    chk("L4 verify.sh is part of the fingerprint", ha.fingerprint(wt) != fp0, True)
    fp1 = ha.fingerprint(wt)
    (wt / "check_literals.py").write_text("print('x')\n")
    chk("L4 check_literals.py is part of the fingerprint", ha.fingerprint(wt) != fp1, True)
    wt = admitted()
    (wt / "verify.sh").write_text((wt / "verify.sh").read_text() + "# tweak\n")
    chk("L4 freeze covers verify.sh: an edit by a -c1 round is restored", ("verify.sh" in ha.enforce_freeze(wt, "auto-author-x-c1")), True)
    wt = mk("ts", tgt, base, refimpl=GOOD_REF)
    ha.note_stop(wt, "repeated_signature", "same failure")
    r = ha.admit(wt, "auto-author-x-c1", target=tgt, dynamic=False)
    chk("L4 reslice recorded => refused", (r["ok"], r["cause"]), (False, "reslice-required"))
    (wt / "verify.sh").write_text((wt / "verify.sh").read_text() + "# fixed\n")
    r = ha.admit(wt, "auto-author-x-c1", target=tgt, dynamic=False)
    chk("L4 editing verify.sh (fingerprint change) lifts the reslice", (r["ok"], r["cause"]), (True, None))
    # history survives a fingerprint change: same defect set after an unrelated verify.sh edit is a RECURRENCE
    wt = mk("ts", tgt, base, refimpl=BAD_REF)
    r1 = ha.admit(wt, "auto-author-x-s3", target=tgt, dynamic=False)
    (wt / "verify.sh").write_text((wt / "verify.sh").read_text() + "# unrelated\n")
    r2 = ha.admit(wt, "auto-author-x-s3-r1", target=tgt, dynamic=False)
    chk("L4 defect history keyed by signature survives a fingerprint change", (r1["ok"], r2["ok"], r2["cause"]), (True, False, "repeated-defect"))
    # --- refusal x respec: ONE owner of the re-spec budget
    au = load(AUTO, "auto_inter")
    wt = mk("ts", tgt, base, refimpl=BAD_REF)
    r = ha.admit(wt, "rt-x-s1", target=tgt, dynamic=False)
    msg = r["message"]
    chk("L2 refusal carries its OWN marker (not SPEC_DEFECT)", (msg.startswith(ha.ADMISSION_ROUTE_PREFIX), "SPEC_DEFECT" in msg), (True, False))
    chk("L2 marker constant is identical in harness_admission / auto / slice", (ha.ADMISSION_ROUTE_PREFIX, au.ADMISSION_ROUTE_PREFIX, sl.ADMISSION_MARK), (ha.ADMISSION_ROUTE_PREFIX,) * 3)
    chk("L2 slicer: is_spec_defect False, is_admission_refusal True", (sl.is_spec_defect(msg), sl.is_admission_refusal(msg)), (False, True))
    chk("L2 respec_class names it 'admission'", sl.respec_class({}, msg), "admission")
    chk("L2 auto: no-more-rounds + 'die' route (never auto-sliced)", (au._failure_route(msg), msg.startswith(au.NO_MORE_ROUNDS_PREFIXES)), ("die", True))
    chk("L2 auto: the continuation ladder / escalation stop on it", au.ADMISSION_ROUTE_PREFIX in au.NO_MORE_ROUNDS_PREFIXES, True)
    # drive the slicer's real failure handler
    sl.save_state = lambda st, **k: None
    sl.slice_job_awaiting_gate = lambda st, sid: None
    removed = []
    sl.remove_worktree = lambda cwt, w: removed.append(w)
    sl.read_state = lambda label: None
    sl.retry_guard = lambda *a, **k: (True, None, [])
    SID = "s3-x"
    def attempt(sig, s):
        s["status"] = "failed"; s["last_auto_error"] = sig
        return sl.bound_stale_worktree_retry({"label": "run", "slices": {SID: s}}, SID, s, "/nonexistent-wt", "/nonexistent-cwt", "test")
    s = {"status": "failed"}
    rr = attempt("ERROR: " + msg, s)
    chk("L2 1st admission refusal => ONE bounded re-spec (retry allowed, worktree removed)", (rr, s.get("respec_count"), removed != []), (True, 1, True))
    chk("L5 refusal is a NON-ATTEMPT: attempt + launch counters handed back", (s.get("author_attempts"), s.get("author_launches_total")), (0, 0))
    rr = attempt("ERROR: " + msg, s)
    chk("L5 ...and the ESCALATED path leaves attempts/launches at 0 too", (s.get("author_attempts"), s.get("author_launches_total")), (0, 0))
    chk("L2 2nd refusal => budget spent => ESCALATED with a repair hint (no 2nd re-spec)", (rr, s["status"], s.get("respec_count"), "re-spec budget already spent" in s.get("escalation_reason", "")), (False, "escalated", 1, True))
    # K identical refusals never trip the generic K-of-K / attempt / launch caps on their own
    s = {"status": "failed", "respec_count": 0}
    outs = []
    for _ in range(sl.MAX_IDENTICAL_AUTHOR_FAILURES + 2):
        s["status"] = "failed"
        outs.append(attempt("ERROR: " + msg, s))
        if s["status"] == "escalated": break
        s["respec_count"] = 0               # re-arm the budget: only the NON-ATTEMPT accounting is under test
    chk("L5 K-of-K identical admission (contract-broken class) refusals do not consume author attempts/launches",
        (s.get("author_attempts"), s.get("author_launches_total"), outs.count(True) == len(outs)), (0, 0, True))
    # a REAL (non-refusal) identical failure still counts and still trips the cap
    s = {"status": "failed", "respec_count": sl.RESPEC_CAP}
    o = []
    for _ in range(sl.MAX_IDENTICAL_AUTHOR_FAILURES):
        o.append(attempt("ERROR: authoring did not converge: job aaaaaaaaaaaa failed (exit 1) | FAIL: same", s))
    chk("L5 control: real identical failures still count and park", (o[-1], s["status"]), (False, "escalated"))
    # REAUTHOR / -rsN labels are gated like any author round, and a recorded reslice stops a re-author run
    for lab in ("auto-author-w-rs1", "auto-author-w-c2", "auto-author-w-esc", "auto-author-w-fresh"):
        chk(f"L2 tier of {lab} is gated (authoring)", ha.tier_for_label(lab), "authoring")
    wt = mk("ts", tgt, base, refimpl=GOOD_REF)
    ha.note_stop(wt, "repeated_signature", "x")
    chk("L2 land_go REAUTHOR spawns an auto run: its first author enqueue is refused at the same harness (reslice) and the queue backstop agrees",
        (ha.admit(wt, "auto-author-w-s3", target=tgt, dynamic=False)["cause"], bool(ha.backstop_refusal(wt, "auto-author-w-s3"))), ("reslice-required", True))
    lg = load(HERE / "land_go.py", "land_go_inter")
    chk("L2 REAUTHOR has its own bounded daily cap", lg.REAUTHOR_CAP_PER_DAY >= 1, True)
    # re-scaffold at the same path clears admission state (SOURCE-TEXT check: the scaffold CLI is not unit-callable)
    chk("L1 scaffold clears admission state after a successful worktree add (source check)",
        "_ha_mod.clear_state(dest)" in (HERE / "ollama-dispatch-scaffold").read_text(), True)
    # --- repair acceptance x admit
    rv = load(HERE / "harness-repair-verify.py", "repair_verify_inter")
    rv.hr.survey = lambda wtp, reqs: {"clean": True}
    rv.hr.survey_clean = lambda sv: True
    import io, contextlib
    wt = mk("ts", tgt, base, refimpl=BAD_REF)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = rv.main([str(wt)])
    chk("L3 repair verify: relevance clean BUT harness still defective => NOT_OK (exit 1, admission cause named)",
        (rc, "HARNESS_REPAIR_NOT_OK" in buf.getvalue(), "refimpl-unapplicable" in buf.getvalue()), (1, True, True))
    wt = mk("ts", tgt, base, refimpl=GOOD_REF)
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = rv.main([str(wt)])
    chk("L3 repair verify: clean + admitted => HARNESS_REPAIR_OK", (rc, "HARNESS_REPAIR_OK" in buf.getvalue()), (0, True))
    import harness_admission as _ham          # the module harness-repair-verify imports
    real = _ham.admit
    _ham.admit = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            rc = rv.main([str(wt)])
        chk("L3 repair verify is FAIL-CLOSED when the gate itself errors", (rc, "could not run" in buf.getvalue()), (1, True))
    finally:
        _ham.admit = real


guard("interactions", interactions)


# ---- M. spec-traceability precision (backtest of 26 real slices: 3 wrongly-refused landed harnesses) -----------
def untraceable_precision():
    import shutil, json as _j
    FX = HERE / "test-fixtures-harness-admission" / "untraceable"
    for name, tok in (("replay-endorse-s1-normalize-tracking", "  ABC123  "),
                      ("rt-egift-link-s1-s1-parse-link", "memberedelivery.evil.io"),
                      ("rt-egift-link-s1-s4-api-route", "must not return a link field")):
        wt = TMP / ("unt-" + name)
        shutil.copytree(FX / name / "base", wt)
        git(TMP, "init", "-q", str(wt)); git(wt, "add", "-A")
        git(wt, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
        for f in ("TASK.md", "verify.test.ts", ".dispatch-harness.json"):
            shutil.copy2(FX / name / f, wt / f)
        tgt = _j.loads((wt / ".dispatch-harness.json").read_text())["target"]
        chk(f"M real landed harness {name}: fixture literal {tok!r} is present in the fixture", tok in (wt / "verify.test.ts").read_text(), True)
        chk(f"M real landed harness {name}: NOT refused as fixture-untraceable", ha.traceability_findings(wt, tgt), [])
    # controls on the same machinery: a truly invented EXPECTED value is still flagged
    wt = mk("ts", "lib/x.ts", "export function f(a){ return a + 1 }\n",
            fixture="import test from 'node:test'\ntest('f', () => { assert.strictEqual(f(1), 'Qxzeta-Vorpal') })\n",
            task="# t\nMake f return the next number.\n\n## Must contain\n- `return 2`\n\nOnly edit `lib/x.ts`.\n")
    chk("M control: an invented expected string (appears nowhere) IS flagged", [x["token"] for x in ha.traceability_findings(wt, "lib/x.ts")], ["Qxzeta-Vorpal"])
    wt = mk("ts", "lib/x.ts", "export function f(a){ return a + 1 }\n",
            fixture="import test from 'node:test'\ntest('f', () => { assert.ok(!r.error.includes('Qxzeta-Vorpal')) })\n",
            task="# t\nMake f return the next number.\n\n## Must contain\n- `return 2`\n\nOnly edit `lib/x.ts`.\n")
    chk("M the same string as an INPUT to a negative assertion (!x.includes) is not an expected value", ha.traceability_findings(wt, "lib/x.ts"), [])
    wt = mk("ts", "lib/x.ts", "export function f(a){ return a + 1 }\n",
            fixture="import test from 'node:test'\ntest('f', () => { assert.strictEqual(f('  Widget7  '), 'Qxzeta-Vorpal') })\n",
            task="# t\nf trims Widget7 (case/whitespace insensitive).\n\n## Must contain\n- `return 2`\n\nOnly edit `lib/x.ts`.\n")
    chk("M whitespace/case variant of a spec'd token is traced; the invented expected beside it is still flagged",
        [x["token"] for x in ha.traceability_findings(wt, "lib/x.ts")], ["Qxzeta-Vorpal"])
    wt = mk("ts", "lib/x.ts", "export function f(a){ return a + 1 }\n",
            fixture="import test from 'node:test'\ntest('f', () => { assert.strictEqual(f(1), 'http://www.zorbland.example/?q=1') })\n",
            task="# t\nf builds links on the zorbland host.\n\n## Must contain\n- `return 2`\n\nOnly edit `lib/x.ts`.\n")
    chk("M a URL whose host appears in the spec is traced (sub-token)", ha.traceability_findings(wt, "lib/x.ts"), [])

    # whitespace/case-folded form with NO >=4-char sub-token (so only the normalization can trace it)
    wt = mk("ts", "lib/x.ts", "export function f(a){ return a + 1 }\n",
            fixture="import test from 'node:test'\ntest('f', () => { assert.strictEqual(f(1), ' ab   cd ') })\n",
            task="# t\nf returns the label Ab Cd.\n\n## Must contain\n- `return 2`\n\nOnly edit `lib/x.ts`.\n")
    chk("M whitespace-collapsed + case-folded form alone is traced", ha.traceability_findings(wt, "lib/x.ts"), [])
    # assertion-message prose is not an expected value
    wt = mk("ts", "lib/x.ts", "export function f(a){ return a + 1 }\n",
            fixture="import test from 'node:test'\ntest('f', () => { assert.ok(f(1) === 2, 'zzqqxx must not be blank here') })\n",
            task="# t\nMake f return the next number.\n\n## Must contain\n- `return 2`\n\nOnly edit `lib/x.ts`.\n")
    chk("M assertion-message prose ('... must not ...') is not flagged", ha.traceability_findings(wt, "lib/x.ts"), [])
    # token-size estimate (aw-codec-floor-s2: 330KB target blew the 131072-token ceiling though bytes/file caps passed)
    wt = mk("ts", "lib/x.ts", "export const big = [" + ",".join(str(i) for i in range(4000)) + "]\n")
    old = ha.CTX_CEILING_TOKENS
    try:
        ha.CTX_CEILING_TOKENS = 1000
        fs = ha.size_findings(wt, "lib/x.ts")
        chk("M oversize: target file bytes count toward a token estimate over the ceiling", any("tokens" in f for f in fs), True)
        ha.CTX_CEILING_TOKENS = 10**9
        chk("M oversize: under the ceiling -> no token finding", any("tokens" in f for f in ha.size_findings(wt, "lib/x.ts")), False)
    finally:
        ha.CTX_CEILING_TOKENS = old


guard("untraceable_precision", untraceable_precision)

def setup_defined_literals():
    FXD = HERE / "test-fixtures-harness-admission" / "untraceable" / "setup-defined"
    FXD.mkdir(parents=True, exist_ok=True)
    fx = (FXD / "verify.test.ts")
    if not fx.exists():
        fx.write_text("import { POST } from './route'\n"
                      "const _orders = [{ id: 100, name: 'Widget' }]\n"
                      "const generateReceiptPdf = async () => Buffer.from('fake-pdf')\n"
                      "test('writes the pdf', async () => {\n"
                      "  assert.ok(written.some((p) => p.includes('/data/files/100/zzblob.bin')))\n"
                      "  assert.equal(bytes.toString(), 'fake-pdf')\n"
                      "  assert.equal(orderId, 100)\n"
                      "})\n")
    task = "# t\nPOST writes the receipt for the matched order.\n\n## Must contain\n- `return 2`\n\nOnly edit `lib/x.ts`.\n"
    wt = mk("ts", "lib/x.ts", "export function POST(a){ return a + 1 }\n", fixture=fx.read_text(), task=task)
    chk("P order id 100 and mock value 'fake-pdf' DEFINED in the fixture's own setup are traced (real Costco s3 FPs)",
        ha.traceability_findings(wt, "lib/x.ts"), [])
    wt = mk("ts", "lib/x.ts", "export function POST(a){ return a + 1 }\n",
            fixture="import test from 'node:test'\nconst _o = [{ id: 100 }]\ntest('f', () => { assert.strictEqual(f(1), 'Qxzeta-Vorpal'); assert.strictEqual(g(1), 7741) })\n", task=task)
    chk("P control: expected literals appearing ONLY on assertion lines stay flagged",
        sorted(x["token"] for x in ha.traceability_findings(wt, "lib/x.ts")), ["7741", "Qxzeta-Vorpal"])
    wt = mk("ts", "lib/x.ts", "export function POST(a){ return a + 1 }\n",
            fixture="import { zzNope } from './x'\nconst zzNope2 = 1\ntest('f', () => { assert.ok(zzNope(1)) })\n", task=task)
    chk("P an imported NAME is never traced to the fixture's own import line", [x["token"] for x in ha.traceability_findings(wt, "lib/x.ts")], ["zzNope"])


guard("setup_defined_literals", setup_defined_literals)


def oversize_target():
    big = "".join(f"x{i} = {i}\n" for i in range(3000))
    wt = mk("python", "lib/big.py", big)
    r = ha.admit(wt, "auto-author-pa-s1", target="lib/big.py", lang="python")
    chk("N s1 stage on a 3000-line target is REFUSED oversize-reslice (tier None used to admit anything)", (r["ok"], r["cause"]), (False, "oversize-reslice"))
    chk("N the refusal carries the ADMISSION marker and says to re-cut onto a new module + wiring slice",
        r["message"].startswith(ha.REFUSAL_PREFIX) and "NEW module" in r["message"] and "wiring slice" in r["message"], True)
    r = ha.admit(wt, "auto-author-pa-s1-r2", target="lib/big.py", lang="python")
    chk("N the s1 repair round (-s1-r2) is refused too", r["cause"], "oversize-reslice")
    r = ha.admit(wt, "auto-author-pa-s1", target="lib/big.py", lang="python", text="Edit only lines 1200-1260 of lib/big.py")
    chk("N an intent that names an anchor window (lines N-M) is admitted", r["ok"], True)
    r = ha.admit(wt, "auto-author-pa-s1", target="lib/big.py", lang="python", text="Add the check after the function `handle_import`")
    chk("N an intent anchored 'after the function `x`' is admitted", r["ok"], True)
    wt2 = mk("python", "lib/small.py", "x = 1\n")
    chk("N a small target is admitted at s1", ha.admit(wt2, "auto-author-pa-s1", target="lib/small.py", lang="python")["ok"], True)
    chk("N the check is authoring-only: a STRICT coding enqueue on a big target is not refused by line count",
        "lines /" not in " ".join(d["detail"] for d in ha.admit(wt, "coding-x", target="lib/big.py", lang="python", dynamic=False)["defects"]), True)
    # the token estimate is a coding-context estimate: dropped for non-strict tiers
    old = ha.CTX_CEILING_TOKENS
    try:
        ha.CTX_CEILING_TOKENS = 100
        d = ha.admit(wt, "auto-author-pa-s3", target="lib/big.py", lang="python", dynamic=False, text="lines 1-10")
        chk("N token estimate does not apply to an authoring tier", any("estimated task context" in x["detail"] for x in d["defects"]), False)
    finally:
        ha.CTX_CEILING_TOKENS = old


guard("oversize_target", oversize_target)

def exploration_exhausted():
    spec = importlib.util.spec_from_file_location("oq_for_test", str(HERE / "ollama-queue.py"))
    oq = importlib.util.module_from_spec(spec); spec.loader.exec_module(oq)
    todo = ("--- iteration 6/6 ---\n[worker] verify stdout:\n  FAIL: TASK.md still has TODO placeholders -- fill them in\n"
            "[worker] VERIFY FAILED (exit 1). Do not trust this output as-is.\n")
    rd = '[worker] tool read_file({"offset": 1, "path": "arr-webhook.py"}) -> [lines 1-100 of 7062]\n'
    chk("O 6/6 + TODO unfilled + only read_file calls -> exploration-exhausted (pa-supersede-on-import)",
        oq.classify_failure("nonconvergence", rd * 5 + todo, authoring=True)[0], "exploration-exhausted")
    chk("O ...with a write_file in the round it stays model", oq.classify_failure("nonconvergence", rd + '[worker] tool write_file({"path": "TASK.md"}) -> ok\n' + todo, authoring=True)[0], "model")
    chk("O ...grep-only run_bash counts as exploration", oq.classify_failure("nonconvergence", rd + '[worker] tool run_bash({"command": "grep -n def x.py"}) -> ok\n' + todo, authoring=True)[0], "exploration-exhausted")
    chk("O ...a redirecting run_bash is a write", oq.classify_failure("nonconvergence", rd + '[worker] tool run_bash({"command": "echo x > TASK.md"}) -> ok\n' + todo, authoring=True)[0], "model")
    chk("O exploration-exhausted is not an nsample (model-side) class", "exploration-exhausted" in oq.NSAMPLE_CLASSES, False)
    chk("O the auto driver passes the prompt to admit() so an anchor window in the intent lifts the refusal",
        "lang=getattr(a, \"lang\", None), text=prompt)" in (HERE / "ollama-dispatch-auto").read_text(), True)
    chk("O the auto driver's attempt class for it is its own (never iteration-cap)",
        "return \"exploration-exhausted\"" in (HERE / "ollama-dispatch-auto").read_text(), True)


guard("exploration_exhausted", exploration_exhausted)


# ---- J. flakiness + TS fixture type-check ----------------------------------------------------
def flake_and_types():
    tgt, base = "lib/x.ts", "export function f(a){ return a + 1 }\n"
    WRITE_REF = 'open("lib/x.ts", "w").write("export function f(a){ return 2 }\\n")\n'
    wt = mk("ts", tgt, base, refimpl=WRITE_REF)
    ok_sc = lambda cmd, cwd, t: (0, "VERIFY_OK: harness discriminates and is satisfiable")
    no_tc = lambda w, fx: {"ok": True, "errors": [], "mocks": 0, "notes": []}
    same = lambda w: (1.0, "ok 1 - a\nnot ok 2 - b\n", "ok 1 - a\nnot ok 2 - b\n")
    diff = lambda w: (1.0, "ok 1 - a\nok 2 - b\n", "ok 1 - a\nnot ok 2 - b\n")
    slow = lambda w: (500.0, "ok 1 - a\n", "ok 1 - a\n")
    kw = dict(selfcheck_runner=ok_sc, typecheck_runner=no_tc, preflight_runner=None)
    def run(fr, tc=no_tc):
        ha.state_path(wt).unlink(missing_ok=True)
        return ha.admit(wt, "rt-flake-s1", target=tgt, flake_runner=fr, typecheck_runner=tc, selfcheck_runner=ok_sc, preflight_runner=NOPRE, runner=lambda c, w, t: (1, "AssertionError x"))
    r = run(same)
    chk("flake: identical case sets => admitted", (r["ok"], r["defects"]), (True, []))
    r = run(diff)
    chk("flake: a case that flips between two runs => flaky-fixture (named, with the case)", (r["ok"], r["cause"], "'b'" in r["message"]), (False, "flaky-fixture", True))
    r = run(slow)
    chk("flake: slow first run is SKIPPED and recorded as a NOTE (not a pass, not a defect)", (r["ok"], any("SKIPPED" in n for n in r["notes"])), (True, True))
    r = run(lambda w: (_ for _ in ()).throw(RuntimeError("x")))
    chk("flake: runner exception is a defect (fail-closed)", (r["ok"], r["cause"]), (False, "flaky-fixture"))
    # contract failure => flake/type checks do not run
    ha.state_path(wt).unlink(missing_ok=True)
    called = []
    r = ha.admit(wt, "rt-flake-s1", target=tgt, flake_runner=lambda w: called.append(1) or (1, "", ""), typecheck_runner=no_tc,
                 selfcheck_runner=lambda c, w, t: (1, "  FAIL: x"), preflight_runner=lambda c, w: {"checks": [{"id": "refimpl-passes", "status": "FAIL", "message": "x"}]}, runner=lambda c, w, t: (1, "AssertionError x"))
    chk("flake/type checks only run once the contract held", (r["ok"], called), (False, []))
    # type-check results
    bad_tc = lambda w, fx: {"ok": False, "errors": [{"line": "verify.test.ts(98,5): error FIXTURE_MOCK_SHAPE: mock of pickReceiptOrder returns kind: \"ambiguous\""}], "notes": []}
    r = run(same, bad_tc)
    chk("typecheck: wrong-shaped mock => fixture-type-mismatch carrying the error line",
        (r["ok"], r["cause"], "error FIXTURE_MOCK_SHAPE" in r["message"]), (False, "fixture-type-mismatch", True))
    r = run(same, lambda w, fx: {"skipped": "typescript not installed in the worktree", "ok": True})
    chk("typecheck: tool missing => NOTE (never silent)", (r["ok"], any("typescript not installed" in n for n in r["notes"])), (True, True))
    r = run(same, lambda w, fx: (_ for _ in ()).throw(RuntimeError("tsc exploded")))
    chk("typecheck: runner crash => defect", (r["ok"], r["cause"]), (False, "fixture-type-mismatch"))
    wp = mk("py", "lib/x.py", "def f(a):\n    return a + 1\n", refimpl=GOOD_REF.replace("lib/x.ts", "lib/x.py"))
    n = []
    chk("typecheck: python fixture skipped cleanly with a note", (ha.typecheck_defects(wp, n), any("not TypeScript" in x for x in n)), ([], True))
    # scratch copy never writes the live worktree
    before = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout
    sc, err = ha.scratch_copy(wt)
    after = subprocess.run(["git", "-C", str(wt), "status", "--porcelain"], capture_output=True, text=True).stdout
    chk("scratch copy: live worktree untouched, refimpl applied only in the copy", (before == after, err, sc is not None and "return 2" in (sc / "lib/x.ts").read_text(), "return 2" in (wt / "lib/x.ts").read_text()), (True, "", True, False))
    ha.drop_scratch(sc)
    # real ha_tscheck.js on a mini TS project (skipped if no typescript is installed anywhere)
    import glob
    tsd = ([g for g in glob.glob(os.path.join(__import__("pwd").getpwuid(os.getuid()).pw_dir, ".ollama-dispatch/node_modules-cache/*/node_modules")) if Path(g, "typescript").exists() and Path(g, "@types/node").exists()] or [None])[0]
    if not tsd or not Path(tsd, "typescript").exists():
        print("ok   - (real tsc probe skipped: no typescript available)")
        return
    proj = TMP / "tsproj"
    (proj / "lib").mkdir(parents=True)
    os.symlink(tsd, proj / "node_modules")
    (proj / "tsconfig.json").write_text(json.dumps({"compilerOptions": {"strict": True, "module": "esnext", "moduleResolution": "bundler",
        "target": "ES2022", "skipLibCheck": True, "allowImportingTsExtensions": True, "paths": {"@/*": ["./*"]}}}))
    (proj / "lib/m.ts").write_text("export function pick(a: number): { kind: 'match'; id: number } | { kind: 'review'; reason: 'none' | 'ambiguous' } {\n  return a ? { kind: 'match', id: a } : { kind: 'review', reason: 'none' }\n}\n")
    head = "import { mock } from 'node:test'\n"
    good = head + "mock.module('@/lib/m', { exports: { pick: (a: number) => (a ? { kind: 'match' as const, id: a } : { kind: 'review' as const, reason: 'ambiguous' as const }) } })\n"
    bad = head + "mock.module('@/lib/m', { exports: { pick: (a: number) => ({ kind: 'ambiguous' as const, orderIds: [a] }) } })\n"
    missing = head + "mock.module('@/lib/m', { exports: { pickX: () => 1 } })\nimport { nope } from './lib/m.ts'\n"
    for nm, txt, want in (("good", good, True), ("bad", bad, False), ("missing", missing, False)):
        (proj / "fx.test.ts").write_text(txt)
        r = subprocess.run(["node", str(HERE / "ha_tscheck.js"), str(proj), "fx.test.ts"], capture_output=True, text=True)
        d = json.loads(r.stdout.strip().splitlines()[-1])
        chk(f"real tsc probe: {nm} mock shape => ok={want}", d["ok"], want)
        if nm == "bad":
            chk("real tsc probe: the error line names the mismatch", any('kind: "ambiguous"' in e["line"] and "FIXTURE_MOCK_SHAPE" in e["line"] for e in d["errors"]), True)


guard("flake_and_types", flake_and_types)


def anchored_size():
    """O: size_findings counts only resolvable anchor windows (+margin) for an anchored target (arr-webhook.py:
    331KB, every anchored slice was refused though the same slices completed at peak ~5-36k prompt tokens)."""
    big = "".join(f"def fn_{i}(a):\n    return a + {i}  # padding padding padding padding padding\n" for i in range(4000))
    tail = "\nOnly edit `lib/big.py`; do not edit `verify.sh`.\n"
    tok = lambda wt: [f for f in ha.size_findings(wt, "lib/big.py") if "estimated task context" in f]
    old = ha.CTX_CEILING_TOKENS
    try:
        ha.CTX_CEILING_TOKENS = 60000
        un = mk("python", "lib/big.py", big, task="# t\nMake fn_5 return 9.\n" + tail)
        chk("O unanchored big file still refused", len(tok(un)), 1)
        an = mk("python", "lib/big.py", big, task="# t\nANCHOR WINDOWS (read only these): lines 101-140 (`fn_50`).\n" + tail)
        chk("O anchored big file passes (windows + margin + harness under the ceiling)", tok(an), [])
        bog = mk("python", "lib/big.py", big, task="# t\nANCHOR WINDOWS: lines 90001-90040 (`fn_50`).\n" + tail)
        chk("O anchor range past EOF does not bypass", len(tok(bog)), 1)
        nos = mk("python", "lib/big.py", big, task="# t\nAdd the check near the function `no_such_fn`.\n" + tail)
        chk("O anchor naming a nonexistent symbol does not bypass", len(tok(nos)), 1)
        okk = mk("python", "lib/big.py", big, task="# t\nAdd the check near the function `fn_7`.\n" + tail)
        chk("O anchor naming an existing symbol passes", tok(okk), [])
        kw = mk("python", "lib/big.py", big, task="# t\nThis slice is anchored somewhere in the file.\n" + tail)
        chk("O the bare word 'anchored' with no resolvable window does not bypass", len(tok(kw)), 1)
        mix = mk("python", "lib/big.py", big, task="# t\nlines 101-140, and near the function `no_such_fn`.\n" + tail)
        chk("O one resolvable + one bogus anchor does not bypass", len(tok(mix)), 1)
        # boundary: ceiling == estimate passes, estimate-1 refuses
        total = sum((an / n).stat().st_size for n in ha.harness_files(an))
        blob = (an / "lib/big.py").read_text()
        lines = blob.splitlines(keepends=True)
        win = sum(len(l) for l in lines[100:140]) + ha.ANCHOR_MARGIN_BYTES
        est = int((total + win) / 3.5 * 1.4)
        ha.CTX_CEILING_TOKENS = est
        chk("O boundary: estimate == ceiling passes", tok(an), [])
        ha.CTX_CEILING_TOKENS = est - 1
        chk("O boundary: estimate == ceiling+1 refuses", len(tok(an)), 1)
    finally:
        ha.CTX_CEILING_TOKENS = old


guard("anchored_size", anchored_size)

def refine_admission():
    """P. REFINE round admission (2026-10-11, sidecar-novnc-tunnel-url): write_refine_guard arms .refine-guard.json
    BEFORE the refine enqueue, and the armed guard makes the self-check FAIL 'REFINE NOT DONE' while the harness
    is unchanged -- exactly at admission. That was measured as contract-broken and refused EVERY refine round
    (pa-supersede-policy x2, amazon, sidecar). Converged refimpl + outstanding survivors => ADMITTED; a really
    non-converged refimpl => still refused; a refine round that changes nothing, twice => refine-no-change."""
    tgt, base = "lib/x.ts", "export function f(a){ return a + 1 }\n"
    WRITE_REF = 'open("lib/x.ts", "w").write("export function f(a){ return 2 }\\n")\n'
    no_tc = lambda w, fx: {"ok": True, "errors": [], "mocks": 0, "notes": []}
    same = lambda w: (1.0, "ok 1 - a\n", "ok 1 - a\n")
    red = lambda c, w, t: (1, "AssertionError x")
    seen = []

    def self_check(converged=True):
        def run(cmd, cwd, t):
            g = Path(cwd) / ".refine-guard.json"
            seen.append(g.is_file())
            if g.is_file():                       # the REAL auto-harness-check behaviour: armed + unchanged => FAIL
                return 1, "  FAIL: REFINE NOT DONE: 7 surviving mutation(s) are still outstanding and nothing changed yet"
            return (0, "VERIFY_OK: harness discriminates") if converged else (1, "  FAIL: fixture still red after refimpl")
        return run

    def arm(wt):
        (wt / ".refine-guard.json").write_text(json.dumps({"files": [tgt, "refimpl.py", "TASK.md"], "hashes": {}, "survivors": 7}))
        return (wt / ".refine-guard.json").read_text()

    def adm(wt, label, converged=True):
        return ha.admit(wt, label, target=tgt, flake_runner=same, typecheck_runner=no_tc,
                        selfcheck_runner=self_check(converged), runner=red)

    def fresh():
        w = mk("ts", tgt, base, refimpl=WRITE_REF)
        (w / "auto-harness-check.py").write_text("# stand-in; the runner is injected\n")
        return w

    wt = fresh(); g0 = arm(wt); seen.clear()
    r = adm(wt, "auto-refine-p-r1")
    chk("P converged refimpl + outstanding survivors (guard armed) => refine ADMITTED", (r["ok"], r["defects"]), (True, []))
    chk("P ...the self-check ran WITHOUT the armed guard", seen, [False])
    chk("P ...and the guard is back, byte-identical, for the model's round", (wt / ".refine-guard.json").is_file() and (wt / ".refine-guard.json").read_text() == g0, True)
    chk("P ...no aside file left behind", (wt / ".refine-guard.json.admission-aside").exists(), False)

    wt = fresh(); g0 = arm(wt)
    r = adm(wt, "auto-refine-p-r1", converged=False)
    chk("P NON-converged refimpl + armed guard => still refused (the real guard stays)", (r["ok"], r["cause"]), (False, "contract-broken"))
    chk("P ...guard restored after a refused measurement too", (wt / ".refine-guard.json").read_text() == g0, True)

    wt = fresh(); arm(wt)
    outs = [adm(wt, f"auto-refine-p-r{i}") for i in (1, 2, 3)]
    chk("P refine that changed nothing: 1st and 2nd admission ok, 3rd (2 no-change rounds) refused refine-no-change",
        [(o["ok"], o["cause"]) for o in outs], [(True, None), (True, None), (False, "refine-no-change")])
    chk("P ...recorded as reslice: the next identical round is reslice-required", adm(wt, "auto-refine-p-r4")["cause"], "reslice-required")
    chk("P ...the repair path stays exempt", adm(wt, "auto-refine-p-s3-r101")["ok"], True)

    wt = fresh(); arm(wt)
    a1, a2 = adm(wt, "auto-refine-p-r1"), adm(wt, "auto-refine-p-r1")
    a3 = adm(wt, "auto-refine-p-r1")
    chk("P the SAME round re-enqueued (infra retry, same label) is not counted as a no-change round", (a1["ok"], a2["ok"], a3["ok"]), (True, True, True))

    wt = fresh(); arm(wt)
    adm(wt, "auto-refine-p-r1"); adm(wt, "auto-refine-p-r2")
    (wt / tgt).write_text(base + "// refine killed a survivor\n")
    r = adm(wt, "auto-refine-p-r3")
    chk("P a round that DID change the harness/target resets the no-change count", (r["ok"], r["cause"]), (True, None))
    r = adm(wt, "auto-refine-p-r4")
    chk("P ...and one no-change round after that is tolerated again", r["ok"], True)

    wt = fresh(); arm(wt)
    st = ha.load_state(wt)
    st.update(status="reslice", fp=ha.fingerprint(wt), history=[],
              detail="contract-broken: [contract-broken] fail-before/pass-after not proven by the self-check (FAIL: REFINE NOT DONE: # surviving mutation(s) are still outstanding and nothing changed ")
    ha.save_state(wt, st)
    r = adm(wt, "auto-refine-p-r1")
    chk("P a reslice record written by the pre-fix false positive (REFINE NOT DONE) is voided", (r["ok"], r["cause"]), (True, None))
    wt = fresh(); arm(wt)
    st = ha.load_state(wt)
    st.update(status="reslice", fp=ha.fingerprint(wt), history=[], detail="contract-broken: [contract-broken] FAIL: fixture still red")
    ha.save_state(wt, st)
    chk("P ...but a genuine contract-broken record is NOT voided", adm(wt, "auto-refine-p-r1")["cause"], "reslice-required")


guard("refine_admission", refine_admission)

print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
sys.exit(1 if FAILS else 0)
