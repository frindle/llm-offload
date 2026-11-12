import json
import os

from docsum_ui_jobs import job_info

NOW = 1_800_000_000.0


def mk(root, jid, meta=None, status=None, age=100, pdf=False, md=False, status_age=10):
    d = root / jid
    d.mkdir(parents=True)
    (d / "meta.json").write_text(json.dumps(meta if meta is not None else {"src_name": "Lease.pdf", "bytes": 1234}))
    os.utime(d / "meta.json", (NOW - age, NOW - age))
    if status is not None:
        (d / "status.json").write_text(status if isinstance(status, str) else json.dumps(status))
        os.utime(d / "status.json", (NOW - status_age, NOW - status_age))
    if pdf:
        (d / "summary.pdf").write_bytes(b"%PDF-")
    if md:
        (d / "summary.md").write_text("x")
    return d


def test_done(tmp_path):
    mk(tmp_path, "aaaaaaaa", status={"ok": True, "pages": 7}, age=100, status_age=40, pdf=True, md=True)
    i = job_info(str(tmp_path), "aaaaaaaa", None, NOW)
    assert i["id"] == "aaaaaaaa" and i["status"] == "done" and i["code"] is None
    assert i["pages"] == 7 and i["elapsed"] == 60 and i["name"] == "Lease.pdf" and i["bytes"] == 1234
    assert i["has_pdf"] is True and i["has_md"] is True


def test_failed_has_no_downloads(tmp_path):
    mk(tmp_path, "bbbbbbbb", status={"ok": False, "code": "E_SCANNED_NO_OCR"}, pdf=True)
    i = job_info(str(tmp_path), "bbbbbbbb", "done", NOW)
    assert i["status"] == "failed" and i["code"] == "E_SCANNED_NO_OCR" and i["pages"] is None
    assert i["has_pdf"] is False and i["has_md"] is False


def test_running_elapsed_runs_to_now(tmp_path):
    mk(tmp_path, "dddddddd", age=90)
    i = job_info(str(tmp_path), "dddddddd", "running", NOW)
    assert i["status"] == "running" and i["elapsed"] == 90 and i["pages"] is None


def test_corrupt_files_do_not_raise(tmp_path):
    mk(tmp_path, "11111111", meta={"x": 1}, status="{not json")
    i = job_info(str(tmp_path), "11111111", "running", NOW)
    assert i["status"] == "running" and i["name"] == "" and i["bytes"] is None


def test_missing_dir_returns_none(tmp_path):
    assert job_info(str(tmp_path), "99999999", None, NOW) is None
