#!/usr/bin/env python3
"""log_retention_config.py -- the ONE place log-retention settings live (config contract).

File: ~/.ollama-dispatch/log-retention.json  (read FRESH on every log-rotate run; the
dashboard edits it through save(), nobody needs a code edit to change retention).

SCHEMA (version 1) -- three user-facing levels + holds:
  {
    "version": 1,
    "all":        30,     # int 1..365. Default days for EVERY log. Missing/0/bad -> 30.
    "failures":   null,   # int 1..365 or null. Overrides "all" for FAILED / FAIL / CONCERNS /
                          #   escalated / parked-bundle job logs. null (or 0/missing) = inherit "all".
    "successful": null,   # int 1..365 or null. Overrides "all" for PASS / completed-ok job logs.
                          #   null (or 0/missing) = inherit "all".
    "ledger_days": 365,   # ADVANCED, not a dashboard level. Floor for the durable result sidecars
                          #   (<id>.gate.json / .done.json / .diff) that gate-audit, `qctl results`,
                          #   run-status and the TTC median read. int 30..3650, default 365.
    "pins": [             # HOLDS: logs of a pinned job/bundle are kept until the pin is removed.
      {"type": "job",    "id": "0160287d360a",    "who": "the operator", "when": "2026-10-13T12:00:00Z", "why": "debugging X"},
      {"type": "bundle", "id": "smoke-egift-s4-api-route", "who": "...", "when": "...", "why": "..."}
    ]
  }
RESOLUTION for one log:  pin (kept)  >  failures/successful override (by the job's outcome)
                         >  "all"  >  built-in 30.   Sidecars: max(that, ledger_days).
Independent of this file, log-rotate NEVER deletes anything referenced by a live queue row, an
unprocessed handoff item, an awaiting-sign-off row or an open ESCALATIONS row.

API for the dashboard:
  load() -> cfg                     always a valid, normalized cfg (never raises)
  load_checked() -> (cfg, errors)   same, plus what was wrong with the file (fell back per field)
  validate(cfg) -> [error strings]  empty list = ok (does not mutate)
  save(cfg, who="dashboard") -> cfg atomic (flock + tmp + os.replace) + audit line; raises ValueError(errors)
  preview(cfg=None) -> dict         what a dry run WOULD delete per level (count/bytes), no deletion
  resolve_days(cfg, outcome, ledger=False) -> int
"""
import copy
import fcntl
import json
import os
import re
import time
from pathlib import Path

VERSION = 1
DEFAULT_DAYS = 30
MIN_DAYS, MAX_DAYS = 1, 365
LEDGER_DEFAULT, LEDGER_MIN, LEDGER_MAX = 365, 30, 3650
HOME = Path(os.environ.get("LOG_ROTATE_HOME") or Path.home())
LEVELS = ("all", "failures", "successful")
_ID_RE = re.compile(r"^[0-9a-f]{12}$")


def config_path(home=None):
    return Path(os.environ.get("LOG_RETENTION_CONFIG")
                or (Path(home) if home else HOME) / ".ollama-dispatch" / "log-retention.json")


def audit_path(home=None):
    return (Path(home) if home else HOME) / ".ollama-dispatch" / "log-rotate.log"


def default_config():
    return {"version": VERSION, "all": DEFAULT_DAYS, "failures": None, "successful": None,
            "ledger_days": LEDGER_DEFAULT, "pins": []}


def _is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def validate(cfg):
    """Errors for a cfg a caller wants to SAVE. 0 / missing are tolerated by load() but a
    saved file must be explicit: all in 1..365; failures/successful null or 1..365."""
    errs = []
    if not isinstance(cfg, dict):
        return ["config must be a JSON object"]
    if cfg.get("version", VERSION) != VERSION:
        errs.append(f"version must be {VERSION}")
    v = cfg.get("all", DEFAULT_DAYS)
    if not _is_int(v) or not (MIN_DAYS <= v <= MAX_DAYS):
        errs.append(f"all must be an integer {MIN_DAYS}..{MAX_DAYS}")
    for k in ("failures", "successful"):
        v = cfg.get(k)
        if v is not None and (not _is_int(v) or not (MIN_DAYS <= v <= MAX_DAYS)):
            errs.append(f"{k} must be null (inherit all) or an integer {MIN_DAYS}..{MAX_DAYS}")
    v = cfg.get("ledger_days", LEDGER_DEFAULT)
    if not _is_int(v) or not (LEDGER_MIN <= v <= LEDGER_MAX):
        errs.append(f"ledger_days must be an integer {LEDGER_MIN}..{LEDGER_MAX}")
    pins = cfg.get("pins", [])
    if not isinstance(pins, list):
        errs.append("pins must be a list")
    else:
        for i, p in enumerate(pins):
            if not isinstance(p, dict):
                errs.append(f"pins[{i}] must be an object"); continue
            if p.get("type") not in ("job", "bundle"):
                errs.append(f"pins[{i}].type must be 'job' or 'bundle'")
            pid = p.get("id")
            if not isinstance(pid, str) or not pid.strip():
                errs.append(f"pins[{i}].id required")
            elif p.get("type") == "job" and not _ID_RE.match(pid):
                errs.append(f"pins[{i}].id must be a 12-hex job id")
            for k in ("who", "when", "why"):
                if not isinstance(p.get(k), str) or not p.get(k).strip():
                    errs.append(f"pins[{i}].{k} required (who/when/why)")
    return errs


def normalize(raw):
    """(cfg, errors): salvage field by field; anything bad falls back to its default."""
    cfg, errs = default_config(), []
    if not isinstance(raw, dict):
        return cfg, ["config is not a JSON object; using defaults"]
    if raw.get("version", VERSION) != VERSION:
        errs.append(f"unknown version {raw.get('version')!r}; reading as v{VERSION}")
    v = raw.get("all")
    if _is_int(v) and MIN_DAYS <= v <= MAX_DAYS:
        cfg["all"] = v
    elif v not in (None, 0):
        errs.append(f"all={v!r} invalid; using {DEFAULT_DAYS}")
    for k in ("failures", "successful"):
        v = raw.get(k)
        if _is_int(v) and MIN_DAYS <= v <= MAX_DAYS:
            cfg[k] = v
        elif v not in (None, 0):
            errs.append(f"{k}={v!r} invalid; inheriting all")
    v = raw.get("ledger_days")
    if _is_int(v) and LEDGER_MIN <= v <= LEDGER_MAX:
        cfg["ledger_days"] = v
    elif v is not None:
        errs.append(f"ledger_days={v!r} invalid; using {LEDGER_DEFAULT}")
    pins = raw.get("pins")
    if isinstance(pins, list):
        for i, p in enumerate(pins):
            if (isinstance(p, dict) and p.get("type") in ("job", "bundle")
                    and isinstance(p.get("id"), str) and p["id"].strip()):
                cfg["pins"].append({k: p.get(k) for k in ("type", "id", "who", "when", "why")})
            else:
                errs.append(f"pins[{i}] malformed; ignored")
    elif pins is not None:
        errs.append("pins malformed; ignored")
    return cfg, errs


def load_checked(path=None):
    p = Path(path) if path else config_path()
    try:
        raw = json.loads(p.read_text())
    except FileNotFoundError:
        return default_config(), []
    except (OSError, ValueError) as e:
        return default_config(), [f"cannot read {p}: {e}; using defaults"]
    return normalize(raw)


def load(path=None):
    return load_checked(path)[0]


def resolve_days(cfg, outcome=None, ledger=False):
    """Pure. outcome in {None,'failures','successful'}. Precedence: override > all > 30."""
    base = cfg.get("all") if _is_int(cfg.get("all")) and cfg.get("all") > 0 else DEFAULT_DAYS
    if outcome in ("failures", "successful"):
        o = cfg.get(outcome)
        if _is_int(o) and o > 0:
            base = o
    if ledger:
        base = max(base, cfg.get("ledger_days") or LEDGER_DEFAULT)
    return base


def append_audit(line, home=None, cap_bytes=256 * 1024):
    """One line to the rotation log; size-capped (keeps the newest half when over cap)."""
    p = audit_path(home)
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        with open(p, "a") as f:
            f.write(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()) + " " + line.rstrip("\n") + "\n")
        if p.stat().st_size > cap_bytes:
            data = p.read_bytes()[-cap_bytes // 2:]
            data = data[data.find(b"\n") + 1:]
            tmp = p.with_name(p.name + ".tmp")
            tmp.write_bytes(data)
            os.replace(tmp, p)
    except OSError:
        pass


def save(cfg, who="dashboard", path=None):
    errs = validate(cfg)
    if errs:
        raise ValueError(errs)
    out, _ = normalize(cfg)
    p = Path(path) if path else config_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    old = load(p)
    with open(p.with_name(p.name + ".lock"), "w") as lk:
        fcntl.flock(lk, fcntl.LOCK_EX)
        tmp = p.with_name(f"{p.name}.tmp.{os.getpid()}")
        with open(tmp, "w") as f:
            json.dump(out, f, indent=2)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, p)
    ch = [f"{k}: {old.get(k)!r}->{out.get(k)!r}" for k in ("all", "failures", "successful", "ledger_days")
          if old.get(k) != out.get(k)]
    if old.get("pins") != out.get("pins"):
        ch.append(f"pins: {len(old.get('pins', []))}->{len(out['pins'])}")
    append_audit(f"config-saved by={who} " + ("; ".join(ch) or "no change"), home=p.parent.parent)
    return out


def preview(cfg=None, paths=None):
    """What a dry run WOULD delete under cfg (not saved): per level count/bytes.
    {"levels": {"all":{count,bytes}, "failures":{...}, "successful":{...}, "ledger":{...}},
     "total": {count,bytes}, "protected": {count,bytes}, "pinned": {count,bytes}, "errors": [...]}"""
    import log_rotate_lib as lib
    cfg = copy.deepcopy(cfg) if cfg is not None else load()
    errs = validate(cfg)
    if errs:
        return {"errors": errs}
    cfg, _ = normalize(cfg)
    res = lib.run(cfg, apply=False, paths=paths, audit=False)
    return {"levels": res["levels"], "total": res["total"], "protected": res["protected"],
            "pinned": res["pinned"], "errors": res["errors"]}
