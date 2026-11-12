"""Frozen tests: prepare_request sets model from the served list. Run: python3 test_prepare_request.py"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import plex_bypass as pb

TOK = "t0k"
AUTH = "Bearer " + TOK
MSG = [{"role": "user", "content": "hi"}]


def _go(d, served, token=TOK, auth=AUTH):
    raw = d if isinstance(d, bytes) else json.dumps(d).encode()
    return pb.prepare_request(auth, raw, token, served=served)


def test_default_served_source_is_loaded_models_file_when_no_served_arg():
    home = tempfile.mkdtemp()
    os.makedirs(os.path.join(home, ".darkbloom"))
    with open(os.path.join(home, ".darkbloom", "loaded-models.json"), "w") as fh:
        json.dump({"models": ["a", "qwen-x"]}, fh)
    orig = os.environ.get("HOME")
    os.environ["HOME"] = home
    try:
        st, out = pb.prepare_request(AUTH, json.dumps({"model": "nope", "messages": MSG}).encode(), TOK)
    finally:
        os.environ["HOME"] = orig
    assert st == 200 and json.loads(out).get("model") == "a"


def test_served_model_kept_case_insensitively_and_other_keys_untouched():
    st, out = _go({"model": "QWEN-X", "messages": MSG, "temperature": 0.2}, lambda: ["a", "qwen-x"])
    d = json.loads(out)
    assert st == 200 and d["model"] == "qwen-x" and d["messages"] == MSG and d["temperature"] == 0.2


def test_unserved_or_absent_or_nonstring_model_becomes_first_served():
    for m in ({"model": "gpt-9"}, {}, {"model": 7}, {"model": None}):
        st, out = _go({**m, "messages": MSG}, lambda: ["first", "second"])
        assert st == 200 and json.loads(out)["model"] == "first", m


def test_none_served_is_503_and_auth_400_413_never_ask_for_served():
    assert _go({"model": "m", "messages": MSG}, lambda: []) == (503, b"no model served")
    calls = []

    def served():
        calls.append(1)
        return ["m1"]
    assert _go({"messages": MSG}, served, auth="Bearer bad")[0] == 401
    assert _go(b"nope", served)[0] == 400
    assert _go({"messages": []}, served)[0] == 400
    assert pb.prepare_request(AUTH, b"x" * (pb.MAX_BODY + 1), TOK, served=served)[0] == 413
    assert calls == []



TESTS = [test_default_served_source_is_loaded_models_file_when_no_served_arg, test_served_model_kept_case_insensitively_and_other_keys_untouched, test_unserved_or_absent_or_nonstring_model_becomes_first_served, test_none_served_is_503_and_auth_400_413_never_ask_for_served]

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
