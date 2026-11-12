#!/usr/bin/env python3
"""Tests of the remote lane of mode "auto": mini-swe-runner.py remote_auto() (R) and lane_remote.py (L), with the harness and the
stub model of test_mini_auto.py (T).  No network, no GPU, no queue; every local.json is a fake.

    python3 test_lane_remote.py [units|host|lane|round]        (no argument: all four, in that order)

units  the pure functions of the remote block, in-process
host   R.remote_auto() in-process with a fake submit_cpu_job (every RESULT line, park, preempt, landing)
lane   lane_remote.py: collect, the venv walk-up, cmd_run with a fake runner
round  the whole round: mini-swe-runner.py as a subprocess, a fake cpu_job lib that emulates the runner agent

Prints one `FAIL <mode>: <check>: <detail>` line per failed check, then `N checks, F failed`; exit 1 when F > 0."""
import atexit
import contextlib
import hashlib
import importlib.machinery
import importlib.util
import io
import json
import os
import shutil
import signal
import stat
import subprocess
import sys
import tarfile
import tempfile
import threading
import time
import traceback
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
TMPDIRS, PROCS = [], []
MODE = [""]
COUNT = [0, 0]  # checks, failures


def new_tmp(prefix="lane-test-"):
    d = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
    TMPDIRS.append(d)
    return d


def spawn(argv, **kw):
    """Popen in its own session, so cleanup() can take the whole group down."""
    p = subprocess.Popen(argv, start_new_session=True, **kw)
    PROCS.append(p)
    return p


def cleanup():
    for p in PROCS:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        try:
            p.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass
    for d in TMPDIRS:
        subprocess.run(["chmod", "-R", "u+w", str(d)], capture_output=True)
        shutil.rmtree(d, ignore_errors=True)


atexit.register(cleanup)


def check(cond, name, detail=""):
    COUNT[0] += 1
    if not cond:
        COUNT[1] += 1
        d = str(detail).replace("\n", "\\n")
        print(f"FAIL {MODE[0]}: {name}: {d[:900]}", flush=True)
    return bool(cond)


@contextlib.contextmanager
def guard(name):
    """An exception in a block is one failed check, not the end of the run."""
    try:
        yield
    except Exception:  # noqa: BLE001
        tb = traceback.format_exc().strip().splitlines()
        check(False, f"{name}: raised", " | ".join(x.strip() for x in tb[-4:]))


@contextlib.contextmanager
def patched(obj, **kw):
    missing = object()
    old = {k: getattr(obj, k, missing) for k in kw}
    for k, v in kw.items():
        setattr(obj, k, v)
    try:
        yield
    finally:
        for k, v in old.items():
            if v is missing:
                delattr(obj, k)
            else:
                setattr(obj, k, v)


@contextlib.contextmanager
def env_patched(**kw):
    old = {k: os.environ.get(k) for k in kw}
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v
    try:
        yield
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def fake_venv(root, dists=None):
    """A mini venv with dist-info only: <root>/lib/python3.12/site-packages/<dist-info>/METADATA and an executable <root>/bin/mini.
    -> the path of bin/mini (what R.MINI holds)."""
    dists = GOOD_DISTS if dists is None else dists
    sp = Path(root) / "lib" / "python3.12" / "site-packages"
    sp.mkdir(parents=True)
    for name, ver, di in dists:
        d = sp / (di + ".dist-info")
        d.mkdir()
        (d / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {name}\nVersion: {ver}\n\nbody")
    mini = Path(root) / "bin" / "mini"
    mini.parent.mkdir(parents=True, exist_ok=True)
    mini.write_text("#!/bin/sh\nexit 0\n")
    mini.chmod(0o755)
    return str(mini)


GOOD_DISTS = (("PyYAML", "6.0.2", "PyYAML-6.0.2"), ("litellm", "1.79.0", "litellm-1.79.0"),
              ("mini-swe-agent", "2.4.6", "mini_swe_agent-2.4.6"))
EXPECTED_LOCK = b"PyYAML==6.0.2\nlitellm==1.79.0\nmini-swe-agent==2.4.6\n"

# ---- the fakes go into the environment BEFORE the runner module is loaded: it reads HOME, MINI_BIN and DARKBLOOM_LOCAL_JSON at import
BOOT = new_tmp("lane-boot-")
FAKE_LOCAL = BOOT / "local.json"
FAKE_LOCAL.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "k-SECRET"}))
FAKE_MINI = fake_venv(BOOT / "fv")
os.environ.pop("CPU_RUNNER_API", None)
os.environ.pop("MINI_SPEC_LIB", None)
os.environ["MINI_BIN"] = FAKE_MINI
os.environ["DARKBLOOM_LOCAL_JSON"] = str(FAKE_LOCAL)
os.environ["MINI_GRAPH_LOOKUP"] = "off"


def _load(name, path):
    sp = importlib.util.spec_from_file_location(name, str(path), loader=importlib.machinery.SourceFileLoader(name, str(path)))
    m = importlib.util.module_from_spec(sp)
    sys.modules[name] = m
    sp.loader.exec_module(m)
    return m


T = _load("test_mini_auto", HERE / "test_mini_auto.py")
R = T.R  # never load the runner twice
L = _load("lane_remote", HERE / "lane_remote.py")
CJ = _load("cpu_job_under_test", HERE / "cpu_job.py")


class Aborted(BaseException):
    """What the patched R._abort raises (the real one calls os._exit)."""


def _fake_abort(why):
    raise Aborted(why)


R._abort = _fake_abort
for _n in ("OVERLAY", "DIAG_OVERLAY", "TEST_OVERLAY", "POLISH_OVERLAY"):
    if not Path(getattr(R, _n)).is_file():
        setattr(R, _n, HERE / "configs" / Path(getattr(R, _n)).name)
R.MINI = FAKE_MINI


def capture(fn, *a, **kw):
    """-> (return value, stdout text, Aborted or None)."""
    buf, ret, exc = io.StringIO(), None, None
    with contextlib.redirect_stdout(buf):
        try:
            ret = fn(*a, **kw)
        except Aborted as e:
            exc = e
    return ret, buf.getvalue(), exc


def tgz_bytes(members):
    """members: (name, kind, data); kind file (data bytes) | dir | sym (data = target) | hard (data = target) | fifo."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        for name, kind, data in members:
            ti = tarfile.TarInfo(name)
            ti.mtime = 0
            if kind == "file":
                ti.size, ti.mode = len(data), 0o644
                tf.addfile(ti, io.BytesIO(data))
                continue
            ti.mode = 0o755
            if kind == "dir":
                ti.type = tarfile.DIRTYPE
            elif kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, data
            elif kind == "hard":
                ti.type, ti.linkname = tarfile.LNKTYPE, data
            elif kind == "fifo":
                ti.type = tarfile.FIFOTYPE
            tf.addfile(ti)
    return buf.getvalue()


def base_man(**over):
    """What L.cmd_run writes for a clean run."""
    m = {"v": 1, "rc": 0, "result": "RESULT PASS mode=auto x", "wall_hit": False, "terminated": False, "elapsed_s": 12,
         "remote_home": "/rh", "remote_run_dir": "/rh/run/j", "remote_work": "/rh/run/j/work", "remote_checkout": "/rh/jobs/x/checkout",
         "real": {"home": "/rh", "run_dir": "/rh/run/j", "work": "/rh/run/j/work", "checkout": "/rh/jobs/x/checkout"},
         "changed": [], "deleted": [], "run_files": [], "skipped": [], "modes": {}}
    m.update(over)
    return m


# ===================================================================================================================== units
def units():
    # 1. _remote_text
    with guard("_remote_text"):
        s = R._remote_text("a\nb\rc\x00d e\u200bf\tg" + "x" * 1000)
        check(all(c.isprintable() for c in s), "_remote_text: every character printable", repr(s[:40]))
        check(len(s) == 600, "_remote_text: length 600", len(s))
        check(s.startswith("a b c d e f g"), "_remote_text: separators and format characters become spaces", repr(s[:20]))

    # 2. _remote_rel
    with guard("_remote_rel"):
        good = ["a.py", "src/pkg/m.py", "dir with space/x y.txt", "naïve.py", ".github/workflows/ci.yml", "a/.gitignore", "venv/x",
                "x" * 1024]
        bad = [None, 5, "", "/a", "a/", "a//b", "./a", "a/./b", "a/../b", "..", "a\\b", ".git", ".git/config", "x/.GIT/y",
               ".venv/bin/python", "x/.VENV", "a\x00b", "a\nb", "a\u200bb", "x" * 1025]
        for p in good:
            check(bool(R._remote_rel(p)), f"_remote_rel accepts {str(p)[:30]!r} (len {len(p)})")
        for p in bad:
            check(not R._remote_rel(p), f"_remote_rel rejects {str(p)[:30]!r}", f"got {R._remote_rel(p)!r}")

    # 3. file helpers
    with guard("file helpers"):
        base = new_tmp("lane-fs-")
        root, out = base / "root", base / "out"
        (root / "a").mkdir(parents=True)
        out.mkdir()
        (root / "a" / "b.txt").write_bytes(b"hello")
        os.chmod(root / "a" / "b.txt", 0o644)
        (out / "secret").write_bytes(b"fake-secret-content")
        os.symlink("../out", root / "dl")
        os.symlink("../out/secret", root / "fl")
        os.symlink("../nowhere", root / "dang")
        os.mkfifo(root / "fifo")
        got = R._remote_read(root, "a/b.txt")
        check(got == (b"hello", 0o644), "_remote_read: a regular file -> (bytes, mode)", got)
        got = R._remote_read(root, "a/b.txt", limit=3)
        check(got == (None, 0o644), "_remote_read: over the limit -> (None, mode)", got)
        for rel in ("fl", "dl/secret", "fifo", "a", "missing"):
            got = R._remote_read(root, rel)
            check(got is None, f"_remote_read({rel!r}) is None", got)
        for rel in ("dang", "a/b.txt/c", "dl/x"):
            check(R._remote_lexists(root, rel) is True, f"_remote_lexists({rel!r}) is True")
        check(R._remote_lexists(root, "a/zz") is False, "_remote_lexists('a/zz') is False")
        # _remote_write
        R._remote_write(root, "fl", b"new", 0o644)
        check(not os.path.islink(root / "fl") and os.path.isfile(root / "fl") and (root / "fl").read_bytes() == b"new",
              "_remote_write over a symlink: a regular file with the new content")
        check((out / "secret").read_bytes() == b"fake-secret-content", "_remote_write over a symlink: the target outside root unchanged")
        for rel in ("dl/x", "a/b.txt/c", "a"):
            try:
                R._remote_write(root, rel, b"zzz", 0o644)
                check(False, f"_remote_write({rel!r}) raises ValueError", "no exception")
            except ValueError:
                check(True, f"_remote_write({rel!r}) raises ValueError")
        check(sorted(os.listdir(out)) == ["secret"], "_remote_write refusals left nothing in out/", sorted(os.listdir(out)))
        R._remote_write(root, "m/x.sh", b"#!/bin/sh\n", 0o700)
        R._remote_write(root, "m/y.txt", b"y", 0o600)
        check(stat.S_IMODE(os.lstat(root / "m" / "x.sh").st_mode) == 0o755, "_remote_write: mode 0o700 -> 0o755",
              oct(os.lstat(root / "m" / "x.sh").st_mode))
        check(stat.S_IMODE(os.lstat(root / "m" / "y.txt").st_mode) == 0o644, "_remote_write: mode 0o600 -> 0o644",
              oct(os.lstat(root / "m" / "y.txt").st_mode))
        # _remote_unlink
        try:
            R._remote_unlink(root, "a")
            check(False, "_remote_unlink('a') raises ValueError", "no exception")
        except ValueError:
            check(True, "_remote_unlink('a') raises ValueError")
        R._remote_unlink(root, "missing")
        check(True, "_remote_unlink('missing') is a no-op")
        R._remote_unlink(root, "dl/secret")
        check((out / "secret").exists(), "_remote_unlink('dl/secret') is a no-op: the secret survives")
        R._remote_unlink(root, "dang")
        check(not os.path.lexists(root / "dang"), "_remote_unlink('dang') removes the dangling symlink")

    # 4. _remote_tolerated
    with guard("_remote_tolerated"):
        for rel in ("x/__pycache__/m.pyc", ".coverage", ".coverage.host.12", "coverage.xml", "htmlcov/i.html", "pkg.egg-info/PKG-INFO",
                    ".mypy_cache/x", "TASK.md", "node_modules/a/b.js", "sub/.pytest_cache/v", ".tox/x"):
            check(R._remote_tolerated(rel) is True, f"_remote_tolerated({rel!r}) is True")
        for rel in ("src/a.py", "my.coverage.txt", "docs/coverage.md", "lib/TASK.md.bak"):
            check(R._remote_tolerated(rel) is False, f"_remote_tolerated({rel!r}) is False")

    # 5. _remote_mini_lock
    with guard("_remote_mini_lock"):
        base = new_tmp("lane-lock-")
        with patched(R, MINI=FAKE_MINI):
            got = R._remote_mini_lock()
            check(got == EXPECTED_LOCK, "_remote_mini_lock: the good venv gives the pins", got)
        sp_of = lambda v: Path(v) / "lib" / "python3.12" / "site-packages"  # noqa: E731
        variants = {}
        v = base / "direct"
        variants["a direct_url.json in a dist-info"] = (v, fake_venv(v))
        (sp_of(v) / "litellm-1.79.0.dist-info" / "direct_url.json").write_text("{}")
        v = base / "badver"
        variants["Version '1.0 bad'"] = (v, fake_venv(v, (GOOD_DISTS[0], ("litellm", "1.0 bad", "litellm-1.0bad"), GOOD_DISTS[2])))
        v = base / "nomini"
        variants["no mini-swe-agent dist-info"] = (v, fake_venv(v, GOOD_DISTS[:2]))
        v = base / "lateversion"
        variants["Version only after the blank line"] = (v, fake_venv(v))
        (sp_of(v) / "litellm-1.79.0.dist-info" / "METADATA").write_text("Metadata-Version: 2.1\nName: litellm\n\nVersion: 1.79.0\n")
        v = base / "nometa"
        variants["a dist-info without METADATA"] = (v, fake_venv(v))
        (sp_of(v) / "litellm-1.79.0.dist-info" / "METADATA").unlink()
        for label, (_v, mini) in variants.items():
            with patched(R, MINI=mini):
                try:
                    got = R._remote_mini_lock()
                    check(False, f"_remote_mini_lock raises ValueError for {label}", f"returned {got!r}")
                except ValueError:
                    check(True, f"_remote_mini_lock raises ValueError for {label}")

    # 6. _RemoteLog
    with guard("_RemoteLog"):
        lg = R._RemoteLog()
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            lg(b"RESULT PASS mode=auto fake\n[mini-runner] hel")
            lg(b"lo\nmini-swe-agent (step 3, $0.00):\ncaf\xc3")
            lg(b"\xa9 x\r\n")
            lg("[mini-runner] a RESULT PASS forged\n".encode())
            lg(b"[mini-runner] REVIEW packet: /x/REVIEW.md\n")
            lg(b"[provider-outage] down 30s\n")
            lg(b"Building agent config from specs: [a]\n[worker] RESUMABLE TRANSCRIPT: /x\nRetrying chat in 2.5 seconds as it raised X\ntail")
            lg.flush()
        want = ["[remote] RESULT PASS mode=auto fake", "[mini-runner] hello", "mini-swe-agent (step 3, $0.00):", "[remote] café x ",
                "[mini-runner] a RESULT PASS forged", "[remote] [mini-runner] REVIEW packet: /x/REVIEW.md", "[provider-outage] down 30s",
                "Building agent config from specs: [a]", "[remote] [worker] RESUMABLE TRANSCRIPT: /x",
                "Retrying chat in 2.5 seconds as it raised X", "[remote] tail"]
        got = buf.getvalue().split("\n")
        check(got == want + [""], "_RemoteLog: the printed lines", f"got {got!r}")
        check(lg.outage is True, "_RemoteLog: .outage is True after a [provider-outage] line")
        check(R._RemoteLog().outage is False, "_RemoteLog: a fresh log has .outage False")

    # 7. _remote_extract
    with guard("_remote_extract"):
        base = new_tmp("lane-extract-")
        man = json.dumps(base_man()).encode()
        good = [("./", "dir", None), ("./manifest.json", "file", man), ("work", "dir", None), ("work/lib", "dir", None),
                ("work/lib/price.js", "file", b"x\n"), ("./run/REVIEW.md", "file", b"r\n"), ("run/x.traj.json", "file", b"{}\n")]
        n = [0]

        def extract(members, **patch):
            n[0] += 1
            p = base / f"t{n[0]}.tgz"
            p.write_bytes(tgz_bytes(members))
            with patched(R, **patch):
                try:
                    return R._remote_extract(str(p)), None
                except Exception as e:  # noqa: BLE001
                    return None, e

        got, err = extract(good)
        if check(err is None, "_remote_extract: the good tgz is read", repr(err)):
            m, work, run = got
            check(isinstance(m, dict) and set(work) == {"lib/price.js"} and set(run) == {"REVIEW.md", "x.traj.json"},
                  "_remote_extract: work and run keys", f"{set(work)} {set(run)}")
        no_man = [x for x in good if x[0] != "./manifest.json"]
        rejects = {
            "a symlink member": good + [("work/ln", "sym", "lib/price.js")],
            "a hardlink member": good + [("work/hl", "hard", "work/lib/price.js")],
            "a fifo member": good + [("work/ff", "fifo", None)],
            "work/../x": good + [("work/../x", "file", b"x")],
            "work/.git/config": good + [("work/.git/config", "file", b"x")],
            "work/.venv/x": good + [("work/.venv/x", "file", b"x")],
            "work//abs": good + [("work//abs", "file", b"x")],
            "/work/x": good + [("/work/x", "file", b"x")],
            "work/A.py with work/a.py": good + [("work/A.py", "file", b"x"), ("work/a.py", "file", b"y")],
            "run/evil.sh": good + [("run/evil.sh", "file", b"x")],
            "run/REVIEW.md twice": good + [("run/REVIEW.md", "file", b"again")],
            "run/sub/REVIEW.md": good + [("run/sub/REVIEW.md", "file", b"x")],
            "other/x": good + [("other/x", "file", b"x")],
            "a top-level x.py": good + [("x.py", "file", b"x")],
            "two manifest.json": good + [("manifest.json", "file", man)],
            "no manifest": no_man,
            "manifest [1]": no_man + [("manifest.json", "file", b"[1]")],
            "manifest {": no_man + [("manifest.json", "file", b"{")],
        }
        for label, members in rejects.items():
            got, err = extract(members)
            check(isinstance(err, ValueError), f"_remote_extract: ValueError for {label}", f"got {err!r} / {got is not None}")
        tiny = b"{}"  # the manifest counts toward the file and total caps too: keep it small so each cap is tested alone
        five = [("manifest.json", "file", tiny), ("work/f.txt", "file", b"12345")]
        two = [("manifest.json", "file", tiny), ("work/f.txt", "file", b"12345"), ("work/g.txt", "file", b"12345")]
        four = [("manifest.json", "file", tiny), ("work/a", "file", b"1"), ("work/b", "file", b"2"), ("work/c", "file", b"3")]
        long_man = [("manifest.json", "file", man), ("work/a", "file", b"1")]
        for label, members, patch in (("REMOTE_FILE_MAX=4 (a 5-byte file)", five, {"REMOTE_FILE_MAX": 4}),
                                      ("REMOTE_TOTAL_MAX=8 (two 5-byte files)", two, {"REMOTE_TOTAL_MAX": 8}),
                                      ("REMOTE_MEMBERS_MAX=3 (4 members)", four, {"REMOTE_MEMBERS_MAX": 3}),
                                      ("REMOTE_MANIFEST_MAX=10 (a longer manifest)", long_man, {"REMOTE_MANIFEST_MAX": 10})):
            got, err = extract(members)
            check(err is None, f"_remote_extract: unpatched control passes for {label}", repr(err))
            got, err = extract(members, **patch)
            check(isinstance(err, ValueError), f"_remote_extract: ValueError for {label}", f"got {err!r}")

    # 8. _remote_manifest
    with guard("_remote_manifest"):
        check(R._remote_manifest(base_man()) is None, "_remote_manifest: a clean manifest is fine", R._remote_manifest(base_man()))
        cases = [({"v": 2}, "v"), ({"rc": True}, "rc"), ({"rc": "0"}, "rc"), ({"rc": None}, None), ({"result": 5}, "result"),
                 ({"wall_hit": None}, "wall_hit/terminated"), ({"elapsed_s": True}, "elapsed_s"), ({"real": {"w": 5}}, "real"),
                 ({"remote_home": None}, "remote paths"), ({"changed": ["a", 5]}, "file lists"), ({"modes": {"a": "644"}}, "modes"),
                 ({"modes": {"a": True}}, "modes"), ({"skipped": [{"path": "a"}]}, "skipped"), ({"error": 5}, "error")]
        for over, want in cases:
            got = R._remote_manifest(base_man(**over))
            check(got == want, f"_remote_manifest({over}) -> {want!r}", f"got {got!r}")
        m = base_man(error="x")
        for k in ("changed", "deleted", "run_files", "skipped", "modes"):
            del m[k]
        check(R._remote_manifest(m) is None, "_remote_manifest: error='x' with the lists and modes removed -> None", R._remote_manifest(m))

    # 9. _remote_rewriter, _rewrite_json
    with guard("_remote_rewriter"):
        man = {"remote_work": "/rh/run/j/work", "remote_run_dir": "/rh/run/j", "remote_home": "/rh", "remote_checkout": "/",
               "real": {"work": "/real/rh/run/j/work", "home": 5, "checkout": "rel/x"}}
        pairs = [("work", "/L/w"), ("run_dir", "/L/r"), ("home", "/L/h"), ("checkout", "/L/c")]
        rw = R._remote_rewriter(man, pairs)
        for src, want in (("/rh/run/j/work/a.py", "/L/w/a.py"), ("/rh/run/j/TASK.md", "/L/r/TASK.md"), ("/rh/run/j/work2", "/L/r/work2"),
                          ("/rh/run/j-old/x", "/L/h/run/j-old/x"), ("/rhx/y", "/rhx/y"), ("/real/rh/run/j/work", "/L/w"),
                          ("/rh/run/j/work.", "/L/w."), ("/other", "/other")):
            check(rw(src) == want, f"_remote_rewriter: {src!r} -> {want!r}", f"got {rw(src)!r}")
        idn = R._remote_rewriter({"remote_checkout": "/", "real": {}}, pairs)
        check(idn("/rh/run/j/work/a.py") == "/rh/run/j/work/a.py" and idn("x") == "x", "_remote_rewriter: nothing to substitute -> identity")
        got = R._rewrite_json({"/rh/run/j/work": ["/rh/run/j/work/a", {"k": "/rh"}, 5, None, True]}, rw)
        want = {"/rh/run/j/work": ["/L/w/a", {"k": "/L/h"}, 5, None, True]}
        check(got == want, "_rewrite_json: string values (nested too) rewritten, keys never", f"got {got!r}")

    # 10. _remote_fspec
    with guard("_remote_fspec"):
        files = ["lib/price.js", "lib/cart.js", "lib/price.test.js", "lib/a.test.js", "lib/b.test.js", "lib/c.test.js"]
        good = {"mode": "two-phase", "allowed": ["lib/price.js"], "test_file": "lib/bulk.test.js", "drop": ["lib/old.test.js"],
                "target_fn": "bulkTotal", "test_cmd": "node --test /rh/run/j/work/lib/bulk.test.js", "baseline_from": "/rj/checkout",
                "task": "/rh/run/j/TASK.fix.md"}
        man = {"remote_work": "/rh/run/j/work", "remote_run_dir": "/rh/run/j", "remote_home": "/rh", "remote_checkout": "/rj/checkout"}
        rw = R._remote_rewriter(man, [("work", "/L/w"), ("run_dir", "/L/r"), ("home", "/L/h"), ("checkout", "/L/c")])
        wt, run_dir, remote = Path("/L/wt"), Path("/L/run"), {"python": "3.12", "max_calls": 7}

        def fspec(raw):
            raw = raw if isinstance(raw, bytes) else json.dumps(raw).encode()
            return R._remote_fspec(raw, rw, wt, run_dir, remote, files)

        fs, why = fspec(good)
        if check(fs is not None and why is None, "_remote_fspec: the good spec is accepted", why):
            check(fs["test_cmd"] == "node --test /L/w/lib/bulk.test.js", "_remote_fspec: test_cmd rewritten", fs["test_cmd"])
            check(fs["baseline_from"] == str(wt), "_remote_fspec: baseline_from == str(wt)", fs["baseline_from"])
            check(fs["task"] == str(run_dir / "TASK.fix.md"), "_remote_fspec: task == run_dir/TASK.fix.md", fs["task"])
            check(fs["remote"] == remote, "_remote_fspec: remote is the one passed in", fs.get("remote"))
            check(fs["allowed"] == good["allowed"] and fs["test_file"] == good["test_file"] and fs["drop"] == good["drop"],
                  "_remote_fspec: allowed / test_file / drop unchanged")
        ut3 = ["lib/price.test.js", "lib/a.test.js", "lib/b.test.js"]
        fs, why = fspec({**good, "update_tests": ut3})
        check(fs is not None and why is None and fs["update_tests"] == ut3,
              "_remote_fspec: update_tests of up to 3 tracked existing test files is accepted, kept as it came", why)
        rej = [("mode auto", {**good, "mode": "auto"}, "two-phase"),
               ("allowed with 2 files", {**good, "allowed": ["lib/price.js", "lib/cart.js"]}, "allowed"),
               ("allowed an untracked file", {**good, "allowed": ["lib/new.js"]}, "allowed"),
               ("allowed a str", {**good, "allowed": "lib/price.js"}, "allowed"),
               ("allowed .git/config", {**good, "allowed": [".git/config"]}, "allowed"),
               ("test_file ../t.js", {**good, "test_file": "../t.js"}, "test_file"),
               ("test_file None", {**good, "test_file": None}, "test_file"),
               ("drop 'x.js'", {**good, "drop": "x.js"}, "drop"),
               ("update_tests ['/abs.js']", {**good, "update_tests": ["/abs.js"]}, "update_tests"),
               ("update_tests with 4 files", {**good, "update_tests": [*ut3, "lib/c.test.js"]}, "update_tests"),
               ("update_tests an untracked file", {**good, "update_tests": ["lib/new.test.js"]}, "update_tests"),
               ("update_tests a file that is not a test", {**good, "update_tests": ["lib/cart.js"]}, "update_tests"),
               ("update_tests the allowed file", {**good, "update_tests": ["lib/price.js"]}, "update_tests"),
               ("update_tests the test_file", {**good, "test_file": "lib/price.test.js", "update_tests": ["lib/price.test.js"]}, "update_tests"),
               ("update_tests a str", {**good, "update_tests": "lib/price.test.js"}, "update_tests"),
               ("target_fn ''", {**good, "target_fn": ""}, "target_fn"),
               ("target_fn []", {**good, "target_fn": []}, "target_fn"),
               ("target_fn ['a', 1]", {**good, "target_fn": ["a", 1]}, "target_fn"),
               ("new_names 'RATE'", {**good, "new_names": "RATE"}, "new_names"),
               ("raw '{'", b"{", "does not parse"),
               ("raw '[1]'", b"[1]", "two-phase"),
               ("raw 100000 nested lists", b"[" * 100000 + b"]" * 100000, "does not parse"),
               ("raw b'\\xff'", b"\xff", "does not parse")]
        def small_stack(fn, *a):
            """fn(*a) on a thread with a 1 MiB stack: CPython 3.14 sizes its C recursion limit from the stack, so with `ulimit -s unlimited`
            (the sandbox) json.loads parses 10**6 nested lists and never raises the RecursionError this input is meant to cause."""
            box = []
            threading.stack_size(1 << 20)
            try:
                t = threading.Thread(target=lambda: box.append(fn(*a)))
                t.start()
                t.join(120)
            finally:
                threading.stack_size(0)
            return box[0] if box else (None, "the call did not finish")

        for label, raw, sub in rej:
            fs, why = small_stack(fspec, raw) if "nested" in label else fspec(raw)
            check(fs is None and isinstance(why, str) and sub in why, f"_remote_fspec rejects {label} ({sub!r})", f"got {fs is not None}, {why!r}")

    # 11. _remote_overlay
    with guard("_remote_overlay"):
        def tree(name):
            cwd = new_tmp("lane-overlay-") / name
            (cwd / "lib").mkdir(parents=True)
            for rel, txt in (("lib/price.js", "P"), ("lib/cart.js", "C"), ("lib/cart2.js", "C2"), ("lib/gone.js", "G"),
                             ("lib/old.test.js", "O"), ("tool.sh", "T"), ("same.txt", "S")):
                (cwd / rel).write_text(txt)
                os.chmod(cwd / rel, 0o644)
            os.symlink("price.js", cwd / "lib" / "alias.js")
            return cwd

        fspec = {"allowed": ["lib/price.js"], "test_file": "lib/bulk.test.js", "drop": ["lib/old.test.js"]}
        work = {"lib/price.js": b"P2", "lib/bulk.test.js": b"T2", "lib/cart.js": b"C2", "same.txt": b"S", "tool.sh": b"T",
                "new/notes.txt": b"N", "x/__pycache__/m.pyc": b"\x00", ".coverage": b"c", "lib/alias.js": b"A"}
        modes = {k: 0o644 for k in work}
        modes["tool.sh"] = 0o755
        man = {"modes": modes, "deleted": ["lib/gone.js", "lib/old.test.js", "lib/never.js"],
               "skipped": [{"path": "run/REVIEW.md", "why": "size 999999999"}, {"path": "big.bin", "why": "size 999999999"},
                           {"path": "lib/cart2.js", "why": "symlink"}, {"path": "caf�.txt", "why": "not a UTF-8 path"}]}
        cwd = tree("main")
        viol, notes, infra = R._remote_overlay(cwd, man, work, fspec)
        check(viol == ["lib/alias.js", "lib/cart.js", "tool.sh", "lib/gone.js", "lib/cart2.js"], "_remote_overlay: violations", viol)
        check(notes == ["new/notes.txt", "run/REVIEW.md", "big.bin", "caf�.txt"], "_remote_overlay: notes", notes)
        check(infra is None, "_remote_overlay: infra is None", infra)
        check((cwd / "lib/price.js").read_text() == "P2" and (cwd / "lib/bulk.test.js").read_text() == "T2",
              "_remote_overlay: price.js and bulk.test.js written")
        check(not os.path.lexists(cwd / "lib/old.test.js"), "_remote_overlay: old.test.js removed")
        check((cwd / "lib/cart.js").read_text() == "C" and (cwd / "lib/gone.js").read_text() == "G",
              "_remote_overlay: cart.js and gone.js untouched")
        check(stat.S_IMODE(os.lstat(cwd / "tool.sh").st_mode) == 0o644 and (cwd / "tool.sh").read_text() == "T",
              "_remote_overlay: tool.sh still 0644")
        check(not os.path.lexists(cwd / "new/notes.txt"), "_remote_overlay: no new/notes.txt")
        check(os.path.islink(cwd / "lib/alias.js"), "_remote_overlay: alias.js still a symlink")
        # infra
        man2 = {"modes": {}, "deleted": [], "skipped": [{"path": "lib/price.js", "why": "size 999999999"}]}
        viol, notes, infra = R._remote_overlay(tree("i1"), man2, {}, fspec)
        check(isinstance(infra, str) and "came back without its content" in infra, "_remote_overlay: a skipped job file -> infra", infra)
        man3 = {"modes": {}, "deleted": ["../x"], "skipped": []}
        viol, notes, infra = R._remote_overlay(tree("i2"), man3, {}, fspec)
        check(isinstance(infra, str) and "an unsafe deleted path" in infra, "_remote_overlay: deleted '../x' -> infra", infra)
        fs3 = {"allowed": ["lib/price.js"], "test_file": "lib/price.js/t.js"}
        try:
            R._remote_overlay(tree("i3"), {"modes": {}, "deleted": [], "skipped": []}, {"lib/price.js/t.js": b"x"}, fs3)
            check(False, "_remote_overlay: test_file under a file raises ValueError", "no exception")
        except ValueError:
            check(True, "_remote_overlay: test_file under a file raises ValueError")


# ===================================================================================================================== host
JID = "abcdef0123456789abcdef01"
TAG = "lane=remote:abcdef012345"
LLM0 = {"calls": 5, "errors": 0, "held": 0, "hold_wait_s": 0}
PASS_RES = "RESULT PASS mode=auto target=lib/price.js:bulkTotal diag_s=1 fix_s=2"
DEL = object()  # in a manifest override: remove the key
DEF = object()  # "the helper's default"


def _bytes(v):
    return v if isinstance(v, bytes) else Path(v).read_bytes()


def spec_auto(name, **over):
    s = {"mode": "two-phase", "allowed": ["lib/price.js"], "test_file": "lib/bulk.test.js", "target_fn": "bulkTotal",
         "test_cmd": f"node --test /rh/run/{name}/work/lib/bulk.test.js", "baseline_from": "/rj/checkout"}
    s.update(over)
    return json.dumps(s, indent=1).encode()


def std_run(name, over=None, spec=DEF):
    """The run files of a finished remote auto run, as the remote wrote them (paths are the remote's)."""
    r = {"spec.auto.json": spec_auto(name) if spec is DEF else spec, "REVIEW.md": f"# review\nthe work dir: /rh/run/{name}/work\n".encode(),
         "DIAGNOSIS.md": T.GOOD_DIAG.encode(), "TASK.fix.md": f"fix it in /rh/run/{name}/work\n".encode()}
    r.update(over or {})
    if spec is None:
        r.pop("spec.auto.json")
    return r


def host():
    root = new_tmp("lane-host-")
    REPO = T.mkrepo(root, "repo")
    old_pf = R.PAUSE_FILE[0]

    def result(kw, man_over=None, work=None, run=None, exit_code=0, infra=None, artifacts=DEF, path=True, llm=DEF, jid=JID, raw=None,
               timed_out=False):
        """What submit_cpu_job returns: writes the artifacts tgz to kw["artifacts_dest"] (manifest.json, work/<rel>, run/<name>)."""
        dest = Path(kw["artifacts_dest"])
        name = dest.parent.name
        work, run = dict(work or {}), dict(run or {})
        man = {"v": 1, "rc": 0, "result": PASS_RES, "wall_hit": False, "terminated": False, "elapsed_s": 12, "remote_home": "/rh",
               "remote_run_dir": f"/rh/run/{name}", "remote_work": f"/rh/run/{name}/work", "remote_checkout": "/rj/checkout",
               "real": {"home": "/rh", "run_dir": f"/rh/run/{name}", "work": f"/rh/run/{name}/work", "checkout": "/rj/checkout"},
               "changed": sorted(work), "deleted": [], "run_files": sorted(run), "skipped": [], "modes": {k: 0o644 for k in work}}
        for k, v in (man_over or {}).items():
            if v is DEL:
                man.pop(k, None)
            else:
                man[k] = v
        if raw is not None:
            dest.write_bytes(raw)
        elif path:
            members = [("manifest.json", "file", json.dumps(man).encode())]
            members += [(f"work/{k}", "file", v) for k, v in sorted(work.items())]
            members += [(f"run/{k}", "file", v) for k, v in sorted(run.items())]
            dest.write_bytes(tgz_bytes(members))
        return CJ.CpuJobResult(exit_code=exit_code, timed_out=timed_out, job_id=jid, infra_error=infra,
                               artifacts={"bytes": 1} if artifacts is DEF else artifacts,
                               artifacts_path=str(dest) if (path or raw is not None) else None,
                               llm=dict(LLM0) if llm is DEF else llm)

    def fin_case(name, build, spec_extra=None, desc=T.DESC, pre=None, repo=None):
        """R.remote_auto() in-process with a fake submit_cpu_job (build(kw) -> its result)."""
        run = root / name
        (run / "work").mkdir(parents=True)
        (run / "TASK.md").write_text(desc)
        spec = {"mode": "auto", "baseline_from": str(repo or REPO), "task": str(run / "TASK.md"), "timeout": 1800, "files": ["price.js"],
                "remote": {"python": "3.12"}}
        spec.update(spec_extra or {})
        calls = []

        def submit(wt, cmd, timeout_s=600, **kw):
            calls.append(SimpleNamespace(wt=wt, cmd=cmd, timeout_s=timeout_s, kw=kw))
            return build(kw)

        sigs = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
        R.PAUSE_FILE[0] = str(run / "work.provider-pause.json")
        try:
            with patched(R, _cpu_job_lib=lambda: SimpleNamespace(submit_cpu_job=submit)):
                if pre:
                    pre(run)
                ret, out, exc = capture(R.remote_auto, SimpleNamespace(cwd=str(run / "work"), model=None), spec)
        finally:
            R.PAUSE_FILE[0] = old_pf
        check({s: signal.getsignal(s) for s in sigs} == sigs, f"{name}: the SIGTERM/SIGINT handlers are the same after the call")
        res = [ln for ln in out.splitlines() if ln.startswith("RESULT ")]
        return SimpleNamespace(name=name, rc=ret, out=out, run=run, cwd=run / "work", calls=calls, exc=exc, res=res, spec=spec,
                               pf=run / "work.provider-pause.json")

    def verdict(c, rc, has=(), cls=None, tag=True, nres=1, absent=()):
        """One RESULT line (or none), the exit code, substrings, the class (once, last) and the lane tag."""
        n = c.name
        check(c.exc is None, f"{n}: the run did not abort", getattr(c.exc, "args", None))
        check(c.rc == rc, f"{n}: exit code {rc}", f"{c.rc!r}; output: {c.out[-300:]}")
        check(len(c.res) == nres, f"{n}: {nres} RESULT line(s)", f"{len(c.res)}: {c.res}")
        line = c.res[0] if c.res else ""
        for s in has:
            check(s in line, f"{n}: the RESULT contains {s!r}", line)
        for s in absent:
            check(s not in line, f"{n}: the RESULT does not contain {s!r}", line)
        if cls:
            check(line.endswith(f" class={cls}") and line.count(" class=") == 1, f"{n}: class {cls}, once, at the end", line)
        if tag:
            check(TAG in line, f"{n}: the RESULT carries {TAG}", line)
        return line

    def simple(name, man_over=None, work=None, run=None, rc=1, has=(), cls=None, tag=True, **kw2):
        with guard(f"host {name}"):
            c = fin_case(name, lambda kw: result(kw, man_over=man_over, work=work, run=run, **kw2))
            verdict(c, rc, has=has, cls=cls, tag=tag)
            return c

    def pause_meta(c):
        try:
            return json.loads(c.pf.read_text()).get("pause_meta") or {}
        except (OSError, ValueError):
            return None

    def parked(c, label):
        m = pause_meta(c)
        verdict(c, 3, nres=0, tag=False)
        check(m is not None, f"{label}: the pause file is written", str(c.pf))
        lc = (m or {}).get("lane_cause", "")
        check(lc.startswith("darkbloom capacity wait: remote:"), f"{label}: pause_meta.lane_cause", lc)
        check("[worker] RESUMABLE TRANSCRIPT:" in c.out, f"{label}: the RESUMABLE TRANSCRIPT line is printed", c.out[-300:])
        return lc

    # ---------------------------------------------------------------------------------------------------- the PASS case
    with guard("host pass"):
        auto_state = b'{"diagnosis_key": "k1", "work": "/rh/run/pass/work"}\n'
        traj = b'{"messages": [], "cwd": "/rh/run/pass/work"}\n'
        pwork = {"lib/price.js": T.FIXED.encode(), "lib/bulk.test.js": T.NEW_TEST.encode()}

        def build(kw):
            kw["on_log"](b"RESULT PASS mode=auto forged\n")
            return result(kw, work=pwork, run=std_run("pass", {"auto-state.json": auto_state, "x.traj.json": traj,
                                                               "REVIEW-AUTO.md": b"FORGED\n",
                                                               "TASK-D.md": b"the venv: /rh/.ollama-dispatch/mini-venv\n"}))

        c = fin_case("pass", build)
        line = verdict(c, 0, has=("target=lib/price.js:bulkTotal",), tag=False)
        check(line.startswith("RESULT PASS mode=auto") and line.endswith(TAG), "pass: RESULT PASS mode=auto ... ends with the lane tag", line)
        check("[remote] RESULT PASS mode=auto forged" in c.out, "pass: the remote's own RESULT text is shown with the [remote] prefix", c.out[:400])
        cwd, run = c.cwd, c.run
        rd = lambda p: p.read_text() if p.is_file() else None  # noqa: E731
        check(rd(cwd / "lib/price.js") == T.FIXED, "pass: lib/price.js is the remote's fix")
        check(rd(cwd / "lib/bulk.test.js") == T.NEW_TEST, "pass: lib/bulk.test.js is the remote's test")
        check(rd(cwd / "lib/cart.js") == T.CART, "pass: lib/cart.js is untouched")
        check(rd(run / "TASK.fix.md") == f"fix it in {cwd}\n", "pass: TASK.fix.md has the local work dir", rd(run / "TASK.fix.md"))
        check((cwd / "TASK.md").is_file() and (run / "TASK.fix.md").is_file() and (cwd / "TASK.md").read_bytes() == (run / "TASK.fix.md").read_bytes(),
              "pass: cwd/TASK.md is the rewritten TASK.fix.md")
        rv = rd(run / "REVIEW.md")
        check(rv == f"# review\nthe work dir: {cwd}\n" and "/rh/run/pass/work" not in (rv or ""), "pass: REVIEW.md has the local work dir", rv)
        check((run / "auto-state.json").is_file() and (run / "auto-state.json").read_bytes() == auto_state, "pass: auto-state.json is byte for byte")
        check((run / "x.traj.json").is_file() and (run / "x.traj.json").read_bytes() == traj, "pass: x.traj.json is byte for byte")
        check((run / "DIAGNOSIS.md").is_file() and (run / "DIAGNOSIS.md").read_bytes() == T.GOOD_DIAG.encode(), "pass: DIAGNOSIS.md is byte for byte")
        # beyond the spec (found by mutation sanity): a path under the remote home but outside the run dir goes to this host's home
        nt = rd(run / "TASK-D.md")
        check(nt == f"the venv: {R.HOME}/.ollama-dispatch/mini-venv\n", "pass: (extra) a remote-home path in a run file is rewritten to this host's home", nt)
        try:
            fs = json.loads((run / "spec.auto.json").read_text())
        except (OSError, ValueError) as e:
            fs = {}
            check(False, "pass: spec.auto.json is readable JSON", repr(e))
        check(fs.get("baseline_from") == str(REPO), "pass: spec.auto.json baseline_from is the local repo", fs.get("baseline_from"))
        check(fs.get("task") == str(run / "TASK.fix.md"), "pass: spec.auto.json task is the local TASK.fix.md", fs.get("task"))
        check(fs.get("remote") == {"python": "3.12"}, "pass: spec.auto.json keeps remote", fs.get("remote"))
        check(fs.get("test_cmd") == f"node --test {cwd}/lib/bulk.test.js", "pass: spec.auto.json test_cmd is the local path", fs.get("test_cmd"))
        ra = rd(run / "REVIEW-AUTO.md") or ""
        check("+  if (qty >= 10) {" in ra, "pass: REVIEW-AUTO.md has the fix diff", ra[-600:])
        check(TAG in ra and "FORGED" not in ra, "pass: REVIEW-AUTO.md has the lane tag and not the forged file", ra[:300])
        # what was submitted
        check(len(c.calls) == 1, "pass: one job submitted", len(c.calls))
        if c.calls:
            k = c.calls[0]
            kw = k.kw
            check(k.wt == str(REPO), "pass: the job's worktree is baseline_from", k.wt)
            check(kw.get("stage") == "lane", "pass: stage lane", kw.get("stage"))
            check(kw.get("label") == "lane pass", "pass: label", kw.get("label"))
            check(kw.get("fallback") is False, "pass: fallback False", kw.get("fallback"))
            check(kw.get("claim_timeout_s") == 1800, "pass: claim_timeout_s 1800", kw.get("claim_timeout_s"))
            check(kw.get("llm") == {"models": [R.DEFAULT_MODEL], "max_calls": 1500}, "pass: llm", kw.get("llm"))
            check((kw.get("setup") or {}).get("timeout_s") == 1800, "pass: setup timeout_s 1800", kw.get("setup"))
            check(k.timeout_s == 9300, "pass: timeout_s 9300 (wall 3300 + 600 + the 5400 s hold allowance)", k.timeout_s)
            check(kw.get("artifacts") is True, "pass: artifacts True", kw.get("artifacts"))
            check(kw.get("owner") == "mini-runner auto pass", "pass: owner", kw.get("owner"))
            check(kw.get("api") == R.REMOTE_API, "pass: api", kw.get("api"))
            for s in ("--wall=3300", "--hold-allow=5400", "--outage-max=1200", "--name=pass", "--model=qwen3.6-35b-a3b-vl-mtp-mxfp8"):
                check(s in k.cmd, f"pass: the run command has {s}", k.cmd)
            scmd = (kw.get("setup") or {}).get("cmd", "")
            check("--python=3.12" in scmd and "--deps-lock" not in scmd, "pass: the setup command has --python=3.12 and no --deps-lock", scmd)
            tools = kw.get("tools") or {}
            check(_bytes(tools["lane/TASK.md"]) == T.DESC.encode() if "lane/TASK.md" in tools else False, "pass: lane/TASK.md is the description")
            try:
                sj = json.loads(_bytes(tools["lane/spec.json"]))
            except (KeyError, OSError, ValueError) as e:
                sj = {}
                check(False, "pass: lane/spec.json is readable JSON", repr(e))
            check(sj.get("name") == "pass" and sj.get("remote") == {"python": "3.12"}, "pass: lane/spec.json has the name and remote", sj)
            check("lane/mini.lock" in tools and _bytes(tools["lane/mini.lock"]) == EXPECTED_LOCK, "pass: lane/mini.lock is the expected lock")
            want_tl = R._lane_tools_lock()
            check(want_tl is not None and b"ruff==" in want_tl and "lane/tools.lock" in tools and _bytes(tools["lane/tools.lock"]) == want_tl,
                  "pass: lane/tools.lock pins the lane's ruff", tools.get("lane/tools.lock"))
            check('--tools-lock="$JOB_TOOLS/lane/tools.lock"' in scmd, "pass: the setup command installs the lane tools", scmd)
            miss = [x for x in R.REMOTE_TOOLS if f"bin/{x}" not in tools]
            check(not miss, "pass: every tool is shipped", miss)
            miss = [f"bin/configs/{Path(p).name}" for p in (R.OVERLAY, R.DIAG_OVERLAY, R.TEST_OVERLAY, R.POLISH_OVERLAY)
                    if f"bin/configs/{Path(p).name}" not in tools]
            check(not miss, "pass: the 4 config overlays are shipped", miss)
            check("lane/auto-state.json" not in tools and "lane/DIAGNOSIS.md" not in tools and "lane/deps.lock" not in tools,
                  "pass: no auto-state / DIAGNOSIS / deps.lock in the first launch", sorted(x for x in tools if x.startswith("lane/")))

    # ---------------------------------------------------------------------------------------------------- RESULT cases
    wrong = T.PRICE.replace("qty > 10", "qty > 5")
    with guard("host failfix"):
        c = simple("failfix", {"rc": 1, "result": "RESULT FAIL mode=auto phase=fix tests=red class=TEST"},
                   work={"lib/price.js": wrong.encode(), "lib/bulk.test.js": T.NEW_TEST.encode()}, run=std_run("failfix"),
                   has=("FAIL mode=auto phase=fix tests=red",), cls="MODEL")
        check(c.res and c.res[0].count(" class=") == 1, "failfix: ' class=' appears once", c.res)
        check((c.cwd / "lib/price.js").is_file() and (c.cwd / "lib/price.js").read_text() == wrong, "failfix: the wrong fix landed in the work dir")
    with guard("host wall"):
        c = fin_case("wall", lambda kw: result(kw, {"wall_hit": True, "rc": 1, "result": None}))
        verdict(c, 1, has=("timeout-total remote wall 3300s",), cls="SIZE", absent=("provider waits",))
    with guard("host wall, a credit under 1 s"):
        c = fin_case("wallsmall", lambda kw: result(kw, {"wall_hit": True, "rc": 1, "result": None, "credited_s": 0.4, "hold_allow_s": 5400}))
        verdict(c, 1, has=("timeout-total remote wall 3300s",), cls="SIZE", absent=("provider waits",))
    with guard("host terminated"):
        simple("term", {"terminated": True, "rc": 1}, has=("the runner stopped the remote run",), cls="INFRA")
    with guard("host no RESULT"):
        simple("norc", {"result": None, "rc": 2}, has=("ended without a RESULT (exit 2)",))
    with guard("host rc1 PASS"):
        simple("rc1pass", {"result": "RESULT PASS mode=auto x", "rc": 1}, has=("not an auto PASS (exit 1)",))
    with guard("host two-phase PASS"):
        simple("twophase", {"result": "RESULT PASS mode=two-phase x"}, has=("not an auto PASS",))
    with guard("host PASS without a spec"):
        simple("nospec", {"result": "RESULT PASS mode=auto x"}, has=("(no spec.auto.json came back)",))
    with guard("host unusable spec"):
        c = simple("badallowed", {"result": "RESULT PASS mode=auto x"}, run=std_run("badallowed", spec=spec_auto("badallowed", allowed=["lib/new.js"])),
                   has=("(its allowed is not one tracked file)",))
        check("the remote spec.auto.json is not used" in c.out, "badallowed: the output says the remote spec.auto.json is not used", c.out[-400:])
    with guard("host scope"):
        cart2 = T.CART + "// touched by the remote run\n"
        c = simple("scope", work={"lib/price.js": T.FIXED.encode(), "lib/bulk.test.js": T.NEW_TEST.encode(), "lib/cart.js": cart2.encode()},
                   run=std_run("scope"), has=("phase=fix remote-scope lib/cart.js",), cls="INFRA")
        check((c.cwd / "lib/price.js").is_file() and (c.cwd / "lib/price.js").read_text() == T.FIXED, "scope: price.js landed")
        check((c.cwd / "lib/cart.js").is_file() and (c.cwd / "lib/cart.js").read_text() == T.CART, "scope: cart.js untouched")
    with guard("host lost job"):
        simple("lostjob", exit_code=None, infra="runner lost the job", path=False, has=("remote-infra runner lost the job",), cls="INFRA")

    # ---------------------------------------------------------------------------------------------------- pause / preempt / artifacts
    with guard("host model hold parks"):
        c = fin_case("holdpark", lambda kw: result(kw, exit_code=None, infra="cancelled: model hold", path=False))
        lc = parked(c, "holdpark")
        check(lc.startswith("darkbloom capacity wait: remote: the model hold outlasted"), "holdpark: lane_cause says the model hold outlasted", lc)
    with guard("host cancelled SIGTERM"):
        c = fin_case("cancelterm", lambda kw: result(kw, exit_code=None, infra="cancelled: SIGTERM", path=False))
        check(c.exc is not None and c.exc.args == ("preempted",) and c.rc is None, "cancelterm: aborted as preempted", (c.exc, c.rc))

    def raise_term(n):
        def build(kw):
            h = signal.getsignal(signal.SIGTERM)
            if h in (signal.SIG_DFL, signal.SIG_IGN, None):
                raise RuntimeError("no SIGTERM handler is installed during the submit")  # never kill the test process itself
            for _ in range(n):
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.05)
            return result(kw, work={"lib/price.js": T.FIXED.encode(), "lib/bulk.test.js": T.NEW_TEST.encode()}, run=std_run("sig%d" % n))
        return build

    for n in (1, 2):
        with guard(f"host {n} SIGTERM"):
            c = fin_case("sig%d" % n, raise_term(n))
            check(c.exc is not None and c.exc.args == ("preempted",) and c.rc is None, f"sig{n}: {n} SIGTERM during the submit -> aborted as preempted",
                  (c.exc, c.rc, c.res, c.out[-200:]))
    with guard("host artifacts errors"):
        simple("arterr", artifacts={"error": "fetch failed: 500"}, path=False, has=("artifacts: fetch failed: 500",), cls="INFRA")
        simple("artnone", path=False, has=("artifacts: none came back",))
        simple("artgz", raw=b"this is not a gzip file", has=("artifacts: ReadError",))
        simple("manv2", {"v": 2}, has=("the manifest is malformed (v)",))
        simple("manerr", {"error": "collect: OSError: x", "changed": DEL, "deleted": DEL, "run_files": DEL, "skipped": DEL, "modes": DEL},
               has=("the remote run's files: collect: OSError: x",))
        simple("manmis", {"changed": ["lib/x.js"]}, has=("do not match the manifest",))
    with guard("host parks"):
        c = fin_case("parkrc3", lambda kw: result(kw, {"rc": 3, "result": None}))
        parked(c, "parkrc3")

        def build_outage(kw):
            kw["on_log"](b"[provider-outage] down\n")
            return result(kw, {"wall_hit": True, "rc": 1, "result": None})

        c = fin_case("parkoutage", build_outage)
        parked(c, "parkoutage")
        c = fin_case("parkheld", lambda kw: result(kw, {"wall_hit": True, "rc": 1, "result": None}, llm={"calls": 5, "errors": 0, "held": 2, "hold_wait_s": 0}))
        parked(c, "parkheld")

    # ------------------------------------------------------------------------------------- the pause: waits given back or not
    HELD2 = {"calls": 5, "errors": 0, "held": 2, "hold_wait_s": 0}

    def wall_case(name, over, llm=DEF, line=None):
        def build(kw):
            if line:
                kw["on_log"](line)
            return result(kw, {"wall_hit": True, "rc": 1, "result": None, **over}, llm=llm)
        return fin_case(name, build)

    with guard("host pause: the waits were given back"):
        c = wall_case("wallpaid", {"credited_s": 1234.5, "hold_allow_s": 5400}, llm=HELD2,
                      line=b"[provider-hold] the call took 400s, 380s of it in the relay; credited 380.0s\n")
        verdict(c, 1, has=("timeout-total remote wall 3300s (+1234s of provider waits)",), cls="SIZE")
        check(not c.pf.exists(), "wallpaid: a run that had its waits back and still hit its wall is a FAIL, not parked", c.out[-300:])
        c = wall_case("wallpaidoutage", {"credited_s": 60.0, "hold_allow_s": 5400}, line=b"[provider-outage] down; credited 60.0s\n")
        verdict(c, 1, has=("timeout-total remote wall 3300s (+60s of provider waits)",), cls="SIZE")
        check(not c.pf.exists(), "wallpaidoutage: an outage given back is not parked either", c.out[-300:])
    with guard("host pause: the waits were not all given back"):
        for nm, over in (("walldue", {"credited_s": 5400.0, "hold_allow_s": 5400}), ("wallover", {"credited_s": 6000.0, "hold_allow_s": 5400}),
                         ("wallnoallow", {"credited_s": 30.0, "hold_allow_s": 0}), ("wallstr", {"credited_s": "12", "hold_allow_s": 5400}),
                         ("wallbool", {"credited_s": True, "hold_allow_s": 5400}), ("wallnoha", {"credited_s": 12.0})):
            c = wall_case(nm, over, llm=HELD2)
            parked(c, nm)
        c = fin_case("termheld", lambda kw: result(kw, {"terminated": True, "rc": 1, "result": None, "credited_s": 10.0, "hold_allow_s": 5400},
                                                   llm=HELD2))
        parked(c, "termheld")

    # ---------------------------------------------------------------------------------------------------- run files / landing
    with guard("host refused diagnosed file"):
        def pre_copy(run):
            shutil.copytree(REPO, run / "work", dirs_exist_ok=True, symlinks=True)

        tok = spec_auto("token", allowed=["lib/token.js"], target_fn="refreshToken")
        c = fin_case("token", lambda kw: result(kw, work={"lib/token.js": (T.TOKEN + "// changed by the remote run\n").encode()},
                                                 run=std_run("token", spec=tok)), pre=pre_copy)
        verdict(c, 1, has=("phase=refused",), cls="SPEC")
        check((c.cwd / "lib/token.js").is_file() and (c.cwd / "lib/token.js").read_text() == T.TOKEN, "token: lib/token.js is not changed")
    with guard("host stale spec.auto.json"):
        def pre_stale(run):
            (run / "spec.auto.json").write_text("{}")

        c = fin_case("stale", lambda kw: result(kw, {"result": "RESULT FAIL mode=auto phase=diagnose x", "rc": 1}), pre=pre_stale)
        verdict(c, 1, has=("phase=diagnose",))
        check(not os.path.lexists(c.run / "spec.auto.json"), "stale: the stale spec.auto.json is deleted")
    with guard("host REVIEW.md symlink"):
        outside = root / "outside.md"
        outside.write_text("OUTSIDE\n")

        def pre_link(run):
            os.symlink(outside, run / "REVIEW.md")

        c = fin_case("revlink", lambda kw: result(kw, {"result": "RESULT FAIL mode=auto phase=diagnose x", "rc": 1},
                                                  run={"REVIEW.md": b"remote review\n"}), pre=pre_link)
        verdict(c, 1, has=("phase=diagnose",))
        rv = c.run / "REVIEW.md"
        check(rv.is_file() and not rv.is_symlink() and rv.read_bytes() == b"remote review\n", "revlink: REVIEW.md is a regular file with the remote's content")
        check(outside.read_text() == "OUTSIDE\n", "revlink: the file the symlink pointed to is unchanged", outside.read_text())
    with guard("host cannot land"):
        c = fin_case("cantland", lambda kw: result(kw, work={"lib/price.js": T.FIXED.encode(), "lib/price.js/t.js": b"x"},
                                                   run=std_run("cantland", spec=spec_auto("cantland", test_file="lib/price.js/t.js"))))
        verdict(c, 1, has=("remote-infra cannot land the files: ValueError",), cls="INFRA")

    # ---------------------------------------------------------------------------------------------------- a model hold
    def hold_build(sleep_after):
        seen = []

        def build(kw):
            kw["on_progress"]("waiting", "darkbloom sandbox hold: 2 models held")
            if sleep_after is None:  # poll cancel_if every 0.05 s, for at most 5 s
                t0, v = time.time(), None
                while time.time() - t0 < 5 and not v:
                    v = kw["cancel_if"]()
                    if not v:
                        time.sleep(0.05)
                seen.append(v)
                return result(kw, exit_code=None, infra="cancelled: model hold", path=False)
            kw["on_progress"]("running", "x")
            time.sleep(sleep_after)
            seen.append(kw["cancel_if"]())
            return result(kw, exit_code=None, infra="x", path=False)
        return build, seen

    with guard("host model hold outlasts REMOTE_OUTAGE_S"):
        build, seen = hold_build(None)
        with patched(R, REMOTE_OUTAGE_S=0.5):
            c = fin_case("hold1", build)
        check(seen == ["model hold"], "hold1: cancel_if returns 'model hold' once the hold has outlasted REMOTE_OUTAGE_S", seen)
        parked(c, "hold1")
    with guard("host model hold ends"):
        build, seen = hold_build(0.7)
        with patched(R, REMOTE_OUTAGE_S=0.5):
            c = fin_case("hold2", build)
        check(len(seen) == 1 and not seen[0], "hold2: cancel_if is still falsy 0.7 s after the hold ended", seen)
        verdict(c, 1, has=("remote-infra x",), cls="INFRA")

    # ---------------------------------------------------------------------------------------------------- pre-checks
    def pre_case(name, has, spec_extra=None, desc=T.DESC, repo=None):
        with guard(f"host precheck {name}"):
            c = fin_case(name, lambda kw: result(kw, exit_code=None, infra="x", path=False), spec_extra=spec_extra, desc=desc, repo=repo)
            check(not c.calls, f"{name}: the job is not submitted", len(c.calls))
            verdict(c, 1, has=(has,), cls="SPEC", tag=False)

    resell = root / "resell-mini"
    shutil.copytree(REPO, resell, symlinks=True)
    pre_case("emptydesc", "the task (description) is empty", desc="")
    pre_case("notroot", "baseline_from must be the root of a git worktree", {"baseline_from": str(REPO / "lib")})
    pre_case("loginform", "must-not-offload", desc="Fix the login form: it rejects valid passwords when the user types a trailing space.\n")
    pre_case("tokenhint", "must-not-offload", {"files": ["lib/token.js"]})
    pre_case("resellrepo", "must-not-offload", {"baseline_from": str(resell)})
    pre_case("skipnofn", "diagnose=skip needs", {"diagnose": "skip"})
    pre_case("remotestr", "remote must be an object", {"remote": "yes"})
    pre_case("py311", "remote.python must be one of", {"remote": {"python": "3.11"}})
    pre_case("depslock", "remote.deps_lock is not a file", {"remote": {"python": "3.12", "deps_lock": "/nonexistent"}})
    pre_case("maxcalls0", "remote.max_calls must be 1..2000", {"remote": {"python": "3.12", "max_calls": 0}})
    pre_case("maxcallsT", "remote.max_calls must be 1..2000", {"remote": {"python": "3.12", "max_calls": True}})
    pre_case("setuptmo", "remote.setup_timeout must be 60..1800 s", {"remote": {"python": "3.12", "setup_timeout": 30}})
    pre_case("timeout100", "timeout must be 300..14400 s", {"timeout": 100})  # the code reads the TOP-LEVEL spec["timeout"]

    # ---------------------------------------------------------------------------------------------------- heavy untracked dirs
    n_heavy = [0]

    def heavy_repo():
        n_heavy[0] += 1
        d = root / f"heavy{n_heavy[0]}"
        shutil.copytree(REPO, d, symlinks=True)
        return d

    def heavy(name, setup, want):
        with guard(f"host heavy {name}"):
            h = heavy_repo()
            setup(h)
            c = fin_case(name, lambda kw: result(kw, exit_code=None, infra="x", path=False), repo=h)
            check(not c.calls, f"{name}: the job is not submitted", len(c.calls))
            verdict(c, 1, has=("baseline_from has", want), cls="SPEC", tag=False)

    def accepted(name, setup):
        with guard(f"host accepted {name}"):
            h = heavy_repo()
            setup(h)
            c = fin_case(name, lambda kw: result(kw, exit_code=None, infra="x", path=False), repo=h)
            check(len(c.calls) == 1, f"{name}: the job is submitted", len(c.calls))
            verdict(c, 1, has=("remote-infra x",), cls="INFRA")

    def venv_dir(h):
        (h / ".venv" / "bin").mkdir(parents=True)
        (h / ".venv" / "bin" / "python").write_text("x")

    def sub_modules(h):
        (h / "sub" / "node_modules").mkdir(parents=True)
        (h / "sub" / "node_modules" / "x").write_text("x")

    def cafe_venv(h):
        (h / "café" / ".venv").mkdir(parents=True)
        (h / "café" / ".venv" / "x").write_text("x")

    def venv_excluded(h):
        venv_dir(h)
        (h / ".git" / "info").mkdir(exist_ok=True)
        with open(h / ".git" / "info" / "exclude", "a") as f:
            f.write(".venv/\n")

    heavy("venvdir", venv_dir, ".venv")
    heavy("venvfile", lambda h: (h / ".venv").write_text("x"), ".venv")
    heavy("submodules", sub_modules, "sub/node_modules")
    heavy("cafevenv", cafe_venv, ".venv")
    accepted("topvenvfile", lambda h: (h / "venv").write_text("x"))
    accepted("venvexcluded", venv_excluded)

    # ---------------------------------------------------------------------------------------------------- infra
    with guard("host no mini-swe-agent in the venv"):
        bad = fake_venv(new_tmp("lane-fv2-") / "fv2", GOOD_DISTS[:2])
        with patched(R, MINI=bad):
            c = fin_case("nolock", lambda kw: result(kw, exit_code=None, infra="x", path=False))
        check(not c.calls, "nolock: the job is not submitted", len(c.calls))
        verdict(c, 1, has=("remote-infra cannot pack the job: ValueError",), cls="INFRA", tag=False)
    with guard("host submit raises"):
        def build_raise(kw):
            raise RuntimeError("queue down")

        c = fin_case("qdown", build_raise)
        verdict(c, 1, has=("remote-infra RuntimeError: queue down",), cls="INFRA", tag=False)


# ===================================================================================================================== lane
def _git(cwd, *a):
    r = subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", "-C", str(cwd), *a],
                       capture_output=True, text=True, timeout=120)
    if r.returncode:
        raise RuntimeError(f"git {' '.join(a[:2])} failed: {r.stderr.strip()[-300:]}")
    return r.stdout


def lane_collect():
    root = new_tmp("lane-collect-")
    work, run_dir, art = root / "work", root / "run", root / "art"
    for d in (work, run_dir, art):
        d.mkdir()
    _git(work, "init", "-q")
    for n, t in (("a.py", "a1\n"), ("b.py", "b1\n"), ("keep.sh", "#!/bin/sh\n")):
        (work / n).write_text(t)
    _git(work, "add", "-A")
    _git(work, "commit", "-qm", "base")
    (work / "a.py").write_text("a2\n")
    os.chmod(work / "keep.sh", 0o755)
    (work / "b.py").unlink()
    (work / "new dir").mkdir()
    (work / "new dir" / "naïve x.py").write_text("n\n")
    (work / "__pycache__").mkdir()
    (work / "__pycache__" / "x.pyc").write_bytes(b"\0")
    (work / "stray.pyc").write_bytes(b"\0")
    os.symlink("a.py", work / "link.py")
    (work / "big.txt").write_text("x" * 64)
    raw_ok = True
    try:
        with open(os.fsencode(str(work)) + b"/caf\xe9.txt", "wb") as f:
            f.write(b"c\n")
    except OSError as e:
        raw_ok = False
        print(f"NOTE lane: this file system refuses a non-UTF-8 file name ({e}); that check is skipped", flush=True)
    (work / "inner").mkdir()
    _git(work / "inner", "init", "-q")
    (work / "inner" / "f.txt").write_text("x\n")
    (run_dir / "REVIEW.md").write_text("review\n")
    (run_dir / "notes.txt").write_text("notes\n")
    os.symlink("REVIEW.md", run_dir / "REVIEW-2.md")
    (run_dir / "x.traj.json").write_text("{}\n")
    (run_dir / "DIAGNOSIS.md").write_text("d" * 64)
    with patched(L, FILE_MAX=32):
        out = L.collect(work, run_dir, art, [10 ** 9])
    check("error" not in out, "collect: no error", out.get("error"))
    check(sorted(out["changed"]) == sorted(["a.py", "keep.sh", "new dir/naïve x.py"]), "collect: changed is a.py, keep.sh and the new file",
          out["changed"])
    check(out["modes"].get("keep.sh") == 0o755, "collect: keep.sh keeps mode 0o755", out["modes"])
    check(out["deleted"] == ["b.py"], "collect: deleted is b.py", out["deleted"])
    sk = {(s["path"], s["why"]) for s in out["skipped"]}
    wanted = [("link.py", "symlink"), ("big.txt", "size 64"), ("inner/", "not a regular file"), ("run/DIAGNOSIS.md", "size 64")]
    if raw_ok:
        wanted.append(("caf�.txt", "not a UTF-8 path"))
    for p, w in wanted:
        check((p, w) in sk, f"collect: skipped has {p!r} ({w})", sorted(sk))
    check(out["run_files"] == ["REVIEW.md", "x.traj.json"], "collect: run_files is REVIEW.md and x.traj.json", out["run_files"])
    pyc = [str(p.relative_to(art)) for p in art.rglob("*") if p.name.endswith((".pyc", ".pyo")) or p.name == "__pycache__"]
    check(not pyc, "collect: no .pyc anywhere in the artifacts", pyc)
    lists = out["changed"] + out["deleted"] + [s["path"] for s in out["skipped"]]
    check(not [x for x in lists if x.endswith((".pyc", ".pyo")) or "__pycache__" in x], "collect: no .pyc in the manifest lists", lists)
    check((art / "work" / "a.py").read_text() == "a2\n" and (art / "run" / "REVIEW.md").is_file(), "collect: changed files and review files copied")
    check(not (art / "run" / "DIAGNOSIS.md").exists() and not (art / "work" / "big.txt").exists(), "collect: a left-out file is not copied")
    out0 = L.collect(work, run_dir, root / "art0", [0])
    check(out0.get("changed") == [] and "error" not in out0, "collect: budget 0 -> nothing changed comes back", out0)
    ng = root / "nogit"
    ng.mkdir()
    with env_patched(GIT_CEILING_DIRECTORIES=str(root)):
        outg = L.collect(ng, run_dir, root / "art-ng", [10 ** 9])
    check("git status failed" in str(outg.get("error", "")), "collect: a non-git dir -> error 'git status failed'", outg)


def lane_walkup():
    base = new_tmp("lane-walk-")
    root = base / "root"
    root.mkdir()
    r = subprocess.run([sys.executable, "-m", "venv", "--without-pip", str(root / ".venv")], capture_output=True, text=True, timeout=300)
    if not check(r.returncode == 0, "walkup: the venv is created", (r.stdout + r.stderr)[-300:]):
        return
    venv_py = root / ".venv" / "bin" / "python"
    (root / "pkg").mkdir()
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "mod.py").write_text("WHO = 'root'\n")
    (root / "src" / "spkg").mkdir(parents=True)
    (root / "src" / "spkg" / "__init__.py").write_text("WHO = 'root-src'\n")
    sp = L.site_packages(root / ".venv")  # what cmd_setup writes, byte for byte, plus an editable install pointing at root
    (sp / "_lane_walkup.py").write_text(L.WALKUP)
    (sp / "_lane_walkup.pth").write_text("import _lane_walkup\n")
    (sp / "zz_editable.pth").write_text(str(root) + "\n")
    work = base / "work"
    shutil.copytree(root, work, ignore=shutil.ignore_patterns(".venv"))
    (work / "pkg" / "mod.py").write_text("WHO = 'work'\n")
    (work / "src" / "spkg" / "__init__.py").write_text("WHO = 'work-src'\n")
    os.symlink(root / ".venv", work / ".venv")
    child = ("import os, json, pkg.mod, spkg; print(json.dumps([pkg.mod.WHO, spkg.WHO, os.path.realpath(pkg.mod.__file__), "
             "os.path.realpath(os.environ.get('LANE_PROJECT_ROOT', ''))]))")
    parent = ("import subprocess, sys; " + child + "; print(subprocess.run([sys.executable, '-c', %r], cwd='/', "
              "capture_output=True, text=True).stdout)") % child

    def walk(cwd, **extra):
        env = {k: v for k, v in os.environ.items() if k not in ("LANE_PROJECT_ROOT", "PYTHONPATH")}
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env.update(extra)
        r = subprocess.run([str(venv_py), "-c", parent], cwd=str(cwd), env=env, capture_output=True, text=True, timeout=120)
        try:
            lines = r.stdout.strip().splitlines()
            return json.loads(lines[0]), json.loads(lines[1]), r.stderr
        except (ValueError, IndexError):
            return None, None, (r.stdout + r.stderr)[-400:]

    def want(who, src, tree):
        return [who, src, os.path.realpath(tree / "pkg" / "mod.py"), os.path.realpath(tree)]

    for label, cwd, extra, who, src, tree in (
            ("cwd work/pkg", work / "pkg", {}, "work", "work-src", work),
            ("cwd root", root, {}, "root", "root-src", root),
            ("cwd /", Path("/"), {}, "root", "root-src", root),
            ("cwd / with LANE_PROJECT_ROOT=work", Path("/"), {"LANE_PROJECT_ROOT": str(work)}, "work", "work-src", work)):
        me, kid, err = walk(cwd, **extra)
        check(me == want(who, src, tree), f"walkup {label}: the imports come from {who}", me if me is not None else err)
        check(kid == want(who, src, tree), f"walkup {label}: a child python started in / sees the same tree", kid if kid is not None else err)


FAKE_RUNNER = r'''#!/usr/bin/env python3
import json, os, shutil, signal, sys, time
from pathlib import Path

home = Path(os.environ["HOME"])
mode = (home / "fake-mode").read_text().strip()
argv = sys.argv[1:]
cwd = Path(argv[argv.index("--cwd") + 1])
spec = json.loads(Path(argv[argv.index("--task-file") + 1]).read_text())
run_dir = cwd.parent
if mode == "sleep":
    def _term(signum, frame):
        print("RESULT FAIL preempted", flush=True)
        os._exit(1)
    signal.signal(signal.SIGTERM, _term)
elif mode == "hang":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
seen = {"argv": argv, "spec": spec, "local": json.loads(Path(os.environ["DARKBLOOM_LOCAL_JSON"]).read_text()),
        "openai_key": "OPENAI_API_KEY" in os.environ, "lane_root": "LANE_PROJECT_ROOT" in os.environ,
        "outage": os.environ.get("MINI_OUTAGE_MAX_S"), "mini_bin": os.environ.get("MINI_BIN")}
(home / "fake-seen.tmp").write_text(json.dumps(seen))
os.replace(home / "fake-seen.tmp", home / "fake-seen.json")
shutil.copytree(spec["baseline_from"], cwd, symlinks=True, dirs_exist_ok=True)
if (home / "fake-lines").is_file():  # what mini_ctx prints for a provider wait, byte for byte
    sys.stdout.buffer.write((home / "fake-lines").read_bytes())
    sys.stdout.buffer.flush()
if mode == "pass":
    (cwd / "lib" / "price.js").write_text("FIXED\n")
    (cwd / "lib" / "new.test.js").write_text("// the new test\n")
    (cwd / "lib" / "token.js").unlink()
    (run_dir / "REVIEW.md").write_text("review\n")
    (run_dir / "spec.auto.json").write_text("{}\n")
    print("[mini-runner] fixing", flush=True)
    print("RESULT PASS mode=auto fake", flush=True)
    sys.exit(0)
if mode == "partial":
    sys.stdout.write("RESULT PASS partial")
    sys.stdout.flush()
    sys.exit(2)
if mode == "silent":
    sys.exit(2)
while True:
    time.sleep(1)
'''


def lane_exec(case, mode, name="n", wall=60, sig=None, timeout=90, hold_allow=None, lines=None, slack=None):
    """One `lane_remote.py run` (the wrapper of the spec) around the fake runner, in its own HOME. -> a namespace of what it left.
    lines: printed by the fake runner first; hold_allow: --hold-allow; slack: lane_remote's CREDIT_SLACK_S."""
    base = new_tmp(f"lane-run-{case}-")
    repo = T.mkrepo(base, "shop")
    h = base / "h"
    (h / "bin").mkdir(parents=True)
    (h / "bin" / "mini-swe-runner.py").write_text(FAKE_RUNNER)
    (h / "fake-mode").write_text(mode)
    if lines is not None:
        (h / "fake-lines").write_bytes("".join(x + "\n" for x in lines).encode())
    tools = base / "tools"
    (tools / "lane").mkdir(parents=True)
    (tools / "lane" / "TASK.md").write_text("The task text.\n")
    (tools / "lane" / "spec.json").write_text(json.dumps({"mode": "auto", "baseline_from": "/mac/repo", "task": "/mac/run/TASK.md", "timeout": 1800,
                                                         "name": name, "remote": {"python": "3.12"}}))
    (tools / "lane" / "auto-state.json").write_text('{"diagnose": "PASS"}\n')
    (tools / "lane" / "DIAGNOSIS.md").write_text("The diagnosis.\n")
    art = base / "art"
    art.mkdir()
    env = {k: v for k, v in os.environ.items() if k not in ("CPU_RUNNER_LLM_URL", "PYTHONPATH", "CPU_RUNNER_PROGRESS")}
    env.update(HOME=str(h), JOB_TOOLS=str(tools), CPU_RUNNER_ARTIFACTS=str(art), OPENAI_API_KEY="leak", LANE_PROJECT_ROOT="/x",
               PYTHONDONTWRITEBYTECODE="1")
    wrap = (f"import sys; sys.path.insert(0, {str(HERE)!r}); import lane_remote as L; L.WALL_GRACE_S = 2; L.TERM_GRACE_S = 1; "
            + (f"L.CREDIT_SLACK_S = {slack}; " if slack is not None else "") + "sys.exit(L.main(sys.argv[1:]))")
    p = spawn([sys.executable, "-c", wrap, "run", f"--name={name}", "--model=m", f"--wall={wall}"]
              + ([f"--hold-allow={hold_allow}"] if hold_allow is not None else []), cwd=str(repo), env=env,
              stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if sig:
        t_end = time.time() + 30
        while time.time() < t_end and not (h / "fake-seen.json").exists() and p.poll() is None:
            time.sleep(0.05)
        time.sleep(0.3)
        if p.poll() is None:
            os.kill(p.pid, sig)
    timed_out = False
    try:
        out = p.communicate(timeout=timeout)[0]
    except subprocess.TimeoutExpired:
        timed_out = True
        os.killpg(p.pid, signal.SIGKILL)
        out = p.communicate()[0]
    mp, sp_ = art / "manifest.json", h / "fake-seen.json"
    return SimpleNamespace(rc=p.returncode, out=out.decode("utf-8", "replace"), timed_out=timed_out, h=h, art=art, repo=repo,
                           man=json.loads(mp.read_text()) if mp.is_file() else None,
                           seen=json.loads(sp_.read_text()) if sp_.is_file() else None)


def lane_run():
    c = lane_exec("pass", "pass")
    check(not c.timed_out and c.rc == 0, "run pass: exit 0", (c.rc, c.out[-300:]))
    run_dir = c.h / "run" / "n"
    if check(c.man is not None and c.seen is not None, "run pass: manifest.json and fake-seen.json exist", c.out[-300:]):
        m, s = c.man, c.seen
        check(m["v"] == 1 and m["rc"] == 0 and m["result"] == "RESULT PASS mode=auto fake" and m["wall_hit"] is False
              and m["terminated"] is False, "run pass: manifest v, rc, result, wall_hit, terminated",
              {k: m.get(k) for k in ("v", "rc", "result", "wall_hit", "terminated")})
        check(m["remote_home"] == str(c.h) and m["remote_run_dir"] == str(run_dir) and m["remote_work"] == str(run_dir / "work")
              and m["remote_checkout"] == str(c.repo), "run pass: manifest remote paths",
              {k: m.get(k) for k in ("remote_home", "remote_run_dir", "remote_work", "remote_checkout")})
        check(m["real"] == {"run_dir": os.path.realpath(run_dir), "work": os.path.realpath(run_dir / "work"),
                            "checkout": os.path.realpath(c.repo), "home": os.path.realpath(c.h)}, "run pass: manifest real paths", m["real"])
        if m["changed"] != ["lib/new.test.js", "lib/price.js"]:
            print(f"NOTE lane run pass: changed came back as {m['changed']} (git status lists the tracked change first); the spec lists "
                  "it sorted, so the check compares sorted", flush=True)
        check(sorted(m["changed"]) == ["lib/new.test.js", "lib/price.js"], "run pass: changed is lib/new.test.js and lib/price.js", m["changed"])
        check(m["deleted"] == ["lib/token.js"], "run pass: deleted is lib/token.js", m["deleted"])
        check({"REVIEW.md", "spec.auto.json", "auto-state.json", "DIAGNOSIS.md"} <= set(m["run_files"])
              and "TASK.md" not in m["run_files"] and "spec.json" not in m["run_files"], "run pass: run_files", m["run_files"])
        check((c.art / "work" / "lib" / "price.js").read_text() == "FIXED\n" and (c.art / "run" / "REVIEW.md").is_file()
              and not (c.art / "run" / "TASK.md").exists() and not (c.art / "run" / "spec.json").exists(),
              "run pass: the files are in the artifacts")
        sp = s["spec"]
        check("remote" not in sp, "run pass: the spec the runner got has no remote", sorted(sp))
        check(sp.get("baseline_from") == os.path.realpath(c.repo), "run pass: baseline_from is the checkout", sp.get("baseline_from"))
        check(sp.get("task") == str(run_dir / "TASK.md"), "run pass: task is $HOME/run/n/TASK.md", sp.get("task"))
        check(sp.get("mode") == "auto" and sp.get("timeout") == 1800, "run pass: the rest of the spec is kept", sp)
        check(s["argv"] == ["--model", "m", "--cwd", str(run_dir / "work"), "--task-file", str(run_dir / "spec.json")],
              "run pass: the runner's argv", s["argv"])
        check(s["openai_key"] is False and s["lane_root"] is False, "run pass: no OPENAI_API_KEY or LANE_PROJECT_ROOT in the runner's env",
              (s["openai_key"], s["lane_root"]))
        check(s["outage"] == "1200", "run pass: MINI_OUTAGE_MAX_S is 1200", s["outage"])
        check(s["local"] == {"base_url": "http://127.0.0.1:9/v1", "api_key": "lane-relay"}, "run pass: local.json", s["local"])
        check(s["mini_bin"] == str(c.h / ".ollama-dispatch" / "mini-venv" / "bin" / "mini"), "run pass: MINI_BIN is the mini venv's", s["mini_bin"])
        for f in ("auto-state.json", "DIAGNOSIS.md"):
            check((run_dir / f).is_file(), f"run pass: {f} from the tools is in the run dir")
    check("RESULT PASS mode=auto fake" in c.out and "[mini-runner] fixing" in c.out, "run pass: the wrapper's stdout echoes the runner's output", c.out[-300:])

    c = lane_exec("sleepwall", "sleep", wall=2)
    check(not c.timed_out and c.man is not None, "run wall: manifest written", c.out[-300:])
    if c.man is not None:
        check(c.man["wall_hit"] is True and c.man["rc"] == 1 and c.man["result"] == "RESULT FAIL preempted",
              "run wall: wall_hit, rc 1, result RESULT FAIL preempted", {k: c.man.get(k) for k in ("wall_hit", "rc", "result", "terminated")})
    check(c.rc == 1, "run wall: exit 1", c.rc)

    lane_run_hold()

    c = lane_exec("sleepterm", "sleep", sig=signal.SIGTERM)
    check(not c.timed_out and c.man is not None, "run term: manifest written", c.out[-300:])
    if c.man is not None:
        check(c.man["terminated"] is True and c.man["wall_hit"] is False, "run term: terminated", {k: c.man.get(k) for k in ("terminated", "wall_hit", "rc")})
    check(c.rc == 1, "run term: exit 1", c.rc)

    c = lane_exec("hangterm", "hang", sig=signal.SIGTERM)
    check(not c.timed_out and c.man is not None, "run hang: manifest written", c.out[-300:])
    if c.man is not None:
        check(c.man["rc"] == -9 and c.man["terminated"] is True, "run hang: rc -9 and terminated", {k: c.man.get(k) for k in ("rc", "terminated", "wall_hit")})
    check(c.rc == 1, "run hang: exit 1", c.rc)

    c = lane_exec("partial", "partial")
    check(c.man is not None and c.man["result"] == "RESULT PASS partial", "run partial: result is the last line without a newline",
          c.man and c.man.get("result"))
    check(c.rc == 2, "run partial: exit 2", c.rc)

    c = lane_exec("silent", "silent")
    check(c.man is not None and c.man["result"] is None, "run silent: result is None", c.man and c.man.get("result"))
    check(c.rc == 2, "run silent: exit 2", c.rc)

    c = lane_exec("badname", "silent", name="../x")
    check(c.rc != 0 and not c.timed_out, "run --name=../x: a non-zero exit", c.rc)
    check(c.man is None and not (c.h / "run").exists(), "run --name=../x: nothing is run or written", (c.man, (c.h / "run").exists()))


def lane_run_hold():
    """The pause: the wall gives back the provider waits the run credited, up to --hold-allow."""
    hold = "[provider-hold] the call took 6s, 4s of it in the relay for the provider (a Darkbloom Sandbox hold); credited 4.0s"

    def man(c, label):
        check(not c.timed_out and c.man is not None, f"{label}: manifest written", c.out[-300:])
        return c.man or {}

    def span(m, lo, hi):
        return isinstance(m.get("elapsed_s"), int) and lo <= m["elapsed_s"] < hi

    c = lane_exec("holdnone", "sleep", wall=2, lines=[hold])
    m = man(c, "run hold, no --hold-allow")
    check(m.get("wall_hit") is True and m.get("credited_s") == 4.0 and m.get("hold_allow_s") == 0 and span(m, 2, 4),
          "run hold, no --hold-allow: the wall is 2 s as before (credited_s 4.0, hold_allow_s 0)",
          {k: m.get(k) for k in ("wall_hit", "credited_s", "hold_allow_s", "elapsed_s")})
    c = lane_exec("holdgiven", "sleep", wall=2, hold_allow=10, lines=[hold])
    m = man(c, "run hold given back")
    check(m.get("wall_hit") is True and m.get("credited_s") == 4.0 and m.get("hold_allow_s") == 10 and span(m, 6, 8),
          "run hold given back: the 2 s wall ends 4 s later", {k: m.get(k) for k in ("wall_hit", "credited_s", "hold_allow_s", "elapsed_s")})
    check(m.get("result") == "RESULT FAIL preempted" and c.rc == 1, "run hold given back: the run is stopped as at any wall", (m.get("result"), c.rc))
    check("wall 2s (+ up to 10s of provider waits)" in c.out and "(2s + 4s of provider waits, of 4s credited; at most 10s given back)" in c.out,
          "run hold given back: the start and stop lines say so", c.out[-500:])
    c = lane_exec("holdcap", "sleep", wall=2, hold_allow=1, lines=[hold])
    m = man(c, "run hold past the allowance")
    check(m.get("wall_hit") is True and m.get("credited_s") == 4.0 and m.get("hold_allow_s") == 1 and span(m, 3, 5),
          "run hold past the allowance: only 1 s is given back", {k: m.get(k) for k in ("wall_hit", "credited_s", "hold_allow_s", "elapsed_s")})
    check("(2s + 1s of provider waits, of 4s credited; at most 1s given back)" in c.out, "run hold past the allowance: the stop line", c.out[-500:])
    c = lane_exec("holdslack", "sleep", wall=2, hold_allow=600, slack=1, lines=["[provider-outage] the provider was down; credited 500s"])
    m = man(c, "run credit cap")
    cr = m.get("credited_s")
    check(m.get("wall_hit") is True and isinstance(cr, float) and 1.0 <= cr < 3.0 and span(m, 3, 6),
          "run credit cap: a credit past the run's own time + CREDIT_SLACK_S is cut to it", {k: m.get(k) for k in ("wall_hit", "credited_s", "elapsed_s")})
    noise = ["[mini-runner] the call; credited 5.0s", "[provider-hold] credited 5.0s", "[provider-hold] x; credited 5.0s and more",
             " [provider-hold] x; credited 5.0s", "[provider-other] x; credited 5.0s", "[provider-hold] x; credited -5s",
             "echo [provider-hold] x; credited 5.0s", "[provider-hold] x; credited 5.0s; credited"]
    c = lane_exec("holdnoise", "sleep", wall=2, hold_allow=600, lines=noise)
    m = man(c, "run credit lines that are not")
    check(m.get("wall_hit") is True and m.get("credited_s") == 0.0 and span(m, 2, 4), "run credit lines that are not: nothing is credited",
          {k: m.get(k) for k in ("wall_hit", "credited_s", "elapsed_s")})
    c = lane_exec("holdpass", "pass", hold_allow=600, lines=["[provider-hold] a; credited 1.5s", "[provider-outage] b; credited 1.0s\r"])
    m = man(c, "run hold, then a PASS")
    check(c.rc == 0 and m.get("wall_hit") is False and m.get("credited_s") == 2.5 and m.get("hold_allow_s") == 600,
          "run hold, then a PASS: the credits add up (a CRLF line too)", {k: m.get(k) for k in ("rc", "wall_hit", "credited_s", "hold_allow_s")})


def lane():
    with guard("lane collect"):
        lane_collect()
    with guard("lane walkup"):
        lane_walkup()
    with guard("lane cmd_run"):
        lane_run()


# ===================================================================================================================== round
FAKE_LIB = r'''"""Stand-in for cpu_job.py (the runner's CPU_JOB_LIB): emulates the dispatch-cpu-runner agent for one job, in this process."""
import hashlib, importlib.util, json, os, shlex, shutil, subprocess, sys, tarfile, time
from pathlib import Path

CFG = json.loads(os.environ["FAKE_JOB"])


def _real():
    m = sys.modules.get("cpu_job_real")
    if m is None:
        sp = importlib.util.spec_from_file_location("cpu_job_real", CFG["cpu_job"])
        m = importlib.util.module_from_spec(sp)
        sys.modules["cpu_job_real"] = m  # dataclasses look their module up
        sp.loader.exec_module(m)
    return m


def _git(*args, cwd):
    r = subprocess.run(["git", "-c", "safe.directory=*", *args], cwd=cwd, capture_output=True, text=True, timeout=300)
    if r.returncode:
        raise RuntimeError("git %s failed: %s" % (args[0], (r.stderr or r.stdout)[-400:]))


def submit_cpu_job(worktree_path, cmd, timeout_s=600, **kw):
    real = _real()
    tools = kw.get("tools") or {}
    here = Path(CFG["bin"])
    secret = False
    for name, data in tools.items():
        # the shipped sources carry the literal in their self-tests; only a tool that is not one of them counts
        src = here / name[4:] if name.startswith("bin/") and not name.startswith("bin/configs/") else None
        if src is not None and src.is_file() and src.read_bytes() == data:
            continue
        secret = secret or b"k-SECRET" in data
    rec = {k: v for k, v in kw.items() if k != "tools" and not callable(v)}
    rec.update(worktree_path=str(worktree_path), cmd=cmd, timeout_s=timeout_s, tools={k: len(v) for k, v in tools.items()},
               secret_in_tools=secret)
    Path(CFG["record"]).write_text(json.dumps(rec, indent=1, default=str))
    mode = CFG["mode"]
    if mode == "raise":
        raise RuntimeError("queue down")
    if mode == "cancel":
        while True:
            why = kw["cancel_if"]()
            if why:
                return real.CpuJobResult(exit_code=None, infra_error="cancelled: " + str(why))
            time.sleep(0.1)
    J, dl = Path(CFG["job"]), Path(CFG["dl"])
    dl.mkdir(parents=True)
    J.mkdir(parents=True)
    payload, patch, _kind = real.make_payload(worktree_path)
    (dl / "payload").write_bytes(payload)
    _git("clone", "-q", str(dl / "payload"), str(J / "checkout"), cwd=str(dl))
    if patch:
        (dl / "patch").write_bytes(patch)
        _git("apply", "--binary", "--whitespace=nowarn", str(dl / "patch"), cwd=str(J / "checkout"))
    (dl / "tools.tgz").write_bytes(real.make_tools_tgz(tools))
    with tarfile.open(dl / "tools.tgz") as tf:
        tf.extractall(J / "tools", filter="data")
    for d in ("home", "art", "shim"):
        (J / d).mkdir()
    shim = J / "shim" / "python3"
    shim.write_text('#!/bin/sh\nexec %s "$@"\n' % shlex.quote(sys.executable))
    shim.chmod(0o755)
    env = {k: v for k, v in os.environ.items() if k not in ("MINI_BIN", "DARKBLOOM_LOCAL_JSON", "CPU_JOB_LIB", "FAKE_JOB")}
    env.update(HOME=str(J / "home"), JOB_TOOLS=str(J / "tools"), CPU_RUNNER_ARTIFACTS=str(J / "art"),
               CPU_RUNNER_PROGRESS=str(J / "progress.txt"), PATH=str(J / "shim") + os.pathsep + env.get("PATH", ""),
               PYTHONDONTWRITEBYTECODE="1")
    syn = subprocess.run(["bash", "-n", "-c", kw["setup"]["cmd"]], capture_output=True, text=True)  # the setup is only syntax-checked
    os.symlink(J / "tools" / "bin", J / "home" / "bin")
    mini = J / "home" / ".ollama-dispatch" / "mini-venv" / "bin" / "mini"
    mini.parent.mkdir(parents=True)
    shutil.copyfile(CFG["stub"], mini)
    mini.chmod(0o755)
    p = subprocess.Popen(["bash", "-c", cmd], cwd=str(J / "checkout"), env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                         stderr=subprocess.STDOUT, start_new_session=True)
    on_log = kw.get("on_log")
    while True:
        chunk = os.read(p.stdout.fileno(), 65536)
        if not chunk:
            break
        if on_log is not None:
            on_log(chunk)
    rc = p.wait()
    dest = kw["artifacts_dest"]
    with tarfile.open(dest, "w:gz") as tf:
        tf.add(str(J / "art"), arcname=".")
    data = Path(dest).read_bytes()
    rec.update(exit_code=rc, setup_syntax_rc=syn.returncode, setup_syntax_err=syn.stderr[-300:])
    Path(CFG["record"]).write_text(json.dumps(rec, indent=1, default=str))
    return real.CpuJobResult(exit_code=rc, timed_out=False, ran_on="runner", job_id="fake0123456789abcdef", infra_error=None,
                             artifacts={"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "http": 200},
                             artifacts_path=dest, llm={"calls": 3, "errors": 0, "held": 0, "hold_wait_s": 0})
'''


def _files_under(base):
    """Every regular, non-symlink file under base, skipping .git directories."""
    for dp, dns, fns in os.walk(base):
        dns[:] = [d for d in dns if d != ".git"]
        for fn in fns:
            p = Path(dp) / fn
            if p.is_file() and not p.is_symlink():
                yield p


def round_():
    root = new_tmp("lane-round-")
    lib = root / "fake_cpu_job.py"
    lib.write_text(FAKE_LIB)
    stub = root / "stub-mini"
    stub.write_text(T.STUB)
    stub.chmod(0o755)
    mac_home = root / "home"
    mac_home.mkdir()
    os.symlink(HERE, mac_home / "bin")  # the runner's overlay configs are $HOME/bin/configs/*.yaml
    (root / "jobs").mkdir()
    repo = T.mkrepo(root, "shop")
    plan = {"D": {"DIAGNOSIS.md": T.GOOD_DIAG}, "A": {"@test": T.NEW_TEST}, "B": {"lib/price.js": T.FIXED}}
    count = [0]

    def launch(name, mode="run", extra=None, reuse=False):
        run = root / name
        if not reuse:
            (run / "work").mkdir(parents=True)
            (run / "TASK.md").write_text(T.DESC)
        count[0] += 1
        n = count[0]
        spec = {"mode": "auto", "baseline_from": str(repo), "task": str(run / "TASK.md"), "timeout": 1800, "diag_attempts": 1,
                "test_attempts": 1, "fix_attempts": 1, "files": ["price.js"], "remote": {"python": "3.12"}, **(extra or {})}
        (run / "spec.json").write_text(json.dumps(spec))
        (run / "plan.json").write_text(json.dumps({**plan, "log": str(run / "stub.log")}))
        J, rec_path = root / "jobs" / f"j{n}", root / "jobs" / f"record{n}.json"
        job = {"mode": mode, "cpu_job": str(HERE / "cpu_job.py"), "bin": str(HERE), "stub": str(stub), "job": str(J),
               "dl": str(root / "jobs" / f"dl{n}"), "record": str(rec_path)}
        env = {k: v for k, v in os.environ.items() if k not in ("MINI_SPEC_LIB", "CPU_RUNNER_API", "CPU_RUNNER_ARTIFACTS",
                                                                "CPU_RUNNER_PROGRESS", "JOB_TOOLS", "LANE_PROJECT_ROOT")}
        env.update(HOME=str(mac_home), STUB_PLAN=str(run / "plan.json"), MINI_BIN=FAKE_MINI, DARKBLOOM_LOCAL_JSON=str(FAKE_LOCAL),
                   MINI_GRAPH_LOOKUP="off", CPU_JOB_LIB=str(lib), FAKE_JOB=json.dumps(job), PYTHONDONTWRITEBYTECODE="1")
        p = spawn([sys.executable, str(T.RUNNER), "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")], env=env,
                  stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if mode == "cancel":
            t_end = time.time() + 120
            while time.time() < t_end and not rec_path.exists() and p.poll() is None:
                time.sleep(0.05)
            time.sleep(0.2)
            if p.poll() is None:
                os.kill(p.pid, signal.SIGTERM)
        timed_out = False
        try:
            out, err = p.communicate(timeout=600)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(p.pid, signal.SIGKILL)
            out, err = p.communicate()
        log = (run / "stub.log").read_text() if (run / "stub.log").exists() else ""
        return SimpleNamespace(rc=p.returncode, out=out, err=err, timed_out=timed_out, run=run, J=J,
                               res=[ln for ln in out.splitlines() if ln.startswith("RESULT ")],
                               stages=[ln[4:] for ln in log.splitlines() if ln.startswith("=== ")],
                               rec=json.loads(rec_path.read_text()) if rec_path.is_file() else None)

    c1 = None
    with guard("round pass"):
        c = c1 = launch("pass")
        tail = (c.rc, c.out[-700:], c.err[-400:])
        check(not c.timed_out and c.rc == 0, "round pass: exit 0", tail)
        check(len(c.res) == 1, "round pass: exactly one RESULT line", c.res)
        r0 = c.res[0] if c.res else ""
        check(r0.startswith("RESULT PASS mode=auto") and "target=lib/price.js:bulkTotal" in r0 and r0.endswith("lane=remote:fake01234567"),
              "round pass: the RESULT line is a PASS with the target and the lane tag", r0)
        check("[remote] RESULT PASS" in c.out, "round pass: the remote run's RESULT is logged with the [remote] prefix", c.out[-500:])
        check(c.stages == ["D", "A", "B"], "round pass: the stub ran the stages D, A, B", c.stages)
        run = c.run
        check((run / "work" / "lib" / "price.js").read_text() == T.FIXED if (run / "work" / "lib" / "price.js").is_file() else False,
              "round pass: the local work has the fix")
        sa = run / "spec.auto.json"
        fs = json.loads(sa.read_text()) if sa.is_file() else {}
        tfile = fs.get("test_file")
        check(bool(tfile) and (run / "work" / tfile).is_file() and (run / "work" / tfile).read_text() == T.NEW_TEST,
              "round pass: the local work has the new test", tfile)
        check(fs.get("baseline_from") == str(repo) and fs.get("remote") == {"python": "3.12"},
              "round pass: spec.auto.json has baseline_from = the repo and keeps remote", {k: fs.get(k) for k in ("baseline_from", "remote")})
        jtxt = str(c.J).encode()
        leaks = []
        for p in _files_under(run):
            if p.parent == run and p.name in ("stub.log", "remote-artifacts.tgz"):
                continue
            if jtxt in p.read_bytes():
                leaks.append(str(p.relative_to(run)))
        check(not leaks, "round pass: no path of the remote job (J) in the files that came back", leaks)
        ra = (run / "REVIEW-AUTO.md").read_text() if (run / "REVIEW-AUTO.md").is_file() else ""
        check("+  if (qty >= 10) {" in ra, "round pass: REVIEW-AUTO.md has the fix diff line", ra[:300])
        check((repo / "lib" / "price.js").read_text() == T.PRICE, "round pass: the baseline repo is untouched")
        st = subprocess.run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
        check(st.returncode == 0 and st.stdout == "", "round pass: the baseline repo is clean", st.stdout)
        sec = []
        for p in _files_under(c.J):
            data = p.read_bytes()
            if b"k-SECRET" not in data:
                continue
            rel = p.relative_to(c.J)
            twin = HERE.joinpath(*rel.parts[2:]) if rel.parts[:2] == ("tools", "bin") else None
            if twin is not None and twin.is_file() and twin.read_bytes() == data:
                continue  # a shipped source, which holds the literal in its self-tests
            sec.append(str(rel))
        check(not sec, "round pass: k-SECRET is nowhere under J (the shipped sources apart)", sec)
        rec = c.rec or {}
        check(rec.get("secret_in_tools") is False, "round pass: k-SECRET is in none of the tools (the shipped sources apart)", rec.get("secret_in_tools"))
        check("k-SECRET" not in c.out + c.err + ra, "round pass: the key is never echoed")
        tn = set(rec.get("tools") or [])
        check({"lane/TASK.md", "lane/spec.json", "lane/mini.lock", "bin/lane_remote.py", "bin/mini-swe-runner.py"} <= tn
              and "lane/auto-state.json" not in tn and "lane/DIAGNOSIS.md" not in tn,
              "round pass: the first launch ships TASK.md, spec.json, mini.lock and the tools, but no auto-state or DIAGNOSIS", sorted(tn))
        check(rec.get("setup_syntax_rc") == 0, "round pass: the setup command is valid bash (bash -n)", rec.get("setup_syntax_err"))

    with guard("round relaunch"):
        c = launch("pass", reuse=True)
        tn = set((c.rec or {}).get("tools") or [])
        check("lane/auto-state.json" in tn and "lane/DIAGNOSIS.md" in tn, "round relaunch: the tools now include auto-state.json and DIAGNOSIS.md", sorted(tn))
        check(c.stages == ["D", "A", "B", "A", "B"], "round relaunch: the stages are D, A, B, A, B", c.stages)
        check("reusing the diagnosis" in c.out, "round relaunch: the output says it reuses the diagnosis", c.out[-500:])
        check(bool(c.res) and c.res[0].startswith("RESULT PASS mode=auto") and c.rc == 0, "round relaunch: PASS, exit 0", (c.rc, c.res, c.err[-300:]))

    with guard("round two-phase"):
        c = launch("twophase", extra={"mode": "two-phase", "allowed": ["lib/price.js"]})
        r0 = c.res[0] if c.res else ""
        check(len(c.res) == 1 and "FAIL bad-spec remote needs mode auto" in r0 and r0.endswith(" class=SPEC") and c.rc == 1,
              "round two-phase: FAIL bad-spec remote needs mode auto, class SPEC, exit 1", (c.rc, c.res, c.err[-300:]))
        check(c.rec is None, "round two-phase: nothing was submitted", c.rec)

    with guard("round raise"):
        c = launch("raise", mode="raise")
        r0 = c.res[0] if c.res else ""
        check(len(c.res) == 1 and "FAIL" in r0 and "remote-infra RuntimeError: queue down" in r0 and r0.endswith(" class=INFRA")
              and "lane=remote:" not in r0 and c.rc == 1, "round raise: remote-infra RuntimeError: queue down, class INFRA, no lane tag, exit 1",
              (c.rc, c.res, c.err[-300:]))

    with guard("round cancel"):
        c = launch("cancel", mode="cancel")
        r0 = c.res[0] if c.res else ""
        check(not c.timed_out and len(c.res) == 1 and r0.startswith("RESULT FAIL preempted") and r0.endswith(" class=INFRA") and c.rc == 1,
              "round cancel: SIGTERM -> RESULT FAIL preempted, class INFRA, exit 1", (c.timed_out, c.rc, c.res, c.err[-300:]))


# ===================================================================================================================== main
MODES = {"units": units, "host": host, "lane": lane, "round": round_}


def main(argv=None):
    argv = sys.argv[1:] if argv is None else list(argv)
    if len(argv) > 1 or (argv and argv[0] not in MODES):
        print("usage: test_lane_remote.py [units|host|lane|round]", file=sys.stderr)
        return 2
    for name in argv or list(MODES):
        MODE[0] = name
        c0, f0 = COUNT
        try:
            MODES[name]()
        except KeyboardInterrupt:
            raise
        except BaseException as e:  # noqa: BLE001 -- a crash (Aborted included) is one failed check, not the end of the run
            tb = traceback.format_exc().strip().splitlines()
            check(False, f"{name}: crashed ({type(e).__name__})", " | ".join(x.strip() for x in tb[-5:]))
        print(f"{name}: {COUNT[0] - c0} checks, {COUNT[1] - f0} failed", flush=True)
    print(f"{COUNT[0]} checks, {COUNT[1]} failed", flush=True)
    return 1 if COUNT[1] else 0


if __name__ == "__main__":
    sys.exit(main())
