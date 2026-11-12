import pytest
import docsum_core as dc


class Fake:
    def __init__(self, reply=None):
        self.calls = []
        self.reply = reply

    def __call__(self, system, user):
        self.calls.append((system, user))
        return self.reply if self.reply is not None else "OUT%d" % len(self.calls)


def test_small_document_is_one_single_pass():
    f = Fake()
    r = dc.summarize(["aaa", "bbb", "ccc"], "SCENARIO-X", f, max_chars=10000)
    assert (r["calls"], r["chunks"], r["summary"]) == (1, 1, "OUT1")
    assert len(f.calls) == 1
    assert "SCENARIO-X" in f.calls[0][1] and "[Page 2]" in f.calls[0][1]


def test_large_document_maps_then_reduces_with_page_labels():
    f = Fake()
    r = dc.summarize(["p" * 100 for _ in range(4)], "SC", f, max_chars=250)
    assert r["chunks"] == 2 and r["calls"] == 3 and len(f.calls) == 3
    last = f.calls[-1][1]
    assert "## Bottom line" in last
    assert "[Pages 1-2]" in last and "[Pages 3-4]" in last
    assert "OUT1" in last and "OUT2" in last
    assert r["summary"] == "OUT3"
    assert "## Bottom line" not in f.calls[0][1]


def test_notes_too_long_for_reduce_are_merged_in_groups_first():
    f = Fake("n" * 100)
    r = dc.summarize(["p" * 100 for _ in range(8)], "SC", f, max_chars=250)
    assert r["chunks"] == 4 and r["calls"] == 7
    assert "## Bottom line" in f.calls[-1][1]
    assert all("## Bottom line" not in u for _, u in f.calls[:-1])


def test_unreducible_notes_raise_error_code():
    f = Fake("n" * 400)
    with pytest.raises(ValueError) as ei:
        dc.summarize(["p" * 100 for _ in range(8)], "SC", f, max_chars=250)
    assert "E_REDUCE_OVERFLOW" in str(ei.value)


def test_merging_repeats_until_the_reduce_input_fits():
    # 16 pages -> 8 map notes; one merge pass leaves 4 notes (still over max_chars), so a second pass must run
    # before the reduce (2026-11-03 review: a single merge pass sent the reduce call more than max_chars).
    f = Fake("n" * 100)
    r = dc.summarize(["p" * 100 for _ in range(16)], "SC", f, max_chars=250)
    assert r["chunks"] == 8 and r["calls"] == 8 + 4 + 2 + 1 == len(f.calls)
    reduce_text = f.calls[-1][1].split("TEXT:\n", 1)[1]
    assert len(reduce_text) <= 250
    assert reduce_text.startswith("[Pages 1-8]\n") and "\n\n[Pages 9-16]\n" in reduce_text
