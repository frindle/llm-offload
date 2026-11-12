#!/usr/bin/env python3
"""Sign-off gate: track which finished jobs still need a reviewer's approval.

Some changes must not be merged until a named reviewer signs off — harness edits,
lockout-critical scripts, prod-affecting work. Today that is done by hand and
nothing records it, so a job that needs sign-off looks exactly like one that
doesn't. This tool builds the state and the surfacing.

Usage:
  signoff.py --evaluate <job-id> --diff <path> [--task-file <path>] \
             # decides whether a job requires sign-off, and records it

  signoff.py --verdict approve|changes|reject <job-id> [--conditions "..." ...] \
             # records a human's decision

  signoff.py --status <job-id>     # prints that job's entry as JSON
  signoff.py --json                # prints all entries as JSON
  signoff.py --agreement [--json]  # SHADOW-mode calibration: how often the
                                   # harness's decision matched the human's

AUTONOMY, IN THREE LAYERS (2026-09-03)
--------------------------------------
auto_decide() is the only place a job can sign itself off, and it is built to
be conservative BY CONSTRUCTION rather than by tuning:

  1. EVIDENCE the harness measured (gate pass, verify red-at-baseline, verify
     non-gameable, clean launch baseline, scope clean). Absent evidence blocks
     and says so -- "we never measured it" is never read as "it was fine".
  2. RELEVANCE of the verify, from the preflight LEDGER (verify-relevance.py
     via ollama-dispatch-preflight). Both-ways proves a verify discriminates;
     mutation relevance proves it tests the property rather than a proxy. A
     verify that let property-breaking mutants of the reference impl pass
     cannot certify a model's fix, so it cannot auto-approve. Read from the
     ledger OUTSIDE the worktree, and the scaffold digests are re-checked
     against the finished tree, so a job cannot edit its way to approval.
  3. CHANGE CLASS. Only a bounded, single-site, non-security-adjacent diff
     is in the auto-approve domain at all. Multi-file, new-file, many-hunk,
     or anything whose path or content touches auth/secrets/money/shell/
     schema stays human-gated regardless of how good its evidence is.

And a MODE. `auto_approve.mode` is "shadow" (default when absent) or "live".
In shadow the decision is computed and RECORDED next to the human's verdict
but never acted on; --agreement then reports how often the two agreed, and
-- the number that matters -- how often the harness would have approved
something the human sent back. Go live only after that number has stayed at
zero over real dispatches. A missing mode key means shadow: the failure mode
to design against is a config typo silently going live.
"""
import fnmatch
import argparse, json, os, sys, time
from pathlib import Path


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p: Path, obj):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, indent=1, sort_keys=True))


def signoff_file():
    """Return the path to signoff.json, respecting SIGNOFF_DIR if set."""
    if "SIGNOFF_DIR" in os.environ:
        return Path(os.environ["SIGNOFF_DIR"]) / "signoff.json"
    else:
        return Path(__file__).parent / "signoff.json"


def rules_file():
    """signoff-rules.json, overridable for tests via SIGNOFF_RULES."""
    if "SIGNOFF_RULES" in os.environ:
        return Path(os.environ["SIGNOFF_RULES"])
    return Path(__file__).parent / "signoff-rules.json"


def load_rules():
    return _load(rules_file(), {})


def ledger_dir():
    """Where ollama-dispatch-preflight writes its per-worktree ledger. The
    same env var and default as the gate; two spellings of one path drift."""
    return Path(os.environ.get(
        "OLLAMA_PREFLIGHT_LEDGER",
        Path.home() / "bin" / "ollama-queue-logs" / "preflight-ledger"))


def ledger_entry_for(job_cwd):
    """The preflight ledger entry for a worktree path, or None."""
    if not job_cwd:
        return None
    import hashlib
    key = hashlib.sha256(str(Path(job_cwd).resolve()).encode()).hexdigest()[:16]
    f = ledger_dir() / f"{key}.json"
    if not f.exists():
        return None
    return _load(f, None)


def _sha256_file(p):
    import hashlib
    try:
        return hashlib.sha256(Path(p).read_bytes()).hexdigest()
    except Exception:
        return None


def parse_diff(diff_text):
    """Structure of a unified diff: files, hunks per file, new/deleted files,
    changed lines. One parser for every consumer in this file."""
    import re
    files, hunks, new_files, deleted, changed = set(), {}, set(), set(), []
    cur = None
    for ln in diff_text.splitlines():
        if ln.startswith("--- "):
            if ln[4:].strip() == "/dev/null":
                cur_old_null = True
            else:
                cur_old_null = False
            continue
        if ln.startswith("+++ "):
            name = ln[4:].strip()
            if name.startswith("b/"):
                name = name[2:]
            if name == "/dev/null":
                deleted.add(cur or "?")
                cur = None
                continue
            cur = name
            files.add(name)
            hunks.setdefault(name, 0)
            if cur_old_null:
                new_files.add(name)
            continue
        if cur is None:
            continue
        if ln.startswith("@@"):
            hunks[cur] += 1
        elif ln.startswith(("+", "-")):
            changed.append(ln)
    return {"files": sorted(files), "hunks": hunks, "new_files": sorted(new_files),
            "deleted_files": sorted(deleted), "changed_lines": changed,
            "diff_lines": len(changed)}


def classify_change(diff_text, rules):
    """Which change class is this diff in, and why.

    Returns (class_name, reasons). Only "bounded_single_site" is auto-
    approvable. Everything else is "human_gated" with the reasons that put it
    there. The thresholds are policy (signoff-rules.json auto_approve.
    safe_classes) and every key there is CONSUMED here -- a declared rule that
    nothing reads is worse than none, because the policy report renders it as
    coverage.

    Security-adjacent is decided on TWO surfaces: the file PATH (an auth
    route, a migration, a payout handler) and the CHANGED LINES, added AND
    removed -- removing an auth check is worse than adding one, and a diff
    that touches "password" anywhere is not a mechanical change whatever its
    size. The pattern list is policy too (auto_approve.security_adjacent_
    patterns), and it errs toward matching: a false "security-adjacent" costs
    a human a look, a false "mechanical" auto-approves a credential change.
    """
    import re
    policy = (rules.get("auto_approve") or {})
    classes = policy.get("safe_classes") or {}
    spec = classes.get("bounded_single_site") or {}
    pats = policy.get("security_adjacent_patterns") or []
    d = parse_diff(diff_text)
    reasons = []
    if not spec:
        return "human_gated", ["no safe_classes.bounded_single_site declared in "
                               "signoff-rules.json -- the auto-approve domain is "
                               "undefined, so it is empty"]
    if not d["files"]:
        reasons.append("diff names no files")
    mf = spec.get("max_files", 1)
    if len(d["files"]) > mf:
        reasons.append(f"{len(d['files'])} files changed; a single-site change "
                       f"touches at most {mf}")
    if d["new_files"] and spec.get("no_new_files", True):
        reasons.append(f"creates new file(s): {', '.join(d['new_files'][:3])} -- "
                       f"new code is not a bounded edit")
    if d["deleted_files"]:
        reasons.append(f"deletes file(s): {', '.join(d['deleted_files'][:3])}")
    mh = spec.get("max_hunks", 3)
    total_hunks = sum(d["hunks"].values())
    if total_hunks > mh:
        reasons.append(f"{total_hunks} hunks; a single-site change has at most {mh}")
    ml = spec.get("max_diff_lines", 80)
    if d["diff_lines"] > ml:
        reasons.append(f"{d['diff_lines']} changed lines; cap for the safe class "
                       f"is {ml}")
    hits = set()
    for pat in pats:
        try:
            rx = re.compile(pat, re.I)
        except re.error:
            reasons.append(f"security pattern {pat!r} does not compile -- "
                           f"treating as a hit (fail closed)")
            hits.add(pat)
            continue
        for f in d["files"]:
            if rx.search(f):
                hits.add(f"path {f!r} ~ /{pat}/")
        for ln in d["changed_lines"]:
            if rx.search(ln[1:]):
                hits.add(f"line {ln.strip()[:60]!r} ~ /{pat}/")
                break
    if hits:
        reasons.append("security-adjacent: " + "; ".join(sorted(hits)[:4]))
    if reasons:
        return "human_gated", reasons
    return "bounded_single_site", [
        f"{len(d['files'])} file, {total_hunks} hunk(s), {d['diff_lines']} "
        f"changed line(s), no new/deleted files, no security-adjacent path or "
        f"line"]


def relevance_evidence(gate, rules):
    """Relevance conditions for auto_decide. Returns (fail_reasons, ok_notes).

    Three-valued like everything else here: relevant / low / never measured,
    and never-measured blocks with its own words.

    TWO SOURCES, and BOTH must agree when both exist:
      gate-applied  gate-on-complete measured the verify against the MODEL'S
                    actual diff, in the finished tree (gate["verify_relevance"]).
                    This is evidence about the code being signed off, so it is
                    the primary source.
      preflight     the ledger (outside the tree, by the gate record's job_cwd)
                    measured it against the REFERENCE impl before launch and
                    pinned the scaffold digests, which are re-hashed against the
                    finished tree: the verify that RAN must be the verify that
                    was MEASURED, or the measurement certifies nothing.
    A LOW from either blocks: a verify weak against the refimpl and a verify
    that does not constrain what the model actually wrote are both a verify
    that cannot certify this job. Neither measured -> NEVER MEASURED, hold.
    """
    policy = (rules.get("auto_approve") or {})
    rel = policy.get("relevance") or {}
    min_score = rel.get("min_score", 0.8)
    max_surv = rel.get("max_survivors", 3)
    fail, notes = [], []

    def _judge(vr, label):
        """Apply the floor and the survivor cap to one measurement."""
        v = vr.get("verdict")
        if v != "relevant":
            surv = "; ".join(f"{m.get('file')}:{m.get('line')} {m.get('mutation')}"
                             for m in (vr.get("survivors") or [])[:3])
            fail.append(f"{label}: verify relevance is {v!r} (score {vr.get('score')}): "
                        + (vr.get("reason") or "") + (f" [{surv}]" if surv else ""))
            return False
        sc = vr.get("score")
        if sc is None or sc < min_score:
            fail.append(f"{label}: verify relevance score {sc} is under the "
                        f"auto-approve floor {min_score}")
        n_surv = len(vr.get("survivors") or [])
        if n_surv > max_surv:
            fail.append(f"{label}: {n_surv} property-breaking mutants survived the "
                        f"verify; auto-approve allows at most {max_surv}")
        return True

    gate_vr = gate.get("verify_relevance")
    gate_measured = (isinstance(gate_vr, dict)
                     and gate_vr.get("verdict") in ("relevant", "low"))
    cwd = gate.get("job_cwd")
    entry = ledger_entry_for(cwd) if cwd else None
    ledger_vr = (entry or {}).get("verify_relevance")
    ledger_measured = (isinstance(ledger_vr, dict)
                       and ledger_vr.get("verdict") in ("relevant", "low"))

    if not gate_measured and not ledger_measured:
        why = []
        if isinstance(gate_vr, dict):
            why.append(f"gate-applied is {gate_vr.get('verdict')!r}: "
                       f"{gate_vr.get('reason') or ''}")
        else:
            why.append("gate record has no verify_relevance block")
        if not cwd:
            why.append("gate record has no job_cwd, so the preflight ledger "
                       "cannot be located")
        elif not entry:
            why.append(f"no preflight ledger entry for {cwd}")
        elif not isinstance(ledger_vr, dict):
            why.append("preflight ledger has no verify_relevance block (waived "
                       "or could not run)")
        else:
            why.append(f"preflight is {ledger_vr.get('verdict')!r}: "
                       f"{ledger_vr.get('reason') or ''}")
        fail.append("verify relevance was never measured for this job, so it is "
                    "unproven -- " + "; ".join(why)
                    + ". ABSENT IS UNMEASURED, not relevant.")
        return fail, notes

    if gate_measured:
        _judge(gate_vr, "gate-applied")
    if ledger_measured and _judge(ledger_vr, "preflight"):
        # The verify that ran must be the verify that was measured.
        digests = entry.get("scaffold_sha256") or {}
        if not digests:
            fail.append("ledger carries no scaffold digests -- cannot confirm the "
                        "gated verify is the one the job ran")
        elif not Path(cwd).is_dir():
            fail.append(f"worktree {cwd} is gone -- cannot confirm the gated verify "
                        f"is the one the job ran")
        else:
            # Skip the __unhashable__ sentinel: preflight's scaffold_digests
            # stores it as a LIST of files it could NOT hash, not a digest, so
            # _sha256_file(cwd/"__unhashable__") is None != list and it would
            # spuriously read as "scaffold changed" for any gate with an
            # unhashable scaffold file (LOW14).
            drift = [name for name, h in digests.items()
                     if name != "__unhashable__"
                     and _sha256_file(Path(cwd) / name) != h]
            if drift:
                fail.append(f"scaffold file(s) changed since the gate measured them: "
                            f"{', '.join(drift)} -- the relevance evidence is for a "
                            f"different verify")
    if not fail:
        parts = []
        if gate_measured:
            parts.append(f"gate-applied score {gate_vr.get('score')}, "
                         f"{len(gate_vr.get('survivors') or [])} survivor(s) "
                         f"on the model's diff")
        if ledger_measured:
            parts.append(f"preflight score {ledger_vr.get('score')}, "
                         f"{len(ledger_vr.get('survivors') or [])} survivor(s), "
                         f"scaffold digests match the gated tree")
        notes.append("verify relevance proven: " + "; ".join(parts))
    return fail, notes


def load_signoffs():
    """Load the current signoff state."""
    return _load(signoff_file(), {})


def save_signoffs(signoffs):
    """Save the current signoff state."""
    _save(signoff_file(), signoffs)


def read_task_file(task_file_path):
    """Read and parse a task file, returning its content as text."""
    if not task_file_path:
        return ""
    try:
        return Path(task_file_path).expanduser().read_text()
    except Exception:
        return ""


def get_basename(file_path):
    """Get the basename of a file path."""
    return Path(file_path).name


def check_requires_signoff(task_content, diff_files_list):
    """Check if a job requires sign-off based on task content or diff files.
    
    Returns (required: bool, reviewer: str, reason: str)
    """
    # Check for explicit requirement in task file
    lines = task_content.split('\n')
    for line in lines:
        if line.startswith('## Requires sign-off:'):
            parts = line[23:].strip().split(' — ')
            if len(parts) >= 2:
                return True, parts[0].strip(), ' '.join(parts[1:]).strip()
            elif len(parts) == 1:
                return True, parts[0].strip(), "No reason provided"
    
    # Check for files in diff that match signoff-rules.json.
    #
    # THREE matchers, and until 2026-09-02 only the first existed:
    #
    #  basenames      exact basename. Matched on BASENAME, not path, because
    #                 dispatches run in scratch worktrees -- arr-webhook.py sits
    #                 under eight different directories on this machine and is
    #                 the same risk-bearing file in every one.
    #
    #  basename_globs was DECLARED in signoff-rules.json, REPORTED by
    #                 --policy-check as "sign-off globs", and consulted by
    #                 NOTHING. Only never_auto_globs was ever wired (in
    #                 auto_decide). So `guided-piv*` -- the YubiKey PIV work,
    #                 about as security-critical as anything here -- never
    #                 triggered a sign-off requirement at all, while the policy
    #                 report said it did. Same family as the auto_decide key-name
    #                 bug and the launch-baseline collapse: a safety rule that is
    #                 inert while reading as active is worse than an absent one.
    #
    #  path_globs     matched against the FULL path. Needed because the highest-
    #                 risk product code cannot be named by basename: a Next.js
    #                 payout handler is `app/api/buyinggroup/order-payouts/
    #                 route.ts`, whose basename is `route.ts` -- adding that as a
    #                 basename would fire on every route in every app, which is
    #                 the crying-wolf failure this gate exists to avoid.
    #                 fnmatch's `*` crosses `/`, so `*payout*` is a path rule.
    # load_rules(), not a second spelling of the path: this one ignored the
    # SIGNOFF_RULES override every other reader honours, so a canary's rules
    # never reached the requires-sign-off decision (found by canary section J).
    rules = load_rules()
    basenames = set(rules.get("basenames", []))
    bglobs = rules.get("basename_globs") or []
    pglobs = rules.get("path_globs") or []
    reviewer = rules.get("default_reviewer", "owner")  # NOT "fable": a
        # hardcoded fallback silently reinstates a retired signer if the
        # key is ever missing or renamed, which is the same "reads as
        # configured when it is not" failure as an inert policy rule.

    for file_path in diff_files_list:
        basename = get_basename(file_path)
        if basename in basenames:
            return True, reviewer, f"File '{basename}' requires sign-off"
        for g in bglobs:
            if fnmatch.fnmatch(basename, g):
                return True, reviewer, (f"File '{basename}' matches sign-off "
                                        f"glob '{g}' and requires sign-off")
        norm = str(file_path).replace(os.sep, "/")
        for g in pglobs:
            if fnmatch.fnmatch(norm, g):
                return True, reviewer, (f"Path '{norm}' matches sign-off path "
                                        f"glob '{g}' and requires sign-off")

    return False, "", ""


def evaluate_job(job_id, diff_path, task_file=None):
    """Evaluate whether a job requires sign-off and record it."""
    # Read the diff
    try:
        diff_content = Path(diff_path).read_text()
    except Exception as e:
        print(f"[signoff] failed to read diff: {e}")
        return 1
    
    # Parse diff to get changed files
    import re
    diff_files_list = sorted({m.group(1) for m in re.finditer(r"^\+\+\+ b/(.+)$", diff_content, re.M)
                              if m.group(1) != "/dev/null"})
    
    # Read task file content
    task_content = read_task_file(task_file)
    
    # Check if sign-off is required
    required, reviewer, reason = check_requires_signoff(task_content, diff_files_list)
    
    # Load current signoffs
    signoffs = load_signoffs()
    
    # Record the result
    signoffs[job_id] = {
        "required": required,
        "reviewer": reviewer,
        "reason": reason,
        "verdict": None,
        "conditions": [],
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    }
    
    save_signoffs(signoffs)
    print(f"[signoff] job {job_id} requires sign-off: {required}")
    if required:
        print(f"  reviewer: {reviewer}")
        print(f"  reason: {reason}")
    return 0


def auto_decide(job_id, gate_path, diff_path=None):
    """Decide, from DECIDABLE evidence only, whether a job can sign itself off.

    Deliberately contains no model opinion. "The reviewer model said it looked
    fine" is the same vacuous acceptance rule as "the repro fired" -- satisfiable
    without the property it claims to establish. Every condition below is
    something the harness MEASURED. The review model's own verdict is used only
    as a veto (a FAIL blocks) and as proof the review ran, never as the reason to
    approve.

    The load-bearing condition is verify_failed_at_baseline. A verify that was
    already green before the model touched anything proves nothing about the
    diff, so a green verify is evidence only when it was PROVEN red at the start.
    Without it this whole mechanism would auto-approve on a vacuous verify -- the
    exact failure that let three resell dispatches through in August.

    never_auto is checked FIRST and no amount of evidence overrides it: those
    files ARE the gate. A change weakening verify-quality.py would otherwise
    approve itself, and every change after it.

    Returns (decision, reasons); decision is "auto-approve" or "human".
    """
    rules = load_rules()
    policy = rules.get("auto_approve") or {}
    gate = _load(Path(gate_path), {}) if gate_path else {}

    diff_files, diff_lines, diff_txt = [], None, ""
    if diff_path:
        try:
            import re as _re
            txt = Path(diff_path).read_text()
            diff_txt = txt
            diff_files = sorted({m.group(1) for m in
                                 _re.finditer(r"^\+\+\+ b/(.+)$", txt, _re.M)
                                 if m.group(1) != "/dev/null"})
            diff_lines = sum(1 for ln in txt.splitlines()
                             if (ln.startswith("+") or ln.startswith("-"))
                             and not ln.startswith(("+++", "---")))
        except Exception:
            pass

    if not policy.get("enabled"):
        return "human", ["auto-approve is disabled in signoff-rules.json"]

    # --- the carve-out, before any evidence is considered ---
    import fnmatch as _fn
    never = set(rules.get("never_auto") or [])
    globs = rules.get("never_auto_globs") or []
    bases = {get_basename(f) for f in diff_files}
    hit = sorted((bases & never) |
                 {b for b in bases for g in globs if _fn.fnmatch(b, g)})
    if hit:
        return "human", [f"touches gate machinery ({', '.join(hit)}) -- never "
                         f"auto-approvable, it would be approving its own gate"]

    # KEY NAMES ARE VERIFIED AGAINST REAL RECORDS, not guessed. The first version
    # of this function read verify_exit, verify_quality.gameable and
    # scope_violations -- none of which exist. Surveying 85 real .gate.json files
    # showed the actual names are verify_exit_reported and verify_quality_exit,
    # and that scope findings live in issues[] with source='scope'. Two checks
    # therefore blocked every job for the wrong reason, and two -- gameable
    # verify, scope violation -- were SILENTLY INERT, because `gate.get(<wrong
    # key>)` is None and None is falsy. An inert safety check is worse than none:
    # it reads as a check that passed.
    #
    # Hence MISSING is its own outcome below. A condition whose evidence is
    # absent blocks and SAYS the evidence was absent, so "we never measured it"
    # can never be mistaken for "we measured it and it was fine".
    fail = []
    MISSING = object()

    def ev(key):
        return gate.get(key, MISSING)

    if gate.get("verdict") != "pass":
        fail.append(f"gate verdict is {gate.get('verdict')!r}, not 'pass'")
    # `counts` ABSENT is not "zero code-high findings". The old form was
    #     counts = gate.get("counts") or {}
    #     if counts.get("code_high"):
    # so a record with no counts key read as measured-and-clean. I had assumed
    # co-occurrence covered it -- across 88 real gate records counts and issues
    # are always both present or both absent, and the adjacent `issues is None`
    # check blocks the both-absent case. But co-occurrence is itself a proxy, and
    # it only holds for BOTH: demonstrated with issues=[] present and counts
    # absent, auto_decide returned "auto-approve" with the reason "gate pass, no
    # code findings" while code_high had never been measured. A live false
    # accept, not a latent one.
    _counts = ev("counts")
    if _counts is MISSING or not isinstance(_counts, dict):
        fail.append("gate record has no counts block -- code-high findings were "
                    "never tallied, so 'no code findings' cannot be asserted. "
                    "ABSENT IS UNMEASURED, NOT ZERO.")
        _counts = {}
    if _counts.get("code_high"):
        fail.append(f"{_counts['code_high']} code-high finding(s)")
    issues = gate.get("issues")
    if issues is None:
        fail.append("no issues list in the gate record -- cannot confirm a clean review")
    else:
        if any((i or {}).get("category") == "code" for i in issues):
            fail.append("gate recorded code-category issue(s)")
        # scope-check findings are issues with source='scope', not a top-level key
        scope = [i for i in issues if (i or {}).get("source") == "scope"]
        if scope:
            fail.append(f"scope-check flagged {len(scope)} finding(s): "
                        f"files the task never named")
    # ...and an ABSENCE of scope findings is only evidence if scope-check ran.
    # gate.py records a not_checked entry when it did not; without consulting
    # that, "no scope issues in the list" is indistinguishable from "the checker
    # crashed", and the second one was reading as clean.
    if any(str(n).startswith("scope-check") for n in (gate.get("not_checked") or [])):
        fail.append("scope-check did not run (see gate not_checked) -- files the "
                    "task never named could not be ruled out. Absent findings from "
                    "a checker that never ran are not a clean result.")

    ve = ev("verify_exit_reported")
    if ve is MISSING:
        fail.append("gate record has no verify_exit_reported -- the job's own "
                    "verify result was never recorded")
    elif ve != 0:
        fail.append(f"the job's own verify exit was {ve!r}, not 0")

    # THREE-VALUED, not boolean: true / false / null, where null means the
    # baseline never ran. `None` must NOT collapse into falsy here -- that would
    # report "the verify was not proven failing at baseline", i.e. "we measured
    # it and it was green", when nothing was measured at all. The exact inversion
    # MISSING exists to prevent, and my own job_facts change introduced it:
    # j.get(...) yields None for a job that predates the field, and setdefault
    # then writes the key as None, so it is PRESENT-but-unknown rather than
    # absent. Flagged by the preflight session, whose state file is three-valued
    # for the same reason.
    vfb = ev("verify_failed_at_baseline")
    if vfb is MISSING or vfb is None:
        fail.append("gate record does not say whether the verify failed at "
                    "baseline (absent or null -- the pre-flight never ran, or its "
                    "result was not plumbed through). Without it a green verify "
                    "proves nothing about the diff. This is NOT the same as the "
                    "baseline having passed.")
    elif not vfb:
        fail.append("verify was NOT proven failing at baseline -- a green verify "
                    "cannot show the diff did anything")

    vq = ev("verify_quality_exit")
    if vq is MISSING:
        fail.append("verify-quality never ran -- cannot rule out a gameable verify")
    elif vq != 0:
        fail.append(f"verify-quality exited {vq!r}: the verify may be gameable")

    rv = str(gate.get("review_verdict") or "unknown")
    if rv.startswith(("FAIL", "not-run", "unknown")):
        fail.append(f"review did not run cleanly (review_verdict={rv!r})")
    # LAUNCH BASELINE IS THREE-VALUED, and reading only `untrusted` collapsed two
    # of the three. gate-on-complete.apply_baseline() writes:
    #   dirty > 0   -> `untrusted` set, plus a HIGH input issue   (measured DIRTY)
    #   dirty <= 0  -> `launch_baseline` key set, no `untrusted`  (measured CLEAN)
    #   no stamp    -> a not_checked entry, and NEITHER key       (NEVER MEASURED)
    # So `untrusted` absent covers BOTH "measured clean" and "never measured",
    # and this function reported the success reason "clean launch baseline" for
    # both. Caught on the first genuine auto-approve candidate (Diplomat
    # 08c288f0), whose own gate record says in not_checked: "a dirty starting
    # tree could not be ruled out" -- while auto_decide called it clean.
    #
    # The presence of the `launch_baseline` KEY is the proof the check ran, so
    # that is what gates it -- the same MISSING discipline the rest of this
    # function already uses, and the same lesson for the sixth time: an un-run
    # check must SAY it did not run; it must never report pass. `clean_launch_
    # baseline` is a declared requirement in signoff-rules.json, and until now
    # it was unenforceable whenever the queue failed to stamp the job.
    lb = ev("launch_baseline")
    if gate.get("untrusted"):
        fail.append(f"launch baseline untrusted: {'; '.join(gate['untrusted'])[:120]}")
    elif lb is MISSING:
        fail.append("gate record has no launch_baseline -- the queue never stamped "
                    "the starting tree, so a dirty start (which would make the diff "
                    "unattributable to this job) could not be ruled out. ABSENT IS "
                    "UNKNOWN, NOT CLEAN.")
    mx_f, mx_l = policy.get("max_files"), policy.get("max_diff_lines")
    if mx_f and len(diff_files) > mx_f:
        fail.append(f"{len(diff_files)} files changed, cap is {mx_f}")
    if mx_l and diff_lines and diff_lines > mx_l:
        fail.append(f"{diff_lines} diff lines, cap is {mx_l}")

    # --- layer 2: RELEVANCE. Discrimination (above) says the verify can tell
    # done from not-done; relevance says it tests the property and not a
    # proxy. Both-ways alone let a grep-for-a-literal certify broken work.
    rel_fail, rel_notes = relevance_evidence(gate, rules)
    fail += rel_fail

    # --- layer 3: CHANGE CLASS. Even perfect evidence only licenses a
    # bounded, single-site, non-security-adjacent change. The domain is
    # narrow on purpose; widen it by measured agreement (--agreement), not
    # by argument.
    if not diff_path:
        fail.append("no --diff: the change class cannot be determined, and an "
                    "unclassified change is not in the auto-approve domain")
        klass, class_notes = "unclassified", []
    else:
        klass, class_notes = classify_change(diff_txt, rules)
        if klass != "bounded_single_site":
            fail.append("change class is not auto-approvable: "
                        + "; ".join(class_notes))

    if fail:
        return "human", fail
    return "auto-approve", [
        "gate pass, no code findings",
        "verify exited 0 and was PROVEN failing at baseline",
        "verify judged non-gameable",
        f"review ran cleanly ({rv})",
        "launch baseline recorded and clean, scope clean",
        f"{len(diff_files)} file(s), {diff_lines} diff line(s) -- within caps",
        *rel_notes,
        f"change class {klass}: " + "; ".join(class_notes),
    ]


def _pat_ok(p):
    import re
    try:
        re.compile(p)
        return True
    except re.error:
        return False


def policy_check():
    """Report what auto-approve can actually EVER approve.

    Exists because the two sets answer different questions and can silently
    collapse into each other:
      basenames  -- "important enough that a human should look"
      never_auto -- "would corrupt the gate itself if it approved itself"
    If basenames is a SUBSET of never_auto, every basename-triggered sign-off is
    un-auto-approvable and the feature is inert -- present, wired, tested, and
    incapable of ever firing on the common case. Nothing else in the system says
    so, and an inert feature reads exactly like a working one that just has not
    triggered yet. Same invariant as everywhere else here: a thing that cannot
    run must SAY it cannot run.
    """
    rules = load_rules()
    b = set(rules.get("basenames") or [])
    n = set(rules.get("never_auto") or [])
    globs = rules.get("basename_globs") or []
    nglobs = rules.get("never_auto_globs") or []
    approvable = sorted(b - n)
    print(f"[signoff] auto-approve enabled: "
          f"{bool((rules.get('auto_approve') or {}).get('enabled'))}")
    print(f"[signoff] basenames requiring sign-off: {len(b)}")
    print(f"[signoff] never_auto:                   {len(n)}")
    print(f"[signoff] basenames that CAN auto-approve: "
          f"{', '.join(approvable) if approvable else 'NONE'}")
    if globs:
        print(f"[signoff] sign-off basename globs: {globs}  "
              f"(never_auto globs: {nglobs})")
    # path_globs is the matcher covering risk-bearing product code whose basename
    # is generic (route.ts). A policy report that omits a matcher understates what
    # the policy does -- the same "reads wrong" error as basename_globs reading as
    # active while nothing consulted it.
    pglobs = rules.get("path_globs") or []
    if pglobs:
        print(f"[signoff] sign-off PATH globs: {pglobs}")
        print("[signoff] (path globs are product-code rules, absent from "
              "never_auto, so they CAN auto-approve)")
    policy = rules.get("auto_approve") or {}
    mode = policy.get("mode") or "shadow"
    print(f"[signoff] auto-approve MODE: {mode}"
          + ("  (decisions are logged, never acted on)" if mode != "live" else
             "  (LIVE: decisions are acted on)"))
    sc = (policy.get("safe_classes") or {}).get("bounded_single_site")
    print(f"[signoff] safe change class: {sc if sc else 'NONE DECLARED -- domain is empty'}")
    pats = policy.get("security_adjacent_patterns") or []
    print(f"[signoff] security-adjacent patterns: {len(pats)}")
    rel = policy.get("relevance") or {}
    print(f"[signoff] relevance floor: score >= {rel.get('min_score', 0.8)}, "
          f"survivors <= {rel.get('max_survivors', 3)}; ledger {ledger_dir()}")
    # Which approvable basenames the security patterns would still catch by
    # PATH. Reported so the policy line above cannot overstate the domain.
    import re as _re
    caught = sorted(bn for bn in approvable
                    if any(_re.search(p, bn, _re.I) for p in pats
                           if _pat_ok(p)))
    if caught:
        print(f"[signoff] ...of which security patterns still route to a human: "
              f"{', '.join(caught)}")
    if not approvable:
        print("[signoff] WARNING: every basename that requires sign-off is also in "
              "never_auto, so auto-approve can NEVER fire for a basename-triggered "
              "sign-off. Its only remaining domain is a task file declaring "
              "'## Requires sign-off:'. That is a POLICY state, not a bug -- but it "
              "means autonomous sign-off is inert for the common case.")
        return 1
    return 0


def auto_signoff(job_id, gate_path, diff_path=None):
    """Apply auto_decide and record the outcome."""
    signoffs = load_signoffs()
    if job_id not in signoffs:
        print(f"[signoff] no record found for job {job_id}")
        return 1
    if not signoffs[job_id].get("required"):
        print(f"[signoff] {job_id} does not require sign-off; nothing to do")
        return 0
    if signoffs[job_id].get("verdict") in ("approve", "auto-approve"):
        print(f"[signoff] {job_id} already {signoffs[job_id]['verdict']}")
        return 0

    decision, reasons = auto_decide(job_id, gate_path, diff_path)
    mode = ((load_rules().get("auto_approve") or {}).get("mode")) or "shadow"
    if mode != "live":
        # SHADOW: decide, record, never act. The human's verdict lands on the
        # same record later via --verdict, and --agreement compares the two.
        # `verdict` stays None so every existing surface still shows the job
        # as awaiting its human.
        signoffs[job_id]["shadow_decision"] = decision
        signoffs[job_id]["shadow_reasons"] = reasons
        signoffs[job_id]["shadow_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_signoffs(signoffs)
        print(f"[signoff] SHADOW ({mode}): harness would "
              f"{'AUTO-APPROVE' if decision == 'auto-approve' else 'HOLD for a human'} "
              f"{job_id}; not acted on. Awaiting "
              f"{signoffs[job_id].get('reviewer') or 'a human'}.")
        for r in reasons:
            print(f"    {'+' if decision == 'auto-approve' else '-'} {r}")
        return 0
    if decision == "auto-approve":
        # A DISTINCT verdict on purpose. It must never be indistinguishable from
        # a human's approve -- the evidence behind the two differs, and anyone
        # auditing later has to be able to tell which one happened.
        signoffs[job_id]["verdict"] = "auto-approve"
        signoffs[job_id]["reviewer"] = "harness"
        signoffs[job_id]["conditions"] = reasons
        signoffs[job_id]["auto_ts"] = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        save_signoffs(signoffs)
        print(f"[signoff] AUTO-APPROVED {job_id}")
        for r in reasons:
            print(f"    + {r}")
        return 0
    signoffs[job_id]["auto_blocked_by"] = reasons
    save_signoffs(signoffs)
    print(f"[signoff] {job_id} stays with {signoffs[job_id].get('reviewer') or 'a human'}:")
    for r in reasons:
        print(f"    - {r}")
    return 0


def agreement(as_json=False):
    """SHADOW calibration: did the harness agree with the human?

    Over every record carrying BOTH a shadow_decision and a human verdict:
      agree          shadow auto-approve & human approve, or
                     shadow human & human changes/reject
      DANGEROUS      shadow auto-approve & human changes/reject
                     -- the harness would have approved something a human
                     sent back. This is the only number that gates going
                     live, and its acceptable value is zero.
      conservative   shadow human & human approve
                     -- the harness held something a human passed. Costs
                     a look; the direction the design is biased toward.
    Records with a shadow decision and NO human verdict yet are counted as
    pending, so "no disagreements" over zero compared pairs cannot read as
    calibration.
    """
    signoffs = load_signoffs()
    pairs, pending = [], 0
    for jid, r in signoffs.items():
        sd = r.get("shadow_decision")
        if not sd:
            continue
        hv = r.get("verdict")
        if hv in ("approve", "changes", "reject"):
            pairs.append((jid, sd, hv))
        elif hv in (None, ""):
            pending += 1
    agree = [p for p in pairs if (p[1] == "auto-approve" and p[2] == "approve")
             or (p[1] == "human" and p[2] in ("changes", "reject"))]
    dangerous = [p for p in pairs if p[1] == "auto-approve" and p[2] in ("changes", "reject")]
    conservative = [p for p in pairs if p[1] == "human" and p[2] == "approve"]
    out = {
        "compared": len(pairs), "pending_human_verdict": pending,
        "agree": len(agree), "dangerous_false_approve": len(dangerous),
        "conservative_hold": len(conservative),
        "agreement_rate": (round(len(agree) / len(pairs), 3) if pairs else None),
        "dangerous_jobs": [p[0] for p in dangerous],
        "conservative_jobs": [p[0] for p in conservative],
        "would_auto_approve_rate": (round(sum(1 for p in pairs if p[1] == "auto-approve")
                                          / len(pairs), 3) if pairs else None),
    }
    if as_json:
        print(json.dumps(out, indent=1))
        return 0
    print(f"[signoff] shadow agreement: {out['agree']}/{out['compared']} compared "
          f"pairs agree" + (f" ({out['agreement_rate']:.0%})" if pairs else
                            " (NO PAIRS YET -- nothing is calibrated)"))
    print(f"[signoff]   dangerous (harness approve, human sent back): "
          f"{out['dangerous_false_approve']}"
          + (f"  <- {', '.join(out['dangerous_jobs'])}" if dangerous else ""))
    print(f"[signoff]   conservative (harness hold, human approved):  "
          f"{out['conservative_hold']}")
    print(f"[signoff]   shadow decisions awaiting a human verdict:    {pending}")
    if out["would_auto_approve_rate"] is not None:
        print(f"[signoff]   harness would have auto-approved "
              f"{out['would_auto_approve_rate']:.0%} of compared jobs")
    if dangerous:
        print("[signoff] DO NOT GO LIVE: the harness would have approved work a "
              "human rejected. Read those jobs' shadow_reasons and fix the "
              "condition that let them through before widening anything.")
    return 0


def set_verdict(job_id, verdict, conditions):
    """Record a human's decision for a job."""
    # Load current signoffs
    signoffs = load_signoffs()
    
    if job_id not in signoffs:
        print(f"[signoff] no record found for job {job_id}")
        return 1
    
    # Validate verdict
    valid_verdicts = ["approve", "changes", "reject"]
    if verdict not in valid_verdicts:
        print(f"[signoff] invalid verdict '{verdict}', must be one of: {valid_verdicts}")
        return 1
    
    # Update the record
    signoffs[job_id]["verdict"] = verdict
    signoffs[job_id]["conditions"] = conditions or []
    
    save_signoffs(signoffs)
    print(f"[signoff] recorded verdict '{verdict}' for job {job_id}")
    return 0


def show_status(job_id):
    """Print the status of a specific job."""
    signoffs = load_signoffs()
    if job_id not in signoffs:
        print(f"[signoff] no record found for job {job_id}")
        return 1
    print(json.dumps(signoffs[job_id], indent=1))
    return 0


def show_all():
    """Print all signoff records."""
    signoffs = load_signoffs()
    print(json.dumps(signoffs, indent=1))
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--evaluate", help="Evaluate a job for sign-off requirements")
    ap.add_argument("--diff", help="Path to the diff file")
    ap.add_argument("--task-file", help="Path to the task file")
    ap.add_argument("--verdict", choices=["approve", "changes", "reject"], 
                    help="Set verdict for a job (approve/changes/reject)")
    ap.add_argument("--conditions", nargs="*", help="Conditions related to the verdict")
    ap.add_argument("--auto", metavar="JOB_ID",
                    help=("decide autonomously, from the gate's DECIDABLE evidence, "
                          "whether this job can sign itself off. Needs --gate (and "
                          "--diff for the size caps and the never_auto carve-out)."))
    ap.add_argument("--gate", metavar="PATH", help="path to the job's .gate.json")
    ap.add_argument("--policy-check", action="store_true",
                    help=("report what auto-approve can EVER approve; exits 1 if "
                          "every sign-off basename is also in never_auto, which "
                          "makes autonomous sign-off inert for the common case"))
    ap.add_argument("--status", help="Show status of a specific job")
    ap.add_argument("--agreement", action="store_true",
                    help="shadow-mode calibration: agreement between the "
                         "harness's recorded decision and the human's verdict")
    ap.add_argument("--json", action="store_true", help="Print all entries as JSON")
    ap.add_argument("job_id", nargs="?", help="Job ID (when using --verdict)")

    a = ap.parse_args()

    if a.agreement:
        return agreement(as_json=a.json)

    if a.json:
        return show_all()
    
    if a.policy_check:
        return policy_check()

    if a.auto:
        return auto_signoff(a.auto, a.gate, a.diff)

    if a.status:
        return show_status(a.status)
    
    # Handle verdict setting - can be with or without --evaluate
    if a.verdict:
        # If job_id is provided as positional argument, use it; otherwise check --evaluate
        if a.job_id:
            return set_verdict(a.job_id, a.verdict, a.conditions)
        elif a.evaluate:
            return set_verdict(a.evaluate, a.verdict, a.conditions)
        else:
            print("Error: --verdict requires either --evaluate or a positional job ID")
            return 1
    
    if a.evaluate and a.diff:
        return evaluate_job(a.evaluate, a.diff, a.task_file)
    
    print("No action specified. Use --help for usage information.")
    return 1


if __name__ == "__main__":
    sys.exit(main())