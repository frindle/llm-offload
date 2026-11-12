#!/usr/bin/env python3
"""land_ground.py -- reviewer-finding GROUNDING + demote-only judging for the land pipeline.

WHY (2026-10-09; research: ~/.ollama-dispatch/merge-queue/2026-10-09-autoland-research.md, (b)/(e))
A local reviewer model invents findings ("hasLink" typo that is really `hashLink`, a deleted
mock that was never in the diff). 22% of LLM review comments are context-misaligned (HalluJudge).
So no finding reaches a packet unless a SCRIPT, with no model, can prove it points at real text:

  schema   {file, line (or line_start/line_end), quote, rule, severity, what, repro?}
  (i)      `file` is a file in the diff, or a readable file under `root`
  (ii)     `quote` is a VERBATIM substring (whitespace-normalised) of the file's text within
           the cited line range (+/- SLOP lines) or of a diff line for that file in that range
  (iii)    `rule` is in the CLOSED list RULES
  (iv)     a BLOCKING finding must ship an executable `repro`; otherwise it is DEMOTED to
           advisory (never silently kept as blocking)
A finding failing (i)-(iii) is DROPPED and COUNTED as a hallucination (per-model metric).

Legacy gate issues ({severity,file,line,what,source,category}) carry no quote. For source
"review" the quotes are the backticked spans of `what`; with none, the finding is
ungrounded. Mechanical sources (scope, completeness, literal, ...) are deterministic and pass
through untouched.

The LLM judge (optional) may only DEMOTE a finding or flag concern, NEVER approve a change:
N=3 order-swapped calls, unanimous to demote, verdicts cached by
hash(diff, finding, model, prompt-version). A recorded verdict changes only on new evidence
(a different diff hash), never by re-asking.

CLI:  land_ground.py check --diff FILE --findings FILE [--root DIR]  -> JSON on stdout
"""
import argparse
import hashlib
import json
import os
import re
import sys
from pathlib import Path

RULES = ("bug", "regression", "spec-mismatch", "scope-creep", "test-weakened", "missing-test",
         "security", "error-handling", "dead-code", "dependency", "style", "other")
MECHANICAL_SOURCES = ("scope", "completeness", "literal", "literals", "verify", "relevance",
                      "harness", "baseline", "preflight", "mechanical")
SLOP = 2
PROMPT_VERSION = "ground-v1"
BLOCKING = ("high", "blocking", "critical")


def _norm(s):
    return re.sub(r"\s+", " ", str(s or "")).strip()


def parse_diff(diff_text):
    """{file: {"added": {lineno: text}, "removed": {lineno_old: text}, "lines": [all +/- texts]}}.
    added keys are new-file line numbers; removed keys are old-file line numbers."""
    out, cur, old_n, new_n = {}, None, 0, 0
    for ln in (diff_text or "").splitlines():
        if ln.startswith("+++ "):
            p = ln[4:].strip()
            cur = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            if cur:
                out.setdefault(cur, {"added": {}, "removed": {}, "lines": []})
            continue
        if ln.startswith("--- "):
            continue
        m = re.match(r"^@@ -(\d+)(?:,\d+)? \+(\d+)(?:,\d+)? @@", ln)
        if m:
            old_n, new_n = int(m.group(1)), int(m.group(2))
            continue
        if cur is None:
            continue
        if ln.startswith("+"):
            out[cur]["added"][new_n] = ln[1:]
            out[cur]["lines"].append(ln[1:])
            new_n += 1
        elif ln.startswith("-"):
            out[cur]["removed"][old_n] = ln[1:]
            out[cur]["lines"].append(ln[1:])
            old_n += 1
        else:
            old_n += 1
            new_n += 1
    return out


def _range(f):
    try:
        a = int(f.get("line_start", f.get("line", 0)) or 0)
        b = int(f.get("line_end", a) or a)
    except (TypeError, ValueError):
        return None
    if a < 0 or b < a:
        return None
    return a, b


def quotes_of(f):
    """The quotes to verify: explicit `quote` (str or list), else backticked spans of `what`
    for LLM-review sources. [] => nothing to ground on."""
    q = f.get("quote")
    if isinstance(q, str) and _norm(q):
        return [_norm(q)]
    if isinstance(q, (list, tuple)) and any(_norm(x) for x in q):
        return [_norm(x) for x in q if _norm(x)]
    if f.get("source") == "review" or f.get("source") is None:
        spans = re.findall(r"`([^`\n]{2,200})`", str(f.get("what") or ""))
        return [_norm(s) for s in spans if _norm(s)]
    return []


def _file_lines(root, rel, reader):
    if reader:
        try:
            t = reader(rel)
        except Exception:
            t = None
        return None if t is None else t.splitlines()
    if root is None:
        return None
    try:
        return (Path(root) / rel).read_text(errors="replace").splitlines()
    except OSError:
        return None


def ground_one(f, parsed, root=None, reader=None):
    """(ok, reason, finding_out). finding_out has normalised fields and `severity_class`."""
    f = dict(f)
    if f.get("source") in MECHANICAL_SOURCES:
        f["grounded"] = "mechanical"
        f["blocking"] = False
        return True, "mechanical source", f
    rule = f.get("rule")
    if rule is None and f.get("source") == "review":
        rule = "other"
    if rule not in RULES:
        return False, f"rule {rule!r} not in the closed rule list", f
    f["rule"] = rule
    rel = f.get("file") or ""
    if not rel:
        return False, "no file cited", f
    rg = _range(f)
    if rg is None:
        return False, "no valid line range cited", f
    qs = quotes_of(f)
    if not qs:
        return False, "no verbatim quote (and no backticked span to ground on)", f
    flines = _file_lines(root, rel, reader)
    d = parsed.get(rel)
    if d is None and flines is None:
        return False, f"file {rel!r} is not in the diff and is not readable", f
    a, b = rg
    lo, hi = max(1, a - SLOP), (b + SLOP) if a else 10 ** 9   # line 0 = unspecified: whole file
    window = []
    if flines is not None:
        window += [_norm(x) for x in flines[lo - 1:hi]]
    if d is not None:
        about_removal = (f.get("side") in ("old", "removed") or rule in ("regression", "test-weakened")
                         or re.search(r"(?i)\b(delet|remov|drop)", str(f.get("what") or "")))
        pool = list(d["added"].items()) + (list(d["removed"].items()) if about_removal else [])
        for n, t in pool:
            if lo <= n <= hi:
                window.append(_norm(t))
        if not a:
            window += [_norm(t) for t in (d["lines"] if about_removal else d["added"].values())]
    blob = "\n".join(window)
    for q in qs:
        if q not in blob:
            return False, f"quote {q!r} does not exist at {rel}:{a}-{b}", f
    f["quote_checked"] = qs
    f["grounded"] = "quote"
    sev = str(f.get("severity") or "").lower()
    f["blocking"] = sev in BLOCKING
    if f["blocking"] and not (f.get("repro") or "").strip():
        f["blocking"] = False
        f["demoted"] = "blocking finding without an executable repro is advisory"
    return True, "grounded", f


def ground_findings(findings, diff_text, root=None, reader=None, model="unknown"):
    """-> {"kept": [...], "dropped": [{finding, reason}], "stats": {...}}"""
    parsed = parse_diff(diff_text)
    kept, dropped = [], []
    for f in findings or []:
        if not isinstance(f, dict):
            dropped.append({"finding": f, "reason": "not an object"})
            continue
        ok, why, out = ground_one(f, parsed, root, reader)
        if ok:
            kept.append(out)
        else:
            dropped.append({"finding": f, "reason": why})
    n = len(findings or [])
    llm = [f for f in (findings or []) if isinstance(f, dict) and f.get("source") not in MECHANICAL_SOURCES]
    llm_dropped = len([d for d in dropped])
    return {"kept": kept, "dropped": dropped,
            "stats": {"model": model, "in": n, "kept": len(kept), "dropped": len(dropped),
                      "hallucinated": llm_dropped, "llm_findings": len(llm),
                      "blocking": sum(1 for f in kept if f.get("blocking")),
                      "advisory": sum(1 for f in kept if not f.get("blocking"))}}


# ---------------- demote-only judge ----------------
def verdict_key(diff_text, finding, model, prompt_version=PROMPT_VERSION):
    h = hashlib.sha256()
    for part in (diff_text or "", json.dumps(finding, sort_keys=True, default=str), model, prompt_version):
        h.update(hashlib.sha256(str(part).encode()).digest())
    return h.hexdigest()


def judge_findings(kept, diff_text, judge_fn=None, cache_dir=None, model="judge", n=3):
    """Only DEMOTES. judge_fn(finding, diff_text, order) -> "demote" | "keep" | anything else
    (= keep). Order-swapped n times; demoted iff UNANIMOUS. Cached by verdict_key so a re-run
    is idempotent. Never promotes advisory->blocking and never approves anything."""
    out = []
    for f in kept:
        f = dict(f)
        if not f.get("blocking") or judge_fn is None:
            out.append(f)
            continue
        key = verdict_key(diff_text, {k: v for k, v in f.items() if k not in ("judge",)}, model)
        cp = Path(cache_dir) / f"{key}.json" if cache_dir else None
        rec = None
        if cp and cp.is_file():
            try:
                rec = json.loads(cp.read_text())
            except (OSError, ValueError):
                rec = None
        if rec is None:
            votes = []
            for i in range(n):
                try:
                    v = judge_fn(f, diff_text, "forward" if i % 2 == 0 else "reversed")
                except Exception:
                    v = "keep"
                votes.append("demote" if v == "demote" else "keep")
            rec = {"votes": votes, "demoted": all(v == "demote" for v in votes)}
            if cp:
                try:
                    cp.parent.mkdir(parents=True, exist_ok=True)
                    tmp = cp.with_name(cp.name + f".tmp{os.getpid()}")
                    tmp.write_text(json.dumps(rec))
                    os.replace(tmp, cp)
                except OSError:
                    pass
        f["judge"] = {"votes": rec["votes"], "cached": cp is not None and cp.is_file()}
        if rec["demoted"]:
            f["blocking"] = False
            f["demoted"] = "judge unanimously demoted (unreproducible)"
        elif len(set(rec["votes"])) > 1:
            f["judge"]["split"] = True       # unresolved: stays blocking (policy default), flagged
        out.append(f)
    return out


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--diff", required=True)
    c.add_argument("--findings", required=True)
    c.add_argument("--root", default=None)
    a = ap.parse_args(argv)
    diff = Path(a.diff).read_text()
    fnd = json.loads(Path(a.findings).read_text())
    if isinstance(fnd, dict):
        fnd = fnd.get("issues") or fnd.get("findings") or []
    print(json.dumps(ground_findings(fnd, diff, root=a.root), indent=1, default=str))
    return 0


if __name__ == "__main__":
    sys.exit(main())
