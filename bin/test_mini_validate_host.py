"""The remote validate job runs in the submit's own lane: mini-submit --host unraid must not validate with host auto
(2026-11-15: an Unraid smoke job's validate job waited behind a Darkbloom sandbox hold)."""
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import mini_validate_remote as MVR  # noqa: E402
import queue_remote as QR  # noqa: E402


def _load_mini_submit():
    loader = importlib.machinery.SourceFileLoader("mini_submit_hosttest", str(HERE / "mini-submit"))
    spec = importlib.util.spec_from_loader(loader.name, loader)
    mod = importlib.util.module_from_spec(spec)
    loader.exec_module(mod)
    return mod


class RemoteValidateHost(unittest.TestCase):
    def _run(self, **kw):
        seen = {}

        def fake_build(spec_path, job, runner, pkg, runner_args=None):
            seen.update(job)
            raise QR.RemoteError("stop after build")

        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "spec.json"
            sp.write_text(json.dumps({"task": "x"}))
            with mock.patch.object(QR, "check_paths", lambda *a: None), \
                    mock.patch.object(MVR, "snapshot", lambda p: ("", {})), \
                    mock.patch.object(MVR.queue_config, "queue_api", lambda: ("http://q.invalid:1", "")), \
                    mock.patch.object(MVR.queue_config, "queue_api_headers", lambda: {}), \
                    mock.patch.object(QR, "build_package", fake_build):
                ok, jid, out = MVR.remote_validate(sp, "qwen3:14b", "lbl", say=lambda *_: None, **kw)
        self.assertFalse(ok)
        self.assertIn("stop after build", out)
        return seen

    def test_host_passed_through(self):
        self.assertEqual(self._run(host="unraid")["host"], "unraid")

    def test_default_stays_auto(self):
        self.assertEqual(self._run()["host"], "auto")

    def _poll(self, rows_seq, grace=None):
        """remote_validate's wait loop against a scripted queue: rows_seq is the queue_jobs() answer per poll."""
        seq = iter(rows_seq)
        last = []

        def fake_jobs():
            nonlocal last
            last = next(seq, last)
            return last

        with tempfile.TemporaryDirectory() as td:
            sp = Path(td) / "spec.json"
            sp.write_text(json.dumps({"task": "x"}))
            patches = [mock.patch.object(QR, "check_paths", lambda *a: None),
                       mock.patch.object(MVR, "snapshot", lambda p: ("", {})),
                       mock.patch.object(MVR.queue_config, "queue_api", lambda: ("http://q.invalid:1", "")),
                       mock.patch.object(MVR.queue_config, "queue_api_headers", lambda: {}),
                       mock.patch.object(QR, "build_package", lambda *a, **k: None),
                       mock.patch.object(QR, "upload", lambda *a: {"id": "abc123def456"}),
                       mock.patch.object(MVR.MFR, "queue_jobs", fake_jobs),
                       mock.patch.object(QR, "fetch_result", mock.Mock(side_effect=QR.RemoteError("fetched"))),
                       mock.patch.dict(os.environ, {"MINI_FINISH_POLL": "0"})]
            if grace is not None:
                patches.append(mock.patch.object(MVR, "VANISH_GRACE_S", grace))
            for pt in patches:
                pt.start()
            try:
                return MVR.remote_validate(sp, "m", "lbl", say=lambda *_: None, timeout=5)
            finally:
                for pt in reversed(patches):
                    pt.stop()

    def test_cancelled_job_fails_fast(self):
        live = [{"id": "abc123def456", "status": "pending"}]
        ok, jid, out = self._poll([live, live, []])
        self.assertFalse(ok)
        self.assertIn("left the queue without finishing", out)

    def test_never_listed_job_fails_after_grace(self):
        ok, jid, out = self._poll([[]], grace=0)
        self.assertIn("left the queue without finishing", out)

    def test_finished_job_still_fetched(self):
        live = [{"id": "abc123def456", "status": "pending"}]
        ok, jid, out = self._poll([live, [{"id": "abc123def456", "status": "done", "exit_code": 0}]])
        self.assertIn("fetched", out)   # reached fetch_result, not the vanish branch

    def test_c5_remote_uses_submit_host(self):
        ms = _load_mini_submit()
        calls = {}

        def fake_rv(spec_path, model, label, host="auto", **kw):
            calls["host"] = host
            return True, "abc123def456", ""

        ctx = ms.Ctx()
        with tempfile.TemporaryDirectory() as td:
            ctx.spec_dir = Path(td)
            ctx.spec_path = Path(td) / "spec.json"
            ctx.model, ctx.label = "qwen3:14b", "lbl"
            for rest, want in ((["--host", "unraid"], "unraid"), (["--host=studio"], "studio"), ([], "auto"),
                               (["--no-such-flag"], "auto")):
                ctx.rest = rest
                with mock.patch.object(MVR, "remote_validate", fake_rv), mock.patch.object(MVR, "marked_job", lambda p: ""):
                    ms.c5_remote(ctx)
                self.assertEqual(calls["host"], want, rest)

    def test_c5_remote_skips_auto_spec(self):
        """An auto spec has no frozen test: no validate job (mini-validate crashed on it with KeyError 'test_file')."""
        ms = _load_mini_submit()
        ctx = ms.Ctx()
        ctx.spec, ctx.rest = {"mode": "auto", "task": "/x/TASK.md", "allowed": []}, ["--host", "unraid"]
        boom = mock.Mock(side_effect=AssertionError("no validate job for an auto spec"))
        with mock.patch.object(MVR, "remote_validate", boom), mock.patch.object(MVR, "marked_job", boom):
            out = ms.c5_remote(ctx)
        self.assertIn("auto spec", out)
        boom.assert_not_called()

    def test_c5_remote_still_validates_two_phase(self):
        ms = _load_mini_submit()
        ctx = ms.Ctx()
        with tempfile.TemporaryDirectory() as td:
            ctx.spec_dir, ctx.spec_path = Path(td), Path(td) / "spec.json"
            ctx.spec, ctx.rest, ctx.model, ctx.label = {"mode": "two-phase", "test_file": "t.py"}, [], "m", "lbl"
            rv = mock.Mock(return_value=(True, "abc123def456", ""))
            with mock.patch.object(MVR, "remote_validate", rv), mock.patch.object(MVR, "marked_job", lambda p: ""):
                ms.c5_remote(ctx)
        rv.assert_called_once()


if __name__ == "__main__":
    unittest.main()
