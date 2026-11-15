#!/usr/bin/env python3
"""Bridge a completed diagnosis-only dispatch to a gated FIX dispatch.

A diagnosis job ends with a prose writeup and nothing downstream: a human then
hand-writes a fix TASK.md and enqueues it. That hand-step is the bottleneck.

This tool removes the TYPING, not the JUDGMENT. It drafts a proposal from the
diagnosis answer and PARKS it; `--accept` is the single human action that hands
the proposal to `ollama-dispatch-auto`, which already turns a one-line intent
into a model-authored, mutation-relevance-gated harness paused at the human
relevance review. Nothing here re-implements that contract.

WHY IT PARKS AND NEVER AUTO-FIRES
  A diagnosis is the one artifact in this pipeline with ZERO mechanical evidence
  behind it. Every other dispatch is gated by a verify proven both ways (red at
  baseline, green on refimpl, mutation-tested for relevance); a diagnosis job has
  no verify at all -- gate-on-complete.py:job_is_ungateable() bails on
  task_kind=research for exactly that reason. Auto-firing a fix from it would be
  the only place in the system where unverified model output drives code changes
  with no human read. So the fix is DRAFTED automatically and LAUNCHED manually.
  See Claude/Projects/diagnosis-to-fix-autoflow.md for the full design, including
  the v2 condition under which auto-fire becomes defensible (a diagnosis that
  emitted a reproducible failing check has a real verify and rejoins the gate).

EXTRACTION RULE: QUOTE, NEVER INVENT
  Answers are long unstructured prose. A regex cannot compose a spec from them,
  and using a model to do it would stack a second unverified inference on the
  first. So every field is either quoted from the answer or marked `missing` with
  a NEEDS-HUMAN marker -- never guessed. A confident-looking wrong spec is worse
  than a blank one: that is precisely the failure that produced the
  bg-actions-s1-item s4 loop (a spec with no honest defect behind it, retried 11
  times because there was nothing real for the model to write).

Usage:
  ollama-diagnosis-followup.py --scan
  ollama-diagnosis-followup.py --draft  <job_id>
  ollama-diagnosis-followup.py --show   <job_id>
  ollama-diagnosis-followup.py --accept <job_id> [--intent "..."] [--target F] [--dry-run]
  ollama-diagnosis-followup.py --self-test
"""
import argparse
import json
import os
import re
import shlex
import subprocess
import sys
from pathlib import Path
import os as _qc_os, sys as _qc_sys
_qc_dir = _qc_os.path.dirname(_qc_os.path.realpath(__file__))
if _qc_dir not in _qc_sys.path:
    _qc_sys.path.append(_qc_dir)
import queue_config  # noqa: E402  (the queue state wherever the queue runs: load_queue_state)

LOG_DIR = Path(os.path.expanduser("~/bin/ollama-queue-logs"))
DISPATCH_AUTO = Path(os.path.expanduser("~/bin/ollama-dispatch-auto"))

# A label that reads as an answer-job. Mirrors ollama-queue.py's LABEL_RESEARCH_RE
# deliberately -- one definition would be better, but importing that 7700-line
# module for one regex costs more than it saves, and the self-test pins the shapes.
LABEL_DIAG_RE = re.compile(r"(^|[-_])(research|diagnos[ei]s|diagnose|diag)([-_]|$)", re.I)

# `path/to/file.ext:123` or `path/to/file.ext` -- the citation shapes a diagnosis
# actually uses. Restricted to known source extensions so prose like "3.5:1" or a
# version string cannot masquerade as a file citation.
_SRC_EXT = r"(?:py|js|jsx|ts|tsx|swift|go|rs|rb|java|kt|c|h|cc|cpp|sh|sql|json|toml|yaml|yml)"
CITE_RE = re.compile(r"(?<![\w/.-])((?:[\w.-]+/)*[\w.-]+\.%s)(?::(\d+))?\b" % _SRC_EXT)

# A "root cause" section heading, in the shapes models actually emit.
ROOT_HEAD_RE = re.compile(
    r"^\s{0,3}#{1,6}\s*(?:\d+[.)]\s*)?"
    r"(root\s*cause|the\s*cause|cause|the\s*bug|the\s*defect|diagnosis|conclusion)\b.*$",
    re.I | re.M)

# Inline lead-in at the start of a line, bolded or not:
#   "**Root cause:** foo does X"   "Root cause: foo does X"   "- Root cause - foo"
# The bold markers are optional because models emit all three forms freely, and a
# plain "Root cause:" line is the single most common shape in practice.
ROOT_INLINE_RE = re.compile(
    r"^[\s>*+-]*\**\s*(?:root\s*cause|the\s*bug|the\s*defect)\s*\**\s*[:\-—]\s*\**\s*",
    re.I | re.M)

# A repro/failing-check signal. v1 only RECORDS this (for the v2 auto-fire rule);
# it never changes v1 behaviour.
REPRO_RE = re.compile(
    r"(reproduc\w+|repro\b|failing test|red test|assert\w*\s+fail|exits?\s+non-?zero)", re.I)

# Files a diagnosis dispatch creates or reads AS ITS OWN SCAFFOLD. They are never
# the code under repair, but they are cited heavily inside the writeup, so without
# this they outrank the real source file and the draft targets the harness instead
# of the bug. Found live: three of five real answers ranked `t_repro.py` and
# `diagnosis.json` above pins.py / lib.py / dedup.py.
SCAFFOLD_BASENAMES = {
    "t_repro.py", "repro.py", "diagnosis.json", "diagnosis.md", "answer.md",
    "verify.sh", "test_fixture.py", "refimpl.py", "check_literals.py",
    "auto-harness-check.py", "task.md", "auto-task.md",
}

CERTAIN, QUOTED, INFERRED, MISSING = "certain", "quoted", "inferred", "missing"
NEEDS_HUMAN = "NEEDS-HUMAN"


# ---------------------------------------------------------------- job records

QUEUE_STATE = Path(os.path.expanduser("~/bin/ollama-queue-state.json"))


def _record_from_queue_state(job_id, state_path=None):
    """Recover a job's identity from the live queue state.

    Needed because `<id>.done.json` is PRUNED while `<id>.answer.md` never is: of
    the 55 answer sidecars on this machine, the older ones have no completion
    record at all. Keying anything on done.json alone silently misses them (found
    live -- --scan reported 0 of 55). The queue state is the next-best durable
    source for label/repo/cwd."""
    data = queue_config.load_queue_state(state_path or QUEUE_STATE)
    if data is None:
        return None
    jobs = data.get("jobs", data) if isinstance(data, dict) else data
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    if not isinstance(jobs, list):
        return None
    for j in jobs:
        if isinstance(j, dict) and j.get("id") == job_id:
            return {"label": j.get("label"), "repo": j.get("repo") or j.get("cwd"),
                    "cwd": j.get("cwd"), "task_kind": j.get("task_kind"),
                    "task_file": j.get("task_file"), "verify": j.get("verify"),
                    "_source": "queue-state"}
    return None


def _done_record(job_id, log_dir=None, state_path=None):
    """The frozen completion record, else the live queue state, else None."""
    d = Path(log_dir or LOG_DIR)
    p = d / f"{job_id}.done.json"
    if p.is_file():
        try:
            return json.loads(p.read_text())
        except Exception:
            pass
    return _record_from_queue_state(job_id, state_path)


try:
    from docsum_private import is_private_label
except ImportError:  # script copied elsewhere (tests): same definition, kept in step by test_docsum_ui_consumers
    import re as _re_dp

    def is_private_label(label):
        return _re_dp.fullmatch(r"docsum-[0-9a-f]{8}", str(label or "")) is not None


def is_diagnosis_job(rec):
    """True when a completion record describes an answer-job (diagnosis/research).

    Order matters: task_kind is the strongest signal, then the task file's own
    shape, then the label. Fail-safe -- anything unreadable is NOT a diagnosis, so
    a coding job can never be swept into this path by accident."""
    if not isinstance(rec, dict):
        return False
    if is_private_label(rec.get("label")):
        return False            # private documents: never read, proposed on or promoted
    if str(rec.get("task_kind") or "") == "research":
        return True
    try:
        tf = rec.get("task_file")
        if tf and Path(tf).is_file() and Path(tf).stat().st_size < 200_000:
            txt = Path(tf).read_text(errors="replace")
            if re.search(r"\bDIAGNOSIS\.md\b", txt):
                return True
            if re.search(r"\bdiagnos(?:e|is|tic|tics|ing|ed)\b", txt, re.I) and \
               re.search(r"\b(write|writeup|write-up|report|explain|root cause)\b", txt, re.I):
                return True
    except Exception:
        pass
    return bool(LABEL_DIAG_RE.search(str(rec.get("label") or "")))


def answer_path_for(job_id, rec=None, log_dir=None):
    """The durable answer sidecar, preferring the recorded path."""
    d = Path(log_dir or LOG_DIR)
    if rec and rec.get("answer_path"):
        p = Path(rec["answer_path"])
        if p.is_file():
            return p
    p = d / f"{job_id}.answer.md"
    return p if p.is_file() else None


# ---------------------------------------------------------------- extraction

def extract_citations(answer, repo_root=None):
    """File citations in the answer, most-cited first.

    Returns [{path, hits, lines, exists}]. `exists` is resolved against repo_root
    when given -- an extracted path that is not actually a file in the repo is the
    single most common way a prose citation misleads, so it is surfaced, not
    silently dropped."""
    counts, lines = {}, {}
    for m in CITE_RE.finditer(answer or ""):
        p = m.group(1)
        counts[p] = counts.get(p, 0) + 1
        if m.group(2):
            lines.setdefault(p, []).append(int(m.group(2)))
    out = []
    for p, n in counts.items():
        exists = None
        if repo_root:
            exists = (Path(repo_root) / p).is_file()
        out.append({"path": p, "hits": n,
                    "lines": sorted(set(lines.get(p, []))), "exists": exists,
                    "scaffold": Path(p).name.lower() in SCAFFOLD_BASENAMES})
    # Scaffold files sink below every real candidate regardless of hit count; then
    # most-cited first; a path confirmed to exist outranks an equally-cited one
    # that does not; shortest path as a stable tiebreak.
    out.sort(key=lambda r: (r["scaffold"], -r["hits"],
                            r["exists"] is False, len(r["path"])))
    return out


def _paragraph_at(text, pos):
    """From pos to the next blank line (or a new heading/bullet block)."""
    rest = text[pos:]
    m = re.search(r"\n\s*\n|\n\s{0,3}#{1,6}\s", rest)
    return rest[:m.start()] if m else rest


def _first_sentence(text, limit=400):
    t = " ".join((text or "").split())
    if not t:
        return ""
    m = re.search(r"(.+?[.!?])(\s|$)", t)
    s = m.group(1) if m else t
    return s[:limit].rstrip()


def extract_root_cause(answer):
    """(quote, confidence). Verbatim from the answer or empty -- never composed.

    Tries the bolded inline form first (`**Root cause:** ...`), which is tighter
    than a section, then the first non-empty prose line under a root-cause-ish
    heading."""
    if not answer:
        return "", MISSING
    m = ROOT_INLINE_RE.search(answer)
    if m:
        # Take the rest of the PARAGRAPH, not the rest of the line: a root-cause
        # sentence routinely wraps, and line-scoping would hand the fix dispatch a
        # truncated fragment ("...divides before") as its intent.
        s = _first_sentence(_paragraph_at(answer, m.end()))
        if s:
            return s, QUOTED
    hm = ROOT_HEAD_RE.search(answer)
    if hm:
        body = answer[hm.end():]
        for raw in body.splitlines():
            line = raw.strip()
            if not line or line.startswith("#"):
                if line.startswith("#"):
                    break
                continue
            line = re.sub(r"^[-*+]\s+", "", line)
            s = _first_sentence(line)
            if s:
                return s, QUOTED
    return "", MISSING


def extract_requires(answer, target_path, repo_root):
    """Backticked identifiers that actually occur in the target file.

    Membership in the target is the whole point: a literal the model invented, or
    one that lives in a different file, would become a frozen `Must contain` the
    fix can only satisfy by writing the wrong thing. Unverifiable -> return none."""
    if not (answer and target_path and repo_root):
        return []
    f = Path(repo_root) / target_path
    try:
        src = f.read_text(errors="replace")
    except Exception:
        return []
    out = []
    for tok in re.findall(r"`([A-Za-z_][\w.]{2,60})`", answer):
        if tok in out:
            continue
        if re.search(r"\b%s\b" % re.escape(tok), src):
            out.append(tok)
    return out[:5]


def build_proposal(job_id, rec, answer, log_dir=None):
    """Assemble the parked proposal. Pure: no I/O beyond the repo existence checks
    the extractors do, so the self-test can drive it directly."""
    repo = rec.get("repo") or rec.get("cwd") or ""
    cites = extract_citations(answer, repo or None)
    # A scaffold file is NEVER a fix target, even when it is the only thing cited:
    # targeting t_repro.py would point the fix dispatch at the diagnosis's own
    # harness. If nothing else was cited, the target is honestly `missing`.
    usable = [c for c in cites if not c["scaffold"]]
    real = [c for c in usable if c["exists"]] or usable
    target, target_conf = "", MISSING
    if real:
        target = real[0]["path"]
        target_conf = QUOTED if real[0]["exists"] else INFERRED
    root, root_conf = extract_root_cause(answer)
    requires = extract_requires(answer, target if target_conf != MISSING else "", repo)

    fields = {
        "repo": {"value": repo, "confidence": CERTAIN if repo else MISSING},
        "target": {"value": target, "confidence": target_conf},
        "intent": {"value": root, "confidence": root_conf},
        "requires": {"value": requires,
                     "confidence": INFERRED if requires else MISSING},
        "citations": {"value": cites[:10], "confidence": QUOTED if cites else MISSING},
    }
    blockers = [k for k in ("repo", "target", "intent")
                if fields[k]["confidence"] == MISSING]
    return {
        "schema": 1,
        "job_id": job_id,
        "label": rec.get("label"),
        "status": "parked",          # v1 NEVER drafts anything but a parked proposal
        "auto_fire": False,
        "repro_detected": bool(REPRO_RE.search(answer or "")),  # v2 hook only
        "answer_path": str(answer_path_for(job_id, rec, log_dir) or ""),
        "fields": fields,
        "blockers": blockers,
        "needs_human": [f"{NEEDS_HUMAN}: {k} could not be quoted from the diagnosis"
                        for k in blockers],
        "ready_command": ready_command(fields) if not blockers else None,
    }


def ready_command(fields):
    """The exact ollama-dispatch-auto invocation --accept would run."""
    argv = [str(DISPATCH_AUTO),
            "--repo", fields["repo"]["value"],
            "--intent", fields["intent"]["value"],
            "--target", fields["target"]["value"]]
    for lit in fields["requires"]["value"]:
        argv += ["--require", lit]
    return argv


# ---------------------------------------------------------------- commands

def cmd_draft(job_id, log_dir=None, quiet=False):
    d = Path(log_dir or LOG_DIR)
    rec = _done_record(job_id, d)
    if rec is None:
        print(f"ERROR: no completion record for {job_id} (not finished, or pruned)",
              file=sys.stderr)
        return 2
    if not is_diagnosis_job(rec):
        print(f"ERROR: {job_id} ({rec.get('label')}) is not a diagnosis/research job "
              f"-- refusing to draft a fix from a coding job's output", file=sys.stderr)
        return 2
    ap = answer_path_for(job_id, rec, d)
    if not ap:
        print(f"ERROR: {job_id} has no answer sidecar -- nothing to draft from",
              file=sys.stderr)
        return 2
    prop = build_proposal(job_id, rec, ap.read_text(errors="replace"), d)
    out = d / f"{job_id}.followup.json"
    out.write_text(json.dumps(prop, indent=1))
    if not quiet:
        print(f"# drafted (PARKED): {out}")
        render(prop)
    return 0


def render(prop):
    f = prop["fields"]
    print(f"# diagnosis job : {prop['job_id']}  {prop.get('label') or ''}")
    print(f"# answer        : {prop['answer_path']}")
    print(f"# repro in text : {prop['repro_detected']}  (v2 auto-fire hook; v1 always parks)")
    for k in ("repo", "target", "intent"):
        print(f"# {k:<13}: [{f[k]['confidence']}] {f[k]['value'] or '(none)'}")
    if f["requires"]["value"]:
        print(f"# requires     : [{f['requires']['confidence']}] "
              f"{', '.join(f['requires']['value'])}")
    top = f["citations"]["value"][:5]
    if top:
        print("# citations    : " + ", ".join(
            f"{c['path']}{':' + ','.join(map(str, c['lines'])) if c['lines'] else ''}"
            f"({c['hits']}x{'' if c['exists'] is not False else ' MISSING-IN-REPO'})"
            for c in top))
    for n in prop["needs_human"]:
        print(f"# {n}")
    if prop["ready_command"]:
        print("#\n# PARKED -- read the intent line above, then:")
        print(f"#   ollama-diagnosis-followup.py --accept {prop['job_id']}")
        print("# which runs:")
        print("#   " + " ".join(shlex.quote(a) for a in prop["ready_command"]))
    else:
        print("#\n# PARKED and INCOMPLETE -- supply the missing field(s) by hand:")
        print(f"#   ollama-diagnosis-followup.py --accept {prop['job_id']} "
              f"--intent \"...\" --target path/to/file.py")


def cmd_show(job_id, log_dir=None):
    d = Path(log_dir or LOG_DIR)
    p = d / f"{job_id}.followup.json"
    if not p.is_file():
        print(f"ERROR: no parked proposal for {job_id} -- run --draft first",
              file=sys.stderr)
        return 2
    render(json.loads(p.read_text()))
    return 0


def cmd_scan(log_dir=None):
    """Completed diagnosis jobs that have an answer but no proposal yet."""
    # Iterate the ANSWER sidecars, not the completion records: an answer is never
    # pruned, a done.json is. Scanning done.json found 0 of 55 real answer jobs.
    d = Path(log_dir or LOG_DIR)
    n = 0
    for ans in sorted(d.glob("*.answer.md")):
        jid = ans.name[:-len(".answer.md")]
        rec = _done_record(jid, d)
        if rec is None:
            # No record anywhere -- the label is gone, so the only honest signal
            # left is that an answer exists at all. Report it as unclassified
            # rather than either dropping it or assuming it is a diagnosis.
            if not (d / f"{jid}.followup.json").is_file():
                print(f"{jid}  (no job record -- label unknown; --draft will refuse)")
                n += 1
            continue
        if not is_diagnosis_job(rec):
            continue
        if (d / f"{jid}.followup.json").is_file():
            continue
        print(f"{jid}  {rec.get('label')}")
        n += 1
    print(f"# {n} diagnosis job(s) with an answer and no parked proposal")
    return 0


def cmd_accept(job_id, intent=None, target=None, dry_run=False, log_dir=None):
    """THE human action. Runs ollama-dispatch-auto, which applies the full gate."""
    d = Path(log_dir or LOG_DIR)
    p = d / f"{job_id}.followup.json"
    if not p.is_file():
        print(f"ERROR: no parked proposal for {job_id} -- run --draft first",
              file=sys.stderr)
        return 2
    prop = json.loads(p.read_text())
    f = prop["fields"]
    if intent:
        f["intent"] = {"value": intent, "confidence": "human"}
    if target:
        f["target"] = {"value": target, "confidence": "human"}
    missing = [k for k in ("repo", "target", "intent")
               if not f[k]["value"]]
    if missing:
        print(f"ERROR: cannot accept -- {', '.join(missing)} still unset. "
              f"Pass --intent/--target explicitly.", file=sys.stderr)
        return 2
    argv = ready_command(f)
    print("# " + " ".join(shlex.quote(a) for a in argv))
    if dry_run:
        print("# --dry-run: not launched")
        return 0
    rc = subprocess.call(argv)
    prop["status"] = "accepted" if rc == 0 else "accept_failed"
    prop["accepted_command"] = argv
    p.write_text(json.dumps(prop, indent=1))
    return rc


# ---------------------------------------------------------------- self-test

def _self_test():
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got == want:
            print(f"ok: {name}")
        else:
            ok = False
            print(f"FAIL: {name}\n  got  {got!r}\n  want {want!r}")

    # -- identification -----------------------------------------------------
    check("task_kind=research is a diagnosis job",
          is_diagnosis_job({"task_kind": "research"}), True)
    check("a *-diagnose label is a diagnosis job",
          is_diagnosis_job({"label": "bfmr-split-reservation-diagnose"}), True)
    check("a -diag- label is a diagnosis job",
          is_diagnosis_job({"label": "diag-ev-charging"}), True)
    check("an ordinary coding label is NOT a diagnosis job",
          is_diagnosis_job({"label": "auto-author-bg-profile-s3-load-profile"}), False)
    check("a label merely CONTAINING 'diagnostic' text is not swept in",
          is_diagnosis_job({"label": "rt-fix-diagnostics-panel"}), False)
    check("garbage in -> False, never a raise",
          is_diagnosis_job(None), False)

    # -- citations ----------------------------------------------------------
    with tempfile.TemporaryDirectory() as td:
        (Path(td) / "lib").mkdir()
        (Path(td) / "lib" / "split.py").write_text(
            "def allocate_reservation(order):\n    return order.qty\n")
        ans = ("Root cause: `allocate_reservation` in lib/split.py:12 divides before\n"
               "the guard. Also seen at lib/split.py:40 and once in other/util.py.\n")
        cites = extract_citations(ans, td)
        check("most-cited real file ranks first",
              cites[0]["path"], "lib/split.py")
        check("line numbers are collected and deduped",
              cites[0]["lines"], [12, 40])
        check("existence is resolved against the repo",
              (cites[0]["exists"], [c["exists"] for c in cites if c["path"] == "other/util.py"]),
              (True, [False]))
        check("a version-like token is not mistaken for a file",
              extract_citations("we bumped to 3.5:1 ratio", td), [])
        # The diagnosis job's OWN scaffold must never outrank the code under repair,
        # however often the writeup cites it (found live on 3 of 5 real answers).
        scaf = ("see t_repro.py and t_repro.py again, plus diagnosis.json, "
                "diagnosis.json -- the bug is in lib/split.py")
        check("scaffold files sink below a less-cited real source file",
              extract_citations(scaf, td)[0]["path"], "lib/split.py")
        check("scaffold files are flagged as such",
              [c["scaffold"] for c in extract_citations(scaf, td)
               if c["path"] == "t_repro.py"], [True])
        check("a diagnosis citing ONLY scaffold yields no target, not a wrong one",
              build_proposal("s", {"repo": td},
                             "t_repro.py fails")["fields"]["target"]["confidence"],
              MISSING)

        # -- root cause -----------------------------------------------------
        check("bolded inline root cause is quoted verbatim",
              extract_root_cause("**Root cause:** the guard runs after the divide. More."),
              ("the guard runs after the divide.", QUOTED))
        check("a heading-form root cause is quoted",
              extract_root_cause("## Root cause\n\nThe split double-counts refunds.\n")[0],
              "The split double-counts refunds.")
        check("no root-cause section -> missing, never invented",
              extract_root_cause("Some prose with no cause heading at all."),
              ("", MISSING))

        # -- requires -------------------------------------------------------
        check("a backticked identifier present in the target is kept",
              extract_requires(ans, "lib/split.py", td), ["allocate_reservation"])
        check("an identifier absent from the target is dropped, not frozen",
              extract_requires("`totally_invented_symbol` is the problem",
                               "lib/split.py", td), [])

        # -- proposal -------------------------------------------------------
        rec = {"label": "bfmr-split-reservation-diagnose", "repo": td,
               "task_kind": "research"}
        prop = build_proposal("deadbeef", rec, ans)
        check("a complete proposal is PARKED, never auto-fired",
              (prop["status"], prop["auto_fire"]), ("parked", False))
        check("target/intent are taken from the quoted evidence",
              (prop["fields"]["target"]["value"],
               prop["fields"]["target"]["confidence"]),
              ("lib/split.py", QUOTED))
        check("ready_command targets ollama-dispatch-auto with the quoted fields",
              (prop["ready_command"][1:6]),
              ["--repo", td, "--intent",
               "`allocate_reservation` in lib/split.py:12 divides before the guard.",
               "--target"])
        check("a wrapped root-cause sentence is NOT truncated at the line break",
              extract_root_cause("Root cause: the guard runs\nafter the divide. More.")[0],
              "the guard runs after the divide.")
        check("a root cause stops at the paragraph, not at the next section",
              extract_root_cause("Root cause: A happens\n\nB is unrelated.")[0],
              "A happens")
        check("a --require literal is passed through",
              "allocate_reservation" in prop["ready_command"], True)
        check("no blockers on a complete proposal", prop["blockers"], [])

        # the blank-over-wrong rule
        thin = build_proposal("beefdead", rec, "The system seems flaky sometimes.")
        check("an unquotable diagnosis yields NO ready_command",
              thin["ready_command"], None)
        check("...and names its blockers explicitly",
              sorted(thin["blockers"]), ["intent", "target"])
        check("...and emits a NEEDS-HUMAN marker per blocker",
              all(n.startswith(NEEDS_HUMAN) for n in thin["needs_human"])
              and len(thin["needs_human"]) == 2, True)
        check("repro detection is recorded but does not change v1 behaviour",
              (build_proposal("r", rec, "a failing test reproduces it: " + ans)["repro_detected"],
               build_proposal("r", rec, "a failing test reproduces it: " + ans)["status"]),
              (True, "parked"))

        # -- draft/accept round-trip ---------------------------------------
        ld = Path(td) / "logs"
        ld.mkdir()
        (ld / "abc123.done.json").write_text(json.dumps(
            {"label": "x-diagnose", "repo": td, "task_kind": "research"}))
        (ld / "abc123.answer.md").write_text(ans)
        check("draft writes a parked sidecar", cmd_draft("abc123", ld, quiet=True), 0)
        saved = json.loads((ld / "abc123.followup.json").read_text())
        check("the saved sidecar is parked", saved["status"], "parked")
        check("accept --dry-run does not launch",
              cmd_accept("abc123", dry_run=True, log_dir=ld), 0)
        check("...and leaves the proposal parked",
              json.loads((ld / "abc123.followup.json").read_text())["status"], "parked")
        (ld / "cod.done.json").write_text(json.dumps(
            {"label": "auto-author-bg-profile-s3", "repo": td}))
        (ld / "cod.answer.md").write_text(ans)
        check("draft REFUSES a non-diagnosis job",
              cmd_draft("cod", ld, quiet=True), 2)
        check("accept refuses an incomplete proposal without explicit overrides",
              (cmd_draft("thin1", ld, quiet=True) if False else
               (lambda: ((ld / "thin1.done.json").write_text(json.dumps(
                   {"label": "y-diagnose", "repo": td, "task_kind": "research"})),
                   (ld / "thin1.answer.md").write_text("vague prose"),
                   cmd_draft("thin1", ld, quiet=True),
                   cmd_accept("thin1", log_dir=ld))[-1])()), 2)

    print("SELF_TEST_OK" if ok else "SELF_TEST_FAILED")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser(
        description="Bridge a completed diagnosis dispatch to a PARKED fix proposal.")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--scan", action="store_true")
    g.add_argument("--draft", metavar="JOB_ID")
    g.add_argument("--show", metavar="JOB_ID")
    g.add_argument("--accept", metavar="JOB_ID")
    g.add_argument("--self-test", action="store_true", dest="self_test")
    ap.add_argument("--intent", help="override the drafted intent (human's words win)")
    ap.add_argument("--target", help="override the drafted target file")
    ap.add_argument("--dry-run", action="store_true",
                    help="with --accept: print the command, do not launch")
    a = ap.parse_args()
    if a.self_test:
        return _self_test()
    if a.scan:
        return cmd_scan()
    if a.draft:
        return cmd_draft(a.draft)
    if a.show:
        return cmd_show(a.show)
    return cmd_accept(a.accept, a.intent, a.target, a.dry_run)


if __name__ == "__main__":
    sys.exit(main())
