#!/usr/bin/env python3
"""Guards 2026-10-05: rt-egift-link-s1-s0-db-schema c1 (7ed88ee015e5) still hit 24/24.

auto-harness-check (ollama-dispatch-auto HARNESS_CHECK):
 (a) DIRTY NON-TARGET: a tracked non-target file the model left modified (it ran
     refimpl.py by hand -> prisma/schema.prisma dirty) is measured at HEAD, and its
     dirty bytes are put back afterwards. Before: refimpl's "already present" guard
     kept the stale line and the literal stayed MISSING forever.
 (b) SCOPE vs PINS: a Must-contain literal pinned `in <file>:` to a file absent from
     the "Only edit" line fails the self-check (the coding job could never satisfy both).
 (c) PLAN-LITERAL TYPO GUARD: a --require literal must appear verbatim in a
     Must-contain literal (OrderEgiftLink vs authored OrderEgmtLink).
ollama-dispatch-scaffold: --edit-file widens the scope line for the tsx runner too.
ollama-dispatch-slice: per-slice target/edit_files, uncovered_edit_files() plan-defect
 detector, _stage_slice() lands the extra files (and refuses a chain-changed extra).

Run: python3 test-scope-pins-dirty-slicefiles.py [--revert-check]
"""
import importlib.util
import os
import json
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

BIN = Path(__file__).resolve().parent
AUTO = Path(os.environ.get("AUTO_SRC") or BIN / "ollama-dispatch-auto")
SCAF = Path(os.environ.get("SCAF_SRC") or BIN / "ollama-dispatch-scaffold")
SLICE = Path(os.environ.get("SLICE_SRC") or BIN / "ollama-dispatch-slice")
PLAN = Path(os.environ.get("PLAN_SRC") or BIN / "ollama-dispatch-plan")
FAILS = []


def check(name, ok, extra=""):
    print(("ok  " if ok else "FAIL") + ": " + name + ("" if ok else "  " + str(extra)[-700:]))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [str(path)]
    ld.exec_module(m)
    return m


def git(wt, *a, check=False):
    r = subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True)
    if check and r.returncode:
        raise RuntimeError(r.stderr)
    return r


def init(wt):
    wt.mkdir(parents=True, exist_ok=True)
    git(wt, "init", "-q"); git(wt, "config", "user.email", "t@t"); git(wt, "config", "user.name", "t")


GOOD_TASK = ("## Must contain\n- in schema.txt: `egiftLink OrderEgiftLink?`\n- `MARK`\n\n"
             "## Scope\nOnly edit `target.txt`, `schema.txt`; do not edit `verify.sh` or `TASK.md`.\n")


def mkwt(task=GOOD_TASK, dirty_schema=True):
    wt = Path(tempfile.mkdtemp(prefix="scopepins-")) / "wt"
    init(wt)
    (wt / "target.txt").write_text("stub\n")
    (wt / "schema.txt").write_text("model Order {\n  id Int\n}\n")
    git(wt, "add", "."); git(wt, "commit", "-qm", "b")
    (wt / "TASK.md").write_text(task)
    (wt / "verify.sh").write_text(
        "grep -q MARK target.txt || { echo 'FAIL: no MARK'; exit 1; }\n"
        "grep -qF 'egiftLink OrderEgiftLink?' schema.txt || { echo 'FAIL: literal MISSING'; exit 1; }\n"
        "echo VERIFY_OK\n")
    # refimpl guards like the authored one did: skip the schema edit if already "present"
    (wt / "refimpl.py").write_text(
        "s=open('schema.txt').read()\n"
        "if 'egiftLink' not in s:\n"
        "    s=s.replace('  id Int\\n','  id Int\\n  egiftLink OrderEgiftLink?\\n')\n"
        "    open('schema.txt','w').write(s)\n"
        "open('target.txt','w').write('MARK\\n')\n")
    (wt / "verify.test.ts").write_text("// fixture\n")
    (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "target.txt"}))
    if dirty_schema:  # what the model's hand-run left behind: stale padded line
        (wt / "schema.txt").write_text("model Order {\n  id Int\n  egiftLink      OrderEgmtLink?\n}\n")
    return wt


def run_check(wt):
    r = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt,
                       capture_output=True, text=True, timeout=120)
    return r.returncode, r.stdout + r.stderr


def auto_cases(m):
    wt = mkwt()
    dirty = (wt / "schema.txt").read_bytes()
    m.write_harness_check(wt, "ts", requires=["OrderEgiftLink"])
    rc, out = run_check(wt)
    check("(a) dirty non-target measured at HEAD -> self-check passes", rc == 0, out)
    check("(a) model's dirty non-target bytes restored after the check",
          (wt / "schema.txt").read_bytes() == dirty, (wt / "schema.txt").read_text())
    check("(a) target restored to stub after the check", (wt / "target.txt").read_text() == "stub\n")
    # control: a clean tree passes as well (no false positive from the reset)
    wt0 = mkwt(dirty_schema=False)
    m.write_harness_check(wt0, "ts", requires=["OrderEgiftLink"])
    rc, out = run_check(wt0)
    check("(a) control: clean tree passes", rc == 0, out)

    wtb = mkwt(task=GOOD_TASK.replace("`target.txt`, `schema.txt`", "`target.txt`"), dirty_schema=False)
    m.write_harness_check(wtb, "ts")
    rc, out = run_check(wtb)
    check("(b) literal pinned to a file outside the scope line -> FAIL",
          rc != 0 and "scope line only allows" in out and "`schema.txt`" in out, out)

    wtc = mkwt(task=GOOD_TASK.replace("OrderEgiftLink", "OrderEgmtLink"), dirty_schema=False)
    (wtc / "verify.sh").write_text((wtc / "verify.sh").read_text().replace("OrderEgiftLink", "OrderEgmtLink"))
    (wtc / "refimpl.py").write_text((wtc / "refimpl.py").read_text().replace("OrderEgiftLink", "OrderEgmtLink"))
    m.write_harness_check(wtc, "ts", requires=["OrderEgiftLink"])
    rc, out = run_check(wtc)
    check("(c) plan literal misspelled in TASK.md -> TYPO FAIL",
          rc != 0 and "TYPO" in out and "OrderEgiftLink" in out, out)
    m.write_harness_check(wtc, "ts", requires=[])
    rc, out = run_check(wtc)
    check("(c) control: no --require -> self-consistent typo harness not flagged by (c)",
          "TYPO" not in out, out)

    # whitespace-only literal miss: refimpl writes the formatter-aligned line
    wtw = mkwt(dirty_schema=False)
    (wtw / "refimpl.py").write_text((wtw / "refimpl.py").read_text().replace(
        "'  id Int\\n  egiftLink OrderEgiftLink?\\n'", "'  id Int\\n  egiftLink      OrderEgiftLink?\\n'"))
    m.write_harness_check(wtw, "ts")
    rc, out = run_check(wtw)
    check("(d) padded line vs single-spaced literal -> DIFFERENT SPACING hint",
          rc != 0 and "DIFFERENT SPACING" in out, out)
    check("(d) control: exact-literal run has no spacing hint", "DIFFERENT SPACING" not in run_check(wt0)[1])


def scaffold_cases():
    td = Path(tempfile.mkdtemp(prefix="scafef-"))
    repo = td / "repo"; init(repo)
    (repo / "lib").mkdir(); (repo / "prisma").mkdir()
    (repo / "lib/egiftLinkStore.ts").write_text("export const x = 1;\n")
    (repo / "prisma/schema.prisma").write_text("model A { id Int @id }\n")
    (repo / "package.json").write_text("{}")
    git(repo, "add", "."); git(repo, "commit", "-qm", "b")
    dest = td / "wt"
    r = subprocess.run([sys.executable, str(SCAF), "--repo", str(repo), "--dest", str(dest),
                        "--target", "lib/egiftLinkStore.ts", "--lang", "ts", "--symbol", "x",
                        "--edit-file", "prisma/schema.prisma", "--label", "t-ef"],
                       capture_output=True, text=True, timeout=300)
    task = (dest / "TASK.md").read_text() if (dest / "TASK.md").exists() else ""
    check("scaffold: --edit-file accepted with the tsx runner", r.returncode == 0, r.stdout + r.stderr)
    check("scaffold: scope line lists the edit file",
          "Only edit `lib/egiftLinkStore.ts`, `prisma/schema.prisma`" in task, task[-800:])


def migration_cases(scaf):
    td = Path(tempfile.mkdtemp(prefix="migdrift-"))
    fb = td / "fakebin"; fb.mkdir()
    (fb / "npx").write_text("#!/bin/sh\nexit 0\n"); (fb / "npx").chmod(0o755)
    wt = td / "wt"; (wt / "prisma/migrations/20260101000000_init").mkdir(parents=True)
    (wt / "package.json").write_text("{}")
    (wt / "node_modules/.bin").mkdir(parents=True)
    log = td / "prisma-calls"
    # Prisma-7-like CLI: help has no --to-schema-datamodel; diff reports drift (rc 2)
    (wt / "node_modules/.bin/prisma").write_text(
        "#!/bin/sh\nif [ \"$3\" = --help ]; then echo '  --to-schema   Path'; exit 0; fi\n"
        f"echo \"$@\" >> {log}\nexit 2\n")
    (wt / "node_modules/.bin/prisma").chmod(0o755)
    (wt / "prisma/schema.prisma").write_text("model A { id Int @id }\n")
    (wt / "prisma/migrations/20260101000000_init/migration.sql").write_text('CREATE TABLE "A" ("id" INTEGER);\n')
    init(wt); git(wt, "add", "."); git(wt, "commit", "-qm", "b")
    script = "fails=0\n" + scaf.TS_BOOTSTRAP + "\necho FAILS=$fails\n"
    env = {**os.environ, "PATH": f"{fb}:{os.environ['PATH']}"}
    run = lambda: subprocess.run(["bash", "-c", script], cwd=wt, capture_output=True, text=True, env=env, timeout=60).stdout
    o0 = run()
    check("migrations: untouched schema -> no migration gate (control)", "FAILS=0" in o0 and not log.exists(), o0)
    with open(wt / "prisma/schema.prisma", "a") as f:
        f.write("model B { id Int @id }\n")
    o1 = run()
    calls = log.read_text() if log.exists() else ""
    check("migrations: schema changed without a migration -> drift check runs and FAILs",
          "FAIL: migrations drift" in o1 and "no migration was added" in o1 and "FAILS=1" in o1, o1)
    check("migrations: Prisma-7 flag --to-schema used (not removed --to-schema-datamodel)",
          "--to-schema prisma/schema.prisma" in calls and "datamodel" not in calls, calls)


def slice_cases(sl):
    st = {"target": "lib/egiftLink.ts"}
    s0 = {"intent": "Add model OrderEgiftLink to prisma/schema.prisma and a migration."}
    check("slice: slice_target falls back to the plan target", sl.slice_target(st, {}) == "lib/egiftLink.ts")
    check("slice: uncovered_edit_files flags the schema edit outside scope",
          sl.uncovered_edit_files(st, s0) == ["prisma/schema.prisma"], sl.uncovered_edit_files(st, s0))
    s0f = dict(s0, target="lib/egiftLinkStore.ts", edit_files=["prisma/schema.prisma", "lib/egiftLinkStore.ts"])
    check("slice: per-slice target honoured", sl.slice_target(st, s0f) == "lib/egiftLinkStore.ts")
    check("slice: edit_files dedups the target", sl.slice_edit_files(st, s0f) == ["prisma/schema.prisma"])
    check("slice: covered slice -> no plan defect", sl.uncovered_edit_files(st, s0f) == [])
    s5 = {"intent": "Create new 'components/EgiftLink.tsx' (mounted in app/orders/[id]/page.tsx).",
          "target": "components/EgiftLink.tsx"}
    check("slice: mounted-in page flagged", sl.uncovered_edit_files(st, s5) == ["app/orders/[id]/page.tsx"],
          sl.uncovered_edit_files(st, s5))
    s4 = {"intent": "Mirror the pattern of app/api/orders/[id]/gift-cards/route.ts using lib/egiftLink.ts.",
          "target": "app/api/x/route.ts"}
    check("slice: a merely-referenced file is not flagged", sl.uncovered_edit_files(st, s4) == [],
          sl.uncovered_edit_files(st, s4))

    # _stage_slice: chain repo + slice worktree off it
    td = Path(tempfile.mkdtemp(prefix="stageslice-"))
    cwt = td / "chain"; init(cwt)
    (cwt / "lib").mkdir(); (cwt / "prisma").mkdir()
    (cwt / "lib/egiftLinkStore.ts").write_text("// stub\n")
    (cwt / "prisma/schema.prisma").write_text("model A {}\n")
    git(cwt, "add", "."); git(cwt, "commit", "-qm", "base", check=True)
    wt = td / "wt"
    git(cwt, "worktree", "add", "-q", "--detach", str(wt), "HEAD", check=True)
    (wt / "lib/egiftLinkStore.ts").write_text("export function getOrderEgiftLink() {}\n")
    (wt / "prisma/schema.prisma").write_text("model A {}\nmodel OrderEgiftLink {}\n")
    try:
        out, msg = sl._stage_slice(str(cwt), str(wt), st, "s0", s0f)
    except Exception as e:  # noqa
        out, msg = "exc", repr(e)
    staged = git(cwt, "diff", "--cached", "--name-only").stdout.split()
    check("slice: _stage_slice ok", out == "ok", (out, msg))
    check("slice: _stage_slice stages the extra edit file", "prisma/schema.prisma" in staged, staged)
    check("slice: _stage_slice stages the target", "lib/egiftLinkStore.ts" in staged, staged)
    git(cwt, "reset", "-q", "--hard", "HEAD")
    # chain moves the extra file after the worktree was seeded -> conflict, nothing staged
    (cwt / "prisma/schema.prisma").write_text("model A {}\nmodel B {}\n")
    git(cwt, "commit", "-qam", "chain moved schema", check=True)
    try:
        out, msg = sl._stage_slice(str(cwt), str(wt), st, "s0", s0f)
    except Exception as e:  # noqa
        out, msg = "exc", repr(e)
    staged = git(cwt, "diff", "--cached", "--name-only").stdout.split()
    check("slice: chain-changed extra file -> conflict", out == "conflict", (out, msg))
    check("slice: conflict leaves nothing staged", staged == [], staged)


def plan_cases(pl, sl):
    fxd = BIN / "test-fixtures-live-artifacts"  # frozen copies (hermetic: no live ~/.ollama-dispatch read)
    pre = fxd / "rt-egift-link-s1.pre-scope.json"
    check("plan: pre-fix plan fixture present", pre.exists(), str(pre))
    if not pre.exists():
        return
    plan = json.loads(pre.read_text())
    codes = [c for c, _ in pl.gate_plan(plan)]
    check("planner: pre-fix egift plan rejected with UNSCOPED_EDIT_FILE",
          codes.count("UNSCOPED_EDIT_FILE") == 4, codes)
    agree = all(pl.unscoped_edit_files(plan, s) == sl.uncovered_edit_files(plan, s)
                for s in plan["slices"])
    check("planner and slicer detectors agree on every pre-fix slice", agree)
    fixed = json.loads((fxd / "rt-egift-link-s1.fixed.json").read_text())
    fcodes = [c for c, _ in pl.gate_plan(fixed)]
    # ROOT CAUSE of the old failure (2026-10-09): this test pins a FROZEN snapshot of the plan
    # (`.slices.json.fixed`, 2026-10-05) as "the fixed plan". plan_lint's HEADER_PAIR_LITERAL
    # gate landed LATER (5c4dab4) and, correctly, flags that snapshot's s4 `Cache-Control:
    # no-store` must_contain (the live plan was fixed for it on 10-06, `.bak-...s4hdr`). The test
    # is about SCOPE (--edit-file coverage), so it asserts exactly that: no scope-family code, and
    # nothing but the later-added header-pair lint remains.
    scope_codes = [c for c in fcodes if "SCOPE" in c or c.startswith("UNSCOPED")]
    check("planner: fixed egift plan has no scope defects", scope_codes == [], fcodes)
    check("planner: the only code left on the frozen snapshot is the later HEADER_PAIR_LITERAL lint",
          set(fcodes) <= {"HEADER_PAIR_LITERAL"}, fcodes)
    check("slicer: fixed plan has no uncovered edit files",
          all(sl.uncovered_edit_files(fixed, s) == [] for s in fixed["slices"]))


def main():
    auto_cases(load(AUTO, "oda_sp"))
    plan_cases(load(PLAN, "odp_sp"), load(SLICE, "osl_sp2"))
    scaffold_cases()
    migration_cases(load(SCAF, "scaf_sp"))
    slice_cases(load(SLICE, "osl_sp"))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTANTS = [
    ("AUTO_SRC", AUTO, "dirty non-target reset removed",
     '    git("checkout", "HEAD", "--", _f)\nr = sh("bash verify.sh")', '    pass\nr = sh("bash verify.sh")'),
    ("AUTO_SRC", AUTO, "scope-vs-pin check removed", "if _unscoped:\n", "if False:\n"),
    ("AUTO_SRC", AUTO, "typo guard removed", "if _lost:\n", "if False:\n"),
    ("AUTO_SRC", AUTO, "requires not substituted",
     '"__REQUIRED_LITERALS__", repr([str(r) for r in (requires or ())])',
     '"__REQUIRED_LITERALS__", "[]"'),
    ("SCAF_SRC", SCAF, "scaffold edit-file restriction restored",
     "    _wt_cleanup = None\n",
     "    if a.edit_file and a.ts_runner != 'node-test':\n        ap.error('edit-file node-test only')\n"
     "    _wt_cleanup = None\n"),
    ("SCAF_SRC", SCAF, "schema-changed-without-migration trigger removed",
     '  if [ -n "$_CHANGED_MIG" ] || [ -n "$_SCHEMA_CHG" ]; then', '  if [ -n "$_CHANGED_MIG" ]; then'),
    ("SCAF_SRC", SCAF, "drift flag reverted to removed --to-schema-datamodel",
     '      _DIFF_TO="--to-schema"\n', '      _DIFF_TO="--to-schema-datamodel"\n'),
    ("PLAN_SRC", PLAN, "prisma attribute calls counted as signatures again",
     "    _sigtext = _DECORATOR_CALL_RE.sub(\" \", text)\n", "    _sigtext = text\n"),
    ("SLICE_SRC", SLICE, "slice target ignored",
     '    return t if isinstance(t, str) and t.strip() else st["target"]', '    return st["target"]'),
    ("SLICE_SRC", SLICE, "uncovered detector disabled",
     "        if f not in have and f not in out:\n", "        if False:\n"),
    ("SLICE_SRC", SLICE, "extras not staged", "    extras = slice_edit_files(st, s)\n", "    extras = []\n"),
    ("SLICE_SRC", SLICE, "extra conflict check removed",
     "        if base and tip and base != tip and subprocess.run(", "        if False and subprocess.run("),
    ("SLICE_SRC", SLICE, "extension match truncates .tsx to .ts",
     "sql|tsx|ts|jsx|js|mjs|cjs|py|css|html|json|swift))(?![\\w-])", "sql|ts|tsx|js|jsx|mjs|cjs|py|css|html|json|swift))"),
    ("PLAN_SRC", PLAN, "planner UNSCOPED_EDIT_FILE gate removed",
     "        for _f in unscoped_edit_files(plan, s):\n", "        for _f in []:\n"),
    ("AUTO_SRC", AUTO, "whitespace-only literal hint removed",
     "            if _wl not in _wb and _wsq(_wl) in _wsq(_wb):", "            if False:"),
]


def revert_check():
    bad = 0
    for var, path, name, old, new in MUTANTS:
        s = Path(path).read_text()
        assert s.count(old) == 1, f"anchor missing/ambiguous: {old[:60]!r}"
        f = tempfile.NamedTemporaryFile("w", suffix="-mut", delete=False, dir=str(BIN))
        f.write(s.replace(old, new)); f.close()
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True, timeout=900)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
