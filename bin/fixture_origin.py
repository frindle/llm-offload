#!/usr/bin/env python3
"""FIXTURE-ORIGIN diagnosis (2026-10-10e, rt-costco-receipt-attach s3: `TypeError: orders.map is not
a function` was raised inside the FIXTURE's own mock; the authoring model burned every iteration editing
refimpl.py). Language-neutral: parse the failing run's output (node:test TAP `stack: |-` blocks or a python
traceback); when the exception's top user frame is in the FIXTURE file itself (not the target / refimpl),
emit a <=12 line block saying so. Fail-open: any problem -> "".

  analyze(text, wt=None, fixture_names=(), target="") -> str
  stash_path(wt) / stash(wt, block) / read_stash(wt)   handoff to the slicer's re-spec note (temp dir)
CLI: fixture_origin.py LOGFILE [--wt DIR] [--fixture NAME]... [--target PATH]"""
import hashlib, os, re, sys, tempfile

_EXC = r"(?:TypeError|ReferenceError|RangeError|SyntaxError|URIError|EvalError|AttributeError|KeyError|" \
       r"NameError|IndexError|ValueError|ZeroDivisionError|UnboundLocalError|StopIteration|RuntimeError|" \
       r"AssertionError|ImportError|ModuleNotFoundError|NotImplementedError|LookupError|OSError|[A-Z]\w*Error)"
_FX_RE = re.compile(r"(^|/)(test_fixture\.py|verify\.test\.[cm]?[tj]sx?|[^/]*\.test\.[cm]?[tj]sx?|"
                    r"[^/]*\.spec\.[cm]?[tj]sx?|[^/]*verifyTests\.swift|VerifyImpl\.cs)$")
_JS_FRAME = re.compile(r"^\s*(?:at\s+)?(?:(?P<fn>[^\s(][^(]*?)\s+)?\(?(?P<path>(?:file://)?/[^():]+|[A-Za-z]:\\[^():]+|[\w./-]+\.[cm]?[tj]sx?):(?P<ln>\d+):(?P<col>\d+)\)?\s*$")
_PY_FRAME = re.compile(r'^\s*File "([^"]+)", line (\d+)(?:, in (\S+))?')
_INTERNAL = re.compile(r"(^node:|/node_modules/|/site-packages/|/lib/python\d|<frozen|\bnode:internal|^<)")


def _short(p):
    return os.path.basename(p or "")


def _is_fixture(path, fixture_names):
    b = _short(path)
    if b in {os.path.basename(n) for n in (fixture_names or ())}:
        return True
    return bool(_FX_RE.search(path or ""))


def _read_line(path, ln, wt=None, col=None):
    cands = [path]
    if wt and not os.path.isabs(path):
        cands.insert(0, os.path.join(wt, path))
    if wt:
        cands.append(os.path.join(wt, _short(path)))
    for c in cands:
        try:
            lines = open(c, encoding="utf-8", errors="replace").read().split("\n")
            if 1 <= ln <= len(lines):
                s = lines[ln - 1]
                if len(s) > 160:     # minified-ish one-liner: window around the column
                    c0 = max(0, (col or 1) - 60)
                    s = ("..." if c0 else "") + s[c0:c0 + 140] + "..."
                return s.strip(), c, lines
        except Exception:
            continue
    return "", None, []


def _norm(p):
    return p[7:] if p.startswith("file://") else p


def _tap_blocks(text):
    """[(exc_name, message, [frames])] from `error:/name:/stack: |-` YAML blocks. frame=(path,ln,col,fn)."""
    out = []
    lines = text.split("\n")
    for i, l in enumerate(lines):
        m = re.match(r"^(\s*)stack: \|-?\s*$", l)
        if not m:
            continue
        ind = len(m.group(1))
        frames = []
        for j in range(i + 1, len(lines)):
            s = lines[j]
            if s.strip() == "" or (len(s) - len(s.lstrip())) <= ind:
                break
            fm = _JS_FRAME.match(s)
            if fm:
                frames.append((_norm(fm.group("path")), int(fm.group("ln")), int(fm.group("col")),
                               (fm.group("fn") or "").strip()))
        name, msg = "", ""
        for k in range(max(0, i - 14), i):          # keys precede `stack:` within the same YAML block
            nm = re.match(r"^\s*name: '?([\w.]+)'?\s*$", lines[k])
            if nm:
                name = nm.group(1)
            em = re.match(r"^\s*error: (.*)$", lines[k])
            if em:
                msg = em.group(1).strip().strip("'\"")
        if frames:
            out.append((name, msg, frames))
    return out


def _py_blocks(text):
    out = []
    for m in re.finditer(r"Traceback \(most recent call last\):\n((?:[ \t]+.*\n?)+)(\S[^\n]*)", text):
        frames = []
        body = m.group(1).split("\n")
        for idx, l in enumerate(body):
            fm = _PY_FRAME.match(l)
            if fm:
                frames.append((fm.group(1), int(fm.group(2)), 0, fm.group(3) or ""))
        em = re.match(r"(" + _EXC + r")(?::\s*(.*))?$", m.group(2).strip())
        if frames and em:
            out.append((em.group(1), (em.group(2) or "").strip(), list(reversed(frames))))  # innermost first
    return out


def _stub_hint(lines, fail_ln, ident, msg):
    """Heuristic: the nearest mock/stub in the fixture whose comment/return mixes shapes (single-or-array,
    object-or-list...). Returns "" when nothing convincing."""
    mix = re.compile(r"(single|one|object|scalar|item)\b.{0,40}\b(or|vs\.?|else)\b.{0,40}\b(array|list|many|multiple|arrays)\b|"
                     r"(array|list|many|multiple)\b.{0,40}\b(or|vs\.?)\b.{0,40}\b(single|one|object|scalar|item)\b|"
                     r"\b(depending on|either)\b.{0,60}\b(array|list|object|single)\b", re.I)
    best = None
    for i, l in enumerate(lines, 1):
        if i == fail_ln:
            continue
        if mix.search(l) and re.search(r"(//|#|\*|/\*)", l):
            # name the stub: the next code line that defines a function/lambda/key
            for k in range(i, min(i + 6, len(lines))):
                dm = re.search(r"(?:function\s+(\w+)|(?:let|const|var)\s+(\w+)\s*=|^\s*(\w+)\s*:\s*(?:async\s*)?\(|def\s+(\w+)|^\s*(\w+)\s*=\s*lambda)",
                               lines[k])
                if dm:
                    nm = next(g for g in dm.groups() if g)
                    best = (i, nm, l.strip().lstrip("/#* ").strip()[:110])
                    break
            if best is None:
                best = (i, "", l.strip().lstrip("/#* ").strip()[:110])
            break
    if not best:
        return ""
    ln, nm, cm = best
    return "likely stub: %s (%s:%d) -- its comment says \"%s\"; make it return the shape %s expects" % (
        nm or "(see comment)", "fixture", ln, cm, repr(ident) if ident else "the consumer")


def analyze(text, wt=None, fixture_names=(), target="", refimpl="refimpl.py"):
    """FIXTURE-ORIGIN block for the LAST exception in `text` whose top user frame is in the fixture, or ""."""
    try:
        text = text or ""
        blocks = _tap_blocks(text) + _py_blocks(text)
        tgt = _short(target)
        for name, msg, frames in blocks:           # first matching block wins (TAP blocks first)
            if not (name or msg):
                continue
            if name and not re.fullmatch(_EXC, name) and "Error" not in name and "Exception" not in name:
                continue
            if name == "AssertionError" or (msg or "").lstrip().startswith("AssertionError"):
                continue    # an assert failing in the fixture is an expectation mismatch the code ran up to, not a crashing mock
            user = [f for f in frames if not _INTERNAL.search(f[0])]
            if not user:
                continue
            top = user[0]
            if _short(top[0]) in (tgt, _short(refimpl)) or not _is_fixture(top[0], fixture_names):
                continue
            src, real, flines = _read_line(top[0], top[1], wt, top[2])
            if not src:
                continue
            caller = next((f for f in user[1:] if not _is_fixture(f[0], fixture_names)), None)
            if caller is None:
                caller = next((f for f in user[1:]), None)
            frm = ("called from %s:%d" % (_short(caller[0]), caller[1])
                   + ((" (%s)" % caller[3]) if caller[3] else "")) if caller else "called from the target"
            im = re.match(r"([A-Za-z_$][\w$]*)(?:\.[\w$]+)+ is not a function|'(\w+)' object has no attribute|"
                          r"Cannot read propert\w+ of (?:undefined|null)", msg or "")
            ident = (im.group(1) or im.group(2)) if im and (im.group(1) or im.group(2)) else ""
            expr = ("`%s` (from `%s`)" % (ident, msg[:80])) if ident else ("`%s`" % (msg[:80] or name))
            hint = _stub_hint(flines, top[1], ident, msg) if flines else ""
            fx_name = _short(top[0])
            out = ["FIXTURE-ORIGIN: the failure was raised inside the fixture's own code at %s:%d `%s` %s; "
                   "this is a fixture defect (a mock/stub returning the wrong shape: %s); fix the fixture's "
                   "stub/mock, do NOT change refimpl.py" % (fx_name, top[1], src[:150], frm, expr),
                   "  exception: %s: %s" % (name or "Error", (msg or "")[:140])]
            if hint:
                out.append("  " + hint.replace("(fixture:", "(" + fx_name + ":").replace("(%s:" % "fixture", "(" + fx_name + ":"))
            out.append("  The target/refimpl code is NOT where it threw (top frame is the fixture): read that "
                       "fixture line and the stub that feeds its argument; the refimpl is not at fault.")
            return "\n".join(out[:12])
        return ""
    except Exception:
        return ""


def stash_path(wt):
    h = hashlib.sha1(os.path.realpath(str(wt)).encode()).hexdigest()[:12]
    return os.path.join(tempfile.gettempdir(), "auto-harness-fixtureorigin-%s.txt" % h)


def stash(wt, block):
    """Persist (or clear) the block outside the tree so the slicer's re-spec can fold it in."""
    try:
        p = stash_path(wt)
        if block:
            open(p, "w").write(block)
        elif os.path.exists(p):
            os.unlink(p)
    except Exception:
        pass


def read_stash(wt):
    try:
        return open(stash_path(wt)).read().strip()
    except Exception:
        return ""


def main(argv):
    if len(argv) < 2:
        print(__doc__)
        return 2
    wt, fx, tg, a = None, [], "", argv[2:]
    while a:
        x = a.pop(0)
        if x == "--wt" and a:
            wt = a.pop(0)
        elif x == "--fixture" and a:
            fx.append(a.pop(0))
        elif x == "--target" and a:
            tg = a.pop(0)
    print(analyze(open(argv[1], errors="replace").read(), wt, fx, tg) or "(no fixture-origin failure)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
