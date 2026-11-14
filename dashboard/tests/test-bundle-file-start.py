#!/usr/bin/env python3
"""bundle_view.file_start: a job's start time is its livelog's BIRTH time, and the queue container (Linux) must find it
too. macOS has st_birthtime. A file imported into the standby container lost its birth time in the copy, so the export
writes <folder>/.birthtimes.json and file_start reads it. A file the container wrote itself has a statx birth time.
A time after the file's last write is never a start.

Asserts, in a temp dir with made-up livelogs:
  * the sidecar's time is used for a copied file whose own birth time is later than its last write;
  * a sidecar time after the last write, a name not in the sidecar and a broken sidecar give None;
  * load_history() takes launched_at / active_s from the sidecar, and falls back to the finish time without one;
  * on a filesystem that reports birth times (stat -c %W), a fresh file's start is its creation (statx).
Run: python3 test-bundle-file-start.py   (Linux or macOS; exit 0 = all pass)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

SRC_DIR = Path(os.environ.get("DASHBOARD_SRC") or Path(__file__).resolve().parent.parent / "src")
sys.path.insert(0, str(SRC_DIR))
import bundle_view as bv  # noqa: E402

FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def fs_has_btime(p):
    try:
        out = subprocess.run(["stat", "-c", "%W", str(p)], capture_output=True, text=True, timeout=10).stdout.strip()
        return out.isdigit() and int(out) > 0
    except (OSError, subprocess.SubprocessError):
        return False


def main():
    tmp = Path(tempfile.mkdtemp(prefix="bv-start-"))
    live, logs = tmp / "livelogs", tmp / "logs"
    live.mkdir()
    logs.mkdir()
    now = time.time()
    end = now - 3600
    a = live / "aaaa00000001-copied.livelog"      # imported: written now, mtime put back an hour
    a.write_text("made-up\n")
    os.utime(a, (end, end))
    b = live / "bbbb00000002-nosidecar.livelog"
    b.write_text("made-up\n")
    os.utime(b, (end, end))
    side = {"format": 1, "files": {a.name: end - 100.0, "cccc00000003-late.livelog": end + 50.0}}
    c = live / "cccc00000003-late.livelog"
    c.write_text("made-up\n")
    os.utime(c, (end, end))
    (live / bv.BIRTH_SIDECAR).write_text(json.dumps(side))
    on_mac = hasattr(os.stat(a), "st_birthtime")

    if not on_mac:
        check("copied file: start comes from the sidecar", bv.file_start(a), end - 100.0)
        check("sidecar time after the last write: not a start", bv.file_start(c), None)
        check("name not in the sidecar, own birth after the last write: None", bv.file_start(b), None)
        hist = {r["id"]: r for r in bv.load_history(str(live), str(logs))}
        check("load_history: launched_at from the sidecar", hist["aaaa00000001"]["launched_at"], end - 100.0)
        check("load_history: active_s from the sidecar", hist["aaaa00000001"]["active_s"], 100.0)
        check("load_history: no birth time anywhere falls back to the finish", hist["bbbb00000002"]["launched_at"], end)
        (live / bv.BIRTH_SIDECAR).write_text("{not json")
        bv._FILE_CACHE.clear()
        check("broken sidecar: None, no raise", bv.file_start(a), None)
    check("missing file: None", bv.file_start(live / "nope.livelog"), None)

    fresh = live / "dddd00000004-fresh.livelog"
    fresh.write_text("made-up\n")
    time.sleep(0.05)
    with open(fresh, "a") as fh:
        fh.write("more\n")
    got = bv.file_start(fresh)
    if on_mac or fs_has_btime(fresh):
        check("fresh file: start is its creation (birth time / statx)",
              got is not None and abs(got - now) < 30 and got <= os.stat(fresh).st_mtime, True)
    else:
        print("skip: this filesystem reports no birth time")
    print("\n%d failed" % len(FAILS))
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
