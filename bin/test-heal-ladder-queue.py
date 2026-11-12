#!/usr/bin/env python3
"""Queue-side escalation/ctx fixes of 2026-10-02 (daemon code):
  * escalation_heal_pending asks the LADDER (dispatch-self-heal.may_heal), so a
    gate-FAIL / auto-land escalation -- which a b/c/d review re-authors via
    retry-notes -- is not declared stuck (bundle parked) while the watcher is about
    to rescue it; PIPELINE BUG (final rung only) and a recorded final-rung still are.
  * the context_threshold resume ceiling is the LANE's (resolve_ctx_ceiling ->
    darkbloom_ctx_ceiling on Darkbloom), not the fixed 64GB-Studio constant.
--revert-check mutates the sources (QUEUE_SRC / HEAL_SRC env) and requires RED."""
import ast, importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
QUEUE = Path(os.environ.get("QUEUE_SRC") or HERE / "ollama-queue.py")
HEAL = Path(os.environ.get("HEAL_SRC") or HERE / "dispatch-self-heal.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [str(path)]
    ld.exec_module(m)
    return m


def main():
    h = load(HEAL, "heal_hl")
    q = load(QUEUE, "q_hl")
    check("may_heal: gate-FAIL reason -> ladder can act", h.may_heal("gate verdict fail on coding job x"), True)
    check("may_heal: auto-land refusal -> ladder can act",
          h.may_heal("auto-land of gate-PASSED job a refused: worktree x no longer matches"), True)
    check("may_heal: authoring-class -> True", h.may_heal("authoring failed DETERMINISTICALLY"), True)
    check("may_heal: PIPELINE BUG -> final rung only -> False", h.may_heal("PIPELINE BUG SUSPECTED: x"), False)
    ehp = q.escalation_heal_pending
    check("queue: gate-FAIL escalation inside the window -> heal pending (LIVE, not parked)",
          ehp("p", "s", "gate verdict fail on coding job x", 0.0, 60.0, ledger={}, mod=h), True)
    check("queue: PIPELINE BUG -> not healable",
          ehp("p", "s", "PIPELINE BUG SUSPECTED: x", 0.0, 60.0, ledger={}, mod=h), False)
    check("queue: a final-rung recorded since seen -> not healable",
          ehp("p", "s", "gate verdict fail on coding job x", 0.0, 60.0, mod=h,
              ledger={"p/s": {"attempts": 0, "log": [{"action": "final-rung",
                                                        "at": "1970-01-01T00:00:30Z"}]}}), False)
    # ctx: Darkbloom lane resolves its own ceiling
    q._darkbloom_url = lambda: "http://127.0.0.1:1"
    q.darkbloom_ctx_ceiling = lambda model=None, **k: 65536
    check("resolve_ctx_ceiling: Darkbloom lane -> darkbloom ceiling",
          q.resolve_ctx_ceiling("studio", "qwen3.6-35b-a3b-vl-mtp-mxfp8"), 65536)
    src = QUEUE.read_text()
    tree = ast.parse(src)
    fns = {n.name: n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef)}

    def uses_resolver(fn):
        return any(isinstance(c, ast.Call) and getattr(c.func, "id", "") == "resolve_ctx_ceiling"
                   for c in ast.walk(fns[fn]))
    check("auto-resume (daemon) sizes the ctx bump with resolve_ctx_ceiling",
          uses_resolver("_auto_resume_paused_jobs"), True)
    resumers = [n for n, f in fns.items() if "context_threshold" in ast.get_source_segment(src, f)
                and "new_ctx = min(current * 2, ceiling)" in ast.get_source_segment(src, f)
                and "ceiling" not in [a.arg for a in f.args.args]]   # pure deciders take it in
    check("manual resume sizes the ctx bump with resolve_ctx_ceiling",
          [uses_resolver(n) for n in resumers], [True] * max(1, len(resumers)))
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("QUEUE_SRC", "predicate back to classify()",
     '        _may = getattr(mod, "may_heal", None)\n', '        _may = None\n'),
    ("QUEUE_SRC", "final-rung not a decline",
     '("none", "refused", "exhausted", "final-rung")', '("none", "refused", "exhausted")'),
    ("QUEUE_SRC", "auto-resume fixed Studio ceiling",
     '                ceiling = resolve_ctx_ceiling(job.get("host_pref"), job["model"])\n',
     '                ceiling = AUTO_RESUME_STUDIO_CTX_CEILING\n'),
    ("QUEUE_SRC", "manual resume fixed Studio ceiling",
     '                       else resolve_ctx_ceiling(job.get("host_pref"), job["model"]))',
     '                       else AUTO_RESUME_STUDIO_CTX_CEILING)'),
    ("HEAL_SRC", "may_heal ignores verdicts",
     '    return any(rung_for(kind, v, reason) != "final" for v in ("b", "c", "d", None))',
     '    return kind != "none"'),
]


def revert_check():
    bad = 0
    srcs = {"QUEUE_SRC": QUEUE, "HEAL_SRC": HEAL}
    for var, name, old, new in MUTATIONS:
        src = srcs[var].read_text()
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-mut.py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, var: f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
