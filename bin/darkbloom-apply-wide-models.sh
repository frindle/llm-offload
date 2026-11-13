#!/bin/bash
# darkbloom-apply-wide-models.sh -- widen the set of models Darkbloom serves WITHOUT losing the
# queue's two models (Qwen3.5-9B + qwen3.6-35b-a3b-vl-mtp-mxfp8). READY TO RUN, NOT EXECUTED by
# whoever wrote it: nothing happens unless you pass --apply.
#
#   darkbloom-apply-wide-models.sh --models "gemma-4-26b-qat-4bit,Foo-Model"      # dry run (plan only)
#   darkbloom-apply-wide-models.sh --models "..." --apply                         # do it
#   darkbloom-apply-wide-models.sh --rollback                                     # restore the last snapshot
#   darkbloom-apply-wide-models.sh --pair-config [--apply]                        # only normalise provider.toml for the pair
#                                    (slots=2, idle_timeout_mins=0, preload_models canonical case); dry run by default
# NOTE (2026-10-08): the queue now widens/narrows automatically (bloom_control.py hold = pair ONLY while
# the queue has work). This script remains for manual one-off widening and for --pair-config.
#
# What --apply does:
#   1. REFUSES if any queue job is running (or the queue state is unreadable). Pending jobs only warn
#      (add --force-pending to silence). Drain-aware: wait for the queue to go idle and re-run.
#   2. Snapshots provider.toml + the current enabled_models to ~/.ollama-dispatch/backups/darkbloom-wide-<ts>/.
#   3. `darkbloom start --model <each> --local-endpoint` with the candidate set (queue pair always included).
#   4. Verifies provider.toml enabled_models, /health, /v1/models list every model, and BOTH queue
#      models are resident in loaded-models.json (nudging with 1-token local requests, resident first,
#      so a nudge never evicts the other queue model).
#   5. Any failure -> rolls back: `darkbloom start` with the ORIGINAL set and re-verifies it.
# NOTE (eviction): with max_model_slots=2 only two models are resident at once; extra models load
# on request and evict the LEAST-RECENTLY-USED. Run darkbloom-keepwarm.py (enable its kill switch) so
# the queue models are re-warmed after any eviction. Never prints the local API key.
#
# Env for tests/overrides: DBAPPLY_BIN, DBAPPLY_TOML, DBAPPLY_LOCAL_JSON, DBAPPLY_LOADED_JSON,
# DBAPPLY_QUEUE_STATE, DBAPPLY_BACKUPS, DBAPPLY_WAIT_S, DBAPPLY_PAIR ("a,b").
# No heredoc (2026-10-08): bash 5.3 deadlocks writing a >pipe-size heredoc when macOS shrinks pipes to
# 512 B under kernel pipe-memory pressure. The program is read from THIS file, after the marker line.
exec python3 -c 'import sys; s = open(sys.argv[1]).read().split("\n#@@PYBODY@@\n", 1)[1]; sys.argv = ["darkbloom-apply-wide-models"] + sys.argv[2:]; exec(compile(s, sys.argv[0], "exec"))' "$0" "$@"
#@@PYBODY@@
import argparse, json, os, re, shutil, subprocess, sys, time, urllib.request
from pathlib import Path

H = Path.home()
E = os.environ.get
BIN = E("DBAPPLY_BIN", str(H / ".darkbloom/bin/darkbloom"))
TOML = Path(E("DBAPPLY_TOML", str(H / ".config/darkbloom/provider.toml")))
LOCAL = Path(E("DBAPPLY_LOCAL_JSON", str(H / ".darkbloom/local.json")))
LOADED = Path(E("DBAPPLY_LOADED_JSON", str(H / ".darkbloom/loaded-models.json")))
QSTATE = Path(E("DBAPPLY_QUEUE_STATE", str(H / "bin/ollama-queue-state.json")))
BACKUPS = Path(E("DBAPPLY_BACKUPS", str(H / ".ollama-dispatch/backups")))
WAIT = float(E("DBAPPLY_WAIT_S", "300"))
PAIR = [m for m in E("DBAPPLY_PAIR", "Qwen3.5-9B,qwen3.6-35b-a3b-vl-mtp-mxfp8").split(",") if m]
SECRET = re.compile(r"[A-Za-z0-9_\-]{28,}")


def say(m):
    print(SECRET.sub("[redacted]", str(m)), flush=True)


def norm(xs):
    return sorted(x.lower() for x in xs)


def toml_enabled():
    out = []
    try:
        for m in re.finditer(r"(?m)^\s*enabled_models\s*=\s*\[([^\]]*)\]", TOML.read_text()):
            out += re.findall(r"['\"]([^'\"]+)['\"]", m.group(1))
    except OSError:
        pass
    return out


def base_key():
    sys.path.insert(0, E("DBAPPLY_CODE_DIR", str(H / "bin")))
    import queue_config
    try:
        j = queue_config.darkbloom_apply_env(json.loads(LOCAL.read_text()))   # env DARKBLOOM_* laid over local.json
        base = str(j.get("base_url") or queue_config.darkbloom_default_base()).rstrip("/")
        if base.endswith("/v1"):    # real local.json carries ".../v1"; every caller appends "/v1/..." itself
            base = base[:-3]
        return base, str(j.get("api_key") or j.get("key") or "")
    except (OSError, ValueError):
        env = queue_config.darkbloom_apply_env({})
        return str(env.get("base_url") or "http://127.0.0.1:8000").rstrip("/"), str(env.get("api_key") or "")


def http(path, body=None, timeout=20):
    base, key = base_key()
    req = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None,
                                 method="POST" if body is not None else "GET",
                                 headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.status, json.loads(r.read() or b"{}")


def health():
    try:
        return http("/health", timeout=5)[0] == 200
    except Exception:
        return False


def listed():
    try:
        return [d.get("id") for d in http("/v1/models", timeout=8)[1].get("data", [])]
    except Exception:
        return None


def resident():
    try:
        return [str(x) for x in json.loads(LOADED.read_text()).get("models", [])]
    except (OSError, ValueError, AttributeError):
        return []


def queue_jobs():
    """-> (running, pending) counts, or None if the queue state is unreadable (treated as busy)."""
    try:
        if str(H / "bin") not in sys.path:
            sys.path.append(str(H / "bin"))
        import queue_config  # the state file here, GET /api/state when the queue runs elsewhere
        st = queue_config.load_queue_state(QSTATE)
        jobs = st.get("jobs", []) if isinstance(st, dict) else None
    except (ImportError, AttributeError):
        return None
    if jobs is None:
        return None
    if isinstance(jobs, dict):
        jobs = list(jobs.values())
    run = sum(1 for j in jobs if isinstance(j, dict) and j.get("status") == "running")
    pend = sum(1 for j in jobs if isinstance(j, dict) and j.get("status") in ("pending", "queued", "paused"))
    return run, pend


def start(models):
    cmd = [BIN, "start"] + [a for m in models for a in ("--model", m)] + ["--local-endpoint"]
    say("RUN: darkbloom start " + " ".join("--model %s" % m for m in models) + " --local-endpoint")
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    tail = (r.stdout + r.stderr).strip().splitlines()[-3:]
    for l in tail:
        say("  darkbloom: " + l)
    return r.returncode == 0


def verify(models, queue_models):
    """-> (ok, why). Filter applied, serving, and every queue model resident."""
    end = time.time() + WAIT
    why = "timeout"
    while time.time() < end:
        if norm(toml_enabled()) != norm(models):
            why = "provider.toml enabled_models=%s" % toml_enabled()
        elif not health():
            why = "/health not ok"
        else:
            ls = listed()
            if ls is None or not set(norm(models)) <= set(norm(ls)):
                why = "/v1/models=%s" % ls
            else:
                res = norm(resident())
                missing = [m for m in queue_models if m.lower() not in res]
                if not missing:
                    return True, "ok"
                why = "queue model(s) not resident: %s" % missing
                # nudge: touch residents first so a load can never evict the other queue model
                order = [m for m in queue_models if m.lower() in res] + missing
                for m in order:
                    try:
                        http("/v1/chat/completions", {"model": m, "messages": [{"role": "user", "content": "ping"}],
                                                      "max_tokens": 1, "temperature": 0}, timeout=240)
                    except Exception as e:
                        why += " (nudge %s failed: %s)" % (m, str(e)[:60])
        time.sleep(float(E("DBAPPLY_POLL_S", "5")))
    return False, why


def snapshot():
    d = BACKUPS / ("darkbloom-wide-" + time.strftime("%Y%m%dT%H%M%SZ", time.gmtime()))
    d.mkdir(parents=True, exist_ok=True)
    if TOML.exists():
        shutil.copy2(TOML, d / "provider.toml")
    (d / "snapshot.json").write_text(json.dumps({"enabled_models": toml_enabled(), "ts": time.time()}))
    (BACKUPS / "darkbloom-wide-last").write_text(str(d))
    return d


def rollback(orig, queue_models, why):
    say("ROLLBACK (%s): restoring %s" % (why, orig))
    if not orig:
        say("ROLLBACK impossible: original enabled_models unknown; restore provider.toml from the snapshot by hand")
        return 3
    if not start(orig):
        say("ROLLBACK start FAILED -- Darkbloom state needs manual attention")
        return 3
    ok, w = verify(orig, [m for m in queue_models if m.lower() in norm(orig)])
    say("ROLLBACK %s (%s)" % ("verified" if ok else "NOT VERIFIED", w))
    return 2 if ok else 3


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="", help="comma list of extra models to serve (queue pair always included)")
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--rollback", action="store_true")
    ap.add_argument("--force-pending", action="store_true")
    ap.add_argument("--pair-config", action="store_true",
                    help="show (dry run) / write (--apply) ONLY the provider.toml normalisation the queue's pair "
                         "needs: max_model_slots=2, idle_timeout_mins=0, preload_models=[pair, canonical case]")
    a = ap.parse_args()
    if (a.apply or a.rollback) and not E("DBAPPLY_BIN") and E("DARKBLOOM_BY_OWNER") != "1":
        say("REFUSED: retired: BloomGauge runs Darkbloom and the queue's BloomGauge control picks models; a hand-run darkbloom/BloomGauge change fights it and spends BloomGauge's hourly change budget. The owner only: DARKBLOOM_BY_OWNER=1")
        return 2
    qj = queue_jobs()
    if a.apply or a.rollback:
        if qj is None:
            say("REFUSED: queue state unreadable (%s); treating as busy" % QSTATE)
            return 1
        if qj[0] > 0:
            say("REFUSED: %d queue job(s) running; wait for the queue to drain and re-run" % qj[0])
            return 1
        if qj[1] and not a.force_pending:
            say("WARN: %d job(s) pending; a reload pauses their launch briefly (--force-pending to silence)" % qj[1])
    if a.rollback:
        p = BACKUPS / "darkbloom-wide-last"
        try:
            snap = json.loads((Path(p.read_text().strip()) / "snapshot.json").read_text())
        except (OSError, ValueError):
            say("no snapshot found at %s" % p)
            return 1
        return rollback(snap["enabled_models"], PAIR, "requested") if qj is not None else 1
    if a.pair_config:
        sys.path.insert(0, str(H / "bin"))
        import bloom_control as bc
        new, changes = bc.desired_toml(TOML.read_text(), PAIR)
        say("provider.toml normalisation for the pair %s:" % PAIR)
        for c in changes or ["(already normalised)"]:
            say("  " + c)
        if not a.apply or not changes:
            say("DRY RUN: nothing changed." if changes else "nothing to do.")
            return 0
        snap = snapshot()
        TOML.write_text(new)
        say("WROTE provider.toml (snapshot %s). Takes effect at the next `darkbloom start` "
            "(bloom_control hold does that)." % snap)
        return 0
    extra = [m.strip() for m in a.models.split(",") if m.strip()]
    if not extra:
        say("pass --models 'A,B,...' (the extra models to serve); nothing to do")
        return 1
    seen, models = set(), []
    for m in PAIR + extra:
        if m.lower() not in seen:
            seen.add(m.lower())
            models.append(m)
    orig = toml_enabled()
    say("current enabled_models : %s" % orig)
    say("candidate (pair kept) : %s" % models)
    say("max_model_slots caveat: only 2 models stay resident; extras evict the LRU on request (run darkbloom-keepwarm)")
    if not a.apply:
        say("DRY RUN: nothing changed. Re-run with --apply.")
        return 0
    if not health():
        say("REFUSED: Darkbloom /health not ok; not applying onto an unhealthy provider")
        return 1
    snap = snapshot()
    say("snapshot: %s" % snap)
    if not start(models):
        return rollback(orig, PAIR, "darkbloom start failed")
    ok, why = verify(models, PAIR)
    if not ok:
        return rollback(orig, PAIR, why)
    say("APPLIED + VERIFIED: filter=%s, queue models resident" % models)
    return 0


sys.exit(main())
