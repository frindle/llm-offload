#!/usr/bin/env python3
"""Hermetic tests for darkbloom_coldgate.py (the cold-load memory gate extracted from bloom_control.py,
2026-10-31). Never touches Darkbloom, sudo or the real loaded-models.json.
Usage: test-darkbloom-coldgate.py   Marker: ALL PASS (exit 1 on any FAIL)."""
import importlib.util
import os
import sys
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
FAILS = []
PRIMARY = "qwen3.6-35b-a3b-vl-mtp-mxfp8"
SMALL = "Qwen3.5-9B"
# the exact wording `darkbloom status` printed live 2026-10-12
BLOCKED = ("Warm models: Qwen3.5-9B\n"
           "Live load memory: 20.9-22.2 GB usable now (no eviction)\n"
           "Cold load blocked (memory): qwen3.6-35b-a3b-vl-mtp-mxfp8 — 23.8 GB model + 6.5 GB serving "
           "reserve = 30.3 GB needed (scanner estimate). Still 1.8-3.1 GB short after idle eviction\n")
ERRONLY = ("Warm models: none loaded\nLive load memory: 18.4 GB usable now (no eviction)\n"
           "Last model-load error: qwen3.6-35b-a3b-vl-mtp-mxfp8: Insufficient memory (23.7 GB free, need 30.3 GB) "
           "and all loaded models are actively serving\n")
BUSY = ("Warm models: none loaded\nLive load memory: 0.0 GB usable now (no eviction)\n"
        "Load readiness temporarily busy: qwen3.6-35b-a3b-vl-mtp-mxfp8 — a request, model load, or reload is active\n")
FITS = ("Warm models: Qwen3.5-9B\nLive load memory: 34.9 GB usable now (no eviction)\n"
        "Not loaded (loads on request): qwen3.6-35b-a3b-vl-mtp-mxfp8\n")


def check(name, cond, extra=""):
    print(("ok  : " if cond else "FAIL: ") + name + ("" if cond else "  -- " + str(extra)))
    if not cond:
        FAILS.append(name)


def load(path, name):
    loader = SourceFileLoader(name, str(path))
    spec = importlib.util.spec_from_loader(name, loader)
    m = importlib.util.module_from_spec(spec)
    loader.exec_module(m)
    return m


def t_parse(bc):
    print("--- parse / verdict")
    i = bc.parse_status_memory(BLOCKED)
    check("usable range parsed", i["usable"] == (20.9, 22.2), i)
    check("warm list parsed", i["warm"] == ["Qwen3.5-9B"], i)
    check("blocked line parsed (needed + short range)",
          i["blocked"].get(PRIMARY) == {"needed": 30.3, "short": (1.8, 3.1)}, i)
    v = bc.infeasible_for(i, PRIMARY)
    check("verdict reason is 'needs X GB, have Y GB usable (Z short)'",
          bool(v) and v["reason"] == "needs 30.3 GB, have 20.9-22.2 GB usable (1.8-3.1 GB short)", v)
    check("a WARM model is never infeasible", bc.infeasible_for(i, SMALL) is None)
    v2 = bc.infeasible_for(bc.parse_status_memory(ERRONLY), PRIMARY)
    check("Insufficient-memory error with usable < need -> infeasible, short computed",
          bool(v2) and v2["needed"] == 30.3 and v2["reason"] == "needs 30.3 GB, have 18.4 GB usable (11.9 GB short)", v2)
    stale = ERRONLY.replace("18.4 GB usable", "34.9 GB usable")
    check("stale error while usable now >= need -> NOT infeasible",
          bc.infeasible_for(bc.parse_status_memory(stale), PRIMARY) is None)
    check("'temporarily busy' (no verdict) -> None, never a guess",
          bc.infeasible_for(bc.parse_status_memory(BUSY), PRIMARY) is None)
    check("fits -> None", bc.infeasible_for(bc.parse_status_memory(FITS), PRIMARY) is None)
    check("garbage / empty -> None", bc.infeasible_for(bc.parse_status_memory("\x00junk"), PRIMARY) is None
          and bc.infeasible_for(bc.parse_status_memory(""), PRIMARY) is None)
    calls = []
    check("resident model: no `darkbloom status` call at all (healthy case unchanged)",
          bc.cold_infeasible(PRIMARY, run=lambda: calls.append(1) or BLOCKED, loaded=[PRIMARY, SMALL]) is None
          and not calls, calls)
    check("not resident + blocked -> verdict",
          bool(bc.cold_infeasible(PRIMARY, run=lambda: BLOCKED, loaded=[SMALL])))
    check("status unreadable -> None (old behaviour)",
          bc.cold_infeasible(PRIMARY, run=lambda: (_ for _ in ()).throw(OSError("x")), loaded=[SMALL]) is None)




def t_extra(cg):
    print("--- purge / loaded / wiring")
    os.environ["BLOOMCTL_PURGE"] = "0"
    check("maybe_purge off by BLOOMCTL_PURGE=0", cg.maybe_purge() is False)
    check("loaded_models unreadable path -> []", cg.loaded_models("/nonexistent/loaded.json") == [])
    check("memory_feasibility run raising -> None",
          cg.memory_feasibility(run=lambda: (_ for _ in ()).throw(OSError("x"))) is None)
    src = (HERE / "ollama-worker.py").read_text()
    check("worker no longer loads bloom_control for the cold gate",
          "darkbloom_coldgate" in src and src.split("def _cold_infeasible_reason")[1].split("\ndef ")[0].count("bloom_control") == 0)


def main():
    cg = load(HERE / "darkbloom_coldgate.py", "darkbloom_coldgate")
    t_parse(cg)
    t_extra(cg)
    if FAILS:
        print("FAILED: %d" % len(FAILS))
        sys.exit(1)
    print("ALL PASS")


if __name__ == "__main__":
    main()
