#!/usr/bin/env python3
"""escalation_grounding.py -- every causal claim in an escalation review must be GROUNDED.

Why (2026-10-11): the headless escalation reviewer writes a verdict and the self-heal
ladder (b = re-spec, c = harness repair, d = bigger model) ACTS on it with nobody reading.
Two reviews were wrong and acted on anyway:
  * job 2f4d18945f75 said the refimpl's bare names resolve to the original function
    (verdict b, spec under-specified). False: refimpl.py read a nonexistent `target.session`
    (NameError swallowed by a bare except) and test_fixture.py had bad expectations.
  * 20261011T083723Z (cardlink s2) blamed a byte-literal vs regex mismatch that had already
    been removed from the spec.
Same idea as the reviewer-grounding gate (a finding must quote code that exists; the
hasLink/hashLink hallucination precedent): a claim is only evidence if a script can find the
quoted text in the cited file near the cited line.

THE RULE (policy v1)
  * A claim cites `path:LINE` plus a quoted snippet (backticks or double quotes) on the same
    line, or is an object in ESC_RESULT {"claims":[{"claim","file","line","quote"}]}.
  * verify_claim() checks the file exists (inside $HOME), and the snippet occurs (whitespace
    normalised) within +-TOL lines of LINE. Anything else is UNGROUNDED and DROPPED.
  * The reviewer must also run the cheapest reproducing command and record it:
        REPRO CMD: <command>
        REPRO OUTPUT:
        ```<verbatim output>```
    (or ESC_RESULT "repro": {"cmd","output"}). If that output shows an exception, the
    review's own claims must mention its type -- a claim "every case returns (0, [])" is
    checked against the actual first traceback/exception, not accepted on prose.
  * A reviewer-derived b/c/d verdict with NO grounded claim (or with a missing / contradicted
    repro) is UNVERIFIED. Self-heal must not act on UNVERIFIED b/c/d automatically.
  * Not subject: mechanical / reason / default verdicts (the queue's own failure_class, not a
    reviewer claim) and (a), which verified_close/verified_skip already prove independently.
  * GRANDFATHERING: only reviews the watcher stamped with POLICY_FOOTER are enforced; older
    review files (the old prompt could not comply) keep behaving exactly as before.

Stdlib only; pure except for reading cited files.
"""
import json
import os
import re
import sys
from pathlib import Path

POLICY_FOOTER = "<!-- grounding-policy: v1 -->"
TOL = 2                      # lines of slack between the cited line and the quote
MIN_QUOTE = 10               # chars (>=10 non-space to verify); "def" / "if" prove nothing
MAX_FILE = 6 * 1024 * 1024
ESC_DIR = Path.home() / ".ollama-dispatch" / "escalations"

_CITE = re.compile(r"(?P<path>(?:~|/)?[\w@.+/-]*[\w-]\.[A-Za-z0-9]{1,6}):(?P<line>\d{1,7})(?:-(?P<end>\d{1,7}))?\b")
_QUOTE = re.compile(r"`([^`\n]{%d,})`|\"([^\"\n]{%d,})\"" % (MIN_QUOTE, MIN_QUOTE))
_EXC = re.compile(r"^\s*(?:[\w.]+\.)?(\w*(?:Error|Exception|Exit|Interrupt))\b", re.M)
_REPRO_CMD = re.compile(r"^\W*REPRO[ _]CMD\W*:?\s*(.+)$", re.I | re.M)
_REPRO_OUT = re.compile(r"^\W*REPRO[ _]OUTPUT\W*:?[ \t]*\n?(.*?)(?=^\W*(?:ESC_RESULT|VERDICT|CLAIM|REPRO[ _]CMD)\b|\Z)",
                        re.I | re.M | re.S)


def enforced(review, env=None):
    env = os.environ if env is None else env
    if str(env.get("ESC_GROUNDING", "on")).lower() == "off":
        return False
    return POLICY_FOOTER in (review or "")


def stamp(review):
    """Mark a review as produced under policy v1 (the watcher does this, never the model)."""
    r = review or ""
    return r if POLICY_FOOTER in r else r.rstrip() + "\n\n" + POLICY_FOOTER + "\n"


# ---------------------------------------------------------------------- parsing
def _esc_result(review):
    best = None
    for m in re.finditer(r"ESC_RESULT:\s*(\{.*\})", review or ""):
        try:
            d = json.loads(m.group(1))
        except ValueError:
            continue
        if isinstance(d, dict):
            best = d
    return best or {}


def extract_claims(review):
    """PURE. [{"claim","file","line","quote","src"}] from ESC_RESULT claims + inline cites.
    The REPRO block is evidence of a run, not a causal claim, so its lines are skipped."""
    out = []
    for c in _esc_result(review).get("claims") or []:
        if isinstance(c, dict):
            try:
                ln = int(c.get("line"))
            except (TypeError, ValueError):
                ln = 0
            out.append({"claim": str(c.get("claim") or "")[:300], "file": str(c.get("file") or ""),
                        "line": ln, "quote": str(c.get("quote") or ""), "src": "json"})
    body = _strip_repro(review or "")
    for raw in body.splitlines():
        if raw.lstrip().startswith("ESC_RESULT"):
            continue
        for cm in _CITE.finditer(raw):
            _from = cm.end() + (1 if raw[cm.end():cm.end() + 1] in ("`", '"', "*") else 0)
            qm = _QUOTE.search(raw, _from)
            q = ""
            if qm:
                q = qm.group(1) or qm.group(2) or ""
            out.append({"claim": raw.strip()[:300], "file": cm.group("path"),
                        "line": int(cm.group("line")), "quote": q, "src": "inline"})
    return out


def _strip_repro(review):
    m = _REPRO_OUT.search(review or "")
    if not m:
        return review or ""
    return (review or "")[:m.start()] + (review or "")[m.end():]


def extract_repro(review):
    """PURE. {"cmd","output"} (either may be '') from REPRO CMD/OUTPUT or ESC_RESULT repro."""
    j = _esc_result(review).get("repro")
    if isinstance(j, dict) and (j.get("cmd") or j.get("output")):
        return {"cmd": str(j.get("cmd") or "").strip(), "output": str(j.get("output") or "").strip()}
    cm = _REPRO_CMD.search(review or "")
    om = _REPRO_OUT.search(review or "")
    out = (om.group(1) if om else "").strip()
    out = re.sub(r"^```\w*\n?|\n?```\s*$", "", out).strip()
    return {"cmd": (cm.group(1).strip().strip("`") if cm else ""), "output": out}


def first_exception(text):
    """PURE. The type name of the FIRST exception/traceback tail in `text`, else None."""
    m = _EXC.search(text or "")
    return m.group(1) if m else None


# ------------------------------------------------------------------ verification
def context_roots(ctx_text):
    """PURE. Directories named in an escalation context (worktree, cwd) -- relative citations
    resolve against these. Always includes ~/bin."""
    roots = []
    for m in re.finditer(r"(?:worktree|cwd)[^`\n]{0,60}`(/[^`\n]+)`", ctx_text or "", re.I):
        p = m.group(1).rstrip("/")
        if p not in roots:
            roots.append(p)
    for m in re.finditer(r"(/Users/[\w.-]+/[^\s`'\"),;<>]+)", ctx_text or ""):
        p = m.group(1).rstrip("/.:")
        d = p if os.path.isdir(p) else os.path.dirname(p)
        if d and d not in roots and os.path.isdir(d) and len(roots) < 40:
            roots.append(d)
    roots.append(str(Path.home() / "bin"))
    return roots


def _norm(s):
    return re.sub(r"\s+", " ", s or "").strip()


def _resolve(path, roots):
    home = os.path.realpath(str(Path.home()))
    import tempfile
    tmp = os.path.realpath(tempfile.gettempdir())
    p = os.path.expanduser(path)
    cands = [p] if os.path.isabs(p) else [os.path.join(r, p) for r in roots]
    for c in cands:
        rp = os.path.realpath(c)
        if (rp == home or rp.startswith(home + os.sep) or rp.startswith("/private/tmp/")
                or rp.startswith(tmp + os.sep)) \
                and os.path.isfile(rp):
            return rp
    return None


def verify_claim(claim, roots):
    """-> (grounded: bool, code: str). code in ok|no-quote|short-quote|no-file|unreadable|
    quote-absent|wrong-line."""
    q = _norm(claim.get("quote"))
    if not q:
        return False, "no-quote"
    if len(q.replace(" ", "")) < MIN_QUOTE:
        return False, "short-quote"
    rp = _resolve(str(claim.get("file") or ""), roots)
    if not rp:
        return False, "no-file"
    try:
        if os.path.getsize(rp) > MAX_FILE:
            return False, "unreadable"
        lines = Path(rp).read_text(errors="replace").splitlines()
    except OSError:
        return False, "unreadable"
    ln = int(claim.get("line") or 0)
    nq = max(1, claim.get("quote", "").count("\n") + 1)
    lo, hi = max(0, ln - 1 - TOL), min(len(lines), ln - 1 + TOL + nq)
    if ln >= 1 and q in _norm(" ".join(lines[lo:hi])):
        return True, "ok"
    return False, ("wrong-line" if q in _norm(" ".join(lines)) else "quote-absent")


def assess(review, ctx_text="", require_repro=True, roots=None, verdict=None):
    """PURE-ish (reads cited files). The grounding verdict for one review.

    -> {"status": GROUNDED|UNVERIFIED|EXEMPT, "verdict", "source", "claims": [...],
        "grounded": n, "ungrounded": n, "repro": {...}, "repro_ok": bool, "reasons": [str]}"""
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    import escalation_verdict as ev
    v, src = ev.parse_review(review)
    v = verdict or v
    base = {"verdict": v, "source": src, "claims": [], "grounded": 0, "ungrounded": 0,
            "repro": {}, "repro_ok": None, "reasons": []}
    if v not in ("b", "c", "d") or not src:
        base["status"] = "EXEMPT"
        base["reasons"].append("no reviewer-derived b/c/d verdict (%s/%s)" % (v, src))
        return base
    roots = roots if roots is not None else context_roots(ctx_text)
    claims = extract_claims(review)
    for c in claims:
        ok, code = verify_claim(c, roots)
        c["grounded"], c["code"] = ok, code
    base["claims"] = claims
    base["grounded"] = sum(1 for c in claims if c["grounded"])
    base["ungrounded"] = len(claims) - base["grounded"]
    if not claims:
        base["reasons"].append("no claim cites file:line plus a quoted snippet")
    elif not base["grounded"]:
        base["reasons"].append("all %d cited claim(s) failed verification (%s)"
                               % (len(claims), ",".join(sorted({c["code"] for c in claims}))))
    rep = extract_repro(review)
    base["repro"] = rep
    if require_repro:
        if not (rep["cmd"] and rep["output"]):
            base["repro_ok"] = False
            base["reasons"].append("no REPRO CMD/OUTPUT: the cheapest reproducing command was not run/recorded")
        else:
            exc = first_exception(rep["output"])
            prose = _norm(_strip_repro(review))
            if exc and exc.lower() not in prose.lower():
                base["repro_ok"] = False
                base["reasons"].append("repro's first exception %s is not addressed by any claim" % exc)
            else:
                base["repro_ok"] = True
    ok = base["grounded"] > 0 and base["repro_ok"] is not False
    base["status"] = "GROUNDED" if ok else "UNVERIFIED"
    return base


def sanitize(review, report):
    """The review with every UNGROUNDED claim line dropped (replaced by a marker), so the
    findings handed to a re-author / re-spec round carry only checked statements."""
    bad = {c["claim"] for c in report.get("claims") or [] if not c.get("grounded") and c.get("src") == "inline"}
    out = []
    for line in (review or "").splitlines():
        if line.strip()[:300] in bad:
            out.append("[dropped: ungrounded claim -- cited text not found at the cited path/line]")
        else:
            out.append(line)
    return "\n".join(out) + ("\n" if (review or "").endswith("\n") else "")


def label(report):
    """One visible token for index rows / notifications."""
    if report.get("status") == "UNVERIFIED":
        return "UNVERIFIED (%d/%d claims grounded%s)" % (
            report.get("grounded", 0), len(report.get("claims") or []),
            "; " + report["reasons"][0] if report.get("reasons") else "")
    return ""


def report_md(report):
    """Markdown appended to the review file so a human sees what was checked."""
    L = ["", "---", "## GROUNDING CHECK (machine) -- %s" % report.get("status"), ""]
    for c in report.get("claims") or []:
        L.append("- [%s] %s:%s %r -- %s" % ("GROUNDED" if c["grounded"] else "DROPPED", c["file"],
                                           c["line"], c["quote"][:80], c["code"]))
    if not report.get("claims"):
        L.append("- (no file:line + quote claims found)")
    if report.get("repro_ok") is not None:
        L.append("- repro: %s" % ("ok" if report["repro_ok"] else "FAILED"))
    for r in report.get("reasons") or []:
        L.append("- reason: " + r)
    if report.get("status") == "UNVERIFIED":
        L.append("")
        L.append("**UNVERIFIED -- self-heal will not act on this verdict (%s) automatically.**"
                 % report.get("verdict"))
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------------- backtest
def backtest(esc_dir=None, require_repro=False):
    """Run assess() over every *.review.md. Returns list of (name, report)."""
    d = Path(esc_dir or ESC_DIR)
    out = []
    for rp in sorted(d.glob("*.review.md")):
        try:
            rv = rp.read_text(errors="replace")
        except OSError:
            continue
        ctxp = rp.with_name(rp.name[:-len(".review.md")] + ".md")
        try:
            ctx = ctxp.read_text(errors="replace")
        except OSError:
            ctx = ""
        out.append((rp.name, assess(rv, ctx, require_repro=require_repro)))
    return out


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--backtest", action="store_true")
    ap.add_argument("--strict", action="store_true", help="backtest: also require REPRO")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--check", metavar="REVIEW.md")
    ap.add_argument("--ctx")
    a = ap.parse_args(argv)
    if a.check:
        rv = Path(a.check).read_text(errors="replace")
        cx = Path(a.ctx).read_text(errors="replace") if a.ctx else ""
        r = assess(rv, cx, require_repro=True)
        print(json.dumps({k: v for k, v in r.items() if k != "claims"}, indent=1))
        print(report_md(r))
        return 0 if r["status"] != "UNVERIFIED" else 1
    res = backtest(require_repro=a.strict)
    n = len(res)
    cnt = {}
    for _n, r in res:
        cnt[r["status"]] = cnt.get(r["status"], 0) + 1
    print("reviews=%d %s (require_repro=%s)" % (n, cnt, a.strict))
    if a.list:
        for name, r in res:
            print("%-11s v=%s g=%d/%d %s" % (r["status"], r["verdict"], r["grounded"],
                                              len(r["claims"]), name))
    return 0


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
