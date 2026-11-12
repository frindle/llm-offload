import json
import os

from docsum_ui_jobs import read_json

NOW = 1_800_000_000.0


def test_valid_dict(tmp_path):
    p = tmp_path / "a.json"
    p.write_text('{"ok": true, "pages": 3}')
    assert read_json(str(p)) == {"ok": True, "pages": 3}


def test_non_dict_json_gives_empty(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("[1, 2]")
    assert read_json(str(p)) == {}


def test_invalid_json_and_missing_file_give_empty(tmp_path):
    p = tmp_path / "a.json"
    p.write_text("{not json")
    assert read_json(str(p)) == {}
    assert read_json(str(tmp_path / "nope.json")) == {}
