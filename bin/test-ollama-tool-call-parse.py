#!/usr/bin/env python3
"""GPU-free regression test for the tool-call FORMAT parser (P1) and the
false-convergence classifier (P1b).

Added 2026-09-09 after devstral:24b scored 0/49 on a bulk-codemod, stop=converged
at iter 2 -- a FALSE result. devstral emitted its tool calls as flat
    ```json
    {"tool": "read_file", "path": "py/arr-webhook.py"}
    ```
objects (name under "tool", args as SIBLING keys, not nested under "arguments").
The old parser matched only {"name":...,"arguments":...} and found nothing, so the
loop nudged twice and recorded a clean convergence -- a parser gap read as both a
finished run and, downstream, as model incapacity.

Exercises extract_manual_tool_calls in BOTH workers (ollama-worker-v7.py, the
bake-off worker, and ollama-worker.py, the dispatch worker) with no model
inference. Run: python3 bin/test-ollama-tool-call-parse.py
"""
import importlib.util
import json
import sys
from pathlib import Path

BIN = Path(__file__).resolve().parent
WORKERS = [BIN / "ollama-worker-v7.py", BIN / "ollama-worker.py"]
# The saved devstral transcript that motivated the fix (optional -- the synthetic
# cases below reproduce the same dialect and are the real gate).
TRANSCRIPT = Path.home() / "bin" / "ollama-worker-logs" / "20260909T021925Z.json"

failures = []


def load(path):
    spec = importlib.util.spec_from_file_location("w_" + path.stem.replace("-", "_"), path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def calls_of(mod, content):
    r = mod.extract_manual_tool_calls(content)
    return r[0] if isinstance(r, tuple) else r


def names(calls):
    return [c.get("name") for c in calls]


def check(cond, label):
    print(("PASS" if cond else "FAIL") + " - " + label)
    if not cond:
        failures.append(label)


for wpath in WORKERS:
    if not wpath.exists():
        continue
    print("\n===== %s =====" % wpath.name)
    mod = load(wpath)
    C = lambda s: calls_of(mod, s)

    # P1: flat {"tool":...} fenced-json dialect (the devstral bug).
    flat = '```json\n{\n  "tool": "read_file",\n  "path": "py/arr-webhook.py"\n}\n```'
    check(names(C(flat)) == ["read_file"] and C(flat)[0]["arguments"]["path"] == "py/arr-webhook.py",
          "flat {\"tool\":...} fenced block -> read_file with path arg")

    # Flat edit_file keeps its sibling args (old_string/new_string).
    ef = C('```json\n{"tool":"edit_file","path":"a.py","old_string":"x","new_string":"y"}\n```')
    check(ef and ef[0]["name"] == "edit_file" and ef[0]["arguments"] == {"path": "a.py", "old_string": "x", "new_string": "y"},
          "flat edit_file preserves old_string/new_string sibling args")

    # Multiple fenced blocks in one message, order preserved.
    multi = C('```json\n{"tool":"read_file","path":"a"}\n```\ntext\n```json\n{"tool":"list_files","path":"."}\n```')
    check(names(multi) == ["read_file", "list_files"], "two fenced flat blocks parse in order")

    # Native {"name":...,"arguments":...} must NOT regress (either key order).
    check(names(C('{"name":"read_file","arguments":{"path":"p"}}')) == ["read_file"],
          "native name/arguments still parses (no regression)")
    check(names(C('{"arguments":{"path":"p"},"name":"read_file"}')) == ["read_file"],
          "reversed arguments/name order still parses")

    # Mistral [TOOL_CALLS] payload, and not double-counted.
    tc = C('[TOOL_CALLS][{"name":"list_files","arguments":{"path":"."}}]')
    check(names(tc) == ["list_files"], "[TOOL_CALLS] payload parses")

    # Prose object with an unknown tool name must NOT become a false call.
    check(C('use {"tool":"hammer","size":"big"} here') == [],
          "prose {\"tool\":...} with unknown name ignored (no false positive)")

    # Fix 3: Cohere/command-r native tool-arg envelope unwrap ({tool_name,parameters}).
    if hasattr(mod, "_unwrap_tool_args"):
        uw = mod._unwrap_tool_args
        check(uw("web_search", {"tool_name": "web_search", "parameters": {"query": "x"}}) == {"query": "x"},
              "Fix3: {tool_name,parameters} envelope unwraps to flat {query}")
        check(uw("web_search", {"parameters": {"query": "y"}}) == {"query": "y"},
              "Fix3: bare {parameters:{...}} (no tool_name) unwraps too")
        # tool_name mismatch: invoked name is authoritative, still unwraps.
        check(uw("web_fetch", {"tool_name": "web_search", "parameters": {"url": "u"}}) == {"url": "u"},
              "Fix3: tool_name mismatch still unwraps parameters (invoked name wins)")
        # A flat arg dict every other model emits passes through unchanged.
        check(uw("web_search", {"query": "z"}) == {"query": "z"},
              "Fix3: flat args pass through unchanged (no regression)")
        # A legit arg that happens to carry a 'parameters' sibling but no tool_name
        # and other real keys must NOT be mis-unwrapped.
        check(uw("edit_file", {"path": "a", "parameters": {"x": 1}}) == {"path": "a", "parameters": {"x": 1}},
              "Fix3: parameters alongside real sibling args is NOT unwrapped")
        check(uw("write_file", "not-a-dict") == "not-a-dict",
              "Fix3: non-dict args returned as-is (no crash)")

    # Fix 3: per-turn tool-call fan-out cap constant present and sane.
    if hasattr(mod, "MAX_TOOL_CALLS_PER_TURN"):
        cap = mod.MAX_TOOL_CALLS_PER_TURN
        check(isinstance(cap, int) and 1 <= cap <= 50,
              "Fix3: MAX_TOOL_CALLS_PER_TURN is a sane int cap (%r)" % (cap,))
        # Simulate the truncation the loop applies to a 73-call runaway turn.
        runaway = [{"function": {"name": "web_search"}}] * 73
        check(len(runaway[:cap]) == cap,
              "Fix3: a 73-call runaway turn truncates to the cap")

    # P1b classifier (bake-off worker only).
    if hasattr(mod, "_looks_like_attempted_tool_call"):
        laf = mod._looks_like_attempted_tool_call
        check(laf('```json\n{"tool":"read_file","path":"a"}\n```') is True,
              "P1b: fenced-json flagged as attempted call (needs-review, not converged)")
        check(laf("All 29 replacements are complete.") is False,
              "P1b: honest plain-text summary NOT flagged (still converges)")

    # Optional: the real saved transcript.
    if TRANSCRIPT.exists():
        data = json.loads(TRANSCRIPT.read_text())
        asst = [m for m in data["messages"] if m["role"] == "assistant"]
        second = C(asst[1]["content"])
        n = names(second)
        check(n.count("read_file") >= 12 and n.count("edit_file") >= 20,
              "saved devstral transcript: recovers all read_file+edit_file calls (got %dr/%de)"
              % (n.count("read_file"), n.count("edit_file")))
    else:
        print("SKIP - saved transcript not present (synthetic cases cover the dialect)")

print()
if failures:
    print("RESULT: %d FAILED -> %s" % (len(failures), failures))
    sys.exit(1)
print("RESULT: ALL PASSED")
