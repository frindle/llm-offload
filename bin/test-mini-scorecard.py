#!/usr/bin/env python3
"""Tests for mini-scorecard against a fake queue-log dir, a fake run dir and a real temp git repo. No network, no GPU."""
import importlib.machinery, importlib.util, json, os, subprocess, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="scorecard-test-"))
os.environ.update(OFFLOAD_HOME=str(tmp / "home"), OFFLOAD_QUEUE_LOGS=str(tmp / "q"), OFFLOAD_RUNS=str(tmp / "runs"))
(tmp / "q").mkdir(); (tmp / "home").mkdir(); (tmp / "runs").mkdir()
ldr = importlib.machinery.SourceFileLoader("sc", str(HERE / "mini-scorecard"))
sp = importlib.util.spec_from_loader("sc", ldr); S = importlib.util.module_from_spec(sp); ldr.exec_module(S)


def job(i, bundle, ok, active=100.0, label=None):
    d = {"id": i, "label": label or f"{bundle}-{i}", "bundle": bundle, "runner": "/x/mini-swe-runner.py", "status": "done" if ok else "failed",
         "runner_verdict": "PASS" if ok else "FAIL", "active_s": active, "persisted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    (tmp / "q" / f"{i}.done.json").write_text(json.dumps(d))


for i in range(10):
    job(f"a{i:02d}", "famA", True)
job("b00", "famB", False, 900.0)
job("w00", "worker", True)
(tmp / "q" / "w00.done.json").write_text(json.dumps({"id": "w00", "runner": "/x/other.py", "status": "done"}))  # not mini-swe: ignored
(tmp / "runs" / "famA-a00").mkdir()
(tmp / "runs" / "famA-a00" / "VALIDATION.md").write_text("- s1 V5 hunks covered: ok 3/3 ref hunks make the test red\n")

jobs = S.load_jobs(14)
assert len(jobs) == 11 and sum(j["ok"] for j in jobs) == 10, jobs
assert S.picked("x", 100) and not S.picked("x", 0)
n = sum(S.picked(f"a{i:02d}", 20) for i in range(10)); assert 0 <= n <= 10
assert sum(S.picked(f"j{i}", 20) for i in range(1000)) in range(150, 250), "20% sampling is about 20%"

sc = S.compute(14)
m = {x["metric"]: x for x in sc["metrics"]}
assert m["GPU-h on failures / total"]["status"] == "BREACH" and abs(m["GPU-h on failures / total"]["value"] - 900 / 1900) < 1e-9
assert m["V5 kill rate on passed jobs' tests"]["value"] == 1.0 and m["V5 kill rate on passed jobs' tests"]["n"] == 3
assert m["review-reject rate of sampled passes"]["status"] == "n/a"
assert S.main(["report", "--json"]) == 0 and S.main(["report"]) == 0

# ledger + escaped defect against a real git repo
repo = tmp / "repo"; repo.mkdir()
g = lambda *a: subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "-c", "commit.gpgsign=false", *a], capture_output=True, text=True, check=True).stdout
g("init", "-q"); (repo / "f.py").write_text("x=1\n"); g("add", "."); g("commit", "-qm", "feat: add f")
sha = g("rev-parse", "HEAD").strip()
S.main(["ledger", "add", "--job", "a01", "--commit", sha, "--repo", str(repo), "--family", "famA"])
assert S.git_escaped(S.read_jsonl(S.LEDGER)[0])[0] is False
(repo / "f.py").write_text("x=2\n"); g("commit", "-qam", "fix: f was wrong")
esc, why = S.git_escaped(S.read_jsonl(S.LEDGER)[0]); assert esc is True and "fix: f was wrong" in why, (esc, why)
sc = S.compute(14); assert sc["families"]["famA"]["escaped"] == 1
assert {x["metric"]: x for x in sc["metrics"]}["escaped defect rate (14d fix/revert on same files)"]["status"] == "BREACH"
assert S.git_escaped({"repo": "/nonexistent", "commit": "abc"})[0] is None

# reviews + throttle
for i in range(5):
    S.main(["review", "add", "--job", f"a0{i}", "--verdict", "ok" if i else "reject", "--family", "famA"])
th = S.throttle(S.compute(14))
assert th["famA"]["sample_pct"] == 100 and th["famA"]["mode"] == "claude_direct", th["famA"]   # escaped + reject rate = 2 breaches -> drop v2 to claude_direct
assert th["famA"]["mode"] in S.MODES
assert th["famB"]["mode"] == "v2" and th["famB"]["sample_pct"] == 20
S.STATE.write_text(json.dumps({"families": {"famA": {"mode": "test_draft"}}}))  # from test_draft the same 2 breaches drop to v2
assert S.throttle(S.compute(14))["famA"]["mode"] == "v2"
# sensitive escaped defect drops even with a single breach
(repo / "f.py").write_text("x=3\n"); g("commit", "-qam", "revert: oops")
S.LEDGER.write_text(json.dumps({"ts": S.iso(), "job": "a02", "commit": sha, "repo": str(repo), "family": "famA", "note": "bfmr dedup"}) + "\n")
S.REVIEWS.write_text(""); S.STATE.unlink()
th4 = S.throttle(S.compute(14)); assert th4["famA"]["mode"] == "claude_direct" and "sensitive" in th4["famA"]["reasons"][0], th4["famA"]
# kill switch + report-only: without --write-state nothing is written
assert S.main(["throttle"]) == 0 and not S.STATE.exists()
os.environ["MINI_SCORECARD"] = "off"
assert S.main(["throttle"]) == 0 and S.main(["sample", "list"]) == 0 and not S.STATE.exists()
del os.environ["MINI_SCORECARD"]
assert S.main(["throttle", "--write-state"]) == 0 and S.STATE.exists()

# global review-all switch (2026-11-03: the first 53 sampled reviews rejected 30%) + review add refreshes the state
rv = lambda job, v, kind="fix": {"ts": S.iso(), "job": job, "verdict": v, "kind": kind, "family": "famZ"}
assert S.global_sample([rv(f"g{i}", "reject" if i < 5 else "ok") for i in range(25)])["sample_pct"] == 100      # 5/25 = 20% > 10%
assert S.global_sample([rv(f"g{i}", "reject" if i < 2 else "ok") for i in range(25)])["sample_pct"] is None     # 2/25 = 8%
assert S.global_sample([rv(f"g{i}", "reject") for i in range(19)])["sample_pct"] is None                        # too few to judge
old = [rv(f"g{i}", "reject") for i in range(40)] + [rv(f"h{i}", "ok") for i in range(30)]
assert S.global_sample(old)["sample_pct"] is None, "only the latest 30 count: old rejects age out"
assert S.global_sample([rv(f"g{i}", "reject" if i < 5 else "ok") for i in range(25)] + [rv(f"g{i}", "ok") for i in range(5)])["sample_pct"] == 100, "an ok re-review does not erase a reject"
assert S.global_sample([rv(f"g{i}", "reject" if i < 2 else "ok") for i in range(25)] + [rv(f"g{i}", "reject") for i in range(2)])["sample_pct"] is None, "a job rejected twice counts once (2/25)"
assert S.global_sample([rv(f"g{i}", "ok") for i in range(25)] + [rv(f"r{i}", "reject", "research") for i in range(10)])["sample_pct"] is None, "research reviews are not pass reviews"
assert S.global_sample([rv(f"g{i}", "ok") for i in range(25)] + [rv(f"d{i}", "reject", "test_draft") for i in range(10)])["sample_pct"] is None, "draft reviews are not pass reviews"
again = [rv(f"g{i}", "reject") for i in range(10)] + [rv(f"h{i}", "ok") for i in range(30)] + [rv(f"g{i}", "reject") for i in range(5)]
assert S.global_sample(again)["sample_pct"] == 100, "a job reviewed again is one of the latest reviews"
assert S.sample_pct("famQ", {}) == 20 and S.sample_pct("famQ", {"families": {"famQ": {"sample_pct": 100}}}) == 100
assert S.sample_pct("famQ", {"global": {"sample_pct": 100}}) == 100, "the global switch reviews every family"
on = S.global_sample([rv(f"g{i}", "reject" if i < 5 else "ok") for i in range(25)], {"sample_pct": 100, "since": "2026-11-01T00:00:00Z"})
assert on["since"] == "2026-11-01T00:00:00Z", "still on: it keeps the time it turned on"
assert S.global_sample([rv(f"g{i}", "reject" if i < 5 else "ok") for i in range(25)], {"sample_pct": None, "since": "2026-11-01T00:00:00Z"})["since"] != "2026-11-01T00:00:00Z"
gst = {"global": {"sample_pct": 100, "since": "2026-11-01T00:00:00Z"}}
assert S.sample_pct("famQ", gst, S.parse_ts("2026-10-31T00:00:00Z")) == 20, "a pass from before the switch is not back-listed"
assert S.sample_pct("famQ", gst, S.parse_ts("2026-11-02T00:00:00Z")) == 100 and S.sample_pct("famQ", gst) == 100
# 2026-11-03: a batch of review adds dipped the switch to exactly 10% for a second, and the new "since" dropped every
# unreviewed pass of the 7 h on-period from `sample list`. A closed on-period is kept as a window, so its passes stay picked.
clean = [rv(f"c{i}", "ok") for i in range(30)]
off = S.global_sample(clean, {"sample_pct": 100, "since": "2026-11-01T00:00:00Z"})
assert off["sample_pct"] is None and [w[0] for w in off["windows"]] == ["2026-11-01T00:00:00Z"], off
assert S.parse_ts(off["windows"][0][1]) >= S.parse_ts("2026-11-01T00:00:00Z"), off
assert S.global_sample(clean, off)["windows"] == off["windows"], "staying off adds no window"
assert S.global_sample(clean)["windows"] == [] and S.global_sample(clean, {"sample_pct": None})["windows"] == []
back = S.global_sample([rv(f"g{i}", "reject" if i < 5 else "ok") for i in range(25)], off)
assert back["sample_pct"] == 100 and back["since"] != "2026-11-01T00:00:00Z" and back["windows"] == off["windows"], back
wst = {"global": {"sample_pct": None, "windows": [["2026-11-01T00:00:00Z", "2026-11-02T00:00:00Z"]]}}
assert S.sample_pct("famQ", wst, S.parse_ts("2026-11-01T12:00:00Z")) == 100, "a pass from a closed on-period stays picked"
assert S.sample_pct("famQ", wst, S.parse_ts("2026-10-31T12:00:00Z")) == 20 and S.sample_pct("famQ", wst, S.parse_ts("2026-11-02T12:00:00Z")) == 20
assert S.sample_pct("famQ", wst) == 20, "a pass with no finish time is under the switch only while it is on"
dip = {"global": dict(back, since="2026-11-03T00:00:00Z", windows=[["2026-11-01T00:00:00Z", "2026-11-02T23:59:59Z"]])}
assert S.sample_pct("famQ", dip, S.parse_ts("2026-11-01T12:00:00Z")) == 100, "a dip and a new since keep the earlier passes"
assert S.sample_pct("famQ", {"global": {"windows": [["x", "y"], ["2026-11-02T00:00:00Z"], None]}}, S.parse_ts("2026-11-01T12:00:00Z")) == 20
stale = {"sample_pct": None, "windows": [["2020-01-01T00:00:00Z", "2020-01-02T00:00:00Z"], ["2026-11-01T00:00:00Z", S.iso()]]}
assert [w[0] for w in S.global_sample(clean, stale)["windows"]] == ["2026-11-01T00:00:00Z"], "windows older than 30 days age out"
os.environ["MINI_SAMPLE_PCT"] = "50"
assert S.sample_pct("famQ", {"families": {"famQ": {"sample_pct": 20}}}) == 50, "MINI_SAMPLE_PCT is a floor the state cannot lower"
del os.environ["MINI_SAMPLE_PCT"]
# re-promote streak: 10+ clean JOBS after the last rejected one; an ok re-review of a rejected job does not start a streak
srv = lambda job, v: json.dumps({"ts": S.iso(), "job": job, "verdict": v, "kind": "fix", "family": "famS"}) + "\n"
S.STATE.write_text(json.dumps({"families": {"famS": {"mode": "claude_direct"}}}))
S.REVIEWS.write_text(srv("s00", "reject") + "".join(srv(f"s{i:02d}", "ok") for i in range(1, 25)) + srv("s00", "ok"))
famS = S.compute(14)["families"]["famS"]
assert (famS["rev_n"], famS["rev_bad"]) == (25, 1), famS
assert S.throttle(S.compute(14))["famS"]["mode"] == "claude_direct", "the rejected job's re-review is the latest: no clean streak"
S.REVIEWS.write_text(srv("s00", "reject") + srv("s00", "ok") + "".join(srv(f"s{i:02d}", "ok") for i in range(1, 25)))
assert S.throttle(S.compute(14))["famS"]["mode"] == "v2", "24 clean jobs after the rejected one re-promote"
S.STATE.unlink(); S.REVIEWS.write_text("")
for i in range(20):
    assert S.main(["review", "add", "--job", f"z{i:02d}", "--verdict", "reject" if i % 4 == 0 else "ok", "--family", "famZ"]) == 0
st = json.loads(S.STATE.read_text())
assert st["global"]["sample_pct"] == 100 and st["families"]["famZ"]["sample_pct"] == 100, st   # 5/20 rejects
S.STATE.write_text(json.dumps(dict(st, **{"global": dict(st["global"], since="2026-11-01T00:00:00Z")})))
assert S.write_state()["global"]["since"] == "2026-11-01T00:00:00Z", "a state refresh keeps the time the switch turned on"
assert S.review_pick({"id": "x1", "runner": "/x/mini-swe-runner.py", "status": "done", "runner_verdict": "PASS", "bundle": "famNew"})[:3] == (True, "famNew", 100)
os.environ["MINI_SCORECARD"] = "off"
S.STATE.unlink()
assert S.main(["review", "add", "--job", "z99", "--verdict", "reject", "--family", "famZ"]) == 0 and not S.STATE.exists(), "kill switch: no state write"
del os.environ["MINI_SCORECARD"]
assert S.main(["caps"]) == 0  # no task files in the fake queue: everything lands in bucket 0, nothing crashes

# exclusions (2026-11-03): the stress run's toy jobs test the queue, not the model; they are kept out of every count, rate and sample
import contextlib, io


def run(*argv):
    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        rc = S.main(list(argv))
    return rc, buf.getvalue()


ids = lambda: {j["id"] for j in S.load_jobs(14)}
job("r00", "famR", True); job("r01", "famR", True)
os.environ["MINI_SAMPLE_PCT"] = "100"
assert {"r00", "r01"} <= ids() and "r00" in run("sample", "list")[1]
try:
    S.main(["exclude", "add", "--job", "r00"])
    raise AssertionError("exclude add without --reason must be refused")
except SystemExit:
    pass
assert run("exclude", "add", "--job", "r00", "--reason", "  ")[0] == 2 and not S.EXCLUDED.exists(), "a blank reason is refused"
rc, txt = run("exclude", "add", "--job", "r00", "r01", "--reason", "synthetic stress-run job")
assert rc == 0 and len(S.read_jsonl(S.EXCLUDED)) == 2, txt
assert run("exclude", "add", "--job", "r00", "--reason", "again")[0] == 0 and len(S.read_jsonl(S.EXCLUDED)) == 2, "adding twice writes nothing"
assert run("exclude", "add", "--job", "r03", "r03", "--reason", "twice")[0] == 0 and len(S.read_jsonl(S.EXCLUDED)) == 3, "named twice, written once"
assert "counts only after" in run("review", "add", "--job", "r03", "--verdict", "ok", "--family", "famT")[1]
assert run("exclude", "remove", "--job", "r03")[0] == 0 and "r03" not in S.exclusions()
assert not {"r00", "r01"} & ids() and "famR" not in S.compute(14)["families"]
assert "r00" not in run("sample", "list")[1] and "r00" not in run("report")[1]
pr = {"id": "r00", "runner": "/x/mini-swe-runner.py", "status": "done", "runner_verdict": "PASS", "bundle": "famR"}
assert S.review_pick(pr)[0] is False and S.review_pick(dict(pr, id="r02"))[0] is True, "the deliverer never asks to review an excluded job"
assert run("exclude", "remove", "--job", "r01")[0] == 0 and "r01" in ids() and "r00" not in ids(), "remove counts the job again"
rc, txt = run("exclude", "list")
assert rc == 0 and "r00" in txt and "r01" not in txt and "synthetic stress-run job" in txt and "# 1 job(s)" in txt, txt
# a review or ledger row of an excluded job does not count either, and counts again once the exclusion is removed
S.append(S.REVIEWS, {"ts": S.iso(), "job": "r00", "verdict": "reject", "kind": "fix", "family": "famR"})
S.append(S.LEDGER, {"ts": S.iso(), "job": "r00", "commit": sha, "repo": str(repo), "family": "famR", "note": ""})
famR = S.compute(14)["families"]["famR"]
assert (famR["rev_bad"], famR["landed"]) == (0, 0), famR
assert run("exclude", "remove", "--job", "r00")[0] == 0
famR = S.compute(14)["families"]["famR"]
assert (famR["rev_bad"], famR["landed"]) == (1, 1), famR
del os.environ["MINI_SAMPLE_PCT"]
# caps: an unreadable record is skipped (it used to raise when it came first, or reuse the previous record's values)
(tmp / "q" / "0bad.done.json").write_text("{not json")
assert S.main(["caps"]) == 0 and S.load_jobs(14)
# auto jobs (2026-11-03): every pass is picked even at a 0% floor, the brief prints REVIEW-AUTO.md inline with the auto checklist,
# `review add` takes kind auto from the spec, and auto verdicts have their own rate (not the sampled-fix rate, not the global switch)
os.environ["MINI_SAMPLE_PCT"] = "0"
ad = tmp / "autorun"; (ad / "work").mkdir(parents=True)
(ad / "spec.json").write_text(json.dumps({"mode": "auto", "allowed": [], "baseline_from": "/x", "task": "/x/TASK.md"}))
(ad / "REVIEW-AUTO.md").write_text("# REVIEW (auto) shop\nRESULT PASS mode=auto target=lib/price.js:bulkTotal\n## Fix diff\n+  if (qty >= 10) {\n")
arec = {"id": "au1", "label": "shop-bulk-discount", "bundle": "famAuto", "runner": "/x/mini-swe-runner.py", "status": "done", "runner_verdict": "PASS",
        "active_s": 2000.0, "persisted_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "task_file": str(ad / "spec.json"), "cwd": str(ad / "work")}
(tmp / "q" / "au1.done.json").write_text(json.dumps(arec))
assert S.job_kind(arec) == "auto" and S.job_kind(dict(arec, task_file="/nonexistent")) == "fix"
assert S.review_pick(arec)[:3] == (True, "famAuto", 100), S.review_pick(arec)
assert S.review_pick(dict(arec, runner_verdict="FAIL", status="failed"))[0] is False, "a failed auto job is not a review"
rc, txt = run("review", "brief", "au1")
assert rc == 0 and "kind auto" in txt and "+  if (qty >= 10) {" in txt and "AUTO job" in txt and "mini-auto review au1 ok" in txt and "mini-auto review au1 reject" in txt, txt
assert "REVIEW-AUTO.md" in txt and "end of packet" in txt, txt
rc, txt = run("sample", "list")
assert rc == 0 and "au1" in txt and "[auto]" in txt, txt
fix_before = S.compute(14)["totals"]["rev_n"]
assert S.main(["review", "add", "--job", "au1", "--verdict", "reject", "--note", "test asserts the wrong total"]) == 0
row = S.read_jsonl(S.REVIEWS)[-1]
assert row["kind"] == "auto" and row["family"] == "famAuto" and "nr" not in row, row
sc = S.compute(14)
m = {x["metric"]: x for x in sc["metrics"]}
am = m["auto-lane review-reject rate (every pass reviewed)"]
assert am["n"] == 1 and am["value"] == 1.0 and am["status"] == "BREACH", am
assert sc["totals"]["rev_n"] == fix_before, "an auto verdict is not a sampled-fix verdict"
assert S.main(["review", "add", "--job", "au1", "--verdict", "ok", "--kind", "fix"]) == 0 and S.read_jsonl(S.REVIEWS)[-1]["kind"] == "fix"
assert "au1" not in run("sample", "list")[1], "a reviewed auto pass leaves the to-review list"
del os.environ["MINI_SAMPLE_PCT"]
print("test-mini-scorecard ok")
