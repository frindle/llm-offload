"""test-review-think-optin.py -- a queue task file's `review_think` turns code-review-agent.py's thinking review on,
and only for a literal true (2026-11-07, review-test arm q36t: the queue's --runner contract can't pass --think).
No producer writes the key, so every other job keeps think=off. The --think flag still works.
Usage: [CRA=<code-review-agent.py>] python3 this.py   -> prints REVIEW_THINK_OPTIN_OK"""
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

CRA = os.environ.get("CRA") or str(Path(__file__).resolve().parent / "code-review-agent.py")
spec = importlib.util.spec_from_file_location("cra", CRA)
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)
seen = {}


class Stop(Exception):
    pass


class FakeModel:
    def __init__(self, host, model, num_ctx, think=False):
        seen["think"] = think
        raise Stop()


m.Model = FakeModel
cases = [(True, True), (None, False), (False, False), ("true", False), (1, False)]
for val, want in cases:
    d = tempfile.mkdtemp()
    tf = os.path.join(d, "task.json")
    t = {"mode": "review"}
    if val is not None:
        t["review_think"] = val
    with open(tf, "w") as fh:
        json.dump(t, fh)
    seen.clear()
    sys.argv = ["cra", "--model", "m", "--host", "http://127.0.0.1:9", "--task-file", tf, "--cwd", d]
    try:
        m.main()
    except Stop:
        pass
    assert seen.get("think") == want, (val, seen)
# the CLI flag still works with no key in the task file
d = tempfile.mkdtemp()
tf = os.path.join(d, "task.json")
with open(tf, "w") as fh:
    json.dump({"mode": "review"}, fh)
seen.clear()
sys.argv = ["cra", "--model", "m", "--host", "http://127.0.0.1:9", "--task-file", tf, "--cwd", d, "--think"]
try:
    m.main()
except Stop:
    pass
assert seen.get("think") is True, seen
print("REVIEW_THINK_OPTIN_OK")
