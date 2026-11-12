#!/usr/bin/env python3
"""Unsatisfiable-job WARNING (2026-10-13, idea borrowed from ollamaMQ's fail-fast visibility).

A PENDING job whose model NO online host/lane can serve at all (Darkbloom's own arithmetic says it
cannot load: bloom-control 'infeasible' record / cold-load gate; or the fit table already held it
'no host fits model footprint') that STAYS that way longer than QUEUE_UNSAT_WARN_S (default 600) is
named in the dashboard waiting banner as a WARNING. WARNING ONLY: nothing is failed, cancelled,
rerouted, parked or written to the queue state; a busy / loading / briefly-offline backend never
triggers it; nothing raises out of the banner path.

Checks the REAL functions (ollama-queue.py: unsat_reason / unsat_track / unsat_warnings_from_state)
and the page's own unsatBannerLines()/renderWaitBanner() under node, plus the API payload wiring.
Run: python3 test-queue-unsat-warn.py [--bin DIR]   ->  UNSAT_WARN_OK"""
import argparse, copy, importlib.util as u, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
BIN = Path(ap.parse_args().bin).resolve()
HOME = Path(tempfile.mkdtemp(prefix="unsat-warn-"))
os.environ["HOME"] = str(HOME)
os.environ.pop("QUEUE_UNSAT_WARN_S", None)
os.environ.pop("QUEUE_UNSAT_MAX_GAP_S", None)
sp = u.spec_from_file_location("q_unsat", BIN / "ollama-queue.py")
q = u.module_from_spec(sp)
sp.loader.exec_module(q)
fails = []


def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c:
        fails.append(n)


NOW = 3_000_000.0
WHY = "needs 30.3 GB, 26.8 GB usable (3.5 short)"
DB = "http://127.0.0.1:8080"


def job(jid="j1", **kw):
    d = {"id": jid, "label": "auto-author-x-s1", "status": "pending", "model": "qwen3.8:27b-q4_K_M",
         "host_pref": "auto", "task_kind": "coding"}
    d.update(kw)
    return d


infeasible = lambda m, now=None, state_path=None: WHY          # bloom-control says: cannot load
none = lambda *a, **k: None
R = lambda j, **kw: q.unsat_reason(j, bloom_note=kw.get("bloom", none), cold_gate=kw.get("cold", none),
                                   db_url=kw.get("url", lambda: DB), to_dbm=lambda m: "qwen3.6-35b")

# ---- 1. unsat_reason: only a truly unsatisfiable pending job has a reason ------------------------
r = R(job(), bloom=infeasible)
chk("bloom-control infeasible record -> reason naming the model and the memory arithmetic",
    r is not None and "qwen3.6-35b" in r and WHY in r)
chk("provider cold-load gate alone also counts", R(job(), cold=lambda m, *a, **k: WHY) is not None)
chk("no signal (a BUSY or LOADING/warming backend is not infeasible) -> None", R(job()) is None)
chk("host_pref darkbloom/studio resolve to the same lane", all(
    R(job(host_pref=p), bloom=infeasible) for p in ("darkbloom", "studio", "auto")))
chk("a running job is never unsatisfiable", R(job(status="running"), bloom=infeasible) is None)
chk("a done/failed job is never unsatisfiable", R(job(status="failed"), bloom=infeasible) is None)
chk("a human-held pending row is not flagged", R(job(user_hold=True), bloom=infeasible) is None)
chk("an Unraid-pinned job is not judged by Darkbloom's memory", R(job(host_pref="unraid"), bloom=infeasible) is None)
chk("with Darkbloom not configured there is no Darkbloom verdict", R(job(), bloom=infeasible, url=lambda: None) is None)
chk("gpu-exclusive row is skipped", R(job(job_kind=q.GPU_JOB_KIND, runner=q.GPU_JOB_RUNNER), bloom=infeasible) is None)
chk("an esc-review row is skipped", R(job(label="esc-review-abc"), bloom=infeasible) is None)
def boom(*a, **k):
    raise RuntimeError("boom")
chk("a raising signal never raises out (None)", R(job(), bloom=boom, cold=boom) is None)
chk("a raising db_url never raises out", q.unsat_reason(job(), bloom_note=infeasible, cold_gate=none, db_url=boom) is None)
fh = job(status="held", fit_hold=True, hold_reason="no host fits model footprint", model="huge:200b")
chk("fit_hold row (the fit router's 'fits no host') is unsatisfiable", "huge:200b" in (R(fh) or ""))
chk("a fit_hold row that a human ALSO held is not flagged", R(dict(fh, user_hold=True)) is None)
chk("a plain held (gate) row is not flagged", R(job(status="held", hold_reason="pending gate")) is None)
chk("garbage input -> None", R(None) is None and R("x") is None and R({}) is None)

# ---- 2. unsat_track: threshold + continuity -------------------------------------------------------
cur = {"j1": {"reason": WHY, "label": "L", "model": "m"}}
st, w = q.unsat_track({}, cur, NOW)
chk("first sighting records since=now and warns nothing", st["j1"]["since"] == NOW and w == [])
stm = st
for dt in range(100, 599, 100):
    stm, w = q.unsat_track(stm, cur, NOW + dt)
st2, w = q.unsat_track(stm, cur, NOW + 599)
chk("just under the 10 min default (polled throughout): still no warning", w == [] and st2["j1"]["since"] == NOW)
st3, w = q.unsat_track(st2, cur, NOW + 601)
chk("past 10 min: one warning with id/label/model/reason/age", len(w) == 1 and w[0]["id"] == "j1"
    and w[0]["label"] == "L" and w[0]["model"] == "m" and w[0]["reason"] == WHY and w[0]["age_s"] >= 600)
st4, w = q.unsat_track(st3, {}, NOW + 700)
chk("the condition clearing forgets the job entirely", "j1" not in st4 and w == [])
st5, w = q.unsat_track(st4, cur, NOW + 710)
chk("a recurrence starts a fresh clock (no instant warning)", st5["j1"]["since"] == NOW + 710 and w == [])
# observation gap > max gap restarts the clock (we cannot prove it stayed unsatisfiable)
stg = {"j1": {"since": NOW, "last": NOW + 10, "reason": WHY}}
stg2, w = q.unsat_track(stg, cur, NOW + 5000)  # default max gap is 300 s
chk("a long observation gap restarts the clock rather than inventing continuity", w == [] and stg2["j1"]["since"] == NOW + 5000)
stc = {"j1": {"since": NOW, "last": NOW + 590, "reason": "old reason"}}
stc2, w = q.unsat_track(stc, {"j1": {"reason": "new numbers 27.0 GB", "label": "L", "model": "m"}}, NOW + 650)
chk("a reason that merely changes numbers keeps the clock and shows the latest reason",
    len(w) == 1 and w[0]["reason"] == "new numbers 27.0 GB")
chk("custom threshold honoured", q.unsat_track(st, cur, NOW + 61, warn_s=60)[1] != [])
os.environ["QUEUE_UNSAT_WARN_S"] = "30"
chk("env QUEUE_UNSAT_WARN_S overrides the default", q.unsat_track(st, cur, NOW + 31)[1] != [])
os.environ["QUEUE_UNSAT_WARN_S"] = "garbage"
chk("a garbage env value falls back to 600", q.unsat_track(st, cur, NOW + 61)[1] == [])
os.environ.pop("QUEUE_UNSAT_WARN_S")
chk("the default threshold is 10 minutes", q.UNSAT_WARN_S == 600.0)
chk("garbage prev/current never raise", q.unsat_track("x", None, NOW) == ({}, []))
p0 = copy.deepcopy(st)
q.unsat_track(st, cur, NOW + 900)
chk("unsat_track does not mutate its input", st == p0)

# ---- 3. unsat_warnings_from_state: end to end, read-only, never raises ----------------------------
sdir = Path(tempfile.mkdtemp(dir=str(HOME)))
spath, seen = sdir / "queue-state.json", sdir / "unsat-seen.json"
bstate = sdir / "bloom-control-state.json"
os.environ["BLOOMCTL_STATE"] = str(bstate)
bstate.write_text(json.dumps({"infeasible": {"model": "qwen3.6-35b", "reason": WHY, "at": NOW}}))
q._darkbloom_url = lambda: DB
q._darkbloom_model = lambda m: "qwen3.6-35b"
q.darkbloom_cold_gate = lambda *a, **k: None
state = {"jobs": [job("a1"), job("b2", model="small:7b", status="running")]}
spath.write_text(json.dumps(state))
before = spath.read_bytes()
mt = spath.stat().st_mtime_ns
call = lambda t: q.unsat_warnings_from_state(now=t, state_path=spath, seen_path=seen)


def poll(t0, t1, step=60):
    """Poll like an open dashboard tab does; returns the last result."""
    out, t = [], t0
    while t <= t1:
        out = call(t)
        t += step
    return out


chk("t+0: tracked, nothing shown", call(NOW) == [])
chk("polled through t+9 min: nothing shown", poll(NOW + 60, NOW + 540) == [])
out = poll(NOW + 600, NOW + 610, 10)
chk("t+10m10s: exactly the unsatisfiable pending job is warned, with model + reason",
    len(out) == 1 and out[0]["id"] == "a1" and out[0]["model"] == "qwen3.8:27b-q4_K_M" and WHY in out[0]["reason"])
chk("the RUNNING job is not warned", all(w["id"] != "b2" for w in out))
chk("queue state file is byte-identical (warning only: no fail/cancel/reroute/park)", spath.read_bytes() == before)
chk("queue state file was not even rewritten", spath.stat().st_mtime_ns == mt)
chk("the job row carries no new field (state not touched)",
    json.loads(spath.read_text())["jobs"][0] == state["jobs"][0])
# restart persistence: the first-seen survives a fresh process via the seen file
q._UNSAT_MEM.clear()
chk("after an API restart (memory cleared) the persisted clock still yields the warning",
    len(poll(NOW + 620, NOW + 650, 10)) == 1)
# BUSY / LOADING / BRIEFLY-OFFLINE: no infeasible record -> never warns however long it waits
bstate.write_text(json.dumps({}))
chk("record cleared (model loaded / memory freed): warning goes away at once", call(NOW + 660) == [])
for dt in (700, 5000, 100000):
    chk(f"busy/loading backend (no infeasible record) never warns (+{dt}s)", poll(NOW + dt, NOW + dt + 900) == [])
# a brief infeasible blip (< threshold) that clears does not accumulate toward a later warning
T1 = NOW + 200000
bstate.write_text(json.dumps({"infeasible": {"model": "qwen3.6-35b", "reason": WHY, "at": T1}}))
poll(T1, T1 + 300)
bstate.write_text(json.dumps({}))
poll(T1 + 360, T1 + 420)
bstate.write_text(json.dumps({"infeasible": {"model": "qwen3.6-35b", "reason": WHY, "at": T1 + 480}}))
chk("a blip that cleared then recurred restarts the clock (no warning at 11 min after first blip)",
    poll(T1 + 480, T1 + 660) == [])
# an expired record (older than BLOOM_INFEASIBLE_TTL) is not a live signal either
bstate.write_text(json.dumps({"infeasible": {"model": "qwen3.6-35b", "reason": WHY, "at": NOW - 99999}}))
chk("a stale infeasible record is ignored", poll(NOW + 300000, NOW + 301000) == [])
# a missing / corrupt state file and a corrupt seen file degrade to []
spath.write_text("{not json")
chk("corrupt queue state -> [] (never raises)", call(NOW + 310000) == [])
spath.unlink()
chk("missing queue state -> []", call(NOW + 310001) == [])
spath.write_text(json.dumps(state))
seen.write_text("garbage")
q._UNSAT_MEM.clear()
bstate.write_text(json.dumps({"infeasible": {"model": "qwen3.6-35b", "reason": WHY, "at": NOW + 400000}}))
chk("corrupt seen file -> restarts cleanly", call(NOW + 400000) == [])
chk("calling with the default paths on a bare HOME never raises", isinstance(q.unsat_warnings_from_state(), list))

# ---- 4. API payload + page banner -----------------------------------------------------------------
shutil.rmtree(HOME / "bin", ignore_errors=True)
os.symlink(str(BIN), str(HOME / "bin"))
asp = u.spec_from_file_location("qapi_unsat", BIN / "ollama-queue-api.py")
api = u.module_from_spec(asp)
asp.loader.exec_module(api)
html = api.FRONTEND_HTML
W = [{"id": "a1", "label": "auto-author-x-s1", "model": "qwen3.8:27b-q4_K_M", "reason": WHY, "age_s": 1260.0, "since": 1.0}]
api.q.unsat_warnings_from_state = lambda now=None: W
pl = api._queue_wait_payload(now=NOW)
chk("API payload carries the unsat warnings", pl.get("unsat") == W)
def raising(now=None):
    raise RuntimeError("x")
api.q.unsat_warnings_from_state = raising
pl2 = api._queue_wait_payload(now=NOW)
chk("a failing unsat probe never breaks the payload (banner still works)", isinstance(pl2, dict) and pl2.get("unsat") == [] and "lanes" in pl2)
del api.q.unsat_warnings_from_state
pl3 = api._queue_wait_payload(now=NOW)
chk("an older queue module without the probe degrades to []", isinstance(pl3, dict) and pl3.get("unsat") == [])


def grab(name):
    m = re.search(r"^(?:async )?function " + name + r"\(.*?\n}\n", html, re.S | re.M)
    return m.group(0) if m else None


def closure(roots):
    """Roots plus every top-level page function they (transitively) call, so a new helper the banner
    path starts using (renderLaneChips, laneChipReason, fmtDur ...) is picked up without editing this test."""
    allfn = {m.group(1) for m in re.finditer(r"^(?:async )?function (\w+)\(", html, re.M)}
    seen, order, todo = set(), [], list(roots)
    while todo:
        n = todo.pop(0)
        if n in seen:
            continue
        seen.add(n)
        body = grab(n)
        if not body:
            continue
        order.append(body)
        todo += [f for f in allfn if f not in seen and re.search(r"\b" + f + r"\(", body)]
    return order


fns = closure(("escapeHtml", "unsatBannerLines", "renderWaitBanner"))
chk("page has unsatBannerLines + renderWaitBanner", all(grab(n) for n in ("unsatBannerLines", "renderWaitBanner")))
if all(fns):
    fake = ("const els={waitBanner:{hidden:true,innerHTML:'',classList:{_s:new Set(),remove(c){this._s.delete(c)},"
            "add(c){this._s.add(c)},toggle(c,on){on?this._s.add(c):this._s.delete(c)}}}};\n"
            "els.laneChips={hidden:true,innerHTML:''};\n"
            "globalThis.document={getElementById:id=>els[id]};\n")
    js = (fake + "\n".join(fns) + "\n"
          "const W=" + json.dumps(W) + ";\n"
          "const idle={source:'state',now:1000,lanes:{'studio-db':{state:'idle',pending:1,idle_since:900,reason:{sentence:'x',since:900}}},stuck:[]};\n"
          "const out={};\n"
          "out.lines=unsatBannerLines({unsat:W});\n"
          "out.none=[unsatBannerLines({}),unsatBannerLines(null),unsatBannerLines({unsat:'zzz'}),unsatBannerLines({unsat:[null,5,{}]})];\n"
          "renderWaitBanner({source:'state',now:1000,lanes:{},stuck:[],unsat:W},[],0);\n"
          "out.only={hidden:els.waitBanner.hidden,html:els.waitBanner.innerHTML,stuck:els.waitBanner.classList._s.has('stuck')};\n"
          "renderWaitBanner({source:'state',now:1000,lanes:{},stuck:[]},[],0);\n"
          "out.clean={hidden:els.waitBanner.hidden};\n"
          "renderWaitBanner(Object.assign({},idle,{unsat:W}),[],0);\n"
          "out.both={html:els.waitBanner.innerHTML,chips:els.laneChips.innerHTML};\n"
          "let threw=false;try{renderWaitBanner({source:'state',now:1,lanes:{},stuck:[],unsat:{a:1}},[],0);renderWaitBanner({source:'state',lanes:null,unsat:[{label:{}}]},[],0);}catch(e){threw=true}\n"
          "out.threw=threw;\n"
          "renderWaitBanner(null,[],0);out.nullHidden=els.waitBanner.hidden;\n"
          "console.log(JSON.stringify(out));")
    r = subprocess.run(["node", "-e", js], capture_output=True, text=True)
    chk("node ran the page functions", r.returncode == 0)
    if r.returncode == 0:
        o = json.loads(r.stdout)
        ln = " ".join(o["lines"])
        chk("a warning line names WARNING, the job label, the model and the reason",
            "WARNING" in ln and "auto-author-x-s1" in ln and "qwen3.8:27b-q4_K_M" in ln and WHY in ln)
        chk("the line says how long it has been unsatisfiable", "21m" in ln)
        chk("malformed/absent unsat -> no lines, no throw", o["none"] == [[], [], [], []])
        chk("unsat-only state: banner shown with the warning (not stuck/red)",
            o["only"]["hidden"] is False and "WARNING" in o["only"]["html"] and o["only"]["stuck"] is False)
        chk("no unsat and no idle lane: banner stays hidden", o["clean"]["hidden"] is True)
        # the idle lane moved from a banner sentence into the lane-summary chips (dashboard fe6018e); the warning stays in the banner
        chk("idle-lane (chip) AND warning (banner) coexist", "studio-db" in o["both"]["chips"] and "WARNING" in o["both"]["html"])
        chk("hostile unsat payloads never throw out of the banner", o["threw"] is False)
        chk("no state still hides the banner", o["nullHidden"] is True)
chk("the banner injects model/reason through escapeHtml (no raw interpolation)",
    "escapeHtml(" in (grab("unsatBannerLines") or ""))

# ---- 5. wiring: nothing in the daemon's mutate path references the warning ------------------------
src = (BIN / "ollama-queue.py").read_text()
body = re.search(r"def unsat_warnings_from_state\(.*?\n\n\n", src, re.S)
chk("the probe never takes the state lock or saves state", body is not None
    and "_Locked" not in body.group(0) and ".save(" not in body.group(0) and "STATE_PATH.write" not in body.group(0))
chk("the daemon loop does not call the warning (API-side, read-only)",
    len(re.findall(r"unsat_warnings_from_state\(", src)) == 1)

print("UNSAT_WARN_OK" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
