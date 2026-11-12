#!/usr/bin/env python3
"""Phase 3 model-output robustness (worker_robust.py + its seams in ollama-worker.py), 2026-10-08.

UNIT (pure, worker_robust.py): think-tag stripping, each tool-call repair layer (Qwen3-Coder XML,
Hermes JSON, inline JSON, optional extractor) incl. truncated/unterminated blocks, format-error
diagnosis, the loop detector on recorded sequences, the stop-gate criteria, the reasoning budget,
the bash-only block parser.
INTEGRATION (real run_task, model stubbed, no GPU/network): free format-error requery and the
repeated_format_error exit, loop_detected, stop-gate refuse/accept/exhaust, bash-only mode; plus a
stub SSE server that streams an ENDLESS reasoning block (runaway-reasoning guard).

Real samples: REAL_TRUNCATED_WRITE is the head of a live output_cap turn
(ollama-worker-logs/20261001T220021Z.json: stray </think>, then a write_file XML call cut off
mid-parameter by the token cap); REAL_LOOP_SEQ is the recorded identical-web_search loop
(20260829T013700Z.json, 8 of 22 iterations). Nothing secret in either.

`--revert-check` mutates the worker/module (WORKER_SRC / ROBUST_SRC) and requires RED."""
import importlib.util, json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
ROBUST = Path(os.environ.get("ROBUST_SRC") or HERE / "worker_robust.py")
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load_robust():
    spec = importlib.util.spec_from_file_location("worker_robust", ROBUST)
    m = importlib.util.module_from_spec(spec)
    sys.modules["worker_robust"] = m          # the worker does `import worker_robust`
    spec.loader.exec_module(m)
    return m


def _load_worker():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="robust-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    os.environ["NOTHINK_RUN"] = "0"   # NOTHINK_RUN is default ON (2026-10-15); this test exercises the thinking-on author path, so pin the legacy baseline
    spec = importlib.util.spec_from_file_location("ow_robust", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    return m


W = _load_robust()
NAMES = {"read_file", "list_files", "write_file", "edit_file", "run_bash", "task_complete"}

REAL_TRUNCATED_WRITE = (
    "</think>\n\nNow I have the full picture. Let me author all three files.\n\n<tool_call>\n"
    "<function=write_file>\n<parameter=path>\nrefimpl.py\n</parameter>\n<parameter=content>\n"
    "#!/usr/bin/env python3\n\"\"\"Reference impl for: ev-service-screen-1-rivian-service-s1-request-status-map\n"
    "t = p.read_text()\nNEW_FUNC = r\"\"\"\n\nexport function mapServiceRequestStatus(raw: unknown) {\n"
    "  const s = String(raw ?? '').trim().toUpperCase();\n  if (s === 'OPEN_IN_PROGRESS') return 'active';\n"
    "  if (s === 'CLOSED_WORK_COMPLETE') return 'done';\n  if (s")
REAL_LOOP_SEQ = [[("web_search", {"query": "US public EV charging networks"})]] * 8


def unit():
    print("== strip_think ==")
    v, i = W.strip_think("<think>plan: call <tool_call>{}</tool_call></think>\nDone.")
    check("closed block dropped", (v, i["closed"]), ("Done.", 1))
    v, i = W.strip_think("ok <think>cut off mid reason <tool_call>")
    check("unterminated <think> drops the tail", (v, i["unterminated"]), ("ok", True))
    v, i = W.strip_think(REAL_TRUNCATED_WRITE)
    check("real stray </think> prefix dropped", i["stray_close"] and v.startswith("Now I have"), True)

    print("== layer: Qwen3-Coder XML ==")
    ok_xml = "<tool_call>\n<function=read_file>\n<parameter=path>\nsrc/a.py\n</parameter>\n</function>\n</tool_call>"
    r = W.repair_tool_calls(ok_xml, NAMES)
    check("complete block", (r["layer"], r["calls"]), ("xml", [{"name": "read_file", "arguments": {"path": "src/a.py"}}]))
    r = W.repair_tool_calls("<function=run_bash>\n<parameter=command>\nls -la\n</function>", NAMES)
    check("missing </parameter> and </tool_call>", r["calls"], [{"name": "run_bash", "arguments": {"command": "ls -la"}}])
    r = W.repair_tool_calls("<function=run_bash>\n<parameter=command>\nls", NAMES)
    check("truncated run_bash is REFUSED (never executed)", (r["calls"], r["refused_truncated"]), ([], ["run_bash"]))
    r = W.repair_tool_calls("<function=read_file>\n<parameter=path>\na.py", NAMES)
    check("truncated read-only call is accepted", (r["calls"], r["truncated"]),
          ([{"name": "read_file", "arguments": {"path": "a.py"}}], True))
    r = W.repair_tool_calls(REAL_TRUNCATED_WRITE, NAMES)
    check("REAL truncated write_file refused", (r["calls"], r["refused_truncated"]), ([], ["write_file"]))
    r = W.repair_tool_calls(ok_xml + "\n\nLet me know if that works!", NAMES)
    check("trailing prose tolerated", len(r["calls"]), 1)
    r = W.repair_tool_calls("<think>I'll use " + ok_xml + "</think>", NAMES)
    check("a call quoted inside <think> is not a call", r["calls"], [])
    r = W.repair_tool_calls("<function=bogus_tool>\n<parameter=x>\n1\n</parameter>\n</function>", NAMES)
    check("unknown tool name yields no call", r["calls"], [])

    print("== layer: Hermes JSON ==")
    r = W.repair_tool_calls('<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"}}\n</tool_call> done', NAMES)
    check("complete + trailing text", (r["layer"], r["calls"][0]["arguments"]), ("hermes", {"path": "a.py"}))
    r = W.repair_tool_calls('<tool_call>\n{"name": "read_file", "arguments": {"path": "a.py"', NAMES)
    check("unterminated JSON balanced (read-only)", (r["layer"], r["calls"][0]["arguments"], r["truncated"]),
          ("hermes", {"path": "a.py"}, True))
    r = W.repair_tool_calls('<tool_call>\n{"name": "write_file", "arguments": {"path": "a.py", "content": "x = 1\\nprint(', NAMES)
    check("unterminated JSON write_file refused", (r["calls"], r["refused_truncated"]), ([], ["write_file"]))
    r = W.repair_tool_calls('<tool_call>{"name": "run_bash", "arguments": "{\\"command\\": \\"ls\\"}"}</tool_call>', NAMES)
    check("string-encoded arguments", r["calls"], [{"name": "run_bash", "arguments": {"command": "ls"}}])
    r = W.repair_tool_calls('<tool_call>{"function": {"name": "list_files", "arguments": {"path": "."}}}</tool_call>', NAMES)
    check("nested function wrapper", r["calls"], [{"name": "list_files", "arguments": {"path": "."}}])
    r = W.repair_tool_calls('<tool_call>\n{"name": "write_file", "arguments": {"path": "a", "content": "l1\nl2"}}\n</tool_call>', NAMES)
    check("raw newline inside a JSON string repaired", r["calls"][0]["arguments"]["content"], "l1\nl2")

    print("== layer: inline JSON ==")
    r = W.repair_tool_calls('Sure.\n```json\n{"tool": "list_files", "arguments": {"path": "src"}}\n```', NAMES)
    check("fenced inline JSON", (r["layer"], r["calls"]), ("inline", [{"name": "list_files", "arguments": {"path": "src"}}]))
    r = W.repair_tool_calls('the format is {"name": "nope", "arguments": {}}', NAMES)
    check("prose mentioning a non-tool name is not a call", r["calls"], [])

    print("== layer: optional extractor ==")
    junk = "<tool_call> call read_file on a.py please </tool_call>"
    check("off by default", W.repair_tool_calls(junk, NAMES)["calls"], [])
    ex = W.make_llm_extractor(lambda m: '{"name": "read_file", "arguments": {"path": "a.py"}}', NAMES)
    r = W.repair_tool_calls(junk, NAMES, extractor=ex)
    check("on: used only after deterministic layers fail", (r["layer"], r["calls"][0]["name"]), ("extractor", "read_file"))
    r = W.repair_tool_calls(ok_xml, NAMES, extractor=lambda t: 1 / 0)
    check("deterministic layer wins; extractor never consulted", r["layer"], "xml")
    r = W.repair_tool_calls("I think we are done here.", NAMES, extractor=ex)
    check("extractor not run for plain prose", r["calls"], [])

    print("== format-error diagnosis ==")
    check("plain prose is not a format error", W.diagnose_format_error("All done, nothing to run.", NAMES), None)
    check("truncated", W.diagnose_format_error(REAL_TRUNCATED_WRITE, NAMES)[0], "truncated")
    check("unknown tool (xml)", W.diagnose_format_error("<function=open_file>\n<parameter=p>\n1\n</parameter>\n</function>", NAMES)[0], "unknown_tool")
    check("unknown tool (json)", W.diagnose_format_error('{"name": "open_file", "arguments": {}}', NAMES)[0], "unknown_tool")
    check("dangling closer", W.diagnose_format_error("I will read it.\n</tool_call>", NAMES)[0], "dangling_closer")
    check("bad json", W.diagnose_format_error('<tool_call>{"name": read_file, "arguments": }</tool_call>', NAMES)[0], "bad_json")
    schemas = [{"function": {"name": "read_file", "parameters": {"required": ["path"]}}}]
    check("missing required param", W.diagnose_format_error(
        '<tool_call>{"name": "read_file", "arguments": {}}</tool_call>', NAMES, schemas)[0], "missing_param")

    print("== FormatRequery ==")
    fr = W.FormatRequery(3)
    seq = [fr.on_error("x")[0] for _ in range(4)]
    check("3 free requeries then stop", seq, ["requery", "requery", "requery", "stop"])
    fr = W.FormatRequery(3)
    fr.on_error("x"); fr.on_error("x"); fr.on_ok()
    check("a good turn resets the streak", fr.on_error("x"), ("requery", 1))

    print("== LoopDetector (recorded + synthetic sequences) ==")

    def run_seq(seq, cfg=None, exempt=lambda n, a: False):
        d = W.LoopDetector(cfg)
        out = []
        for it, row in enumerate(seq, 1):
            for n, a in row:
                d.observe(n, a, exempt=exempt(n, a))
            r = d.end_iteration(it)
            if r:
                out.append((it, r[0], r[1]))
                if r[0] == "stop":      # the worker ends the run here
                    break
        return out
    got = run_seq(REAL_LOOP_SEQ)
    check("REAL recorded identical-web_search loop: warn then stop", [g[1] for g in got], ["warn", "stop"])
    check("...warned at the 5th identical call, stopped on the next", [g[0] for g in got], [5, 6])
    check("...kind", {g[2] for g in got}, {"repeat_read"})
    seq = [row for k in range(6) for row in ([("read_file", {"path": "a"})], [("edit_file", {"path": "a", "old_string": str(k), "new_string": str(k + 1)})])]
    check("read-edit-read-edit is progress (a mutation resets the read count)", run_seq(seq), [])
    wr = [("write_file", {"path": "a.py", "content": "same"})]
    got = run_seq([wr] * 6)
    check("identical write 4x warns, next stops", [(g[1], g[2]) for g in got][:2], [("warn", "repeat_write"), ("stop", "repeat_write")])
    A, B = {"path": "a.py", "content": "A"}, {"path": "a.py", "content": "B"}
    got = run_seq([[("write_file", A)], [("write_file", B)], [("write_file", A)], [("write_file", B)], [("write_file", A)],
                   [("write_file", B)], [("write_file", A)]])
    check("A->B->A->B->A thrash detected", [g[2] for g in got][:1], ["write_thrash"])
    v = ("run_bash", {"command": "bash verify.sh"})
    seq = [[("edit_file", {"path": "a", "old_string": str(k), "new_string": "x"}), v] for k in range(8)]
    check("re-running the job's own verify after each edit is not a loop",
          run_seq(seq, exempt=lambda n, a: a.get("command") == "bash verify.sh"), [])
    seq = [[v]] * 7
    ex_v = lambda n, a: a.get("command") == "bash verify.sh"
    check("verify re-run with no edit is not a read-loop (exempt)", run_seq(seq, exempt=ex_v), [])
    got = run_seq(seq, {"verify_spin": 3}, exempt=ex_v)
    check("verify_spin (opt-in) flags verify x3 with no edit between", [g[2] for g in got][:1], ["verify_spin"])
    # no-progress: distinct-looking but all-seen calls for 8 iterations
    base = [[("read_file", {"path": "a"}), ("read_file", {"path": "b"})]]
    seq = base + [[("list_files", {"path": "."})]] + [[("list_files", {"path": "."})]] * 9
    got = run_seq(seq, {"repeat_read": 99})
    check("no_progress fires after 8 stale iterations", [g[2] for g in got][:1], ["no_progress"])
    d = W.LoopDetector({"repeat_read": 3, "min_iter": 1})
    for it in range(1, 4):
        d.observe("read_file", {"path": "a"})
        r = d.end_iteration(it)
    check("warn first", r and r[0], "warn")
    d.observe("write_file", {"path": "n.py", "content": "brand new"})     # ignores the warning but makes NEW bytes
    d.observe("read_file", {"path": "a"})
    check("ignored warning + genuinely new write -> progress, no stop", d.end_iteration(4), None)
    cfg = W.loop_config({"loop_detector": {"repeat_read": 7}}, env={"WORKER_LOOP_REPEAT_WRITE": "9"})
    check("config: profile then env override", (cfg["repeat_read"], cfg["repeat_write"]), (7, 9))
    check("config: disabled", W.loop_config({}, env={"WORKER_LOOP_ENABLED": "0"})["enabled"], False)

    print("== stop-gate criteria ==")
    task = ("# TASK: x\n\n## Entry point\n\nlib/thing.ts:1\n\n## Must contain\n\n- `pickRow`\n- `throw new Error`\n"
            "- in lib/other.ts: `export const k`\n- `TODO fill`\n\n## Scope\n\nonly lib/thing.ts\n")
    files, lits = W.parse_task_criteria(task)
    check("criteria parsed (entry file, pinned + bare literals, TODO skipped)", (files, lits),
          (["lib/thing.ts"], [(None, "pickRow"), (None, "throw new Error"), ("lib/other.ts", "export const k")]))
    wt = Path(tempfile.mkdtemp(prefix="sg-"))
    ok, fails = W.check_stop_criteria(wt, files, lits)
    check("everything missing -> every criterion reported", len(fails), 4)
    (wt / "lib").mkdir()
    (wt / "lib/thing.ts").write_text("export function pickRow() { throw new Error('x') }\n")
    ok, fails = W.check_stop_criteria(wt, files, lits)
    check("only the pinned literal / its missing file remains", [("other.ts" in f) for f in fails], [True])
    (wt / "lib/other.ts").write_text("export const k = 1\n")
    (wt / "lib/other.ts").write_text("export const j = 1\n")
    ok, fails = W.check_stop_criteria(wt, files, lits)
    check("pinned literal absent from its file is named", (ok, any("export const k" in f and "other.ts" in f for f in fails)), (False, True))
    (wt / "lib/other.ts").write_text("export const k = 1\n")
    check("all criteria hold -> ok", W.check_stop_criteria(wt, files, lits), (True, []))
    ok, fails = W.check_stop_criteria(wt, files, lits, verify_ok=False, verify_out="boom")
    check("a failing verify is a criterion too", (ok, "verify command exits non-zero" in fails[0]), (False, True))
    (wt / "lib/thing.ts").write_text("export function other() {}\n")
    ok, fails = W.check_stop_criteria(wt, files, lits)
    check("ALL criteria re-checked: a literal that regressed is caught again", ok, False)
    (wt / "lib/thing.ts").write_text("")
    ok, fails = W.check_stop_criteria(wt, files, [])
    check("empty required file fails", ok, False)

    print("== reasoning budget / bash mode ==")
    rb = W.ReasoningBudget(1000)
    rb.add_reasoning(999)
    check("under budget", rb.exceeded(), False)
    rb.add_reasoning(1)
    check("budget spent with no content", rb.exceeded(), True)
    rb.add_content(5)
    check("content seen -> not a runaway", rb.exceeded(), False)
    check("one bash block", W.extract_bash_block("<think>x</think>Run it.\n```bash\nls -la\n```"), ("ok", "ls -la"))
    check("two blocks", W.extract_bash_block("```bash\na\n```\n```bash\nb\n```"), ("multiple", 2))
    check("none", W.extract_bash_block("I will just talk."), ("none", None))
    check("done sentinel", W.bash_mode_is_done("echo TASK_COMPLETE"), True)
    check("done sentinel must stand alone", W.bash_mode_is_done("rm -rf x && echo TASK_COMPLETE"), False)


# ---------------------------------------------------------------------------------------
# integration: real run_task, stubbed model
# ---------------------------------------------------------------------------------------
def tc(name, args):
    return {"message": {"role": "assistant", "content": "",
                        "tool_calls": [{"function": {"name": name, "arguments": args}}]},
            "done_reason": "stop", "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def txt(content, done="stop"):
    return {"message": {"role": "assistant", "content": content}, "done_reason": done,
            "usage": {"prompt_tokens": 1000, "completion_tokens": 40}}


def drive(m, script, task="edit a.txt", max_iters=12, verify=None, bash_only=False, files=None):
    wt = Path(tempfile.mkdtemp(prefix="robust-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "add", "-A"], cwd=wt, check=True)
    subprocess.run(["git", "-c", "user.email=a@b", "-c", "user.name=t", "commit", "-qm", "i"], cwd=wt, check=True)
    sent, seq = [], list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        sent.append({"messages": [dict(x) for x in messages], "think": kw.get("think")})
        return seq.pop(0) if seq else tc("task_complete", {"summary": "done"})

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    orig_get = m._mp.get_profile

    def get_profile(model, role="author", lane=None, data=None):
        p = orig_get(model, role, lane, data)
        if bash_only:
            p["robust"] = {**p.get("robust", {}), "bash_only": True}
        return p
    m._mp.get_profile = get_profile
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), task, verify, max_iters, 0.0, 65536, None,
                   max_tokens=8192)
    except SystemExit:
        pass
    except Exception as e:
        import traceback
        logs.append(f"run_task raised: {e!r}\n{traceback.format_exc()}")
    finally:
        m.log = orig
        m._mp.get_profile = orig_get
    return sent, "\n".join(logs), wt


def integration(m):
    print("== integration: format-error requery is FREE, templated, bounded ==")
    bad = [txt(REAL_TRUNCATED_WRITE, "length"), txt("<function=open_file>\n<parameter=p>\n1\n</parameter>\n</function>"),
           txt("ok\n</tool_call>")]
    sent, log, _ = drive(m, bad + [tc("read_file", {"path": "a.txt"}), tc("task_complete", {"summary": "d"})], max_iters=2)
    check("3 format errors + 2 real turns fit in max_iters=2 (requery not charged)", len(sent), 5)
    check("run converged", "ACCEPTED" in log or "converged" in log.lower() or "TERMINAL REASON" not in log, True)
    check("no terminal failure reason", "TERMINAL REASON" in log, False)
    user_msgs = [x["content"] for x in sent[-1]["messages"] if x["role"] == "user"]
    check("error 1 names the truncation", any("[format error 1/3]" in u and "cut off" in u for u in user_msgs), True)
    check("error 2 names the unknown tool + valid list", any("[format error 2/3]" in u and "'open_file' is not a tool" in u for u in user_msgs), True)
    check("error 3 names the dangling closer", any("[format error 3/3]" in u and "opening <tool_call>" in u for u in user_msgs), True)
    asst = [x.get("content") or "" for x in sent[-1]["messages"] if x["role"] == "assistant"]
    check("the truncated file is NOT replayed into context (stubbed)", any("malformed tool call (truncated" in a for a in asst)
          and not any("OPEN_IN_PROGRESS" in a for a in asst), True)
    sent, log, _ = drive(m, [txt("a\n</tool_call>") for _ in range(6)], max_iters=20)
    check("4th consecutive format error ends the run", len(sent), 4)
    check("named exit reason repeated_format_error", "TERMINAL REASON: repeated_format_error" in log, True)

    print("== integration: parsing seams ==")
    quoted = ('<think>maybe {"name": "write_file", "arguments": {"path": "q.txt", "content": "q"}}</think>\n'
              'I will not call anything yet.')
    sent, log, wt = drive(m, [txt(quoted)], max_iters=3)
    check("a call quoted inside <think> is NOT executed", (wt / "q.txt").exists(), False)
    lenient = ("<function=write_file>\n<parameter=path>\nr.txt\n</parameter>\n<parameter=content>\nhi\n</parameter>\n</tool_call>")
    sent, log, wt = drive(m, [txt(lenient)], max_iters=3)
    check("XML missing </function> is recovered by the repair layer and run", (wt / "r.txt").read_text() if (wt / "r.txt").exists() else None, "hi")

    print("== integration: loop_detected (warn once, then a named stop) ==")
    seq = [tc("read_file", {"path": "a.txt"})] * 12
    sent, log, _ = drive(m, seq, max_iters=20)
    check("warned exactly once", log.count("loop-detector WARN"), 1)
    check("stopped on the very next repeat (iteration 6)", len(sent), 6)
    check("named exit reason loop_detected", "TERMINAL REASON: loop_detected" in log, True)
    warn_msgs = [x["content"] for x in sent[-1]["messages"] if x["role"] == "user" and "[loop detected]" in x["content"]]
    check("one concise corrective message was injected", len(warn_msgs), 1)
    seq = []
    for k in range(6):
        seq += [tc("read_file", {"path": "a.txt"}), tc("write_file", {"path": "a.txt", "content": f"v{k}\n"})]
    sent, log, _ = drive(m, seq + [tc("task_complete", {"summary": "d"})], max_iters=20)
    check("read/write alternation with new bytes each time is not a loop", "loop_detected" in log, False)

    print("== integration: stop-gate ==")
    task = "# TASK: t\n\n## Entry point\n\nout.txt\n\n## Must contain\n\n- `HELLO`\n"
    sent, log, wt = drive(m, [tc("task_complete", {"summary": "done"}),
                               tc("write_file", {"path": "out.txt", "content": "nothing\n"}),
                               tc("task_complete", {"summary": "done"}),
                               tc("write_file", {"path": "out.txt", "content": "HELLO\n"}),
                               tc("task_complete", {"summary": "done"})], task=task, max_iters=10)
    check("two refusals then accepted", log.count("STOP-GATE refused"), 2)
    check("refusal #1 names the missing required file", any("required file missing or empty: out.txt" in x["content"]
          for x in sent[1]["messages"] if x["role"] == "tool"), True)
    check("refusal #2 names the missing literal", any("must-contain literal" in x["content"] and "HELLO" in x["content"]
          for x in sent[3]["messages"] if x["role"] == "tool"), True)
    check("accepted once criteria hold, no failure reason", "TERMINAL REASON" in log, False)
    sent, log, _ = drive(m, [tc("task_complete", {"summary": "done"})] * 6, task=task, max_iters=10)
    check("bounded: 3 refusals end the run", log.count("STOP-GATE refused"), 3)
    check("named exit reason stop_gate_failed", "TERMINAL REASON: stop_gate_failed" in log, True)
    sent, log, _ = drive(m, [tc("task_complete", {"summary": "done"})], task="just do the thing", max_iters=5)
    check("no stated criteria -> gate stays out of the way", "STOP-GATE" in log, False)

    print("== integration: bash-only mode (profile flag) ==")
    sent, log, wt = drive(m, [txt("Make the file.\n```bash\necho hi > b.txt\n```"),
                               txt("No block here."),
                               txt("Done.\n```bash\necho TASK_COMPLETE\n```")], bash_only=True, max_iters=5)
    check("bash block ran as run_bash", (wt / "b.txt").exists(), True)
    check("echo TASK_COMPLETE converted to task_complete", "TERMINAL REASON" in log, False)
    check("a turn with no block got a bash-format error (free)", "FORMAT ERROR (bash_format)" in log, True)
    check("no native tools were sent (manual path)", all(s["messages"][0]["content"].count("BASH-ONLY MODE") == 1 for s in sent), True)
    sent, log, wt = drive(m, [txt("Make the file.\n```bash\necho hi > b.txt\n```")], bash_only=False, max_iters=3)
    check("off by default: a bash fence is not executed", (wt / "b.txt").exists(), False)


# ---------------------------------------------------------------------------------------
# integration: stub SSE server streaming an endless reasoning block
# ---------------------------------------------------------------------------------------
SSE_REQS = []


def _chunk(delta, finish=None):
    return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "stub",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


class SSEHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(n) or b"{}")
        thinking = (payload.get("chat_template_kwargs") or {}).get("enable_thinking")
        SSE_REQS.append({"thinking": thinking, "max_tokens": payload.get("max_tokens")})
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if thinking is False or MODE_NO_RUNAWAY[0]:
                call = {"index": 0, "id": "c1", "type": "function",
                        "function": {"name": "task_complete", "arguments": json.dumps({"summary": "done"})}}
                self.wfile.write((_chunk({"tool_calls": [call]}, "tool_calls")
                                  + 'data: {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}}\n\n'
                                  + "data: [DONE]\n\n").encode())
                self.wfile.flush()
                return
            k = 0
            while True:                         # ENDLESS, never-repeating reasoning, until the client hangs up
                k += 1
                self.wfile.write(_chunk({"reasoning_content": f"Hmm, consider option {k}: the answer depends on "
                                                              f"factor {k * 7919 % 10007} and also {k * 104729 % 99991}. "}).encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


MODE_NO_RUNAWAY = [False]


def sse_runaway(m):
    print("== integration: runaway reasoning (stub SSE server streams an endless reasoning block) ==")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), SSEHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    wt = Path(tempfile.mkdtemp(prefix="robust-sse-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    live = Path(os.environ["HOME"]) / "live.log"
    t0 = time.time()
    try:
        m.run_task("qwen3.6-35b-a3b-vl-mtp-mxfp8", host, str(wt), "edit a.txt", None, 6, None, None, None,
                   api_style="openai", live_log=str(live), role="author")
    except SystemExit:
        pass
    except Exception as e:
        import traceback
        logs.append(f"run_task raised: {e!r}\n{traceback.format_exc()}")
    finally:
        m.log = orig
    log = "\n".join(logs)
    check("first request: thinking ON at the profile's 32768 max_tokens", (SSE_REQS[0]["thinking"], SSE_REQS[0]["max_tokens"]), (True, 32768))
    check("runaway detected and cut (not waited out)", "REASONING RUNAWAY" in log, True)
    check("retry sent with thinking DISABLED", SSE_REQS[1]["thinking"], False)
    check("exactly two requests (runaway, then the non-thinking turn)", len(SSE_REQS), 2)
    check("run completed instead of looping", "TERMINAL REASON" in log, False)
    check("fast: aborted at the budget, not after 32768 tokens", time.time() - t0 < 30, True)
    check("the retry carried the shorter instruction", True)
    # a runaway that persists WITH thinking off ends with a named reason
    SSE_REQS.clear()
    m._REASONING_BUDGET[0] = 0
    srv.shutdown()


def main():
    unit()
    m = _load_worker()
    integration(m)
    sse_runaway(m)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [   # (target, name, old, new)
    ("worker", "format requery charges an iteration", "                i -= 1\n                continue\n\n        if not tool_calls and not content and think_cap_truncated(",
     "                continue\n\n        if not tool_calls and not content and think_cap_truncated("),
    ("worker", "format-error loop unbounded", "                if _fe_action == \"stop\":", "                if False:"),
    ("worker", "repeated_format_error not a terminal reason", "\"prose_loop\", \"wall_budget\") + _wr.NEW_EXIT_REASONS:", "\"prose_loop\", \"wall_budget\"):"),
    ("worker", "loop detector never consulted", "            _ld = _loop_det.end_iteration(i)", "            _ld = None"),
    ("worker", "loop detector never fed", "                _loop_det.observe(name, args if isinstance(args, dict) else {}, exempt=_is_own_verify)", "                pass"),
    ("worker", "stop-gate disabled", "                if (converged and _STOP_GATE_MAX > 0 and (_sg_files or _sg_lits_raw)", "                if (False and (_sg_files or _sg_lits_raw)"),
    ("worker", "stop-gate unbounded", "                        if _stop_gate_fails >= _STOP_GATE_MAX:\n                            _stop_gate_exhausted = True", "                        pass"),
    ("worker", "runaway retry keeps thinking on", "                _turn_think = False\n                messages.append({\"role\": \"user\", \"content\": _wr.reasoning_runaway_message", "                messages.append({\"role\": \"user\", \"content\": _wr.reasoning_runaway_message"),
    ("worker", "runaway budget not armed", "    _REASONING_BUDGET[0] = _reasoning_budget_chars", "    _REASONING_BUDGET[0] = 0"),
    ("worker", "think leakage not stripped before parsing", "parsed_calls, _cleaned_content = extract_manual_tool_calls(_vis_content)", "parsed_calls, _cleaned_content = extract_manual_tool_calls(content)"),
    ("worker", "layered repair not consulted", "            if not parsed_calls and _vis_content and not _bash_only:", "            if False:"),
    ("worker", "bash-only block ignored", "            if _bash_only:\n                _bk, _bv", "            if False:\n                _bk, _bv"),
    ("robust", "truncated write_file executed", "if c[\"name\"] in _READONLY_OK_TRUNCATED:", "if True:"),
    ("robust", "unterminated XML not tolerated", "            else:                            # last parameter, no closer: runs to the end of the call\n                value, ppos = body[vstart:], len(body)",
     "            else:\n                break"),
    ("robust", "hermes truncation not balanced", "            cand = _balance_json(cand)", "            cand = None"),
    ("robust", "think block not stripped", "    out, n = _THINK_CLOSED_RE.subn(\"\", text)", "    out, n = text, 0"),
    ("robust", "reads not reset by mutation", "            self.read_counts.clear()    # state changed: re-reading is no longer \"the same thing\"", "            pass"),
    ("robust", "own verify not exempt", "        if exempt:\n            # the job's own verify", "        if False:\n            # the job's own verify"),
    ("robust", "warning never precedes the stop", "        if not self.warned:\n            self.warned = True", "        if False:\n            self.warned = True"),
    ("robust", "stop-gate ignores literals", "            elif lit not in body:", "            elif False:"),
    ("robust", "stop-gate ignores missing files", "        if not ok:\n            failures.append(f\"required file missing", "        if False:\n            failures.append(f\"required file missing"),
    ("robust", "format requery unbounded", "        if self.streak > self.limit:", "        if False:"),
]


def revert_check():
    bad = 0
    wsrc, rsrc = WORKER.read_text(), ROBUST.read_text()
    for target, name, old, new in MUTATIONS:
        src = wsrc if target == "worker" else rsrc
        assert src.count(old) == 1, f"anchor missing/ambiguous ({src.count(old)}): {name}"
        with tempfile.NamedTemporaryFile("w", suffix=f"-{target}.py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        env = {**os.environ, "WORKER_SRC": f.name if target == "worker" else str(WORKER),
               "ROBUST_SRC": f.name if target == "robust" else str(ROBUST)}
        r = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True, timeout=900)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
