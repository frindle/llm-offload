#!/usr/bin/env python3
"""Darkbloom HTTP 422 "Inference generation failed" recovery ladder (ollama-worker.py), 2026-10-09.

Darkbloom 0.9.19 (MTP + tool-call generation) answers 422 deterministically for some
contexts when tools are present (the model emits </think> then dies at the first tool-call
token). Identical retries never help; before the fix the worker retried 2x then PAUSED FOR
REVIEW (exit 3), blocking the whole cell. The fix: a bounded ladder (_recover_422), one
attempt per step, on a COPY of the messages: (1) tool_choice "none" + a nudge asking for the
text-embedded call the fallback parser consumes, (2) last tool result cut to 40 lines.

Real HTTP stub server (no GPU). Scenario A: 422 for any request with tools AND the full tool
result, tool_choice none works -> run continues, recovery logged, real tool output kept in
the transcript. Scenario B: only truncation works (tool_choice none also 422s). Scenario C:
permanently failing server -> still ends PAUSED FOR REVIEW, attempts capped.

Run: python3 test-worker-422-recovery.py   (exit 0 = all pass; WORKER_SRC overrides the worker)"""
import importlib.util, json, os, subprocess, sys, tempfile, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

WORKER = Path(os.environ.get("WORKER_SRC") or Path(__file__).resolve().parent / "ollama-worker.py")
FAILS = []
REQS = []
MODE = ["A"]
BIG = "\n".join(f"line {k}" for k in range(1, 121))


def check(name, got, want=True):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _tc(name, args):
    return {"role": "assistant", "content": "", "tool_calls": [
        {"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}]}


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(b)))
        self.end_headers()
        self.wfile.write(b)

    def do_POST(self):
        p = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
        msgs = p.get("messages") or []
        last_tool = next((m for m in reversed(msgs) if m.get("role") == "tool"), None)
        full = bool(last_tool) and "[truncated" not in str(last_tool.get("content"))
        tc = p.get("tool_choice")
        REQS.append({"tools": bool(p.get("tools")), "tool_choice": tc, "has_tool_msg": bool(last_tool),
                     "full": full, "last_user": (msgs[-1].get("content") or "")[:60] if msgs else ""})
        fail = {"error": {"message": "Inference generation failed", "type": "invalid_request_error"}}
        if last_tool is None:                         # preflight / first turn: run something noisy
            return self._send(200, {"choices": [{"message": _tc("run_bash", {"command": "seq 1 120"})}],
                                    "usage": {"prompt_tokens": 50, "completion_tokens": 5, "total_tokens": 55}})
        if MODE[0] == "C" and p.get("tools"):
            return self._send(422, fail)
        if p.get("tools") and full and tc != "none" and MODE[0] in ("A", "B"):
            return self._send(422, fail)
        if MODE[0] == "B" and p.get("tools") and tc == "none":
            return self._send(422, fail)
        if tc == "none":                              # text-embedded call, parsed by the fallback parser
            msg = {"role": "assistant", "content": "<tool_call>\n<function=task_complete>\n<parameter=summary>\n"
                                                    "done via text call\n</parameter>\n</function>\n</tool_call>"}
        else:
            msg = _tc("task_complete", {"summary": "done"})
        self._send(200, {"choices": [{"message": msg}],
                         "usage": {"prompt_tokens": 60, "completion_tokens": 6, "total_tokens": 66}})


def load_worker():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="r422-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    spec = importlib.util.spec_from_file_location("ow_r422", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    return m


def drive(m, host, mode):
    MODE[0] = mode
    REQS.clear()
    wt = Path(tempfile.mkdtemp(prefix="r422-wt-"))
    subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
    (wt / "a.txt").write_text("x\n")
    m.ensure_model_ready = lambda *a, **k: None
    m.set_keep_alive = lambda *a, **k: None
    logs, orig = [], m.log
    m.log = lambda s, *a, **k: logs.append(str(s))
    code = None
    try:
        m.run_task("qwen3.6-35b-a3b-vl-mtp-mxfp8", host, str(wt), "edit a.txt", None, 6, None, None, None,
                   api_style="openai", role="author")
    except SystemExit as e:
        code = e.code
    except Exception as e:
        import traceback
        logs.append(f"run_task raised: {e!r}\n{traceback.format_exc()}")
    finally:
        m.log = orig
    return "\n".join(logs), code, dict(m._dispatch_metrics), wt


def main():
    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    host = f"http://127.0.0.1:{srv.server_address[1]}"
    m = load_worker()
    print("== A: tool_choice=none recovers ==")
    log, code, met, wt = drive(m, host, "A")
    check("recovery step 1 logged ok", "422 recovery: step 1" in log and "step 1 (tool_choice=none + nudge) ok" in log, True)
    check("run was NOT paused for review", "PAUSED FOR REVIEW" in log, False)
    check("text-embedded call parsed by the fallback parser", "fallback-parse" in log or "tool-call repair" in log, True)
    check("recovery request carried tool_choice none + nudge",
          any(r["tool_choice"] == "none" and "Continue" in r["last_user"] for r in REQS), True)
    check("recovery counter recorded in metrics", (met.get("recovery_422") or {}).get("ok"), 1)
    check("task completed (no terminal exit 3)", code in (None, 0), True)
    print("== B: only truncation works ==")
    log, code, met, wt = drive(m, host, "B")
    check("step 1 failed then step 2 ok", "step 1 (tool_choice=none + nudge) failed" in log and "step 2" in log and "truncated to 40 lines) ok" in log, True)
    check("a request with the truncated tool result was sent",
          any(r["has_tool_msg"] and not r["full"] and r["tools"] for r in REQS), True)
    check("not paused", "PAUSED FOR REVIEW" in log, False)
    tr = sorted((Path(os.environ["HOME"]) / "bin" / "ollama-worker-logs").glob("*.json")) if (Path(os.environ["HOME"]) / "bin" / "ollama-worker-logs").exists() else []
    if tr:
        t = json.loads(tr[-1].read_text())
        tools = [x["content"] for x in t["messages"] if x.get("role") == "tool"]
        check("transcript keeps the REAL full tool output (no truncation marker)",
              bool(tools) and "\\n120" in tools[0] and "[truncated" not in tools[0], True)
    print("== C: permanently failing server still pauses, capped ==")
    log, code, met, wt = drive(m, host, "C")
    check("paused for review", "PAUSED FOR REVIEW" in log, True)
    n = (met.get("recovery_422") or {}).get("attempts", 0)
    check("recovery attempts bounded (<= cap)", 0 < n <= m.RECOVERY_422_MAX_ATTEMPTS, True)
    trs = sorted((Path(os.environ["HOME"]) / "bin" / "ollama-worker-logs").glob("*.json"))
    check("transcript saved resumable (chat_request_failed)", json.loads(trs[-1].read_text()).get("pause_reason"), "chat_request_failed")
    print("== D: per-run cap (unit, _recover_422) ==")
    calls = []

    def fake(msgs, **kw):
        calls.append(kw)
        raise RuntimeError("HTTP Error 422: Inference generation failed")
    msgs = [{"role": "user", "content": "t"}, {"role": "tool", "content": "\n".join("x" for _ in range(90))}]
    met = {"recovery_422": {"attempts": m.RECOVERY_422_MAX_ATTEMPTS, "ok": 0, "events": []}}
    check("cap reached -> no request sent, None", (m._recover_422(fake, msgs, met), len(calls)), (None, 0))
    met = {"recovery_422": {"attempts": m.RECOVERY_422_MAX_ATTEMPTS - 1, "ok": 0, "events": []}}
    check("one attempt left -> exactly one request", (m._recover_422(fake, msgs, met), len(calls)), (None, 1))
    check("real messages never mutated", msgs[1]["content"].count("\n"), 89)
    srv.shutdown()
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
