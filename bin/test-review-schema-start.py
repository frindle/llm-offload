#!/usr/bin/env python3
"""Review-test arms can start the Darkbloom schema ladder lower and send reasoning_effort (2026-11-09).

The fair Gemma re-test (offload plan step 8) needs two request changes that no env switch offers:
  - schema_start="none": skip the strict and relaxed json_schema rungs; JSON is asked for in the prompt only;
  - reasoning_effort="medium": Darkbloom 0.10.11 makes Gemma 4 think when this field is sent.
code-review-agent.py takes them from the task file (review_schema_start, review_reasoning_effort).

Pins, with darkbloom_chat._post stubbed (no network):
  (a) default call: first request carries a strict json_schema and no reasoning_effort (unchanged behaviour);
  (b) schema_start="none": one request, no response_format, the JSON contract is in the system prompt;
  (c) schema_start="relaxed": first request carries the relaxed (non-strict) schema;
  (d) reasoning_effort="medium" is sent as-is, and only when given;
  (e) an unknown schema_start value is ignored (strict first);
  (f) code-review-agent Model.chat passes schema_start/reasoning_effort through to darkbloom_chat.chat;
  (g) the agent reads only exact task-file values (source check of the parse lines).

Run: python3 test-review-schema-start.py [--src-dir DIR]
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

FAILS = []
OK_MARK = "REVIEW_SCHEMA_START_OK"


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(name, path):
    ld = importlib.machinery.SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.modules[name] = m
    ld.exec_module(m)
    return m


SCHEMA = {"type": "object", "properties": {"findings": {"type": "array"}}, "required": ["findings"]}


def run_chat(dbk, **kw):
    sent = []

    def fake_post(base, wire, timeout, sleep=None, urlopen=None):
        sent.append(json.loads(json.dumps(wire)))
        return {"choices": [{"message": {"content": '{"findings": []}'}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5}}

    dbk._post = fake_post
    dbk.base_url = lambda: "http://127.0.0.1:8000"
    content, fin = dbk.chat("http://127.0.0.1:8000", "qwen3.6-35b-a3b-vl-mtp-mxfp8", "SYS", "USER",
                            schema=SCHEMA, **kw)
    return sent, content


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-dir", default=str(Path(__file__).resolve().parent))
    a = ap.parse_args()
    src = Path(a.src_dir)
    home = tempfile.mkdtemp(prefix="schema-start-")
    os.environ["HOME"] = home
    sys.path.insert(0, str(src))
    dbk = load("darkbloom_chat", src / "darkbloom_chat.py")

    sent, _ = run_chat(dbk)
    rf = sent[0].get("response_format") or {}
    check("(a) default: first rung is strict json_schema", (rf.get("type"), rf.get("json_schema", {}).get("strict")),
          ("json_schema", True))
    check("(a) default: no reasoning_effort", "reasoning_effort" in sent[0], False)

    sent, content = run_chat(dbk, schema_start="none")
    check("(b) none: one request", len(sent), 1)
    check("(b) none: no response_format", "response_format" in sent[0], False)
    check("(b) none: JSON contract in the system prompt",
          "Respond with ONLY a single JSON object" in sent[0]["messages"][0]["content"], True)
    check("(b) none: content returned", json.loads(content), {"findings": []})

    sent, _ = run_chat(dbk, schema_start="relaxed")
    rf = sent[0].get("response_format") or {}
    check("(c) relaxed: first rung is the non-strict schema", rf.get("json_schema", {}).get("strict"), False)

    sent, _ = run_chat(dbk, schema_start="none", reasoning_effort="medium")
    check("(d) reasoning_effort sent", sent[0].get("reasoning_effort"), "medium")

    sent, _ = run_chat(dbk, schema_start="bogus")
    rf = sent[0].get("response_format") or {}
    check("(e) unknown schema_start ignored", rf.get("json_schema", {}).get("strict"), True)

    agent_src = src / "code-review-agent.py"
    cra = load("code_review_agent_t", agent_src)
    seen = {}

    def fake_chat(host, model, system, user, schema=None, **kw):
        seen.update(kw)
        return '{"findings": []}', "stop"

    cra._dbk.chat = fake_chat
    cra._dbk.is_darkbloom = lambda h: True
    m = cra.Model("http://127.0.0.1:8000", "gemma-4-26b", 32768)
    m.schema_start, m.reasoning_effort = "none", "medium"
    m.chat("SYS", "USER", schema=SCHEMA)
    check("(f) Model.chat passes schema_start", seen.get("schema_start"), "none")
    check("(f) Model.chat passes reasoning_effort", seen.get("reasoning_effort"), "medium")
    m2 = cra.Model("http://127.0.0.1:8000", "gemma-4-26b", 32768)
    seen.clear()
    m2.chat("SYS", "USER", schema=SCHEMA)
    check("(f) Model without the attributes passes None", (seen.get("schema_start"), seen.get("reasoning_effort")),
          (None, None))

    text = agent_src.read_text()
    check("(g) task-file schema_start limited to relaxed/none",
          'spec.get("review_schema_start") in ("relaxed", "none")' in text, True)
    check("(g) task-file effort limited to low/medium/high",
          'spec.get("review_reasoning_effort") in ("low", "medium", "high")' in text, True)

    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(OK_MARK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
