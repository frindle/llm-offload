#!/usr/bin/env python3
"""Plain tests for diag_check.py: python3 ~/bin/test_diag_check.py"""
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import diag_check as D  # noqa: E402

LIB = "\n".join([
    "'use strict';",
    "const fs = require('fs');",
    "",
    "function isPidAlive(pid) {",
    "  try { process.kill(pid, 0); return true; } catch { return false; }",
    "}",
    "",
    "function writeLock(file, holder) {",
    "  fs.writeFileSync(file, JSON.stringify(holder), { flag: 'wx' });",
    "}",
    "",
    "function acquireBrowserLock(file, holder) {",
    "  const cur = JSON.parse(fs.readFileSync(file, 'utf8'));",
    "  if (!isPidAlive(cur.pid)) {",
    "    // stale: break it",
    "    writeLock(file, holder);",
    "  }",
    "  return holder;",
    "}",
    "",
    "module.exports = { acquireBrowserLock };",
]) + "\n"
POLL = "const { acquireBrowserLock } = require('./lib');\nasync function handleCommand(c) {\n  await acquireBrowserLock('x', c);\n}\nmodule.exports = { handleCommand };\n"
# fix_quotes fixture (bibtexr2 5329a2a546bb shapes): a raw regex and quote-heavy strings; lines 16 and 18 share a key
SPLIT = r'''import re


def split(bibstr):
    marks = re.finditer(
        r"(?<!\\)[\{\}\",=\n]|@[\w]*( |\t)*(?={)", bibstr, re.MULTILINE
    )
    looking_for = None
    for mark in marks:
        if mark.group(0) == '"':
            looking_for = '`"`'
        elif mark.group(0) == "{":
            looking_for = "`}`"
        else:
            looking_for = "`,` or `}`"
    sep = "alpha, beta"
    if not sep:
        sep = 'alpha, beta'
    q = '"'
    return looking_for, sep, q
'''
SPLIT_FENCE = "```python sidecar/split.py:5-15\n" + "\n".join(SPLIT.splitlines()[4:15]) + "\n```\n"

GOOD = """# Diagnosis

## Code path

The stale lock is judged live because the pid check passes.

```js sidecar/lib.js:4-6
function isPidAlive(pid) {
  try { process.kill(pid, 0); return true; } catch { return false; }
}
```

```js sidecar/lib.js:12-18
function acquireBrowserLock(file, holder) {
  const cur = JSON.parse(fs.readFileSync(file, 'utf8'));
  if (!isPidAlive(cur.pid)) {
    // stale: break it
    writeLock(file, holder);
  }
  return holder;
}
```

```js sidecar/poll.js:2-4
async function handleCommand(c) {
  await acquireBrowserLock('x', c);
}
```

The wx write at sidecar/lib.js:9 throws EEXIST because the stale file is never unlinked.

## Root cause

acquireBrowserLock treats a stale holder by calling writeLock with flag wx without first unlinking the old file, so the write throws EEXIST.

## Second cause

handleCommand (sidecar/poll.js:2) lets the EEXIST propagate after the command was marked running, so the job is stuck.

## Minimal fix

- File: `sidecar/lib.js`
- Function: `acquireBrowserLock`

Unlink the stale lock file before writeLock, then retry once; keep the busy path for live holders unchanged.

## Regression test

Create a lock file whose pid is dead, call acquireBrowserLock and assert it returns the new holder instead of throwing EEXIST.
It must fail on the current code because the wx write hits the existing file.

## Not verified

I did not run the container; the pid-collision after restart is inferred from the task text, not observed in a live system.
"""


def sh(cwd, *a):
    subprocess.run(a, cwd=cwd, check=True, capture_output=True)


def mkrepo(d):
    d = Path(d)
    (d / "sidecar").mkdir(parents=True)
    (d / "sidecar/lib.js").write_text(LIB)
    (d / "sidecar/poll.js").write_text(POLL)
    sh(d, "git", "init", "-q")
    sh(d, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
    sh(d, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
    return d


SPEC = {"must_mention": ["acquireBrowserLock", "writeLock", "handleCommand"]}
n_ok = 0


def expect(name, ok, fails, want_ok, needle=None):
    global n_ok
    good = ok == want_ok and (needle is None or any(needle in f for f in fails))
    print(("ok   " if good else "FAIL ") + name + ("" if good else f"  -> ok={ok} fails={fails}"))
    if not good:
        sys.exit(1)
    n_ok += 1


def main():
    with tempfile.TemporaryDirectory() as t:
        repo = mkrepo(Path(t) / "r")
        rep = repo / "DIAGNOSIS.md"
        rep.write_text(GOOD)
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("good report passes", ok, fails, True)
        ok, fails = D.check(repo, "DIAGNOSIS.md", {"must_mention": ["nonexistentLiteral"]})
        expect("must_mention miss fails", ok, fails, False, "nonexistentLiteral")

        rep.write_text(GOOD.replace("    writeLock(file, holder);\n  }\n  return holder;", "    unlinkAndWrite(file, holder);\n  }\n  return holder;"))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("fabricated quote fails", ok, fails, False, "unlinkAndWrite")

        rep.write_text(GOOD.replace("sidecar/lib.js:12-18", "sidecar/lib.js:1-3"))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("wrong line range fails", ok, fails, False, "not found in lines")
        expect("a missed quote names where the line really is", ok, fails, False, "(it is at line 12)")
        rep.write_text(GOOD.replace("    writeLock(file, holder);\n  }\n  return holder;", "    unlinkAndWrite(file, holder);\n  }\n  return holder;"))
        expect("an invented quote says it is nowhere", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "(it is nowhere in sidecar/lib.js)")

        # elision lines (bench-dupes 5dc3adcb4f59: a `...` line inside a quote rejected an otherwise real citation)
        rep.write_text(GOOD.replace("    // stale: break it\n", "    // ...\n"))
        expect("a `// ...` elision line is skipped", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace("    // stale: break it\n", "    ...\n"))
        expect("a bare `...` elision line is skipped", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace("async function handleCommand(c) {\n  await acquireBrowserLock('x', c);\n}", "...\n# ... rest unchanged"))
        expect("a block of elisions only is empty", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "empty block")
        # abbreviated lines (optuna 141aee2f8f34: `raise ValueError(...)` stood for a real 4-line raise in the cited range, and
        # the gate called the report's last defect invented): kept text must be real, in order, from one line on
        rep.write_text(GOOD.replace("  await acquireBrowserLock('x', c);", "  await acquireBrowserLock(...);"))
        expect("an abbreviated call `f(...)` matches the real call", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        if_block = "  if (!isPidAlive(cur.pid)) {\n    // stale: break it\n    writeLock(file, holder);\n  }\n"
        assert GOOD.count(if_block) == 1
        rep.write_text(GOOD.replace(if_block, "  if (!isPidAlive(cur.pid)) { ... }\n"))
        expect("an elided block `if (...) { ... }` matches across lines", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace("  await acquireBrowserLock('x', c);", "  await releaseBrowserLock(...);"))
        expect("an abbreviated call of code that is not there is invented", *D.check(repo, "DIAGNOSIS.md", SPEC), False,
               "(it is nowhere in sidecar/poll.js)")
        rep.write_text(GOOD.replace("  return holder;\n}\n```", "  return holder; ... writeLock(file, holder);\n}\n```"))
        expect("elided pieces out of order do not match", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "quoted line not found")
        rep.write_text(GOOD.replace("  return holder;\n}\n```", "  r...;\n}\n```"))
        expect("an elision that keeps under 6 chars matches nothing", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "quoted line not found")

        # fix_ranges (bench-dupes: cited from an edit that was then restored, every range ~9 lines off; the quotes were real)
        drift = GOOD.replace("sidecar/lib.js:12-18", "sidecar/lib.js:20-21")
        rep.write_text(drift)
        expect("a drifted range fails the gate as is", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "(it is at line 12)")
        mv = D.fix_ranges(repo, "DIAGNOSIS.md")
        assert mv == [("sidecar/lib.js:20-21", "sidecar/lib.js:12-21")], mv
        assert "```js sidecar/lib.js:12-21\n" in rep.read_text() and rep.read_text().count("\n") == drift.count("\n"), rep.read_text()[:300]
        expect("fix_ranges moves it to the quoted lines, then the gate passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(drift.replace(if_block, "  if (!isPidAlive(cur.pid)) { ... }\n"))
        assert D.fix_ranges(repo, "DIAGNOSIS.md") == [("sidecar/lib.js:20-21", "sidecar/lib.js:12-21")], rep.read_text()[:400]
        expect("fix_ranges places an elided line too", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(drift)
        assert D.fix_ranges(repo, "DIAGNOSIS.md", reach=3) == [] and rep.read_text() == drift
        print("ok   fix_ranges leaves a line beyond reach alone")
        bad_drift = drift.replace("    writeLock(file, holder);\n  }\n  return holder;", "    unlinkAndWrite(file, holder);\n  }\n  return holder;")
        rep.write_text(bad_drift)
        assert D.fix_ranges(repo, "DIAGNOSIS.md") == [] and rep.read_text() == bad_drift
        print("ok   fix_ranges leaves a fence with an invented line alone")
        rep.write_text(GOOD)
        assert D.fix_ranges(repo, "DIAGNOSIS.md") == [] and rep.read_text() == GOOD
        print("ok   fix_ranges leaves a correct report byte-identical")

        # fix_quotes (bibtexr2 5329a2a546bb: all three attempts were rejected for lines the model had read, a raw regex copied
        # with one ' too many and `looking_for = '`"`'` with one backtick dropped)
        qrepo = mkrepo(Path(t) / "q")
        (qrepo / "sidecar/split.py").write_text(SPLIT)
        sh(qrepo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
        sh(qrepo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "split")
        qrep = qrepo / "DIAGNOSIS.md"
        QGOOD = GOOD.replace("The wx write at", SPLIT_FENCE + "\nThe wx write at", 1)
        regex = SPLIT.splitlines()[5]
        lf = SPLIT.splitlines()[10]
        far_lf = SPLIT.splitlines()[14]
        assert QGOOD != GOOD and regex.endswith("re.MULTILINE") and lf.endswith("'`\"`'") and far_lf.endswith('or `}`"')
        assert all(QGOOD.count(x + "\n") == 1 for x in (regex, lf, far_lf))
        qrep.write_text(QGOOD)
        expect("a quote-heavy citation copied exactly passes", *D.check(qrepo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_quotes(qrepo, "DIAGNOSIS.md") == [] and qrep.read_text() == QGOOD
        print("ok   fix_quotes leaves a correct report byte-identical")
        qrep.write_text(QGOOD.replace(regex + "\n", regex.replace(r'\",', r"\"',") + "\n"))
        expect("a regex copied with one ' too many fails as is", *D.check(qrepo, "DIAGNOSIS.md", SPEC), False,
               "(it is nowhere in sidecar/split.py)")
        fx = D.fix_quotes(qrepo, "DIAGNOSIS.md")
        assert [(c, new) for c, _old, new in fx] == [("sidecar/split.py:5-15", regex)] and "\"'," in fx[0][1], fx
        assert qrep.read_text() == QGOOD, qrep.read_text()[:900]
        expect("fix_quotes restores the regex, then the gate passes", *D.check(qrepo, "DIAGNOSIS.md", SPEC), True)
        qrep.write_text(QGOOD.replace(lf + "\n", lf[:-2] + "'\n"))
        expect("a dropped backtick fails as is", *D.check(qrepo, "DIAGNOSIS.md", SPEC), False, "looking_for = ")
        assert [new for _c, _old, new in D.fix_quotes(qrepo, "DIAGNOSIS.md")] == [lf] and qrep.read_text() == QGOOD
        expect("fix_quotes restores it from the one line with an equal key (three lines contain it)",
               *D.check(qrepo, "DIAGNOSIS.md", SPEC), True)
        para = QGOOD.replace("    marks = re.finditer(\n", "    matches = re.finditer(\n", 1)
        qrep.write_text(para)
        assert D.fix_quotes(qrepo, "DIAGNOSIS.md") == [] and qrep.read_text() == para
        expect("fix_quotes leaves a paraphrase for the gate", *D.check(qrepo, "DIAGNOSIS.md", SPEC), False, "matches = re.finditer(")
        for name, extra, needle in (("a line two different source lines match", "    sep = \"alpha, beta'", 'sep = "alpha, beta'),
                                    ("a line with under 10 chars of text", "    q = \"'\"", 'q = "')):
            odd = QGOOD.replace(far_lf + "\n", far_lf + "\n" + extra + "\n", 1)
            qrep.write_text(odd)
            assert D.fix_quotes(qrepo, "DIAGNOSIS.md") == [] and qrep.read_text() == odd, name
            expect(f"fix_quotes leaves {name} for the gate", *D.check(qrepo, "DIAGNOSIS.md", SPEC), False, needle)
        far = QGOOD.replace("sidecar/split.py:5-15", "sidecar/split.py:5-9", 1).replace(far_lf + "\n", far_lf.replace("`}`", "}`") + "\n", 1)
        qrep.write_text(far)
        assert [new for _c, _old, new in D.fix_quotes(qrepo, "DIAGNOSIS.md")] == [far_lf]
        assert D.fix_ranges(qrepo, "DIAGNOSIS.md") == [("sidecar/split.py:5-9", "sidecar/split.py:5-15")]
        expect("fix_quotes then fix_ranges repair a mis-copied line beyond the cited window", *D.check(qrepo, "DIAGNOSIS.md", SPEC), True)
        prop = QGOOD.replace("Unlink the stale lock file", "```python sidecar/split.py:6\n" + regex.replace(r'\",', r"\"',")
                             + "\n```\n\nUnlink the stale lock file", 1)
        assert prop != QGOOD and prop.index("sidecar/split.py:6") > prop.index("## Minimal fix")
        qrep.write_text(prop)
        assert D.fix_quotes(qrepo, "DIAGNOSIS.md") == [] and qrep.read_text() == prop
        expect("fix_quotes leaves a Minimal fix fence (it may hold the proposed code) for the gate",
               *D.check(qrepo, "DIAGNOSIS.md", SPEC), False, "(it is nowhere in sidecar/split.py)")
        L = prop.split("\n").index("```python sidecar/split.py:6") + 1
        assert D.fix_proposed(qrepo, "DIAGNOSIS.md") == [f"sidecar/split.py:6 (report line {L})"]
        now = qrep.read_text()
        assert "Proposed or paraphrased code, not quoted from the repo (it was cited as `sidecar/split.py`, line(s) 6):\n```python\n" in now, now[-900:]
        expect("fix_proposed un-cites a Minimal fix fence of proposed code, then the gate passes", *D.check(qrepo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_proposed(qrepo, "DIAGNOSIS.md") == [] and qrep.read_text() == now
        realq = QGOOD.replace("Unlink the stale lock file", "```python sidecar/split.py:6\n" + regex + "\n```\n\nUnlink the stale lock file", 1)
        qrep.write_text(realq)
        assert D.fix_proposed(qrepo, "DIAGNOSIS.md") == [] and qrep.read_text() == realq
        expect("fix_proposed leaves a Minimal fix fence of real lines", *D.check(qrepo, "DIAGNOSIS.md", SPEC), True)
        ff = prop.replace("I did not run the container;", "I did not run\x0c the container;", 1)
        qrep.write_text(ff)
        assert ff != prop and D.fix_proposed(qrepo, "DIAGNOSIS.md") == [] and qrep.read_text() == ff
        print("ok   fix_proposed leaves a report whose line separators split() and splitlines() disagree on")

        # close_fences (bench-dupes2 7938a1ad1fec attempt 3: a script wrote the openers but one closer; every heading after the
        # second quote sat inside a code block, and the gate said only "missing section" for headings the model could grep)
        isp = "  try { process.kill(pid, 0); return true; } catch { return false; }\n}\n"
        poll = "  await acquireBrowserLock('x', c);\n}\n"
        open2 = GOOD.replace(isp + "```\n", isp).replace(poll + "```\n", poll)
        assert open2 != GOOD and open2.count("```") == GOOD.count("```") - 2
        rep.write_text(open2)
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("an unclosed fence is named first, with the headings it swallows", ok, fails, False, "is never closed, so '## Root cause'")
        assert fails[0].startswith("the fence opened at report line ") and "sidecar/poll.js:2-4" in fails[0] and "2 more heading(s)" in fails[0], fails[0]
        cl = D.close_fences(repo, "DIAGNOSIS.md")
        assert cl == ["sidecar/lib.js:4-6 (report line 7)", "sidecar/poll.js:2-4 (report line 24)"], cl
        assert rep.read_text() == GOOD, rep.read_text()[:600]
        expect("close_fences closes each after its cited lines: the report is the closed one and passes",
               *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        assert D.close_fences(repo, "DIAGNOSIS.md") == [] and rep.read_text() == GOOD
        print("ok   close_fences leaves a closed report byte-identical")
        bad_open = open2.replace("  await acquireBrowserLock('x', c);", "  await acquireBrowserLock(x);")
        rep.write_text(bad_open)
        assert D.close_fences(repo, "DIAGNOSIS.md") == ["sidecar/lib.js:4-6 (report line 7)"] and "sidecar/poll.js:2-4\nasync" in rep.read_text()
        assert rep.read_text().count("```") == bad_open.count("```") + 1
        print("ok   close_fences leaves a fence whose lines are not the file's alone (the gate names it)")
        short = open2.replace(isp, "}\n")
        rep.write_text(short)
        assert all("lib.js:4-6" not in x for x in D.close_fences(repo, "DIAGNOSIS.md"))
        print("ok   close_fences: fewer verbatim lines than the range names -> left open")
        eof = GOOD.split("## Root cause")[0] + "```text\n## Root cause\n"
        rep.write_text(eof)
        assert D.close_fences(repo, "DIAGNOSIS.md") == [] and rep.read_text() == eof
        print("ok   close_fences: a fence without a citation info string is left alone")
        tail = GOOD.rstrip("\n") + "\n\n```js sidecar/lib.js:8-10\nfunction writeLock(file, holder) {\n" \
            "  fs.writeFileSync(file, JSON.stringify(holder), { flag: 'wx' });\n}\n"
        rep.write_text(tail)
        assert D.close_fences(repo, "DIAGNOSIS.md") == ["sidecar/lib.js:8-10 (report line 56)"] and rep.read_text().endswith("}\n```\n"), rep.read_text()[-80:]
        print("ok   close_fences: a fence open at the end of the file is closed after its lines")

        rep.write_text(GOOD.replace("sidecar/lib.js:9", "sidecar/lib.js:900"))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("bare mention past EOF fails", ok, fails, False, "beyond end")

        rep.write_text(GOOD.replace("## Second cause", "## Other"))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("missing section fails", ok, fails, False, "missing section '## Second cause'")

        rep.write_text(GOOD.replace("Function: `acquireBrowserLock`", "Function: `nope`"))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("fix function not defined fails", ok, fails, False, "does not define function nope")
        assert not any("defined in" in f or "not source code" in f for f in fails), fails

        # cfnlint r2 b493e7708c88: File: a JSON schema, Function: CfnInit.validate (defined in a .py rule): rejected 3 times as
        # "does not define" with no word on where the function is or that the fix changes only code
        (repo / "pkg").mkdir()
        (repo / "pkg/rules.py").write_text("class CfnInit:\n    def validate(self, v):\n        return v\n")
        (repo / "pkg/other.py").write_text("def validate(v):\n    return v\nvalidate_all = None\n")
        (repo / "pkg/schema.json").write_text('{"validate": {"CfnInit": true}}\n')
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "pkg")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "pkg")
        assert D._defined_in(repo, "CfnInit.validate") == ["pkg/rules.py"], D._defined_in(repo, "CfnInit.validate")
        assert D._defined_in(repo, "validate") == ["pkg/other.py", "pkg/rules.py"], D._defined_in(repo, "validate")
        assert D._defined_in(repo, "other.validate") == ["pkg/other.py"] and D._defined_in(repo, "Nope.validate") == []
        assert D._defined_in(repo, "fs") == [] and D._defined_in(repo, "acquireBrowserLock", skip="sidecar/lib.js") == []
        print("ok   _defined_in: dotted names need the owner (class or module stem); values and the skipped file are not hits")
        fn0 = "Function: `acquireBrowserLock`"
        rep.write_text(GOOD.replace("- File: `sidecar/lib.js`", "- File: `pkg/schema.json`").replace(fn0, "Function: `CfnInit.validate`"))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("a JSON File: names where the function is defined", ok, fails, False,
               "pkg/schema.json does not define function CfnInit.validate (CfnInit.validate is defined in pkg/rules.py: name that file on the File: line)")
        expect("a JSON File: says the fix changes only code", ok, fails, False,
               "; pkg/schema.json is not source code, and the fix changes only code: name the function whose code makes the wrong decision")
        rep.write_text(GOOD.replace(fn0, "Function: `handleCommand`"))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("a function defined in another source file is named", ok, fails, False,
               "sidecar/lib.js does not define function handleCommand (handleCommand is defined in sidecar/poll.js: name that file on the File: line)")
        assert not any("not source code" in f for f in fails), fails
        rep.write_text(GOOD.replace(fn0, fn0 + "\n- Function: `validate`"))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("several definitions are counted; with two Function: lines the File: must define both", ok, fails, False,
               "(validate is defined in 2 tracked files: pkg/other.py, pkg/rules.py: the File: line names the file that defines every "
               "Function: line's function)")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "rm", "-rq", "pkg")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "no pkg")

        # multi-function targets (bench-dupes: one fault copied in a radarr and a sonarr twin; `(and `b`)` was rejected as 0 functions)
        fn = "- Function: `acquireBrowserLock`"
        rep.write_text(GOOD.replace(fn, fn + "\n- Function: `writeLock`"))
        expect("two Function lines (one fault in parallel functions) pass", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_target("- File: `sidecar/lib.js`\n" + fn + " (and `writeLock`)\n") == (["sidecar/lib.js"], ["acquireBrowserLock"], ["writeLock"])
        rep.write_text(GOOD.replace(fn, fn + " (and `writeLock`)"))
        expect("an `(and `b`)` tail names a second target and passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_target(fn + " (specifically the `writeLock` call)\n")[1:] == (["acquireBrowserLock"], [])
        rep.write_text(GOOD.replace(fn, fn + " (specifically the `writeLock` call)"))
        expect("a parenthetical comment after the name is not a target", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace("- File: `sidecar/lib.js`", "- **File:** `sidecar/lib.js`").replace(fn, "- **Function:** `acquireBrowserLock`"))
        expect("bold File/Function labels pass", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace(fn, fn + "\n- Function: `nope`"))
        expect("an undefined second Function line fails", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "does not define function nope")
        rep.write_text(GOOD.replace(fn, fn + "\n- Function: `writeLock`\n- Function: `isPidAlive` (and `handleCommand`)"))
        expect("three targets pass; an extra the file does not define is ignored", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_names(["acquireBrowserLock", "writeLock", "isPidAlive"], ["handleCommand"], LIB) == ["acquireBrowserLock", "writeLock", "isPidAlive"]
        rep.write_text(GOOD.replace(fn, fn + "\n" + fn))
        expect("a repeated name counts once", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace("- File: `sidecar/lib.js`", "- File: `sidecar/lib.js`\n- File: `sidecar/poll.js`"))
        expect("two File lines fail, and the message says the fix changes one file", *D.check(repo, "DIAGNOSIS.md", SPEC), False,
               "Minimal fix names 2 files (sidecar/lib.js, sidecar/poll.js): the fix changes ONE file only. Keep one 'File: `path`' line")
        rep.write_text(GOOD.replace("- File: `sidecar/lib.js`", "- File: `sidecar/lib.js`\n- File: `sidecar/poll.js`").replace(fn + "\n", ""))
        expect("two File lines and no Function line: the one-file message", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "names 2 files (")
        rep.write_text(GOOD.replace("- File: `sidecar/lib.js`\n", ""))
        expect("no File line fails", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "(found 0 file, 1 function)")
        # bench-dupes2 7938a1ad1fec: a File: line above each of two Function: lines (one file) was rejected 3 times as "2 file"
        rep.write_text(GOOD.replace(fn, fn + "\n- File: `./sidecar/lib.js`\n- Function: `writeLock`"))
        expect("a File line repeated per Function line (same file) passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_target("- File: `a.js`\n- Function: `f`\n- File: `./a.js`\n- Function: `g`\n") == (["a.js"], ["f", "g"], [])
        assert D.fix_target("- File: `a.js`\n- Function: `f`\n- File: `b.js`\n- Function: `g`\n")[0] == ["a.js", "b.js"]
        print("ok   fix_target: a repeated File path is said once, two paths stay two")
        rep.write_text(GOOD.replace(fn + "\n", ""))
        expect("no Function line fails", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "one 'Function: `name`' line per function")
        (repo / "sidecar/lib.js").write_text(LIB + "function releaseLock(file) {\n  fs.unlinkSync(file);\n}\n")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "4th fn")
        rep.write_text(GOOD.replace(fn, fn + "\n- Function: `writeLock`\n- Function: `isPidAlive`\n- Function: `releaseLock`"))
        expect(f"four targets fail (max {D.MAX_FNS})", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "at most 3")
        (repo / "sidecar/lib.js").write_text(LIB)
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qam", "back")

        # module-level values (cfgkeys 2cdee16a8b71: `Function: DEFAULTS` named a dict of known keys; the fix edits the dict)
        rep.write_text(GOOD.replace(fn, "- Function: `fs`"))
        expect("a module-level value on the Function line fails with the Constant hint", *D.check(repo, "DIAGNOSIS.md", SPEC), False,
               "fs is a module-level value in sidecar/lib.js, not a function: name it on a '- Constant: `fs`' line")
        rep.write_text(GOOD.replace(fn, fn + "\n- Constant: `fs`"))
        expect("a Constant line the file assigns at top level passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace(fn, fn + "\n- Constant: `nope`"))
        expect("a Constant the file does not assign fails", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "assigns no module-level value nope")
        rep.write_text(GOOD.replace(fn, "- Constant: `fs`"))
        expect("a Constant without a Function line fails with the reader hint", *D.check(repo, "DIAGNOSIS.md", SPEC), False,
               "the Function: line names the function whose decision reads that value")
        assert D.fix_consts("- File: `a.py`\n- Function: `f`\n- **Constant:** `A` (and `B`)\n- Constant: `m.C`\n") == ["A", "B", "C"]
        assert D.fix_consts(GOOD) == []
        assert D.fix_target("- File: `a.py`\n- Function: `f`\n- Constant: `A`\n") == (["a.py"], ["f"], [])
        py = "import re\nDEFAULTS = {\n    'a': 1,\n}\nKNOWN: dict = {}\nf = lambda x: x\nX == 1\ndef g():\n    Y = 2\n"
        assert D.is_value(py, "DEFAULTS") and D.is_value(py, "KNOWN") and not D.is_value(py, "f") and not D.is_value(py, "g")
        assert not D.binds_top(py, "X") and not D.binds_top(py, "Y") and D.binds_top(py, "f")
        assert D.is_value("const fs = require('fs');\n", "fs") and not D.is_value("const h = async (r) => r;\n", "h")
        assert not D.is_value("export const run = function () {};\n", "run") and D.is_value("export const KEYS = new Set();\n", "KEYS")
        rep.write_text(GOOD)

        rep.write_text(GOOD.replace("ONE", "").replace("so the write throws EEXIST.", "so the write throws EEXIST. It also hangs the queue."))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("two-sentence root cause fails", ok, fails, False, "ONE sentence")

        # dupes3 69c0a9690916: a correct 404-char Root cause, 72 chars of it `(line N)` references, was rejected; line
        # references are not counted toward ROOT_MAX, prose still is
        rc0 = "acquireBrowserLock treats a stale holder by calling writeLock with flag wx without first unlinking the old file, so the write throws EEXIST."
        refs = ("acquireBrowserLock (lines 3-12) treats a stale holder, one whose pid is gone or whose heartbeat is older than the lease, "
                "by calling writeLock (line 9) with flag wx without first unlinking the old lock file that the dead process left behind "
                "(lines 5-7 / line 9), so the exclusive-create write throws EEXIST on every poll and the command that asked for the "
                "browser stays running until someone removes the file (sidecar/lib.js:9).")
        assert len(refs) > D.ROOT_MAX >= D.root_len(refs), (len(refs), D.root_len(refs))
        rep.write_text(GOOD.replace(rc0, refs))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("line references do not count toward the Root cause length", ok, fails, True)
        prose = D.LINE_REF_RE.sub("", refs)[:-1] + ", because nothing else ever removes it and every later poll trips over it again."
        # real-issue bench 2026-11-05/06: 9 reports were rejected at 401-511 chars (sqlmetar3 f28c586f4214: 511), each a full
        # re-diagnosis; the gate allows ROOT_GATE while the contract still asks for ROOT_MAX, and line references still do not count
        assert D.ROOT_MAX < D.root_len(prose) <= D.ROOT_GATE, D.root_len(prose)
        rep.write_text(GOOD.replace(rc0, prose))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("a Root cause over ROOT_MAX but within ROOT_GATE passes", ok, fails, True)
        lrefs = " (lines 3-12 / line 9 / lines 20-31 / line 40 / lines 55-60 / line 61 / lines 70-79 / line 80)"
        refs2 = prose[:-1] + lrefs + lrefs + "."
        assert len(refs2) > D.ROOT_GATE >= D.root_len(refs2), (len(refs2), D.root_len(refs2))
        rep.write_text(GOOD.replace(rc0, refs2))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("line references do not count toward ROOT_GATE either", ok, fails, True)
        long = (prose[:-1] + ", and the operator who finally deletes the file by hand gets no message saying which pid held it or how "
                "old its heartbeat was, so the same stall comes back after the next crash of the sidecar.")
        assert D.root_len(long) > D.ROOT_GATE, D.root_len(long)
        rep.write_text(GOOD.replace(rc0, long))
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("a Root cause over ROOT_GATE fails, naming both limits", ok, fails, False,
               f"Root cause is {D.root_len(long)} chars (> {D.ROOT_GATE}): one sentence of at most {D.ROOT_MAX}")
        assert D.root_len("year (2019) and f(x) stay") == len("year (2019) and f(x) stay")
        assert D.root_len("a (lines 2643 and 2952) b") == len("a b") and D.root_len("a (app.py:3, lib/b.js:4-6) b") == len("a b")
        rep.write_text(GOOD)

        rep.write_text(GOOD)
        (repo / "sidecar/lib.js").write_text(LIB + "// edit\n")
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("tracked source edit fails", ok, fails, False, "tracked files changed")
        sh(repo, "git", "checkout", "--", "sidecar/lib.js")

        (repo / "extra.js").write_text("x\n")
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect("untracked extra file fails", ok, fails, False, "untracked files")
        (repo / "extra.js").unlink()

        rep.write_text("Let me write the diagnosis now, here is what I found in the code and all of it follows below.\n")
        n = len(rep.read_bytes())
        ok, fails = D.check(repo, "DIAGNOSIS.md", SPEC)
        expect(f"{n}-byte narration file fails", ok, fails, False, "narration")
        assert any("bytes" in f for f in fails), fails

        expect("missing file fails", *D.check(repo, "nope.md", SPEC), False, "does not exist")

        # path resolution + retry hints (diag-clamshell-web 542f8970edcd: `client.js:179` rejected, `sideclient/` never corrected)
        rep.write_text(GOOD.replace("sidecar/lib.js:9", "lib.js:9"))
        expect("bare basename mention of a unique tracked file passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace("sidecar/lib.js:9", "lib.js:900"))
        expect("bare basename mention past EOF still fails", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "beyond end")
        rep.write_text(GOOD.replace("sidecar/lib.js:9", "sideclient/lib.js:9"))
        expect("wrong-prefix mention fails and names the real path", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "named lib.js: sidecar/lib.js")
        rep.write_text(GOOD.replace("```js sidecar/lib.js:4-6", "```js sideclient/lib.js:4-6"))
        expect("wrong-prefix citation stays strict and names the real path", *D.check(repo, "DIAGNOSIS.md", SPEC), False,
               "citation sideclient/lib.js:4-6 (report line 7): file does not exist in the repo (use the full repo-relative path; tracked files named lib.js: sidecar/lib.js)")
        rep.write_text(GOOD.replace("sidecar/lib.js:9", "ghost.js:9"))
        expect("mention of a name no file has says so", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "no tracked file is named ghost.js")
        # placeholder and wrong-directory citation paths resolve by their quoted lines (dmcg1 r2 222308c65fb9)
        rep.write_text(GOOD.replace("```js sidecar/poll.js:2-4", "```js <path>:2-4"))
        expect("a <path> placeholder citation fails as is", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "citation <path>:2-4")
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("<path>", "sidecar/poll.js")] and rep.read_text() == GOOD, rep.read_text()[:900]
        rep.write_text(GOOD.replace("```js sidecar/lib.js:12-18", "```js src/sidecar/lib.js:12-18"))
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("src/sidecar/lib.js", "sidecar/lib.js")] and rep.read_text() == GOOD
        fixfence = GOOD.replace("Unlink the stale lock file", "```js <path>:12-13\n" + "\n".join(LIB.splitlines()[11:13]) + "\n```\n\nUnlink the stale lock file", 1)
        rep.write_text(fixfence)
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("<path>", "sidecar/lib.js")] and rep.read_text() == fixfence.replace("<path>:12-13", "sidecar/lib.js:12-13")
        expect("a Minimal fix fence of real lines under <path> resolves, and the gate passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        one = GOOD.replace("Unlink the stale lock file", "```js <path>:18-19\n  return holder;\n}\n```\n\nUnlink the stale lock file", 1)
        rep.write_text(one)
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [] and rep.read_text() == one
        print("ok   fix_paths resolves <path> and wrong-directory citations by their quoted lines; one strong line is not enough")
        rep.write_text(GOOD)
        assert D.fix_proposed(repo, "DIAGNOSIS.md") == [] and rep.read_text() == GOOD
        rt = GOOD.replace("It must fail on the current code because the wx write hits the existing file.\n",
                          "It must fail on the current code because the wx write hits the existing file.\n\n```js <path>:1-3\n"
                          "const lock = acquireBrowserLock(staleFile, holder);\nassert.deepStrictEqual(lock, holder);\n```\n", 1)
        rep.write_text(rt)
        assert rt != GOOD and D.fix_paths(repo, "DIAGNOSIS.md") == [] and rep.read_text() == rt
        out = D.fix_proposed(repo, "DIAGNOSIS.md")
        assert len(out) == 1 and out[0].startswith("<path>:1-3 (report line "), out
        expect("a Regression test fence of new test code under <path> is un-cited, then the gate passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        inv = GOOD.replace("    writeLock(file, holder);\n", "    unlinkAndWrite(file, holder);\n", 1)
        rep.write_text(inv)
        assert inv != GOOD and D.fix_proposed(repo, "DIAGNOSIS.md") == [] and rep.read_text() == inv
        expect("fix_proposed never touches an invented Code path line", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "unlinkAndWrite")
        (repo / "other").mkdir()
        (repo / "other/lib.js").write_text(LIB)
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "other/lib.js")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "dup")
        amb = GOOD.replace("```js sidecar/lib.js:4-6", "```js <path>:4-6")
        rep.write_text(amb)
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [] and rep.read_text() == amb
        print("ok   fix_paths leaves a <path> citation whose lines two files hold")
        # shortened paths the report itself disambiguates (scrapy r2 5f2fbdc72e8e: D1 was rejected for `crawl.py:33` beside its own
        # citations of scrapy/commands/crawl.py, in a repo with a second crawl.py; isort r2: citations `python output.py:362-369`)
        rep.write_text(GOOD.replace("sidecar/lib.js:9", "lib.js:9"))
        expect("an ambiguous short mention resolves to the one candidate the report names in full", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("lib.js", "sidecar/lib.js")] and rep.read_text() == GOOD, rep.read_text()[:600]
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [] and rep.read_text() == GOOD
        print("ok   fix_paths rewrites it to the full path, then leaves the report byte-identical")
        for u in ("u1", "u2"):
            (repo / u).mkdir()
            (repo / u / "util.js").write_text("x\n")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "u1/util.js", "u2/util.js")
        sh(repo, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "two utils")
        rep.write_text(GOOD.replace("sidecar/lib.js:9", "sidecar/lib.js:9 (util.js:1)"))
        expect("an ambiguous short mention the report never names in full fails and lists both", *D.check(repo, "DIAGNOSIS.md", SPEC),
               False, "tracked files named util.js: u1/util.js, u2/util.js")
        both = GOOD.replace("sidecar/lib.js:9", "sidecar/lib.js:9 (util.js:1, from u1/util.js or u2/util.js)")
        rep.write_text(both)
        expect("one the report names both candidates of in full stays ambiguous", *D.check(repo, "DIAGNOSIS.md", SPEC), False,
               "mention util.js:1: file does not exist")
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [] and rep.read_text() == both
        print("ok   fix_paths leaves an unresolved path alone")
        rep.write_text(GOOD.replace("```js sidecar/poll.js:2-4", "```js poll.js:2-4"))
        expect("a citation of a unique short path is checked against that file", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("poll.js", "sidecar/poll.js")] and rep.read_text() == GOOD
        rep.write_text(GOOD.replace("```js sidecar/lib.js:4-6", "```js lib.js:4-6"))
        expect("a citation of an ambiguous short path the report names in full passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("lib.js", "sidecar/lib.js")] and rep.read_text() == GOOD
        print("ok   fix_paths rewrites citation info strings")
        sdrift = GOOD.replace("```js sidecar/lib.js:12-18", "```js ./lib.js:20-21")
        rep.write_text(sdrift)
        assert D.fix_ranges(repo, "DIAGNOSIS.md") == [] and rep.read_text() == sdrift  # it needs the real path
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("./lib.js", "sidecar/lib.js")]
        assert D.fix_ranges(repo, "DIAGNOSIS.md") == [("sidecar/lib.js:20-21", "sidecar/lib.js:12-21")]
        expect("fix_paths then fix_ranges repair a short, drifted citation", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        rep.write_text(GOOD.replace("- File: `sidecar/lib.js`", "- File: `lib.js`"))
        expect("a short Minimal fix File: fails as is (auto() reads that value)", *D.check(repo, "DIAGNOSIS.md", SPEC), False,
               "Minimal fix file lib.js does not exist")
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("lib.js", "sidecar/lib.js")] and rep.read_text() == GOOD
        expect("fix_paths rewrites the File: value and the report passes", *D.check(repo, "DIAGNOSIS.md", SPEC), True)
        keep = GOOD.replace("## Not verified\n", "## Not verified\n\nFile: `poll.js`\n\n```text\nsee lib.js:9\n```\n")
        rep.write_text(keep)
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [] and rep.read_text() == keep
        print("ok   fix_paths leaves quoted code and a File: line outside Minimal fix alone")
        rep.write_text(open2.replace("```js sidecar/lib.js:4-6", "```js lib.js:4-6"))
        assert D.close_fences(repo, "DIAGNOSIS.md") == ["lib.js:4-6 (report line 7)", "sidecar/poll.js:2-4 (report line 24)"]
        assert D.fix_paths(repo, "DIAGNOSIS.md") == [("lib.js", "sidecar/lib.js")] and rep.read_text() == GOOD
        print("ok   close_fences closes a fence cited by a short path; fix_paths then gives the closed report")
        assert D._named("a/b.py", "see `a/b.py:3`") and D._named("a/b.py", "(./a/b.py)") and D._named("a/b.py", "a/b.py.")
        assert not D._named("a/b.py", "x/a/b.py:3") and not D._named("a/b.py", "a/b.pyc") and not D._named("a/b.py", "a/b.py.bak")
        print("ok   _named: a full path, not the tail of a longer one")
        # the six real D1 layouts (tracked files modeled on scrapy r2, canvas, conan2, moto, mcpsdk and isort r2)
        lays = [("scrapy", ["scrapy/commands/crawl.py", "scrapy/spiders/crawl.py"], "crawl.py",
                 "```python scrapy/commands/crawl.py:30-34", "scrapy/commands/crawl.py"),
                ("canvas", ["canvasapi/util.py", "tests/util.py"], "util.py", "- File: `canvasapi/util.py`", "canvasapi/util.py"),
                ("conan2", ["conan/internal/graph/graph.py", "conans/client/graph/graph.py"], "graph.py",
                 "the edge is built at conan/internal/graph/graph.py:139", "conan/internal/graph/graph.py"),
                ("moto", ["moto/dynamodb/models/__init__.py", "moto/s3/models/__init__.py"], "models/__init__.py",
                 "```python moto/dynamodb/models/__init__.py:734-759", "moto/dynamodb/models/__init__.py"),
                ("moto", ["moto/dynamodb/responses.py", "moto/s3/responses.py", "moto/sqs/responses.py"], "responses.py",
                 "```python moto/dynamodb/responses.py:1452-1460", "moto/dynamodb/responses.py"),
                ("mcpsdk", ["src/mcp/server/fastmcp/server.py", "src/mcp/server/lowlevel/server.py"], "server.py",
                 "```python src/mcp/server/fastmcp/server.py:367-380", "src/mcp/server/fastmcp/server.py"),
                ("isort", ["isort/output.py", "isort/wrap.py", "isort/wrap_modes.py"], "output.py", "", "isort/output.py"),
                ("isort", ["isort/output.py", "isort/wrap.py", "isort/wrap_modes.py"], "wrap.py", "", "isort/wrap.py")]
        for name, tracked, short_p, said, want in lays:
            with tempfile.TemporaryDirectory() as t2:
                lr = Path(t2)
                for f in tracked:
                    (lr / f).parent.mkdir(parents=True, exist_ok=True)
                    (lr / f).write_text("x\n")
                sh(lr, "git", "init", "-q")
                sh(lr, "git", "-c", "user.email=t@t", "-c", "user.name=t", "add", ".")
                sh(lr, "git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base")
                got = D._resolve_mention(lr, short_p, {}, f"{said}\nthe check at {short_p}:33 is skipped\n")
                assert got == want, (name, short_p, got)
                if len(tracked) > 1 and said:
                    assert D._resolve_mention(lr, short_p, {}, f"the check at {short_p}:33 is skipped\n") is None, (name, short_p)
        print(f"ok   the {len(lays)} real layouts resolve; the ambiguous ones only with the full path in the report")
        rep.write_text(GOOD.replace("sidecar/lib.js:9", "sidecar/lib.js:9 and r/sidecar/lib.js:9"))
        expect("prefix beyond the repo root does not resolve", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "mention r/sidecar/lib.js:9")
        rep.write_text(GOOD.replace("sidecar/lib.js:9", "sidecar/lib.js:9 and App/Ghost.swift:12"))
        expect("bare .swift mention is checked", *D.check(repo, "DIAGNOSIS.md", SPEC), False, "Ghost.swift")

        # scorer
        rep.write_text(GOOD)
        exp = {"facts": {"A": {"path": "sidecar/lib.js", "lines": [14, 17], "terms": [["stale"], ["break", "unlink"], ["wx", "EEXIST"]]},
                         "B": {"path": "sidecar/poll.js", "lines": [3, 3], "terms": [["rethrow", "propagate"], ["running"]]},
                         "C": {"path": "sidecar/poll.js", "lines": [300, 310], "terms": []}}}
        sc = D.score(rep, exp)
        assert sc["A"][0] and sc["B"][0] and not sc["C"][0], sc
        print("ok   scorer hits A and B, misses C")

        # old wrong report as negative control, only if it still exists
        old = Path("/Users/user/dispatch-worktrees/rt-browser-lock-diag-48e49f12/DIAGNOSIS.md")
        if old.is_file():
            ok, fails = D.check("/Users/user/dispatch-worktrees/rt-browser-lock-diag-48e49f12", "DIAGNOSIS.md", SPEC)
            expect("OLD wrong report fails (negative control)", ok, fails, False)
        else:
            print("skip old report no longer exists")
        # cited_lines (loguru r3 bf6183725d19): lines the fault sections point at, plain `lines N` going to the nearest path
        # mention before them; Code path quotes, other files, ranges of 10+ lines and unattributed refs are left out
        lrep = ("## Code path\n\n```python loguru/_handler.py:158-165\nx\n```\n\n"
                "## Root cause\n\nIn `Handler.emit()` (`loguru/_handler.py:127`), all `format_map(formatter_record)` calls (lines 161, "
                "165, 175, 180, 184, 192) can raise `KeyError`, caught by a generic `except Exception` block (line 207).\n\n"
                "## Second cause\n\nThe handler at `loguru/_handler.py:207` delegates to `_error_interceptor.print()`, see "
                "`loguru/_error_interceptor.py:18-24` (lines 30-31).\n\n"
                "## Minimal fix\n\n- File: `loguru/_handler.py`\n- Function: `emit`\n\nAdd an except clause before line 207.\n")
        got = D.cited_lines(lrep, "loguru/_handler.py")
        assert got == [127, 161, 165, 175, 180, 184, 192, 207], got
        got = D.cited_lines(lrep, "loguru/_error_interceptor.py")
        assert got == [18, 19, 20, 21, 22, 23, 24, 30, 31], got
        lrep2 = ("## Root cause\n\n`_handler.py:150` and lines 151-152; the range `loguru/_handler.py:120-200` is context.\n\n"
                 "```python loguru/_handler.py:180-181\n        formatted = precomputed_format.format_map(formatter_record)\n```\n\n"
                 "## Second cause\n\nNone. Line 99 is unrelated.\n")
        got = D.cited_lines(lrep2, "loguru/_handler.py")
        assert got == [150, 151, 152, 180, 181], got
        assert D.cited_lines("## Root cause\n\nlines 5 and 7 of the parser\n", "a.py") == [], "no path mention: not attributed"
        assert D.cited_lines("## Root cause\n\nlines 5 and 7 of `pkg/a.py`\n", "pkg/a.py") == [5, 7]
        print("ok   cited_lines")
        # CLI
        r = subprocess.run([sys.executable, str(Path(__file__).with_name("diag_check.py")), "--repo", str(repo), "DIAGNOSIS.md",
                            "--spec", '{"must_mention": ["writeLock"]}'], capture_output=True, text=True)
        assert r.returncode == 0 and "DIAG OK" in r.stdout, r.stdout + r.stderr
        print("ok   CLI exit 0 on good report")
    print(f"ALL PASS ({n_ok} checks)")


if __name__ == "__main__":
    main()
