#!/usr/bin/env python3
"""Permanent, re-runnable tests for the ollama-worker parser + read_file cap
that v9's pre-dispatch checklist item 3 depends on. Fable's second-review
nitpick #1: the original tests were ephemeral (run inline, never saved), so
"unit-tested" was unverifiable after the fact. Run: python3 test-worker-parsers.py

Covers both worker copies (v7 = the one the driver runs; v8 = kept in parity)
and asserts they are byte-identical in the parser + tool functions, which is
the precondition for pre-dispatch checklist item 8's v7==v8 checksum assert.
"""
import importlib.util as ilu
import sys
import tempfile
import os
from pathlib import Path

BIN = Path.home() / "bin"


def load(name):
    spec = ilu.spec_from_file_location(name.replace("-", "_").replace(".", "_"), BIN / name)
    mod = ilu.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def check(cond, msg):
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def test_worker(m, label):
    # --- Qwen3-Coder XML dialect (R2 fix) ---
    r = m.extract_manual_tool_calls(
        "<tool_call><function=read_file><parameter=path>app/page.tsx</parameter></function></tool_call>")
    check(r == [{"name": "read_file", "arguments": {"path": "app/page.tsx"}}],
          f"{label}: XML single call parsed")

    r = m.extract_manual_tool_calls(
        "<function=write_file><parameter=path>a.py</parameter>"
        "<parameter=content>line1\nline2</parameter></function>")
    check(r and r[0]["arguments"]["content"] == "line1\nline2",
          f"{label}: XML multi-param, multi-line content preserved")

    # JSON present -> XML fallback must NOT fire (no double-count)
    r = m.extract_manual_tool_calls('{"name":"list_files","arguments":{"path":"."}}')
    check(r == [{"name": "list_files", "arguments": {"path": "."}}],
          f"{label}: JSON-first, XML fallback suppressed")

    # --- read_file cap (R1 fix) ---
    check(m.READ_FILE_MAX_LINES == 600, f"{label}: READ_FILE_MAX_LINES == 600")
    d = tempfile.mkdtemp()
    big = os.path.join(d, "big.py")
    open(big, "w").write("\n".join(f"line{i}" for i in range(5000)))
    out = m.tool_read_file(Path(d), {"path": "big.py"})
    check(out.splitlines()[0] == "line0" and "showing lines 1-600 of 5000" in out,
          f"{label}: big file windowed to 600 with continuation hint")
    out2 = m.tool_read_file(Path(d), {"path": "big.py", "offset": 600})
    check(out2.splitlines()[0] == "line600", f"{label}: paging with offset works")
    small = os.path.join(d, "s.py")
    open(small, "w").write("a\nb\nc\n")
    check(m.tool_read_file(Path(d), {"path": "s.py"}) == "a\nb\nc\n",
          f"{label}: small file returned verbatim")


def test_parity(v7, v8):
    import inspect
    for fn in ("extract_manual_tool_calls", "extract_qwen_xml_tool_calls", "tool_read_file"):
        a = inspect.getsource(getattr(v7, fn))
        b = inspect.getsource(getattr(v8, fn))
        check(a == b, f"parity: {fn} byte-identical between v7 and v8")


if __name__ == "__main__":
    v7 = load("ollama-worker-v7.py")
    v8 = load("ollama-worker-v8.py")
    test_worker(v7, "v7")
    test_worker(v8, "v8")
    test_parity(v7, v8)
    print("\nALL PARSER + READ-CAP TESTS PASS")
