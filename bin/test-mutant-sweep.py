#!/usr/bin/env python3
"""mutant_sweep + verify-relevance.apply_sweep_equivalence (2026-10-14, resell cardBonuses-v2).

Live: the survivor-feedback loop gave the author 11 surviving mutants as bare mutated lines; 5 of them were
EQUIVALENT (no input distinguishes them) and 6 were real holes with a one-line distinguishing call. The model
could not tell which in prose (four 32k-token cut-offs, zero edits). mutant_sweep runs the refimpl-applied
module and each mutant over a deterministic input sweep: equivalent -> excluded + reported; differs -> a
concrete WITNESS call; anything uncertain (impure, helper-only line, timeout, off) -> unknown, untouched.

Ground truth is a replica of the refimpl the v2 run authored (fixtures-cardbonus/cardBonuses.refimpl-replica.ts)
and its 11 recorded survivors. SWEEP_SRC / VR_SRC override the modules for --revert-check."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
SWEEP = Path(os.environ.get("SWEEP_SRC") or HERE / "mutant_sweep.py")
VR = Path(os.environ.get("VR_SRC") or HERE / "verify-relevance.py")
REPLICA = HERE / "fixtures-cardbonus" / "cardBonuses.refimpl-replica.ts"
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def load_sweep():
    sp = importlib.util.spec_from_file_location("mutant_sweep", str(SWEEP))
    m = importlib.util.module_from_spec(sp)
    sys.modules["mutant_sweep"] = m
    sp.loader.exec_module(m)
    return m


def load_vr():
    ld = SourceFileLoader("vr_sweep_t", str(VR))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("vr_sweep_t", ld))
    ld.exec_module(m)
    return m


# the 11 survivors recorded for cardBonuses-v2 (preflight record), (line, original, mutated)
SURV = [(16, "spend < 0", "spend <= 0", True), (27, "rule.points < 0 ||", "rule.points <= 0 ||", False),
        (13, "typeof spend !== 'number' ||", "typeof spend !== 'number' &&", True),
        (23, "typeof rule !== 'object' ||", "typeof rule !== 'object' &&", True),
        (25, "typeof rule.points !== 'number' ||", "typeof rule.points !== 'number' &&", False),
        (28, "!isFinite(rule.points) ||", "!isFinite(rule.points) &&", False),
        (29, "typeof rule.perDollars !== 'number' ||", "typeof rule.perDollars !== 'number' &&", False),
        (32, "!isFinite(rule.perDollars)", "!rule.perDollars", True),
        (49, "if (maxTimes < 0) {", "if (maxTimes <= 0) {", True),
        (50, "return { times: 0, points: 0 };", "return { times: 1, points: 0 };", False),
        (28, "!isFinite(rule.points) ||", "!rule.points ||", False)]


def mut_src(src, line, old, new):
    ls = src.split("\n")
    assert old in ls[line - 1], (line, old, ls[line - 1])
    ls[line - 1] = ls[line - 1].replace(old, new, 1)
    return "\n".join(ls)


PY = """def clamp_points(rule, spend):
    zero = {"times": 0, "points": 0}
    try:
        if spend < 0:
            return zero
        p = rule["points"]
        if p != p or p == float("inf") or p < 0:
            return zero
        d = rule["perDollars"]
        if d != d or d <= 0:
            return zero
        t = max(0, min(int(spend // d), rule.get("maxTimes", 99)))
        return {"times": t, "points": t * p}
    except Exception:
        return zero
"""


def main():
    ms = load_sweep()
    src = REPLICA.read_text()
    # ---- 1. the recorded survivors split exactly into equivalent / real holes
    got = []
    for line, old, new, want_eq in SURV:
        r = ms.sweep_mutant("lib/cardBonuses.ts", src, mut_src(src, line, old, new), line)
        got.append((line, new, r["status"]))
    chk("11 recorded survivors: 5 sweep-equivalent, 6 real holes with a witness",
        sorted(s for _, _, s in got).count("equivalent"), 5)
    chk("...and every non-equivalent one is 'differs' (never unknown)",
        sorted(set(s for _, _, s in got if s != "equivalent")), ["differs"])
    chk("...each survivor lands on the expected side",
        [(s == "equivalent") for _, _, s in got], [w for _, _, _, w in SURV])
    # ---- 2. the witness is a concrete, correct distinguishing call
    bsrc = "export function isAdult(age) {\n  return age >= 18;\n}\n"
    chk("boundary mutant (>= 18 -> > 18) is NOT called equivalent (literal-harvested inputs hit 18)",
        ms.sweep_mutant("lib/a.mjs", bsrc, bsrc.replace(">= 18", "> 18"), 2)["status"], "differs")
    chk("harvest_literals adds the literal and its neighbours", all(x in ms.harvest_literals("x >= 18")[0] for x in (17, 18, 19)), True)
    r = ms.sweep_mutant("lib/cardBonuses.ts", src, mut_src(src, 27, "rule.points < 0 ||", "rule.points <= 0 ||"), 27)
    w = r.get("witness") or {}
    chk("witness names the call with points: 0", "points: 0" in (w.get("call") or ""), True)
    chk("witness: real result keeps times, mutant zeroes them",
        (w.get("expected", "").startswith("{times: 5") or "times: 0" not in w.get("expected", "")), True)
    chk("witness: mutant result differs from the real one", w.get("expected") != w.get("got"), True)
    chk("render_witness is a prompt line quoting the call", "THIS call" in ms.render_witness(w), True)
    # ---- 3. fail-closed: anything uncertain is 'unknown'
    impure = src.replace("export function", "const _t = Date.now();\nexport function", 1)
    chk("impure module (clock) -> unknown",
        ms.sweep_mutant("a.ts", impure, mut_src(impure, 17, "spend < 0", "spend <= 0"), 17)["status"], "unknown")
    helper = "function helper(x: number) {\n  return x < 0 ? 0 : x;\n}\nexport function f(x: number) {\n  return helper(x) + 1;\n}\n"
    chk("mutated line inside a non-exported helper -> unknown",
        ms.sweep_mutant("a.ts", helper, helper.replace("x < 0", "x <= 0"), 2)["status"], "unknown")
    chk("syntax-broken mutant -> unknown (not 'equivalent')",
        ms.sweep_mutant("a.ts", src, src.replace("spend < 0", "spend < <"), 16)["status"], "unknown")
    chk("unsupported language -> unknown", ms.sweep_mutant("a.rb", "x", "y", 1)["status"], "unknown")
    chk("identical text -> equivalent", ms.sweep_mutant("a.ts", src, src, 16)["status"], "equivalent")
    os.environ["MUTANT_SWEEP"] = "off"
    chk("MUTANT_SWEEP=off -> unknown",
        ms.sweep_mutant("lib/cardBonuses.ts", src, mut_src(src, 16, "spend < 0", "spend <= 0"), 16)["status"], "unknown")
    del os.environ["MUTANT_SWEEP"]
    # ---- 4. python
    chk("python: `< 0` -> `<= 0` where 0 gives the same result -> equivalent",
        ms.sweep_mutant("m.py", PY, PY.replace("spend < 0", "spend <= 0"), 4)["status"], "equivalent")
    r = ms.sweep_mutant("m.py", PY, PY.replace("p < 0", "p <= 0"), 7)
    chk("python: `points < 0` -> `<= 0` is a real hole with a witness",
        (r["status"], bool(r.get("witness"))), ("differs", True))
    chk("python: impure import -> unknown",
        ms.sweep_mutant("m.py", "import os\n" + PY, "import os\n" + PY.replace("spend < 0", "spend <= 0"), 5)["status"], "unknown")
    # ---- 5. verify-relevance wiring
    vr = load_vr()
    wt = Path(tempfile.mkdtemp(prefix="sweep-wt-"))
    (wt / "lib").mkdir()
    (wt / "lib" / "cardBonuses.ts").write_text(src)
    by_id, mutants, survivors = {}, [], []
    for i, (line, old, new, _) in enumerate(SURV):
        sid = f"s{i:02d}"
        by_id[sid] = SimpleNamespace(klass="compare-flip", source=mut_src(src, line, old, new))
        survivors.append({"id": sid, "file": "lib/cardBonuses.ts", "line": line, "class": "compare-flip",
                          "mutation": f"{old} -> {new}", "killed": False, "original": old, "snippet": new})
        mutants.append({"id": sid, "file": "lib/cardBonuses.ts", "line": line, "killed": False})
    for i in range(29):                     # 29 killed mutants spread over the same lines (every site exercised)
        mutants.append({"id": f"k{i:02d}", "file": "lib/cardBonuses.ts", "line": SURV[i % 11][0], "killed": True})

    def rec(extra_bank=False):
        sv = [dict(s) for s in survivors]
        if extra_bank:
            sv[1]["gate"] = "wrong-solution-bank"
        return {"verdict": "low", "score": 0.725, "threshold": 0.8, "min_mutants": 3, "evidence_mutants": 40,
                "killed": 29, "survived": 11, "mutant_score": 0.725, "site_score": 0.7, "site_coverage": 1.0,
                "survivors": sv, "mutants": [dict(m) for m in mutants], "untried_sites": [],
                "unexercised_sites": [], "necessity": {"unnecessary": [], "untested": []},
                "wrong_bank": {"variants": [], "survivors": []}, "reason": "x"}
    r1 = rec()
    vr.apply_sweep_equivalence(wt, r1, by_id)
    chk("sweep: 5 equivalent excluded, 6 real holes remain",
        (len(r1["survivors"]), len(r1["equivalent_survivors"]), r1["survived"], r1["evidence_mutants"]), (6, 5, 6, 35))
    chk("...every remaining survivor carries a witness", all(s.get("witness") for s in r1["survivors"]), True)
    chk("...equivalents are reported with their evidence", all(e.get("sweep", {}).get("calls") for e in r1["equivalent_survivors"]), True)
    chk("...score recomputed over the new denominator", r1["mutant_score"], round(29 / 35, 3))
    chk("...score >= threshold and nothing else open -> verdict relevant", r1["verdict"], "relevant")
    r2 = rec(extra_bank=True)
    vr.apply_sweep_equivalence(wt, r2, by_id)
    chk("an open wrong-solution-bank survivor keeps the verdict LOW (fail-closed)", r2["verdict"], "low")
    os.environ["VR_SWEEP_EQUIV_EXCLUDE"] = "off"
    r3 = rec()
    vr.apply_sweep_equivalence(wt, r3, by_id)
    chk("VR_SWEEP_EQUIV_EXCLUDE=off: equivalents still counted, witnesses still given",
        (len(r3["survivors"]), r3["verdict"], sum(1 for s in r3["survivors"] if s.get("sweep_equivalent")),
         all(s.get("witness") for s in r3["survivors"] if not s.get("sweep_equivalent"))), (11, "low", 5, True))
    del os.environ["VR_SWEEP_EQUIV_EXCLUDE"]
    os.environ["MUTANT_SWEEP"] = "off"
    r4 = rec()
    vr.apply_sweep_equivalence(wt, r4, by_id)
    chk("MUTANT_SWEEP=off: the record is untouched", (len(r4["survivors"]), r4["verdict"], "sweep" in r4), (11, "low", False))
    del os.environ["MUTANT_SWEEP"]
    r5 = rec(); r5["survivors"] = []
    vr.apply_sweep_equivalence(wt, r5, by_id)
    chk("no survivors (a converged run) -> nothing changes", (r5["verdict"], "sweep" in r5), ("low", False))
    r6 = rec()
    vr.apply_sweep_equivalence(wt, r6, {})              # no mutant objects: nothing classifiable
    chk("survivors with no mutant source are left alone", (len(r6["survivors"]), "equivalent_survivors" in r6), (11, False))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("mutant_sweep.py", "equivalent sweep verdict not returned",
     'return {"status": "equivalent", "calls": calls,\n            "reason"', 'return {"status": "differs", "calls": calls, "witness": {},\n            "reason"'),
    ("mutant_sweep.py", "a differing result is ignored",
     "    if (ra !== rb) {", "    if (false) {"),
    ("mutant_sweep.py", "impure modules are swept anyway",
     'if not ok:\n        return {"status": "unknown", "reason": f"not sweepable: {why}"}\n    ok, why = (py_eligible(mut_src) if is_py else js_eligible(mut_src))\n    if not ok:\n        return {"status": "unknown", "reason": f"mutant not sweepable: {why}"}',
     'if False:\n        return {"status": "unknown"}'),
    ("mutant_sweep.py", "helper-only line classified",
     'if enc is None:\n        return {"status": "unknown"', 'enc = enc or fns[0]\n    if False:\n        return {"status": "unknown"'),
    ("mutant_sweep.py", "boundary literals not harvested",
     "        nums, strs = harvest_literals(orig_src + \"\\n\" + mut_src)", "        nums, strs = [], []"),
    ("verify-relevance.py", "equivalents never excluded",
     "            if _sweep_exclude_enabled():\n                excl.add(sv[\"id\"])", "            pass"),
    ("verify-relevance.py", "an open bank survivor no longer blocks the verdict flip",
     "and not open_bank and not rec.get(\"untried_sites\")", "and not rec.get(\"untried_sites\")"),
    ("verify-relevance.py", "witness not attached",
     '            sv["witness"] = r.get("witness")', "            pass"),
]


def revert_check():
    bad = 0
    for fname, name, old, new in MUTANTS:
        path = HERE / fname
        src = path.read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-" + fname, delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        env = {**os.environ, ("SWEEP_SRC" if fname == "mutant_sweep.py" else "VR_SRC"): f.name}
        r = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
