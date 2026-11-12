import os
import pytest
from docsum_make_test_pdf import make_pdf
import docsum_extract as de


def test_extracts_each_page_in_order(tmp_path):
    p = str(tmp_path / "a.pdf")
    make_pdf(p, ["Alpha rent is $1,250.00", "Beta deadline 2026-12-01", "Gamma clause"])
    pages = de.extract_pages(p)
    assert len(pages) == 3
    assert "$1,250.00" in pages[0]
    assert "2026-12-01" in pages[1]
    assert "Gamma" in pages[2]
    assert "Alpha" not in pages[2]


def test_missing_file_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        de.extract_pages(str(tmp_path / "nope.pdf"))


def test_garbage_file_raises_not_silent(tmp_path):
    p = tmp_path / "bad.pdf"
    p.write_bytes(b"this is not a pdf at all")
    with pytest.raises(Exception) as ei:
        de.extract_pages(str(p))
    assert not isinstance(ei.value, NotImplementedError)
    assert not isinstance(ei.value, FileNotFoundError)
