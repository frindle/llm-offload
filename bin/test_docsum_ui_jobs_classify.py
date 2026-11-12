import json
import os

from docsum_ui_jobs import classify

NOW = 1_800_000_000.0


def test_done_with_pages():
    assert classify({"ok": True, "pages": 7}, None, 5) == ("done", None, 7)
    assert classify({"ok": True}, "failed", 5) == ("done", None, None)


def test_failed_code_only():
    assert classify({"ok": False, "code": "E_SCANNED_NO_OCR"}, "done", 5) == ("failed", "E_SCANNED_NO_OCR", None)


def test_untrusted_code_is_masked():
    assert classify({"ok": False, "code": "secret words in here"}, None, 5) == ("failed", "E_UNKNOWN", None)
    assert classify({"ok": False}, None, 5) == ("failed", "E_UNKNOWN", None)


def test_queue_status_without_status_file():
    assert classify({}, "running", 5) == ("running", None, None)
    for qs in ("failed", "error", "needs_opus", "cancelled"):
        assert classify({}, qs, 5) == ("failed", "E_JOB_FAILED", None)
    for qs in ("queued", "paused", None):
        assert classify({}, qs, 5) == ("queued", None, None)


def test_lost_when_no_queue_row_and_old():
    assert classify({}, None, 7200) == ("failed", "E_LOST", None)
    assert classify({}, "running", 7200) == ("running", None, None)
