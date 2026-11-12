#!/usr/bin/env python3
"""test-prefetch-register.py -- the pipeline FEEDS cpu-prefetch's backlog, idempotently and safely.

Hermetic: every state dir is a temp dir (CPU_PREFETCH_DIR / OLLAMA_DISPATCH_DIR / OLLAMA_DISPATCH_AUTO_RUNS_DIR);
nothing real is launched or registered.
  1. cpu-prefetch.register: new -> registered; again -> already (no 2nd entry); finished entry is NOT resurrected
     (done/failed/cancelled -> skip:*); bad input never raises (error:*); force re-arms; decisions.jsonl line
  2. ollama-dispatch-auto --prefetch: registers the exact argv (minus --prefetch) as kind=start and runs NO driver;
     --resume-harness registers kind=resume from the argv record; no --label -> exit 2; registration error -> exit 1
  3. dispatch-self-heal.resume_auto_driver: launch OK -> immediate relaunch, NOTHING registered (a stuck chain never
     waits on prefetch admission); launch raises -> converged harness handed to the backlog as kind=resume and the
     sweep does not raise; prefetch_register tolerates a broken prefetcher
  4. candidates: advisory only (excludes exit 0 / live / already queued / no harness) and never writes the backlog
  5. end to end: a registered entry is launched by run_pass under the usual gates, and a held READY-TO-LAND label
     registered the same way is still refused at launch (the launch gate stays the single authority)
Exit 0 = all pass (prints PREFETCH_REGISTER_OK)."""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path
from types import SimpleNamespace

HERE = Path(__file__).resolve().parent
T = Path(tempfile.mkdtemp(prefix="prefetch-reg-"))
os.environ.update(CPU_PREFETCH_DIR=str(T / "pf"), OLLAMA_DISPATCH_DIR=str(T / "od"),
                  OLLAMA_DISPATCH_AUTO_RUNS_DIR=str(T / "od" / "auto-runs"),
                  OLLAMA_QUEUE_STATE=str(T / "queue.json"), OLLAMA_QUEUE_NO_NOTIFY="1",
                  DISPATCH_VERIFY_SANDBOX="1")
RUNS = T / "od" / "auto-runs"
(RUNS / "argv").mkdir(parents=True)
FAILS = []


def check(name, got, want=True):
    ok = got == want
    print(("ok  : " if ok else "FAIL: ") + name + ("" if ok else " -- got %r, want %r" % (got, want)))
    if not ok:
        FAILS.append(name)


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    ld.exec_module(m)
    return m


pf = load(HERE / "cpu-prefetch.py", "pf_t")
heal = load(HERE / "dispatch-self-heal.py", "heal_t")
auto = load(HERE / "ollama-dispatch-auto", "auto_t")
BL = T / "pf" / "backlog"


def entries():
    return sorted(p.stem for p in BL.glob("*.json")) if BL.is_dir() else []


def decisions():
    p = T / "pf" / "decisions.jsonl"
    return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []


# ------------------------------------------------------------------ 1. register
check("new -> registered", pf.register("lab-a", ["--repo", "/r", "--label", "lab-a"], source="t"), "registered")
check("entry carries source", json.loads((BL / "lab-a.json").read_text()).get("source"), "t")
check("again -> already:queued, no second entry",
      (pf.register("lab-a", ["--repo", "/r", "--label", "lab-a"]), entries()), ("already:queued", ["lab-a"]))
check("registration logged", [d["action"] for d in decisions() if d.get("label") == "lab-a"], ["registered"])
for st in ("done", "failed", "cancelled"):
    pf.set_entry("lab-a", status=st)
    check("finished (%s) entry is not resurrected" % st, pf.register("lab-a", ["--label", "lab-a"]), "skip:" + st)
check("force re-arms a finished entry", pf.register("lab-a", ["--label", "lab-a"], force=True), "registered")
check("no argv -> error, no raise", pf.register("lab-b", []).startswith("error:"), True)
check("label mismatch -> error, no raise", pf.register("lab-c", ["--label", "zzz"]).startswith("error:"), True)
check("resume without argv record -> error, no raise", pf.register("lab-d", None, "resume").startswith("error:"), True)
check("failed registrations left no entry", entries(), ["lab-a"])

# ------------------------------------------------------------------ 2. auto --prefetch
base = [sys.executable, str(HERE / "ollama-dispatch-auto")]
r = subprocess.run(base + ["--repo", str(T), "--target", "x.ts", "--lang", "typescript", "--label", "lab-p",
                           "--bundle", "bun-p", "--intent", "do a thing", "--prefetch"],
                   capture_output=True, text=True, timeout=60, env=dict(os.environ))
e = json.loads((BL / "lab-p.json").read_text()) if (BL / "lab-p.json").exists() else {}
check("--prefetch exits 0 and registers", (r.returncode, e.get("kind"), e.get("bundle")), (0, "start", "bun-p"))
check("--prefetch stores the argv without --prefetch, intent intact",
      ("--prefetch" in e.get("argv", []), "do a thing" in e.get("argv", [])), (False, True))
check("--prefetch ran no driver (no chain record, no worktree)", (list(RUNS.glob("*.json")), (T / "wt").exists()), ([], False))
r = subprocess.run(base + ["--repo", str(T), "--target", "x.ts", "--lang", "typescript", "--intent", "i", "--label", "lab-p", "--prefetch"], capture_output=True, text=True,
                   timeout=60, env=dict(os.environ))
check("--prefetch twice is a no-op (exit 0, already)", (r.returncode, "already" in r.stdout), (0, True))
r = subprocess.run(base + ["--repo", str(T), "--prefetch"], capture_output=True, text=True, timeout=60, env=dict(os.environ))
check("--prefetch without --label exits 2", r.returncode, 2)
(RUNS / "argv" / "lab-r.json").write_text(json.dumps({"label": "lab-r", "bundle": "bun-r", "worktree": str(T / "wtr"),
                                                      "argv": ["--repo", "/r", "--label", "lab-r"], "cwd": "/"}))
r = subprocess.run(base + ["--repo", "/r", "--target", "x.ts", "--lang", "typescript", "--intent", "i", "--label", "lab-r", "--resume-harness", "--prefetch"],
                   capture_output=True, text=True, timeout=60, env=dict(os.environ))
e = json.loads((BL / "lab-r.json").read_text()) if (BL / "lab-r.json").exists() else {}
check("--prefetch --resume-harness registers kind=resume from the record",
      (r.returncode, e.get("kind"), e.get("argv", []).count("--resume-harness")), (0, "resume", 1))
bad = SimpleNamespace(label="lab-e", bundle=None)


class Boom:
    def register(self, *a, **k):
        raise RuntimeError("x")
check("prefetch_register: broken prefetcher -> exit 1, no raise", auto.prefetch_register(bad, ["--label", "lab-e"], mod=Boom()), 1)

# ------------------------------------------------------------------ 3. self-heal
(RUNS / "argv" / "rt-x.json").write_text(json.dumps({"label": "rt-x", "bundle": "rt-xb", "worktree": str(T / "wtx"),
                                                     "argv": ["--repo", "/r", "--label", "rt-x"], "cwd": str(T)}))
job = {"id": "c1", "label": "auto-author-rt-x-c1", "status": "needs_opus", "bundle": "rt-xb", "cwd": str(T)}
heal.AUTO_RUNS = RUNS
launched = []
act = heal.resume_auto_driver(job, "cA", runs_dir=RUNS, launch=lambda c, w, l: launched.append(c) or 99,
                              ledger_path=T / "heal.json", alive=lambda p: False, slice_runs=T / "nosr",
                              decisions=T / "dec.jsonl", notifier=lambda *a, **k: None)
check("launch ok -> immediate relaunch, nothing registered", (act.startswith("resumed:"), len(launched), "rt-x" in entries()),
      (True, 1, False))


def boom(cmd, cwd, log):
    raise OSError("no fork")
act = heal.resume_auto_driver(job, "cB", runs_dir=RUNS, launch=boom, ledger_path=T / "heal2.json",
                              alive=lambda p: False, slice_runs=T / "nosr", decisions=T / "dec.jsonl",
                              notifier=lambda *a, **k: None)
e = json.loads((BL / "rt-x.json").read_text()) if (BL / "rt-x.json").exists() else {}
check("launch raises -> deferred to the backlog as kind=resume, sweep survives",
      (act, e.get("kind"), e.get("bundle"), e.get("argv", [])[-1:]), ("deferred:registered", "resume", "rt-xb", ["--resume-harness"]))
check("self-heal prefetch_register tolerates a broken prefetcher",
      heal.prefetch_register("zz", "resume", "w", mod=Boom()), "error:RuntimeError")

# ------------------------------------------------------------------ 4. candidates
wt = T / "wt-cand"
wt.mkdir()
for f in pf.HARNESS_FILES:
    (wt / f).write_text("x")
for lbl, outcome, with_h in (("c-open", None, True), ("c-landed", "exit 0", True), ("c-noharness", None, False)):
    w = wt if with_h else T / "nowt"
    (RUNS / "argv" / (lbl + ".json")).write_text(json.dumps({"label": lbl, "bundle": lbl, "worktree": str(w), "argv": ["--label", lbl]}))
    if outcome:
        (RUNS / (lbl + ".json")).write_text(json.dumps({"label": lbl, "phase": "ended", "outcome": outcome}))
before = entries()
pf.ps_rows = lambda: []
got = pf.candidates()
check("candidates: authored+no-driver+not exit 0 only", [c for c in got if c.startswith("c-")], ["c-open"])
check("candidates never write the backlog", entries(), before)

# ------------------------------------------------------------------ 4b. driver detection is exact
D = "/opt/homebrew/Cellar/python@3.14/x/Python.app/Contents/MacOS/Python /Users/user/bin/ollama-dispatch-auto --repo /r --label g"
check("real driver command lines count as a driver",
      [pf._cmd_is_driver(c, "g") for c in (D, "python3 /b/ollama-dispatch-auto --label g", "/b/ollama-dispatch-auto --label g")],
      [True, True, True])
check("a shell/tail/grep that merely MENTIONS the tool + label is not a driver",
      [pf._cmd_is_driver(c, "g") for c in ("/bin/zsh -c cd ~/bin && python3 ollama-dispatch-auto --label g --prefetch",
                                           "tail -f x/ollama-dispatch-auto --label g", "grep ollama-dispatch-auto --label g")],
      [False, False, False])

# ------------------------------------------------------------------ 5. end to end through the launch gates
for f in BL.glob("*.json"):
    f.unlink()
pf.register("e2e-ok", ["--label", "e2e-ok", "--repo", "/r"], source="t")
pf.register("e2e-held", ["--label", "e2e-held", "--repo", "/r"], source="t")
started = []
view = {"jobs": [], "parked": {}, "commit": {}}
helpers = dict(open_index_rows=lambda k: {"e2e-held"} & set(k), superseded=lambda k: set(),
               slicer_owns=lambda b: False, tree_lock_held=lambda w: False)
res = pf.run_pass(launch=lambda cmd, cwd, log: started.append(cmd) or 4321, rows=[], view=view,
                  cfg=dict(pf.DEFAULTS), helpers=helpers, load=0.1, lane=0)
acts = {l: a for l, a, _ in res if l.startswith("e2e")}
check("registered entry is launched; held READY-TO-LAND one is refused at launch",
      (acts.get("e2e-ok"), acts.get("e2e-held"), len(started)), ("launched", "skip", 1))

print("PREFETCH_REGISTER_OK" if not FAILS else "FAILED: %s" % FAILS)
sys.exit(1 if FAILS else 0)
