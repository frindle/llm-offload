"""mini_validate_remote -- mini-validate (V1-V8) and mini-submit's C5/C6 as a validate job on a queue on another machine.

When queue_config.queue_is_remote() the spec's tests must not run on this machine. remote_validate() packs the spec like any job
(queue_remote.build_package, the runner flag --validate-spec through the runner_args route), waits for the queue's verdict, and on a
PASS writes VALIDATION.md next to the spec so mini-submit's C3 (a pure hash comparison, `mini-validate --check-fresh`) holds:

  * the text is the queue's, but its "## Fingerprint" block is recomputed here from the local spec / frozen tests / task files (the
    queue hashed its own unpacked copies, whose paths differ);
  * spec fields mini-validate fills in (red_ok, baseline_green, fn_scope, target_fn, fix_steps, fix_wall) come back in the result
    and are written into the local spec first, as a local mini-validate run writes them;
  * a line `- validated on the queue: job <id>` marks the file. mini-submit then skips its own C5/C6 (the job ran them).
The `- runner: ... sha=` line stays the QUEUE's runner sha: when it differs from this machine's runner, C3 says so (the queue image
and ~/bin are out of step), which is the right answer. Nothing fetched from the queue is executed here.
"""
import importlib.machinery
import importlib.util
import json
import os
import re
import tempfile
import time
from pathlib import Path

import queue_config
import queue_remote as QR
import mini_finish_remote as MFR

HERE = Path(os.path.realpath(__file__)).parent
RUNNER_NAME = "mini-swe-runner.py"
MARK = "- validated on the queue: job "
FILL_KEYS = ("red_ok", "baseline_green", "fn_scope", "target_fn", "fix_steps", "fix_wall")


def marked_job(vfile):
    """The queue job id a VALIDATION.md was produced by (see MARK), or None."""
    try:
        m = re.search(r"^" + re.escape(MARK) + r"([0-9a-f]{6,64})\s*$", Path(vfile).read_text(), re.M)
    except OSError:
        return None
    return m.group(1) if m else None


def _load_validate():
    path = Path(os.environ.get("MINI_VALIDATE") or HERE / "mini-validate")
    loader = importlib.machinery.SourceFileLoader("mini_validate_mod", str(path))
    mod = importlib.util.module_from_spec(importlib.util.spec_from_loader("mini_validate_mod", loader))
    loader.exec_module(mod)
    return mod


def apply_fills(spec, filled):
    """Copy the fill keys mini-validate wrote on the queue (in `filled`, the spec it validated) into `spec`; returns the changes."""
    changes = []
    pairs = [(spec, filled)]
    if isinstance(spec.get("steps"), list) and isinstance(filled.get("steps"), list):
        pairs += [(a, b) for a, b in zip(spec["steps"], filled["steps"]) if isinstance(a, dict) and isinstance(b, dict)]
    for tgt, src in pairs:
        for k in FILL_KEYS:
            if k in src and tgt.get(k) != src[k]:
                changes.append((k, tgt.get(k), src[k]))
                tgt[k] = src[k]
    return changes


def write_validation(spec_path, remote_text, jid):
    """Local VALIDATION.md from the queue's: fingerprint recomputed for the local files, marker added."""
    spec_path = Path(spec_path)
    mod = _load_validate()
    R = mod.load_runner()
    fp = mod.fingerprint(spec_path, json.loads(spec_path.read_text()), R)
    block = "".join("- %s: %s\n" % (k, v) for k, v in fp.items())
    text, n = re.subn(r"(## Fingerprint[^\n]*\n\n)(?:- [^\n]*\n)+", lambda m: m.group(1) + block, remote_text, count=1)
    if not n or "\nRESULT: PASS" not in text:
        raise QR.RemoteError("the queue's VALIDATION.md has no fingerprint block / PASS line")
    text = re.sub(r"^(- runner: [^\n]*\n)", lambda m: m.group(1) + MARK + jid + "\n", text, count=1, flags=re.M)
    if MARK not in text:
        raise QR.RemoteError("the queue's VALIDATION.md has no runner line")
    (spec_path.parent / "VALIDATION.md").write_text(text)


def snapshot(spec_path):
    """What a validate job is given: the spec's text and mini-validate's fingerprint of it (frozen tests, task files). Taken
    before the upload and again after the job, so a file changed during the wait is never covered by the queue's PASS."""
    spec_path = Path(spec_path)
    text = spec_path.read_text()
    mod = _load_validate()
    return text, mod.fingerprint(spec_path, json.loads(text), mod.load_runner())


def remote_validate(spec_path, model, label, say=print, timeout=7200):
    """Run the validate job. Returns (ok, jid, output): ok only when the queue ran V1-V8, C5 and C6 green and VALIDATION.md was
    written next to the spec. On failure output holds the reason / the job's log tail. Never raises for a queue problem."""
    spec_path = Path(spec_path)
    jid = ""
    try:
        spec = json.loads(spec_path.read_text())
        QR.check_paths(spec, spec_path.parent)
        before = snapshot(spec_path)
        job = {"host": "auto", "task_kind": "coding", "model": str(model or "check"),
               "label": ("%s-val-%s" % (label, time.strftime("%H%M%S")))[:60], "allow_duplicate_label": True}
        base, _tok = queue_config.queue_api()
        headers = queue_config.queue_api_headers()
        with tempfile.TemporaryDirectory(prefix="mini-validate-") as td:
            pkg = Path(td) / "package.tar"
            QR.build_package(spec_path, job, RUNNER_NAME, pkg, runner_args=[["--validate-spec"]])
            res = QR.upload(base, headers, pkg)
            jid = str(res.get("id") or "")
            if not MFR._JID.match(jid):
                raise QR.RemoteError("the queue answered a validate job id %r that is not a plain job id" % jid[:40])
            say("mini-validate: validate job %s queued on %s (%s); waiting for it" % (jid, base, job["label"]))
            row, end = None, time.time() + timeout
            poll = float(os.environ.get("MINI_FINISH_POLL") or 5)
            while True:
                row = next((j for j in MFR.queue_jobs() if str(j.get("id")) == jid), None)
                if row is not None and row.get("status") not in MFR.LIVE:
                    break
                if time.time() >= end:
                    return False, jid, "the validate job %s did not finish within %ss" % (jid, timeout)
                time.sleep(poll)
            out_dir = Path(td) / "result"
            QR.fetch_result(base, headers, jid, out_dir)
            log = ""
            try:
                log = (out_dir / "log-tail.txt").read_text(errors="replace")
            except OSError:
                pass
            rc = row.get("exit_code")
            rc = rc if isinstance(rc, int) else (0 if row.get("status") in ("done", "completed", "passed") else 1)
            vf = out_dir / "work" / "VALIDATION.md"
            if rc != 0 or not vf.is_file() or "\nRESULT: PASS" not in vf.read_text(errors="replace"):
                MFR.clear_card(base, headers, jid)
                return False, jid, "validate job %s failed (rc %s):\n%s" % (jid, rc, log[-1500:])
            if snapshot(spec_path) != before:
                MFR.clear_card(base, headers, jid)
                return False, jid, ("the spec or one of its frozen test / task files changed while validate job %s ran; the "
                                    "queue validated the old copy, so validate again" % jid)
            filled = out_dir / "work" / "validated-spec.json"
            if filled.is_file():
                try:
                    fs = json.loads(filled.read_text())
                    if isinstance(fs, dict) and apply_fills(spec, fs):
                        spec_path.write_text(json.dumps(spec, indent=1) + "\n")
                        say("mini-validate: filled spec fields from the queue's run (%s)" % ", ".join(k for k in FILL_KEYS if k in fs))
                except (OSError, ValueError):
                    pass
            write_validation(spec_path, vf.read_text(errors="replace"), jid)
            MFR.clear_card(base, headers, jid)
            return True, jid, log[-600:]
    except (QR.RemoteError, OSError, ValueError) as e:
        return False, jid, "remote validate failed: %s" % e
