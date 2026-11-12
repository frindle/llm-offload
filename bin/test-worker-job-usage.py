#!/usr/bin/env python3
"""Per-job token + speed stats (seam `jobusage`). Worker side: usage normalization for the
Ollama native + OpenAI-compatible backends (non-streaming and streaming), per-iteration
accumulation into dispatch-metrics (NEW optional fields only), and the USAGE SUMMARY log line.
Dashboard side: the log-line parser (_usage_summary) and the finished-row display.

Properties pinned: usage the backend does not report yields absent/None (never an exception,
never a changed result); the pre-existing usage keys + metrics fields are unchanged.
No network, no servers (urlopen stubbed), HOME sandboxed."""
import importlib.util, io, json, os, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKER = Path(os.environ.get("WORKER_SRC") or HERE / "ollama-worker.py")
API = Path(os.environ.get("API_SRC") or HERE / "ollama-queue-api.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def _load():
    os.environ["HOME"] = tempfile.mkdtemp(prefix="jobusage-home-")
    os.environ.pop("VAULT_TOKEN_FILE", None)
    os.environ.pop("MODEL_SAMPLING_ARM", None)
    sys.path.insert(0, str(WORKER.parent))
    spec = importlib.util.spec_from_file_location("ow_jobusage", WORKER)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.log_dispatch_to_vault = lambda *a, **k: None
    return m


class FakeResp:
    def __init__(self, body=b"", lines=()):
        self._b, self._l = body, list(lines)

    def read(self, *a):
        return self._b

    def __iter__(self):
        return iter(self._l)

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def close(self):
        pass


def with_urlopen(m, resp, fn):
    orig = m.urllib.request.urlopen
    m.urllib.request.urlopen = lambda req, timeout=None: resp
    try:
        return fn()
    finally:
        m.urllib.request.urlopen = orig


MSG = [{"role": "user", "content": "x"}]


def main():
    m = _load()

    # ---- pure normalization -------------------------------------------------
    u = m.usage_from_ollama({"prompt_eval_count": 100, "eval_count": 50, "eval_duration": 2_000_000_000})
    check("ollama usage keeps the three legacy keys", {k: u[k] for k in ("prompt_tokens", "completion_tokens", "total_tokens")},
          {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150})
    check("ollama usage adds eval_duration_s", u.get("eval_duration_s"), 2.0)
    u = m.usage_from_ollama({})
    check("ollama usage with nothing reported = zeros and NO eval_duration_s",
          u, {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    check("garbage eval_duration is dropped, not raised", "eval_duration_s" in m.usage_from_ollama({"eval_duration": "x"}), False)
    check("negative / zero / bool eval_duration dropped",
          [("eval_duration_s" in m.usage_from_ollama({"eval_duration": v})) for v in (-5, 0, True, None)], [False] * 4)
    check("non-dict raw tolerated", m.usage_from_ollama(None)["total_tokens"], 0)

    # ---- accumulation across iterations -------------------------------------
    met = {}
    m.usage_accumulate(met, {"prompt_tokens": 1000, "completion_tokens": 40, "eval_duration_s": 1.0}, 2.0)
    m.usage_accumulate(met, {"prompt_tokens": 1500, "completion_tokens": 60, "eval_duration_s": 3.0}, 4.0)
    check("prompt tokens summed", met["sum_prompt_tokens"], 2500)
    check("call seconds + tokens summed", (met["sum_call_s"], met["sum_call_completion_tokens"]), (6.0, 100))
    check("eval seconds + tokens summed", (met["sum_eval_s"], met["sum_eval_completion_tokens"]), (4.0, 100))
    met["sum_completion_tokens"], met["calls"] = 100, 2
    line = m.usage_finalize(met)
    check("overall tok/s = tokens / summed call seconds", met["tok_s_overall"], round(100 / 6.0, 1))
    check("decode tok/s = tokens / summed eval seconds", met["decode_tok_s"], 25.0)
    check("summary line shape", line, "[worker] USAGE SUMMARY: prompt_tokens=2500 completion_tokens=100 tok_s=16.7 decode_tok_s=25.0 calls=2")

    # backend with no usage / partial usage / garbage
    for label, us in (("missing usage", None), ("empty usage", {}), ("garbage usage", {"prompt_tokens": "x", "completion_tokens": [1]}),
                      ("non-dict usage", "oops"), ("zero usage", {"prompt_tokens": 0, "completion_tokens": 0})):
        mm = {}
        try:
            m.usage_accumulate(mm, us, 1.0)
            m.usage_accumulate(mm, us, None)
            ln = m.usage_finalize(mm)
            check(f"{label}: no exception, nothing invented", (mm, ln), ({}, ""))
        except Exception as e:
            check(f"{label}: no exception", repr(e), "none")
    mm = {}
    m.usage_accumulate(mm, {"prompt_tokens": 7, "completion_tokens": 3}, 1.5)   # OpenAI: no eval duration
    m.usage_finalize(mm)
    check("openai-style usage: overall tok/s present, decode_tok_s absent",
          ("tok_s_overall" in mm, "decode_tok_s" in mm), (True, False))
    mm = {"sum_prompt_tokens": 5}
    m.usage_finalize(mm)
    check("finalize with only prompt tokens leaves tok/s absent", ("tok_s_overall" in mm), False)
    check("finalize never raises on a bad dict", m.usage_finalize({"sum_call_s": "a", "sum_call_completion_tokens": []}), "")

    # ---- non-streaming backends (urlopen stubbed) ---------------------------
    body = json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True, "done_reason": "stop",
                       "prompt_eval_count": 321, "eval_count": 45, "eval_duration": 1_500_000_000}).encode()
    r = with_urlopen(m, FakeResp(body), lambda: m.call_ollama("http://x", "stub", MSG, 0.2, 4096, timeout=5, tools=False, api_style="ollama", max_tokens=8))
    check("ollama non-stream: legacy usage preserved + eval_duration_s",
          (r["usage"]["prompt_tokens"], r["usage"]["completion_tokens"], r["usage"]["total_tokens"], r["usage"]["eval_duration_s"]),
          (321, 45, 366, 1.5))
    body = json.dumps({"message": {"role": "assistant", "content": "hi"}, "done": True}).encode()
    r = with_urlopen(m, FakeResp(body), lambda: m.call_ollama("http://x", "stub", MSG, 0.2, 4096, timeout=5, tools=False, api_style="ollama", max_tokens=8))
    check("ollama non-stream, server omits counters: zeros, no eval_duration_s, call still succeeds",
          r["usage"], {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
    body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}],
                       "usage": {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15}}).encode()
    r = with_urlopen(m, FakeResp(body), lambda: m.call_ollama("http://x", "stub", MSG, 0.2, 4096, timeout=5, tools=False, api_style="openai", max_tokens=8))
    check("openai non-stream: usage passed through unchanged", r["usage"], {"prompt_tokens": 12, "completion_tokens": 3, "total_tokens": 15})
    body = json.dumps({"choices": [{"index": 0, "finish_reason": "stop", "message": {"role": "assistant", "content": "hi"}}]}).encode()
    r = with_urlopen(m, FakeResp(body), lambda: m.call_ollama("http://x", "stub", MSG, 0.2, 4096, timeout=5, tools=False, api_style="openai", max_tokens=8))
    check("openai non-stream, server omits usage: call succeeds with content, usage empty-ish",
          (r["message"]["content"], not (r.get("usage") or {}).get("completion_tokens")), ("hi", True))

    # ---- streaming backends -------------------------------------------------
    def ndjson(*objs):
        return [(json.dumps(o) + "\n").encode() for o in objs]
    lines = ndjson({"message": {"content": "he"}}, {"message": {"content": "llo"}},
                   {"message": {"content": ""}, "done": True, "done_reason": "stop",
                    "prompt_eval_count": 90, "eval_count": 30, "eval_duration": 1_000_000_000})
    r = with_urlopen(m, FakeResp(lines=lines), lambda: m.call_ollama_streaming("http://x", "stub", MSG, 0.0, 4096, timeout=5, tools=False, max_tokens=8))
    check("ollama stream: counters + eval_duration_s, content intact",
          (r["usage"]["prompt_tokens"], r["usage"]["completion_tokens"], r["usage"].get("eval_duration_s"), r["message"]["content"]),
          (90, 30, 1.0, "hello"))
    lines = ndjson({"message": {"content": "hi"}}, {"message": {"content": ""}, "done": True, "done_reason": "stop"})
    r = with_urlopen(m, FakeResp(lines=lines), lambda: m.call_ollama_streaming("http://x", "stub", MSG, 0.0, 4096, timeout=5, tools=False, max_tokens=8))
    check("ollama stream, server omits counters: no exception, no eval_duration_s, content intact",
          (r["usage"]["completion_tokens"], "eval_duration_s" in r["usage"], r["message"]["content"]), (0, False, "hi"))

    def sse(*objs):
        return [("data: " + json.dumps(o) + "\n\n").encode() for o in objs] + [b"data: [DONE]\n\n"]
    ch = lambda delta, fin=None: {"choices": [{"index": 0, "delta": delta, "finish_reason": fin}]}
    lines = sse(ch({"content": "ab"}), ch({"content": "cd"}, "stop"), {"choices": [], "usage": {"prompt_tokens": 111, "completion_tokens": 22, "total_tokens": 133}})
    r = with_urlopen(m, FakeResp(lines=lines), lambda: m.call_openai_streaming("http://x", "stub", MSG, 0.0, 4096, timeout=5, tools=False, max_tokens=8))
    check("openai stream: reported usage preserved", (r["usage"]["prompt_tokens"], r["usage"]["completion_tokens"], r["message"]["content"]), (111, 22, "abcd"))
    lines = sse(ch({"content": "ab"}), ch({"content": "cd"}, "stop"))
    r = with_urlopen(m, FakeResp(lines=lines), lambda: m.call_openai_streaming("http://x", "stub", MSG, 0.0, 4096, timeout=5, tools=False, max_tokens=8))
    check("openai stream, server omits usage: no exception, content intact", r["message"]["content"], "abcd")

    # ---- run_task integration: multi-iteration, mixed usage, result unchanged
    def drive(usages):
        wt = Path(tempfile.mkdtemp(prefix="jobusage-wt-"))
        subprocess.run(["git", "init", "-q", "."], cwd=wt, check=True)
        (wt / "a.py").write_text("x = 1\n")
        seq = []
        for i, us in enumerate(usages):
            calls = [("task_complete", {"summary": "done"})] if i == len(usages) - 1 else [("read_file", {"path": "a.py"})]
            t = {"message": {"role": "assistant", "content": "",
                             "tool_calls": [{"function": {"name": n, "arguments": a}} for n, a in calls]},
                 "done_reason": "stop"}
            if us is not None:
                t["usage"] = us
            seq.append(t)

        def fake_call(host, model, messages, temperature, num_ctx, **kw):
            time.sleep(0.02)
            return seq.pop(0) if seq else seq_done()
        seq_done = lambda: {"message": {"role": "assistant", "content": "", "tool_calls": [{"function": {"name": "task_complete", "arguments": {"summary": "done"}}}]}, "done_reason": "stop"}
        m.call_ollama = fake_call
        m.call_ollama_streaming = lambda *a, **k: fake_call(*a, **k)
        m.ensure_model_ready = lambda *a, **k: None
        m.set_keep_alive = lambda *a, **k: None
        m.clamp_unraid_ctx = lambda host, model, n: n
        m.DISPATCH_METRICS_PATH = Path(tempfile.mkdtemp(prefix="jobusage-met-")) / "dm.jsonl"
        logs, orig = [], m.log
        m.log = lambda s, *a, **k: logs.append(str(s))
        raised = None
        try:
            m.run_task("stub-model", "http://127.0.0.1:9", str(wt), "edit a.py", None, 20, 0.0, 65536, None, max_tokens=8192)
        except SystemExit:
            pass
        except Exception as e:
            raised = repr(e)
        finally:
            m.log = orig
        rows = [json.loads(x) for x in m.DISPATCH_METRICS_PATH.read_text().splitlines()] if m.DISPATCH_METRICS_PATH.exists() else []
        return rows[-1] if rows else {}, logs, raised

    row, logs, raised = drive([{"prompt_tokens": 1000, "completion_tokens": 40, "total_tokens": 1040},
                               {"prompt_tokens": 1200, "completion_tokens": 60, "total_tokens": 1260},
                               {"prompt_tokens": 1400, "completion_tokens": 20, "total_tokens": 1420}])
    check("run_task: no exception", raised, None)
    check("run_task: legacy metrics fields unchanged", (row.get("calls"), row.get("peak_prompt_tokens"), row.get("sum_completion_tokens"), row.get("status")),
          (3, 1400, 120, "converged"))
    check("run_task: sum_prompt_tokens summed over iterations", row.get("sum_prompt_tokens"), 3600)
    check("run_task: tok_s_overall computed and positive", (row.get("tok_s_overall") or 0) > 0, True)
    check("run_task: USAGE SUMMARY line logged", any(l.startswith("[worker] USAGE SUMMARY: prompt_tokens=3600 completion_tokens=120") for l in logs), True)

    row2, logs2, raised2 = drive([None, {}, None])       # backend reports nothing at all
    check("no-usage run: no exception", raised2, None)
    check("no-usage run: same terminal status as a run with usage", row2.get("status"), row.get("status"))
    check("no-usage run: new fields absent (not zero, not error)",
          [k in row2 for k in ("sum_prompt_tokens", "tok_s_overall", "decode_tok_s")], [False, False, False])
    check("no-usage run: no USAGE SUMMARY line", any("USAGE SUMMARY" in l for l in logs2), False)

    # ---- dashboard parser ---------------------------------------------------
    src = API.read_text()
    ns = {}
    import re as _re
    consts = src[src.index("_USAGE_RE = "):src.index("_USAGE_CACHE = {}") + len("_USAGE_CACHE = {}")]
    fn = src[src.index("def _usage_summary"):src.index("def _ollama_resident_models")]
    exec("import os, re\n" + consts + "\n" + fn, ns)
    d = tempfile.mkdtemp(prefix="jobusage-log-")
    p = Path(d) / "j.log"
    p.write_text("noise\n" + "[worker] USAGE SUMMARY: prompt_tokens=3600 completion_tokens=120 tok_s=16.7 decode_tok_s=n/a calls=3\nmore\n")
    check("dashboard parser reads the summary line", ns["_usage_summary"](str(p)),
          {"prompt_tokens": 3600, "completion_tokens": 120, "tok_s": 16.7, "decode_tok_s": None, "calls": 3})
    p2 = Path(d) / "old.log"
    p2.write_text("an old job log with no summary\n")
    check("dashboard parser: old log without the line -> None", ns["_usage_summary"](str(p2)), None)
    check("dashboard parser: missing file / no path -> None, no exception", (ns["_usage_summary"](str(Path(d) / "nope")), ns["_usage_summary"](None)), (None, None))
    check("dashboard: JS row display is guarded on j.usage", "(j.usage && j.usage.completion_tokens != null)" in src, True)
    check("dashboard: usage attached only to finished jobs", 'if j.get("status") not in ("pending", "running") else None' in src, True)
    check("dashboard: legacy tok_s/iteration fields still emitted", all(x in src for x in ('d["tok_s"] = tok_s', 'd["iteration"] = iteration', 'd["max_iters"] = max_iters')), True)

    print()
    print(f"{'ALL PASS' if not FAILS else 'FAILURES: ' + ', '.join(FAILS)}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
