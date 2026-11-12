import json

import bloomgauge_control as C


def test_lease_save_writes_sorted_json_and_creates_dir(tmp_path):
    p = tmp_path / "sub" / "lease.json"
    C.lease_save(str(p), {"since": 1.0, "state": "HELD", "a": [1]})
    assert json.loads(p.read_text()) == {"since": 1.0, "state": "HELD", "a": [1]}
    assert p.read_text() == json.dumps({"a": [1], "since": 1.0, "state": "HELD"})
    C.lease_save(str(p), {"state": "DRAIN"})
    assert json.loads(p.read_text()) == {"state": "DRAIN"}
    assert sorted(x.name for x in p.parent.iterdir()) == ["lease.json"]


def test_lease_save_none_removes_and_tolerates_missing(tmp_path):
    p = tmp_path / "lease.json"
    p.write_text("{}")
    C.lease_save(str(p), None)
    assert not p.exists()
    C.lease_save(str(p), None)
    assert not p.exists()


def test_ledger_append_adds_sorted_lines(tmp_path):
    p = tmp_path / "d" / "ledger.jsonl"
    C._ledger_append(str(p), {"b": 2, "a": 1})
    C._ledger_append(str(p), {"z": None})
    assert p.read_text() == '{"a": 1, "b": 2}\n{"z": null}\n'
    q = tmp_path / "old.jsonl"
    q.write_text('{"keep": true}\n')
    C._ledger_append(str(q), {"n": 3})
    assert q.read_text().splitlines() == ['{"keep": true}', '{"n": 3}']
