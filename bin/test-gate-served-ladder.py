#!/usr/bin/env python3
"""Gate model-fallback ladder skips rungs that SERVE as an already-tried model
(Darkbloom aliases every ':'-tagged name to one default, so the old 4-rung ladder
was 4 runs of the same model). --revert-check mutates gate-on-complete.py and
requires RED. GATE_SRC env overrides the file under test."""
import ast, importlib.util, os, subprocess, sys, tempfile, types
from importlib.machinery import SourceFileLoader
from pathlib import Path

SRC = Path(os.environ.get("GATE_SRC") or Path(__file__).resolve().parent / "gate-on-complete.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    ld = SourceFileLoader("gc_sl", str(SRC))
    g = importlib.util.module_from_spec(importlib.util.spec_from_loader("gc_sl", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(g)
    fake = types.SimpleNamespace(
        _darkbloom_url=lambda: "http://127.0.0.1:1",
        DARKBLOOM_PREFS=("auto", "studio"),
        _darkbloom_model=lambda m: "DB-DEFAULT" if (not m or ":" in m) else m)
    g._SERVED_Q = fake
    check("served: ':'-tag on studio -> Darkbloom default", g.served_model("qwen3:32b-q8_0", "studio"), "DB-DEFAULT")
    check("served: bare id passes through", g.served_model("Qwen3.5-9B", "studio"), "Qwen3.5-9B")
    check("served: non-Darkbloom host unchanged", g.served_model("qwen3:32b-q8_0", "unraid"), "qwen3:32b-q8_0")
    fake._darkbloom_url = lambda: None
    check("served: Darkbloom off -> unchanged", g.served_model("qwen3:32b-q8_0", "studio"), "qwen3:32b-q8_0")
    fake._darkbloom_url = lambda: "http://127.0.0.1:1"
    old = g._normalize_ladder(["qwen3.8:27b-q4_K_M", "qwen3.6:35b-a3b-q4_K_M",
                               "qwen3:32b-q8_0", "llama3.3:70b"])
    check("the old 4-rung ladder is exhausted after the first Darkbloom run",
          g.plan_model_ladder("qwen3.8:27b-q4_K_M", [], old, served=g.served_model), None)
    check("...including when the job ran on the bare default id",
          g.plan_model_ladder("DB-DEFAULT", [], old, served=g.served_model), None)
    # the production call site passes the served mapper
    calls = [n for n in ast.walk(ast.parse(SRC.read_text()))
             if isinstance(n, ast.Call) and getattr(n.func, "id", "") == "plan_model_ladder"
             and len(n.args) >= 3 and getattr(n.args[0], "id", "") == "cur_model"]
    check("autofix call site passes served=served_model",
          [any(k.arg == "served" and getattr(k.value, "id", "") == "served_model" for k in c.keywords)
           for c in calls], [True])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("served dedupe dropped",
     '        if e["model"] not in tried and sv(e["model"], e["host"]) not in tried_served:',
     '        if e["model"] not in tried:'),
    ("call site without served", "ladder, served=served_model,", "ladder,"),
    ("served ignores Darkbloom", "            return q._darkbloom_model(model)", "            return model"),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-gate.py", delete=False, dir=str(SRC.parent)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "GATE_SRC": f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
