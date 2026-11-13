"""queue_remote -- submit a job to a queue on another machine (the Docker queue on the NAS) and fetch its result.

Used by mini-submit when queue_config.queue_is_remote(). The queue shares no disk with this machine, so the job's
inputs are packed into ONE tar (format in the server's queue_upload.py): a spec, plus per tree (a baseline_from dir,
the ref dir, an absolute task file) either a `git bundle` + an overlay of uncommitted/untracked files (git trees) or the
files themselves (plain dirs). The server rewrites the spec's path values to its own unpacked copies.

Handled path-valued spec keys (top level and inside "steps"): baseline_from, ref, task, graph, replay.refs, and
remote.deps_lock (shipped as trees, the server rewrites the value). Command keys (test_cmd, suite_cmd, typecheck_cmd,
draft_accept_cmd, mutation_gate.cmd) may name absolute paths: one inside a shipped tree is rewritten by the server to the
unpacked copy (manifest "cmd_refs"); one that points at this machine and lies in NO shipped tree makes build_package /
check_paths raise RemoteError (mini-submit refuses with that message). Relative paths in test_file, files, locate, ... are
relative to the baseline and need nothing. `auto_repo` is read only by mini-auto on this machine. Any other key holding a
path is sent as written and will not resolve on the server; `unhandled_path_keys(spec)` lists the likely ones so the caller
can warn.
Never prints or logs the token."""
import http.client
import json
import os
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", ".DS_Store"}
PATH_KEYS = ("baseline_from", "ref", "task")
# every key whose value is a path to ship, as a path of dict keys (the first three are the original PATH_KEYS)
PATH_KEY_PATHS = (("baseline_from",), ("ref",), ("task",), ("graph",), ("replay", "refs"), ("remote", "deps_lock"))
CMD_KEY_PATHS = (("test_cmd",), ("suite_cmd",), ("typecheck_cmd",), ("draft_accept_cmd",), ("mutation_gate", "cmd"))
CLIENT_ONLY_KEYS = {"auto_repo"}   # read by mini-auto on this machine only; the runner never opens it
_LOCAL_PREFIXES = ("/Users/", "/private/", "/Volumes/", "/opt/homebrew/", "/tmp/", "/var/folders/", "/home/")
_CMD_PATH = re.compile(r"(?<![\w./~-])(~?/[\w./+@:,-]+)")
_SAFE_REL = re.compile(r"^[\w./+@:,-]*$")
# a test-draft job's draft_accept_cmd names this machine's interpreter, mini-testdraft and the original spec. For the queue it is
# rewritten to the queue host's own copy of mini-testdraft (in the image, under $HOME/bin) and three placeholders the server swaps
# for unpacked trees (the cmd_refs route): the spec (with its task file) as a small tree of its own, the ref dir, the baseline.
DRAFT_CMD_HEAD = 'python3 "$HOME/bin/mini-testdraft" check'
DRAFT_PH = {"spec": "/__qdraft_spec__/spec.json", "ref": "/__qdraft_ref__", "base": "/__qdraft_base__"}
DRAFT_MIN_MUTATION = 0.7   # the gate's floor (mini-testdraft's default); a command asking for less is refused, never rewritten
_DRAFT_SAFE = re.compile(r"^[\w][\w./+@-]*$")
JOB_FLAGS = {"--host": "host", "--task-kind": "task_kind", "--verify": "verify", "--api": "api",
             "--num-ctx": "num_ctx", "--max-iters": "max_iters", "--temperature": "temperature",
             "--chat-timeout": "chat_timeout"}
JOB_SWITCHES = {"--manual-tools": "manual_tools", "--allow-duplicate-label": "allow_duplicate_label"}
IGNORED_SWITCHES = {"--allow-no-verify", "--allow-unisolated"}   # client-side enqueue gates; the server path has none


class RemoteError(Exception):
    pass


def _git(args, cwd, text=True, check=True):
    p = subprocess.run(["git"] + args, cwd=cwd, capture_output=True, text=text)
    if check and p.returncode != 0:
        raise RemoteError("git %s failed: %s" % (" ".join(args[:2]), (p.stderr or "")[-200:] if text else "see git"))
    return p


def _is_git_root(d):
    p = _git(["rev-parse", "--show-toplevel"], d, check=False)
    return p.returncode == 0 and os.path.realpath(p.stdout.strip()) == os.path.realpath(d)


def _nul_list(args, cwd):
    out = _git(args, cwd, text=False).stdout
    return [x.decode("utf-8", "surrogateescape") for x in out.split(b"\0") if x]


def _copy_into(src_root, rels, dest_root):
    for rel in rels:
        s = Path(src_root) / rel
        d = Path(dest_root) / rel
        if s.is_dir() and not s.is_symlink():
            continue
        d.parent.mkdir(parents=True, exist_ok=True)
        if s.is_symlink():
            os.symlink(os.readlink(s), d)
        elif s.is_file():
            shutil.copy2(s, d)


def stage_tree(tid, path, stage):
    """Stage one tree under stage/trees/<tid>/ and return its manifest entry."""
    path = os.path.realpath(os.path.expanduser(path))
    out = Path(stage) / "trees" / tid
    overlay = out / "overlay"
    overlay.mkdir(parents=True)
    if _is_git_root(path):
        commit = _git(["rev-parse", "HEAD"], path).stdout.strip()
        _git(["bundle", "create", str(out / "bundle"), "HEAD"], path)
        changed = _nul_list(["diff", "--name-only", "--no-renames", "-z", "HEAD"], path)
        untracked = _nul_list(["ls-files", "-o", "--exclude-standard", "-z"], path)
        present = [r for r in changed if os.path.lexists(os.path.join(path, r))]
        deleted = [r for r in changed if not os.path.lexists(os.path.join(path, r))]
        _copy_into(path, present + untracked, overlay)
        return {"id": tid, "kind": "git", "commit": commit, "deleted": deleted}
    for root, dirs, files in os.walk(path):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS]
        rels = [os.path.relpath(os.path.join(root, f), path) for f in files if f not in SKIP_DIRS]
        _copy_into(path, rels, overlay)
    return {"id": tid, "kind": "dir"}


def _holders(spec):
    """[(json path prefix, dict)] -- the spec itself and every dict in its "steps" list."""
    out = [([], spec)] if isinstance(spec, dict) else []
    if isinstance(spec, dict) and isinstance(spec.get("steps"), list):
        out += [(["steps", i], st) for i, st in enumerate(spec["steps"]) if isinstance(st, dict)]
    return out


def _get(d, keys):
    for k in keys:
        if not isinstance(d, dict) or k not in d:
            return None
        d = d[k]
    return d


def collect_refs(spec, spec_dir):
    """[(json_path, abs_path)] for every handled path-valued key that exists on disk. Relative values resolve against the
    spec dir (`ref` defaults to <spec dir>/ref when unset and present)."""
    found = []
    for base, h in _holders(spec):
        for keys in PATH_KEY_PATHS:
            k = keys[-1]
            v = _get(h, keys)
            if keys == ("ref",) and not v and not base and (Path(spec_dir) / "ref").is_dir():
                found.append((base + list(keys), str(Path(spec_dir) / "ref")))
                continue
            if isinstance(v, str) and v.strip():
                p = Path(os.path.expanduser(v))
                if not p.is_absolute():
                    p = Path(spec_dir) / p
                if p.exists():
                    found.append((base + list(keys), str(p)))
    return found


def _is_local(tok):
    t = os.path.expanduser(tok)
    return t.startswith(_LOCAL_PREFIXES) or t.startswith(os.path.expanduser("~") + os.sep)


def plan_cmd_refs(spec, roots):
    """Absolute paths inside the command keys. `roots` is [(realpath, tree id, is_file)] of the shipped trees. Returns
    (subs, refused): subs = [{"path": json path, "from": token, "tree": id, "rel": relative path}] for tokens inside a shipped
    tree (the server swaps the token for the unpacked path); refused = [(key, token)] for tokens that name something on
    this machine outside every shipped tree (the queue cannot see it)."""
    subs, refused = [], []
    for base, h in _holders(spec):
        for keys in CMD_KEY_PATHS:
            v = _get(h, keys)
            if not isinstance(v, str):
                continue
            if keys == ("draft_accept_cmd",) and not base and is_draft_spec(spec):
                continue   # rewritten as a whole by plan_draft, which refuses what it cannot carry
            for tok in dict.fromkeys(_CMD_PATH.findall(v)):
                real = os.path.realpath(os.path.expanduser(tok))
                hit = None
                for root, tid, is_file in roots:
                    if is_file and real == root:
                        hit = (tid, os.path.basename(root))
                    elif not is_file and (real == root or real.startswith(root + os.sep)):
                        hit = (tid, os.path.relpath(real, root) if real != root else "")
                    if hit:
                        break
                if hit:
                    if not _SAFE_REL.match(hit[1]):
                        refused.append((".".join(keys), tok + " (unusual characters in its path inside the tree)"))
                        continue
                    subs.append({"path": base + list(keys), "from": tok, "tree": hit[0],
                                 "rel": "" if hit[1] == "." else hit[1]})
                elif _is_local(tok):
                    refused.append((".".join(keys), tok))
    return subs, refused


def is_draft_spec(spec):
    """A test-draft job spec: phase_a_only with an acceptance command."""
    return (isinstance(spec, dict) and bool(spec.get("phase_a_only")) and isinstance(spec.get("draft_accept_cmd"), str)
            and bool(spec["draft_accept_cmd"].strip()))


def parse_draft_cmd(cmd):
    """Take apart `<python> <path>/mini-testdraft check SPEC [--ref D] [--baseline D] --draft {draft} [flags]`. Returns
    {"spec", "ref", "baseline", "flags"} (flags = the rebuilt, safe tail). Raises RemoteError for any other shape or a flag that
    weakens the gate: --allow-unscored, --shadow, --keep, --min-mutation below DRAFT_MIN_MUTATION, an unknown flag."""
    try:
        t = shlex.split(cmd)
    except ValueError:
        raise RemoteError("draft_accept_cmd does not parse")
    if (len(t) < 5 or not re.match(r"^python[\d.]*$", os.path.basename(t[0])) or os.path.basename(t[1]) != "mini-testdraft"
            or t[2] != "check"):
        raise RemoteError("draft_accept_cmd must be `python mini-testdraft check SPEC --draft {draft}`; a remote queue cannot "
                          "run any other acceptance command")
    out = {"spec": t[3], "ref": None, "baseline": None, "flags": []}
    seen_draft, i = False, 4
    while i < len(t):
        a = t[i]
        if a in ("--ref", "--baseline") and i + 1 < len(t):
            out[a[2:]] = t[i + 1]
            i += 2
        elif a == "--draft" and i + 1 < len(t) and t[i + 1] == "{draft}" and not seen_draft:
            seen_draft = True
            i += 2
        elif a == "--mutmut-pythonpath":
            vals = []
            i += 1
            while i < len(t) and not t[i].startswith("-"):
                vals.append(t[i])
                i += 1
            if not vals or not all(_DRAFT_SAFE.match(v) and ".." not in v.split("/") for v in vals):
                raise RemoteError("draft_accept_cmd: --mutmut-pythonpath needs repo-relative directory names")
            out["flags"] += ["--mutmut-pythonpath"] + vals
        elif a == "--max-mutants" and i + 1 < len(t) and t[i + 1].isdigit() and 1 <= int(t[i + 1]) <= 1000:
            out["flags"] += [a, t[i + 1]]
            i += 2
        elif a == "--min-mutation" and i + 1 < len(t):
            try:
                v = float(t[i + 1])
            except ValueError:
                v = -1.0
            if not (DRAFT_MIN_MUTATION <= v <= 1.0):
                raise RemoteError("draft_accept_cmd weakens the gate (--min-mutation %s is below %s)" % (t[i + 1], DRAFT_MIN_MUTATION))
            out["flags"] += [a, t[i + 1]]
            i += 2
        else:
            why = "weakens the gate" if a in ("--allow-unscored", "--shadow", "--keep") else "has a flag a remote queue does not take"
            raise RemoteError("draft_accept_cmd %s (%s)" % (why, a))
    if not seen_draft:
        raise RemoteError("draft_accept_cmd needs `--draft {draft}`")
    return out


def _resolve(p, base_dir):
    p = Path(os.path.expanduser(p))
    return p if p.is_absolute() else Path(base_dir) / p


def _draft_parts(spec, spec_dir):
    """Validate the draft command against the job spec; returns (parsed command, original spec path, original spec dict)."""
    pc = parse_draft_cmd(spec["draft_accept_cmd"])
    sp = _resolve(pc["spec"], spec_dir)
    if not sp.is_file():
        raise RemoteError("draft_accept_cmd names the spec %s, which is not a file here" % pc["spec"])
    try:
        orig = json.loads(sp.read_text())
    except ValueError:
        raise RemoteError("the spec %s named by draft_accept_cmd does not parse as JSON" % pc["spec"])
    if not isinstance(orig, dict) or orig.get("steps"):
        raise RemoteError("draft_accept_cmd: step-chained specs are not supported")
    for keys in PATH_KEY_PATHS:
        if keys not in (("baseline_from",), ("ref",), ("task",)) and _get(orig, keys):
            raise RemoteError("the spec %s names %s, which a remote draft check does not ship" % (pc["spec"], ".".join(keys)))
    for keys in CMD_KEY_PATHS:
        v = _get(orig, keys)
        if isinstance(v, str) and keys != ("draft_accept_cmd",):
            for tok in _CMD_PATH.findall(v):
                if _is_local(tok):
                    raise RemoteError("the spec %s: %s names %s on this machine; use a path relative to the baseline"
                                      % (pc["spec"], ".".join(keys), tok))
    for k, want in (("baseline", "baseline_from"), ("ref", "ref")):
        if pc[k] is not None and spec.get(want):
            if os.path.realpath(_resolve(pc[k], spec_dir)) != os.path.realpath(_resolve(spec[want], spec_dir)):
                raise RemoteError("draft_accept_cmd --%s differs from the job's %s; a remote draft check uses the shipped one" % (k, want))
    if not (spec.get("baseline_from") and (spec.get("ref") or pc["ref"])):
        raise RemoteError("a remote draft job needs baseline_from and a ref dir")
    return pc, sp, orig


def check_draft(spec, spec_dir):
    """Dry check of a draft spec's acceptance command (no packing). No-op for other specs."""
    if is_draft_spec(spec):
        _draft_parts(spec, spec_dir)


def plan_draft(spec, spec_dir, stage, trees, refs):
    """For a draft spec: stage the original spec (and its task file) as a tree of its own, and return (new job spec dict, cmd_refs).
    The new spec's draft_accept_cmd runs the queue host's mini-testdraft on the shipped copies. None, [] for other specs."""
    if not is_draft_spec(spec):
        return None, []
    pc, sp, orig = _draft_parts(spec, spec_dir)
    tid_of = {tuple(r["path"]): r for r in refs}
    bref, rref = tid_of.get(("baseline_from",)), tid_of.get(("ref",))
    if pc["ref"] is not None and rref is None:   # the spec has no ref of its own: ship the command's
        rd = _resolve(pc["ref"], spec_dir)
        if not rd.is_dir():
            raise RemoteError("draft_accept_cmd --ref %s is not a directory" % pc["ref"])
        tid = "t%d" % len(trees)
        trees.append(stage_tree(tid, str(rd), stage))
        rref = {"path": ["ref"], "tree": tid, "rel": ""}
        spec = dict(spec, ref=str(rd))
        refs.append(rref)
    if bref is None or rref is None or bref["rel"] or rref["rel"]:
        raise RemoteError("a remote draft job needs baseline_from and ref to be directories that exist here")
    d = dict(orig)
    d.pop("ref", None)
    d.pop("draft_accept_cmd", None)
    tf = None
    t = d.get("task")
    if isinstance(t, str) and t and not t.startswith(("#", "\n")) and len(t) < 400 and _resolve(t, sp.parent).is_file():
        tf = _resolve(t, sp.parent)
        d["task"] = "task-" + re.sub(r"[^\w.-]", "_", tf.name)
    dtid = "t%d" % len(trees)
    ov = Path(stage) / "trees" / dtid / "overlay"
    ov.mkdir(parents=True)
    (ov / "spec.json").write_text(json.dumps(d, indent=1))
    if tf is not None:
        shutil.copy2(tf, ov / d["task"])
    trees.append({"id": dtid, "kind": "dir"})
    cmd = " ".join([DRAFT_CMD_HEAD, DRAFT_PH["spec"], "--ref", DRAFT_PH["ref"], "--baseline", DRAFT_PH["base"], "--draft", "{draft}"]
                   + [shlex.quote(x) for x in pc["flags"]])
    subs = [{"path": ["draft_accept_cmd"], "from": DRAFT_PH["spec"], "tree": dtid, "rel": "spec.json"},
            {"path": ["draft_accept_cmd"], "from": DRAFT_PH["ref"], "tree": rref["tree"], "rel": ""},
            {"path": ["draft_accept_cmd"], "from": DRAFT_PH["base"], "tree": bref["tree"], "rel": ""}]
    return dict(spec, draft_accept_cmd=cmd), subs


def _refusal(refused):
    k, tok = refused[0]
    return ("spec key %r names %s on this machine, outside every tree that is shipped (baseline_from, ref, task, ...); the "
            "queue cannot see it. Put the file inside the baseline or use a path relative to it%s"
            % (k, tok, "" if len(refused) == 1 else " (and %d more)" % (len(refused) - 1)))


def unhandled_path_keys(spec):
    """Spec keys (top level) that look like paths but are not rewritten: a warning list, not an error."""
    handled = {keys[0] for keys in PATH_KEY_PATHS} | {keys[0] for keys in CMD_KEY_PATHS} | CLIENT_ONLY_KEYS
    bad = []
    if isinstance(spec, dict):
        for k, v in spec.items():
            if k in handled or k == "steps" or not isinstance(v, str):
                continue
            if v.startswith(("/", "~/")) and os.path.exists(os.path.expanduser(v)):
                bad.append(k)
    return bad


def _stage_all(spec, spec_dir, stage):
    """Stage every shipped tree. Returns (trees, refs, cmd_refs); raises RemoteError for a command path nobody can see."""
    trees, refs, by_path = [], [], {}
    for jpath, abs_path in collect_refs(spec, spec_dir):
        ap = os.path.realpath(abs_path)
        if os.path.isfile(ap):
            # a lone file: its own one-file dir tree (never the whole directory it sits in)
            if ap not in by_path:
                tid = "t%d" % len(trees)
                d = Path(stage) / "trees" / tid / "overlay"
                d.mkdir(parents=True)
                shutil.copy2(ap, d / os.path.basename(ap))
                trees.append({"id": tid, "kind": "dir"})
                by_path[ap] = (tid, os.path.basename(ap))
        elif ap not in by_path:
            tid = "t%d" % len(trees)
            trees.append(stage_tree(tid, ap, stage))
            by_path[ap] = (tid, "")
        tid, rel = by_path[ap]
        refs.append({"path": jpath, "tree": tid, "rel": rel})
    roots = [(p, tid, bool(rel)) for p, (tid, rel) in by_path.items()]
    cmd_refs, refused = plan_cmd_refs(spec, roots)
    if refused:
        raise RemoteError(_refusal(refused))
    return trees, refs, cmd_refs


def check_paths(spec, spec_dir):
    """Dry check (nothing is packed): raises RemoteError when a command key names a path on this machine that no shipped tree
    contains."""
    roots = [(os.path.realpath(a), "t%d" % i, os.path.isfile(a)) for i, (_j, a) in enumerate(collect_refs(spec, spec_dir))]
    _subs, refused = plan_cmd_refs(spec, roots)
    if refused:
        raise RemoteError(_refusal(refused))
    check_draft(spec, spec_dir)


def build_package(spec_path, job, runner_name, dest_tar, extra_trees=None, runner_args=None):
    """Write the package tar. `job` holds model/label and the optional enqueue fields. Returns the manifest.
    extra_trees {id: local dir} are shipped as dir trees besides those the spec points at; runner_args is a list of
    ["--flag"] / ["--flag", {"tree": id, "rel": path in that tree, "prefix": "f="}] items the queue checks against its
    allowlist (queue_upload.RUNNER_FLAGS) and turns into runner arguments (mini-finish's remote check)."""
    spec_path = Path(spec_path)
    spec_text = spec_path.read_text()
    try:
        spec = json.loads(spec_text)
    except ValueError:
        raise RemoteError("spec does not parse as JSON")
    stage = tempfile.mkdtemp(prefix="qremote-")
    try:
        trees, refs, cmd_refs = _stage_all(spec, spec_path.parent, stage)
        dspec, dsubs = plan_draft(spec, spec_path.parent, stage, trees, refs)
        if dspec is not None:   # a test-draft job: the accept command now runs the queue's mini-testdraft on shipped copies
            spec_text = json.dumps(dspec, indent=1)
            cmd_refs = cmd_refs + dsubs
        for tid, tpath in sorted((extra_trees or {}).items()):
            ent = stage_tree(tid, tpath, stage)
            if ent["kind"] != "dir":
                raise RemoteError("extra tree %s must be a plain directory" % tid)
            trees.append(ent)
        manifest = {"version": 1, "job": job, "runner": runner_name, "trees": trees, "refs": refs}
        if runner_args:
            manifest["runner_args"] = runner_args
        if cmd_refs:
            manifest["cmd_refs"] = cmd_refs
        (Path(stage) / "manifest.json").write_text(json.dumps(manifest))
        (Path(stage) / "spec.json").write_text(spec_text)
        with tarfile.open(dest_tar, "w") as tf:
            tf.add(Path(stage) / "manifest.json", arcname="manifest.json")
            tf.add(Path(stage) / "spec.json", arcname="spec.json")
            if (Path(stage) / "trees").is_dir():
                tf.add(Path(stage) / "trees", arcname="trees")
        return manifest
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def parse_enqueue_flags(rest):
    """Map mini-submit's pass-through enqueue flags to manifest job fields. Unknown flags raise: a silently dropped flag
    would change what runs."""
    fields, i = {}, 0
    rest = list(rest)
    while i < len(rest):
        a = rest[i]
        key, eq, val = a.partition("=")
        if key in JOB_SWITCHES:
            fields[JOB_SWITCHES[key]] = True
        elif key in IGNORED_SWITCHES:
            pass
        elif key in JOB_FLAGS:
            if not eq:
                i += 1
                if i >= len(rest):
                    raise RemoteError("%s needs a value" % key)
                val = rest[i]
            fields[JOB_FLAGS[key]] = val
        else:
            raise RemoteError("enqueue flag %s is not supported when the queue is remote" % key)
        i += 1
    return fields


def _conn(base, timeout):
    u = urlsplit(base)
    cls = http.client.HTTPSConnection if u.scheme == "https" else http.client.HTTPConnection
    return cls(u.hostname, u.port or (443 if u.scheme == "https" else 80), timeout=timeout), u.path.rstrip("/")


def upload(base, headers, pkg_path, timeout=600):
    """POST the package; returns the parsed JSON answer ({"id": ...}). Raises RemoteError with the server's message."""
    c, prefix = _conn(base, timeout)
    size = os.path.getsize(pkg_path)
    try:
        with open(pkg_path, "rb") as f:
            c.request("POST", prefix + "/api/jobs/upload", body=f,
                      headers=dict(headers, **{"Content-Type": "application/x-tar", "Content-Length": str(size)}))
            r = c.getresponse()
            body = r.read().decode("utf-8", "replace")
    except OSError as e:
        raise RemoteError("upload failed: %s" % type(e).__name__)
    finally:
        c.close()
    if r.status != 200:
        try:
            msg = json.loads(body).get("error") or body
        except ValueError:
            msg = body
        raise RemoteError("queue answered %d: %s" % (r.status, str(msg)[:300]))
    try:
        return json.loads(body)
    except ValueError:
        raise RemoteError("queue answered something that is not JSON")


def fetch_result(base, headers, job_id, dest_dir, timeout=300, scope=None):
    """GET the job's result tar.gz and unpack it into dest_dir (tarfile "data" filter). scope="run" asks for the whole run
    dir instead (mini-finish's mirror). Returns dest_dir."""
    c, prefix = _conn(base, timeout)
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        c.request("GET", prefix + "/api/jobs/%s/result%s" % (job_id, "?scope=run" if scope == "run" else ""), headers=dict(headers))
        r = c.getresponse()
        if r.status != 200:
            raise RemoteError("queue answered %d for the result of %s" % (r.status, job_id))
        tmp = dest / ".result.tgz"
        with open(tmp, "wb") as f:
            shutil.copyfileobj(r, f)
    except OSError as e:
        raise RemoteError("result fetch failed: %s" % type(e).__name__)
    finally:
        c.close()
    try:
        with tarfile.open(tmp, "r:gz") as tf:
            for m in tf.getmembers():   # a result holds plain files and dirs only; the "data" filter below is the second line
                parts = Path(m.name).parts
                if m.name.startswith(("/", "~")) or ".." in parts or m.issym() or m.islnk() or not (m.isfile() or m.isdir()):
                    raise RemoteError("result archive refused: unsafe member %r" % m.name[:80])
            tf.extractall(dest, filter="data")
    except (tarfile.TarError, OSError) as e:
        raise RemoteError("result archive refused: %s" % type(e).__name__)
    finally:
        try:
            tmp.unlink()
        except OSError:
            pass
    return dest
