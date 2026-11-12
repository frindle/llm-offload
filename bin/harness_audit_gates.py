#!/usr/bin/env python3
"""harness_audit_gates -- spec/harness defect gates from the 2026-10-09 research
(~/.ollama-dispatch/merge-queue/2026-10-09-spec-defect-research.md), wave 3.

Pure, CPU-only functions (no GPU, no network).  Wired into ollama-dispatch-preflight as
WARN-mode checks (nothing here is blocking until `harness-audit-replay.py` has measured its fire
rate on the preserved worktrees) plus one execution-time CLI:

  E  freshness_snapshot / freshness_drift / revalidate    re-check blob SHAs + the fast gates
                                                          (creation-exists, anchors, NOP) when a
                                                          job is popped from the queue
  C  determinism_findings / flake_lint                    3x verify must agree; flake-source lint
  A  untraceable_assertions                               every identifier/string/constant the
                                                          fixture asserts on the target must trace
                                                          to TASK.md or the base file
  D  trivial_variants / run_trivial_battery               literals-only patch, no-op exports,
                                                          return None, touch-only must stay RED
  B  constant_conflicts                                   prose constants vs fixture constants
                                                          (refine trigger only)

CLI:  python3 harness_audit_gates.py revalidate <worktree> [--verify CMD] [--timeout S]
      exit 0 = fresh or still-valid, 3 = SPEC_DEFECT (park, 0 GPU), 4 = stale and now green/red wrong.
Finding = {"code","token","detail","line"}.  Every check can be silenced per line with
`spec-ok` / `flake-ok` in a comment.
"""
import ast
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

SNAP_FILES = ("verify.sh", "refimpl.py", "TASK.md", "check_literals.py", "test_fixture.py",
              "verify.test.ts", "verify.test.mts")


def _read(p):
    try:
        return Path(p).read_text(errors="replace")
    except OSError:
        return ""


def _git(wt, *a, timeout=60):
    try:
        r = subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


def _sha(b):
    return hashlib.sha256(b).hexdigest()


def _hg():
    try:
        import importlib.util as iu
        sp = iu.spec_from_file_location("dispatch_harness_gates",
                                        Path(__file__).resolve().parent / "dispatch_harness_gates.py")
        m = iu.module_from_spec(sp)
        sp.loader.exec_module(m)
        return m
    except Exception:
        return None


# ===================================================================================== E
def freshness_snapshot(wt, target=None):
    """What the preflight GO verdict was measured against: worktree HEAD, the target's blob at
    HEAD (base side), and the sha256 of every harness file."""
    wt = Path(wt)
    snap = {"head": (_git(wt, "rev-parse", "HEAD") or "").strip() or None, "files": {}}
    if target:
        snap["target"] = target
        snap["target_blob"] = (_git(wt, "rev-parse", f"HEAD:{target}") or "").strip() or None
    for n in SNAP_FILES:
        p = wt / n
        if p.is_file():
            snap["files"][n] = _sha(p.read_bytes())
    return snap


def freshness_drift(wt, snap):
    """[{item, was, now}] where the worktree no longer matches the snapshot. [] = fresh."""
    wt = Path(wt)
    now = freshness_snapshot(wt, snap.get("target"))
    out = []
    if snap.get("head") and now["head"] != snap["head"]:
        out.append({"item": "HEAD", "was": snap["head"][:12], "now": (now["head"] or "?")[:12]})
    if snap.get("target") and snap.get("target_blob") != now.get("target_blob"):
        out.append({"item": f"base blob of {snap['target']}",
                    "was": (snap.get("target_blob") or "absent")[:12],
                    "now": (now.get("target_blob") or "absent")[:12]})
    for n, h in (snap.get("files") or {}).items():
        if now["files"].get(n) != h:
            out.append({"item": n, "was": h[:12], "now": (now["files"].get(n) or "missing")[:12]})
    for n in now["files"]:
        if n not in (snap.get("files") or {}):
            out.append({"item": n, "was": "absent", "now": now["files"][n][:12]})
    return out


def revalidate(wt, verify_cmd="bash verify.sh", timeout=300, run=None):
    """Queue-pop re-check.  fresh -> nothing to do (NO verify run).  Drift -> re-run the FAST gates:
    base ancestry + creation-exists + refimpl anchors (static) and the NOP check (the verify must still
    be RED on the untouched tree).  Returns {"status": fresh|revalidated|SPEC_DEFECT|NOP_GREEN|no-snapshot,
    "drift": [...], "findings": [...]}."""
    wt = Path(wt)
    try:
        sf = json.loads((wt / ".preflight-state.json").read_text())
        snap = sf.get("freshness")
    except (OSError, ValueError):
        snap = None
    if not snap:
        return {"status": "no-snapshot", "drift": [], "findings": []}
    drift = freshness_drift(wt, snap)
    if not drift:
        return {"status": "fresh", "drift": [], "findings": []}
    findings = []
    hg = _hg()
    if hg is not None:
        try:
            fs = hg.spec_defects(wt, target=snap.get("target"), verify_green=None)
            # BASE_POISONED_BY_SEAL is an ADMISSION finding (a fresh dispatch on a poisoned base). A queue auto-fix /
            # refine round launches on exactly such a tree BY DESIGN (the round seal commits the previous round's work, which
            # moves HEAD and so trips this drift check) -- reporting it here would park every continuation round.
            fs = [f for f in fs if f.get("code") not in ("ALREADY_SATISFIED", "BASE_POISONED_BY_SEAL")]
            findings += [{"code": f["code"], "token": f.get("path"), "detail": f["message"]} for f in fs]
        except Exception as e:
            findings.append({"code": "REVALIDATE_ERROR", "token": "", "detail": str(e)[:200]})
    if findings:
        return {"status": "SPEC_DEFECT", "drift": drift, "findings": findings}
    # NOP: untouched tree must still fail the verify (a drifted base can make it green = nothing to do)
    try:
        r = (run or (lambda c: subprocess.run(c, shell=True, cwd=str(wt), capture_output=True, text=True,
                                              timeout=timeout)))(verify_cmd)
        rc = r.returncode
        out = (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return {"status": "revalidated", "drift": drift, "findings": [
            {"code": "NOP_TIMEOUT", "token": "", "detail": f"verify timed out after {timeout}s"}]}
    if rc == 0 or "VERIFY_OK" in out:
        return {"status": "NOP_GREEN", "drift": drift, "findings": [
            {"code": "ALREADY_SATISFIED", "token": "", "detail":
             "the verify is GREEN on the untouched tree after the base moved: nothing left to build"}]}
    return {"status": "revalidated", "drift": drift, "findings": []}


# ===================================================================================== C
def verdict_signature(rc, out):
    """Stable verdict of one verify run: (pass?, sorted failing-case names)."""
    names = set(re.findall(r"^\s*not ok \d+ - (.+?)\s*(?:#.*)?$", out or "", re.M))
    names |= set(re.findall(r"^\s*FAIL(?:ED)?[:\s]+([\w./:\[\]-]+)", out or "", re.M))
    names |= set(re.findall(r"^(?:FAILED|ERROR)\s+(\S+::\S+)", out or "", re.M))
    return (rc == 0 or "VERIFY_OK" in (out or ""), tuple(sorted(names)))


def determinism_findings(runs):
    """runs = [(rc, output), ...] of the SAME verify on the SAME tree.  Identical verdicts and
    identical failing-case sets are required."""
    sigs = [verdict_signature(rc, out) for rc, out in runs]
    if len(set(sigs)) <= 1:
        return []
    return [{"code": "NONDETERMINISTIC_VERIFY", "token": "", "line": 0,
             "detail": "verify gave different verdicts on the same tree: "
                       + " | ".join(f"run{i + 1}: {'PASS' if s[0] else 'FAIL'} {list(s[1])[:3]}"
                                    for i, s in enumerate(sigs))}]


_FLAKE_RES = (
    (r"\bDate\.now\s*\(|\bnew Date\s*\(\s*\)", "wall clock (Date.now / new Date())"),
    (r"\bMath\.random\s*\(|\brandom\.(?:random|randint|choice|shuffle|sample|uniform)\s*\(", "random"),
    (r"\btime\.time\s*\(|\bdatetime\.(?:now|utcnow|today)\s*\(|\bdate\.today\s*\(", "wall clock"),
    (r"\bos\.listdir\s*\(|\breaddirSync\s*\(|\bglob\.glob\s*\(|\bos\.walk\s*\(", "unordered directory listing"),
    (r"\bfor\s+\w+\s+in\s+(?:set\s*\(|\{[^}:]*\}\s*:)|\blist\s*\(\s*set\s*\(", "iteration over a set"),
    (r"\bsetTimeout\s*\(|\btime\.sleep\s*\(", "timing-based wait"),
    (r"\buuid\.?uuid[14]\s*\(|\brandomUUID\s*\(|\bcrypto\.randomBytes\s*\(", "random id"),
    (r"\bfetch\s*\(\s*['\"`]https?://|\brequests\.(?:get|post)\s*\(|\bhttp\.client\b", "network call"),
)


def flake_lint(text, name="fixture"):
    """[finding] -- flake sources in a fixture/refimpl.  WARN-grade (medium FP risk)."""
    out = []
    for i, ln in enumerate((text or "").splitlines(), 1):
        if "flake-ok" in ln or ln.strip().startswith(("#", "//", "*")):
            continue
        for rx, why in _FLAKE_RES:
            if re.search(rx, ln):
                out.append({"code": "FLAKE_SOURCE", "token": ln.strip()[:80], "line": i,
                            "detail": f"{name}:{i}: {why}"})
                break
    return out


# ===================================================================================== A
_PY_BUILTIN_OK = {"True", "False", "None", "self", "assertEqual", "assertTrue", "assertFalse", "raises",
                  "main", "run", "test"}


def _stem(target):
    return Path(target or "").stem


_WS_RE = re.compile(r"\s+")
_URL_NOISE = {"http", "https", "www", "html", "json", "null", "true", "false", "undefined", "example",
              "test", "this", "that", "with", "from", "have", "must", "should", "string", "value"}


def _norm_ws(t):
    return _WS_RE.sub(" ", (t or "").strip()).casefold()


def _str_parts(tok):
    """Meaningful sub-tokens of a string literal: dotted hosts and words >= 4 chars that contain a letter
    (URL scaffolding like http/www/com is noise)."""
    out = []
    for w in re.findall(r"[A-Za-z0-9_][A-Za-z0-9_-]{3,}", tok):
        if re.search(r"[A-Za-z]", w) and w.casefold() not in _URL_NOISE:
            out.append(w.casefold())
    return out


def _str_traced(tok, task, base):
    """A string literal is traced when ANY of: it appears verbatim; its trimmed / whitespace-collapsed /
    case-folded form appears (a whitespace or case variant of a spec'd token); or any meaningful sub-token
    (host, URL part, word >= 4 chars) of it appears in TASK.md or the base target. A truly invented value
    ('QX-7741-ZETA') shares no sub-token with either and stays untraced."""
    if tok in task or tok in base or tok.lower() in task.lower():
        return True
    n = _norm_ws(tok)
    nt, nb = _norm_ws(task), _norm_ws(base)
    if n and (n in nt or n in nb):
        return True
    for w in _str_parts(tok):
        if w in nt or w in nb:
            return True
    for d in re.findall(r"(?<!\d)\d{3,}(?!\d)", tok):      # an id embedded in a path/URL ('/files/100/x.pdf')
        if re.search(r"(?<!\d)" + d + r"(?!\d)", nt + " " + nb):
            return True
    return False


# a line whose string literal is an INPUT to a reject/negative assertion (absence / throws / 4xx), not an expected value
_NEG_LINE_RE = re.compile(
    r"!\s*[\w.$\[\]()]*\.(?:includes|startsWith|endsWith|test)\s*\(|\.not\.|\bnot\s+in\b|notEqual|notStrictEqual|"
    r"doesNotMatch|assertNot\w*|assertRaises|pytest\.raises|\.throws?\s*\(|\.rejects?\b|toThrow|\bassert\.ok\s*\(\s*!|"
    r"\bassert\s+not\b|status[^\n]{0,40}\b4\d\d\b|\b4\d\d\b[^\n]{0,40}status", re.I)
_PROSE_STR_RE = re.compile(
    r"^(?=(?:\S+\s+){3,}\S)[^\n]*\b(?:must|should|shall|expected?|not|never|does|did|returns?|when|on)\b[^\n]*$", re.I)


def _traced(tok, task, base, kind):
    if kind == "str":
        return _str_traced(tok, task, base)
    return re.search(r"(?<![\w$])" + re.escape(tok) + r"(?![\w$])", task) is not None or \
        re.search(r"(?<![\w$])" + re.escape(tok) + r"(?![\w$])", base) is not None


def _py_tokens(src, target):
    tree = ast.parse(src)
    stem = _stem(target)
    ids, mods = set(), set()
    for n in ast.walk(tree):
        if isinstance(n, ast.ImportFrom) and n.module and n.module.split(".")[-1] == stem:
            ids |= {a.name for a in n.names if a.name != "*"}
        elif isinstance(n, ast.Import):
            for a in n.names:
                if a.name.split(".")[-1] == stem:
                    mods.add(a.asname or a.name.split(".")[-1])
    toks = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name) and n.value.id in mods:
            toks.append((n.attr, "id", n.lineno))
    for i in sorted(ids):
        toks.append((i, "id", 0))
    asserts = [n for n in ast.walk(tree) if isinstance(n, ast.Assert)] + \
        [n for n in ast.walk(tree) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
         and (n.func.attr.startswith("assert") or n.func.attr in ("raises",))]
    for a in asserts:
        for c in ast.walk(a):
            if isinstance(c, ast.Constant):
                v = c.value
                if isinstance(v, str) and len(v) >= 3 and not v.isspace():
                    toks.append((v, "str", c.lineno))
                elif isinstance(v, (int, float)) and not isinstance(v, bool) and (
                        isinstance(v, float) or abs(v) >= 10):
                    toks.append((repr(v), "num", c.lineno))
    return toks


def _js_tokens(src, target):
    stem = _stem(target)
    toks = []
    for m in re.finditer(r"import\s*\{([^}]*)\}\s*from\s*['\"]([^'\"]+)['\"]", src):
        if Path(m.group(2)).stem == stem or m.group(2).endswith(stem):
            for nm in m.group(1).split(","):
                nm = nm.strip().split(" as ")[0].strip()
                if nm:
                    toks.append((nm, "id", src[:m.start()].count("\n") + 1))
    for m in re.finditer(r"(?:expect|assert(?:\.\w+)?)\s*\(([^;\n]*)", src):
        ln = src[:m.start()].count("\n") + 1
        seg = m.group(0)
        for s in re.findall(r"""(['"`])((?:\\.|(?!\1).){3,}?)\1""", seg):
            toks.append((s[1], "str", ln))
        for nmb in re.findall(r"(?<![\w.])(\d+\.\d+|\d{2,})(?![\w.])", seg):
            toks.append((nmb, "num", ln))
    return toks


_TITLE_LINE_RE = re.compile(r"\s*(?:(?:test|it|describe|suite)(?:\.\w+)*\s*\(|(?:async\s+)?def\s+test_\w*|class\s+Test\w*)")
_ASSERT_LINE_RE = re.compile(r"\bexpect\s*\(|\bassert\w*\b|\.(?:toBe|toEqual|toThrow|toContain|toMatch)\w*\(|\bt\.(?:is|equal|deepEqual|ok|throws)\(")


def untraceable_assertions(fixture_text, task_text, base_text, target, lang="python"):
    """[finding] UNTRACEABLE_ASSERTION: a name imported from the target, a string constant or a
    significant number asserted by the fixture that appears in NEITHER TASK.md NOR the base target.
    The finding itself is grade-less: advisory for the audit callers, but BLOCKING at the strict tier of
    harness_admission.admit (after its allowlist), so it must stay precise. A string counts as traced via
    its verbatim / whitespace+case-folded form or any meaningful sub-token (_str_traced); strings that are
    INPUTS to a negative assertion (!x.includes(..), .not., throws, 4xx status) and assertion-message prose
    are not expected values and are skipped. A literal also DEFINED on a non-assertion fixture line (mock return,
    setup data, constant) is traced (data flow). Lines carrying `spec-ok` are exempt."""
    lines = (fixture_text or "").splitlines()
    if str(lang).lower() not in ("python", "py"):
        # comment-only lines (the scaffold's `// import { theFn } ...` hint) are not assertions;
        # blank them in place so line numbers stay stable
        fixture_text = "\n".join("" if re.match(r"\s*//", l) else l for l in lines)
    try:
        toks = (_py_tokens(fixture_text, target) if str(lang).lower() in ("python", "py")
                else _js_tokens(fixture_text, target))
    except SyntaxError:
        return []
    # SETUP-DEFINED values: a literal the fixture itself defines OUTSIDE any assertion line (mock return, stub data,
    # constant: `_orders=[{id:100}]`, `generateReceiptPdf = () => 'fake-pdf'`) and then asserts on is data FLOW, not an
    # invented expectation. An expected literal that appears ONLY on assertion lines stays flagged.
    # TEST TITLES are prose, not data (2026-10-14, 1a53991: `test('returns bronze tier for cardValue 10-49', ...)`
    # "defined" 'bronze' on a non-assertion line, so the invented tier name traced as setup data). Kill switch:
    # DISPATCH_TRACE_TITLES=off restores the old behaviour.
    _titles = os.environ.get("DISPATCH_TRACE_TITLES", "on").strip().lower() not in ("off", "0", "no", "false")
    setup_text = "\n".join(l for l in lines if not _ASSERT_LINE_RE.search(l) and not re.match(r"\s*(?://|#)", l)
                           and not (_titles and _TITLE_LINE_RE.match(l)))
    base_ids = base_text or ""                     # imported NAMES must trace to the target, never to the fixture itself
    base_text = base_ids + "\n" + setup_text
    out, seen = [], set()
    for tok, kind, ln in toks:
        if (tok, kind) in seen or tok in _PY_BUILTIN_OK:
            continue
        seen.add((tok, kind))
        if ln and 0 < ln <= len(lines) and "spec-ok" in lines[ln - 1]:
            continue
        if kind == "str" and ln and 0 < ln <= len(lines) and (
                _NEG_LINE_RE.search(lines[ln - 1]) or _PROSE_STR_RE.match(tok)):
            continue          # negative-assertion input / assertion-message prose: not an expected value
        if kind == "id":
            if _traced(tok, task_text or "", base_ids, "id"):
                continue
        elif kind == "num":
            if _traced(tok, task_text or "", base_text or "", "id"):
                continue
        elif _traced(tok, task_text or "", base_text or "", kind):
            continue
        out.append({"code": "UNTRACEABLE_ASSERTION", "token": tok[:80], "line": ln,
                    "detail": f"fixture asserts on {kind} {tok[:60]!r} (line {ln}) that appears in neither "
                              f"TASK.md nor the base target"})
    return out


# ===================================================================================== B
_UNITS = r"(ms|milliseconds?|seconds?|secs?|minutes?|hours?|days?|px|pixels?|chars?|characters?|bytes?|" \
         r"items?|spaces?|retries|attempts?|times|%|percent|rows?|entries|elements?)"
_NUM_UNIT_RE = re.compile(r"(?<![\w.])(\d+(?:\.\d+)?)\s*" + _UNITS + r"\b", re.I)


def _unit_norm(u):
    u = u.lower()
    for k, v in (("millisecond", "ms"), ("ms", "ms"), ("second", "s"), ("sec", "s"), ("minute", "min"),
                 ("hour", "h"), ("day", "d"), ("pixel", "px"), ("px", "px"), ("char", "char"),
                 ("byte", "byte"), ("item", "item"), ("space", "space"), ("retr", "retry"),
                 ("attempt", "retry"), ("time", "retry"), ("percent", "%"), ("%", "%"),
                 ("row", "row"), ("entr", "row"), ("element", "item")):
        if u.startswith(k):
            return v
    return u


def constant_conflicts(task_text, fixture_text):
    """[finding] CONSTANT_CONFLICT: the prose and the fixture use the same unit with DISJOINT numeric
    values (SWE-bench Pro: prompt says one leading space, the test needs two).  Refine trigger only."""
    spec, fx = {}, {}
    for m in _NUM_UNIT_RE.finditer(task_text or ""):
        spec.setdefault(_unit_norm(m.group(2)), set()).add(float(m.group(1)))
    for ln in (fixture_text or "").splitlines():
        if "spec-ok" in ln:
            continue
        for m in _NUM_UNIT_RE.finditer(ln):
            fx.setdefault(_unit_norm(m.group(2)), set()).add(float(m.group(1)))
    out = []
    for u in sorted(set(spec) & set(fx)):
        if not (spec[u] & fx[u]):
            out.append({"code": "CONSTANT_CONFLICT", "token": u, "line": 0,
                        "detail": f"TASK.md says {sorted(spec[u])} {u}; the fixture uses {sorted(fx[u])} {u}"})
    return out


# ===================================================================================== D
def _brace_body(src, open_idx):
    """Index of the '}' matching the '{' at open_idx (naive: skips quotes), or -1."""
    depth, i, q = 0, open_idx, None
    while i < len(src):
        c = src[i]
        if q:
            if c == "\\":
                i += 1
            elif c == q:
                q = None
        elif c in "'\"`":
            q = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return -1


def _literal_names(literals):
    names = []
    literals = [(l[1] if isinstance(l, (tuple, list)) else l) for l in (literals or ())]
    for lit in literals:
        for m in re.finditer(r"(?:def|function|const|class|export\s+(?:async\s+)?function)\s+([A-Za-z_]\w*)", lit):
            names.append(m.group(1))
        m = re.fullmatch(r"([A-Za-z_]\w*)\s*\(?", lit.strip())
        if m:
            names.append(m.group(1))
    seen = []
    for n in names:
        if n not in seen:
            seen.append(n)
    return seen


def trivial_variants(base_text, target, literals=(), lang="python", creation=False):
    """[(name, new_text)] degenerate versions of the target that a behavioural verify MUST reject:
    touch-only, literals-only patch, empty/stub file (creation), return-None / no-op bodies for the
    functions the spec names.  Variants identical to the base are dropped."""
    py = str(lang).lower() in ("python", "py")
    cm = "# " if py else "// "
    base = base_text or ""
    out = []
    out.append(("touch-only", base + "\n" + cm + "touched\n"))
    lits = [l for l in ((x[1] if isinstance(x, (tuple, list)) else x) for x in (literals or ())) if str(l).strip()]
    if lits:
        if py:
            dead = "\n_DEAD_LITERALS = " + json.dumps(list(lits)) + "\n"
        else:
            dead = "\nconst __deadLiterals = " + json.dumps(list(lits)) + ";\n"
        out.append(("literals-only", base + dead + "".join(cm + str(l) + "\n" for l in lits)))
    names = _literal_names(lits)
    if creation or not base.strip():
        out.append(("empty-stub", "" if py else "export {};\n"))
        if names:
            if py:
                out.append(("noop-exports", "".join(f"def {n}(*a, **k):\n    return None\n\n" for n in names)))
            else:
                out.append(("noop-exports", "".join(f"export function {n}(..._a: any[]): any {{ return undefined; }}\n"
                                                    for n in names)))
    if base.strip():
        if py:
            try:
                tree = ast.parse(base)
                lines = base.splitlines(keepends=True)
                edits = []
                for n in ast.walk(tree):
                    if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and (not names or n.name in names):
                        b0 = n.body[0]
                        s = b0.lineno - 1
                        if isinstance(b0, ast.Expr) and isinstance(getattr(b0, "value", None), ast.Constant) \
                                and isinstance(b0.value.value, str) and len(n.body) > 1:
                            s = n.body[1].lineno - 1
                        edits.append((s, n.end_lineno, n.body[0].col_offset))
                if edits:
                    new = list(lines)
                    for s, e, col in sorted(edits, reverse=True):
                        new[s:e] = [" " * col + "return None\n"]
                    txt = "".join(new)
                    try:
                        ast.parse(txt)
                        out.append(("return-none", txt))
                    except SyntaxError:
                        pass
            except SyntaxError:
                pass
        elif names:
            new, changed = base, False
            for n in names:
                for m in re.finditer(r"function\s+" + re.escape(n) + r"\s*(?:<[^>]*>)?\s*\([^)]*\)[^{;]*\{", new):
                    close = _brace_body(new, m.end() - 1)
                    if close > 0:
                        new = new[:m.end()] + " return undefined as any; " + new[close:]
                        changed = True
                        break
            if changed:
                out.append(("noop-bodies", new))
    seen, res = set(), []
    for n, t in out:
        if t != base and n not in seen:
            seen.add(n)
            res.append((n, t))
    return res


def run_trivial_battery(wt, target, variants, run_verify, max_variants=6):
    """Apply each degenerate variant IN PLACE, run the verify, ALWAYS restore the original bytes
    (or delete a file that did not exist).  [finding] TRIVIAL_SOLUTION_PASSES for each variant the
    verify accepts.  `run_verify()` -> (rc, output)."""
    p = Path(wt) / target
    orig = p.read_bytes() if p.is_file() else None
    out = []
    try:
        for name, text in variants[:max_variants]:
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
            rc, o = run_verify()
            if rc == 0 or "VERIFY_OK" in (o or ""):
                out.append({"code": "TRIVIAL_SOLUTION_PASSES", "token": name, "line": 0,
                            "detail": f"verify is GREEN with the degenerate target variant {name!r}: it "
                                      f"certifies something that implements nothing"})
    finally:
        if orig is None:
            try:
                p.unlink()
            except OSError:
                pass
        else:
            p.write_bytes(orig)
    return out


# ===================================================================================== CLI
def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if len(argv) >= 2 and argv[0] == "revalidate":
        wt = argv[1]
        verify, timeout = "bash verify.sh", 300
        if "--verify" in argv:
            verify = argv[argv.index("--verify") + 1]
        if "--timeout" in argv:
            timeout = int(argv[argv.index("--timeout") + 1])
        res = revalidate(wt, verify, timeout)
        print(json.dumps(res, indent=1))
        return {"fresh": 0, "revalidated": 0, "no-snapshot": 0, "SPEC_DEFECT": 3, "NOP_GREEN": 4}.get(res["status"], 0)
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main())
