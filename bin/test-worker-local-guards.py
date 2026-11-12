#!/usr/bin/env python3
"""Local-guards regression (worker_local_guards.py wired into ollama-worker.py run_task): scripted tool-call
sequences reproduce the psab-C1-a465e147cf00 pattern (identical web_search x8 about a repo-local Prisma column,
edits with no verify). Model stubbed, HOME sandboxed.  Run: python3 test-worker-local-guards.py [--mutants]"""
import importlib.util, os, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
FAILS = []


def ok(name, cond, detail=""):
    print(f"  ok   {name}" if cond else f"  FAIL {name} {detail}")
    if not cond:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="lg-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    sys.path.insert(0, str(SRC.parent))
    spec = importlib.util.spec_from_file_location("ow_lg", SRC)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    return m


def tc(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def drive(m, script, max_iters=14):
    wt = Path(tempfile.mkdtemp(prefix="lg-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "prisma").mkdir()
    (wt / "prisma/schema.prisma").write_text("model syncEvent { id Int }\n")
    (wt / "a.txt").write_text("start\n")
    (wt / "verify.sh").write_text("#!/bin/bash\ngrep -q GOOD a.txt\n")
    subprocess.run(["git", "add", "."], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "b"], cwd=wt, check=True)
    seq = list(script)
    seen = []

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        seen[:] = [dict(x) for x in messages]
        return seq.pop(0) if seq else tc("run_bash", {"command": "true"})

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    m.tool_web_search = lambda cwd, args, host: "EXTERNAL-RESULT unrelated site"
    m._dispatch_metrics.clear()
    logs = []
    orig = m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "make a.txt contain GOOD", "bash verify.sh",
                   max_iters, 0.0, 65536, None, max_tokens=8192)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    tools = [x.get("content", "") for x in seen if x.get("role") in ("tool",) or str(x.get("content", "")).startswith("[tool result")]
    return wt, logs, dict(m._dispatch_metrics), tools, seen


def ws(q):
    return tc("web_search", {"query": q})


def main():
    m = _load()
    os.environ.pop("WORKER_LOCAL_GUARDS", None)
    Q = "does syncEvent have skippedCancelled column prisma"
    print("== identical searches x8: 3rd blocked + redirected to the repo ==")
    _, logs, met, tools, _ = drive(m, [ws(Q)] * 8, max_iters=9)
    blocked = [t for t in tools if "THIS repository" in t]
    ok("calls 1-2 served externally", sum("EXTERNAL-RESULT" in t for t in tools) == 2, [t[:60] for t in tools])
    ok("3rd+ blocked with ERROR naming the repo", len(blocked) >= 1 and blocked[0].startswith("ERROR"), tools[:4])
    ok("block names prisma/schema.prisma", any("prisma/schema.prisma" in t for t in blocked))
    ok("named event repeated_search_blocked in log + metrics",
       any("event=repeated_search_blocked" in l for l in logs) and met.get("local_guard_events", {}).get("repeated_search_blocked", 0) >= 1, met.get("local_guard_events"))
    print("== near-identical (reordered/stopword) queries count as the same ==")
    _, logs, met, tools, _ = drive(m, [ws("syncEvent skippedCancelled prisma column"), ws("does the syncEvent have skippedCancelled column prisma"),
                                      ws("prisma column skippedCancelled syncEvent")], max_iters=4)
    ok("3rd near-identical blocked", met.get("local_guard_events", {}).get("repeated_search_blocked", 0) == 1, met.get("local_guard_events"))
    print("== local identifier -> hint, search still runs ==")
    _, logs, met, tools, _ = drive(m, [ws("syncEvent model fields")], max_iters=2)
    ok("hint present and external result kept", any("found locally at" in t and "EXTERNAL-RESULT" in t for t in tools), tools[:2])
    print("== genuine external search, nothing local: untouched ==")
    _, logs, met, tools, _ = drive(m, [ws("how does the fetch api abort signal work")], max_iters=2)
    ok("no hint, no block, no events", tools and tools[0] == "EXTERNAL-RESULT unrelated site" and not met.get("local_guard_events"), (tools[:1], met.get("local_guard_events")))
    print("== edits without verify: one nudge per streak ==")
    ed = lambda c: tc("write_file", {"path": "a.txt", "content": c})
    _, logs, met, tools, _ = drive(m, [ed("x1\n"), ed("x2\n"), tc("read_file", {"path": "a.txt"}), ed("x3\n"), tc("read_file", {"path": "a.txt"}),
                                      tc("read_file", {"path": "verify.sh"})], max_iters=7)
    n = sum("without running the verify" in t for t in tools)
    ok("nudge appended exactly once for the streak", n == 1 and met["local_guard_events"].get("verify_nudge") == 1, (n, met.get("local_guard_events")))
    ok("nudge names the real verify command", any("bash verify.sh" in t and "HARNESS" in t for t in tools))
    print("== a verify run resets the streak ==")
    _, logs, met, tools, _ = drive(m, [ed("x1\n"), tc("run_bash", {"command": "bash verify.sh"}), ed("x2\n"), tc("read_file", {"path": "a.txt"})], max_iters=5)
    ok("no nudge when verify intervenes", not met.get("local_guard_events", {}).get("verify_nudge"), met.get("local_guard_events"))
    print("== task_complete while unverified: counted + refused by the claim gate ==")
    _, logs, met, tools, _ = drive(m, [ed("BAD\n"), tc("task_complete", {"summary": "done"})], max_iters=3)
    ok("complete_unverified event", met.get("local_guard_events", {}).get("complete_unverified") == 1, met.get("local_guard_events"))
    ok("claim gate refused (verify fails)", met.get("status") != "converged", met.get("status"))
    print("== _find_local: a path-shaped ident matches by PATH, not basename (psc-C4) ==")
    sys.path.insert(0, str(HERE))
    import worker_local_guards as _wlg
    _d = tempfile.mkdtemp(prefix="lg-find-")
    for _f in ("app/api/a/route.ts", "app/api/b/route.ts", "lib/util.py"):
        os.makedirs(os.path.dirname(os.path.join(_d, _f)), exist_ok=True)
        open(os.path.join(_d, _f), "w").write("x\n")
    subprocess.run(["git", "init", "-q"], cwd=_d)
    subprocess.run(["git", "add", "-A"], cwd=_d)
    ok("nonexistent path does NOT match other route.ts files", _wlg._find_local(_d, "app/api/import/route.ts") == [], _wlg._find_local(_d, "app/api/import/route.ts"))
    ok("existing path matches exactly itself", _wlg._find_local(_d, "app/api/a/route.ts") == ["app/api/a/route.ts"])
    ok("path suffix matches", _wlg._find_local(_d, "api/b/route.ts") == ["app/api/b/route.ts"])
    # adversarial review 2026-10-13: a path suffix matches on a '/' boundary only ("pi/b/route.ts" is not "api/b/route.ts")
    ok("path suffix needs a '/' boundary", _wlg._find_local(_d, "pi/b/route.ts") == [], _wlg._find_local(_d, "pi/b/route.ts"))
    ok("bare filename still matches by basename", sorted(_wlg._find_local(_d, "route.ts")) == ["app/api/a/route.ts", "app/api/b/route.ts"])
    shutil.rmtree(_d, ignore_errors=True)
    print("== kill switch WORKER_LOCAL_GUARDS=0 ==")
    os.environ["WORKER_LOCAL_GUARDS"] = "0"
    _, logs, met, tools, _ = drive(m, [ws(Q)] * 5, max_iters=6)
    ok("no blocks, no hints, no events", not any("THIS repository" in t or "found locally" in t for t in tools) and not met.get("local_guard_events"), met.get("local_guard_events"))
    _, logs, met, tools, _ = drive(m, [ed("x1\n"), ed("x2\n"), tc("read_file", {"path": "a.txt"})], max_iters=4)
    ok("no verify nudge when off", not any("without running the verify" in t for t in tools))
    os.environ.pop("WORKER_LOCAL_GUARDS")


MUTANTS = [
    ("worker_local_guards.py", "if self.queries[key] > SEARCH_REPEAT_N:", "if False:"),
    ("worker_local_guards.py", "if self.on and self.unverified and self.verify:", "if False:"),
    ("worker_local_guards.py", "            self._ev(\"local_redirect\"", "            return None, \"\"\n            self._ev(\"local_redirect\""),
    ("worker_local_guards.py", "self.pending = True", "pass"),
    ("worker_local_guards.py", "return (env if env is not None else os.environ).get(\"WORKER_LOCAL_GUARDS\", \"1\").strip() != \"0\"", "return True"),
    ("worker_local_guards.py", 'if "/" in ident:', 'if False:'),
    ("ollama-worker.py", "            elif _lg_block is not None:", "            elif False:"),
    ("ollama-worker.py", "                result = _lg_prefix + result", "                pass"),
]


def mutants():
    bad = 0
    for i, (f, old, new) in enumerate(MUTANTS):
        d = Path(tempfile.mkdtemp(prefix="lg-mut-"))
        for p in ("ollama-worker.py", "worker_local_guards.py"):
            shutil.copy(HERE / p, d / p)
        for p in HERE.glob("*.py"):
            if not (d / p.name).exists():
                (d / p.name).symlink_to(p)
        t = (d / f).read_text()
        if old not in t:
            print(f"MUTANT {i} NOT APPLICABLE: {old[:50]}"); bad += 1; continue
        (d / f).write_text(t.replace(old, new, 1))
        r = subprocess.run([sys.executable, str(HERE / "test-worker-local-guards.py")], env={**os.environ, "WORKER_SRC": str(d / "ollama-worker.py")},
                           capture_output=True, text=True)
        killed = r.returncode != 0
        print(f"mutant {i} ({f}: {old[:44]!r}): {'killed' if killed else 'SURVIVED'}")
        bad += not killed
    print("ALL MUTANTS KILLED" if not bad else f"{bad} MUTANT(S) SURVIVED")
    return bad


if __name__ == "__main__":
    if "--mutants" in sys.argv:
        sys.exit(1 if mutants() else 0)
    main()
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
