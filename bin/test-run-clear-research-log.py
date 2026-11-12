#!/usr/bin/env python3
"""A CLEARED research/diagnosis run must not come back as a log-only row (2026-11-09).

The owner asked for every finished queue run to be cleared. The dashboard's clear
(_archive_run) moves <id>.done.json / .gate.json / .diff into LOG_DIR/archive/ and
leaves the run log (<id>-<label>.log) in place when the job had a done/gate sidecar.
_iter_job_results' log-only fallback then picked the log up again (label matches
LABEL_RESEARCH_RE), _log_only_result recovered an answer, and 4 diag rows reappeared.

Pins, in a temp LOG_DIR:
  (a) a research job with done.json + log is listed;
  (b) once its done.json is moved to archive/ (= cleared), it is NOT listed;
  (c) same for a job cleared with only a gate.json;
  (d) a true log-only research job (no sidecars anywhere) is STILL listed;
  (e) a log-only coding job stays unlisted.
(b) and (c) fail on the pre-fix code.

Run: python3 test-run-clear-research-log.py [--src PATH]
"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
FAILS = []
OK_MARK = "RUN_CLEAR_RESEARCH_LOG_OK"


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def load(src):
    home = tempfile.mkdtemp(prefix="clear-research-")
    os.environ["HOME"] = home
    (Path(home) / "bin").mkdir()
    ld = importlib.machinery.SourceFileLoader("q_clear_research", str(src))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("q_clear_research", ld))
    sys.modules["q_clear_research"] = m
    ld.exec_module(m)
    return m


def write_log(ld, jid, label, answer):
    (ld / f"{jid}-{label}.log").write_text(
        f"[queue] job {jid} label {label}\n[worker] model: {answer}\n")


def ids(q, ld):
    return {r.get("id") for r in q._iter_job_results(ld)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--src", default=str(BIN / "ollama-queue.py"))
    a = ap.parse_args()
    q = load(a.src)
    with tempfile.TemporaryDirectory() as td:
        ld = Path(td) / "logs"
        arch = ld / "archive"
        arch.mkdir(parents=True)

        # research job with a done.json and a run log, plus its answer sidecar
        write_log(ld, "aaaaaa111111", "rt-browser-lock-diag", "the lock is held by the old browser")
        (ld / "aaaaaa111111.done.json").write_text(json.dumps(
            {"id": "aaaaaa111111", "label": "rt-browser-lock-diag", "status": "done", "exit_code": 0}))
        (ld / "aaaaaa111111.answer.md").write_text("the lock is held by the old browser\n")
        # research job cleared with only a gate.json (older style)
        write_log(ld, "bbbbbb222222", "diagnose-arr-supersede-r3", "supersede fired twice")
        (arch / "bbbbbb222222.gate.json").write_text(json.dumps({"verdict": "PASS"}))
        # true log-only research job (no sidecars anywhere): must still surface
        write_log(ld, "cccccc333333", "qagent-costco-signin-diag-083115", "cookie expired")
        # log-only coding job: never surfaced
        write_log(ld, "dddddd444444", "rt-fix-split-total", "patched")

        got = ids(q, ld)
        check("(a) research job with done.json is listed", "aaaaaa111111" in got, True)
        check("(c) research job cleared with gate.json only is not listed", "bbbbbb222222" in got, False)
        check("(d) true log-only research job still listed", "cccccc333333" in got, True)
        check("(e) log-only coding job not listed", "dddddd444444" in got, False)

        # clear job a the way the dashboard does: sidecars to archive/, log stays
        for suf in (".done.json",):
            (ld / f"aaaaaa111111{suf}").rename(arch / f"aaaaaa111111{suf}")
        got = ids(q, ld)
        check("(b) cleared research job (done.json archived) is not listed", "aaaaaa111111" in got, False)
        check("(b) its log is left in place", (ld / "aaaaaa111111-rt-browser-lock-diag.log").exists(), True)
        check("(b) direct _log_only_result is None", q._log_only_result("aaaaaa111111", ld), None)
        check("(d) true log-only still listed after the clear", "cccccc333333" in ids(q, ld), True)

    if FAILS:
        print(f"FAILED {len(FAILS)}: {FAILS}")
        return 1
    print(OK_MARK)
    return 0


if __name__ == "__main__":
    sys.exit(main())
