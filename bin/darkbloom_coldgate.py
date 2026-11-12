"""Darkbloom cold-load memory gate (extracted from the retired bloom_control.py, 2026-10-31).

`darkbloom status` is the only place the provider publishes its live load-gate arithmetic. This module
parses it so the worker can tell "cold load cannot fit in memory" (do not wait on a busy slot) from a
genuinely busy slot. Behaviour-identical to bloom_control.cold_infeasible and its helpers; no dependency
on bloom_control. Every public function is fail-soft (never raises).

Env: BLOOMCTL_DARKBLOOM_BIN, BLOOMCTL_LOADED_JSON, BLOOMCTL_PURGE (0 disables), BLOOMCTL_PURGE_MIN_S (120).
"""
import json
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

_GB = r"([\d.]+)(?:\s*-\s*([\d.]+))?\s*GB"


def darkbloom_bin():
    return (os.environ.get("BLOOMCTL_DARKBLOOM_BIN") or shutil.which("darkbloom")
            or str(Path.home() / ".darkbloom/bin/darkbloom"))


def loaded_json_path():
    return Path(os.environ.get("BLOOMCTL_LOADED_JSON") or (Path.home() / ".darkbloom" / "loaded-models.json"))


def parse_status_memory(text):
    """PURE. `darkbloom status` text -> {"usable": (lo, hi)|None, "warm": [ids] | None,
    "blocked": {model: {"needed", "short": (lo, hi)|None}}, "last_error": {"model", "free", "need"}|None}.
    Unknown/odd text yields None/empty parts, never an exception."""
    out = {"usable": None, "warm": None, "blocked": {}, "last_error": None}
    text = str(text or "")
    m = re.search(r"Live load memory:\s*" + _GB + r"\s*usable", text)
    if m:
        lo = float(m.group(1))
        out["usable"] = (lo, float(m.group(2)) if m.group(2) else lo)
    m = re.search(r"(?m)^Warm models:\s*(.*)$", text)
    if m:
        w = m.group(1).strip()
        out["warm"] = [] if (not w or w.lower().startswith("none")) else [x.strip() for x in w.split(",") if x.strip()]
    for m in re.finditer(r"Cold load blocked \(memory\):\s*([^\s:]+)\s*[\u2014\u2013-]+\s*([\d.]+)\s*GB model \+ "
                         r"([\d.]+)\s*GB serving reserve = ([\d.]+)\s*GB needed([^\n]*)", text):
        sh = re.search(r"Still\s*" + _GB + r"\s*short", m.group(5))
        short = None
        if sh:
            short = (float(sh.group(1)), float(sh.group(2)) if sh.group(2) else float(sh.group(1)))
        out["blocked"][m.group(1).lower()] = {"needed": float(m.group(4)), "short": short}
    m = re.search(r"Last model-load error:\s*([^\s:]+):\s*Insufficient memory \(([\d.]+)\s*GB free, need ([\d.]+)\s*GB\)", text)
    if m:
        out["last_error"] = {"model": m.group(1).lower(), "free": float(m.group(2)), "need": float(m.group(3))}
    return out


def infeasible_for(info, model):
    """PURE. None when `model` can load (or the status gives no verdict: 'temporarily busy', odd
    text). Else {"needed", "usable", "short", "reason"} for a model that is NOT warm and whose
    cold load the provider says cannot fit: a 'Cold load blocked (memory)' line for it, or its
    latest 'Insufficient memory (X free, need Y)' error while usable is still below Y."""
    if not info:
        return None
    low = str(model or "").lower()
    if low in {str(w).lower() for w in (info.get("warm") or [])}:
        return None
    usable = info.get("usable")
    needed = short = None
    b = (info.get("blocked") or {}).get(low)
    if b:
        needed = b["needed"]
        short = b["short"]
    else:
        e = info.get("last_error")
        if e and e["model"] == low and usable is not None and usable[1] < e["need"]:
            needed = e["need"]
    if needed is None:
        return None
    if usable is None:
        return None
    if short is None:
        short = (max(0.0, needed - usable[1]), max(0.0, needed - usable[0]))
    have = ("%.1f" % usable[0]) if usable[0] == usable[1] else ("%.1f-%.1f" % usable)
    sh = ("%.1f" % short[0]) if short[0] == short[1] else ("%.1f-%.1f" % short)
    return {"needed": needed, "usable": usable, "short": short, "model": low,
            "reason": "needs %.1f GB, have %s GB usable (%s GB short)" % (needed, have, sh)}


def loaded_models(path=None):
    try:
        return [str(x) for x in (json.loads(Path(path or loaded_json_path()).read_text()).get("models") or [])]
    except (OSError, ValueError, AttributeError):
        return []


def memory_feasibility(run=None, timeout=15):
    """`darkbloom status` -> parse_status_memory dict, or None when it cannot be read (provider
    down, timeout). `run` is a test seam returning the status text. Never raises."""
    try:
        if run is not None:
            return parse_status_memory(run())
        p = subprocess.run([darkbloom_bin(), "status"], capture_output=True, text=True, timeout=timeout)
        return parse_status_memory(p.stdout) if p.returncode == 0 else None
    except Exception:
        return None


_LAST_PURGE = [0.0]


def maybe_purge():
    """True when `sudo -n /usr/sbin/purge` ran (rc 0). Rate-limited (BLOOMCTL_PURGE_MIN_S, default 120);
    BLOOMCTL_PURGE=0 disables; needs a passwordless sudoers entry for purge. Never raises."""
    try:
        if str(os.environ.get("BLOOMCTL_PURGE", "1")) == "0":
            return False
        now = time.time()
        if now - _LAST_PURGE[0] < float(os.environ.get("BLOOMCTL_PURGE_MIN_S", 120)):
            return False
        _LAST_PURGE[0] = now
        p = subprocess.run(["sudo", "-n", "/usr/sbin/purge"], capture_output=True, text=True, timeout=60)
        if p.returncode == 0:
            time.sleep(3)
        return p.returncode == 0
    except Exception:
        return False


def cold_infeasible(model, run=None, loaded=None):
    """None, or the infeasible_for() dict, for `model` when it is not resident (not in
    loaded-models.json) and the provider's own arithmetic says its cold load cannot fit.
    A resident model costs nothing here (no subprocess): the healthy case is unchanged."""
    try:
        ld = loaded_models() if loaded is None else loaded
        if str(model or "").lower() in {str(x).lower() for x in ld}:
            return None
        inf = infeasible_for(memory_feasibility(run=run), model)
        if inf and run is None and maybe_purge():
            # file cache counted against "usable"; one purge often frees the shortfall -- re-ask the provider
            inf = infeasible_for(memory_feasibility(), model)
        return inf
    except Exception:
        return None
