#!/usr/bin/env python3
"""test-queue-upload.py -- POST /api/jobs/upload and GET /api/jobs/<id>/result of the queue API, with the real Handler on
127.0.0.1 port 0 (a subclass reports a LAN peer so the bearer token is enforced), plus the client side (queue_remote.py).
Proves: 401 without / with a wrong token; a round trip from a throwaway git repo (unpushed commit + uncommitted edit +
untracked test file + deleted file) ends as a pending job whose cwd and runner are the QUEUE's paths and whose unpacked
baseline equals the source tree; the result route returns job.json and the work dir; a body over the cap gets 413 before
it is read; tars with ../ names, absolute names, an escaping symlink or a device are refused; a bundle without the claimed
commit and a runner that is not a bare name are refused; refusals leave no run dir behind; unsupported enqueue flags raise.
Sandboxed: temp HOME with a copy of --bin (the pipeline bin) with the dashboard src overlaid. Fake token only.
Run (sandbox only): python3 tests/test-queue-upload.py [--bin PIPELINE_BIN]"""
import argparse
import http.client
import importlib.util
import io
import json
import re
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import threading
from pathlib import Path

HERE = Path(__file__).resolve().parent
ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=os.path.join(os.environ["HOME"], "bin"))
ARGS = ap.parse_args()
SRC = HERE.parent / "src"
HOME = tempfile.mkdtemp(prefix="qupload-home-")
os.environ.update(HOME=HOME, DISPATCH_VERIFY_SANDBOX="1", OLLAMA_QUEUE_NO_NOTIFY="1", QUEUE_API_TOKEN="fake-test-token",
                  MINI_SUBMIT_HOME=str(Path(HOME) / ".ollama-dispatch"), QUEUE_UPLOAD_MAX_MB="1")
B = Path(HOME) / "bin"
shutil.copytree(ARGS.bin, B, symlinks=True, ignore=shutil.ignore_patterns(
    "ollama-queue-logs", "ollama-queue-livelogs", "__pycache__", "*.log", ".git"))
for f in SRC.glob("*.py"):
    d = B / f.name
    if d.is_symlink() or d.exists():
        d.unlink()
    shutil.copy(f, d)
(B / "test-runner.py").write_text("print('runner')\n")
sys.path.insert(0, str(B))
spec = importlib.util.spec_from_file_location("oqapi_up", B / "ollama-queue-api.py")
api = importlib.util.module_from_spec(spec)
spec.loader.exec_module(api)
api.q.STATE_PATH = Path(HOME) / "t-state.json"
api.q.LOCK_PATH = Path(HOME) / "t-state.lock"
import queue_remote  # noqa: E402
import queue_upload  # noqa: E402

FAILS = []


def ok(name, cond, extra=""):
    print(("  ok   " if cond else "  FAIL ") + name + ("" if cond else " " + str(extra)))
    if not cond:
        FAILS.append(name)


def serve(peer):
    class H(api.Handler):
        def __init__(self, request, client_address, server):
            super().__init__(request, (peer, client_address[1]), server)
    srv = api.ThreadingServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


SRV = serve("192.0.2.7")
BASE = "http://127.0.0.1:%d" % SRV.server_address[1]
GOOD = {"Authorization": "Bearer fake-test-token"}


def post(path, body, headers):
    c = http.client.HTTPConnection("127.0.0.1", SRV.server_address[1], timeout=30)
    try:
        c.request("POST", path, body=body, headers=dict(headers, **{"Content-Length": str(len(body))}))
        r = c.getresponse()
        return r.status, r.read()
    except (ConnectionError, BrokenPipeError) as e:
        return -1, repr(e).encode()
    finally:
        c.close()


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", *a], check=True,
                          capture_output=True, text=True).stdout


def tar_bytes(members):
    """members: list of (name, bytes|None, kind) kind in file/sym/dev."""
    bio = io.BytesIO()
    with tarfile.open(fileobj=bio, mode="w") as tf:
        for name, data, kind in members:
            ti = tarfile.TarInfo(name)
            if kind == "sym":
                ti.type, ti.linkname = tarfile.SYMTYPE, data
                tf.addfile(ti)
            elif kind == "dev":
                ti.type = tarfile.CHRTYPE
                tf.addfile(ti)
            else:
                ti.size = len(data)
                tf.addfile(ti, io.BytesIO(data))
    return bio.getvalue()


def run_dirs():
    r = queue_upload.work_root()
    return sorted(p.name for p in r.iterdir()) if r.exists() else []


# ---- repo with unpushed commit, uncommitted edit, untracked test file, deleted file ---------------------------------
tmp = Path(tempfile.mkdtemp(prefix="qupload-src-"))
repo = tmp / "repo"
repo.mkdir()
git(repo, "init", "-q")
(repo / "app.py").write_text("x = 1\n")
(repo / "gone.txt").write_text("bye\n")
(repo / "keep.txt").write_text("k\n")
git(repo, "add", "-A")
git(repo, "commit", "-qm", "one")
(repo / "app.py").write_text("x = 2\n")
git(repo, "commit", "-qam", "two (never pushed)")
(repo / "keep.txt").write_text("k edited, uncommitted\n")
(repo / "test_new.py").write_text("def test_x():\n    assert 1\n")
(repo / "gone.txt").unlink()
(repo / "node_modules").mkdir()
(repo / "node_modules" / "junk.js").write_text("junk")
(repo / ".gitignore").write_text("node_modules/\n")
task = tmp / "task.md"
task.write_text("fix the thing\n")
specf = tmp / "spec.json"
specf.write_text(json.dumps({"baseline_from": str(repo), "task": str(task), "mode": "auto"}))

print("auth")
pkg = tmp / "pkg.tar"
man = queue_remote.build_package(specf, {"model": "m1", "label": "up-roundtrip"}, "test-runner.py", pkg)
ok("manifest has a git tree and a file tree", [t["kind"] for t in man["trees"]] == ["git", "dir"], man["trees"])
body = pkg.read_bytes()
st, _ = post("/api/jobs/upload", body, {})
ok("no token -> 401", st == 401, st)
st, _ = post("/api/jobs/upload", body, {"Authorization": "Bearer wrong"})
ok("wrong token -> 401", st == 401, st)

print("round trip")
res = queue_remote.upload(BASE, GOOD, pkg)
jid = res.get("id")
ok("upload answers a job id", bool(jid), res)
with api.q._Locked() as lock:
    state = lock.load()
job = next((j for j in state["jobs"] if j["id"] == jid), None)
ok("job row exists and is pending", job is not None and job["status"] == "pending", job)
root = queue_upload.work_root()
ok("cwd is under the queue's mini-work", job and str(Path(job["cwd"]).resolve()).startswith(str(root.resolve())), job and job["cwd"])
ok("runner is the queue's own bin file", job and job["runner"] == str((B / "test-runner.py").resolve()), job and job["runner"])
ok("label and model carried", job and job["label"] == "up-roundtrip" and job["model"] == "m1")
sp = json.loads(Path(job["task_file"]).read_text())
unp = Path(sp["baseline_from"])
ok("baseline_from rewritten into the run dir", str(unp).startswith(str(root.resolve())) or str(unp).startswith(str(root)), unp)
ok("task rewritten and its content shipped", Path(sp["task"]).read_text() == "fix the thing\n", sp["task"])
ok("unpushed commit content present", (unp / "app.py").read_text() == "x = 2\n")
ok("uncommitted edit present", (unp / "keep.txt").read_text() == "k edited, uncommitted\n")
ok("untracked test file present", (unp / "test_new.py").is_file())
ok("deleted file absent", not (unp / "gone.txt").exists())
ok("ignored dir not shipped", not (unp / "node_modules").exists())
ok("source tree is unchanged by packing", (repo / "keep.txt").read_text() == "k edited, uncommitted\n")
(Path(job["cwd"]) / "out.txt").write_text("produced\n")
(Path(job["cwd"]) / "node_modules").mkdir()
(Path(job["cwd"]) / "node_modules" / "x.js").write_text("n")
(Path(job["cwd"]) / "lnk").symlink_to("/etc/hosts")
got = queue_remote.fetch_result(BASE, GOOD, jid, tmp / "result")
ok("result has job.json", (got / "job.json").is_file())
ok("result has the work file", (got / "work" / "out.txt").read_text() == "produced\n")
ok("result skips node_modules and symlinks", not (got / "work" / "node_modules").exists() and not (got / "work" / "lnk").exists())
c = http.client.HTTPConnection("127.0.0.1", SRV.server_address[1])
c.request("GET", "/api/jobs/%s/result" % jid)
r = c.getresponse(); r.read()
ok("result without token -> 401", r.status == 401, r.status)
c.close()
c = http.client.HTTPConnection("127.0.0.1", SRV.server_address[1])
c.request("GET", "/api/jobs/ffffffffffff/result", headers=GOOD)
r = c.getresponse(); r.read()
ok("result for an unknown job -> 404", r.status == 404, r.status)
c.close()
dup = queue_remote.build_package(specf, {"model": "m1", "label": "up-roundtrip"}, "test-runner.py", tmp / "pkg2.tar")
before = run_dirs()
st, out = post("/api/jobs/upload", (tmp / "pkg2.tar").read_bytes(), GOOD)
ok("same live label -> 409 and the run dir is removed", st == 409 and run_dirs() == before, (st, out[:80]))

print("refusals")
before = run_dirs()
st, out = post("/api/jobs/upload", b"x" * (2 * 1024 * 1024), GOOD)
ok("over the cap -> 413", st in (413, -1), st)
good_man = lambda **k: json.dumps(dict({"version": 1, "job": {"model": "m", "label": "bad-case"}, "runner": "test-runner.py",
                                         "trees": [], "refs": []}, **k)).encode()
cases = {
    "../ name": [("manifest.json", good_man(), "file"), ("spec.json", b"{}", "file"), ("../evil", b"x", "file")],
    "absolute name": [("manifest.json", good_man(), "file"), ("spec.json", b"{}", "file"), ("/tmp/evil", b"x", "file")],
    "escaping symlink": [("manifest.json", good_man(), "file"), ("spec.json", b"{}", "file"), ("l", "../../etc", "sym")],
    "absolute symlink": [("manifest.json", good_man(), "file"), ("spec.json", b"{}", "file"), ("l", "/etc", "sym")],
    "device node": [("manifest.json", good_man(), "file"), ("spec.json", b"{}", "file"), ("d", None, "dev")],
    "runner with a path": [("manifest.json", good_man(runner="../x"), "file"), ("spec.json", b"{}", "file")],
    "unknown runner": [("manifest.json", good_man(runner="nope.py"), "file"), ("spec.json", b"{}", "file")],
    "missing manifest": [("spec.json", b"{}", "file")],
    "not a tar": None,
}
for name, members in cases.items():
    data = tar_bytes(members) if members is not None else b"this is not a tar at all" * 50
    st, out = post("/api/jobs/upload", data, GOOD)
    ok("refused: " + name, st == 400, (st, out[:100]))
ok("no run dir left by any refusal", run_dirs() == before, set(run_dirs()) - set(before))
ok("nothing escaped the run dir", not Path("/tmp/evil").exists())

# bundle that lacks the claimed commit
b2 = tmp / "pkg3.tar"
queue_remote.build_package(specf, {"model": "m1", "label": "bad-commit"}, "test-runner.py", b2)
raw = tarfile.open(b2)
members = []
for m in raw.getmembers():
    f = raw.extractfile(m) if m.isfile() else None
    members.append((m, f.read() if f else None))
mm = [d for m, d in members if m.name == "manifest.json"][0]
mj = json.loads(mm)
for t in mj["trees"]:
    if t["kind"] == "git":
        t["commit"] = "0" * 39 + "1"
bio = io.BytesIO()
with tarfile.open(fileobj=bio, mode="w") as out:
    for m, d in members:
        if m.name == "manifest.json":
            d = json.dumps(mj).encode()
            m.size = len(d)
        out.addfile(m, io.BytesIO(d) if d is not None else None)
st, out = post("/api/jobs/upload", bio.getvalue(), GOOD)
ok("bundle without the claimed commit -> 400", st == 400 and b"not in the bundle" in out, (st, out[:120]))

print("unpack limits and overlay links")
# a small gzip that unpacks past the unpacked cap -> 413
bio = io.BytesIO()
with tarfile.open(fileobj=bio, mode="w:gz") as tf:
    for name, data in (("manifest.json", good_man(label="bomb")), ("spec.json", b"{}"),
                       ("trees/a/overlay/zeros", b"\0" * (3 * 1024 * 1024))):
        ti = tarfile.TarInfo(name); ti.size = len(data); tf.addfile(ti, io.BytesIO(data))
os.environ["QUEUE_UPLOAD_MAX_UNPACKED_MB"] = "2"
before = run_dirs()
st, out = post("/api/jobs/upload", bio.getvalue(), GOOD)
del os.environ["QUEUE_UPLOAD_MAX_UNPACKED_MB"]
ok("packed under the cap but unpacks over it -> 413", st == 413 and len(bio.getvalue()) < 1024 * 1024, (st, out[:100]))
ok("no run dir left by the 413", run_dirs() == before)
# an overlay symlink that stays inside the package but leaves its tree where it lands
man_l = json.dumps({"version": 1, "job": {"model": "m", "label": "link-out"}, "runner": "test-runner.py",
                    "trees": [{"id": "a", "kind": "dir"}], "refs": []}).encode()
st, out = post("/api/jobs/upload", tar_bytes([("manifest.json", man_l, "file"), ("spec.json", b"{}", "file"),
                                              ("trees/a/overlay/l", "../../../x", "sym")]), GOOD)
ok("overlay link that leaves its tree -> 400", st == 400 and b"points out" in out, (st, out[:100]))
# a checkout whose committed symlink points outside: an overlay file under it must not create anything out there
outside = Path(tempfile.mkdtemp(prefix="qupload-outside-"))
lrepo = tmp / "lrepo"
lrepo.mkdir()
git(lrepo, "init", "-q")
(lrepo / "d").symlink_to(str(outside))
git(lrepo, "add", "-A")
git(lrepo, "commit", "-qm", "link")
lspec = tmp / "lspec.json"
lspec.write_text(json.dumps({"baseline_from": str(lrepo)}))
lpkg = tmp / "lpkg.tar"
queue_remote.build_package(lspec, {"model": "m1", "label": "via-link"}, "test-runner.py", lpkg)
bio = io.BytesIO()
with tarfile.open(lpkg) as src, tarfile.open(fileobj=bio, mode="w") as out_tf:
    for m in src.getmembers():
        out_tf.addfile(m, src.extractfile(m) if m.isfile() else None)
    ti = tarfile.TarInfo("trees/t0/overlay/d/x/f"); ti.size = 1; out_tf.addfile(ti, io.BytesIO(b"z"))
before = run_dirs()
st, out = post("/api/jobs/upload", bio.getvalue(), GOOD)
ok("overlay through a checkout link -> 400", st == 400 and b"through a link" in out, (st, out[:100]))
ok("nothing created outside through the link", list(outside.iterdir()) == [], list(outside.iterdir()))
ok("no run dir left by the link refusal", run_dirs() == before)

print("mini-submit remote path")
import types  # noqa: E402
from importlib.machinery import SourceFileLoader  # noqa: E402
os.environ["QUEUE_API"] = BASE
ms_spec = importlib.util.spec_from_loader("mini_submit_t", SourceFileLoader("mini_submit_t", str(B / "mini-submit")))
ms = importlib.util.module_from_spec(ms_spec)
ms_spec.loader.exec_module(ms)
specf2 = tmp / "spec2.json"
specf2.write_text(json.dumps({"baseline_from": str(repo), "task": str(task), "notes": "/etc/hosts"}))
os.environ["MINI_RUNNER"] = str(B / "test-runner.py")
ctx = types.SimpleNamespace(spec_path=specf2)
a = types.SimpleNamespace(model="m1", label="ms-remote", dry_run=True)
buf = io.StringIO()
real, sys.stdout = sys.stdout, buf
rc = ms.submit_remote(a, ctx, ["--host", "auto", "--allow-no-verify"])
a.dry_run = False
rc2 = ms.submit_remote(a, ctx, ["--host", "auto", "--allow-no-verify"])
rc3 = ms.submit_remote(a, ctx, ["--bogus-flag"])
sys.stdout = real
out = buf.getvalue()
ok("dry-run uploads nothing", rc == 0 and "DRY-RUN" in out)
ok("submit_remote uploads and prints a job id", rc2 == 0 and "job_id: " in out, out[-300:])
ok("an unhandled path key is warned about", "'notes'" in out)
ok("an unsupported flag fails the submit", rc3 == 1 and "not supported" in out, out[-200:])
with api.q._Locked() as lock:
    msj = next((j for j in lock.load()["jobs"] if j.get("label") == "ms-remote"), None)
ok("remote submit keeps the local defaults (host auto, task kind coding)",
   msj is not None and msj.get("task_kind") == "coding" and msj.get("host_pref") == "auto", msj)

print("resolve route")
def post_resolve(jid_):
    return post("/api/jobs/%s/resolve" % jid_, b"", GOOD)
st, out = post("/api/jobs/%s/resolve" % jid, b"", {})
ok("resolve without token -> 401", st == 401, st)
st, out = post_resolve(jid)
with api.q._Locked() as lock:
    stays = any(j["id"] == jid for j in lock.load()["jobs"])
ok("resolve of a pending job -> 409 and the job stays", st == 409 and stays, (st, out[:100]))
with api.q._Locked() as lock:
    s_ = lock.load()
    for j in s_["jobs"]:
        if j["id"] == jid:
            j["status"] = "failed"
    lock.save(s_)
st, out = post_resolve(jid)
with api.q._Locked() as lock:
    gone = all(j["id"] != jid for j in lock.load()["jobs"])
ok("resolve of a failed job -> 200 and the row is gone", st == 200 and gone, (st, out[:100]))
st, out = post_resolve(jid)
ok("resolve again -> 404 (job not found)", st == 404 and b"job not found" in out, (st, out[:100]))

print("spec path keys (1c)")
pk = tmp / "pk"
(pk / "plain").mkdir(parents=True)
(pk / "plain" / "tool.py").write_text("print(1)\n")
(pk / "plain" / "sub").mkdir()
(pk / "plain" / "sub" / "t2.py").write_text("print(2)\n")
(pk / "graph.json").write_text("{}")
(pk / "deps.lock").write_text("pkg==1\n")
(pk / "replay").mkdir()
(pk / "replay" / "s1").mkdir()
(pk / "replay" / "s1" / "f.txt").write_text("r\n")
outside = pk / "outside-tool.py"
outside.write_text("x\n")
pspec = {"baseline_from": str(repo), "task": str(task), "graph": str(pk / "graph.json"),
         "replay": {"refs": str(pk / "replay"), "through": 1}, "remote": {"python": "3.12", "deps_lock": str(pk / "deps.lock")},
         "auto_repo": str(repo), "test_file": "test_new.py",
         "test_cmd": "python3 %s/test_new.py && python3 %s/app.py2" % (repo, repo),
         "suite_cmd": "cd sub && python3 %s/app.py --x=%s/keep.txt ./local.py ../rel.py" % (repo, repo),
         "mutation_gate": {"cmd": "mut --work . --pair {file}=%s/app.py --test-cmd {test_cmd}" % repo},
         "steps": [{"task": str(task), "test_cmd": "python3 %s/test_new.py" % repo, "test_file": "test_new.py"}]}
pspecf = tmp / "pspec.json"
pspecf.write_text(json.dumps(pspec))
pman = queue_remote.build_package(pspecf, {"model": "m1", "label": "pk-roundtrip"}, "test-runner.py", tmp / "pkpkg.tar")
ok("every handled key becomes a tree ref", sorted(".".join(map(str, r["path"])) for r in pman["refs"]) ==
   sorted(["baseline_from", "task", "graph", "replay.refs", "remote.deps_lock", "steps.0.task"]), pman["refs"])
ok("commands inside the baseline become cmd_refs", {".".join(map(str, c["path"])) for c in pman["cmd_refs"]} ==
   {"test_cmd", "suite_cmd", "mutation_gate.cmd", "steps.0.test_cmd"}, pman.get("cmd_refs"))
ok("auto_repo is not warned about", queue_remote.unhandled_path_keys(pspec) == [])
pres = queue_remote.upload(BASE, GOOD, tmp / "pkpkg.tar")
with api.q._Locked() as lock:
    pjob = next((j for j in lock.load()["jobs"] if j["id"] == pres.get("id")), None)
psp = json.loads(Path(pjob["task_file"]).read_text())
pbase = Path(psp["baseline_from"])
ok("graph file shipped and rewritten", Path(psp["graph"]).read_text() == "{}" and str(psp["graph"]).startswith(str(root)), psp["graph"])
ok("replay.refs dir shipped and rewritten", (Path(psp["replay"]["refs"]) / "s1" / "f.txt").read_text() == "r\n", psp["replay"])
ok("replay.through left alone", psp["replay"]["through"] == 1)
ok("remote.deps_lock shipped and rewritten", Path(psp["remote"]["deps_lock"]).read_text() == "pkg==1\n", psp["remote"])
ok("auto_repo stays as written (client only)", psp["auto_repo"] == str(repo))
ok("test_cmd path points into the unpacked baseline", psp["test_cmd"] == "python3 %s/test_new.py && python3 %s/app.py2" % (pbase, pbase),
   psp["test_cmd"])
ok("suite_cmd: relative parts untouched, absolute ones rewritten", psp["suite_cmd"] ==
   "cd sub && python3 %s/app.py --x=%s/keep.txt ./local.py ../rel.py" % (pbase, pbase), psp["suite_cmd"])
ok("mutation_gate.cmd {file}=path rewritten, placeholders kept", psp["mutation_gate"]["cmd"] ==
   "mut --work . --pair {file}=%s/app.py --test-cmd {test_cmd}" % pbase, psp["mutation_gate"]["cmd"])
ok("step test_cmd rewritten", psp["steps"][0]["test_cmd"] == "python3 %s/test_new.py" % pbase, psp["steps"][0])
ok("rewritten command paths exist", (pbase / "test_new.py").is_file())
# a path on this machine outside every shipped tree is refused with a message, nothing is packed
bad = dict(pspec, suite_cmd="python3 %s" % outside)
badf = tmp / "badspec.json"
badf.write_text(json.dumps(bad))
try:
    queue_remote.build_package(badf, {"model": "m1", "label": "pk-bad"}, "test-runner.py", tmp / "bad.tar")
    ok("a command path outside every shipped tree is refused", False)
except queue_remote.RemoteError as e:
    ok("a command path outside every shipped tree is refused", "suite_cmd" in str(e) and str(outside) in str(e), str(e))
ok("no package written for the refusal", not (tmp / "bad.tar").exists())
try:
    queue_remote.check_paths(bad, tmp)
    ok("check_paths (dry-run) refuses the same spec", False)
except queue_remote.RemoteError:
    ok("check_paths (dry-run) refuses the same spec", True)
ok("system paths and relative commands pass", queue_remote.check_paths(
    {"baseline_from": str(repo), "test_cmd": "/usr/bin/env python3 -m pytest ./x ../y"}, tmp) is None)
before = run_dirs()
cm = lambda refs: tar_bytes([("manifest.json", json.dumps({"version": 1, "job": {"model": "m", "label": "bad-cmd"},
    "runner": "test-runner.py", "trees": [{"id": "t0", "kind": "dir"}], "refs": [], "cmd_refs": refs}).encode(), "file"),
    ("spec.json", json.dumps({"test_cmd": "python3 /Users/x/a.py"}).encode(), "file")])
for name, refs in {"rel escapes the tree": [{"path": ["test_cmd"], "from": "/Users/x/a.py", "tree": "t0", "rel": "../x"}],
                   "rel with a shell character": [{"path": ["test_cmd"], "from": "/Users/x/a.py", "tree": "t0", "rel": "a;b"}],
                   "token not in the string": [{"path": ["test_cmd"], "from": "/Users/y/zz.py", "tree": "t0", "rel": "a.py"}],
                   "unknown tree": [{"path": ["test_cmd"], "from": "/Users/x/a.py", "tree": "t9", "rel": "a.py"}],
                   "path to nothing": [{"path": ["nope"], "from": "/Users/x/a.py", "tree": "t0", "rel": "a.py"}],
                   "relative token": [{"path": ["test_cmd"], "from": "a.py", "tree": "t0", "rel": "a.py"}]}.items():
    st, out = post("/api/jobs/upload", cm(refs), GOOD)
    ok("cmd_refs refused: " + name, st == 400, (st, out[:100]))
ok("no run dir left by cmd_refs refusals", run_dirs() == before)
ms_bad = tmp / "ms-bad.json"
ms_bad.write_text(json.dumps(bad))
a = types.SimpleNamespace(model="m1", label="ms-bad", dry_run=True)
buf = io.StringIO()
real, sys.stdout = sys.stdout, buf
rc = ms.submit_remote(a, types.SimpleNamespace(spec_path=ms_bad), [])
sys.stdout = real
ok("mini-submit refuses (even in dry-run) with the message", rc == 1 and "suite_cmd" in buf.getvalue(), buf.getvalue()[-200:])

print("run scope + runner_args (mini-finish remote)")
rs_dir = tmp / "rs-fetch"
(Path(pjob["cwd"]) / "edited.py").write_text("y = 2\n")
queue_remote.fetch_result(BASE, GOOD, pjob["id"], rs_dir, scope="run")
ok("run scope: the run dir (work/, spec.json, trees/) comes back", (rs_dir / "spec.json").is_file() and (rs_dir / "work" / "edited.py").is_file()
   and (rs_dir / "trees").is_dir() and (rs_dir / "_queue" / "job.json").is_file(), sorted(p.name for p in rs_dir.iterdir()))
ok("run scope: git hooks of the checkout are not shipped", not list(rs_dir.rglob("hooks")))
try:
    queue_remote.fetch_result(BASE, {}, pjob["id"], tmp / "rs-noauth", scope="run")
    ok("run scope needs the token", False)
except queue_remote.RemoteError:
    ok("run scope needs the token", True)
try:
    queue_remote.fetch_result(BASE, GOOD, "0123456789ab", tmp / "rs-unknown", scope="run")
    ok("run scope: unknown job -> refused", False)
except queue_remote.RemoteError:
    ok("run scope: unknown job -> refused", True)
print("run scope: the cap, and a _queue dir of the run's own")
_run_d = Path(pjob["cwd"]).parent
(_run_d / "_queue").mkdir(exist_ok=True)
(_run_d / "_queue" / "job.json").write_text('{"id": "planted"}')
_big = Path(pjob["cwd"]) / "big.bin"
_big.write_bytes(b"x" * (2 * 1024 * 1024))
_big_arc = Path(pjob["cwd"]).name + "/big.bin"
os.environ["QUEUE_UPLOAD_RUN_MAX_MB"] = "1"
try:
    _cap_dir = queue_remote.fetch_result(BASE, GOOD, pjob["id"], tmp / "rs-cap", scope="run")
finally:
    del os.environ["QUEUE_UPLOAD_RUN_MAX_MB"]
_sk = (_cap_dir / "_queue" / "skipped.txt").read_text() if (_cap_dir / "_queue" / "skipped.txt").is_file() else ""
ok("run scope over upload.run_max_mb: the file left out is listed in _queue/skipped.txt", _big_arc in _sk
   and not (_cap_dir / _big_arc).exists() and _sk.startswith("# "), _sk[:300])
ok("run scope: the run's own _queue dir is not shipped (job.json is the queue's record)",
   json.loads((_cap_dir / "_queue" / "job.json").read_text()).get("id") == pjob["id"])
_tgz = tmp / "rs-cap-direct.tgz"
queue_upload.build_run_tar(pjob, _tgz, queue_upload.work_root())
with tarfile.open(_tgz) as _tf:
    _names = _tf.getnames()
ok("run scope under the cap: nothing skipped, one _queue/job.json", "_queue/skipped.txt" not in _names
   and _names.count("_queue/job.json") == 1 and _big_arc in _names, [n for n in _names if n.startswith("_queue")])
_big.unlink()
shutil.rmtree(_run_d / "_queue")
ra_dir = tmp / "ra"
ra_dir.mkdir()
(ra_dir / "cand.py").write_text("x = 1\n")
ra_spec = tmp / "ra-spec.json"
ra_spec.write_text(json.dumps({"baseline_from": str(repo)}))
ra_items = [["--check-only"], ["--mutation"], ["--try-test", {"tree": "cand", "rel": "cand.py"}],
            ["--try-fix", {"tree": "cand", "rel": "cand.py", "prefix": "cand.py="}]]
queue_remote.build_package(ra_spec, {"model": "m1", "label": "ra-ok"}, "test-runner.py", tmp / "ra.tar",
                           extra_trees={"cand": str(ra_dir)}, runner_args=ra_items)
rres = queue_remote.upload(BASE, GOOD, tmp / "ra.tar")
with api.q._Locked() as lock:
    rjob = next((j for j in lock.load()["jobs"] if j["id"] == rres.get("id")), None)
ra_out = (rjob or {}).get("runner_args") or []
ok("runner_args stored: flags kept, tree paths resolved inside the unpacked run", ra_out[:2] == ["--check-only", "--mutation"]
   and ra_out[2] == "--try-test" and Path(ra_out[3]).read_text() == "x = 1\n" and ra_out[4] == "--try-fix"
   and ra_out[5].startswith("cand.py=") and Path(ra_out[5][len("cand.py="):]).is_file(), ra_out)
for tag, bad_items in (("unknown flag", [["--model", "x"]]), ("flag with a path leaving its tree", [["--try-test", {"tree": "cand", "rel": "../x"}]]),
                       ("unknown tree", [["--try-test", {"tree": "nope", "rel": "a"}]]),
                       ("bad prefix", [["--try-fix", {"tree": "cand", "rel": "cand.py", "prefix": "a b;rm="}]]),
                       ("prefix that reads as a flag", [["--try-fix", {"tree": "cand", "rel": "cand.py", "prefix": "--cwd="}]]),
                       ("prefix that reads as a path", [["--try-fix", {"tree": "cand", "rel": "cand.py", "prefix": "/etc/x="}]])):
    # a label of its own per case: a duplicate label would be refused for that reason alone and hide the case
    queue_remote.build_package(ra_spec, {"model": "m1", "label": "ra-bad-" + re.sub(r"\W+", "-", tag)}, "test-runner.py", tmp / "rabad.tar",
                               extra_trees={"cand": str(ra_dir)}, runner_args=bad_items)
    try:
        queue_remote.upload(BASE, GOOD, tmp / "rabad.tar")
        ok("runner_args refused: " + tag, False)
    except queue_remote.RemoteError as e:
        ok("runner_args refused: " + tag, "400" in str(e) or "refus" in str(e).lower() or "bad" in str(e).lower(), str(e))
print("client flags")
ok("known flags map", queue_remote.parse_enqueue_flags(["--host", "unraid", "--allow-no-verify", "--task-kind=coding"])
   == {"host": "unraid", "task_kind": "coding"})
try:
    queue_remote.parse_enqueue_flags(["--worktree-from", "x"])
    ok("unknown flag raises", False)
except queue_remote.RemoteError:
    ok("unknown flag raises", True)
ok("unhandled path keys are listed", queue_remote.unhandled_path_keys({"baseline_from": "/x", "notes": "/etc/hosts"}) == ["notes"])

print("%d failed" % len(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
