#!/usr/bin/env python3
"""land_packet.py -- the LANDING PACKET: everything the final reviewer needs, computed.

A packet is built per finished chain and written to
  ~/.ollama-dispatch/landing/<chain>/packet.json + packet.md
It never writes a product repo's branches: the only git writes here are a throw-away
detached worktree (removed after the checks) and reads. Landing is `ollama-land approve`.

Contents: diff stats, gate/verify verdict, relevance + necessity + wrong-bank numbers,
GROUNDED review findings (land_ground), repo CI/test commands run locally against the staged
branch (with a baseline compare so a pre-existing red is not blamed on the change), a
rule-based risk score + tier (research (e)), the diff-scope facts the research asked for
(files outside declared scope, tests weakened, new deps, secrets, deny-list paths, LOC), and
the exact revert command.

Research: ~/.ollama-dispatch/merge-queue/2026-10-09-autoland-research.md
  (a)4 deterministic diff-scope checks; (a)5 learned risk NOT adopted -> rule-based score;
  (e) Step 0 refuse conditions (-> `auto_blockers`), Step 3 tiers T0/T1/T2.
"""
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import land_ground  # noqa: E402

DOC_EXTS = (".md", ".txt", ".rst", ".adoc")
TEST_RE = re.compile(r"(^|/)(tests?|__tests__)/|(\.|_)(test|spec)\.[a-z]+$|(^|/)test_[^/]+\.py$|(^|/)verify[^/]*\.test\.", re.I)
DENY_RES = [re.compile(p, re.I) for p in (
    r"(^|/)\.github/", r"(^|/)Dockerfile", r"docker-compose", r"(^|/)\.env", r"secret", r"(^|/)auth[^/]*\.",
    r"(^|/)(auth|oauth)/", r"billing", r"(^|/)migrations?/", r"schema\.prisma$", r"LaunchAgents/|LaunchDaemons/",
    r"(^|/)bin/(gate-on-complete|ollama-queue|ollama-worker|ollama-dispatch|handoff-emit|ollama-land|land_)",
    r"package-lock\.json$|yarn\.lock$|pnpm-lock\.yaml$|poetry\.lock$")]
SECRET_RES = [re.compile(p) for p in (
    r"AKIA[0-9A-Z]{16}", r"-----BEGIN (RSA |EC |OPENSSH |)PRIVATE KEY-----", r"ghp_[A-Za-z0-9]{30,}",
    r"sk-[A-Za-z0-9]{32,}", r"xox[baprs]-[A-Za-z0-9-]{10,}",
    r"(?i)(api[_-]?key|secret|token|passwd|password)\s*[:=]\s*['\"][A-Za-z0-9/+_\-]{16,}['\"]")]
ASSERT_RE = re.compile(r"\b(assert\w*|expect\s*\(|\.should|self\.assert\w+|strictEqual|deepEqual|toBe\w*|toEqual|toThrow)\b")
SKIP_RE = re.compile(r"(\.skip\s*\(|\bxit\s*\(|\bxtest\s*\(|\bxdescribe\s*\(|pytest\.mark\.skip|@unittest\.skip|\bpytest\.skip\s*\(|\.todo\s*\()")
SCAFFOLD = {"TASK.md", "verify.sh", "verify_impl.mts", "verify_impl.mjs", "verify_impl.js", "test_fixture.py",
            "check_literals.py", "refimpl.py", "fix.patch", ".preflight-state.json", ".dispatch-harness.json",
            "auto-harness-check.py", "AUTO-TASK.md"}

LOC_CAP, FILE_CAP = 200, 8          # research (e) step 0
T1_LOC, T1_FILES = 80, 3


def sh(cmd, cwd=None, timeout=900, env=None):
    """(rc, out) with a hard timeout; process group killed on expiry. Never raises."""
    e = dict(os.environ, DISPATCH_VERIFY_SANDBOX="1")
    e.update(env or {})
    try:
        p = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=cwd, env=e, text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            out, _ = p.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(p.pid, 9)
            except OSError:
                pass
            out, _ = p.communicate()
            return 124, (out or "") + f"\n[timeout after {timeout}s]"
        return p.returncode, out or ""
    except OSError as ex:
        return 127, f"{type(ex).__name__}: {ex}"


def git(repo, *args, timeout=120):
    rc, out = sh(["git", "-C", str(repo), *args], timeout=timeout)
    return rc, out.strip()


def default_branch(repo):
    rc, out = git(repo, "symbolic-ref", "--quiet", "--short", "refs/remotes/origin/HEAD")
    if rc == 0 and "/" in out:
        return out.split("/", 1)[1]
    for b in ("main", "master"):
        if git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{b}")[0] == 0:
            return b
    rc, out = git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    return out if rc == 0 else "main"


# ---------------- repo profile (conventions + the repo's OWN test command) ----------------
def repo_profile(repo, overrides=None):
    repo = Path(repo)
    prof = {"test_cmds": [], "ci_cmds": [], "conventions": [], "notes": []}
    pj = repo / "package.json"
    if pj.is_file():
        try:
            scripts = (json.loads(pj.read_text()).get("scripts") or {})
        except ValueError:
            scripts = {}
        if scripts.get("test"):
            prof["test_cmds"].append(["npm test --silent", "npm test"])
        wf = repo / ".github" / "workflows"
        wtxt = "".join(p.read_text(errors="replace") for p in sorted(wf.glob("*.y*ml"))) if wf.is_dir() else ""
        if "tsc" in wtxt and (repo / "tsconfig.json").is_file():
            prof["ci_cmds"].append(["npx tsc --noEmit", "tsc"])
        if re.search(r"npm (run )?lint", wtxt) and scripts.get("lint"):
            prof["ci_cmds"].append(["npm run lint --silent", "lint"])
    elif (list(repo.glob("test_*.py")) or (repo / "tests").is_dir()
          or ((repo / "pyproject.toml").is_file() and "pytest" in (repo / "pyproject.toml").read_text(errors="replace"))):
        # Many repos here ship self-check SCRIPTS (test_*.py that exit non-zero on failure) and the
        # box's python3 may have no pytest: use pytest only when it is importable.
        if sh("python3 -c 'import pytest'", timeout=30)[0] == 0 and ((repo / "tests").is_dir() or (repo / "conftest.py").is_file()
                                                                    or (repo / "pyproject.toml").is_file()):
            prof["test_cmds"].append(["python3 -m pytest -q -x -p no:cacheprovider", "pytest"])
        else:
            prof["test_cmds"].append(['set -e; for f in test_*.py tests/test_*.py; do [ -f "$f" ] && python3 "$f"; done; true',
                                      "test_*.py scripts"])
    for name in ("CLAUDE.md", "AGENTS.md", "README.md"):
        p = repo / name
        if p.is_file():
            t = p.read_text(errors="replace")
            for ln in t.splitlines():
                if re.search(r"(?i)\b(merge|push|squash|rebase|branch|commit message|co-authored)\b", ln) and len(ln) < 220:
                    prof["conventions"].append(f"{name}: {ln.strip()}")
    prof["conventions"] = prof["conventions"][:8]
    prof.update((overrides or {}).get(str(repo), {}) or {})
    return prof


# ---------------- diff facts + risk (pure) ----------------
def analyze_diff(diff_text, declared_scope=None, extra_deny=(), deps_added=None):
    parsed = land_ground.parse_diff(diff_text)
    files = sorted(parsed)
    added = sum(len(d["added"]) for d in parsed.values())
    removed = sum(len(d["removed"]) for d in parsed.values())
    scope = set(declared_scope or [])
    outside = [f for f in files if scope and f not in scope and Path(f).name not in SCAFFOLD] if scope else []
    weakened, secrets, deny = [], [], []
    deny_res = DENY_RES + [re.compile(p, re.I) for p in extra_deny]
    for f, d in parsed.items():
        is_test = bool(TEST_RE.search(f))
        if is_test:
            ra = sum(1 for t in d["removed"].values() if ASSERT_RE.search(t))
            aa = sum(1 for t in d["added"].values() if ASSERT_RE.search(t))
            if ra > aa:
                weakened.append(f"{f}: {ra} assertion line(s) removed vs {aa} added")
            for n, t in d["added"].items():
                if SKIP_RE.search(t):
                    weakened.append(f"{f}:{n}: test skipped/todo added")
        for n, t in d["added"].items():
            for rx in SECRET_RES:
                if rx.search(t):
                    secrets.append(f"{f}:{n}")
                    break
        if any(rx.search(f) for rx in deny_res):
            deny.append(f)
    doc_only = bool(files) and all(f.lower().endswith(DOC_EXTS) for f in files)
    test_only_add = bool(files) and all(TEST_RE.search(f) or f.lower().endswith(DOC_EXTS) for f in files) \
        and removed == 0 and not weakened
    return {"files": files, "n_files": len(files), "added": added, "removed": removed, "loc": added + removed,
            "outside_scope": outside, "tests_weakened": weakened, "secrets": sorted(set(secrets)),
            "deny_hits": deny, "new_deps": sorted(deps_added or []), "doc_only": doc_only,
            "test_only_additive": test_only_add}


def risk(facts, concerns=False, checks_unverified=False, adjudicated=False):
    """-> {"score":0-100, "tier":"T0|T1|T2", "reasons":[...], "auto_blockers":[...]}"""
    r, why, ab = 0, [], []
    loc, nf = facts["loc"], facts["n_files"]
    r += min(40, loc // 5)
    if loc:
        why.append(f"{loc} changed lines in {nf} file(s)")
    if facts["outside_scope"]:
        r += min(20, 5 * len(facts["outside_scope"]))
        why.append(f"{len(facts['outside_scope'])} file(s) outside the declared scope")
    if facts["tests_weakened"]:
        r += 25
        ab.append("tests weakened")
    if facts["new_deps"]:
        r += 20
        ab.append("new dependency: " + ", ".join(facts["new_deps"][:4]))
    if facts["secrets"]:
        r += 40
        ab.append("secret-like string in added lines")
    if facts["deny_hits"]:
        r += 20
        ab.append("deny-list path: " + ", ".join(facts["deny_hits"][:3]))
    if loc > LOC_CAP or nf > FILE_CAP:
        ab.append(f"over size cap ({LOC_CAP} LOC / {FILE_CAP} files)")
    if concerns:
        r += 10
        why.append("gate verdict was CONCERNS" + (" (adjudicated)" if adjudicated else ""))
    if checks_unverified:
        r += 15
        why.append("repo checks could not be verified locally")
        ab.append("repo checks unverified")
    if facts["doc_only"] or facts["test_only_additive"]:
        tier = "T0"
    elif loc <= T1_LOC and nf <= T1_FILES and not facts["deny_hits"] and not facts["new_deps"]:
        tier = "T1"
    else:
        tier = "T2"
    return {"score": min(100, r), "tier": tier, "reasons": why, "auto_blockers": ab}


# ---------------- checks (repo's own CI/tests) ----------------
def _wt_add(repo, ref):
    tmp = Path(tempfile.mkdtemp(prefix="land-wt-"))
    wt = tmp / "wt"
    rc, out = git(repo, "worktree", "add", "--detach", str(wt), ref)
    if rc != 0:
        shutil.rmtree(tmp, ignore_errors=True)
        return None, out
    nm = Path(repo) / "node_modules"
    if nm.is_dir() and not (wt / "node_modules").exists():
        try:
            os.symlink(nm, wt / "node_modules")
        except OSError:
            pass
    return wt, ""


def _wt_remove(repo, wt):
    if wt is None:
        return
    git(repo, "worktree", "remove", "--force", str(wt))
    shutil.rmtree(Path(wt).parent, ignore_errors=True)
    git(repo, "worktree", "prune")


def run_checks(repo, ref, base_ref, profile, timeout=900, runner=sh, flake_rerun=True):
    """Run the repo's test + CI commands at `ref`. A red result is re-run once (flake) and, if
    still red, compared with `base_ref`: red-on-both = 'preexisting' (not this change's fault).
    -> {"status": pass|fail|preexisting|unverified|none, "runs": [...]}"""
    cmds = [c for c in profile.get("test_cmds", [])] + [c for c in profile.get("ci_cmds", [])]
    if not cmds:
        return {"status": "none", "runs": [], "note": "no test command detected for this repo"}
    wt, err = _wt_add(repo, ref)
    if wt is None:
        return {"status": "unverified", "runs": [], "note": f"could not create worktree: {err[-200:]}"}
    runs, worst = [], "pass"
    base_wt = None
    try:
        for cmd, name in cmds:
            t0 = time.time()
            rc, out = runner(cmd, cwd=str(wt), timeout=timeout)
            row = {"name": name, "cmd": cmd, "rc": rc, "seconds": round(time.time() - t0, 1), "tail": out[-600:]}
            if rc != 0 and flake_rerun:
                rc2, out2 = runner(cmd, cwd=str(wt), timeout=timeout)
                row["flake_rerun_rc"] = rc2
                if rc2 == 0:
                    row["status"] = "pass (flaky: red once)"
                    rc = 0
                else:
                    rc, out = rc2, out2
                    row["tail"] = out2[-600:]
            if rc == 0:
                row.setdefault("status", "pass")
            elif rc in (124, 127):
                row["status"] = "unverified"
                worst = "unverified" if worst == "pass" else worst
            else:
                if base_wt is None:
                    base_wt, _ = _wt_add(repo, base_ref)
                if base_wt is not None:
                    brc, _bo = runner(cmd, cwd=str(base_wt), timeout=timeout)
                    row["base_rc"] = brc
                    if brc != 0:
                        row["status"] = "preexisting"
                        worst = "preexisting" if worst in ("pass",) else worst
                    else:
                        row["status"] = "fail"
                        worst = "fail"
                else:
                    row["status"] = "fail"
                    worst = "fail"
            runs.append(row)
    finally:
        _wt_remove(repo, wt)
        _wt_remove(repo, base_wt)
    return {"status": worst, "runs": runs}


# ---------------- new dependencies ----------------
def deps_added_between(repo, base_ref, ref):
    out = []
    def load(r, path):
        rc, t = git(repo, "show", f"{r}:{path}")
        return t if rc == 0 else None
    for path in ("package.json",):
        a, b = load(base_ref, path), load(ref, path)
        if b is None:
            continue
        try:
            ja = json.loads(a) if a else {}
            jb = json.loads(b)
        except ValueError:
            continue
        for sect in ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies"):
            for k in (jb.get(sect) or {}):
                if k not in (ja.get(sect) or {}):
                    out.append(f"{k} ({sect})")
    for path in ("requirements.txt",):
        a, b = load(base_ref, path) or "", load(ref, path)
        if b is None:
            continue
        have = {re.split(r"[=<>!~\[ ;]", l.strip(), 1)[0].lower() for l in a.splitlines() if l.strip() and not l.startswith("#")}
        for l in b.splitlines():
            n = re.split(r"[=<>!~\[ ;]", l.strip(), 1)[0].lower()
            if l.strip() and not l.startswith("#") and n and n not in have:
                out.append(f"{n} ({path})")
    return out


# ---------------- revert ----------------
def revert_command(repo, dref, commits):
    """The exact, precomputed revert for a landing of `commits` (oldest..newest shas)."""
    q = shlex.quote
    if not commits:
        return ""
    if len(commits) == 1:
        body = f"git -C {q(str(repo))} revert --no-edit {commits[0]}"
    else:
        body = f"git -C {q(str(repo))} revert --no-edit {commits[0]}^..{commits[-1]}"
    return f"{body} && git -C {q(str(repo))} push origin {dref}:{dref}"


# ---------------- packet ----------------
def relevance_summary(gate_or_state):
    vr = (gate_or_state or {}).get("verify_relevance") or {}
    nec = vr.get("necessity") or {}
    bank = vr.get("wrong_bank") or {}
    return {"verdict": vr.get("verdict"), "score": vr.get("score"), "threshold": vr.get("threshold"),
            "killed": vr.get("killed"), "evidence_mutants": vr.get("evidence_mutants"),
            "survivors": len(vr.get("survivors") or []),
            "necessity": ({"checked": nec.get("checked"), "necessary": nec.get("necessary"),
                           "unnecessary": len(nec.get("unnecessary") or []),
                           "untested": len(nec.get("untested") or [])} if nec else None),
            "wrong_bank": ({"variants": len(bank.get("variants") or []),
                            "survivors": len(bank.get("survivors") or []),
                            "unavailable": len(bank.get("unavailable") or [])} if bank else None)}


def build_packet(cand, cfg=None, runner=sh, now=None, run_repo_checks=True):
    """cand: {chain, kind('integration'|'dispatch'), repo, branch, job_id?, gate?(dict), issues?(list),
              declared_scope?, plan?, worktree?, label?, adjudication?(dict)}"""
    cfg = cfg or {}
    now = now or time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    repo, branch = cand["repo"], cand["branch"]
    dref = cand.get("onto") or default_branch(repo)
    P = {"chain": cand["chain"], "kind": cand["kind"], "created": now, "repo": str(repo), "default_branch": dref,
         "integrate_branch": branch, "job_id": cand.get("job_id"), "plan": cand.get("plan"),
         "worktree": cand.get("worktree"), "blockers": [], "warnings": [], "state": "building"}
    rc, base_tip = git(repo, "rev-parse", "--verify", f"refs/heads/{dref}")
    rc2, tip = git(repo, "rev-parse", "--verify", f"refs/heads/{branch}")
    if rc or rc2:
        P["blockers"].append(f"cannot resolve {dref} or {branch} in {repo}")
        P["state"] = "blocked"
        return P
    P["base_tip"], P["branch_tip"] = base_tip, tip
    anc = git(repo, "merge-base", "--is-ancestor", base_tip, tip)[0] == 0
    P["fast_forward"] = anc
    if tip == base_tip:
        P["blockers"].append("branch tip equals the default branch tip: already landed (or nothing staged)")
    elif not anc:
        P["blockers"].append(f"{dref} moved on: its tip is not an ancestor of {branch} -- re-stage (stale)")
    diff = sh(["git", "-C", str(repo), "diff", "--no-color", f"{base_tip}..{tip}"], timeout=120)[1]
    P["diff_sha256"] = __import__("hashlib").sha256(diff.encode()).hexdigest()
    P["diff_stat"] = git(repo, "diff", "--stat", f"{base_tip}..{tip}")[1][-1500:]
    scope = cand.get("declared_scope") or []
    deps = deps_added_between(repo, base_tip, tip) if anc else []
    facts = analyze_diff(diff, scope, deps_added=deps)
    P["facts"] = facts
    rc, cl = git(repo, "rev-list", "--reverse", f"{base_tip}..{tip}")
    commits = cl.split() if rc == 0 else []
    P["commits"] = commits
    P["revert"] = {"command": revert_command(repo, dref, commits), "note": "valid after the fast-forward landing"}
    gate = cand.get("gate") or {}
    P["gate"] = {"verdict": gate.get("verdict"), "job_id": gate.get("job_id") or cand.get("job_id"),
                 "verify_exit": gate.get("verify_exit_reported"), "verify_failed_at_baseline": gate.get("verify_failed_at_baseline"),
                 "review_verdict": gate.get("review_verdict"), "review_model": gate.get("review_model")}
    P["relevance"] = relevance_summary(gate)
    g = land_ground.ground_findings(cand.get("issues") or gate.get("issues") or [], diff,
                                    reader=lambda rel: git(repo, "show", f"{tip}:{rel}")[1] if git(repo, "cat-file", "-e", f"{tip}:{rel}")[0] == 0 else None,
                                    model=gate.get("review_model") or "unknown")
    P["findings"] = g["kept"]
    P["findings_dropped"] = g["dropped"]
    P["findings_stats"] = g["stats"]
    if run_repo_checks and anc and tip != base_tip:
        prof = repo_profile(repo, cfg.get("repos"))
        P["conventions"] = prof["conventions"]
        P["checks"] = run_checks(repo, tip, base_tip, prof, runner=runner, timeout=cfg.get("check_timeout", 900))
    else:
        P["checks"] = {"status": "skipped", "runs": []}
    verdict = (P["gate"]["verdict"] or "").lower()
    adj = cand.get("adjudication")
    P["adjudication"] = adj
    concerns = verdict in ("concerns", "disputed")
    if verdict in ("fail", "error"):
        P["blockers"].append(f"gate verdict {verdict.upper()}")
    elif concerns and not (adj and adj.get("decision") == "accept"):
        P["blockers"].append("gate verdict CONCERNS with no accepting adjudication")
    elif verdict not in ("pass", "concerns", "disputed"):
        P["blockers"].append(f"gate verdict {verdict or 'MISSING'} is not landable")
    cs = P["checks"]["status"]
    if cs == "fail":
        P["blockers"].append("repo checks FAIL on the staged branch (and pass on the base)")
    if cs == "preexisting":
        P["warnings"].append("repo checks red on the base too (pre-existing); not attributed to this change")
    if cs in ("none", "unverified", "skipped"):
        P["warnings"].append(f"repo checks {cs}: {P['checks'].get('note', '')}".strip())
    blocking = [f for f in P["findings"] if f.get("blocking")]
    if blocking:
        P["blockers"].append(f"{len(blocking)} grounded BLOCKING finding(s) with repro")
    rel = P["relevance"]
    if rel.get("necessity") and rel["necessity"].get("unnecessary"):
        P["blockers"].append(f"{rel['necessity']['unnecessary']} unnecessary hunk(s) (necessity gate)")
    if rel.get("wrong_bank") and rel["wrong_bank"].get("survivors"):
        P["blockers"].append(f"{rel['wrong_bank']['survivors']} wrong-solution survivor(s) (bank gate)")
    if P["facts"]["secrets"]:
        P["blockers"].append("secret-like string in added lines: " + ", ".join(P["facts"]["secrets"][:3]))
    P["risk"] = risk(facts, concerns=concerns, checks_unverified=cs in ("none", "unverified", "skipped"),
                     adjudicated=bool(adj))
    P["state"] = "needs_rework" if P["blockers"] else "awaiting_final_review"
    return P


def render_md(P):
    f, r, g = P.get("facts") or {}, P.get("risk") or {}, P.get("gate") or {}
    L = [f"# Landing packet: {P['chain']}", "",
         f"- state: **{P['state']}**   kind: {P['kind']}   built: {P['created']}",
         f"- repo: `{P['repo']}`  {P.get('default_branch')} <- `{P.get('integrate_branch')}` "
         f"(base {str(P.get('base_tip'))[:9]}, tip {str(P.get('branch_tip'))[:9]}, ff={P.get('fast_forward')})",
         f"- risk: **{r.get('score')}/100 tier {r.get('tier')}**  {'; '.join(r.get('reasons') or [])}",
         f"- auto-blockers: {', '.join(r.get('auto_blockers') or []) or 'none'}", ""]
    if P.get("blockers"):
        L += ["## BLOCKERS (not land-ready)"] + [f"- {b}" for b in P["blockers"]] + [""]
    if P.get("warnings"):
        L += ["## Warnings"] + [f"- {b}" for b in P["warnings"]] + [""]
    L += ["## Gate / verify",
          f"- verdict: {g.get('verdict')}  job {g.get('job_id')}  verify_exit={g.get('verify_exit')}  "
          f"failed-at-baseline={g.get('verify_failed_at_baseline')}  reviewer={g.get('review_model')}", ""]
    rel = P.get("relevance") or {}
    L += ["## Relevance / necessity / wrong-solution bank", f"- {json.dumps(rel)}", ""]
    c = P.get("checks") or {}
    L += [f"## Repo checks: {c.get('status')}"]
    for run in c.get("runs") or []:
        L.append(f"- `{run['cmd']}` -> {run.get('status')} (rc {run['rc']}, {run['seconds']}s)")
    L.append("")
    L += ["## Diff scope", f"- files {f.get('n_files')}  +{f.get('added')} -{f.get('removed')}  "
          f"outside scope: {f.get('outside_scope') or 'none'}  tests weakened: {f.get('tests_weakened') or 'none'}  "
          f"new deps: {f.get('new_deps') or 'none'}  secrets: {f.get('secrets') or 'none'}  "
          f"deny paths: {f.get('deny_hits') or 'none'}", "", "```", (P.get("diff_stat") or "").strip(), "```", ""]
    st = P.get("findings_stats") or {}
    L += [f"## Grounded findings ({st.get('kept', 0)} kept, {st.get('dropped', 0)} dropped as ungrounded)"]
    for x in P.get("findings") or []:
        tag = "BLOCKING" if x.get("blocking") else "advisory"
        L.append(f"- [{tag}] {x.get('file')}:{x.get('line')} ({x.get('rule', x.get('source'))}) {str(x.get('what'))[:200]}"
                 + (f"  quote: `{x['quote_checked'][0][:80]}`" if x.get("quote_checked") else ""))
    for d in P.get("findings_dropped") or []:
        fd = d["finding"] if isinstance(d["finding"], dict) else {}
        L.append(f"- (dropped) {fd.get('file')}:{fd.get('line')} -- {d['reason']}")
    L.append("")
    if P.get("adjudication"):
        a = P["adjudication"]
        L += ["## Adjudication", f"- decision: **{a.get('decision')}**  by {a.get('model')}  {a.get('reasons')}", ""]
    L += ["## Conventions seen in the repo docs"] + [f"- {x}" for x in (P.get("conventions") or ["(none found)"])] + [""]
    L += ["## Revert (precomputed)", "```", (P.get("revert") or {}).get("command", ""), "```", "",
          "## Decide", f"- approve: `ollama-land approve {P['chain']}`",
          f"- reject:  `ollama-land reject {P['chain']} --reason \"...\"`", ""]
    return "\n".join(L)


if __name__ == "__main__":
    print(__doc__)
