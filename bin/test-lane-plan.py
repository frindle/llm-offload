#!/usr/bin/env python3
"""Tests for lane_plan.py (pipeline-hardening design section 1). Sandboxed: no real ~/.darkbloom,
no network, no model calls, no queue.

  python3 test-lane-plan.py              run the tests
  python3 test-lane-plan.py --mutants    every mutant of lane_plan.py must make the tests FAIL
  python3 test-lane-plan.py --revert-check   same as --mutants (this repo's revert-proof convention
                                          is a mutants list; baseline must pass first)

LANE_PLAN_SRC=<path> points the tests at a mutated copy (used by --mutants)."""
import importlib.util
import io
import json
import os
import pickle
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = os.environ.get("LANE_PLAN_SRC") or str(HERE / "lane_plan.py")
os.environ.pop("LANE_PLAN", None)
os.environ.pop("DARKBLOOM_STICKY_S", None)
spec = importlib.util.spec_from_file_location("lane_plan_under_test", SRC)
L = importlib.util.module_from_spec(spec)
sys.modules["lane_plan_under_test"] = L
spec.loader.exec_module(L)

FAILS = []
KEY1 = "SENTINEL-KEY-ONE-aaaaaaaaaaaaaaaaaaaaaaaa"
KEY2 = "SENTINEL-KEY-TWO-bbbbbbbbbbbbbbbbbbbbbbbb"
NO_DISABLE = Path("/nonexistent/lane-plan.disabled")
L.DISABLE_FILE = NO_DISABLE          # never let a real disable file in ~ affect the tests


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
        print(f"FAIL {name} {detail}")
    else:
        print(f"ok   {name}")


def raises(fn, exc=Exception):
    try:
        fn()
    except exc:
        return True
    except BaseException:       # noqa: BLE001
        return False
    return False


class Box:
    """A sandbox ~/.darkbloom + provider.toml + hosts.json."""

    def __init__(self):
        self.root = Path(tempfile.mkdtemp(prefix="lanep-"))
        self.src = L.Sources.under(self.root)

    def local(self, key=KEY1, pid=100, base="http://127.0.0.1:8000/v1", version="0.9.20", **extra):
        rec = {"api_key": key, "pid": pid, "port": 8000, "version": version,
               "updated_at": "2026-10-13T00:00:00Z"}
        if base is not None:
            rec["base_url"] = base
        rec.update(extra)
        self.src.local_json.write_text(json.dumps(rec))

    def daemon(self, pid=100, version="0.9.20", advertised=("Qwen3.5-9B",)):
        self.src.daemon_state.write_text(json.dumps({
            "schema": 1, "version": version, "pid": pid,
            "process_identity": {"pid": pid, "start_time_micros": 1791934508807052},
            "advertised_models": list(advertised)}))

    def served(self, ids=("Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8")):
        arr = ", ".join(f"'{i}'" for i in ids)
        self.src.provider_toml.write_text(f"[backend]\nenabled_models = [ {arr} ]\npreload_models = [ {arr} ]\n")
        self.src.loaded_models.write_text(json.dumps({"models": list(ids), "schema": 1}))

    def hosts(self):
        self.src.hosts_json.write_text(json.dumps({"hosts": {"unraid": {"url": "http://198.51.100.145:11434/", "usable_bytes": 5}}}))

    def pidfile(self, pid=100):
        self.src.provider_pid.write_text(str(pid))

    def full(self, **kw):
        self.local(**{k: v for k, v in kw.items() if k in ("key", "pid", "base", "version")})
        self.daemon(pid=kw.get("pid", 100))
        self.served()
        self.hosts()
        self.pidfile(kw.get("pid", 100))
        return self

    def wipe(self, p):
        Path(p).write_text("")

    def clean(self):
        shutil.rmtree(self.root, ignore_errors=True)


def stub_profile(model, role, lane, api, src):
    """Deterministic stand-in for model_profile (alias qwen3.8:* -> the 35B like the real yaml)."""
    resolved = model
    alias = None
    if api == "openai" and model.startswith("qwen3.8:"):
        resolved, alias = "qwen3.6-35b-a3b-vl-mtp-mxfp8", f"profile:{model}->qwen3.6-35b-a3b-vl-mtp-mxfp8"
    return resolved, alias, {"key": "p", "mode": "thinking_coding", "role": role,
                             "yaml_sha": "y1", "fields_sha": "f1"}, 65536


def plan_for(snap, model="qwen3.6-35b-a3b-vl-mtp-mxfp8", job_id="J", att="A", lane="http://127.0.0.1:8000", **kw):
    return L.resolve({"id": job_id, "attempt_id": att, "model": model}, lane, snap,
                     profile_fn=stub_profile, **kw)


# ============================================================================================
def t_snapshot_basic():
    b = Box().full()
    s = L.take_snapshot(None, now=1000.0, sources=b.src, sticky_s=60)
    d = s.darkbloom
    check("snap.up", d.state == "up" and d.base == "http://127.0.0.1:8000", repr(s))
    check("snap.gen", d.gen["pid"] == 100 and d.gen["version"] == "0.9.20" and d.gen_verified)
    check("snap.fp", d.key_fp == L.key_fp(KEY1) and d.key_fp.startswith("sha256:") and len(d.key_fp) == 15)
    check("snap.served", d.served_exact == ("Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8")
          and "qwen3.5-9b" in d.served_lower)
    check("snap.hosts", s.ollama_hosts["unraid"]["url"] == "http://198.51.100.145:11434")
    check("snap.provider_alive", d.provider_pid == 100 and d.provider_alive in (True, False, None))
    sha1 = s.sources_sha
    b.local(key=KEY2)
    check("snap.sources_sha_changes", L.take_snapshot(None, 1.0, b.src).sources_sha != sha1)
    b.clean()


def t_base_variants():
    b = Box().full(base=None)                 # 0.9.17+: no base_url, api_key only
    s = L.take_snapshot(None, 1.0, b.src)
    check("base.api_key_only_is_up", s.darkbloom.state == "up" and s.darkbloom.base == L.DEFAULT_BASE)
    b.local(base="http://127.0.0.1:8000")
    check("base.no_v1_suffix", L.take_snapshot(None, 1.0, b.src).darkbloom.base == "http://127.0.0.1:8000")
    b.src.local_json.write_text(json.dumps({"port": 8000, "pid": 100}))      # parsed, no key/base
    s = L.take_snapshot(None, 1.0, b.src)
    check("base.no_key_no_base_is_off", s.darkbloom.state == "off", repr(s))
    # parity with darkbloom_chat.base_from_record
    try:
        sys.path.insert(0, str(HERE))
        import darkbloom_chat as dbc
        recs = [{}, {"api_key": "k"}, {"base_url": "http://h:1/v1"}, {"base_url": "http://h:1/", "api_key": "k"},
                {"base_url": "ftp://x"}, {"api_key": ""}, "str", None]
        same = all(L.base_from_record(r) == dbc.base_from_record(r, L.DEFAULT_BASE) for r in recs)
        check("base.parity_with_darkbloom_chat", same)
    except ImportError:
        check("base.parity_with_darkbloom_chat", True, "skipped: darkbloom_chat not importable")
    b.clean()


def t_empty_torn_missing():
    b = Box().full()
    good = L.take_snapshot(None, 1000.0, b.src, sticky_s=60)
    b.wipe(b.src.local_json)
    s = L.take_snapshot(good, 1010.0, b.src, sticky_s=60)
    d = s.darkbloom
    check("torn.empty_sticky_whole_prev", d.state == "up" and d.base == good.darkbloom.base
          and d.key_fp == good.darkbloom.key_fp and 9 < d.stale_s < 11, repr(s))
    b.src.local_json.write_text('{"api_key": "SENTINEL-KEY-ONE-aa')            # truncated mid-write
    s = L.take_snapshot(good, 1020.0, b.src, sticky_s=60)
    check("torn.truncated_sticky", s.darkbloom.state == "up" and s.darkbloom.stale_s > 0)
    b.src.local_json.write_text("[1,2]")
    check("torn.non_object_sticky", L.take_snapshot(good, 1030.0, b.src, sticky_s=60).darkbloom.state == "up")
    # window is measured from the last GOOD read, never extended by sticky answers
    s2 = L.take_snapshot(L.take_snapshot(good, 1030.0, b.src, sticky_s=60), 1070.0, b.src, sticky_s=60)
    check("torn.window_not_extended", s2.darkbloom.state == "restarting" and s2.darkbloom.base is None, repr(s2))
    check("torn.no_prev_is_restarting", L.take_snapshot(None, 5.0, b.src).darkbloom.state == "restarting")
    check("torn.kill_switch_sticky0", L.take_snapshot(good, 1001.0, b.src, sticky_s=0).darkbloom.state == "restarting")
    # MISSING is "off" and never masked, even a second after a good read
    b.src.local_json.unlink()
    s = L.take_snapshot(good, 1001.0, b.src, sticky_s=60)
    check("torn.missing_is_off_never_masked", s.darkbloom.state == "off" and s.darkbloom.base is None, repr(s))
    # unreadable (directory in place of file) never raises
    b.src.local_json.mkdir()
    s = L.take_snapshot(good, 1001.0, b.src, sticky_s=60)
    check("torn.unreadable_unknown_no_raise", s.darkbloom.state in ("unknown", "off", "restarting"), repr(s))
    b.clean()
    # garbage in every file never raises
    b = Box()
    for p in b.src.paths():
        Path(p).write_bytes(b"\xff\xfe\x00garbage{{{")
    try:
        L.take_snapshot(None, 1.0, b.src)
        ok = True
    except Exception as e:      # noqa: BLE001
        ok = False
        print(e)
    check("torn.garbage_everywhere_no_raise", ok)
    b.clean()


def t_pid_mismatch():
    b = Box().full(pid=100)
    good = L.take_snapshot(None, 1000.0, b.src, sticky_s=60)
    b.daemon(pid=200)                                  # daemon-state already rewritten, local.json not
    s = L.take_snapshot(good, 1005.0, b.src, sticky_s=60)
    check("pid.mismatch_sticky_prev", s.darkbloom.state == "up" and s.darkbloom.stale_s > 0
          and s.darkbloom.gen["pid"] == 100, repr(s))
    s = L.take_snapshot(None, 1005.0, b.src, sticky_s=60)
    check("pid.mismatch_no_prev_restarting", s.darkbloom.state == "restarting")
    b.local(pid=200)                                   # local.json catches up
    s = L.take_snapshot(good, 1006.0, b.src, sticky_s=60)
    check("pid.agree_after_rewrite", s.darkbloom.state == "up" and s.darkbloom.stale_s == 0
          and s.darkbloom.gen["pid"] == 200 and s.darkbloom.gen_verified)
    b.src.daemon_state.unlink()
    s = L.take_snapshot(None, 1007.0, b.src)
    check("pid.no_daemon_state_up_unverified", s.darkbloom.state == "up" and not s.darkbloom.gen_verified)
    b.clean()


def t_served_sticky_and_casing():
    b = Box().full()
    good = L.take_snapshot(None, 1000.0, b.src, sticky_s=60)
    b.wipe(b.src.provider_toml)
    b.wipe(b.src.loaded_models)
    s = L.take_snapshot(good, 1010.0, b.src, sticky_s=60)
    check("served.empty_sticky", s.darkbloom.served_exact == good.darkbloom.served_exact, repr(s.darkbloom.served_exact))
    s = L.take_snapshot(good, 1100.0, b.src, sticky_s=60)
    check("served.empty_expires", s.darkbloom.served_exact == (), repr(s.darkbloom.served_exact))
    s = L.take_snapshot(good, 1010.0, b.src, sticky_s=0)
    check("served.kill_switch", s.darkbloom.served_exact == ())
    b.served()
    snap = L.take_snapshot(None, 1000.0, b.src)
    p = plan_for(snap, model="qwen3.5-9b")
    check("case.canonical_served_id", p.model == "Qwen3.5-9B", p.model)
    p = plan_for(snap, model="QWEN3.6-35B-A3B-VL-MTP-MXFP8")
    check("case.upper_to_served", p.model == "qwen3.6-35b-a3b-vl-mtp-mxfp8", p.model)
    check("case.helper_ambiguous_unchanged", L.canonical_served("Ab", ["aB", "AB"]) == "Ab")
    check("case.unknown_unchanged", L.canonical_served("nope", ["A"]) == "nope")
    check("case.not_served_raises", raises(lambda: plan_for(snap, model="Ghost-1B"), L.LaneUnavailable))
    # parity with model_profile.served_exact_ids on the same files
    try:
        sys.path.insert(0, str(HERE))
        import model_profile as mp
        b.served(("Qwen3.5-9B", "x-Y", "Qwen3.5-9B"))
        exact = mp.served_exact_ids(str(b.src.provider_toml), str(b.src.loaded_models))
        mine = list(L.take_snapshot(None, 1.0, b.src).darkbloom.served_exact)
        check("case.parity_model_profile_served_exact_ids", exact == mine, f"{exact} vs {mine}")
        check("case.parity_canonical", mp.canonical_served_id("x-y", exact) == L.canonical_served("x-y", mine))
    except ImportError:
        check("case.parity_model_profile_served_exact_ids", True, "skipped")
    b.clean()


def t_provider_restart_window():
    """Whole restart sequence: good -> torn -> pid-mismatch -> new key landed."""
    b = Box().full(key=KEY1, pid=100)
    seq = []
    snap = L.take_snapshot(None, 0.0, b.src, sticky_s=60)
    plan = plan_for(snap)
    seq.append(snap)
    b.wipe(b.src.local_json); b.wipe(b.src.provider_toml)
    snap = L.take_snapshot(snap, 3.0, b.src, sticky_s=60); seq.append(snap)
    check("restart.window_state_up_stale", snap.darkbloom.state == "up" and snap.darkbloom.stale_s > 0)
    # plan resolved DURING the window uses the whole previous snapshot, not a half-empty one
    p2 = plan_for(snap)
    check("restart.plan_in_window_equals_plan_before", p2.sha == plan.sha and p2.host == plan.host)
    b.daemon(pid=101); b.served()
    snap = L.take_snapshot(snap, 6.0, b.src, sticky_s=60)
    check("restart.torn_daemon_ahead", snap.darkbloom.gen["pid"] == 100 and snap.darkbloom.stale_s > 0)
    b.local(key=KEY2, pid=101)
    snap = L.take_snapshot(snap, 9.0, b.src, sticky_s=60)
    check("restart.recovered_new_key", snap.darkbloom.key_fp == L.key_fp(KEY2) and snap.darkbloom.stale_s == 0)
    p3 = plan_for(snap)
    check("restart.new_gen_new_sha", p3.sha != plan.sha and p3.auth["fp"] == L.key_fp(KEY2))
    check("restart.old_plan_unchanged", plan.auth["fp"] == L.key_fp(KEY1))
    b.clean()


def t_rotation_refresh():
    b = Box().full(key=KEY1, pid=100)
    plan = plan_for(L.take_snapshot(None, 0.0, b.src))
    clock = [0.0]

    def tick(dt):
        clock[0] += dt
    mk = dict(sources=b.src, clock=lambda: clock[0], sleep=tick, wait_s=8)
    r = L.refresh_key(plan, **mk)
    check("rot.unchanged_after_wait", r.status == "unchanged" and clock[0] >= 8, f"{r.status} {clock[0]}")
    clock[0] = 0
    b.local(key=KEY2, pid=100)
    r = L.refresh_key(plan, **mk)
    check("rot.new_key_ok", r.status == "ok" and r.fp == L.key_fp(KEY2) and r.key.reveal() == KEY2, str(r))
    check("rot.rotation_record_fingerprints_only",
          r.rotation["old_fp"] == L.key_fp(KEY1) and r.rotation["new_fp"] == L.key_fp(KEY2)
          and KEY1 not in json.dumps(r.rotation) and KEY2 not in json.dumps(r.rotation))
    # same key but new gen (provider restarted, same key) also ok
    b.local(key=KEY1, pid=300); b.daemon(pid=300)
    r = L.refresh_key(plan, **mk)
    check("rot.new_gen_same_key_ok", r.status == "ok" and r.fp == L.key_fp(KEY1))
    # different base -> expired
    b.local(key=KEY2, pid=100, base="http://127.0.0.1:9999/v1")
    b.daemon(pid=100)
    r = L.refresh_key(plan, **mk)
    check("rot.changed_base_expired", r.status == "expired" and "endpoint_changed" in r.why, str(r))
    # off -> expired
    b.src.local_json.unlink()
    r = L.refresh_key(plan, **mk)
    check("rot.off_expired", r.status == "expired" and "off" in r.why, str(r))
    # model no longer served -> expired
    b.local(key=KEY2, pid=100); b.served(("Other-1B",))
    r = L.refresh_key(plan, **mk)
    check("rot.model_unserved_expired", r.status == "expired" and "model_not_served" in r.why, str(r))
    # restart window: file empty at first, key lands after one sleep -> waits then ok
    b.served()
    b.wipe(b.src.local_json)
    calls = []

    def sleeper(dt):
        calls.append(dt)
        clock[0] += dt
        if len(calls) == 2:
            b.local(key=KEY2, pid=100)
    clock[0] = 0
    r = L.refresh_key(plan, sources=b.src, clock=lambda: clock[0], sleep=sleeper, wait_s=8)
    check("rot.waits_out_restart_window", r.status == "ok" and len(calls) == 2, f"{r.status} {calls}")
    clock[0] = 0
    b.wipe(b.src.local_json)
    r = L.refresh_key(plan, sources=b.src, clock=lambda: clock[0], sleep=tick, wait_s=3)
    check("rot.window_never_ends_unchanged", r.status == "unchanged", str(r))
    b.clean()


def t_expiry_is_event_based():
    b = Box().full()
    snap = L.take_snapshot(None, 0.0, b.src)
    plan = plan_for(snap)
    later = L.take_snapshot(None, 86400.0 * 30, b.src)               # a month later, nothing changed
    check("expiry.no_wallclock", L.expire_reason(plan, later) is None)
    b.local(base="http://127.0.0.1:9001/v1")
    check("expiry.endpoint_change", "endpoint_changed" in (L.expire_reason(plan, L.take_snapshot(None, 1, b.src)) or ""))
    b.local()
    b.served(("Nope-1B",))
    check("expiry.model_gone", "model_not_served" in (L.expire_reason(plan, L.take_snapshot(None, 1, b.src)) or ""))
    b.served()
    b.wipe(b.src.local_json)
    check("expiry.restarting_is_not_expiry", L.expire_reason(plan, L.take_snapshot(None, 1, b.src, sticky_s=0)) is None)
    b.src.local_json.unlink()
    check("expiry.off", L.expire_reason(plan, L.take_snapshot(None, 1, b.src)) == "darkbloom_off")
    b.clean()


def t_resolve_lanes_and_c10():
    b = Box().full()
    snap = L.take_snapshot(None, 0.0, b.src)
    p = plan_for(snap, model="qwen3.8:27b-q4_K_M")
    check("lane.legacy_tag_aliased", p.model == "qwen3.6-35b-a3b-vl-mtp-mxfp8" and p.alias and p.lane == "studio-db"
          and p.api == "openai" and p.host == "http://127.0.0.1:8000")
    p = plan_for(snap, model="llama3:8b", lane="http://198.51.100.145:11434/")
    check("lane.unraid_ollama", p.lane == "unraid" and p.api == "ollama" and p.auth["fp"] is None
          and p.auth["delivery"] == "none" and p.host == "http://198.51.100.145:11434")
    check("lane.c10_darkbloom_only_model_on_ollama_rejected",
          raises(lambda: plan_for(snap, model="Qwen3.5-9B", lane="http://198.51.100.145:11434"), L.LaneUnavailable))
    try:
        plan_for(snap, model="Qwen3.5-9B", lane="http://198.51.100.145:11434")
    except L.LaneUnavailable as e:
        check("lane.c10_cause", e.cause == "model_lane_mismatch", e.cause)
    b.wipe(b.src.local_json)
    snap_r = L.take_snapshot(None, 0.0, b.src, sticky_s=0)
    check("lane.restarting_never_plans_as_ollama",
          raises(lambda: plan_for(snap_r, model="Qwen3.5-9B", lane="http://127.0.0.1:8000"), L.LaneUnavailable)
          and raises(lambda: plan_for(snap_r, model="Qwen3.5-9B", lane="http://198.51.100.145:11434"), L.LaneUnavailable))
    b.local(key="")
    snap_nk = L.take_snapshot(None, 0.0, b.src)
    check("lane.no_key_not_planned", raises(lambda: plan_for(snap_nk), L.LaneUnavailable))
    check("lane.no_host", raises(lambda: L.resolve({"model": "x"}, None, snap, profile_fn=stub_profile), L.LaneUnavailable))
    (b.root / "bonsai-host").write_text("http://192.0.2.34:8092\n")
    snap2 = L.take_snapshot(None, 0.0, b.src)
    b.local()
    snap2 = L.take_snapshot(None, 0.0, b.src)
    p = plan_for(snap2, model="bonsai-model", lane="http://192.0.2.34:8092")
    check("lane.bonsai", p.lane == "bonsai" and p.api == "openai" and p.auth["fp"] is None)
    check("lane.bonsai.preserve_reasoning", p.flags["preserve_reasoning"] is True and plan_for(snap2).flags["preserve_reasoning"] is False)
    pj = L.resolve({"id": "j", "attempt_id": "a", "model": "Qwen3.5-9B", "num_ctx": 12345},
                   "http://127.0.0.1:8000", snap2, profile_fn=stub_profile)
    check("lane.job_ctx_wins", pj.num_ctx == 12345 and pj.ctx_source == "job")
    check("lane.profile_ctx_default", plan_for(snap2).num_ctx == 65536 and plan_for(snap2).ctx_source == "profile")
    b.clean()


def t_real_profile_path():
    """resolve() with the real model_profile.py/yaml (skipped when unavailable)."""
    b = Box().full()
    snap = L.take_snapshot(None, 0.0, b.src)
    src2 = L.Sources.under(b.root)
    try:
        sys.path.insert(0, str(HERE))
        import model_profile  # noqa: F401
    except ImportError:
        check("real.profile", True, "skipped")
        return
    stderr, sys.stderr = sys.stderr, io.StringIO()
    try:
        p = L.resolve({"id": "j", "attempt_id": "a", "model": "qwen3.8:27b-q4_K_M"}, "http://127.0.0.1:8000", snap,
                      sources=src2)
    finally:
        sys.stderr = stderr
    check("real.profile_identity", p.profile and p.profile["fields_sha"] and p.profile["mode"], str(p.profile))
    check("real.profile_alias_to_served", p.model == "qwen3.6-35b-a3b-vl-mtp-mxfp8", p.model)
    b.clean()


def t_determinism_and_immutability():
    b = Box().full()
    snap = L.take_snapshot(None, 0.0, b.src)
    a = plan_for(snap, att="A1", now=1.0)
    c = plan_for(L.take_snapshot(None, 999.0, b.src), att="A2", now=2.0)
    check("det.same_inputs_same_sha", a.sha == c.sha and len(a.sha) == 64)
    check("det.plan_id_differs_by_attempt", a.plan_id != c.plan_id)
    check("det.repeat_identical", plan_for(snap).to_dict() == plan_for(snap).to_dict())
    b.local(key=KEY2)
    check("det.key_change_changes_sha", plan_for(L.take_snapshot(None, 0.0, b.src)).sha != a.sha)
    check("det.model_change_changes_sha", plan_for(snap, model="Qwen3.5-9B").sha != a.sha)
    check("imm.attr_set", raises(lambda: setattr(a, "model", "x"), Exception))
    check("imm.profile_mutation", raises(lambda: a.profile.__setitem__("key", "x"), TypeError))
    check("imm.auth_mutation", raises(lambda: a.auth.__setitem__("fp", "x"), TypeError))
    check("imm.auth_update", raises(lambda: a.auth.update({"fp": "x"}), TypeError))
    check("imm.snapshot_attr", raises(lambda: setattr(snap, "taken_at", 1), Exception))
    check("imm.snapshot_hosts", raises(lambda: snap.ollama_hosts.__setitem__("x", 1), TypeError))
    check("imm.keybox_attr", raises(lambda: setattr(snap.darkbloom.key, "fp", "x"), Exception))
    b.clean()


def t_plan_file():
    b = Box().full()
    p = plan_for(L.take_snapshot(None, 0.0, b.src))
    path = L.write_plan(p, b.root / "plans")
    mode = path.stat().st_mode & 0o777
    check("file.mode_0600", mode == 0o600, oct(mode))
    d = L.load_plan(path)
    check("file.roundtrip", d["sha"] == p.sha and d["host"] == p.host)
    check("file.no_stray_tmp", [x.name for x in (b.root / "plans").iterdir()] == [path.name])
    tampered = json.loads(path.read_text()); tampered["host"] = "http://evil:1"
    path.write_text(json.dumps(tampered))
    check("file.tamper_detected", raises(lambda: L.load_plan(path), ValueError))
    check("file.plan_id_sanitised", "/" not in L.write_plan(
        L.LanePlan(**{**{f: getattr(p, f) for f in p.__dataclass_fields__}, "plan_id": "../../etc/x"}),
        b.root / "plans").name)
    b.clean()


def t_no_key_anywhere():
    b = Box().full(key=KEY1)
    snap = L.take_snapshot(None, 0.0, b.src)
    plan = plan_for(snap)
    blobs = {
        "repr(snap)": repr(snap), "repr(plan)": repr(plan), "str(plan)": str(plan),
        "snap.public": json.dumps(snap.public(), default=str), "plan.public": json.dumps(plan.public(), default=str),
        "log_line": plan.log_line(), "repr(darkbloom)": repr(snap.darkbloom), "str(keybox)": str(snap.darkbloom.key),
        "fmt(keybox)": f"{snap.darkbloom.key} {snap.darkbloom.key!r} {snap.darkbloom.key:>40}",
        "dataclass-repr": repr(plan.auth) + repr(plan.provider),
    }
    path = L.write_plan(plan, b.root / "plans")
    blobs["plan_file"] = path.read_text()
    for k, v in blobs.items():
        check(f"leak.{k}", KEY1 not in v)
    check("leak.pickle_keybox_refused", raises(lambda: pickle.dumps(snap.darkbloom.key), TypeError))
    check("leak.scan_detects_secret_fields", L.scan_for_secret_fields({"a": {"api_key": "x"}, "b": [{"token": "y"}]})
          == ["a.api_key", "b[0].token"])
    check("leak.scan_clean_on_plan", L.scan_for_secret_fields(plan.to_dict()) == [])
    check("leak.scan_clean_on_snapshot", L.scan_for_secret_fields(snap.public()) == [])
    rk = L.refresh_key(plan, sources=b.src, wait_s=0, clock=lambda: 0.0, sleep=lambda s: None)
    check("leak.refresh_result_repr", KEY1 not in repr(rk) and KEY2 not in repr(rk))
    # fd delivery: env and argv of the child carry no key; the child gets the fp via the pipe
    env_before = dict(os.environ)
    r = L.open_key_pipe(snap)
    check("leak.pipe_fd_open", isinstance(r, int))
    child = subprocess.Popen(
        [sys.executable, "-c",
         "import os,sys,time\nsys.path.insert(0,%r)\nimport importlib.util as u\n"
         "s=u.spec_from_file_location('lp',%r);m=u.module_from_spec(s);s.loader.exec_module(m)\n"
         "k=m.read_key_fd(int(sys.argv[1]))\nprint(k.fp, flush=True)\ntime.sleep(2)" % (str(HERE), SRC), str(r)],
        pass_fds=(r,), stdout=subprocess.PIPE, text=True)
    os.close(r)
    fp = child.stdout.readline().strip()
    ps = subprocess.run(["ps", "-ww", "-o", "command=", "-p", str(child.pid)], capture_output=True, text=True).stdout
    try:
        envdump = subprocess.run(["ps", "-ww", "-E", "-o", "command=", "-p", str(child.pid)],
                                 capture_output=True, text=True).stdout
    except OSError:
        envdump = ""
    child.kill(); child.wait()
    check("leak.fd_roundtrip_fp", fp == L.key_fp(KEY1), fp)
    check("leak.ps_argv_clean", KEY1 not in ps and "SENTINEL" not in ps, ps)
    check("leak.child_env_clean", KEY1 not in envdump)
    check("leak.parent_env_unchanged", dict(os.environ) == env_before and KEY1 not in json.dumps(dict(os.environ)))
    # every file under the sandbox except the vendor sources that legitimately hold the key
    holders = {b.src.local_json.resolve()}
    leaked = [str(p) for p in b.root.rglob("*") if p.is_file() and p.resolve() not in holders and KEY1 in p.read_text(errors="ignore")]
    check("leak.no_other_file_has_key", not leaked, str(leaked))
    check("leak.pipe_none_without_key", (b.local(key="") or True) and
          L.open_key_pipe(L.take_snapshot(None, 0.0, b.src)) is None)
    b.clean()


def t_secret_guards():
    kb = L.KeyBox(KEY1)
    check("guard.thaw_never_serialises_keybox", L._thaw({"a": [kb], "b": kb}) == {"a": [None], "b": None})
    b = Box().full()
    snap = L.take_snapshot(None, 0.0, b.src)
    evil = lambda m, r, l, a, s: (m, None, {"key": "p", "api_key": KEY1}, 1)      # noqa: E731
    check("guard.plan_with_secret_field_refused",
          raises(lambda: L.resolve({"id": "j", "model": "Qwen3.5-9B"}, "http://127.0.0.1:8000", snap, profile_fn=evil), AssertionError))
    b.clean()


def t_kill_switch():
    b = Box().full()
    snap = L.take_snapshot(None, 0.0, b.src)
    plan = plan_for(snap)
    off = {"LANE_PLAN": "0"}
    check("kill.enabled_default", L.enabled({}))
    check("kill.env0", not L.enabled(off) and not L.enabled({"LANE_PLAN": " off "}) and L.enabled({"LANE_PLAN": "1"}))
    check("kill.resolve_none", plan_for(snap, env=off) is None)
    check("kill.pipe_none", L.open_key_pipe(snap, env=off) is None)
    check("kill.refresh_disabled", L.refresh_key(plan, env=off).status == "disabled")
    check("kill.snapshot_still_works", L.take_snapshot(None, 0.0, b.src).darkbloom.state == "up")
    f = b.root / "lane-plan.disabled"
    check("kill.file_absent_enabled", L.enabled({}, disable_file=f))
    f.write_text("")
    check("kill.file_disables", not L.enabled({}, disable_file=f))
    old = L.DISABLE_FILE
    L.DISABLE_FILE = f
    try:
        check("kill.file_via_resolve", plan_for(snap) is None)
    finally:
        L.DISABLE_FILE = old
    check("kill.sticky_env_parse", [L.sticky_s_from_env(x) for x in ("off", "", "abc", "-1", "nan", "0", "30", None, "No")]
          == [0.0, 60.0, 60.0, 60.0, 60.0, 0.0, 30.0, 60.0, 0.0])
    b.clean()


def t_import_safety():
    home = Path(tempfile.mkdtemp(prefix="lanep-home-"))
    r = subprocess.run([sys.executable, "-c",
                        f"import importlib.util as u;s=u.spec_from_file_location('lp',{SRC!r});"
                        "m=u.module_from_spec(s);s.loader.exec_module(m)"],
                       env={"HOME": str(home), "PATH": os.environ.get("PATH", "")}, capture_output=True, text=True)
    check("import.clean_exit_no_output", r.returncode == 0 and not r.stdout and not r.stderr, r.stderr)
    check("import.no_files_created", not any(home.iterdir()))
    shutil.rmtree(home, ignore_errors=True)
    # snapshot with a nonexistent HOME-based default: never raises
    r = subprocess.run([sys.executable, "-c",
                        f"import importlib.util as u;s=u.spec_from_file_location('lp',{SRC!r});"
                        "m=u.module_from_spec(s);s.loader.exec_module(m);print(m.take_snapshot().darkbloom.state)"],
                       env={"HOME": "/nonexistent-home", "PATH": os.environ.get("PATH", "")}, capture_output=True, text=True)
    check("import.snapshot_without_home", r.returncode == 0 and r.stdout.strip() in ("off", "unknown"), r.stdout + r.stderr)


def main():
    for t in (t_snapshot_basic, t_base_variants, t_empty_torn_missing, t_pid_mismatch, t_served_sticky_and_casing,
              t_provider_restart_window, t_rotation_refresh, t_expiry_is_event_based, t_resolve_lanes_and_c10,
              t_real_profile_path, t_determinism_and_immutability, t_plan_file, t_no_key_anywhere, t_secret_guards, t_kill_switch,
              t_import_safety):
        try:
            t()
        except Exception as e:      # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__}: crashed {type(e).__name__}: {e}")
            print(f"FAIL {t.__name__} crashed: {e}")


# (old, new) text mutants of lane_plan.py. Each must make this test fail.
# Equivalent (not listed): `is_db and state != up` (base is None unless up, so is_db implies up);
# `KeyBox(key) if key else None` (an empty KeyBox is falsy, same as None).
MUTANTS = [
    ('flags = {"preserve_reasoning": lane == BONSAI_LANE}', 'flags = {"preserve_reasoning": False}'),
    ('db = DarkbloomState(state="off", why="local.json missing", ',
     'db = last_good("masked") if pdb is not None else DarkbloomState(state="off", why="local.json missing", '),
    ('db = last_good("local.json empty/torn (restart rewrite)")', 'db = DarkbloomState(state="off", why="torn")'),
    ("if lpid is not None and ds_pid is not None and lpid != ds_pid:", "if False:"),
    ("and t - pdb.good_at < win:", ":"),
    ("if win > 0 and pdb is not None and pdb.state == \"up\" and pdb.good_at is not None", "if False and pdb is not None and pdb.state == \"up\" and pdb.good_at is not None"),
    ("if not served and win > 0 and prev is not None", "if False and prev is not None"),
    ("return f\"<KeyBox {self.fp}>\"", "return f\"<KeyBox {self._k}>\""),
    ("return None                      # never serialise a key, even by accident", "return o.reveal()"),
    ("return \"sha256:\" + hashlib.sha256(str(key).encode()).hexdigest()[:8]", "return \"sha256:\" + str(key)"),
    ("if _SECRET_FIELD_RE.search(str(k)) and k not in (\"key_ref\",):", "if False:"),
    ("return next(iter(hits)) if len(hits) == 1 else m", "return m"),
    ("if db.state == \"up\" and db.base != plan.host:", "if False:"),
    ("if db.state == \"up\" and db.served_exact and plan.model not in db.served_exact:", "if False:"),
    ("why = expire_reason(plan, snap)\n        if why:", "why = None\n        if why:"),
    ("if db.key_fp != old_fp or gen_changed:", "if True:"),
    ("if db.key_fp != old_fp or gen_changed:", "if db.key_fp != old_fp:"),
    ("    if not enabled(env):\n        return None\n    src = sources or Sources()", "    src = sources or Sources()"),
    ("    if not enabled(env):\n        return None\n    kb = snap.darkbloom.key", "    kb = snap.darkbloom.key"),
    ("    if not enabled(env):\n        return RefreshResult(\"disabled\"", "    if False:\n        return RefreshResult(\"disabled\""),
    ("os.fchmod(fd, 0o600)", "os.fchmod(fd, 0o644)"),
    ("if _sha(body) != want:", "if False:"),
    ("if not is_db and only_db:", "if False:"),
    ("body = dict(v=SCHEMA, lane=lane,", "body = dict(created_at=time.time(), v=SCHEMA, lane=lane,"),
    ("__setitem__ = __delitem__ = clear = pop = popitem = setdefault = update = _ro", "pass"),
    ("@dataclasses.dataclass(frozen=True)\nclass LanePlan:", "@dataclasses.dataclass\nclass LanePlan:"),
    ("if s in (\"off\", \"false\", \"no\", \"disable\", \"disabled\"):\n        return 0.0", "if s in (\"off\", \"false\", \"no\", \"disable\", \"disabled\"):\n        return 60.0"),
    ("if is_db and not db.key:", "if False:"),
    ("if db.state == \"restarting\":\n        return None\n    if db.state == \"up\" and db.base", "if db.state == \"restarting\":\n        return \"restarting\"\n    if db.state == \"up\" and db.base"),
    ("if bad:                               # structural guard", "if False:                               # structural guard"),
    ("finally:\n        os.close(w)", "finally:\n        pass"),
    ("model = canonical_served(model, served)", "pass"),
]


def mutants():
    base = Path(SRC).read_text()
    bad = 0
    for i, (old, new) in enumerate(MUTANTS):
        if old not in base:
            print(f"MUTANT {i} NOT APPLICABLE: {old[:60]!r}")
            bad += 1
            continue
        d = Path(tempfile.mkdtemp(prefix="lp-mut-"))
        (d / "lane_plan.py").write_text(base.replace(old, new, 1))
        try:
            r = subprocess.run([sys.executable, str(HERE / "test-lane-plan.py")],
                               env={**os.environ, "LANE_PLAN_SRC": str(d / "lane_plan.py")},
                               capture_output=True, text=True, timeout=90)
            killed = r.returncode != 0
        except subprocess.TimeoutExpired:       # a hang is a detected regression too
            killed = True
        print(f"mutant {i} ({old[:48]!r}): {'killed' if killed else 'SURVIVED'}")
        bad += not killed
        shutil.rmtree(d, ignore_errors=True)
    print("ALL MUTANTS KILLED" if not bad else f"{bad} MUTANT(S) SURVIVED / NOT APPLICABLE")
    return bad


if __name__ == "__main__":
    if "--mutants" in sys.argv or "--revert-check" in sys.argv:
        r = subprocess.run([sys.executable, str(Path(__file__).resolve())], capture_output=True, text=True)
        if r.returncode != 0:
            print(r.stdout[-3000:])
            print("BASELINE FAILS: mutants meaningless")
            sys.exit(2)
        sys.exit(1 if mutants() else 0)
    main()
    print("ALL PASS" if not FAILS else f"FAILED: {FAILS}")
    sys.exit(1 if FAILS else 0)
