"""queue_upload -- server side of submitting a job (and reading its result) from a machine that shares no disk
with the queue. Used by ollama-queue-api.py behind its token gate.

An upload is ONE tar stream (the "package"):
  manifest.json                 {"version": 1, "job": {...fields _enqueue takes...}, "runner": "<name>",
                                 "trees": [{"id", "kind": "git"|"dir", "commit", "deleted": [...]}],
                                 "refs": [{"path": [json path in the spec], "tree": id, "rel": relative path}],
                                 "cmd_refs": [{"path": [json path of a command string], "from": "<absolute token>",
                                               "tree": id, "rel": relative path}]}   (optional: an absolute path
                                 inside a command string is swapped for the unpacked path)
  spec.json                     the spec / task text, verbatim from the client
  trees/<id>/bundle             git kind: `git bundle` of the baseline commit
  trees/<id>/overlay/...        files to lay over the checkout (uncommitted + untracked-not-ignored), or the whole
                                tree for the dir kind

Everything in the package is untrusted: the body is streamed to disk under a size cap, the tar is extracted with
tarfile's "data" filter after its names are checked, the bundle is cloned with hooks disabled, and nothing from the
upload is ever executed. The runner is chosen BY NAME inside the queue's own bin directory.
"""
import json
import os
import re
import shutil
import subprocess
import tarfile
import time
import uuid
from pathlib import Path

DEFAULT_MAX_MB = 512
DEFAULT_MAX_UNPACKED_MB = 2048   # what the tar may expand to (a small gzip can hold a huge tree)
RESULT_MAX_MB = 128
LOG_TAIL_BYTES = 256 * 1024
MAX_MEMBERS = 200_000
_TREE_ID = re.compile(r"^[A-Za-z][A-Za-z0-9]{0,15}$")
_RUNNER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_JOB_FIELDS = ("model", "label", "host", "task_kind", "verify", "manual_tools", "api", "num_ctx", "max_iters",
               "temperature", "chat_timeout", "allow_duplicate_label")
_SKIP_RESULT_DIRS = {".git", "node_modules", "__pycache__", ".venv"}
# Runner flags a package may ask for (mini-finish's remote check): flag -> takes a path value. Anything else is refused.
RUNNER_FLAGS = {"--check-only": False, "--mutation": False, "--draft-check": False, "--validate-spec": False,
                "--try-test": True, "--try-fix": True, "--try-diagnosis": True}
# a "file=" prefix (--try-fix): never "-" or "/" first, so it cannot read as another runner flag or an absolute path
_ARG_PREFIX = re.compile(r"^[\w.+@][\w./+@-]{0,199}=$")


class UploadError(Exception):
    def __init__(self, status, msg):
        super().__init__(msg)
        self.status = status
        self.msg = msg


def max_bytes(queue_config):
    return max(1, queue_config.get_int("upload.max_mb", DEFAULT_MAX_MB)) * 1024 * 1024


def max_unpacked_bytes(queue_config):
    return max(1, queue_config.get_int("upload.max_unpacked_mb", DEFAULT_MAX_UNPACKED_MB)) * 1024 * 1024


def run_max_bytes(queue_config):
    """Cap on the files of a run-scope result (mini-finish's mirror): upload.run_max_mb / QUEUE_UPLOAD_RUN_MAX_MB."""
    return max(1, queue_config.get_int("upload.run_max_mb", RESULT_MAX_MB)) * 1024 * 1024


def work_root(state_home=None):
    """Where unpacked runs live: $MINI_SUBMIT_HOME/mini-work, else ~/.ollama-dispatch/mini-work (the same place a local
    mini-submit uses, which the container mounts as a volume)."""
    home = state_home or os.environ.get("MINI_SUBMIT_HOME") or (Path.home() / ".ollama-dispatch")
    return Path(home).expanduser() / "mini-work"


def stream_to_file(rfile, length, dest, cap):
    """Copy exactly `length` bytes from rfile to dest in 1 MiB chunks. Refuses (413) before reading when the declared
    length is over cap, and again if the stream turns out longer than declared."""
    if length < 0:
        raise UploadError(400, "bad Content-Length")
    if length > cap:
        raise UploadError(413, "upload larger than the %d MB cap" % (cap // (1024 * 1024)))
    left = length
    with open(dest, "wb") as out:
        while left > 0:
            chunk = rfile.read(min(1024 * 1024, left))
            if not chunk:
                raise UploadError(400, "upload ended early")
            out.write(chunk)
            left -= len(chunk)


def _bad_name(name):
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        return True
    return any(p in ("", "..") for p in name.split("/") if p != ".") if name != "." else False


def safe_extract(tar_path, dest, max_members=MAX_MEMBERS, max_total=DEFAULT_MAX_UNPACKED_MB * 1024 * 1024):
    """Extract a package into dest. Refuses absolute names, `..`, devices/fifos, links that point out of dest
    (tarfile's "data" filter does the last part; names are checked first because the filter only strips a leading /),
    and a package whose files add up to more than max_total bytes."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        with tarfile.open(tar_path, "r:*") as tf:
            members = []
            total = 0
            for i, m in enumerate(tf):
                if i >= max_members:
                    raise UploadError(400, "too many entries in the upload")
                total += max(0, m.size)
                if total > max_total:
                    raise UploadError(413, "upload unpacks to more than %d MB" % (max_total // (1024 * 1024)))
                if _bad_name(m.name):
                    raise UploadError(400, "unsafe path in the upload: %r" % m.name[:80])
                if not (m.isfile() or m.isdir() or m.issym()):
                    raise UploadError(400, "unsupported entry type in the upload: %r" % m.name[:80])
                if m.issym() and (m.linkname.startswith("/") or _escapes(m.name, m.linkname)):
                    raise UploadError(400, "link leaves the upload: %r" % m.name[:80])
                members.append(m)
            try:
                tf.extractall(dest, members=members, filter="data")
            except (tarfile.FilterError, OSError) as e:
                raise UploadError(400, "refused entry: %s" % type(e).__name__)
    except tarfile.TarError:
        raise UploadError(400, "not a tar archive")


def _escapes(member, linkname):
    depth = 0
    for part in os.path.dirname(member).split("/"):
        if part not in ("", "."):
            depth += 1
    for part in linkname.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            depth -= 1
            if depth < 0:
                return True
        else:
            depth += 1
    return False


def _git(args, cwd=None, timeout=300):
    # No system or user git config during intake (an LFS smudge filter or similar there would run on upload content).
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull,
               GIT_LFS_SKIP_SMUDGE="1")
    p = subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-c", "protocol.ext.allow=never",
                        "-c", "protocol.file.allow=user"] + args, cwd=cwd, env=env, capture_output=True, text=True,
                       timeout=timeout)
    return p


def _inside(root, path):
    r = os.path.realpath(root)
    p = os.path.realpath(path)
    return p == r or p.startswith(r + os.sep)


def _rel_ok(rel):
    return isinstance(rel, str) and not _bad_name(rel) and rel != "."


def _make_parent(dest, rel):
    """mkdir dest/<rel's parent> one level at a time, refusing to go through a symlink (the checkout comes from the
    upload, so a directory in it may be a link to anywhere). Nothing is created outside dest."""
    cur = Path(dest)
    for part in rel.parent.parts:
        cur = cur / part
        if cur.is_symlink():
            raise UploadError(400, "overlay path goes through a link: %s" % rel)
        if not cur.exists():
            cur.mkdir()
        elif not cur.is_dir():
            raise UploadError(400, "overlay path goes through a file: %s" % rel)
    return cur


def _copy_overlay(overlay, dest):
    """Lay overlay/ over dest. Every target must resolve inside dest (a checkout may hold a symlink that points out),
    and an overlay symlink may only point inside dest from where it lands."""
    dest_real = os.path.realpath(dest)
    for root, dirs, files in os.walk(overlay):
        for f in files:
            src = Path(root) / f
            rel = src.relative_to(overlay)
            tgt = _make_parent(dest, rel) / rel.name
            if not _inside(dest, tgt.parent):
                raise UploadError(400, "overlay path leaves the tree: %s" % rel)
            if tgt.is_symlink() or tgt.exists():
                if tgt.is_dir() and not tgt.is_symlink():
                    raise UploadError(400, "overlay file replaces a directory: %s" % rel)
                tgt.unlink()
            if src.is_symlink():
                link = os.readlink(src)
                aim = os.path.normpath(os.path.join(os.path.realpath(tgt.parent), link))
                if os.path.isabs(link) or not (aim == dest_real or aim.startswith(dest_real + os.sep)):
                    raise UploadError(400, "overlay link points out of the tree: %s" % rel)
                os.symlink(link, tgt)
            else:
                shutil.copy2(src, tgt)


def build_tree(tree, staged, out_dir):
    """Create out_dir for one tree entry of the manifest from staged/trees/<id>/."""
    tid = tree["id"]
    src = Path(staged) / "trees" / tid
    overlay = src / "overlay"
    kind = tree.get("kind")
    if kind == "git":
        bundle = src / "bundle"
        commit = tree.get("commit")
        if not bundle.is_file() or not isinstance(commit, str) or not _SHA.match(commit):
            raise UploadError(400, "tree %s: git tree needs a bundle and a 40-hex commit" % tid)
        c = _git(["clone", "--quiet", "--no-checkout", str(bundle), str(out_dir)])
        if c.returncode != 0:
            raise UploadError(400, "tree %s: bundle cannot be cloned" % tid)
        if _git(["cat-file", "-e", commit + "^{commit}"], cwd=out_dir).returncode != 0:
            shutil.rmtree(out_dir, ignore_errors=True)
            raise UploadError(400, "tree %s: commit %s is not in the bundle" % (tid, commit[:12]))
        if _git(["checkout", "--quiet", "--detach", commit], cwd=out_dir).returncode != 0:
            raise UploadError(400, "tree %s: cannot check out %s" % (tid, commit[:12]))
        for rel in tree.get("deleted") or []:
            if not _rel_ok(rel):
                raise UploadError(400, "tree %s: bad deleted path" % tid)
            t = Path(out_dir) / rel
            if _inside(out_dir, t.parent) and (t.is_file() or t.is_symlink()):
                t.unlink()
    elif kind == "dir":
        Path(out_dir).mkdir(parents=True)
    else:
        raise UploadError(400, "tree %s: unknown kind" % tid)
    if overlay.is_dir():
        _copy_overlay(overlay, out_dir)


def _set_path(obj, path, value):
    cur = obj
    for k in path[:-1]:
        cur = cur[k]
    cur[path[-1]] = value


def _get_path(obj, path):
    cur = obj
    for k in path:
        cur = cur[k]
    return cur


_CMD_REL = re.compile(r"^[\w./+@:,-]*$")   # what a command-embedded path may hold: no quote, space or shell character


def unpack_package(pkg_path, run_dir, bin_dir, allowed_runners=None, max_unpacked=None):
    """Unpack + validate a package into run_dir. Returns {"fields", "task", "cwd", "runner", "run_dir"}: the job fields
    `_enqueue` takes (model, label, ...), the rewritten spec text, the empty work dir and the runner's server path."""
    run_dir = Path(run_dir)
    staged = run_dir / "staged"
    if max_unpacked is None:
        safe_extract(pkg_path, staged)
    else:
        safe_extract(pkg_path, staged, max_total=max_unpacked)
    try:
        man = json.loads((staged / "manifest.json").read_text())
        spec_text = (staged / "spec.json").read_text()
        spec = json.loads(spec_text)
    except (OSError, ValueError):
        raise UploadError(400, "package needs manifest.json and a spec.json that parses")
    if not isinstance(man, dict) or man.get("version") != 1 or not isinstance(spec, (dict, list)):
        raise UploadError(400, "unsupported package")
    name = man.get("runner")
    if not isinstance(name, str) or not _RUNNER.match(name):
        raise UploadError(400, "runner must be a bare file name")
    runner = Path(bin_dir) / name
    if not runner.is_file():
        raise UploadError(400, "unknown runner %r" % name)
    if allowed_runners is not None and str(runner) not in allowed_runners and str(runner.resolve()) not in allowed_runners:
        raise UploadError(400, "runner %r is not allowed on this queue" % name)
    job = man.get("job")
    if not isinstance(job, dict):
        raise UploadError(400, "manifest.job missing")
    fields = {k: job[k] for k in _JOB_FIELDS if k in job and job[k] is not None}
    if not str(fields.get("model", "")).strip() or not str(fields.get("label", "")).strip():
        raise UploadError(400, "model and label are required")
    trees = man.get("trees") or []
    if not isinstance(trees, list) or len(trees) > 64:
        raise UploadError(400, "bad trees")
    root = run_dir / "trees"
    root.mkdir()
    ids = set()
    for t in trees:
        if not isinstance(t, dict) or not isinstance(t.get("id"), str) or not _TREE_ID.match(t["id"]) or t["id"] in ids:
            raise UploadError(400, "bad tree id")
        ids.add(t["id"])
        build_tree(t, staged, root / t["id"])
    for ref in man.get("refs") or []:
        try:
            path, tid, rel = ref["path"], ref["tree"], ref.get("rel") or ""
            assert isinstance(path, list) and path and tid in ids and isinstance(rel, str)
            if rel and not _rel_ok(rel):
                raise UploadError(400, "bad ref path")
            target = root / tid / rel if rel else root / tid
            if not _inside(root / tid, target):
                raise UploadError(400, "ref leaves its tree")
            _set_path(spec, path, str(target))
        except UploadError:
            raise
        except (KeyError, TypeError, IndexError, AssertionError):
            raise UploadError(400, "bad ref")
    for ref in man.get("cmd_refs") or []:
        try:
            path, tid, rel, tok = ref["path"], ref["tree"], ref.get("rel") or "", ref["from"]
            assert isinstance(path, list) and path and tid in ids and isinstance(rel, str)
            assert isinstance(tok, str) and tok.startswith(("/", "~/")) and len(tok) < 4096
            if rel and (not _rel_ok(rel) or not _CMD_REL.match(rel)):
                raise UploadError(400, "bad cmd ref path")
            target = root / tid / rel if rel else root / tid
            if not _inside(root / tid, target) or not _CMD_REL.match(str(target)):
                raise UploadError(400, "cmd ref leaves its tree")
            cur = _get_path(spec, path)
            assert isinstance(cur, str) and tok in cur
            _set_path(spec, path, re.sub(r"(?<![\w./~-])" + re.escape(tok) + r"(?![\w./+@:,-])",
                                         lambda _m: str(target), cur))
        except UploadError:
            raise
        except (KeyError, TypeError, IndexError, AssertionError):
            raise UploadError(400, "bad cmd ref")
    runner_args = []
    ras = man.get("runner_args") or []
    if not isinstance(ras, list) or len(ras) > 64:
        raise UploadError(400, "bad runner_args")
    for item in ras:
        try:
            assert isinstance(item, list) and 1 <= len(item) <= 2 and item[0] in RUNNER_FLAGS
            runner_args.append(item[0])
            if RUNNER_FLAGS[item[0]]:
                v = item[1]
                tid, rel, pre = v["tree"], v.get("rel") or "", v.get("prefix") or ""
                assert tid in ids and isinstance(rel, str) and isinstance(pre, str) and (not pre or _ARG_PREFIX.match(pre))
                if not rel or not _rel_ok(rel):
                    raise UploadError(400, "bad runner_args path")
                target = root / tid / rel
                if not _inside(root / tid, target):
                    raise UploadError(400, "runner_args path leaves its tree")
                runner_args.append(pre + str(target))
            else:
                assert len(item) == 1
        except UploadError:
            raise
        except (KeyError, TypeError, IndexError, AssertionError):
            raise UploadError(400, "bad runner_args")
    work = run_dir / "work"
    work.mkdir()
    (run_dir / "spec.json").write_text(json.dumps(spec, indent=1))
    shutil.rmtree(staged, ignore_errors=True)
    return {"fields": fields, "task": json.dumps(spec, indent=1), "cwd": str(work), "runner": str(runner),
            "run_dir": str(run_dir), "runner_args": runner_args}


def new_run_dir(root, label):
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", str(label))[:60] or "job"
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    d = root / ("%s-%s-%s" % (safe, time.strftime("%Y%m%d%H%M%S"), uuid.uuid4().hex[:6]))
    d.mkdir()
    return d


# ---- results ----------------------------------------------------------------------------------------------------

def _tail(path, n):
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - n))
            return f.read()
    except OSError:
        return None


def build_result(job, dest_tar, state_dir, mini_root, cap=RESULT_MAX_MB * 1024 * 1024):
    """Write a tar.gz of a finished job's result to dest_tar: job.json, done.json, log-tail.txt and the work dir
    (without .git / node_modules / caches; symlinks are skipped) when it sits under the mini-work root. Total size
    capped; returns the list of member names."""
    names = []
    total = 0
    state_dir = Path(state_dir)
    with tarfile.open(dest_tar, "w:gz") as tf:
        def add_bytes(name, data):
            nonlocal total
            if total + len(data) > cap:
                return
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = int(time.time())
            import io
            tf.addfile(ti, io.BytesIO(data))
            total += len(data)
            names.append(name)

        add_bytes("job.json", json.dumps(job, indent=1, default=str).encode())
        jid = str(job.get("id") or "")
        if re.fullmatch(r"[0-9a-f]{6,64}", jid):
            done = state_dir / "ollama-queue-logs" / (jid + ".done.json")
            if done.is_file() and done.stat().st_size <= 4 * 1024 * 1024:
                add_bytes("done.json", done.read_bytes())
        lp = job.get("log_path")
        if lp and _inside(state_dir, lp):
            t = _tail(lp, LOG_TAIL_BYTES)
            if t is not None:
                add_bytes("log-tail.txt", t)
        cwd = job.get("cwd")
        if cwd and Path(cwd).is_dir() and _inside(mini_root, cwd) and os.path.realpath(cwd) != os.path.realpath(mini_root):
            base = Path(cwd)
            for root, dirs, files in os.walk(base):
                dirs[:] = [d for d in dirs if d not in _SKIP_RESULT_DIRS]
                for f in files:
                    p = Path(root) / f
                    if p.is_symlink() or not p.is_file():
                        continue
                    sz = p.stat().st_size
                    if total + sz > cap:
                        continue
                    tf.add(p, arcname="work/" + str(p.relative_to(base)), recursive=False)
                    total += sz
                    names.append("work/" + str(p.relative_to(base)))
    return names


def build_run_tar(job, dest_tar, mini_root, state_dir=None, cap=RESULT_MAX_MB * 1024 * 1024):
    """Write a tar.gz of the job's whole run dir (the job cwd's parent: work/, spec files, step-refs/, REVIEW*.md,
    finish/) to dest_tar, for mini-finish's mirror. Only when the run dir sits under the mini-work root and is not the
    root itself. .git is included (mini-finish runs git show HEAD:<f>) minus its hooks; symlinks are skipped; node_modules
    and caches are left out. Total size capped: files that do not fit are listed (size, path) in _queue/skipped.txt, so
    the client knows its copy is partial. A _queue dir of the run's own is never shipped (that name is the queue's record).
    Returns the member names, or None when the run dir is not eligible."""
    cwd = job.get("cwd")
    if not cwd or not Path(cwd).is_dir():
        return None
    run = Path(os.path.realpath(Path(cwd).parent))
    if not _inside(mini_root, run) or run == Path(os.path.realpath(mini_root)):
        return None
    skip = {"node_modules", "__pycache__", ".venv"}
    names, total, skipped = [], 0, []
    with tarfile.open(dest_tar, "w:gz") as tf:
        # the queue's own record of the job (for the client's refusal checks); the names cannot clash: a run dir holds no job.json
        import io
        jid = str(job.get("id") or "")
        recs = [("_queue/job.json", json.dumps(job, indent=1, default=str).encode())]
        done = Path(state_dir) / "ollama-queue-logs" / (jid + ".done.json") if state_dir and re.fullmatch(r"[0-9a-f]{6,64}", jid) else None
        if done is not None and done.is_file() and done.stat().st_size <= 4 * 1024 * 1024:
            recs.append(("_queue/done.json", done.read_bytes()))
        lp = job.get("log_path")
        if state_dir and lp and _inside(state_dir, lp):
            t = _tail(lp, LOG_TAIL_BYTES)
            if t is not None:
                recs.append(("_queue/log-tail.txt", t))
        for name, data in recs:
            ti = tarfile.TarInfo(name)
            ti.size = len(data)
            ti.mtime = int(time.time())
            tf.addfile(ti, io.BytesIO(data))
            names.append(name)
        for root, dirs, files in os.walk(run):
            rel_root = Path(root).relative_to(run)
            dirs[:] = [d for d in dirs if d not in skip and not (d == "hooks" and rel_root.name == ".git")
                       and not (d == "_queue" and rel_root == Path("."))]
            for f in files:
                p = Path(root) / f
                if p.is_symlink() or not p.is_file():
                    continue
                sz = p.stat().st_size
                arc = str(p.relative_to(run))
                if total + sz > cap:
                    skipped.append((sz, arc))
                    continue
                tf.add(p, arcname=arc, recursive=False)
                total += sz
                names.append(arc)
        if skipped:
            lines = ["# %d file(s), %d bytes, left out: over the run result cap of %d MB" % (
                len(skipped), sum(sz for sz, _ in skipped), cap // (1024 * 1024))]
            lines += ["%d\t%s" % (sz, arc) for sz, arc in skipped[:5000]]
            if len(skipped) > 5000:
                lines.append("# ... %d more" % (len(skipped) - 5000))
            data = ("\n".join(lines) + "\n").encode("utf-8", "replace")
            ti = tarfile.TarInfo("_queue/skipped.txt")
            ti.size = len(data)
            ti.mtime = int(time.time())
            tf.addfile(ti, io.BytesIO(data))
            names.append("_queue/skipped.txt")
    return names
