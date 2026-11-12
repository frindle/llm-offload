import json
import os

from docsum_ui_jobs import list_jobs

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


def test_list_newest_first_and_skips_junk(tmp_path):
    mk(tmp_path, "aaaaaaaa", age=500, status={"ok": True, "pages": 1})
    mk(tmp_path, "bbbbbbbb", age=50)
    (tmp_path / "not-an-id").mkdir()
    (tmp_path / "cccccccc").mkdir()
    (tmp_path / "stray.txt").write_text("x")
    rows = [{"label": "docsum-bbbbbbbb", "status": "running"}, {"label": "other-job", "status": "running"}]
    out = list_jobs(str(tmp_path), rows, NOW)
    assert [j["id"] for j in out] == ["bbbbbbbb", "aaaaaaaa"]
    assert out[0]["status"] == "running" and out[1]["status"] == "done"


def test_list_missing_root_and_no_rows(tmp_path):
    assert list_jobs(str(tmp_path / "nope"), [], NOW) == []
    mk(tmp_path, "aaaaaaaa", age=10)
    assert [j["status"] for j in list_jobs(str(tmp_path), None, NOW)] == ["queued"]
