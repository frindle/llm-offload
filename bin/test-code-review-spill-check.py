#!/usr/bin/env python3
"""code-review-agent.py's zero-spill check: any off-GPU bytes abort the run before a real call.

Stdlib only; run as a script (the test gate does, on claude-sandbox).
"""
import importlib.util
import io
import json
import os
import sys
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
_spec = importlib.util.spec_from_file_location("cra", os.path.join(HERE, "code-review-agent.py"))
cra = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cra)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(ps_models, calls):
    def urlopen(req, timeout=None):
        url = req if isinstance(req, str) else req.full_url
        calls.append(url)
        if url.endswith("/api/generate"):
            return _Resp(json.dumps({"done": True, "done_reason": "load"}).encode())
        if url.endswith("/api/ps"):
            return _Resp(json.dumps({"models": ps_models}).encode())
        if url.endswith("/api/chat"):
            return _Resp(json.dumps({"message": {"content": "ok"}, "done_reason": "stop",
                                     "prompt_eval_count": 10, "eval_count": 2}).encode())
        raise AssertionError(f"unexpected url {url}")
    return urlopen


def model(name="qwen3:14b", ctx=16384):
    return cra.Model("http://gpu-host:11434", name, ctx)


class SpillCheck(unittest.TestCase):
    def run_with(self, ps_models):
        calls = []
        return mock.patch.object(cra.urllib.request, "urlopen", fake_urlopen(ps_models, calls)), calls

    def test_spilled_model_aborts_before_any_chat(self):
        p, calls = self.run_with([{"name": "qwen3:14b", "size": 13_530_000_000, "size_vram": 10_790_000_000}])
        with p, self.assertRaisesRegex(RuntimeError, "SPILLOVER"):
            model().chat("sys", "user")
        self.assertFalse(any(u.endswith("/api/chat") for u in calls))

    def test_one_byte_off_gpu_still_aborts(self):
        p, _ = self.run_with([{"name": "qwen3:14b", "size": 1000, "size_vram": 999}])
        with p, self.assertRaisesRegex(RuntimeError, "SPILLOVER"):
            model().chat("sys", "user")

    def test_fully_on_gpu_runs_and_checks_once(self):
        p, calls = self.run_with([{"name": "ornith:9b", "size": 9_000_000_000, "size_vram": 9_000_000_000}])
        with p:
            m = model("ornith:9b", 32768)
            self.assertEqual(m.chat("sys", "user"), "ok")
            self.assertEqual(m.chat("sys", "again"), "ok")
        self.assertEqual(sum(u.endswith("/api/ps") for u in calls), 1)
        self.assertEqual(sum(u.endswith("/api/chat") for u in calls), 2)

    def test_untagged_name_matches_latest(self):
        p, _ = self.run_with([{"name": "mistral:latest", "size": 5, "size_vram": 5}])
        with p:
            self.assertEqual(model("mistral", 32768).chat("sys", "user"), "ok")

    def test_model_missing_from_ps_aborts(self):
        p, _ = self.run_with([{"name": "other:7b", "size": 5, "size_vram": 5}])
        with p, self.assertRaisesRegex(RuntimeError, "not in"):
            model().chat("sys", "user")

    def test_unreachable_host_aborts(self):
        def boom(req, timeout=None):
            raise OSError("connection refused")
        with mock.patch.object(cra.urllib.request, "urlopen", boom), \
                self.assertRaisesRegex(RuntimeError, "could not load"):
            model().chat("sys", "user")


if __name__ == "__main__":
    unittest.main()
