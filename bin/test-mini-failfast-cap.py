#!/usr/bin/env python3
"""failfast_wall: no-op when MINI_FAILFAST_CAP is unset/0 (even with a caps file); when 1, cuts only to 2x pass p90 of the bucket, never raises wall,
never below 120s, and falls back to wall without a file/bucket."""
import importlib.util, json, os, tempfile
from pathlib import Path
here = Path(__file__).resolve().parent
sp = importlib.util.spec_from_file_location("r", here / "mini-swe-runner.py"); R = importlib.util.module_from_spec(sp); sp.loader.exec_module(R)
f = Path(tempfile.mkdtemp()) / "caps.json"
f.write_text(json.dumps({"buckets": {"60": {"cap_s": 700}, "40": {"cap_s": 50}, "100": {"cap_s": 5000}}}))
os.environ["MINI_FAILFAST_CAPS_FILE"] = str(f)
os.environ.pop("MINI_FAILFAST_CAP", None)
assert R.failfast_wall({"fix_steps": 50}, 900) == 900, "OFF is a no-op"
os.environ["MINI_FAILFAST_CAP"] = "0"; assert R.failfast_wall({"fix_steps": 50}, 900) == 900
os.environ["MINI_FAILFAST_CAP"] = "1"
assert R.failfast_wall({"fix_steps": 50}, 900) == 700, "bucket 60 -> cap 700"
assert R.failfast_wall({"fix_steps": 50}, 600) == 600, "never raises wall"
assert R.failfast_wall({"fix_steps": 30}, 900) == 120, "floor 120s"
assert R.failfast_wall({"fix_steps": 90}, 900) == 900, "cap above wall"
assert R.failfast_wall({"fix_steps": 70}, 900) == 900, "no bucket -> unchanged"
os.environ["MINI_FAILFAST_CAPS_FILE"] = "/nonexistent"; assert R.failfast_wall({"fix_steps": 50}, 900) == 900
print("test-mini-failfast-cap ok")
