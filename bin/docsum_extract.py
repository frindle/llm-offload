"""PDF text extraction and chunking for the document-summary capability."""


MAX_PAGES = 500          # more than this is refused (E_TOO_MANY_PAGES) rather than ground through
MAX_SECONDS = 180.0      # wall-time cap for extraction (E_EXTRACT_TIMEOUT)


def extract_pages(pdf_path, max_pages=MAX_PAGES, max_seconds=MAX_SECONDS):
    """Per-page text. The wall-time cap is enforced by SIGALRM when running in the main thread (so ONE pathological page
    cannot hang the job) and by a between-pages check everywhere else."""
    import signal
    import threading
    if threading.current_thread() is not threading.main_thread() or not hasattr(signal, "setitimer") or max_seconds <= 0:
        return _extract(pdf_path, max_pages, max_seconds)

    def on_alarm(signum, frame):
        raise ValueError("E_EXTRACT_TIMEOUT")
    old = signal.signal(signal.SIGALRM, on_alarm)
    signal.setitimer(signal.ITIMER_REAL, max_seconds)
    try:
        return _extract(pdf_path, max_pages, max_seconds)
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, old)


def _extract(pdf_path, max_pages, max_seconds):
    import os
    import time
    from pypdf import PdfReader
    import pdfplumber

    if not os.path.exists(pdf_path):
        raise FileNotFoundError(pdf_path)

    t0 = time.time()

    def guard(n):
        if n > max_pages:
            raise ValueError("E_TOO_MANY_PAGES")
        if time.time() - t0 > max_seconds:
            raise ValueError("E_EXTRACT_TIMEOUT")

    pages = []
    try:
        reader = PdfReader(pdf_path)
        guard(len(reader.pages))
        for page in reader.pages:
            guard(len(pages))
            pages.append(page.extract_text() or "")
    except ValueError as e:
        if str(e).startswith("E_"):
            raise
        pages = []
        pages = _plumber(pdf_path, pdfplumber, guard)
    except Exception:
        pages = []                       # drop any partial pypdf result: no duplicate pages from the fallback
        pages = _plumber(pdf_path, pdfplumber, guard)
    return pages


def _plumber(pdf_path, pdfplumber, guard):
    pages = []
    with pdfplumber.open(pdf_path) as pdf:
        guard(len(pdf.pages))
        for page in pdf.pages:
            guard(len(pages))
            pages.append(page.extract_text() or "")
    return pages


def looks_scanned(pages, min_chars_per_page=40):
    if not pages:
        return True
    total = sum(sum(1 for c in page if not c.isspace()) for page in pages)
    return (total / len(pages)) < min_chars_per_page
