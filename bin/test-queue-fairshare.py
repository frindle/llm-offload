#!/usr/bin/env python3
"""DECAYED FAIR-SHARE TIEBREAK (2026-10-13, idea borrowed from ollamaMQ; no code copied).

When the scheduler must pick which IDLE bundle to START next among otherwise-equal candidates, the
bundle whose source has been charged less recently goes first. Charge = 1 per dispatch (whatever the
outcome), halving every FAIRSHARE_HALF_LIFE_S (default 30 min). It is ONLY a tiebreak:
  * "otherwise equal" = same head tier AND same model AND same host_pref -- the bundles of a class
    only permute among THEIR OWN slots, so no other bundle moves and the model sequence (swap
    minimisation) is unchanged;
  * running / pinned / committed / sticky-focus / override bundles are never touched;
  * a bundle's own jobs stay contiguous and in order (no interleaving);
  * no history (or FAIRSHARE=0) => ordering byte-identical to before.
Drives the REAL pending_launch_order / fairshare_* from ollama-queue.py and, for the no-history
equivalence property, the PRE-CHANGE ordering function frozen in test-data/ via --orig.
Run: python3 test-queue-fairshare.py [--bin DIR] [--orig PATH]  ->  FAIRSHARE_OK"""
import argparse, copy, importlib.util as u, json, os, random, re, sys, tempfile
from pathlib import Path

os.environ["OLLAMA_QUEUE_NO_NOTIFY"] = "1"
ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=str(Path(__file__).resolve().parent))
ap.add_argument("--orig", default=None, help="the pre-change ollama-queue.py (no-history equivalence oracle)")
A = ap.parse_args()
BIN = Path(A.bin).resolve()
HOME = Path(tempfile.mkdtemp(prefix="fairshare-"))
os.environ["HOME"] = str(HOME)
os.environ.pop("FAIRSHARE", None)
os.environ.pop("FAIRSHARE_HALF_LIFE_S", None)


def load(path, name):
    sp = u.spec_from_file_location(name, path)
    m = u.module_from_spec(sp)
    sp.loader.exec_module(m)
    return m


q = load(BIN / "ollama-queue.py", "q_fair")
fails = []


def chk(n, c):
    print(("ok   " if c else "FAIL ") + n)
    if not c:
        fails.append(n)


NOW = 5_000_000.0
_n = [0]


def J(bundle, status="pending", label=None, model="m1", host="auto", **kw):
    _n[0] += 1
    d = {"id": f"j{_n[0]:03d}", "status": status, "bundle": bundle, "model": model, "host_pref": host,
         "label": label or f"auto-author-{bundle}-s{_n[0]}"}
    d.update(kw)
    return d


ids = lambda order: [j["id"] for j in order]
bun = lambda order: [j["bundle"] for j in order]
PLO = q.pending_launch_order

# ---- 1. charge / decay / buckets -------------------------------------------------------------------
chk("shipped defaults: half-life 30 min, kill switch on", q.FAIRSHARE_HALF_LIFE_S == 1800.0 and q.fairshare_enabled())
s = {}
q.fairshare_charge(s, "A", NOW)
chk("one dispatch charges 1.0", abs(q.fairshare_decayed(s["_fairshare"]["A"], NOW) - 1.0) < 1e-9)
chk("the charge halves after one half-life", abs(q.fairshare_decayed(s["_fairshare"]["A"], NOW + 1800) - 0.5) < 1e-9)
chk("and quarters after two", abs(q.fairshare_decayed(s["_fairshare"]["A"], NOW + 3600) - 0.25) < 1e-9)
q.fairshare_charge(s, "A", NOW + 1800)
chk("charges accumulate on top of the decayed balance (0.5 + 1)", abs(q.fairshare_decayed(s["_fairshare"]["A"], NOW + 1800) - 1.5) < 1e-9)
q.fairshare_charge(s, "B", NOW + 1800)
q.fairshare_charge(s, "C", NOW + 1800 + 40 * 1800)
chk("fully decayed entries are pruned on the next charge", "A" not in s["_fairshare"] and "B" not in s["_fairshare"])
os.environ["FAIRSHARE_HALF_LIFE_S"] = "60"
s2 = {}
q.fairshare_charge(s2, "A", NOW)
chk("half-life env override", abs(q.fairshare_decayed(s2["_fairshare"]["A"], NOW + 60) - 0.5) < 1e-9)
os.environ["FAIRSHARE_HALF_LIFE_S"] = "junk"
chk("garbage half-life env falls back to 1800", abs(q.fairshare_decayed({"c": 1.0, "t": NOW}, NOW + 1800) - 0.5) < 1e-9)
os.environ.pop("FAIRSHARE_HALF_LIFE_S")
for bad in (None, "", 0):
    t = {}
    q.fairshare_charge(t, bad, NOW)
    chk(f"charging key {bad!r} is a no-op", not t.get("_fairshare"))
chk("garbage ledger never raises", q.fairshare_buckets({"_fairshare": "x"}, NOW) == {}
    and q.fairshare_buckets({"_fairshare": {"A": 5}}, NOW) == {} and q.fairshare_buckets(None, NOW) == {})

st = {}
for _ in range(1):
    q.fairshare_charge(st, "A", NOW)
b = q.fairshare_buckets(st, NOW)
chk("a fresh charge lands in a positive bucket", b.get("A", 0) > 0)
chk("no charge -> no entry (empty means 'no history')", q.fairshare_buckets({}, NOW) == {})
chk("more recent charge => higher bucket than an older one",
    q.fairshare_buckets({"_fairshare": {"A": {"c": 1.0, "t": NOW}, "B": {"c": 1.0, "t": NOW - 3600}}}, NOW)["A"]
    > q.fairshare_buckets({"_fairshare": {"A": {"c": 1.0, "t": NOW}, "B": {"c": 1.0, "t": NOW - 3600}}}, NOW)["B"])
chk("an old charge decays to bucket 0 (== never charged)",
    q.fairshare_buckets({"_fairshare": {"A": {"c": 1.0, "t": NOW - 8 * 3600}}}, NOW) == {})

# kill switch
os.environ["FAIRSHARE"] = "0"
chk("FAIRSHARE=0 disables the buckets", q.fairshare_buckets(st, NOW) == {} and not q.fairshare_enabled())
k = {}
q.fairshare_charge(k, "A", NOW)
chk("FAIRSHARE=0 does not even write a ledger (state stays byte-identical)", k == {})
os.environ.pop("FAIRSHARE")

# protected bundles: never reordered by the tiebreak (mid-bundle jobs are never displaced)
led = {"A": {"c": 3.0, "t": NOW}, "B": {"c": 3.0, "t": NOW}, "C": {"c": 3.0, "t": NOW}, "D": {"c": 3.0, "t": NOW}, "E": {"c": 3.0, "t": NOW}}
base = {"_fairshare": led}
chk("committed bundle is excluded", "A" not in q.fairshare_buckets(dict(base, _bundle_commit={"key": "A"}), NOW))
chk("sticky-focus bundle is excluded", "B" not in q.fairshare_buckets(dict(base, _focus_wait={"key": "B"}), NOW))
chk("a live focus override bundle is excluded",
    "C" not in q.fairshare_buckets(dict(base, _focus_override={"key": "C", "set_at": NOW - 5}), NOW))
chk("the pinned group is excluded", "D" not in q.fairshare_buckets(dict(base, pinned_group="D"), NOW))
chk("the others keep their buckets", "E" in q.fairshare_buckets(dict(base, _bundle_commit={"key": "A"}, _focus_wait={"key": "B"},
                                                                   pinned_group="D"), NOW))

# ---- 2. ordering: tiebreak only ----------------------------------------------------------------------
a1, a2, b1, c1 = J("A"), J("A"), J("B"), J("C")
Q = [a1, b1, a2, c1]
chk("no history: A, B, C in first-appearance order (unchanged)", bun(PLO(Q)) == ["A", "A", "B", "C"])
chk("empty fair map is identical to omitting it", ids(PLO(Q, None, {})) == ids(PLO(Q)))
chk("fair=None identical", ids(PLO(Q, None, None)) == ids(PLO(Q)))
chk("equal buckets keep FIFO", ids(PLO(Q, None, {"A": 3, "B": 3, "C": 3})) == ids(PLO(Q)))
chk("the charged bundle yields to the uncharged equals (A charged, B/C not)", bun(PLO(Q, None, {"A": 3})) == ["B", "C", "A", "A"])
chk("ties among the uncharged keep FIFO; the middle charge sits in the middle",
    bun(PLO(Q, None, {"A": 3, "B": 1})) == ["C", "B", "A", "A"])
chk("the lowest charge wins among several", bun(PLO(Q, None, {"A": 4, "B": 3, "C": 1})) == ["C", "B", "A", "A"])
out = PLO(Q, None, {"A": 4})
chk("a bundle's own jobs stay contiguous and in queue order (no interleave)",
    [j["id"] for j in out if j["bundle"] == "A"] == [a1["id"], a2["id"]] and bun(out) == ["B", "C", "A", "A"])
# running bundle keeps the lanes whatever it owes
run = J("A", status="running")
chk("a RUNNING bundle's pending jobs still lead, however heavily charged",
    bun(PLO([run, b1, a2, c1], None, {"A": 99})) == ["A", "B", "C"])
chk("a PINNED bundle still leads, however heavily charged", bun(PLO(Q, "A", {"A": 99})) == ["A", "A", "B", "C"])
# different model / host / tier => NOT equal => never reordered (swap minimisation preserved)
bm = J("B", model="m2")
chk("different head model is never reordered (model-swap minimisation)", bun(PLO([a1, bm], None, {"A": 9})) == ["A", "B"])
bh = J("B", host="unraid")
chk("different host_pref is never reordered", bun(PLO([a1, bh], None, {"A": 9})) == ["A", "B"])
bt = J("B", label="gate-zzz")
chk("different tier (a gate/coding job vs authoring) is never reordered", bun(PLO([a1, bt], None, {"A": 9})) == ["A", "B"])
t_a, t_b = J("A", label="auto-author-A-s1"), J("B", label="rt-fix-coding-thing")
chk("different followup_tier heads are not a class",
    q.followup_tier(t_a) != q.followup_tier(t_b) and bun(PLO([t_a, t_b], None, {"A": 9})) == bun(PLO([t_a, t_b])))
# a non-equal bundle sitting between two equals does not move
mid = J("M", model="m2")
chk("a non-equal bundle between two equals keeps its slot",
    bun(PLO([a1, mid, b1], None, {"A": 9})) == ["B", "M", "A"])
# standalone (own-id) jobs are never penalised: a never-seen key has no bucket
s1, s2_ = J(None, label="plain-one"), J(None, label="plain-two")
for x in (s1, s2_):
    x.pop("bundle")
chk("standalone jobs (no bundle) keep FIFO", ids(PLO([s1, s2_], None, {"A": 9})) == [s1["id"], s2_["id"]])
chk("garbage fair map never raises and never reorders", ids(PLO(Q, None, "garbage")) == ids(PLO(Q))
    and ids(PLO(Q, None, {"A": "x"})) == ids(PLO(Q)))

# ---- 3. no-history equivalence vs the pre-change function (property) --------------------------------
def ref_order(jobs, pinned_group=None):
    """FROZEN copy of the pre-2026-10-13 pending_launch_order (the oracle for 'no history =>
    byte-identical'). Uses only the unchanged helpers."""
    pending = [j for j in (jobs or []) if j.get("status") == "pending"]
    jobs_by_id = {j.get("id"): j for j in (jobs or []) if j.get("id")}
    pk = lambda j: q._launch_plan_key(j, {}, jobs_by_id)
    running_plans = {pk(j) for j in (jobs or []) if j.get("status") == "running"}
    plan_rank = {}
    for j in pending:
        k = pk(j)
        if k not in plan_rank:
            plan_rank[k] = len(plan_rank)
    def key(j):
        k = pk(j)
        return (0 if (pinned_group is not None and k == pinned_group) else 1,
                0 if k in running_plans else 1, plan_rank.get(k, len(plan_rank)), q.followup_tier(j))
    return sorted(pending, key=key)


q.slice_group_index = lambda *a, **k: {}      # hermetic: no slice-plan files
rnd = random.Random(7)
bad = 0
for trial in range(400):
    jobs = []
    for i in range(rnd.randint(1, 14)):
        bk = rnd.choice(["A", "B", "C", "D", None])
        lab = rnd.choice(["auto-author-x-s1", "gate-abc", "plain-job", "auto-refine-y-r2", "draft-wt-z"])
        d = {"id": f"p{trial}-{i}", "status": rnd.choice(["pending", "pending", "pending", "running", "done"]),
             "label": lab, "model": rnd.choice(["m1", "m2"]), "host_pref": rnd.choice(["auto", "unraid"])}
        if bk:
            d["bundle"] = bk
        jobs.append(d)
    pin = rnd.choice([None, "A", "B"])
    old = ids(ref_order(copy.deepcopy(jobs), pin))
    for fair in (None, {}):
        if ids(PLO(copy.deepcopy(jobs), pin, fair)) != old:
            bad += 1
    if ids(PLO(copy.deepcopy(jobs), pin)) != old:
        bad += 1
    # every bundle charged identically == no information => still identical
    keys = {q._launch_plan_key(j, {}, {}) for j in jobs}
    if ids(PLO(copy.deepcopy(jobs), pin, {k: 2 for k in keys})) != old:
        bad += 1
chk(f"no-history ordering is IDENTICAL to the pre-change function on 400 random queues (bad={bad})", bad == 0)
if A.orig:     # optional: also the real pre-change file
    qo = load(A.orig, "q_orig")
    chk("and identical to the real pre-change module on a sample", all(
        ids(qo.pending_launch_order(copy.deepcopy(jobs), None)) == ids(PLO(copy.deepcopy(jobs), None)) for _ in range(1)))

# ---- 4. simulation: bundles run to completion, never interleave, even with the tiebreak on ----------
def simulate(n_bundles=4, per=3, use_fair=True):
    st = {"jobs": []}
    for b in range(n_bundles):
        key = f"B{b}"
        for i in range(per):
            st["jobs"].append(J(key, label=f"auto-author-{key}-s{i + 1}", status="pending"))
    # preload an unfair history: B0 was just heavily used
    if use_fair:
        for _ in range(5):
            q.fairshare_charge(st, "B0", NOW)
    started, t = [], NOW
    commit = None
    pk = lambda j: j.get("bundle")
    for _step in range(n_bundles * per):
        pend = [j for j in st["jobs"] if j["status"] == "pending"]
        if not pend:
            break
        # the engine: a committed bundle that still has work keeps the lanes (bundle-to-completion)
        sub = [j for j in pend if commit is None or pk(j) == commit] or pend
        stt = dict(st, _bundle_commit={"key": commit} if commit else None)
        fair = q.fairshare_buckets(stt, t) if use_fair else {}
        order = [j for j in PLO(st["jobs"], None, fair) if j in sub]
        j = order[0]
        j["status"] = "done"
        started.append(pk(j))
        if use_fair:
            q.fairshare_charge(st, pk(j), t)
        commit = pk(j) if any(x["status"] == "pending" and pk(x) == pk(j) for x in st["jobs"]) else None
        t += 60
    return started


seq = simulate()
runs = [k for i, k in enumerate(seq) if i == 0 or seq[i - 1] != k]
chk("sim: every bundle runs to completion before another starts (no interleave)", len(runs) == len(set(runs)) == 4)
chk("sim: the heavily-charged bundle B0 goes LAST among equals", runs[-1] == "B0" and runs[:3] == ["B1", "B2", "B3"])
seq0 = simulate(use_fair=False)
chk("sim: with no history the order is plain FIFO", seq0 == ["B0"] * 3 + ["B1"] * 3 + ["B2"] * 3 + ["B3"] * 3)

# mid-bundle: B1 started (committed) and then charged heavily -> still next, never displaced by uncharged B2
st = {"jobs": [J("B1", label="auto-author-B1-s2"), J("B2", label="auto-author-B2-s1")], "_bundle_commit": {"key": "B1"}}
for _ in range(9):
    q.fairshare_charge(st, "B1", NOW)
fair = q.fairshare_buckets(st, NOW)
chk("a mid-bundle (committed) job is never displaced, however charged", bun(PLO(st["jobs"], None, fair))[0] == "B1" and "B1" not in fair)

# ---- 5. wiring (source level) -----------------------------------------------------------------------
src = (BIN / "ollama-queue.py").read_text()
chk("the main launch order passes the fair map (3 daemon call sites)",
    len(re.findall(r"pending_launch_order\(state\[\"jobs\"\], state\.get\(\"pinned_group\"\), _fair\)", src)) == 3)
chk("a dispatch charges its bundle (after the job is marked running)", re.search(
    r'job\["launched_at"\] = datetime\.now\(timezone\.utc\)\.isoformat\(\)\n(?:.*\n){0,6}?\s+fairshare_charge\(state, _pk\(job\), time\.time\(\)\)', src) is not None)
chk("the tiebreak never touches active_bundle()'s own call", "order = pending_launch_order(jobs)\n" in src)

print("FAIRSHARE_OK" if not fails else f"{len(fails)} FAILED: {fails}")
sys.exit(1 if fails else 0)
