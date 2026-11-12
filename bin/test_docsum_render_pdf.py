import io
from pypdf import PdfReader
import docsum_pdf as dp


def read(data):
    return PdfReader(io.BytesIO(data))


def test_valid_pdf_header_and_trailer():
    data = dp.render_pdf([[("h1", "Title"), ("body", "hello")]])
    assert data.startswith(b"%PDF-")
    assert data.rstrip().endswith(b"%%EOF")
    bold = dp.render_pdf([[("h2", "Heading"), ("body", "x")]])
    assert b"Helvetica-Bold" in bold and b"/Helvetica" in bold


def test_text_round_trips_and_pages_counted():
    pages = [[("h1", "Summary Title"), ("body", "Rent is $1,250.00 (monthly)")],
             [("bullet", "- Deadline 2026-12-01 \\ slash")]]
    r = read(dp.render_pdf(pages))
    assert len(r.pages) == 2
    t0, t1 = r.pages[0].extract_text(), r.pages[1].extract_text()
    assert "Summary Title" in t0
    assert "$1,250.00 (monthly)" in t0
    assert "Deadline 2026-12-01 \\ slash" in t1
    assert "Rent" not in t1
    odd = dp.render_pdf([[("body", "curly \u201cquote\u201d \u2014 dash \u2022 \u4e2d")]])
    assert "curly" in read(odd).pages[0].extract_text()
