#!/usr/bin/env python3
"""mini_mutate: line-scoped mutation score for Python. Used by mini-testdraft check (lever 1: does a drafted test pin the ref fix?).

Only lines the reference fix changed are mutated (difflib base -> ref, ref side). Each mutant is run with the spec's own test_cmd, the same
command V3/V4 ran, in a throwaway clone. A mutant is killed when the command fails or times out. There is no mutmut or pytest dependency,
so a test in tests/ against src/ works, and so does a plain `python3 test_x.py` script.

Operators, applied only to nodes on changed lines:
- compare flip and boundary
- and/or swap
- drop a `not`
- arithmetic swap
- constant change (n+1, True/False, '' / 'mut')
- `return X` -> `return None`
- negate an if/while/assert test
- a single-line statement -> `pass`
- breadth: a string match widened or narrowed (`x.startswith(S)` / `x.endswith(S)` / `S in x` -> `x == S`, and `x == S` ->
  `str(x).startswith(S)`): a survivor means the test does not decide between an exact and a prefix/substring match

Docstrings, f-string parts, annotations and match patterns are skipped. A mutant that does not compile is dropped, not counted as killed.

Sanity before scoring (either failure -> unscored, never a REJECT of the test):
- the unmutated ref must pass test_cmd;
- a canary (the whole file replaced by a raise) must fail it, which proves the test loads this file from the clone.

Containment (every run: ref, canary, mutants). A mutant can break the test's own isolation line, so runs get a throwaway HOME and
DISPATCH_VERIFY_SANDBOX=1, and on macOS a sandbox-exec profile. The profile blocks writes outside the temp dirs, connects to ports that
were already listening (live services) or to web ports, unix sockets outside the temp dirs, and signals to outside processes.
A test that only passes with the real HOME or a live service is unscored, never run uncontained. See _contain().

  python3 mini_mutate.py --work DIR --pair REL=BASEFILE [--pair ...] --test-cmd CMD [--max 60] [--jobs 4] [--json]
    DIR = a tree with the REF version of each REL already in place, plus the test to score (not written: clones are used).
Prints `MUTATION killed=K survived=S timeout=T total=N` and, for each survivor, `survived <file>:<line> <op>: <before> -> <after>`.
"""
import argparse
import ast
import difflib
import hashlib
import json
import os
import queue
import re
import shutil
import signal
import site
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

CMP_FLIP = {ast.Eq: ast.NotEq, ast.NotEq: ast.Eq, ast.Lt: ast.GtE, ast.GtE: ast.Lt, ast.Gt: ast.LtE, ast.LtE: ast.Gt,
            ast.In: ast.NotIn, ast.NotIn: ast.In, ast.Is: ast.IsNot, ast.IsNot: ast.Is}
CMP_EDGE = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE, ast.GtE: ast.Gt}
CMP_STR = {ast.Eq: "==", ast.NotEq: "!=", ast.Lt: "<", ast.LtE: "<=", ast.Gt: ">", ast.GtE: ">=", ast.In: "in", ast.NotIn: "not in",
           ast.Is: "is", ast.IsNot: "is not"}
BIN_SWAP = {ast.Add: "-", ast.Sub: "+", ast.Mult: "/", ast.Div: "*", ast.FloorDiv: "*", ast.Mod: "//"}
MAX_SPAN = 4  # an expression spanning more lines than this is not mutated as a whole (its changed part is mutated inside it)


def changed_lines(base_text, ref_text):
    """1-based ref-side line numbers the fix inserted or replaced. Pure deletions add none (V5 covers those)."""
    a, b = base_text.splitlines(), ref_text.splitlines()
    out = set()
    for tag, _i1, _i2, j1, j2 in difflib.SequenceMatcher(None, a, b, autojunk=False).get_opcodes():
        if tag in ("replace", "insert"):
            out.update(range(j1 + 1, j2 + 1))
    return out


def _seg(bl, n):
    l1, c1, l2, c2 = n.lineno - 1, n.col_offset, n.end_lineno - 1, n.end_col_offset
    if l1 == l2:
        return bl[l1][c1:c2].decode("utf-8", "replace")
    return (bl[l1][c1:] + b"".join(bl[l1 + 1:l2]) + bl[l2][:c2]).decode("utf-8", "replace")


def _splice(bl, n, repl):
    l1, c1, l2, c2 = n.lineno - 1, n.col_offset, n.end_lineno - 1, n.end_col_offset
    return b"".join(bl[:l1] + [bl[l1][:c1] + repl.encode() + bl[l2][c2:]] + bl[l2 + 1:]).decode("utf-8", "replace")


def _skip_ids(tree):
    """ids of nodes never mutated: docstrings, anything inside an f-string, an annotation or a match pattern."""
    skip = set()

    def mark(n):
        for x in ast.walk(n):
            skip.add(id(x))
    for n in ast.walk(tree):
        body = getattr(n, "body", None)
        if isinstance(n, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and body and isinstance(body[0], ast.Expr) \
                and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
            mark(body[0])
        if isinstance(n, ast.JoinedStr):
            mark(n)
        for fld in ("annotation", "returns"):
            if getattr(n, fld, None) is not None:
                mark(getattr(n, fld))
        if isinstance(n, ast.match_case):
            mark(n.pattern)
    return skip


def mutants(rel, ref_text, lines):
    """[(line, op, before, after, new_text)] for one file. Deterministic order; compile-checked; deduped."""
    try:
        tree = ast.parse(ref_text)
    except SyntaxError:
        return []
    bl = ref_text.encode().splitlines(keepends=True)
    skip = _skip_ids(tree)
    out, seen = [], {hashlib.sha1(ref_text.encode()).hexdigest()}

    def on(n, single=False):
        if not hasattr(n, "lineno") or n.end_lineno is None or id(n) in skip:
            return False
        if single:
            return n.lineno == n.end_lineno and n.lineno in lines
        return n.end_lineno - n.lineno < MAX_SPAN and any(n.lineno <= x <= n.end_lineno for x in lines)

    def add(n, op, repl):
        try:
            new = _splice(bl, n, repl)
        except (IndexError, UnicodeError):
            return
        h = hashlib.sha1(new.encode()).hexdigest()
        if h in seen:
            return
        try:
            compile(new, rel, "exec")
        except (SyntaxError, ValueError):
            return
        seen.add(h)
        out.append((n.lineno, op, _seg(bl, n)[:80], repl[:80], new))

    for n in ast.walk(tree):
        if isinstance(n, ast.Compare) and on(n):
            parts = [_seg(bl, n.left)] + [_seg(bl, c) for c in n.comparators]
            for i, o in enumerate(n.ops):
                for table, name in ((CMP_FLIP, "cmp-flip"), (CMP_EDGE, "cmp-edge")):
                    new_op = table.get(type(o))
                    if new_op:
                        ops = [CMP_STR[type(x)] for x in n.ops]
                        ops[i] = CMP_STR[new_op]
                        expr = "(" + parts[0] + "".join(f" {ops[k]} ({parts[k + 1]})" for k in range(len(ops))) + ")"
                        add(n, name, expr)
        elif isinstance(n, ast.BoolOp) and on(n):
            j = " or " if isinstance(n.op, ast.And) else " and "
            add(n, "bool-swap", "(" + j.join("(" + _seg(bl, v) + ")" for v in n.values) + ")")
        elif isinstance(n, ast.UnaryOp) and isinstance(n.op, ast.Not) and on(n):
            add(n, "drop-not", "(" + _seg(bl, n.operand) + ")")
        elif isinstance(n, ast.BinOp) and type(n.op) in BIN_SWAP and on(n):
            add(n, "arith", "((" + _seg(bl, n.left) + ") " + BIN_SWAP[type(n.op)] + " (" + _seg(bl, n.right) + "))")
        elif isinstance(n, ast.Constant) and on(n):
            v = n.value
            if isinstance(v, bool):
                add(n, "const", repr(not v))
            elif isinstance(v, (int, float)) and not isinstance(v, bool):
                add(n, "const", repr(v + 1))
            elif isinstance(v, str):
                add(n, "const", repr("" if v else "mut"))
            elif isinstance(v, bytes):
                add(n, "const", repr(b"" if v else b"mut"))
        if isinstance(n, ast.Call) and on(n) and isinstance(n.func, ast.Attribute) and n.func.attr in ("startswith", "endswith") \
                and len(n.args) == 1 and not n.keywords and isinstance(n.args[0], ast.Constant) and isinstance(n.args[0].value, str):
            add(n, "breadth", "((" + _seg(bl, n.func.value) + ") == " + _seg(bl, n.args[0]) + ")")
        if isinstance(n, ast.Compare) and on(n) and len(n.ops) == 1:
            lft, rgt, o = n.left, n.comparators[0], n.ops[0]
            is_s = lambda x: isinstance(x, ast.Constant) and isinstance(x.value, str)
            if isinstance(o, (ast.In, ast.NotIn)) and is_s(lft) and lft.value and not isinstance(rgt, (
                    ast.List, ast.Tuple, ast.Set, ast.Dict, ast.ListComp, ast.SetComp, ast.DictComp, ast.Constant)):
                add(n, "breadth", "((" + _seg(bl, rgt) + ") " + ("==" if isinstance(o, ast.In) else "!=") + " " + _seg(bl, lft) + ")")
            elif isinstance(o, (ast.Eq, ast.NotEq)) and (is_s(lft) != is_s(rgt)):
                sx, xx = (rgt, lft) if is_s(rgt) else (lft, rgt)
                if len(sx.value) >= 2:
                    add(n, "breadth", "(" + ("not " if isinstance(o, ast.NotEq) else "") + "str(" + _seg(bl, xx) + ").startswith("
                        + _seg(bl, sx) + "))")
        if isinstance(n, ast.Return) and n.value is not None and on(n) and not (isinstance(n.value, ast.Constant) and n.value.value is None):
            add(n.value, "return-none", "None")
        if isinstance(n, (ast.If, ast.While, ast.Assert, ast.IfExp)) and on(n.test) and not isinstance(n.test, (ast.Compare, ast.UnaryOp)):
            add(n.test, "negate", "(not (" + _seg(bl, n.test) + "))")
        if isinstance(n, (ast.Assign, ast.AugAssign, ast.AnnAssign, ast.Expr, ast.Return, ast.Raise, ast.Delete)) and on(n, single=True):
            if not (isinstance(n, ast.Expr) and isinstance(n.value, ast.Constant)):
                add(n, "del-stmt", "pass")
    out.sort(key=lambda m: (m[0], m[1], m[2], m[3]))
    return out


def _clone(src, dst):
    if sys.platform == "darwin" and subprocess.run(["cp", "-cR", str(src), str(dst)], capture_output=True).returncode == 0:
        pass
    else:
        shutil.rmtree(dst, ignore_errors=True)
        shutil.copytree(src, dst, symlinks=True, ignore_dangling_symlinks=True)
    for root, dirs, _files in os.walk(dst):  # no stale bytecode: a same-size mutant could otherwise load the ref's .pyc
        for d in list(dirs):
            if d == "__pycache__":
                shutil.rmtree(Path(root, d), ignore_errors=True)
                dirs.remove(d)


# Containment. A mutant runs deliberately broken code, and the line it breaks can be the test's own isolation (a temp path, an
# env override, an injected fake client). On 2026-11-02 an uncontained calibration run of queue-events-emit wrote 16 fixture
# events into the live ~/.ollama-dispatch/queue-events.jsonl that way. Every run here (ref, canary, mutants) therefore gets:
# - HOME = a throwaway dir, so state paths derived from HOME land there. PYTHONUSERBASE and GIT_CONFIG_GLOBAL keep the real
#   user site-packages and git identity, and ~ / $HOME in test_cmd are expanded to the real home first.
# - DISPATCH_VERIFY_SANDBOX=1: ollama-queue.py refuses every mutating subcommand under it.
# - On macOS, a sandbox-exec profile:
#   - no file writes outside the temp dirs;
#   - no connects to a TCP port that was already listening when scoring started (queue API, BloomGauge, ollama ...), or to
#     web/ssh ports anywhere. A server the test starts itself stays reachable.
#   - no unix-socket connects outside the per-user temp dir and /var/run;
#   - no signals to processes outside the sandbox.
# With no sandbox-exec the result is unscored. MINI_MUTATE_SANDBOX=off keeps only the HOME/env part, for other platforms.
REMOTE_PORTS = (22, 80, 443, 8080, 8443)
LIVE_PORTS = (7684, 7690, 8765, 8766, 11434)  # queue API, BloomGauge, ollama: denied even when discovery misses them


def _listening_ports():
    """-> set of TCP ports with a listener right now, or None when neither probe works. lsof sees this user's processes;
    netstat sees every owner but returns nothing under some parents (seen from a sandboxed Python), so both are tried."""
    ports, ok = set(), False
    for argv, rx in ((["lsof", "-nP", "-w", "-iTCP", "-sTCP:LISTEN", "-Fn"], r"^n.*:(\d+)$"),
                     (["netstat", "-an", "-p", "tcp"], r"^tcp\S*\s+\d+\s+\d+\s+\S*\.(\d+)\s+\S+\s+LISTEN")):
        try:
            out = subprocess.run(argv, capture_output=True, text=True, timeout=30).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        found = {int(m.group(1)) for m in re.finditer(rx, out, re.M)}
        ok = ok or bool(found)
        ports |= found
    return ports if ok else None


def _q(s):
    return '"' + str(s).replace("\\", "\\\\").replace('"', '\\"') + '"'


def _profile(user_tmp, ports, writable=()):
    """SBPL text; writable = extra dirs the run may write (a throwaway work dir). These rule forms were checked on macOS 27.0.1:
    - for file and signal rules, a later allow overrides an earlier deny;
    - for network, only per-port `*:N` denies are used. Mixing `*:*` (or require-not) with allows makes the per-port denies go
      missing, so `*:*` is used alone, as the strict fallback when the listening ports are unknown;
    - a bare `(remote unix-socket)` deny also denies TCP, so the unix-socket deny carries a path-regex."""
    rules = ["(version 1)", "(allow default)",
             '(deny file-write* (subpath "/"))',
             f'(allow file-write* (subpath {_q(user_tmp)}) (subpath "/private/tmp") (subpath "/dev")'
             + "".join(f" (subpath {_q(os.path.realpath(w))})" for w in writable) + ")",
             '(deny network-outbound (remote unix-socket (path-regex #"^/")))',  # a bare (remote unix-socket) denies TCP too
             f'(allow network-outbound (remote unix-socket (subpath {_q(user_tmp)})) (remote unix-socket (subpath "/private/var/run")))',
             "(deny signal)", "(allow signal (target same-sandbox))"]
    if ports is None:
        rules.append('(deny network-outbound (remote ip "*:*"))')
    else:
        rules += [f'(deny network-outbound (remote ip "*:{p}"))' for p in sorted(set(ports) | set(REMOTE_PORTS) | set(LIVE_PORTS))]
    return "\n".join(rules) + "\n"


def _contain(tmp, env, test_cmd, writable=(), strict=True):
    """-> (env, argv prefix, test_cmd) for a contained run under tmp, or a str reason when it cannot be contained.
    writable: extra dirs the run may write (the throwaway work dir of a drafted test). strict=False: with no sandbox-exec, return the
    HOME/env part alone instead of a reason (the test-draft gates: mini-swe-runner run_cmd, mini-validate run)."""
    real_home = str(Path.home())
    home = Path(tmp) / "home"
    home.mkdir(exist_ok=True)
    env = {**env, "HOME": str(home), "DISPATCH_VERIFY_SANDBOX": "1"}
    env.setdefault("PYTHONUSERBASE", site.getuserbase())
    if Path(real_home, ".gitconfig").is_file():
        env.setdefault("GIT_CONFIG_GLOBAL", str(Path(real_home, ".gitconfig")))
    cmd = re.sub(r"(?<![^\s=:;&|(])~(?=/|\s|$)", lambda _m: real_home, test_cmd)
    cmd = re.sub(r"\$\{HOME\}|\$HOME\b", lambda _m: real_home, cmd)
    if os.environ.get("MINI_MUTATE_SANDBOX", "").lower() == "off":
        return env, [], cmd
    sbx = shutil.which("sandbox-exec") if sys.platform == "darwin" else None
    if not sbx:
        if not strict:
            return env, [], cmd
        return "no sandbox-exec here; mutants run broken code and only run sandboxed (MINI_MUTATE_SANDBOX=off overrides)"
    user_tmp = str(Path(os.path.realpath(tempfile.gettempdir())).parent)  # /private/var/folders/xx/yyyy: its T/ and C/
    prof = Path(tmp) / "sandbox.sb"
    prof.write_text(_profile(user_tmp, _listening_ports(), writable))
    return env, [sbx, "-f", str(prof)], cmd


def _run(cmd, cwd, env, timeout, wrap=()):
    """-> (rc, seconds, tail). rc 124 on timeout; the whole process group is killed. wrap = argv prefix (the sandbox)."""
    t0 = time.time()
    p = subprocess.Popen([*wrap, "bash", "-c", cmd], cwd=cwd, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
                         errors="replace", start_new_session=True)
    try:
        out, _ = p.communicate(timeout=timeout)
        return p.returncode, time.time() - t0, out[-400:]
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            pass
        p.communicate()
        return 124, time.time() - t0, "TIMEOUT"


def score(work, pairs, test_cmd, env=None, max_mutants=60, jobs=4, min_timeout=15.0):
    """pairs = {rel: base_text}; work holds the ref version of each rel. -> dict with killed/survived/timeout/total/score/survivors,
    or {'unscored': reason}. work is never written."""
    work = Path(work)
    env = {**os.environ, **(env or {}), "PYTHONDONTWRITEBYTECODE": "1"}
    env.pop("OPENAI_API_KEY", None)
    ms = []
    refs = {}
    for rel, base_text in pairs.items():
        p = work / rel
        if not p.is_file() or not rel.endswith(".py"):
            continue
        refs[rel] = p.read_text(errors="replace")
        ms += [(rel, *m) for m in mutants(rel, refs[rel], changed_lines(base_text or "", refs[rel]))]
    if not refs:
        return {"unscored": "no changed .py file to mutate"}
    total_found = len(ms)
    if len(ms) > max_mutants:  # evenly spread, deterministic
        step = len(ms) / max_mutants
        ms = [ms[int(i * step)] for i in range(max_mutants)]
    tmp = Path(tempfile.mkdtemp(prefix="mini-mutate-"))
    try:
        got = _contain(tmp, env, test_cmd)
        if isinstance(got, str):
            return {"unscored": got}
        env, wrap, test_cmd = got
        c0 = tmp / "c0"
        _clone(work, c0)
        rc, dur, tail = _run(test_cmd, c0, env, max(120.0, min_timeout * 8), wrap)
        if rc != 0:
            return {"unscored": f"the test fails on the unmutated ref copy (rc={rc}; contained: throwaway HOME"
                                f"{', sandbox' if wrap else ''}): {tail.strip()[-200:]}"}
        for rel in refs:
            (c0 / rel).write_text('raise RuntimeError("mini-mutate canary")\n')
            crc, _d, _t = _run(test_cmd, c0, env, max(60.0, dur * 6), wrap)
            (c0 / rel).write_text(refs[rel])
            if crc == 0:
                return {"unscored": f"canary survived: the test does not load {rel} from the work tree (cwd/PYTHONPATH?)"}
        if not ms:
            return {"killed": 0, "survived": 0, "timeout": 0, "total": 0, "score": None, "survivors": [], "found": 0}
        tmo = max(min_timeout, dur * 6)
        clones = [c0] + [tmp / f"c{i}" for i in range(1, max(1, min(jobs, len(ms))))]
        for c in clones[1:]:
            _clone(work, c)
        q, res, lock = queue.Queue(), [], threading.Lock()
        for m in ms:
            q.put(m)

        def worker(c):
            while True:
                try:
                    rel, line, op, before, after, new = q.get_nowait()
                except queue.Empty:
                    return
                (c / rel).write_text(new)
                mrc, _d, _t = _run(test_cmd, c, env, tmo, wrap)
                (c / rel).write_text(refs[rel])
                st = "timeout" if mrc == 124 else ("killed" if mrc != 0 else "survived")
                with lock:
                    res.append((st, rel, line, op, before, after))
        ths = [threading.Thread(target=worker, args=(c,)) for c in clones]
        for t in ths:
            t.start()
        for t in ths:
            t.join()
        k = sum(1 for r in res if r[0] == "killed")
        t_ = sum(1 for r in res if r[0] == "timeout")
        s = sorted((r for r in res if r[0] == "survived"), key=lambda r: (r[1], r[2], r[3]))
        n = len(res)
        return {"killed": k, "survived": len(s), "timeout": t_, "total": n, "found": total_found, "score": round((k + t_) / n, 3) if n else None,
                "survivors": [{"file": r[1], "line": r[2], "op": r[3], "before": r[4], "after": r[5]} for r in s]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


CONTAIN_PROBE = r'''
import os, signal, socket, sys, threading, http.server
real, port, pid, usock = sys.argv[1], int(sys.argv[2]), int(sys.argv[3]), sys.argv[4]
work = sys.argv[5] if len(sys.argv) > 5 else None
r = {}
def t(k, f):
    try:
        f()
        r[k] = "ok"
    except Exception:
        r[k] = "denied"
t("home_write", lambda: open(real, "w").write("x"))
t("tmp_write", lambda: open(os.path.join(os.environ["HOME"], "f"), "w").write("x"))
if work:
    t("work_write", lambda: open(os.path.join(work, "f"), "w").write("x"))
t("live_port", lambda: socket.create_connection(("127.0.0.1", port), timeout=3).close())
srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), http.server.BaseHTTPRequestHandler)
threading.Thread(target=srv.serve_forever, daemon=True).start()
t("own_port", lambda: socket.create_connection(("127.0.0.1", srv.server_address[1]), timeout=3).close())
t("unix_outside", lambda: socket.socket(socket.AF_UNIX).connect(usock))
def own_unix():
    p = os.path.join(os.environ["HOME"], "s.sock")
    a = socket.socket(socket.AF_UNIX); a.bind(p); a.listen(1)
    socket.socket(socket.AF_UNIX).connect(p)
t("own_unix", own_unix)
t("signal_outside", lambda: os.kill(pid, signal.SIGCONT))
print(" ".join(f"{k}={v}" for k, v in sorted(r.items())))
'''


def _self_test_containment():
    """A contained run cannot write under the real home, reach a port that was listening before it started, connect to a unix
    socket outside the temp dirs, or signal an outside process. It can use its own HOME, its own server and its own socket."""
    import shlex
    import socket
    real = Path.home() / f".mini-mutate-selftest-{os.getpid()}"
    work = Path.home() / f".mini-mutate-selftest-work-{os.getpid()}"  # stands in for a mini-work dir under the real home
    work.mkdir()
    live = socket.socket()
    live.bind(("127.0.0.1", 0))
    live.listen(1)  # stands in for a live service: it is listening before the profile is built
    udir = Path(tempfile.mkdtemp(prefix="mm-ct-", dir="/private/tmp"))
    usrv = socket.socket(socket.AF_UNIX)
    usrv.bind(str(udir / "s.sock"))
    usrv.listen(1)  # stands in for a session socket in /tmp/cc-socks
    outside = subprocess.Popen(["sleep", "60"])
    tmp = Path(tempfile.mkdtemp(prefix="mini-mutate-ct-"))
    try:
        got = _contain(tmp, dict(os.environ), "echo ~/a $HOME/b ${HOME}/c x~y", writable=[work])
        assert not isinstance(got, str), got
        env, wrap, cmd = got
        h = str(Path.home())
        assert cmd == f"echo {h}/a {h}/b {h}/c x~y", cmd
        assert env["HOME"] != h and env["DISPATCH_VERIFY_SANDBOX"] == "1", env["HOME"]
        if not wrap:
            return "sandbox off"
        (tmp / "probe.py").write_text(CONTAIN_PROBE)
        args = " ".join(shlex.quote(str(x)) for x in (real, live.getsockname()[1], outside.pid, udir / "s.sock", work))
        rc, _d, out = _run(f"python3 probe.py {args}", tmp, env, 60, wrap)
        want = ("home_write=denied live_port=denied own_port=ok own_unix=ok signal_outside=denied tmp_write=ok unix_outside=denied "
                "work_write=ok")
        assert out.strip().endswith(want), (rc, out)
        assert not real.exists(), "a sandboxed run wrote under the real home"
        assert outside.poll() is None, "a sandboxed run signalled an outside process"
        return "sandboxed"
    finally:
        if real.exists():
            real.unlink()
        shutil.rmtree(work, ignore_errors=True)
        outside.kill()
        outside.wait()
        live.close()
        usrv.close()
        shutil.rmtree(udir, ignore_errors=True)
        shutil.rmtree(tmp, ignore_errors=True)


def _self_test():
    contained = _self_test_containment()
    d = Path(tempfile.mkdtemp(prefix="mini-mutate-st-"))
    try:
        base = "def f(a, b):\n    return a + b\n"
        ref = ('def f(a, b):\n    """doc"""\n    if a > b and b >= 0:\n        return a - b\n    return a + b\n\n\n'
               'def g(x: "T") -> "T":\n    return f"{x}!"\n')
        (d / "m.py").write_text(ref)
        strong = "from m import f\nassert f(3, 1) == 2\nassert f(1, 3) == 4\nassert f(2, 2) == 4\nassert f(3, -1) == 2\nassert f(1, 0) == 1\n"
        weak = "from m import f\nassert f(1, 3) == 4\n"
        lines = changed_lines(base, ref)
        assert lines == {2, 3, 4, 6, 7, 8, 9}, lines
        ms = mutants("m.py", ref, lines)
        ops = {m[1] for m in ms}
        assert {"cmp-flip", "cmp-edge", "bool-swap", "arith", "const", "return-none", "del-stmt"} <= ops, ops
        assert not any(m[0] == 2 for m in ms), "docstring line mutated"
        assert all("T" not in m[2] or m[1] == "del-stmt" or m[1] == "return-none" for m in ms if m[0] == 8), "annotation mutated"
        for m in ms:
            compile(m[4], "m.py", "exec")
        (d / "t.py").write_text(strong)
        rs = score(d, {"m.py": base}, "python3 t.py", jobs=2)
        (d / "t.py").write_text(weak)
        rw = score(d, {"m.py": base}, "python3 t.py", jobs=2)
        assert "unscored" not in rs and "unscored" not in rw, (rs, rw)
        assert rs["score"] > rw["score"], (rs["score"], rw["score"])
        assert rs["killed"] + rs["timeout"] + rs["survived"] == rs["total"] > 5
        (d / "t.py").write_text("assert True\n")
        rc = score(d, {"m.py": base}, "python3 t.py")
        assert "canary survived" in rc.get("unscored", ""), rc
        (d / "t.py").write_text("from m import f\nassert f(1, 1) == 3\n")
        rf = score(d, {"m.py": base}, "python3 t.py")
        assert "fails on the unmutated ref" in rf.get("unscored", ""), rf
        assert (d / "m.py").read_text() == ref, "work tree was written"
        nm = mutants("m.py", ref, set())
        assert nm == [], "mutants outside the changed lines"
        # breadth: a prefix match the test cannot tell from an exact one survives; a near-miss case kills it
        kb = "def k(label):\n    if label.startswith('sonarr'):\n        return 'tv'\n    return 'other'\n"
        kr = "def k(label):\n    if label.startswith('sonarr') or label.startswith('library-seed'):\n        return 'tv'\n    return 'other'\n"
        kms = [m for m in mutants("k.py", kr, changed_lines(kb, kr)) if m[1] == "breadth"]
        assert any(m[3] == "((label) == 'library-seed')" for m in kms), kms
        eq = [m for m in mutants("e.py", "def e(x):\n    return x == 'ab' or 'cd' in x or 'ef' in ('ef',)\n", {2}) if m[1] == "breadth"]
        assert {m[3] for m in eq} == {"(str(x).startswith('ab'))", "((x) == 'cd')"}, eq
        (d / "k.py").write_text(kr)
        (d / "tk.py").write_text("from k import k\nassert k('library-seed') == 'tv'\nassert k('radarr') == 'other'\n")
        rk = score(d, {"k.py": kb}, "python3 tk.py", jobs=2)
        assert any(x["op"] == "breadth" and "library-seed" in x["after"] for x in rk.get("survivors", [])), rk
        (d / "tk.py").write_text("from k import k\nassert k('library-seed') == 'tv'\nassert k('library-seed2') == 'other'\n"
                                 "assert k('sonarr2') == 'tv'\nassert k('radarr') == 'other'\n")
        (d / "k.py").write_text(kr.replace("label.startswith('library-seed')", "label == 'library-seed'"))
        rk2 = score(d, {"k.py": kb}, "python3 tk.py", jobs=2)
        assert not any(x["op"] == "breadth" for x in rk2.get("survivors", [])), rk2
        print(f"mini_mutate self-test ok (strong {rs['score']} vs weak {rw['score']}, {len(ms)} mutants, containment: {contained})")
    finally:
        shutil.rmtree(d, ignore_errors=True)


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--work")
    ap.add_argument("--pair", action="append", default=[], help="REL=BASEFILE (BASEFILE may be /dev/null for a new file)")
    ap.add_argument("--test-cmd")
    ap.add_argument("--max", type=int, default=60)
    ap.add_argument("--jobs", type=int, default=4)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        _self_test()
        return 0
    if not (a.work and a.pair and a.test_cmd):
        ap.error("--work, --pair and --test-cmd are required")
    pairs = {}
    for p in a.pair:
        rel, _, bf = p.partition("=")
        pairs[rel] = Path(bf).read_text(errors="replace") if bf and Path(bf).is_file() else ""
    r = score(a.work, pairs, a.test_cmd, max_mutants=a.max, jobs=a.jobs)
    if a.json:
        print(json.dumps(r, indent=1))
    elif "unscored" in r:
        print(f"MUTATION unscored: {r['unscored']}")
    else:
        print(f"MUTATION killed={r['killed']} survived={r['survived']} timeout={r['timeout']} total={r['total']} score={r['score']}")
        for s in r["survivors"]:
            print(f"survived {s['file']}:{s['line']} {s['op']}: {s['before']} -> {s['after']}")
    return 0 if "unscored" not in r else 3


if __name__ == "__main__":
    sys.exit(main())
