#!/usr/bin/env python3
"""REACH PROBE -- why does the fixture never reach the behaviour it asserts?  (2026-10-10)

Failure class this exists for (aw-codec-floor s3, 2 fixtures in a row): the harness's own refimpl
fails EVERY fixture case ("expected 1 supersede, got 0").  The author kept re-writing the fixture
because nothing told it WHY the handler under test never reached the supersede / record_activity
calls: a title stub returned set(), so `torrent_matches_any_title(...)` was always False and the
handler returned early.  This probe RUNS the fixture under a tracer (python only) with the refimpl
applied and reports, for the function under test on its first call:
  * where it returned / raised (line + source text);
  * which helper calls returned falsy (None / False / 0 / '' / set() / [] / {}) -- stubs
    defined in the fixture are named as such -- and which of them sit on the path to the exit;
  * which fixture stubs patched onto the target (recorders, I/O boundaries) were NEVER invoked.

Fail-open everywhere: any problem -> "" (no block), never an exception to the caller.

Library:  probe_inplace(wt, target=None, fixture=None) -> str   (refimpl already applied in wt)
          probe_copy(wt, target=None, fixture=None)    -> str   (copies wt, applies refimpl.py in the
                                                                 copy; the real tree is untouched)
          render(result_dict) -> str  (<= MAX_LINES lines)
CLI:      reach_probe.py WT [--target T] [--fixture F] [--copy]
Child:    reach_probe.py --child WT TARGET FIXTURE OUTJSON [PREFER,..]   (internal)
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

MAX_LINES = 25
MARK = "=== REACH PROBE"
MAX_ENTRIES = 12          # depth-0 fixture->target calls recorded before tracing stops
MAX_LINE_EVENTS = 6000
EXC_TYPES = ("AttributeError", "TypeError", "KeyError", "ValueError", "IndexError")
RECORDERISH = re.compile(r"record|supersede|activity|log|notify|send|post|delete|remove|add|write|"
                         r"update|label|download|save|insert|enqueue|emit|alert", re.I)


# ---------------------------------------------------------------- child (runs INSIDE the tracer)
def _child(wt, target, fixture, outp, prefer):
    import linecache
    import runpy
    import atexit
    wt = os.path.realpath(wt)
    os.chdir(wt)
    sys.path.insert(0, wt)
    tpath = os.path.realpath(os.path.join(wt, target))
    fpath = os.path.realpath(os.path.join(wt, fixture))
    prefer = set(p for p in (prefer or "").split(",") if p)
    entries = []
    st = {"active": None, "done": False, "dumped": False}

    def _src(path, ln):
        try:
            return linecache.getline(path, ln).strip()[:110]
        except Exception:
            return ""

    def _where(path):
        p = os.path.realpath(path) if path else ""
        if p == tpath:
            return "target"
        if p == fpath:
            return "fixture"
        return None

    def _code(v):
        # getattr on a lazy proxy (flask `request`, werkzeug LocalProxy) can raise RuntimeError
        try:
            if isinstance(v, (type, int, str, bytes, float, tuple, list, dict, set)):
                return None
            return getattr(v, "__code__", None)
        except BaseException:
            return None

    def _falsy(v):
        try:
            return not v
        except Exception:
            return False

    def _repr(v):
        try:
            r = repr(v)
        except Exception:
            r = "<unrepr>"
        return r if len(r) <= 48 else r[:45] + "..."

    def _name_for(code, gl, back):
        for g in (gl, getattr(back, "f_globals", None) or {}):
            try:
                for k, v in g.items():
                    if _code(v) is code:
                        return k
            except Exception:
                pass
        cn = code.co_name
        try:
            line = _src(back.f_code.co_filename, back.f_lineno)
            if cn == "<lambda>":
                m = re.search(r"([A-Za-z_][A-Za-z0-9_\.]*)\s*\(", line)
                return (m.group(1) if m else "<lambda>") + "<lambda>"
            m = re.search(r"([A-Za-z_][A-Za-z0-9_\.]*\b" + re.escape(cn) + r")\s*\(", line)
            if m:
                return m.group(1)
        except Exception:
            pass
        return cn

    def _dump():
        if st["dumped"]:
            return
        st["dumped"] = True
        try:
            sys.settrace(None)
        except Exception:
            pass
        out = {"tpath": tpath, "fpath": fpath, "target": target, "fixture": fixture,
               "entries": [{k: v for k, v in e.items() if not k.startswith("_")} for e in entries],
               "prefer": sorted(prefer)}
        try:
            with open(outp, "w") as fh:
                json.dump(out, fh, default=str)
        except Exception:
            pass

    def _note_exc(e, arg):
        try:
            et, ev, tb = arg
            name = getattr(et, "__name__", "")
            if name not in EXC_TYPES:
                return
            deep, t = None, tb
            while t is not None:
                w = _where(t.tb_frame.f_code.co_filename)
                if w:
                    deep = (w, t.tb_lineno, t.tb_frame.f_code.co_name)
                t = t.tb_next
            if deep is None:
                return
            e["exc_info"] = {"type": name, "msg": str(ev)[:160], "where": deep[0], "line": deep[1],
                             "func": deep[2], "src": _src(tpath if deep[0] == "target" else fpath, deep[1])}
        except BaseException:
            pass

    def local_entry(frame, event, arg):
        try:
            return _local_entry(frame, event, arg)
        except BaseException:
            return local_entry

    def _local_entry(frame, event, arg):
        e = st["active"]
        if e is None:
            return None
        if event == "line":
            if len(e["lines"]) < MAX_LINE_EVENTS:
                e["lines"].append(frame.f_lineno)
            e["_last"] = frame.f_lineno
            e["_pend"] = False
        elif event == "exception":
            e["exc"] = "%s: %s" % (getattr(arg[0], "__name__", arg[0]), str(arg[1])[:80])
            e["exc_line"] = frame.f_lineno
            e["_pend"] = True
            _note_exc(e, arg)
        elif event == "return":
            _seen = []
            for _ln in e["lines"]:
                if not _seen or _seen[-1] != _ln:
                    _seen.append(_ln)
            e["tail_src"] = dict((str(_l), _src(tpath, _l)) for _l in _seen[-6:])
            e["ret"] = _repr(arg)
            e["ret_line"] = e.get("_last") or frame.f_lineno
            for _l in reversed(_seen[-3:]):
                if _src(tpath, _l).startswith(("return", "raise")):
                    e["ret_line"] = _l
                    break
            e["ret_src"] = _src(tpath, e["ret_line"])
            e["ret_kind"] = "exception" if (e.get("exc") and arg is None and e.get("_pend")) else "return"
            gl = frame.f_globals
            stubs = []
            for k, v in list(gl.items()):
                c = _code(v)
                if c is not None and _where(c.co_filename) == "fixture":
                    stubs.append({"name": k, "line": c.co_firstlineno, "called": c in e["_called"]})
            e["stubs"] = stubs
            st["active"] = None
            if len(entries) >= MAX_ENTRIES:
                _dump()
        return local_entry

    callee_recs = {}

    def local_fix(frame, event, arg):
        # an exception raised in fixture code right after an entry call = that case's assertion failed
        try:
            if event == "exception" and entries and st["active"] is None and "failed" not in entries[-1]:
                entries[-1]["failed"] = "%s: %s" % (getattr(arg[0], "__name__", arg[0]), str(arg[1])[:90])
        except BaseException:
            pass
        return local_fix

    def local_callee_fn(frame, event, arg):
        try:
            return _local_callee_fn(frame, event, arg)
        except BaseException:
            return local_callee_fn

    def _local_callee_fn(frame, event, arg):
        if event == "return":
            rec = callee_recs.pop(id(frame), None)
            if rec is not None:
                rec["val"] = _repr(arg)
                rec["type"] = type(arg).__name__
                rec["falsy"] = _falsy(arg)
                rec["none"] = arg is None
        return local_callee_fn

    def gtrace(frame, event, arg):
        try:
            return _gtrace(frame, event, arg)
        except BaseException:
            return None

    def _gtrace(frame, event, arg):
        if event != "call" or st["done"]:
            return None
        code = frame.f_code
        w = _where(code.co_filename)
        e = st["active"]
        if e is None:
            if w == "target" and code.co_name != "<module>" and len(entries) < MAX_ENTRIES:
                back = frame.f_back
                if back is not None and _where(back.f_code.co_filename) == "target":
                    return None
                e = {"name": code.co_name, "line": code.co_firstlineno, "lines": [], "calls": [],
                     "_called": set(), "_frame": frame, "_gl": frame.f_globals}
                entries.append(e)
                st["active"] = e
                return local_entry
            if w == "fixture":
                return local_fix
            if len(entries) >= MAX_ENTRIES:
                st["done"] = True
                sys.settrace(None)
            return None
        # an entry call is in flight: note every call into the fixture / target
        if w is None:
            return None
        e["_called"].add(code)
        depth, f = 1, frame.f_back
        while f is not None and f is not e["_frame"] and depth < 8:
            f = f.f_back
            depth += 1
        if code.co_name in ("<genexpr>", "<listcomp>", "<setcomp>", "<dictcomp>"):
            return None
        if depth > 3 or len(e["calls"]) >= 80:
            return None
        back = frame.f_back
        rec = {"name": _name_for(code, e["_gl"], back), "where": w, "line": back.f_lineno if back else 0,
               "def_line": code.co_firstlineno, "val": None, "falsy": False, "none": False}
        e["calls"].append(rec)
        callee_recs[id(frame)] = rec
        return local_callee_fn

    atexit.register(_dump)
    sys.argv = [fpath]
    sys.settrace(gtrace)
    try:
        runpy.run_path(fpath, run_name="__main__")
    except SystemExit:
        pass
    except BaseException:
        pass
    finally:
        _dump()


# ---------------------------------------------------------------- parent side
def _unreached(e):
    return [x for x in (e.get("stubs") or []) if not x.get("called") and RECORDERISH.search(x["name"])]


def _pick(res):
    """The entry to explain: the function under test (named in TASK.md, else the biggest of the first
    few calls), and within it the first call that left a recorder stub unreached (the symptom the
    fixture asserts on), else its first call."""
    ents = res.get("entries") or []
    pref = set(res.get("prefer") or [])
    pool = [e for e in ents if e.get("name") in pref]
    if not pool:
        best = None
        for e in ents[:8]:
            n = len(set(e.get("lines") or []))
            if best is None or n > best[0]:
                best = (n, e)
        if best is None:
            return None, []
        pool = [e for e in ents if e.get("name") == best[1].get("name")]
    failed = [e for e in pool if e.get("failed")] or pool
    best = max(failed, key=lambda x: len(_unreached(x)))      # first of the ties = earliest call
    return best, pool


def _need(exc, msg):
    """(types the failing object could be, what the failing op needs) from an exception message."""
    m = msg or ""
    a = re.search(r"has no attribute '(\w+)'", m)
    if a:
        at = a.group(1)
        kinds = {"mapping (dict)": ("items", "keys", "values", "get", "setdefault", "update", "pop", "popitem"),
                 "list": ("append", "extend", "sort", "insert", "remove", "reverse"),
                 "set": ("add", "discard", "union", "intersection"),
                 "str": ("lower", "upper", "split", "strip", "startswith", "endswith", "encode", "format",
                         "replace", "join")}
        for k, names in kinds.items():
            if at in names:
                return "." + at + "() => " + k
        return ".%s => an object that has .%s" % (at, at)
    if exc == "KeyError":
        return "[%s] lookup => mapping (dict) holding that key" % (m[:40] or "key")
    if exc == "IndexError":
        return "[i] index => a non-empty sequence"
    if "not iterable" in m:
        return "`in`/iteration => a container (dict/list/set)"
    if "not subscriptable" in m:
        return "[...] subscript => mapping (dict) or sequence"
    if re.search(r"(list|tuple|string) indices must be integers", m):
        return "[key] with a non-int key => mapping (dict)"
    if "unpack" in m:
        return "tuple-unpack => a sequence of the expected length"
    if "unhashable" in m:
        return "used as a dict key / set member => a hashable value"
    return "an op that failed (%s)" % m[:60]


def _types_in(exc, msg):
    m = msg or ""
    ts = set(re.findall(r"'(\w+)' object", m)) | set(re.findall(r"type '(\w+)'", m)) | \
        set(re.findall(r"unhashable type: '(\w+)'", m))
    for w in ("list", "tuple", "string"):
        if re.search(w + r" indices must be integers", m):
            ts.add("str" if w == "string" else w)
    if exc == "KeyError":
        ts.add("dict")
    if exc == "IndexError":
        ts |= {"list", "tuple", "str"}
    return ts


def _lhs_names(src):
    m = re.match(r"\s*(?:for\s+)?([\w\.\s,\(\)\[\]]+?)\s*(?::[^=]*)?(?:=(?!=)|\s+in\s)", src or "")
    return set(re.findall(r"[A-Za-z_]\w*", m.group(1))) if m else set()


def _short(name):
    return (name or "").split(".")[-1].replace("<lambda>", "")


def _real_shape(tpath, name):
    """How the REAL target function `name` builds its return value (ast): 'returns dict (...)' or ""."""
    try:
        import ast
        tree = ast.parse(open(tpath, errors="replace").read())
        fns = [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name]
        if not fns:
            return ""
        fn = fns[0]
        assigns = {}
        for n in ast.walk(fn):
            if isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name):
                        assigns.setdefault(t.id, []).append(n.value)
            elif isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name) and n.value is not None:
                assigns.setdefault(n.target.id, []).append(n.value)
        callmap = {"dict": "dict", "defaultdict": "dict", "OrderedDict": "dict", "list": "list", "sorted": "list",
                   "set": "set", "frozenset": "set", "tuple": "tuple", "str": "str"}

        def cls(v, depth=0):
            if isinstance(v, (ast.Dict, ast.DictComp)):
                return {"dict"}
            if isinstance(v, (ast.List, ast.ListComp)):
                return {"list"}
            if isinstance(v, (ast.Set, ast.SetComp)):
                return {"set"}
            if isinstance(v, ast.Tuple):
                return {"tuple"}
            if isinstance(v, (ast.Constant, ast.JoinedStr)):
                return {"None" if getattr(v, "value", 0) is None and isinstance(v, ast.Constant)
                        else ("str" if isinstance(v, ast.JoinedStr) else type(v.value).__name__)}
            if isinstance(v, ast.Call):
                f = v.func
                n = f.id if isinstance(f, ast.Name) else (f.attr if isinstance(f, ast.Attribute) else "")
                if n == "get" and isinstance(f, ast.Attribute) and len(v.args) >= 2:
                    return cls(v.args[1], depth)        # X.get(key, {}) -> the default's shape
                return {callmap[n]} if n in callmap else set()
            if isinstance(v, ast.IfExp):
                return cls(v.body, depth) | cls(v.orelse, depth)
            if isinstance(v, ast.BoolOp):
                r = set()
                for x in v.values:
                    r |= cls(x, depth)
                return r
            if isinstance(v, ast.Name) and depth < 2:
                r = set()
                for x in assigns.get(v.id, []):
                    r |= cls(x, depth + 1)
                return r
            return set()
        kinds, shown = [], []
        for n in ast.walk(fn):
            if isinstance(n, ast.Return) and n.value is not None:
                ks = cls(n.value)
                kinds += [k for k in sorted(ks) if k not in kinds]
                txt = "return " + ast.unparse(n.value)[:36]
                if isinstance(n.value, ast.Name) and assigns.get(n.value.id):
                    txt += "; " + ast.unparse(assigns[n.value.id][0])[:0] + n.value.id + " = " + \
                        ast.unparse(assigns[n.value.id][0])[:30]
                if txt not in shown and len(shown) < 2:
                    shown.append(txt)
        if not shown:
            return ""
        if not kinds:
            return "real %s (target:%s) returns (%s) -- type not statically known" % (name, fn.lineno, " | ".join(shown))
        return "real %s (target:%s) returns %s (%s)" % (name, fn.lineno, "/".join(kinds), " | ".join(shown))
    except Exception:
        return ""


def _exc_hint(res, e):
    """HINT lines when the traced case raised one of EXC_TYPES from a stub-supplied value, else []."""
    x = e.get("exc_info")
    if not x:
        return []
    propagated = e.get("ret_kind") == "exception" and (e.get("exc") or "").startswith(x["type"])
    if not (propagated or x["type"] in (e.get("failed") or "")):
        if x["type"] not in ("AttributeError", "TypeError"):
            return []
    types = _types_in(x["type"], x["msg"])
    need = _need(x["type"], x["msg"])
    src = x.get("src") or ""
    best = None
    for i, c in enumerate(e.get("calls") or []):
        if c.get("where") != "fixture" or c.get("type") not in types:
            continue
        lhs = _lhs_names(_src_line(res["tpath"], c.get("line")))
        same = c.get("line") == x["line"] and x["where"] == "target"
        feeds = same or any(re.search(r"\b%s\b" % re.escape(n), src) for n in lhs)
        rank = (0 if feeds else 1, -i)
        if best is None or rank < best[0]:
            best = (rank, c, feeds)
    out = ["HINT: case raised %s: %s at %s:%s `%s`%s" % (
        x["type"], x["msg"][:90], x["where"], x["line"], src[:80],
        "" if propagated else "  (caught inside the target)")]
    if best is None:
        if not propagated:
            return []
        out.append("no fixture stub returned a %s on this path: the bad value comes from a fixture arg/global/"
                   "state, or from the target itself" % "/".join(sorted(types)) if types else
                   "no fixture stub return value matched the failing op")
        return out
    c = best[1]
    out.append("stub %s (fixture:%s) returned %s (%s) but target:%s uses it as %s%s" % (
        c["name"], c["def_line"], c.get("type"), c.get("val"), x["line"], need,
        "" if best[2] else "  [probable: variable not matched]"))
    rs = _real_shape(res["tpath"], _short(c["name"]))
    if rs:
        out.append(rs + "; make the stub return that shape")
    else:
        out.append("(no real target function named %s to compare; build the stub's value like the %s the "
                   "target code expects)" % (_short(c["name"]), need.split("=>")[-1].strip()))
    return out


def _src_line(path, ln):
    try:
        import linecache
        return linecache.getline(path, int(ln)).strip()
    except Exception:
        return ""


def render(res):
    """PURE. The REACH PROBE block (<= MAX_LINES lines) for a child result dict, or ""."""
    if not res:
        return ""
    t, fx = res.get("target") or "?", res.get("fixture") or "?"
    e, pool = _pick(res)
    out = []
    if e is None:
        return "\n".join([
            MARK + " (python; refimpl applied) ===",
            "fixture %s never called any function defined in %s directly: no case can reach the" % (fx, t),
            "behaviour under test. Check the fixture imports/loads the right file and calls the",
            "entry function (not only helpers)."])
    out.append("%s: call %d of %d to %s() @%s:%s, refimpl applied) ===" % (
        MARK, pool.index(e) + 1, len(pool), e["name"], t, e["line"]))
    if e.get("failed"):
        out.append("this call's case then failed: %s" % e["failed"])
    try:
        exc_hint = _exc_hint(res, e)
    except Exception:
        exc_hint = []
    if exc_hint:
        out.extend(exc_hint)
    if exc_hint:
        pass
    elif e.get("exc") and e.get("ret_kind") == "exception":
        out.append("raised %s at %s:%s `%s`" % (e["exc"], t, e.get("exc_line"), e.get("ret_src", "")))
    else:
        out.append("returned %s at %s:%s `%s`" % (e.get("ret"), t, e.get("ret_line"), e.get("ret_src", "")))
    lines = []
    for ln in e.get("lines") or []:
        if not lines or lines[-1] != ln:
            lines.append(ln)
    tail_lines = lines[-8:]
    if lines:
        out.append("last lines executed (%d line events):" % len(lines))
        ts = e.get("tail_src") or {}
        for ln in tail_lines[-(3 if exc_hint else 5):]:
            out.append("  L%s %s" % (ln, ts.get(str(ln), "")))
    feed = set(tail_lines)
    calls = [c for c in (e.get("calls") or []) if c.get("falsy")]
    seen, cand = set(), []
    for c in calls:
        k = (c["name"], c["line"])
        if k in seen:
            continue
        seen.add(k)
        feeds = (not c.get("none")) and any(abs(c["line"] - x) <= 1 for x in feed)
        rank = (0 if feeds else 1, 1 if c.get("none") else 0, 0 if c["where"] == "fixture" else 1)
        cand.append((rank, c, feeds))
    cand.sort(key=lambda z: z[0])
    if cand:
        out.append("falsy helper results on the path (call-site line -> callee [where] = value):")
        for _, c, feeds in cand[:(2 if exc_hint else 6)]:
            out.append("  L%s %s [%s%s] -> %s%s" % (
                c["line"], c["name"], "STUB test fixture:%s" % c["def_line"] if c["where"] == "fixture"
                else "target:%s" % c["def_line"], "", c.get("val"), "   <-- feeds the exit" if feeds else ""))
    else:
        out.append("no helper call returned a falsy value on this path.")
    if len(pool) > 1:
        out.append("exit line of each traced call: " + "  ".join(
            "#%d:L%s%s" % (i + 1, x.get("ret_line"), "*" if x is e else "") for i, x in enumerate(pool[:12])))
    stubs = [s for s in (e.get("stubs") or []) if not s.get("called")]
    stubs.sort(key=lambda s: (0 if RECORDERISH.search(s["name"]) else 1, s["name"]))
    if stubs:
        out.append("fixture stubs patched onto the target but NEVER invoked in this call: " +
                   ", ".join("%s (fixture:%s)" % (s["name"], s["line"]) for s in stubs[:8]))
    if exc_hint:
        return "\n".join(out[:MAX_LINES])
    feeders = [c for _, c, f in cand if f and c["where"] == "fixture"]
    feeders_t = [c for _, c, f in cand if f and c["where"] != "fixture"]
    if feeders or feeders_t:
        c = (feeders or feeders_t)[0]
        out.append("HINT: the function exited because %s(...) returned %s on the path to its exit; make the "
                   "fixture's stubs/state take the branch the case asserts (realistic non-empty data), "
                   "then re-run." % (c["name"], c.get("val")))
    elif stubs and RECORDERISH.search(stubs[0]["name"]):
        ts = e.get("tail_src") or {}
        g = [ln for ln in tail_lines if (ts.get(str(ln)) or "").startswith(("if ", "elif ", "while "))]
        out.append("HINT: the recorder stubs above were never reached -- %s ended the call; make the fixture's "
                   "inputs/stubs (or per-case state it shares, e.g. ids/caches reused across cases) fail that "
                   "condition." % ("the guard at L%s `%s`" % (g[-1], ts[str(g[-1])][:70]) if g
                                   else "an earlier guard/branch in %s()" % e["name"]))
    return "\n".join(out[:MAX_LINES])


def _fixture_for(wt, fixture=None):
    if fixture and os.path.isfile(os.path.join(wt, fixture)):
        return fixture
    for n in ("test_fixture.py",):
        if os.path.isfile(os.path.join(wt, n)):
            return n
    return None


def _target_for(wt, target=None):
    if target and os.path.isfile(os.path.join(wt, target)) and target.endswith(".py"):
        return target
    try:
        t = (json.load(open(os.path.join(wt, ".dispatch-harness.json"))) or {}).get("target") or ""
        if t.endswith(".py") and os.path.isfile(os.path.join(wt, t)):
            return t
    except Exception:
        pass
    return None


def _prefer(wt):
    try:
        txt = open(os.path.join(wt, "TASK.md"), errors="replace").read()
        return ",".join(sorted(set(re.findall(r"`([A-Za-z_][A-Za-z0-9_]*)\(?[^`]{0,40}`", txt)))[:200])
    except Exception:
        return ""


def _py_for(wt):
    py = os.path.join(str(wt), ".venv", "bin", "python")
    return py if os.access(py, os.X_OK) else sys.executable


def probe_inplace(wt, target=None, fixture=None, timeout=60, py=None):
    """Run the fixture in `wt` (refimpl ALREADY applied) under the tracer. "" on any problem."""
    try:
        wt = os.path.realpath(str(wt))
        fx, tg = _fixture_for(wt, fixture), _target_for(wt, target)
        if not fx or not tg:
            return ""
        py = py or _py_for(wt)
        fd, outp = tempfile.mkstemp(prefix="reach-probe-", suffix=".json")
        os.close(fd)
        try:
            env = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            subprocess.run([py, os.path.realpath(__file__), "--child", wt, tg, fx, outp, _prefer(wt)],
                           cwd=wt, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                           timeout=timeout, env=env)
            try:
                res = json.load(open(outp))
            except Exception:
                return ""
        finally:
            try:
                os.unlink(outp)
            except OSError:
                pass
        return render(res)
    except Exception:
        return ""


def probe_copy(wt, target=None, fixture=None, timeout=90, require_all_fail=True):
    """Copy `wt` (no .git/.venv/node_modules), apply refimpl.py in the COPY, probe it there.
    The real tree is never touched. "" when there is no refimpl/fixture or the fixture passes."""
    tmp = None
    try:
        wt = os.path.realpath(str(wt))
        if not os.path.isfile(os.path.join(wt, "refimpl.py")) or not _fixture_for(wt, fixture):
            return ""
        py = _py_for(wt)        # the REAL tree's venv (deps); the copy is source-only
        tmp = tempfile.mkdtemp(prefix="reach-probe-wt-")
        dst = os.path.join(tmp, "wt")
        shutil.copytree(wt, dst, symlinks=True, ignore=shutil.ignore_patterns(
            ".git", ".venv", "node_modules", "__pycache__", "*.pyc"))
        r = subprocess.run([py, "refimpl.py"], cwd=dst, capture_output=True, timeout=timeout)
        if r.returncode != 0:
            return ""
        fx = _fixture_for(dst, fixture)
        r = subprocess.run([py, fx], cwd=dst, capture_output=True, timeout=timeout)
        if r.returncode == 0:
            return ""       # the fixture passes with the refimpl: nothing to explain
        if require_all_fail:
            m = re.search(r"^\s*(\d+)/(\d+) case\(s\) passed", (r.stdout or b"").decode("utf-8", "replace"), re.M)
            if not m or int(m.group(1)) != 0 or int(m.group(2)) < 2:
                return ""   # not the K-of-K condition (some cases pass): other failures are not this class
        return probe_inplace(dst, target, fixture, timeout, py=py)
    except Exception:
        return ""
    finally:
        if tmp:
            shutil.rmtree(tmp, ignore_errors=True)


def main(argv):
    if len(argv) >= 6 and argv[1] == "--child":
        _child(argv[2], argv[3], argv[4], argv[5], argv[6] if len(argv) > 6 else "")
        return 0
    if len(argv) < 2:
        print(__doc__)
        return 2
    wt, target, fixture, cp = argv[1], None, None, False
    a = argv[2:]
    while a:
        x = a.pop(0)
        if x == "--target" and a:
            target = a.pop(0)
        elif x == "--fixture" and a:
            fixture = a.pop(0)
        elif x == "--copy":
            cp = True
    out = (probe_copy if cp else probe_inplace)(wt, target, fixture)
    print(out or "(no reach probe: nothing to explain / not a python fixture)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
