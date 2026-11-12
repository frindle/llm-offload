#!/usr/bin/env python3
"""test-harness-repair.py -- IN-PLACE HARNESS REPAIR of a parked slice (2026-10-10).

The exact state shape of aw-codec-floor/s3: slice ESCALATED with "HARNESS CONVERGED,
PREFLIGHT NO-GO -- worktree KEPT ... last blockers=['verify-relevance']", an esc-review
ending `ESC_RESULT: {"verdict": "c"}` and no proposed diff, a kept worktree. Asserts:
  * exactly ONE repair job is enqueued (label auto-refine-<plan>-<sid>-r101, slice bundle,
    the slice's own model, the kept worktree, a verify = auto-harness-check AND the
    relevance-gate check, a prompt that carries the exact survivors + the review's reasoning);
  * idempotent: nothing more while it is in flight (ledger OR a live queue row);
  * the sweep waits while live, launches the EXISTING --regate on a passed job (never
    --retry-slice; the worktree survives), reverts an out-of-scope edit and does not regate;
  * caps at HARNESS_REPAIR_MAX=2, then ONE deduped alert and the existing ladder (no 3rd job);
  * nothing happens for any other park cause (authoring, no survivors, other blockers, a
    hand-locked tree, a non-relevance verdict-c reason, ACTIONS-OFF).
Hermetic: injected queue / survey / launcher; temp ledger, esc dir, decisions log.
Usage: test-harness-repair.py [--bin DIR]. Prints HARNESS_REPAIR_TEST_OK.
"""
import argparse, importlib.util, json, os, subprocess, sys, tempfile
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=str(Path.home() / "bin"))
BIN = Path(ap.parse_args().bin)
R = []


def check(name, got, want):
    ok = got == want
    R.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def load(path, name):
    sp = importlib.util.spec_from_file_location(name, str(path))
    m = importlib.util.module_from_spec(sp)
    sys.modules[name] = m
    sp.loader.exec_module(m)
    return m


sh = load(BIN / "dispatch-self-heal.py", "sh_repair_t")
hr = sh._hr()

CONVERGED = ("HARNESS CONVERGED, PREFLIGHT NO-GO -- worktree KEPT (not re-authored): preflight "
             "NO-GO every round, last blockers=['verify-relevance']. Fix the named blocker in /w, "
             "then resume with: ollama-dispatch-slice p.json --regate s1")
REVIEW_C = ("The fixture passes under BOTH baseline and refimpl because incoming_name==new_filename.\n"
            "VERDICT: c -- harness defect\n"
            'ESC_RESULT: {"verdict": "c", "confidence": "high", "why": "names identical"}\n\n---\n'
            "proposed fix: none written (no fix proposed)\n")
SURV = [{"file": "arr-webhook.py", "line": 3071, "mutation": "cmp-flip >= -> >", "class": "boundary",
         "snippet": "if incoming_codec and codec_rank(name) > incoming_codec:"}]
SURVEY_BLOCKED = {"parsed": True, "verdict": "NO-GO", "relevance_blocked": True, "survivors": SURV,
                  "other_blockers": [], "relevance_verdict": "low", "reason": "LOW relevance 1/1"}
SURVEY_CLEAN = {"parsed": True, "verdict": "GO", "relevance_blocked": False, "survivors": [],
                "other_blockers": [], "relevance_verdict": "relevant", "reason": ""}


def git(wt, *a):
    return subprocess.run(["git", "-C", str(wt), *a], capture_output=True, text=True, check=True).stdout


class World:
    def __init__(self, reason=CONVERGED, survey=SURVEY_BLOCKED):
        self.td = Path(tempfile.mkdtemp(prefix="hrepair-"))
        td = self.td
        self.wt = td / "wt"
        self.wt.mkdir()
        subprocess.run(["git", "init", "-q", str(self.wt)], check=True)
        git(self.wt, "config", "user.email", "t@t")
        git(self.wt, "config", "user.name", "t")
        (self.wt / "test_fixture.py").write_text("assert 1\n")
        (self.wt / "refimpl.py").write_text("print('ref')\n")
        (self.wt / "TASK.md").write_text("spec\n")
        (self.wt / "target.py").write_text("x = 1\n")
        (self.wt / ".dispatch-harness.json").write_text(json.dumps({
            "authored": ["TASK.md", "refimpl.py", "test_fixture.py", "verify.sh"],
            "fixture": "test_fixture.py", "target": "target.py"}))
        git(self.wt, "add", "-A")
        git(self.wt, "commit", "-qm", "seal")
        self.runs = td / "runs"
        self.runs.mkdir()
        self.plan = td / "plan.json"
        self.plan.write_text("{}")
        self.reason = reason
        self.write_state()
        self.led, self.ctx, self.esc = td / "heal.json", td / "ctx.md", td / "esc"
        self.ctx.write_text("# ctx\n")
        self.enq, self.launched, self.alerts, self.jobs = [], [], [], []
        self.survey = survey
        sh.DECISIONS, sh.ESC_DIR = td / "decisions.jsonl", self.esc
        sh.heal._notifier = lambda t, m, **k: self.alerts.append((m, k.get("dedupe_key")))
        sh.heal._verdict_cache = td / "vcache.json"
        sh.heal._review_path = None
        self.sidecars = {"a1": {"id": "a1", "model": "qwen3.8:27b-q4_K_M", "host_pref": "studio",
                                "num_ctx": 131072}}
        self.n_id = 0

        def enqueue(argv):
            self.n_id += 1
            self.enq.append(argv)
            jid = "rep%09d" % self.n_id
            return jid, "enqueued %s" % jid
        self.hooks = {"jobs": self.jobs, "enqueue": enqueue, "auto_mod": False,
                      "survey": lambda wt, reqs: self.survey, "esc_dir": self.esc,
                      "slice_runs": self.runs, "sidecar": lambda j: self.sidecars.get(j),
                      "launch": lambda pp, args, lp: self.launched.append(list(args)),
                      "notifier": lambda t, m, **k: self.alerts.append((m, k.get("dedupe_key")))}
        sh.heal._repair_hooks = self.hooks

    def write_state(self, status="escalated"):
        (self.runs / "p.json").write_text(json.dumps({
            "label": "p", "plan_path": str(self.plan), "target": "target.py", "lang": "python",
            "bundle": "p-bundle", "slices": {"s1": {
                "status": status, "worktree": str(self.wt), "escalation_reason": self.reason,
                "must_contain": ["incoming_codec"], "author_job_ids": ["a1"]}}}))

    def heal(self, review=REVIEW_C):
        return sh.heal("p", "s1", self.ctx, review, self.runs, self.led,
                       lambda pp, args, lp: self.launched.append(list(args)))

    def finish(self, jid, status="done", exit_code=0):
        self.jobs[:] = [j for j in self.jobs if j["id"] != jid]
        self.sidecars[jid] = {"id": jid, "status": status, "exit_code": exit_code}
        self.hooks["sidecar"] = lambda j: self.sidecars.get(j)

    def ledger(self):
        return (json.loads(self.led.read_text()) if self.led.exists() else {}).get("p/s1", {})


# ---- pure cause table --------------------------------------------------------------------
C = sh.harness_repair_cause
check("cause: converged NO-GO naming verify-relevance",
      C(CONVERGED, "c", "regate"), "converged-nogo-verify-relevance")
check("cause: converged NO-GO needs no verdict",
      C(CONVERGED, None, "regate"), "converged-nogo-verify-relevance")
check("cause: verdict c + LOW relevance NO-GO",
      C("mechanical relevance gate (preflight) NO-GO -- verify-relevance: LOW relevance 3/9", "c", "regate"),
      "review-c-relevance-survivors")
check("cause: same NO-GO under verdict b is NOT ours",
      C("mechanical relevance gate (preflight) NO-GO -- verify-relevance: LOW relevance", "b", "regate"), None)
check("cause: converged NO-GO on another blocker (baseline) is NOT ours",
      C("HARNESS CONVERGED, PREFLIGHT NO-GO -- worktree KEPT: last blockers=['baseline-clean']", "c", "regate"), None)
check("cause: authoring-class reason is NOT ours",
      C("authoring made no progress", "c", "retry"), None)
check("cause: PIPELINE BUG SUSPECTED is NOT ours",
      C("PIPELINE BUG SUSPECTED: " + CONVERGED, "c", "regate"), None)
check("cause: 'already satisfied' is NOT ours",
      C("already satisfied; " + CONVERGED, "c", "none"), None)

# ---- 1. the exact state shape: exactly ONE job, right shape -------------------------------
w = World()
got = w.heal()
check("1 parked converged slice + verdict c + no diff -> a repair job is enqueued", got, "repair:rep000000001")
check("1 exactly one enqueue", len(w.enq), 1)
a = w.enq[0]
opt = lambda k: a[a.index(k) + 1]
check("1 label is the refine-round lane r101", opt("--label"), "auto-refine-p-s1-r101")
check("1 stamped with the slice's bundle", opt("--bundle"), "p-bundle")
check("1 runs in the KEPT worktree", opt("--cwd"), str(w.wt))
check("1 local model of the slice's author job", (opt("--model"), opt("--host")),
      ("qwen3.8:27b-q4_K_M", "studio"))
check("1 coding task kind", opt("--task-kind"), "coding")
check("1 verify = auto-harness-check AND the relevance-gate check",
      "python3 auto-harness-check.py &&" in opt("--verify") and "harness-repair-verify.py" in opt("--verify")
      and "--require=incoming_codec" in opt("--verify"), True)
task = Path(opt("--task-file")).read_text()
check("1 prompt carries the exact survivor", "arr-webhook.py:3071" in task and "cmp-flip >= -> >" in task, True)
check("1 prompt carries the review's reasoning", "incoming_name==new_filename" in task, True)
check("1 prompt confines the edit to the fixture", "Edit `test_fixture.py` and NOTHING else" in task, True)
check("1 NO --retry-slice / --regate yet; worktree intact", (w.launched, w.wt.is_dir()), ([], True))
rp = w.ledger().get("repairs", {})
check("1 ledger: n=1, one OPEN job", (rp.get("n"), [j["state"] for j in rp.get("jobs", [])]), (1, ["open"]))
check("1 does not burn the existing attempt budget", w.ledger().get("attempts"), 0)

# ---- 2. idempotent -----------------------------------------------------------------------
check("2 second heal while in flight -> wait, no 2nd job", w.heal().startswith("wait:"), True)
check("2 still exactly one enqueue", len(w.enq), 1)
w2 = World()
w2.jobs.append({"id": "live1", "label": "auto-refine-p-s1-r101", "status": "pending"})
check("2 a LIVE queue row (ledger lost) also blocks a duplicate", w2.heal().startswith("wait:"), True)
check("2 ...and enqueues nothing", w2.enq, [])

# ---- 3. sweep: live -> wait ---------------------------------------------------------------
w.jobs.append({"id": "rep000000001", "label": "auto-refine-p-s1-r101", "status": "running"})
check("3 sweep while the job is live -> wait, nothing launched",
      [x[1][:5] for x in sh.harness_repair_sweep(w.led, w.runs, w.hooks)] + [w.launched], ["wait:", []])

# ---- 4. sweep: passed -> existing --regate, never retry -----------------------------------
w.finish("rep000000001")
(w.wt / "test_fixture.py").write_text("assert 1\nassert 2  # new case\n")
res = sh.harness_repair_sweep(w.led, w.runs, w.hooks)
check("4 passed + only the fixture changed -> --regate s1", (res[0][1], w.launched), ("regate", [["--regate", "s1"]]))
check("4 the worktree was NOT deleted and the fixture edit is intact",
      (w.wt.is_dir(), "new case" in (w.wt / "test_fixture.py").read_text()), (True, True))
check("4 job closed as passed", w.ledger()["repairs"]["jobs"][0]["state"], "passed")
check("4 sweep is idempotent (no 2nd regate)", (sh.harness_repair_sweep(w.led, w.runs, w.hooks), len(w.launched)), ([], 1))

# ---- 5. caps at 2, then park + alert ------------------------------------------------------
w = World()
w.heal()
w.finish("rep000000001", "failed", 1)
res = sh.harness_repair_sweep(w.led, w.runs, w.hooks)
check("5 failed attempt 1 -> the 2nd attempt is enqueued", (res[0][1], len(w.enq)), ("repair:rep000000002", 2))
check("5 2nd label r102", w.enq[1][w.enq[1].index("--label") + 1], "auto-refine-p-s1-r102")
w.finish("rep000000002", "failed", 1)
res = sh.harness_repair_sweep(w.led, w.runs, w.hooks)
check("5 failed attempt 2 -> NO 3rd job; park + ONE deduped alert",
      (res[0][1], len(w.enq), len(w.alerts)), ("park:final-rung", 2, 1))
check("5 no lever pulled (no --retry-slice/--regate), worktree intact", (w.launched, w.wt.is_dir()), ([], True))
check("5 ledger n capped at 2", w.ledger()["repairs"]["n"], 2)
got = w.heal()
check("5 a later heal after the cap is the EXISTING ladder, never a 3rd repair",
      (str(got).startswith("repair:"), len(w.enq)), (False, 2))

# ---- 6. out-of-scope edit is reverted and not regated -------------------------------------
w = World()
w.heal()
w.finish("rep000000001")
(w.wt / "test_fixture.py").write_text("assert 1\nassert 2\n")
(w.wt / "target.py").write_text("x = 999  # weakened the target\n")
res = sh.harness_repair_sweep(w.led, w.runs, w.hooks)
check("6 a passed job that edited the TARGET does not --regate", [l for l in w.launched if "--regate" in l], [])
check("6 the out-of-scope edit is reverted", (w.wt / "target.py").read_text(), "x = 1\n")
check("6 treated as a failed attempt (next repair enqueued)", (res[0][1], len(w.enq)), ("repair:rep000000002", 2))

# ---- 7. nothing for other park causes ------------------------------------------------------
def none_for(name, w, review=REVIEW_C, want_prefix=None):
    got = w.heal(review)
    check("7 %s -> no repair job" % name, (str(got).startswith("repair:"), w.enq), (False, []))


none_for("authoring-class park", World(reason="authoring made no progress (3 rounds)"))
none_for("converged NO-GO on another blocker",
         World(reason="HARNESS CONVERGED, PREFLIGHT NO-GO -- worktree KEPT: last blockers=['baseline-clean']"))
none_for("relevance NO-GO under verdict b",
         World(reason="mechanical relevance gate (preflight) NO-GO -- verify-relevance: LOW relevance"),
         "VERDICT: b -- spec\nESC_RESULT: {\"verdict\": \"b\", \"why\": \"x\"}\n")
none_for("gate FAIL on the coding job", World(reason="coding job gate verdict FAIL: reviewer-code-high"))
nos = World(survey=dict(SURVEY_BLOCKED, survivors=[]))
none_for("relevance gate that names NO survivors (unproven / source-text)", nos)
oth = World(survey={"parsed": False, "survivors": [], "relevance_blocked": False, "reason": "no json"})
none_for("unparseable preflight", oth)
hl = World()
(hl.wt / sh.HAND_LOCK).write_text("x")
none_for("HAND-LOCKED worktree", hl)
gone = World()
import shutil
shutil.rmtree(gone.wt)
none_for("worktree gone", gone)
off = World()
os.environ["ESC_ACTIONS"] = "off"
none_for("ACTIONS-OFF kill switch", off)
del os.environ["ESC_ACTIONS"]
nw = World(reason="HARNESS CONVERGED, PREFLIGHT NO-GO -- worktree KEPT: last blockers=['verify-relevance']")
nw.write_state(status="enqueued")
check("7 a slice that is not escalated is untouched", (str(nw.heal()).startswith("skip:"), nw.enq), (True, []))

# ---- 8. already-clean harness: one re-gate, once ------------------------------------------
w = World(survey=SURVEY_CLEAN)
check("8 relevance already clean on the kept harness -> a single --regate",
      (w.heal(), w.launched, w.enq), ("regate", [["--regate", "s1"]], []))
w.launched.clear()
w.heal()
check("8 ...and never a second repair-regate (a still-NO-GO tree goes to the existing ladder)",
      [l for l in w.launched if "--regate" in l], [])

# ---- 9. harness_repair helpers --------------------------------------------------------------
pj = {"verdict": "NO-GO", "blockers": [{"check": "verify-relevance", "message": "LOW relevance"},
                                         {"check": "cwd-exclusive", "message": "busy"}],
      "verify_relevance": {"verdict": "low", "survivors": SURV}}
sv = hr.survey_from_payload(hr.parse_preflight_json("banner\n" + json.dumps(pj)))
check("9 survey: relevance blocked + survivor + the other blocker kept apart",
      (sv["relevance_blocked"], len(sv["survivors"]), [b["check"] for b in sv["other_blockers"]]),
      (True, 1, ["cwd-exclusive"]))
check("9 survey_clean is False for a low verdict, True only for 'relevant'",
      (hr.survey_clean(sv), hr.survey_clean(SURVEY_CLEAN), hr.survey_clean({"parsed": True, "relevance_verdict": None})),
      (False, True, False))
check("9 unparseable -> not parsed", hr.survey_from_payload(hr.parse_preflight_json("garbage"))["parsed"], False)
w = World()
head = git(w.wt, "rev-parse", "HEAD").strip()
(w.wt / "test_fixture.py").write_text("assert 2\n")
(w.wt / "__pycache__").mkdir()
(w.wt / "__pycache__" / "x.pyc").write_text("x")
(w.wt / "auto-harness-check.py").write_text("x")
check("9 scope: fixture edit + harness noise are in scope", hr.scope_violations(w.wt, head, ["test_fixture.py"]), [])
(w.wt / "refimpl.py").write_text("changed\n")
check("9 scope: refimpl edit is a violation", hr.scope_violations(w.wt, head, ["test_fixture.py"]), ["refimpl.py"])
check("9 fixture_files never offers refimpl/TASK.md/target", hr.fixture_files(w.wt, "target.py"), ["test_fixture.py"])

print("HARNESS_REPAIR_TEST_OK" if all(R) else "HARNESS_REPAIR_TEST_FAILED")
sys.exit(0 if all(R) else 1)
