#!/usr/bin/env python3
"""log-rotate.py -- rotate queue/dispatch live logs on a configurable window (default 30 days).

  log-rotate.py            DRY RUN (default): print what WOULD be deleted, delete nothing
  log-rotate.py --apply    delete
  log-rotate.py --json     machine-readable result

Config: ~/.ollama-dispatch/log-retention.json (see log_retention_config.py), re-read every run;
a missing/malformed file falls back to 30 days and is logged. Audit: ~/.ollama-dispatch/log-rotate.log
(size-capped). Never crashes: errors are logged and exit code stays 0 unless --strict.
"""
import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import log_retention_config as cfgmod  # noqa: E402
import log_rotate_lib as lib  # noqa: E402


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--apply", action="store_true", help="actually delete (default is dry-run)")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--verbose", "-v", action="store_true", help="list every file that would be deleted")
    ap.add_argument("--strict", action="store_true", help="exit 1 if there were errors")
    a = ap.parse_args(argv)
    try:
        cfg, cerrs = cfgmod.load_checked()
        if cerrs:
            cfgmod.append_audit("config-problems " + " | ".join(cerrs))
        res = lib.run(cfg, apply=a.apply)
    except Exception as e:  # last-resort: log, never crash the LaunchAgent
        cfgmod.append_audit(f"FATAL {type(e).__name__}: {e}")
        print(f"log-rotate: {type(e).__name__}: {e}", file=sys.stderr)
        return 1 if a.strict else 0
    ents = res.pop("entries", [])
    if a.json:
        print(json.dumps(res, indent=1, default=str))
    else:
        print(f"mode={'APPLY' if a.apply else 'DRY-RUN'}  retention: all={cfg['all']} failures={cfg['failures']} "
              f"successful={cfg['successful']} ledger={cfg['ledger_days']} pins={len(cfg['pins'])}")
        for root, v in sorted(res["by_dir"].items()):
            print(f"  {'deleted' if a.apply else 'would delete'} {v['count']:6d} entries {v['bytes']/1e6:9.1f} MB  {root}")
        for lv, v in res["levels"].items():
            print(f"  level {lv:10s} {v['count']:6d} {v['bytes']/1e6:9.1f} MB")
        print(f"  TOTAL {res['total']['count']} entries, {res['total']['bytes']/1e6:.1f} MB; "
              f"protected={res['protected']['count']} pinned={res['pinned']['count']}")
        print("  skipped:", json.dumps(res["skipped"], sort_keys=True))
        for e in res["errors"]:
            print("  ERROR:", e)
        if a.verbose:
            for e in ents:
                if e["action"] == "delete":
                    print("   -", e["path"])
    return 1 if (a.strict and res["errors"]) else 0


if __name__ == "__main__":
    sys.exit(main())
