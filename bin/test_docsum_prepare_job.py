import json
import os
import stat
import pytest
import docsum_cli as dc
from docsum_make_test_pdf import make_pdf


def mode(p):
    return stat.S_IMODE(os.stat(p).st_mode)


def test_private_layout_and_modes(tmp_path):
    src = tmp_path / "Secret Name.pdf"
    make_pdf(str(src), ["hello"])
    root = tmp_path / "private"
    j = dc.prepare_job(str(src), "scenario CANARY text", str(root), job_id="ab12cd34")
    d = root / "ab12cd34"
    assert j["job_id"] == "ab12cd34" and j["dir"] == str(d) and j["task_file"] == str(d / "task.json")
    assert (d / "input.pdf").read_bytes() == src.read_bytes()
    assert (d / "scenario.txt").read_text() == "scenario CANARY text"
    assert mode(root) == 0o700 and mode(d) == 0o700
    for n in ("input.pdf", "scenario.txt", "meta.json", "task.json"):
        assert mode(d / n) == 0o600


def test_task_file_is_opaque_and_meta_has_the_name(tmp_path):
    src = tmp_path / "Secret Name.pdf"
    make_pdf(str(src), ["hello"])
    j = dc.prepare_job(str(src), "scenario CANARY text", str(tmp_path / "p"), job_id="ab12cd34")
    task = json.loads(open(j["task_file"]).read())
    assert task == {"job": "ab12cd34", "root": str(tmp_path / "p")}
    raw = open(j["task_file"]).read()
    assert "Secret" not in raw and "CANARY" not in raw
    assert json.loads(open(os.path.join(j["dir"], "meta.json")).read())["src_name"] == "Secret Name.pdf"


def test_random_id_and_existing_job_refused(tmp_path):
    src = tmp_path / "a.pdf"
    make_pdf(str(src), ["hello"])
    a = dc.prepare_job(str(src), "s", str(tmp_path / "p"))
    b = dc.prepare_job(str(src), "s", str(tmp_path / "p"))
    assert a["job_id"] != b["job_id"] and len(a["job_id"]) == 8
    with pytest.raises(ValueError) as ei:
        dc.prepare_job(str(src), "s", str(tmp_path / "p"), job_id=a["job_id"])
    assert str(ei.value) == "E_JOB_EXISTS"
