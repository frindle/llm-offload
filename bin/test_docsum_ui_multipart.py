import pytest
from docsum_ui_multipart import parse_multipart

B = "----XyZ123"
CT = "multipart/form-data; boundary=" + B


def build(parts, boundary=B):
    out = b""
    for name, filename, data in parts:
        out += ("--%s\r\n" % boundary).encode()
        disp = 'Content-Disposition: form-data; name="%s"' % name
        if filename is not None:
            disp += '; filename="%s"' % filename
        out += disp.encode() + b"\r\n"
        if filename is not None:
            out += b"Content-Type: application/pdf\r\n"
        out += b"\r\n" + data + b"\r\n"
    return out + ("--%s--\r\n" % boundary).encode()


def test_fields_names_and_params():
    body = build([("scenario", None, b"I am a tenant"), ("file", "my doc.pdf", b"%PDF-1.4\n..."), ("model", None, b"")])
    r = parse_multipart(body, CT)
    assert r["scenario"] == {"filename": None, "data": b"I am a tenant"}
    assert r["file"]["filename"] == "my doc.pdf" and r["file"]["data"] == b"%PDF-1.4\n..."
    assert r["model"]["data"] == b""
    assert parse_multipart(build([("file", "r\u00e9sum\u00e9;x.pdf", b"x")]), CT)["file"]["filename"] == "r\u00e9sum\u00e9;x.pdf"
    ct = 'multipart/form-data; charset=utf-8; boundary="%s"' % B
    assert parse_multipart(build([("a", None, b"1")]), ct)["a"]["data"] == b"1"


def test_binary_with_crlf_and_dashes_preserved():
    blob = b"%PDF-1.4\r\n\r\n--not-the-boundary\r\n\x00\xff\r\n\r\nend"
    r = parse_multipart(build([("file", "a.pdf", blob)]), CT)
    assert r["file"]["data"] == blob


@pytest.mark.parametrize("ct", ["multipart/form-data", "", None, "application/json"])
def test_missing_boundary_is_code_only(ct):
    with pytest.raises(ValueError) as e:
        parse_multipart(build([("a", None, b"SECRET")]), ct)
    assert str(e.value) == "E_BAD_UPLOAD"


def test_no_parts_or_garbage_is_code_only():
    for body in (b"", b"SECRETGARBAGE", ("--%s--\r\n" % B).encode()):
        with pytest.raises(ValueError) as e:
            parse_multipart(body, CT)
        assert str(e.value) == "E_BAD_UPLOAD"


def test_part_without_name_or_too_many_parts_is_code_only():
    body = ("--%s\r\nContent-Disposition: form-data\r\n\r\nSECRET\r\n--%s--\r\n" % (B, B)).encode()
    for bad in (body, build([("f%d" % i, None, b"x") for i in range(40)])):
        with pytest.raises(ValueError) as e:
            parse_multipart(bad, CT)
        assert str(e.value) == "E_BAD_UPLOAD"
