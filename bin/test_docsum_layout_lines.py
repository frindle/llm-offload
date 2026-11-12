import docsum_pdf as dp


def flat(pages):
    return [ln for p in pages for ln in p]


def test_heading_styles_and_text_stripped():
    pages = dp.layout_lines("# Title\n## Bottom line\nBody text")
    assert flat(pages) == [("h1", "Title"), ("h2", "Bottom line"), ("body", "Body text")]


def test_bullets_get_dash_prefix_and_indent_on_wrap():
    out = flat(dp.layout_lines("- " + "word " * 10, width=20))
    assert out[0][0] == "bullet" and out[0][1].startswith("- ")
    assert all(s == "bullet" for s, _ in out)
    assert all(t.startswith("  ") for _, t in out[1:])
    assert all(len(t) <= 20 for _, t in out)


def test_wrap_respects_width_and_keeps_all_words():
    text = " ".join("w%d" % i for i in range(60))
    out = flat(dp.layout_lines(text, width=30))
    assert len(out) > 1
    assert all(len(t) <= 30 for _, t in out)
    assert " ".join(t for _, t in out).split() == text.split()


def test_blank_line_is_kept_as_empty_body_line():
    out = flat(dp.layout_lines("one\n\ntwo"))
    assert out == [("body", "one"), ("body", ""), ("body", "two")]
    assert len(dp.layout_lines("")) == 1


def test_pagination():
    md = "\n".join("line %d" % i for i in range(23))
    pages = dp.layout_lines(md, per_page=10)
    assert [len(p) for p in pages] == [10, 10, 3]
