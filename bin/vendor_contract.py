#!/usr/bin/env python3
"""vendor_contract: OFFLINE / metadata-only tier of the vendor contract tests (design section 5).

Checks that the Darkbloom FILES and CLI SHAPE the pipeline depends on still look the way the
code assumes, names the consumer each check protects, and detects the installed vendor version
from files (no CLI call needed) to compare against the recorded known-good versions.

  * no network, no model calls, no inference, no writes
  * never prints or retains a secret: key checks record only type and length class
  * CLI text (`darkbloom --version`, `start --help`, `--help`, `status`) is INJECTED by the caller
    (`cli_texts`) or collected by `collect_cli()` only when asked; tests are stub-driven
  * meta (GET-only) and inference tiers are NOT here; they belong to the probe (design section 2)

Library:   run_offline(root_or_sources, cli_texts=None) -> list[Result]
           detect_versions(...) / version_status(v) / summarize(results)
CLI:       python3 vendor_contract.py [--root DIR] [--cli] [--json]     (exit 1 on any FAIL)

Wave 0 note: reads the raw vendor files itself because shape checks need the raw bytes/keys that
lane_plan abstracts away. This file must therefore be on the Wave 1 `noreread` allowlist.
"""
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import lane_plan as _lp   # noqa: E402  (shared _read/_json_obj + Sources; same single-reader helpers)

try:
    import tomllib as _toml
except ImportError:       # py < 3.11
    _toml = None

# Recorded known-good versions. `local_json_base_url`: True = record carries base_url,
# False = api_key-only (0.9.17 dropped it; consumers fall back to the fixed local port).
# `note` cites where the assumption is encoded.
KNOWN_GOOD = {
    "0.9.14": {"local_json_base_url": True, "note": "ollama-queue.py ~5085 'verified 0.9.14'"},
    "0.9.17": {"local_json_base_url": False, "note": "ollama-queue.py ~5131 base_url dropped; _darkbloom_base_from_record"},
    "0.9.20": {"local_json_base_url": True, "note": "live 2026-10-13: local.json + daemon-state.json both 0.9.20"},
}

START_FLAGS = ("--model", "--local-endpoint", "--idle-timeout", "--timeout", "--force")   # bloom_control
SUBCOMMANDS = ("start", "switch", "stop", "restart", "status", "update")
DAEMON_CAPACITY_KEYS = ("load_usable_gb", "load_headroom_gb")
DAEMON_TOP_KEYS = ("written_at", "advertised_models", "last_model_load_error", "warm_models",
                   "load_transition_active", "capacity", "autopilot_state", "version", "process_identity")
RESIDENT_KEYS = ("model_id", "idle_seconds")
TOML_BACKEND_KEYS = ("model_cache_directory", "max_model_slots", "idle_timeout_mins")


class Result(dict):
    """{id, vendor, tier, status: pass|fail|skip, detail, consumers}"""


def _res(cid, status, detail, consumers, tier="offline"):
    return Result(id=cid, vendor="darkbloom", tier=tier, status=status, detail=detail,
                  consumers=list(consumers))


# ---------------------------------------------------------------- version logic
def _vtuple(v):
    m = re.fullmatch(r"\s*v?(\d+)\.(\d+)\.(\d+)\s*", str(v or ""))
    return tuple(int(x) for x in m.groups()) if m else None


def version_status(v, known=None):
    """PURE. 'known' | 'newer' (than the newest known-good: re-run contracts + probe) | 'older'
    | 'between' (unrecorded version inside the known range) | 'unparsable' | 'missing'."""
    known = KNOWN_GOOD if known is None else known
    if not v:
        return "missing"
    t = _vtuple(v)
    if t is None:
        return "unparsable"
    if str(v).strip().lstrip("v") in known:
        return "known"
    ts = sorted(_vtuple(k) for k in known)
    if t > ts[-1]:
        return "newer"
    if t < ts[0]:
        return "older"
    return "between"


def version_changed(prev, cur):
    """PURE. True when the vendor version differs (patch included: 0.9.20 -> 0.9.21 is a change).
    None/empty on either side is never a change (unknown != changed)."""
    return bool(prev) and bool(cur) and str(prev).strip() != str(cur).strip()


def detect_versions(ctx):
    """Versions as each source reports them: {'local_json','daemon_state','cli'} -> str|None."""
    cli = ctx["cli"].get("version") or ""
    m = re.search(r"(\d+\.\d+\.\d+)", cli)
    return {"local_json": (ctx["local"] or {}).get("version"),
            "daemon_state": (ctx["daemon"] or {}).get("version"),
            "cli": m.group(1) if m else None}


def detect_version(ctx):
    """Best single version from files (local.json first, then daemon-state); CLI only if no file."""
    v = detect_versions(ctx)
    return v["local_json"] or v["daemon_state"] or v["cli"]


# ---------------------------------------------------------------- context
def load_context(root=None, cli_texts=None, sources=None):
    """Read the vendor files once (read-only). `root`: a directory laid out like the lane_plan
    test sandbox (local.json, daemon-state.json, loaded-models.json, provider.toml, ...).
    The api_key VALUE is dropped here: ctx['key_info'] keeps only type/length."""
    src = sources or (_lp.Sources.under(root) if root else _lp.Sources())
    ls, local = _lp._json_obj(src.local_json)
    ds, daemon = _lp._json_obj(src.daemon_state)
    lms, loaded = _lp._json_obj(src.loaded_models)
    ts, toml_text = _lp._read(src.provider_toml)
    try:
        mode = stat.S_IMODE(src.local_json.stat().st_mode)
    except OSError:
        mode = None
    key = (local or {}).get("api_key")
    key_info = {"type": type(key).__name__, "len": len(key) if isinstance(key, str) else 0} if "api_key" in (local or {}) else None
    if local is not None:
        local = {k: v for k, v in local.items() if k != "api_key"}    # secret never leaves this function
        local["_has_api_key"] = key_info is not None
    return {"src": src, "local_status": ls, "local": local, "local_mode": mode, "key_info": key_info,
            "daemon_status": ds, "daemon": daemon, "loaded_status": lms, "loaded": loaded,
            "toml_status": ts, "toml_text": toml_text, "cli": dict(cli_texts or {})}


def collect_cli(runner=None, timeout=15):
    """Run the harmless local CLI (`--version`, `--help`, `start --help`, `status`). `runner(argv)`
    -> text is injectable; default shells out to `darkbloom`. Failures give '' (check -> skip)."""
    def default(argv):
        try:
            r = subprocess.run(["darkbloom", *argv], capture_output=True, text=True, timeout=timeout)
            return (r.stdout or "") + (r.stderr or "")
        except (OSError, subprocess.TimeoutExpired):
            return ""
    run = runner or default
    return {"version": run(["--version"]), "help": run(["--help"]),
            "start_help": run(["start", "--help"]), "status": run(["status"])}


# ---------------------------------------------------------------- checks
def _get(d, path):
    cur = d
    for p in path.split("."):
        if not isinstance(cur, dict) or p not in cur:
            return None, False
        cur = cur[p]
    return cur, True


def _c_local_shape(ctx):
    cons = ["ollama-queue.py:_darkbloom_base_from_record", "ollama-worker.py:_darkbloom_auth_headers",
            "darkbloom_chat.py:base_from_record", "lane_plan.py:take_snapshot"]
    out = []
    if ctx["local_status"] != "ok":
        return [_res("db.local.parses", "fail", f"local.json {ctx['local_status']}", cons)]
    L = ctx["local"]
    out.append(_res("db.local.parses", "pass", "object", cons))
    ki = ctx["key_info"]
    out.append(_res("db.local.api_key", "pass" if ki and ki["type"] == "str" and ki["len"] >= 20 else "fail",
                    "missing" if not ki else f"type={ki['type']} len>=20={ki['len'] >= 20}", cons))
    for k, t in (("port", int), ("pid", int), ("version", str)):
        ok = isinstance(L.get(k), t) and not isinstance(L.get(k), bool)
        out.append(_res(f"db.local.{k}", "pass" if ok else "fail", f"{k}: {type(L.get(k)).__name__}", cons))
    bu = L.get("base_url")
    if bu is None:
        v = L.get("version")
        rec = KNOWN_GOOD.get(v or "", {})
        out.append(_res("db.local.base_url", "pass" if L.get("_has_api_key") else "fail",
                        "absent (api_key-only record; consumers use the fixed local port)"
                        if L.get("_has_api_key") else "absent AND no api_key: Darkbloom looks off", cons))
    else:
        ok = isinstance(bu, str) and re.fullmatch(r"https?://[^/\s]+(:\d+)?(/v1)?/?", bu) is not None
        out.append(_res("db.local.base_url", "pass" if ok else "fail", f"shape ok={ok}", cons))
    m = ctx["local_mode"]
    out.append(_res("db.local.mode_0600", "pass" if m == 0o600 else "fail",
                    f"mode={oct(m) if m is not None else None}", ["lane_plan.py (key at rest)"]))
    return out


def _c_pid(ctx):
    cons = ["lane_plan.py:take_snapshot (restart detection)"]
    if ctx["local_status"] != "ok" or ctx["daemon_status"] != "ok":
        return [_res("db.local.pid_matches_daemon", "skip", "local.json or daemon-state.json unavailable", cons)]
    lp = (ctx["local"] or {}).get("pid")
    pi = (ctx["daemon"] or {}).get("process_identity")
    dp = pi.get("pid") if isinstance(pi, dict) else None
    return [_res("db.local.pid_matches_daemon", "pass" if lp is not None and lp == dp else "fail",
                 f"local.pid={lp} daemon.process_identity.pid={dp}", cons)]


def _c_daemon(ctx):
    cons = ["bloom_control.py:provider_snapshot", "lane_plan.py:take_snapshot"]
    if ctx["daemon_status"] != "ok":
        return [_res("db.daemon.parses", "fail", f"daemon-state.json {ctx['daemon_status']}", cons)]
    D = ctx["daemon"]
    out = [_res("db.daemon.schema", "pass" if D.get("schema") == 1 else "fail", f"schema={D.get('schema')!r}", cons)]
    missing = [k for k in DAEMON_TOP_KEYS if k not in D]
    out.append(_res("db.daemon.top_keys", "pass" if not missing else "fail", f"missing={missing}", cons))
    cap = D.get("capacity") if isinstance(D.get("capacity"), dict) else {}
    mc = [k for k in DAEMON_CAPACITY_KEYS if not isinstance(cap.get(k), (int, float)) or isinstance(cap.get(k), bool)]
    out.append(_res("db.daemon.capacity", "pass" if not mc else "fail", f"bad/missing={mc}", cons))
    ap = D.get("autopilot_state") if isinstance(D.get("autopilot_state"), dict) else {}
    bad = [] if isinstance(ap.get("max_model_slots"), int) else ["max_model_slots"]
    rm = ap.get("resident_models")
    if not isinstance(rm, list):
        bad.append("resident_models")
    else:
        for i, r in enumerate(rm):
            if not isinstance(r, dict) or any(k not in r for k in RESIDENT_KEYS) \
                    or not any(k in r for k in ("resident_gb", "weights_gb")):
                bad.append(f"resident_models[{i}]")
    out.append(_res("db.daemon.autopilot", "pass" if not bad else "fail", f"bad={bad}", cons))
    adv = D.get("advertised_models")
    out.append(_res("db.daemon.advertised", "pass" if isinstance(adv, list) and all(isinstance(x, str) for x in adv) else "fail",
                    f"type={type(adv).__name__}", cons))
    return out


def _c_loaded(ctx):
    cons = ["ollama-queue.py:_darkbloom_served", "model_profile.py:served_exact_ids", "lane_plan.py:served_from_texts"]
    if ctx["loaded_status"] != "ok":
        return [_res("db.loaded.parses", "fail", f"loaded-models.json {ctx['loaded_status']}", cons)]
    L = ctx["loaded"]
    ok_m = isinstance(L.get("models"), list) and all(isinstance(x, str) for x in L["models"])
    return [_res("db.loaded.schema", "pass" if L.get("schema") == 1 else "fail", f"schema={L.get('schema')!r}", cons),
            _res("db.loaded.models", "pass" if ok_m else "fail", f"models: {type(L.get('models')).__name__}", cons)]


def _c_toml(ctx):
    cons = ["ollama-queue.py:_darkbloom_served (regex)", "ollama-worker.py:_is_darkbloom_only_model (regex)",
            "model_profile.py:served_exact_ids (regex)", "lane_plan.py:served_from_texts (regex)",
            "bloom_control.py (provider.toml editor)"]
    if ctx["toml_status"] != "ok":
        return [_res("db.toml.readable", "fail", f"provider.toml {ctx['toml_status']}", cons)]
    if _toml is None:
        return [_res("db.toml.parses", "skip", "tomllib unavailable (python < 3.11)", cons)]
    try:
        T = _toml.loads(ctx["toml_text"])
    except Exception as e:      # noqa: BLE001
        return [_res("db.toml.parses", "fail", f"TOMLDecodeError: {str(e)[:80]}", cons)]
    out = [_res("db.toml.parses", "pass", "tomllib ok", cons)]
    be = T.get("backend") if isinstance(T.get("backend"), dict) else {}
    parsed = list(dict.fromkeys([*(be.get("enabled_models") or []), *(be.get("preload_models") or [])]))
    regex = _lp.served_from_texts(ctx["toml_text"], None)
    out.append(_res("db.toml.regex_equals_tomllib", "pass" if set(parsed) == set(regex) else "fail",
                    f"tomllib={sorted(parsed)} regex={sorted(regex)}", cons))
    miss = [k for k in TOML_BACKEND_KEYS if k not in be]
    out.append(_res("db.toml.backend_keys", "pass" if not miss else "fail", f"missing={miss}", cons[-1:]))
    return out


def _c_versions(ctx):
    cons = ["ollama-queue.py:parse_darkbloom_status", "lane_plan.py (gen.version)", "vendor version watch"]
    v = detect_versions(ctx)
    out = []
    a, b = v["local_json"], v["daemon_state"]
    if a and b:
        out.append(_res("db.version.files_agree", "pass" if a == b else "fail", f"local.json={a} daemon-state={b}", cons))
    else:
        out.append(_res("db.version.files_agree", "skip", f"local.json={a} daemon-state={b}", cons))
    cur = detect_version(ctx)
    st = version_status(cur)
    out.append(_res("db.version.known_good", "pass" if st == "known" else ("skip" if st == "missing" else "fail"),
                    f"version={cur} status={st} known={sorted(KNOWN_GOOD)}", cons))
    # per-version shape expectation (0.9.17 api_key-only vs base_url)
    rec = KNOWN_GOOD.get(cur or "")
    if rec and ctx["local_status"] == "ok":
        has_base = (ctx["local"] or {}).get("base_url") is not None
        out.append(_res("db.version.shape_matches_recorded", "pass" if has_base == rec["local_json_base_url"] else "fail",
                        f"recorded base_url={rec['local_json_base_url']} actual={has_base}", cons))
    return out


def _c_cli(ctx):
    cons = ["bloom_control.py (start/switch/stop/restart flags)", "ollama-queue.py:parse_darkbloom_status"]
    cli = ctx["cli"]
    out = []
    if not cli:
        return [_res("db.cli.*", "skip", "no CLI text supplied (offline-files-only run)", cons)]
    if cli.get("version"):
        m = re.search(r"(\d+\.\d+\.\d+)", cli["version"])
        files = detect_version({**ctx, "cli": {}})
        ok = bool(m) and (not files or m.group(1) == files)
        out.append(_res("db.cli.version_matches_files", "pass" if ok else "fail",
                        f"cli={m.group(1) if m else None} files={files}", cons))
    if cli.get("start_help"):
        flags = set(re.findall(r"--[a-z][a-z-]*", cli["start_help"]))
        miss = [f for f in START_FLAGS if f not in flags]
        out.append(_res("db.cli.start_flags", "pass" if not miss else "fail", f"missing={miss}", cons[:1]))
    if cli.get("help"):
        sub = set(re.findall(r"(?m)^\s{2}([a-z][a-z-]*)\s{2,}\S", cli["help"]))
        miss = [s for s in SUBCOMMANDS if s not in sub]
        out.append(_res("db.cli.subcommands", "pass" if not miss else "fail", f"missing={miss}", cons[:1]))
    if cli.get("status"):
        p = parse_status(cli["status"])
        miss = [k for k in ("version", "daemon_running", "usable_gb") if p.get(k) is None]
        out.append(_res("db.cli.status_parse", "pass" if not miss else "fail", f"unparsed={miss}", cons[1:]))
    return out


def parse_status(text):
    """MIRROR of ollama-queue.py parse_darkbloom_status (version/daemon_running/usable_gb only).
    Wave 1: replace with the queue's function via an import hook so there is one parser."""
    out = {}
    m = re.search(r"^darkbloom\s+(\d+\.\d+\.\d+)", text or "", re.M)
    if m:
        out["version"] = m.group(1)
    m = re.search(r"^Daemon:\s*(\w+)", text or "", re.M)
    if m:
        out["daemon_running"] = m.group(1).lower() == "running"
    m = re.search(r"Live load memory:\s*([\d.]+)\s*GB usable", text or "")
    if m:
        out["usable_gb"] = float(m.group(1))
    return out


CHECKS = (_c_local_shape, _c_pid, _c_daemon, _c_loaded, _c_toml, _c_versions, _c_cli)


def run_offline(root=None, cli_texts=None, sources=None, ctx=None):
    """Run every offline check. Never raises: a crashing check becomes a fail row."""
    ctx = ctx or load_context(root, cli_texts, sources)
    out = []
    for fn in CHECKS:
        try:
            out += fn(ctx)
        except Exception as e:      # noqa: BLE001
            out.append(_res(f"crash.{fn.__name__}", "fail", f"{type(e).__name__}: {str(e)[:80]}", [fn.__name__]))
    return out


def summarize(results):
    fails = [r for r in results if r["status"] == "fail"]
    return {"pass": sum(r["status"] == "pass" for r in results), "fail": len(fails),
            "skip": sum(r["status"] == "skip" for r in results),
            "failed_ids": [r["id"] for r in fails],
            "consumers_at_risk": sorted({c for r in fails for c in r["consumers"]})}


def main(argv=None):
    a = list(sys.argv[1:] if argv is None else argv)
    root = a[a.index("--root") + 1] if "--root" in a else None
    cli = collect_cli() if "--cli" in a else None
    res = run_offline(root, cli)
    s = summarize(res)
    if "--json" in a:
        print(json.dumps({"summary": s, "results": res}, indent=1))
    else:
        for r in res:
            print(f"{r['status'].upper():4} {r['id']:36} {r['detail'][:90]}")
        print(f"{s['pass']} pass, {s['fail']} fail, {s['skip']} skip"
              + (f"; consumers at risk: {s['consumers_at_risk']}" if s["fail"] else ""))
    return 1 if s["fail"] else 0


if __name__ == "__main__":
    sys.exit(main())
