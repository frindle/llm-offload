#!/usr/bin/env python3
"""Regression (the owner 2026-10-09 dashboard screenshot): card 'Job - needs-opus-auto-rt-bg-
getcommitments-status' read red `failed` while its own `escalation / heal` row was still
`pending` ("held on running job 48c096d8d690 (rt-bg-sync-zero-guard)"). Two defects:

  1. TERMINAL: a job whose heal/escalation ladder still has an outstanding (pending/held/
     running/paused/queued) esc-review row can still advance, so it is NOT failed -- it is
     `escalation-review` (healing, non-attention). `failed` (red, attention) only when no
     heal row is outstanding (ladder exhausted: reviews finished / parked final rung).
     The bundle headline (bundle_outcome failed_slices) must agree with the card.
  2. BLOCKER TEXT: a waiting row's reason names the job it waits on in plain words and WHOSE
     bundle it is: 'waiting for job <id> (<label>; another bundle: <key>) to finish'.

Pure bundle_view.build_job_view / outstanding_heal / bundle_outcome + api._wait_reason_for.
Run: python3 test-heal-nonterminal.py [--api PATH] [--revert-check]
(--revert-check runs mutants of the sources; each must FAIL). Prints ALL PASSED."""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _api_path():
    if "--api" in sys.argv:
        return Path(sys.argv[sys.argv.index("--api") + 1])
    if os.environ.get("API_SRC"):
        return Path(os.environ["API_SRC"])
    repo = HERE.parent / "src" / "ollama-queue-api.py"
    return repo if repo.exists() else HERE / "ollama-queue-api.py"


API = _api_path()
def _bv_path():
    if os.environ.get("BV_SRC"):
        return Path(os.environ["BV_SRC"])
    for d in (API.parent, HERE.parent / "src", Path.home() / "bin"):
        if (d / "bundle_view.py").exists():
            return d / "bundle_view.py"
    return API.parent / "bundle_view.py"


BV = _bv_path()
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def run():
    spec = importlib.util.spec_from_file_location("bv_under_test", str(BV))
    bv = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bv)
    NOW = 1_791_577_700.0
    nj = {"id": "444c051f2538", "label": "needs-opus-auto-rt-bg-getcommitments-status",
          "status": "needs_opus", "bundle": "rt-bg-commitments-guard"}

    def esc(status, jid="2ac42be77bcc"):
        return {"id": jid, "label": "esc-review-20261009T202155Z-job-444c051f2538",
                "status": status, "bundle": "rt-bg-commitments-guard"}

    def slice_of(jobs):
        v = bv.build_job_view("rt-bg-commitments-guard", jobs, now=NOW)
        return v, v["slices"][0]

    for st in ("pending", "held", "running", "paused", "queued"):
        v, s = slice_of([nj, esc(st)])
        check(f"needs_opus + {st} esc-review is NON-terminal (not failed, not attention)",
              (s["phase"], s["attention"], s["active"]), ("escalation-review", False, True))
    v, s = slice_of([nj, esc("pending")])
    check("the card says it is healing (detail names the status), still carries status needs_opus",
          ("healing" in s["detail"], "pending" in s["detail"], s["status"]), (True, True, "needs_opus"))
    check("bundle headline agrees: no failed slice while the heal row is pending",
          bv.bundle_outcome(v)[1], 0)
    # exhausted / never started: failed is right
    v2, s2 = slice_of([nj, esc("done")])
    check("needs_opus whose esc-review FINISHED (ladder exhausted) is terminal failed",
          (s2["phase"], s2["attention"]), ("failed", True))
    v3, s3 = slice_of([nj])
    check("needs_opus with no heal row at all is failed",
          (s3["phase"], s3["attention"]), ("failed", True))
    check("...and the headline counts it (same definition as the card)",
          (bv.bundle_outcome(v3), bv.bundle_outcome(v2)), (("failed", 1), ("failed", 1)))
    failed_job = dict(nj, status="failed", label="auto-author-x-s1")
    v4, s4 = slice_of([failed_job, dict(esc("pending"),
                                        label="esc-review-20261009T202155Z-job-444c051f2538")])
    check("a plain failed job with a pending heal row is non-terminal too",
          (s4["phase"], s4["attention"]), ("escalation-review", False))
    check("a gate/other waiting child is NOT a heal row",
          bv.outstanding_heal([{"label": "gate-444c051f2538", "status": "pending"},
                               {"label": "secondop-444c051f2538", "status": "pending"}]), [])

    # --- blocker text -----------------------------------------------------------
    os.environ.setdefault("OLLAMA_QUEUE_NO_NOTIFY", "1")
    sys.argv = [str(API)]
    sys.path.insert(0, str(API.parent)); sys.path.insert(0, str(Path.home() / "bin"))
    aspec = importlib.util.spec_from_file_location("api_under_test", str(API))
    api = importlib.util.module_from_spec(aspec)
    aspec.loader.exec_module(api)
    heal = {"id": "2ac42be77bcc", "label": "esc-review-20261009T202155Z-job-444c051f2538",
            "status": "pending", "group_key": "rt-bg-commitments-guard", "lane": "studio-db"}
    other = {"id": "48c096d8d690", "label": "rt-bg-sync-zero-guard", "status": "running",
             "group_key": "rt-bg-sync-zero-guard", "lane": "studio-db"}
    w = api._wait_reason_for(heal, None, {}, [other], {})
    check("blocker names the job in plain words and that it is another bundle",
          w, "waiting for job 48c096d8d690 (rt-bg-sync-zero-guard; another bundle: "
             "rt-bg-sync-zero-guard) to finish")
    same = dict(other, group_key="rt-bg-commitments-guard")
    check("a blocker in the SAME bundle says so",
          api._wait_reason_for(heal, None, {}, [same], {}),
          "waiting for job 48c096d8d690 (rt-bg-sync-zero-guard; in this bundle) to finish")
    solo = dict(other, group_key="48c096d8d690")
    check("a standalone blocker says it is not in a bundle",
          api._wait_reason_for(heal, None, {}, [solo], {}).endswith("not in any bundle) to finish"), True)
    check("the old ambiguous 'held on running job' phrasing is gone",
          "held on running job" in api._wait_reason_for(heal, None, {}, [other], {}), False)


MUT = [
    ("BV_SRC", BV, "        elif heal_wait:\n", "        elif False:\n"),
    ("BV_SRC", BV, "            if esc_review_ref(j.get(\"label\")) and j.get(\"status\") in _LIVE]",
     "            if esc_review_ref(j.get(\"label\")) and j.get(\"status\") == \"running\"]"),
    ("BV_SRC", BV, "            detail = f\"{st}: healing", "            detail = f\"{st}: stuck"),
    ("API_SRC", API, "        whose = \"in this bundle\"", "        whose = \"another bundle: \" + str(bk)"),
    ("API_SRC", API, "        whose = f\"another bundle: {bk}\"", "        whose = \"\""),
    ("API_SRC", API, "        return (_busy_job_phrase(r, busy[0])",
     "        return (f\"held on running job {busy[0].get('id')} ({busy[0].get('label')})\""),
]

if __name__ == "__main__":
    if "--revert-check" in sys.argv:
        bad = 0
        for i, (env, path, o, nw) in enumerate(MUT):
            src = path.read_text()
            if src.count(o) != 1:
                print(f"ANCHOR MISSING: mutant {i}"); bad += 1; continue
            td = Path(tempfile.mkdtemp())
            for sib in API.parent.glob("*.py"):      # api imports its siblings by path
                (td / sib.name).write_text(sib.read_text())
            t = td / path.name
            t.write_text(src.replace(o, nw))
            args = [sys.executable, __file__, "--api", str(t if env == "API_SRC" else API)]
            p = subprocess.run(args, env=dict(os.environ, **({env: str(t)} if env == "BV_SRC" else {})),
                               capture_output=True, text=True)
            crashed = "Traceback" in p.stderr or "crashed" in p.stdout
            print("revert %-3d %s" % (i, "INERT" if not p.returncode else ("CRASH(not an assertion)" if crashed else "bites")))
            if p.returncode and crashed:
                bad += 1
            bad += 0 if p.returncode else 1
        print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad})")
        sys.exit(1 if bad else 0)
    try:
        run()
    except Exception:
        import traceback; traceback.print_exc(); FAILS.append("crashed")
    print(f"\n{len(FAILS)} failure(s)" if FAILS else "\nALL PASSED")
    sys.exit(1 if FAILS else 0)
