#!/usr/bin/env python3
"""Tests for vendor_contract.py (offline tier of design section 5). Stub-driven: sandbox files and
injected CLI text only; no network, no model calls, no real ~/.darkbloom needed.

  python3 test-vendor-contract.py [--mutants|--revert-check]
VENDOR_CONTRACT_SRC=<path> points at a mutated copy (used by --mutants)."""
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = os.environ.get("VENDOR_CONTRACT_SRC") or str(HERE / "vendor_contract.py")
sys.path.insert(0, str(HERE))
spec = importlib.util.spec_from_file_location("vendor_contract_under_test", SRC)
V = importlib.util.module_from_spec(spec)
sys.modules["vendor_contract_under_test"] = V
spec.loader.exec_module(V)

FAILS = []
SECRET = "SENTINEL-VENDOR-KEY-cccccccccccccccccccc"


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
        print(f"FAIL {name} {detail}")
    else:
        print(f"ok   {name}")


HELP = """OVERVIEW: Swift-native provider CLI for Darkbloom.

USAGE: darkbloom <subcommand>

OPTIONS:
  --version               Show the version.

SUBCOMMANDS:
  start                   Start the provider as a background service.
  switch                  Change the running provider's hosted models without
                          restarting.
  stop                    Drain accepted requests.
  restart                 Restart the provider.
  status                  Show local provider configuration and hardware status.
  update                  Check for updates and self-update the provider binary.
"""
START_HELP = "OPTIONS:\n  --model <m>\n  --local-endpoint\n  --idle-timeout <n>\n  --timeout <s>\n  --force\n  --port <p>\n"


def status_text(ver="0.9.20"):
    return f"darkbloom {ver}\nDaemon: running\nLive load memory: 41.5 GB usable\nRequests served: 12\n"


def cli(ver="0.9.20"):
    return {"version": f"{ver}\n", "help": HELP, "start_help": START_HELP, "status": status_text(ver)}


class Box:
    def __init__(self, ver="0.9.20", base_url=True, models=("Qwen3.5-9B", "qwen3.6-35b-a3b-vl-mtp-mxfp8")):
        self.root = Path(tempfile.mkdtemp(prefix="vc-"))
        self.ver = ver
        self.models = models
        rec = {"api_key": SECRET, "pid": 100, "port": 8000, "version": ver, "updated_at": "x"}
        if base_url:
            rec["base_url"] = "http://127.0.0.1:8000/v1"
        self.write("local.json", rec, 0o600)
        self.write("daemon-state.json", {
            "schema": 1, "version": ver, "pid": 100, "process_identity": {"pid": 100, "start_time_micros": 1},
            "written_at": 1.0, "advertised_models": list(models), "last_model_load_error": None,
            "warm_models": list(models), "load_transition_active": False,
            "capacity": {"load_usable_gb": 40.0, "load_headroom_gb": 5.0},
            "autopilot_state": {"max_model_slots": 2, "resident_models": [
                {"model_id": m, "idle_seconds": 3, "resident_gb": 5.0, "weights_gb": 6.0} for m in models]}})
        self.write("loaded-models.json", {"models": list(models), "schema": 1})
        arr = ", ".join(f"'{m}'" for m in models)
        self.toml(f"[backend]\nenabled_models = [ {arr} ]\npreload_models = [ {arr} ]\n"
                  f"idle_timeout_mins = 0\nmax_model_slots = 2\nmodel_cache_directory = '/x'\n")

    def write(self, name, obj, mode=None):
        p = self.root / name
        p.write_text(json.dumps(obj))
        if mode:
            p.chmod(mode)

    def toml(self, text):
        (self.root / "provider.toml").write_text(text)

    def edit(self, name, fn):
        p = self.root / name
        d = json.loads(p.read_text())
        fn(d)
        p.write_text(json.dumps(d))

    def run(self, cli_texts=None):
        return V.run_offline(self.root, cli_texts)

    def clean(self):
        shutil.rmtree(self.root, ignore_errors=True)


def failed(res):
    return {r["id"] for r in res if r["status"] == "fail"}


def red(name, mutate, expect, cli_texts=None, ver="0.9.20", base_url=True):
    b = Box(ver=ver, base_url=base_url)
    mutate(b)
    res = b.run(cli_texts if cli_texts is not None else cli(ver))
    f = failed(res)
    check(f"red.{name}", expect in f, f"expected {expect} in {sorted(f)}")
    b.clean()


def t_green_each_known_version():
    for ver, bu in (("0.9.14", True), ("0.9.17", False), ("0.9.20", True)):
        b = Box(ver=ver, base_url=bu)
        res = b.run(cli(ver))
        s = V.summarize(res)
        check(f"green.{ver}", s["fail"] == 0 and s["pass"] >= 20, f"{s}")
        check(f"green.{ver}.known_good", any(r["id"] == "db.version.known_good" and r["status"] == "pass" for r in res))
        b.clean()
    b = Box()
    res = b.run(None)
    check("green.no_cli_text_skips_cli", all(r["status"] != "fail" for r in res)
          and any(r["id"] == "db.cli.*" and r["status"] == "skip" for r in res))
    b.clean()


def t_red_mutated_fixtures():
    red("base_url_removed_no_api_key",
        lambda b: b.edit("local.json", lambda d: (d.pop("base_url"), d.pop("api_key"))), "db.local.base_url")
    red("api_key_missing", lambda b: b.edit("local.json", lambda d: d.pop("api_key")), "db.local.api_key")
    red("api_key_short", lambda b: b.edit("local.json", lambda d: d.__setitem__("api_key", "short")), "db.local.api_key")
    red("api_key_not_str", lambda b: b.edit("local.json", lambda d: d.__setitem__("api_key", 12345678901234567890123)),
        "db.local.api_key")
    red("port_str", lambda b: b.edit("local.json", lambda d: d.__setitem__("port", "8000")), "db.local.port")
    red("pid_missing", lambda b: b.edit("local.json", lambda d: d.pop("pid")), "db.local.pid")
    red("version_missing", lambda b: b.edit("local.json", lambda d: d.pop("version")), "db.local.version")
    red("base_url_garbage", lambda b: b.edit("local.json", lambda d: d.__setitem__("base_url", "not a url")), "db.local.base_url")
    red("mode_0644", lambda b: (b.root / "local.json").chmod(0o644), "db.local.mode_0600")
    red("local_empty", lambda b: (b.root / "local.json").write_text(""), "db.local.parses")
    red("local_missing", lambda b: (b.root / "local.json").unlink(), "db.local.parses")
    red("pid_mismatch", lambda b: b.edit("daemon-state.json", lambda d: d["process_identity"].__setitem__("pid", 7)),
        "db.local.pid_matches_daemon")
    red("daemon_schema_2", lambda b: b.edit("daemon-state.json", lambda d: d.__setitem__("schema", 2)), "db.daemon.schema")
    red("daemon_missing_top_key", lambda b: b.edit("daemon-state.json", lambda d: d.pop("warm_models")), "db.daemon.top_keys")
    red("daemon_capacity_renamed",
        lambda b: b.edit("daemon-state.json", lambda d: d["capacity"].__setitem__("load_headroom_gbs", d["capacity"].pop("load_headroom_gb"))),
        "db.daemon.capacity")
    red("daemon_resident_models_gone",
        lambda b: b.edit("daemon-state.json", lambda d: d["autopilot_state"].pop("resident_models")), "db.daemon.autopilot")
    red("daemon_resident_idle_gone",
        lambda b: b.edit("daemon-state.json", lambda d: d["autopilot_state"]["resident_models"][0].pop("idle_seconds")),
        "db.daemon.autopilot")
    red("daemon_resident_no_gb",
        lambda b: b.edit("daemon-state.json", lambda d: [(r.pop("resident_gb"), r.pop("weights_gb"))
                                                         for r in d["autopilot_state"]["resident_models"]]), "db.daemon.autopilot")
    red("daemon_slots_str", lambda b: b.edit("daemon-state.json", lambda d: d["autopilot_state"].__setitem__("max_model_slots", "2")),
        "db.daemon.autopilot")
    red("daemon_advertised_dict", lambda b: b.edit("daemon-state.json", lambda d: d.__setitem__("advertised_models", {})),
        "db.daemon.advertised")
    red("daemon_unparsable", lambda b: (b.root / "daemon-state.json").write_text("{"), "db.daemon.parses")
    red("loaded_schema_2", lambda b: b.edit("loaded-models.json", lambda d: d.__setitem__("schema", 2)), "db.loaded.schema")
    red("loaded_models_dict", lambda b: b.edit("loaded-models.json", lambda d: d.__setitem__("models", {"a": 1})), "db.loaded.models")
    red("loaded_models_ints", lambda b: b.edit("loaded-models.json", lambda d: d.__setitem__("models", [1, 2])), "db.loaded.models")
    # provider.toml: a comment containing ']' inside a multi-line array makes the legacy regex truncate
    red("toml_multiline_comment_bracket",
        lambda b: b.toml("[backend]\nenabled_models = [\n  'Qwen3.5-9B', # was [old]\n  'Other-1B',\n]\nmax_model_slots = 2\n"
                         "idle_timeout_mins = 0\nmodel_cache_directory = '/x'\n"), "db.toml.regex_equals_tomllib")
    red("toml_invalid", lambda b: b.toml("[backend\nbroken ="), "db.toml.parses")
    red("toml_key_renamed", lambda b: b.toml("[backend]\nenabled_models = ['a']\npreload_models = ['a']\n"
                                           "max_model_slots = 2\nidle_timeout = 0\nmodel_cache_directory = '/x'\n"),
        "db.toml.backend_keys")
    red("toml_missing", lambda b: (b.root / "provider.toml").unlink(), "db.toml.readable")
    red("version_files_disagree", lambda b: b.edit("daemon-state.json", lambda d: d.__setitem__("version", "0.9.21")),
        "db.version.files_agree")
    red("version_newer_unrecorded",
        lambda b: (b.edit("local.json", lambda d: d.__setitem__("version", "0.9.21")),
                   b.edit("daemon-state.json", lambda d: d.__setitem__("version", "0.9.21"))),
        "db.version.known_good", cli_texts=cli("0.9.21"))
    red("version_shape_0917_regained_base_url",
        lambda b: b.edit("local.json", lambda d: d.__setitem__("base_url", "http://127.0.0.1:8000/v1")),
        "db.version.shape_matches_recorded", ver="0.9.17", base_url=False)
    red("version_shape_0920_lost_base_url", lambda b: b.edit("local.json", lambda d: d.pop("base_url")),
        "db.version.shape_matches_recorded")
    # CLI text mutations
    c = cli()
    red("cli_flag_renamed", lambda b: None, "db.cli.start_flags",
        cli_texts={**c, "start_help": c["start_help"].replace("--local-endpoint", "--local-ep")})
    red("cli_flag_force_removed", lambda b: None, "db.cli.start_flags",
        cli_texts={**c, "start_help": c["start_help"].replace("  --force\n", "")})
    red("cli_subcommand_renamed", lambda b: None, "db.cli.subcommands",
        cli_texts={**c, "help": c["help"].replace("  switch ", "  swap   ")})
    red("cli_subcommand_update_gone", lambda b: None, "db.cli.subcommands",
        cli_texts={**c, "help": c["help"].replace("  update ", "  upgrade")})
    red("cli_status_version_line_changed", lambda b: None, "db.cli.status_parse",
        cli_texts={**c, "status": c["status"].replace("darkbloom 0.9.20", "Darkbloom v0.9.20")})
    red("cli_status_daemon_line_changed", lambda b: None, "db.cli.status_parse",
        cli_texts={**c, "status": c["status"].replace("Daemon: running", "Service: up")})
    red("cli_status_memory_line_changed", lambda b: None, "db.cli.status_parse",
        cli_texts={**c, "status": c["status"].replace("GB usable", "GiB free")})
    red("cli_version_disagrees_with_files", lambda b: None, "db.cli.version_matches_files",
        cli_texts={**c, "version": "0.9.99\n"})


def t_failure_names_consumers():
    b = Box()
    b.edit("local.json", lambda d: d.pop("api_key"))
    s = V.summarize(b.run(cli()))
    check("consumers.named", "ollama-worker.py:_darkbloom_auth_headers" in s["consumers_at_risk"]
          and "db.local.api_key" in s["failed_ids"], str(s))
    b.clean()


def t_version_logic():
    vs = V.version_status
    check("ver.known", all(vs(v) == "known" for v in ("0.9.14", "0.9.17", "0.9.20", " 0.9.20 ", "v0.9.20")))
    check("ver.newer_patch", vs("0.9.21") == "newer")
    check("ver.newer_minor", vs("0.10.0") == "newer" and vs("1.0.0") == "newer")
    check("ver.numeric_not_lexical", vs("0.9.100") == "newer" and vs("0.10.1") == "newer")   # "0.9.9" > "0.10.1" lexically
    check("ver.older", vs("0.9.13") == "older" and vs("0.8.99") == "older")
    check("ver.between", vs("0.9.15") == "between" and vs("0.9.18") == "between")
    check("ver.missing_unparsable", vs(None) == "missing" and vs("") == "missing" and vs("abc") == "unparsable"
          and vs("0.9") == "unparsable")
    check("ver.changed_patch", V.version_changed("0.9.20", "0.9.21"))
    check("ver.not_changed_same", not V.version_changed("0.9.20", "0.9.20") and not V.version_changed("0.9.20", " 0.9.20"))
    check("ver.unknown_is_not_change", not V.version_changed(None, "0.9.20") and not V.version_changed("0.9.20", None)
          and not V.version_changed("", ""))
    check("ver.known_good_table", set(V.KNOWN_GOOD) == {"0.9.14", "0.9.17", "0.9.20"}
          and V.KNOWN_GOOD["0.9.17"]["local_json_base_url"] is False and V.KNOWN_GOOD["0.9.20"]["local_json_base_url"] is True)
    b = Box(ver="0.9.20")
    ctx = V.load_context(b.root, cli("0.9.20"))
    d = V.detect_versions(ctx)
    check("ver.detect_from_files", d == {"local_json": "0.9.20", "daemon_state": "0.9.20", "cli": "0.9.20"}, str(d))
    b.edit("local.json", lambda x: x.pop("version"))
    ctx = V.load_context(b.root, None)
    check("ver.detect_falls_back_to_daemon_state", V.detect_version(ctx) == "0.9.20")
    b.edit("daemon-state.json", lambda x: x.pop("version"))
    check("ver.detect_none_without_files_or_cli", V.detect_version(V.load_context(b.root, None)) is None)
    check("ver.detect_cli_last_resort", V.detect_version(V.load_context(b.root, {"version": "darkbloom 0.9.18"})) == "0.9.18")
    b.clean()


def t_no_secret_and_no_side_effects():
    b = Box()
    ctx = V.load_context(b.root, cli())
    check("secret.not_in_ctx", SECRET not in repr(ctx.get("local")) and SECRET not in json.dumps(
        {k: v for k, v in ctx.items() if k != "src"}, default=str))
    res = b.run(cli())
    check("secret.not_in_results", SECRET not in json.dumps(res))
    b.edit("local.json", lambda d: d.__setitem__("api_key", "x"))
    check("secret.not_in_failing_results", SECRET not in json.dumps(b.run(cli())))
    # a run must not modify the vendor files
    before = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in b.root.iterdir()}
    b.run(cli()); V.run_offline(b.root, None)
    after = {p.name: (p.stat().st_mtime_ns, p.read_bytes()) for p in b.root.iterdir()}
    check("noside.files_untouched", before == after and set(before) == set(after))
    # CLI entry prints no key and exit code tracks failures
    r = subprocess.run([sys.executable, SRC, "--root", str(b.root)], capture_output=True, text=True)
    check("cli.exit_1_on_fail_no_secret", r.returncode == 1 and SECRET not in r.stdout + r.stderr, r.stdout[-300:])
    b2 = Box()
    r = subprocess.run([sys.executable, SRC, "--root", str(b2.root), "--json"], capture_output=True, text=True)
    j = json.loads(r.stdout)
    check("cli.json_green_exit0", r.returncode == 0 and j["summary"]["fail"] == 0 and SECRET not in r.stdout, r.stdout[-200:])
    b.clean(); b2.clean()


def t_crash_isolation_and_collect_cli():
    b = Box()
    orig = V.CHECKS
    def _boom(ctx):
        raise RuntimeError("injected check crash")   # not ZeroDivisionError: `except ZeroDivisionError` must not pass
    V.CHECKS = (_boom, *orig)
    try:
        res = b.run(cli())
    finally:
        V.CHECKS = orig
    check("crash.becomes_fail_row", any(r["id"].startswith("crash.") and r["status"] == "fail" for r in res)
          and any(r["id"] == "db.local.parses" for r in res))
    calls = []
    out = V.collect_cli(runner=lambda argv: calls.append(argv) or "x")
    check("collect.injected_runner", calls == [["--version"], ["--help"], ["start", "--help"], ["status"]]
          and set(out) == {"version", "help", "start_help", "status"})
    out = V.collect_cli(runner=None) if shutil.which("darkbloom") is None else {"version": ""}
    check("collect.missing_binary_is_empty_text", out.get("version", "") == "" or shutil.which("darkbloom") is not None)
    b.clean()


def main():
    for t in (t_green_each_known_version, t_red_mutated_fixtures, t_failure_names_consumers, t_version_logic,
              t_no_secret_and_no_side_effects, t_crash_isolation_and_collect_cli):
        try:
            t()
        except Exception as e:      # noqa: BLE001
            import traceback
            traceback.print_exc()
            FAILS.append(f"{t.__name__}: {type(e).__name__}: {e}")
            print(f"FAIL {t.__name__} crashed: {e}")


MUTANTS = [
    ('"0.9.17": {"local_json_base_url": False', '"0.9.17": {"local_json_base_url": True'),
    ("if t > ts[-1]:\n        return \"newer\"", "if False:\n        return \"newer\""),
    ("return tuple(int(x) for x in m.groups()) if m else None", "return tuple(m.groups()) if m else None"),
    ("return bool(prev) and bool(cur) and str(prev).strip() != str(cur).strip()", "return bool(prev) and bool(cur) and str(prev).strip()[:-2] != str(cur).strip()[:-2]"),
    ("local = {k: v for k, v in local.items() if k != \"api_key\"}", "local = dict(local)"),
    ("ok_m = isinstance(L.get(\"models\"), list) and all(isinstance(x, str) for x in L[\"models\"])", "ok_m = True"),
    ("\"pass\" if set(parsed) == set(regex) else \"fail\"", "\"pass\""),
    ("\"pass\" if m == 0o600 else \"fail\"", "\"pass\""),
    ("\"pass\" if lp is not None and lp == dp else \"fail\"", "\"pass\""),
    ("\"pass\" if D.get(\"schema\") == 1 else \"fail\"", "\"pass\""),
    ("miss = [f for f in START_FLAGS if f not in flags]", "miss = []"),
    ("miss = [s for s in SUBCOMMANDS if s not in sub]", "miss = []"),
    ("miss = [k for k in (\"version\", \"daemon_running\", \"usable_gb\") if p.get(k) is None]", "miss = []"),
    ("\"pass\" if a == b else \"fail\"", "\"pass\""),
    ("ok = bool(m) and (not files or m.group(1) == files)", "ok = True"),
    ("\"pass\" if has_base == rec[\"local_json_base_url\"] else \"fail\"", "\"pass\""),
    ("\"pass\" if st == \"known\" else (\"skip\" if st == \"missing\" else \"fail\")", "\"pass\""),
    ("ok = isinstance(bu, str) and re.fullmatch(", "ok = isinstance(bu, str) or re.fullmatch("),
    ("\"pass\" if ki and ki[\"type\"] == \"str\" and ki[\"len\"] >= 20 else \"fail\"", "\"pass\" if ki else \"fail\""),
    ("miss = [k for k in TOML_BACKEND_KEYS if k not in be]", "miss = []"),
    ("missing = [k for k in DAEMON_TOP_KEYS if k not in D]", "missing = []"),
    ("mc = [k for k in DAEMON_CAPACITY_KEYS if not isinstance(cap.get(k), (int, float)) or isinstance(cap.get(k), bool)]", "mc = []"),
    ("        out.append(_res(\"db.local.base_url\", \"pass\" if L.get(\"_has_api_key\") else \"fail\",", "        out.append(_res(\"db.local.base_url\", \"pass\","),
    ("        except Exception as e:      # noqa: BLE001\n            out.append(_res(f\"crash.", "        except ZeroDivisionError as e:      # noqa: BLE001\n            out.append(_res(f\"crash."),
    ("return 1 if s[\"fail\"] else 0", "return 0"),
]


def mutants():
    base = Path(SRC).read_text()
    bad = 0
    for i, (old, new) in enumerate(MUTANTS):
        if old not in base:
            print(f"MUTANT {i} NOT APPLICABLE: {old[:60]!r}")
            bad += 1
            continue
        d = Path(tempfile.mkdtemp(prefix="vc-mut-"))
        (d / "vendor_contract.py").write_text(base.replace(old, new, 1))
        try:
            r = subprocess.run([sys.executable, str(HERE / "test-vendor-contract.py")],
                               env={**os.environ, "VENDOR_CONTRACT_SRC": str(d / "vendor_contract.py"),
                                    "PYTHONPATH": str(HERE)},      # the CLI subprocess must import lane_plan
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
