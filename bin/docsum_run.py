#!/usr/bin/env python3
"""docsum_run.py -- ollama-queue --runner: scenario-focused PDF summary, fully local and private.

Queue contract (same five flags as studio-research.py): --model --host --num-ctx --cwd --task-file.
PRIVACY: the task file holds only {"job": <opaque id>, "root": <private dir>}. The PDF copy, scenario, extracted text and
outputs live in <root>/<job>/ (mode 700, files 600). This program prints ONLY the opaque id, sizes, counts, durations and
E_* codes -- never document, summary or scenario text, nor the file name -- so queue logs / done.json carry no content.
"""
import argparse
import contextlib
import io
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import docsum_cli  # noqa: E402
from docsum_extract import extract_pages, looks_scanned  # noqa: E402
from docsum_core import summarize  # noqa: E402
from docsum_pdf import layout_lines, render_pdf  # noqa: E402

PRIVATE_ROOT = os.path.expanduser("~/Documents/doc-summaries/private")


def emit(msg):
    print("[docsum] " + msg, flush=True)


def _write(path, data):
    mode = "wb" if isinstance(data, bytes) else "w"
    with open(path, mode) as f:
        f.write(data)
    os.chmod(path, 0o600)


def _is_loopback(host):
    import ipaddress
    h = (host or "").strip("[]").lower()
    if h == "localhost":
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def make_call(host, model):
    """The document goes only to a Darkbloom endpoint on this Mac: refuse (E_NOT_LOCAL) unless its base URL is loopback."""
    import darkbloom_chat as dbk
    from urllib.parse import urlparse
    try:
        base_host = urlparse(str(dbk.base_url() or "")).hostname
        if not _is_loopback(base_host):
            raise ValueError("E_NOT_LOCAL")
    except Exception:
        raise ValueError("E_NOT_LOCAL")

    def call(system, user):
        with contextlib.redirect_stderr(io.StringIO()):  # the adapter may echo request detail on errors
            content, _fin = dbk.chat(host, model, system, user, max_tokens=3500, role="review")
        return content
    return call


def run_job(task_file, model, host, num_ctx, call=None):
    t0 = time.time()
    try:
        task = json.load(open(task_file))
        job, root = str(task["job"]), str(task["root"])
        jdir = os.path.join(root, job)
        assert os.path.isdir(jdir) and os.path.realpath(jdir).startswith(os.path.realpath(root) + os.sep)
    except Exception:
        emit("FAIL code=E_TASK")
        return 2
    emit("job=%s start model=%s num_ctx=%s" % (job, model, num_ctx))

    def fail(code):
        try:
            _write(os.path.join(jdir, "status.json"), json.dumps({"ok": False, "code": code}))
        except Exception:           # e.g. the job dir was deleted under us: still exit with the code only
            pass
        emit("job=%s FAIL code=%s" % (job, code))
        return 1

    try:
        pages = extract_pages(os.path.join(jdir, "input.pdf"))
    except ValueError as e:
        return fail(str(e) if str(e).startswith("E_") else "E_EXTRACT")
    except Exception:
        return fail("E_EXTRACT")
    if looks_scanned(pages):
        return fail("E_SCANNED_NO_OCR")
    try:
        with open(os.path.join(jdir, "scenario.txt")) as f:
            scenario = f.read()
        with open(os.path.join(jdir, "meta.json")) as f:
            meta = json.load(f)
        if not isinstance(meta, dict):
            meta = {}
    except Exception:
        return fail("E_READ")
    if call is None:      # resolve the live model now: Darkbloom may have swapped since enqueue; --model is only a hint
        try:
            model = docsum_cli.current_model()
        except ValueError as e:
            return fail(str(e))
        emit("job=%s model=%s" % (job, model))
    max_chars = max(4000, int(int(num_ctx) * 1.8))
    try:
        res = summarize(pages, scenario, call or make_call(host, model), max_chars=max_chars)
    except ValueError as e:
        return fail(str(e) if str(e).startswith("E_") else "E_SUMMARIZE")
    except Exception:
        return fail("E_MODEL_CALL")
    if not (res["summary"] or "").strip():
        return fail("E_EMPTY_SUMMARY")
    try:
        return _finish(jdir, job, res, meta, scenario, pages, model, t0)
    except Exception:               # render/write problems surface as a code, never a traceback
        return fail("E_WRITE")


def _finish(jdir, job, res, meta, scenario, pages, model, t0):
    stamp = time.strftime("%Y-%m-%d %H:%M")
    md = "# Document summary: %s\n\n**Scenario:** %s\n\n%s\n\n---\n*%d pages, summarised locally with %s on %s.*\n" % (
        meta.get("src_name", "document"), " ".join(scenario.split()), res["summary"].strip(), len(pages), model, stamp)
    _write(os.path.join(jdir, "summary.md"), md)
    pdf = render_pdf(layout_lines(md))
    _write(os.path.join(jdir, "summary.pdf"), pdf)
    _write(os.path.join(jdir, "status.json"), json.dumps({"ok": True, "pages": len(pages), "chunks": res["chunks"], "calls": res["calls"]}))
    emit("job=%s OK pages=%d chunks=%d calls=%d md_bytes=%d pdf_bytes=%d secs=%.0f" % (
        job, len(pages), res["chunks"], res["calls"], len(md.encode()), len(pdf), time.time() - t0))
    return 0


def _go_silent():
    """pypdf/pdfminer warnings and log records can echo text extracted from the PDF. Send them nowhere: the queue captures
    this process's stdout/stderr into logs the pipeline (and Claude) can read. emit() writes to stdout only."""
    import logging
    import warnings
    logging.disable(logging.CRITICAL)
    warnings.simplefilter("ignore")
    try:
        sys.stderr.flush()
        fd = os.open(os.devnull, os.O_WRONLY)
        os.dup2(fd, 2)
        os.close(fd)
    except OSError:
        pass


def main(argv=None):
    ap = argparse.ArgumentParser(description="docsum queue runner")
    ap.add_argument("--model", required=True)
    ap.add_argument("--host", default="darkbloom")
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--cwd", default=None)
    ap.add_argument("--task-file", required=True)
    a = ap.parse_args(argv)
    _go_silent()
    try:
        return run_job(a.task_file, a.model, a.host, a.num_ctx)
    except Exception:               # last resort: opaque code, no traceback (it could quote document text)
        emit("FAIL code=E_INTERNAL")
        return 1


if __name__ == "__main__":
    sys.exit(main())
