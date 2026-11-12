#!/usr/bin/env python3
"""bloom-idle-switch: RETIRED 2026-10-08. Replaced by ~/bin/bloom_control.py, driven by the
queue daemon on queue events (no 10-minute idle timer, no launchd tick).

  queue has work  -> bloom_control.hold_for_queue()   (BloomGauge Manual + pair-only Darkbloom)
  queue done      -> bloom_control.release_to_bloom() (BloomGauge Automatic, immediately)

This shim exists so a stale launchd job or habit does nothing harmful:
  * a tick (no args) clears any leftover legacy 'away/restoring' state (so the queue never
    waits on a retired helper) and exits 0;
  * --status prints bloom_control's state.
See docs/bloom-control.md (how to turn the integration off: BLOOM_CONTROL=0 or the file
~/.ollama-dispatch/bloom-control.disabled)."""
import json
import os
import subprocess
import sys
from pathlib import Path


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    home = Path(os.environ.get("BLOOMIDLE_HOME") or Path.home() / ".ollama-dispatch")
    if "--status" in argv:
        return subprocess.call([sys.executable, str(Path(__file__).resolve().parent / "bloom_control.py"), "state"])
    legacy = Path(os.environ.get("BLOOM_IDLE_STATE") or home / "bloom-idle-state.json")
    try:
        st = json.loads(legacy.read_text())
        if st.get("state") != "home":
            legacy.write_text(json.dumps({"state": "home"}))
            print("bloom-idle-switch is retired: cleared legacy %r state" % st.get("state"))
    except (OSError, ValueError):
        pass
    print("bloom-idle-switch is retired; the queue daemon drives bloom_control.py (docs/bloom-control.md)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
