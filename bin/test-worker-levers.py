#!/usr/bin/env python3
"""Opt-in CONVERGENCE LEVERS for coding runs (worker_levers.py + 6 gated call sites in ollama-worker.py).

  L1 VERIFY_ON_EDIT=1   verify after every 2nd successful non-harness edit (max 6 runs), compact failing-case
                        summary appended to the edit's tool result; PASS says "call task_complete".
  L2 PROTECT_HARNESS=1  sealed coding harness only: refimpl.py invisible/unreadable, `python3 refimpl.py` refused,
                        harness files un-editable, git checkout/restore/stash of harness or whole tree refused,
                        revert refused while the last verify PASSED.
  L3 READ_MIN_WINDOW=1  read_file length < 120 is served >= 120 lines with the real range noted.
  L4 PROSE_STICKY_NOTHINK=1  after the FIRST prose-runaway abort the recovery request profile (thinking off + recovery
                        sampling) is kept for every later author turn; turns before it / non-author roles untouched.
Every lever defaults OFF and must then be BYTE-IDENTICAL to the pre-lever worker: asserted by replaying the same
scripted tool loop through the REAL run_task (stub OpenAI SSE server, no GPU) with the flags unset vs "0" and
comparing every request body the model would have seen. --revert-check mutates the code and requires RED."""
import importlib.util, json, os, re, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
LEVERS = Path(os.environ.get("LEVERS_SRC") or HERE / "worker_levers.py")
FAILS = []
LAST_ENV = []
SEQ, REQS = [], []
FLAG_KEYS = ("VERIFY_ON_EDIT", "PROTECT_HARNESS", "READ_MIN_WINDOW", "PROSE_STICKY_NOTHINK", "READONLY_JOB", "PROSE_ACT_NUDGE",
             "PROSE_PRESENCE_PENALTY", "NOTHINK_RUN", "REASONING_PARSER")
BYFLAG = {"mode": None, "tools": []}      # L4 stub: mode "think" = prose runaway iff thinking is ON; "always" = every turn
LOOP_TEXT = "\n".join(["Let me analyze the function. Let me read the function again and plan the change carefully."] * 3000)


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="levers-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    for k in FLAG_KEYS:
        os.environ.pop(k, None)
    if LEVERS != HERE / "worker_levers.py":          # revert-check mutates a COPY of the lever module
        sys.modules.pop("worker_levers", None)
        sp = importlib.util.spec_from_file_location("worker_levers", LEVERS)
        mod = importlib.util.module_from_spec(sp)
        sys.modules["worker_levers"] = mod
        sp.loader.exec_module(mod)
    sys.path.insert(0, str(HERE))
    spec = importlib.util.spec_from_file_location("ow_levers", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert m._wl is not None, "worker_levers failed to import"
    return m


def chunk(delta, finish=None):
    return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "stub",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        REQS.append(json.loads(self.rfile.read(n) or b"{}"))
        turn = SEQ.pop(0) if SEQ else ("task_complete", {"summary": "done"})
        if BYFLAG["mode"]:
            think_on = ((REQS[-1].get("chat_template_kwargs") or {}).get("enable_thinking")) is True
            if BYFLAG["mode"] == "always" or think_on:
                turn = ("PROSE", {})
            else:
                turn = BYFLAG["tools"].pop(0) if BYFLAG["tools"] else ("task_complete", {"summary": "done"})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        w = self.wfile
        try:
            if turn[0] == "PROSE":
                for j in range(0, len(LOOP_TEXT), 400):
                    w.write(chunk({"content": LOOP_TEXT[j:j + 400]}).encode())
                    w.flush()
                w.write(chunk({}, finish="length").encode())
                w.write(b"data: [DONE]\n\n")
                w.flush()
                return
            w.write(chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function", "function": {
                "name": turn[0], "arguments": json.dumps(turn[1])}}]}).encode())
            w.write(chunk({}, finish="tool_calls").encode())
            w.write(b"data: [DONE]\n\n")
            w.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


TARGET = "".join(f"# filler line {i}\n" for i in range(1, 301)) + "FIXED = False\n"
VERIFY_SH = r'''#!/usr/bin/env bash
cd "$(dirname "$0")" || exit 1
if grep -q '^FIXED = True' target.py; then echo "PASS case_fixed"; exit 0; fi
echo "FAIL case_fixed: target is not fixed"
echo "    AssertionError: expected FIXED = True, got FIXED = False"
echo "PASS case_parses"
exit 1
'''
REFIMPL = "import sys\nprint('reference solution')\n"
SEAL = "seal dispatch harness (clean launch baseline)"
# L2 deep guard fixture: a reference solution that WRITES the fix (like the real refimpls: an OLD/NEW block pair)
DEEP_SECRET = "".join(f"SECRET_SOLUTION_LINE_{i} = compute_reference_answer({i}, mode='sealed')\n" for i in range(8))
DEEP_REF = ("#!/usr/bin/env python3\n\"\"\"Reference impl for: deep-guard fixture.\"\"\"\nimport sys, pathlib\nNEW = '''\n" + DEEP_SECRET
            + "FIXED = True\n'''\npathlib.Path(sys.argv[1] if len(sys.argv) > 1 else '.', 'target.py').write_text(NEW)\n"
            + "print('APPLIED_REFERENCE')\n")


def deep_leak(out, wt):
    """Did refimpl.py content or its execution reach the model? (secret text in a tool result, or target rewritten by it)"""
    return ("SECRET_SOLUTION_LINE_3" in out or "APPLIED_REFERENCE" in out or "Reference impl for: deep" in out
            or "SECRET_SOLUTION" in (wt / "target.py").read_text())


def make_repo(sealed=True, with_refimpl=True, refimpl=None, target=None):
    wt = Path(tempfile.mkdtemp(prefix="levers-wt-")).resolve()
    g = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=wt,
                                  check=True, capture_output=True,
                                  env={**os.environ, "GIT_AUTHOR_DATE": "2026-01-01T00:00:00Z",
                                       "GIT_COMMITTER_DATE": "2026-01-01T00:00:00Z"})   # deterministic hashes
    g("init", "-q", ".")
    (wt / "target.py").write_text(target or TARGET)
    (wt / "verify.sh").write_text(VERIFY_SH)
    (wt / "test_fixture.py").write_text("# fixture\n")
    (wt / "TASK.md").write_text("# TASK: x\n")
    if with_refimpl:
        (wt / "refimpl.py").write_text(refimpl or REFIMPL)
    g("add", "-A")
    g("commit", "-qm", SEAL if sealed else "initial")
    return wt


def setflags(_nothink_default=False, **kw):
    """Reset the flag env. NOTHINK_RUN became DEFAULT ON (2026-10-15), so the legacy baseline every older
    scenario was written against is NOTHINK_RUN=0 unless a scenario sets it or passes _nothink_default=True
    (= leave it unset, i.e. the live default)."""
    for k in FLAG_KEYS + ("PH_DEEP_GUARD",):
        os.environ.pop(k, None)
    if not _nothink_default:
        os.environ["NOTHINK_RUN"] = "0"
    for k, v in kw.items():
        os.environ[k] = v


def run(m, host, seq, wt, task="Fix target.py so verify.sh passes.", verify="bash verify.sh", max_iters=12,
        model="stub-model", temperature=0.0, role="author"):
    SEQ[:] = list(seq)
    REQS.clear()
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda h, mo, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    lf = Path(os.environ["HOME"]) / f"live-{time.time_ns()}.log"
    try:
        m.run_task(model, host, str(wt), task, verify, max_iters, temperature, 65536, None,
                   api_style="openai", live_log=str(lf), dispatch_tag="t", max_tokens=32768, task_kind="coding",
                   role=role)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    return "\n".join(logs), json.loads(json.dumps(REQS))


def norm(reqs, wt):
    return json.loads(json.dumps(reqs).replace(str(wt), "<WT>"))


def tool_results(reqs):
    """All tool-role message contents in the LAST request, in order."""
    return [x.get("content") or "" for x in reqs[-1]["messages"] if x.get("role") == "tool"]


def E(old, new):
    return ("edit_file", {"path": "target.py", "old_string": old, "new_string": new})


SCRIPT_EDITS = [E("# filler line 5\n", "# filler line 5 (touched)\n"), E("# filler line 6\n", "# filler line 6 (touched)\n"),
                E("FIXED = False", "FIXED = True"), E("# filler line 7\n", "# filler line 7 (touched)\n"),
                ("task_complete", {"summary": "done"})]
SCRIPT_MISC = [("read_file", {"path": "target.py", "offset": 10, "length": 10}),
               ("list_files", {"path": "."}),
               ("read_file", {"path": "refimpl.py"}),
               ("run_bash", {"command": "ls"}),
               E("# filler line 8\n", "# filler line 8 (touched)\n"),
               ("write_file", {"path": "verify.sh", "content": "exit 0\n"}),
               ("run_bash", {"command": "git stash"}),
               ("task_complete", {"summary": "done"})]


def main():
    m = _load()
    wl = m._wl
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"

    # ================= pure unit: L3 =================
    setflags()
    check("L3 off: window untouched", [wl.min_window_length(x) for x in (None, 0, 10, 119, 120, 300)],
          [None, 0, 10, 119, 120, 300])
    setflags(READ_MIN_WINDOW="1")
    check("L3 on: 0<n<120 -> 120; None/0/>=120 untouched", [wl.min_window_length(x) for x in (None, 0, 1, 10, 119, 120, 300)],
          [None, 0, 120, 120, 120, 120, 300])
    setflags(READ_MIN_WINDOW="0")
    check("L3 '0' = off", wl.min_window_length(10), 10)

    # ================= pure unit: PROSE_ACT_NUDGE (default OFF) =================
    setflags()
    check("ACT_NUDGE off: no suffix even with no edit", wl.act_nudge_suffix(True), "")
    setflags(PROSE_ACT_NUDGE="0")
    check("ACT_NUDGE '0' = off", wl.act_nudge_suffix(True), "")
    setflags(PROSE_ACT_NUDGE="1")
    sfx = wl.act_nudge_suffix(True)
    check("ACT_NUDGE on + no edit yet: plain stop-analysing text", ("Stop analysing the tests." in sfx
          and "first edit now" in sfx and "bash verify.sh" in sfx), True)
    check("ACT_NUDGE on but an edit already happened: no suffix", wl.act_nudge_suffix(False), "")
    setflags()
    env = {}
    wl.apply_task_flags("Worker-flags: PROSE_ACT_NUDGE=1", env)
    check("PROSE_ACT_NUDGE is an allow-listed task flag", env, {"PROSE_ACT_NUDGE": "1"})
    src = WORKER.read_text()
    check("worker nudge site: suffix gated on the flag + no-edit snapshot, base nudge text unchanged",
          ('_wl.flag("PROSE_ACT_NUDGE")' in src and "_wl.act_nudge_suffix(" in src
           and "prose_runaway_nudge(\n                    _PROSE_BUDGET_TOKENS, _pr_reason, task_kind) + _pan" in src), True)

    # ================= pure unit: task-text passthrough =================
    env = {}
    got = wl.apply_task_flags("x\nWorker-flags: VERIFY_ON_EDIT=1 PROTECT_HARNESS=1 READ_MIN_WINDOW=1 PROSE_STICKY_NOTHINK=1\ny", env)
    check("Worker-flags line sets exactly the 4 allow-listed names", (sorted(env), got.__len__()),
          (sorted(k for k in FLAG_KEYS if k not in ("READONLY_JOB", "PROSE_ACT_NUDGE", "PROSE_PRESENCE_PENALTY", "NOTHINK_RUN", "REASONING_PARSER")), 4))
    env = {}
    wl.apply_task_flags("Worker-flags: READONLY_JOB=1", env)
    check("READONLY_JOB is an allow-listed task flag", env, {"READONLY_JOB": "1"})
    env = {"READ_MIN_WINDOW": "0"}
    wl.apply_task_flags("Worker-flags: READ_MIN_WINDOW=1 EVIL=1 PATH=/x VERIFY_ON_EDIT=2", env)
    check("pre-set env wins; unknown names / non-0/1 values ignored", env, {"READ_MIN_WINDOW": "0"})
    env = {}
    wl.apply_task_flags("no marker here", env)
    check("no marker = nothing set", env, {})

    # ================= pure unit: L4 =================
    logs4 = []
    setflags()
    wl.arm_run(Path(tempfile.mkdtemp()), "x", "coding", None)
    check("L4 off: abort never engages, sticky False", (wl.l4_note_abort("author", True, log=logs4.append), wl.l4_sticky(), logs4), (False, False, []))
    setflags(PROSE_STICKY_NOTHINK="1")
    check("L4 on but not aborted yet: sticky False", wl.l4_sticky(), False)
    check("L4 on: non-author role never engages", (wl.l4_note_abort("reviewer", True, log=logs4.append), wl.l4_sticky()), (False, False))
    check("L4 on: model without a thinking switch never engages", (wl.l4_note_abort("author", None, log=logs4.append), wl.l4_sticky()), (False, False))
    check("L4 on: first author abort engages + logs exactly once",
          (wl.l4_note_abort("author", True, log=logs4.append), wl.l4_note_abort("author", True, log=logs4.append), wl.l4_sticky(), logs4),
          (True, True, True, ["[worker] L4: prose runaway seen - thinking stays OFF for the rest of this run"]))
    setflags()
    check("L4: flag switched off again -> sticky False even though flipped", wl.l4_sticky(), False)
    setflags(PROSE_STICKY_NOTHINK="1")
    wl.arm_run(Path(tempfile.mkdtemp()), "x", "coding", None)
    check("L4: arm_run resets the per-run state", wl.l4_sticky(), False)
    setflags()

    # ================= pure unit: L1 summary + schedule =================
    out = ("ok: setup\nnot ok 3 - handles cancelled rows\n  ---\n  error: 'expected 1 got 0'\n  ...\n"
           "FAIL case_b: counters missing\n    AssertionError: expected cancelledMarked\nPASS case_c\n")
    s = wl.summarize_failures(out)
    check("summary names failing cases + first assertion line",
          ("not ok 3 - handles cancelled rows" in s, "expected 1 got 0" in s, "FAIL case_b" in s, "PASS case_c" in s),
          (True, True, True, False))
    big = "\n".join(f"FAIL case_{i}: " + "x" * 400 for i in range(200))
    check("summary capped at 1500 chars", len(wl.summarize_failures(big)) <= 1500, True)
    check("unrecognized output falls back to a tail", "output tail" in wl.summarize_failures("weird\nstuff\n"), True)

    calls = []

    def runner(ok, secs=0.0):
        def r(v, c):
            calls.append(v)
            return ok, "FAIL case_x: bad\n  AssertionError: expected 1" if not ok else "all good"
        return r

    class Clock:
        def __init__(self): self.t = 0.0
        def __call__(self): return self.t

    wt = make_repo()
    OK = "OK: replaced 1 occurrence in target.py (10 bytes total)"
    setflags()
    wl.arm_run(wt, "t", "coding", "bash verify.sh")
    calls.clear()
    check("L1 off: after_edit never verifies and returns ''",
          ([wl.after_edit(wt, "bash verify.sh", "target.py", OK, runner(False)) for _ in range(6)], calls), (([""] * 6), []))
    setflags(VERIFY_ON_EDIT="1")
    wl.arm_run(wt, "t", "coding", "bash verify.sh")
    calls.clear()
    res = [wl.after_edit(wt, "bash verify.sh", "target.py", OK, runner(False)) for _ in range(4)]
    check("L1 on: verify runs on edits 2 and 4 only", (len(calls), [bool(x) for x in res]), (2, [False, True, False, True]))
    check("L1 fail note carries the cases", "FAIL case_x" in res[1] and "run 1/6" in res[1], True)
    calls.clear()
    wl.arm_run(wt, "t", "coding", "bash verify.sh")
    wl.after_edit(wt, "bash verify.sh", "target.py", OK, runner(True))
    p = wl.after_edit(wt, "bash verify.sh", "target.py", OK, runner(True))
    check("L1 PASS says so and says task_complete", ("now PASSES" in p and "task_complete" in p), True)
    wl.arm_run(wt, "t", "coding", "bash verify.sh")
    calls.clear()
    for _ in range(30):
        wl.after_edit(wt, "bash verify.sh", "target.py", OK, runner(False))
    check("L1 bounded: never more than 6 verify runs", len(calls), 6)
    wl.arm_run(wt, "t", "coding", "bash verify.sh")
    calls.clear()
    clk = Clock()

    def slow(v, c):
        calls.append(v)
        clk.t += 200
        return False, "FAIL a"
    for _ in range(10):
        wl.after_edit(wt, "bash verify.sh", "target.py", OK, slow, clock=clk)
    check("L1 skips once a verify took > 120s (1 run only)", len(calls), 1)
    wl.arm_run(wt, "t", "coding", "bash verify.sh")
    calls.clear()
    wl.note_verify_secs(0.0)
    r1 = [wl.after_edit(wt, "bash verify.sh", "verify.sh", OK, runner(False)) for _ in range(4)]
    r2 = [wl.after_edit(wt, "bash verify.sh", "target.py", "ERROR: old_string not found", runner(False)) for _ in range(4)]
    check("L1 ignores harness-file edits and failed edits", (calls, r1 + r2), ([], [""] * 8))
    setflags()

    # ================= tool level (module functions, no run_task) =================
    wt = make_repo()
    setflags()
    m._wl.arm_run(wt, "fix it", "coding", "bash verify.sh")
    base = {
        "list": m.tool_list_files(wt, {"path": "."}),
        "read_ref": m.tool_read_file(wt, {"path": "refimpl.py"}),
        "read10": m.tool_read_file(wt, {"path": "target.py", "offset": 10, "length": 10}),
        "readfull": m.tool_read_file(wt, {"path": "target.py"}),
        "bash_ref": m.tool_run_bash(wt, {"command": "python3 refimpl.py ."}),
        "bash_ls": m.tool_run_bash(wt, {"command": "ls"}),
    }
    check("OFF: refimpl visible + readable + runnable (baseline behaviour)",
          ("refimpl.py" in base["list"], "reference solution" in base["read_ref"], "reference solution" in base["bash_ref"],
           "refimpl.py" in base["bash_ls"]), (True, True, True, True))
    check("OFF: read_file length 10 is exactly 10 lines, no lever note",
          (base["read10"].count("# filler line"), "READ_MIN_WINDOW" in base["read10"]), (10, False))
    for off_val in ("0", ""):                         # explicit off spellings == unset, byte for byte
        setflags(VERIFY_ON_EDIT=off_val, PROTECT_HARNESS=off_val, READ_MIN_WINDOW=off_val)
        m._wl.arm_run(wt, "fix it", "coding", "bash verify.sh")
        again = {
            "list": m.tool_list_files(wt, {"path": "."}),
            "read_ref": m.tool_read_file(wt, {"path": "refimpl.py"}),
            "read10": m.tool_read_file(wt, {"path": "target.py", "offset": 10, "length": 10}),
            "readfull": m.tool_read_file(wt, {"path": "target.py"}),
            "bash_ref": m.tool_run_bash(wt, {"command": "python3 refimpl.py ."}),
            "bash_ls": m.tool_run_bash(wt, {"command": "ls"}),
        }
        check(f"OFF ('{off_val}'): all six tool results identical to unset", again, base)
    subprocess.run(["git", "checkout", "--", "."], cwd=wt)

    # L3 on
    setflags(READ_MIN_WINDOW="1")
    r = m.tool_read_file(wt, {"path": "target.py", "offset": 10, "length": 10})
    check("L3 on: 10-line ask served 120 lines + real range noted",
          (r.count("# filler line"), "lines 10-129 of 301" in r, "READ_MIN_WINDOW" in r, "asked for 10" in r), (120, True, True, True))
    r = m.tool_read_file(wt, {"path": "target.py", "offset": 250, "length": 5})
    check("L3 on: near EOF served to EOF", ("lines 250-301 of 301" in r, "READ_MIN_WINDOW" in r), (True, True))
    r = m.tool_read_file(wt, {"path": "target.py", "offset": 10, "length": 150})
    check("L3 on: ask >= 120 untouched", "READ_MIN_WINDOW" in r, False)
    r = m.tool_read_file(wt, {"path": "verify.sh"})
    check("L3 on: small file read in full has no note", "READ_MIN_WINDOW" in r, False)
    setflags()

    # L2 on, armed (sealed + coding)
    setflags(PROTECT_HARNESS="1")
    m._wl.arm_run(wt, "fix it", "coding", "bash verify.sh")
    check("L2 armed for a sealed coding job", m._wl.ph_active(wt), True)
    check("L2: list_files hides refimpl.py but shows the rest",
          ("refimpl.py" in m.tool_list_files(wt, {"path": "."}), "target.py" in m.tool_list_files(wt, {"path": "."})), (False, True))
    check("L2: read_file refimpl.py = file not found", m.tool_read_file(wt, {"path": "refimpl.py"}).startswith("ERROR: file not found"), True)
    check("L2: read_file ./refimpl.py and ../ spellings too",
          all(m.tool_read_file(wt, {"path": p}).startswith("ERROR: file not found") for p in ("./refimpl.py", str(wt / "refimpl.py"))), True)
    check("L2: read_file of the verify/fixture still works (model may read them)",
          ("PASS case_fixed" in m.tool_read_file(wt, {"path": "verify.sh"}), "# fixture" in m.tool_read_file(wt, {"path": "test_fixture.py"})), (True, True))
    for f in ("verify.sh", "test_fixture.py", "TASK.md", "refimpl.py", "check_literals.py", "verify.test.ts"):
        e = m.tool_edit_file(wt, {"path": f, "old_string": "x", "new_string": "y"})
        w = m.tool_write_file(wt, {"path": f, "content": "pwn\n"})
        check(f"L2: edit/write of {f} refused (no file created/changed)",
              (e.startswith(("REFUSED", "ERROR")), w.startswith(("REFUSED", "ERROR"))), (True, True))
    check("L2: harness files untouched on disk", (wt / "verify.sh").read_text() == VERIFY_SH and not (wt / "verify.test.ts").exists()
          and not (wt / "check_literals.py").exists(), True)
    check("L2: the TARGET is still editable",
          m.tool_edit_file(wt, {"path": "target.py", "old_string": "# filler line 3\n", "new_string": "# filler line 3 x\n"}).startswith("OK:"), True)
    bash = lambda c: m.tool_run_bash(wt, {"command": c})
    for c in ("python3 refimpl.py .", "cat refimpl.py", "cd . && python3 ./refimpl.py", "grep -n x refimpl.py"):
        check(f"L2: run_bash `{c}` refused", bash(c).startswith("REFUSED") and "refimpl" in bash(c), True)
    ls = json.loads(bash("ls"))
    check("L2: `ls` output scrubbed of refimpl.py", ("refimpl.py" in ls["stdout"], "target.py" in ls["stdout"]), (False, True))
    os.environ["PH_DEEP_GUARD"] = "0"      # the base L2 name scrub on its own (deep guard killed) still hides it
    ls0 = json.loads(bash("ls"))
    check("L2 (deep guard off): `ls` output scrubbed of refimpl.py", ("refimpl.py" in ls0["stdout"], "target.py" in ls0["stdout"]), (False, True))
    os.environ.pop("PH_DEEP_GUARD")
    fnd = json.loads(bash("find . -name '*.py' -not -path './.git/*'"))
    check("L2: `find` scrubbed", ("refimpl" in fnd["stdout"], "target.py" in fnd["stdout"]), (False, True))
    for c in ("git checkout -- verify.sh", "git restore test_fixture.py", "git stash", "git stash -u", "git checkout .",
              "git checkout -- .", "git restore .", "git reset --hard", "git clean -fd",
              "echo pwn > verify.sh", "sed -i 's/a/b/' verify.sh", "rm verify.sh", "tee test_fixture.py < /dev/null",
              "cd . && git checkout -- check_literals.py"):
        check(f"L2: `{c}` refused", bash(c).startswith("REFUSED"), True)
    ok = json.loads(bash("bash verify.sh 2>&1 | tail -3"))
    check("L2: running the verify (with 2>&1) is NOT blocked", (ok["exit_code"] in (0, 1), "case_fixed" in ok["stdout"]), (True, True))
    check("L2: harmless git (diff/status/log/stash list) allowed",
          all(not bash(c).startswith("REFUSED") for c in ("git diff", "git status", "git log -1", "git stash list", "git checkout -b scratch")), True)
    check("L2: single-file revert of the TARGET allowed while verify is NOT passing",
          bash("git checkout -- target.py").startswith("REFUSED"), False)
    m.tool_edit_file(wt, {"path": "target.py", "old_string": "FIXED = False", "new_string": "FIXED = True"})
    pas = json.loads(bash("bash verify.sh"))
    check("verify passes after the fix", pas["exit_code"], 0)
    rv = bash("git checkout -- target.py && python3 test_safety.py")
    check("L2: reverting the target after a PASSING verify is refused (the 76d74 failure)", (rv.startswith("REFUSED"), "PASSED" in rv), (True, True))
    check("...and the passing edit is still on disk", "FIXED = True" in (wt / "target.py").read_text(), True)
    m._wl.after_edit(wt, "bash verify.sh", "target.py", OK, runner(False))
    check("...an edit clears the passing state (revert allowed again)", bash("git checkout -- target.py").startswith("REFUSED"), False)
    dm = m.make_tool_request_diagnostics("bash verify.sh")
    check("L2: request_diagnostics naming refimpl refused", dm(wt, {"requests": [{"kind": "file", "path": "refimpl.py"}]}).startswith("ERROR: refimpl.py"), True)
    setflags()

    # ================= L2 deep guard (2026-10-14): refimpl.py CONTENT is unreachable, not only its name =================
    def deep_repo():
        w = make_repo(refimpl=DEEP_REF, target="# small target\nFIXED = False\n")   # small: no output clipping hides a leak
        g = lambda c: subprocess.run(c, shell=True, cwd=w, check=True, capture_output=True,
                                     env={**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
                                          "GIT_COMMITTER_EMAIL": "t@t"})
        g("git checkout -q -b future && echo 'FUTURE_FIX = 1' >> target.py && git commit -qam future && git checkout -q -")
        return w

    def deep_reset(w):
        subprocess.run("git checkout -q -- . && git clean -qfd", shell=True, cwd=w)
    wd = deep_repo()
    setflags(PROTECT_HARNESS="1")
    m._wl.arm_run(wd, "fix it", "coding", "bash verify.sh")
    check("L2 deep: armed with the refimpl fingerprint", (m._wl.ph_deep_active(wd), len(m._wl._PH_DEEP["keys"] or []) > 8), (True, True))
    dbash = lambda c: m.tool_run_bash(wd, {"command": c})
    DEEP_PATHS = ("git show HEAD", "git log -p", "git log -p -1 --stat", "git diff $(git hash-object -t tree /dev/null) HEAD", "cat r*", "cat *.py", "head -n 50 *",
                  "grep -rh SECRET .", "grep -rh SECRET_SOLUTION_LINE_3 .", "grep -r SECRET_SOLUTION_LINE_3 .", "git archive HEAD | tar -xO", "B=$(git ls-tree HEAD | awk '/ref/{print $3}'); git cat-file -p $B",
                  "python3 r*.py .", "for f in *.py; do python3 $f .; done", "cp r*.py /tmp/_lv_probe.py && python3 /tmp/_lv_probe.py .")
    for c in DEEP_PATHS:
        os.environ["PH_DEEP_GUARD"] = "0"
        off = deep_leak(dbash(c), wd)
        deep_reset(wd)
        os.environ.pop("PH_DEEP_GUARD")
        out = dbash(c)
        check(f"L2 deep: `{c}` reaches refimpl.py with the deep guard OFF, not with it ON", (off, deep_leak(out, wd)), (True, False))
        deep_reset(wd)
    check("L2 deep: diff / multi-file sections are replaced by a withheld note",
          ("withheld" in dbash("git show HEAD"), "withheld" in dbash("head -n 50 *"), "withheld" in dbash("cat r*")), (True, True, True))
    subprocess.run("cp refimpl.py helper_copy.py", shell=True, cwd=wd)
    check("L2 deep: read_file of an untracked in-tree copy = not found",
          m.tool_read_file(wd, {"path": "helper_copy.py"}).startswith("ERROR: file not found"), True)
    check("L2 deep: running an untracked copy refused", dbash("python3 helper_copy.py .").startswith("REFUSED"), True)
    deep_reset(wd)
    dmd = m.make_tool_request_diagnostics("bash verify.sh")
    for nm, rq in (("git show", {"kind": "git", "args": ["show", "HEAD"]}), ("git log -p", {"kind": "git", "args": ["log", "-p"]}),
                   ("grep, no glob", {"kind": "grep", "pattern": "SECRET_SOLUTION_LINE_3"}),
                   ("grep, glob r*.py", {"kind": "grep", "pattern": "SECRET", "glob": "r*.py"})):
        os.environ["PH_DEEP_GUARD"] = "0"
        off = deep_leak(m.make_tool_request_diagnostics("bash verify.sh")(wd, {"requests": [rq]}), wd)
        os.environ.pop("PH_DEEP_GUARD")
        check(f"L2 deep: request_diagnostics {nm}: leaks OFF, refused ON", (off, deep_leak(dmd(wd, {"requests": [rq]}), wd)), (True, False))
    for c in ("git log --all -p", "git show future:target.py", "git diff future", "git checkout future -- target.py", "git reflog"):
        r = dbash(c)
        check(f"L2 deep: other branches out of scope: `{c}` refused", (r.startswith("REFUSED"), "FUTURE_FIX" in r + (wd / "target.py").read_text()),
              (True, False))
        deep_reset(wd)
    (wd / "target.py").write_text(DEEP_SECRET + "FIXED = True\n")
    check("L2 deep: the model's OWN edit (identical to refimpl) stays visible in cat / git diff / read_file",
          all("SECRET_SOLUTION_LINE_3" in x and "withheld" not in x for x in
              (dbash("cat target.py"), dbash("git diff"), m.tool_read_file(wd, {"path": "target.py"}))), True)
    deep_reset(wd)
    check("L2 deep: ordinary commands still run (git log/diff/show --stat, grep -rn, ls, python3 -c, new branch)",
          [c for c in ("git log --oneline -3", "git diff HEAD -- target.py", "git show HEAD --stat", "grep -rn FIXED .", "ls -la",
                       "python3 -c 'print(1)'", "git checkout -b scratch2", "git log -1 HEAD~1") if dbash(c).startswith("REFUSED")], [])
    check("L2 deep: `git show HEAD --stat` / `git status` lines naming refimpl.py scrubbed",
          "refimpl" in dbash("git show HEAD --stat") + dbash("git ls-tree HEAD"), False)
    os.environ["PH_DEEP_GUARD"] = "0"
    check("L2 deep: PH_DEEP_GUARD=0 kill switch restores the pre-deep L2 exactly (history leaks again, name gate still on)",
          (deep_leak(dbash("git show HEAD"), wd), deep_leak(dbash("cat r*"), wd), dbash("cat refimpl.py").startswith("REFUSED"),
           m._wl.ph_deep_scrub(wd, "x\n" + DEEP_SECRET), m._wl.ph_deep_bash_verdict(wd, "python3 r*.py .")),
          (True, True, True, "x\n" + DEEP_SECRET, None))
    os.environ.pop("PH_DEEP_GUARD", None)
    deep_reset(wd)
    setflags()
    check("L2 deep: inert with PROTECT_HARNESS unset (scrub/verdicts are identity / None)",
          (m._wl.ph_deep_scrub(wd, DEEP_SECRET), m._wl.ph_deep_bash_verdict(wd, "python3 r*.py ."),
           m._wl.ph_deep_diag_verdict(wd, [{"kind": "git", "args": ["show"]}]), m._wl._deep_read_verdict(wd, "refimpl.py")),
          (DEEP_SECRET, None, None, None))
    m._wl.arm_run(wd, "fix it", "coding", "bash verify.sh")
    check("L2 deep: re-arm with the flag unset clears the fingerprint", m._wl._PH_DEEP["keys"], None)

    # L2 NOT armed: unsealed repo, authoring task, self-check verify, no verify
    for label, kw in (("unsealed repo", dict(sealed=False)),):
        w2 = make_repo(**kw)
        setflags(PROTECT_HARNESS="1")
        m._wl.arm_run(w2, "fix it", "coding", "bash verify.sh")
        check(f"L2 inert: {label}", (m._wl.ph_active(w2), "refimpl.py" in m.tool_list_files(w2, {"path": "."}),
                                      "reference solution" in m.tool_read_file(w2, {"path": "refimpl.py"})), (False, True, True))
    w3 = make_repo()
    setflags(PROTECT_HARNESS="1")
    for label, task, kind, ver in (("authoring task", "# AUTHORING TASK: x", "coding", "bash verify.sh"),
                                   ("refine task", "# REFINE TASK: x", "coding", "bash verify.sh"),
                                   ("harness self-check verify", "fix", "coding", "python3 auto-harness-check.py ."),
                                   ("research kind", "fix", "research", "bash verify.sh"),
                                   ("no verify", "fix", "coding", None)):
        m._wl.arm_run(w3, task, kind, ver)
        check(f"L2 inert: {label}", (m._wl.ph_active(w3), "reference solution" in m.tool_read_file(w3, {"path": "refimpl.py"}),
                                     m.tool_edit_file(w3, {"path": "verify.sh", "old_string": "cd", "new_string": "cd"}).startswith("REFUSED")),
              (False, True, False))
    setflags()
    m._wl.arm_run(w3, "x", "coding", "bash verify.sh")
    check("flag unset after a re-arm: not active", m._wl.ph_active(w3), False)

    # ================= e2e through the REAL run_task =================
    def e2e(script, wtsrc=None, task="Fix target.py so verify.sh passes.", **flags):
        setflags(**flags)
        w = wtsrc or make_repo()
        logs, reqs = run(m, host, script, w, task=task)
        LAST_ENV[:] = sorted(k for k in FLAG_KEYS if k in os.environ and not (k == "NOTHINK_RUN" and os.environ[k] == "0"))
        setflags()
        return logs, reqs, w

    # byte-identical: the SAME scripted loop with flags unset vs "0" -- every request body the model sees
    for nm, sc in (("edit loop", SCRIPT_EDITS), ("misc loop", SCRIPT_MISC)):
        l0, r0, w0 = e2e(sc)
        l1, r1, w1 = e2e(sc, VERIFY_ON_EDIT="0", PROTECT_HARNESS="0", READ_MIN_WINDOW="0", PROSE_STICKY_NOTHINK="0")
        check(f"e2e OFF ({nm}): request bodies identical flags-unset vs '0'", norm(r0, w0), norm(r1, w1))
        check(f"e2e OFF ({nm}): no lever text anywhere in what the model saw",
              any(t in json.dumps(r0) for t in ("VERIFY ON EDIT", "READ_MIN_WINDOW", "PROTECT_HARNESS", "sealed verification")), False)
        check(f"e2e OFF ({nm}): no lever log lines", any(t in l0 for t in ("VERIFY_ON_EDIT", "PROTECT_HARNESS", "READ_MIN_WINDOW", "PROSE_STICKY", "L4")), False)
    l0, r0, w0 = e2e(SCRIPT_MISC)
    tr = tool_results(r0)
    check("e2e OFF baseline really does expose refimpl + allow harness edit + stash",
          ("refimpl.py" in tr[1], "reference solution" in tr[2], tr[5].startswith("OK:") and "Saved working" in tr[6]), (True, True, True))

    # L1 e2e
    l1, r1, w1 = e2e(SCRIPT_EDITS, VERIFY_ON_EDIT="1")
    tr = tool_results(r1)
    check("e2e L1: edit 1 untouched, edit 2 carries a FAIL summary, edit 4 carries PASS",
          ("VERIFY ON EDIT" in tr[0], "still FAILS" in tr[1] and "case_fixed" in tr[1] and "AssertionError" in tr[1],
           "VERIFY ON EDIT" in tr[2], "now PASSES" in tr[3] and "task_complete" in tr[3]), (False, True, False, True))
    check("e2e L1: converged", "VERIFY PASSED" in l1 and "DID NOT CONVERGE" not in l1, True)
    check("e2e L1: only the edit results differ from the OFF run",
          [re.sub(r"\n\n\[VERIFY ON EDIT.*", "", x, flags=re.S) for x in tr],
          [x for x in tool_results(e2e(SCRIPT_EDITS)[1])])

    # L3 e2e + L2 e2e (misc loop with everything on)
    l2, r2, w2 = e2e(SCRIPT_MISC, VERIFY_ON_EDIT="1", PROTECT_HARNESS="1", READ_MIN_WINDOW="1")
    tr = tool_results(r2)
    check("e2e L3: the 10-line read came back as 120 lines with the note", (tr[0].count("# filler line"), "READ_MIN_WINDOW" in tr[0]), (120, True))
    check("e2e L2: list hides refimpl", "refimpl.py" in tr[1], False)
    check("e2e L2: read refimpl = not found", tr[2].startswith("ERROR: file not found"), True)
    check("e2e L2: ls scrubbed", "refimpl" in tr[3], False)
    check("e2e L2: verify.sh write refused and file intact", (tr[5].startswith("REFUSED"), (w2 / "verify.sh").read_text() == VERIFY_SH), (True, True))
    check("e2e L2: git stash refused", tr[6].startswith("REFUSED"), True)
    check("e2e L2: armed log line", "PROTECT_HARNESS: sealed coding harness detected" in l2, True)

    # task-text passthrough end to end: no env, Worker-flags line in the task
    l3, r3, w3_ = e2e(SCRIPT_MISC, task="Fix target.py.\n\nWorker-flags: PROTECT_HARNESS=1 READ_MIN_WINDOW=1\n")
    tr = tool_results(r3)
    check("e2e task-line passthrough activates the levers", (tr[0].count("# filler line"), tr[2].startswith("ERROR: file not found")), (120, True))
    check("...and only the two named flags were set in env", LAST_ENV, ["PROTECT_HARNESS", "READ_MIN_WINDOW"])
    setflags()

    # the 76d747232905 replay: model applies refimpl, verifies, then reverts its own passing state
    replay = [("run_bash", {"command": "python3 refimpl.py ."}),
              ("edit_file", {"path": "target.py", "old_string": "FIXED = False", "new_string": "FIXED = True"}),
              ("run_bash", {"command": "bash verify.sh"}),
              ("run_bash", {"command": "git checkout -- target.py && echo reverted"}),
              ("task_complete", {"summary": "done"})]
    l4, r4, w4 = e2e(replay)
    check("replay OFF: the passing state IS reverted by the model (the incident reproduces)",
          ("FIXED = False" in (w4 / "target.py").read_text(), "DID NOT CONVERGE" in l4 or "FIXED" in (w4 / "target.py").read_text()), (True, True))
    l5, r5, w5 = e2e(replay, PROTECT_HARNESS="1")
    tr = tool_results(r5)
    check("replay PROTECT_HARNESS: refimpl run refused, revert refused, passing edit kept, run converges",
          (tr[0].startswith("REFUSED"), tr[3].startswith("REFUSED"), "FIXED = True" in (w5 / "target.py").read_text(),
           "DID NOT CONVERGE" in l5), (True, True, True, False))

    # L2 deep e2e through the REAL run_task: a model that hunts for the reference through history / globs / copies
    deep_script = [("run_bash", {"command": "git log -p -3"}), ("run_bash", {"command": "cat *.py"}),
                   ("run_bash", {"command": "python3 r*.py ."}), ("run_bash", {"command": "cp r*.py h.py; cat h.py"}),
                   ("request_diagnostics", {"requests": [{"kind": "git", "args": ["show", "HEAD"]}]}),
                   ("run_bash", {"command": "git show future:target.py"}),
                   E("FIXED = False", "FIXED = True"), ("run_bash", {"command": "bash verify.sh"}), ("task_complete", {"summary": "done"})]
    l6, r6, w6 = e2e(deep_script, wtsrc=deep_repo(), PROTECT_HARNESS="1")
    seen = json.dumps([x.get("content") for x in r6[-1]["messages"] if x.get("role") == "tool"])
    check("e2e L2 deep: nothing the model saw carries refimpl content / its output; target fixed by the model's own edit; converged",
          ("SECRET_SOLUTION" in seen, "APPLIED_REFERENCE" in seen, "FUTURE_FIX" in seen, (w6 / "target.py").read_text().count("SECRET"),
           "deep guard armed" in l6, "DID NOT CONVERGE" in l6), (False, False, False, 0, True, False))
    l7, r7, w7 = e2e(deep_script, wtsrc=deep_repo(), PROTECT_HARNESS="1", PH_DEEP_GUARD="0")
    seen7 = json.dumps([x.get("content") for x in r7[-1]["messages"] if x.get("role") == "tool"])
    check("e2e L2 deep OFF (PH_DEEP_GUARD=0): the same script DOES reach the reference (the leak this guard closes)",
          ("SECRET_SOLUTION" in seen7, "deep guard armed" in l7), (True, False))

    # ================= L4 e2e: thinking-ON turns loop in prose, thinking-OFF (recovery) turns call tools =================
    QW = "qwen3.6-35b-a3b-vl-mtp-mxfp8"

    def l4run(mode="think", task="Fix target.py so verify.sh passes.", **flags):
        setflags(**flags)
        BYFLAG["mode"] = mode
        BYFLAG["tools"][:] = [E("# filler line 5\n", "# filler line 5 (touched)\n"), E("# filler line 6\n", "# filler line 6 (touched)\n"),
                              E("FIXED = False", "FIXED = True"), ("task_complete", {"summary": "done"})]
        w = make_repo()
        try:
            lg, rq = run(m, host, [], w, task=task, model=QW, temperature=None, max_iters=14)
        finally:
            BYFLAG["mode"] = None
            os.environ.pop("PROSE_STICKY_NOTHINK", None)
            setflags()
        return lg, rq, w

    def thinking(rq):
        return [(r.get("chat_template_kwargs") or {}).get("enable_thinking") for r in rq]

    def nomsg(r):
        return {k: v for k, v in r.items() if k != "messages"}

    la, ra, wa = l4run()                                   # flag unset
    lz, rz, wz = l4run(PROSE_STICKY_NOTHINK="0")
    check("L4 e2e OFF: flag unset == '0' request bodies", norm(ra, wa), norm(rz, wz))
    check("L4 e2e OFF: today's alternation (ON,off,ON,off,...) then the 'looping in prose' early abort",
          (thinking(ra)[:7], "looping in prose" in la, "L4" in la), ([True, False, True, False, True, False, True], True, False))
    lb, rb, wb = l4run(PROSE_STICKY_NOTHINK="1")
    tb = thinking(rb)
    check("L4 e2e ON: turn 1 thinking ON, every later author turn thinking OFF", (tb[0], set(tb[1:])), (True, {False}))
    check("L4 e2e ON: inert before the first abort (request 1 identical to OFF)", norm(rb[:1], wb), norm(ra[:1], wa))
    check("L4 e2e ON: the first recovery request is byte-identical to OFF's recovery request",
          json.dumps(norm(rb[1:2], wb), sort_keys=True), json.dumps(norm(ra[1:2], wa), sort_keys=True))
    check("L4 e2e ON: recovery sampling differs from turn 1 (repetition penalty 1.2 vs 1.0)",
          (rb[1]["temperature"], rb[1]["repetition_penalty"], rb[0]["temperature"], rb[0]["repetition_penalty"]), (0.6, 1.2, 0.6, 1.0))
    check("L4 e2e ON: every post-flip request equals the recovery body except the messages",
          [nomsg(r) for r in rb[2:]], [nomsg(rb[1])] * len(rb[2:]))
    check("L4 e2e ON: one flip line, run converges, the early-abort counter is NOT tripped by the good turns",
          (lb.count("L4: prose runaway seen - thinking stays OFF for the rest of this run"), "VERIFY PASSED" in lb,
           "looping in prose" in lb, "EARLY ABORT" in lb, lb.count("[worker] cut-off turn ")), (1, True, False, False, 1))
    lc, rc, wc = l4run(mode="always", PROSE_STICKY_NOTHINK="1")
    check("L4 e2e ON, model still loops with thinking off: the early-abort guard still fires",
          ("EARLY ABORT" in lc, set(thinking(rc)[1:])), (True, {False}))
    lt, rt, wt_ = l4run(task="Fix it.\n\nWorker-flags: PROSE_STICKY_NOTHINK=1\n")
    check("L4 e2e: task-text Worker-flags line engages it", (set(thinking(rt)[1:]), "L4: prose runaway seen" in lt), ({False}, True))


    # ================= A/B levers (2026-10-12): PROSE_PRESENCE_PENALTY / NOTHINK_RUN =================
    check("AB unit: presence override off by default / author only",
          (wl.presence_penalty_override("author", {}), wl.presence_penalty_override("author", {"PROSE_PRESENCE_PENALTY": "1"}),
           wl.presence_penalty_override("reviewer", {"PROSE_PRESENCE_PENALTY": "1"})), (None, 1.0, None))
    check("AB unit: nothink_run DEFAULT ON (2026-10-15) / author only / explicit 0,false,off restore old",
          (wl.nothink_run("author", {}), wl.nothink_run("author", {"NOTHINK_RUN": "1"}), wl.nothink_run("reviewer", {}), wl.nothink_run("reviewer", {"NOTHINK_RUN": "1"}),
           wl.nothink_run("gate", {}), wl.nothink_run("judge", {}),
           [wl.nothink_run("author", {"NOTHINK_RUN": v}) for v in ("0", "false", "OFF", " 0 ")]),
          (True, True, False, False, False, False, [False] * 4))
    setflags(_nothink_default=True, PROSE_STICKY_NOTHINK="1")
    wl._L4["sticky"] = False
    _spam = []
    check("AB unit: B + C(default on) combined: B never engages (redundant), no sticky, no log",
          (wl.l4_note_abort("author", True, log=_spam.append), wl.l4_sticky(), _spam), (False, False, []))
    wl._L4["sticky"] = False
    setflags(PROSE_STICKY_NOTHINK="1")      # C explicitly off: B still engages exactly as before
    check("AB unit: B with NOTHINK_RUN=0 still engages", (wl.l4_note_abort("author", True), wl.l4_sticky()), (True, True))
    setflags()
    wl._L4["sticky"] = False
    check("AB unit: both are Worker-flags allow-listed",
          wl.apply_task_flags("Worker-flags: PROSE_PRESENCE_PENALTY=1 NOTHINK_RUN=1", {}), ["PROSE_PRESENCE_PENALTY=1", "NOTHINK_RUN=1"])
    lab0, rab0, wab0 = l4run(mode=None)
    check("AB e2e OFF: thinking ON every turn, profile presence_penalty",
          (set(thinking(rab0)), {r.get("presence_penalty") for r in rab0} == {0.0}), ({True}, True))
    lab1, rab1, wab1 = l4run(mode=None, PROSE_PRESENCE_PENALTY="1")
    check("AB e2e presence: every request presence_penalty=1.0, thinking untouched, rest identical",
          ({r.get("presence_penalty") for r in rab1}, set(thinking(rab1)),
           norm([dict(r, presence_penalty=0.0) for r in rab1], wab1) == norm(rab0, wab0)), ({1.0}, {True}, True))
    lab2, rab2, wab2 = l4run(mode=None, NOTHINK_RUN="1")
    ldef, rdef, wdef = l4run(mode=None, _nothink_default=True)
    check("AB e2e DEFAULT (NOTHINK_RUN unset): thinking OFF on every turn incl. turn 1 == explicit NOTHINK_RUN=1, request bodies identical",
          (set(thinking(rdef)), norm(rdef, wdef) == norm(rab2, wab2)), ({False}, True))
    lbc, rbc, wbc = l4run(mode="think", _nothink_default=True, PROSE_STICKY_NOTHINK="1")
    lc0, rc0, wc0 = l4run(mode="think", _nothink_default=True)
    check("AB e2e default C + B=1: identical requests to C alone, no L4 log (harmless combination)",
          (norm(rbc, wbc) == norm(rc0, wc0), "L4" in lbc), (True, False))
    lof, rof, wof = l4run(mode=None, NOTHINK_RUN="0")
    check("AB e2e NOTHINK_RUN=0 == legacy: thinking ON every turn, same bodies as the pre-flip baseline", (set(thinking(rof)), norm(rof, wof)), ({True}, norm(rab0, wab0)))
    lof2, rof2, wof2 = l4run(mode=None, task="Fix it.\n\nWorker-flags: NOTHINK_RUN=0\n", _nothink_default=True)
    check("AB e2e per-job `Worker-flags: NOTHINK_RUN=0` turns the default off", set(thinking(rof2)), {True})
    check("AB e2e nothink: thinking OFF on every turn incl. turn 1, presence untouched",
          (set(thinking(rab2)), {r.get("presence_penalty") for r in rab2}), ({False}, {0.0}))
    lab3, rab3, wab3 = l4run(mode=None, task="Fix it.\n\nWorker-flags: NOTHINK_RUN=1 PROSE_PRESENCE_PENALTY=1\n", _nothink_default=True)
    check("AB e2e: task-text Worker-flags engages both", (set(thinking(rab3)), {r.get("presence_penalty") for r in rab3}), ({False}, {1.0}))

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    # (name, file, old, new)
    ("ACT_NUDGE: on even when flag off", "worker_levers.py", 'if (no_edit_yet and flag("PROSE_ACT_NUDGE", environ))', 'if (no_edit_yet)'),
    ("ACT_NUDGE: ignores edit-already-made", "worker_levers.py", 'if (no_edit_yet and flag("PROSE_ACT_NUDGE", environ))', 'if (flag("PROSE_ACT_NUDGE", environ))'),
    ("L1: verify never run", "worker_levers.py", "        ok, out = runner(verify, cwd)\n", "        ok, out = False, 'x'\n        return ''\n"),
    ("L1: runs every edit", "worker_levers.py", "        if _VOE[\"edits\"] % VOE_EVERY:\n            return \"\"\n", ""),
    ("L1: no run cap", "worker_levers.py", "        if _VOE[\"runs\"] >= VOE_MAX_RUNS:\n            return \"\"\n", ""),
    ("L1: no slow-verify skip", "worker_levers.py", "        if max(_VOE[\"last_secs\"], _LAST_VERIFY_SECS[0]) > VOE_SKIP_IF_SLOWER_S:\n            return \"\"\n", ""),
    ("L1: on even when flag off", "worker_levers.py", "        if not flag(\"VERIFY_ON_EDIT\") or task_kind != \"coding\" or not verify:\n            return \"\"\n", "        if not verify:\n            return \"\"\n"),
    ("L1: PASS gives no finish instruction", "worker_levers.py", "call task_complete now.]", "carry on.]"),
    ("L1: harness-file edit counts", "worker_levers.py", "        if rel is None or is_harness_rel(rel):\n            return \"\"\n", ""),
    ("L1: worker hook not called", "ollama-worker.py", "                result += _wl.after_edit(cwd, verify,", "                _ = (cwd, verify,"),
    ("L2: refimpl listed", "worker_levers.py", "        return ph_active(cwd) and _rel(cwd, child) in HIDDEN_FILES", "        return False"),
    ("L2: refimpl readable", "worker_levers.py", "        if rel in HIDDEN_FILES:\n            return f\"ERROR: file not found", "        if False:\n            return f\"ERROR: file not found"),
    ("L2: harness writable", "worker_levers.py", "        if op == \"write\" and is_harness_rel(rel):", "        if False:"),
    ("L2: refimpl runnable from bash", "worker_levers.py", "        if re.search(r\"refimpl\", cmd, re.I):", "        if False:"),
    ("L2: git revert unchecked", "worker_levers.py", "            v = _git_revert_verdict(sub, args, _PH[\"passing\"])", "            v = None"),
    ("L2: passing state ignored", "worker_levers.py", "    if passing:\n        return (f\"REFUSED: `git {sub} {' '.join(paths)}`", "    if False:\n        return (f\"REFUSED: `git {sub} {' '.join(paths)}`"),
    ("L2: shell write to harness allowed", "worker_levers.py", "            if _HARNESS_WORD.search(seg) and (_REDIR_HARNESS.search(seg) or _MUTATORS.search(seg)):", "            if False:"),
    ("L2: ls not scrubbed", "worker_levers.py", "        if not text or not ph_active(cwd) or \"refimpl\" not in text:\n            return text", "        return text"),
    ("L2: armed for any job (no sealed check)", "worker_levers.py", "            if sealed_coding_harness(cwd, task, task_kind, verify):", "            if True:"),
    ("L2: authoring task not excluded", "worker_levers.py", "        if head.startswith((\"# AUTHORING TASK\", \"# REFINE TASK\", \"# CONTINUE --\")):\n            return False\n", ""),
    ("L2: bash gate not wired", "ollama-worker.py", "        _ph = _wl.ph_bash_verdict(cwd, args[\"command\"])", "        _ph = None  # "),
    ("L2: edit gate not wired", "ollama-worker.py", "    if _wl is not None:\n        _ph = _wl.ph_file_verdict(cwd, args[\"path\"], \"write\")     # PROTECT_HARNESS (no-op unless armed)\n        if _ph:\n            return _ph\n    p = resolve_path(cwd, args[\"path\"])\n    if not p.exists():\n        return f\"ERROR: file not found: {args['path']}\"",
     "    p = resolve_path(cwd, args[\"path\"])\n    if not p.exists():\n        return f\"ERROR: file not found: {args['path']}\""),
    ("L3: window not widened", "worker_levers.py", "    if flag(\"READ_MIN_WINDOW\") and ln is not None and 0 < ln < MIN_WINDOW_LINES:\n        return MIN_WINDOW_LINES", "    if False:\n        return MIN_WINDOW_LINES"),
    ("L3: widened when flag off", "worker_levers.py", "    if flag(\"READ_MIN_WINDOW\") and ln is not None and 0 < ln < MIN_WINDOW_LINES:", "    if ln is not None and 0 < ln < MIN_WINDOW_LINES:"),
    ("L3: no range note", "ollama-worker.py", "    if _mw_note:\n        footer += \"\\n\" + _mw_note\n", ""),
    ("L4: sticky never engages", "worker_levers.py", "        _L4[\"sticky\"] = True\n        if log:", "        if log:"),
    ("L4: engages with the flag off", "worker_levers.py", "        if not flag(\"PROSE_STICKY_NOTHINK\") or role != \"author\" or enable_thinking is None:\n            return False", "        if role != \"author\" or enable_thinking is None:\n            return False"),
    ("L4: engages for non-author roles", "worker_levers.py", "or role != \"author\" or enable_thinking is None:", "or enable_thinking is None:"),
    ("L4: sticky ignores the flag", "worker_levers.py", "    return bool(_L4[\"sticky\"] and flag(\"PROSE_STICKY_NOTHINK\"))", "    return bool(_L4[\"sticky\"])"),
    ("L4: sticky from turn 1 (not after the first abort)", "worker_levers.py", "    return bool(_L4[\"sticky\"] and flag(\"PROSE_STICKY_NOTHINK\"))", "    return flag(\"PROSE_STICKY_NOTHINK\")"),
    ("L4: state not reset per run", "worker_levers.py", "        _L4[\"sticky\"] = False\n        if flag(\"PROTECT_HARNESS\"):", "        if flag(\"PROTECT_HARNESS\"):"),
    ("L4: not in the Worker-flags allow-list", "worker_levers.py", "\"READ_MIN_WINDOW\", \"PROSE_STICKY_NOTHINK\", ", "\"READ_MIN_WINDOW\", "),
    ("L4: worker never reports the abort", "ollama-worker.py", "                _wl.l4_note_abort(role, _prof.get(\"enable_thinking\"), log=log)", "                pass"),
    ("L4: later turns think again", "ollama-worker.py", "        if _l4_sticky:\n            _turn_think = False\n", ""),
    ("L4: later turns lose the recovery sampling", "ollama-worker.py", "        if _cap_cut_recover or _l4_sticky:", "        if _cap_cut_recover:"),
    ("task line: any env name accepted", "worker_levers.py", "            if k not in FLAGS or v not in (\"0\", \"1\") or k in environ:", "            if v not in (\"0\", \"1\"):"),
    ("AB: presence override ignores the flag", "worker_levers.py", 'return PRESENCE_PENALTY_VALUE if (flag("PROSE_PRESENCE_PENALTY", environ) and', 'return PRESENCE_PENALTY_VALUE if (True and'),
    ("AB: nothink ignores the off switch", "worker_levers.py", 'return flag_default_on("NOTHINK_RUN", environ) and (role', 'return True and (role'),
    ("AB: nothink default flipped back off", "worker_levers.py", 'return flag_default_on("NOTHINK_RUN", environ) and (role', 'return flag("NOTHINK_RUN", environ) and (role'),
    ("AB: nothink off switch drops 'false'", "worker_levers.py", '("0", "false", "off")', '("0",)'),
    ("AB: nothink applies to every role", "worker_levers.py", 'flag_default_on("NOTHINK_RUN", environ) and (role or "author") == "author"', 'flag_default_on("NOTHINK_RUN", environ)'),
    ("AB: B engages on top of C", "worker_levers.py", "        if nothink_run(role):      # thinking is already off", "        if False:      # thinking is already off"),
    ("AB: worker drops the presence override", "ollama-worker.py", '"presence_penalty": (_wl.presence_penalty_override(role) if _wl is not None else None)}', '"presence_penalty": None}'),
    ("AB: worker drops nothink", "ollama-worker.py", '    if _wl is not None and _wl.nothink_run(role):', '    if False:'),
    ("AB: not in Worker-flags allow-list", "worker_levers.py", '"PROSE_PRESENCE_PENALTY", "NOTHINK_RUN", "REASONING_PARSER")', '"PROSE_PRESENCE_PENALTY", "REASONING_PARSER")'),
    ("task line not parsed by worker", "ollama-worker.py", "        _wl.apply_task_flags(task, log=log)", "        pass"),
    ("run not armed by worker", "ollama-worker.py", "        _wl.arm_run(cwd, task, task_kind, verify, log=log)", "        pass"),
    # L2 deep guard (2026-10-14)
    ("L2 deep: refimpl diff section kept", "worker_levers.py", '            if _DIFF_HDR.match(s) and "refimpl.py" in s:', "            if False:"),
    ("L2 deep: content spans not withheld", "worker_levers.py", '        spans = _ref_spans(cwd, [x.rstrip("\\r\\n") for x in out])', "        spans = []"),
    ("L2 deep: model's own edit withheld (no legit check)", "worker_levers.py", " and not _legit_span(cwd, span_keys, cache):", ":"),
    ("L2 deep: glob that runs/copies refimpl allowed", "worker_levers.py", "                if execy and _GLOB_CH.search(t) and any(", "                if False and any("),
    ("L2 deep: running an untracked copy allowed", "worker_levers.py", "not in tracked and _contains_ref(cwd, p.read_text(", "not in tracked and False and _contains_ref(cwd, p.read_text("),
    ("L2 deep: read_file of a copy allowed", "worker_levers.py", '        if p.is_file() and p.stat().st_size <= PH_SCAN_MAX_BYTES and _contains_ref(cwd, p.read_text(errors="ignore")):', "        if False:"),
    ("L2 deep: --all / --branches allowed", "worker_levers.py", "        if a in _FOREIGN_OPTS or a.startswith(", "        if False and a.startswith("),
    ("L2 deep: non-ancestor revs allowed", "worker_levers.py", "            if anc is False:", "            if False:"),
    ("L2 deep: reflog allowed", "worker_levers.py", '    if sub in ("reflog", "clone",', '    if sub in ("clone",'),
    ("L2 deep: grep -h allowed", "worker_levers.py", '            if _GREP_CMD.search(" " + seg) and any(', "            if False and any("),
    ("L2 deep: diagnostics not gated", "worker_levers.py", "    return ph_deep_diag_verdict(cwd, requests)", "    return None"),
    ("L2 deep: kill switch ignored", "worker_levers.py", '    return ph_active(cwd) and os.environ.get("PH_DEEP_GUARD") != "0"', "    return ph_active(cwd)"),
    ("L2 deep: fingerprint never taken", "worker_levers.py", "                _deep_arm(cwd)\n", ""),
    ("L2 deep: fingerprint not reset per run", "worker_levers.py", "        _PH_DEEP.update(keys=None, index=None, blob=None, tracked=None, order=None)\n        _L4", "        _L4"),
    ("L2 deep: bash gate not wired", "worker_levers.py", "        _dv = ph_deep_bash_verdict(cwd, cmd)", "        _dv = None"),
    ("L2 deep: scrub not wired", "worker_levers.py", "    text = ph_deep_scrub(cwd, text) ", "    text = text "),
    ("L2 deep: read gate not wired", "worker_levers.py", "            _dv = _deep_read_verdict(cwd, path_arg) ", "            _dv = None "),
]


def revert_check():
    bad = 0
    srcs = {"ollama-worker.py": WORKER.read_text(), "worker_levers.py": LEVERS.read_text()}
    for name, fn, old, new in MUTATIONS:
        assert srcs[fn].count(old) == 1, f"anchor missing: {name} ({srcs[fn].count(old)})"
        env = dict(os.environ)
        tmp = []
        for f, key in (("ollama-worker.py", "WORKER_SRC"), ("worker_levers.py", "LEVERS_SRC")):
            if f == fn:
                with tempfile.NamedTemporaryFile("w", suffix=f"-{f}", delete=False, dir=str(HERE)) as fh:
                    fh.write(srcs[f].replace(old, new))
                env[key] = fh.name
                tmp.append(fh.name)
        r = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True, timeout=900)
        for t in tmp:
            os.unlink(t)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
