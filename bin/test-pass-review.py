#!/usr/bin/env python3
"""Sampled pass review (plan section 5): mini-scorecard picks about 1 in 5 passed mini-swe FIX jobs, the queue-events deliverer
marks a picked pass's event with `review=~/bin/mini-scorecard review brief <id>`, the brief says what to read and how to
record the verdict, and the verdicts feed the review-reject rate.

Hermetic: temp queue logs, temp OFFLOAD_HOME, temp QEL_* dirs. No network, no queue, no daemon.
Run: python3 test-pass-review.py [--scorecard PATH] [--deliver PATH] [--mutants]     (exit 0 = ALL PASSED)
"""
import contextlib, importlib.util, io, json, os, re, subprocess, sys, tempfile, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
A = sys.argv
SC_PATH = Path(A[A.index("--scorecard") + 1]) if "--scorecard" in A else HERE / "mini-scorecard"
DL_PATH = Path(A[A.index("--deliver") + 1]) if "--deliver" in A else HERE / "queue-events-deliver.py"
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(name, path):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    ld.exec_module(m)
    return m


def run_checks():
    tmp = Path(tempfile.mkdtemp(prefix="pass-review-test-"))
    q, home, runs, qd = tmp / "q", tmp / "home", tmp / "runs", tmp / "qd"
    for p in (q, q / "archive", home, runs, qd):
        p.mkdir(parents=True, exist_ok=True)
    os.environ.update(OFFLOAD_HOME=str(home), OFFLOAD_QUEUE_LOGS=str(q), OFFLOAD_RUNS=str(runs), QEL_DIR=str(qd),
                      QEL_LOG_DIR=str(q), QEL_STATE_PATH=str(tmp / "state.json"), QEL_SCORECARD=str(SC_PATH))
    for k in ("MINI_SCORECARD", "MINI_SAMPLE_PCT"):
        os.environ.pop(k, None)
    S = load("sc_under_test", SC_PATH)
    D = load("deliver_under_test", DL_PATH)
    assert (D.LOG_DIR, S.QLOGS) == (str(q), q), "nothing may point at the real queue"

    ids = [f"j{i:03d}" for i in range(400)]
    PICK = [i for i in ids if S.picked(i, 20)]
    SKIP = [i for i in ids if not S.picked(i, 20)]
    pk = iter(PICK)

    def spec(name, **kw):
        p = tmp / f"{name}.spec.json"
        p.write_text(json.dumps({"mode": "two-phase", "allowed": ["a.py"], **kw}))
        return str(p)

    def job(jid, bundle="famA", ok=True, runner="/Users/x/bin/mini-swe-runner.py", task_file=None, where=q, packet=True, cwd=True, nr=None):
        run = runs / f"{jid}-run"
        (run / "work").mkdir(parents=True, exist_ok=True)
        if packet:
            (run / "REVIEW.md").write_text("# REVIEW packet: PASS test_green=ok fn_scope=ok\n\n## Diff\n")
        d = {"id": jid, "label": f"{bundle}-{jid}", "bundle": bundle, "runner": runner, "status": "done" if ok else "failed",
             "runner_verdict": "PASS" if ok else "FAIL", "task_file": task_file or spec("fix"), "active_s": 100.0,
             "cwd": str(run / "work") if cwd else "", "persisted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
        if nr:   # what ollama-queue.py records for the runner's `RESULT PASS needs-review soft=<gates>`
            d.update(runner_verdict=f"NEEDS-REVIEW (soft: {','.join(nr)})", needs_review=True, needs_review_gates=list(nr))
        (where / f"{jid}.done.json").write_text(json.dumps(d))
        return d

    # --- what is a fix job, and what the sample picks
    check("kind: unreadable spec counts as fix", S.job_kind({"task_file": str(tmp / "missing.json")}), "fix")
    check("kind: no task file counts as fix", S.job_kind({}), "fix")
    check("kind: a two-phase spec is a fix", S.job_kind({"task_file": spec("k1")}), "fix")
    check("kind: phase_a_only is a test draft", S.job_kind({"task_file": spec("k2", phase_a_only=True)}), "test_draft")
    check("kind: a chain step with phase_a_only is a test draft",
          S.job_kind({"task_file": spec("k3", steps=[{"task": "a"}, {"task": "b", "phase_a_only": True}])}), "test_draft")
    check("kind: diagnose", S.job_kind({"task_file": spec("k4", mode="diagnose")}), "diagnose")

    fix_p, fix_s = job(next(pk)), job(SKIP[0])
    check("pick: a passed fix job the hash selects", S.review_pick(fix_p)[:2], (True, "famA"))
    check("pick: a passed fix job the hash skips", S.review_pick(fix_s)[0], False)
    check("pick: a failed job is never picked", S.review_pick(job(next(pk), ok=False))[0], False)
    check("pick: a job of another runner is never picked", S.review_pick(job(next(pk), runner="/x/ollama-worker.py"))[0], False)
    draft = job(next(pk), task_file=spec("d1", phase_a_only=True))
    check("pick: a test draft is never picked (Claude reads all of them)", S.review_pick(draft)[0], False)
    diag = job(next(pk), task_file=spec("g1", mode="diagnose"))
    check("pick: a diagnosis is never picked", S.review_pick(diag)[0], False)
    nr_s = job(SKIP[1], nr=["fix_hunks", "must_not_contain"])
    check("pick: a needs-review pass is always picked, even when the hash skips it", S.review_pick(nr_s)[:3], (True, "famA", 100))
    check("pick: a needs-review test draft is still not picked",
          S.review_pick(job(SKIP[2], nr=["fix_hunks"], task_file=spec("d2", phase_a_only=True)))[0], False)
    check("pick: a needs-review pass of another runner is never picked",
          S.review_pick(job(SKIP[3], nr=["fix_hunks"], runner="/x/ollama-worker.py"))[0], False)
    os.environ["MINI_SCORECARD"] = "off"
    check("pick: the kill switch drops needs-review picks too", S.review_pick(nr_s)[0], False)
    del os.environ["MINI_SCORECARD"]
    jrow = {j["id"]: j for j in S.load_jobs(14)}[nr_s["id"]]
    check("jobs: a needs-review pass is a pass, flagged nr, not a failure", (jrow["ok"], jrow["nr"], jrow["failed"]), (True, True, False))
    check("pick: a family the throttle put at 100% picks every pass",
          S.review_pick(fix_s, {"families": {"famA": {"sample_pct": 100}}})[:3], (True, "famA", 100))
    gon = {"global": {"sample_pct": 100, "since": "2026-11-01T00:00:00Z"}}
    check("pick: the review-all switch does not reach a pass that finished before it turned on",
          S.review_pick(dict(fix_s, persisted_at="2026-10-31T00:00:00Z"), gon)[:3], (False, "famA", 20))
    check("pick: the review-all switch picks a pass that finished after it turned on", S.review_pick(fix_s, gon)[:3], (True, "famA", 100))
    check("pick: a pass with no finish time is under the switch", S.review_pick(dict(fix_s, persisted_at=None), gon)[2], 100)
    os.environ["MINI_SCORECARD"] = "off"
    check("pick: the kill switch picks nothing", S.review_pick(fix_p)[0], False)
    del os.environ["MINI_SCORECARD"]

    # --- the queue event of a picked pass carries the review command
    def ev(jid, state="passed", seq=1):
        return {"seq": seq, "id": jid, "label": f"famA-{jid}", "state": state, "verdict": "PASS" if state == "passed" else "FAIL"}

    hint = f" review=`~/bin/mini-scorecard review brief {fix_p['id']}`"
    check("event: a picked pass carries review=", D.fmt(ev(fix_p["id"]), "yours").endswith(hint), True)
    check("event: a skipped pass has no review=", "review=" in D.fmt(ev(fix_s["id"]), "yours"), False)
    check("event: a failed event never has review=", "review=" in D.fmt(ev(fix_p["id"], "failed"), "yours"), False)
    nr_ev = dict(ev(nr_s["id"], "needs-review"), verdict="NEEDS-REVIEW (soft: fix_hunks,must_not_contain)")
    check("event: a needs-review event always carries review=",
          D.fmt(nr_ev, "yours").endswith(f" review=`~/bin/mini-scorecard review brief {nr_s['id']}`"), True)
    check("event: the header says a needs-review pass is never landed unreviewed",
          all(x in D.HEADER for x in ("needs-review:", "never land it unreviewed")), True)
    check("event: a test draft's pass has no review=", "review=" in D.fmt(ev(draft["id"]), "yours"), False)
    check("event: no record, no review=", "review=" in D.fmt(ev(next(pk)), "yours"), False)
    arch = job(next(pk), where=q / "archive")
    check("event: a record already archived still counts", "review=" in D.fmt(ev(arch["id"]), "orphan"), True)
    check("event: an id that is not a plain name is never looked up", D.review_hint(ev("../" + fix_p["id"])), None)
    D._SCORECARD.clear()
    os.environ["QEL_SCORECARD"] = str(tmp / "no-such-scorecard")
    check("event: a broken scorecard means no hint, not a failed delivery", D.fmt(ev(fix_p["id"]), "yours").endswith("PASS"), True)
    os.environ["QEL_SCORECARD"] = str(SC_PATH)
    D._SCORECARD.clear()
    check("event: the header says what review= asks and forbids the API", all(x in D.HEADER for x in ("review=", "no API", "1-in-5")), True)
    lines = D.deliver([ev(fix_p["id"], seq=1), ev(fix_s["id"], seq=2)], "sid-1", None, None)[0]
    check("deliver: the hook/watch lines carry the hint for the picked pass only",
          ["review=" in x for x in lines], [True, False])

    # --- an auto pass: the packet rides in the event itself, ending in the one-step verdict + land
    au = job(SKIP[4], task_file=spec("au", mode="auto"))
    apkt = Path(au["cwd"]).parent / "REVIEW-AUTO.md"
    apkt.write_text("# AUTO review: shop\n\nRESULT PASS mode=auto target=a.py:f\n\n## Test `test_a.py` (model-written)\n\n```\n"
                    "def test_f():\n    assert f(10) == 90\n```\n\n## Fix diff\n\n```diff\n+    if qty >= 10:\n```\n\n"
                    "## Files\n\n- packets: /x/REVIEW.md\n\n## Review asks\n\n1. Does the test assert ...\n")
    at = D.fmt(ev(au["id"]), "yours")
    first, _, rest = at.partition("\n")
    check("auto event: the first line says the packet is inline (no brief call needed)",
          first.endswith("review=inline (packet below)") and "review brief" not in first, True)
    check("auto event: the packet's test and diff follow the line",
          all(x in rest for x in ("assert f(10) == 90", "+    if qty >= 10:", str(apkt), "end of packet")), True)
    check("auto event: the file list and review asks are replaced by the one-step command",
          ("## Files" in rest, "## Review asks" in rest, f"~/bin/mini-auto review {au['id']} ok --note" in rest,
           f"~/bin/mini-auto review {au['id']} reject --note" in rest, "Test first" in rest), (False, False, True, True, True))
    check("auto event: a failed auto job carries no packet", "\n" in D.fmt(ev(au["id"], "failed"), "yours"), False)
    apkt.write_text("# AUTO review: big\n\n" + "x" * (D.AUTO_PKT_CAP + 500) + "\n")
    big = D.fmt(ev(au["id"]), "yours")
    check("auto event: an oversized packet is cut, naming the whole file", f"cut at {D.AUTO_PKT_CAP} chars" in big
          and len(big) < D.AUTO_PKT_CAP + 2000, True)
    apkt.unlink()
    check("auto event: no packet file falls back to the brief hint",
          D.fmt(ev(au["id"]), "yours").endswith(f"review=`~/bin/mini-scorecard review brief {au['id']}`"), True)
    apkt.write_text("# AUTO review: shop\n\nRESULT PASS mode=auto\n")
    lines = D.deliver([ev(au["id"], seq=3)], "sid-1", None, None)[0]
    check("deliver: an auto pass is one delivered entry holding the packet", (len(lines), "end of packet" in lines[0]), (1, True))
    check("header: points auto jobs at the one-step review", "mini-auto review <id> ok|reject" in D.HEADER, True)

    # --- the brief
    def brief(jid):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            rc = S.main(["review", "brief", jid])
        return rc, out.getvalue()

    rc, txt = brief(fix_p["id"])
    pkt = str(Path(fix_p["cwd"]).parent / "REVIEW.md")
    check("brief: exit 0", rc, 0)
    check("brief: names the REVIEW.md next to the work dir, with its gate line",
          bool(re.search(re.escape(pkt) + r" .*# REVIEW packet: PASS test_green=ok", txt)), True)
    check("brief: says the job was picked", "(picked at 20%" in txt, True)
    check("brief: gives both record commands for this job",
          all(f"review add --job {fix_p['id']} --verdict {v}" in txt for v in ("ok", "reject")), True)
    check("brief: subscription only", "never the" in txt and "claude -p" in txt, True)
    check("brief: no record -> exit 1", brief("nojob")[0], 1)
    rc, nrt = brief(nr_s["id"])
    check("brief: a needs-review pass names its soft gates and what each one means",
          (rc, "NEEDS REVIEW: soft gate(s) failed: fix_hunks, must_not_contain" in nrt, "fix_hunks: a code hunk no test needs" in nrt,
           "must_not_contain: text TASK.md says must go" in nrt, "(picked at 100%" in nrt), (0, True, True, True, True))
    check("brief: a plain pass has no needs-review line", "NEEDS REVIEW" in brief(fix_p["id"])[1], False)
    cwdless = job(next(pk), cwd=False)
    here = os.getcwd()
    os.chdir(runs / f"{fix_p['id']}-run")      # a REVIEW.md in the current dir must not be taken for the job's
    try:
        rc, txt2 = brief(cwdless["id"])
    finally:
        os.chdir(here)
    check("brief: a record without a work dir says there is no packet", "packet: none" in txt2 and "# REVIEW packet" not in txt2, True)
    nopkt = job(next(pk), packet=False)
    check("brief: a missing packet points at the work dir's diff", "git -C <work> diff" in brief(nopkt["id"])[1], True)

    job(SKIP[4], bundle="famQ", nr=["must_not_contain"])   # famQ: no reject, so only the needs-review flag can list it

    # --- recording verdicts
    with contextlib.redirect_stdout(io.StringIO()):
        S.main(["review", "add", "--job", fix_p["id"], "--verdict", "reject", "--note", "dead code"])
        S.main(["review", "add", "--job", fix_p["id"], "--verdict", "ok", "--note", "re-reviewed after the fix"])
        S.main(["review", "add", "--job", fix_s["id"], "--verdict", "ok", "--family", "famZ"])
        S.main(["review", "add", "--job", "rsch1", "--verdict", "reject", "--kind", "research", "--family", "famR"])
        S.main(["review", "add", "--job", nr_s["id"], "--verdict", "reject", "--note", "stale comment left"])
    rows = S.read_jsonl(S.REVIEWS)
    check("add: a needs-review job's verdict is stamped nr, a plain pass's is not", (rows[4].get("nr"), "nr" in rows[0]), (True, False))
    check("add: the family comes from the job's record", rows[0].get("family"), "famA")
    check("add: an explicit --family wins", rows[2].get("family"), "famZ")
    tot = S.compute(14)["totals"]
    check("rate: a job reviewed twice counts once, as a reject (an ok re-review after the fix does not erase the miss)",
          (tot["rev_n"], tot["rev_bad"]), (2, 1))
    check("rate: a research review is not a pass review", S.global_sample(rows)["reason"].startswith("review rejects 1/2 "), True)
    check("rate: a needs-review reject is counted on its own line, not in the sampled-pass rate",
          (tot["nr_rev_n"], tot["nr_rev_bad"], tot["nr"] >= 1), (1, 1, True))
    check("rate: the review-all switch ignores needs-review verdicts",
          S.global_sample([dict(r, job=f"x{i}", nr=True, verdict="reject") for i, r in enumerate(rows * 10)])["reason"],
          "review rejects 0/0 of the latest reviews (too few to judge)")
    check("brief: a reviewed job says so, and that it still counts as a reject",
          all(x in brief(fix_p["id"])[1] for x in ("ALREADY REVIEWED: ok", "still counted as a reject")), True)

    # --- the pending list
    out = io.StringIO()
    with contextlib.redirect_stdout(out):
        S.main(["sample", "list"])
    listed = {ln.split()[0] for ln in out.getvalue().splitlines() if ln and not ln.startswith("#")}
    check("pending: a reviewed pass is not listed", fix_p["id"] in listed, False)
    check("pending: a picked, unreviewed fix pass is listed", nopkt["id"] in listed and cwdless["id"] in listed, True)
    check("pending: a picked test draft is not listed", draft["id"] in listed, False)
    check("pending: a reviewed needs-review pass is not listed", nr_s["id"] in listed, False)
    check("pending: an unreviewed needs-review pass is listed and marked, whatever the hash says",
          any(ln.startswith(SKIP[4] + " ") and ln.endswith("[needs-review]") for ln in out.getvalue().splitlines()), True)
    check("pending: the footer says how to review one", "review brief <id>" in out.getvalue(), True)
    with contextlib.redirect_stdout(io.StringIO()):
        S.main(["exclude", "add", "--job", SKIP[4], "--reason", "synthetic"])
    check("pick: an excluded needs-review pass is not picked", S.review_pick(S.job_record(SKIP[4]))[0], False)


MUTANTS = [
    ("pick ignores the job kind (test drafts sampled)", "sc", '    if job_kind(d) != "fix":\n        return False, None, 0\n    if EXCLUDED', '    if EXCLUDED'),
    ("pick ignores the verdict (failed jobs sampled)", "sc", 'or not is_pass(d)', 'or False'),
    ("a needs-review pass is only sampled", "sc", '    if is_nr(d):\n        return True, fam, 100      # a soft gate failed: always reviewed, never sampled\n', ''),
    ("a needs-review pass counts as failed", "sc", '(None, "PASS") and not nr)', '(None, "PASS"))'),
    ("needs-review verdicts drive the review-all switch", "sc", 'job_verdicts([r for r in reviews if not r.get("nr")], ("fix",))', 'job_verdicts(reviews, ("fix",))'),
    ("needs-review verdicts count in the sampled-pass rate", "sc", 'if kind == "fix" and r.get("nr"):', 'if False:'),
    ("review add does not stamp nr", "sc", 'if rec and is_nr(rec):', 'if False:'),
    ("the pending list skips unpicked needs-review passes", "sc", 'j["nr"] or picked(', 'picked('),
    ("the brief drops the soft gates", "sc", "    if is_nr(d):\n        gates =", "    if False:\n        gates ="),
    ("the hint skips needs-review events", "dl", 'not in ("passed", "needs-review")', '!= "passed"'),
    ("pick ignores the kill switch", "sc", 'if os.environ.get("MINI_SCORECARD", "on") == "off":\n        return False, None, 0\n    if "mini-swe-runner"', 'if "mini-swe-runner"'),
    ("the event line drops the hint", "dl", 'return f\'#{e["seq"]} {e["state"]} ({tag}): {e.get("label") or "?"} [{e.get("id")}] {extra}\'.rstrip() + fin + rev', 'return f\'#{e["seq"]} {e["state"]} ({tag}): {e.get("label") or "?"} [{e.get("id")}] {extra}\'.rstrip() + fin'),
    ("the hint fires for failed events", "dl", 'if e.get("state") not in ("passed", "needs-review") or not', 'if not'),
    ("the hint does not look in the archive", "dl", ', os.path.join(LOG_DIR, "archive", f"{jid}.done.json")):\n            try:\n                with open(p) as fh:', '):\n            try:\n                with open(p) as fh:'),
    ("a broken scorecard breaks delivery", "dl", '            return f"~/bin/mini-scorecard review brief {jid}"\n    except Exception:\n        pass', '            return f"~/bin/mini-scorecard review brief {jid}"\n    except ValueError:\n        pass'),
    ("ids are not checked before the lookup", "dl", 'or not re.fullmatch(r"[\\w.-]+", jid)', 'or not jid'),
    ("the brief looks for REVIEW.md inside the work dir", "sc", 'Path(work).parent / "REVIEW.md" if work else None', 'Path(work) / "REVIEW.md" if work else None'),
    ("the brief reads ./REVIEW.md when the record has no work dir", "sc", 'Path(work).parent / "REVIEW.md" if work else None', 'Path(work).parent / "REVIEW.md"'),
    ("review add keeps an empty family", "sc", 'fam = a.family or (family_of(rec) if rec else "")', 'fam = a.family'),
    ("a re-review counts twice", "sc", 'for job, r, bad in job_verdicts(reviews, (kind,)):',
     'for job, r, bad in [(x.get("job"), x, x.get("verdict") == "reject") for x in reviews if x.get("kind", "fix") == kind]:'),
    ("an ok re-review erases an earlier reject", "sc", 'bool(prev and prev[1]) or r.get("verdict") == "reject"', 'r.get("verdict") == "reject"'),
    ("research reviews count as pass reviews", "sc", 'if kinds is not None and r.get("kind", "fix") not in kinds:',
     'if kinds is not None and r.get("kind", "fix") not in kinds + ("research",):'),
    ("pick ignores when the pass finished", "sc", 'sample_pct(fam, state, parse_ts(d.get("persisted_at")) or None)', 'sample_pct(fam, state)'),
    ("the pending list keeps test drafts", "sc", ' and job_kind(j) == "fix"]', ']'),
]


def run_mutants():
    sp = Path(tempfile.mkdtemp(prefix="pass-review-mut-"))
    src = {"sc": SC_PATH.read_text(), "dl": DL_PATH.read_text()}
    survived = []
    for name, which, old, new in MUTANTS:
        if src[which].count(old) != 1:
            print(f"MUTANT SETUP: {name}: pattern found {src[which].count(old)} times")
            survived.append(name)
            continue
        files = {"sc": sp / "mini-scorecard", "dl": sp / "queue-events-deliver.py"}
        for k, p in files.items():
            p.write_text(src[k].replace(old, new) if k == which else src[k])
        r = subprocess.run([sys.executable, __file__, "--scorecard", str(files["sc"]), "--deliver", str(files["dl"])],
                           capture_output=True, text=True, timeout=300)
        red = r.returncode != 0
        print(("killed  : " if red else "SURVIVED: ") + name)
        if not red:
            survived.append(name)
    return survived


if __name__ == "__main__":
    if "--mutants" in A:
        s = run_mutants()
        print("ALL MUTANTS KILLED" if not s else f"{len(s)} mutant(s) survived: {s}")
        sys.exit(1 if s else 0)
    run_checks()
    print("ALL PASSED" if not FAILS else f"{len(FAILS)} failure(s)")
    sys.exit(1 if FAILS else 0)
