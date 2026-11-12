import docsum_extract as de


def test_empty_list_is_scanned():
    assert de.looks_scanned([]) is True


def test_blank_pages_are_scanned():
    assert de.looks_scanned(["", "  \n ", ""]) is True


def test_text_pages_are_not_scanned():
    assert de.looks_scanned(["x" * 500, "y" * 300]) is False


def test_threshold_uses_average_of_non_whitespace_chars():
    pages = ["a b c d e " * 5, ""]  # 25 non-ws chars on page 1, 0 on page 2 -> avg 12.5
    assert de.looks_scanned(pages, min_chars_per_page=10) is False
    assert de.looks_scanned(pages, min_chars_per_page=13) is True
