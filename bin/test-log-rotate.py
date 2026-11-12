#!/usr/bin/env python3
"""log-rotate: age cutoff, protected refs, allowlist, symlinks, live-file, dry-run/apply, variable
retention (3 levels, inheritance, precedence, pins), config fallback/validation, preview, lock, audit cap.
Hermetic: builds a fake HOME in a temp dir (LOG_ROTATE_HOME) -- never touches real logs.
--revert-check: copies log_retention_config.py + log_rotate_lib.py, applies each mutant, and the suite
(run with LR_SRC_DIR=<mutant dir>) must FAIL for every one."""
import json, os, shutil, subprocess, sys, tempfile, time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("LR_SRC_DIR") or HERE)

MUTANTS = [  # (name, file, old, new)
    ("age cutoff ignored", "log_rotate_lib.py", 'elif now - mt < e["days"] * 86400:', 'elif now - mt < 0:'),
    ("live-queue/handoff id protection off", "log_rotate_lib.py", 'elif jid and jid in prot["ids"]:', 'elif False:'),
    ("closed escalation row protected", "log_rotate_lib.py", 'if line.lstrip().startswith("- [ ]"):', 'if True:'),
    ("open escalation unprotected", "log_rotate_lib.py", 'if line.lstrip().startswith("- [ ]"):', 'if False:'),
    ("signoff protection off", "log_rotate_lib.py", 'rec.get("required") and rec.get("verdict") != "approve"', 'False'),
    ("plan follows symlinks", "log_rotate_lib.py", '            if os.path.islink(p):\n                e["reason"] = "symlink-refused"', '            if False:\n                e["reason"] = "symlink-refused"'),
    ("allowed() accepts symlinks", "log_rotate_lib.py", '    if p.is_symlink():\n        return False\n    for key', '    for key'),
    ("allowed() ignores name pattern", "log_rotate_lib.py", 'if p.parent == root and rx.match(p.name) and', 'if p.parent == root and True and'),
    ("allowed() ignores root", "log_rotate_lib.py", 'if p.parent == root and rx.match(p.name) and', 'if True and rx.match(p.name) and'),
    ("symlink inside dir ignored", "log_rotate_lib.py", 'if os.path.islink(os.path.join(r, n)):\n                sym = True', 'if False:\n                sym = True'),
    ("live-file grace off", "log_rotate_lib.py", 'elif now - mt < LIVE_GRACE_S:', 'elif False:'),
    ("recheck-at-delete off", "log_rotate_lib.py", 'if min_age_s is not None and time.time() - m < min_age_s:', 'if False:'),
    ("dry-run deletes", "log_rotate_lib.py", '                if apply:\n                    ok, why', '                if True:\n                    ok, why'),
    ("pin (job) ignored", "log_rotate_lib.py", 'if p.get("type") == "job" and p.get("id") == jid:', 'if False:'),
    ("pin (bundle) ignored", "log_rotate_lib.py", 'if p.get("type") == "bundle" and bundle and p.get("id") == bundle:', 'if False:'),
    (".keep marker ignored", "log_rotate_lib.py", 'return bool(jid) and (Path(ql) / f"{jid}.keep").exists()', 'return False'),
    ("missing protection source tolerated", "log_rotate_lib.py", 'raise SourceError(f"{p} missing")', 'return {}'),
    ("lock not taken", "log_rotate_lib.py", 'fcntl.flock(lock_f, fcntl.LOCK_EX | fcntl.LOCK_NB)', 'None'),
    ("transcript ref check off", "log_rotate_lib.py", '            if rj:\n', '            if False:\n'),
    ("concerns not a failure", "log_rotate_lib.py", 'v in ("fail", "concerns")', 'v in ("fail",)'),
    ("failed status not a failure", "log_rotate_lib.py", 'st in ("failed", "error")', 'st in ("error",)'),
    ("escalated not a failure", "log_rotate_lib.py", 'or jid in prot["escalated"]', 'or False'),
    ("parked bundle not a failure", "log_rotate_lib.py", 'or (bundle and bundle in prot["parked"])', 'or False'),
    ("override (failures/successful) ignored", "log_retention_config.py", 'if _is_int(o) and o > 0:', 'if False:'),
    ("ledger floor off", "log_retention_config.py", 'base = max(base, cfg.get("ledger_days") or LEDGER_DEFAULT)', 'base = base'),
    ("default fallback != 30", "log_retention_config.py", 'DEFAULT_DAYS = 30', 'DEFAULT_DAYS = 31'),
    ("max bound loosened", "log_retention_config.py", 'MIN_DAYS, MAX_DAYS = 1, 365', 'MIN_DAYS, MAX_DAYS = 1, 3650'),
    ("zero accepted as days (all)", "log_retention_config.py", 'if _is_int(v) and MIN_DAYS <= v <= MAX_DAYS:\n        cfg["all"] = v', 'if _is_int(v):\n        cfg["all"] = v'),
    ("failures zero not inherit", "log_retention_config.py", '        if _is_int(v) and MIN_DAYS <= v <= MAX_DAYS:\n            cfg[k] = v', '        if _is_int(v):\n            cfg[k] = v'),
    ("corrupt config crashes", "log_retention_config.py", 'except (OSError, ValueError) as e:\n        return default_config(), [f"cannot read', 'except OSError as e:\n        return default_config(), [f"cannot read'),
    ("audit log uncapped", "log_retention_config.py", 'if p.stat().st_size > cap_bytes:', 'if False:'),
    ("save skips validation", "log_retention_config.py", '    errs = validate(cfg)\n    if errs:\n        raise ValueError(errs)\n    out, _', '    out, _'),
    ("save not atomic (no replace)", "log_retention_config.py", 'os.replace(tmp, p)\n    ch =', 'shutil_copy = None\n    ch ='),
    ("preview deletes", "log_retention_config.py", 'apply=False, paths=paths, audit=False', 'apply=True, paths=paths, audit=False'),
]

if "--revert-check" in sys.argv:
    bad = 0
    for name, fn, old, new in MUTANTS:
        d = Path(tempfile.mkdtemp(prefix="lrmut-"))
        for f in ("log_retention_config.py", "log_rotate_lib.py"):
            shutil.copy(HERE / f, d / f)
        s = (d / fn).read_text()
        if s.count(old) != 1:
            print(f"BAD MUTANT (pattern count {s.count(old)}): {name}"); bad += 1; continue
        (d / fn).write_text(s.replace(old, new))
        r = subprocess.run([sys.executable, str(Path(__file__).resolve())], env=dict(os.environ, LR_SRC_DIR=str(d)),
                           capture_output=True, text=True, timeout=300)
        killed = r.returncode != 0
        print(("killed   " if killed else "SURVIVED ") + name)
        bad += (not killed)
        shutil.rmtree(d, ignore_errors=True)
    print(f"{len(MUTANTS)-bad}/{len(MUTANTS)} mutants killed")
    sys.exit(1 if bad else 0)

T = Path(tempfile.mkdtemp(prefix="lr-"))
os.environ["LOG_ROTATE_HOME"] = str(T)
os.environ["HANDOFF_DIR"] = str(T / "handoff")
os.environ.pop("LOG_RETENTION_CONFIG", None)
sys.path.insert(0, str(SRC))
import log_retention_config as C
import log_rotate_lib as L

FAILS = []
def chk(n, ok):
    print(("ok  " if ok else "FAIL") + " - " + n)
    if not ok: FAILS.append(n)

DAY = 86400
NOW = time.time()
def mk(path, age_days=0, size=10, text=None, age_s=None):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text if text is not None else "x" * size)
    t = NOW - (age_s if age_s is not None else age_days * DAY)
    os.utime(path, (t, t), follow_symlinks=False)
    return path

def world():
    shutil.rmtree(T, ignore_errors=True)
    for d in ("bin/ollama-queue-logs", "bin/ollama-queue-livelogs", "bin/ollama-worker-logs", ".ollama-dispatch/escalations",
              ".darkbloom/power", "handoff/pending", "handoff/complete"):
        (T / d).mkdir(parents=True)
    (T / "bin/ollama-queue-state.json").write_text(json.dumps({"jobs": [], "_bundle_parked": {}}))
    (T / "bin/signoff.json").write_text("{}")
    (T / ".ollama-dispatch/escalations/ESCALATIONS.md").write_text("")
    return L.make_paths(T)

def hexid(n): return f"{n:012x}"
def qlog(P, jid, age, label="job"): return mk(P["queue_logs"] / f"{jid}-{label}.log", age)
def done(P, jid, age, status="done", bundle=None, fc=None, tr=None):
    return mk(P["queue_logs"] / f"{jid}.done.json", age, text=json.dumps({"id": jid, "status": status, "bundle": bundle, "failure_class": fc, "terminal_reason": tr}))
def gate(P, jid, age, verdict):
    return mk(P["queue_logs"] / f"{jid}.gate.json", age, text=json.dumps({"verdict": verdict}))
def cfg(**kw):
    c = C.default_config(); c.update(kw); return c
def deleted(res): return {Path(e["path"]).name for e in res["entries"] if e["action"] == "delete"}
def exists(p): return os.path.lexists(p)

# ---------- 1 age cutoff + dry-run vs apply + idempotence
P = world()
old = qlog(P, hexid(1), 40); young = qlog(P, hexid(2), 5); lv = mk(P["livelogs"] / f"{hexid(1)}-job.livelog", 40)
r = L.run(cfg(), apply=False, paths=P, audit=False)
chk("dry-run lists the 40d log + livelog, not the 5d log", {old.name, lv.name} <= deleted(r) and young.name not in deleted(r))
chk("dry-run deletes nothing", exists(old) and exists(lv) and r["total"]["count"] == 2)
r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("apply deletes the 40d files, keeps the 5d file", not exists(old) and not exists(lv) and exists(young))
chk("bytes freed reported", r["total"]["bytes"] == 20)
r2 = L.run(cfg(), apply=True, paths=P, audit=False)
chk("idempotent: second apply deletes nothing", r2["total"]["count"] == 0 and exists(young))
chk("missing dirs (no worker logs / livelogs) do not crash", L.run(cfg(), apply=True, paths=dict(P, livelogs=T/"nope", worker_logs=T/"nope2"), audit=False)["errors"] == [])

# ---------- 2 protected references (regardless of age)
P = world()
a, b, c, d, e, f = (hexid(i) for i in range(10, 16))
files = {x: qlog(P, x, 100) for x in (a, b, c, d, e, f)}
(T / "bin/ollama-queue-state.json").write_text(json.dumps({"jobs": [{"id": a, "status": "running"}], "_bundle_parked": {}}))
mk(P["handoff"] / "complete" / f"2026-10-01-thing-{b}.md", 1)
(T / "bin/signoff.json").write_text(json.dumps({c: {"required": True, "verdict": None}, f: {"required": True, "verdict": "approve"}}))
P["escalations"].write_text(f"- [ ] `x` **{d}** D - open\n- [x] `y` **{e}** D - closed\n")
r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("live queue row protected", exists(files[a]))
chk("unprocessed handoff item protected", exists(files[b]))
chk("awaiting-signoff protected", exists(files[c]))
chk("open ESCALATIONS row protected", exists(files[d]))
chk("closed escalation row NOT protected (old, deleted)", not exists(files[e]))
chk("approved sign-off not protected", not exists(files[f]))
chk("skipped-and-why recorded", r["skipped"].get("protected:live-queue-row") == 1 and r["skipped"].get("protected:open-escalation") == 1)
# protection source unreadable -> delete NOTHING
(T / "bin/ollama-queue-state.json").unlink()
old2 = qlog(P, hexid(30), 100)
r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("missing queue state: nothing deleted + error logged", exists(old2) and r["errors"] and r["total"]["count"] == 0)
(T / "bin/ollama-queue-state.json").write_text("{not json")
r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("corrupt queue state: nothing deleted", exists(old2) and r["total"]["count"] == 0)
(T / "bin/ollama-queue-state.json").write_text(json.dumps({"jobs": []}))

# ---------- 3 allowlist refusal (+ tax records / unknown names never touched)
P = world()
outside = mk(T / "elsewhere" / f"{hexid(40)}-x.log", 400)
chk("safe_delete refuses a path outside the allowlist", L.safe_delete(outside, P)[0] is False and exists(outside))
unlisted = mk(P["queue_logs"] / "dispatch-escalation-watcher.log", 400)
chk("safe_delete refuses an unlisted name inside an allowlisted dir", L.safe_delete(unlisted, P)[0] is False and exists(unlisted))
csv = mk(T / ".darkbloom/power/power-log-2026-08.csv", 400)
arch = mk(T / ".darkbloom/earnings-archive/earnings-2026-08.jsonl", 400)
metrics = mk(P["worker_logs"] / "dispatch-metrics.jsonl", 400)
cpu = mk(P["dispatch"] / "cpu-lane.log", 400)
wt = mk(P["dispatch"] / "worktrees" / "wt1" / "build.log", 400)
arc = mk(P["queue_logs"] / "archive" / f"{hexid(41)}.gate.json", 900)
mk(P["queue_logs"] / "verify-orphan-reap.jsonl", 400)
L.run(cfg(), apply=True, paths=P, audit=False)
chk("tax records, metrics, growing logs, worktrees, archive/, unlisted names survive apply",
    all(exists(x) for x in (csv, arch, metrics, cpu, wt, arc, unlisted, P["queue_logs"] / "verify-orphan-reap.jsonl")))

# ---------- 4 symlinks
P = world()
tgt = mk(T / "precious.txt", 400)
ln = P["queue_logs"] / f"{hexid(50)}-link.log"; os.symlink(tgt, ln)
os.utime(ln, (NOW - 400 * DAY,) * 2, follow_symlinks=False)
r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("symlink named like a log is not deleted and target survives", os.path.islink(ln) and exists(tgt))
chk("safe_delete refuses a symlink", L.safe_delete(ln, P)[0] is False and os.path.islink(ln))
chk("plan reports symlink-refused", r["skipped"].get("symlink-refused") == 1)
rd = P["queue_logs"] / f"{hexid(51)}-review"; mk(rd / "run.json", 400); os.symlink(tgt, rd / "evil")
os.utime(rd / "run.json", (NOW - 400 * DAY,) * 2); os.utime(rd, (NOW - 400 * DAY,) * 2)
r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("review dir containing a symlink is refused whole", exists(rd / "run.json") and exists(tgt))
chk("safe_delete refuses a dir containing a symlink", L.safe_delete(rd, P)[0] is False and exists(rd / "run.json"))
linkroot = T / "bin" / "linkroot"; os.symlink(P["queue_logs"], linkroot)

# ---------- 5 live file + dir unit + recheck-at-delete
P = world()
fresh = qlog(P, hexid(60), 0, "big"); big_old = qlog(P, hexid(61), 5, "big")
L.LIVE_GRACE_S = 10 * DAY   # make 'recent write' wider than the 1-day retention floor to isolate the live guard
r = L.run(cfg(all=1), apply=True, paths=P, audit=False)
chk("recently-written log is never deleted (live-file skip)", exists(fresh) and exists(big_old) and r["skipped"].get("live-file(recent-write)") == 2)
L.LIVE_GRACE_S = 3600
okd, why = L.safe_delete(fresh, P, min_age_s=3600)
chk("recheck at delete: file touched since planning is skipped", okd is False and exists(fresh))
rd = P["queue_logs"] / f"{hexid(62)}-review"
mk(rd / "run.json", 100); mk(rd / "report.md", 0)
os.utime(rd, (NOW - 100 * DAY,) * 2)
L.run(cfg(), apply=True, paths=P, audit=False)
chk("review dir with a recent file inside is kept (newest mtime rules)", exists(rd / "run.json"))
mk(rd / "report.md", 100); os.utime(rd / "run.json", (NOW - 100 * DAY,) * 2); os.utime(rd, (NOW - 100 * DAY,) * 2)
L.run(cfg(), apply=True, paths=P, audit=False)
chk("fully old review dir removed as a unit", not exists(rd))

# ---------- 6 variable retention: levels, inheritance, precedence, pins
P = world()
fa, su, un, pn, bn, kp = (hexid(70 + i) for i in range(6))
def trio(jid, age): return qlog(P, jid, age)
done(P, fa, 40, "failed", fc="model"); done(P, su, 40, "done"); gate(P, su, 40, "pass")
F = {"fail": trio(fa, 40), "succ": trio(su, 40), "unk": trio(un, 40)}
F["failst"] = trio(hexid(81), 40); done(P, hexid(81), 40, "failed")
F["fail60"] = trio(hexid(80), 60); done(P, hexid(80), 60, "done"); gate(P, hexid(80), 60, "concerns")
F["pin"] = trio(pn, 400); F["bund"] = trio(bn, 400); done(P, bn, 400, "done", bundle="bun-x")
F["keep"] = trio(kp, 400); mk(P["queue_logs"] / f"{kp}.keep", 0)
c = cfg(failures=90, successful=7, pins=[{"type": "job", "id": pn, "who": "p", "when": "t", "why": "w"},
                                           {"type": "bundle", "id": "bun-x", "who": "p", "when": "t", "why": "w"}])
r = L.run(c, apply=False, paths=P, audit=False)
dl = deleted(r)
chk("failures override (90) keeps a 40d failed log", F["fail"].name not in dl)
chk("failed status alone (no failure_class) counts as failure", F["failst"].name not in dl)
chk("concerns gate verdict counts as failure (60d < 90)", F["fail60"].name not in dl)
chk("successful override (7) deletes a 40d PASS log", F["succ"].name in dl)
chk("unclassified job uses 'all' (30): 40d deleted", F["unk"].name in dl)
chk("job pin beats everything", F["pin"].name not in dl)
chk("bundle pin holds the bundle's logs", F["bund"].name not in dl)
chk(".keep marker holds the job's logs", F["keep"].name not in dl)
chk("pinned counted separately", r["pinned"]["count"] >= 3)
c2 = cfg(failures=None, successful=None)
dl2 = deleted(L.run(c2, apply=False, paths=P, audit=False))
chk("inheritance: failures/successful null -> follow 'all' (30)", {F["fail"].name, F["succ"].name, F["unk"].name} <= dl2)
c3 = cfg(all=60, failures=None, successful=None)
dl3 = deleted(L.run(c3, apply=False, paths=P, audit=False))
chk("all=60: 40d logs kept via inheritance", not ({F["fail"].name, F["succ"].name, F["unk"].name} & dl3))
r4 = L.run(cfg(failures=10, successful=90), apply=False, paths=P, audit=False)
chk("override can be SHORTER than all (failures=10 deletes 40d failed)", F["fail"].name in deleted(r4) and F["succ"].name not in deleted(r4))
chk("levels attributed in result", r4["levels"]["failures"]["count"] >= 1)
P2 = world()
j = hexid(90); done(P2, j, 40, "done", bundle="pb"); gate(P2, j, 40, "pass"); lg = qlog(P2, j, 40)
(T / "bin/ollama-queue-state.json").write_text(json.dumps({"jobs": [], "_bundle_parked": {"pb": {"kind": "stalled"}}}))
chk("parked bundle's logs classified as failures", lg.name not in deleted(L.run(cfg(failures=90), apply=False, paths=P2, audit=False)))
P2["escalations"].write_text(f"- [x] `a` **{hexid(91)}** D - closed\n")
qlog(P2, hexid(91), 40)
chk("escalated (even closed) job classified as failures", f"{hexid(91)}-job.log" not in deleted(L.run(cfg(failures=90), apply=False, paths=P2, audit=False)))

# ---------- 7 ledger sidecars
P = world()
g1 = gate(P, hexid(100), 100, "pass"); g2 = gate(P, hexid(101), 400, "pass"); dj = done(P, hexid(102), 200, "done")
dl = deleted(L.run(cfg(), apply=False, paths=P, audit=False))
chk("gate/done sidecars keep ledger floor (100d/200d kept, 400d deleted)", g1.name not in dl and dj.name not in dl and g2.name in dl)
chk("shortening 'all' cannot shorten the ledger", g1.name not in deleted(L.run(cfg(all=1, successful=1, failures=1), apply=False, paths=P, audit=False)))

# ---------- 8 worker transcripts
P = world()
t_un = mk(P["worker_logs"] / "20260801T000000Z.json", 60)
t_ref = mk(P["worker_logs"] / "20260802T000000Z.json", 60)
t_gone = mk(P["worker_logs"] / "20260803T000000Z.json", 60)
t_sal = mk(P["worker_logs"] / "20260804T000000Z.salvage.patch", 60)
keepj, gonej = hexid(110), hexid(111)
mk(P["queue_logs"] / f"{keepj}-j.log", 5, text=f"x ollama-worker-logs/20260802T000000Z.json y")
mk(P["queue_logs"] / f"{gonej}-j.log", 60, text=f"x ollama-worker-logs/20260803T000000Z.json y")
r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("unreferenced old transcript + salvage patch deleted", not exists(t_un) and not exists(t_sal))
chk("transcript referenced by a SURVIVING log is kept", exists(t_ref))
chk("transcript referenced only by a log deleted in the same run is deleted", not exists(t_gone))
chk("worker-logs non-transcript files untouched", exists(mk(P["worker_logs"] / "queue-daemon.log", 400)) and exists(P["worker_logs"] / "queue-daemon.log"))

# ---------- 9 concurrency lock
P = world()
v = qlog(P, hexid(120), 100)
import fcntl
with open(P["lock"], "w") as lk:
    fcntl.flock(lk, fcntl.LOCK_EX)
    r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("concurrent run backs off (lock held): busy, nothing deleted", r["busy"] and exists(v))
r = L.run(cfg(), apply=True, paths=P, audit=False)
chk("lock released -> runs", not exists(v) and not r["busy"])

# ---------- 10 config: defaults, fallback, validation, save, audit cap, preview
P = world()
cp = T / ".ollama-dispatch" / "log-retention.json"
chk("missing config -> 30 default", C.load(cp)["all"] == 30)
cp.write_text("{ not json")
cc, errs = C.load_checked(cp)
chk("corrupt config -> defaults + error reported, no crash", cc["all"] == 30 and errs)
cp.write_text(json.dumps({"version": 1, "all": 0, "failures": 0, "successful": 999}))
cc, errs = C.load_checked(cp)
chk("all=0 -> 30; failures=0 -> inherit; successful=999 out of range -> inherit", cc["all"] == 30 and cc["failures"] is None and cc["successful"] is None)
cp.write_text(json.dumps({"all": "ten", "pins": "nope"}))
chk("garbage types -> defaults", C.load(cp)["all"] == 30 and C.load(cp)["pins"] == [])
cp.write_text(json.dumps([1, 2]))
chk("non-object config -> defaults", C.load(cp)["all"] == 30)
cp.write_text(json.dumps({"all": 14, "failures": 90}))
chk("valid partial config honoured", C.load(cp) ["all"] == 14 and C.load(cp)["failures"] == 90 and C.load(cp)["successful"] is None)
chk("resolve_days precedence: override > all > 30", C.resolve_days({"all": 14, "failures": 90}, "failures") == 90
    and C.resolve_days({"all": 14, "failures": 90}, "successful") == 14 and C.resolve_days({}, None) == 30)
chk("validate: bounds", C.validate(cfg(all=0)) and C.validate(cfg(all=366)) and C.validate(cfg(failures=400)) and C.validate(cfg(successful=0))
    and not C.validate(cfg(all=365, failures=1, successful=None)))
chk("validate: bool/str rejected", C.validate(cfg(all=True)) and C.validate(cfg(all="30")))
chk("validate: pins need who/when/why + 12-hex job id", C.validate(cfg(pins=[{"type": "job", "id": "zz"}])) and
    not C.validate(cfg(pins=[{"type": "bundle", "id": "b", "who": "a", "when": "t", "why": "w"}])))
try:
    C.save(cfg(all=0)); ok = False
except ValueError:
    ok = True
chk("save() rejects invalid cfg and leaves the file alone", ok and C.load(cp)["all"] == 14)
os.environ["LOG_RETENTION_CONFIG"] = str(cp)
out = C.save(cfg(all=45, failures=120, pins=[{"type": "job", "id": hexid(1), "who": "owner", "when": "t", "why": "w"}]), who="tester")
chk("save() roundtrip + no temp files left", C.load(cp)["all"] == 45 and C.load(cp)["failures"] == 120 and
    not [x for x in os.listdir(cp.parent) if ".tmp" in x])
chk("save() writes an audit line", "config-saved by=tester" in (T / ".ollama-dispatch/log-rotate.log").read_text())
for i in range(3000):
    C.append_audit("x" * 200 + str(i))
chk("audit log is size-capped", (T / ".ollama-dispatch/log-rotate.log").stat().st_size <= 256 * 1024 + 400)
# preview
os.environ.pop("LOG_RETENTION_CONFIG")
P = world()
done(P, hexid(130), 40, "failed", fc="model"); qlog(P, hexid(130), 40); qlog(P, hexid(131), 40)
pv = C.preview(cfg(failures=90), paths=P)
chk("preview: per-level would-delete counts/bytes, nothing deleted",
    pv["levels"]["all"]["count"] == 1 and pv["levels"]["failures"]["count"] == 0 and exists(P["queue_logs"] / f"{hexid(131)}-job.log")
    and pv["total"]["count"] == 1)
pv2 = C.preview(cfg(failures=10), paths=P)
chk("preview reflects an unsaved cfg", pv2["levels"]["failures"]["count"] == 1 and pv2["levels"]["failures"]["bytes"] == 10)
chk("preview of an invalid cfg returns errors, deletes nothing", C.preview(cfg(all=0), paths=P).get("errors"))
chk("audit line emitted per real run", (lambda: (L.run(cfg(), apply=True, paths=P), "rotate mode=apply deleted=" in (T / ".ollama-dispatch/log-rotate.log").read_text())[1])())

print("ALL PASS" if not FAILS else "FAILED: " + "; ".join(FAILS))
shutil.rmtree(T, ignore_errors=True)
sys.exit(1 if FAILS else 0)
