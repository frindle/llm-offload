#!/usr/bin/env python3
"""The queue API's REAL _TOKS_RE/_USAGE_RE against the worker's and the mini-runner's log line formats."""
import importlib.util, re, subprocess, sys
from importlib.machinery import SourceFileLoader
ld = SourceFileLoader("oqa", "/Users/user/bin/ollama-queue-api.py")
m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oqa", ld)); ld.exec_module(m)
assert m._TOKS_RE.findall("x (12.5 tok/s)") == ["12.5"]
assert m._TOKS_RE.findall("[mini-runner] step 3: 50 in / 20 out (10.0 tok/s wall incl. prefill+tools, 2s since prev reply)") == ["10.0"]
old = "[worker] USAGE SUMMARY: prompt_tokens=10 completion_tokens=20 tok_s=5.0 decode_tok_s=7.5 calls=3"
new = "[worker] USAGE SUMMARY: prompt_tokens=10 completion_tokens=20 tok_s=5.0 decode_tok_s=None tok_s_basis=wall-incl-prefill calls=3"
assert m._USAGE_RE.findall(old) == [("10", "20", "5.0", "7.5", "3")]
assert m._USAGE_RE.findall(new) == [("10", "20", "5.0", "None", "3")]
import tempfile, os
for line, dts in ((old, 7.5), (new, None)):
    with tempfile.NamedTemporaryFile("w", suffix=".log", delete=False) as f:
        f.write("noise\n" + line + "\n")
    u = m._usage_summary(f.name); os.unlink(f.name)
    assert u == {"prompt_tokens": 10, "completion_tokens": 20, "tok_s": 5.0, "decode_tok_s": dts, "calls": 3}, u
# the runner's own output must match the same regexes
spec = importlib.util.spec_from_file_location("R", "/Users/user/bin/mini-swe-runner.py"); R = importlib.util.module_from_spec(spec); spec.loader.exec_module(R)
U = R.Usage(); U.rows = [(1, 10, 20)]; U.secs = 4
assert m._USAGE_RE.findall(U.summary()), U.summary()
assert m._TOKS_RE.findall(R.tok_report([(100, 50, 20)], 98, 0)[0]) == ["10.0"]
print("api regex tests ok")
