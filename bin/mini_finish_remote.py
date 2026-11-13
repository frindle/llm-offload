"""mini_finish_remote -- what mini-finish does differently when the queue is on another machine (queue_config.queue_is_remote()).

  * The failed job's run dir (work/, spec, step-refs/, REVIEW*.md, finish/ ...) is fetched from the queue
    (GET /api/jobs/<id>/result?scope=run) into a mirror, <home>/mini-finish-remote/<job>/, where the Claude session edits the
    work dir and mini-finish keeps its claim. The mirror is data from another machine: it is unpacked with tarfile's "data"
    filter, every .git has its hooks removed and its config replaced by a minimal one (trees/*/.git is dropped), symlinks are
    dropped, and every git command in this process runs with hooks and fsmonitor off (git_env).
  * --check never runs the job's gates here. remote_runner() packs the candidate files and the spec, sends them to the queue as a
    check job (the runner chosen by name, the same --check-only arguments), waits for it, fetches its result and returns the
    runner's output for mini-finish to read the verdict from, then clears the check job's dashboard card.
  * Paths the queue wrote into the spec (its own run dir) are rewritten to the mirror (xlate).
  * A run dir over the queue's run result cap comes back partial, with the left-out files in _queue/skipped.txt. The fetch
    says so, and --check refuses a partial mirror (a check on missing files can pass wrongly) unless
    MINI_FINISH_ALLOW_PARTIAL=1. A mirror is fetched once; to fetch it again, delete its folder.
Nothing fetched from the queue is ever executed here.
"""
import json
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

import queue_config
import queue_remote as QR

MIRROR_DIR = "mini-finish-remote"
GIT_CONFIG = "[core]\n\trepositoryformatversion = 0\n\tfilemode = true\n\tbare = false\n"
LIVE = ("pending", "queued", "scheduled", "running", "paused", "held")
RUNNER_NAME = "mini-swe-runner.py"
VALUE_FLAGS = ("--try-test", "--try-fix", "--try-diagnosis")
SWITCH_FLAGS = ("--check-only", "--mutation", "--draft-check")
_JID = re.compile(r"^[0-9a-f]{6,64}$")   # the queue's job ids (its result route takes no other form)


def git_env():
    """Hooks and fsmonitor off for every git this process (and the runner module it loads) starts."""
    os.environ.update({"GIT_CONFIG_COUNT": "2", "GIT_CONFIG_KEY_0": "core.hooksPath", "GIT_CONFIG_VALUE_0": "/dev/null",
                       "GIT_CONFIG_KEY_1": "core.fsmonitor", "GIT_CONFIG_VALUE_1": "false", "GIT_TERMINAL_PROMPT": "0"})


def git_prefix():
    return ["git", "-c", "core.hooksPath=/dev/null", "-c", "core.fsmonitor=false"]


def mirror_dir(home, jid):
    """The mirror of job jid. The id comes from the queue, so it must be a plain job id: never a path that leaves the root."""
    if not _JID.match(str(jid)):
        raise QR.RemoteError("queue job id %r is not a plain job id" % str(jid)[:40])
    return Path(home) / MIRROR_DIR / str(jid)


def queue_jobs():
    st = queue_config.load_queue_state()
    return [j for j in (st or {}).get("jobs", []) if isinstance(j, dict) and j.get("id")]


def sanitize(root):
    """Make a fetched tree inert: no git hooks, a minimal git config, no nested checkout metadata under trees/, no symlinks."""
    root = Path(root)
    for d, dirs, files in os.walk(root):
        dp = Path(d)
        for name in list(dirs) + list(files):
            if (dp / name).is_symlink():
                (dp / name).unlink()
        dirs[:] = [x for x in dirs if (dp / x).is_dir()]
        if ".git" in dirs:
            g = dp / ".git"
            if "trees" in g.relative_to(root).parts:
                shutil.rmtree(g, ignore_errors=True)
            else:
                shutil.rmtree(g / "hooks", ignore_errors=True)
                (g / "config").write_text(GIT_CONFIG)
                for extra in ("info/attributes",):
                    if (g / extra).is_file():
                        (g / extra).unlink()
            dirs.remove(".git")
        if ".git" in files:   # a gitfile points at some other directory: never follow it
            (dp / ".git").unlink()


def skipped(mirror):
    """The files the queue left out of a mirror (over its run result cap), as their paths; [] for a complete mirror."""
    try:
        text = (Path(mirror) / "_queue" / "skipped.txt").read_text(errors="replace")
    except OSError:
        return []
    return [ln.partition("\t")[2] or ln for ln in text.splitlines() if ln and not ln.startswith("#")]


def partial_note(mirror):
    """One message about a partial mirror, or "" when it is complete."""
    left = skipped(mirror)
    if not left:
        return ""
    shown = ", ".join(left[:3]) + (" ..." if len(left) > 3 else "")
    return ("the queue left %d file(s) out of this mirror (over its run result cap; list: %s): %s. Raise upload.run_max_mb "
            "on the queue, delete %s and run again to fetch it whole" % (len(left), Path(mirror) / "_queue" / "skipped.txt",
                                                                          shown, mirror))


def fetch_mirror(home, jid):
    """Download the run dir of job jid into <home>/mini-finish-remote/<jid>/ (only if it is not there yet). Returns the dir."""
    dest = mirror_dir(home, jid)
    if dest.is_dir():
        return dest
    root = dest.parent
    root.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=".fetch-", dir=str(root)))
    try:
        base, _tok = queue_config.queue_api()
        QR.fetch_result(base, queue_config.queue_api_headers(), jid, tmp, scope="run")
        sanitize(tmp)
        os.rename(tmp, dest)
    except BaseException:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    note = partial_note(dest)
    if note:
        import sys
        print("mini-finish: WARNING: " + note, file=sys.stderr)
    return dest


def localize(rec, home, fetch):
    """Point a queue row at the mirror. With fetch=False and no mirror yet, the row is returned marked `_need_fetch`."""
    rec = dict(rec)
    jid = str(rec["id"])
    srv_work = str(rec.get("cwd") or "")
    rec["_remote"] = True
    rec["_srv_run"] = str(Path(srv_work).parent) if srv_work else ""
    try:
        m = mirror_dir(home, jid)
    except QR.RemoteError:
        if not fetch:   # listed from its queue row; a fetch refuses it
            rec["_need_fetch"] = True
            return rec
        raise
    if not m.is_dir():
        if not fetch:
            rec["_need_fetch"] = True
            return rec
        m = fetch_mirror(home, jid)
    rec.pop("_need_fetch", None)
    done = {}
    try:
        done = json.loads((m / "_queue" / "done.json").read_text())
        done = done if isinstance(done, dict) else {}
    except (OSError, ValueError):
        pass
    merged = dict(done)
    merged.update({k: v for k, v in rec.items() if v is not None})
    merged["cwd"] = str(m / "work")
    if (m / "spec.json").is_file():
        merged["task_file"] = str(m / "spec.json")
    merged["_mirror"] = str(m)
    return merged


def xlate(obj, rec):
    """Rewrite the queue's run-dir prefix to the mirror inside every string of a spec."""
    srv, mir = rec.get("_srv_run"), rec.get("_mirror")
    if not srv or not mir:
        return obj
    if isinstance(obj, str):
        return obj.replace(srv, mir) if srv in obj else obj
    if isinstance(obj, list):
        return [xlate(x, rec) for x in obj]
    if isinstance(obj, dict):
        return {k: xlate(v, rec) for k, v in obj.items()}
    return obj


def livelog(rec):
    m = rec.get("_mirror")
    p = Path(m) / "_queue" / "log-tail.txt" if m else None
    return p if p and p.is_file() else None


def clear_card(base, headers, jid, timeout=30):
    """Best effort: clear a finished job's dashboard run card (POST /api/runs/<id>/clear, as `ollama-queue.py clear-card`
    sends it). Returns None when cleared (or already gone), else a short reason."""
    import urllib.error
    import urllib.request
    req = urllib.request.Request(base + "/api/runs/%s/clear" % jid, data=b"{}", method="POST",
                                 headers=dict(headers, **{"Content-Type": "application/json"}))
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            r.read()
        return None
    except urllib.error.HTTPError as e:
        return None if e.code == 404 else "HTTP %d" % e.code
    except (urllib.error.URLError, OSError) as e:
        return type(e).__name__


def _arg_items(extra, chk):
    """mini-finish's local runner arguments -> the package's runner_args (candidate paths become paths inside the "cand" tree)."""
    cand = os.path.realpath(str(Path(chk) / "cand"))
    items, i = [["--check-only"]], 0
    extra = list(extra)
    while i < len(extra):
        a = extra[i]
        if a in SWITCH_FLAGS:
            if a != "--check-only":
                items.append([a])
        elif a in VALUE_FLAGS and i + 1 < len(extra):
            i += 1
            pre, path = "", extra[i]
            if a == "--try-fix":
                pre, sep, path = extra[i].partition("=")
                if not sep:
                    raise QR.RemoteError("--try-fix needs file=path")
                pre += "="
            rp = os.path.realpath(path)
            if not rp.startswith(cand + os.sep):
                raise QR.RemoteError("check file %s is not a candidate file" % path)
            items.append([a, {"tree": "cand", "rel": os.path.relpath(rp, cand), "prefix": pre}])
        else:
            raise QR.RemoteError("runner argument %s cannot be sent to the queue" % a)
        i += 1
    return items


def items_flat(items):
    return [x for it in items for x in it if isinstance(x, str)]


def fetch_draft_files(base, headers, cid, chk, say=None):
    """DRAFT-CHECK.txt and draft_<test> of a check job are written to its run dir; pull just those two kinds of file into chk."""
    chk = Path(chk)
    with tempfile.TemporaryDirectory(prefix="mini-finish-run-") as td:
        try:
            QR.fetch_result(base, headers, cid, td, scope="run")
        except QR.RemoteError:
            return
        for f in Path(td).iterdir():
            if f.is_file() and not f.is_symlink() and (f.name.startswith("DRAFT-CHECK") or f.name.startswith("draft_")):
                shutil.copy2(f, chk / f.name)


def remote_runner(c, chk, spec_path, extra, say=print, timeout=3600):
    """The remote stand-in for mini-finish's run_runner: returns (rc, output) of the check job the queue ran."""
    chk = Path(chk)
    note = partial_note(c.rec["_mirror"]) if c.rec.get("_mirror") else ""
    if note and os.environ.get("MINI_FINISH_ALLOW_PARTIAL") != "1":
        msg = ("[mini-finish] remote check not sent: %s (or set MINI_FINISH_ALLOW_PARTIAL=1 to check anyway)\n" % note)
        say(msg.rstrip())   # mini-finish's own verdict line only says "see the log"
        try:
            (chk / "check.log").write_text(msg)
        except OSError:
            pass
        return 125, msg
    try:
        spec_path = Path(spec_path)
        if not str(spec_path.resolve()).startswith(str(chk.resolve()) + os.sep):
            spec_path = chk / "spec.remote.json"   # the queue's own spec text, with its paths pointed at the mirror
            spec_path.write_text(json.dumps(c.raw, indent=1))
        items = _arg_items(extra, chk)
        job = {"host": "auto", "task_kind": "coding", "model": str(c.rec.get("model") or "check"),
               "label": ("%s-chk-%s" % (c.label, time.strftime("%H%M%S")))[:60], "allow_duplicate_label": True}
        base, _tok = queue_config.queue_api()
        headers = queue_config.queue_api_headers()
        cand = chk / "cand"
        cand.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="mini-finish-chk-") as td:
            pkg = Path(td) / "package.tar"
            QR.build_package(spec_path, job, RUNNER_NAME, pkg, extra_trees={"cand": str(cand)}, runner_args=items)
            res = QR.upload(base, headers, pkg)
        cid = str(res.get("id") or "")
        if not _JID.match(cid):
            raise QR.RemoteError("the queue answered a check job id %r that is not a plain job id" % cid[:40])
        say(f"mini-finish: check job {cid} queued on {base} ({job['label']}); waiting for it")
        row, end = None, time.time() + timeout
        poll = float(os.environ.get("MINI_FINISH_POLL") or 5)
        while True:
            row = next((j for j in queue_jobs() if str(j.get("id")) == cid), None)
            if row is not None and row.get("status") not in LIVE:
                break
            if time.time() >= end:
                return 124, f"[mini-finish] the check job {cid} did not finish within {timeout}s\n"
            time.sleep(poll)
        res_dir = chk / "result"
        QR.fetch_result(base, headers, cid, res_dir)
        if "--draft-check" in items_flat(items):   # a test-draft check: the verdict file and the kept draft sit in the run dir, not work/
            fetch_draft_files(base, headers, cid, chk)
        out = ""
        try:
            out = (res_dir / "log-tail.txt").read_text(errors="replace")
        except OSError:
            pass
        rc = row.get("exit_code")
        rc = rc if isinstance(rc, int) else (0 if row.get("status") in ("done", "completed", "passed") else 1)
        (chk / "check.log").write_text("$ remote check job %s (runner %s %s)\n" % (cid, RUNNER_NAME, json.dumps(items)) + out)
        why = clear_card(base, headers, cid)   # the check job is ours: its card would otherwise stay on the dashboard
        if why:
            say(f"mini-finish: the check job's card stays ({why}); clear it with: ollama-queue.py clear-card {cid}")
        return rc, out
    except (QR.RemoteError, OSError) as e:
        msg = f"[mini-finish] remote check failed: {e}\n"
        say(msg.rstrip())
        try:
            (chk / "check.log").write_text(msg)
        except OSError:
            pass
        return 125, msg
