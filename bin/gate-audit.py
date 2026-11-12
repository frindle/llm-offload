#!/usr/bin/env python3
"""gate-audit.py -- READ-ONLY self-audit of the dispatch gate's verdicts vs outcomes.

WHY (2026-10-06, the owner: "get the seam defects and gate issues worked out"): every
gate defect found so far was found by a human noticing one wrong verdict on one
live job. This reproduces the cross-tab from the live ledgers so the false-PASS /
false-FAIL rate can be watched instead of anecdotally rediscovered.

Inputs (all read-only; nothing here writes outside --out, enqueues, or takes a lock):
  ~/bin/ollama-queue-logs/{,archive/}<id>.gate.json   gate verdicts (newest copy wins)
  ~/bin/ollama-queue-logs/{,archive/}<id>.diff        the diff each verdict judged
  ~/bin/ollama-queue-logs/{,archive/}<id>.done.json   terminal job sidecars
  <HANDOFF_DIR>/acted.json                            coordinator dispositions
  ~/.ollama-dispatch/slice-runs/*.json                slice outcome (done/relanded)
  git (read-only `git show` / `git ls-tree`, GIT_OPTIONAL_LOCKS=0) on the repos

OUTCOME (what eventually happened to the code a verdict judged) -- the honest proxy:
  CONTENT SURVIVAL = the fraction of the diff's significant added PRODUCT lines that
  are present, verbatim (whitespace-stripped), in the same file on the repo's default
  branch NOW.  >=0.7 landed, <0.3 not-landed, otherwise partial.  A coordinator
  disposition (acted.json 'rejected' / sign-off override) overrides it.

CLASSES (each a candidate list, not a conviction -- read the rows):
  false-PASS   final verdict pass AND (coordinator rejected OR a later auto-fix round
               of the same chain exists OR not-landed while a sibling job of the same
               feature landed)
  false-BLOCK  final verdict fail/concerns AND its content landed (>=0.9) AND no later
               chain round added anything it lacked (same code shipped as-is)
  PASS-property tallies (no outcome needed): relevance not proven, tiny mutant sample,
               no both-ways baseline proof, review not a clean PASS, cross-family
               disagreement, source-text fixture, 'ready-to-apply' on any of those.

Usage:
  gate-audit.py                 headline + cross-tab + top candidates
  gate-audit.py --json          machine-readable full result
  gate-audit.py --since 2026-10-01 --candidates 40
  gate-audit.py --self-test
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from pathlib import Path

os.environ.setdefault("GIT_OPTIONAL_LOCKS", "0")

HOME = Path.home()
LOGS = Path(os.environ.get("GATE_AUDIT_LOGS") or HOME / "bin" / "ollama-queue-logs")
HANDOFF = Path(os.environ.get("HANDOFF_DIR")
               or HOME / "Desktop" / "GitHub Projects" / "ollama" / "ollama-handoff")
SLICE_RUNS = Path(os.environ.get("OLLAMA_DISPATCH_HOME")
                  or HOME / ".ollama-dispatch") / "slice-runs"
REPO_ROOTS = [HOME / "Desktop" / "GitHub Projects", HOME / ".ollama-dispatch" / "projects"]

SCAFFOLD = {"TASK.md", "AUTO-TASK.md", "verify.sh", "check_literals.py", "refimpl.py",
            "test_fixture.py", ".preflight-state.json", "auto-harness-check.py",
            ".dispatch-harness.json", "verify_impl.mjs", "verify_impl.mts",
            "verify_impl.js", ".refine-guard.json"}
_SCAFFOLD_RE = re.compile(r"(^|/)verify\.test\.[cm]?[jt]sx?$|(^|/)verify_impl\.")
_TRIVIAL = re.compile(r"^[\s{}()\[\];,.:]*$|^(//|#|/\*|\*|--)")
_AUTOFIX_SUFFIX = re.compile(r"\s*\[auto-fix r\d+\]\s*$")
_FAMILY_SUFFIX = re.compile(r"(-v\d+|-retry\d+|-relaunch\d*|-redo\d*|-impl\d+|-c\d+|-esc)$")
TEST_CWD_MARKERS = ("/testprojects/", "/bakeoff/", "/plan-gen/", "/private/tmp/", "/tmp/",
                    "/r3probe", "dbk-smoke", "/canary")


def _git(repo, *args, timeout=20):
    try:
        r = subprocess.run(["git", "-C", str(repo), *args], capture_output=True,
                           text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else None
    except Exception:
        return None


# ---------------------------------------------------------------- loading
def load_records(logs: Path = LOGS) -> dict:
    """job id -> gate payload. Live dir wins over archive (it is the newer copy)."""
    recs = {}
    for f in sorted(glob.glob(str(logs / "archive" / "*.gate.json"))) + \
            sorted(glob.glob(str(logs / "*.gate.json"))):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        jid = Path(f).name[:12]
        d["_gate_path"] = f
        recs[jid] = d
    return recs


def find_side(logs: Path, jid: str, ext: str):
    for p in (logs / f"{jid}{ext}", logs / "archive" / f"{jid}{ext}"):
        if p.is_file():
            return p
    return None


def load_acted(handoff: Path = HANDOFF) -> dict:
    try:
        return json.loads((handoff / "acted.json").read_text())
    except Exception:
        return {}


def load_slice_jobs(root: Path = SLICE_RUNS) -> dict:
    """job id -> {run, sid, status, relanded}."""
    out = {}
    for f in glob.glob(str(root / "*.json")) + glob.glob(str(root / "archive" / "*.json")):
        try:
            d = json.loads(Path(f).read_text())
        except Exception:
            continue
        sl = d.get("slices") or {}
        items = sl.items() if isinstance(sl, dict) else enumerate(sl)
        for sid, s in items:
            if not isinstance(s, dict):
                continue
            for j in [s.get("job_id")] + list(s.get("author_job_ids") or []):
                if j:
                    out[j] = {"run": d.get("label"), "sid": s.get("id", sid),
                              "status": s.get("status"),
                              "relanded": bool(s.get("relanded_from")),
                              "author": j != s.get("job_id")}
            if s.get("relanded_from"):
                out.setdefault(s["relanded_from"], {}).update(
                    {"run": d.get("label"), "sid": s.get("id", sid), "reland_victim": True,
                     "reland_reason": (s.get("reland_reason") or "")[:160]})
    return out


# ---------------------------------------------------------------- classification
def job_kind(d: dict) -> str:
    lab = d.get("job_label") or ""
    cwd = d.get("job_cwd") or ""
    if lab.startswith(("auto-author-", "auto-refine-")):
        return "author"
    if lab.startswith(("plan-gen", "plangen")):
        return "plangen"
    if lab.startswith(("idle-test", "canary", "bakeoff", "eval-", "probe", "clamp-")) or \
            any(m in cwd for m in TEST_CWD_MARKERS) or d.get("scored_arm"):
        return "test"
    return "code"


def final_verdict(d: dict) -> str:
    v = str(d.get("verdict"))
    if v == "pass-pending-review":
        return "pending"
    return v


def base_label(label: str) -> str:
    lab = _AUTOFIX_SUFFIX.sub("", label or "")
    for _ in range(3):
        lab2 = _FAMILY_SUFFIX.sub("", lab)
        if lab2 == lab:
            break
        lab = lab2
    return lab


def is_scaffold(path: str) -> bool:
    return Path(path).name in SCAFFOLD or bool(_SCAFFOLD_RE.search(path))


def significant_added(diff_text: str) -> dict:
    """path -> set of stripped added lines that carry content (product files only)."""
    out = defaultdict(set)
    cur = None
    for ln in diff_text.splitlines():
        if ln.startswith("+++ "):
            p = ln[4:].strip()
            cur = None if p == "/dev/null" else (p[2:] if p.startswith("b/") else p)
            continue
        if ln.startswith("diff --git"):
            cur = None
            continue
        if cur and ln.startswith("+") and not ln.startswith("+++"):
            s = ln[1:].strip()
            if len(s) >= 10 and not _TRIVIAL.match(s):
                out[cur].add(s)
    return {p: v for p, v in out.items() if not is_scaffold(p) and v}


class RepoIndex:
    def __init__(self, roots=REPO_ROOTS):
        self.repos = []
        for r in roots:
            if not r.is_dir():
                continue
            for c in sorted(r.iterdir()):
                if (c / ".git").exists():
                    self.repos.append(c)
        self._ref, self._files, self._blob = {}, {}, {}

    def ref(self, repo):
        if repo not in self._ref:
            ref = None
            for cand in ("main", "master", "origin/main", "HEAD"):
                if _git(repo, "rev-parse", "--verify", "-q", cand):
                    ref = cand
                    break
            self._ref[repo] = ref
        return self._ref[repo]

    def files(self, repo):
        if repo not in self._files:
            ref = self.ref(repo)
            out = _git(repo, "ls-tree", "-r", "--name-only", ref, timeout=60) if ref else None
            self._files[repo] = set((out or "").splitlines())
        return self._files[repo]

    def blob_lines(self, repo, path):
        k = (repo, path)
        if k not in self._blob:
            ref = self.ref(repo)
            out = _git(repo, "show", f"{ref}:{path}") if ref else None
            self._blob[k] = None if out is None else {x.strip() for x in out.splitlines()}
        return self._blob[k]

    def resolve(self, cwd: str, paths):
        if cwd and os.path.isdir(cwd):
            out = _git(cwd, "rev-parse", "--path-format=absolute", "--git-common-dir")
            if out:
                root = Path(out.strip()).parent
                if root in self.repos or (root / ".git").exists():
                    if root not in self.repos:
                        self.repos.append(root)
                    return root
        best, score = None, 0
        hint = (cwd or "").lower()
        for r in self.repos:
            fs = self.files(r)
            n = sum(1 for p in paths if p in fs)
            if n and r.name.lower() in hint:
                n += 0.5
            if n > score:
                best, score = r, n
        return best


def survival(idx: RepoIndex, cwd: str, added: dict):
    if not added:
        return None, None
    repo = idx.resolve(cwd, list(added))
    if repo is None:
        return None, None
    tot = hit = 0
    for p, lines in added.items():
        cur = idx.blob_lines(repo, p)
        tot += len(lines)
        if cur:
            hit += len(lines & cur)
    return (hit / tot if tot else None), repo.name


def acted_class(a: dict | None) -> str | None:
    if not a:
        return None
    r = (a.get("reason") or "").lower()
    if a.get("signoff_override_reason"):
        return "override-accepted"
    if "reject" in r:
        return "rejected"
    if a.get("merged") and a.get("commit"):
        return "merged-verified"
    if "supersed" in r:
        return "superseded"
    return "acted"


def outcome(surv, act):
    if act in ("rejected",):
        return "rejected"
    if act == "override-accepted":
        return "override-accepted"
    if surv is None:
        return "unresolved"
    if surv >= 0.7:
        return "landed"
    if surv < 0.3:
        return "not-landed"
    return "partial"


def pass_properties(d: dict) -> list:
    """Mechanical weaknesses of a PASS, needing no outcome at all."""
    out = []
    vr = d.get("verify_relevance") or {}
    rv = str(vr.get("verdict"))
    if rv != "relevant":
        out.append(f"relevance-{rv if vr else 'absent'}")
    else:
        ev = vr.get("evidence_mutants")
        if isinstance(ev, int) and ev <= 3:
            out.append("tiny-sample(<=3 mutants)")
        if vr.get("survivors"):
            out.append("relevant-with-survivors")
    if d.get("verify_failed_at_baseline") is not True:
        out.append("no-both-ways-proof")
    rvw = str(d.get("review_verdict") or "")
    if rvw.upper() != "PASS":
        out.append(f"review={rvw or 'none'}")
    if d.get("second_opinion_agreement") == "disagree":
        out.append("cross-family-disagree")
    if (vr.get("source_text_harness") or {}).get("verdict") == "source-text":
        out.append("source-text-fixture")
    if d.get("untrusted"):
        out.append("untrusted-baseline")
    if (vr.get("optout_rejected") or []):
        out.append("optout-rejected")
    return out


# ---------------------------------------------------------------- audit
def audit(recs: dict, acted: dict, slices: dict, idx: RepoIndex | None, since: str = ""):
    rows = []
    chains = defaultdict(list)
    for jid, d in recs.items():
        if since and str(d.get("ts") or "") < since:
            continue
        root = d.get("auto_fix_root") or jid
        rnd = d.get("auto_fix_round") or 0
        chains[root].append((rnd, jid))
    for jid, d in recs.items():
        if since and str(d.get("ts") or "") < since:
            continue
        kind = job_kind(d)
        v = final_verdict(d)
        row = {"id": jid, "label": d.get("job_label"), "ts": d.get("ts"), "kind": kind,
               "verdict": v, "authority": d.get("gate_authority") or (
                   "pregate" if d.get("review_tier") == "pregate" else None),
               "relevance": (d.get("verify_relevance") or {}).get("verdict"),
               "rel_score": (d.get("verify_relevance") or {}).get("score"),
               "evidence": (d.get("verify_relevance") or {}).get("evidence_mutants"),
               "review": d.get("review_verdict"),
               "both_ways": d.get("verify_failed_at_baseline"),
               "ready_to_apply": d.get("auto_pipeline_status") == "ready-to-apply"
               or d.get("auto_pipeline_action") == "apply",
               "diff_base": d.get("diff_base") or (d.get("launch_baseline") or {}).get("head"),
               "judged_id": d.get("judged") or None}
        root = d.get("auto_fix_root") or jid
        rnd = d.get("auto_fix_round") or 0
        later = sorted(j for r, j in chains.get(root, []) if r > rnd)
        row["later_rounds"] = later
        row["acted"] = acted_class(acted.get(jid))
        sl = slices.get(jid) or {}
        row["slice"] = (f"{sl.get('run')}:{sl.get('sid')}={sl.get('status')}"
                        if sl.get("run") else None)
        row["reland_victim"] = bool(sl.get("reland_victim"))
        row["props"] = pass_properties(d) if v == "pass" else []
        surv = repo = None
        added = {}
        if kind == "code" and idx is not None and v in ("pass", "fail", "concerns", "pending"):
            dp = find_side(LOGS, jid, ".diff")
            if dp:
                try:
                    added = significant_added(dp.read_text(errors="replace"))
                except Exception:
                    added = {}
                surv, repo = survival(idx, d.get("job_cwd") or "", added)
        row["survival"] = None if surv is None else round(surv, 2)
        row["repo"] = repo
        row["_added"] = added
        row["outcome"] = outcome(surv, row["acted"])
        rows.append(row)

    by_id = {r["id"]: r for r in rows}
    landed_families = defaultdict(list)
    for r in rows:
        if r["outcome"] in ("landed", "override-accepted"):
            landed_families[base_label(r["label"] or "")].append(r["id"])

    for r in rows:
        cls = []
        if r["kind"] != "code":
            r["classes"] = cls
            continue
        if r["verdict"] == "pass":
            if r["outcome"] == "rejected":
                cls.append("false-PASS:coordinator-rejected")
            if r["reland_victim"]:
                cls.append("false-PASS:slice-relanded")
            if r["later_rounds"]:
                cls.append("false-PASS?:reworked-after-pass")
            fam = [x for x in landed_families.get(base_label(r["label"] or ""), [])
                   if x != r["id"]]
            if r["outcome"] == "not-landed" and fam:
                cls.append("false-PASS?:sibling-landed-instead")
        if r["verdict"] in ("fail", "concerns"):
            landed_as_is = (r["survival"] is not None and r["survival"] >= 0.9) \
                or r["outcome"] == "override-accepted"
            if landed_as_is:
                extra = False
                mine = set().union(*r["_added"].values()) if r["_added"] else set()
                for lj in r["later_rounds"]:
                    lr = by_id.get(lj)
                    if lr and lr["_added"]:
                        theirs = set().union(*lr["_added"].values())
                        if theirs - mine:
                            extra = True
                            break
                if not extra:
                    cls.append("false-BLOCK:landed-as-is")
        r["classes"] = cls
    for r in rows:
        r.pop("_added", None)
    return rows


def summarize(rows):
    s = {}
    code = [r for r in rows if r["kind"] == "code"]
    s["records"] = len(rows)
    s["by_kind"] = dict(Counter(r["kind"] for r in rows))
    s["code_verdicts"] = dict(Counter(r["verdict"] for r in code))
    xt = defaultdict(Counter)
    for r in code:
        xt[r["verdict"]][r["outcome"]] += 1
    s["code_crosstab"] = {k: dict(v) for k, v in xt.items()}
    passes = [r for r in code if r["verdict"] == "pass"]
    pc = Counter()
    for r in passes:
        for p in r["props"]:
            pc[p.split("(")[0]] += 1
    s["pass_count"] = len(passes)
    s["pass_properties"] = dict(pc.most_common())
    s["pass_fully_mechanical"] = sum(1 for r in passes if not r["props"])
    s["ready_to_apply"] = sum(1 for r in passes if r["ready_to_apply"])
    s["ready_to_apply_unproven"] = sum(1 for r in passes if r["ready_to_apply"]
                                       and any(p.startswith("relevance-") for p in r["props"]))
    cc = Counter()
    for r in code:
        for c in r["classes"]:
            cc[c] += 1
    s["classes"] = dict(cc.most_common())
    s["identity_missing"] = sum(1 for r in code if r["verdict"] in ("pass", "fail", "concerns")
                                and not r["judged_id"])
    return s


def render(s, rows, ncand=25):
    out = []
    out.append(f"GATE AUDIT: {s['records']} verdict records  by kind {s['by_kind']}")
    out.append(f"code verdicts: {s['code_verdicts']}")
    out.append("code verdict x outcome (content survival on default branch; acted overrides):")
    for v, c in sorted(s["code_crosstab"].items()):
        out.append(f"  {v:10s} {dict(sorted(c.items()))}")
    out.append(f"PASS (code) = {s['pass_count']}; with NO mechanical weakness = "
               f"{s['pass_fully_mechanical']}; ready-to-apply = {s['ready_to_apply']} "
               f"(of which relevance NOT proven: {s['ready_to_apply_unproven']})")
    out.append(f"PASS weaknesses: {s['pass_properties']}")
    out.append(f"candidate classes: {s['classes']}")
    out.append(f"verdicts without a judged-identity stamp: {s['identity_missing']}")
    cands = [r for r in rows if r["classes"]]
    cands.sort(key=lambda r: str(r["ts"]), reverse=True)
    if cands:
        out.append(f"-- newest {min(ncand, len(cands))} candidates --")
        for r in cands[:ncand]:
            out.append(f"  {r['id']} {r['verdict']:8s} {r['outcome']:17s} surv={r['survival']} "
                       f"{','.join(r['classes'])} | {(r['label'] or '')[:60]} | "
                       f"rel={r['relevance']} props={','.join(r['props'])[:80]}")
    return "\n".join(out)


# ---------------------------------------------------------------- self-test
def _self_test() -> int:
    import tempfile
    ok = True

    def check(name, got, want):
        nonlocal ok
        good = got == want
        ok &= good
        print(("PASS " if good else "FAIL ") + name + ("" if good else f": got {got!r} want {want!r}"))

    diff = ("diff --git a/src/a.py b/src/a.py\n--- a/src/a.py\n+++ b/src/a.py\n@@ -1 +1,3 @@\n"
            "+def compute_total(x):\n+    return x * 2 + offset\n+}\n"
            "diff --git a/verify.sh b/verify.sh\n--- a/verify.sh\n+++ b/verify.sh\n"
            "@@ -0,0 +1 @@\n+python3 test_fixture.py --strict\n")
    sa = significant_added(diff)
    check("significant_added keeps product lines only", sorted(sa), ["src/a.py"])
    check("...and drops trivial lines", len(sa["src/a.py"]), 2)
    check("base_label strips auto-fix + family suffix",
          base_label("rt-x-s1-foo-v2 [auto-fix r1]"), "rt-x-s1-foo")
    check("kind author", job_kind({"job_label": "auto-author-x"}), "author")
    check("kind test by cwd", job_kind({"job_label": "x", "job_cwd": "/a/testprojects/b"}), "test")
    check("kind code", job_kind({"job_label": "rt-fix", "job_cwd": "/x/worktrees/wt-rt"}), "code")
    p = pass_properties({"verdict": "pass", "verify_relevance": {"verdict": "unproven"},
                         "verify_failed_at_baseline": False, "review_verdict": "PASS"})
    check("unproven pass flagged", ("relevance-unproven" in p, "no-both-ways-proof" in p),
          (True, True))
    p = pass_properties({"verdict": "pass", "verify_relevance": {
        "verdict": "relevant", "evidence_mutants": 2, "survivors": []},
        "verify_failed_at_baseline": True, "review_verdict": "PASS"})
    check("tiny sample flagged even when 'relevant'", p, ["tiny-sample(<=3 mutants)"])
    p = pass_properties({"verdict": "pass", "verify_relevance": {
        "verdict": "relevant", "evidence_mutants": 9, "survivors": []},
        "verify_failed_at_baseline": True, "review_verdict": "PASS"})
    check("fully mechanical pass has no weakness", p, [])
    check("outcome: rejected wins over survival", outcome(1.0, "rejected"), "rejected")
    check("outcome: landed", outcome(0.8, None), "landed")
    check("outcome: not-landed", outcome(0.1, None), "not-landed")

    # end-to-end on a throwaway repo: a FAIL whose content landed as-is is a
    # false-BLOCK; a PASS that was later reworked is flagged.
    td = Path(tempfile.mkdtemp(prefix="gate-audit-st-"))
    repo = td / "proj" / "app"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "src").mkdir()
    (repo / "src" / "a.py").write_text("def compute_total(x):\n    return x * 2 + offset\n")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=a@b", "-c", "user.name=a",
                    "commit", "-qm", "x"], check=True)
    logs = td / "logs"
    logs.mkdir()
    (logs / "aaaaaaaaaaaa.diff").write_text(diff)
    (logs / "bbbbbbbbbbbb.diff").write_text(diff.replace("offset", "other_thing"))
    recs = {
        "aaaaaaaaaaaa": {"job_label": "app-fix", "job_cwd": "/gone/wt-app", "verdict": "concerns",
                         "ts": "2026-10-01T00:00:00Z"},
        "bbbbbbbbbbbb": {"job_label": "app-fix2", "job_cwd": "/gone/wt-app", "verdict": "pass",
                         "ts": "2026-10-01T00:00:00Z", "verify_failed_at_baseline": True,
                         "review_verdict": "PASS",
                         "verify_relevance": {"verdict": "relevant", "evidence_mutants": 8}},
        "cccccccccccc": {"job_label": "app-fix2 [auto-fix r1]", "job_cwd": "/gone/wt-app",
                         "verdict": "pass", "auto_fix_root": "bbbbbbbbbbbb", "auto_fix_round": 1,
                         "ts": "2026-10-01T01:00:00Z"},
    }
    global LOGS
    _old = LOGS
    LOGS = logs
    try:
        rows = audit(recs, {}, {}, RepoIndex([td / "proj"]))
    finally:
        LOGS = _old
    by = {r["id"]: r for r in rows}
    check("content that landed verbatim -> survival 1.0", by["aaaaaaaaaaaa"]["survival"], 1.0)
    check("concerns whose code landed as-is -> false-BLOCK",
          by["aaaaaaaaaaaa"]["classes"], ["false-BLOCK:landed-as-is"])
    check("changed content -> partial survival", by["bbbbbbbbbbbb"]["outcome"], "partial")
    check("pass followed by an auto-fix round -> reworked-after-pass",
          "false-PASS?:reworked-after-pass" in by["bbbbbbbbbbbb"]["classes"], True)
    print("SELF-TEST", "OK" if ok else "FAILED")
    return 0 if ok else 1


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--since", default="")
    ap.add_argument("--candidates", type=int, default=25)
    ap.add_argument("--no-git", action="store_true", help="skip content-survival outcome")
    ap.add_argument("--self-test", action="store_true")
    a = ap.parse_args(argv)
    if a.self_test:
        return _self_test()
    recs = load_records()
    rows = audit(recs, load_acted(), load_slice_jobs(), None if a.no_git else RepoIndex(),
                 since=a.since)
    s = summarize(rows)
    if a.json:
        print(json.dumps({"summary": s, "rows": rows}, indent=1, default=str))
    else:
        print(render(s, rows, a.candidates))
    return 0


if __name__ == "__main__":
    sys.exit(main())
