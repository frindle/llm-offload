#!/usr/bin/env python3
"""Tests for fixed_lane.py + edit_guard.py (the fixed repair-and-validate lane).

A stub OpenAI-compatible server (SSE and non-stream) is driven through the REAL
OpenAITransport, so request bodies, streaming parse, runaway cuts and retries are
exercised end to end. No live model, no queue, no network beyond 127.0.0.1.

Prints "ALL PASS" on success (the pipeline-canary seam marker).
FIXED_LANE_SRC=<dir> points at another copy of fixed_lane.py / edit_guard.py.
"""
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC = Path(os.environ.get("FIXED_LANE_SRC") or Path(__file__).resolve().parent)
sys.path.insert(0, str(SRC))
import edit_guard as eg          # noqa: E402
import fixed_lane as fl          # noqa: E402

FAILS = []
MODEL = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
SECRET = "sk-test-SECRET-key-123"


def chk(name, got, want=True):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def chk_in(name, needle, hay):
    ok = needle in hay
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  ({needle!r} not in {str(hay)[:200]!r})"))
    if not ok:
        FAILS.append(name)


# ------------------------------------------------------------------------- stub server
class Stub:
    """handler(body, n) -> dict(content, reasoning, finish_reason, mode) | callable streaming spec."""

    def __init__(self, handler):
        self.handler, self.requests, self.headers, self.n = handler, [], [], 0
        stub = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                ln = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(ln))
                stub.requests.append(body)
                stub.headers.append(dict(self.headers))
                n = stub.n
                stub.n += 1
                spec = stub.handler(body, n)
                if spec.get("http_error"):
                    self.send_response(spec["http_error"])
                    self.end_headers()
                    self.wfile.write(b'{"error":"boom"}')
                    return
                usage = {"prompt_tokens": 100, "completion_tokens": 50}
                if not body.get("stream"):
                    out = json.dumps({"choices": [{"message": {"content": spec.get("content", ""),
                                                                "reasoning_content": spec.get("reasoning", "")},
                                                    "finish_reason": spec.get("finish_reason", "stop")}],
                                      "usage": usage}).encode()
                    self.send_response(200)
                    self.send_header("Content-Type", "application/json")
                    self.send_header("Content-Length", str(len(out)))
                    self.end_headers()
                    self.wfile.write(out)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()

                def send(delta, fin=None):
                    d = {"choices": [{"delta": delta, "finish_reason": fin}]}
                    self.wfile.write(b"data: " + json.dumps(d).encode() + b"\n\n")
                    self.wfile.flush()
                try:
                    if spec.get("forever"):
                        key = spec["forever"]            # "content" | "reasoning"
                        t_end = time.time() + 6          # a lane that fails to cut it still ends the test
                        while time.time() < t_end:
                            send({key: "x" * 40})
                            time.sleep(0.001)
                        send({}, "stop")
                        return
                    r = spec.get("reasoning", "")
                    if r:
                        for i in range(0, len(r), 200):
                            send({"reasoning_content": r[i:i + 200]})
                    c = spec.get("content", "")
                    for i in range(0, max(len(c), 1), 37):
                        send({"content": c[i:i + 37]})
                    send({}, spec.get("finish_reason", "stop"))
                    self.wfile.write(b"data: " + json.dumps({"choices": [], "usage": usage}).encode() + b"\n\n")
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, OSError):
                    pass

        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.srv.daemon_threads = True
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}"
        threading.Thread(target=self.srv.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True).start()

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


def sr(path, old, new):
    return f"{path}\n```\n<<<<<<< SEARCH\n{old}\n=======\n{new}\n>>>>>>> REPLACE\n```\n"


# ------------------------------------------------------------------------- workspace
CALC = '''import os

def add(a, b):
    return a - b


def double(x):
    return add(x, x)
'''
CHECK = '''import sys
sys.path.insert(0, ".")
from calc import add, double
assert add(2, 3) == 5, "add(2,3) should be 5 but got %r" % add(2, 3)
assert double(4) == 8, "FAIL double"
print("OK")
'''


def mkws(calc=CALC, extra=None):
    d = tempfile.mkdtemp(prefix="fl-test-")
    Path(d, "calc.py").write_text(calc)
    Path(d, "check.py").write_text(CHECK)
    for k, v in (extra or {}).items():
        Path(d, k).write_text(v)
    return d


def task(d, **kw):
    t = {"cwd": d, "targets": ["calc.py"], "verify": "python3 check.py", "verify_timeout_s": 20,
         "spec": "add() must return the sum of its arguments.", "title": "fix add"}
    t.update(kw)
    return t


FIX = sr("calc.py", "    return a - b", "    return a + b")
WRONG = sr("calc.py", "    return a - b", "    return a * b")
BREAK = sr("calc.py", "    return a - b", "    return a +")


def lane(stub, d, stream=True, overrides=None, **kw):
    tr = fl.OpenAITransport(host=stub.url, api_key_fn=lambda: SECRET, stream=stream,
                            sleep=lambda s: None, retries=1)
    ov = {"reflections": 2, "temperatures": [None, 0.7, 0.9, 1.0], "format": "sr"}
    ov.update(overrides or {})
    logs = []
    res = fl.run_lane(task(d), model=MODEL, overrides=ov, transport=tr, log=logs.append, **kw)
    res["_logs"] = logs
    return res


def main():
    # -------------------------------------------------------------- constants
    chk("reason constants", fl.FIXED_LANE_FAILURE_REASONS,
        ("fixed_lane_exhausted", "fixed_lane_apply_failed", "fixed_lane_transport_error"))

    # -------------------------------------------------------------- eligibility router
    d = mkws()
    cfg = fl.lane_config(MODEL)
    chk("eligible: small task", fl.lane_eligible(task(d), cfg)[0], True)
    chk("eligible: 2 targets ok", fl.lane_eligible(task(d, targets=["calc.py", "check.py"]), cfg)[0], True)
    ok, why = fl.lane_eligible(task(d, targets=["calc.py", "check.py", "x.py"]), cfg)
    chk("ineligible: 3 targets", ok, False)
    chk_in("3-target reason", "3 target files", why[0])
    chk("ineligible: no verify", fl.lane_eligible(task(d, verify=""), cfg)[0], False)
    ok, why = fl.lane_eligible(task(d, verify_timeout_s=300), cfg)
    chk("ineligible: slow verify", ok, False)
    chk_in("slow verify reason", "not fast", why[0])
    ok, why = fl.lane_eligible(task(d, spec="Investigate why the totals drift and figure out the cause"), cfg)
    chk("ineligible: exploratory spec", ok, False)
    chk_in("exploratory reason", "exploratory", why[0])
    chk("ineligible: no targets", fl.lane_eligible(task(d, targets=[]), cfg)[0], False)
    chk("ineligible: target outside cwd", fl.lane_eligible(task(d, targets=["../etc/passwd"]), cfg)[0], False)
    big = mkws(calc="x = 1\n" * 500)
    ok, why = fl.lane_eligible(task(big), cfg)
    chk("ineligible: 500-line target", ok, False)
    chk_in("big target reason", "skeleton", why[0])
    chk("eligible: file to create (missing target)", fl.lane_eligible(task(d, targets=["new_mod.py"]), cfg)[0], True)
    chk("per-profile K: 35B-A3B=4", fl.lane_config(MODEL)["k"], 4)
    chk("per-profile K: 9B=2", fl.lane_config("Qwen3.5-9B")["k"], 2)
    chk("override beats profile", fl.lane_config(MODEL, {"k": 3})["k"], 3)

    # -------------------------------------------------------------- SEARCH/REPLACE parse
    e, errs = fl.parse_search_replace(FIX)
    chk("sr parse: one well-formed block", (len(e), errs), (1, []))
    chk("sr parse: path/search/replace", (e[0]["path"], e[0]["search"], e[0]["replace"]),
        ("calc.py", ["    return a - b"], ["    return a + b"]))
    e, errs = fl.parse_search_replace("Plan.\n\ncalc.py\n<<<<<<<<< SEARCH\nx\n=========\ny\n>>>>>>>>> REPLACE\n")
    chk("sr parse: no fence, 9-char markers", (len(e), errs), (1, []))
    e, errs = fl.parse_search_replace("calc.py\n```python\n<<<<<<< SEARCH\nx = 1\n=======\ny = 2")
    chk("sr parse: truncated reply -> error, no edit", (len(e), len(errs)), (0, 1))
    chk_in("truncated message", "never closed", errs[0])
    e, errs = fl.parse_search_replace("calc.py\n<<<<<<< SEARCH\nx = 1\n")
    chk_in("no-divider message", "no ======= divider", errs[0])
    e, errs = fl.parse_search_replace("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n")
    chk_in("no path -> error", "no file path", errs[0])
    e, errs = fl.parse_search_replace("<<<<<<< SEARCH\na\n=======\nb\n>>>>>>> REPLACE\n", default_path="calc.py")
    chk("single-target default path", e[0]["path"], "calc.py")
    two = FIX + "\nand\n" + sr("check.py", 'print("OK")', 'print("DONE")')
    e, _ = fl.parse_search_replace(two)
    chk("sr parse: two blocks two files", [x["path"] for x in e], ["calc.py", "check.py"])

    # -------------------------------------------------------------- apply
    files = {"calc.py": CALC}
    r = fl.apply_search_replace(files, fl.parse_search_replace(FIX)[0], ["calc.py"])
    chk("apply exact", ("return a + b" in r["new_files"]["calc.py"], r["failed"]), (True, []))
    r = fl.apply_search_replace(files, [{"path": "calc.py", "n": 1, "search": ["return a - b"],
                                         "replace": ["return a + b"]}], ["calc.py"])
    chk("apply indent-shift ladder", (r["applied"][0]["how"], "    return a + b\n" in r["new_files"]["calc.py"]),
        ("indent-shift", True))
    r = fl.apply_search_replace(files, [{"path": "calc.py", "n": 1, "search": ["def  add(a,  b):"],
                                         "replace": ["def add(a, b):  # sum"]}], ["calc.py"])
    chk("apply whitespace-collapsed ladder", r["applied"][0]["how"], "whitespace-collapsed")
    r = fl.apply_search_replace(files, [{"path": "calc.py", "n": 1, "search": ["    return a - c"],
                                         "replace": ["    return a + b"]}], ["calc.py"])
    chk("apply no match: failed + nothing written", (len(r["failed"]), r["new_files"]), (1, {}))
    chk_in("no-match: did you mean", "Did you mean", r["failed"][0]["error"])
    r = fl.apply_search_replace(files, [{"path": "calc.py", "n": 1, "search": ["def add(a, b):"],
                                         "replace": ["def add(a, b):"]}], ["calc.py"])
    amb = "x = 1\ny = 2\nx = 1\n"
    r = fl.apply_search_replace({"m.py": amb}, [{"path": "m.py", "n": 1, "search": ["x = 1"],
                                                 "replace": ["x = 3"]}], ["m.py"])
    chk("apply ambiguous: refused, file untouched", (len(r["failed"]), r["new_files"]), (1, {}))
    chk_in("ambiguous message", "matches 2 places", r["failed"][0]["error"])
    done = CALC.replace("a - b", "a + b")
    r = fl.apply_search_replace({"calc.py": done}, fl.parse_search_replace(FIX)[0], ["calc.py"])
    chk_in("already-applied hint", "already in calc.py", r["failed"][0]["error"])
    r = fl.apply_search_replace(files, [{"path": "evil.py", "n": 1, "search": ["a"], "replace": ["b"]}], ["calc.py"])
    chk_in("undeclared file refused", "not a declared editable file", r["failed"][0]["error"])
    r = fl.apply_search_replace(files, [{"path": "calc.py", "n": 1, "search": ["    return a - b"],
                                         "replace": ["    ...", "    return a + b"]}], ["calc.py"])
    chk_in("elision placeholder refused", "elision placeholder", r["failed"][0]["error"])
    r = fl.apply_search_replace({"new.py": None}, [{"path": "new.py", "n": 1, "search": [""],
                                                    "replace": ["print(1)"]}], ["new.py"])
    chk("create file via empty SEARCH", r["new_files"], {"new.py": "print(1)\n"})
    r = fl.apply_search_replace(files, [{"path": "calc.py", "n": 1, "search": [""], "replace": ["x"]}], ["calc.py"])
    chk_in("empty SEARCH on non-empty file refused", "empty SEARCH", r["failed"][0]["error"])
    r = fl.apply_search_replace({"calc.py": CALC.rstrip("\n")}, fl.parse_search_replace(FIX)[0], ["calc.py"])
    chk("no-trailing-newline file keeps none", r["new_files"]["calc.py"].endswith("\n"), False)

    # -------------------------------------------------------------- whole-file parse/apply
    body = "calc.py\n```python\ndef add(a, b):\n    return a + b\n```\n"
    f, errs = fl.parse_whole_files(body, ["calc.py"])
    chk("whole parse", (list(f), errs), (["calc.py"], []))
    nested = "calc.py\n```md\ntext\n```py\ncode\n```\nmore\n```\n"
    f, _ = fl.parse_whole_files(nested, ["calc.py"])
    chk("whole parse: inner fence kept", f["calc.py"], "text\n```py\ncode\n```\nmore\n")
    f, errs = fl.parse_whole_files("calc.py\n```python\ndef add(a, b):\n", ["calc.py"])
    chk("whole parse: truncated -> error", (f, len(errs)), ({}, 1))
    r = fl.apply_whole({"calc.py": CALC}, {"calc.py": CALC.replace("a - b", "a + b")}, ["calc.py"], "length", cfg)
    chk_in("whole: truncated (finish=length) nothing written", "cut off", r["failed"][0]["error"])
    r = fl.apply_whole({"calc.py": CALC}, {"calc.py": "def add(a, b):\n    ...\n"}, ["calc.py"], "stop", cfg)
    chk("whole: elision refused", len(r["failed"]), 1)
    r = fl.apply_whole({"calc.py": CALC * 3}, {"calc.py": "x = 1\n"}, ["calc.py"], "stop", cfg)
    chk_in("whole: big shrink refused", "omitted content", r["failed"][0]["error"])
    chk("format auto: small -> whole", fl.choose_format(cfg, {"a.py": "x\n" * 10}, ["a.py"]), "whole")
    chk("format auto: >150 lines -> sr", fl.choose_format(cfg, {"a.py": "x\n" * 200}, ["a.py"]), "sr")

    # -------------------------------------------------------------- edit guard
    ok_ = eg.guard_edit("a.py", "x = 1\n", "x = 2\n")
    chk("guard: clean edit passes", ok_.ok, True)
    bad = eg.guard_edit("a.py", "x = 1\n", "x = (\n")
    chk("guard: syntax regression rejected", (bad.ok, "breaks syntax" in bad.reasons[0]), (False, True))
    und = eg.guard_edit("a.py", "def f():\n    return 1\n", "def f():\n    return undefined_thing\n")
    chk("guard: new undefined name rejected", (und.ok, "undefined_thing" in und.reasons[0]), (False, True))
    pre = eg.guard_edit("a.py", "def f():\n    return missing\n", "def f():\n    return missing + 1\n")
    chk("guard: pre-existing lint error not blamed", pre.ok, True)
    broken_before = eg.guard_edit("a.py", "x = (\n", "x = (\n1)\n")
    chk("guard: broken file fixed passes", broken_before.ok, True)
    chk("guard: no-op edit rejected", eg.guard_edit("a.py", "x = 1\n", "x = 1\n").ok, False)
    chk("guard: json regression", eg.guard_edit("a.json", '{"a":1}', '{"a":').ok, False)
    chk("guard: unknown type unjudged", eg.guard_edit("a.txt", "a", "b").ok, True)
    chk("guard: builtin/import names ok", eg.guard_edit("a.py", "x = 1\n", "import os\nx = len(os.sep)\n").ok, True)
    if shutil.which("node"):
        chk("guard: js syntax regression", eg.guard_edit("a.js", "let a = 1;\n", "let a = ;\n").ok, False)

    # strip_think parity with the worker's helper
    try:
        import worker_robust as wr
        samples = ["a<think>b</think>c", "x<think>cut", "pre</think>post", "plain"]
        chk("strip_think parity with worker_robust", [fl.strip_think(s) for s in samples],
            [wr.strip_think(s) for s in samples])
    except ImportError:
        pass

    # stale bytecode: a write removes the file's cached .pyc
    d = mkws()
    pc = Path(d, "__pycache__"); pc.mkdir()
    Path(pc, "calc.cpython-314.pyc").write_bytes(b"stale")
    Path(pc, "other.cpython-314.pyc").write_bytes(b"keep")
    fl.Workspace(d, ["calc.py"]).write("calc.py", CALC)
    chk("write drops that file's stale .pyc only", sorted(x.name for x in pc.iterdir()), ["other.cpython-314.pyc"])

    # -------------------------------------------------------------- lane: green first try, SSE + non-stream
    for stream in (True, False):
        d = mkws()
        stub = Stub(lambda b, n: {"content": "The sign is wrong.\n\n" + FIX})
        res = lane(stub, d, stream=stream)
        tag = "SSE" if stream else "non-stream"
        chk(f"[{tag}] lane passes on first turn", (res["status"], res["reason"], len(res["samples"])), ("passed", None, 1))
        chk(f"[{tag}] fix left applied", "return a + b" in Path(d, "calc.py").read_text(), True)
        chk(f"[{tag}] exactly one model request", len(stub.requests), 1)
        chk(f"[{tag}] tokens accounted", res["tokens"], 150)
        b = stub.requests[0]
        chk(f"[{tag}] NO tools in request body",
            [k for k in ("tools", "tool_choice", "functions", "function_call") if k in b], [])
        chk(f"[{tag}] stream flag matches transport", bool(b.get("stream")), stream)
        chk(f"[{tag}] thinking OFF by default", b["chat_template_kwargs"], {"enable_thinking": False})
        chk(f"[{tag}] presence_penalty overridden to 0", b["presence_penalty"], 0.0)
        chk(f"[{tag}] api key sent as Bearer", stub.headers[0].get("Authorization"), "Bearer " + SECRET)
        chk(f"[{tag}] api key never in the result", SECRET in json.dumps(res), False)
        chk(f"[{tag}] api key never logged", SECRET in "\n".join(res["_logs"]), False)
        chk_in(f"[{tag}] prompt carries failing verify output", "add(2,3) should be 5", b["messages"][1]["content"])
        chk_in(f"[{tag}] prompt carries file + spec", "def add(a, b):", b["messages"][1]["content"])
        chk(f"[{tag}] system prompt is the SR one", "SEARCH/REPLACE" in b["messages"][0]["content"], True)
        stub.close()
        shutil.rmtree(d, ignore_errors=True)

    # -------------------------------------------------------------- thinking policy in the request body
    def body_for(thinking, model=MODEL):
        d = mkws()
        stub = Stub(lambda b, n: {"content": FIX})
        tr = fl.OpenAITransport(host=stub.url, api_key_fn=lambda: "k", sleep=lambda s: None)
        fl.run_lane(task(d), model=model, overrides={"thinking": thinking, "format": "sr", "k": 1},
                    transport=tr, log=lambda m: None)
        b = stub.requests[0]
        stub.close()
        shutil.rmtree(d, ignore_errors=True)
        return b
    b = body_for("off")
    chk("policy off: enable_thinking false, no reasoning_effort",
        (b["chat_template_kwargs"]["enable_thinking"], "reasoning_effort" in b, b["max_tokens"]), (False, False, 6144))
    b = body_for("on")
    chk("policy on: enable_thinking true, capped tokens, no effort",
        (b["chat_template_kwargs"]["enable_thinking"], "reasoning_effort" in b, b["max_tokens"]), (True, False, 12288))
    b = body_for("low")
    chk("policy low: enable_thinking true + reasoning_effort low",
        (b["chat_template_kwargs"]["enable_thinking"], b["reasoning_effort"]), (True, "low"))
    b = body_for("off", "gemma-4-26b-qat-4bit")
    chk("model without a thinking switch: no chat_template_kwargs", "chat_template_kwargs" in b, False)

    # -------------------------------------------------------------- reflection rounds
    d = mkws()
    FIX2 = sr("calc.py", "    return a * b", "    return a + b")   # against the CURRENT (round-1) file
    seq = [WRONG, FIX2]
    stub = Stub(lambda b, n: {"content": seq[min(n, len(seq) - 1)]})
    res = lane(stub, d)
    chk("reflection: second round fixes it", (res["status"], len(res["samples"]), len(stub.requests)), ("passed", 1, 2))
    r2 = stub.requests[1]["messages"]
    chk("reflection: history = system,user,assistant,user", [m["role"] for m in r2], ["system", "user", "assistant", "user"])
    chk_in("reflection: verify output fed back", "add(2,3) should be 5 but got 6", r2[-1]["content"])
    chk_in("reflection: CURRENT file shown (applied edit included)", "return a * b", r2[-1]["content"])
    chk("reflection rounds recorded", [r["verify_rc"] for r in res["samples"][0]["rounds"]], [1, 0])
    stub.close()

    # reflection budget: initial + 2 reflections = 3 requests per sample, then restart
    d = mkws()
    stub = Stub(lambda b, n: {"content": WRONG})
    res = lane(stub, d, overrides={"k": 1})
    chk("reflections capped at 2 (3 turns per sample)", len(stub.requests), 3)
    chk("exhausted result", (res["status"], res["reason"]), ("exhausted", "fixed_lane_exhausted"))
    stub.close()

    # -------------------------------------------------------------- apply failure -> reflection with Aider message
    d = mkws()
    seq = [sr("calc.py", "    return a -- b", "    return a + b"), FIX]
    stub = Stub(lambda b, n: {"content": seq[min(n, 1)]})
    res = lane(stub, d)
    chk("apply failure then recovery", res["status"], "passed")
    last = stub.requests[1]["messages"][-1]["content"]
    chk_in("apply failure fed back with did-you-mean", "Did you mean", last)
    chk_in("apply failure: none applied wording", "None of your edits could be applied", last)
    chk("no verify run when nothing applied", res["samples"][0]["rounds"][0]["verify_rc"], None)
    stub.close()

    # ambiguous match is a feedback round, not a silent wrong edit
    d = mkws(calc="def add(a, b):\n    return a - b\n\ndef sub(a, b):\n    return a - b\n")
    seq = [sr("calc.py", "    return a - b", "    return a + b"),
           sr("calc.py", "def add(a, b):\n    return a - b", "def add(a, b):\n    return a + b")]
    Path(d, "check.py").write_text("from calc import add\nassert add(2,3)==5\nprint('OK')\n")
    stub = Stub(lambda b, n: {"content": seq[min(n, 1)]})
    res = lane(stub, d)
    chk("ambiguous SEARCH refused then disambiguated", (res["status"], "matches 2 places" in
        stub.requests[1]["messages"][-1]["content"]), ("passed", True))
    chk("sub() untouched by the ambiguous attempt", "def sub(a, b):\n    return a - b" in Path(d, "calc.py").read_text(), True)
    stub.close()

    # -------------------------------------------------------------- syntax guard rejects, file not written
    d = mkws()
    seq = [BREAK, FIX]
    stub = Stub(lambda b, n: {"content": seq[min(n, 1)]})
    res = lane(stub, d)
    chk("guard rejection then recovery", res["status"], "passed")
    chk("guard rejection recorded", res["samples"][0]["rounds"][0]["guard_rejected"], 1)
    chk("guard rejection ran no verify", res["samples"][0]["rounds"][0]["verify_rc"], None)
    chk_in("guard rejection fed back", "REJECTED by the post-edit check", stub.requests[1]["messages"][-1]["content"])
    stub.close()

    # -------------------------------------------------------------- K-sample restart + selection
    d = mkws()
    seq = [WRONG, WRONG, WRONG, FIX]     # sample 0 burns 3 turns, sample 1 passes first try
    stub = Stub(lambda b, n: {"content": seq[min(n, 3)]})
    res = lane(stub, d)
    chk("K restart: second sample passes", (res["status"], len(res["samples"]), res["selected_index"]), ("passed", 2, 1))
    s1_first = stub.requests[3]["messages"]
    chk("fresh sample starts from the BASELINE file", ("return a - b" in s1_first[1]["content"],
                                                        "return a * b" in s1_first[1]["content"]), (True, False))
    chk("fresh sample has fresh history", [m["role"] for m in s1_first], ["system", "user"])
    temps = [r.get("temperature") for r in stub.requests]
    chk("temperature variation: sample 0 profile temp, sample 1 varied",
        (temps[0], temps[3]), (0.7, 0.7))
    stub.close()
    d = mkws()
    stub = Stub(lambda b, n: {"content": WRONG})
    res = lane(stub, d, overrides={"k": 4})
    chk("K=4: four fresh samples then exhaustion", (len(res["samples"]), len(stub.requests)), (4, 12))
    chk("temperature schedule across samples", sorted({r["temperature"] for r in stub.requests}), [0.7, 0.9, 1.0])
    stub.close()

    d = mkws()
    seq = [FIX, sr("calc.py", "    return a - b", "    return (a\n            + b)")]
    ws_stub = Stub(lambda b, n: {"content": seq[0] if n == 0 else
                                 sr("calc.py", "    return a - b", "    # sum\n    x = a\n    y = b\n    return x + y")})
    res = lane(ws_stub, d, overrides={"k": 2, "stop_at_first_pass": False})
    chk("stop_at_first_pass=False runs all K", len(res["samples"]), 2)
    chk("selection: both pass -> smallest diff wins", (res["status"], res["selected_index"]), ("passed", 0))
    chk("winner state left applied on disk", Path(d, "calc.py").read_text().count("return a + b"), 1)
    ws_stub.close()
    # selection unit
    S = lambda **k: dict({"index": 0, "passed": False, "applied_any": True, "fail_score": 5, "diff_lines": 4,
                          "hunks": 1, "tokens": 10}, **k)
    chk("select: pass beats fail", fl.select_best([S(index=0), S(index=1, passed=True, diff_lines=40)])["index"], 1)
    chk("select: smaller diff among passes", fl.select_best([S(index=0, passed=True, diff_lines=9),
                                                              S(index=1, passed=True, diff_lines=3)])["index"], 1)
    chk("select: fewer hunks on equal diff", fl.select_best([S(index=0, passed=True, hunks=3),
                                                              S(index=1, passed=True, hunks=1)])["index"], 1)
    chk("select: applied beats unapplied", fl.select_best([S(index=0, applied_any=False), S(index=1)])["index"], 1)
    chk("select: fewer failure lines", fl.select_best([S(index=0, fail_score=9), S(index=1, fail_score=2)])["index"], 1)
    chk("select: empty", fl.select_best([]), None)

    # -------------------------------------------------------------- exhaustion handoff
    d = mkws()
    base = Path(d, "calc.py").read_text()
    stub = Stub(lambda b, n: {"content": WRONG})
    res = lane(stub, d, overrides={"k": 2})
    h = res["handoff"]
    chk("handoff reason", h["reason"], "fixed_lane_exhausted")
    chk_in("handoff seed diff has the best attempt", "+    return a * b", h["seed_diff"])
    chk_in("handoff seed_files", "return a * b", h["seed_files"]["calc.py"])
    chk_in("handoff carries last verify output", "add(2,3) should be 5", h["last_verify_output"])
    chk("worktree restored to baseline on exhaustion", Path(d, "calc.py").read_text(), base)
    p = subprocess.run(["git", "apply", "--check", "-"], input=h["seed_diff"], capture_output=True, text=True, cwd=d)
    subprocess.run(["git", "init", "-q"], cwd=d)
    p = subprocess.run(["git", "apply", "--check", "-"], input=h["seed_diff"], capture_output=True, text=True, cwd=d)
    chk("seed diff applies with git apply", p.returncode, 0)
    stub.close()
    d = mkws()
    stub = Stub(lambda b, n: {"content": WRONG})
    res = lane(stub, d, overrides={"k": 1}, keep_state="best")
    chk("keep_state=best leaves the attempt applied", "a * b" in Path(d, "calc.py").read_text(), True)
    stub.close()

    d = mkws()
    stub = Stub(lambda b, n: {"content": "I think the bug is in add. No edits needed."})
    res = lane(stub, d, overrides={"k": 2})
    chk("never lands an edit -> apply_failed", (res["status"], res["reason"]), ("apply_failed", "fixed_lane_apply_failed"))
    chk_in("no-blocks feedback", "no SEARCH/REPLACE blocks", stub.requests[1]["messages"][-1]["content"])
    chk("apply_failed handoff has no seed but baseline output", (res["handoff"]["seed_diff"], "should be 5" in res["handoff"]["last_verify_output"]), ("", True))
    stub.close()

    # -------------------------------------------------------------- already green / ineligible do no model calls
    d = mkws(calc=CALC.replace("a - b", "a + b"))
    stub = Stub(lambda b, n: {"content": FIX})
    res = lane(stub, d)
    chk("already green: no model call", (res["status"], len(stub.requests)), ("already_green", 0))
    stub.close()
    d = mkws()
    stub = Stub(lambda b, n: {"content": FIX})
    tr = fl.OpenAITransport(host=stub.url, api_key_fn=lambda: "k")
    res = fl.run_lane(task(d, targets=["a", "b", "c"]), model=MODEL, transport=tr, log=lambda m: None)
    chk("ineligible: no model call", (res["status"], len(stub.requests)), ("ineligible", 0))
    stub.close()

    # -------------------------------------------------------------- whole-file lane
    d = mkws()
    whole = "calc.py\n```python\n" + CALC.replace("a - b", "a + b") + "```\n"
    stub = Stub(lambda b, n: {"content": whole})
    res = lane(stub, d, overrides={"format": "auto", "k": 1})
    chk("whole-file format auto for a small file", (res["status"], "ENTIRE new content" in stub.requests[0]["messages"][0]["content"]), ("passed", True))
    stub.close()
    d = mkws()
    stub = Stub(lambda b, n: {"content": whole, "finish_reason": "length"})
    res = lane(stub, d, overrides={"format": "auto", "k": 1})
    chk("whole-file truncated reply never written", (res["status"], Path(d, "calc.py").read_text() == CALC), ("apply_failed", True))
    stub.close()

    # -------------------------------------------------------------- thinking inline in content
    d = mkws()
    stub = Stub(lambda b, n: {"content": "<think>hmm the sign</think>\n" + FIX})
    res = lane(stub, d, overrides={"thinking": "on", "k": 1})
    chk("inline <think> stripped before parsing", res["status"], "passed")
    stub.close()

    # -------------------------------------------------------------- runaway guard interaction
    d = mkws()
    stub = Stub(lambda b, n: {"forever": "content"})
    t0 = time.time()
    res = lane(stub, d, overrides={"k": 1, "max_tokens_off": 100, "reflections": 0})
    chk("stream-forever (content) is cut", (time.time() - t0 < 20, res["samples"][0]["rounds"][0]["cut"]), (True, "content_cap"))
    chk("cut turn is a failed round, nothing written", (res["status"], Path(d, "calc.py").read_text() == CALC),
        ("apply_failed", True))
    stub.close()

    d = mkws()
    seq_h = lambda b, n: ({"forever": "reasoning"} if n == 0 else {"content": FIX})
    stub = Stub(seq_h)
    res = lane(stub, d, overrides={"thinking": "on", "k": 1, "reasoning_budget_chars": 2000})
    chk("runaway reasoning cut, next turn retried NON-thinking and passes",
        (res["status"], res["samples"][0]["rounds"][0]["cut"], stub.requests[0]["chat_template_kwargs"]["enable_thinking"],
         stub.requests[1]["chat_template_kwargs"]["enable_thinking"]), ("passed", "reasoning_budget", True, False))
    chk_in("cut notice in next prompt", "CUT OFF", stub.requests[1]["messages"][-1]["content"])
    stub.close()

    d = mkws()
    stub = Stub(lambda b, n: {"forever": "content"} if n == 0 else {"content": FIX})
    res = lane(stub, d, overrides={"thinking": "on", "k": 1, "reasoning_budget_chars": 1500, "max_tokens_think": 1000000})
    chk("inline-thinking runaway (no </think>) cut by the reasoning budget",
        res["samples"][0]["rounds"][0]["cut"], "reasoning_budget")
    stub.close()

    # degenerate repetition + wall time via the guard directly (fake clock)
    g = fl.RunawayGuard(10 ** 6, 10 ** 6, 100, clock=lambda: 0.0)
    cut = None
    for _ in range(200):
        cut = g.feed("content", "same repeated line here\n") or cut
        if cut:
            break
    chk("guard: degenerate repeated line cut", cut, "degenerate")
    now = [0.0]
    g = fl.RunawayGuard(10 ** 6, 10 ** 6, 30, clock=lambda: now[0])
    chk("guard: under wall cap ok", g.feed("content", "a"), None)
    now[0] = 31.0
    chk("guard: wall cap cut", g.feed("content", "b"), "wall_time")

    # -------------------------------------------------------------- transport failure paths
    d = mkws()
    stub = Stub(lambda b, n: {"http_error": 500})
    sleeps = []
    tr = fl.OpenAITransport(host=stub.url, api_key_fn=lambda: "k", sleep=sleeps.append, retries=2)
    res = fl.run_lane(task(d), model=MODEL, overrides={"k": 4, "format": "sr"}, transport=tr, log=lambda m: None)
    chk("5xx retried then transport_error, no further samples",
        (res["status"], res["reason"], len(res["samples"]), len(stub.requests), len(sleeps)),
        ("transport_error", "fixed_lane_transport_error", 1, 3, 2))
    stub.close()
    tr = fl.OpenAITransport(host="http://127.0.0.1:1", api_key_fn=lambda: "k", sleep=lambda s: None, retries=1)
    try:
        tr.complete({"model": "m", "messages": []})
        chk("connect refused raises TransportError", False)
    except fl.TransportError:
        chk("connect refused raises TransportError", True)

    # -------------------------------------------------------------- context assembly
    d = mkws(calc="from helper import util\n" + CALC, extra={"helper.py": "def util(x):\n    return x\n\nCONST_ONE = 1\n"})
    stub = Stub(lambda b, n: {"content": FIX})
    res = lane(stub, d, overrides={"k": 1})
    chk_in("related import skeleton included", "def util(x):", stub.requests[0]["messages"][1]["content"])
    chk("related file body NOT dumped", "return x" in stub.requests[0]["messages"][1]["content"], False)
    stub.close()
    d = mkws()
    t = task(d)
    try:
        fl.build_context(t, dict(fl.lane_config(MODEL), context_frac=0.00001), {"calc.py": CALC}, ["calc.py"], "boom", 1000)
        chk("context too large raises", False)
    except fl.ContextTooLarge:
        chk("context too large raises", True)

    # -------------------------------------------------------------- CLI
    d = mkws()
    tj = Path(d, "task.json")
    tj.write_text(json.dumps(task(d)))
    p = subprocess.run([sys.executable, str(SRC / "fixed_lane.py"), "eligible", "--task", str(tj)],
                       capture_output=True, text=True)
    chk("CLI eligible", (p.returncode, json.loads(p.stdout)["eligible"]), (0, True))

    print()
    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
