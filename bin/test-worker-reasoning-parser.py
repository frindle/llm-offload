#!/usr/bin/env python3
"""REASONING_PARSER=qwen3 lever (worker_levers.py + model_profile.reasoning_parser_for + ollama-worker.py), DEFAULT OFF.

Why: Darkbloom's LOCAL endpoint returns Qwen3.6's thinking inside message.content unless the request carries
`reasoning_parser: "qwen3"`; with it the thinking arrives in reasoning_content. Probed 2026-10-12 (Darkbloom 0.9.19):
  * streaming + parser: a turn cut at max_tokens -> reasoning_content only, content empty.
  * NON-streaming + parser: a turn cut at max_tokens inside an unclosed think block -> content = the thinking,
    reasoning_content EMPTY, no </think> (the residual leak this suite's unclosed-think detection covers).
  * tool calls arrive intact with the parser on/off (no <tool_call> text in content).
  * Darkbloom ignores reasoning_content / thinking keys on assistant history messages (same prompt_tokens).
Asserted here with a stub server and the REAL worker functions (no GPU):
  A  request bodies: field only with the lever on + profile-declared + Darkbloom-lane host + thinking turn; never on the
     Unraid/Ollama native paths, a non-Darkbloom OpenAI host, other models; lever off == byte-identical body.
  B  response handling: reasoning_content never counted as prose; a reasoning-only cut is not promoted into content when the
     parser is on (and still is when it is off); lever-off return value unchanged.
  C  worker_levers.unclosed_think_cutoff table + Worker-flags parsing.
  D  run_task end to end (non-streaming and streaming): an unclosed-think cutoff is logged, never replayed as assistant
     history, and the next turn is the thinking-off recovery; lever off changes nothing.
--revert-check mutates the code and requires the suite RED."""
import importlib.util, json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
LEVERS = Path(os.environ.get("LEVERS_SRC") or HERE / "worker_levers.py")
MP = Path(os.environ.get("MP_SRC") or HERE / "model_profile.py")
PROFILES = Path(os.environ.get("PROFILES_SRC") or HERE / "model_profiles.yaml")
FAILS = []
REQS, SEQ = [], []
Q36 = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
COT = ("Here's a thinking process:\n1. Analyze the request. Let me re-read the task. Wait, the test contradicts the spec. "
       "Let me check again.\n" * 40)


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load():
    import pwd
    os.environ.setdefault("MODEL_CARDS_ROOT", os.path.join(pwd.getpwuid(os.getuid()).pw_dir, "Desktop/GitHub Projects/references"))   # real home, whatever HOME is
    os.environ["HOME"] = tempfile.mkdtemp(prefix="rp-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    os.environ["NOTHINK_RUN"] = "0"   # NOTHINK_RUN is default ON (2026-10-15); this test exercises the thinking-on author path, so pin the legacy baseline
    os.environ.pop("REASONING_PARSER", None)
    os.environ["MODEL_PROFILES_PATH"] = str(PROFILES)
    sys.path.insert(0, str(HERE))
    for name, path in (("worker_levers", LEVERS), ("model_profile", MP)):
        sys.modules.pop(name, None)
        sp = importlib.util.spec_from_file_location(name, path)
        mod = importlib.util.module_from_spec(sp)
        sys.modules[name] = mod
        sp.loader.exec_module(mod)
    spec = importlib.util.spec_from_file_location("ow_rp", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert m._wl is not None
    return m

COT_VAR = "".join(f"Step {i}: weigh option {i * 7 % 13} against constraint {i * 3 % 11}; Wait, the test contradicts the spec here. " for i in range(60))


# ------------------------------------------------------------------ stub Darkbloom
class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        b = b"{}"
        self.send_response(200)
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(n) or b"{}")
        REQS.append(body)
        if self.path == "/api/chat":
            out = json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True, "done_reason": "stop",
                              "prompt_eval_count": 3, "eval_count": 1}).encode() + b"\n"
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        spec = SEQ.pop(0) if SEQ else {"tool": ("task_complete", {"summary": "done"}), "finish": "tool_calls"}
        content, reasoning, finish = spec.get("content", ""), spec.get("reasoning", ""), spec.get("finish", "stop")
        tool = spec.get("tool")
        usage = {"prompt_tokens": 10, "completion_tokens": spec.get("ct", 5), "total_tokens": 15}
        if not body.get("stream"):
            msg = {"role": "assistant", "content": content}
            if reasoning:
                msg["reasoning_content"] = reasoning
            if tool:
                msg["tool_calls"] = [{"id": "c1", "type": "function", "function": {"name": tool[0], "arguments": json.dumps(tool[1])}}]
            out = json.dumps({"choices": [{"message": msg, "finish_reason": finish}], "usage": usage}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def ch(delta, fin=None):
            return ("data: " + json.dumps({"choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}) + "\n\n").encode()
        w = self.wfile
        try:
            for j in range(0, len(reasoning), 200):
                w.write(ch({"reasoning_content": reasoning[j:j + 200]}))
            for j in range(0, len(content), 200):
                w.write(ch({"content": content[j:j + 200]}))
            if tool:
                w.write(ch({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                            "function": {"name": tool[0], "arguments": json.dumps(tool[1])}}]}))
            w.write(ch({}, finish))
            w.write(("data: " + json.dumps({"choices": [], "usage": usage}) + "\n\n").encode())
            w.write(b"data: [DONE]\n\n")
            w.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def make_repo():
    wt = Path(tempfile.mkdtemp(prefix="rp-wt-")).resolve()
    g = lambda *a: subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=wt, check=True, capture_output=True)
    g("init", "-q", ".")
    (wt / "target.py").write_text("FIXED = False\n")
    (wt / "verify.sh").write_text("#!/usr/bin/env bash\ncd \"$(dirname \"$0\")\"; grep -q '^FIXED = True' target.py && exit 0; echo FAIL; exit 1\n")
    g("add", "-A")
    g("commit", "-qm", "initial")
    return wt


def run(m, host, seq, live, task="Fix target.py so verify.sh passes.", max_iters=4):
    SEQ[:] = list(seq)
    REQS.clear()
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda h, mo, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    lf = str(Path(os.environ["HOME"]) / f"live-{time.time_ns()}.log") if live else None
    try:
        m.run_task(Q36, host, str(make_repo()), task, "bash verify.sh", max_iters, 0.0, 65536, None,
                   api_style="openai", live_log=lf, dispatch_tag="t", max_tokens=32768, task_kind="coding", role="author", preserve_reasoning=True)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    return "\n".join(logs), json.loads(json.dumps(REQS))


def main():
    m = _load()
    wl = m._wl
    mp = sys.modules["model_profile"]
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    m._is_darkbloom_lane = lambda h: str(h).rstrip("/") == host        # the stub plays the Darkbloom endpoint
    msgs = [{"role": "user", "content": "x"}]

    def body_of(fn, *a, **k):
        REQS.clear()
        SEQ[:] = [{"content": "ok"}]
        fn(host, Q36, msgs, None, None, *a, tools=False, role="author", **k)
        return REQS[-1]

    # ---------------- A: request bodies ----------------
    os.environ.pop("REASONING_PARSER", None)
    off_ns = body_of(m.call_ollama, api_style="openai")
    off_st = body_of(m.call_openai_streaming)
    check("A: lever unset -> no reasoning_parser (blocking + streaming)", ("reasoning_parser" in off_ns, "reasoning_parser" in off_st), (False, False))
    for v in ("0", "1", "deepseek_r1", "QWEN3", ""):
        os.environ["REASONING_PARSER"] = v
        check(f"A: REASONING_PARSER={v!r} is OFF (body identical)",
              (body_of(m.call_ollama, api_style="openai") == off_ns, body_of(m.call_openai_streaming) == off_st), (True, True))
    os.environ["REASONING_PARSER"] = "qwen3"
    on_ns = body_of(m.call_ollama, api_style="openai")
    on_st = body_of(m.call_openai_streaming)
    check("A: lever on -> reasoning_parser == 'qwen3' on blocking AND streaming", (on_ns.get("reasoning_parser"), on_st.get("reasoning_parser")), ("qwen3", "qwen3"))
    check("A: lever on adds ONLY that field (blocking)", {k: v for k, v in on_ns.items() if k != "reasoning_parser"}, off_ns)
    check("A: lever on adds ONLY that field (streaming)", {k: v for k, v in on_st.items() if k != "reasoning_parser"}, off_st)
    check("A: thinking-off turn carries no field", "reasoning_parser" in body_of(m.call_ollama, api_style="openai", think=False), False)
    check("A: tools=True request still carries it with the tools", (lambda b: ("tools" in b, b.get("reasoning_parser")))(
        (lambda: (REQS.clear(), SEQ.__setitem__(slice(None), [{"content": "ok"}]),
                  m.call_ollama(host, Q36, msgs, None, None, api_style="openai", tools=True, role="author"), REQS[-1])[-1])()), (True, "qwen3"))
    for other in ("Qwen3.5-9B", "gemma-4-26b-qat-4bit", "qwen3.8:27b-q4_K_M", "qwen3:14b", "nonexistent:1b"):
        REQS.clear()
        SEQ[:] = [{"content": "ok"}]
        m.call_ollama(host, other, msgs, None, None, api_style="openai", tools=False, role="author")
        check(f"A: profile without a declaration ({other}) never gets the field", "reasoning_parser" in REQS[-1], False)
    # Unraid / native Ollama
    REQS.clear()
    m.call_ollama(host, Q36, msgs, None, None, api_style="ollama", tools=False, role="author")
    m.call_ollama_streaming(host, Q36, msgs, None, None, tools=False, role="author")
    check("A: native Ollama /api/chat (blocking + streaming) never gets the field", ["reasoning_parser" in r for r in REQS], [False, False])
    real = m.__dict__.get("_is_darkbloom_lane")
    m._is_darkbloom_lane = lambda h: False
    REQS.clear(); SEQ[:] = [{"content": "ok"}, {"content": "ok"}]
    m.call_ollama(host, Q36, msgs, None, None, api_style="openai", tools=False, role="author")
    m.call_openai_streaming(host, Q36, msgs, None, None, tools=False, role="author")
    check("A: a non-Darkbloom OpenAI host (llama-server / Unraid) never gets the field", ["reasoning_parser" in r for r in REQS], [False, False])
    m._is_darkbloom_lane = real
    check("A: build_request_fields api=ollama never emits it",
          "reasoning_parser" in mp.build_request_fields(Q36, "author", "ollama", reasoning_parser="qwen3"), False)
    check("A: a backend the profile does not list never gets it (ollama-backend qwen3.8, no lane)",
          "reasoning_parser" in mp.build_request_fields("qwen3.8:27b-q4_K_M", "author", "openai", reasoning_parser="qwen3"), False)
    check("A: --check passes with the declaration", mp.check_profiles(), [])
    base = mp.load_profiles()
    import copy
    for what, edit in (("entry backend is not darkbloom", lambda e: e.update(backend="ollama")),
                       ("declaration lists no darkbloom backend", lambda e: e["reasoning_parser"].update(backends=["ollama"])),
                       ("declaration names another parser", lambda e: e["reasoning_parser"].update(name="deepseek_r1"))):
        d = copy.deepcopy(base)
        edit(d["models"][Q36])
        check(f"A: reasoning_parser_for -> None when the {what}", mp.reasoning_parser_for(Q36, "author", "qwen3", data=d), None)
    check("A: reasoning_parser_for -> 'qwen3' for the declared darkbloom entry", mp.reasoning_parser_for(Q36, "author", "qwen3"), "qwen3")

    # ---------------- B: response handling ----------------
    CUT_R = "Let me think about the plan. " * 30
    os.environ["REASONING_PARSER"] = "qwen3"
    SEQ[:] = [{"reasoning": CUT_R, "finish": "length", "ct": 32768}]
    r = m.call_openai_streaming(host, Q36, msgs, None, None, tools=False, role="author")
    check("B: parser on, streaming reasoning-only cut -> content empty (not promoted to prose), thinking kept",
          (r["message"]["content"], r["message"].get("thinking"), r["_reasoning_parser"]), ("", CUT_R, True))
    SEQ[:] = [{"reasoning": CUT_R, "finish": "stop", "ct": 50}]
    r = m.call_openai_streaming(host, Q36, msgs, None, None, tools=False, role="author")
    check("B: parser on, reasoning-only turn that STOPPED keeps the narrow old promotion", r["message"]["content"], CUT_R)
    SEQ[:] = [{"reasoning": CUT_R, "content": "The answer is 391.", "finish": "stop"}]
    r = m.call_openai_streaming(host, Q36, msgs, None, None, tools=False, role="author")
    check("B: parser on, reasoning + answer -> content is the clean answer only", r["message"]["content"], "The answer is 391.")
    SEQ[:] = [{"reasoning": CUT_R, "content": "The answer is 391.", "finish": "stop"}]
    r = m.call_ollama(host, Q36, msgs, None, None, api_style="openai", tools=False, role="author")
    check("B: parser on, blocking reasoning + answer -> clean content, thinking kept",
          (r["message"]["content"], r["message"].get("thinking"), r.get("done_reason")), ("The answer is 391.", CUT_R, "stop"))
    SEQ[:] = [{"reasoning": CUT_R, "finish": "length", "ct": 32768}]
    r = m.call_ollama(host, Q36, msgs, None, None, api_style="openai", tools=False, role="author")
    check("B: parser on, blocking reasoning-only cut -> content empty", r["message"]["content"], "")
    os.environ.pop("REASONING_PARSER", None)
    SEQ[:] = [{"reasoning": CUT_R, "finish": "length", "ct": 32768}]
    r = m.call_openai_streaming(host, Q36, msgs, None, None, tools=False, role="author")
    check("B: parser OFF: unchanged old behaviour (reasoning-only cut promoted to content)", r["message"]["content"], CUT_R)
    SEQ[:] = [{"content": "hi"}]
    r = m.call_ollama(host, Q36, msgs, None, None, api_style="openai", tools=False, role="author")
    check("B: parser OFF: blocking return value is exactly {message, usage} (byte-identical shape)", sorted(r), ["message", "usage"])
    SEQ[:] = [{"content": "hi"}]
    r = m.call_openai_streaming(host, Q36, msgs, None, None, tools=False, role="author")
    check("B: parser OFF: streaming return has no lever keys", [k for k in r if k.startswith("_")], [])

    # ---------------- C: pure ----------------
    cut = {"content": COT}
    chk = lambda msg, fin, ct=5, mt=32768, on=True, th=True: wl.unclosed_think_cutoff(msg, fin, {"completion_tokens": ct}, mt, on, th)
    check("C: parser on + thinking + finish=length + reasoning empty + content -> unclosed", chk(dict(cut), "length"), True)
    check("C: ...by completion tokens at the cap with no finish_reason", chk(dict(cut), None, ct=32768), True)
    check("C: parser off -> never", chk(dict(cut), "length", on=False), False)
    check("C: thinking off -> never (short prose cut is the ordinary prose cutoff)", chk(dict(cut), "length", th=False), False)
    check("C: reasoning present -> never", chk({"content": COT, "thinking": "x"}, "length"), False)
    check("C: tool call present -> never", chk({"content": COT, "tool_calls": [{}]}, "length"), False)
    check("C: closed </think> in content -> never (already split by _strip_inline_think)", chk({"content": "a</think>b"}, "length"), False)
    check("C: empty content -> never", chk({"content": "  "}, "length"), False)
    check("C: normal stop with short answer -> never", chk({"content": "Done."}, "stop"), False)
    check("C: huge CoT-looking turn that stopped -> unclosed", chk({"content": COT * 4}, "stop"), True)
    check("C: huge NON-CoT turn that stopped -> not flagged", chk({"content": "x = 1\n" * 4000}, "stop"), False)
    env = {}
    wl.apply_task_flags("Worker-flags: REASONING_PARSER=qwen3", env)
    check("C: Worker-flags REASONING_PARSER=qwen3 is parsed", env, {"REASONING_PARSER": "qwen3"})
    for bad in ("REASONING_PARSER=evil", "REASONING_PARSER=deepseek_r1", "EVIL=qwen3"):
        env = {}
        wl.apply_task_flags("Worker-flags: " + bad, env)
        check(f"C: Worker-flags {bad} ignored", env, {})
    env = {"REASONING_PARSER": "0"}
    wl.apply_task_flags("Worker-flags: REASONING_PARSER=qwen3", env)
    check("C: pre-set env wins over the task line", env, {"REASONING_PARSER": "0"})
    check("C: reasoning_parser_request", (wl.reasoning_parser_request({}), wl.reasoning_parser_request({"REASONING_PARSER": "qwen3"}),
                                          wl.reasoning_parser_request({"REASONING_PARSER": "1"})), (None, "qwen3", None))

    # ---------------- D: run_task end to end ----------------
    TOOL_DONE = {"tool": ("task_complete", {"summary": "done"}), "finish": "tool_calls"}
    for live in (False, True):
        tag = "streaming" if live else "blocking"
        os.environ["REASONING_PARSER"] = "qwen3"
        if live:   # streaming + parser: the thinking IS split, the cut is a reasoning-only turn (probed)
            seq = [{"reasoning": COT_VAR, "finish": "length", "ct": 32768}, TOOL_DONE, TOOL_DONE]
        else:      # blocking + parser: the unclosed thinking lands in content (probed)
            seq = [{"content": COT, "finish": "length", "ct": 32768}, TOOL_DONE, TOOL_DONE]
        logs, reqs = run(m, host, seq, live)
        check(f"D[{tag}] parser on: every request carried reasoning_parser while thinking", reqs[0].get("reasoning_parser"), "qwen3")
        if not live:
            check(f"D[{tag}] parser on: UNCLOSED THINK detected + logged", "UNCLOSED THINK at iteration 1" in logs, True)
        check(f"D[{tag}] parser on: the cut thinking turn is recovered as a think-cap turn", "spent the whole" in logs and "THINKING" in logs, True)
        second = reqs[1] if len(reqs) > 1 else {"messages": []}
        replay = json.dumps([x for x in second.get("messages") or [] if x.get("role") == "assistant"])
        check(f"D[{tag}] parser on: the cut thinking is NOT replayed as assistant history in the next request", "Wait, the test contradicts" in replay, False)
        check(f"D[{tag}] parser on: recovery turn is thinking-OFF and carries no parser field",
              ((second.get("chat_template_kwargs") or {}).get("enable_thinking"), "reasoning_parser" in second), (False, False))
        check(f"D[{tag}] parser on: tool calls still land (task_complete reached)", any("task_complete" in x for x in [logs]), True)
    # a looping reasoning stream is now visible to the reasoning-loop guard (the point of the parser)
    os.environ["REASONING_PARSER"] = "qwen3"
    logs, reqs = run(m, host, [{"reasoning": COT, "finish": "length", "ct": 32768}, TOOL_DONE, TOOL_DONE], True)
    check("D[streaming] parser on: a repetitive reasoning stream is caught by the REASONING LOOP guard", "REASONING LOOP" in logs, True)
    # lever OFF with the same blocking leak: nothing new happens
    os.environ.pop("REASONING_PARSER", None)
    logs, reqs = run(m, host, [{"content": COT, "finish": "length", "ct": 32768}, TOOL_DONE, TOOL_DONE], False)
    check("D: lever OFF: no UNCLOSED THINK handling, no reasoning_parser anywhere",
          ("UNCLOSED THINK" in logs, any("reasoning_parser" in r for r in reqs)), (False, False))
    # per-job passthrough: the task line alone arms the lever end to end
    os.environ.pop("REASONING_PARSER", None)
    logs, reqs = run(m, host, [TOOL_DONE, TOOL_DONE], False, task="Fix target.py.\nWorker-flags: REASONING_PARSER=qwen3\n")
    check("D: Worker-flags task line alone puts reasoning_parser on the wire", reqs[0].get("reasoning_parser") if reqs else None, "qwen3")
    os.environ.pop("REASONING_PARSER", None)

    print(f"\n{'ALL PASS' if not FAILS else 'FAILED: ' + str(len(FAILS))}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("lever ignored by the builder", "model_profile.py", '    if not requested:\n        return None\n', '    requested = "qwen3"\n'),
    ("builder skips the profile declaration check", "model_profile.py", '    if not isinstance(rp, dict) or str(rp.get("name") or "") != str(requested):\n        return None\n', ''),
    ("builder skips the backend check", "model_profile.py", '    if "darkbloom" not in (rp.get("backends") or []) or (entry or {}).get("backend") != "darkbloom":\n        return None\n', ''),
    ("builder never emits the field", "model_profile.py", '            f["reasoning_parser"] = _rp\n', '            pass\n'),
    ("builder emits it on thinking-off turns", "model_profile.py", 'if _rp and thinking is not False:', 'if _rp:'),
    ("builder emits it on the ollama api", "model_profile.py", '        f = {"options": opts}\n', '        f = {"options": opts}\n        f["reasoning_parser"] = "qwen3"\n'),
    ("worker never reads the lever", "ollama-worker.py", '(_wl.reasoning_parser_request() if (_wl is not None and api_style == "openai" and host', '(None if (_wl is not None and api_style == "openai" and host'),
    ("worker sends it to a non-Darkbloom host", "ollama-worker.py", '                                                   and _is_darkbloom_lane(host)) else None)', '                                                   and True) else None)'),
    ("worker blocking path drops host", "ollama-worker.py", 'max_tokens, repeat_penalty, think,\n                         sampling_step=sampling_step, seed=seed, host=host)\n    preserve_reasoning = preserve_reasoning or _mp.role_wants_reasoning_kept(model, role)\n\n    if api_style == "openai":', 'max_tokens, repeat_penalty, think,\n                         sampling_step=sampling_step, seed=seed)\n    preserve_reasoning = preserve_reasoning or _mp.role_wants_reasoning_kept(model, role)\n\n    if api_style == "openai":'),
    ("worker streaming path drops host", "ollama-worker.py", 'max_tokens, repeat_penalty, think,\n                         sampling_step=sampling_step, seed=seed, host=host)\n    preserve_reasoning = preserve_reasoning or _mp.role_wants_reasoning_kept(model, role)\n    payload = {"model": model, "messages": messages, "stream": True}', 'max_tokens, repeat_penalty, think,\n                         sampling_step=sampling_step, seed=seed)\n    preserve_reasoning = preserve_reasoning or _mp.role_wants_reasoning_kept(model, role)\n    payload = {"model": model, "messages": messages, "stream": True}'),
    ("blocking reasoning cut is promoted to prose", "ollama-worker.py", '                                    and not (_rp_on and str(_fin or "").lower() == "length")):', '                                    ):'),
    ("streaming reasoning cut is promoted to prose", "ollama-worker.py", '                and not ("reasoning_parser" in payload and str(done_reason or "").lower() == "length")):', '                ):'),
    ("lever-off blocking return gains keys", "ollama-worker.py", '                    if _rp_on:     # extra keys ONLY', '                    if True:     # extra keys ONLY'),
    ("worker never runs the unclosed-think detection", "ollama-worker.py", '        if (_wl is not None and resp.get("_reasoning_parser")\n                and _wl.unclosed_think_cutoff(', '        if (False and _wl is not None and resp.get("_reasoning_parser")\n                and _wl.unclosed_think_cutoff('),
    ("unclosed thinking stays in content (replayed)", "ollama-worker.py", '            msg["thinking"], msg["content"] = msg.get("content") or "", ""\n', '            pass\n'),
    ("cut thinking replayed via the thinking key", "ollama-worker.py", '        if not preserve_reasoning or _drop_cut_reasoning:\n', '        if not preserve_reasoning:\n'),
    ("detector ignores finish_reason/cap", "worker_levers.py", '    if str(done_reason or "").lower() == "length":\n        return True\n    ct = (usage or {}).get("completion_tokens") or 0\n    if max_tokens and ct >= max_tokens:\n        return True\n', ''),
    ("detector fires with the parser off", "worker_levers.py", '    if not parser_on or not thinking_on:\n        return False\n    if msg.get("tool_calls")', '    if msg.get("tool_calls")'),
    ("detector fires on thinking-off turns", "worker_levers.py", '    if not parser_on or not thinking_on:\n        return False\n', '    if not parser_on:\n        return False\n'),
    ("detector fires despite reasoning present", "worker_levers.py", 'if msg.get("tool_calls") or msg.get("thinking") or msg.get("reasoning") or msg.get("reasoning_content"):', 'if msg.get("tool_calls"):'),
    ("detector eats closed-think content", "worker_levers.py", 'if not c.strip() or "</think>" in c:', 'if not c.strip():'),
    ("Worker-flags cannot set REASONING_PARSER", "worker_levers.py", 'if k == "REASONING_PARSER" and v in REASONING_PARSER_VALUES and k not in environ:', 'if False:'),
    ("Worker-flags overrides a pre-set env", "worker_levers.py", 'if k == "REASONING_PARSER" and v in REASONING_PARSER_VALUES and k not in environ:', 'if k == "REASONING_PARSER" and v in REASONING_PARSER_VALUES:'),
    ("lever accepts any value", "worker_levers.py", '    return v if v in REASONING_PARSER_VALUES else None', '    return v or None'),
    ("profile declaration removed", "model_profiles.yaml", '    reasoning_parser: {name: qwen3,', '    xreasoning_parser: {name: qwen3,'),
]


def revert_check():
    bad = 0
    srcs = {"ollama-worker.py": WORKER.read_text(), "worker_levers.py": LEVERS.read_text(),
            "model_profile.py": MP.read_text(), "model_profiles.yaml": PROFILES.read_text()}
    keys = {"ollama-worker.py": "WORKER_SRC", "worker_levers.py": "LEVERS_SRC", "model_profile.py": "MP_SRC", "model_profiles.yaml": "PROFILES_SRC"}
    for name, fn, old, new in MUTATIONS:
        assert srcs[fn].count(old) == 1, f"anchor missing: {name} ({srcs[fn].count(old)})"
        with tempfile.NamedTemporaryFile("w", suffix=f"-{fn}", delete=False, dir=str(HERE)) as fh:
            fh.write(srcs[fn].replace(old, new))
        env = dict(os.environ)
        env[keys[fn]] = fh.name
        r = subprocess.run([sys.executable, __file__], env=env, capture_output=True, text=True, timeout=900)
        os.unlink(fh.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
