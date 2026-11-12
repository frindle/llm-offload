#!/usr/bin/env python3
"""source_text_harness.py -- detect a verify FIXTURE that reads the target's
SOURCE TEXT and asserts on its shape instead of EXECUTING the code.

WHY (2026-10-05, slice rt-bfmr-link-sync-feedback)
--------------------------------------------------
That slice passed preflight, the pre-gate, the authoritative re-gate (PASS) and
the cross-family second opinion (agree) with a verify.test.ts that never ran
the code under test. Every case did

    const content = fs.readFileSync('./components/BfmrReservationLinker.tsx', 'utf8');
    assert.ok(content.includes('webError'));
    assert.ok(/webNeededVal\\s*=\\s*d\\.webNeeded\\s+!=\\s+null\\s*\\?\\s*Number\\(/.test(content));

That is a PROXY (feedback_assert_the_property_not_a_proxy): it pins the SOURCE
TEXT of one implementation. A correct fix written differently FAILS it; a wrong
fix that happens to contain the strings PASSES it. Mutation relevance could not
see the defect -- a regex over the exact line kills every mutant of that line
by textual coincidence -- and the refine loop made it WORSE: each survivor was
"killed" by adding another literal regex over the mutated line ("ADVERSARIAL
CASES (mutation-relevance gate survivors)"). Relevance measured discrimination,
not relevance (feedback_verify_relevance_not_just_discrimination).

THE RULE
--------
A fixture (verify.test.ts / test_fixture.py / the files verify.sh runs as
tests) must not ASSERT on the target's source text. Literal presence checks have
exactly one sanctioned home: TASK.md `## Must contain`, frozen into
check_literals.py (which is therefore never scanned here). So:

  * the fixture READS a target code file as text (readFileSync / readFile /
    open() / Path.read_text / inspect.getsource / `?raw` import ...), AND
  * at least one assertion depends on that text (includes / match / regex
    .test / `in` / re.search / assertIn ...)   ->  SOURCE-TEXT HARNESS, NO-GO.

Text that flows only into eval/exec/vm/compile (the fixture EXECUTES the source)
is not a text assertion. Data targets (.json/.yaml/.md/.css/...) are not code
and are never flagged -- reading a JSON target and asserting on its parsed
values IS its behaviour.

PURE module: no subprocess, no writes. Consumers: verify-quality.py (problem
`source-text-harness`, a preflight NO-GO), verify-relevance.py (verdict LOW
with the reason, before any mutant runs -- so the refine loop is never handed
survivors to "kill" with more regexes), auto-harness-check.py (immediate
self-check failure while authoring), gate-on-complete.py (diagnosis class).
"""
from __future__ import annotations

import ast
import json
import re
from pathlib import Path

CODE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts",
             ".swift", ".cs", ".go", ".rs", ".rb", ".java", ".kt")
JS_EXTS = (".ts", ".tsx", ".js", ".jsx", ".mjs", ".cjs", ".mts", ".cts")
# Harness infrastructure that legitimately reads the target as text. The
# literal gate is SANCTIONED (it is the one place literal checks belong); the
# others are pipeline machinery, never the behavioural fixture.
NOT_FIXTURES = {"check_literals.py", "auto-harness-check.py", "refimpl.py",
                "verify.sh"}
DEFAULT_FIXTURES = ("verify.test.ts", "verify.test.mts", "verify.test.js",
                    "test_fixture.py", "verify_impl.mts", "verify_impl.mjs",
                    "verify_impl.js")

FIX_TEXT = (
    "Delete every case that reads the target file as text. Literal presence is "
    "ALREADY enforced by TASK.md `## Must contain` -> check_literals.py; that is "
    "the only place a literal check belongs. Replace them with cases that EXECUTE "
    "the code: (1) if the logic sits inside a component/handler that is awkward "
    "to drive, have refimpl.py EXTRACT it into an exported pure function in the "
    "target (e.g. `export function syncResultMessage(status, body)`), make the "
    "component call it, then IMPORT that function from the target in the fixture "
    "and assert its return values for each case in the spec (happy path, each "
    "error branch, each boundary such as ==0 / >0); or (2) import the real "
    "export and drive it with the repo's test stack (node:test + mock.module, a "
    "fake fetch returning a Response-like {ok,status,json()}, a NextRequest for a "
    "route; pytest/TestClient for Python) and assert the observable OUTPUT "
    "(returned value, rendered text, status + JSON body, the exact request "
    "body/URL a fake fetch received). To kill a surviving mutant, add an INPUT "
    "whose output differs under the mutation -- never a regex over the mutated "
    "line. (A pure ABSENCE constraint the spec states -- 'must NOT import "
    "@prisma' -- may stay as a negated check, `!src.includes(...)`.)")


# --------------------------------------------------------------------------
# shared helpers
# --------------------------------------------------------------------------
_TEST_FILE_RE = re.compile(r"(^|/)(test_[^/]*\.py|[^/]*_test\.py|tests?/[^/]*\.py|"
                           r"[^/]*\.(test|spec)\.[cm]?[jt]sx?|__tests__/[^/]+)$")


def _is_code_target(rel: str) -> bool:
    """A code file whose BEHAVIOUR a fixture should execute. A TEST-file target
    (the deliverable is a corrected test) is excluded: its locked runner may
    legitimately inspect the test's text, and relevance is N/A there anyway."""
    s = str(rel).replace("\\", "/")
    return s.lower().endswith(CODE_EXTS) and not _TEST_FILE_RE.search(s)


def _target_tokens(targets) -> list[tuple[str, str, str]]:
    """(rel, basename, stem) per CODE target."""
    out = []
    for t in targets or []:
        rel = str(t).replace("\\", "/").lstrip("./") if not str(t).startswith("../") else str(t)
        if not rel or not _is_code_target(rel):
            continue
        base = rel.rsplit("/", 1)[-1]
        stem = base.rsplit(".", 1)[0]
        out.append((rel, base, stem))
    return out


def _lit_refs_target(lit: str, toks) -> str | None:
    """The target a string literal names as a FILE (basename with extension),
    or None. A bare stem ('BfmrReservationLinker') is a module name, not a
    file read, and is matched only by the import rules."""
    s = lit.replace("\\", "/")
    for rel, base, _stem in toks:
        if s == base or s.endswith("/" + base) or s.endswith(rel):
            return rel
    return None


def _spec_imports_target(spec: str, toks) -> str | None:
    """Does an import/require specifier resolve (by name) to a target?"""
    s = spec.replace("\\", "/").split("?", 1)[0]
    last = s.rsplit("/", 1)[-1]
    last_noext = re.sub(r"\.(?:[cm]?[jt]sx?|py)$", "", last)
    for rel, _base, stem in toks:
        if last_noext != stem:
            continue
        parts = rel.rsplit("/", 2)
        parent = parts[-2] if len(parts) >= 2 else ""
        sparts = s.rsplit("/", 2)
        sparent = sparts[-2] if len(sparts) >= 2 else ""
        # './x' / 'x' (same dir) or a matching parent dir (aliases like '@/components/X')
        if not parent or sparent in ("", ".", "..", "@", parent) or sparent.startswith("@"):
            return rel
    return None


# --------------------------------------------------------------------------
# JS / TS
# --------------------------------------------------------------------------
_REGEX_PREV = set("(,=:[!&|?{};+-*%<>~^")
_REGEX_PREV_WORDS = ("return", "typeof", "case", "do", "else", "in", "of", "void",
                     "yield", "await", "delete", "throw", "new")


def _js_mask(src: str):
    """Blank comments; mask string/template/regex BODIES with spaces (quotes and
    slashes kept). Returns (masked, strings[(value, start, end)])."""
    out = list(src)
    strings = []
    i, n = 0, len(src)
    prev_sig = ""        # last significant non-space char of CODE
    prev_word = ""
    while i < n:
        c = src[i]
        d = src[i + 1] if i + 1 < n else ""
        if c == "/" and d == "/":
            j = src.find("\n", i)
            j = n if j < 0 else j
            for k in range(i, j):
                out[k] = " "
            i = j
            continue
        if c == "/" and d == "*":
            j = src.find("*/", i + 2)
            j = n if j < 0 else j + 2
            for k in range(i, j):
                if out[k] != "\n":
                    out[k] = " "
            i = j
            continue
        if c in "'\"`":
            q = c
            j = i + 1
            buf = []
            while j < n and src[j] != q:
                if src[j] == "\\" and j + 1 < n:
                    buf.append(src[j:j + 2])
                    j += 2
                    continue
                if q != "`" and src[j] == "\n":
                    break
                buf.append(src[j])
                j += 1
            val = "".join(buf)
            try:
                val = bytes(val, "utf-8").decode("unicode_escape") if "\\" in val else val
            except Exception:
                pass
            strings.append((val, i, min(j + 1, n)))
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            prev_sig, prev_word = q, ""
            continue
        if c == "/" and (prev_sig == "" or prev_sig in _REGEX_PREV
                         or prev_word in _REGEX_PREV_WORDS):
            j = i + 1
            in_cls = False
            while j < n and src[j] != "\n":
                ch = src[j]
                if ch == "\\":
                    j += 2
                    continue
                if ch == "[":
                    in_cls = True
                elif ch == "]":
                    in_cls = False
                elif ch == "/" and not in_cls:
                    break
                j += 1
            if j < n and src[j] == "/":
                for k in range(i + 1, j):
                    out[k] = " "
                j += 1
                while j < n and (src[j].isalpha()):
                    j += 1
                i = j
                prev_sig, prev_word = "/", ""
                continue
        if not c.isspace():
            if c.isalnum() or c in "_$":
                j = i
                while j < n and (src[j].isalnum() or src[j] in "_$"):
                    j += 1
                prev_word = src[i:j]
                prev_sig = src[j - 1]
                i = j
                continue
            prev_sig, prev_word = c, ""
        i += 1
    return "".join(out), strings


def _js_segments(masked: str):
    """Statement-ish segments: split at newline/';' only where the innermost
    open bracket is a `{` (block body) or there is none -- so a multi-line
    `assert.ok(\\n content.includes(x),\\n 'msg')` stays one segment, while the
    statements inside `test('x', async () => { ... })` are separate."""
    segs = []
    stack = []
    start = 0
    for i, c in enumerate(masked):
        if c in "([{":
            stack.append(c)
        elif c in ")]}":
            if stack:
                stack.pop()
        if c in "\n;" and (not stack or stack[-1] == "{"):
            if masked[start:i].strip():
                segs.append((start, i))
            start = i + 1
    if masked[start:].strip():
        segs.append((start, len(masked)))
    return segs


def _paren_end(masked: str, open_idx: int) -> int:
    depth = 0
    for j in range(open_idx, len(masked)):
        if masked[j] == "(":
            depth += 1
        elif masked[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return len(masked) - 1


_JS_READ_CALL = re.compile(
    r"\b(?:readFileSync|readFile|readTextFile(?:Sync)?|readFileAsync)\s*\(|"
    r"\bBun\s*\.\s*file\s*\(")
_JS_TEXT_OP = re.compile(
    r"\.\s*(?:includes|indexOf|lastIndexOf|match|matchAll|search|startsWith|"
    r"endsWith|test|exec)\s*\(|\bassert\s*\.\s*(?:match|doesNotMatch)\s*\(|"
    r"\.\s*(?:not\s*\.\s*)?(?:toContain|toMatch|toInclude|stringContaining)\s*\(")
_JS_ASSERT = re.compile(
    r"\bassert(?:\s*\.\s*\w+)?\s*\(|\bexpect\s*\(|\bt\s*\.\s*(?:assert|is|not|"
    r"deepEqual|equal|true|false|ok|match|throws|rejects)\w*\s*\(|"
    r"\b(?:chk|check|ok|expectTrue)\s*\(")
_JS_NEG_SEG = re.compile(r"\bdoesNotMatch\s*\(|\.\s*not\s*\.|\bassert\s*\.\s*(?:equal|"
                         r"strictEqual)\s*\([^,]*,\s*(?:false|-1)\s*[,)]")
_JS_EXEC = re.compile(
    r"\b(?:eval|Function|runInNewContext|runInThisContext|runInContext|Script|"
    r"SourceTextModule|transpileModule|transpile|transformSync|transform)\s*\(")
_JS_DECL = re.compile(
    r"\b(?:const|let|var)\s+(\[[^\]=]*\]|\{[^}=]*\}|[A-Za-z_$][\w$]*)\s*"
    r"(?::\s*[^=;\n]+?)?=(?!=)")
_JS_REASSIGN = re.compile(r"(?:^|[;{\s])([A-Za-z_$][\w$]*)\s*=(?![=>])")
_JS_FOROF = re.compile(
    r"\bfor\s*\(\s*(?:const|let|var)\s+(\[[^\]]*\]|\{[^}]*\}|[A-Za-z_$][\w$]*)\s+"
    r"(?:of|in)\s+([A-Za-z_$][\w$.]*)")
_JS_FUNC = re.compile(
    r"\bfunction\s+([A-Za-z_$][\w$]*)\s*\(|"
    r"\b(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=\s*(?:async\s*)?"
    r"(?:function\b[^(]*\(|\([^)]*\)\s*(?::\s*[^=]+?)?=>|[A-Za-z_$][\w$]*\s*=>)")


def _names(lhs: str) -> list[str]:
    return re.findall(r"[A-Za-z_$][\w$]*", lhs) if lhs[:1] in "[{" else [lhs]


def _words(text: str) -> set[str]:
    return set(re.findall(r"[A-Za-z_$][\w$]*", text))


def scan_js(text: str, targets) -> dict:
    toks = _target_tokens(targets)
    res = {"lang": "js", "target_reads": [], "imports_target": [],
           "text_asserts": 0, "behavior_asserts": 0, "examples": []}
    if not toks:
        return res
    masked, strings = _js_mask(text)

    def lits_in(a, b):
        return [s for s in strings if s[1] >= a and s[2] <= b + 1]

    # imports / executions of a target
    for m in re.finditer(r"\bfrom\s+(['\"])|\bimport\s*\(\s*(['\"])|\brequire\s*\(\s*(['\"])|"
                         r"^\s*import\s+(['\"])", masked, re.M):
        q = m.end() - 1
        lit = next((s for s in strings if s[1] == q), None)
        if lit:
            hit = _spec_imports_target(lit[0], toks)
            if hit and "?raw" not in lit[0]:
                res["imports_target"].append(hit)
    # target-path variables (fixpoint)
    segs = _js_segments(masked)
    tvars: set[str] = set()
    for _ in range(4):
        grew = False
        for a, b in segs:
            seg = masked[a:b]
            for m in _JS_DECL.finditer(seg):
                rhs_a = a + m.end()
                rhs = masked[rhs_a:b]
                named = any(_lit_refs_target(s[0], toks) for s in lits_in(rhs_a, b))
                if named or (_words(rhs) & tvars):
                    for nm in _names(m.group(1)):
                        if nm not in tvars and not _JS_READ_CALL.search(rhs):
                            tvars.add(nm)
                            grew = True
        if not grew:
            break
    # dynamic import / spawn of a target var counts as execution
    for m in re.finditer(r"\bimport\s*\(\s*([A-Za-z_$][\w$]*)|"
                         r"\b(?:spawn|spawnSync|exec|execSync|execFile|execFileSync|fork)\s*\(",
                         masked):
        if m.group(1) and m.group(1) in tvars:
            res["imports_target"].append("(dynamic import of a target path)")
        elif not m.group(1):
            e = _paren_end(masked, masked.index("(", m.start()))
            if (_words(masked[m.start():e]) & tvars) or any(
                    _lit_refs_target(s[0], toks) for s in lits_in(m.start(), e)):
                res["imports_target"].append("(spawns the target)")

    # read calls of a target; reader functions
    def read_spans(scope_a=0, scope_b=None, readers=frozenset()):
        scope_b = len(masked) if scope_b is None else scope_b
        spans = []
        for m in _JS_READ_CALL.finditer(masked, scope_a, scope_b):
            op = masked.index("(", m.start())
            e = _paren_end(masked, op)
            arg = masked[op:e]
            if (_words(arg) & tvars) or any(_lit_refs_target(s[0], toks)
                                            for s in lits_in(op, e)):
                spans.append((m.start(), e))
        if readers:
            for m in re.finditer(r"\b(" + "|".join(map(re.escape, readers)) + r")\s*\(",
                                 masked[scope_a:scope_b]):
                st = scope_a + m.start()
                if re.search(r"function\s+$", masked[max(0, st - 12):st]):
                    continue
                spans.append((st, _paren_end(masked, scope_a + m.end() - 1)))
        return spans

    readers: set[str] = set()
    for _ in range(3):
        grew = False
        for m in _JS_FUNC.finditer(masked):
            nm = m.group(1) or m.group(2)
            if not nm or nm in readers:
                continue
            # body: from the first '{' after the match (or the arrow expression)
            bo = masked.find("{", m.end())
            if bo < 0:
                continue
            depth, be = 0, len(masked)
            for j in range(bo, len(masked)):
                if masked[j] == "{":
                    depth += 1
                elif masked[j] == "}":
                    depth -= 1
                    if depth == 0:
                        be = j
                        break
            if read_spans(bo, be, frozenset(readers - {nm})):
                readers.add(nm)
                grew = True
        if not grew:
            break

    reads = read_spans(readers=frozenset(readers))
    for a, b in reads:
        line = text.count("\n", 0, a) + 1
        res["target_reads"].append({"line": line,
                                    "code": text[a:b + 1].strip()[:120]})
    if not reads:
        return res

    # content-derived variables (fixpoint), exec flows
    def seg_has_read(a, b):
        return any(ra >= a and ra < b for ra, _rb in reads)

    # cvars: name -> polarity (True = the value is a NEGATED text test, e.g.
    # `const noGT1 = !/x/.test(content)`; asserting it is an ABSENCE check).
    cvars: dict[str, bool] = {}
    for _ in range(6):
        grew = False
        for a, b in segs:
            seg = masked[a:b]
            src_ref = seg_has_read(a, b) or bool(_words(seg) & set(cvars))
            if not src_ref:
                continue
            for m in _JS_FOROF.finditer(seg):
                if m.group(2).split(".")[0] in cvars:
                    for nm in _names(m.group(1)):
                        if nm not in cvars:
                            cvars[nm] = False
                            grew = True
            for m in _JS_DECL.finditer(seg):
                rhs = seg[m.end():]
                rhs_ref = seg_has_read(a + m.end(), b) or bool(_words(rhs) & set(cvars))
                if rhs_ref and not _JS_EXEC.search(rhs):
                    neg = rhs.lstrip().startswith("!")
                    for nm in _names(m.group(1)):
                        if nm not in cvars:
                            cvars[nm] = neg
                            grew = True
            for m in _JS_REASSIGN.finditer(seg):
                rhs = seg[m.end():]
                if (seg_has_read(a + m.end(), b) or (_words(rhs) & set(cvars))) \
                        and not _JS_EXEC.search(rhs) and m.group(1) not in (
                            "const", "let", "var"):
                    if m.group(1) not in cvars:
                        cvars[m.group(1)] = rhs.lstrip().startswith("!")
                        grew = True
        if not grew:
            break

    def occ_positive(a, b) -> bool:
        """Is any tainted reference in masked[a:b] a POSITIVE (presence/match)
        test? A reference directly negated with `!` flips the polarity."""
        seg = masked[a:b]
        if _JS_NEG_SEG.search(seg):
            return False
        hits = []
        for ra, _rb in reads:
            if a <= ra < b:
                hits.append((ra, False))
        for m in re.finditer(r"[A-Za-z_$][\w$]*", seg):
            if m.group(0) in cvars:
                hits.append((a + m.start(), cvars[m.group(0)]))
        for pos, var_neg in hits:
            # walk back over `(`, whitespace and a leading regex/receiver like
            # `/re/.test(` so `!/x/.test(content)` reads as negated
            pre = masked[max(a, pos - 200):pos]
            negated = bool(re.search(r"!\s*\(?\s*(?:/\s*/[a-z]*|[\w$.\]\)]+)?\s*"
                                     r"(?:\.\s*(?:test|exec)\s*\(\s*)?$", pre)) \
                and not re.search(r"!=\s*$", pre)
            if negated != var_neg:      # double negation is positive
                continue
            return True
        return False

    for a, b in segs:
        seg = masked[a:b]
        tainted = seg_has_read(a, b) or bool(_words(seg) & set(cvars))
        is_assert = bool(_JS_ASSERT.search(seg))
        if tainted and _JS_EXEC.search(seg) and not _JS_TEXT_OP.search(seg):
            continue
        # a bare check unit: a case-table thunk / `if (line.includes(x))` /
        # `return src.includes(x)` -- a text test that is not a declaration
        bare = (not is_assert and tainted and bool(_JS_TEXT_OP.search(seg))
                and not _JS_DECL.search(seg)
                and not re.match(r"\s*[A-Za-z_$][\w$]*\s*=(?!=)", seg))
        if is_assert or bare:
            if tainted:
                if occ_positive(a, b):
                    res["text_asserts"] += 1
                    if len(res["examples"]) < 4:
                        res["examples"].append(
                            f"line {text.count(chr(10), 0, a) + 1}: "
                            + " ".join(text[a:b].split())[:140])
                else:
                    res["absence_asserts"] = res.get("absence_asserts", 0) + 1
            else:
                res["behavior_asserts"] += 1
    return res


# --------------------------------------------------------------------------
# Python
# --------------------------------------------------------------------------
_PY_TEXT_METHODS = {"count", "find", "rfind", "index", "rindex", "startswith",
                    "endswith", "search", "match", "fullmatch", "findall",
                    "finditer", "splitlines", "split"}
_PY_EXEC = {"exec", "eval", "compile", "run_path", "run_module"}


def _py_strs(node) -> list[str]:
    return [n.value for n in ast.walk(node)
            if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _py_names(node) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)}


def _py_call_name(call) -> str:
    f = call.func
    if isinstance(f, ast.Name):
        return f.id
    if isinstance(f, ast.Attribute):
        return f.attr
    return ""


def scan_py(text: str, targets) -> dict:
    toks = _target_tokens(targets)
    res = {"lang": "python", "target_reads": [], "imports_target": [],
           "text_asserts": 0, "behavior_asserts": 0, "examples": []}
    if not toks:
        return res
    try:
        tree = ast.parse(text)
    except SyntaxError:
        res["unparsed"] = True
        return res
    py_stems = {stem for rel, _b, stem in toks if rel.endswith(".py")}
    lines = text.splitlines()

    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            for al in n.names:
                if al.name.rsplit(".", 1)[-1] in py_stems:
                    res["imports_target"].append(al.name)
        elif isinstance(n, ast.ImportFrom):
            mod = n.module or ""
            if mod.rsplit(".", 1)[-1] in py_stems or any(
                    al.name in py_stems for al in n.names):
                res["imports_target"].append(mod or "(relative)")

    def refs_target(node, tvars):
        return any(_lit_refs_target(s, toks) for s in _py_strs(node)) or bool(
            _py_names(node) & tvars)

    # target-path variables
    tvars: set[str] = set()
    assigns = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign))]
    for _ in range(4):
        grew = False
        for n in assigns:
            val = n.value
            if val is None:
                continue
            if refs_target(val, tvars) and not _is_py_read(val, toks, tvars):
                tg = n.targets if isinstance(n, ast.Assign) else [n.target]
                for t in tg:
                    for nm in _py_names(t):
                        if nm not in tvars:
                            tvars.add(nm)
                            grew = True
        if not grew:
            break
    # importlib / runpy / subprocess on a target path -> execution
    for n in ast.walk(tree):
        if isinstance(n, ast.Call):
            cn = _py_call_name(n)
            if cn in ("spec_from_file_location", "SourceFileLoader", "run_path",
                      "import_module", "run", "check_output", "check_call", "Popen",
                      "call", "load_source"):
                args = list(n.args) + [k.value for k in n.keywords]
                if any(refs_target(x, tvars) for x in args) or (
                        cn == "import_module" and any(
                            isinstance(x, ast.Constant) and isinstance(x.value, str)
                            and x.value.rsplit(".", 1)[-1] in py_stems for x in n.args)):
                    res["imports_target"].append(f"({cn} of a target)")

    reads = [n for n in ast.walk(tree) if isinstance(n, ast.Call)
             and _is_py_read(n, toks, tvars, direct=True)]
    # `with open(T) as f` -> f is a handle; f.read() is a read
    handles: set[str] = set()
    for n in ast.walk(tree):
        if isinstance(n, (ast.With, ast.AsyncWith)):
            for it in n.items:
                if it.optional_vars is not None and isinstance(it.context_expr, ast.Call) \
                        and _is_py_read(it.context_expr, toks, tvars, direct=True, open_only=True):
                    handles |= _py_names(it.optional_vars)
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) \
                and n.func.attr in ("read", "readlines", "readline") \
                and isinstance(n.func.value, ast.Name) and n.func.value.id in handles:
            reads.append(n)
    # reader helpers: `def src(): return Path(TARGET).read_text()` -> src() is a read
    readers: set[str] = set()
    funcs = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for _ in range(3):
        rid = {id(r) for r in reads}
        grew = False
        for f in funcs:
            if f.name in readers:
                continue
            if any(id(x) in rid for x in ast.walk(f)):
                readers.add(f.name)
                grew = True
        if grew:
            for n in ast.walk(tree):
                if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) \
                        and n.func.id in readers and id(n) not in {id(r) for r in reads}:
                    reads.append(n)
        else:
            break
    if not reads:
        return res
    read_ids = {id(r) for r in reads}
    for r in reads:
        res["target_reads"].append({"line": r.lineno,
                                    "code": (lines[r.lineno - 1].strip()[:120]
                                             if r.lineno - 1 < len(lines) else "")})

    def has_read(node):
        return any(id(x) in read_ids for x in ast.walk(node))

    def is_exec(node):
        return any(isinstance(x, ast.Call) and _py_call_name(x) in _PY_EXEC
                   for x in ast.walk(node))

    cvars: set[str] = set(handles)
    stmts = [n for n in ast.walk(tree) if isinstance(n, (ast.Assign, ast.AnnAssign,
                                                         ast.AugAssign, ast.For,
                                                         ast.comprehension))]
    for _ in range(6):
        grew = False
        for n in stmts:
            if isinstance(n, (ast.For, ast.comprehension)):
                src, tg = n.iter, [n.target]
            else:
                src = n.value
                tg = (n.targets if isinstance(n, ast.Assign) else [n.target])
            if src is None:
                continue
            if (has_read(src) or (_py_names(src) & cvars)) and not is_exec(src):
                for t in tg:
                    for nm in _py_names(t):
                        if nm not in cvars:
                            cvars.add(nm)
                            grew = True
        if not grew:
            break

    def tainted(node):
        return has_read(node) or bool(_py_names(node) & cvars)

    parents = {}
    for p in ast.walk(tree):
        for ch in ast.iter_child_nodes(p):
            parents[id(ch)] = p

    def negated(x, unit) -> bool:
        """Is text test `x` an ABSENCE check within `unit`? (`not in`, `not
        re.search(...)`, assertNotIn / assertNotRegex / assertFalse)"""
        if isinstance(x, ast.Compare) and all(isinstance(o, ast.NotIn) for o in x.ops):
            pol = True
        else:
            pol = False
        cur = x
        while id(cur) in parents and cur is not unit:
            p = parents[id(cur)]
            if isinstance(p, ast.UnaryOp) and isinstance(p.op, ast.Not):
                pol = not pol
            if isinstance(p, ast.Call) and _py_call_name(p) in (
                    "assertNotIn", "assertNotRegex", "assertFalse", "assertIsNone"):
                pol = not pol
            cur = p
        return pol

    def positive_text(unit) -> bool:
        """Does `unit` contain a POSITIVE test on the target's text?"""
        found = False
        for x in ast.walk(unit):
            tt = False
            if isinstance(x, ast.Compare) and any(isinstance(o, (ast.In, ast.NotIn))
                                                  for o in x.ops) and tainted(x):
                tt = True
            elif isinstance(x, ast.Call) and tainted(x) and (
                    _py_call_name(x) in _PY_TEXT_METHODS
                    or _py_call_name(x) in ("assertIn", "assertNotIn", "assertRegex",
                                            "assertNotRegex")):
                tt = True
            if tt:
                found = True
                if not negated(x, unit):
                    return True
        if not found and tainted(unit):
            # e.g. `assert has_x` where has_x = "x" in src: positive unless negated
            return not any(isinstance(x, ast.UnaryOp) and isinstance(x.op, ast.Not)
                           for x in ast.walk(unit))
        return False

    units = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Assert):
            units.append(n)
        elif isinstance(n, ast.Expr) and isinstance(n.value, ast.Call) and (
                _py_call_name(n.value).startswith("assert")
                or _py_call_name(n.value) in ("check", "expect", "ok")):
            units.append(n)
        elif isinstance(n, ast.Lambda) and isinstance(parents.get(id(n)), (ast.Tuple, ast.List)):
            units.append(n)      # a CASES-table thunk: ("desc", lambda: ..., want)
    for n in units:
        body = n.body if isinstance(n, ast.Lambda) else n
        if tainted(body) and not is_exec(body):
            if positive_text(body):
                res["text_asserts"] += 1
                if len(res["examples"]) < 4:
                    res["examples"].append(
                        f"line {n.lineno}: " + " ".join(
                            (lines[n.lineno - 1] if n.lineno - 1 < len(lines) else "").split())[:140])
            else:
                res["absence_asserts"] = res.get("absence_asserts", 0) + 1
        else:
            res["behavior_asserts"] += 1
    return res


def _is_py_read(node, toks, tvars, direct=False, open_only=False) -> bool:
    """A call that reads a target FILE's text (open/io.open/codecs.open,
    Path(..).read_text/read_bytes, inspect.getsource on a target name)."""
    calls = [node] if direct else [x for x in ast.walk(node) if isinstance(x, ast.Call)]
    for c in calls:
        if not isinstance(c, ast.Call):
            continue
        cn = _py_call_name(c)
        args = list(c.args) + [k.value for k in c.keywords]

        def refs(x):
            return any(_lit_refs_target(s, toks) for s in _py_strs(x)) or bool(
                _py_names(x) & tvars)
        if cn == "open" and args and refs(args[0]):
            return True
        if open_only:
            continue
        if cn in ("read_text", "read_bytes") and isinstance(c.func, ast.Attribute) \
                and refs(c.func.value):
            return True
        if cn in ("getsource", "getsourcelines"):
            return True
    return False


# --------------------------------------------------------------------------
# driver
# --------------------------------------------------------------------------
def scan_text(text: str, fixture_name: str, targets, strict: bool = False) -> dict:
    """Scan ONE fixture's text.

    verdict:
      "source-text"  POSITIVE assertions on the target's text (presence /
                     match: `src.includes(x)`, `/re/.test(src)`, `"x" in src`)
                     AND either the target is never imported/executed, or those
                     assertions are >= the behavioural ones (dominated).  NO-GO.
      "mixed"        some positive source-text assertions alongside a MAJORITY
                     of behavioural ones on an imported target. Advisory at the
                     gate/preflight; a FAILURE in `strict` mode (the authoring
                     self-check), so a refine round can never "kill" a mutant by
                     adding a regex over the mutated line.
      "ok"           none. ABSENCE checks (`!src.includes('@prisma')`, `"x" not
                     in src`) are structural constraints (a banned import, a
                     removed call) a behavioural test cannot observe; they are
                     counted (absence_asserts) but never flagged.
    """
    name = str(fixture_name)
    if name.endswith(".py"):
        r = scan_py(text, targets)
    elif name.endswith(JS_EXTS):
        r = scan_js(text, targets)
    else:
        return {"fixture": name, "verdict": "ok", "reason": "not a JS/TS/Python fixture"}
    r["fixture"] = name
    r["imports_target"] = sorted(set(r.get("imports_target") or []))
    r.setdefault("absence_asserts", 0)
    n_txt, n_beh = r["text_asserts"], r["behavior_asserts"]
    never = not r["imports_target"]
    if r["target_reads"] and n_txt > 0:
        dominated = never or n_txt >= n_beh
        r["verdict"] = "source-text" if (dominated or strict) else "mixed"
        first = r["target_reads"][0]
        r["reason"] = (
            f"{name} reads the target's SOURCE TEXT ({len(r['target_reads'])} "
            f"read(s), e.g. line {first['line']}: `{first['code']}`) and {n_txt} "
            f"assertion(s) test that text for PRESENCE/MATCH (includes/regex/`in`) "
            f"instead of behaviour"
            + (" -- and the target is never imported or executed, so NOT ONE case "
               "runs the code under test" if never else
               f" (vs {n_beh} behavioural assertion(s)"
               + (" -- source-text checks dominate)" if n_txt >= n_beh else ")"))
            + ". That pins the source SHAPE of one implementation (a proxy): a "
              "correct fix written differently fails it, a wrong fix containing "
              "the strings passes it, and a regex over a mutated line 'kills' "
              "the mutant by textual coincidence.")
    else:
        r["verdict"] = "ok"
        r["reason"] = ("no presence/match assertion on the target's source text"
                       + (f" ({len(r['target_reads'])} read(s); "
                          f"{r['absence_asserts']} absence check(s))"
                          if r["target_reads"] else ""))
    return r


_TEST_FILES_RE = re.compile(r"""^\s*TEST_FILES=(?:"([^"]*)"|'([^']*)'|(\S+))""", re.M)
_RUN_FILE_RE = re.compile(
    r"(?:^|[|;&(\s])(?:python3?|node|tsx|pytest|deno|bun|\"?\$\{?\w+\}?\"?)"
    r"(?:\s+-{1,2}[\w=-]+)*\s+([\w./-]+\.(?:py|[cm]?[jt]sx?))\b", re.M)


def fixture_files(worktree, verify_cmd: str = "bash verify.sh") -> list[str]:
    """Worktree-relative fixture files the verify runs as TESTS: the manifest's
    `fixture`, verify.sh's TEST_FILES, files it hands an interpreter/runner, and
    the default fixture names when present. Harness machinery
    (check_literals.py, auto-harness-check.py, refimpl.py) is excluded."""
    wt = Path(worktree)
    out: list[str] = []

    def add(rel):
        rel = str(rel).strip().lstrip("./") if not str(rel).startswith("../") else str(rel)
        if not rel or rel.rsplit("/", 1)[-1] in NOT_FIXTURES:
            return
        if (wt / rel).is_file() and rel not in out:
            out.append(rel)

    try:
        d = json.loads((wt / ".dispatch-harness.json").read_text())
        if isinstance(d, dict) and d.get("fixture"):
            add(d["fixture"])
    except Exception:
        pass
    texts = [verify_cmd or ""]
    for m in re.finditer(r"([\w./-]+\.sh)\b", verify_cmd or ""):
        p = wt / m.group(1)
        if p.is_file():
            try:
                texts.append(p.read_text(errors="replace"))
            except Exception:
                pass
    for t in texts:
        for m in _TEST_FILES_RE.finditer(t):
            for f in (m.group(1) or m.group(2) or m.group(3) or "").split():
                if "$" not in f:
                    add(f)
        for m in _RUN_FILE_RE.finditer(t):
            add(m.group(1))
    for f in DEFAULT_FIXTURES:
        add(f)
    return out


def targets_from(worktree, diff_text: str = "", task_text: str = "") -> list[str]:
    """Code targets: files in the diff, the manifest's declared target, and
    (for a hand-scaffolded worktree with neither) the backticked paths on
    TASK.md's scope line (``Only edit `x` ...``)."""
    out = []
    for line in (task_text or "").splitlines():
        if re.search(r"\bonly edit\b|\bentry point\b|^\s*TARGET\b", line, re.I):
            out += re.findall(r"`([\w./@-]+\.[A-Za-z]{1,5})(?::\d+)?`", line)
    for m in re.finditer(r"^\+\+\+ b/(\S+)", diff_text or "", re.M):
        out.append(m.group(1))
    for m in re.finditer(r"^diff --git a/\S+ b/(\S+)", diff_text or "", re.M):
        out.append(m.group(1))
    try:
        d = json.loads((Path(worktree) / ".dispatch-harness.json").read_text())
        if isinstance(d, dict) and d.get("target"):
            out.append(str(d["target"]))
    except Exception:
        pass
    seen, res = set(), []
    for t in out:
        name = t.rsplit("/", 1)[-1]
        if t in seen or name in NOT_FIXTURES or name in DEFAULT_FIXTURES:
            continue
        seen.add(t)
        if _is_code_target(t):
            res.append(t)
    return res


def scan_worktree(worktree, verify_cmd: str = "bash verify.sh",
                  diff_text: str = "", targets=None, strict: bool = False,
                  task_text: str | None = None) -> dict:
    """Scan every fixture of a worktree. Returns
    {"verdict": "source-text"|"mixed"|"ok"|"n/a", "reason", "fix", "fixtures": [...]}.
    Fail-OPEN on its own errors ("n/a"): this is one more check stacked on a
    gate that already measures relevance, never the only one."""
    try:
        wt = Path(worktree)
        if task_text is None:
            try:
                task_text = (wt / "TASK.md").read_text(errors="replace")
            except Exception:
                task_text = ""
        tg = list(targets) if targets else targets_from(wt, diff_text, task_text)
        tg = [t for t in tg if t.rsplit("/", 1)[-1] not in NOT_FIXTURES]
        fx = fixture_files(wt, verify_cmd)
        fx = [f for f in fx if f not in tg]
        if not tg or not fx:
            return {"verdict": "n/a", "reason": "no code target or no fixture found",
                    "fixtures": [], "targets": tg}
        rows = []
        for f in fx:
            try:
                rows.append(scan_text((wt / f).read_text(errors="replace"), f, tg,
                                      strict=strict))
            except Exception as e:
                rows.append({"fixture": f, "verdict": "ok",
                             "reason": f"unreadable: {type(e).__name__}"})
        for verdict in ("source-text", "mixed"):
            bad = [r for r in rows if r.get("verdict") == verdict]
            if bad:
                return {"verdict": verdict, "reason": " | ".join(r["reason"] for r in bad),
                        "fix": FIX_TEXT, "fixtures": rows, "targets": tg,
                        "examples": [e for r in bad for e in r.get("examples", [])][:4]}
        return {"verdict": "ok", "reason": "; ".join(r["reason"] for r in rows),
                "fixtures": rows, "targets": tg}
    except Exception as e:
        return {"verdict": "n/a", "reason": f"scan failed: {type(e).__name__}: {e}",
                "fixtures": []}


if __name__ == "__main__":
    import argparse
    import sys
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("worktree")
    ap.add_argument("--verify", default="bash verify.sh")
    ap.add_argument("--target", action="append", default=[])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true",
                    help="any positive source-text assertion fails (authoring mode)")
    a = ap.parse_args()
    rec = scan_worktree(a.worktree, a.verify, targets=a.target or None, strict=a.strict)
    if a.json:
        print(json.dumps(rec, indent=2))
    else:
        print(f"source-text-harness: {rec['verdict'].upper()}  {rec['reason']}")
        for e in rec.get("examples", []):
            print(f"  {e}")
        if rec.get("fix"):
            print(f"  FIX: {rec['fix']}")
    sys.exit(1 if rec["verdict"] == "source-text" else 0)
