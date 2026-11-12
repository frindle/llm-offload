#!/usr/bin/env python3
"""cpu_dispatch.py -- thin wrapper the dispatch pipeline calls to run a CPU-only stage
(verify, preflight both-ways, relevance mutation, harness self-check, slicer scaffolding)
on the Unraid CPU runner, with a local-subprocess fallback.

    from cpu_dispatch import run_cpu_stage
    r = run_cpu_stage(worktree, "bash verify.sh", timeout_s=300, stage="final-verify",
                      bundle_id=<queue bundle key>, tools={"verify-relevance.py": path})
    r.exit_code, r.timed_out, r.stdout_tail, r.stderr_tail, r.ran_on, r.fell_back

Why bundle_id matters: while the stage runs (remote OR local) the queue daemon sees it
(cpu_lane.outstanding_by_bundle) and releases the GPU lanes / bundle commitment to other
bundles; the bundle resumes first when the result lands. Pass the SAME bundle key the
queue rows carry (job["bundle"]) or the lane-release does nothing for that stage.

Remote is used only when it can actually run: CPU_DISPATCH_REMOTE != 0, the dashboard API
answers, and a runner has polled in the last RUNNER_FRESH_S. Otherwise (or on any runner /
network failure) the stage runs locally with identical timeout semantics and a local
marker is registered so it still does not hold other bundles' lanes. Never raises for
infrastructure reasons; ran_on / fell_back / infra_error say what happened.

Config: CPU_RUNNER_API (default http://127.0.0.1:7684 -- the producer is the Mac itself),
CPU_RUNNER_TOKEN_FILE (default ~/.config/dispatch-cpu-runner/token), CPU_DISPATCH_REMOTE=0
to force local.
"""
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER_REPO_CLIENT = Path.home() / "Desktop" / "GitHub Projects" / "dispatch-cpu-runner" / "client"
RUNNER_FRESH_S = 120
DEFAULT_API = "http://127.0.0.1:7684"


def _load_client():
    """cpu_job from ~/bin (vendored copy) first, else the runner repo checkout."""
    for d in (HERE, RUNNER_REPO_CLIENT):
        if (d / "cpu_job.py").exists():
            if str(d) not in sys.path:
                sys.path.insert(0, str(d))
            import cpu_job            # noqa: PLC0415
            return cpu_job
    return None


def _runner_online(api, token):
    """True when the API answers and at least one runner polled within RUNNER_FRESH_S."""
    try:
        req = urllib.request.Request(api.rstrip("/") + "/api/cpu/runners")
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=4) as r:
            runners = json.loads(r.read()).get("runners") or []
        return any(float(x.get("seen_s_ago", 1e9)) < RUNNER_FRESH_S for x in runners)
    except Exception:
        return False


def _sat_wait():
    try:
        return float(os.environ.get("CPU_LANE_SATURATION_WAIT_S", 1200))
    except ValueError:
        return 1200.0


LEGACY_NODE = 22     # what a runner that reports no caps (pre-1.3 image) is known to have


def combine_caps(runners):
    """Conservative capability set for a list of fresh runners (any may take the job): the weakest value of each
    capability. A runner with no caps is the legacy image (node 22, no job python, no sqlite3). None for no runners."""
    if not runners:
        return None
    cs = [r.get("caps") if isinstance(r.get("caps"), dict) else {} for r in runners]
    nodes = [c.get("node") if isinstance(c.get("node"), int) else LEGACY_NODE for c in cs]
    pys = {c.get("python") for c in cs}
    mods = [set(c.get("py_modules") or ()) for c in cs]
    return {"node": min(nodes),
            "python": pys.pop() if len(pys) == 1 and None not in pys else None,
            "py_modules": sorted(set.intersection(*mods)) if mods else [],
            "sqlite3": all(bool(c.get("sqlite3")) for c in cs),
            "subpkg_install": all(bool(c.get("subpkg_install")) for c in cs),
            "runners": len(cs), "legacy": sum(1 for c in cs if not c)}


def _runner_caps(api, token):
    """combine_caps() over the runners that polled within RUNNER_FRESH_S; None when unknown (API trouble,
    no runners) -> callers treat that exactly like a legacy runner. Never raises."""
    try:
        req = urllib.request.Request(api.rstrip("/") + "/api/cpu/runners")
        req.add_header("Authorization", "Bearer " + token)
        with urllib.request.urlopen(req, timeout=4) as r:
            runners = json.loads(r.read()).get("runners") or []
        return combine_caps([x for x in runners if float(x.get("seen_s_ago", 1e9)) < RUNNER_FRESH_S])
    except Exception:
        return None


def _marker(stage, bundle_id, timeout_s):
    """(end_fn) registering a local-stage marker so the queue sees it. Best effort."""
    try:
        if str(HERE) not in sys.path:
            sys.path.insert(0, str(HERE))
        import cpu_lane              # noqa: PLC0415
        s = cpu_lane.Store(cpu_lane.BASE)
        jid = s.begin_local(stage, bundle_id, timeout_s)
        return lambda code=None: s.end_local(jid, code)
    except Exception:
        return lambda code=None: None


def _owner(worktree, stage):
    """Display-only owner label (cpu_stage.owner_label); '' on any trouble (fail-open)."""
    try:
        import cpu_stage
        return cpu_stage.owner_label(worktree, stage)
    except Exception:  # noqa: BLE001
        return ""


def run_cpu_stage(worktree, cmd, timeout_s, stage, bundle_id, lockfile_hash=None, tools=None,
                  cwd_rel=None, env=None, label=None, mode="bundle", claim_timeout_s=60):
    cj = _load_client()
    if cj is None:
        raise RuntimeError("cpu_job client library not found (expected %s or %s)" % (HERE, RUNNER_REPO_CLIENT))
    api = os.environ.get("CPU_RUNNER_API") or DEFAULT_API
    why = None
    if os.environ.get("CPU_DISPATCH_REMOTE", "1") == "0":
        why = "CPU_DISPATCH_REMOTE=0"
    elif not _runner_online(api, cj._token()):
        why = "no CPU runner online"
    if why is None:
        try:
            return cj.submit_cpu_job(worktree, cmd, timeout_s, cwd_rel=cwd_rel, env=env, stage=stage,
                                     label=label or stage, bundle_id=bundle_id, tools=tools, mode=mode,
                                     api=api, claim_timeout_s=max(claim_timeout_s, _sat_wait()), fallback=False,
                                     should_abort=lambda: None if _runner_online(api, cj._token()) else "runner offline (heartbeat stale) while waiting",
                                     lockfile_hash=lockfile_hash, owner=_owner(worktree, stage))
        except RuntimeError as e:
            why = str(e)
        except Exception as e:          # noqa: BLE001 -- the pipeline must not die on lane trouble
            why = "%s: %s" % (type(e).__name__, e)
    end = _marker(stage, bundle_id, timeout_s)
    code = None
    try:
        r = cj.run_local(os.path.abspath(worktree), cmd, timeout_s, cwd_rel, env)
        code = r.exit_code
    finally:
        end(code)
    r.fell_back, r.infra_error = True, why
    return r


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="run a CPU stage on the runner (local fallback)")
    ap.add_argument("worktree")
    ap.add_argument("cmd")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--stage", default="adhoc")
    ap.add_argument("--bundle", default=None)
    a = ap.parse_args()
    res = run_cpu_stage(a.worktree, a.cmd, a.timeout, a.stage, a.bundle)
    sys.stdout.write(res.stdout_tail)
    sys.stderr.write(res.stderr_tail)
    print("[cpu_dispatch] ran_on=%s exit=%s fell_back=%s %s" % (res.ran_on, res.exit_code, res.fell_back,
                                                               res.infra_error or ""), file=sys.stderr)
    sys.exit(res.exit_code if res.exit_code is not None else 1)
