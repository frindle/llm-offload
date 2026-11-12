#!/usr/bin/env python3
"""The owner 2026-10-09 (rt-bg-commitments-guard): superseded failed retry attempts sat as `failed`
rows after their chain exited 0. hygiene_sweep -> passed_lineage_actions must resolve rows whose
lineage PROVABLY passed later (auto-run exit 0 for L / L-sN, or superseded_by chain reaching a
done exit-0 job) plus the job-form esc-review rows about them -- and nothing else.
Hermetic (pure function + injected act). --revert-check mutates SELF_HEAL_SRC; each must go RED."""
import importlib.util, os, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SH = Path(os.environ.get("SELF_HEAL_SRC") or HERE / "dispatch-self-heal.py")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else f" -- got {got!r}, want {want!r}"))
    if not ok:
        FAILS.append(name)


def run():
    s = importlib.util.spec_from_file_location("sh_t", str(SH))
    m = importlib.util.module_from_spec(s)
    s.loader.exec_module(m)
    B = "rt-bg-commitments-guard"
    ts = lambda h: "2026-10-09T%02d:00:00+00:00" % h
    J = lambda i, l, st, h, **kw: dict(id=i, label=l, status=st, bundle=B, enqueued_at=ts(h), **kw)
    runs = [("rt-bg-sync-zero-guard", B, m._parse_ts(ts(20)))]
    ids = lambda acts: sorted(i for _v, i, _w in acts)

    rows = [J("f1", "auto-author-rt-bg-sync-zero-guard-s3-r1", "failed", 15),
            J("f2f2f2f2f2f2", "auto-author-rt-bg-sync-zero-guard", "failed", 16),
            J("e1", "esc-review-20261009T160000Z-job-f2f2f2f2f2f2", "failed", 17),
            J("l1", "auto-author-rt-bg-other", "failed", 15)]
    check("rounds + esc-review of a chain that exited 0 are resolved; unrelated chain untouched",
          ids(m.passed_lineage_actions(rows, runs)), ["e1", "f1", "f2f2f2f2f2f2"])
    check("no passed run -> nothing",
          ids(m.passed_lineage_actions(rows, [])), [])
    check("run of another bundle proves nothing",
          ids(m.passed_lineage_actions(rows, [("rt-bg-sync-zero-guard", "zzz", m._parse_ts(ts(20)))])), [])
    check("failure NEWER than the pass stays",
          ids(m.passed_lineage_actions([J("n1", "auto-author-rt-bg-sync-zero-guard-r2", "failed", 22)], runs)), [])
    check("a live row in the lineage blocks the whole lineage",
          ids(m.passed_lineage_actions(rows + [J("p1", "rt-bg-sync-zero-guard", "pending", 18)], runs)), [])
    check("prefix-only label does not match",
          ids(m.passed_lineage_actions(rows, [("rt-bg-sync", B, m._parse_ts(ts(20)))])), [])
    check("awaiting_signoff never resolved",
          ids(m.passed_lineage_actions([J("s1", "rt-bg-sync-zero-guard", "failed", 15, awaiting_signoff=True)], runs)), [])
    # proof 2: superseded_by chain reaches a passed job
    ch = [J("c1", "needs-opus-auto-lib", "needs_opus", 1, superseded_by="c2"),
          J("c2", "auto-author-lib-r1", "failed", 2, superseded_by="c3"),
          dict(id="c3", label="lib", status="done", exit_code=0, bundle=B, enqueued_at=ts(3))]
    check("superseded_by chain reaching a done exit-0 job resolves the chain",
          ids(m.passed_lineage_actions(ch, [])), ["c1", "c2"])
    ch[2]["status"] = "running"
    check("...but not while the successor is still running",
          ids(m.passed_lineage_actions(ch, [])), [])
    ch[2].update(status="done", exit_code=1)
    check("...nor when the successor finished non-zero",
          ids(m.passed_lineage_actions(ch, [])), [])
    # through the sweep, with injected act: logged + applied
    seen, dec = [], tempfile.mktemp()
    with tempfile.TemporaryDirectory() as d:
        out = m.hygiene_sweep(jobs=rows, ledger_path=Path(d) / "led.json", sidecars=[],
                              act=lambda v, j: seen.append((v, j)) or True,
                              notifier=lambda *a, **k: None, decisions=dec,
                              refresh=lambda: {j["id"]: j for j in rows})
    # auto_run_passes reads the real dir in hygiene_sweep; assert wiring via source instead
    check("hygiene_sweep feeds auto-run passes to superseded_review_actions",
          "runs=auto_run_passes()" in SH.read_text(), True)


if "--revert-check" in sys.argv:
    src = SH.read_text()
    muts = {"auto-run proof dropped": ('why = "chain %s ended exit 0 after this attempt" % L', 'pass'),
            "ordering ignored": ('if not _lineage_matches(lab, L) or t >= end:', 'if not _lineage_matches(lab, L):'),
            "live-guard dropped": ('            if any(x.get("status") in _LIVE for x in mates):\n                continue\n', ''),
            "bundle check dropped": ('if j.get("bundle") and bundle and j.get("bundle") != bundle:', 'if False:'),
            "chain proof dropped": ('if _passed(nr):\n                    return n', 'if False:\n                    return n'),
            "sweep unwired": ('runs=auto_run_passes())', 'runs=None)'),
            "sN match dropped": ('+ r"-s\\d+", stem)', '+ r"-ZZ", stem)')}
    bad = 0
    for name, (a, b) in muts.items():
        assert a in src, name
        with tempfile.TemporaryDirectory() as d:
            mp = Path(d) / "dispatch-self-heal.py"
            mp.write_text(src.replace(a, b, 1))
            r = subprocess.run([sys.executable, __file__], env={**os.environ, "SELF_HEAL_SRC": str(mp)},
                               capture_output=True, text=True)
        print(("mutant caught: " if r.returncode else "MUTANT SURVIVED: ") + name)
        bad += 0 if r.returncode else 1
    sys.exit(1 if bad else 0)
run()
print("ALL PASSED" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
