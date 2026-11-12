#!/usr/bin/env python3
"""Tests for mini_rule_coverage.py (TASK.md's `## Rule coverage`), its use in `mini-spec --lint` and the TASK draft, and
mini-validate's task_text. Temp dirs only: no git, no runner, no network."""
import contextlib, importlib.machinery, importlib.util, io, json, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mini_rule_coverage as C  # noqa: E402


def load(name, path):
    ldr = importlib.machinery.SourceFileLoader(name, str(path))
    sp = importlib.util.spec_from_loader(name, ldr)
    m = importlib.util.module_from_spec(sp)
    ldr.exec_module(m)
    return m


PY_TEST = '''import os
def test_skips_undated_rows():
    assert 1 == 1
class TestX:
    def test_method_case(self):
        assert 2 == 2
async def test_async_case():
    assert 3 == 3
def helper():
    pass
TESTS = [test_skips_undated_rows]
'''
JS_TEST = '''const { test } = require('node:test');
test('skips undated rows', () => {});
it("rejects a bad name", () => {});
test.skip(`later case`, () => {});
const ok = /x/.test('not a test title');
'''

# test_names
assert C.test_names(PY_TEST, "a/test_x.py") == {"test_skips_undated_rows", "test_method_case", "test_async_case"}
assert C.test_names(JS_TEST, "a/x.test.js") == {"skips undated rows", "rejects a bad name", "later case"}, C.test_names(JS_TEST, "x.js")
assert C.test_names("", "t.py") == set() and C.test_names(None, "t.js") == set()

# rules: bullets of every kind, continuations joined, prose ignored, the next ## (or #) heading ends the section, ### does not
TASK = """# TASK: x

## Required change

- not a coverage line -> `test_skips_undated_rows`

## Rule coverage

Each rule with the test that pins it:

- a row without a date is skipped
  -> `test_skips_undated_rows`
* a method case -> `TestX::test_method_case`
1. an async case -> `test_async_case()`
2) one more -> `test_skips_undated_rows`
### a sub-heading stays in the section
+ last -> `test_method_case`

## Scope

- not a coverage line either
"""
rs = C.rules(TASK)
assert rs == ["a row without a date is skipped -> `test_skips_undated_rows`", "a method case -> `TestX::test_method_case`",
              "an async case -> `test_async_case()`", "one more -> `test_skips_undated_rows`", "last -> `test_method_case`"], rs
assert C.rules("# TASK\n\n## Required change\n\n- x\n") is None
assert C.rules("## Rule coverage\n\nprose only\n\n## Scope\n") == []
assert C.rules("## Rule coverage\n- at the end of the file -> `t`") == ["at the end of the file -> `t`"]

# cited: path:: prefix and () dropped, several names, none
assert C.cited("x -> `a/test_x.py::test_y` and `test_z()`") == {"test_y", "test_z"}
assert C.cited("no backticks here") == set()

# check: all covered, a bad-input rule present -> clean
errs, warns = C.check(TASK, PY_TEST, "a/test_x.py")
assert errs == [] and warns == [], (errs, warns)

# no section: a warning only
errs, warns = C.check("# TASK\n\n## Required change\n\n- x\n", PY_TEST, "t.py")
assert errs == [] and warns == [C.NO_SECTION], (errs, warns)

# empty section: an error
errs, warns = C.check("## Rule coverage\n\nprose only\n", PY_TEST, "t.py")
assert errs == ["`## Rule coverage` lists no rules"] and warns == [], (errs, warns)

# a rule naming no test, or a test the file does not define, or only other code in backticks: an error each, numbered
T2 = """## Rule coverage
- a row without a date is skipped -> `test_skips_undated_rows`
- the total is summed
- a bad name raises `ValueError("bad")` -> `test_nonexistent`
"""
errs, warns = C.check(T2, PY_TEST, "a/test_x.py")
assert len(errs) == 2 and errs[0].startswith("rule 2 names no test that a/test_x.py defines") and errs[1].startswith("rule 3 "), errs
assert warns == [], warns

# no rule about bad or missing input or an error path: a warning
T3 = "## Rule coverage\n- the total is summed -> `test_skips_undated_rows`\n- labels are sorted -> `test_method_case`\n"
errs, warns = C.check(T3, PY_TEST, "t.py")
assert errs == [] and len(warns) == 1 and "bad or missing input" in warns[0], (errs, warns)

# the bad-input words: ValueError / KeyError, None, non-empty, not a str, missing, skipped, rejects, empty; plain words do not
for phrase in ("raises ValueError", "a KeyError", "None stays None", "a non-empty name", "not a str", "a missing key", "is skipped",
               "rejects it", "an empty list", "malformed JSON", "too long names", "returns False without a file"):
    assert C.BAD_INPUT.search(phrase), phrase
for phrase in ("the total is summed", "labels are sorted", "nothing changes", "not available"):
    assert not C.BAD_INPUT.search(phrase), phrase

# node titles
T4 = "## Rule coverage\n- an undated row is skipped -> `skips undated rows`\n- a bad name -> `rejects a bad name`\n"
assert C.check(T4, JS_TEST, "a/x.test.js") == ([], [])

# ---- mini-spec: the TASK draft carries the section with the marker; lint checks it against frozen/<test_file> ----------------
M = load("mini_spec_under_test", HERE / "mini-spec")
md = M.draft_task("calc", "add() must reject None.", 1, 1, "add", ["src/calc.py"], {"src/calc.py": "def add(a, b):\n    return a + b\n"},
                  {"add": ("src/calc.py", (1, 2))}, ["add"], "src/test_calc.py", "python3 test_calc.py", [], "")
rs = C.rules(md)
assert rs is not None and len(rs) == 1 and M.MARK in rs[0] and "`test_skips_undated_rows`" in rs[0] and "frozen/src/test_calc.py" in rs[0], rs
assert md.index("## Test notes") < md.index("## Rule coverage") < md.index("## Scope")
md_js = M.draft_task("calc", "x", 1, 1, "add", ["src/calc.js"], {"src/calc.js": "function add(a, b) { return a + b; }\nmodule.exports = { add };\n"},
                     {"add": ("src/calc.js", (1, 1))}, ["add"], "src/calc.test.js", "node --test src/calc.test.js", [], "")
assert "`skips undated rows`" in C.rules(md_js)[0]

M.run_precheck = lambda _p: (0, "precheck ok (stubbed by the test)")


def lint(rd):
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        rc = M.lint(rd)
    return rc, out.getvalue()


td = Path(tempfile.mkdtemp(prefix="rule-coverage-test-"))
base = td / "repo"
(base / "src").mkdir(parents=True)
rd = td / "run"
(rd / "frozen" / "src").mkdir(parents=True)
spec = {"baseline_from": str(base), "allowed": ["src/calc.py"], "task": str(rd / "TASK.md"),
        "steps": [{"test_file": "src/test_a.py", "test_cmd": "x"}, {"task": str(rd / "TASK-s2.md"), "test_file": "src/test_b.py", "test_cmd": "y"}]}
(rd / "spec.json").write_text(json.dumps(spec))
MC = "## Must contain\n\n- `x`\n\n"
(rd / "TASK.md").write_text("# TASK\n\n" + MC + "## Rule coverage\n\n- a missing date is skipped -> `test_a1`\n")
(rd / "TASK-s2.md").write_text("# TASK\n\n" + MC + "## Rule coverage\n\n- a missing date is skipped -> `test_a1`\n")
(rd / "frozen/src/test_a.py").write_text("def test_a1():\n    assert 1\n")
(base / "src/test_b.py").write_text("def test_b1():\n    assert 1\n")  # step 2: already frozen into baseline_from, no frozen/ copy

rc, out = lint(rd)
assert rc == 1 and "FAIL: TASK-s2.md: rule 1 names no test that src/test_b.py defines" in out and "TASK.md: rule" not in out, out

(rd / "TASK-s2.md").write_text("# TASK\n\n" + MC + "## Rule coverage\n\n- a missing date is skipped -> `test_b1`\n")
rc, out = lint(rd)
assert rc == 0 and "lint ok" in out and "Rule coverage" not in out, out

# no section: a WARN, lint still passes
(rd / "TASK-s2.md").write_text("# TASK\n\n" + MC)
rc, out = lint(rd)
assert rc == 0 and f"WARN: TASK-s2.md: {C.NO_SECTION}" in out, out

# the draft's marker still in the section: only the marker FAIL, no coverage FAIL on top
(rd / "TASK-s2.md").write_text("# TASK\n\n" + MC + f"## Rule coverage\n\n- {M.MARK}: one line per rule\n")
rc, out = lint(rd)
assert rc == 1 and "TASK-s2.md: 1 unconfirmed" in out and "names no test" not in out, out

# no frozen test anywhere yet: a WARN, not a FAIL
(rd / "TASK-s2.md").write_text("# TASK\n\n" + MC + "## Rule coverage\n\n- a missing date is skipped -> `test_b1`\n")
(base / "src/test_b.py").unlink()
rc, out = lint(rd)
assert rc == 0 and "WARN: TASK-s2.md: no frozen test src/test_b.py yet" in out, out

# a plain (one-step) spec
(rd / "TASK-s2.md").unlink()
(rd / "spec.json").write_text(json.dumps({"baseline_from": str(base), "allowed": ["src/calc.py"], "task": str(rd / "TASK.md"),
                                          "test_file": "src/test_a.py", "test_cmd": "x"}))
rc, out = lint(rd)
assert rc == 0 and "lint ok" in out, out
(rd / "frozen/src/test_a.py").write_text("def test_other():\n    assert 1\n")
rc, out = lint(rd)
assert rc == 1 and "FAIL: TASK.md: rule 1 names no test that src/test_a.py defines" in out, out

# ---- mini-validate: the step's task text (file, relative file, inline, unreadable, none) ----------------------------------
V = load("mini_validate_under_test", HERE / "mini-validate")
R = M.R
assert V.task_text({"task": str(rd / "TASK.md")}, rd / "spec.json", R).startswith("# TASK")
assert V.task_text({"task": "TASK.md"}, rd / "spec.json", R).startswith("# TASK")
assert V.task_text({"task": "Fix add.\nIt must reject None."}, rd / "spec.json", R) == "Fix add.\nIt must reject None."
assert V.task_text({"task": "missing.md"}, rd / "spec.json", R) == ""
assert V.task_text({}, rd / "spec.json", R) == ""
# a relative task is a file in the working tree (the step's copy of baseline_from) first, as the runner reads it
(base / "TASK.md").write_text("# repo TASK\n")
assert V.task_text({"task": "TASK.md"}, rd / "spec.json", R, base).startswith("# repo TASK")
assert V.task_text({"task": "TASK.md"}, rd / "spec.json", R, td / "nowhere").startswith("# TASK")  # not in the tree: next to the spec
assert V.task_text({"task": str(rd / "TASK.md")}, rd / "spec.json", R, base).startswith("# TASK")  # absolute: as is
assert V.load_coverage().check is not None

print("test_mini_rule_coverage: all ok")
