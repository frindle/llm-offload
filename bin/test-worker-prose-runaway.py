#!/usr/bin/env python3
"""No-tool-call PROSE RUNAWAY guard + cut-off turn capture (ollama-worker.py, 2026-10-08).

Live: smoke job 6cf6fb3afca4 had FOUR assistant turns of ~110-122K chars (~32K tokens, ~400s
each) with no tool call; prose_loop only fired after ~28 min and the cut-off text was lost to
compaction. Now (a) the streaming path aborts a tool-call-free coding turn at the profile's
robust.prose_budget_tokens or on degenerate repetition, handing the partial turn to the EXISTING
cut-off handling, and (b) every cut-off turn is persisted beside the transcript with stats.

Drives the REAL call_openai_streaming against a local SSE stub and the REAL run_task against it
(HOME sandboxed, no GPU/network). A large legit write_file must NOT be aborted.
--revert-check mutates the worker (WORKER_SRC env) and requires RED."""
import importlib.util, json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
FAILS = []
MODE = ["short"]
SEQ = []          # run_task e2e: list of modes, one per request
REQS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="proserun-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    spec = importlib.util.spec_from_file_location("ow_proserun", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert str(m.LOG_DIR).startswith(os.environ["HOME"]), m.LOG_DIR
    return m


def chunk(delta, finish=None):
    return "data: " + json.dumps({"id": "x", "object": "chat.completion.chunk", "model": "stub",
                                  "choices": [{"index": 0, "delta": delta, "finish_reason": finish}]}) + "\n\n"


def distinct_prose(n_chars):
    out, i = [], 0
    while sum(len(x) + 1 for x in out) < n_chars:
        out.append(f"Step {i}: weigh alternative approach number {i} for the payout module carefully.")
        i += 1
    return "\n".join(out)


LOOP_TEXT = "\n".join(["Let me re-read the INTENT one more time:", "I think this means something else.",
                       "But that doesn't make sense here.", "Unless... a different number applies."] * 3000)
BIG_FILE = "\n".join(f"export const row{i} = {{ id: {i}, label: 'row number {i}', ok: true }};"
                     for i in range(4000))          # ~230K chars, distinct lines


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        n = int(self.headers.get("Content-Length") or 0)
        REQS.append(json.loads(self.rfile.read(n) or b"{}"))
        mode = SEQ.pop(0) if SEQ else MODE[0]
        if self.path.endswith("/api/chat"):
            return self.ollama(mode)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        w = self.wfile
        try:
            if mode == "short":
                w.write(chunk({"content": "all good\n"}, finish="stop").encode())
            elif mode in ("runaway", "loop"):
                text = distinct_prose(300_000) if mode == "runaway" else LOOP_TEXT
                for j in range(0, len(text), 400):
                    w.write(chunk({"content": text[j:j + 400]}).encode())
                    w.flush()
                w.write(chunk({}, finish="length").encode())
            elif mode == "bashcall":     # research replay turn 1: one run_bash call
                w.write(chunk({"tool_calls": [{"index": 0, "id": "b1", "type": "function", "function": {
                    "name": "run_bash", "arguments": json.dumps({"command": "echo status"})}}]}).encode())
                w.write(chunk({}, finish="tool_calls").encode())
            elif mode == "bigwrite":     # large legit write_file as a REAL tool_call delta
                args = json.dumps({"path": "big.ts", "content": BIG_FILE})
                w.write(chunk({"tool_calls": [{"index": 0, "id": "c1", "type": "function",
                                               "function": {"name": "write_file", "arguments": ""}}]}).encode())
                for j in range(0, len(args), 400):
                    w.write(chunk({"tool_calls": [{"index": 0, "function": {"arguments": args[j:j + 400]}}]}).encode())
                    w.flush()
                w.write(chunk({}, finish="tool_calls").encode())
            elif mode == "bigmarkup":    # large write_file emitted as <tool_call> text in content
                text = ("Writing it now.\n<tool_call>\n<function=write_file>\n<parameter=path>\nbig.ts\n</parameter>\n"
                        "<parameter=content>\n" + BIG_FILE + "\n</parameter>\n</function>\n</tool_call>")
                for j in range(0, len(text), 400):
                    w.write(chunk({"content": text[j:j + 400]}).encode())
                    w.flush()
                w.write(chunk({}, finish="stop").encode())
            elif mode == "toolfirst":    # tool call opens, THEN >budget of content streams
                w.write(chunk({"tool_calls": [{"index": 0, "id": "c2", "type": "function",
                                               "function": {"name": "write_file", "arguments": ""}}]}).encode())
                text = distinct_prose(120_000)
                for j in range(0, len(text), 400):
                    w.write(chunk({"content": text[j:j + 400]}).encode())
                    w.flush()
                w.write(chunk({"tool_calls": [{"index": 0, "function": {"arguments": "{}"}}]}, finish="tool_calls").encode())
            elif mode == "complete":
                w.write(chunk({"tool_calls": [{"index": 0, "id": "c9", "type": "function", "function": {
                    "name": "task_complete", "arguments": json.dumps({"summary": "done"})}}]}).encode())
                w.write(chunk({}, finish="tool_calls").encode())
            w.write(b"data: [DONE]\n\n")
            w.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def _nd(msg, done=False):
    return (json.dumps({"model": "stub", "message": dict(role="assistant", **msg), "done": done,
                        **({"done_reason": "stop", "prompt_eval_count": 5, "eval_count": 5} if done else {})})
            + "\n").encode()


def _ollama(self, mode):
    self.send_response(200)
    self.send_header("Content-Type", "application/x-ndjson")
    self.send_header("Connection", "close")
    self.end_headers()
    self.close_connection = True
    try:
        if mode == "runaway":
            text = distinct_prose(300_000)
            for j in range(0, len(text), 400):
                self.wfile.write(_nd({"content": text[j:j + 400]}))
                self.wfile.flush()
        elif mode == "bigwrite":
            self.wfile.write(_nd({"content": "", "tool_calls": [{"function": {
                "name": "write_file", "arguments": {"path": "big.ts", "content": BIG_FILE}}}]}))
        self.wfile.write(_nd({"content": ""}, done=True))
        self.wfile.flush()
    except (BrokenPipeError, ConnectionResetError, OSError):
        pass


H.ollama = _ollama


def run(m, host, seq, max_iters=8, task_kind="coding"):
    SEQ[:] = list(seq)
    REQS.clear()
    wt = Path(tempfile.mkdtemp(prefix="proserun-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda h, mo, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    lf = Path(os.environ["HOME"]) / f"live-{time.time_ns()}.log"
    try:
        m.run_task("stub-model", host, str(wt), "edit a.txt", None, max_iters, 0.0, 65536, None,
                   api_style="openai", live_log=str(lf), dispatch_tag="t", max_tokens=32768,
                   task_kind=task_kind)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    last = {}
    try:
        last = json.loads(Path(m.DISPATCH_METRICS_PATH).read_text().strip().splitlines()[-1])
    except Exception:
        pass
    return "\n".join(logs), last, wt


def main():
    m = _load()
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    live = m.LiveLog(Path(os.environ["HOME"]) / "live.log", "t", "stub", host)
    msgs = [{"role": "user", "content": "do it"}]
    BUDGET_TOK = 12000
    m._PROSE_BUDGET[0] = BUDGET_TOK * 4

    def call():
        return m.call_openai_streaming(host, "stub", msgs, 0.0, 65536, timeout=60, live=live, max_tokens=32768)

    # ---- pure helpers
    check("repeated >=20-char line x25 is degenerate",
          bool(m.degenerate_repetition("\n".join(["same long line of prose here"] * 30))), True)
    check("short structural lines may repeat (code)",
          m.degenerate_repetition("\n".join(f"  field{i}: {i},\n  }}," for i in range(500))), None)
    check("distinct prose is not degenerate", m.degenerate_repetition(distinct_prose(60000)), None)
    check("markup tool call is never budget-aborted",
          m.prose_runaway_check("<tool_call>\n" + "x" * 90000, 48000), None)
    check("budget trips on long tool-call-free text", bool(m.prose_runaway_check(distinct_prose(50000), 48000)), True)
    st = m.turn_text_stats("a line of text 1\nb line of text 2\na line of text 1\nb line of text 2\n")
    check("stats: dup ratio + repeated block", (st["dup_line_ratio"], st["longest_repeated_line_block"]), (0.5, 2))

    # ---- streaming: pure runaway is cut at ~budget, not at the 300K the server would send
    MODE[0] = "runaway"
    t0 = time.time()
    try:
        call()
        got = None
    except m.ChatAbortedForProseRunaway as e:
        got = e
    check("pure runaway aborted mid-stream", got is not None, True)
    if got:
        check("...cut near the budget (48K..52K chars of 300K)", 48000 <= len(got.content) <= 52000, True)
        check("...flagged as streamed", got.streamed, True)

    MODE[0] = "loop"
    try:
        call()
        got = None
    except m.ChatAbortedForProseRunaway as e:
        got = e
    check("repetition loop aborted mid-stream", got is not None, True)
    if got:
        check("...long before the char budget (<10K chars)", len(got.content) < 10000, True)
        check("...reason names the repetition", "repeated" in got.reason, True)

    MODE[0] = "bigwrite"
    r = call()
    tcs = r["message"].get("tool_calls") or []
    check("large legit write_file (real tool_call) NOT aborted", len(tcs), 1)
    check("...arguments arrived intact", len(json.loads(tcs[0]["function"]["arguments"])["content"]) > 200000, True)
    MODE[0] = "bigmarkup"
    r = call()
    check("large write_file as <tool_call> markup NOT aborted", len(r["message"]["content"]) > 200000, True)
    MODE[0] = "toolfirst"
    r = call()
    check("tool call already open + >budget content NOT aborted (tool-call exemption)",
          (len(r["message"].get("tool_calls") or []), len(r["message"]["content"]) > 100000), (1, True))
    MODE[0] = "short"
    check("normal short turn untouched", call()["message"]["content"], "all good\n")
    # native Ollama /api/chat stream branch
    def ocall():
        return m.call_ollama_streaming(host, "stub", msgs, 0.0, 65536, timeout=60, live=live, max_tokens=32768)
    MODE[0] = "runaway"
    try:
        ocall()
        got = None
    except m.ChatAbortedForProseRunaway as e:
        got = e
    check("ollama stream: pure runaway aborted at the budget",
          got is not None and 48000 <= len(got.content) <= 52000, True)
    MODE[0] = "bigwrite"
    r = ocall()
    check("ollama stream: large legit write_file NOT aborted", len(r["message"].get("tool_calls") or []), 1)
    m._PROSE_BUDGET[0] = 0
    MODE[0] = "runaway"
    check("budget 0 = guard off (runaway streams to the end)", len(call()["message"]["content"]) > 290000, True)
    m._PROSE_BUDGET[0] = BUDGET_TOK * 4

    # ---- run_task end to end: capture + nudge + accounting
    logs, met, wt = run(m, host, ["runaway", "complete"])
    check("e2e: runaway turn aborted by the guard", "ABORTED by the prose-runaway guard" in logs, True)
    check("...nudged and the run went on to task_complete (2 requests)", len(REQS), 2)
    nudge = [x for x in REQS[1]["messages"] if x["role"] == "user" and "STOPPED after" in (x.get("content") or "")]
    check("...the retry carried the prose-runaway nudge", len(nudge), 1)
    asst = [x for x in REQS[1]["messages"] if x["role"] == "assistant"]
    check("...the aborted turn was compacted in the retry context",
          bool(asst) and len(asst[0].get("content") or "") <= m.OUTPUT_CAP_CUT_KEEP_CHARS + 200, True)
    caps = sorted(Path(m.LOG_DIR).glob("*.cutoff-1.txt"))
    check("e2e: raw turn persisted beside the transcript", len(caps), 1)
    if caps:
        txt = caps[0].read_text()
        check("...capture holds content past the compaction limit", len(txt) > 40000, True)
        check("...and is labelled with finish_reason/streamed", "finish_reason=length streamed=True" in txt, True)
    ct = (met.get("cutoff_turns") or [{}])[0]
    check("metrics row: cutoff_turns stats present",
          all(k in ct for k in ("chars", "completion_tokens", "dup_line_ratio",
                                "longest_repeated_line_block", "head300", "tail300", "file")), True)
    check("metrics row: prose_runaway_aborts counted", met.get("prose_runaway_aborts"), 1)
    check("log line points at the capture file", "raw turn saved to" in logs, True)

    logs, met, wt = run(m, host, ["loop", "loop", "loop", "loop"])
    check("e2e: consecutive runaways end the run as output_cap_loop after MAX nudges + 1",
          (len(REQS), met.get("early_abort")), (m.OUTPUT_CAP_CUT_MAX + 1, "output_cap_loop"))

    logs, met, wt = run(m, host, ["bigwrite", "complete"])
    check("e2e: a large legit write_file is executed, not aborted",
          ("prose-runaway guard" in logs, (wt / "big.ts").exists()), (False, True))
    logs, met, wt = run(m, host, ["bigmarkup", "complete"])
    check("e2e: a large markup write_file is not aborted", "prose-runaway guard" in logs, False)

    # ---- job f268674ef763 replay: read-only RESEARCH task, one tool call, then an endless stream of
    # DISTINCT lines (no repetition for the detector to see, no tool call). Used to run unbounded
    # because the stream guard was armed for task_kind == "coding" only.
    logs, met, wt = run(m, host, ["bashcall", "runaway", "short"], task_kind="research")
    check("research replay: endless distinct answer aborted by the guard", "ABORTED by the prose-runaway guard" in logs, True)
    check("...cut at the 6000-token answer budget (24000 chars), not streamed to 300K",
          [c.get("chars") for c in (met.get("cutoff_turns") or [])][:1], [24000])
    check("...research nudge (final answer now), not the write_file coding nudge",
          (any("FINAL answer now" in (x.get("content") or "") for x in REQS[-1]["messages"]),
           any("write_file tool call" in (x.get("content") or "") for x in REQS[-1]["messages"])), (True, False))
    check("...run continued after the abort (>= 3 requests)", len(REQS) >= 3, True)
    logs, met, wt = run(m, host, ["bashcall", "runaway", "runaway", "runaway", "runaway"], task_kind="research")
    check("research: repeated runaways end the run with a named reason (output_cap_loop), bounded requests",
          (met.get("early_abort"), len(REQS) <= m.OUTPUT_CAP_CUT_MAX + 2), ("output_cap_loop", True))

    # ---- oversized tool result must say it was clipped (f268674ef763: silent tail-only listing)
    big = "\n".join(f"[failed  ] row{i:04d} reason=x" for i in range(900))
    clipped, om = m.clip_tool_stream(big)
    check("clip: marker names omission + how to narrow",
          ("OUTPUT TRUNCATED" in clipped and "INCOMPLETE" in clipped and "grep" in clipped and om > 0), True)
    check("clip: keeps the tail on a whole line", clipped.rstrip().endswith("row0899 reason=x") and "\nrow" not in clipped.split("]\n", 1)[1][:1], True)
    check("clip: small output untouched", m.clip_tool_stream("a\nb"), ("a\nb", 0))
    wt2 = Path(tempfile.mkdtemp(prefix="clip-"))
    (wt2 / "big.txt").write_text(big)
    out = json.loads(m.tool_run_bash(wt2, {"command": "cat big.txt"}))
    check("run_bash: stdout carries the truncation marker", out["stdout"].startswith("[OUTPUT TRUNCATED"), True)

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("openai stream guard never consulted",
     "                    if _PROSE_BUDGET[0] and not tc_slots and _pc_len - _pc_checked >= PROSE_GUARD_STRIDE:",
     "                    if False:"),
    ("guard armed with budget 0", "    _PROSE_BUDGET[0] = _PROSE_BUDGET_TOKENS * 4\n",
     "    _PROSE_BUDGET[0] = 0\n"),
    ("non-coding kinds unguarded (the f268674ef763 cause)",
     "                            else int(_rb.get(\"answer_budget_tokens\", 6000) or 0))",
     "                            else 0)"),
    ("non-coding abort not handed to cut-off handling",
     "        if (task_kind == \"coding\" or resp.get(\"_prose_runaway\")) and output_cap_cut_prose(",
     "        if task_kind == \"coding\" and output_cap_cut_prose("),
    ("run_bash clip is silent", "            \"stdout\": clip_tool_stream(out)[0],", "            \"stdout\": out[-4000:],"),
    ("clip marker dropped", "    return (f\"[OUTPUT TRUNCATED: the first", "    return (f\"[x: the first"),
    ("research nudge dropped", "    if task_kind != \"coding\":\n        return (f\"Your last response was STOPPED after",
     "    if False:\n        return (f\"Your last response was STOPPED after"),
    ("no exemption for a tool call in progress",
     "                    if _PROSE_BUDGET[0] and not tc_slots and _pc_len", "                    if _PROSE_BUDGET[0] and _pc_len"),
    ("no exemption for markup tool calls",
     '    if "<tool_call>" in text or "<function=" in text:\n        return None\n', ""),
    ("repetition check removed", "    return degenerate_repetition(text)\n", "    return None\n"),
    ("aborted turn not handed to cut-off handling",
     '"done_reason": "length", "streamed": pr_abort.streamed,', '"done_reason": "stop", "streamed": pr_abort.streamed,'),
    ("raw turn not persisted", "            _co = persist_cutoff_turn(", "            _co = (lambda *a, **k: {'chars': 0, 'completion_tokens': 0, 'dup_line_ratio': 0, 'longest_repeated_line_block': 0, 'streamed': 0})("),
    ("prose-runaway nudge not used", "                messages.append({\"role\": \"user\", \"content\": prose_runaway_nudge(",
     "                messages.append({\"role\": \"user\", \"content\": output_cap_cut_nudge("),
    ("ollama stream guard dropped",
     "                if _PROSE_BUDGET[0] and not tool_calls_acc and _pc_len", "                if False and _pc_len"),
]


def revert_check():
    bad = 0
    src = WORKER.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name} ({src.count(old)})"
        with tempfile.NamedTemporaryFile("w", suffix="-worker.py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "WORKER_SRC": f.name},
                           capture_output=True, text=True, timeout=900)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
