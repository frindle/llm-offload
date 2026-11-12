#!/usr/bin/env python3
"""Edit-tool guards in ollama-worker.py (2026-10-09, pipeline-review steal list items 1, 2, 4, 5).

  1. POST-EDIT SYNTAX LINT-DELTA: edit_file / write_file REJECT (nothing written) a change that
     introduces a syntax error the file did not have; pre-existing errors never block; unknown
     types / kill switch pass through.
  2. AIDER-STYLE HINTS: a missed old_string gets 'did you mean' lines (with line numbers), and
     'this change looks already applied' when new_string is already in the file.
  4. SALVAGE: a non-converged exit keeps the diff as <transcript>.salvage.patch (+ metrics).
  5. REQUIRED PARAMS: every tool parameter is required in the schema; a call that omits the
     defaulted ones (offset/length) is still honoured.

--revert-check mutates the worker (WORKER_SRC env) and requires RED."""
import importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="editguard-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    os.environ.pop("WORKER_SYNTAX_GUARD", None)
    sys.path.insert(0, str(WORKER.parent))
    spec = importlib.util.spec_from_file_location("ow_editguard", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert str(m.LOG_DIR).startswith(os.environ["HOME"]), m.LOG_DIR
    return m


def tc(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def drive(m, script, files=None, max_iters=12, verify=None):
    wt = Path(tempfile.mkdtemp(prefix="editguard-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    for k, v in (files or {"a.py": "x = 1\n"}).items():
        (wt / k).write_text(v)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"], cwd=wt, check=True)
    seq = list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        return seq.pop(0) if seq else tc("task_complete", {"summary": "done"})

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "edit a.py", verify,
                   max_iters, 0.0, 65536, None, max_tokens=8192)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    return wt, "\n".join(logs)


def main():
    m = _load()
    wt = Path(tempfile.mkdtemp(prefix="editguard-unit-"))

    print("== 1. syntax lint-delta guard ==")
    (wt / "ok.py").write_text("def f():\n    return 1\n")
    r = m.tool_edit_file(wt, {"path": "ok.py", "old_string": "return 1", "new_string": "return (1"})
    check("python: edit introducing a syntax error is REJECTED", r.startswith("ERROR") and "REJECTED" in r, True)
    check("...parser error shown", "SyntaxError" in r, True)
    check("...file left byte-identical", (wt / "ok.py").read_text(), "def f():\n    return 1\n")
    r = m.tool_edit_file(wt, {"path": "ok.py", "old_string": "return 1", "new_string": "return 2"})
    check("python: a valid edit still applies", r.startswith("OK"), True)
    (wt / "broken.py").write_text("def f(:\n    pass\n")
    r = m.tool_edit_file(wt, {"path": "broken.py", "old_string": "pass", "new_string": "pass  # touched"})
    check("DELTA: a file that was already broken does not block an edit", r.startswith("OK"), True)
    r = m.tool_write_file(wt, {"path": "new.py", "content": "def g(:\n"})
    check("write_file of a new broken .py is REJECTED", r.startswith("ERROR") and "REJECTED" in r, True)
    check("...and nothing was created", (wt / "new.py").exists(), False)
    r = m.tool_write_file(wt, {"path": "ok.py", "content": "if True\n    x = 1\n"})
    check("write_file overwrite that breaks a good file is REJECTED", "REJECTED" in r, True)
    check("...old content kept", "return 2" in (wt / "ok.py").read_text(), True)
    (wt / "d.json").write_text('{"a": 1}')
    r = m.tool_edit_file(wt, {"path": "d.json", "old_string": '1}', "new_string": '1,}'})
    check("json: trailing comma REJECTED", "REJECTED" in r, True)
    r = m.tool_write_file(wt, {"path": "notes.txt", "content": "def f(:\n"})
    check("unknown type (.txt) is never checked", r.startswith("OK"), True)
    (wt / "t.ts").write_text("export const a: number = 1;\n")
    r = m.tool_edit_file(wt, {"path": "t.ts", "old_string": "= 1;", "new_string": "= ;"})
    check("ts: syntax error REJECTED (node type-stripper)", "REJECTED" in r, True)
    r = m.tool_edit_file(wt, {"path": "t.ts", "old_string": "a: number", "new_string": "a: string"})
    check("ts: a type-only change applies", r.startswith("OK"), True)
    (wt / "m.js").write_text("import x from 'y';\nexport const a = 1;\n")
    r = m.tool_edit_file(wt, {"path": "m.js", "old_string": "= 1;", "new_string": "= (;"})
    check("esm .js: syntax error REJECTED", "REJECTED" in r, True)
    os.environ["WORKER_SYNTAX_GUARD"] = "0"
    try:
        r = m.tool_write_file(wt, {"path": "kill.py", "content": "def g(:\n"})
        check("kill switch WORKER_SYNTAX_GUARD=0 disables the guard", r.startswith("OK"), True)
    finally:
        del os.environ["WORKER_SYNTAX_GUARD"]
    m._SYNTAX_GUARD_ON[0] = False
    r = m.tool_write_file(wt, {"path": "kill2.py", "content": "def g(:\n"})
    check("profile switch (robust.syntax_guard: false) disables the guard", r.startswith("OK"), True)
    m._SYNTAX_GUARD_ON[0] = True
    import worker_robust as W
    check("guard never raises on garbage input", W.syntax_delta(None, None, None), None)
    check("profile flag parsed", W.syntax_guard_enabled({"syntax_guard": False}, env={}), False)

    print("== 2. edit-failure hints ==")
    body = "def compute_total(items):\n    total = 0\n    for it in items:\n        total += it.price\n    return total\n"
    (wt / "h.py").write_text(body)
    r = m.tool_edit_file(wt, {"path": "h.py", "old_string": "    for itm in items:\n        total += itm.price",
                              "new_string": "    for it in items:\n        total += it.cost"})
    check("not-found still an ERROR", r.startswith("ERROR: old_string not found"), True)
    check("...'did you mean' with a real line number", "line 3: " in r and "for it in items:" in r, True)
    check("...suggests the next read_file call", "read_file offset=" in r, True)
    (wt / "h.py").write_text(body.replace("it.price", "it.cost"))
    r = m.tool_edit_file(wt, {"path": "h.py", "old_string": "total += it.price", "new_string": "total += it.cost"})
    check("new_string already present -> 'already applied'", "ALREADY APPLIED" in r and "line 4" in r, True)
    r = m.tool_edit_file(wt, {"path": "h.py", "old_string": "zzzz qqqq wwww", "new_string": "yyyy"})
    check("nothing similar -> plain error, no bogus hint", "Did you mean" not in r and "ALREADY" not in r, True)

    print("== 5. required tool parameters ==")
    opt = {t["function"]["name"]: [k for k in t["function"]["parameters"]["properties"]
                                   if k not in t["function"]["parameters"].get("required", [])] for t in m.TOOLS}
    check("no tool has an optional parameter", {k: v for k, v in opt.items() if v}, {})
    rf = [t for t in m.TOOLS if t["function"]["name"] == "read_file"][0]["function"]
    check("read_file defaults documented in the descriptions",
          "use 1" in rf["parameters"]["properties"]["offset"]["description"]
          and "use 0" in rf["parameters"]["properties"]["length"]["description"], True)
    (wt / "big.txt").write_text("".join(f"line {i}\n" for i in range(1, 11)))
    old = m.tool_read_file(wt, {"path": "big.txt"})
    new = m.tool_read_file(wt, {"path": "big.txt", "offset": 1, "length": 0})
    check("an OLD call (no offset/length) reads exactly like offset=1,length=0", old, new)
    check("...range call still honoured", m.tool_read_file(wt, {"path": "big.txt", "offset": 3, "length": 2}).count("line "), 2)
    check("defaulted params are not flagged missing by validate_calls",
          W.validate_calls([{"name": "read_file", "arguments": {"path": "a"}}], m.TOOLS), [])
    check("a real required param still is",
          W.validate_calls([{"name": "read_file", "arguments": {}}], m.TOOLS), [("read_file", ["path"])])

    print("== 4. salvage on a non-converged exit ==")
    same = ("edit_file", {"path": "a.py", "old_string": "NOPE_NOT_THERE", "new_string": "x"})
    script = [tc("write_file", {"path": "b.py", "content": "y = 2\n"})] + [tc(*same) for _ in range(8)]
    wt2, log = drive(m, script)
    check("the run ended on a named loop reason", "TERMINAL REASON: error_loop" in log, True)
    patches = list(Path(m.LOG_DIR).glob("*.salvage.patch"))
    check("a salvage patch was kept next to the transcript", len(patches), 1)
    check("...and holds the model's new file", patches and "b.py" in patches[0].read_text()
          and "+y = 2" in patches[0].read_text(), True)
    check("SALVAGE logged", "SALVAGE: kept the non-converged diff" in log, True)
    check("the worktree diff itself is untouched", (wt2 / "b.py").read_text(), "y = 2\n")
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("guard never rejects (edit path)",
     "    new_content = content.replace(old_string, new_string, 1)\n    rejected = _syntax_guard_reject(args[\"path\"], content, new_content)\n",
     "    new_content = content.replace(old_string, new_string, 1)\n    rejected = None\n"),
    ("guard never rejects (write path)",
     "    rejected = _syntax_guard_reject(args[\"path\"], before, content)\n",
     "    rejected = None\n"),
    ("guard is not a delta (pre-existing errors block)",
     "        if before is not None and syntax_error_for(path, before, timeout):\n            return None\n",
     "", "worker_robust.py"),
    ("kill switch ignored",
     "    if ev in (\"0\", \"false\", \"off\", \"no\"):\n        return False\n", "", "worker_robust.py"),
    ("no 'already applied' hint", "        if new_string and new_string.strip() and new_string in content:",
     "        if False:"),
    ("no 'did you mean' lines", "            if picks:\n                lines_out = []", "            if False:\n                lines_out = []"),
    ("read_file params optional again", '"required": ["path", "offset", "length"],', '"required": ["path"],'),
    ("defaulted params flagged missing",
     "                     if k not in DEFAULTED_PARAMS.get(name, {})]", "                     ]", "worker_robust.py"),
    ("no salvage patch", "            _salv = save_salvage_patch(cwd,", "            _salv = (lambda *a, **k: {})(cwd,"),
]


def revert_check():
    bad = 0
    for mut in MUTATIONS:
        name, old, new = mut[:3]
        fname = mut[3] if len(mut) > 3 else "ollama-worker.py"
        srcp = (HERE / fname) if fname != "ollama-worker.py" else WORKER
        src = srcp.read_text()
        assert src.count(old) == 1, f"anchor missing: {name} ({src.count(old)})"
        tmpd = Path(tempfile.mkdtemp(prefix="editguard-mut-"))
        for f in HERE.glob("*.py"):
            pass
        for dep in ("worker_robust.py", "model_profile.py", "ollama-worker.py"):
            if (HERE / dep).exists():
                (tmpd / dep).write_text((HERE / dep).read_text())
        (tmpd / fname).write_text(src.replace(old, new))
        env = {**os.environ, "WORKER_SRC": str(tmpd / "ollama-worker.py")}
        r = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True, timeout=900)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
