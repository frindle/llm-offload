#!/usr/bin/env python3
"""Does verify_text() follow the venv idiom the ollama-dispatch skill mandates?

The skill tells every dispatch to bootstrap a venv and run checks through it:

    PY=.venv/bin/python
    "$PY" test_fixture.py

`"$PY"` is a VARIABLE, so an interpreter alternation made of literal tokens
(bash|sh|python3?|...) never matches, the follow stops at verify.sh, and the
assertions one hop down are invisible. verify-quality then reports "the verify
asserts none of the spec literals" against a verify that asserts all of them --
a FALSE PROBLEM against a sound verify, which is the failure mode that trains
people to discount the tool.

Reported by the Preflight session, confirmed here against the real function.
Arms cover every spelling; the last two must keep working (no regression) and
the bogus one must NOT be followed.
"""
import importlib.util, os, sys, tempfile
from pathlib import Path

BIN = Path(os.path.expanduser("~/bin"))
spec = importlib.util.spec_from_file_location("vq", BIN / "verify-quality.py")
vq = importlib.util.module_from_spec(spec)
spec.loader.exec_module(vq)

MARKER = "SENTINEL_LITERAL_XYZZY"

ARMS = [
    # (label, verify.sh body, must_follow)
    ('"$PY" fixture.py   (skill venv idiom)', 'PY=.venv/bin/python\n"$PY" fixture.py\n', True),
    ('$PY fixture.py     (unquoted var)',     'PY=.venv/bin/python\n$PY fixture.py\n',   True),
    ('"${PY}" fixture.py (braced var)',       'PY=.venv/bin/python\n"${PY}" fixture.py\n', True),
    ('python3 fixture.py (literal - regression guard)', 'python3 fixture.py\n', True),
    ('./fixture.py       (relative - regression guard)', './fixture.py\n', True),
    ('"$PY" nosuchfile.py (must NOT invent a follow)', '"$PY" nosuchfile.py\n', False),
]

fails = 0
for label, body, must_follow in ARMS:
    with tempfile.TemporaryDirectory() as d:
        wt = Path(d)
        (wt / "verify.sh").write_text(body)
        # the real assertions live one hop down, as they do in a real scaffold
        (wt / "fixture.py").write_text(f'assert "{MARKER}" in open("x").read()\n')
        text = vq.verify_text("bash verify.sh", wt)
        followed = MARKER in text
        ok = (followed == must_follow)
        fails += (not ok)
        print(f"  {'ok  ' if ok else 'FAIL'} followed={str(followed):<5} "
              f"want={str(must_follow):<5} {label}")

print()
if fails:
    print(f"FOLLOW CANARY FAILED ({fails} arm(s))")
    sys.exit(1)
print("verify_text FOLLOWS the venv idiom, keeps literal+relative forms, "
      "and does not follow a path that does not exist")
