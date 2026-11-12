#!/usr/bin/env python3
"""spec_conformance -- does the authored TASK.md / produced code stay inside the INTENT's named surface?

WHY (2026-10-14, 1a53991): a dispatch whose --intent was the unfilled template placeholder produced a TASK.md
that named `bonusTier`, `bonusAmount`, `cardValue`, `CardBonusResult` (none of which the intent -- or the base
code -- contained) and a fixture/implementation with invented bronze/silver/gold tiers and 5%/10%/20% rates.
Every downstream gate compared the code against the TASK (which was itself the invention), so everything
agreed. The only reference that cannot be invented by the authoring model is the INTENT (+ `--interface`)
and the base code. This module diffs the TASK's named surface and the produced code's public surface and
fixed numeric constants against exactly those.

Pure functions, no model calls:
  - task_named_surface(task_text)        -> the identifiers listed under `## Must contain` / `## Entry point`
  - code_public_surface(src, lang)       -> exported names, interface/type fields, returned object keys
  - invented_terms(intent, interface, base, names)  -> [name] not traced to intent/interface/base
  - invented_constants(intent+task, base, added_src) -> numeric literals (0.05, 100 ...) in ADDED code not
                                                       in intent/interface/TASK/base
  - verdict(...)  -> {"status": "ok"|"warn"|"block", "invented": [...], "constants": [...], "why": str}

BLOCK only on the strong signal: the intent is THIN (< 40 words) and ALL (>= 3) of the TASK's named surface is
absent from the intent+interface+base (replayed over 45 recorded runs: only the placeholder run blocks). Everything weaker is WARN (logged, never blocks). Kill switch:
SPEC_CONFORMANCE=off (verdict is always ok); SPEC_CONFORMANCE=warn (never blocks).
"""
from __future__ import annotations

import os
import re

_STOP = {"true", "false", "null", "none", "undefined", "self", "this", "string", "number", "boolean", "void",
         "int", "float", "str", "bool", "dict", "list", "any", "return", "export", "import", "const", "let",
         "var", "function", "def", "class", "type", "interface", "async", "await", "new", "if", "else",
         "for", "while", "try", "catch", "pass", "and", "or", "not", "in", "is", "main", "args", "kwargs"}


def mode() -> str:
    return os.environ.get("SPEC_CONFORMANCE", "on").strip().lower()


def task_named_surface(task_text: str) -> list[str]:
    """Identifiers the TASK commits to: the backticked bullets under `## Must contain`, filtered to
    identifier-shaped tokens (a literal like `>=` or `export {}` is not a name)."""
    m = re.search(r"##+\s*Must contain[^\n]*\n(.*?)(?=\n##\s|\Z)", task_text or "", re.S | re.I)
    out = []
    if m:
        for line in m.group(1).splitlines():
            if not re.match(r"\s*-\s", line):
                continue
            for g in re.finditer(r"`([^`\n]{1,200})`", line):
                t = g.group(1).strip()
                if re.fullmatch(r"[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)*", t) and t.lower() not in _STOP \
                        and "TODO" not in t:
                    out.append(t)
    return list(dict.fromkeys(out))


def _words(text: str) -> set[str]:
    """Lower-cased identifier fragments (camelCase / snake_case split) AND whole identifiers."""
    out = set()
    for ident in re.findall(r"[A-Za-z_$][\w$]*", text or ""):
        out.add(ident.lower())
        for part in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", ident):
            if len(part) >= 3:
                out.add(part.lower())
    return out


def _traced(name: str, ref_words: set[str], ref_text: str) -> bool:
    if name in (ref_text or "") or name.lower() in ref_words:
        return True
    parts = [p.lower() for p in re.findall(r"[A-Z]?[a-z]+|[A-Z]+(?![a-z])|\d+", name) if len(p) >= 3]
    # a compound (bonusTier) traces when the intent names ALL its parts (bonus, tier); a single word needs itself
    return bool(parts) and all(p in ref_words for p in parts)


def invented_terms(intent: str, interface: str, base_text: str, names: list[str]) -> list[str]:
    ref = "\n".join(x for x in (intent, interface, base_text) if x)
    w = _words(ref)
    return [n for n in names if not _traced(n.split(".")[-1], w, ref)]


def code_public_surface(src: str, lang: str = "ts") -> list[str]:
    """Exported function/const/class names, interface/type member names, and the keys of returned object
    literals (`return { a: 1, b }`) of a TS/JS module; public def/class names and returned dict keys of a
    Python module. Heuristic regex, tuned for precision (it only has to surface invented NAMES)."""
    names = []
    if str(lang).lower() in ("py", "python"):
        names += re.findall(r"^(?:async\s+)?def\s+([A-Za-z]\w*)\s*\(", src or "", re.M)
        names += re.findall(r"^class\s+([A-Za-z]\w*)", src or "", re.M)
        for m in re.finditer(r"return\s*\{([^{}]{0,300})\}", src or ""):
            names += re.findall(r"['\"]([A-Za-z_]\w*)['\"]\s*:", m.group(1))
    else:
        names += re.findall(r"^export\s+(?:async\s+)?(?:function|const|class|let|var)\s+([A-Za-z_$][\w$]*)", src or "", re.M)
        names += re.findall(r"^export\s+(?:interface|type|enum)\s+([A-Za-z_$][\w$]*)", src or "", re.M)
        for m in re.finditer(r"(?:interface\s+\w+|type\s+\w+\s*=)\s*\{([^{}]*)\}", src or ""):
            names += re.findall(r"([A-Za-z_$][\w$]*)\??\s*:", m.group(1))
        for m in re.finditer(r"return\s*\{([^{}]{0,300})\}", src or ""):
            names += re.findall(r"(?:^|[,{]\s*)([A-Za-z_$][\w$]*)\s*(?::|,|$)", m.group(1).strip() + ",")
    return [n for n in dict.fromkeys(names) if n.lower() not in _STOP]


_NUM_RE = re.compile(r"(?<![\w.$])(\d+\.\d+|\d{2,})(?![\w.])")


def invented_constants(ref_text: str, base_text: str, added_src: str) -> list[str]:
    """Numeric constants (decimals, or 2+ digit integers) in ADDED code that appear in neither the reference
    text (intent + interface + TASK) nor the base. Small integers (0-9) are exempt."""
    ref = (ref_text or "") + "\n" + (base_text or "")
    refnums = set(_NUM_RE.findall(ref))
    refnums |= {n.replace(",", "") for n in re.findall(r"\d[\d,]*\d", ref)}
    out = []
    for ln in (added_src or "").splitlines():
        s = ln.strip()
        if s.startswith(("//", "#", "*", "/*")):
            continue
        for n in _NUM_RE.findall(re.sub(r"(['\"`]).*?\1", "''", s)):
            if n not in refnums and n not in out and not re.fullmatch(r"1e\d+|0\.0+1|1e-\d+", n):
                out.append(n)
    return out


def verdict(intent: str, interface: str, task_text: str, base_text: str, added_src: str = "",
            lang: str = "ts") -> dict:
    md = mode()
    res = {"status": "ok", "invented": [], "constants": [], "named": [], "why": ""}
    if md in ("off", "0", "no", "false"):
        return res
    named = task_named_surface(task_text)
    res["named"] = named
    inv = invented_terms(intent, interface, base_text, named)
    res["invented"] = inv
    res["constants"] = invented_constants("\n".join((intent or "", interface or "", task_text or "")), base_text,
                                          added_src)[:8]
    thin = len((intent or "").split()) < 40
    if thin and len(named) >= 3 and len(inv) == len(named):
        res["status"] = "warn" if md == "warn" else "block"
        res["why"] = (f"{len(inv)}/{len(named)} of the TASK's named surface ({', '.join(inv[:6])}) appears nowhere in "
                      f"the --intent/--interface or the base code: the TASK author invented the spec (the intent was "
                      f"a placeholder or far too thin). Fix the intent, not the harness.")
    elif inv or res["constants"]:
        res["status"] = "warn"
        res["why"] = (f"TASK/code names not traceable to the intent: {inv[:6]}; numeric constants not in the "
                      f"intent/TASK/base: {res['constants'][:6]}")
    return res


if __name__ == "__main__":
    import json
    import sys
    if len(sys.argv) >= 3:
        intent, task = open(sys.argv[1]).read(), open(sys.argv[2]).read()
        print(json.dumps(verdict(intent, "", task, ""), indent=1))
    else:
        print(__doc__)
