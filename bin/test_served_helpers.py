"""Frozen tests: served-model helpers. Run: python3 test_served_helpers.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_bypass as pb


def test_resolve_model():
    assert pb.resolve_model("B", ["a", "b"]) == "b"
    assert pb.resolve_model("zz", ["a", "b"]) == "a"
    assert pb.resolve_model(5, ["a"]) == "a"
    assert pb.resolve_model(5, []) is None


def test_served_model_ids():
    tmp_path = __import__('pathlib').Path(tempfile.mkdtemp())
    p = tmp_path / "loaded.json"
    p.write_text(json.dumps({"models": ["zeta", "", "alpha", 3]}))
    assert pb.served_model_ids(str(p)) == ["zeta", "alpha"]
    p.write_text("{torn")
    assert isinstance(pb.served_model_ids(str(p)), list)
    assert isinstance(pb.served_model_ids(str(tmp_path / "missing.json")), list)


def test_cached_served_ttl_and_empty_not_cached():
    t = [100.0]
    seen = []

    def loader():
        seen.append(1)
        return ["m"] if len(seen) != 3 else []
    kw = dict(ttl=30.0, now=lambda: t[0], loader=loader)
    assert pb.cached_served(**kw) == ["m"] and pb.cached_served(**kw) == ["m"] and len(seen) == 1
    t[0] += 31
    assert pb.cached_served(**kw) == ["m"] and len(seen) == 2
    t[0] += 31
    assert pb.cached_served(**kw) == [] and pb.cached_served(**kw) == ["m"] and len(seen) == 4


TESTS = [test_resolve_model, test_served_model_ids, test_cached_served_ttl_and_empty_not_cached]

if __name__ == '__main__':
    order = TESTS[::-1] if os.environ.get('TEST_ORDER') == 'reverse' else TESTS
    bad = 0
    for t in order:
        try:
            t()
            print(f'RESULT {t.__name__} PASS')
        except Exception as e:
            bad += 1
            print(f'RESULT {t.__name__} FAIL {type(e).__name__}: {e}')
    sys.exit(1 if bad else 0)
