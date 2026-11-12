#!/usr/bin/env python3
"""Plan / harness lint helpers: ROOT-CAUSE checks for unsatisfiable slice plans.

Written 2026-10-06 after two evidence cases (see the callers):

  (A) rt-egift-link-s1 s4-api-route burned ~10 author jobs because the PLAN said
      must_contain "Cache-Control: no-store": the author refimpl used the whole
      string as a header NAME (Headers.append threw -> every handler 500). The
      refimpl read req.nextUrl while the fixture passed a plain Request; the
      fixture sent PUT as FormData while the intent said JSON.
  (B) replay-endorse: the plan never defined a data field a slice's spec depends on,
      and a sibling slice (s4b) duplicated s2 (vacuous must_contain at the chain tip).

Everything here is PURE (strings/dicts in, findings out); the callers decide what is
blocking. The old dispatch tools that called it are retired (use mini-swe).

Run `python3 plan_lint.py PLAN.json` to lint a plan file.
"""
import json
import re
import sys

# --------------------------------------------------------------------------
# 1. header / pair literals
# --------------------------------------------------------------------------
_HEADER_LIT_RE = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)*)\s*:\s+\S")
_PLAIN_HEADERS = {"authorization", "accept", "location", "cookie", "origin", "host",
                  "referer", "etag", "vary", "expires", "pragma", "connection", "link"}


def header_pair_literals(slices):
    """[(sid, literal)] for a must_contain literal that is an HTTP-header-style
    `Name: value` pair (hyphenated name, or a well-known header). Such a literal is
    not source text: the author turns the whole string into a header NAME / key
    (`headers.append('Cache-Control: no-store')` throws 'invalid header name') or the
    literal is only satisfiable inside a string/comment. Name the TWO tokens
    separately (`Cache-Control`, `no-store`) instead. A plain `key: value` object
    literal (`status: 'ok'`) is NOT flagged: it is valid code."""
    out = []
    for s in slices or []:
        if not isinstance(s, dict):
            continue
        for lit in (s.get("must_contain") or []):
            if not isinstance(lit, str):
                continue
            m = _HEADER_LIT_RE.match(lit)
            if not m:
                continue
            name = m.group(1)
            if "-" in name or name.lower() in _PLAIN_HEADERS:
                out.append((s.get("id"), lit))
    return out


# --------------------------------------------------------------------------
# 2. intent vs fixture/verify body shape
# --------------------------------------------------------------------------
_JSON_RE = re.compile(r"\bJSON\b|application/json|\.json\(\)|JSON\.stringify", re.I)
_FORM_RE = re.compile(r"FormData|multipart/form-data|x-www-form-urlencoded|urlencoded|"
                      r"\.formData\(\)|form[- ]data", re.I)
# NOT/never/instead of in front of a mention means the text rejects it
_NEG_NEXTURL_RE = re.compile(
    r"(?:\bNOT\b|\bnever\b|\bnot use\b|\binstead of\b|\bavoid\b|\bwithout\b|\bno\b)"
    r"[^.;\n]{0,40}?\b(?:req(?:uest)?\.)?nextUrl\b", re.I)
_NEXTURL_RE = re.compile(r"\bnextUrl\b|\bNextRequest\b")
_PLAIN_REQ_RE = re.compile(r"\bplain\s+(?:web\s+|fetch\s+)?Request\b|\bnew Request\(", re.I)


def _shapes(text):
    t = text or ""
    s = set()
    if _JSON_RE.search(t):
        s.add("json")
    if _FORM_RE.search(t):
        s.add("form")
    return s


def intent_shape_conflicts(s):
    """[(sid, message)] for ONE plan slice whose intent and verify_shape disagree
    about the request body shape (JSON vs form) or about reading `req.nextUrl` while
    the verify drives handlers with a plain web Request (nextUrl is undefined there).
    Only an EXPLICIT contradiction is reported: a shape the intent names (JSON) that
    the verify_shape never mentions is fine; a shape the verify_shape uses that the
    intent does not allow is not."""
    out = []
    sid = s.get("id")
    intent = s.get("intent") if isinstance(s.get("intent"), str) else ""
    vs = s.get("verify_shape") if isinstance(s.get("verify_shape"), str) else ""
    ish, vsh = _shapes(intent), _shapes(vs)
    if ish and vsh and (vsh - ish):
        extra = sorted(vsh - ish)[0]
        out.append((sid, f"verify_shape sends the body as {extra.upper()} but the intent only "
                         f"allows {'/'.join(sorted(x.upper() for x in ish))} -- the fixture "
                         f"and the implementation will disagree. State the accepted shape(s) "
                         f"once, identically, in both."))
    uses_next = _NEXTURL_RE.search(intent) and not _NEG_NEXTURL_RE.search(intent)
    if uses_next and _PLAIN_REQ_RE.search(vs + " " + intent):
        out.append((sid, "the intent reads req.nextUrl / NextRequest but the verify drives the "
                         "handler with a plain web Request, where nextUrl is undefined. Read the "
                         "query with `new URL(req.url).searchParams` (works for both)."))
    return out


def harness_shape_conflicts(task_text, fixture_text, refimpl_text):
    """[str] hints for an AUTHORED harness whose own refimpl fails its own fixture for a
    SHAPE reason a human can see by reading the three files (rt-egift-link-s1-s4):
      * fixture builds its request body as FormData while TASK/intent says JSON (and
        never mentions form data), or the reverse;
      * refimpl reads `.nextUrl` while the fixture builds a plain `new Request(` and
        never a NextRequest."""
    out = []
    task, fx, ri = task_text or "", fixture_text or "", refimpl_text or ""
    tsh = _shapes(task)
    fx_form = bool(re.search(r"new\s+FormData\s*\(|body\s*:\s*(?:form|fd)\b", fx))
    fx_json = bool(re.search(r"JSON\.stringify|application/json", fx))
    if fx_form and not fx_json and tsh == {"json"}:
        out.append("the fixture sends its request body as FormData but TASK.md/INTENT says the "
                   "body is JSON -- send JSON.stringify(...) with content-type application/json "
                   "(or, if the spec really allows form data, say so in TASK.md).")
    if fx_json and not fx_form and tsh == {"form"}:
        out.append("the fixture sends its request body as JSON but TASK.md/INTENT says the body "
                   "is form data -- make the fixture send a FormData body.")
    if re.search(r"\.nextUrl\b", ri) and re.search(r"new\s+Request\s*\(", fx) \
            and not re.search(r"NextRequest", fx):
        out.append("refimpl.py emits code reading `req.nextUrl`, but the fixture calls the "
                   "handlers with a plain `new Request(...)` where nextUrl is UNDEFINED -- "
                   "every case that reads it throws. Read the query with "
                   "`new URL(req.url).searchParams` in the code refimpl.py writes.")
    # a whole 'Name: value' pair passed as ONE header name/key
    for m in re.finditer(r"""(?:headers?\.(?:append|set)|setHeader)\(\s*['"`]([A-Za-z][\w-]*:\s*[^'"`]+)['"`]\s*\)""", ri):
        out.append("refimpl.py passes the whole pair %r as a header NAME (it throws 'invalid header "
                   "name' -> every handler 500s). A header is TWO values: "
                   "headers.set('Cache-Control', 'no-store') / { 'Cache-Control': 'no-store' }."
                   % m.group(1))
    return out


# --------------------------------------------------------------------------
# 3. fields a slice depends on must be defined earlier
# --------------------------------------------------------------------------
_CAMEL_RE = re.compile(r"(?<![\w$.])([a-z][a-z0-9]*(?:[A-Z][a-z0-9]+)+)\b")


def _slice_text(s):
    mc = s.get("must_contain") if isinstance(s.get("must_contain"), list) else []
    return " \n ".join([str(s.get(k) or "") for k in ("title", "intent", "verify_shape")]
                       + [str(x) for x in mc if isinstance(x, str)])


def undefined_field_defects(top, slices, ground_text="", in_repo=None):
    """[(sid, token)] for a camelCase data token a slice's intent/verify_shape uses that
    is DEFINED nowhere it could come from: not in the plan's top-level intent (the data
    contract), not in the baseline target / repo files the intent names (`ground_text`),
    not in the text of any EARLIER slice, and not in this slice's own must_contain (a
    slice that introduces a name pins it there). The spec then depends on a datum nobody
    ever specified (replay-endorse: `reservationTracking`) and the author invents its
    own rule for it. `in_repo(token) -> bool` (optional) says the token already exists
    somewhere in the repo (an imported helper/field of an existing module), which also
    counts as defined."""
    hay_base = (top or "") + "\n" + (ground_text or "")
    out, seen_text = [], []
    for s in slices or []:
        if not isinstance(s, dict):
            continue
        own_mc = " ".join(x for x in (s.get("must_contain") or []) if isinstance(x, str))
        own = " \n ".join(str(s.get(k) or "") for k in ("intent", "verify_shape"))
        hay = hay_base + "\n" + "\n".join(seen_text)
        for tok in dict.fromkeys(_CAMEL_RE.findall(own)):
            if len(tok) < 6:
                continue
            if re.search(r"(?<![\w$])%s(?![\w$])" % re.escape(tok), hay + "\n" + own_mc):
                continue
            if in_repo is not None:
                try:
                    if in_repo(tok):
                        continue
                except Exception:
                    continue
            out.append((s.get("id"), tok))
        seen_text.append(_slice_text(s))
    return out


# --------------------------------------------------------------------------
# 4. sibling-slice overlap
# --------------------------------------------------------------------------
def sibling_overlap_defects(slices, plan_target=None):
    """[(sid, {literal: earlier_sid})] for a slice whose EVERY must_contain literal is a
    substring of a must_contain literal of an EARLIER slice (array order = execution
    order on the serial chain) editing the SAME file. That earlier slice's own gate
    forces the literal into the file first, so this slice is green on an empty diff and
    can never be authored (replay-endorse s4b duplicated s2). Ancestors are left to the
    plan gate's VACUOUS_AT_CHAIN_BASELINE; this reports the NON-ancestor (sibling)
    case only."""
    good = [s for s in (slices or []) if isinstance(s, dict)]
    out = []

    def tgt(s):
        return s.get("target") or plan_target

    for i, s in enumerate(good):
        if s.get("kind") == "invariant":
            continue
        mc = s.get("must_contain")
        if not isinstance(mc, list) or not mc or any(not isinstance(x, str) or not x.strip() for x in mc):
            continue
        anc = _anc(good, s.get("id"))
        cover = {}
        for lit in mc:
            for e in good[:i]:
                if e.get("id") in anc or tgt(e) != tgt(s):
                    continue
                if any(isinstance(m, str) and lit in m for m in (e.get("must_contain") or [])):
                    cover[lit] = e.get("id")
                    break
        if len(cover) == len(mc):
            out.append((s.get("id"), cover))
    return out


def _anc(good, sid):
    deps = {x.get("id"): [d for d in (x.get("depends_on") or []) if isinstance(d, str)] for x in good}
    seen, stack = set(), list(deps.get(sid, []))
    while stack:
        x = stack.pop()
        if x in seen:
            continue
        seen.add(x)
        stack.extend(deps.get(x, []))
    return seen


# --------------------------------------------------------------------------
def lint_plan(plan, ground_text="", in_repo=None):
    """[(code, severity, message)] for a whole plan dict. severity: 'error' | 'warn'."""
    out = []
    if not isinstance(plan, dict):
        return out
    slices = [s for s in (plan.get("slices") or []) if isinstance(s, dict)]
    for sid, lit in header_pair_literals(slices):
        out.append(("HEADER_PAIR_LITERAL", "error",
                    f"slice {sid!r} must_contain {lit!r} is a `Name: value` header pair. The author "
                    f"turns the whole string into one header NAME (`Headers.append` throws "
                    f"'invalid header name' and every handler 500s). List the two tokens "
                    f"separately, e.g. 'Cache-Control' and 'no-store', and describe the pair in "
                    f"the intent as a real header pair."))
    for s in slices:
        for sid, msg in intent_shape_conflicts(s):
            out.append(("INTENT_SHAPE_CONFLICT", "error", f"slice {sid!r}: {msg}"))
    for sid, cover in sibling_overlap_defects(slices, plan.get("target")):
        out.append(("SIBLING_OVERLAP", "warn",
                    f"slice {sid!r}: every must_contain literal is already forced into the same "
                    f"file by an EARLIER slice ("
                    + ", ".join(f"{l!r} via {v!r}" for l, v in list(cover.items())[:6])
                    + ") -- green on an empty diff, so it can never be authored. Delete it (fold "
                      "any new behaviour into that earlier slice) or name something THIS slice "
                      "introduces."))
    top = plan.get("intent")
    if isinstance(top, str) and top.strip():
        for sid, tok in undefined_field_defects(top, slices, ground_text, in_repo):
            out.append(("UNDEFINED_FIELD", "warn",
                        f"slice {sid!r} depends on `{tok}`, which neither the plan's top-level "
                        f"intent, the target, nor any earlier slice defines. Say where it comes "
                        f"from (type/field/param and what it holds) in the intent of the slice "
                        f"that adds it, or the author will invent its own rule for it."))
    return out


if __name__ == "__main__":
    for a in sys.argv[1:]:
        p = json.load(open(a))
        for code, sev, msg in lint_plan(p):
            print(f"{sev.upper():5} {code}: {msg}")
