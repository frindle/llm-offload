#!/usr/bin/env python3
"""Behavioural test: verify-relevance must not count (a) TS/JS lines that were
only RE-INDENTED inside a hunk that also adds new lines, or (b) a hunk-revert of
a hunk made solely of one-line TS type aliases, as changed sites.

Origin: rt-giftcard-copy-to-remaining b5c740c54701 (2026-10-06) read relevance LOW
naming GiftCards.tsx:798/815 (pre-existing onChange lines re-indented into a new
wrapper div) and :294 (the move of `type DraftRow = ...`).

Also pins the anti-weakening guards: a hunk whose added lines are ALL re-indents
(an un-wrap) keeps them, and Python is never filtered.

Usage: test-relevance-reindent-typeonly.py [--target PATH-TO-verify-relevance.py]
(--target lets the revert test run this against the pre-fix backup.)
"""
import importlib.util, subprocess, sys, tempfile, textwrap
from pathlib import Path

target = Path(__file__).resolve().parent / "verify-relevance.py"
if "--target" in sys.argv:
    target = Path(sys.argv[sys.argv.index("--target") + 1])
import importlib.machinery
spec = importlib.util.spec_from_file_location(
    "vr", str(target), loader=importlib.machinery.SourceFileLoader("vr", str(target)))
vr = importlib.util.module_from_spec(spec)
sys.modules["vr"] = vr
spec.loader.exec_module(vr)

fails = 0


def check(name, cond, detail=""):
    global fails
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else f"  -- {detail}"))
    if not cond:
        fails += 1


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True).stdout


BEFORE = textwrap.dedent("""\
    export function helper(n: number): number {
      return n + 1;
    }

    export function render(rows: string[]) {
      type Row = { a: string };
      const out: string[] = [];
      for (const r of rows) {
        out.push(r.trim());
      }
      return out;
    }
    """)

AFTER = textwrap.dedent("""\
    type Row = { a: string };

    export function helper(n: number): number {
      return n + 1;
    }

    export function render(rows: string[]) {
      const out: string[] = [];
      for (const r of rows) {
        if (r.length > 0) {
          out.push(r.trim());
        }
      }
      return out;
    }
    """)

with tempfile.TemporaryDirectory() as td:
    wt = Path(td)
    git(wt, "init", "-q")
    git(wt, "config", "user.email", "t@t")
    git(wt, "config", "user.name", "t")
    (wt / "x.tsx").write_text(BEFORE)
    git(wt, "add", ".")
    git(wt, "commit", "-qm", "base")
    (wt / "x.tsx").write_text(AFTER)
    diff = git(wt, "diff", "-U0")
    files = vr.parse_unified_diff(diff)
    info = files["x.tsx"]
    lines = AFTER.split("\n")
    push_no = next(i + 1 for i, l in enumerate(lines) if "out.push" in l)
    guard_no = next(i + 1 for i, l in enumerate(lines) if "r.length > 0" in l)

    reind = getattr(vr, "reindent_only_added", None)
    check("reindent_only_added exists", reind is not None)
    got = reind(info["hunks"]) if reind else set()
    check("re-indented out.push (wrapped by a NEW guard) is not a changed site",
          push_no in got, f"push line {push_no} not in {sorted(got)}")
    check("the new guard line stays a changed site", guard_no not in got,
          f"{sorted(got)}")

    # generate(): no ts mutant may sit on the re-indented line; the guard still
    # gets mutants; no hunk-revert mutant comes from the type-alias-only hunks.
    ms, notes = vr.generate(wt, diff, [])
    on_push = [m for m in ms if m.lineno == push_no and m.klass != "hunk-revert"]
    on_guard = [m for m in ms if m.lineno == guard_no]
    check("generate: no mutant on the re-indented line", not on_push,
          [(m.klass, m.desc) for m in on_push])
    check("generate: the guard line still carries mutants", bool(on_guard),
          "no mutants on the guard -- filter over-reached")
    type_reverts = [m for m in ms if m.klass == "hunk-revert"
                    and (m.snippet.startswith("type Row") or m.snippet == "")]
    check("generate: no hunk-revert of a type-alias-only hunk", not type_reverts,
          [m.desc for m in type_reverts])

# anti-weakening: an UN-WRAP hunk (all added lines are re-indents) keeps them.
unwrap = textwrap.dedent("""\
    diff --git a/y.ts b/y.ts
    --- a/y.ts
    +++ b/y.ts
    @@ -2,3 +2 @@
    -  if (x) {
    -    foo();
    -  }
    +  foo();
    """)
if reind:
    h = vr.parse_unified_diff(unwrap)["y.ts"]["hunks"]
    check("un-wrap hunk (only re-indents) keeps its lines as sites", reind(h) == set(),
          f"{reind(h)}")

ttype = getattr(vr, "_is_ts_type_only", None)
check("_is_ts_type_only exists", ttype is not None)
if ttype:
    check("type alias line is type-only", ttype(["  type A = { a: string };"]))
    check("export type with generics is type-only", ttype(["export type B<T> = T[];"]))
    check("enum is NOT type-only (emits code)", not ttype(["enum E { A, B };"]))
    check("const is NOT type-only", not ttype(["const t = 1;"]))
    check("multi-line interface is NOT treated as type-only",
          not ttype(["interface I {", "  a: string;", "}"]))
    check("empty hunk is NOT type-only", not ttype(["", "  "]))

# Python: never filtered (indentation is semantics). generate() must not call the
# re-indent filter for .py -- verified by source inspection of the dispatch site.
src = target.read_text()
gen = src[src.index("def generate("):src.index("def _site_of(")]
py_branch = gen[gen.index('if rel.endswith(".py"):\n            ms += python_mutants'):]
py_branch = py_branch[:py_branch.index("elif rel.endswith(TS_EXTS)")]
check("python branch does not apply the re-indent filter",
      "reindent_only_added" not in py_branch)

print(f"--- {fails} failed ---")
sys.exit(1 if fails else 0)
