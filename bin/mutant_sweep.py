#!/usr/bin/env python3
"""mutant_sweep -- equivalent-mutant detection + concrete witnesses for SURVIVING mutants.

WHY (2026-10-14, resell cardBonuses-v2): the relevance gate reported 11 surviving mutants
(`spend < 0` -> `spend <= 0`, `|| -> &&` on redundant type guards, ...) and the survivor-
feedback loop handed the author only a mutated *line*. The model had to simulate the function
in prose to learn whether the mutant was observable, and was cut off at the output cap four
times in one round (no edit) -- then the loop stopped with 'NO PROGRESS (+0 kills)'. Some of
those survivors are genuinely equivalent (no input distinguishes them); some are real holes.
Neither kind was told apart for the model, although telling them apart is a mechanical job.

WHAT: run the ORIGINAL (refimpl-applied) module and each surviving MUTANT of it over a
deterministic input sweep (every exported function; parameter candidates from the signature
and the property accesses in the body; one-at-a-time variations around a base call + seeded
random tuples) and compare results / thrown error names.
  - no input distinguishes them            -> status "equivalent"  (reported, excluded by the caller)
  - an input distinguishes them            -> status "differs" + WITNESS (the call, the original's
                                              result, the mutant's result) -- the survivor is a real
                                              hole and the author is shown the exact case to add
  - anything uncertain                     -> status "unknown" (impure/unsupported/helper-only
                                              line/timeout): treated exactly as before (fail-closed)

Fail-closed by construction: a mutant is only "equivalent" when the module is pure (no IO, clock,
randomness, async, top-level mutable state), the mutated line sits inside an EXPORTED function the
sweep calls, the original returned a non-error result on a healthy share of calls, and the runner
completed. Languages: TypeScript/JavaScript (node strip-types) and Python.

Kill switch: MUTANT_SWEEP=off (the caller treats every survivor as unknown).
Pure helpers + CLI:  python3 mutant_sweep.py --self-test
"""
from __future__ import annotations

import ast
import json
import os
import re
import subprocess
import sys
import tempfile
from pathlib import Path

SWEEP_VERSION = 1
MIN_OK_SHARE = 0.05          # the original must return a non-error result on >= 5% of calls
PER_MUTANT_TIMEOUT_S = 25


def enabled() -> bool:
    return os.environ.get("MUTANT_SWEEP", "on").strip().lower() not in ("off", "0", "no", "false")


# ---------------------------------------------------------------------------------------------
# source analysis (pure)
# ---------------------------------------------------------------------------------------------
_JS_BANNED = re.compile(
    r"\brequire\s*\(|\bawait\b|\basync\b|\bfetch\s*\(|Date\.now|new\s+Date\s*\(\s*\)|Math\.random|"
    r"performance\.|\bprocess\b|setTimeout|setInterval|localStorage|\bwindow\b|\bdocument\b|"
    r"crypto\.|console\.|\bimport\s*\(|\byield\b|\bclass\b|\beval\s*\(")
_JS_VALUE_IMPORT = re.compile(r"^\s*import\s+(?!type\b)[^;\n]*from|^\s*import\s+['\"]", re.M)
_JS_TOP_MUTABLE = re.compile(r"^(?:let|var)\s", re.M)


def _strip_js_comments(src: str) -> str:
    out, i, q, n = [], 0, None, len(src)
    while i < n:
        c = src[i]
        if q:
            out.append(c)
            if c == "\\" and i + 1 < n:
                out.append(src[i + 1]); i += 2; continue
            if c == q:
                q = None
        elif c in "'\"`":
            q = c; out.append(c)
        elif src.startswith("//", i):
            j = src.find("\n", i)
            i = n if j < 0 else j
            continue
        elif src.startswith("/*", i):
            j = src.find("*/", i + 2)
            blanked = src[i: (n if j < 0 else j + 2)]
            out.append("".join("\n" if ch == "\n" else " " for ch in blanked))
            i = n if j < 0 else j + 2
            continue
        else:
            out.append(c)
        i += 1
    return "".join(out)


def js_eligible(src: str) -> tuple[bool, str]:
    code = _strip_js_comments(src)
    m = _JS_BANNED.search(code)
    if m:
        return False, f"impure/unsupported construct `{m.group(0).strip()}`"
    if _JS_VALUE_IMPORT.search(code):
        return False, "module imports another module (not self-contained)"
    if _JS_TOP_MUTABLE.search(code):
        return False, "top-level mutable state"
    if not re.search(r"^export\s+(?:default\s+)?(?:function|const)\b", code, re.M):
        return False, "no exported function"
    return True, ""


def _match_brace(code: str, open_idx: int) -> int:
    depth, i, q, n = 0, open_idx, None, len(code)
    while i < n:
        c = code[i]
        if q:
            if c == "\\":
                i += 2; continue
            if c == q:
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


def _split_params(sig: str) -> list[str]:
    parts, depth, cur, q = [], 0, [], None
    for c in sig:
        if q:
            cur.append(c)
            if c == q:
                q = None
            continue
        if c in "'\"`":
            q = c; cur.append(c); continue
        if c in "([{<":
            depth += 1
        elif c in ")]}>":
            depth -= 1
        if c == "," and depth == 0:
            parts.append("".join(cur).strip()); cur = []
        else:
            cur.append(c)
    if "".join(cur).strip():
        parts.append("".join(cur).strip())
    return parts


def js_exported_functions(src: str) -> list[dict]:
    """[{name, params:[{name, hint, keys}], first_line, last_line}] of `export function` /
    `export const f = (..) =>` declarations. Lines are 1-based into `src`."""
    code = _strip_js_comments(src)
    out = []
    for m in re.finditer(r"^export\s+function\s+([A-Za-z_$][\w$]*)\s*(?:<[^>(]*>)?\s*\(", code, re.M):
        name, p0 = m.group(1), m.end() - 1
        depth, i = 0, p0
        while i < len(code):
            if code[i] == "(":
                depth += 1
            elif code[i] == ")":
                depth -= 1
                if depth == 0:
                    break
            i += 1
        sig = code[p0 + 1:i]
        b = code.find("{", i)
        if b < 0:
            continue
        e = _match_brace(code, b)
        if e < 0:
            continue
        body = code[b:e + 1]
        params = []
        for raw in _split_params(sig):
            raw = raw.split("=")[0].strip() if "=" in raw and not raw.startswith("{") else raw
            nm, _, hint = raw.partition(":")
            nm = nm.strip().rstrip("?").strip()
            if not re.fullmatch(r"[A-Za-z_$][\w$]*", nm):
                params.append({"name": "_", "hint": hint.strip(), "keys": [], "opaque": True})
                continue
            keys = sorted(set(re.findall(rf"\b{re.escape(nm)}\s*\??\.\s*([A-Za-z_$][\w$]*)", body))
                          | set(re.findall(rf"\b{re.escape(nm)}\s*\[\s*['\"]([\w$]+)['\"]\s*\]", body)))
            keys = [k for k in keys if k not in ("length", "toString", "valueOf", "toFixed", "map", "filter",
                                                  "reduce", "forEach", "some", "every", "includes", "slice",
                                                  "indexOf", "join", "push", "trim", "toLowerCase",
                                                  "toUpperCase", "split", "startsWith", "endsWith",
                                                  "replace", "match", "find", "sort", "concat")]
            h = hint.strip()
            for km in re.finditer(r"([A-Za-z_$][\w$]*)\??\s*:", h) if h.startswith("{") else []:
                if km.group(1) not in keys:
                    keys.append(km.group(1))
            params.append({"name": nm, "hint": h, "keys": keys, "opaque": False})
        first = code[:m.start()].count("\n") + 1
        last = code[:e].count("\n") + 1
        out.append({"name": name, "params": params, "first_line": first, "last_line": last})
    return out


def py_eligible(src: str) -> tuple[bool, str]:
    try:
        tree = ast.parse(src)
    except SyntaxError as e:
        return False, f"syntax error: {e.msg}"
    allowed = {"math", "re", "typing", "dataclasses", "decimal", "fractions", "collections", "itertools",
               "functools", "enum", "__future__", "string", "json", "operator", "statistics", "abc"}
    pub = False
    for n in ast.walk(tree):
        if isinstance(n, (ast.Import, ast.ImportFrom)):
            mods = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""]
            for md in mods:
                if md.split(".")[0] not in allowed:
                    return False, f"imports `{md}` (not in the pure allowlist)"
        elif isinstance(n, (ast.Global, ast.Nonlocal, ast.AsyncFunctionDef, ast.Await, ast.Yield, ast.YieldFrom)):
            return False, "global state / async / generator"
        elif isinstance(n, ast.Name) and n.id in ("open", "exec", "eval", "input", "print", "compile",
                                                  "__import__", "globals", "locals", "setattr", "getattr"):
            return False, f"uses `{n.id}`"
    for st in tree.body:
        if isinstance(st, ast.FunctionDef) and not st.name.startswith("_"):
            pub = True
        if isinstance(st, (ast.Assign, ast.AugAssign)) and any(
                isinstance(x, (ast.List, ast.Dict, ast.Set)) for x in ast.walk(st)):
            return False, "top-level mutable container"
    return (True, "") if pub else (False, "no public function")


def py_public_functions(src: str) -> list[dict]:
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return []
    out = []
    for st in tree.body:
        if not isinstance(st, ast.FunctionDef) or st.name.startswith("_"):
            continue
        a = st.args
        plain = list(a.posonlyargs) + list(a.args)
        ndef = len(a.defaults)
        params = []
        for i, arg in enumerate(plain):
            if arg.arg in ("self", "cls"):
                continue
            hint = ast.unparse(arg.annotation) if arg.annotation is not None else ""
            has_default = i >= len(plain) - ndef
            keys = set()
            for n in ast.walk(st):
                if (isinstance(n, ast.Subscript) and isinstance(n.value, ast.Name) and n.value.id == arg.arg
                        and isinstance(n.slice, ast.Constant) and isinstance(n.slice.value, str)):
                    keys.add(n.slice.value)
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute) and n.func.attr == "get"
                        and isinstance(n.func.value, ast.Name) and n.func.value.id == arg.arg and n.args
                        and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str)):
                    keys.add(n.args[0].value)
            params.append({"name": arg.arg, "hint": hint, "keys": sorted(keys), "optional": has_default})
        out.append({"name": st.name, "params": params, "first_line": st.lineno,
                    "last_line": getattr(st, "end_lineno", st.lineno)})
    return out


# ---------------------------------------------------------------------------------------------
# runners (executed in a subprocess; one per language)
# ---------------------------------------------------------------------------------------------
def harvest_literals(src: str):
    """Numeric and short string literals of the module(s) under test, each numeric with its +-1 and
    +-0.01 neighbours: a boundary mutant (`age >= 18` -> `> 18`) differs ONLY at the literal, which a
    fixed generic value pool never hits (it was declared 'equivalent' -> a false exclusion; canary
    seam relnecessity/relbank). Capped, deterministic."""
    nums = []
    for m in re.finditer(r"(?<![\w.$])(\d+(?:\.\d+)?)(?![\w.])", src or ""):
        try:
            v = float(m.group(1))
        except ValueError:
            continue
        for d in (0, 1, -1, 0.01, -0.01, 0.5, -0.5):
            x = v + d
            x = int(x) if x == int(x) else round(x, 4)
            if x not in nums:
                nums.append(x)
        if len(nums) >= 200:
            break
    strs = []
    for m in re.finditer(r"""(['"])([^'"\\\n]{0,24})\1""", src or ""):
        if m.group(2) not in strs:
            strs.append(m.group(2))
        if len(strs) >= 60:
            break
    return nums, strs


_JS_RUNNER = r"""
import { pathToFileURL } from 'node:url';
const spec = JSON.parse(process.argv[2]);
let seed = 1234567;
const rnd = () => { seed = (seed * 1103515245 + 12345) & 0x7fffffff; return seed / 0x7fffffff; };
const pick = (a) => a[Math.floor(rnd() * a.length)];
const NUMS0 = [-1000, -100, -10, -1, -0.5, 0, 0.1, 0.3, 0.5, 1, 1.5, 2, 3, 5, 7, 9.99, 10, 49.99, 50, 99, 100, 100.5, 101, 250, 1000, 1e6, NaN, Infinity, -Infinity];
const NUMS = [...new Set([...NUMS0, ...(spec.nums || [])])];
const STRS = [...new Set(['', 'a', '0', '10', 'abc', ' 5 ', 'true', ...(spec.strs || [])])];
const MIXED = [...NUMS, ...STRS, null, undefined, true, false, {}, []];
const lit = (v) => {
  if (v === undefined) return 'undefined';
  if (typeof v === 'number') return Object.is(v, -0) ? '-0' : String(v);
  if (typeof v === 'string') return JSON.stringify(v);
  if (v === null || typeof v === 'boolean') return String(v);
  if (Array.isArray(v)) return '[' + v.map(lit).join(', ') + ']';
  if (typeof v === 'object') return '{' + Object.keys(v).map(k => k + ': ' + lit(v[k])).join(', ') + '}';
  return String(v);
};
const clone = (v) => (v && typeof v === 'object') ? (Array.isArray(v) ? v.map(clone) : Object.fromEntries(Object.entries(v).map(([k, x]) => [k, clone(x)]))) : v;
const run = (f, args) => { try { return lit(f(...clone(args))); } catch (e) { return 'THROW:' + (e && e.name); } };
const BASEV = [5, 100, 3, 250, 7, 2];
function baseFor(p, idx) {
  const h = p.hint || '';
  if (p.keys.length) { const o = {}; p.keys.forEach((k, i) => { o[k] = BASEV[i % BASEV.length]; }); return o; }
  if (/string/.test(h)) return 'abc';
  if (/boolean/.test(h)) return true;
  if (/\[\]|Array/.test(h)) return [1, 2, 3];
  return BASEV[(idx + 3) % BASEV.length];
}
function candsFor(p, idx) {
  const h = p.hint || '';
  const base = baseFor(p, idx);
  if (p.keys.length) {
    const out = [base, null, undefined, 5, 'x', [], {}];
    for (const k of p.keys) {
      for (const v of MIXED) { const o = clone(base); if (v === undefined) delete o[k]; else o[k] = v; out.push(o); }
    }
    for (let i = 0; i < 260; i++) { const o = {}; for (const k of p.keys) { const v = pick(MIXED); if (v !== undefined) o[k] = v; } out.push(o); }
    return { base, cands: out };
  }
  if (/^\s*(number|bigint)\s*$/.test(h)) return { base, cands: [...NUMS] };
  if (/^\s*string\s*$/.test(h)) return { base, cands: [...STRS, ...NUMS.slice(0, 6)] };
  if (/^\s*boolean\s*$/.test(h)) return { base, cands: [true, false, null, undefined] };
  if (/\[\]|Array/.test(h)) {
    const out = [[], [1], [1, 2, 3], [0, 0], [-1, 5], ['a', 'b'], [NaN], [null], [[1], [2]]];
    for (let i = 0; i < 40; i++) out.push(Array.from({ length: Math.floor(rnd() * 5) }, () => pick(NUMS)));
    return { base, cands: out };
  }
  return { base, cands: [...MIXED] };
}
const out = { status: 'equivalent', calls: 0, ok_calls: 0, witnesses: [], notes: [] };
let A, B;
try { A = await import(pathToFileURL(spec.orig).href); B = await import(pathToFileURL(spec.mut).href); }
catch (e) { console.log(JSON.stringify({ status: 'error', error: String(e && e.message).slice(0, 300) })); process.exit(0); }
for (const fn of spec.fns) {
  const fa = A[fn.name], fb = B[fn.name];
  if (typeof fa !== 'function' || typeof fb !== 'function') { out.notes.push('missing:' + fn.name); continue; }
  const info = fn.params.map((p, i) => candsFor(p, i));
  const bases = info.map(x => x.base);
  const tuples = [];
  tuples.push(bases);
  info.forEach((x, i) => { for (const c of x.cands) { const t = bases.slice(); t[i] = c; tuples.push(t); } });
  for (let i = 0; i < info.length; i++) for (let j = i + 1; j < info.length; j++) {
    for (let n = 0; n < 700; n++) { const t = bases.slice(); t[i] = pick(info[i].cands); t[j] = pick(info[j].cands); tuples.push(t); }
  }
  for (let n = 0; n < 2500; n++) tuples.push(info.map(x => pick(x.cands)));
  if (!info.length) tuples.push([]);
  for (const t of tuples) {
    const ra = run(fa, t), rb = run(fb, t);
    out.calls++; if (!ra.startsWith('THROW:')) out.ok_calls++;
    if (ra !== rb) {
      out.status = 'differs';
      out.witnesses.push({ fn: fn.name, call: fn.name + '(' + t.map(lit).join(', ') + ')', expected: ra, got: rb, size: t.map(lit).join('').length + 1000 * ((t.map(lit).join(' ').match(/\b(true|false|null|undefined)\b|"|\[\]|\{\}/g) || []).length) });
    }
  }
}
out.witnesses.sort((a, b) => a.size - b.size);
out.witnesses = out.witnesses.slice(0, 3);
console.log(JSON.stringify(out));
"""

_PY_RUNNER = r"""
import sys, json, math, random, importlib.util
spec = json.loads(sys.argv[1])
rng = random.Random(1234567)
NUMS0 = [-1000, -100, -10, -1, -0.5, 0, 0.1, 0.3, 0.5, 1, 1.5, 2, 3, 5, 7, 9.99, 10, 49.99, 50, 99, 100, 100.5, 101, 250, 1000, 10**6, float('nan'), float('inf'), float('-inf')]
NUMS = NUMS0 + [x for x in spec.get('nums', []) if x not in NUMS0]
STRS = ['', 'a', '0', '10', 'abc', ' 5 '] + [x for x in spec.get('strs', []) if x not in ('', 'a', '0', '10', 'abc', ' 5 ')]
MIXED = NUMS + STRS + [None, True, False, {}, []]
BASEV = [5, 100, 3, 250, 7, 2]
def load(path, name):
    s = importlib.util.spec_from_file_location(name, path); m = importlib.util.module_from_spec(s); s.loader.exec_module(m); return m
def canon(v):
    if isinstance(v, bool): return v
    if isinstance(v, float) and v == v and v not in (float('inf'), float('-inf')) and v == int(v): return int(v)
    if isinstance(v, (list, tuple)): return [canon(x) for x in v]
    if isinstance(v, dict): return {k: canon(x) for k, x in v.items()}
    return v
def lit(v):
    try: return repr(v)
    except Exception: return '<unrepr>'
import copy
def run(f, args):
    try: return lit(canon(f(*copy.deepcopy(args))))
    except RecursionError: return 'THROW:RecursionError'
    except BaseException as e: return 'THROW:' + type(e).__name__
def base_for(p, i):
    h = p.get('hint') or ''
    if p['keys']: return {k: BASEV[j % len(BASEV)] for j, k in enumerate(p['keys'])}
    if 'str' in h: return 'abc'
    if 'bool' in h: return True
    if 'list' in h.lower() or 'List' in h: return [1, 2, 3]
    return BASEV[(i + 3) % len(BASEV)]
def cands_for(p, i):
    h = p.get('hint') or ''
    base = base_for(p, i)
    if p['keys']:
        out = [base, None, 5, 'x', [], {}]
        for k in p['keys']:
            for v in MIXED + ['<missing>']:
                o = dict(base)
                if v == '<missing>': o.pop(k, None)
                else: o[k] = v
                out.append(o)
        for _ in range(260):
            o = {}
            for k in p['keys']:
                v = rng.choice(MIXED + ['<missing>'])
                if v != '<missing>': o[k] = v
            out.append(o)
        return base, out
    if h.strip() in ('int', 'float', 'number'): return base, list(NUMS)
    if h.strip() == 'str': return base, STRS + NUMS[:6]
    if h.strip() == 'bool': return base, [True, False, None]
    if 'list' in h.lower():
        return base, [[], [1], [1, 2, 3], [0, 0], [-1, 5], ['a', 'b'], [float('nan')], [None]] + [[rng.choice(NUMS) for _ in range(rng.randint(0, 4))] for _ in range(40)]
    return base, list(MIXED)
out = {'status': 'equivalent', 'calls': 0, 'ok_calls': 0, 'witnesses': [], 'notes': []}
try:
    A = load(spec['orig'], 'sweep_a'); B = load(spec['mut'], 'sweep_b')
except BaseException as e:
    print(json.dumps({'status': 'error', 'error': (type(e).__name__ + ': ' + str(e))[:300]})); sys.exit(0)
for fn in spec['fns']:
    fa, fb = getattr(A, fn['name'], None), getattr(B, fn['name'], None)
    if not callable(fa) or not callable(fb): out['notes'].append('missing:' + fn['name']); continue
    info = [cands_for(p, i) for i, p in enumerate(fn['params'])]
    bases = [x[0] for x in info]
    tuples = [list(bases)]
    for i, (b, cs) in enumerate(info):
        for c in cs:
            t = list(bases); t[i] = c; tuples.append(t)
    for i in range(len(info)):
        for j in range(i + 1, len(info)):
            for _ in range(700):
                t = list(bases); t[i] = rng.choice(info[i][1]); t[j] = rng.choice(info[j][1]); tuples.append(t)
    for _ in range(2500): tuples.append([rng.choice(x[1]) for x in info])
    for t in tuples:
        ra, rb = run(fa, t), run(fb, t)
        out['calls'] += 1
        if not ra.startswith('THROW:'): out['ok_calls'] += 1
        if ra != rb:
            out['status'] = 'differs'
            out['witnesses'].append({'fn': fn['name'], 'call': fn['name'] + '(' + ', '.join(lit(x) for x in t) + ')', 'expected': ra, 'got': rb, 'size': len(lit(t)) + 1000 * sum(lit(t).count(x) for x in ('True', 'False', 'None', "'", '[]', '{}'))})
out['witnesses'].sort(key=lambda w: w['size']); out['witnesses'] = out['witnesses'][:3]
print(json.dumps(out))
"""


# ---------------------------------------------------------------------------------------------
# sweep one mutant
# ---------------------------------------------------------------------------------------------
def _node() -> str | None:
    for c in (os.environ.get("NODE"), "/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if c and Path(c).exists():
            return c
    from shutil import which
    return which("node")


def _enclosing(fns: list[dict], line: int) -> dict | None:
    for f in fns:
        if f["first_line"] <= line <= f["last_line"]:
            return f
    return None


def sweep_mutant(rel: str, orig_src: str, mut_src: str, line: int, timeout: int = PER_MUTANT_TIMEOUT_S) -> dict:
    """{status: equivalent|differs|unknown, reason, calls, witness?}. Never raises."""
    try:
        return _sweep_mutant(rel, orig_src, mut_src, line, timeout)
    except Exception as e:                                              # fail closed
        return {"status": "unknown", "reason": f"sweep error {type(e).__name__}: {e}"[:200]}


def _sweep_mutant(rel, orig_src, mut_src, line, timeout):
    if not enabled():
        return {"status": "unknown", "reason": "MUTANT_SWEEP=off"}
    if orig_src == mut_src:
        return {"status": "equivalent", "reason": "identical text", "calls": 0}
    is_py = rel.endswith(".py")
    is_js = rel.endswith((".ts", ".tsx", ".js", ".mjs", ".cjs", ".mts", ".jsx"))
    if rel.endswith((".tsx", ".jsx")):
        return {"status": "unknown", "reason": "jsx/tsx not swept"}
    if not (is_py or is_js):
        return {"status": "unknown", "reason": "unsupported language"}
    ok, why = (py_eligible(orig_src) if is_py else js_eligible(orig_src))
    if not ok:
        return {"status": "unknown", "reason": f"not sweepable: {why}"}
    ok, why = (py_eligible(mut_src) if is_py else js_eligible(mut_src))
    if not ok:
        return {"status": "unknown", "reason": f"mutant not sweepable: {why}"}
    fns = py_public_functions(orig_src) if is_py else js_exported_functions(orig_src)
    if not fns:
        return {"status": "unknown", "reason": "no exported function found"}
    enc = _enclosing(fns, line)
    if enc is None:
        return {"status": "unknown", "reason": f"line {line} is not inside an exported function (helper/top-level)"}
    node = None if is_py else _node()
    if not is_py and not node:
        return {"status": "unknown", "reason": "node not found"}
    with tempfile.TemporaryDirectory(prefix="mutsweep-") as td:
        ext = ".py" if is_py else (".mjs" if rel.endswith((".js", ".mjs", ".cjs")) else ".mts")
        pa, pb = Path(td) / ("orig" + ext), Path(td) / ("mut" + ext)
        pa.write_text(orig_src); pb.write_text(mut_src)
        # sweep the enclosing function first (the one that owns the mutated line), then the rest
        order = [enc] + [f for f in fns if f is not enc]
        nums, strs = harvest_literals(orig_src + "\n" + mut_src)
        spec = json.dumps({"orig": str(pa), "mut": str(pb), "fns": order[:6], "nums": nums, "strs": strs})
        if is_py:
            rn = Path(td) / "run.py"; rn.write_text(_PY_RUNNER)
            cmd = [sys.executable, str(rn), spec]
        else:
            rn = Path(td) / "run.mjs"; rn.write_text(_JS_RUNNER)
            cmd = [node, str(rn), spec]
        try:
            cp = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=td)
        except subprocess.TimeoutExpired:
            return {"status": "unknown", "reason": f"sweep timed out after {timeout}s (hang or heavy loop)"}
        txt = (cp.stdout or "").strip().splitlines()
        try:
            res = json.loads(txt[-1]) if txt else None
        except ValueError:
            res = None
        if not isinstance(res, dict):
            return {"status": "unknown", "reason": "runner produced no result: " + (cp.stderr or "")[-160:]}
    if res.get("status") == "error":
        return {"status": "unknown", "reason": "runner error: " + str(res.get("error"))}
    calls, okc = int(res.get("calls") or 0), int(res.get("ok_calls") or 0)
    if res.get("status") == "differs":
        w = (res.get("witnesses") or [{}])[0]
        return {"status": "differs", "calls": calls, "witness": {
            "call": w.get("call"), "expected": w.get("expected"), "got": w.get("got")}}
    if calls < 50 or okc < max(5, int(calls * MIN_OK_SHARE)):
        return {"status": "unknown", "calls": calls,
                "reason": f"sweep too weak (original returned a result on only {okc}/{calls} calls)"}
    return {"status": "equivalent", "calls": calls,
            "reason": f"identical results on {calls} generated calls ({okc} non-throwing)"}


def render_witness(w: dict | None) -> str:
    """One prompt line for a survivor's witness, or ''."""
    if not isinstance(w, dict) or not w.get("call"):
        return ""
    return (f"add a case with THIS call: `{w['call']}` -- the real implementation returns "
            f"`{w.get('expected')}`, the mutant returns `{w.get('got')}`; assert the real result")


def classify_survivors(worktree: Path, survivors: list[dict], sources: dict, progress=None) -> dict:
    """{survivor_id: sweep result}. `sources` = {survivor_id: mutant source text}. The ORIGINAL text is
    read from the worktree (refimpl applied). Bounded: at most 40 survivors, never raises."""
    out = {}
    cache = {}
    for s in (survivors or [])[:40]:
        sid, rel = s.get("id"), s.get("file")
        src = sources.get(sid)
        if not (sid and rel and isinstance(src, str)):
            continue
        if rel not in cache:
            try:
                cache[rel] = (Path(worktree) / rel).read_text()
            except OSError:
                cache[rel] = None
        if cache[rel] is None:
            continue
        r = sweep_mutant(rel, cache[rel], src, int(s.get("line") or 0))
        out[sid] = r
        if progress:
            progress(sid, r)
    return out


# ---------------------------------------------------------------------------------------------
if __name__ == "__main__":
    if sys.argv[1:2] == ["--self-test"]:
        ts = """export function f(rule: { points: number; perDollars: number }, spend: number) {
  if (typeof spend !== 'number' || isNaN(spend) || spend < 0) {
    return { times: 0, points: 0 };
  }
  const times = Math.floor(spend / rule.perDollars);
  return { times, points: times * rule.points };
}
"""
        eq = ts.replace("spend < 0", "spend <= 0")
        diff = ts.replace("Math.floor(", "Math.ceil(")
        print(json.dumps([sweep_mutant("a.ts", ts, eq, 2)["status"], sweep_mutant("a.ts", ts, diff, 5)]))
        sys.exit(0)
    print(__doc__)
