#!/usr/bin/env python3
"""test-esc-review-bundle.py -- an escalation review joins the REAL bundle of the row it reviews
(2026-10-09, esc-review-...-job-fb82a438d263 minted bundle `job-fb82a438d263` and, as the 'running'
bundle, held the lanes of rt-walmart-cancel).

  1. a needs-opus-auto-X placeholder is born WITH its bundle (explicit, else the chain record, else the
     label's default) -- never bundle-less;
  2. default_bundle_from_label / chain_bundle_for_label resolve needs-opus-auto-X / auto-author-X-sN-rM;
  3. enqueue_bundle_key(esc-review-<ts>-job-<id>) joins that row's real bundle (live row, pruned sidecar);
  4. the janitor (retag_unbundled_rows) re-tags unbundled / minted-slug rows and NEVER a running one;
  5. an esc-review row never makes its bundle the running/committed bundle or keeps it 'working';
  6. the watcher's review_bundle() resolves a bundle-less row the same way.
Usage: test-esc-review-bundle.py [--bin DIR]. Prints ESC_REVIEW_BUNDLE_TEST_OK.
"""
import argparse, importlib.machinery, importlib.util, json, os, sys, tempfile
from pathlib import Path

os.environ["DISPATCH_VERIFY_SANDBOX"] = "1"
os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=str(Path.home() / "bin"))
a = ap.parse_args()
BIN = Path(a.bin)
R = []


def check(name, got, want):
    ok = got == want
    R.append(ok)
    print(("PASS " if ok else "FAIL ") + name + ("" if ok else f"\n     got={got!r}\n    want={want!r}"))


def load(path, name):
    ld = importlib.machinery.SourceFileLoader(name, str(path))
    sp = importlib.util.spec_from_loader(name, ld)
    m = importlib.util.module_from_spec(sp)
    sys.modules[name] = m
    ld.exec_module(m)
    return m


tmp = Path(tempfile.mkdtemp(prefix="esc-bundle-"))
q = load(BIN / "ollama-queue.py", "oq_escbundle")
q.STATE_PATH, q.LOCK_PATH, q.LOG_DIR = tmp / "state.json", tmp / "state.lock", tmp / "logs"
q.LOG_DIR.mkdir()
runs = tmp / "auto-runs"
runs.mkdir()
q.CHAIN_RUNS_DIR = runs
(runs / "rt-card-bonus.json").write_text(json.dumps({"runs": {"rt-card-bonus-lib": {
    "key": "rt-card-bonus", "label": "rt-card-bonus-lib", "bundle": "rt-card-bonus"}}}))

check("2 default_bundle_from_label strips needs-opus-auto-",
      q.default_bundle_from_label("needs-opus-auto-rt-card-bonus-lib"), "rt-card-bonus-lib")
check("2 chain_bundle_for_label: needs-opus-auto-X -> the chain record's bundle",
      q.chain_bundle_for_label("needs-opus-auto-rt-card-bonus-lib", runs), "rt-card-bonus")
check("2 chain_bundle_for_label: auto-author-X-s3-r1 -> same bundle",
      q.chain_bundle_for_label("auto-author-rt-card-bonus-lib-s3-r1", runs), "rt-card-bonus")
check("2 chain_bundle_for_label: unknown label -> None", q.chain_bundle_for_label("zzz-unknown", runs), None)

# 1. born with a bundle
pj = q._build_parked_job("r", "persistent-nogo", next_command="x", label="needs-opus-auto-foo", bundle="bFoo")
check("1 _build_parked_job carries the bundle", pj.get("bundle"), "bFoo")
q.create_parked_job("r", category="persistent-nogo", next_command="x", label="needs-opus-auto-rt-card-bonus-lib")
st = json.loads(q.STATE_PATH.read_text())
check("1 create_parked_job with no --bundle resolves it from the chain record",
      [j.get("bundle") for j in st["jobs"]], ["rt-card-bonus"])

# 3. esc-review joins the reviewed row's real bundle
parked = {"id": "fb82a438d263", "label": "needs-opus-auto-rt-card-bonus-lib", "status": "needs_opus", "bundle": None}
rev = {"id": "e92bf8379d85", "label": "esc-review-20261009T160326Z-job-fb82a438d263", "status": "pending"}
check("3 esc-review of a bundle-less needs_opus row joins the real bundle (not job-<id>)",
      q.enqueue_bundle_key(rev, [parked, rev], reverse={}, log_dir=q.LOG_DIR), "rt-card-bonus")
(q.LOG_DIR / "aaaaaaaaaaaa.done.json").write_text(json.dumps({"label": "auto-author-zed-s1", "bundle": "zed"}))
rev2 = {"id": "bbbbbbbbbbbb", "label": "esc-review-20261009T160326Z-job-aaaaaaaaaaaa", "status": "pending"}
check("3 ...a PRUNED reviewed row: the sidecar's bundle", q.enqueue_bundle_key(rev2, [rev2], reverse={}, log_dir=q.LOG_DIR), "zed")

# 4. janitor
jobs = [dict(parked), dict(rev, status="running", bundle="job-fb82a438d263"),
        {"id": "cccccccccccc", "label": "esc-review-20261009T000000Z-job-fb82a438d263", "status": "pending",
         "bundle": "job-fb82a438d263"},
        {"id": "dddddddddddd", "label": "x-s1", "status": "pending", "bundle": None}]
todo, running = q.retag_unbundled_rows(jobs, q.LOG_DIR, runs)
check("4 janitor: re-tags the parked row and the pending review; skips + reports the RUNNING one; ignores plain rows",
      (sorted((i, n) for i, o, n in todo), running),
      ([("cccccccccccc", "rt-card-bonus"), ("fb82a438d263", "rt-card-bonus")], ["e92bf8379d85"]))

# 5. an esc-review never owns the lanes
pk = lambda j: j.get("bundle")
only_rev = [{"id": "r1", "label": "esc-review-20261009T160326Z-job-x", "status": "running", "bundle": "bX"}]
check("5 a bundle whose ONLY row is a running esc-review is not 'working' (no commitment)",
      q.bundle_commit_status("bX", only_rev, pk, {}, {})[0] != "working", True)
check("5 control: a running coding row still makes it working",
      q.bundle_commit_status("bX", [{"id": "c", "label": "job", "status": "running", "bundle": "bX"}], pk, {}, {})[0],
      "working")
check("5 esc-review rows are recognised", (q._is_esc_review_job(rev), q._is_esc_review_job({"label": "gate-x"})), (True, False))

# 6. watcher
w = load(BIN / "dispatch-escalation-watcher.py", "w_escbundle")
_home = tmp / "home"
(_home / "bin").mkdir(parents=True)
(_home / ".ollama-dispatch" / "auto-runs").mkdir(parents=True)
(_home / ".ollama-dispatch" / "auto-runs" / "rt-card-bonus.json").write_text((runs / "rt-card-bonus.json").read_text())
(_home / "bin" / "ollama-queue-state.json").write_text(json.dumps({"jobs": [parked]}))
_old_home = os.environ.get("HOME")
os.environ["HOME"] = str(_home)
try:
    check("6 watcher review_bundle: explicit bundle wins", w.review_bundle({"bundle": "bQ", "job_id": "zz"}), "bQ")
    check("6 watcher review_bundle: a bundle-less reviewed row resolves to the REAL bundle (not None -> job-<id> slug)",
          w.review_bundle({"job_id": "fb82a438d263"}), "rt-card-bonus")
finally:
    os.environ["HOME"] = _old_home
print("ESC_REVIEW_BUNDLE_TEST_OK" if all(R) else "ESC_REVIEW_BUNDLE_TEST_FAILED")
sys.exit(0 if all(R) else 1)
