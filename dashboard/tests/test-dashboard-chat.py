"""Adversarial safety + behaviour tests for dashboard_chat.py (stdlib only). Exit 1 on any failure."""
import ast, base64, importlib.util, json, os, shutil, sys, tempfile
from pathlib import Path

HERE = Path(os.environ.get("DASHBOARD_SRC") or Path(__file__).resolve().parent.parent / "src")  # repo src/
spec = importlib.util.spec_from_file_location("dc", HERE / "dashboard_chat.py")
dc = importlib.util.module_from_spec(spec)
sys.modules["dc"] = dc
spec.loader.exec_module(dc)

PNG = b"\x89PNG\r\n\x1a\n" + b"0" * 16
SID = "0123456789abcdef" * 2
FAILS = []


def check(name, fn):
    try:
        ok, detail = fn()
    except Exception as e:
        ok, detail = False, "raised %s: %s" % (type(e).__name__, e)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else " -- " + str(detail)))
    if not ok:
        FAILS.append(name)


def tmp():
    return tempfile.mkdtemp(prefix="dcsafe")


def proj(files=None, symlink=None):
    base, root = tmp(), tmp()
    for rel, data in (files or {}).items():
        p = Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    if symlink:
        os.symlink(*symlink(root))
    json.dump([{"name": "p", "path": root}], open(os.path.join(base, "projects.json"), "w"))
    return base, root


def raises(fn, *exc):
    try:
        fn()
    except exc:
        return True, ""
    except Exception as e:
        return False, "wrong exception %s" % type(e).__name__
    return False, "no exception"


# --- stdlib only (AST) ---
def t_stdlib():
    tree = ast.parse((HERE / "dashboard_chat.py").read_text())
    bad = []
    for n in ast.walk(tree):
        names = [a.name for a in n.names] if isinstance(n, ast.Import) else [n.module or ""] if isinstance(n, ast.ImportFrom) else []
        for m in names:
            if m.split(".")[0] not in sys.stdlib_module_names:
                bad.append(m)
    return not bad, bad
check("only stdlib imports", t_stdlib)

# --- session ids ---
for bad in (SID + "\n", "../" + SID, SID[:-1], SID.upper(), "", SID + "/x"):
    check("sid rejected %r" % bad[-6:], lambda b=bad: raises(lambda: dc.validate_session_id(b), ValueError))
    check("write_session rejects %r" % bad[-6:], lambda b=bad: raises(lambda: dc.write_session(tmp(), b, {}), ValueError))

# --- images ---
check("GIF87a/GIF89a ok, bare GIF8 not", lambda: ((dc.validate_image(b"GIF89a" + b"0" * 10), dc.validate_image(b"GIF87a" + b"0" * 10), dc.validate_image(b"GIF8xx0000")) == ("gif", "gif", None), None))
check("empty bytes rejected", lambda: (dc.validate_image(b"") is None, None))
check("RIFF non-WEBP rejected", lambda: (dc.validate_image(b"RIFF0000WAVE") is None, None))
check("7 images over limit", lambda: (dc.check_image_limits([PNG] * 7) is False, None))
check("8MB+1 over limit", lambda: (dc.check_image_limits([PNG + b"0" * (8 * 1024 * 1024)]) is False, None))

def t_store_cap():
    b = tmp()
    outs = [dc.stores_image(b, SID, PNG) for _ in range(7)]
    return all(not o.startswith("error") for o in outs[:6]) and outs[6].startswith("error"), outs[-1]
check("store caps at 6", t_store_cap)

def t_store_unique():
    b = tmp()
    d = Path(b) / "images" / SID
    p0 = dc.stores_image(b, SID, PNG)
    os.remove(p0)
    p1 = dc.stores_image(b, SID, PNG)
    p2 = dc.stores_image(b, SID, PNG)
    return len({p1, p2}) == 2 and Path(p1).read_bytes() == PNG, (p1, p2)
check("stored filenames never overwrite", t_store_unique)

# --- project confinement ---
def t_read(path, files=None, **kw):
    base, root = proj(files or {"a.py": b"print(1)\n"}, **kw)
    return lambda: dc.read_project_file(base, "p", path)

check("read ok", lambda: (t_read("a.py")() == "print(1)\n", None))
check("abs path rejected", lambda: raises(t_read("/etc/passwd"), ValueError))
check("dotdot rejected", lambda: raises(t_read("../x"), ValueError))
check("dotdot mid-path rejected", lambda: raises(t_read("sub/../../x"), ValueError))
check("symlink escape rejected", lambda: raises(t_read("lnk", symlink=lambda r: ("/etc/hosts", os.path.join(r, "lnk"))), ValueError))
check("NUL binary rejected", lambda: raises(t_read("b.bin", {"b.bin": b"ab\x00cd"}), ValueError))
check("256KB+1 rejected", lambda: raises(t_read("big.txt", {"big.txt": b"a" * (256 * 1024 + 1)}), ValueError))
check("256KB exactly ok", lambda: (len(t_read("big.txt", {"big.txt": b"a" * (256 * 1024)})()) == 256 * 1024, None))
for d in (".git", "node_modules", ".venv", "__pycache__"):
    check("read skips %s" % d, lambda d=d: raises(t_read(d + "/x.py", {d + "/x.py": b"secret"}), ValueError, FileNotFoundError))
check("unknown project", lambda: raises(lambda: dc.read_project_file(proj()[0], "nope", "a.py"), ValueError))
check("non-utf8 gives ValueError not crash", lambda: raises(t_read("l.txt", {"l.txt": b"\xff\xfe\xfa"}), ValueError))

def t_tree():
    base, root = proj({"a.py": b"x", "sub/b.py": b"x", ".git/c": b"x", "node_modules/d": b"x"})
    return sorted(dc.list_project_tree(base, "p")) == ["a.py", os.path.join("sub", "b.py")], dc.list_project_tree(base, "p")
check("tree skips junk dirs", t_tree)
check("tree path escape rejected", lambda: raises(lambda: dc.list_project_tree(proj()[0], "p", "../.."), ValueError))
check("tree prefix-sibling escape rejected", lambda: raises(lambda: dc.list_project_tree(*(lambda b, r: (b, "p", "../" + os.path.basename(r) + "x"))(*proj())), ValueError, FileNotFoundError))

# --- request shape: the model must actually see turns and file CONTENT ---
def t_roles():
    r = dc.build_request("p", [{"role": "user", "content": "q1"}, {"role": "assistant", "content": "a1"}, {"role": "user", "content": "q2"}], [], [])
    roles = [m["role"] for m in r["messages"]]
    return roles[0] == "system" and roles[1:] == ["user", "assistant", "user"], roles
check("history keeps separate roles", t_roles)

def t_filecontent():
    r = dc.build_request("p", [{"role": "user", "content": "q"}], [{"path": "a.py", "content": "SECRET_BODY"}], [])
    return "SECRET_BODY" in json.dumps(r) and "a.py" in json.dumps(r), None
check("attached file content reaches the model", t_filecontent)

def t_prompt():
    r = dc.build_request("p", [], [], [])
    s = json.dumps(r["messages"][0])
    return "only with fenced" not in s.lower() and "diff" in s.lower(), "system prompt forbids discussion"
check("system prompt allows conversation, asks diffs for changes", t_prompt)


def t_job_files():
    base, root = proj({"a.py": b"UNIQUE_BODY_123\n"})
    os.environ["DASHBOARD_CHAT_HOME"] = base
    dc.write_session(base, SID, {"project": "p", "created": "c", "messages": [
        {"role": "user", "content": "look", "files": ["a.py"], "images": []},
        {"role": "assistant", "content": "", "status": "queued", "files": [], "images": []}]})
    seen = []
    dc.job(SID, 1, base_dir=base, call=lambda r: (seen.append(r), {"status": "done", "text": "ok"})[1])
    return "UNIQUE_BODY_123" in json.dumps(seen), "job sends file paths only, never contents"
check("job() sends attached file CONTENT to the model", t_job_files)

def t_job_badfile():
    base, root = proj({"a.py": b"x"})
    dc.write_session(base, SID, {"project": "p", "created": "c", "messages": [
        {"role": "user", "content": "look", "files": ["../../etc/passwd"], "images": []},
        {"role": "assistant", "content": "", "status": "queued", "files": [], "images": []}]})
    seen = []
    dc.job(SID, 1, base_dir=base, call=lambda r: (seen.append(r), {"status": "done", "text": "ok"})[1])
    return "root:" not in json.dumps(seen), "escaped path content leaked"
check("job() never leaks files outside the project", t_job_badfile)

# --- key never leaks ---
def t_leak():
    h = tempfile.mkdtemp(); os.makedirs(h + "/.darkbloom")
    json.dump({"api_key": "KEY-abc123"}, open(h + "/.darkbloom/local.json", "w"))
    old = os.environ.get("HOME"); os.environ["HOME"] = h
    try:
        outs = [dc.call_model({"messages": []}, http_func=lambda u, hd, b, t: (200, b"{bad KEY-abc123")),
                dc.call_model({"messages": []}, http_func=lambda u, hd, b, t: (_ for _ in ()).throw(RuntimeError("KEY-abc123 boom"))),
                dc.call_model({"messages": []}, http_func=lambda u, hd, b, t: (500, b"KEY-abc123"))]
    finally:
        os.environ["HOME"] = old
    return all("KEY-abc123" not in json.dumps(o) for o in outs), outs
check("key never in call_model results", t_leak)

def t_endpoint():
    import types
    seen = []
    fake = types.ModuleType("queue_config")
    fake.darkbloom_endpoint = lambda: ("http://db.example:9000", "FAKE-KEY")
    old = sys.modules.get("queue_config"); sys.modules["queue_config"] = fake
    try:
        out = dc.call_model({"messages": []}, http_func=lambda u, hd, b, t: (seen.append((u, hd["Authorization"])),
                                                                         (200, b'{"choices":[{"message":{"content":"hi"}}]}'))[1])
    finally:
        if old is None: sys.modules.pop("queue_config", None)
        else: sys.modules["queue_config"] = old
    return seen == [("http://db.example:9000/v1/chat/completions", "Bearer FAKE-KEY")] and out.get("text") == "hi", (seen, out)
check("call_model uses queue_config.darkbloom_endpoint() when importable", t_endpoint)

def t_endpoint_v1():
    import types
    seen = []
    fake = types.ModuleType("queue_config")
    fake.darkbloom_endpoint = lambda: ("http://127.0.0.1:8000/v1", "FAKE-KEY")
    old = sys.modules.get("queue_config"); sys.modules["queue_config"] = fake
    try:
        dc.call_model({"messages": []}, http_func=lambda u, hd, b, t: (seen.append(u),
                                                                    (200, b'{"choices":[{"message":{"content":"hi"}}]}'))[1])
    finally:
        if old is None: sys.modules.pop("queue_config", None)
        else: sys.modules["queue_config"] = old
    return seen == ["http://127.0.0.1:8000/v1/chat/completions"], seen
check("a base_url ending in /v1 (local.json on the Mac) is not doubled", t_endpoint_v1)

def t_endpoint_fallback():
    # queue_config not importable: the base comes from Darkbloom's local.json (it follows the bind address)
    res = []
    for lj, want in (({"api_key": "FAKE-KEY", "base_url": "http://192.0.2.5:8000/v1"}, "http://192.0.2.5:8000/v1/chat/completions"),
                     ({"api_key": "FAKE-KEY"}, "http://127.0.0.1:8000/v1/chat/completions"),
                     ({"api_key": "FAKE-KEY", "base_url": "file:///etc/passwd"}, "http://127.0.0.1:8000/v1/chat/completions")):
        h = tempfile.mkdtemp(); os.makedirs(h + "/.darkbloom")
        json.dump(lj, open(h + "/.darkbloom/local.json", "w"))
        seen = []
        old_home, old_mod = os.environ.get("HOME"), sys.modules.get("queue_config", "absent")
        os.environ["HOME"] = h; sys.modules["queue_config"] = None   # import_module raises ImportError
        try:
            dc.call_model({"messages": []}, http_func=lambda u, hd, b, t: (seen.append(u),
                                                                        (200, b'{"choices":[{"message":{"content":"hi"}}]}'))[1])
        finally:
            os.environ["HOME"] = old_home
            if old_mod == "absent": sys.modules.pop("queue_config", None)
            else: sys.modules["queue_config"] = old_mod
        res.append(seen == [want])
    return all(res), res
check("without queue_config, call_model follows local.json's base_url (http(s) only, else loopback)", t_endpoint_fallback)

# --- routes ---
def t_routes():
    os.environ["DASHBOARD_CHAT_HOME"] = tmp()
    out = []
    for m, p, q, b in (("GET", "/nope", {}, b""), ("POST", "/api/chat/sessions", {}, b"not json"), ("POST", "/api/chat/sessions", {}, b"[]"),
                       ("GET", "/api/chat/sessions/../../etc", {}, b""), ("GET", "/api/chat/sessions/" + "z" * 32, {}, b""),
                       ("GET", "/api/chat/projects/p/file", {"path": ["../../etc/passwd"]}, b""),
                       ("POST", "/api/chat/sessions/" + SID + "/messages", {}, b'{"text": 5}')):
        try:
            s = dc.handle(m, p, q, b)[0]
        except Exception as e:
            s = "raised " + type(e).__name__
        out.append(s)
    return all(isinstance(s, int) and 400 <= s < 500 for s in out), out
check("hostile requests give 4xx, never raise", t_routes)

def t_cli_bad():
    import subprocess
    r = subprocess.run([sys.executable, str(HERE / "dashboard_chat.py"), "job", "../x", "1"], capture_output=True, text=True)
    return r.returncode == 1 and "Traceback" not in r.stderr, r.stderr[:100]
check("CLI bad id exits 1 cleanly", t_cli_bad)

def t_enqueue_argv():
    import stat
    b = tmp(); os.environ["DASHBOARD_CHAT_HOME"] = b
    (Path(b) / "sessions").mkdir()
    dc.write_session(b, SID, {"id": SID, "project": "p", "created": "", "messages": [
        {"role": "user", "content": "investigate X", "files": [], "images": [], "ts": "", "status": "completed"},
        {"role": "assistant", "content": "", "files": [], "images": [], "ts": "", "status": "queued", "mode": "job"}]})
    rec = Path(b) / "argv.txt"
    fake = Path(b) / "fakequeue.sh"
    fake.write_text('#!/bin/sh\necho "$@" > "%s"\n' % rec)
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    os.environ["DASHBOARD_CHAT_QUEUE_CMD"] = str(fake)
    try:
        dc.enqueue_job(SID, 1)
    finally:
        del os.environ["DASHBOARD_CHAT_QUEUE_CMD"]
    argv = rec.read_text()
    task = (Path(b) / "jobs" / (SID + "-1") / "TASK.md").read_text()
    ok = ("--task-kind coding" in argv and "--capture-final-as ANSWER.md" in argv
          and "investigate X" in task and "ANSWER.md" in task and "enqueue" in argv)
    return ok, argv + task[:80]
check("enqueue_job: coding kind, answer-file instruction, uses overridable queue cmd (never the live queue)", t_enqueue_argv)

# --- Darkbloom busy (sandbox hold / restart): direct messages wait and retry instead of failing ---
import threading, types

def chat_session(extra=()):
    b = tmp(); os.environ["DASHBOARD_CHAT_HOME"] = b
    dc.write_session(b, SID, {"project": "p", "created": "", "messages": [
        {"role": "user", "content": "hi", "files": [], "images": [], "status": "completed"},
        {"role": "assistant", "content": "", "files": [], "images": [], "status": "running", "mode": "chat"},
        *extra]})
    return b

def t_busy_classes():
    got = [dc.darkbloom_busy(r) for r in (
        {"status": "error", "text": "http 503"}, {"status": "error", "text": "http 429"},
        {"status": "error", "text": "URLError: <urlopen error [Errno 61] Connection refused>"},
        {"status": "error", "text": "RemoteDisconnected: Remote end closed connection without response"},
        {"status": "error", "text": "http 400"}, {"status": "error", "text": "http 500"},
        {"status": "error", "text": "no api key"}, {"status": "done", "text": "http 503"}, None)]
    return got == [True, True, True, True, False, False, False, False, False], got
check("busy = 429/503/connection refused, not 400/500/no key/done", t_busy_classes)

def t_wait_then_answer():
    b = chat_session()
    answers = iter([{"status": "error", "text": "http 503"}, {"status": "error", "text": "http 503"},
                    {"status": "done", "text": "hello"}])
    seen, sleeps = [], []
    def sleep(s):
        m = dc.read_session(b, SID)["messages"][1]
        seen.append((m["status"], m.get("wait_reason", ""), m["content"]))
        sleeps.append(s)
    dc.job(SID, 1, base_dir=b, call=lambda r: next(answers), sleep=sleep, wait_reason=lambda r: "WAITING")
    m = dc.read_session(b, SID)["messages"][1]
    ok = (seen == [("queued", "WAITING", "")] * 2 and sleeps == [15, 30] and m["status"] == "done"
          and m["content"] == "hello" and "wait_reason" not in m and "wait_since" not in m)
    return ok, (seen, sleeps, m)
check("503 -> message queued with a reason, retried, then answered (reason cleared)", t_wait_then_answer)

def t_give_up():
    b = chat_session()
    t = [0.0]
    def sleep(s): t[0] += 3600
    dc.job(SID, 1, base_dir=b, call=lambda r: {"status": "error", "text": "http 503"}, sleep=sleep,
           clock=lambda: t[0], wait_reason=lambda r: "W")
    m = dc.read_session(b, SID)["messages"][1]
    return (m["status"] == "error" and "http 503" in m["content"] and "stayed busy" in m["content"]
            and "wait_reason" not in m and t[0] >= dc.CHAT_WAIT_MAX_S), m
check("gives up with an error after CHAT_WAIT_MAX_S", t_give_up)

def t_other_error_now():
    b = chat_session()
    sleeps = []
    dc.job(SID, 1, base_dir=b, call=lambda r: {"status": "error", "text": "http 400"}, sleep=sleeps.append)
    m = dc.read_session(b, SID)["messages"][1]
    return m["status"] == "error" and m["content"] == "http 400" and not sleeps, (m, sleeps)
check("a non-busy error fails at once, no wait", t_other_error_now)

def t_no_lost_turns():
    b = chat_session()
    answers = iter([{"status": "error", "text": "http 503"}, {"status": "done", "text": "A1"}])
    def sleep(s):  # the user sends another message while the first one waits
        dc._append_turn(b, SID, {"role": "user", "content": "second", "files": [], "images": [], "status": "completed"},
                        {"role": "assistant", "content": "", "files": [], "images": [], "status": "queued", "mode": "job"})
    dc.job(SID, 1, base_dir=b, call=lambda r: next(answers), sleep=sleep, wait_reason=lambda r: "W")
    ms = dc.read_session(b, SID)["messages"]
    return len(ms) == 4 and ms[1]["content"] == "A1" and ms[2]["content"] == "second", ms
check("a message sent during the wait is kept (no stale overwrite)", t_no_lost_turns)

def t_in_order():
    b = chat_session(({"role": "user", "content": "q2", "files": [], "images": [], "status": "completed"},
                      {"role": "assistant", "content": "", "files": [], "images": [], "status": "running", "mode": "chat"}))
    go, calls = threading.Event(), []
    def call(r):
        calls.append(json.dumps(r))
        return {"status": "error", "text": "http 503"} if len(calls) == 1 else {"status": "done", "text": "A%d" % len(calls)}
    t1 = threading.Thread(target=dc.job, args=(SID, 1), kwargs=dict(base_dir=b, call=call, sleep=lambda s: go.wait(5), wait_reason=lambda r: "W"))
    t1.start()
    for _ in range(250):  # up to 5 s for the first call
        if calls or not t1.is_alive():
            break
        threading.Event().wait(0.02)
    t2 = threading.Thread(target=dc.job, args=(SID, 3), kwargs=dict(base_dir=b, call=call, sleep=lambda s: None))
    t2.start(); t2.join(0.3)
    early = len(calls)
    go.set(); t1.join(5); t2.join(5)
    ms = dc.read_session(b, SID)["messages"]
    return (early == 1 and ms[1]["content"] == "A2" and ms[3]["content"] == "A3" and "A2" in calls[2]), (early, calls[2][-200:], ms)
check("direct messages of one session go in order; the 2nd sees the 1st answer", t_in_order)

def t_resume():
    b = chat_session(({"role": "user", "content": "j", "files": [], "images": [], "status": "completed"},
                      {"role": "assistant", "content": "", "files": [], "images": [], "status": "queued", "mode": "job"}))
    dc._set_message(b, SID, 1, status="queued", wait_reason="W", wait_since=1)
    started, real = [], dc.start_direct
    dc.start_direct = lambda sid, i, base_dir=None: (started.append(i), True)[1]
    try:
        n = dc.resume_direct(b, SID)
    finally:
        dc.start_direct = real
    return n == 1 and started == [1], (n, started)
check("resume_direct restarts only waiting direct messages (not queue jobs)", t_resume)

def t_start_once():
    b = chat_session()
    gate, calls = threading.Event(), []
    real = dc.job
    dc.job = lambda sid, i, base_dir=None: (calls.append(i), gate.wait(5))
    try:
        first, second = dc.start_direct(SID, 1, base_dir=b), dc.start_direct(SID, 1, base_dir=b)
        gate.set()
        for _ in range(100):
            if (SID, 1) not in dc._DIRECT_ACTIVE:
                break
            threading.Event().wait(0.02)
    finally:
        dc.job = real
    return first is True and second is False and calls == [1] and (SID, 1) not in dc._DIRECT_ACTIVE, (first, second, calls)
check("start_direct never runs two threads for one message", t_start_once)

def t_collect_skips_chat():
    b = chat_session()
    dc._set_message(b, SID, 1, status="queued", wait_reason="W")
    jd = Path(b) / "jobs" / (SID + "-1"); jd.mkdir(parents=True)
    (jd / "ANSWER.md").write_text("STRAY")
    n = dc.collect_job_results(b, SID)
    m = dc.read_session(b, SID)["messages"][1]
    return n == 0 and m["content"] == "" and m["wait_reason"] == "W", (n, m)
check("collect_job_results leaves waiting direct messages alone", t_collect_skips_chat)

def t_reason_from_queue():
    fake = types.ModuleType("ollama_queue_lib")
    fake.darkbloom_view = lambda: {"cause": "sandbox-hold", "hold": True, "sandbox_jobs": 1, "at": 0}
    fake.darkbloom_cause_phrase = lambda v: "Darkbloom lends the machine to a Darkbloom Sandbox job"
    old = sys.modules.get("ollama_queue_lib"); sys.modules["ollama_queue_lib"] = fake
    try:
        a = dc.darkbloom_wait_reason({"status": "error", "text": "http 503"})
        fake.darkbloom_cause_phrase = lambda v: ""
        b2 = dc.darkbloom_wait_reason({"status": "error", "text": "http 503"})
        fake.darkbloom_view = lambda: (_ for _ in ()).throw(RuntimeError("x"))
        c = dc.darkbloom_wait_reason({"status": "error", "text": "http 503"})
    finally:
        if old is None:
            sys.modules.pop("ollama_queue_lib", None)
        else:
            sys.modules["ollama_queue_lib"] = old
    return ("Sandbox job" in a and "1 Darkbloom Sandbox job running" in b2 and "http 503" in c and "queued" in c), (a, b2, c)
check("wait reason names the sandbox hold when the queue module knows it, else the HTTP answer", t_reason_from_queue)

def t_post_chat_returns_202():
    b = chat_session()
    real = dc.start_direct; got = []
    dc.start_direct = lambda sid, i, base_dir=None: got.append(i)
    try:
        st, _, body = dc.handle("POST", "/api/chat/sessions/" + SID + "/messages", {}, json.dumps({"text": "x", "mode": "chat"}).encode())
    finally:
        dc.start_direct = real
    ms = dc.read_session(b, SID)["messages"]
    return st == 202 and got == [3] and len(ms) == 4 and json.loads(body)["message_index"] == 3, (st, got, len(ms))
check("POST message appends both turns and starts the direct job", t_post_chat_returns_202)

print("%d failed" % len(FAILS))
sys.exit(1 if FAILS else 0)
