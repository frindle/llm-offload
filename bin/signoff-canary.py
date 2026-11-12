#!/usr/bin/env python3
"""Regression canary for signoff.py -- run after ANY change to signoff.py or
signoff-rules.json.  INSTALL TO ~/bin/signoff-canary.py.

Two properties, both broken and shipped before this file existed:

A. auto_decide's launch-baseline condition is THREE-VALUED.
   gate-on-complete.apply_baseline() writes three distinguishable states, and
   auto_decide read only `untrusted`, collapsing "measured clean" and "never
   measured" into one answer. It reported the success reason "clean launch
   baseline" for a job whose own gate record said, in not_checked, that a dirty
   starting tree could not be ruled out. Found on the FIRST genuine auto-approve
   candidate (Diplomat 08c288f0), 2026-09-02.

B. check_requires_signoff honours all three matchers.
   `basename_globs` was declared in signoff-rules.json and printed by
   --policy-check as active while nothing consulted it -- `guided-piv*` never
   triggered a sign-off. `path_globs` is what lets risk-bearing product code be
   named at all, since the highest-risk handlers are called route.ts.

The negatives matter as much as the positives: a rule set that fired on
everything would pass every positive here. Crying wolf is the failure mode this
gate exists to avoid.
"""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

BIN = Path(os.environ.get("SIGNOFF_BIN",
                          Path(__file__).resolve().parent)).expanduser()
spec = importlib.util.spec_from_file_location("signoff", BIN / "signoff.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

fails = 0
checks = 0

# --------------------------------------------------------------------- fixture
# auto_decide now needs RELEVANCE evidence from the preflight ledger, keyed by
# the gate record's job_cwd, and re-hashes the scaffold in that tree. So the
# clean gate below points at a fixture worktree with a ledger entry saying the
# verify was measured relevant. Every arm that is not ABOUT relevance inherits
# this, so the only variable across those arms stays the one they name.
FIX = Path(tempfile.mkdtemp(prefix="signoff-canary-"))
WT = FIX / "wt"
WT.mkdir()
(WT / "verify.sh").write_text("#!/bin/bash\necho VERIFY_OK\n")
(WT / "TASK.md").write_text("# task\n")
(WT / "test_fixture.py").write_text("print('ok')\n")
LEDGER = FIX / "ledger"
LEDGER.mkdir()
os.environ["OLLAMA_PREFLIGHT_LEDGER"] = str(LEDGER)
os.environ["SIGNOFF_DIR"] = str(FIX / "state")
import hashlib


def _sha(p):
    return hashlib.sha256(Path(p).read_bytes()).hexdigest()


def write_ledger(wt=WT, verdict="relevant", score=1.0, survivors=(), digests=None,
                 present=True):
    key = hashlib.sha256(str(Path(wt).resolve()).encode()).hexdigest()[:16]
    f = LEDGER / f"{key}.json"
    if not present:
        if f.exists():
            f.unlink()
        return
    entry = {
        "worktree": str(wt), "verdict": "GO", "baseline_commit": "abc",
        "scaffold_sha256": digests if digests is not None else {
            n: _sha(Path(wt) / n) for n in ("verify.sh", "TASK.md", "test_fixture.py")},
        "verify_failed_at_baseline": True,
        "verify_relevance": None if verdict is None else {
            "verdict": verdict, "score": score, "reason": "canary",
            "survivors": [{"file": "t.py", "line": 1, "mutation": "x"}
                          for _ in survivors],
        },
    }
    f.write_text(json.dumps(entry))


write_ledger()

# --------------------------------------------------------------------------- A
# A gate record satisfying every OTHER auto-approve condition, so the only
# variable across arms is the launch-baseline evidence.
CLEAN_GATE = {
    "verdict": "pass",
    "issues": [],
    "counts": {"code_high": 0, "code": 0, "input": 0, "total": 0},
    "review_verdict": "PASS",
    "verify_exit_reported": 0,
    "verify_quality_exit": 0,
    "verify_failed_at_baseline": True,
    "job_cwd": str(WT),
}

TINY_DIFF = ("diff --git a/app.py b/app.py\n"
             "--- a/app.py\n+++ b/app.py\n@@ -1,2 +1,2 @@\n-old\n+new\n")


def _decide(mutate):
    g = json.loads(json.dumps(CLEAN_GATE))
    mutate(g)
    gp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(g, gp); gp.close()
    dp = tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False)
    dp.write(TINY_DIFF); dp.close()
    try:
        return m.auto_decide("canary", gp.name, dp.name)
    finally:
        os.unlink(gp.name); os.unlink(dp.name)


print("A. launch-baseline is three-valued")
BASELINE_ARMS = [
    ("measured CLEAN (dirty=0)",
     lambda g: g.update({"launch_baseline": {"head": "abc", "dirty": 0}}),
     "auto-approve"),
    ("measured DIRTY (untrusted set)",
     lambda g: g.update({"launch_baseline": {"head": "abc", "dirty": 3},
                         "untrusted": ["launch baseline was dirty (3 path(s))"]}),
     "human"),
    ("NEVER MEASURED (no stamp at all)",
     lambda g: None,
     "human"),
]
for label, mut, expect in BASELINE_ARMS:
    d, reasons = _decide(mut)
    ok = (d == expect)
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<34} -> {d} (expected {expect})")
    if not ok:
        for r in reasons:
            print(f"        {r[:110]}")

# The unmeasured arm must say WHY, not merely block: an un-run check must be
# distinguishable from a failed one.
_, reasons = _decide(lambda g: None)
ok = any("no launch_baseline" in r for r in reasons)
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} unmeasured arm names the missing evidence")

# --------------------------------------------------------------------------- B
print("\nB. check_requires_signoff honours basenames + basename_globs + path_globs")
CASES = [
    ("bin/gate.py", True),
    ("some/scratch/wt/ollama-worker.py", True),
    ("clamshell/guided-piv-enroll.sh", True),          # basename_globs (was inert)
    ("plex-wt-dedupe/arr-webhook.py", True),
    ("resell-tracker/prisma/schema.prisma", True),
    ("resell-tracker/lib/paymentStatus.ts", True),
    ("resell-tracker/lib/orderLock.ts", True),
    ("resell-tracker/app/api/buyinggroup/order-payouts/route.ts", True),  # path_globs
    ("ev-dashboard/app/api/rivian/auth/route.ts", True),
    ("ev-dashboard/app/auth/callback/route.ts", True),
    ("resell-tracker/prisma/migrations/20260101_x/migration.sql", True),
    # negatives -- ordinary product code stays silent
    ("ev-dashboard/app/api/vehicles/route.ts", False),
    ("ev-dashboard/components/VehicleCard.tsx", False),
    ("resell-tracker/lib/formatOrderDate.ts", False),
    ("resell-tracker/components/OrderForm.tsx", False),
    ("ev-dashboard/lib/chargeHistory.ts", False),
    ("README.md", False),
]
for path, expect in CASES:
    req, _, reason = m.check_requires_signoff("", [path])
    ok = (req == expect)
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} req={str(req):<5} exp={str(expect):<5} {path}")
    if not ok:
        print(f"        reason: {reason}")

req, _, _ = m.check_requires_signoff("## Requires sign-off: owner — risky\n",
                                     ["README.md"])
ok = req is True
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} task-file '## Requires sign-off:' still fires")

# --------------------------------------------------------------------------- B2
# `counts` ABSENT is not "zero code-high findings". The old code did
# `counts = gate.get("counts") or {}` then `if counts.get("code_high")`, so a
# record with no counts block read as measured-and-clean. I had assumed
# co-occurrence covered it (across 88 real records counts and issues are always
# both present or both absent, and the adjacent issues-is-None check blocks that
# case) -- but co-occurrence is a proxy, and it only holds for BOTH. With
# issues=[] present and counts absent, auto_decide returned auto-approve claiming
# "no code findings" while code_high was never tallied.
print("\nB2. an ABSENT counts block is unmeasured, not zero")
COUNT_ARMS = [
    ("counts ABSENT", lambda g: g.pop("counts", None), "human"),
    ("counts present, all zero",
     lambda g: g.update({"counts": {"code_high": 0, "code": 0, "input": 0, "total": 0}}),
     "auto-approve"),
    ("counts present, 1 code-high",
     lambda g: g.update({"counts": {"code_high": 1, "code": 1, "input": 0, "total": 1}}),
     "human"),
]
for label, mut, expect in COUNT_ARMS:
    def _m(g, _mut=mut):
        g.update({"launch_baseline": {"head": "abc", "dirty": 0}})
        _mut(g)
    d, reasons = _decide(_m)
    ok = (d == expect)
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<30} -> {d} (expected {expect})")

# --------------------------------------------------------------------------- C
print("\nC. auto-approve has a non-empty domain (the widening is not inert)")
rc = subprocess.run([sys.executable, str(BIN / "signoff.py"), "--policy-check"],
                    capture_output=True, text=True)
ok = rc.returncode == 0 and "CAN auto-approve: NONE" not in rc.stdout
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} --policy-check exits 0 with a real domain")

# --------------------------------------------------------------------------- D
# An absence of scope findings is evidence only if scope-check actually RAN.
# gate.py sets `scope = {}` whenever the subprocess crashed or emitted non-JSON
# (its rc was discarded), so every scope finding vanished silently and read
# downstream as "scope clean" -- while `scope_clean` is a declared auto_approve
# requirement. gate.py now records a not_checked entry; auto_decide blocks on it.
# The third arm is the one that keeps this honest: an UNRELATED not_checked entry
# must NOT block, or auto-approve is gutted (26 of 88 real gate records carry a
# completeness not_checked entry).
print("\nD. an un-run scope-check does not read as 'scope clean'")
SCOPE_ARMS = [
    ("scope ran, nothing unchecked", [], "auto-approve"),
    ("scope-check DID NOT RUN",
     ["scope-check (exit 1; emitted no JSON, so files the task never named could "
      "not be ruled out -- this is NOT 'scope clean')"], "human"),
    ("unrelated not_checked must NOT block",
     ["completeness (task declares no `## Must contain` section)"], "auto-approve"),
]
for label, nc, expect in SCOPE_ARMS:
    d, reasons = _decide(lambda g, _nc=nc: g.update(
        {"launch_baseline": {"head": "abc", "dirty": 0}, "not_checked": _nc}))
    ok = (d == expect)
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<38} -> {d} (expected {expect})")
    if not ok:
        for r in reasons:
            print(f"        {r[:110]}")

# --------------------------------------------------------------------------- E
# RELEVANCE IS THREE-VALUED: relevant / low / never measured. A verify that
# only DISCRIMINATES (both-ways) can certify a grep for a literal; the ledger's
# mutation score is what says it tests the property. Absent evidence blocks
# and SAYS it was never measured -- the same MISSING discipline as every other
# condition, applied to the one condition that was the ceiling on autonomy.
print("\nE. verify relevance is three-valued and read from the ledger")
CLEANLB = lambda g: g.update({"launch_baseline": {"head": "abc", "dirty": 0}})
ARM_GATE = {}      # extra gate-record keys an arm wants (gate-applied relevance)


def _rel_arm(label, setup, expect, needle=None):
    global fails, checks
    setup()
    d, reasons = _decide(lambda g: (CLEANLB(g), g.update(ARM_GATE)))
    ok = (d == expect) and (needle is None or any(needle in r for r in reasons))
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<44} -> {d} (expected {expect})")
    if not ok:
        for r in reasons:
            print(f"        {r[:110]}")
    write_ledger()   # restore the clean fixture for the next arm
    ARM_GATE.clear()


_rel_arm("ledger says RELEVANT (score 1.0)", lambda: write_ledger(), "auto-approve")
_rel_arm("ledger says LOW (score 0.4)",
         lambda: write_ledger(verdict="low", score=0.4, survivors=(1, 2)),
         "human", needle="relevance is 'low'")
_rel_arm("ledger ABSENT (never measured)",
         lambda: write_ledger(present=False), "human", needle="never measured")
_rel_arm("ledger present, relevance WAIVED (null)",
         lambda: write_ledger(verdict=None), "human", needle="unproven")
_rel_arm("relevant but under the score floor (0.7)",
         lambda: write_ledger(score=0.7), "human", needle="under the auto-approve floor")
_rel_arm("relevant but too many survivors (5 > 3)",
         lambda: write_ledger(survivors=(1, 2, 3, 4, 5)), "human", needle="survived")
_rel_arm("scaffold digest DRIFT (verify.sh edited after the gate)",
         lambda: write_ledger(digests={"verify.sh": "0" * 64}), "human",
         needle="changed since the gate measured")
# job_cwd missing from the gate record entirely
d, reasons = _decide(lambda g: (CLEANLB(g), g.pop("job_cwd")))
ok = d == "human" and any("no job_cwd" in r for r in reasons)
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} {'gate record has no job_cwd':<44} -> {d} (expected human)")
# worktree gone
d, reasons = _decide(lambda g: (CLEANLB(g), g.update({"job_cwd": str(FIX / "gone")})))
ok = d == "human"
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} {'worktree path does not exist':<44} -> {d} (expected human)")

# GATE-APPLIED relevance: gate-on-complete measures the verify against the
# MODEL'S diff and writes gate["verify_relevance"]. It is evidence on its own
# (a job with no preflight ledger can still prove relevance at the gate), and
# a LOW from either source blocks even when the other says relevant.
def _gate_vr(verdict, score=1.0, survivors=0):
    return {"verdict": verdict, "score": score, "source": "gate-applied",
            "reason": "canary",
            "survivors": [{"file": "t.py", "line": 1, "mutation": "x"}] * survivors}


_rel_arm("no ledger, gate-applied RELEVANT (1.0)",
         lambda: (write_ledger(present=False),
                  ARM_GATE.update({"verify_relevance": _gate_vr("relevant")})),
         "auto-approve", needle="gate-applied score")
_rel_arm("ledger RELEVANT, gate-applied LOW (0.5)",
         lambda: ARM_GATE.update({"verify_relevance": _gate_vr("low", 0.5, 4)}),
         "human", needle="gate-applied: verify relevance is 'low'")
_rel_arm("ledger LOW, gate-applied RELEVANT",
         lambda: (write_ledger(verdict="low", score=0.4, survivors=(1, 2)),
                  ARM_GATE.update({"verify_relevance": _gate_vr("relevant")})),
         "human", needle="preflight: verify relevance is 'low'")
_rel_arm("no ledger, gate-applied UNPROVEN",
         lambda: (write_ledger(present=False),
                  ARM_GATE.update({"verify_relevance": _gate_vr("unproven", None)})),
         "human", needle="never measured")
_rel_arm("no ledger, gate-applied relevant, 5 survivors",
         lambda: (write_ledger(present=False),
                  ARM_GATE.update({"verify_relevance": _gate_vr("relevant", 0.9, 5)})),
         "human", needle="survived")

# --------------------------------------------------------------------------- F
# CHANGE CLASS. Perfect evidence only licenses a bounded single-site change.
print("\nF. only the bounded single-site class can auto-approve")


def _diff(files, added=("+new",), hunks=1, new=False):
    out = []
    for f in files:
        out.append(f"diff --git a/{f} b/{f}\n--- {'/dev/null' if new else 'a/' + f}\n+++ b/{f}\n")
        for h in range(hunks):
            out.append(f"@@ -{h+1},1 +{h+1},1 @@\n-old\n" + "\n".join(added) + "\n")
    return "".join(out)


def _class_arm(label, diff_text, expect, needle=None):
    global fails, checks
    g = json.loads(json.dumps(CLEAN_GATE)); CLEANLB(g)
    gp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
    json.dump(g, gp); gp.close()
    dp = tempfile.NamedTemporaryFile("w", suffix=".diff", delete=False)
    dp.write(diff_text); dp.close()
    try:
        d, reasons = m.auto_decide("canary", gp.name, dp.name)
    finally:
        os.unlink(gp.name); os.unlink(dp.name)
    ok = (d == expect) and (needle is None or any(needle in r for r in reasons))
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label:<44} -> {d} (expected {expect})")
    if not ok:
        for r in reasons:
            print(f"        {r[:110]}")


_class_arm("one file, one hunk, one line", _diff(["app.py"]), "auto-approve")
_class_arm("two files", _diff(["app.py", "lib.py"]), "human", "files changed")
_class_arm("new file created", _diff(["new.py"], new=True), "human", "new file")
_class_arm("too many hunks (4 > 3)", _diff(["app.py"], hunks=4), "human", "hunks")
_class_arm("too many lines (100 > 80)",
           _diff(["app.py"], added=tuple(f"+l{i}" for i in range(100))), "human",
           "changed lines")
_class_arm("security-adjacent PATH (auth route)",
           _diff(["app/api/auth/route.ts"]), "human", "security-adjacent")
_class_arm("security-adjacent ADDED line (password)",
           _diff(["app.py"], added=("+pw = os.environ['PASSWORD']",)), "human",
           "security-adjacent")
_class_arm("security-adjacent REMOVED line (subprocess)",
           _diff(["app.py"], added=("+x = 1",)).replace("-old", "-subprocess.run(cmd)"),
           "human", "security-adjacent")
_class_arm("migration path", _diff(["prisma/migrations/1/migration.sql"]), "human",
           "security-adjacent")
# no --diff at all: unclassified is not in the domain
g = json.loads(json.dumps(CLEAN_GATE)); CLEANLB(g)
gp = tempfile.NamedTemporaryFile("w", suffix=".json", delete=False)
json.dump(g, gp); gp.close()
d, reasons = m.auto_decide("canary", gp.name, None)
os.unlink(gp.name)
ok = d == "human" and any("no --diff" in r for r in reasons)
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} {'no --diff: unclassified':<44} -> {d} (expected human)")

# --------------------------------------------------------------------------- G
# SHADOW MODE: decide, record, never act. And the mode key ABSENT is shadow.
print("\nG. shadow mode records the decision beside the human's and never acts")
import copy
base_rules = json.loads((BIN / "signoff-rules.json").read_text())


def _with_mode(mode):
    r = copy.deepcopy(base_rules)
    if mode is None:
        r["auto_approve"].pop("mode", None)
    else:
        r["auto_approve"]["mode"] = mode
    f = FIX / f"rules-{mode}.json"
    f.write_text(json.dumps(r))
    return f


def _shadow_arm(mode, expect_verdict, expect_shadow):
    global fails, checks
    os.environ["SIGNOFF_RULES"] = str(_with_mode(mode))
    write_ledger()
    sd = FIX / f"state-{mode}"
    os.environ["SIGNOFF_DIR"] = str(sd)
    m.save_signoffs({"j1": {"required": True, "reviewer": "owner", "reason": "x",
                            "verdict": None, "conditions": []}})
    g = json.loads(json.dumps(CLEAN_GATE)); CLEANLB(g)
    gp = FIX / f"gate-{mode}.json"; gp.write_text(json.dumps(g))
    dp = FIX / f"diff-{mode}.diff"; dp.write_text(TINY_DIFF)
    import io, contextlib
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        m.auto_signoff("j1", str(gp), str(dp))
    rec = m.load_signoffs()["j1"]
    ok = (rec.get("verdict") == expect_verdict
          and rec.get("shadow_decision") == expect_shadow)
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} mode={str(mode):<7} verdict={rec.get('verdict')!r:<16} "
          f"shadow_decision={rec.get('shadow_decision')!r}")
    os.environ.pop("SIGNOFF_RULES", None)
    os.environ["SIGNOFF_DIR"] = str(FIX / "state")
    return rec


_shadow_arm("shadow", None, "auto-approve")
_shadow_arm(None, None, "auto-approve")          # ABSENT means shadow
rec = _shadow_arm("live", "auto-approve", None)
ok = rec.get("reviewer") == "harness"
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} live mode stamps reviewer=harness")

# --------------------------------------------------------------------------- H
# AGREEMENT METRIC. The dangerous quadrant is the only number that gates going
# live; zero compared pairs must not read as calibrated.
print("\nH. --agreement counts the four quadrants and the pending backlog")
os.environ["SIGNOFF_DIR"] = str(FIX / "state-agree")
m.save_signoffs({
    "a": {"shadow_decision": "auto-approve", "verdict": "approve"},
    "b": {"shadow_decision": "human", "verdict": "changes"},
    "c": {"shadow_decision": "auto-approve", "verdict": "reject"},   # DANGEROUS
    "d": {"shadow_decision": "human", "verdict": "approve"},         # conservative
    "e": {"shadow_decision": "auto-approve", "verdict": None},       # pending
    "f": {"verdict": "approve"},                                      # no shadow: ignored
})
import io, contextlib
buf = io.StringIO()
with contextlib.redirect_stdout(buf):
    m.agreement(as_json=True)
out = json.loads(buf.getvalue())
exp = {"compared": 4, "agree": 2, "dangerous_false_approve": 1,
       "conservative_hold": 1, "pending_human_verdict": 1}
for k, v in exp.items():
    ok = out.get(k) == v
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {k:<26} = {out.get(k)} (expected {v})")
ok = out.get("dangerous_jobs") == ["c"]
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} dangerous job named: {out.get('dangerous_jobs')}")
m.save_signoffs({})
with contextlib.redirect_stdout(buf):
    m.agreement(as_json=True)
out = json.loads(buf.getvalue().splitlines()[-1] if False else buf.getvalue()[buf.getvalue().rfind("{"):])
ok = out.get("agreement_rate") is None and out.get("compared") == 0
fails += (not ok); checks += 1
print(f"  {'ok  ' if ok else 'FAIL'} zero pairs -> agreement_rate None, not 100%")
os.environ["SIGNOFF_DIR"] = str(FIX / "state")

# --------------------------------------------------------------------------- I
# EVERY DECLARED POLICY KEY HAS A CONSUMER. A key nothing reads is worse than
# absent: --policy-check renders it as coverage. (feedback: declared rule with
# no consumer.)
print("\nI. every auto_approve policy key is consumed by signoff.py")
src = (BIN / "signoff.py").read_text()
for key in sorted(base_rules.get("auto_approve", {}).keys()):
    if key.startswith("_") or key == "requires":
        continue
    ok = f'"{key}"' in src
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} auto_approve.{key} is read by signoff.py")
for key in ("min_score", "max_survivors", "max_hunks", "no_new_files"):
    ok = f'"{key}"' in src
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} nested key {key} is read by signoff.py")

# --------------------------------------------------------------------------- J
# THE GATE -> SIGNOFF PATH, END TO END. Every arm above calls auto_decide()
# directly; none ever ran gate-on-complete.py, which is how the readback that
# raised `signoff_error: "load: NameError: name '_load' is not defined"` on
# every real dispatch (26c4359e581a and the rest) survived a green canary. This
# drives the real hook in GATE_TEST_MODE (no queue, no notify, no handoff)
# against a fixture repo whose queue row, ledger and rules are the canary's
# own, then simulates the review landing (the queue's merge call) -- the ONE
# path that can end in auto-approve -- and a review FAIL on the same path.
print("\nJ. gate-on-complete -> signoff.py records a real decision (end to end)")
J = FIX / "j"; J.mkdir()
JBIN = J / "bin"; JBIN.mkdir()          # GATE_BIN: the queue state lives here, nothing else
JOUT = J / "out"; JOUT.mkdir()
REPO = J / "repo"
_spec_t = importlib.util.spec_from_file_location("tvr", BIN / "test-verify-relevance.py")
tvr = importlib.util.module_from_spec(_spec_t); _spec_t.loader.exec_module(tvr)
# the task must NAME the file or scope-check abstains (and an un-run scope blocks)
tvr.build(REPO, task=tvr.TASK + "\n## Scope\nEdit `target.py` only.\n")
subprocess.run(["git", "apply", "fix.patch"], cwd=REPO, check=True)   # the model's work
(REPO / "fix.patch").unlink()
write_ledger(wt=REPO, digests={n: _sha(REPO / n)
                               for n in ("TASK.md", "verify.sh", "test_fixture.py")})
_rules = json.loads((BIN / "signoff-rules.json").read_text())
_rules["basenames"].append("target.py")
_rules["auto_approve"]["mode"] = "shadow"
JRULES = J / "rules.json"; JRULES.write_text(json.dumps(_rules))


def _gate(job_id, cwd=None, *extra):
    (JBIN / "ollama-queue-state.json").write_text(json.dumps({"jobs": [{
        "id": job_id, "status": "done", "exit_code": 0, "label": "canary-j",
        "model": "canary", "cwd": str(REPO), "verify": "bash verify.sh",
        "launch_baseline": {"head": "abc", "dirty": 0},
        "verify_failed_at_baseline": True}]}))
    env = dict(os.environ, GATE_TEST_MODE="1", GATE_BIN=str(JBIN),
               SIGNOFF_DIR=str(J / "state"), SIGNOFF_RULES=str(JRULES),
               OLLAMA_PREFLIGHT_LEDGER=str(LEDGER), GATE_RELEVANCE_MAX_MUTANTS="12")
    p = subprocess.run([sys.executable, str(BIN / "gate-on-complete.py"),
                        "--job-id", job_id, "--cwd", str(cwd or REPO),
                        "--task-file", str(REPO / "TASK.md"),
                        "--verify", "bash verify.sh", "--out-dir", str(JOUT), *extra],
                       capture_output=True, text=True, timeout=900, env=env)
    rp = JOUT / f"{job_id}.gate.json"
    return (json.loads(rp.read_text()) if rp.exists() else {}), p


def _j(ok, label, extra=""):
    global fails, checks
    fails += (not ok); checks += 1
    print(f"  {'ok  ' if ok else 'FAIL'} {label}" + (f"  [{extra[:300]}]" if extra and not ok else ""))


g, p = _gate("jshadow")
_j(bool(g), "gate record written", (p.stdout + p.stderr)[-400:])
_j("signoff_error" not in g, "no signoff_error in the record", str(g.get("signoff_error")))
_j(g.get("signoff_required") is True,
   f"signoff_required is True (target.py is a sign-off basename), got {g.get('signoff_required')!r}")
_j(g.get("signoff_shadow_decision") in ("auto-approve", "human"),
   f"--auto ran: shadow_decision={g.get('signoff_shadow_decision')!r}")
_j(g.get("signoff_verdict") is None, "shadow mode leaves verdict None")
_vr = g.get("verify_relevance") or {}
_j(_vr.get("verdict") == "relevant" and _vr.get("source") == "gate-applied",
   f"gate-applied relevance measured on the model's diff: {_vr.get('verdict')} score={_vr.get('score')}",
   json.dumps(_vr)[:300])
_j((REPO / "target.py").read_text() == tvr.TARGET_FIXED,
   "the model's tree is byte-identical after the relevance measurement")
_j(sorted(g.get("scaffold_excluded") or []) == ["TASK.md", "test_fixture.py", "verify.sh"],
   f"scaffold artifacts excluded from the diff (test_fixture.py by digest; "
   f"TASK.md + verify.sh by name since the 0b6ab88 scaffold-leak fix): "
   f"{g.get('scaffold_excluded')}")
_j(g.get("verdict") == "pass-pending-review",
   f"decidable verdict is pass-pending-review at emit time, got {g.get('verdict')!r}",
   json.dumps(g.get("issues"))[:300] + " " + json.dumps(g.get("not_checked"))[:300])

# LIVE mode, then the review lands: the ONE path that can end in auto-approve.
_rules["auto_approve"]["mode"] = "live"; JRULES.write_text(json.dumps(_rules))
g, p = _gate("jlive")
_j(g.get("signoff_verdict") is None
   and any("pending-review" in r for r in (g.get("signoff_auto_blocked_by") or [])),
   "live mode HOLDS while the review is pending (auto_blocked_by names it)",
   json.dumps(g.get("signoff_auto_blocked_by")))
_rd = Path(g.get("review_dir") or (JOUT / "jlive-review"))
(_rd / "report.md").write_text("# review\n\n## VERDICT: PASS\n\nno findings\n")
_gate("jlive", _rd, "--job-label", "gate-jlive")      # the merge call the queue makes
g2 = json.loads((JOUT / "jlive.gate.json").read_text())
_j(g2.get("review") == "done" and g2.get("verdict") == "pass",
   f"review merged: review={g2.get('review')} verdict={g2.get('verdict')}")
_j(g2.get("signoff_redecided_after_review") is True, "sign-off re-decided after the review landed")
_j(g2.get("signoff_verdict") == "auto-approve" and g2.get("signoff_reviewer") == "harness",
   f"AUTO-APPROVE on complete evidence: verdict={g2.get('signoff_verdict')!r} "
   f"reviewer={g2.get('signoff_reviewer')!r}",
   "; ".join(g2.get("signoff_auto_blocked_by") or []))
# the same path with a review FAIL must never approve
g, p = _gate("jfail")
_rd = Path(g.get("review_dir") or (JOUT / "jfail-review"))
(_rd / "report.md").write_text(
    "# review\n\n## VERDICT: FAIL\n\n| 1 | high | `target.py:7` | returns True for count=5 |\n")
_gate("jfail", _rd, "--job-label", "gate-jfail")
g3 = json.loads((JOUT / "jfail.gate.json").read_text())
_j(g3.get("verdict") == "fail" and g3.get("signoff_verdict") != "auto-approve",
   f"review FAIL -> verdict={g3.get('verdict')}, sign-off {g3.get('signoff_verdict')!r} (never auto-approve)")
os.environ["SIGNOFF_DIR"] = str(FIX / "state")

# A TEST FILE THAT LOSES A SECTION DOES NOT FAIL -- IT STOPS ASKING.
# 0648899 reverted this canary to an earlier capture and dropped all of section D
# (the scope-check-not-run arms) while the gate.py guard they cover stayed in the
# tree. It still printed PASS, because nothing compared the number of assertions
# to anything: "green" only ever meant "the arms that still exist pass", and D's
# absence read as coverage. That is this batch's own failure mode -- an un-run
# check reporting pass -- turned on the checker itself.
# Raise EXPECTED_CHECKS deliberately when you add arms; never lower it to go green.
EXPECTED_CHECKS = 89
print()
if checks < EXPECTED_CHECKS:
    print(f"SIGNOFF CANARY TRUNCATED: ran {checks} assertions, expected at least "
          f"{EXPECTED_CHECKS}. Arms have been REMOVED from this file -- the "
          f"remaining ones passing is not a result. Restore them (git log "
          f"bin/signoff-canary.py) rather than lowering EXPECTED_CHECKS.")
    sys.exit(1)
if fails:
    print(f"SIGNOFF CANARY FAILED ({fails} of {checks} case(s))")
    sys.exit(1)
print(f"SIGNOFF CANARY PASSED ({checks} checks): baseline discriminates 3 "
      f"ways, matcher discriminates, un-run scope blocks, domain non-empty, "
      f"relevance three-valued, change class bounded, shadow never acts, "
      f"agreement counts, every policy key consumed")
