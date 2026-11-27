"""Tests for darkbloom-catalog-watch.py. Every outside call is a stub chosen by env override (the way the demand
and apply tests do it): catalog/list = `cat fixture`, download and bloomctl = tiny scripts that log their calls
and edit the fixtures. The real script runs as a subprocess. Run: python3 test-darkbloom-catalog-watch.py"""
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

SCRIPT = str(Path(__file__).resolve().parent / "darkbloom-catalog-watch.py")
PY = sys.executable

DOWNLOAD_STUB = """
import json, sys
d, mid = sys.argv[1], sys.argv[2]
open(d + '/downloads.log', 'a').write(mid + '\\n')
if mid in json.load(open(d + '/fail_download.json')):
    sys.stderr.write('network down'); sys.exit(1)
loc = json.load(open(d + '/local.json'))
loc['models'].append({'id': mid, 'size_bytes': 5_000_000_000})
json.dump(loc, open(d + '/local.json', 'w'))
"""

BLOOMCTL_STUB = """
import json, sys
d = sys.argv[1]
args = [a for a in sys.argv[2:] if a != '--json']
plan = json.load(open(d + '/plan.json'))
avail = json.load(open(d + '/available.json'))
if args == ['status']:
    print(json.dumps({'plan': {'models': plan}}))
elif args == ['model', 'list']:
    print(json.dumps({'models': [{'id': i, 'available': a} for i, a in avail.items()]}))
elif args[:2] == ['model', 'add']:
    mid = args[2]
    open(d + '/adds.log', 'a').write(mid + '\\n')
    if mid in json.load(open(d + '/fail_add.json')):
        sys.stderr.write('refused'); sys.exit(4)
    if mid not in plan:
        plan.append(mid)
    json.dump(plan, open(d + '/plan.json', 'w'))
    print(json.dumps({'changed': True, 'models': plan}))
else:
    sys.exit(2)
"""


def cat_row(mid, gb=5.0, ram=24, active=True):
    return {"id": mid, "active": active, "size_gb": gb, "total_size_bytes": int(gb * 1e9), "min_ram_gb": ram,
            "hugging_face_artifact": {"repo_id": "org/" + mid}}


class Rig:
    def __init__(self, catalog=("A", "B"), local=("A",), plan=("B",), free_gb=500):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        d = self.d
        Path(d, "dl.py").write_text(DOWNLOAD_STUB)
        Path(d, "bc.py").write_text(BLOOMCTL_STUB)
        self.put("catalog.json", [cat_row(m) for m in catalog])
        self.put("local.json", {"cacheDirectory": d, "models": [{"id": m, "size_bytes": 5_000_000_000} for m in local]})
        self.put("plan.json", list(plan))
        self.put("available.json", {m: True for m in set(catalog) | set(local) | set(plan)})
        self.put("fail_download.json", [])
        self.put("fail_add.json", [])
        self.put("queue.json", {"jobs": []})
        self.env = dict(os.environ)
        self.env.update({
            "DBWATCH_MANIFEST": d + "/manifest.json", "DBWATCH_LOCK": d + "/lock",
            "DBWATCH_CATALOG_CMD": "cat %s/catalog.json" % d, "DBWATCH_LIST_CMD": "cat %s/local.json" % d,
            "DBWATCH_DOWNLOAD_CMD": "%s %s/dl.py %s" % (PY, d, d), "DBWATCH_BLOOMCTL": "%s %s/bc.py %s" % (PY, d, d),
            "DBWATCH_FREE_GB": str(free_gb), "DBWATCH_LEASE_FILE": d + "/lease.json", "DBWATCH_QUEUE_STATE": d + "/queue.json",
            "DBWATCH_NOTIFY_LOG": d + "/alerts.log", "DBWATCH_TOML": d + "/provider.toml", "DBWATCH_NOW": "1000000",
            "DBWATCH_CACHE_DIR": d,
        })
        Path(d, "provider.toml").write_text("enabled_models = ['B']\n")

    def put(self, name, data):
        Path(self.d, name).write_text(json.dumps(data))

    def get(self, name):
        return json.loads(Path(self.d, name).read_text())

    def log(self, name):
        p = Path(self.d, name)
        return p.read_text().split() if p.exists() else []

    def alerts(self):
        p = Path(self.d, "alerts.log")
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def manifest(self):
        return self.get("manifest.json")

    def run(self, *flags, now=None):
        env = dict(self.env)
        if now is not None:
            env["DBWATCH_NOW"] = str(now)
        p = subprocess.run([PY, SCRIPT] + list(flags), capture_output=True, text=True, env=env, timeout=60)
        return p.returncode, p.stdout + p.stderr

    def apply(self, now=None):
        return self.run("--apply", now=now)


def seeded(rig, **kw):
    rc, out = rig.apply(now=1000000)
    assert rc == 0, out
    return rig


def test_first_run_seeds_and_acts_on_nothing():
    r = Rig(catalog=("A", "B", "N"), local=("A",), plan=("B",))   # N is in the catalog on the first run: not new
    rc, out = r.apply()
    assert rc == 0 and "FIRST RUN" in out, out
    m = r.manifest()
    assert set(m["models"]) == {"A", "B", "N"} and m["plan_last"] == ["B"]
    assert r.log("downloads.log") == [] and r.log("adds.log") == [] and r.alerts() == []


def test_dry_run_is_default_writes_nothing_and_projects_the_next_run():
    r = Rig()
    rc, out = r.run()
    assert rc == 0 and "DRY-RUN" in out and "would seed" in out, out
    assert "A [enable]" in out and "would enable" in out, out
    assert not Path(r.d, "manifest.json").exists()
    assert r.log("adds.log") == [] and r.alerts() == []


def test_already_downloaded_model_is_enabled_on_the_second_run():
    r = seeded(Rig())                              # A on disk, not in plan
    assert r.log("adds.log") == []
    rc, out = r.apply(now=1021600)
    assert "A [enable]" in out and "enabled" in out, out
    assert r.log("adds.log") == ["A"] and r.log("downloads.log") == []
    assert "A" in r.get("plan.json") and r.manifest()["models"]["A"]["enabled_by_watcher"]
    assert [a["key"] for a in r.alerts()] == ["darkbloom-catalog-added-A"]
    assert r.manifest()["plan_last"] == ["B", "A"]  # our own change is not later mistaken for an owner edit


def test_new_model_needs_two_consecutive_runs_then_downloads_and_enables():
    r = seeded(Rig())
    r.apply(now=1021600)                           # A enabled
    r.put("catalog.json", [cat_row(m) for m in ("A", "B", "N")])
    r.put("available.json", {"A": True, "B": True, "N": True})
    rc, out = r.apply(now=1043200)                 # N seen once: nothing
    assert r.log("downloads.log") == [] and r.manifest()["candidates"]["N"]["count"] == 1, out
    rc, out = r.apply(now=1064800)                 # seen twice: act
    assert r.log("downloads.log") == ["N"] and r.log("adds.log") == ["A", "N"], out
    assert "N" in r.get("plan.json")
    keys = [a["key"] for a in r.alerts()]
    assert keys == ["darkbloom-catalog-added-A", "darkbloom-catalog-new-N", "darkbloom-catalog-added-N"], keys
    rc, out = r.apply(now=1086400)                 # steady state: no repeat notices
    assert [a["key"] for a in r.alerts()] == keys and r.log("downloads.log") == ["N"]


def test_one_run_blip_does_nothing():
    r = seeded(Rig())
    r.put("catalog.json", [cat_row(m) for m in ("A", "B", "N")])
    r.apply(now=1021600)
    assert r.manifest()["candidates"]["N"]["count"] == 1
    r.put("catalog.json", [cat_row(m) for m in ("A", "B")])      # gone again
    r.apply(now=1043200)
    assert "N" not in r.manifest()["candidates"] and "N" not in r.manifest()["models"]
    assert r.log("downloads.log") == [] and "N" not in r.get("plan.json")
    r.put("catalog.json", [cat_row(m) for m in ("A", "B", "N")])  # back once: count restarts at 1
    r.apply(now=1064800)
    assert r.log("downloads.log") == [] and r.manifest()["candidates"]["N"]["count"] == 1


def test_empty_or_erroring_catalog_never_overwrites_the_manifest():
    r = seeded(Rig())
    before = Path(r.d, "manifest.json").read_text()
    r.put("catalog.json", [])
    rc, out = r.apply(now=1021600)
    assert rc == 3 and "ABORT" in out and "empty" in out, out
    r.env["DBWATCH_CATALOG_CMD"] = "false"
    rc, out = r.apply(now=1043200)
    assert rc == 3 and "ABORT" in out, out
    r.env["DBWATCH_CATALOG_CMD"] = "echo not-json"
    rc, out = r.apply(now=1064800)
    assert rc == 3, out
    assert Path(r.d, "manifest.json").read_text() == before
    assert r.log("adds.log") == [] and r.alerts() == []


def test_unreadable_bloomgauge_aborts_without_writing():
    r = seeded(Rig())
    before = Path(r.d, "manifest.json").read_text()
    r.env["DBWATCH_BLOOMCTL"] = "false"
    rc, out = r.apply(now=1021600)
    assert rc == 3 and "ABORT" in out and Path(r.d, "manifest.json").read_text() == before, out


def test_disk_too_small_blocks_and_retries_only_daily():
    r = seeded(Rig(free_gb=50))
    r.apply(now=1021600)                            # A is on disk: enable needs no disk
    r.put("catalog.json", [cat_row(m) for m in ("A", "B")] + [cat_row("N", gb=10)])
    r.put("available.json", {"A": True, "B": True, "N": True})
    r.apply(now=1043200)
    rc, out = r.apply(now=1064800)                  # need 10 * 1.3 + 50 = 63 GB, have 50
    assert "disk too small" in out and r.log("downloads.log") == [], out
    b = r.manifest()["models"]["N"]["blocked"]
    assert b["tries"] == 1 and "disk too small" in b["error"]
    assert r.alerts()[-1]["key"] == "darkbloom-catalog-failed-N"
    n_alerts = len(r.alerts())
    r.apply(now=1064800 + 6 * 3600)                 # inside the 24 h retry window: not tried again
    assert r.manifest()["models"]["N"]["blocked"]["tries"] == 1 and len(r.alerts()) == n_alerts
    r.env["DBWATCH_FREE_GB"] = "500"                # space freed; a day later it goes through
    r.apply(now=1064800 + 25 * 3600)
    assert r.log("downloads.log") == ["N"] and "N" in r.get("plan.json")
    assert "blocked" not in r.manifest()["models"]["N"]


def test_disk_floor_constant_is_weights_x_1_3_plus_50():
    r = seeded(Rig(free_gb=62.9))
    r.apply(now=1021600)
    r.put("catalog.json", [cat_row(m) for m in ("A", "B")] + [cat_row("N", gb=10)])
    r.put("available.json", {"A": True, "B": True, "N": True})
    r.apply(now=1043200)
    r.apply(now=1064800)
    assert r.log("downloads.log") == []             # 62.9 < 63.0
    r.env["DBWATCH_FREE_GB"] = "63.1"
    r.apply(now=1064800 + 25 * 3600)
    assert r.log("downloads.log") == ["N"]


def test_held_lease_or_running_job_defers_everything():
    r = seeded(Rig())
    for state in ("HELD", "DRAIN", "HANDBACK"):
        r.put("lease.json", {"state": state})
        rc, out = r.apply(now=1021600)
        assert "deferred (lease %s)" % state in out, out
        assert r.log("adds.log") == [] and r.log("downloads.log") == [] and r.alerts() == []
    Path(r.d, "lease.json").write_text("{corrupt")  # a corrupt lease counts as DRAIN
    rc, out = r.apply(now=1021600)
    assert "deferred" in out and r.log("adds.log") == []
    Path(r.d, "lease.json").unlink()
    r.put("queue.json", {"jobs": [{"status": "running"}]})
    rc, out = r.apply(now=1021600)
    assert "1 queue job(s) running" in out and r.log("adds.log") == []
    r.put("queue.json", {"jobs": [{"status": "pending"}]})
    rc, out = r.apply(now=1021600)
    assert r.log("adds.log") == ["A"], out


def test_owner_removed_tombstone_then_manual_readd_clears_it():
    r = seeded(Rig())
    r.apply(now=1021600)                            # A enabled by the watcher
    r.put("plan.json", ["B"])                       # the owner removes A in BloomGauge
    rc, out = r.apply(now=1043200)
    assert "owner_removed" in out and r.manifest()["models"]["A"]["owner_removed"] == 1043200, out
    assert r.log("adds.log") == ["A"]               # not re-added
    for i in range(3):
        r.apply(now=1064800 + i * 21600)
    assert r.log("adds.log") == ["A"] and "A" not in r.get("plan.json")
    r.put("plan.json", ["B", "A"])                  # the owner adds it back by hand
    rc, out = r.apply(now=1200000)
    assert "owner_removed" not in r.manifest()["models"]["A"], out
    r.put("plan.json", ["B"])                       # removing it again tombstones it again
    r.apply(now=1221600)
    assert r.manifest()["models"]["A"]["owner_removed"] == 1221600


def test_removal_before_the_watcher_ever_enabled_it_is_also_a_tombstone():
    r = seeded(Rig(plan=("B", "A")))                # A on disk AND in the plan at seed time
    r.put("plan.json", ["B"])                       # owner takes it out
    r.apply(now=1021600)
    assert r.manifest()["models"]["A"]["owner_removed"]
    r.apply(now=1043200)
    assert r.log("adds.log") == []


def test_failed_add_is_blocked_alerted_and_retried_daily():
    r = seeded(Rig())
    r.put("fail_add.json", ["A"])
    rc, out = r.apply(now=1021600)
    b = r.manifest()["models"]["A"]["blocked"]
    assert "blocked: bloomctl model add exit 4" in out and b["tries"] == 1, out
    assert [a["key"] for a in r.alerts()] == ["darkbloom-catalog-failed-A"]
    r.apply(now=1021600 + 3600)                     # too soon
    assert r.log("adds.log") == ["A"]
    r.apply(now=1021600 + 25 * 3600)                # a day later: tried again, fails again
    assert r.log("adds.log") == ["A", "A"] and r.manifest()["models"]["A"]["blocked"]["tries"] == 2
    r.put("fail_add.json", [])
    r.apply(now=1021600 + 50 * 3600)
    assert "A" in r.get("plan.json") and "blocked" not in r.manifest()["models"]["A"]
    assert r.alerts()[-1]["key"] == "darkbloom-catalog-added-A"


def test_failed_download_is_blocked_and_never_enables():
    r = seeded(Rig())
    r.apply(now=1021600)
    r.put("catalog.json", [cat_row(m) for m in ("A", "B", "N")])
    r.put("available.json", {"A": True, "B": True, "N": True})
    r.put("fail_download.json", ["N"])
    r.apply(now=1043200)
    rc, out = r.apply(now=1064800)
    assert "download failed" in out and r.log("adds.log") == ["A"] and "N" not in r.get("plan.json")
    assert r.manifest()["models"]["N"]["blocked"]["tries"] == 1


def test_model_bloomgauge_does_not_list_yet_is_deferred_not_failed():
    r = seeded(Rig())
    r.put("available.json", {"A": False, "B": True})
    rc, out = r.apply(now=1021600)
    assert "deferred (BloomGauge does not list it" in out and r.log("adds.log") == []
    assert "blocked" not in r.manifest()["models"]["A"] and r.alerts() == []


def test_dry_run_with_a_seeded_manifest_acts_on_nothing():
    r = seeded(Rig())
    before = Path(r.d, "manifest.json").read_text()
    rc, out = r.run(now=1021600)
    assert "would enable" in out
    assert Path(r.d, "manifest.json").read_text() == before
    assert r.log("adds.log") == [] and r.log("downloads.log") == [] and r.alerts() == []


def test_lock_is_exclusive():
    import fcntl
    r = Rig()
    fh = open(r.d + "/lock", "a+")
    fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    rc, out = r.apply()
    assert rc == 0 and "holds the lock" in out and not Path(r.d, "manifest.json").exists()


def test_never_starts_or_restarts_anything():
    r = Rig()
    r.env["DBWATCH_DOWNLOAD_CMD"] = "%s %s/dl.py %s" % (PY, r.d, r.d)
    seeded(r)
    src = Path(SCRIPT).read_text()
    for banned in ('"start"', '"switch"', '"restart"', '"stop"'):
        assert banned not in src.replace('# ', ''), banned


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    passed = failed = 0
    for t in tests:
        try:
            t()
            print("  PASS:", t.__name__)
            passed += 1
        except Exception as e:
            import traceback
            print("  FAIL:", t.__name__, "-", repr(e))
            traceback.print_exc()
            failed += 1
    print("\n%d/%d passed" % (passed, passed + failed))
    sys.exit(1 if failed else 0)
