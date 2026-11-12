#!/usr/bin/env python3
"""Reasoning-loop detector false positives + recovery ladder (ollama-worker.py, 2026-10-12).

REASONING_PARSER=qwen3 A/B: every arm-B run ended paused/exit 3 on `reasoning_loop`. Two defects:
 1. think_buf_loop_check fired when the 400-char tail occurred ONCE earlier. A model that drafts
    the same function/command twice in its reasoning before writing it out trips that (measured
    offline: 17 of 134 historical fires were exits the model made on its own). Now it needs the tail
    to have occurred INTRA_TURN_REPEAT_MIN_EARLIER times (0 measured false fires).
 2. When the retry also looped, the run PAUSED (parked awaiting a human). It now takes the same
    rung as a reasoning runaway: nudge + the turn re-run with thinking OFF. The aborted reasoning is
    persisted so a fire can be judged afterwards.

Pure checks on the detector + the REAL run_task with the model stubbed (HOME sandboxed).
--revert-check mutates the worker (WORKER_SRC env) and requires RED."""
import importlib.util, json, os, subprocess, sys, tempfile, threading, time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
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
    os.environ["HOME"] = tempfile.mkdtemp(prefix="rlfp-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    os.environ["NOTHINK_RUN"] = "0"   # NOTHINK_RUN is default ON (2026-10-15); this test exercises the thinking-on author path, so pin the legacy baseline
    spec = importlib.util.spec_from_file_location("ow_rlfp", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    assert str(m.LOG_DIR).startswith(os.environ["HOME"]), m.LOG_DIR
    return m


# --- fixtures ---------------------------------------------------------------------------
DRAFT = ("def save_screenshots(files, data_dir, first_vm_id):\n"
         "    ss_dir = os.path.join(data_dir, 'screenshots', str(first_vm_id))\n"
         "    os.makedirs(ss_dir, exist_ok=True)\n    saved = []\n"
         "    for i, f in enumerate(files):\n"
         "        ext = os.path.splitext(f.filename or '')[1] or '.jpg'\n"
         "        rel = f'screenshots/{first_vm_id}/{i}{ext}'\n"
         "        with open(os.path.join(data_dir, rel), 'wb') as fh:\n"
         "            fh.write(f.read())\n        saved.append(rel)\n"
         "    conn.execute('update voicemails set screenshot_paths = ? where id = ?', (json.dumps(saved), first_vm_id))\n"
         "    conn.commit()\n    return saved\n")


def distinct(n, tag):
    return " ".join(f"Considering {tag} case number {k}: the handler must reject input {k * 7} "
                    f"because condition {k * 13} is violated." for k in range(n))


# the model drafts the same function once, reasons about something else, drafts it again, then goes on
LEGIT = (distinct(14, "alpha") + "\nLet me write it out:\n" + DRAFT + "\n" + distinct(14, "beta")
         + "\nOk, final version of the function:\n" + DRAFT + "\n" + distinct(6, "gamma"))
# a real cycle: the same paragraph over and over
CYCLE = "Wait, let me re-read the task. " + (distinct(1, "loop") + " Hmm, but the test needs X. ") * 0
PARA = ("Wait, let me re-read the task. The verify script checks that extractCardFromPaymentHtml is "
        "defined in sidecar/src/amazon.js and called from fetchOrderDetails, and that paymentMissing "
        "is surfaced. But the file has 808 lines and the function is not there. So I need to add it. "
        "Hmm, let me look at the test again to see the exact contract before I decide anything. ")
LOOP = distinct(10, "intro") + "\n" + PARA * 20


def stream_fire(m, text, step=500):
    """Feed `text` the way the stream does (growing buffer, check cadence). -> (reason, chars) or None."""
    last = 0
    for n in range(step, len(text) + 1, step):
        chk, loop, last2, why = m.think_buf_loop_check(text[:n], last, False)
        if chk:
            last = last2
        if loop:
            return why, n
    return None


class _Abort(Exception):
    pass


def drive(m, script, max_iters=12):
    """script items: 'LOOP' | 'RUNAWAY' | response dict. -> (calls, log, files)"""
    wt = Path(tempfile.mkdtemp(prefix="rlfp-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    calls, seq = [], list(script)

    def fake_call(host, model, messages, temperature, num_ctx, **kw):
        calls.append({"think": kw.get("think"), "temperature": temperature,
                      "last_user": next((mm.get("content") for mm in reversed(messages)
                                         if mm.get("role") == "user"), "")})
        item = seq.pop(0) if seq else {"message": {"role": "assistant", "content": "",
                                                  "tool_calls": [{"function": {"name": "task_complete",
                                                                               "arguments": {"summary": "done"}}}]},
                                       "done_reason": "stop", "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
        if item == "LOOP":
            e = m.ChatAbortedForReasoningLoop("intra-turn reasoning loop (repeat): 14036 chars of thinking")
            e.thinking = LOOP
            raise e
        if item == "RUNAWAY":
            e = m.ChatAbortedForReasoningRunaway(24002)
            e.thinking = "x" * 24002
            raise e
        return item

    m.call_ollama = fake_call
    m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    m.clamp_unraid_ctx = lambda host, model, n: n
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    try:
        m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "edit a.txt", None,
                   max_iters, 0.0, 65536, None, max_tokens=8192)
    except SystemExit:
        pass
    except Exception as e:
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
    files = sorted(m.LOG_DIR.glob("*.reasoning-abort-*.txt"))
    return calls, "\n".join(logs), files


# --- end-to-end through the real SSE streaming path (stub server) -------------------------
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
        SSE_REQS.append(thinking)
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            if thinking is False:
                call = {"index": 0, "id": "c1", "type": "function",
                        "function": {"name": "task_complete", "arguments": json.dumps({"summary": "done"})}}
                self.wfile.write((_chunk({"tool_calls": [call]}, "tool_calls")
                                  + 'data: {"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 5, "total_tokens": 14}}\n\n'
                                  + "data: [DONE]\n\n").encode())
                self.wfile.flush()
                return
            self.wfile.write(_chunk({"reasoning_content": distinct(10, "intro") + "\n"}).encode())
            while True:                         # a true fixed point: the same paragraph forever
                self.wfile.write(_chunk({"reasoning_content": PARA}).encode())
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass


def sse_ladder(m):
    print("== end-to-end: a persistent REAL loop on the SSE path ends on the thinking-off rung, not a pause ==")
    srv = ThreadingHTTPServer(("127.0.0.1", 0), SSEHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    wt = Path(tempfile.mkdtemp(prefix="rlfp-sse-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    for fp in m.LOG_DIR.glob("*.reasoning-abort-*.txt"):
        fp.unlink()
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
        logs.append(f"run_task raised: {e!r}")
    finally:
        m.log = orig
        srv.shutdown()
    log = "\n".join(logs)
    check("SSE: requests = thinking, thinking (retry), thinking OFF", SSE_REQS[:3], [True, True, False])
    check("SSE: the loop was detected as a repeat, not waited out", "intra-turn reasoning loop (repeat)" in log, True)
    check("SSE: not parked as reasoning_loop", "PAUSED FOR REVIEW" in log, False)
    check("SSE: run completed", "TERMINAL REASON" in log, False)
    check("SSE: fast", time.time() - t0 < 60, True)
    files = sorted(m.LOG_DIR.glob("*.reasoning-abort-*.txt"))
    check("SSE: both aborted reasoning blocks persisted with their text",
          len(files) == 2 and all("Wait, let me re-read the task" in f.read_text() for f in files), True)


def main():
    m = _load()
    # --- detector (pure) ---
    check("drafting the same function twice in the reasoning is NOT a loop", stream_fire(m, LEGIT), None)
    at_second_draft = LEGIT[:LEGIT.rfind(DRAFT) + len(DRAFT)]
    check("buffer ending exactly on the second draft (tail seen once before) is not a loop",
          m.think_buf_loop_check(at_second_draft, 0, False)[1], False)
    f = stream_fire(m, LOOP)
    check("a paragraph cycled 20x IS a loop (repeat)", f is not None and f[0] == "repeat", True)
    check("...caught before the 24000-char reasoning budget", f is not None and f[1] < 24000, True)
    check("a single re-quote of 400+ chars is not a loop",
          stream_fire(m, distinct(12, "one") + PARA + distinct(12, "two") + PARA + distinct(12, "three")), None)
    check("INTRA_TURN_REPEAT_MIN_EARLIER >= 2 (one earlier occurrence is not a fixed point)",
          m.INTRA_TURN_REPEAT_MIN_EARLIER >= 2, True)
    check("any output this turn -> never a loop", m.think_buf_loop_check(LOOP * 3, 0, True)[1], False)
    # --- recovery ladder (real run_task) ---
    calls, log, files = drive(m, ["LOOP", "LOOP"])
    check("loop, loop-again -> third call is thinking OFF", [c["think"] for c in calls][:3] != [], True)
    check("...the third request has think=False", len(calls) >= 3 and calls[2]["think"] is False, True)
    check("...and carries the reasoning nudge", len(calls) >= 3 and "[reasoning budget]" in calls[2]["last_user"], True)
    check("...the run is NOT parked as reasoning_loop paused",
          "pausing as resumable" in log or "PAUSED FOR REVIEW" in log, False)
    check("...and finishes on the model's task_complete", "task_complete" in log or "converged" in log.lower(), True)
    check("the first retry stays a plain retry (thinking not yet disabled)", calls[1]["think"] is not False, True)
    check("each aborted turn's reasoning was persisted for later judging", len(files) >= 2, True)
    check("...with the streamed text in it", bool(files) and "Wait, let me re-read the task" in files[0].read_text(), True)
    for fp in files:
        fp.unlink()
    # a loop that recovers on the first retry never reaches the thinking-off rung
    calls, log, files = drive(m, ["LOOP"])
    check("one loop then a normal turn: no thinking-off rung", all(c["think"] is not False for c in calls), True)
    for fp in files:
        fp.unlink()
    sse_ladder(m)
    # thinking-off recovery cannot be used forever: a persistent loop still ends, with the named pause
    calls, log, files = drive(m, ["LOOP"] * 12, max_iters=12)
    check("a persistent loop still terminates (bounded)", len(calls) <= 12, True)
    check("flag OFF (default): spent recovery still pauses as resumable (legacy, byte-identical path)",
          "pausing as resumable" in log and "reasoning_loop_exhausted" not in log, True)
    for fp in files:
        fp.unlink()
    os.environ["WORKER_LOOP_EXHAUST_EXIT"] = "1"
    try:
        calls, log, files = drive(m, ["LOOP"] * 12, max_iters=12)
    finally:
        del os.environ["WORKER_LOOP_EXHAUST_EXIT"]
    check("flag ON: spent recovery ends with the named reason reasoning_loop_exhausted",
          "EARLY ABORT" in log and "reasoning_loop_exhausted" in log, True)
    check("flag ON: the run is NOT parked paused", "pausing as resumable" in log or "PAUSED FOR REVIEW" in log, False)
    check("flag ON: the thinking-off rung still runs first", any(c["think"] is False for c in calls), True)
    for fp in files:
        fp.unlink()
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("exhaust-exit flag ignored (still pauses)", "                        if _loop_exhaust_exit:\n", "                        if False:\n"),
    ("exhaust-exit flag always on", "_loop_exhaust_exit = _wr.loop_exhaust_exit_enabled(_rb)", "_loop_exhaust_exit = True"),
    ("single earlier occurrence fires again", "if _count_overlapping(earlier, tail, INTRA_TURN_REPEAT_MIN_EARLIER) >= INTRA_TURN_REPEAT_MIN_EARLIER:",
     "if tail in earlier:"),
    ("min-earlier constant lowered to 1", "INTRA_TURN_REPEAT_MIN_EARLIER = 3", "INTRA_TURN_REPEAT_MIN_EARLIER = 1"),
    ("exhausted retries park the job (old behaviour)",
     "                    if _turn_think is False or _runaway_streak > 2:\n                        if not _loop_exhaust_exit:",
     "                    if True:\n                        if not _loop_exhaust_exit:"),
    ("thinking-off rung does not disable thinking",
     "                    _turn_think = False\n                    messages.append({\"role\": \"user\", \"content\": _wr.reasoning_runaway_message(_lc)})",
     "                    messages.append({\"role\": \"user\", \"content\": _wr.reasoning_runaway_message(_lc)})"),
    ("no nudge message on the thinking-off rung",
     "                    messages.append({\"role\": \"user\", \"content\": _wr.reasoning_runaway_message(_lc)})",
     "                    pass"),
    ("aborted reasoning not persisted",
     "                _persist_reasoning_abort(log_path, i, \"loop\", loop_err, log, _dispatch_metrics)\n", ""),
    ("abort exception does not carry the thinking",
     "    exc.thinking = thinking\n    return exc", "    return exc"),
    ("stream loop raise drops the thinking (openai path)",
     "                        raise _with_thinking(ChatAbortedForReasoningLoop(", "                        raise (ChatAbortedForReasoningLoop("),
]


def revert_check():
    bad = 0
    src = WORKER.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-worker.py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "WORKER_SRC": f.name},
                           capture_output=True, text=True, timeout=600)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
