#!/usr/bin/env python3
"""Invented constants/names must not trace to nothing (2026-10-14, resell 1a53991).

1a53991 landed a card-bonus module with INVENTED bronze/silver/gold tiers and 5%/10%/20% rates. The traceability
gate (harness_audit_gates.untraceable_assertions + harness_admission._allowlisted) existed and ran, but had two
leaks that turned the invented constants into 'traced':
  - TEST TITLES (`test('returns bronze tier ...')`) count as non-assertion SETUP lines, so a title that mentions an
    invented string 'defines' it (data flow) -> 'bronze'/'silver'/'gold' traced;
  - the tiny-integer allowlist rule matched bare DECIMALS (0.05, 0.1, 0.2: `.` is in its punctuation class) ->
    the rates were allowlisted.
Plus spec_conformance (TASK named surface vs intent) for the root cause (placeholder intent).

GATES_SRC / ADM_SRC / SC_SRC override the modules for --revert-check (a single mutated copy is placed first on
sys.path under its real module name)."""
import importlib, json, glob, os, subprocess, sys, tempfile, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILS = []
_over = {}
for env, name in (("GATES_SRC", "harness_audit_gates"), ("ADM_SRC", "harness_admission"), ("SC_SRC", "spec_conformance")):
    if os.environ.get(env):
        d = _over.setdefault("dir", tempfile.mkdtemp(prefix="trace-mut-"))
        shutil.copy(os.environ[env], Path(d) / f"{name}.py")
if _over:
    sys.path.insert(0, _over["dir"])
sys.path.insert(1 if _over else 0, str(HERE))
import harness_audit_gates as hag          # noqa: E402
import harness_admission as ha             # noqa: E402
import spec_conformance as sc              # noqa: E402


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


# the recorded fixture + TASK + base of the run that landed 1a53991 (rt-card-bonus-lib-rq)
FIXTURE = """import { test } from 'node:test';
import assert from 'node:assert/strict';
import { calculateCardBonus } from './lib/cardBonuses.ts';

test('returns none tier for cardValue below 10', () => {
  const result = calculateCardBonus(5);
  assert.strictEqual(result.bonusTier, 'none');
  assert.strictEqual(result.bonusAmount, 0);
});

test('returns bronze tier for cardValue 10-49', () => {
  const result = calculateCardBonus(25);
  assert.strictEqual(result.bonusTier, 'bronze');
  assert.strictEqual(result.bonusAmount, 25 * 0.05);
});

test('returns silver tier for cardValue 50-99', () => {
  const result = calculateCardBonus(75);
  assert.strictEqual(result.bonusTier, 'silver');
  assert.strictEqual(result.bonusAmount, 75 * 0.1);
});

test('returns gold tier for cardValue 100+', () => {
  const result = calculateCardBonus(150);
  assert.strictEqual(result.bonusTier, 'gold');
  assert.strictEqual(result.bonusAmount, 150 * 0.2);
});
"""
TASK = """# TASK: rt-card-bonus-lib-rq

## Required change

Implement a `calculateCardBonus` function that computes gift card bonus amounts based on card value and bonus tier rules. The function takes a card value (number) and returns a bonus object with tier and amount.

## Must contain

- `calculateCardBonus`
- `cardValue`
- `bonusTier`
- `bonusAmount`
- `CardBonusResult`
"""
BASE = "// Stub for lib/cardBonuses.ts -- implement per TASK.md.\nexport {};\n"


def surviving(fx, task, base=BASE):
    fs = hag.untraceable_assertions(fx, task, base, "lib/cardBonuses.ts", "ts")
    lines = fx.splitlines()
    return sorted(f["token"] for f in fs if not ha._allowlisted(f, lines))


def main():
    got = surviving(FIXTURE, TASK)
    chk("1a53991 replay: the invented tier names are flagged", [t for t in got if t in ("bronze", "silver", "gold")], ["bronze", "gold", "silver"])
    chk("1a53991 replay: the invented rates are flagged (decimals are not 'tiny integers')", [t for t in got if t[0].isdigit()], ["0.05", "0.1", "0.2"])
    os.environ["DISPATCH_TRACE_TITLES"] = "off"
    chk("DISPATCH_TRACE_TITLES=off restores the title leak (tier names pass)", [t for t in surviving(FIXTURE, TASK) if t.isalpha()], [])
    del os.environ["DISPATCH_TRACE_TITLES"]
    os.environ["DISPATCH_TRACE_DECIMALS"] = "off"
    chk("DISPATCH_TRACE_DECIMALS=off restores the decimal allowlist", [t for t in surviving(FIXTURE, TASK) if t[0].isdigit()], [])
    del os.environ["DISPATCH_TRACE_DECIMALS"]
    # ---- precision: traced values and legit titles are untouched
    spec = TASK + "\nTiers: bronze 5% (0.05), silver 10% (0.1), gold 20% (0.2); none below 10.\n"
    chk("the same fixture is clean when the TASK states the tiers and rates", surviving(FIXTURE, spec), [])
    fx2 = ("import { test } from 'node:test';\nimport assert from 'node:assert/strict';\n"
           "const RATE = 0.05;\n"
           "test('applies the bronze rate', () => {\n  assert.strictEqual(calc(2), 2 * RATE);\n});\n")
    chk("a value DEFINED on a setup line (const RATE = 0.05) is still data flow, not invented", surviving(fx2, "Implement calc."), [])
    fx3 = ("test('keeps whole dollars', () => {\n  assert.strictEqual(calc(2), 4);\n  assert.strictEqual(calc(10), 20);\n});\n")
    chk("tiny integers stay allowlisted", surviving(fx3, "Implement calc."), [])
    fx4 = ("test('handles a half', () => {\n  assert.strictEqual(calc(1), 0.5);\n});\n")
    chk("an invented decimal expected value alone IS flagged", surviving(fx4, "Implement calc."), ["0.5"])
    chk("...unless the TASK states it", surviving(fx4, "Implement calc. calc(1) returns 0.5."), [])
    py = ("def test_returns_gold_tier():\n    assert calc(150)['tier'] == 'gold'\n")
    got_py = [f["token"] for f in hag.untraceable_assertions(py, "Implement calc.", "def calc(x): pass\n", "m.py", "python")]
    chk("python: a test_ name no longer 'defines' the asserted string", "gold" in got_py, True)
    # ---- spec_conformance
    chk("task_named_surface reads the Must-contain identifiers",
        sc.task_named_surface(TASK), ["calculateCardBonus", "cardValue", "bonusTier", "bonusAmount", "CardBonusResult"])
    thin = "<paste the full intent text here, without the surrounding quotes>"
    v = sc.verdict(thin, "", TASK, BASE, "", "ts")
    chk("placeholder intent + TASK that invents its surface -> block", v["status"], "block")
    real = ("lib/cardBonuses.ts hard-codes INVENTED bronze/silver/gold tiers. Card bonuses are USER-CONFIGURED: export a pure "
            "function calculateCardBonus(rule, spend) returning a CardBonusResult with bonusTier and bonusAmount for the cardValue")
    chk("...a real intent naming that surface -> ok/warn, never block", sc.verdict(real, "", TASK, BASE, "", "ts")["status"] != "block", True)
    long_intent = "word " * 60
    chk("a LONG intent never blocks (only thin ones do)", sc.verdict(long_intent, "", TASK, BASE, "", "ts")["status"], "warn")
    chk("invented_constants: 0.05/0.1/0.2/100 in added code but in no reference text",
        sc.invented_constants("calculateCardBonus rule", BASE, "if (v < 100) return v * 0.05;\nreturn v * 0.1;\n"), ["100", "0.05", "0.1"])
    chk("invented_constants: a constant the intent states is traced",
        sc.invented_constants("earn 0.05 per dollar over 100", BASE, "return v * 0.05; // 100"), [])
    chk("code_public_surface finds exports, interface fields and returned keys",
        sorted(sc.code_public_surface("export interface R {\n  bonusTier: string;\n}\nexport function f() {\n  return { bonusTier: 'x', bonusAmount: 1 };\n}\n", "ts")),
        ["R", "bonusAmount", "bonusTier", "f"])
    os.environ["SPEC_CONFORMANCE"] = "off"
    chk("SPEC_CONFORMANCE=off -> ok", sc.verdict(thin, "", TASK, BASE, "", "ts")["status"], "ok")
    del os.environ["SPEC_CONFORMANCE"]
    # ---- replay over the recorded runs: only the placeholder run blocks
    blocks, n = [], 0
    for f in sorted(glob.glob(str(Path.home() / ".ollama-dispatch/auto-runs/argv/*.json"))):
        try:
            d = json.load(open(f))
            a = d.get("argv") or []
            tp = Path(d.get("worktree") or "/nonexistent") / "TASK.md"
            if "--intent" not in a or not tp.is_file():
                continue
        except Exception:
            continue
        n += 1
        r = sc.verdict(a[a.index("--intent") + 1], a[a.index("--interface") + 1] if "--interface" in a else "",
                       tp.read_text(errors="replace"), "", "", "ts")
        if r["status"] == "block":
            blocks.append(os.path.basename(f))
    chk(f"replay over {n} recorded runs: only the placeholder-intent run blocks", [b for b in blocks if b != "rt-card-bonus-lib-rq.json"], [])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("harness_audit_gates.py", "GATES_SRC", "test titles count as setup data again",
     "and not (_titles and _TITLE_LINE_RE.match(l)))", ")"),
    ("harness_admission.py", "ADM_SRC", "decimals allowlisted again",
     'and re.fullmatch(r"-?\\d*\\.\\d+", tok)):\n        return False', 'and False):\n        return False'),
    ("spec_conformance.py", "SC_SRC", "thin-intent total-invention no longer blocks",
     'if thin and len(named) >= 3 and len(inv) == len(named):', 'if False:'),
    ("spec_conformance.py", "SC_SRC", "a long intent can block",
     "    thin = len((intent or \"\").split()) < 40", "    thin = True"),
    ("spec_conformance.py", "SC_SRC", "invented constants not reported",
     "if n not in refnums and n not in out", "if False and n not in out"),
]


def revert_check():
    bad = 0
    for fname, env, name, old, new in MUTANTS:
        src = (HERE / fname).read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-" + fname, delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, env: f.name}, capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
