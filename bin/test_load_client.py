import bloomgauge_control as C

STUB = """
class Unavailable(Exception):
    pass

def get(path, params=None):
    return {"path": path}

def set_manager(args):
    return args

def pick_model(args):
    return args
"""


def _outcome(mod):
    return None if mod is None else mod.__file__


def test_empty_string_path_behaves_like_none(tmp_path):
    empty = _outcome(C.load_client(""))
    default = _outcome(C.load_client(None))
    assert empty == default, "empty path must fall back like None: %r vs %r" % (empty, default)


def test_explicit_nonexistent_path_returns_none(tmp_path):
    assert C.load_client(str(tmp_path / "missing.py")) is None


def test_explicit_path_does_not_fall_back(tmp_path):
    good = tmp_path / "agent_mcp.py"
    good.write_text(STUB)
    mod = C.load_client(str(good))
    assert mod is not None and mod.__file__ == str(good)
    assert C.load_client(str(tmp_path / "other.py")) is None
