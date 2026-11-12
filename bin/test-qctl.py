#!/usr/bin/env python3
"""Tests for ~/bin/qctl, focused on the CLEAR-SAFETY contract (revert-checked):

  * clear refuses unless the slice is done / skipped / superseded;
  * clear refuses while any of the slice's jobs is live, or the plan driver is live;
  * a row awaiting sign-off is KEPT (never handed to handoff --acted or run clear);
  * already-handled run rows are not re-cleared; checked ledger rows not re-ticked;
  * end to end on a fixture: ledger rows of THAT slice only are ticked, other
    slices' rows untouched, --dry-run changes nothing, refusals exit nonzero and
    are logged to decisions.jsonl;
  * esc-review-<ts>Z- labels map to their slice.
Run: python3 test-qctl.py [--revert-check]
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("QCTL_SRC") or HERE / "qctl")
FAILS = []


def check(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f": {name}" + ("" if ok else f"  (got {got!r}, want {want!r})"))
    if not ok:
        FAILS.append(name)


def load(env):
    os.environ.update(env)
    loader = SourceFileLoader("qctl_t", str(SRC))
    spec = importlib.util.spec_from_loader("qctl_t", loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def main():
    root = Path(tempfile.mkdtemp(prefix="qctl-")).resolve()
    disp = root / "disp"
    (disp / "slice-runs").mkdir(parents=True)
    (disp / "escalations").mkdir()
    plan = root / "plan.json"
    plan.write_text("{}")
    (disp / "slice-runs" / "p.json").write_text(json.dumps({
        "label": "p", "plan_path": str(plan), "order": ["s1", "s2"],
        "slices": {"s1": {"status": "done"}, "s2": {"status": "escalated"}}}))
    ledger = disp / "escalations" / "ESCALATIONS.md"
    ledger.write_text(
        "# ledger\n"
        "- [ ] `p` **s1** A - t - VERDICT: x - `f1`\n"
        "- [x] `p` **s1** A - t - old - `f0`\n"
        "- [ ] `p` **s2** A - t - VERDICT: y - `f2`\n"
        "- [ ] `other` **s1** A - t - z - `f3`\n")
    qstate = root / "q.json"
    jobs = [{"id": "a1", "label": "auto-author-p-s1", "status": "done"},
            {"id": "a2", "label": "auto-refine-p-s1-r1", "status": "done"},
            {"id": "a3", "label": "esc-review-52905Z-p-s1", "status": "done"},
            {"id": "b1", "label": "auto-author-p-s2", "status": "failed"}]
    qstate.write_text(json.dumps({"jobs": jobs}))
    q = load({"QCTL_BIN": str(HERE), "QCTL_DISPATCH": str(disp), "QCTL_QUEUE_STATE": str(qstate)})
    q.LEDGER, q.SLICE_RUNS, q.DECISIONS = ledger, disp / "slice-runs", disp / "decisions.jsonl"

    # --- pure clear_plan ---------------------------------------------------------
    sj = q.slice_jobs("p", "s1")
    check("esc-review-<ts>Z- labels map to their slice", sorted(j["id"] for j in sj), ["a1", "a2", "a3"])
    runs = {"a1": {"group": "needs_eyes"}, "a2": {"group": "handled"},
            "a3": {"group": "needs_eyes", "awaiting_signoff": True}}
    led = q.ledger_rows("p", "s1")
    why, plan_ = q.clear_plan("escalated", sj, runs, None, led)
    check("clear refuses a slice that is not done/skipped/superseded", (why is not None, plan_), (True, None))
    why, plan_ = q.clear_plan("done", sj + [{"id": "x", "status": "running"}], runs, None, led)
    check("clear refuses while a job of the slice is live", why is not None and "x (running)" in why, True)
    why, plan_ = q.clear_plan("done", sj, runs, 4242, led)
    check("clear refuses while the plan driver is live", why is not None and "4242" in why, True)
    why, plan_ = q.clear_plan("done", sj, runs, None, led)
    check("done slice -> allowed", why, None)
    check("awaiting-signoff row is KEPT", plan_["kept"], [("a3", "awaiting sign-off")])
    check("...and never handed to handoff --acted", "a3" in plan_["handoff"], False)
    check("...nor to run-status clear", "a3" in plan_["runs"], False)
    check("already-handled run rows are not re-cleared", plan_["runs"], ["a1"])
    check("only UNchecked ledger rows of the slice are ticked", plan_["ledger"], [1])


    # --- end to end clear with stubbed API + tools --------------------------------
    posted, ran = [], []
    q._api = lambda m, p, body=None, timeout=10: (
        (200, [{"id": k, **v} for k, v in runs.items()]) if m == "GET"
        else (posted.append(p) or (200, {"ok": True})))
    q._run = lambda cmd, dry, timeout=600: (ran.append((cmd, dry)) or (0, ""))
    q.driver_pid = lambda b: None
    before = ledger.read_text()
    rc = q.main(["clear", "p", "s1", "--dry-run"])
    check("--dry-run exits 0", rc, 0)
    check("--dry-run changes no ledger row", ledger.read_text(), before)
    check("--dry-run posts no run-status clear", posted, [])
    check("--dry-run only previews handoff", [d for _c, d in ran], [True])
    ran.clear()
    rc = q.main(["clear", "p", "s1"])
    lines = ledger.read_text().splitlines()
    check("clear exits 0", rc, 0)
    check("this slice's open row is ticked", lines[1].startswith("- [x] `p` **s1**"), True)
    check("another slice's row is untouched", lines[3].startswith("- [ ] `p` **s2**"), True)
    check("another bundle's row is untouched", lines[4].startswith("- [ ] `other`"), True)
    check("run-status clear posted for the needs-eyes row only", posted, ["/api/runs/a1/clear"])
    hand = [c for c, _d in ran if "--acted" in c]
    check("handoff --acted gets a1+a2, never a3",
          hand and sorted(hand[0][hand[0].index("--acted") + 1:hand[0].index("--reason")]), ["a1", "a2"])
    rc = q.main(["clear", "p", "s2"])
    check("a refused clear exits nonzero", rc, 2)
    dec = [json.loads(l) for l in (disp / "decisions.jsonl").read_text().splitlines()]
    check("every action and refusal is logged",
          [(d["action"], d["outcome"]) for d in dec],
          [("clear", "dry-run"), ("clear", "done"), ("clear", "refused")])
    q._api = lambda m, p, body=None, timeout=10: (None, "down")
    check("API down -> clear refuses (cannot prove no sign-off pending)",
          q.main(["clear", "p", "s1"]), 2)
    # --- runs-clear: preview by default, explicit ids only, refusals nonzero ------
    calls = []
    def _rapi(m, p, body=None, timeout=10):
        calls.append((m, p, body))
        if m == "GET":
            return 200, {"mode": "live", "scanned": 3, "eligible": [
                {"id": "h1", "label": "auto-author-p-s1", "category": "harness", "reason": "r"}]}
        return 200, {"results": [{"id": i, "ok": i == "h1",
                                  "result": "cleared" if i == "h1" else "REFUSED"} for i in body["ids"]]}
    q._api = _rapi
    check("runs-clear with no --apply only previews (GET, never POST)",
          (q.main(["runs-clear"]), [c[0] for c in calls]), (0, ["GET"]))
    calls.clear()
    check("runs-clear --apply --dry-run never POSTs",
          (q.main(["runs-clear", "--apply", "h1", "--dry-run"]), [c[0] for c in calls]), (0, ["GET"]))
    calls.clear()
    check("runs-clear --apply posts exactly the named ids",
          (q.main(["runs-clear", "--apply", "h1"]), calls[-1][2]), (0, {"ids": ["h1"]}))
    check("runs-clear --apply with a refused id exits nonzero",
          q.main(["runs-clear", "--apply", "h1", "deliv"]), 2)
    q._api = lambda m, p, body=None, timeout=10: (None, "down")
    check("runs-clear: API down refuses", q.main(["runs-clear"]), 2)
    # --- retire: supersede + plan --cancel + resolve rows, with refusals ------------
    os.environ["OLLAMA_SUPERSEDED_FILE"] = str(root / "superseded.json")
    sp = importlib.util.spec_from_file_location("pc_t", str(HERE / "plan_cancel.py"))
    pcm = importlib.util.module_from_spec(sp); sp.loader.exec_module(pcm)
    rp_plan = root / "rp-plan.json"; rp_plan.write_text("{}")
    (disp / "slice-runs" / "rp.json").write_text(json.dumps({
        "label": "rp", "plan_path": str(rp_plan), "order": ["s1", "s2"],
        "slices": {"s1": {"status": "done"}, "s2": {"status": "pending"}}}))
    def qrows(*rows): qstate.write_text(json.dumps({"jobs": list(rows)}))
    R1 = {"id": "r1", "label": "auto-author-rp-s1", "status": "failed"}
    R2 = {"id": "r2", "label": "auto-refine-rp-s1-r1", "status": "done"}
    R3 = {"id": "r3", "label": "auto-author-rp-s2", "status": "paused"}
    OTHER = {"id": "o1", "label": "auto-author-other-s1", "status": "failed"}
    calls = []
    def rrun(cmd, dry, timeout=600):
        calls.append((list(map(str, cmd)), dry))
        if dry:
            return 0, ""
        if "--cancel" in cmd:
            pcm.mark_cancelled("rp", "t", "t", runs_dir=disp / "slice-runs")
        if "resolve" in cmd:
            qrows(*[j for j in json.loads(qstate.read_text())["jobs"] if j["id"] != cmd[-1]])
        return 0, ""
    q._run = rrun
    q._api = lambda m, p, body=None, timeout=10: (200, [])
    qrows(R1, R2, R3, OTHER)
    supf = lambda: json.loads((root / "superseded.json").read_text()) if (root / "superseded.json").exists() else {}
    before_q = qstate.read_text()
    check("retire without --reason refused", q.main(["retire", "rp"]), 2)
    check("retire with a blank --reason refused", q.main(["retire", "rp", "--reason", "   "]), 2)
    check("...and nothing was touched", (calls, supf(), qstate.read_text()), ([], {}, before_q))
    qrows(R1, {"id": "pj", "label": "auto-author-rp-s2", "status": "pending"})
    check("retire refuses while a job is PENDING", q.main(["retire", "rp", "--reason", "r"]), 2)
    qrows(R1, {"id": "rj", "label": "auto-author-rp-s2", "status": "running"})
    check("retire refuses while a job is RUNNING", q.main(["retire", "rp", "--reason", "r"]), 2)
    qrows(R1, R2)
    q._api = lambda m, p, body=None, timeout=10: (200, [{"id": "r2", "awaiting_signoff": True}])
    check("retire refuses a row awaiting_signoff", q.main(["retire", "rp", "--reason", "r"]), 2)
    q._api = lambda m, p, body=None, timeout=10: (None, "down")
    check("retire refuses when the API is down (cannot prove no sign-off)", q.main(["retire", "rp", "--reason", "r"]), 2)
    check("refusals changed nothing", (calls, supf(), (disp / "slice-runs" / "rp.cancelled").exists()), ([], {}, False))
    q._api = lambda m, p, body=None, timeout=10: (200, [])
    qrows(R1, R2, R3, OTHER)
    check("retire --dry-run exits 0", q.main(["retire", "rp", "--reason", "r", "--dry-run"]), 0)
    check("--dry-run: every tool call is a dry call, no marker written",
          (all(d for _c, d in calls), supf(), (disp / "slice-runs" / "rp.cancelled").exists()), (True, {}, False))
    calls.clear()
    check("retire exits 0", q.main(["retire", "rp", "--reason", "shipped in abc123"]), 0)
    m = supf().get("rp") or {}
    check("supersede marker recorded WITH the reason", "shipped in abc123" in str(m.get("reason")), True)
    check("plan cancel marker written by retire (no external tool)",
          (disp / "slice-runs" / "rp.cancelled").exists(), True)
    check("finished rows resolved (failed + done); paused row and other bundle kept",
          sorted(c[-1] for c, _d in calls if "resolve" in c), ["r1", "r2"])
    check("queue still holds the paused row and the OTHER bundle's row",
          sorted(j["id"] for j in json.loads(qstate.read_text())["jobs"]), ["o1", "r3"])
    calls.clear()
    check("retire is idempotent (rc 0)", q.main(["retire", "rp", "--reason", "a DIFFERENT reason"]), 0)
    check("...no second cancel, no resolve, first reason kept",
          (calls, "shipped in abc123" in str(supf()["rp"]["reason"])), ([], True))
    dec = [json.loads(l) for l in (disp / "decisions.jsonl").read_text().splitlines()]
    check("retire actions and refusals are logged",
          [d["outcome"] for d in dec if d["action"] == "retire"].count("refused") >= 5
          and [d["outcome"] for d in dec if d["action"] == "retire"].count("done") == 2, True)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 0 if not FAILS else 1


MUTATIONS = [
    ("clear any slice status", "    if slice_status not in CLEARABLE_SLICE:", "    if False:"),
    ("ignore live jobs", "    if live:\n        return (\"a live agent still needs", "    if False:\n        return (\"a live agent still needs"),
    ("ignore live driver", "    if driver:\n        return f\"the plan's advance driver", "    if False:\n        return f\"the plan's advance driver"),
    ("clear awaiting-signoff rows", "        if r.get(\"awaiting_signoff\"):", "        if False:"),
    ("re-clear handled runs", "        if r and r.get(\"group\") != \"handled\":", "        if r:"),
    ("dry-run writes the ledger", "        if not a.dry_run:\n            lines = LEDGER", "        if True:\n            lines = LEDGER"),
    ("API down tolerated", "    if code != 200 or not isinstance(runs, list):\n        raise Refused",
     "    if False:\n        raise Refused"),
    ("runs-clear preview posts", "    if not a.apply or a.dry_run:", "    if not a.apply:"),
    ("runs-clear swallows refusals", "    if bad:\n        raise Refused", "    if False:\n        raise Refused"),
    ("runs-clear widens scope", '{"ids": a.apply}', '{"ids": a.apply + ["extra"]}'),
    ("retire ignores live jobs", '    live = [j for j in rows if j.get("status") in RETIRE_LIVE]', '    live = []'),
    ("retire ignores sign-off", '    held = [j["id"] for j in rows if j.get("id") in set(signoff_ids)]', '    held = []'),
    ("retire reason optional", '    if not (reason or "").strip():\n        return "--reason is required', '    if False:\n        return "--reason is required'),
    ("retire never cancels", '"--cancel", "--reason", f"retired via', '"--status", "--reason", f"retired via'),
    ("retire resolves paused rows", 'RETIRE_RESOLVABLE = ("done",', 'RETIRE_RESOLVABLE = ("paused", "done",'),
    ("retire re-cancels (not idempotent)", '    if own:\n        print("  cancel: plan already', '    if False:\n        print("  cancel: plan already'),
    ("retire overwrites the first reason", '    if a.bundle in bv.load_superseded():', '    if False:'),
    ("retire dry-run writes the marker", '    elif a.dry_run:\n        print(f"  [dry-run] would mark', '    elif False:\n        print(f"  [dry-run] would mark'),
    ("retire dry-run really resolves", '"resolve", j["id"]], a.dry_run, 120)', '"resolve", j["id"]], False, 120)'),
    ("retire tolerates API down", '    if not api_ok:\n        raise Refused', '    if False:\n        raise Refused'),
    ("retire resolves the other bundle", 'return [j for j in jobs if j.get("bundle") == bundle', 'return [j for j in jobs if j.get("bundle") == bundle or "other" in str(j.get("label"))'),
]


def revert_check():
    bad = 0
    src = SRC.read_text()
    for name, old, new in MUTATIONS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix="-qctl", delete=False) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "QCTL_SRC": f.name},
                           capture_output=True, text=True, timeout=120)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": revert '{name}' -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
