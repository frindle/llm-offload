import re

from docsum_ui_page import page_html

def test_document_and_no_external_resources():
    H = page_html()
    low = H.lower()
    assert low.lstrip().startswith("<!doctype html>") and "</html>" in low
    assert "http://" not in low and "https://" not in low
    assert "src=" not in low and "<link" not in low and "@import" not in low and "url(" not in low
    assert "innerhtml" not in low and "outerhtml" not in low and "document.write" not in low
    assert "<iframe" not in low and "<img" not in low
    assert len(re.findall(r"<script", low)) == 1 and low.count("</script>") == 1
    assert "localstorage" not in low and "sessionstorage" not in low and "console.log" not in low and "document.cookie" not in low


def test_form_controls():
    H = page_html()
    assert re.search(r'<input[^>]*type="file"[^>]*accept="[^"]*pdf', H) or re.search(r'accept="[^"]*pdf[^"]*"[^>]*type="file"', H)
    assert re.search(r'<textarea[^>]*id="scenario"', H)
    assert re.search(r'<textarea[^>]*placeholder="[^"]*who I am', H, re.I)
    assert 'id="drop"' in H and 'id="jobs"' in H and 'id="go"' in H
    assert "<select" not in H and 'id="model"' not in H
    assert "100 MB" in H


def test_no_model_selection():
    H = page_html()
    assert "<option" not in H and "<select" not in H and 'append("model"' not in H


def test_script_wires_the_api():
    H = page_html()
    assert "api/jobs" in H and "api/upload" in H and "api/delete/" in H
    assert "job/" in H and "/pdf" in H and "/md" in H
    assert "FormData" in H and "dragover" in H and "drop" in H
    assert "textContent" in H and "setInterval" in H
    assert "refname" in H and "api/rename/" in H


def test_labels_for_status_and_columns():
    H = page_html()
    for w in ("queued", "running", "done", "failed", "Download PDF", "Download MD", "Delete"):
        assert w in H
