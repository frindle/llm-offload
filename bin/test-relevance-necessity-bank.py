#!/usr/bin/env python3
"""Behavioural tests for verify-relevance's 2026-10-09 post-gates:

  1. PER-HUNK NECESSITY (hard gate): every material hunk of the reference solution
     must be necessary -- reverting it alone must turn the verify red. An
     unnecessary hunk -> LOW, naming `file:start-end`. Explicit, tested exemptions:
     doc-only files, comment/type-only (non-material) hunks, the single-hunk file
     (a creation task's whole file / a one-hunk fix), a revert that drops a
     `## Must contain` literal, a revert that is AST/comment-equivalent, a hunk made
     only of honoured `relevance: unobservable` lines.
  2. WRONG-SOLUTION BANK (hard gate): a small deterministic set of plausible-wrong
     variants (boundary, swapped-compare, dropped-guard, wrong-default, early-return)
     that the fixture MUST reject; a survivor -> LOW naming the category.
  3. Deterministic equivalent-mutant filter (identical / AST / comment / dead code).
  4. Opt-in LLM hooks stay OFF by default.
  5. Fail closed: a bank/necessity run the budget did not finish makes RELEVANT unproven.

Usage: test-relevance-necessity-bank.py [--target PATH-TO-verify-relevance.py]
       test-relevance-necessity-bank.py --revert-check   (each gate removed => suite RED)
"""
import importlib.machinery
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = Path(os.environ.get("VR_TARGET", HERE / "verify-relevance.py"))
if "--target" in sys.argv:
    TARGET = Path(sys.argv[sys.argv.index("--target") + 1])
FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -- {detail}"))
    if not cond:
        FAILS.append(name)


if not os.environ.get("TS_MUTATOR") and (HERE / "ts-mutator" / "ts-mutator.mjs").is_file():
    os.environ["TS_MUTATOR"] = str(HERE / "ts-mutator" / "ts-mutator.mjs")   # --target copies live elsewhere


def load():
    spec = importlib.util.spec_from_file_location(
        "vr_nb", str(TARGET), loader=importlib.machinery.SourceFileLoader("vr_nb", str(TARGET)))
    m = importlib.util.module_from_spec(spec)
    sys.modules["vr_nb"] = m
    spec.loader.exec_module(m)
    return m


def sh(wt, *a):
    return subprocess.run(a, cwd=str(wt), capture_output=True, text=True)


BASE_MOD = textwrap.dedent('''\
    def score(n):
        return n


    def label(n):
        return "x"
    ''')
FIXED_MOD = textwrap.dedent('''\
    def score(n):
        if n < 0:
            raise ValueError("neg")
        return n


    def label(n):
        print("labelling", n)
        return "x"
    ''')
FIXTURE_FULL = textwrap.dedent('''\
    import importlib.util, sys
    spec = importlib.util.spec_from_file_location("m", "mod.py")
    m = importlib.util.module_from_spec(spec); spec.loader.exec_module(m)
    bad = []
    def raises(n):
        try:
            m.score(n)
        except ValueError:
            return True
        return False
    if m.score(5) != 5: bad.append("5")
    if m.score(0) != 0: bad.append("0")
    if not raises(-1): bad.append("-1")
    if m.label(1) != "x": bad.append("label")
    sys.exit(1 if bad else 0)
    ''')
FIXTURE_NO_BOUNDARY = FIXTURE_FULL.replace('if m.score(0) != 0: bad.append("0")\n', "")
VERIFY = '#!/bin/bash\ncd "$(dirname "$0")"\npython3 fixture.py && echo VERIFY_OK || exit 1\n'


def mkrepo(td, base, fixed, fixture, task="# TASK\n\n## Must contain (literal tokens)\n- `def score(n):`\n"):
    wt = Path(td)
    sh(wt, "git", "init", "-q", "-b", "main")
    sh(wt, "git", "config", "user.email", "t@t")
    sh(wt, "git", "config", "user.name", "t")
    for k, v in base.items():
        (wt / k).write_text(v)
    (wt / "fixture.py").write_text(fixture)
    (wt / "v.sh").write_text(VERIFY)
    (wt / "TASK.md").write_text(task)
    sh(wt, "git", "add", "-A")
    sh(wt, "git", "commit", "-qm", "base")
    for k, v in fixed.items():
        (wt / k).write_text(v)
    return wt


def run_cli(wt, *extra, env=None):
    e = dict(os.environ)
    e.update(env or {})
    p = subprocess.run([sys.executable, str(TARGET), str(wt), "--applied", "--verify", "bash v.sh",
                        "--min-mutants", "1", "--json", *extra],
                       capture_output=True, text=True, env=e)
    try:
        return p.returncode, json.loads(p.stdout)
    except Exception:
        return p.returncode, {"verdict": "PARSE-ERROR", "_raw": p.stdout + p.stderr}


def main():
    vr = load()

    # ---------------- 3. deterministic equivalence (pure) ----------------
    M = vr.Mutant
    fx = "def f(n):\n    if False:\n        n = n + 1\n    return n\n"
    dead = vr.python_unreachable_lines(fx)
    check("dead: body of `if False` is unreachable", dead == {3}, sorted(dead))
    m_dead = M("a.py", "binop-swap", "+ -> -", fx.replace("n + 1", "n - 1"), 3)
    check("equivalent: mutation inside a dead branch",
          "dead code" in (vr.equivalent_reason(m_dead, fx, dead) or ""))
    ret = "def g(n):\n    return n\n    n = n + 1\n"
    check("dead: statement after return", 3 in vr.python_unreachable_lines(ret))
    m_same = M("a.py", "x", "x", fx.replace("n + 1", "n  +  1  # c"), 3)
    check("equivalent: comment/whitespace-only (AST-identical)",
          "AST-identical" in (vr.equivalent_reason(m_same, fx, set()) or ""))
    m_ident = M("a.py", "x", "x", fx, 3)
    check("equivalent: identical text", vr.equivalent_reason(m_ident, fx) == "identical text")
    ts = "export const a = 1; // note\nexport const b = 2;\n"
    m_ts = M("a.ts", "x", "x", "export const a = 1; // another note\nexport const b = 2;\n", 1)
    check("equivalent: TS comment-only change",
          "comment" in (vr.equivalent_reason(m_ts, ts) or ""))
    m_ts2 = M("a.ts", "x", "x", "export const a = 1; // note\nexport const b = 3;\n", 2)
    check("NOT equivalent: TS real change", vr.equivalent_reason(m_ts2, ts) is None)
    ts3 = 'export const u = "http://a"; \n'
    m_ts3 = M("a.ts", "x", "x", 'export const u = "http://b"; \n', 1)
    check("NOT equivalent: a `//` inside a string is not a comment",
          vr.equivalent_reason(m_ts3, ts3) is None)
    m_real = M("a.py", "compare-flip", "x", fx.replace("False", "True"), 2)
    check("NOT equivalent: a real change", vr.equivalent_reason(m_real, fx, dead) is None)

    # generate() drops the dead mutant and RECORDS it
    with tempfile.TemporaryDirectory() as td:
        wt = mkrepo(td, {"mod.py": "def f(n):\n    return n\n"}, {"mod.py": fx}, FIXTURE_FULL)
        diff = sh(wt, "git", "diff", "-U0").stdout
        ms, notes = vr.generate(wt, diff, [])
        check("generate: nothing mutated on the dead line", not [x for x in ms if x.lineno == 3 and x.klass != "hunk-revert"],
              [(x.klass, x.lineno) for x in ms])
        eq = notes.get("equivalent_filtered") or []
        check("generate: the drop is recorded with its reason",
              any(e["line"] == 3 and "dead code" in e["reason"] for e in eq), eq)

    # ---------------- bank_category / extras (pure) ----------------
    def mm(klass, desc, orig=""):
        m = M("a.py", klass, desc, "s", 1)
        m.original = orig
        return m
    check("category: >= -> > is boundary", vr.bank_category(mm("compare-flip", ">= -> >")) == "boundary")
    check("category: < -> <= is boundary", vr.bank_category(mm("compare-flip", "< -> <=")) == "boundary")
    check("category: == -> != is swapped-compare", vr.bank_category(mm("compare-flip", "== -> !=")) == "swapped-compare")
    check("category: < -> >= is swapped-compare", vr.bank_category(mm("compare-flip", "< -> >=")) == "swapped-compare")
    check("category: const-default is wrong-default", vr.bank_category(mm("const-default", "x")) == "wrong-default")
    check("category: guard delete is dropped-guard",
          vr.bank_category(mm("stmt-delete", "d", "if not xs:")) == "dropped-guard")
    check("category: non-guard if delete is NOT dropped-guard",
          vr.bank_category(mm("stmt-delete", "d", "if mode == 'fast':")) is None)
    check("category: const-int outside a comparison is not boundary",
          vr.bank_category(mm("const-int", "1 -> 2", "TIMEOUT = 1")) is None)
    ex_src = textwrap.dedent('''\
        def pick(xs, limit=10, strict=False):
            """doc"""
            a = len(xs)
            return xs[:limit] if a else []
        ''')
    ex = vr.wrong_solution_extras("mod.py", ex_src, {1, 2, 3, 4})
    kinds = sorted(m.klass for m in ex)
    check("extras: early-return + 2 wrong defaults", kinds == ["const-default", "const-default", "early-return"], kinds)
    er = next(m for m in ex if m.klass == "early-return")
    check("extras: early return sits AFTER the docstring, before the first statement",
          er.source.split("\n")[2].strip() == "return None" and er.source.split("\n")[1].strip() == '"""doc"""',
          er.source)
    check("extras: not generated for functions the refimpl did not add",
          vr.wrong_solution_extras("mod.py", ex_src, {99}) == [])
    gen_src = "def g(xs):\n    a = 1\n    yield a\n"
    check("extras: no early return into a generator",
          not [m for m in vr.wrong_solution_extras("m.py", gen_src, {1}) if m.klass == "early-return"])

    # ---------------- 1. necessity (end to end) ----------------
    with tempfile.TemporaryDirectory() as td:
        wt = mkrepo(td, {"mod.py": BASE_MOD}, {"mod.py": FIXED_MOD}, FIXTURE_FULL)
        rc, rec = run_cli(wt)
        nec = rec.get("necessity") or {}
        check("necessity: an UNTESTED hunk (print) -> LOW", rec.get("verdict") == "low" and rc == 1,
              (rc, rec.get("verdict"), rec.get("reason")))
        un = nec.get("unnecessary") or []
        check("necessity: names the hunk file:start-end", len(un) == 1 and un[0]["file"] == "mod.py"
              and "UNNECESSARY HUNK mod.py:8-8" in un[0]["message"], un)
        check("necessity: the reason leads with the hunk and says what to do",
              (rec.get("reason") or "").startswith("UNNECESSARY HUNK") and "DELETE the hunk" in rec["reason"],
              rec.get("reason"))
        check("necessity: the guard hunk was tested and IS necessary", nec.get("necessary") == 1, nec)
        sv = rec.get("survivors") or []
        check("necessity: hunk is first among the survivors the refine loop gets",
              sv and sv[0].get("unnecessary_hunk") and sv[0]["line"] == 8, sv[:1])
        # a --max-mutants cap used to hide the hunk-revert from the sample entirely
        rc2, rec2 = run_cli(wt, "--max-mutants", "2")
        check("necessity: still found when the mutant cap hides the revert",
              rec2.get("verdict") == "low" and (rec2.get("necessity") or {}).get("unnecessary"),
              (rec2.get("verdict"), rec2.get("reason")))
        rc3, rec3 = run_cli(wt, "--no-necessity")
        check("necessity: --no-necessity switches only this gate off",
              not (rec3.get("necessity") or {}).get("enabled"), rec3.get("necessity"))

    with tempfile.TemporaryDirectory() as td:
        full2 = FIXTURE_FULL.replace('if m.label(1) != "x": bad.append("label")\n',
                                     'if m.label(1) != "x": bad.append("label")\n'
                                     'import io, contextlib\n'
                                     'buf = io.StringIO()\n'
                                     'with contextlib.redirect_stdout(buf):\n'
                                     '    m.label(2)\n'
                                     'if "labelling" not in buf.getvalue(): bad.append("print")\n')
        wt = mkrepo(td, {"mod.py": BASE_MOD}, {"mod.py": FIXED_MOD}, full2)
        rc, rec = run_cli(wt)
        nec = rec.get("necessity") or {}
        check("necessity: both hunks tested -> every hunk necessary, nothing unnecessary",
              nec.get("necessary") == 2 and not nec.get("unnecessary"), nec)
        check("necessity: and the verdict is RELEVANT", rec.get("verdict") == "relevant" and rc == 0,
              (rc, rec.get("verdict"), rec.get("reason")))

        # fail closed: the post-gate budget ran out -> a RELEVANT verdict is not given
        mod = load()
        diff = sh(wt, "git", "diff", "-U0").stdout
        objs, gnotes = mod.generate(wt, diff, [])
        rec0 = {"verdict": "relevant", "reason": "stub", "survivors": [], "mutants": [],
                "generation": gnotes, "killed": 0, "survived": 0, "evidence_mutants": 0}
        mod.apply_post_gates(wt, "bash v.sh", diff, rec0, objs, {"post_budget_s": -1})
        r = rec0
        check("fail-closed: unrun necessity/bank checks turn RELEVANT into UNPROVEN",
              r.get("verdict") == "unproven" and "could not finish" in (r.get("reason") or ""),
              (r.get("verdict"), r.get("reason")))
        check("fail-closed: and say which checks were not run",
              (r.get("necessity") or {}).get("untested"), r.get("necessity"))
        check("tree is restored byte-for-byte after the post-gates",
              (wt / "mod.py").read_text() == FIXED_MOD)

    # exemptions
    with tempfile.TemporaryDirectory() as td:
        readme_b = "# T\n\nline a\n\nline b\n\nline c\n"
        readme_f = "# T\n\nline A\n\nline b\n\nline C\n"
        wt = mkrepo(td, {"mod.py": BASE_MOD, "README.md": readme_b},
                    {"mod.py": FIXED_MOD.replace('    print("labelling", n)\n', ""), "README.md": readme_f},
                    FIXTURE_FULL)
        rc, rec = run_cli(wt)
        nec = rec.get("necessity") or {}
        check("exempt: a 2-hunk README edit (doc-only file) is NOT gated",
              any(e["file"] == "README.md" and "doc-only" in e["reason"] for e in nec.get("exempt", []))
              and not nec.get("unnecessary"), nec)

    with tempfile.TemporaryDirectory() as td:
        fixed = FIXED_MOD.replace('    print("labelling", n)\n', '    # label it\n')
        wt = mkrepo(td, {"mod.py": BASE_MOD}, {"mod.py": fixed}, FIXTURE_FULL)
        rc, rec = run_cli(wt)
        nec = rec.get("necessity") or {}
        check("exempt: a comment-only hunk is non-material, not 'unnecessary'",
              any("non-material" in e["reason"] for e in nec.get("exempt", [])) and not nec.get("unnecessary"), nec)

    with tempfile.TemporaryDirectory() as td:
        fixed = FIXED_MOD.replace('print("labelling", n)', 'print("labelling", n)  # relevance: unobservable')
        wt = mkrepo(td, {"mod.py": BASE_MOD}, {"mod.py": fixed}, FIXTURE_FULL)
        rc, rec = run_cli(wt)
        nec = rec.get("necessity") or {}
        check("exempt: a Python line carrying `relevance: unobservable` is not gated",
              any(e["hunk"] == 8 for e in nec.get("exempt", [])) and not nec.get("unnecessary"), nec)

    with tempfile.TemporaryDirectory() as td:
        fixed = FIXED_MOD.replace('    print("labelling", n)\n', "") + "\n\nMAGIC = 42\n"
        task = "# TASK\n\n## Must contain (literal tokens)\n- `def score(n):`\n- `MAGIC = 42`\n"
        wt = mkrepo(td, {"mod.py": BASE_MOD}, {"mod.py": fixed}, FIXTURE_FULL, task=task)
        rc, rec = run_cli(wt)
        nec = rec.get("necessity") or {}
        check("exempt: a hunk the spec's `## Must contain` literal REQUIRES is not gated",
              any("Must contain" in e["reason"] for e in nec.get("exempt", [])) and not nec.get("unnecessary"), nec)

    with tempfile.TemporaryDirectory() as td:
        # two hunks: a respaced guard (AST-identical to its revert) + a real, tested change
        base = ('def score(n):\n    if n<0:\n        raise ValueError("neg")\n    return n\n\n\n'
                'def label(n):\n    return "y"\n')
        fixed = ('def score(n):\n    if n < 0:\n        raise ValueError("neg")\n    return n\n\n\n'
                 'def label(n):\n    return "x"\n')
        wt = mkrepo(td, {"mod.py": base}, {"mod.py": fixed}, FIXTURE_FULL)
        rc, rec = run_cli(wt)
        nec = rec.get("necessity") or {}
        check("exempt: a whitespace-only hunk is an equivalent revert, not 'unnecessary'",
              any("equivalent revert" in e["reason"] for e in nec.get("exempt", []))
              and not nec.get("unnecessary"), nec)

    with tempfile.TemporaryDirectory() as td:
        # CREATION task: the whole file is one hunk (refimpl-cmd creates it)
        wt = mkrepo(td, {"seed.txt": "x\n"}, {}, FIXTURE_FULL.replace("mod.py", "helper.py"))
        (wt / "helper.py").write_text(FIXED_MOD)
        sh(wt, "git", "add", "-N", "helper.py")
        (wt / "fixture.py").write_text(FIXTURE_FULL.replace("mod.py", "helper.py"))
        rc, rec = run_cli(wt)
        nec = rec.get("necessity") or {}
        check("exempt: creation file (one hunk) is explicit, not 'unnecessary'",
              any("single material hunk" in e["reason"] for e in nec.get("exempt", []))
              and not nec.get("unnecessary"), nec)

    # ---------------- 2. wrong-solution bank ----------------
    with tempfile.TemporaryDirectory() as td:
        fixed = FIXED_MOD.replace('    print("labelling", n)\n', "")
        wt = mkrepo(td, {"mod.py": BASE_MOD}, {"mod.py": fixed}, FIXTURE_NO_BOUNDARY)
        rc, rec = run_cli(wt)
        bk = rec.get("wrong_bank") or {}
        check("bank: a fixture missing the boundary case -> LOW", rec.get("verdict") == "low" and rc == 1,
              (rc, rec.get("verdict"), rec.get("reason")))
        check("bank: names the category that survived",
              any("WRONG-SOLUTION [boundary]" in s for s in bk.get("survivors", [])), bk.get("survivors"))
        check("bank: the survivor reaches the refine loop's survivor list",
              any(s.get("gate") == "wrong-solution-bank" for s in rec.get("survivors", [])), rec.get("survivors"))
        check("bank: deterministic -- same variants on a re-run",
              [v["mutation"] for v in run_cli(wt)[1]["wrong_bank"]["variants"]]
              == [v["mutation"] for v in bk["variants"]])

    with tempfile.TemporaryDirectory() as td:
        fixed = FIXED_MOD.replace('    print("labelling", n)\n', "")
        wt = mkrepo(td, {"mod.py": BASE_MOD}, {"mod.py": fixed}, FIXTURE_FULL)
        rc, rec = run_cli(wt)
        bk = rec.get("wrong_bank") or {}
        cats = sorted({v["category"] for v in bk.get("variants", [])})
        check("bank: a complete fixture rejects every variant -> RELEVANT",
              rec.get("verdict") == "relevant" and not bk.get("survivors"),
              (rec.get("verdict"), rec.get("reason")))
        check("bank: categories exercised include boundary, swapped-compare, early-return",
              {"boundary", "swapped-compare", "early-return"} <= set(cats), cats)
        check("bank: categories with no site are reported, not silently dropped",
              "unavailable" in bk, bk)
        check("bank: capped (cheap)", len(bk.get("variants", [])) <= 2 * len(vr.BANK_CATEGORIES) + 4,
              len(bk.get("variants", [])))
        rc2, rec2 = run_cli(wt, "--no-wrong-bank")
        check("bank: --no-wrong-bank switches only this gate off", not (rec2.get("wrong_bank") or {}).get("enabled"))

    # ---------------- 4. opt-in LLM hooks ----------------
    with tempfile.TemporaryDirectory() as td:
        fixed = FIXED_MOD.replace('    print("labelling", n)\n', "")
        wt = mkrepo(td, {"mod.py": BASE_MOD}, {"mod.py": fixed}, FIXTURE_FULL)
        marker = Path(td) / "llm-was-called"
        stub = Path(td) / "stub.py"
        stub.write_text(textwrap.dedent(f'''\
            import json, sys
            open({str(marker)!r}, "a").write("x")
            req = json.load(sys.stdin)
            files = req.get("files")
            if files is None:
                print(json.dumps([]))
            else:
                src = files["mod.py"].replace("    return n\\n", "    return n if n != 7 else 8\\n", 1)
                print(json.dumps([{{"file": "mod.py", "source": src, "why": "special-cases 7", "line": 4}}]))
            '''))
        rc, rec = run_cli(wt)
        check("llm: OFF by default -- the stub was never invoked", not marker.exists())
        check("llm: default run is RELEVANT without it", rec.get("verdict") == "relevant", rec.get("reason"))
        rc, rec = run_cli(wt, "--llm-wrong-solutions-cmd", f"{sys.executable} {stub}")
        check("llm: a plausible-wrong variant the fixture misses -> LOW (category llm)",
              rec.get("verdict") == "low" and any("[llm]" in s for s in (rec.get("wrong_bank") or {}).get("survivors", [])),
              (rec.get("verdict"), (rec.get("wrong_bank") or {}).get("survivors")))
        # equivalence classifier is advisory
        bound = Path(td) / "eq.py"
        bound.write_text("import json,sys\nr=json.load(sys.stdin)\nprint(json.dumps([s['id'] for s in r['survivors']]))\n")
        wt2 = mkrepo(tempfile.mkdtemp(dir=td), {"mod.py": BASE_MOD}, {"mod.py": fixed}, FIXTURE_NO_BOUNDARY)
        rc, rec = run_cli(wt2, "--llm-equivalence-cmd", f"{sys.executable} {bound}")
        check("llm-equiv: survivors are flagged advisory...",
              rec.get("llm_equivalent", 0) >= 1 and all(s.get("llm_equivalent") for s in rec["survivors"]),
              (rec.get("llm_equivalent"), rec.get("survivors")))
        check("llm-equiv: ...but the verdict still counts them (fail-closed)", rec.get("verdict") == "low")

    # ---------------- JS (ts-mutator sidecar) ----------------
    def _ts_works():
        if not (shutil.which("node") and vr._ts_mutator_path()):
            return False
        with tempfile.TemporaryDirectory() as pd:
            (Path(pd) / "p.mjs").write_text("export function f(a) {\n  return a >= 1;\n}\n")
            return bool(vr.ts_mutants(Path(pd), "p.mjs", (Path(pd) / "p.mjs").read_text(), {2}))
    if _ts_works():
        with tempfile.TemporaryDirectory() as td:
            wt = Path(td)
            sh(wt, "git", "init", "-q", "-b", "main")
            sh(wt, "git", "config", "user.email", "t@t")
            sh(wt, "git", "config", "user.name", "t")
            (wt / "lib.mjs").write_text("export function isAdult(age) {\n  return false;\n}\n\nexport function tag() {\n  return 'a';\n}\n")
            (wt / "TASK.md").write_text("# T\n\n## Must contain (literal tokens)\n- `export function isAdult(age)`\n")
            chk_full = ('import { isAdult } from "./lib.mjs";\nlet bad = 0;\n'
                        'if (isAdult(17)) bad++;\nif (!isAdult(18)) bad++;\nif (!isAdult(40)) bad++;\n'
                        'process.exit(bad ? 1 : 0);\n')
            chk_nob = chk_full.replace("if (!isAdult(18)) bad++;\n", "")
            (wt / "check.mjs").write_text(chk_nob)
            (wt / "v.sh").write_text('#!/bin/bash\ncd "$(dirname "$0")"\nnode check.mjs && echo VERIFY_OK || exit 1\n')
            sh(wt, "git", "add", "-A")
            sh(wt, "git", "commit", "-qm", "b")
            (wt / "lib.mjs").write_text("export function isAdult(age) {\n  return age >= 18;\n}\n\nexport function tag() {\n  return 'a';\n}\n")
            rc, rec = run_cli(wt)
            check("js: boundary variant (>= -> >) missed by the fixture -> LOW [boundary]",
                  rec.get("verdict") == "low" and any("[boundary]" in s for s in (rec.get("wrong_bank") or {}).get("survivors", [])),
                  (rec.get("verdict"), rec.get("reason")))
            (wt / "check.mjs").write_text(chk_full)
            sh(wt, "git", "add", "check.mjs")
            sh(wt, "git", "commit", "-qm", "full fixture")
            rc, rec = run_cli(wt)
            check("js: with the 18 case the bank rejects every variant -> RELEVANT",
                  rec.get("verdict") == "relevant" and not (rec.get("wrong_bank") or {}).get("survivors"),
                  (rec.get("verdict"), rec.get("reason")))
            # honoured opt-out on a material TS hunk: exempt from necessity
            (wt / "lib.mjs").write_text('export function isAdult(age) {\n  return age >= 18;\n}\n\nexport function tag() {\n'
                                        '  console.log("t"); // relevance: unobservable\n  return \'a\';\n}\n')
            rc, rec = run_cli(wt)
            nec = rec.get("necessity") or {}
            check("js: a hunk of honoured `relevance: unobservable` lines is exempt from necessity",
                  any("opt-out" in e["reason"] for e in nec.get("exempt", [])) and not nec.get("unnecessary"), nec)
            (wt / "lib.mjs").write_text('export function isAdult(age) {\n  return age >= 18;\n}\n\nexport function tag() {\n'
                                        '  console.log("t");\n  return \'a\';\n}\n')
            rc, rec = run_cli(wt)
            check("js: the same untested console.log hunk WITHOUT the opt-out is UNNECESSARY -> LOW",
                  rec.get("verdict") == "low" and (rec.get("necessity") or {}).get("unnecessary"),
                  (rec.get("verdict"), rec.get("necessity")))
    else:
        print("SKIP js bank (node / ts-mutator unavailable)")

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


REVERTS = [
    ("necessity gate removed", "    if necessity:\n        cands: list[Mutant] = []", "    if False:\n        cands: list[Mutant] = []"),
    ("wrong-solution bank removed", "    if bank:\n        pool = [m for m in objs", "    if False:\n        pool = [m for m in objs"),
    ("doc-only exemption removed", "if rel.endswith(NECESSITY_DOC_EXTS):", "if False:"),
    ("must-contain exemption removed", "                if not _lit_ok(m, file_lits):\n                    nec[\"exempt\"]", "                if False:\n                    nec[\"exempt\"]"),
    ("opt-out exemption removed", "if body and all(n in annotated for n in body):", "if False:"),
    ("equivalent-revert exemption removed", "                why = equivalent_reason(m, fixed, dead)\n                if why:\n                    nec[\"exempt\"]", "                why = None\n                if why:\n                    nec[\"exempt\"]"),
    ("equivalent filter removed from generate", "            why = equivalent_reason(m, fixed, dead)\n            if why:\n                notes.setdefault", "            why = None\n            if why:\n                notes.setdefault"),
    ("fail-closed on unrun checks removed", "    elif untested_msgs and rec.get(\"verdict\") == \"relevant\":", "    elif False:"),
    ("hard verdict removed", "    if reasons:\n        rec[\"verdict\"] = \"low\"", "    if reasons:\n        pass"),
    ("survivors not handed to refine loop", "        rec[\"survivors\"] = new_surv + old", "        rec[\"survivors\"] = old"),
    ("early-return/default extras removed", "    if not rel.endswith(\".py\"):\n        return []\n    try:\n        tree = ast.parse(fixed)\n    except SyntaxError:\n        return []\n    annotated, _ = optout_lines(fixed)\n    src = Src(fixed)",
     "    return []\n    annotated, _ = optout_lines(fixed)\n    src = Src(fixed)"),
    ("boundary category merged away", "return \"boundary\"\n        return \"swapped-compare\"", "return \"swapped-compare\"\n        return \"swapped-compare\""),
    ("advisory llm flag ignored by verdict is fine; make it override", "                    s[\"llm_equivalent\"] = True\n                    n += 1", "                    s[\"llm_equivalent\"] = True\n                    rec[\"verdict\"] = \"relevant\"\n                    n += 1"),
]


def revert_check():
    src = TARGET.read_text()
    bad = 0
    for name, old, new in REVERTS:
        if src.count(old) != 1:
            print(f"ANCHOR MISSING ({src.count(old)}): {name}")
            bad += 1
            continue
        with tempfile.NamedTemporaryFile("w", suffix="-vr.py", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__, "--target", f.name], capture_output=True, text=True)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad})")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
