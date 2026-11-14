#!/usr/bin/env python3
"""queue_standby -- the standby container's state import and back-test bookkeeping. stdlib only.

The queue container runs in standby (QUEUE_STANDBY=1) before the cutover with a COPY of the Mac queue's state.
This module takes that copy in as a tarball and puts it where the queue code reads it:

  state/      -> queue_config.state_dir()     the queue state JSON, the per-job sidecars (done/gate/review ...)
  dispatch/   -> queue_config.dispatch_dir()  CPU lane store, event log, escalations, hosts.json, plan files ...
  manifest.json (optional, top level)         {"format": 1, "files": {"<tar path>": "<sha256>", ...}}

Only a fixed list of top-level names is accepted inside state/ and dispatch/ (STATE_NAMES, DISPATCH_NAMES);
worktrees, mini-work, caches and virtualenvs are never part of it. The extraction is strict: regular files and
directories only (no links, devices, fifos), no absolute names, no `..`, no name outside the allowlist, a cap on the
unpacked size and the member count, and nothing is written unless the whole archive passed the first scan. The files
are written to a staging folder inside each target folder (same volume), checked against the manifest when there is
one, and only then swapped in. Whatever was there goes to `.standby-backup-<stamp>/` next to it (the newest two
backups are kept).

The back-test (queue-backtest) keeps its runs under backtest_root(); the API starts it and serves its results.
"""
import hashlib
import json
import os
import shutil
import sqlite3
import tarfile
import time
import uuid
from pathlib import Path

# ---- what a state tarball may hold ------------------------------------------------------------------------------
TOP_NAMES = ("manifest.json", "state", "dispatch")

# state/: the queue state JSON and the per-job records. (The lock files are never imported.)
STATE_NAMES = frozenset((
    "ollama-queue-state.json", "ollama-queue-logs", "ollama-queue-livelogs", "signoff.json", "signoff-rules.json",
))

# dispatch/: the small live state of ~/.ollama-dispatch. Never worktrees, mini-work, mini-runs, mini-venv,
# node_modules-cache, retired*, scratch or backup folders.
DISPATCH_NAMES = frozenset((
    "cpu-jobs", "escalations", "hosts.json", "queue-events.jsonl", "queue-events.jsonl.1", "queue-events.cursor",
    "queue-events.failed.json", "queue-events.state.json", "queue-events.sessions", "decisions.jsonl",
    "failures.jsonl", "ttc-decisions.jsonl", "ttc-mode.json", "landed.jsonl", "reviews.jsonl",
    "bundle-attention.json", "bundle-superseded.json", "bundle-nsample.json", "defaults.json", "model-ladder.json",
    "log-retention.json", "queue-wait.json", "queue-slots.json", "queue-degraded.json", "offload-throttle.json",
    "ollama-inventory.json", "infra-incidents.jsonl", "mini-overrides.jsonl", "mini-finish.jsonl", "slice-runs",
    "slice-plans", "plans", "auto-runs", "triage", "unsat-seen.json", "scope-creep.jsonl", "excluded.jsonl",
    "conformance", "continuation.disabled", "lane-plan.disabled", "unraid-research-quality.json",
    "slot-measure.jsonl", "darkbloom-usable-peak.json",
))
ROOT_NAMES = {"state": STATE_NAMES, "dispatch": DISPATCH_NAMES}

MAX_MEMBERS = 400_000
MAX_DEPTH = 40
MAX_NAME_LEN = 240
MAX_MANIFEST_BYTES = 8 * 1024 * 1024
KEEP_BACKUPS = 2
MARKER = ".standby-imported"
BACKUP_PREFIX = ".standby-backup-"
STAGE_PREFIX = ".standby-stage-"
CASES_TOP = "cases"


class StandbyError(Exception):
    """A refusal with an HTTP status and a short message that is safe to show (never echoes a whole untrusted name)."""

    def __init__(self, msg, status=400):
        super().__init__(msg)
        self.msg, self.status = msg, status


def _show(name):
    s = "".join(c if 32 <= ord(c) < 127 else "?" for c in str(name))
    return s[:80]


def import_max_bytes():
    """Cap on the unpacked size, from env QUEUE_STANDBY_IMPORT_MAX_MB (default 2048)."""
    try:
        return int(float(os.environ.get("QUEUE_STANDBY_IMPORT_MAX_MB") or 2048) * 1024 * 1024)
    except ValueError:
        return 2048 * 1024 * 1024


def upload_max_bytes():
    """Cap on the uploaded tarball itself (same env, a little tighter than the unpacked cap)."""
    return import_max_bytes()


def sha256_file(path, bufsize=1024 * 1024):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            b = f.read(bufsize)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


# ---- scan and extract -------------------------------------------------------------------------------------------
def clean_name(name):
    """The member name as a tuple of path parts, or raise StandbyError. Accepts a leading './' only."""
    n = name
    while n.startswith("./"):
        n = n[2:]
    if n in ("", "."):
        return ()
    if n.startswith("/"):
        raise StandbyError("absolute path in archive: " + _show(name))
    if "\\" in n or "\0" in n:
        raise StandbyError("bad character in archive path: " + _show(name))
    if len(n) > MAX_NAME_LEN:
        raise StandbyError("path too long in archive: " + _show(name))
    parts = n.rstrip("/").split("/")
    if len(parts) > MAX_DEPTH:
        raise StandbyError("path too deep in archive: " + _show(name))
    for p in parts:
        if p in ("", ".", ".."):
            raise StandbyError("'..' or empty path part in archive: " + _show(name))
    return tuple(parts)


def scan(tar_path, max_unpacked=None, max_members=MAX_MEMBERS, rule=None):
    """First pass: validate every member without writing anything. -> {"files": n, "bytes": total, "tops": {...}}
    rule(parts, member) raises StandbyError for a name the archive may not hold (default: the state allowlist)."""
    rule = rule or _state_rule
    max_unpacked = import_max_bytes() if max_unpacked is None else max_unpacked
    total = files = count = 0
    seen_roots = set()
    try:
        tf = tarfile.open(tar_path, "r:*")
    except (tarfile.TarError, OSError, EOFError) as e:
        raise StandbyError("not a readable tar archive: %s" % type(e).__name__, 400)
    with tf:
        try:
            for m in tf:
                count += 1
                if count > max_members:
                    raise StandbyError("archive has more than %d entries" % max_members, 413)
                if m.issym() or m.islnk():
                    raise StandbyError("links are not allowed in the archive: " + _show(m.name))
                if not (m.isreg() or m.isdir()):
                    raise StandbyError("only files and folders are allowed in the archive: " + _show(m.name))
                parts = clean_name(m.name)
                if not parts:
                    continue
                rule(parts, m)
                seen_roots.add(parts[0])
                if m.isreg():
                    if m.size < 0:
                        raise StandbyError("bad size in archive")
                    total += m.size
                    files += 1
                    if total > max_unpacked:
                        raise StandbyError("unpacked size is over the %d MB cap" % (max_unpacked // (1024 * 1024)), 413)
        except (tarfile.TarError, EOFError, OSError) as e:
            raise StandbyError("damaged tar archive: %s" % type(e).__name__, 400)
    return {"files": files, "bytes": total, "tops": sorted(seen_roots)}


def _state_rule(parts, m):
    """What a state tarball may hold: TOP_NAMES at the top, the allowlisted names under state/ and dispatch/."""
    top = parts[0]
    if top not in TOP_NAMES:
        raise StandbyError("top-level name not allowed: " + _show(top))
    if top == "manifest.json":
        if len(parts) != 1 or not m.isreg():
            raise StandbyError("manifest.json must be a single file")
        if m.size > MAX_MANIFEST_BYTES:
            raise StandbyError("manifest.json too large", 413)
    elif len(parts) == 1:
        if not m.isdir():
            raise StandbyError("%s must be a folder" % top)
    elif parts[1] not in ROOT_NAMES[top]:
        raise StandbyError("name not allowed under %s/: %s" % (top, _show(parts[1])))


def _cases_rule(parts, m):
    """What a cases tarball may hold: everything under cases/ and nothing else."""
    if parts[0] != CASES_TOP:
        raise StandbyError("a cases archive holds only cases/: " + _show(parts[0]))
    if len(parts) == 1 and not m.isdir():
        raise StandbyError("cases must be a folder")


def extract(tar_path, stage_dirs, manifest_out=None):
    """Second pass: write the validated members under stage_dirs {"state": Path, "dispatch": Path}.
    Returns {tar path: sha256} for every regular file written (the manifest.json itself is parsed into manifest_out)."""
    written = {}
    with tarfile.open(tar_path, "r:*") as tf:
        for m in tf:
            parts = clean_name(m.name)
            if not parts:
                continue
            top = parts[0]
            if top == "manifest.json":
                data = tf.extractfile(m).read(MAX_MANIFEST_BYTES + 1)
                try:
                    doc = json.loads(data)
                except ValueError:
                    raise StandbyError("manifest.json is not valid JSON")
                if manifest_out is not None and isinstance(doc, dict):
                    manifest_out.update(doc)
                continue
            dest = stage_dirs[top].joinpath(*parts[1:])
            if m.isdir():
                dest.mkdir(parents=True, exist_ok=True)
                os.chmod(dest, 0o755)
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            h = hashlib.sha256()
            src = tf.extractfile(m)
            # O_NOFOLLOW + O_EXCL: never write through a link or over an existing path
            fd = os.open(str(dest), os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o600)
            with os.fdopen(fd, "wb") as out:
                while True:
                    b = src.read(1024 * 1024)
                    if not b:
                        break
                    h.update(b)
                    out.write(b)
            os.chmod(dest, 0o755 if m.mode & 0o111 else 0o644)
            written["/".join(parts)] = h.hexdigest()
    return written


def check_manifest(manifest, written):
    """-> (ok, problems). Every written file must be listed with the right sha256 and every listed file written."""
    files = manifest.get("files") if isinstance(manifest, dict) else None
    if not isinstance(files, dict):
        return False, ["manifest has no files table"]
    bad = []
    for p, sha in written.items():
        if files.get(p) != sha:
            bad.append(("not listed or wrong sha256: " + _show(p)))
    for p in files:
        if p not in written:
            bad.append("listed but not in the archive: " + _show(p))
    return (not bad), bad[:20]


# ---- what is already there, and the swap ------------------------------------------------------------------------
def existing_summary(state_dir, dispatch_dir):
    """Reasons the target already holds a queue's state ([] = empty enough to import without ?replace=1)."""
    why = []
    state_dir, dispatch_dir = Path(state_dir), Path(dispatch_dir)
    if (state_dir / MARKER).exists():
        why.append("a previous import is recorded")
    sj = state_dir / "ollama-queue-state.json"
    try:
        d = json.loads(sj.read_text())
        if isinstance(d, dict) and d.get("jobs"):
            why.append("the state file holds %d job(s)" % len(d["jobs"]))
    except (OSError, ValueError):
        pass
    try:
        if (dispatch_dir / "queue-events.jsonl").stat().st_size > 0:
            why.append("the event log is not empty")
    except OSError:
        pass
    db = dispatch_dir / "cpu-jobs" / "jobs.sqlite"
    if db.exists():
        try:
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
            try:
                n = c.execute("select count(*) from jobs").fetchone()[0]
            finally:
                c.close()
            if n:
                why.append("the CPU lane store holds %d job(s)" % n)
        except sqlite3.Error:
            pass
    return why


def _prune_backups(target, keep=KEEP_BACKUPS):
    olds = sorted(p for p in target.iterdir() if p.name.startswith(BACKUP_PREFIX) and p.is_dir())
    for p in olds[:-keep] if keep else olds:
        shutil.rmtree(p, ignore_errors=True)


def swap(stage_dirs, targets, stamp=None):
    """Move the old allowlisted names to <target>/.standby-backup-<stamp>/ and the staged ones in. Rolls back on an
    error. -> {"backup": {"state": path, "dispatch": path}}. Caller holds whatever lock keeps the queue still."""
    stamp = stamp or time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    moved_old, moved_new, backups = [], [], {}
    try:
        for root, stage in stage_dirs.items():
            target = Path(targets[root])
            target.mkdir(parents=True, exist_ok=True)
            bdir = target / (BACKUP_PREFIX + stamp)
            backups[root] = str(bdir)
            for name in sorted(ROOT_NAMES[root]):
                cur = target / name
                if cur.exists() or cur.is_symlink():
                    bdir.mkdir(exist_ok=True)
                    os.rename(cur, bdir / name)
                    moved_old.append((cur, bdir / name))
            for child in sorted(stage.iterdir()):
                os.rename(child, target / child.name)
                moved_new.append(target / child.name)
    except Exception:
        for p in reversed(moved_new):
            try:
                if p.is_dir() and not p.is_symlink():
                    shutil.rmtree(p, ignore_errors=True)
                else:
                    p.unlink()
            except OSError:
                pass
        for cur, bak in reversed(moved_old):
            try:
                os.rename(bak, cur)
            except OSError:
                pass
        raise
    for root in stage_dirs:
        try:
            _prune_backups(Path(targets[root]))
        except OSError:
            pass
    return {"backup": backups}


def import_state(tar_path, state_dir, dispatch_dir, sha256=None, replace=False, max_unpacked=None, around_swap=None):
    """The whole import. tar_path is a local file already on this volume. sha256 (hex, optional but expected) must
    match the file. around_swap is a context-manager factory entered for the swap (the API passes its queue lock).
    -> a summary dict; raises StandbyError for every refusal. Nothing is changed unless everything checked out."""
    state_dir, dispatch_dir = Path(state_dir), Path(dispatch_dir)
    got = sha256_file(tar_path)
    if sha256 and got != str(sha256).strip().lower():
        raise StandbyError("sha256 of the upload does not match the one sent", 422)
    info = scan(tar_path, max_unpacked)
    why = existing_summary(state_dir, dispatch_dir)
    if why and not replace:
        raise StandbyError("standby already holds state (%s); use ?replace=1 to replace it (the old one is kept as a "
                           "backup)" % "; ".join(why), 409)
    sid = uuid.uuid4().hex[:8]
    stage = {"state": state_dir / (STAGE_PREFIX + sid), "dispatch": dispatch_dir / (STAGE_PREFIX + sid)}
    manifest = {}
    try:
        for p in stage.values():
            p.mkdir(parents=True, exist_ok=False)
            os.chmod(p, 0o700)
        written = extract(tar_path, stage, manifest_out=manifest)
        manifest_ok = None
        if manifest:
            manifest_ok, problems = check_manifest(manifest, written)
            if not manifest_ok:
                raise StandbyError("manifest check failed: " + "; ".join(problems[:5]), 422)
        import contextlib
        with (around_swap() if around_swap else contextlib.nullcontext()):
            res = swap(stage, {"state": state_dir, "dispatch": dispatch_dir})
        (state_dir / "ollama-queue-logs").mkdir(exist_ok=True)
        (state_dir / MARKER).write_text(json.dumps({"at": time.time(), "tar_sha256": got, "files": len(written),
                                                      "bytes": info["bytes"]}))
    finally:
        for p in stage.values():
            shutil.rmtree(p, ignore_errors=True)
    return {"ok": True, "tar_sha256": got, "files": info["files"], "bytes": info["bytes"],
            "manifest_ok": manifest_ok, "replaced": bool(why), "backup": res["backup"]}


def import_cases(tar_path, root, sha256=None, max_unpacked=None):
    """The back-test's case files (cases/b3.jsonl, cases/bases/..., cases/b6/routes.json ...), made from the Mac's real
    history by the operator step, into <root>/cases (root = backtest_root(), where queue-backtest looks by default).
    Same strict scan and extraction as the state import (files and folders only, no links, no '..', size and count
    caps, nothing written unless the whole archive passed); the staged folder then replaces <root>/cases whole.
    -> {"ok", "tar_sha256", "files", "bytes", "cases": {"b3": n, ...}}; raises StandbyError for every refusal."""
    root = Path(root)
    got = sha256_file(tar_path)
    if sha256 and got != str(sha256).strip().lower():
        raise StandbyError("sha256 of the upload does not match the one sent", 422)
    info = scan(tar_path, max_unpacked, rule=_cases_rule)
    if CASES_TOP not in info["tops"]:
        raise StandbyError("the archive holds no cases/ folder")
    root.mkdir(parents=True, exist_ok=True)
    sid = uuid.uuid4().hex[:8]
    stage, old, target = root / (STAGE_PREFIX + sid), root / (BACKUP_PREFIX + "cases-" + sid), root / CASES_TOP
    try:
        stage.mkdir(exist_ok=False)
        os.chmod(stage, 0o700)
        extract(tar_path, {CASES_TOP: stage})
        os.chmod(stage, 0o755)
        if target.exists() or target.is_symlink():
            os.rename(target, old)
        try:
            os.rename(stage, target)
        except OSError:
            if old.exists():
                os.rename(old, target)
            raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
        shutil.rmtree(old, ignore_errors=True)
    counts = {}
    for name in ("b3", "b4", "b5"):
        try:
            with open(target / (name + ".jsonl")) as f:
                counts[name] = sum(1 for line in f if line.strip())
        except OSError:
            pass
    counts["b6"] = (target / "b6" / "routes.json").is_file()
    counts["b7"] = (target / "b7" / "mac").is_dir()
    return {"ok": True, "tar_sha256": got, "files": info["files"], "bytes": info["bytes"], "cases": counts}


# ---- export (the reverse of the import: fallback pulls it back to the Mac; the Mac's cutover packs the same way) ---
def _add_tree(tf, src, arc, files, skip_sqlite_sidecars=True):
    """Add `src` (file or folder) as `arc`: regular files only (no links), sha256 per file into `files`."""
    src = Path(src)
    if src.is_symlink() or not src.exists():
        return 0
    n = 0
    if src.is_dir():
        for child in sorted(src.iterdir()):
            n += _add_tree(tf, child, arc + "/" + child.name, files, skip_sqlite_sidecars)
        return n
    if not src.is_file():
        return 0
    if skip_sqlite_sidecars and src.name.endswith(("-wal", "-shm")):
        return 0
    files[arc] = sha256_file(src)
    tf.add(str(src), arcname=arc, recursive=False)
    return 1


def build_export(state_dir, dispatch_dir, out_tar, scratch=None):
    """Pack the small live state (the same allowlist the import takes) into out_tar with a manifest.json of sha256s.
    The CPU-lane sqlite is copied with the sqlite backup API (a consistent snapshot, WAL folded in), never file-copied.
    -> {"tar_sha256", "files", "bytes"}. Links, devices and anything outside the allowlist are left out."""
    state_dir, dispatch_dir, out_tar = Path(state_dir), Path(dispatch_dir), Path(out_tar)
    import tempfile
    scratch = Path(scratch) if scratch else Path(tempfile.mkdtemp(prefix=".standby-export-", dir=str(out_tar.parent)))
    scratch.mkdir(parents=True, exist_ok=True)
    files = {}
    try:
        with tarfile.open(out_tar, "w") as tf:
            for top, root, names in (("state", state_dir, STATE_NAMES), ("dispatch", dispatch_dir, DISPATCH_NAMES)):
                for name in sorted(names):
                    if top == "dispatch" and name == "cpu-jobs":
                        continue                                          # handled below (sqlite by backup)
                    _add_tree(tf, root / name, top + "/" + name, files)
            lane = dispatch_dir / "cpu-jobs"
            db = lane / "jobs.sqlite"
            if db.is_file() and not db.is_symlink():
                snap = scratch / "jobs.sqlite"
                src = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=30)
                dst = sqlite3.connect(str(snap))
                try:
                    src.backup(dst)
                finally:
                    dst.close()
                    src.close()
                files["dispatch/cpu-jobs/jobs.sqlite"] = sha256_file(snap)
                tf.add(str(snap), arcname="dispatch/cpu-jobs/jobs.sqlite", recursive=False)
            if lane.is_dir() and not lane.is_symlink():
                for child in sorted(lane.iterdir()):
                    if not child.name.startswith("jobs.sqlite"):          # the database went in above, by backup
                        _add_tree(tf, child, "dispatch/cpu-jobs/" + child.name, files)
            data = json.dumps({"format": 1, "files": files, "created": time.time()}, sort_keys=True).encode()
            ti = tarfile.TarInfo("manifest.json")
            ti.size = len(data)
            ti.mtime = int(time.time())
            import io
            tf.addfile(ti, io.BytesIO(data))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)
    return {"tar_sha256": sha256_file(out_tar), "files": len(files), "bytes": out_tar.stat().st_size}


def running_counts(state_dir, dispatch_dir):
    """-> {"running": jobs in the queue state marked running, "cpu_running": CPU-lane jobs running, "pending": pending
    jobs in the queue state}. Read-only; an unreadable store counts as 0."""
    out = {"running": 0, "cpu_running": 0, "pending": 0}
    try:
        d = json.loads((Path(state_dir) / "ollama-queue-state.json").read_text())
        for j in d.get("jobs") or []:
            st = j.get("status")
            if st == "running":
                out["running"] += 1
            elif st == "pending":
                out["pending"] += 1
    except (OSError, ValueError, AttributeError):
        pass
    db = Path(dispatch_dir) / "cpu-jobs" / "jobs.sqlite"
    if db.exists():
        try:
            c = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=5)
            try:
                out["cpu_running"] = c.execute("select count(*) from jobs where status='running'").fetchone()[0]
            finally:
                c.close()
        except sqlite3.Error:
            pass
    return out


# ---- back-test runs ---------------------------------------------------------------------------------------------
def backtest_root(state_dir=None):
    """Scratch root of the back-test: $QUEUE_BACKTEST_DIR, else <state dir>/.backtest. It is not an import name,
    so a state import never touches it."""
    p = os.environ.get("QUEUE_BACKTEST_DIR")
    if p:
        return Path(p)
    if state_dir is None:
        import queue_config
        state_dir = queue_config.state_dir()
    return Path(state_dir) / ".backtest"


def valid_run_id(rid):
    return isinstance(rid, str) and len(rid) == 12 and all(c in "0123456789abcdef" for c in rid)


def new_run_id():
    return uuid.uuid4().hex[:12]


def read_run(root, rid):
    """-> {"id", "state": "running"|"done"|"failed"|"unknown", "results": {...} | None}, or None for an unknown id."""
    if not valid_run_id(rid):
        return None
    d = Path(root) / rid
    if not d.is_dir():
        return None
    out = {"id": rid, "state": "unknown", "results": None}
    try:
        out.update(json.loads((d / "status.json").read_text()))
    except (OSError, ValueError):
        pass
    try:
        out["results"] = json.loads((d / "results.json").read_text())
    except (OSError, ValueError):
        pass
    return out
