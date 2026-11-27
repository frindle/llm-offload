#!/usr/bin/env python3
"""anchored_edit -- hash-tagged, line-anchored edits for the worker's shell.

The worker model only has a shell. This gives it a two-command edit tool that never asks it to
retype the text it is replacing:

  anchored_edit.py view FILE [A-B]   print the file's tag and its lines as `  N| text`
  anchored_edit.py edit FILE TAG     read ops from stdin, apply them, print the new tag + region

Ops (stdin of `edit`; line numbers are the numbers `view` printed for that tag):

  replace A[-B]        replace lines A..B with the `+` rows that follow
  insert-after N       insert the `+` rows after line N (0 = top of file)
  delete A[-B]         remove lines A..B
  +text                one new line (exactly one leading `+` is the marker)

Three checks run, in order, before a byte is written:
  1. version     the tag must be the tag of the file as it is on disk now
  2. provenance  every line an op anchors on was printed by a `view` of that exact content
  3. determinism ops must not overlap; the result is the same wherever the ops sit in the input

Refusals print `EDIT-REFUSED[kind]: ...` (kind: stale, unseen, invalid, guard) and exit 2; a
success prints `EDIT-OK ...` and exit 0. mutation_recovery.py reads those markers.

The tag is 4 uppercase hex digits of a SHA-256 over the file with line endings normalised to LF and
trailing blanks stripped from every line. It is an index, never an identity: a tag match alone never
authorises a write, the stored snapshot's full-text digest must match too.

State (which lines each viewed version showed) lives in a directory outside the repository:
$ANCHOR_EDIT_STATE, else a per-working-directory folder under the system temp dir.
"""
import hashlib
import json
import os
import re
import sys
import tempfile

MAX_VIEW_LINES = 200       # one `view` prints at most this many lines (narrow with A-B)
MAX_LINE_CHARS = 2000      # a longer line is clipped in the view and does NOT count as seen
MAX_SNAPSHOTS = 4          # versions kept per file
OK, REFUSED = "EDIT-OK", "EDIT-REFUSED"

_TRAIL = re.compile(r"[ \t\r]+$")


# ---- text model ---------------------------------------------------------------------------------
def decode(raw):
    """(bom, eol, lines, final_newline) from file bytes; ValueError for something that is not text."""
    bom = b""
    if raw.startswith(b"\xef\xbb\xbf"):
        bom, raw = raw[:3], raw[3:]
    if b"\x00" in raw:
        raise ValueError("binary file")
    text = raw.decode("utf-8")      # UnicodeDecodeError is a ValueError
    crlf, lf = text.count("\r\n"), text.count("\n") - text.count("\r\n")
    eol = "\r\n" if crlf > lf else "\n"
    text = text.replace("\r\n", "\n")
    final_newline = text.endswith("\n")
    lines = text.split("\n")
    if final_newline:
        lines.pop()
    return bom, eol, lines, final_newline


def encode(bom, eol, lines, final_newline):
    body = eol.join(lines) + (eol if final_newline and lines else "")
    return bom + body.encode("utf-8")


def _normalised(lines):
    return "\n".join(_TRAIL.sub("", ln) for ln in lines) + "\n"


def digest(lines):
    return hashlib.sha256(_normalised(lines).encode("utf-8")).hexdigest()


def tag_of(lines):
    """4 uppercase hex digits: the low 16 bits of the normalised text's SHA-256."""
    return digest(lines)[-4:].upper()


# ---- session state ------------------------------------------------------------------------------
def state_dir():
    d = os.environ.get("ANCHOR_EDIT_STATE")
    if not d:
        d = os.path.join(tempfile.gettempdir(), "anchored-edit-" + hashlib.sha256(os.getcwd().encode()).hexdigest()[:12])
    os.makedirs(d, exist_ok=True)
    return d


def _state_file(path):
    return os.path.join(state_dir(), hashlib.sha256(os.path.realpath(path).encode()).hexdigest()[:20] + ".json")


def _load(path):
    try:
        with open(_state_file(path)) as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return []


def record(path, lines, seen):
    """Remember that `seen` (1-based line numbers) of this exact text were shown. Same tag AND same full-text
    digest -> the same snapshot, seen sets merged (paginated views add up). Returns the tag."""
    full, snaps = digest(lines), _load(path)
    merged = set(seen)
    for s in snaps:
        if s["digest"] == full:
            merged |= set(s["seen"])
    snaps = [s for s in snaps if s["digest"] != full]
    snaps.insert(0, {"tag": full[-4:].upper(), "digest": full, "seen": sorted(merged)})
    with open(_state_file(path), "w") as fh:
        json.dump(snaps[:MAX_SNAPSHOTS], fh)
    return full[-4:].upper()


def seen_for(path, lines):
    """The seen-line set of the stored snapshot whose full text equals `lines`, or None when never viewed."""
    full = digest(lines)
    for s in _load(path):
        if s["digest"] == full:
            return set(s["seen"])
    return None


# ---- view ---------------------------------------------------------------------------------------
def render(lines, a, b):
    """(text, shown line numbers) for lines a..b (1-based, inclusive); over-long lines are clipped and not 'shown'."""
    out, shown = [], []
    for n in range(a, b + 1):
        ln = lines[n - 1].rstrip("\r")
        if len(ln) > MAX_LINE_CHARS:
            out.append(f"{n:>6}| {ln[:MAX_LINE_CHARS]} [line clipped, not anchorable]")
        else:
            out.append(f"{n:>6}| {ln}")
            shown.append(n)
    return "\n".join(out), shown


def cmd_view(path, rng=None):
    try:
        with open(path, "rb") as fh:
            _, _, lines, _ = decode(fh.read())
    except OSError as e:
        return 1, f"{REFUSED}[invalid]: cannot read {path}: {e.strerror}"
    except ValueError as e:
        return 1, f"{REFUSED}[invalid]: {path} is not an editable text file ({e})"
    n = len(lines)
    a, b = 1, min(n, MAX_VIEW_LINES)
    if rng:
        m = re.fullmatch(r"(\d+)(?:-(\d+))?", rng)
        if not m:
            return 1, f"{REFUSED}[invalid]: range must look like 10-60"
        a = int(m.group(1))
        b = int(m.group(2) or a)
        if a < 1 or b < a:
            return 1, f"{REFUSED}[invalid]: bad range {rng}"
        b = min(b, n, a + MAX_VIEW_LINES - 1)
    if n and a > n:
        return 1, f"{REFUSED}[invalid]: {path} has only {n} lines"
    text, shown = render(lines, a, b) if n else ("", [])
    tag = record(path, lines, shown)
    more = f"\n(lines {b + 1}-{n} not shown: run view again with a range)" if b < n else ""
    return 0, f"{path} tag={tag} lines={n} showing={a}-{b}\n{text}{more}"


# ---- ops ----------------------------------------------------------------------------------------
class OpError(Exception):
    def __init__(self, kind, msg):
        super().__init__(msg)
        self.kind, self.msg = kind, msg


def parse_ops(text):
    """[(verb, a, b, body)]: replace/delete take A[-B]; insert-after takes N (a = b = N)."""
    ops, cur = [], None
    for raw in text.splitlines():
        if raw.startswith("+"):
            if cur is None:
                raise OpError("invalid", "a `+` row before any op line (use replace, insert-after or delete first)")
            cur[3].append(raw[1:])
            continue
        s = raw.strip()
        if not s:
            continue
        m = re.fullmatch(r"(replace|delete|insert-after)\s+(\d+)(?:-(\d+))?", s)
        if not m:
            raise OpError("invalid", f"cannot read op line {s[:60]!r}; use `replace A-B`, `insert-after N` or `delete A-B`, then `+` rows")
        verb, a = m.group(1), int(m.group(2))
        b = int(m.group(3)) if m.group(3) else a
        if verb == "insert-after" and m.group(3):
            raise OpError("invalid", "insert-after takes one line number")
        if b < a:
            raise OpError("invalid", f"range {a}-{b} runs backwards")
        cur = [verb, a, b, []]
        ops.append(cur)
    if not ops:
        raise OpError("invalid", "no ops on stdin")
    for verb, a, b, body in ops:
        if verb == "replace" and not body:
            raise OpError("invalid", f"replace {a}-{b} has no `+` rows (use delete to remove lines)")
        if verb == "delete" and body:
            raise OpError("invalid", f"delete {a}-{b} must not have `+` rows")
        if verb == "insert-after" and not body:
            raise OpError("invalid", f"insert-after {a} has no `+` rows")
    return [tuple(o) for o in ops]


def check_anchors(ops, n, seen):
    """Bounds, overlap, provenance. seen is the set of displayed line numbers."""
    spans = []
    for verb, a, b, _ in ops:
        if verb == "insert-after":
            if not 0 <= a <= n:
                raise OpError("invalid", f"insert-after {a}: the file has {n} lines")
            need = [a] if a else []
        else:
            if a < 1 or b > n:
                raise OpError("invalid", f"{verb} {a}-{b}: the file has {n} lines")
            need = range(a, b + 1)
            spans.append((a, b, verb))
        miss = [k for k in need if k not in seen]
        if miss:
            raise OpError("unseen", f"line(s) {_fmt(miss)} were never shown to you for this version of the file. "
                                    f"Run `view` on that range again, then edit")
    for i, (a1, b1, v1) in enumerate(spans):
        for a2, b2, v2 in spans[i + 1:]:
            if a1 <= b2 and a2 <= b1:
                raise OpError("invalid", f"{v1} {a1}-{b1} overlaps {v2} {a2}-{b2}")
    for verb, a, _, _ in ops:
        if verb == "insert-after":
            for s, e, _v in spans:
                if s <= a < e:
                    raise OpError("invalid", f"insert-after {a} falls inside the replaced/deleted range {s}-{e}")


def _fmt(nums):
    nums = list(nums)[:6]
    return ", ".join(map(str, nums)) + (" ..." if len(nums) == 6 else "")


def apply_ops(lines, ops):
    """New line list plus (first, last) changed line numbers in the NEW numbering (None, None if nothing)."""
    ins = {}   # position (count of original lines kept before it) -> rows
    cut = {}   # original line -> replacement rows to emit at its first line (None = deleted)
    for verb, a, b, body in ops:
        if verb == "insert-after":
            ins.setdefault(a, []).extend(body)
        else:
            cut[a] = (b, body if verb == "replace" else [])
    out, i, touched = [], 1, []
    out.extend(ins.get(0, []))
    if 0 in ins:
        touched += [1, len(out)]
    n = len(lines)
    while i <= n:
        if i in cut:
            b, body = cut[i]
            start = len(out) + 1
            out.extend(body)
            touched += [start, max(len(out), start)] if body else [max(len(out), 1)]
            i = b + 1
            tail = ins.get(b, [])
            if tail:
                start = len(out) + 1
                out.extend(tail)
                touched += [start, len(out)]
            continue
        out.append(lines[i - 1])
        for r in ins.get(i, []):
            out.append(r)
            touched.append(len(out))
        i += 1
    return out, (min(touched), max(touched)) if touched else (None, None)


# ---- edit ---------------------------------------------------------------------------------------
def _guard(path, before, after):
    try:
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        import edit_guard
    except ImportError:
        return None
    r = edit_guard.guard_edit(path, "\n".join(before) + "\n", "\n".join(after) + "\n")
    return None if r.ok else "; ".join(r.reasons)


def cmd_edit(path, tag, ops_text):
    try:
        with open(path, "rb") as fh:
            bom, eol, lines, final_nl = decode(fh.read())
    except OSError as e:
        return 2, f"{REFUSED}[invalid]: cannot read {path}: {e.strerror}"
    except ValueError as e:
        return 2, f"{REFUSED}[invalid]: {path} is not an editable text file ({e})"
    live = tag_of(lines)
    if tag.upper() != live:
        return 2, (f"{REFUSED}[stale]: tag {tag} does not match {path} as it is now (tag {live}). The file changed since you "
                   f"viewed it. Run `view {path}` again and redo the edit from the new line numbers.")
    seen = seen_for(path, lines)
    if seen is None:
        return 2, (f"{REFUSED}[unseen]: no `view` of this version of {path} was recorded. Run `view {path}` first and "
                   f"use the tag it prints.")
    try:
        ops = parse_ops(ops_text)
        check_anchors(ops, len(lines), seen)
    except OpError as e:
        return 2, f"{REFUSED}[{e.kind}]: {e.msg}"
    new, (first, last) = apply_ops(lines, ops)
    if new == lines:
        return 2, f"{REFUSED}[invalid]: the ops change nothing"
    why = _guard(path, lines, new)
    if why:
        return 2, f"{REFUSED}[guard]: {why}. Nothing was written; fix the replacement rows and edit again with the same tag."
    tmp = f"{path}.anchored-tmp"
    try:
        with open(tmp, "wb") as fh:
            fh.write(encode(bom, eol, new, final_nl or not lines))
        try:
            os.chmod(tmp, os.stat(path).st_mode & 0o7777)
        except OSError:
            pass
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)
    lo, hi = max(1, (first or 1) - 2), min(len(new), (last or 1) + 2)
    cut = hi > lo + MAX_VIEW_LINES - 1
    hi = min(hi, lo + MAX_VIEW_LINES - 1)
    text, shown = render(new, lo, hi) if new else ("", [])
    # Seen after the edit: what this output prints, plus earlier-seen lines whose number still points at the same text
    # (all lines above the change; the lines below it too when the line count did not change). A line below an insert or
    # delete has a new number the model never saw, so anchoring on its old number is refused instead of hitting a neighbour.
    same = len(new) == len(lines)
    keep = {k for k in seen if k < (first or 1) or (same and k > (last or 0))}
    newtag = record(path, new, keep | set(shown))
    more = f"\n(changed lines continue past {hi}: view that range before editing there)" if cut else ""
    return 0, f"{OK} {path} tag={newtag} lines={len(new)} (was {live})\n{text}{more}"


def main(argv):
    if len(argv) >= 2 and argv[0] == "view" and len(argv) in (2, 3):
        rc, out = cmd_view(argv[1], argv[2] if len(argv) == 3 else None)
    elif len(argv) == 3 and argv[0] == "edit":
        rc, out = cmd_edit(argv[1], argv[2], sys.stdin.read())
    else:
        print(__doc__)
        return 2
    print(out)
    return rc


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
