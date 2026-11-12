"""Frozen tests: the docsum page must work both at / and behind a path prefix (/docsum/), so every URL is relative.
Run: python3 test_docsum_ui_page_relative.py"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from docsum_ui_page import page_html

H = page_html()


def test_no_root_absolute_urls():
    assert not re.search(r'fetch\("/', H), "fetch must use a relative URL"
    assert not re.search(r'href="/', H) and not re.search(r"href=\"/", H)
    assert '"/api/' not in H and '"/job/' not in H


def test_relative_urls_present():
    for want in ('fetch("api/upload"', 'fetch("api/jobs"', 'fetch("api/delete/"'):
        assert want in H, want
    assert H.count('href="job/"') == 2


def test_rest_of_page_unchanged():
    assert "FormData" in H and "dragover" in H and "setInterval(refresh,3000)" in H
    assert 'id="drop"' in H and 'id="jobs"' in H and 'id="go"' in H and "100 MB" in H


TESTS = [test_no_root_absolute_urls, test_relative_urls_present, test_rest_of_page_unchanged]

if __name__ == "__main__":
    order = TESTS[::-1] if os.environ.get("TEST_ORDER") == "reverse" else TESTS
    bad = 0
    for t in order:
        try:
            t()
            print(f"RESULT {t.__name__} PASS")
        except Exception as e:
            bad += 1
            print(f"RESULT {t.__name__} FAIL {type(e).__name__}: {e}")
    sys.exit(1 if bad else 0)
