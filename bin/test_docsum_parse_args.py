import pytest
import docsum_cli as dc
from docsum_make_test_pdf import make_pdf

@pytest.fixture
def pdf(tmp_path):
    p = tmp_path / "doc.pdf"
    make_pdf(str(p), ["hello"])
    return str(p)


def code(argv):
    with pytest.raises(ValueError) as ei:
        dc.parse_args(argv)
    return str(ei.value)


def test_defaults(pdf):
    a = dc.parse_args([pdf, "--scenario", "my private scenario"])
    assert a == {"pdf": pdf, "scenario": "my private scenario", "ctx": None}


def test_ctx_option(pdf):
    a = dc.parse_args([pdf, "--scenario", "s", "--ctx", "32768"])
    assert a["ctx"] == 32768 and "model" not in a


def test_error_codes_never_echo_content(pdf, tmp_path):
    secret = "CANARY-SCENARIO-TEXT"
    assert code([str(tmp_path / "nope.pdf"), "--scenario", secret]) == "E_NO_FILE"
    txt = tmp_path / "x.txt"
    txt.write_text("hello")
    assert code([str(txt), "--scenario", secret]) == "E_NOT_PDF"
    fake = tmp_path / "fake.pdf"
    fake.write_text("not really a pdf")
    assert code([str(fake), "--scenario", secret]) == "E_NOT_PDF"
    assert code([pdf, "--scenario", "   "]) == "E_NO_SCENARIO"
    assert code([pdf]) == "E_NO_SCENARIO"
    assert code([pdf, "--scenario", secret, "--model", "llama3:8b"]) == "E_USAGE"   # no model selection exists
    assert code([pdf, "--scenario", secret, "--ctx", "100"]) == "E_BAD_CTX"
    assert code([pdf, "--scenario", secret, "--ctx", "abc"]) == "E_BAD_CTX"
    assert code([pdf, "--scenario", secret, "--bogus", "1"]) == "E_USAGE"
    assert code([]) == "E_USAGE"


def test_current_model_is_first_served():
    assert dc.current_model(lambda: ["live-a", "live-b"]) == "live-a"


def test_current_model_ranked_best_of_many():
    ids = ["Qwen3.5-9B", "mlx/gemma-4-26b-qat-4bit", "qwen3.5-35b-a3b", "Qwen3.6-35B-A3B"]
    assert dc.current_model(lambda: ids) == "Qwen3.6-35B-A3B"
    assert dc.current_model(lambda: ids[:3]) == "mlx/gemma-4-26b-qat-4bit"
    assert dc.current_model(lambda: ["gemma-4-26b-8bit", "gemma-4-26b"]) == "gemma-4-26b-8bit"


def test_current_model_only_9b():
    assert dc.current_model(lambda: ["qwen3.5-9b"]) == "qwen3.5-9b"


def test_current_model_unknown_after_known_and_list_order():
    assert dc.current_model(lambda: ["zzz", "qwen3.5-9b"]) == "qwen3.5-9b"
    assert dc.current_model(lambda: ["zzz", "yyy"]) == "zzz"


def test_current_model_empty():
    with pytest.raises(ValueError, match="E_MODEL_NOT_SERVED"):
        dc.current_model(lambda: [])


def test_current_model_none_served_or_error():
    def boom():
        raise RuntimeError("x")
    for fn in (lambda: [], boom):
        with pytest.raises(ValueError) as ei:
            dc.current_model(fn)
        assert str(ei.value) == "E_MODEL_NOT_SERVED"
