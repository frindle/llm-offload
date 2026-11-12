import json
import os

import bloomgauge_control as C

GOOD = """
class Unavailable(Exception):
    pass

def get(path, params=None):
    return {"path": path}

def set_manager(args):
    return args

def pick_model(args):
    return args
"""


def test_load_client_loads_a_complete_module(tmp_path):
    p = tmp_path / "agent_mcp.py"
    p.write_text(GOOD)
    mod = C.load_client(str(p))
    assert mod is not None
    assert mod.get("/api/x") == {"path": "/api/x"}
    assert issubclass(mod.Unavailable, Exception)


def test_load_client_returns_none_instead_of_raising(tmp_path):
    assert C.load_client(str(tmp_path / "missing.py")) is None
    partial = tmp_path / "partial.py"
    partial.write_text("def get(path, params=None):\n    return {}\n")
    assert C.load_client(str(partial)) is None
    broken = tmp_path / "broken.py"
    broken.write_text("def get(:\n")
    assert C.load_client(str(broken)) is None
    boom = tmp_path / "boom.py"
    boom.write_text("raise RuntimeError('import time failure')\n")
    assert C.load_client(str(boom)) is None


def test_lease_load_missing_and_valid(tmp_path):
    assert C.lease_load(str(tmp_path / "none.json")) is None
    p = tmp_path / "lease.json"
    lease = {"state": "HANDBACK", "since": 5.0, "prior_manager_on": False}
    p.write_text(json.dumps(lease))
    assert C.lease_load(str(p)) == lease


def test_lease_load_corrupt_hands_back(tmp_path):
    for i, text in enumerate(["{not json", "[1, 2]", json.dumps({"state": "WEIRD"}), ""]):
        p = tmp_path / f"lease{i}.json"
        p.write_text(text)
        os.utime(p, (1234.0, 1234.0))
        got = C.lease_load(str(p))
        assert got == {"state": "DRAIN", "idle_since": 0, "prior_manager_on": True, "since": 1234.0, "corrupt": True}, text
