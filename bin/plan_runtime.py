#!/usr/bin/env python3
"""plan_runtime: the queue/worker glue for the resolved-lane PLAN (hardening design sections 1+3, WAVE 2).

lane_plan.py is the pure resolver; conformance_log.py is the pure record/classify library. THIS module
is the only code that ollama-queue.py and ollama-worker.py call into, so each of those 20k/10k-line files
carries a handful of one-line hooks instead of the logic:

  mode()                  off | shadow | enforce.   LANE_PLAN=0 or ~/.ollama-dispatch/lane-plan.disabled -> off.
                          LANE_PLAN_ENFORCE=1 -> enforce.  Default: SHADOW.
  SHADOW  the legacy path (local.json / provider.toml re-reads, the sticky caches) still DECIDES. The plan
          path is computed beside it; every difference is appended to
          ~/.ollama-dispatch/conformance/plan-mismatch.jsonl via conformance_log.write_mismatch with a drift
          class. Shadow code never raises into the caller and never changes a return value.
  ENFORCE the plan decides: the worker takes host/lane/key/served-set from the plan, performs NO
          local.json / provider.toml read (the key arrives on an inherited pipe fd; the only sanctioned
          re-read is lane_plan.refresh_key on HTTP 401), and the queue builds the worker argv from the plan.

Queue side:  tick_snapshot, plan_for_launch, compare_cmd, plan_argv, apply_plan_to_cmd, job_plan_summary,
             reap_check, prune_plans.
Worker side: init_worker, worker_auth / shadow_auth, worker_is_lane / shadow_is_lane, worker_served /
             shadow_served, worker_rotated_retry / shadow_rotated_retry, rec (one conformance record per
             request), summary (per-dispatch request count -> GAP detection), init_failure_cause.

Kill switches: LANE_PLAN=0 (everything), WORKER_PLAN=0 (worker ignores --plan/--key-fd),
LANE_PLAN_KEY_FD=0 (default: the queue does not pass the key fd; the worker reads the key once through
lane_plan if it is enforcing), CONFORMANCE_LOG=0 (no records), CONFORMANCE_CHECK=0 (no reap comparison),
CONFORMANCE_ENFORCE=0 (default; flag only), CONFORMANCE_ALERT=0 (default; notify only when 1).

This file reads no vendor file itself: lane_plan.py owns that (the `noreread` ratchet scans for it).
Secrets: the key lives only in lane_plan.KeyBox; logs/records carry sha256 fingerprints. Import-safe.
"""
import os
import sys
import time
import types
from pathlib import Path

_HERE = os.path.dirname(os.path.realpath(__file__))
if _HERE not in sys.path:
    sys.path.insert(0, _HERE)

import conformance_log as CL      # noqa: E402
import lane_plan as LP            # noqa: E402

PLANS_DIR = Path(os.path.expanduser("~/.ollama-dispatch/plans"))
PLAN_RETENTION_S = 7 * 86400
_FALSE = ("0", "off", "false", "no", "disable", "disabled")


def _flag(name, default, env=None):
    e = os.environ if env is None else env
    return str(e.get(name, default)).strip().lower() not in _FALSE


def conf_dir(env=None):
    e = os.environ if env is None else env
    return Path(e["CONFORMANCE_DIR"]) if e.get("CONFORMANCE_DIR") else CL.DEFAULT_DIR


def mode(env=None):
    """off | shadow | enforce (queue view)."""
    e = os.environ if env is None else env
    if not LP.enabled(e):
        return "off"
    return "enforce" if _flag("LANE_PLAN_ENFORCE", "0", e) else "shadow"


def worker_mode(env=None):
    e = os.environ if env is None else env
    m = mode(e)
    if m == "off" or not _flag("WORKER_PLAN", "1", e):
        return "off"
    return m


def key_fd_enabled(env=None):
    return _flag("LANE_PLAN_KEY_FD", "0", env)


def norm(u):
    return LP.norm_url(u)


def _mm(cls, field, plan_v, legacy_v, **kw):
    kw.setdefault("directory", conf_dir())
    return CL.write_mismatch(cls, field, plan_v, legacy_v, **kw)


# ======================================================================= queue side
def tick_snapshot(prev=None):
    """ONE consistent vendor read per daemon tick (call OUTSIDE the state lock). Never raises; None when off."""
    try:
        if mode() == "off":
            return None
        return LP.take_snapshot(prev)
    except Exception:        # noqa: BLE001
        return prev


class LaunchPlan:
    __slots__ = ("plan", "path", "sha", "keyfd", "snap")

    def __init__(self, plan, path, snap):
        self.plan, self.path, self.sha, self.snap, self.keyfd = plan, path, plan.sha, snap, None


def plan_for_launch(job, chosen_url, snap, att_id, role=None, plans_dir=None, env=None):
    """Resolve + persist the plan for one launch. None when off / no snapshot / resolve disabled.
    LaneUnavailable: SHADOW logs it (what enforce would do) and returns None; ENFORCE re-raises
    (the caller turns it into an INFRA WAIT, never a job failure). Any other error: shadow swallows
    (info row), enforce re-raises."""
    m = mode(env)
    if m == "off" or snap is None:
        return None
    j = {"id": job.get("id"), "attempt_id": att_id, "model": job.get("model"), "num_ctx": job.get("num_ctx")}
    try:
        plan = LP.resolve(j, chosen_url, snap, role=role or job.get("role") or "author", env=env)
    except LP.LaneUnavailable as e:
        _mm("PLAN_UNAVAILABLE", e.cause, (e.detail or "plan: unavailable")[:200], "legacy: launched",
            job=job.get("id"), att=att_id, where="queue.launch", kind="would_block")
        if m == "enforce":
            raise
        return None
    except Exception as e:   # noqa: BLE001
        _mm("PLAN_ERROR", type(e).__name__, str(e)[:200], "legacy: launched",
            job=job.get("id"), att=att_id, where="queue.launch", kind="info")
        if m == "enforce":
            raise
        return None
    if plan is None:
        return None
    path = None
    try:
        path = str(LP.write_plan(plan, plans_dir or PLANS_DIR))
    except Exception as e:   # noqa: BLE001
        _mm("PLAN_ERROR", "write_plan", str(e)[:200], "-", job=job.get("id"), att=att_id,
            where="queue.launch", kind="info")
        if m == "enforce":
            raise
    return LaunchPlan(plan, path, snap)


def _argv_val(cmd, flag):
    try:
        i = cmd.index(flag)
        return cmd[i + 1] if i + 1 < len(cmd) else None
    except ValueError:
        return None


def is_worker_cmd(cmd):
    return "--task" in cmd


def compare_cmd(plan, cmd, legacy_lane=None):
    """PURE. Differences between the plan and the argv the LEGACY _build_cmd produced:
    [(drift_class, field, plan_value, legacy_value)]. Runner commands carry only --model/--host/
    --num-ctx (the model is the runner's own business), so only host + num_ctx are compared."""
    p = plan.to_dict() if hasattr(plan, "to_dict") else dict(plan)
    out = []
    worker = is_worker_cmd(cmd)
    h = _argv_val(cmd, "--host")
    if norm(h) != norm(p.get("host")):
        out.append(("HOST_DRIFT", "host", norm(p.get("host")), norm(h)))
    if legacy_lane is not None and legacy_lane != p.get("lane"):
        out.append(("HOST_DRIFT", "lane", p.get("lane"), legacy_lane))
    if worker:
        m = _argv_val(cmd, "--model")
        if m != p.get("model"):
            out.append(("MODEL_DRIFT", "model", p.get("model"), m))
        api = _argv_val(cmd, "--api") or "ollama"
        if api != p.get("api"):
            out.append(("API_DRIFT", "api", p.get("api"), api))
        pr = "--preserve-reasoning" in cmd
        if pr != bool((p.get("flags") or {}).get("preserve_reasoning")):
            out.append(("API_DRIFT", "flags.preserve_reasoning",
                        bool((p.get("flags") or {}).get("preserve_reasoning")), pr))
    nc = _argv_val(cmd, "--num-ctx")
    if p.get("num_ctx") is not None and nc is not None and str(p.get("num_ctx")) != str(nc):
        out.append(("CTX_DRIFT", "num_ctx", p.get("num_ctx"), nc))
    return out


def log_cmd_mismatches(lp, cmd, job, att, legacy_lane=None):
    """SHADOW: append every plan-vs-legacy-argv difference. Returns the list (for the caller's log line)."""
    diffs = []
    try:
        diffs = compare_cmd(lp.plan, cmd, legacy_lane)
        for cls, field, pv, lv in diffs:
            _mm(cls, field, pv, lv, job=job.get("id"), att=att, where="queue.build_cmd")
    except Exception:        # noqa: BLE001
        pass
    return diffs


def plan_argv(lp, worker_supports):
    """Extra worker argv: --plan PATH --plan-sha SHA when the worker declares it. [] otherwise.
    `worker_supports` is the queue's _worker_supports_flag. The key fd flag is separate (open_key_fd)."""
    try:
        if lp is None or not lp.path or not worker_supports("--plan"):
            return []
        return ["--plan", lp.path, "--plan-sha", lp.sha]
    except Exception:        # noqa: BLE001
        return []


def open_key_fd(lp, worker_supports, env=None):
    """LANE_PLAN_KEY_FD=1 only: pipe holding the Darkbloom key for lp.plan. Returns (argv, pass_fds).
    The caller MUST close the fds in `pass_fds` in the parent after Popen (finally). ([], ()) otherwise."""
    try:
        if (lp is None or not key_fd_enabled(env) or lp.plan.lane != LP.DARKBLOOM_LANE
                or not worker_supports("--key-fd") or lp.snap is None):
            return [], ()
        r = LP.open_key_pipe(lp.snap, env)
        if r is None:
            return [], ()
        return ["--key-fd", str(r)], (r,)
    except Exception:        # noqa: BLE001
        return [], ()


def apply_plan_to_cmd(lp, cmd):
    """ENFORCE only: the plan decides the argv. Rewrites --model/--host/--num-ctx/--api and the
    --preserve-reasoning flag in place of the legacy values. Returns a NEW list."""
    p = lp.plan.to_dict()
    out = list(cmd)

    def setv(flag, val):
        if val is None:
            return
        if flag in out:
            out[out.index(flag) + 1] = str(val)
        else:
            out.extend([flag, str(val)])
    worker = is_worker_cmd(out)
    setv("--host", p["host"])
    setv("--num-ctx", p.get("num_ctx"))
    if worker:
        setv("--model", p["model"])
        if p["api"] != "ollama":
            setv("--api", p["api"])
        elif "--api" in out:
            i = out.index("--api")
            del out[i:i + 2]
        pr = bool((p.get("flags") or {}).get("preserve_reasoning"))
        if pr and "--preserve-reasoning" not in out:
            out.append("--preserve-reasoning")
        if not pr and "--preserve-reasoning" in out:
            out.remove("--preserve-reasoning")
    return out


def prepare_launch(lp, cmd, job, att, legacy_lane, worker_supports, env=None):
    """The whole launch-side hook sequence the queue runs after the legacy _build_cmd, in one call (so the
    queue carries one line and the tests drive exactly what the queue runs):
      1. SHADOW/ENFORCE: log every plan-vs-legacy-argv difference to the mismatch file;
      2. ENFORCE: the plan decides the argv (apply_plan_to_cmd);
      3. worker commands: append --plan/--plan-sha (when the worker declares them) and, with
         LANE_PLAN_KEY_FD=1, --key-fd N + the pipe's read fd in `pass_fds`.
    Returns (cmd, pass_fds). The caller MUST os.close() every fd in pass_fds after Popen (success or failure).
    Never raises: on any internal error the legacy cmd is returned unchanged with no fds."""
    if lp is None:
        return cmd, ()
    try:
        log_cmd_mismatches(lp, cmd, job, att, legacy_lane=legacy_lane)
        out = apply_plan_to_cmd(lp, cmd) if mode(env) == "enforce" else list(cmd)
        fds = ()
        if not job.get("runner"):
            out += plan_argv(lp, worker_supports)
            kargv, fds = open_key_fd(lp, worker_supports, env)
            out += kargv
        return out, fds
    except Exception:        # noqa: BLE001
        return cmd, ()


def close_fds(fds):
    """Close every inherited-pipe fd the launch opened (after Popen, or on a launch failure). Never raises.
    Returns () so the caller can rebind and never double-close."""
    for fd in fds or ():
        try:
            os.close(fd)
        except OSError:
            pass
    return ()


def job_plan_summary(lp, m=None):
    """The small dict stored on the job row (no key, no served list)."""
    p = lp.plan
    return {"plan_id": p.plan_id, "sha": p.sha[:16], "lane": p.lane, "host": p.host, "api": p.api,
            "model": p.model, "kfp": (p.auth or {}).get("fp"), "path": lp.path, "mode": m or mode()}


def prune_plans(plans_dir=None, now=None, keep_s=PLAN_RETENTION_S):
    """Delete plan files older than keep_s. Never raises. Returns the number removed."""
    n = 0
    try:
        d = Path(plans_dir or PLANS_DIR)
        t = time.time() if now is None else now
        for p in d.glob("*.json"):
            try:
                if t - p.stat().st_mtime > keep_s:
                    p.unlink()
                    n += 1
            except OSError:
                pass
    except Exception:        # noqa: BLE001
        pass
    return n


def _summary_calls(job_id, att, directory):
    """Latest worker-reported chat-call count for (job, attempt) or None."""
    try:
        best = None
        for r in CL.read_summaries(directory):
            if r.get("job") == job_id and r.get("att") == att:
                best = r
        return None if best is None else int(best.get("calls"))
    except Exception:        # noqa: BLE001
        return None


def reap_check(job, now=None, plans_dir=None, directory=None, env=None):
    """At job reap: classify the attempt's conformance records against its plan. Returns the verdict
    dict stored on job["conformance"] ({"ok","drift","n","calls"}) or None when there is nothing to
    check (no plan / disabled). Drift rows are also appended to the mismatch file (kind "drift").
    Reads two small files; never raises."""
    try:
        ps = job.get("plan")
        if not ps or not CL.check_enabled(env):
            return None
        d = directory or conf_dir(env)
        plan = LP.load_plan(ps["path"])
        att = str(ps["plan_id"]).rsplit("-", 1)[-1]
        since = None
        try:                      # bound the scan to the attempt's own day file(s): this runs under the state lock
            from datetime import datetime
            since = datetime.fromisoformat(str(job.get("launched_at"))).timestamp() - 120
        except Exception:         # noqa: BLE001
            since = (time.time() if now is None else now) - 2 * 86400
        recs = list(CL.iter_records(d, since=since, att=att, job=job.get("id")))
        calls = _summary_calls(job.get("id"), att, d)
        v = CL.classify(plan, recs, expected_calls=calls, env=env)
        v["calls"] = calls
        if v.get("drift"):
            for dr in v["drift"]:
                _mm(dr["class"], "reap", dr.get("detail"), f"n={dr.get('n')}", job=job.get("id"), att=att,
                    where="queue.reap", kind="drift", directory=d)
        return v
    except Exception as e:   # noqa: BLE001
        return {"ok": None, "drift": [], "n": 0, "error": f"{type(e).__name__}: {e}"[:200]}


def alert_cmd(job, verdict):
    """argv for notify-owner.py, or None (CONFORMANCE_ALERT=0 default, or nothing drifted)."""
    try:
        if not verdict or verdict.get("ok") is not False or not _flag("CONFORMANCE_ALERT", "0"):
            return None
        classes = ",".join(sorted({d["class"] for d in verdict["drift"]}))
        return [sys.executable, os.path.join(_HERE, "notify-owner.py"),
                f"Lane conformance drift: {classes}", f"job {job.get('id')} ({job.get('label')}) lane {job.get('lane')}",
                "--dedupe-key", f"conformance-{(job.get('plan') or {}).get('lane')}-{classes}", "--dedupe-s", "3600"]
    except Exception:        # noqa: BLE001
        return None


# ======================================================================= worker side
class PlanExpired(RuntimeError):
    """ENFORCE: the plan is no longer valid (endpoint changed / off / model not served). The worker pauses
    resumable with lane_cause 'plan_expired: ...'; the queue relaunches with a FRESH plan."""


class WorkerPlan:
    def __init__(self, plan, key, m):
        self.plan, self.key, self.mode = plan, key, m
        self.host = norm(plan.get("host"))
        self.is_db = plan.get("lane") == LP.DARKBLOOM_LANE
        self.fp = (plan.get("auth") or {}).get("fp")
        self.gen = (plan.get("auth") or {}).get("gen") or {}
        self.rotations = []
        self.ev = None
        self.served_lower = {str(x).lower() for x in ((plan.get("provider") or {}).get("served") or [])}

    def view(self):
        """Attribute-style object for lane_plan.refresh_key (it reads plan.auth/.host/.model/.lane)."""
        return types.SimpleNamespace(
            lane=self.plan.get("lane"), host=self.host, model=self.plan.get("model"),
            auth={"fp": self.fp, "gen": self.gen, "rotations": list(self.rotations)})

    def headers(self, host):
        if self.is_db and self.key and norm(host) == self.host:
            return {"Authorization": "Bearer " + self.key.reveal()}
        return {}


_WP = [None]
_INIT_ERR = [None]
_REC = [None]
_SEEN_ONCE = set()


def current():
    return _WP[0]


def reset_for_tests():
    _WP[0] = _INIT_ERR[0] = _REC[0] = None
    _SEEN_ONCE.clear()


def init_failure_cause():
    """ENFORCE: why the plan could not be loaded (the worker pauses with this lane_cause), else None."""
    return _INIT_ERR[0]


def init_worker(plan_path, plan_sha=None, key_fd=None, env=None, job=None):
    """Load the plan (sha verified) and the key (inherited fd). Never raises.
    SHADOW: a bad plan is logged (info row) and ignored. ENFORCE: sets init_failure_cause()."""
    m = worker_mode(env)
    key = None
    try:
        if key_fd is not None:
            try:
                key = LP.read_key_fd(int(key_fd))       # always drained + closed, whatever the mode
            except Exception:    # noqa: BLE001
                key = None
        if m == "off" or not plan_path:
            return None
        err = None
        d = None
        try:
            d = LP.load_plan(plan_path)
            if plan_sha and d.get("sha") != plan_sha:
                err = "plan_sha_mismatch: queue " + str(plan_sha)[:12] + " file " + str(d.get("sha"))[:12]
        except Exception as e:   # noqa: BLE001
            err = f"plan_load_failed: {type(e).__name__}: {e}"[:200]
        if err:
            _mm("PLAN_ERROR", "load", err, "-", job=job, where="worker.init", kind="info" if m == "shadow" else "mismatch")
            if m == "enforce":
                _INIT_ERR[0] = err
            return None
        wp = WorkerPlan(d, key, m)
        if wp.is_db and wp.key is None and m == "enforce":
            # LANE_PLAN_KEY_FD=0: the ONE sanctioned vendor read of an enforcing worker, through lane_plan.
            try:
                snap = LP.take_snapshot(None, sticky_s=0)
                if snap.darkbloom.state == "up" and norm(snap.darkbloom.base) == wp.host:
                    wp.key = snap.darkbloom.key
            except Exception:    # noqa: BLE001
                pass
        if wp.is_db:
            got = LP.key_fp(wp.key.reveal()) if wp.key else None
            if got != wp.fp and (got is not None or m == "enforce"):   # shadow w/o a key fd: nothing to compare
                _mm("KEY_DRIFT", "plan.auth.fp", wp.fp, got, job=job, where="worker.init",
                    kind="mismatch")
                if m == "enforce":
                    _INIT_ERR[0] = f"plan_key_mismatch: plan {wp.fp} key {got}"
                    return None
        _WP[0] = wp
        return wp
    except Exception as e:       # noqa: BLE001
        if m == "enforce":
            _INIT_ERR[0] = f"plan_init_error: {type(e).__name__}: {e}"[:200]
        return None


def _enforcing():
    wp = _WP[0]
    return wp is not None and wp.mode == "enforce"


def _job():
    return os.environ.get("OLLAMA_DISPATCH_VIA_QUEUE")


def _att(wp=None):
    a = os.environ.get("OLLAMA_DISPATCH_ATTEMPT")
    if a:
        return a
    wp = wp or _WP[0]
    return str(wp.plan.get("plan_id")).rsplit("-", 1)[-1] if wp else None


def _bearer_fp(headers):
    h = (headers or {}).get("Authorization") or ""
    return LP.key_fp(h[7:]) if h.startswith("Bearer ") and len(h) > 7 else None


# ---- auth
def worker_auth(host):
    """ENFORCE: the plan's headers for `host` (no vendor read). None = not enforcing (legacy decides)."""
    wp = _WP[0]
    if wp is None or wp.mode != "enforce":
        return None
    return wp.headers(host)


def shadow_auth(host, legacy_headers):
    """SHADOW: compare what the legacy path sent with what the plan says. Returns legacy_headers unchanged."""
    try:
        wp = _WP[0]
        if wp is None or wp.mode != "shadow":
            return legacy_headers
        lfp = _bearer_fp(legacy_headers)
        expects = wp.is_db and norm(host) == wp.host
        if expects and lfp is None:
            _mm("KEY_DRIFT", "auth.present", "key expected", "no key sent", job=_job(), att=_att(wp), where="worker.auth")
        elif expects and lfp != wp.fp:
            # legacy re-reads local.json per request: a different fp is either a mid-flight rotation
            # (benign, the plan has not refreshed) or a real split. Recorded as info; enforce refreshes via 401.
            _mm("KEY_DRIFT", "auth.fp", wp.fp, lfp, job=_job(), att=_att(wp), where="worker.auth", kind="info")
        elif not expects and lfp is not None:
            _mm("KEY_DRIFT", "auth.leak", f"no key for {norm(host)}", lfp, job=_job(), att=_att(wp), where="worker.auth")
    except Exception:        # noqa: BLE001
        pass
    return legacy_headers


# ---- lane identity
def worker_is_lane(host):
    wp = _WP[0]
    if wp is None or wp.mode != "enforce":
        return None
    return bool(wp.is_db and norm(host) == wp.host)


def shadow_is_lane(host, legacy):
    try:
        wp = _WP[0]
        if wp is not None and wp.mode == "shadow":
            want = bool(wp.is_db and norm(host) == wp.host)
            if want != bool(legacy):
                _mm("HOST_DRIFT", "is_darkbloom_lane", want, bool(legacy), job=_job(), att=_att(wp), where="worker.lane")
    except Exception:        # noqa: BLE001
        pass
    return legacy


# ---- served-model set (ensure_model_ready)
def worker_served():
    wp = _WP[0]
    if wp is None or wp.mode != "enforce" or not wp.is_db:
        return None
    return set(wp.served_lower)


def shadow_served(legacy):
    try:
        wp = _WP[0]
        if wp is not None and wp.mode == "shadow" and wp.is_db and "served" not in _SEEN_ONCE:
            _SEEN_ONCE.add("served")
            if set(legacy) != wp.served_lower:
                _mm("MODEL_DRIFT", "served_set", sorted(wp.served_lower)[:12], sorted(legacy)[:12],
                    job=_job(), att=_att(wp), where="worker.served")
    except Exception:        # noqa: BLE001
        pass
    return legacy


# ---- 401 / rotation
def worker_rotated_retry(host, state, enabled=True):
    """ENFORCE: True = one uncharged retry with the refreshed in-memory key; False = normal accounting;
    None = not enforcing (legacy decides). Raises PlanExpired when the plan no longer holds. At most one
    refresh per call (state['k401'])."""
    wp = _WP[0]
    if wp is None or wp.mode != "enforce":
        return None
    if not enabled or not wp.is_db or norm(host) != wp.host or state.get("k401", 0) >= 1:
        return False
    state["k401"] = state.get("k401", 0) + 1
    res = LP.refresh_key(wp.view())
    if res.status == "disabled":
        return None
    if res.status == "expired":
        raise PlanExpired(f"plan_expired: {res.why}")
    if res.status == "ok":
        wp.key, wp.fp = res.key, res.fp
        if res.rotation:
            wp.gen = res.rotation.get("gen") or wp.gen
            wp.rotations.append(res.rotation)
        wp.ev = "key_rotated"
        return True
    return False                                  # unchanged: a bad key, not a rotation


def shadow_rotated_retry(host, legacy):
    """SHADOW: after the legacy 401 handling decided, ask refresh_key (no wait: the legacy wait already
    happened) what the plan path would have done and log a disagreement. Returns legacy."""
    try:
        wp = _WP[0]
        if wp is None or wp.mode != "shadow" or not wp.is_db or norm(host) != wp.host:
            return legacy
        res = LP.refresh_key(wp.view(), wait_s=0)
        if res.status == "expired":
            _mm("PLAN_WOULD_EXPIRE", "401", res.why, f"legacy retry={legacy}", job=_job(), att=_att(wp),
                where="worker.401", kind="info")
        elif (res.status == "ok") != bool(legacy):
            _mm("KEY_DRIFT", "401.rotation", res.status, f"legacy retry={legacy}", job=_job(), att=_att(wp),
                where="worker.401")
    except Exception:        # noqa: BLE001
        pass
    return legacy


# ---- conformance records
def _recorder():
    if _REC[0] is None:
        _REC[0] = CL.Recorder(directory=conf_dir())
    return _REC[0]


_N = [0]


def override_names(**overrides):
    """Body field names the CALLER deliberately set (non-None). Both bare and ollama `options.` spellings."""
    names = []
    amap = {"repeat_penalty": ("repetition_penalty", "repeat_penalty"),
            "repetition_penalty": ("repetition_penalty", "repeat_penalty"),
            "think": ("think", "chat_template_kwargs", "chat_template_kwargs.enable_thinking",
                      "chat_template_kwargs.preserve_thinking")}
    for k, v in overrides.items():
        if v is None:
            continue
        for n in amap.get(k, (k,)):
            names += [n, "options." + n]
    return names


def _from_raw(raw, api):
    """(usage, finish_reason, echoed model) from a parsed response body; all None on anything odd."""
    try:
        if not isinstance(raw, dict):
            return None, None, None
        if api == "openai":
            ch = (raw.get("choices") or [{}])[0]
            return raw.get("usage"), ch.get("finish_reason"), raw.get("model")
        return ({"prompt_tokens": raw.get("prompt_eval_count"), "completion_tokens": raw.get("eval_count")},
                raw.get("done_reason"), raw.get("model"))
    except Exception:        # noqa: BLE001
        return None, None, None


def rec(*, host, path, api, model, body, req=None, role=None, status=None, t0=None, usage=None, fin=None,
        rmodel=None, step=0, ov=None, raw=None):
    """One conformance record for one request on the wire. NEVER raises, never blocks the request.
    `req`: the urllib Request (its Authorization header is turned into a key fingerprint; the key is
    scrubbed from the line even if it somehow reached a field)."""
    try:
        if not CL.log_enabled():
            return False
        wp = _WP[0]
        job = _job()
        if wp is None and not job:
            return False                                # a direct/test run: nothing to attribute to
        if wp is not None and wp.mode == "off":
            return False
        key = None
        if req is not None:
            h = req.get_header("Authorization") or ""
            key = h[7:] if h.startswith("Bearer ") and len(h) > 7 else None
        if raw is not None:
            usage, fin, rmodel = _from_raw(raw, api)
        u = usage if isinstance(usage, dict) else {}
        _N[0] += 1
        ms = None if t0 is None else int((time.monotonic() - t0) * 1000)
        ev = None
        if wp is not None and wp.ev:
            ev, wp.ev = wp.ev, None
        return _recorder().record(
            body=body, key=key, job=job, att=_att(wp), plan=(wp.plan if wp else None), n=_N[0], role=role,
            host=host, path=path, api=api, model=model, status=status, ms=ms,
            ptok=u.get("prompt_tokens"), ctok=u.get("completion_tokens"), fin=fin, rmodel=rmodel,
            ev=ev, ov=ov, step=step)
    except Exception:        # noqa: BLE001
        return False


def summary(metrics):
    """End of a dispatch: how many chat calls the loop made (the reap comparison's GAP check). Never raises."""
    try:
        if not CL.log_enabled():
            return False
        job = _job()
        wp = _WP[0]
        if not job and wp is None:
            return False
        return CL.write_summary({"job": job, "att": _att(wp), "calls": int((metrics or {}).get("calls") or 0),
                                 "recorded": _N[0]}, directory=conf_dir())
    except Exception:        # noqa: BLE001
        return False


if __name__ == "__main__":
    print(f"plan_runtime mode={mode()} worker_mode={worker_mode()} key_fd={key_fd_enabled()} plans_dir={PLANS_DIR}")
