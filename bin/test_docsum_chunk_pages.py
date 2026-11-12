import docsum_chunk as de


def test_empty():
    assert de.chunk_pages([], 1000) == []


def test_all_fit_in_one_chunk():
    ch = de.chunk_pages(["aaa", "bbb", "ccc"], 10000)
    assert len(ch) == 1
    assert (ch[0]["start"], ch[0]["end"]) == (1, 3)
    assert "[Page 1]\naaa\n" in ch[0]["text"]
    assert "[Page 3]\nccc\n" in ch[0]["text"]


def test_split_keeps_page_ranges():
    pages = ["a" * 50, "b" * 50, "c" * 50, "d" * 50]
    size = len("[Page 1]\n" + "a" * 50 + "\n")
    ch = de.chunk_pages(pages, size * 2)
    assert [(c["start"], c["end"]) for c in ch] == [(1, 2), (3, 4)]
    assert "[Page 4]" in ch[1]["text"] and "[Page 4]" not in ch[0]["text"]


def test_oversize_page_own_chunk_not_dropped_or_split():
    pages = ["a" * 10, "b" * 500, "c" * 10]
    ch = de.chunk_pages(pages, 100)
    assert [(c["start"], c["end"]) for c in ch] == [(1, 1), (2, 2), (3, 3)]
    assert "b" * 500 in ch[1]["text"]


def test_oversize_first_page_does_not_emit_empty_chunk():
    ch = de.chunk_pages(["b" * 500, "c" * 10], 100)
    assert [(c["start"], c["end"]) for c in ch] == [(1, 1), (2, 2)]
    assert all(c["text"] for c in ch)
