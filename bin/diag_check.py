#!/usr/bin/env python3
"""diag_check.py -- deterministic gate for a read-only DIAGNOSIS.md (mini-swe-runner "diagnose" mode).

  diag_check.py --repo . DIAGNOSIS.md [--spec JSON|FILE]       exit 0 = accepted, 1 = rejected (every miss listed)
  diag_check.py --score REPORT --expect JSON                   acceptance scorer: which expected facts the report hits

Importable: check(repo, report, spec) -> (ok, fails).

Contract (H2 headings, case-insensitive prefix match):
  Code path        >= 3 fenced citations, info string `<lang> <path>:<N>[-<M>]`; every non-blank line of the block
                   (whitespace-collapsed) must appear in <path> within lines N-5..M+5. An elision line (`...`, `# ...`,
                   `// ...`: no code, so no claim) is skipped. A line with `...` inside it abbreviates real code
                   (`raise ValueError(...)` for a 4-line raise, optuna 141aee2f8f34): it matches when the text around each
                   `...` (whitespace ignored, >= 6 chars in all) appears in that order in one run of up to 15 lines whose
                   first line holds the first piece and starts in the window. A quoted line found elsewhere in the file is
                   named in the failure (`it is at line K`). fix_ranges() (the runner calls it before check) moves a fence's range to
                   where its quoted lines are when every missing one is within 60 lines of the cited range (a model that
                   cites from memory, or from an edit that was since restored, is off by a few lines; the quote is real).
                   fix_quotes() (the runner calls it after fix_paths, before fix_ranges) gives a quoted line that is nowhere
                   in the file (in a fence under Code path, Root cause, Second cause or Not verified; a Minimal fix fence
                   may hold the proposed code) the text of the one line within 60 lines of the range that matches it once
                   whitespace, quote characters (' " `) and backslashes are ignored (bibtexr2 5329a2a546bb: a regex copied
                   with one ' too many, a quote-heavy string with one backtick dropped; every attempt was rejected for lines
                   it had read). fix_proposed() (after fix_quotes) un-cites a Minimal fix or Regression test fence that
                   cites a file but holds code that is not in it (conan2 r1 014d9fdb192b); its info string keeps only the
                   language, after a line saying it is proposed code
  Root cause       one paragraph, one sentence, <= 600 chars (ROOT_GATE; the contract asks for 400, ROOT_MAX, and the
                   gate lets a sentence run over: the real-issue bench of 2026-11-05/06 rejected 9 reports at 401-511
                   chars, each one a full re-diagnosis); parenthesized line references (`(line 2586)`,
                   `(lines 10-12 / line 40)`, `(app.py:12)`) are not counted (root_len: dupes3 69c0a9690916 was
                   rejected at 404 chars, 72 of them line refs, for a correct diagnosis)
  Second cause     non-empty (may say none)
  Minimal fix      exactly one `File: path` line and one `Function: name` line per function the fix changes (1-3, one
                   file); the file must define each. Text after the name is a comment (`Function: a (inner b)` targets a),
                   except a tail that adds names (`(and b)`, `, b`): those the file defines are targets too (fix_target).
                   Optional `Constant: NAME` lines (at most 3, fix_consts): module-level values the fix edits (a table of
                   known keys); the file must assign each at top level. A Function: line naming such a value is rejected
                   with that hint: the Function: line names the function whose decision reads it.
  Regression test  non-empty
  Not verified     non-empty
Other gates: file >= 800 bytes and not narration; bare `path:N` prose mentions must exist and N <= line count; a missing
path's failure lists the real tracked files with that name, so a retry can correct it. A shortened path (`client.js:179`,
`output.py:362-369`) resolves when exactly one tracked file ends with it or, when several do, the report names exactly one of
them in full elsewhere: a citation or mention of it is checked against that file, and fix_paths() (the runner calls it
before fix_ranges) rewrites it, and a Minimal fix File: value, to the full path (the File: check itself needs the full path);
a citation whose path does not resolve (`<path>`, a wrong directory: dmcg1 r2 222308c65fb9) is rewritten by fix_paths to
the one tracked file that holds its quoted lines near the cited range (_resolve_by_content: >= 2 distinct lines of >= 10 chars);
spec["must_mention"] literals present; no tracked diff vs HEAD and untracked files only DIAGNOSIS.md/TASK.md/ANSWER.md.
"""
import argparse
import json
import re
import subprocess
import sys
from pathlib import Path

HEADINGS = ["Code path", "Root cause", "Second cause", "Minimal fix", "Regression test", "Not verified"]
MIN_BYTES, MIN_CITES, ROOT_MAX, SLACK = 800, 3, 400, 5
ROOT_GATE = 600  # Root cause: what the gate allows; the contract and the form repair ask for ROOT_MAX
NARRATION = ("Let me", "Now I")
_REF_FIRST = r"(?:(?:lines?|L)\s*\d+(?:\s*[-\u2013]\s*\d+)?|[\w./-]+\.\w+:\d+(?:-\d+)?)"
_REF_NEXT = r"(?:(?:(?:lines?|L)\s*)?\d+(?:\s*[-\u2013]\s*\d+)?|[\w./-]+\.\w+:\d+(?:-\d+)?)"
LINE_REF_RE = re.compile(r"\s*\(\s*" + _REF_FIRST + r"(?:\s*(?:[/,;&]|and)\s*" + _REF_NEXT + r")*\s*\)")
UNTRACKED_OK = {"DIAGNOSIS.md", "TASK.md", "ANSWER.md"}
FENCE_RE = re.compile(r"^(`{3,}|~{3,})\s*(.*?)\s*$")
CITE_RE = re.compile(r"^(\w[\w+-]*)\s+(\S+?):(\d+)(?:-(\d+))?$")
CODE_EXT = ("js|ts|jsx|tsx|mjs|cjs|py|sh|json|md|yml|yaml|css|html|toml|go|rb|rs|java"
            "|swift|kt|kts|m|mm|c|h|cc|cpp|hpp|cs|php|sql|prisma|vue|svelte|scss|lua|zsh|bash")  # swift etc. added 2026-10-22 (bare Swift mentions went unchecked)
ELISION_RE = re.compile(r"^\s*(?:(?:#|//|--|;|/\*|<!--)\s*)?(?:\.\.\.|\u2026)\s*(?:\*/|-->)?\s*$"  # `...`, `/* ... */`
                        r"|^\s*(?:#|//|--|;)\s*(?:\.\.\.|\u2026)")  # a comment that starts with an ellipsis: `# ... unchanged`
ELLIPSIS_RE = re.compile(r"\.\.\.|…")
ELIDE_SPAN, ELIDE_MIN = 15, 6  # an elided quoted line: lines its pieces may run over, and the least text it must keep
REACH = 60  # fix_ranges: how far from the cited range a missing quoted line may be
QUOTE_NOISE_RE = re.compile(r"[\s'\"`\\]+")  # fix_quotes: what a quote-heavy line is mis-copied in (and whitespace)
QUOTE_KEY_MIN = 10  # fix_quotes: the least text a quoted line must keep once that noise is removed
QUOTE_SECTIONS = ("Code path", "Root cause", "Second cause", "Not verified")  # fix_quotes: fences that quote existing code
PROPOSED_SECTIONS = ("Minimal fix", "Regression test")  # fix_proposed: fences that may hold the fix's or the test's new code
CONTENT_MIN = 2  # fix_paths: distinct strong quoted lines a citation needs before its file is found by its lines
CONTENT_MAX_FILES, CONTENT_MAX_BYTES = 400, 2_000_000  # fix_paths: most candidate files read, largest one
MAX_FNS = 3  # Minimal fix: at most this many target functions (parallel copies of one fault, e.g. radarr/sonarr twins)
FILE_LINE_RE = re.compile(r"(?im)^[\s>*_-]*(?:file|path)[*_]*\s*[:=][*_]*\s*[*_]*`?([^\s`*]+?)`?[*_]*\s*$")  # `**File:** x` too
FN_LINE_RE = re.compile(r"(?im)^[\s>*_-]*function[*_]*\s*[:=][*_]*\s*(.*?)\s*$")
FN_NAME_RE = re.compile(r"[*_]*`?([\w$.]+?)(?:\(\))?`?[*_]*(?=$|[\s,;(&+])(.*)$")
FN_MORE_RE = re.compile(r"^[(\[]?\s*(?:(?:and|also|plus)\b|[,&+;])", re.I)  # a tail that names more targets
CONST_LINE_RE = re.compile(r"(?im)^[\s>*_-]*constant[*_]*\s*[:=][*_]*\s*(.*?)\s*$")  # `- Constant: `DEFAULTS`` (a module-level value)
MAX_CONSTS = 3
BARE_RE = re.compile(rf"(?<![\w/.:-])((?:[\w.-]+/)*[\w.-]+\.(?:{CODE_EXT})):(\d+)(?:-(\d+))?(?![\w])")


def _collapse(s):
    return re.sub(r"\s+", " ", s).strip()


def _defines(text, fn):  # same logic as mini-swe-runner._defines (copied, not imported)
    n = re.escape(fn)
    return bool(re.search(
        rf"\b(?:def|function\*?|class)\s+{n}\b|\b(?:const|let|var)\s+{n}\b|\b{n}\s*[:=]\s*(?:async\s*)?(?:function\b|\(|[\w$]+\s*=>)"
        rf"|^\s*(?:(?:public|private|protected|static|async|get|set|export)\s+)*{n}\s*\([^)]*\)\s*(?::[^{{\n]+)?\{{", text, re.M))


def binds_top(text, name):  # same logic as mini-swe-runner._binds_top (copied, not imported)
    """True when a module-level statement of `text` assigns `name`: python `NAME = ...` / `NAME: T = ...` at column 0, JS/TS
    `[export] const|let|var NAME =` at column 0 (a dict/list/set/scalar value, e.g. a table of known keys)."""
    n = re.escape(name)
    return bool(re.search(rf"^(?:{n}\s*(?::[^=\n]+)?=(?!=)|(?:export\s+)?(?:const|let|var)\s+{n}\s*(?::[^=\n]+)?=(?!=))", text, re.M))


def _fn_valued(text, name):
    """A module-level binding of `name` whose value is a function: `const f = (a) => ..`, `= async function`, python `f = lambda`."""
    n = re.escape(name)
    return bool(re.search(rf"^(?:export\s+)?(?:(?:const|let|var)\s+)?{n}\s*(?::[^=\n]+)?=\s*(?:async\s+)?"
                          rf"(?:function\b|lambda\b|\([^)]*\)\s*(?::[^=\n]+)?=>|[\w$]+\s*=>)", text, re.M))


def is_value(text, name):
    """True when `name` is a module-level value of `text` (binds_top, not function-valued) and nothing else defines it as a
    function: a Function: line naming it is a Constant (JS `const fs = require('fs')`, python `KNOWN = ("a", "b")`)."""
    if not binds_top(text, name) or _fn_valued(text, name):
        return False
    n = re.escape(name)
    return not re.search(rf"\b(?:def|function\*?|class)\s+{n}\b", text)


def root_len(body):
    """Length of a Root cause with its parenthesized line references removed: a cite costs the reader nothing."""
    return len(LINE_REF_RE.sub("", body))


def parse(text):
    """-> (sections {name: body_text}, blocks [(info, [lines], start_line, section)], prose_text (outside fences), headings_found)"""
    sections, blocks, prose = {}, [], []
    cur, fence, buf, info, start = None, None, [], "", 0
    for i, ln in enumerate(text.splitlines(), 1):
        m = FENCE_RE.match(ln)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
                blocks.append((info, buf, start, cur))
                fence = None
            else:
                buf.append(ln)
            if cur is not None:
                sections[cur].append(ln)
            continue
        if m:
            fence, info, buf, start = m.group(1), m.group(2), [], i
            if cur is not None:
                sections[cur].append(ln)
            continue
        h = re.match(r"^##\s+(.+?)\s*#*\s*$", ln)
        if h:
            name = next((n for n in HEADINGS if h.group(1).lower().lstrip("0123456789. )").startswith(n.lower())), None)
            cur = name or ("?" + h.group(1))
            sections.setdefault(cur, [])
            continue
        prose.append(ln)
        if cur is not None:
            sections[cur].append(ln)
    if fence:
        blocks.append((info, buf, start, cur))  # unterminated fence still counts; its content is checked
    return {k: "\n".join(v) for k, v in sections.items()}, blocks, "\n".join(prose)


def _heading(ln):
    """The HEADINGS name an `## ` line opens (parse()'s rule), else None."""
    h = re.match(r"^##\s+(.+?)\s*#*\s*$", ln)
    return next((n for n in HEADINGS if h.group(1).lower().lstrip("0123456789. )").startswith(n.lower())), None) if h else None


def swallowed(text):
    """[(fence_line, info, [heading, ...])]: report headings that sit inside a fence left open above them, per fence."""
    out, fence, info, start = [], None, "", 0
    for i, ln in enumerate(text.splitlines(), 1):
        m = FENCE_RE.match(ln)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
                fence = None
            elif _heading(ln):
                if not out or out[-1][0] != start:
                    out.append((start, info, []))
                out[-1][2].append(_heading(ln))
            continue
        if m:
            fence, info, start = m.group(1), m.group(2), i
    return out


def _verbatim_run(src, body, k):
    """True when body[:k] are k consecutive lines of src: exact after whitespace collapse (a blank line matches a blank line)."""
    want = [_collapse(x) for x in body[:k]]
    if len(want) < k or not any(want):
        return False
    have = [_collapse(x) for x in src]
    return any(have[j:j + k] == want for j in range(len(have) - k + 1) if have[j] == want[0])


def close_fences(repo, report):
    """Close the citation fences a report left open (bench-dupes2 7938a1ad1fec: a script wrote six ```python path:N-M openers
    and one closer, so every heading after the second quote sat inside one code block and the gate found no section). A fence
    opened by a citation info string and still open at the next citation opener, report heading or the end of the file is
    closed right after its first K = M-N+1 lines when those are K consecutive lines of the cited file (exact after whitespace
    collapse, anywhere in the file: fix_ranges moves a drifted range afterwards). The quote is then exactly what a closed fence
    would hold and the lines after it are prose. Any other open fence is left alone (check() names it). Rewrites the report in
    place; returns ["path:N-M (report line S)"], one per fence closed."""
    repo, rp = Path(repo), Path(report)
    if not rp.is_absolute():
        rp = repo / report
    try:
        text = rp.read_text(errors="replace")
    except OSError:
        return []
    out, done, cache, open_ = [], [], {}, None  # open_ = (marker, citation match or None, index of the opener in out)

    def close_open():
        marker, cm, at = open_
        n = int(cm.group(3))
        k = int(cm.group(4) or n) - n + 1
        real = _resolve_mention(repo, cm.group(2), cache, text)  # a shortened path: fix_paths rewrites it next
        src = _file_lines(repo, real, cache) if real else None
        if not src or k < 1 or not _verbatim_run(src, out[at + 1:], k):
            return False
        out.insert(at + 1 + k, marker)
        done.append(f"{cm.group(2)}:{cm.group(3)}{'-' + cm.group(4) if cm.group(4) else ''} (report line {at + 1})")
        return True

    for ln in text.split("\n"):
        m = FENCE_RE.match(ln)
        if open_:
            if m and m.group(1)[0] == open_[0][0] and len(m.group(1)) >= len(open_[0]) and not m.group(2):
                open_ = None
                out.append(ln)
                continue
            if not (open_[1] and ((m and CITE_RE.match(m.group(2))) or _heading(ln)) and close_open()):
                out.append(ln)
                continue
            open_ = None  # closed above this line, which is read again as outside a fence
        if m:
            open_ = (m.group(1), CITE_RE.match(m.group(2)), len(out))
        out.append(ln)
    if open_ and open_[1]:
        close_open()
    if done:
        rp.write_text("\n".join(out))
    return done


def _file_lines(repo, rel, cache):
    if rel in cache:
        return cache[rel]
    p = (Path(repo) / rel)
    try:
        ok = p.is_file() and not Path(rel).is_absolute() and ".." not in Path(rel).parts and p.resolve().is_relative_to(Path(repo).resolve())
        cache[rel] = p.read_text(errors="replace").splitlines() if ok else None
    except OSError:
        cache[rel] = None
    return cache[rel]


def _tracked(repo, cache):
    """Tracked repo paths (git ls-files), cached under a key no real path can take; [] outside git."""
    if "\0tracked" not in cache:
        rc, out = _git(repo, "ls-files")
        cache["\0tracked"] = [x for x in out.splitlines() if x] if rc == 0 else []
    return cache["\0tracked"]


def _named(path, text):
    """True when `text` names `path` in full: not as the tail of a longer path (`./path` counts)."""
    return bool(re.search(r"(?:(?<=\./)|(?<![\w/.-]))" + re.escape(path) + r"(?![\w/-]|\.\w)", text or ""))


def _resolve_mention(repo, path, cache, text=None):
    """A report may shorten a path (`client.js:179`, `webclient/client.js:179`): it resolves to the tracked file that ends with
    those path components when exactly one does or, when several do, to the one of them the report (`text`) names in full
    elsewhere (scrapy r2 5f2fbdc72e8e: `crawl.py:33` beside citations of scrapy/commands/crawl.py, in a repo with a second
    crawl.py; moto: `responses.py:1452` beside moto/dynamodb/responses.py, one of dozens). Returns the repo-relative path, or
    None (missing or ambiguous)."""
    if _file_lines(repo, path, cache) is not None:
        return path
    p = re.sub(r"^(?:\./)+", "", path)
    hits = [t for t in _tracked(repo, cache) if t.endswith("/" + p)]
    if len(hits) > 1:
        hits = [t for t in hits if _named(t, text)]
    return hits[0] if len(hits) == 1 and _file_lines(repo, hits[0], cache) is not None else None


def _fence_body(lines, i):
    """The content lines of the fence opened at lines[i] (0-based), up to its closing fence or the end (parse()'s rule)."""
    fence = FENCE_RE.match(lines[i]).group(1)
    body = []
    for ln in lines[i + 1:]:
        m = FENCE_RE.match(ln)
        if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
            break
        body.append(ln)
    return body


def _resolve_by_content(repo, n, m, body, cache):
    """The one tracked file that holds a citation's quoted lines near its range, for a citation whose path does not resolve
    (dmcg1 r2 222308c65fb9: the contract's `<path>:N-M` copied literally, and a remembered wrong directory, over real quotes).
    Needs CONTENT_MIN distinct quoted lines of at least QUOTE_KEY_MIN chars (no `...`); candidates are the tracked files holding
    the longest word of those lines (git grep -F, at most CONTENT_MAX_FILES, none over CONTENT_MAX_BYTES), and a candidate must
    hold every one of them within REACH of lines n-m. None when no file or more than one does."""
    strong = list(dict.fromkeys(x for x in _quoted(body) if len(_quote_key(x)) >= QUOTE_KEY_MIN and not ELLIPSIS_RE.search(x)))
    if len(strong) < CONTENT_MIN or m < n:
        return None
    word = max((w for x in strong for w in x.split(" ")), key=len)
    try:
        rc, out = _git(repo, "grep", "-l", "-z", "-I", "-F", "-e", word)
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    files = [f for f in out.split("\0") if f] if rc == 0 else []
    if not files or len(files) > CONTENT_MAX_FILES:
        return None
    hits = []
    for rel in files:
        try:
            if Path(repo, rel).stat().st_size > CONTENT_MAX_BYTES:
                continue
        except OSError:
            continue
        src = _file_lines(repo, rel, cache)
        if not src:
            continue
        lo, hi = max(1, n - REACH), min(len(src), m + REACH)
        if all(any(_seen(x, src, i) for i in range(lo, hi + 1)) for x in strong):
            hits.append(rel)
            if len(hits) > 1:
                return None
    return hits[0] if hits else None


def fix_paths(repo, report):
    """Rewrite each shortened path that resolves (_resolve_mention, against the whole report) to the full repo-relative path:
    in a citation info string (isort r2: `python output.py:362-369` when only isort/output.py ends with it), a bare prose
    mention (`crawl.py:33`) and the Minimal fix File: line (auto() reads that value as the fix target). Quoted code is never
    touched; a path that is missing or stays ambiguous is left for check() to name, except a citation's: one whose path does
    not resolve (the contract's `<path>` copied literally, a wrong directory: dmcg1 r2 222308c65fb9) takes the one tracked
    file that holds its quoted lines near the cited range (_resolve_by_content). The runner calls it before fix_ranges, which
    needs the real path to move a range. Rewrites the report in place; returns [(old, new)], one per distinct (old, new) pair."""
    repo, rp = Path(repo), Path(report)
    if not rp.is_absolute():
        rp = repo / report
    try:
        text = rp.read_text(errors="replace")
    except OSError:
        return []
    cache, done, lines, fence, sec = {}, [], text.split("\n"), None, None

    def real(path, body=None, n=0, e=0):
        r = _resolve_mention(repo, path, cache, text)
        if r is None and body is not None:  # a citation whose path does not resolve: the file that holds its quoted lines
            r = _resolve_by_content(repo, n, e, body, cache)
        if r and r != path:
            done.append((path, r))
            return r
        return path

    for i, ln in enumerate(lines):
        m = FENCE_RE.match(ln)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
                fence = None
            continue
        if m:
            fence = m.group(1)
            cm = CITE_RE.match(m.group(2))
            if cm:
                at = ln.index(m.group(2)) + cm.start(2)
                body = _fence_body(lines, i)
                lines[i] = ln[:at] + real(cm.group(2), body, int(cm.group(3)), int(cm.group(4) or cm.group(3))) + ln[at + len(cm.group(2)):]
            continue
        sec = _heading(ln) or (None if re.match(r"^##\s", ln) else sec)
        ln = BARE_RE.sub(lambda g: real(g.group(1)) + g.group(0)[len(g.group(1)):], ln)
        fm = FILE_LINE_RE.match(ln) if sec == "Minimal fix" else None
        if fm:
            ln = ln[:fm.start(1)] + real(fm.group(1)) + ln[fm.end(1):]
        lines[i] = ln
    if done:
        rp.write_text("\n".join(lines))
    return list(dict.fromkeys(done))


def _did_you_mean(repo, path, cache):
    """Retry feedback for a path that does not exist: the real tracked files with that file name."""
    name = Path(path).name
    hits = [t for t in _tracked(repo, cache) if Path(t).name == name]
    if not hits:
        return f" (no tracked file is named {name}; find the real path with `git ls-files | grep NAME`)"
    more = f" and {len(hits) - 5} more" if len(hits) > 5 else ""
    return f" (use the full repo-relative path; tracked files named {name}: {', '.join(hits[:5])}{more})"


DATA_EXT = frozenset("json md yml yaml toml css scss prisma txt cfg ini csv xml lock".split())  # data or prose: defines no function


def _defined_in(repo, name, skip=None):
    """The tracked source files (a CODE_EXT outside DATA_EXT), other than `skip`, that define function `name` (_defines, not a
    module-level value); a dotted `Owner.name` also needs Owner defined in the file or named by its stem. [] outside git.
    cfnlint r2 b493e7708c88: `File:` a JSON schema with `Function: CfnInit.validate` was rejected 3 times as "does not define"
    and never told where CfnInit.validate is (src/cfnlint/rules/resources/CfnInit.py)."""
    parts = name.split(".")
    last, owner = parts[-1], (parts[-2] if len(parts) > 1 else None)
    exts = [e for e in CODE_EXT.split("|") if e not in DATA_EXT]
    rc, out = _git(repo, "grep", "-l", "-z", "-I", "-w", "-F", "-e", last, "--", *[f"*.{e}" for e in exts])
    hits = []
    for rel in (out.split("\0") if rc == 0 else []):
        src = _file_lines(repo, rel, {}) if rel and rel != skip else None  # a fresh cache: keep no copy of each file read
        t = "\n".join(src or [])
        if t and _defines(t, last) and not is_value(t, last) and (not owner or _defines(t, owner) or Path(rel).stem == owner):
            hits.append(rel)
    return hits


def _undefined_fn(repo, path, name, many):
    """Gate 3 feedback when File: `path` does not define Function: `name`: where it is defined (_defined_in) and, for a data file,
    that the fix changes only code. `many`: the report names more than one function (the File: line must define them all)."""
    where, msg = _defined_in(repo, name, skip=path), f"Minimal fix: {path} does not define function {name}"
    if where:
        msg += (f" ({name} is defined in " + (where[0] if len(where) == 1 else f"{len(where)} tracked files: {', '.join(where[:3])}"
                                               + (" ..." if len(where) > 3 else ""))
                + (": the File: line names the file that defines every Function: line's function)" if many else
                   (": name that file on the File: line)" if len(where) == 1 else ": name the one whose code the fix changes on the File: line)")))
    if Path(path).suffix[1:].lower() in DATA_EXT:
        msg += (f"; {path} is not source code, and the fix changes only code: name the function whose code makes the wrong decision "
                "(when the wrong result comes from that data, the function that reads it) and the source file that defines it")
    return msg


def citations(blocks):
    """[(path, N, M, lines, block_start, section)] for fences whose info string is `<lang> <path>:<N>[-<M>]`."""
    out = []
    for info, lines, start, sec in blocks:
        m = CITE_RE.match(info)
        if m:
            n = int(m.group(3))
            out.append((m.group(2), n, int(m.group(4) or n), lines, start, sec))
    return out


FAULT_SECTIONS = ("Root cause", "Second cause")
CITE_SPAN = 10  # cited_lines: a range longer than this is context (a quote of the whole function), not a pointer at the fault
PATH_RE = re.compile(rf"(?<![\w/.:-])((?:[\w.-]+/)*[\w.-]+\.(?:{CODE_EXT}))(?![\w])")
LINE_WORD_RE = re.compile(r"(?<![\w.:/-])(?:[Ll]ines?|L)\s*(\d+(?:\s*[-–]\s*\d+)?"
                          r"(?:\s*(?:,|;|/|&|\band\b|\bor\b)\s*(?:[Ll]ines?\s*)?\d+(?:\s*[-–]\s*\d+)?)*)")
NUM_RANGE_RE = re.compile(r"(\d+)(?:\s*[-–]\s*(\d+))?")


def _same_file(mention, path):
    """True when a report's path mention names `path`: in full, or as its trailing components (`_handler.py`)."""
    m = re.sub(r"^(?:\./)+", "", mention)
    return m == path or path.endswith("/" + m)


def cited_lines(text, path, sections=FAULT_SECTIONS, span=CITE_SPAN):
    """Lines of `path` that a report's fault sections (Root cause, Second cause) point at: `path:N[-M]` prose mentions,
    `<lang> path:N-M` quotes, and plain `line N` / `lines N, M` / `L N` references, which belong to the nearest path mention
    before them in the same section (else to the section's only mentioned file). A range longer than `span` lines is skipped.
    The runner's test gate waives the body-coverage floor for a target function when the phase A test runs one of these lines
    inside it (loguru r3 bf6183725d19: `Handler.emit()` (`loguru/_handler.py:127`), all `format_map` calls (lines 161, 165, 175,
    180, 184, 192) ... caught by a generic `except Exception` block (line 207): the test of that path ran 26% of emit's 84
    lines). Returns sorted line numbers."""
    out = set()

    def add(n, m=None):
        n, m = int(n), int(m or n)
        if n <= m and m - n < span:
            out.update(range(n, m + 1))

    fence, sec, prose = None, None, {}
    for ln in text.splitlines():
        m = FENCE_RE.match(ln)
        if fence:
            if m and m.group(1)[0] == fence[0] and len(m.group(1)) >= len(fence) and not m.group(2):
                fence = None
            continue
        if m:
            fence = m.group(1)
            cm = CITE_RE.match(m.group(2))
            if cm and sec in sections and _same_file(cm.group(2), path):
                add(cm.group(3), cm.group(4))
            continue
        if re.match(r"^##\s", ln):
            sec = _heading(ln)
            continue
        if sec in sections:
            prose.setdefault(sec, []).append(ln)
    for body in ("\n".join(v) for v in prose.values()):
        for bm in BARE_RE.finditer(body):
            if _same_file(bm.group(1), path):
                add(bm.group(2), bm.group(3))
        mentions = [(pm.start(), pm.group(1)) for pm in PATH_RE.finditer(body)]
        only = {p for _i, p in mentions}
        for lm in LINE_WORD_RE.finditer(body):
            before = [p for i, p in mentions if i < lm.start()]
            owner = before[-1] if before else (next(iter(only)) if len(only) == 1 else None)
            if owner and _same_file(owner, path):
                for nm in NUM_RANGE_RE.finditer(lm.group(1)):
                    add(nm.group(1), nm.group(2))
    return sorted(out)


def fix_target(body):
    """`## Minimal fix` body -> (files, firsts, extras): every File:/path: value, the first name of each Function: line, and
    the names a Function: line adds after `and`/`also`/`,`/`&` (`- Function: a (and b)`, names in backticks). Any other text after the
    name is a comment (`- Function: desired_toml (the inner setkey)` targets desired_toml only). fix_names() keeps
    the extras the file defines (an undefined one is a new helper or prose). Files: in order, a repeat said once (bench-dupes2
    7938a1ad1fec wrote a File: line above each of its two Function: lines, one file, and was rejected 3 times as "2 file")."""
    fp = list(dict.fromkeys(re.sub(r"^\./", "", x) for x in FILE_LINE_RE.findall(body)))
    firsts, extras = [], []
    for rest in FN_LINE_RE.findall(body):
        m = FN_NAME_RE.match(rest)
        if not m:
            continue
        firsts.append(m.group(1))
        tail = m.group(2).strip()
        if FN_MORE_RE.match(tail):
            extras += re.findall(r"`([\w$.]+?)(?:\(\))?`", tail)
    return fp, firsts, extras


def fix_consts(body):
    """`## Minimal fix` body -> the names on `Constant:` lines (module-level values the fix edits, e.g. a table of known keys),
    with the `and`/`,` extras such a line adds; last dotted part, in order, no repeats. [] when there is none."""
    out = []
    for rest in CONST_LINE_RE.findall(body):
        m = FN_NAME_RE.match(rest)
        if not m:
            continue
        names = [m.group(1)] + (re.findall(r"`([\w$.]+?)`", m.group(2)) if FN_MORE_RE.match(m.group(2).strip()) else [])
        for x in names:
            x = x.split(".")[-1]
            if x not in out:
                out.append(x)
    return out


def fix_names(firsts, extras, text):
    """Target function names (last dotted part, in order, no repeats): every first name, plus the extras `text` defines."""
    out = []
    for x in [*firsts, *(e for e in extras if _defines(text, e.split(".")[-1]))]:
        n = x.split(".")[-1]
        if n not in out:
            out.append(n)
    return out


def _quoted(lines):
    """The lines of a fence that make a claim about the code: non-blank and not an elision marker (whitespace-collapsed)."""
    return [_collapse(x) for x in lines if x.strip() and not ELISION_RE.match(x)]


def _elided_at(x, src, i):
    """True when x, a quoted line with `...` inside it, abbreviates code starting at src line i (1-based): the pieces around
    each `...` (whitespace removed, at least ELIDE_MIN chars in all) appear in that order, the first in line i itself and the
    rest within ELIDE_SPAN lines of it. Kept text is real text; only what the `...` stands for goes unchecked."""
    parts = [re.sub(r"\s+", "", p) for p in ELLIPSIS_RE.split(x)]
    frags = [p for p in parts if p]
    if len(parts) < 2 or not frags or sum(map(len, frags)) < ELIDE_MIN:
        return False
    first = re.sub(r"\s+", "", src[i - 1])
    pos = first.find(frags[0])
    if pos < 0:
        return False
    run, pos = first + "".join(re.sub(r"\s+", "", w) for w in src[i:i - 1 + ELIDE_SPAN]), pos + len(frags[0])
    for f in frags[1:]:
        pos = run.find(f, pos)
        if pos < 0:
            return False
        pos += len(f)
    return True


def _seen(x, src, i):
    """True when quoted line x (whitespace-collapsed) is in src line i (1-based), or abbreviates code starting there."""
    return x in _collapse(src[i - 1]) or (ELLIPSIS_RE.search(x) is not None and _elided_at(x, src, i))


def _misses(quoted, src, n, m):
    """The quoted lines found nowhere in lines n-SLACK..m+SLACK of src (clamped)."""
    lo, hi = max(1, n - SLACK), min(len(src), m + SLACK)
    return [x for x in quoted if not any(_seen(x, src, i) for i in range(lo, hi + 1))]


def _quote_key(s):
    """s without whitespace, quote characters (' " `) and backslashes: what fix_quotes compares."""
    return QUOTE_NOISE_RE.sub("", s)


def fix_quotes(repo, report, reach=REACH):
    """Restore each fenced quoted line that is nowhere in the cited file but is one source line mis-copied in its quote
    characters, backslashes or whitespace (bibtexr2 5329a2a546bb: `looking_for = '`"'` for `looking_for = '`"`'`, and a raw
    regex with an extra '; all three attempts were rejected for those lines alone). The match is by key (_quote_key, at least
    QUOTE_KEY_MIN chars) among lines within `reach` of the cited range: the lines whose key equals it, else the lines whose
    key contains it (a quote of part of a line); it must name one text. A paraphrase, a short line, an ambiguous one, one
    with `...` stay for the gate to reject. A fence outside QUOTE_SECTIONS is left alone: a Minimal fix or Regression test
    fence may hold the proposed code (an escape or quoting fix differs from the line it replaces only in that noise), and
    fix_proposed un-cites one that does. The
    report line keeps its indentation and takes the source line's text. Runs after fix_paths (the full path) and before
    fix_ranges (a restored line outside the window moves the range). Rewrites the report in place; returns
    [("path:N-M", old line, new line)]."""
    repo, rp = Path(repo), Path(report)
    if not rp.is_absolute():
        rp = repo / report
    try:
        text = rp.read_text(errors="replace")
    except OSError:
        return []
    out_lines = text.split("\n")
    _, blocks, _ = parse(text)
    cache, fixed = {}, []
    for info, lines, start, sec in blocks:
        m = CITE_RE.match(info)
        if not m or sec not in QUOTE_SECTIONS:
            continue
        path, n = m.group(2), int(m.group(3))
        e = int(m.group(4) or n)
        real = _resolve_mention(repo, path, cache, text)
        src = _file_lines(repo, real, cache) if real else None
        if not src or e < n:
            continue
        keys = None
        for j, ln in enumerate(lines):
            if not ln.strip() or ELISION_RE.match(ln) or ELLIPSIS_RE.search(ln):
                continue
            x = _collapse(ln)
            if any(_seen(x, src, i) for i in range(1, len(src) + 1)):
                continue  # real text (fix_ranges or the gate place it)
            k = _quote_key(ln)
            if len(k) < QUOTE_KEY_MIN:
                continue
            if keys is None:
                keys = {i: _quote_key(src[i - 1]) for i in range(max(1, n - reach), min(len(src), e + reach) + 1)}
            hits = [i for i, v in keys.items() if v == k] or [i for i, v in keys.items() if k in v]
            texts = {src[i - 1].strip() for i in hits}
            if len(texts) != 1:
                continue
            at = start + j  # content line j of the fence opened at report line `start` (1-based) is out_lines[start + j]
            if not (0 <= at < len(out_lines)) or out_lines[at] != ln:
                continue  # a line separator splitlines() knows and split("\n") does not: leave it to the gate
            new = ln[:len(ln) - len(ln.lstrip())] + texts.pop()
            out_lines[at] = new
            fixed.append((f"{path}:{n}" + (f"-{m.group(4)}" if m.group(4) else ""), ln, new))
    if fixed:
        rp.write_text("\n".join(out_lines))
    return fixed


def fix_proposed(repo, report):
    """Un-cite a Minimal fix or Regression test fence that cites a file but holds code that is not in it: the fix's or the
    test's new code, labelled as a quote (conan2 r1 014d9fdb192b: the fix's new validation loop cited as
    `python ...profile_loader.py:249-249`, about 11 'quoted line not found' failures for a correct diagnosis). The fence's info
    string keeps only the language, after a line saying the code is proposed and where it was cited. A fence whose every quoted
    line is in the cited file stays (fix_ranges and the gate judge it); fences under Code path, Root cause, Second cause and Not
    verified are never touched. Runs after fix_quotes, before fix_ranges. Rewrites the report in place; returns
    ["path:N-M (report line L)"]."""
    repo, rp = Path(repo), Path(report)
    if not rp.is_absolute():
        rp = repo / report
    try:
        text = rp.read_text(errors="replace")
    except OSError:
        return []
    if len(text.splitlines()) != text.count("\n") + (0 if text.endswith("\n") or not text else 1):
        return []  # a line separator splitlines() knows and split("\n") does not: report lines would not line up
    out_lines = text.split("\n")
    _, blocks, _ = parse(text)
    cache, todo = {}, []
    for info, lines, start, sec in blocks:
        m = CITE_RE.match(info)
        if not m or sec not in PROPOSED_SECTIONS:
            continue
        quoted = _quoted(lines)
        if not quoted:
            continue
        path = m.group(2)
        real = _resolve_mention(repo, path, cache, text)
        src = _file_lines(repo, real, cache) if real else None
        if src and all(any(_seen(x, src, i) for i in range(1, len(src) + 1)) for x in quoted):
            continue
        todo.append((start, info, path, m.group(3) + (f"-{m.group(4)}" if m.group(4) else "")))
    done = []
    for start, info, path, span in reversed(todo):
        k = start - 1
        fm = FENCE_RE.match(out_lines[k]) if 0 <= k < len(out_lines) else None
        if not fm or fm.group(2) != info:
            continue
        ln = out_lines[k]
        out_lines[k] = ln[:ln.index(info)] + CITE_RE.match(info).group(1)
        out_lines.insert(k, f"Proposed or paraphrased code, not quoted from the repo (it was cited as `{path}`, line(s) {span}):")
        done.append(f"{path}:{span} (report line {start})")
    if done:
        rp.write_text("\n".join(out_lines))
    return done[::-1]


def fix_ranges(repo, report, reach=REACH):
    """Move each fenced citation's range to where its quoted lines are, when every quoted line that misses the cited window
    is in the same file within `reach` lines of the cited range (nearest occurrence). The new range is the union of the cited
    one and those lines, clamped to the file. A fence with a line found nowhere near is left alone (the gate rejects it).
    Rewrites the report in place; returns [(old "path:N-M", new "path:N-M")]."""
    repo, rp = Path(repo), Path(report)
    if not rp.is_absolute():
        rp = repo / report
    try:
        text = rp.read_text(errors="replace")
    except OSError:
        return []
    out_lines = text.split("\n")
    _, blocks, _ = parse(text)
    cache, moved = {}, []
    for info, lines, start, _sec in blocks:
        m = CITE_RE.match(info)
        if not m:
            continue
        path, n = m.group(2), int(m.group(3))
        e = int(m.group(4) or n)
        src = _file_lines(repo, path, cache)
        if src is None or e < n or not src:
            continue
        miss = _misses(_quoted(lines), src, n, e)
        if not miss:
            continue
        pos = []
        for x in miss:
            near = [i for i in range(max(1, n - reach), min(len(src), e + reach) + 1) if _seen(x, src, i)]
            if not near:
                pos = None
                break
            pos.append(min(near, key=lambda i: n - i if i < n else i - e))
        if not pos:
            continue
        lo, hi = max(1, min([n] + pos)), min(len(src), max([e] + pos))
        old, new = f"{path}:{n}" + (f"-{m.group(4)}" if m.group(4) else ""), f"{path}:{lo}-{hi}"
        k = start - 1
        if 0 <= k < len(out_lines) and old in out_lines[k]:
            out_lines[k] = out_lines[k].replace(old, new, 1)
            moved.append((old, new))
    if moved:
        rp.write_text("\n".join(out_lines))
    return moved


def _git(repo, *args):
    r = subprocess.run(["git", *args], cwd=repo, capture_output=True, text=True)
    return r.returncode, r.stdout


def check(repo, report, spec=None):
    spec, fails = spec or {}, []
    repo, rp = Path(repo), Path(report)
    if not rp.is_absolute() and not rp.exists():
        rp = repo / report
    if not rp.is_file():
        return False, [f"{report} does not exist"]
    raw = rp.read_bytes()
    text = raw.decode("utf-8", errors="replace")
    # gate 1: shape
    if len(raw) < MIN_BYTES:
        fails.append(f"report is {len(raw)} bytes (< {MIN_BYTES})")
    if text.lstrip().startswith(NARRATION):
        fails.append("report starts with narration (" + text.lstrip()[:30].replace("\n", " ") + "...): write only the report")
    sections, blocks, prose = parse(text)
    for s, info, hs in swallowed(text)[:2]:  # said first: it is why those sections are missing (bench-dupes2 attempt 3)
        fails.append(f"the fence opened at report line {s} (```{info}) is never closed, so "
                     + ", ".join(f"'## {h}'" for h in hs[:3]) + (f" and {len(hs) - 3} more heading(s)" if len(hs) > 3 else "")
                     + " sit inside it as code: end every quoted block with a line holding only ```")
    for h in HEADINGS:
        if h not in sections:
            fails.append(f"missing section '## {h}'")
    cites = citations(blocks)
    # gate 2: citations
    in_path = [c for c in cites if c[5] == "Code path"]
    if "Code path" in sections and len(in_path) < MIN_CITES:
        fails.append(f"Code path has {len(in_path)} fenced citation(s), need >= {MIN_CITES} "
                     "(fence info string must be exactly `<lang> <path>:<N>[-<M>]`)")
    cache = {}
    for path, n, m, lines, start, _ in cites:
        real = _resolve_mention(repo, path, cache, text)
        src = _file_lines(repo, real, cache) if real else None
        if src is None:
            fails.append(f"citation {path}:{n}-{m} (report line {start}): file does not exist in the repo" + _did_you_mean(repo, path, cache))
            continue
        if m < n:
            fails.append(f"citation {path}:{n}-{m} (report line {start}): range end before start")
            continue
        quoted = _quoted(lines)
        miss = _misses(quoted, src, n, m)
        if not quoted:
            fails.append(f"citation {path}:{n}-{m} (report line {start}): empty block")
        for x in miss:
            at = [i for i in range(1, len(src) + 1) if _seen(x, src, i)]
            where = (f" (it is at line {at[0]})" if len(at) == 1 else f" (it is at lines {', '.join(map(str, at[:4]))}{' ...' if len(at) > 4 else ''})"
                     if at else f" (it is nowhere in {path})")
            fails.append(f"citation {path}:{n}-{m} (report line {start}): quoted line not found in lines {max(1, n - SLACK)}-{m + SLACK}: {x[:100]!r}{where}")
    for mm in BARE_RE.finditer(prose):
        path, n = mm.group(1), int(mm.group(2))
        hi = int(mm.group(3) or n)
        real = _resolve_mention(repo, path, cache, text)
        src = _file_lines(repo, real, cache) if real else None
        if src is None:
            fails.append(f"mention {mm.group(0)}: file does not exist in the repo" + _did_you_mean(repo, path, cache))
        elif max(n, hi) > len(src) or n < 1:
            fails.append(f"mention {mm.group(0)}: line beyond end of file ({len(src)} lines)")
    # root cause
    if "Root cause" in sections:
        body = sections["Root cause"].strip()
        flat = re.sub(r"`[^`]*`", "``", body)
        if not body:
            fails.append("Root cause is empty")
        else:
            if root_len(body) > ROOT_GATE:
                fails.append(f"Root cause is {root_len(body)} chars (> {ROOT_GATE}): one sentence of at most {ROOT_MAX}")
            if "\n\n" in body or "```" in body or len(re.findall(r"[.!?](?:\s+(?=[A-Z0-9`\"'(])|$)", flat.strip())) > 1:
                fails.append("Root cause must be ONE sentence in one paragraph")
    for h in ("Second cause", "Regression test", "Not verified"):
        if h in sections and len(sections[h].strip()) < (4 if h == "Second cause" else 20):
            fails.append(f"'{h}' is empty or too short")
    # gate 3: fix target
    if "Minimal fix" in sections:
        fp, firsts, extras = fix_target(sections["Minimal fix"])
        consts = fix_consts(sections["Minimal fix"])
        if len(fp) > 1:  # sqlmetar3 f28c586f4214: "(found 2 file, 3 function)" did not say why; D2 re-explored and never edited
            fails.append(f"Minimal fix names {len(fp)} files ({', '.join(fp[:4])}{', ...' if len(fp) > 4 else ''}): the fix changes ONE "
                         "file only. Keep one 'File: `path`' line, for the file whose function makes the wrong decision, with a "
                         "'Function: `name`' line per function of that file the fix changes; delete the other File: lines and their "
                         "Function: lines (the fix must work by changing that one file alone)")
        elif len(fp) != 1 or not firsts:
            fails.append(f"Minimal fix needs exactly one 'File: `path`' line and one 'Function: `name`' line per function the fix "
                         f"changes (found {len(fp)} file, {len(firsts)} function)"
                         + ("; with a Constant: line, the Function: line names the function whose decision reads that value (the "
                            "regression test calls it)" if consts and len(fp) == 1 else ""))
        else:
            src = _file_lines(repo, fp[0], cache)
            if src is None:
                fails.append(f"Minimal fix file {fp[0]} does not exist in the repo" + _did_you_mean(repo, fp[0], cache))
            else:
                ftext = "\n".join(src)  # not `text`: that is the report, which gate 4 reads
                for x in firsts:
                    if _defines(ftext, x.split(".")[-1]) and not is_value(ftext, x.split(".")[-1]):
                        continue
                    if is_value(ftext, x.split(".")[-1]):  # cfgkeys 2cdee16a8b71: `Function: DEFAULTS` for a dict of known keys
                        fails.append(f"Minimal fix: {x} is a module-level value in {fp[0]}, not a function: name it on a "
                                     f"'- Constant: `{x.split('.')[-1]}`' line, and on the Function: line the function whose decision "
                                     "reads it (the regression test calls that function)")
                    else:
                        fails.append(_undefined_fn(repo, fp[0], x, len(set(firsts)) > 1))
                for c in consts:
                    if not binds_top(ftext, c):
                        fails.append(f"Minimal fix: {fp[0]} assigns no module-level value {c} (a Constant: line names a value assigned "
                                     "at the top level of the File:, e.g. `NAME = {...}`)")
                if len(consts) > MAX_CONSTS:
                    fails.append(f"Minimal fix names {len(consts)} constants; at most {MAX_CONSTS}")
                names = fix_names(firsts, extras, ftext)
                if len(names) > MAX_FNS:
                    fails.append(f"Minimal fix names {len(names)} functions ({', '.join(names)}); at most {MAX_FNS}: name only the "
                                 "functions whose code the fix changes")
    # gate 4: must_mention
    for lit in spec.get("must_mention") or []:
        if lit not in text:
            fails.append(f"must_mention literal not in report: {lit!r}")
    # gate 5: scope
    rc, out = _git(repo, "diff", "--name-only", "HEAD")
    if rc != 0:
        fails.append("scope: git diff failed (not a git repo?)")
    else:
        tracked = [x for x in out.splitlines() if x]
        if tracked:
            fails.append(f"scope: tracked files changed vs HEAD: {tracked}")
        rc, out = _git(repo, "ls-files", "--others", "--exclude-standard")
        okset = UNTRACKED_OK | set(spec.get("untracked_ok") or [])  # runner: files already untracked in the baseline copy
        extra = sorted(x for x in out.splitlines() if x and x not in okset and x.split("/")[0] not in okset)
        if extra:
            fails.append(f"scope: untracked files other than DIAGNOSIS.md/TASK.md/ANSWER.md: {extra[:10]}")
    return not fails, fails


def score(report, expect):
    """expect = {"facts": {NAME: {"path": P, "lines": [lo, hi], "terms": [[alt, ...], ...]}}}.
    A fact is hit when a citation (fenced or bare) of `path` overlaps lo..hi (+-5) AND every term group has an alternative
    (case-insensitive regex) somewhere in the report. Returns {NAME: (hit, why)}."""
    text = Path(report).read_text(errors="replace")
    _, blocks, prose = parse(text)
    spans = [(p, n, m) for p, n, m, *_ in citations(blocks)]
    spans += [(g.group(1), int(g.group(2)), int(g.group(3) or g.group(2))) for g in BARE_RE.finditer(prose)]
    res = {}
    for name, f in (expect.get("facts") or {}).items():
        lo, hi = f["lines"]
        near = [s for s in spans if s[0].endswith(f["path"]) and s[1] - SLACK <= hi and s[2] + SLACK >= lo]
        absent = [" | ".join(g) for g in f.get("terms", []) if not any(re.search(a, text, re.I) for a in g)]
        why = []
        if not near:
            why.append(f"no citation of {f['path']} near {lo}-{hi} (cited: {[s for s in spans if s[0].endswith(f['path'])]})")
        if absent:
            why.append("terms missing: " + "; ".join(absent))
        res[name] = (not why, "; ".join(why) or f"cites {near[0][0]}:{near[0][1]}-{near[0][2]}, terms present")
    return res


def _load_json(s):
    p = Path(s)
    return json.loads(p.read_text() if p.is_file() else s)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("report", nargs="?", help="DIAGNOSIS.md (relative to --repo)")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--spec", help="JSON string or file: {must_mention: [...]}")
    ap.add_argument("--score", metavar="FILE", help="score FILE against --expect instead of gating it")
    ap.add_argument("--expect", help="JSON string or file for --score")
    a = ap.parse_args(argv)
    if a.score:
        if not a.expect:
            ap.error("--score needs --expect")
        res = score(a.score, _load_json(a.expect))
        for k, (hit, why) in res.items():
            print(f"FACT {k}: {'HIT' if hit else 'MISS'} {why}")
        allhit = bool(res) and all(h for h, _ in res.values())
        print(f"SCORE {sum(h for h, _ in res.values())}/{len(res)} {'PASS' if allhit else 'FAIL'}")
        return 0 if allhit else 1
    if not a.report:
        ap.error("report path required")
    ok, fails = check(a.repo, a.report, _load_json(a.spec) if a.spec else {})
    if ok:
        print("DIAG OK")
        return 0
    print(f"DIAG REJECTED ({len(fails)}):")
    for f in fails:
        print(f"- {f}")
    return 1


if __name__ == "__main__":
    sys.exit(main())
