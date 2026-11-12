#!/usr/bin/env python3
"""SHARED HANDLED LEDGER (2026-10-14): run-status and the handoff panel share ONE
'handled' record (handoff's acted.json), so a job cleared in either is cleared in both.

  * handoff --acted mark  -> the run-status row folds to 'handled' with the acted reason
    (still listed, verdict untouched, sidecars NOT archived)
  * run-status clear      -> sidecars archived AND an acted mark written (first mark wins)
  * a required, unapproved sign-off is NEVER folded by a plain acted mark (only by one
    that recorded an explicit sign-off override)
  * a sign-off row past the newest-100 cap is still listed (the handoff panel shows it)
  * a fixture LOG_DIR that is not handoff's log dir never touches the ledger
  * RUNSTATUS_SHARED_LEDGER=off restores the old two-ledger behaviour exactly

--revert-check mutates each piece and requires the suite to go RED.
API_SRC env overrides the file under test."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

SRC = Path(os.environ.get("API_SRC") or Path(os.environ.get("DASHBOARD_SRC") or Path(__file__).resolve().parent.parent / "src") / "ollama-queue-api.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load():
    loader = SourceFileLoader("api_sl", str(SRC))
    spec = importlib.util.spec_from_loader("api_sl", loader)
    m = importlib.util.module_from_spec(spec)
    sys.argv = [str(SRC)]
    loader.exec_module(m)
    return m


def mk(ld, jid, label, verdict, ts, status="done"):
    (ld / f"{jid}.done.json").write_text(json.dumps({
        "id": jid, "label": label, "model": "qwen3.8:27b-q4_K_M", "host_pref": "studio",
        "status": status, "exit_code": 0, "persisted_at": ts}))
    (ld / f"{jid}.gate.json").write_text(json.dumps({"verdict": verdict, "regate": "done"}))


def setup(m, live_jobs=()):
    root = Path(tempfile.mkdtemp(prefix="sharedledger-"))
    ld, hd = root / "logs", root / "handoff"
    ld.mkdir(); hd.mkdir()
    m.q.LOG_DIR = ld
    m.ho.LOGS = ld
    m.ho.ACTED = hd / "acted.json"
    m.ho.ACTED_LASTGOOD = hd / "acted.json.lastgood"
    m.ho.ACTED_RESTORE_LOG = hd / "acted.restore.log"
    m.ho.STATE = root / "state.json"
    m.ho.STATE.write_text(json.dumps({"jobs": list(live_jobs)}))
    so_path = root / "signoff.json"
    so_path.write_text("{}")
    m._signoff_path = lambda: so_path

    def blocks(jid, label):
        rec = json.loads(so_path.read_text()).get(jid) or {}
        return "REQUIRES sign-off" if rec.get("required") and rec.get("verdict") != "approve" else None
    m.ho.signoff_blocks_acting = blocks

    class FakeLock:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def load(self):
            return {"jobs": list(live_jobs)}
    m.q._Locked = lambda *a, **k: FakeLock()
    m._auto_run_states = lambda: {}
    m._live_auto_runs = lambda: []
    return root, ld, hd, so_path


def rows_by_id(m, limit=100):
    return {r["id"]: r for r in m._run_status_jobs(limit=limit)}


def main():
    m = load()
    m.SHARED_LEDGER_MODE = "on"
    root, ld, hd, so_path = setup(m)
    mk(ld, "aaaafail0001", "proj-fix-a", "fail", "2026-10-01T01:00:00Z")
    mk(ld, "bbbbpass0002", "proj-fix-b", "pass", "2026-10-01T02:00:00Z")
    mk(ld, "ccccfail0003", "proj-fix-c", "fail", "2026-10-01T03:00:00Z")
    mk(ld, "ddddsign0004", "proj-fix-d", "fail", "2026-10-01T04:00:00Z")
    mk(ld, "eeeeold00005", "proj-old-sign", "concerns", "2026-09-01T00:00:00Z")
    so_path.write_text(json.dumps({
        "ddddsign0004": {"required": True, "verdict": None},
        "eeeeold00005": {"required": True, "verdict": None}}))

    base = rows_by_id(m)
    check("baseline: an unacted FAIL is needs_eyes", base["aaaafail0001"]["group"], "needs_eyes")
    check("baseline: an unacted PASS is good_to_go", base["bbbbpass0002"]["group"], "good_to_go")

    # --- direction 1: handoff --acted -> run-status folds it ---
    m.ho._save_acted({
        "aaaafail0001": {"ts": "2026-10-02T00:00:00Z", "how": "marked with --acted (reason: x)",
                         "reason": "reviewed: stale harness fail"},
        "bbbbpass0002": {"ts": "2026-10-02T00:00:00Z", "how": "marked with --acted"},
        "ddddsign0004": {"ts": "2026-10-02T00:00:00Z", "how": "marked with --acted"}})
    r = rows_by_id(m)
    check("acted FAIL folds to handled", r["aaaafail0001"]["group"], "handled")
    check("acted fold carries the acted reason",
          r["aaaafail0001"]["handled_reason"],
          "acted on in handoff 2026-10-02T00:00:00Z: reviewed: stale harness fail")
    check("acted fold keeps the verdict untouched", r["aaaafail0001"]["raw_verdict"],
          base["aaaafail0001"]["raw_verdict"])
    check("acted fold is flagged handoff_acted", r["aaaafail0001"].get("handoff_acted"), True)
    check("acted fold archives nothing", (ld / "aaaafail0001.gate.json").exists(), True)
    check("acted PASS folds to handled too", r["bbbbpass0002"]["group"], "handled")
    check("an unacted FAIL stays needs_eyes", r["ccccfail0003"]["group"], "needs_eyes")
    check("a plain acted mark NEVER folds an owed sign-off",
          (r["ddddsign0004"]["group"], r["ddddsign0004"]["awaiting_signoff"]), ("needs_eyes", True))
    m.ho._save_acted(dict(json.loads(m.ho.ACTED.read_text()), ddddsign0004={
        "ts": "2026-10-03T00:00:00Z", "how": "marked with --acted (SIGN-OFF OVERRIDDEN)",
        "signoff_override_reason": "operator ship"}))
    check("an acted mark WITH a recorded sign-off override folds it",
          rows_by_id(m)["ddddsign0004"]["group"], "handled")

    # --- beyond the cap: a sign-off row the newest-N cap dropped is still listed ---
    capped = rows_by_id(m, limit=2)
    check("a sign-off row past the cap is still listed", "eeeeold00005" in capped, True)
    check("...flagged beyond_cap and needs_eyes",
          (capped.get("eeeeold00005") or {}).get("beyond_cap"), True)
    check("...as awaiting sign-off", (capped.get("eeeeold00005") or {}).get("group"), "needs_eyes")
    check("an ordinary row past the cap stays out (cap still applies)",
          "aaaafail0001" in capped, False)

    # --- direction 2: run-status clear -> acted mark written ---
    res = m._archive_run("ccccfail0003")
    check("run-status clear succeeds", res.get("ok"), True)
    check("run-status clear reports the ledger mark", res.get("handoff_acted"), "marked")
    acted = json.loads(m.ho.ACTED.read_text())
    check("run-status clear wrote an acted mark", "ccccfail0003" in acted, True)
    check("the mark says where it came from",
          acted.get("ccccfail0003", {}).get("how", "").startswith("cleared in run-status"), True)
    check("last-good copy updated too (survives a transient read failure)",
          "ccccfail0003" in json.loads(m.ho.ACTED_LASTGOOD.read_text()), True)
    before = dict(acted["aaaafail0001"])
    res2 = m._archive_run("aaaafail0001")
    check("clearing an already-acted row keeps the first mark",
          (res2.get("handoff_acted"), json.loads(m.ho.ACTED.read_text())["aaaafail0001"]),
          ("already", before))
    # a job still sitting in queue-state (slice member, done) -- the shape whose
    # run-status clear used to leave it on the handoff panel
    collect_ids = lambda: {x["id"] for x in m.ho.collect(json.loads(m.ho.ACTED.read_text()))[1]}
    m.ho.STATE.write_text(json.dumps({"jobs": [{"id": "ffffslice006", "label": "proj-s1-x",
                                                 "status": "done"}]}))
    mk(ld, "ffffslice006", "proj-s1-x", "fail", "2026-10-01T05:00:00Z")
    check("a done slice member in queue-state is on the handoff panel", "ffffslice006" in collect_ids(), True)
    m._archive_run("ffffslice006")
    check("...and a run-status clear now drops it from the handoff panel too",
          "ffffslice006" in collect_ids(), False)
    # sign-off override clear records the override on the shared mark
    m.ho._save_acted({k: v for k, v in json.loads(m.ho.ACTED.read_text()).items() if k != "ddddsign0004"})
    res3 = m._archive_run("ddddsign0004", override="ship it")
    sm = json.loads(m.ho.ACTED.read_text()).get("ddddsign0004", {})
    check("override clear records signoff_override_reason on the shared mark",
          (res3.get("ok"), sm.get("signoff_override_reason")), (True, "ship it"))
    check("a refused clear (sign-off owed, no override) writes NO mark",
          (m._archive_run("eeeeold00005").get("ok"),
           "eeeeold00005" in json.loads(m.ho.ACTED.read_text())), (False, False))

    # --- fixture isolation: a LOG_DIR that is not handoff's never touches the ledger ---
    m2 = load()
    _r, ld2, hd2, _s = setup(m2)
    m2.ho.LOGS = ld2.parent / "elsewhere"
    mk(ld2, "gggg00000007", "iso-job", "fail", "2026-10-01T06:00:00Z")
    out = m2._archive_run("gggg00000007")
    check("a foreign LOG_DIR clear writes no ledger mark",
          (out.get("handoff_acted"), m2.ho.ACTED.exists()), ("off", False))

    # --- kill switch ---
    m3 = load()
    _r, ld3, hd3, _s = setup(m3)
    m3.SHARED_LEDGER_MODE = "off"
    mk(ld3, "hhhh00000008", "ks-job", "fail", "2026-10-01T07:00:00Z")
    m3.ho._save_acted({"hhhh00000008": {"ts": "t", "how": "marked with --acted"}})
    check("kill switch: an acted row is NOT folded", rows_by_id(m3)["hhhh00000008"]["group"], "needs_eyes")
    mk(ld3, "iiii00000009", "ks-job2", "fail", "2026-10-01T08:00:00Z")
    check("kill switch: a clear writes no mark",
          (m3._archive_run("iiii00000009").get("handoff_acted"),
           "iiii00000009" in json.loads(m3.ho.ACTED.read_text())), ("off", False))

    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("acted fold removed", 'if _am is not None and (not r.get("awaiting_signoff")',
     'if False and (not r.get("awaiting_signoff")'),
    ("fold ignores sign-off", 'if _am is not None and (not r.get("awaiting_signoff")',
     'if _am is not None and (True'),
    ("clear writes no mark", 'ledger = _mark_shared_acted(job_id, label,', 'ledger = "off" or _mark_shared_acted(job_id, label,'),
    ("first mark overwritten", '            if job_id in acted:\n                return "already"\n',
     '            if False:\n                return "already"\n'),
    ("no beyond-cap rows", '    rows += _signoff_rows_beyond_cap(rows, acted)\n', '\n'),
    ("fixture dir guard dropped", 'return Path(q.LOG_DIR).resolve() == Path(ho.LOGS).resolve()', 'return True'),
    ("kill switch ignored", 'if SHARED_LEDGER_MODE in ("off", "0", "false", "no"):', 'if False:'),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing/ambiguous: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-api.py", delete=False,
                                         dir=str(SRC.parent)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "API_SRC": f.name},
                           capture_output=True, text=True, timeout=300)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name}")
        bad += 0 if red else 1
    print("REVERT-CHECK " + ("PASSED" if not bad else f"FAILED ({bad} inert)"))
    return 0 if not bad else 1


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
