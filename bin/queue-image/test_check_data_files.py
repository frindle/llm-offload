#!/usr/bin/env python3
"""Tests for check-data-files.py, on made-up trees."""
import importlib.util
import io
import sys
import tempfile
from contextlib import redirect_stdout
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("cdf", HERE / "check-data-files.py")
cdf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(cdf)

fails = 0


def check(name, got, want):
    global fails
    if got != want:
        fails += 1
        print(f"FAIL {name}: got {got!r} want {want!r}")
    else:
        print(f"PASS {name}")


def tree(files, manifest):
    d = Path(tempfile.mkdtemp())
    for rel, text in files.items():
        (d / rel).parent.mkdir(parents=True, exist_ok=True)
        (d / rel).write_text(text)
    (d / "MANIFEST").write_text(manifest)
    return d


def run(d):
    out = io.StringIO()
    with redirect_stdout(out):
        rc = cdf.main(["--base", str(d), "--list", str(d / "MANIFEST")])
    return rc, out.getvalue()


runner = 'OVERLAY = HOME / "bin/configs/mini-local.yaml"\nP = "model_profiles.yaml"\n'
d = tree({"runner.py": runner, "configs/mini-local.yaml": "a: 1\n", "model_profiles.yaml": "b: 2\n"}, "runner.py\n")
rc, out = run(d)
check("unlisted data files fail", rc, 1)
check("each miss is named with the file that names it",
      sorted(l for l in out.splitlines() if l.startswith("MISSING")),
      ["MISSING configs/mini-local.yaml  (named in runner.py)", "MISSING model_profiles.yaml  (named in runner.py)"])

d = tree({"runner.py": runner, "configs/mini-local.yaml": "a: 1\n", "model_profiles.yaml": "b: 2\n"},
         "runner.py\n# overlays\nconfigs/mini-local.yaml\nmodel_profiles.yaml  # profiles\n")
check("listed data files pass", run(d)[0], 0)

d = tree({"runner.py": 'A = "~/bin/x.json"\nB = "/bin/y.json"\nC = "gone.yaml"\nD = "../up.txt"\n',
          "x.json": "{}", "y.json": "{}"}, "runner.py\n")
rc, out = run(d)
check("~/bin/ is dropped; absolute paths and names with no file are ignored",
      sorted(l.split()[1] for l in out.splitlines() if l.startswith("MISSING")), ["x.json"])

d = tree({"notes.md": 'see "data.yaml"\n', "data.yaml": "x: 1\n", "runner.py": "pass\n"}, "notes.md\nrunner.py\n")
check("only code files are read", run(d)[0], 0)

d = tree({"runner.py": 'L = "real.yaml"\n', "real.yaml": "x: 1\n"}, "runner.py\n")
(d / "link.yaml").symlink_to(d / "real.yaml")
(d / "runner.py").write_text('L = "link.yaml"\n')
check("a symlinked target is ignored", run(d)[0], 0)

d = tree({"mini_ctx.py": 'S = "bloom_control.py"\n', "bloom_control.py": "pass\n"}, "mini_ctx.py\n")
check("NOT_SHIPPED names pass", run(d)[0], 0)
check("every NOT_SHIPPED entry has a reason", all(v.strip() for v in cdf.NOT_SHIPPED.values()), True)

api = ("import json\n\ndef route(h):\n    try:\n        import docsum_proxy\n    except Exception:\n        return 503\n"
       "    return docsum_proxy.proxy(h)\n")
d = tree({"api.py": api, "docsum_proxy.py": "pass\n"}, "api.py\n")
rc, out = run(d)
check("an unlisted module imported inside a function fails (the 11-17 /docsum 503)", rc, 1)
check("the miss says which file imports it",
      [l for l in out.splitlines() if l.startswith("MISSING")], ["MISSING docsum_proxy.py  (imported by api.py)"])
check("listing the imported module passes", run(tree({"api.py": api, "docsum_proxy.py": "pass\n"},
                                                     "api.py\ndocsum_proxy.py\n"))[0], 0)

d = tree({"api.py": "from helper import x\nfrom helper2.sub import y\nfrom . import rel\nimport os, pkgmod.deep\n",
          "helper.py": "x = 1\n", "helper2.py": "y = 1\n", "rel.py": "", "os.txt": ""}, "api.py\n")
check("`from X import` and dotted names count; relative imports and names with no X.py don't",
      sorted(l.split()[1] for l in run(d)[1].splitlines() if l.startswith("MISSING")), ["helper.py", "helper2.py"])

d = tree({"api.py": "import linked\n", "real/linked.py": "pass\n"}, "api.py\n")
(d / "linked.py").symlink_to(d / "real" / "linked.py")
check("a symlinked module counts (dashboard files are links in ~/bin)", run(d)[0], 1)

d = tree({"mini_ctx.py": "import bloom_control\n", "bloom_control.py": "pass\n"}, "mini_ctx.py\n")
check("a NOT_SHIPPED module may be imported", run(d)[0], 0)

d = tree({"broken.py": "def f(:\n", "x.py": ""}, "broken.py\n")
check("a .py file that does not parse is skipped, not a crash", run(d)[0], 0)

d = tree({"run.sh": "python3 -c 'import helper'\n", "helper.py": ""}, "run.sh\n")
check("only .py files are read for imports", run(d)[0], 0)

d = tree({}, "")
check("an empty manifest passes", run(d)[0], 0)
rc = cdf.main(["--base", str(d / "nope")])
check("a missing base is a usage error", rc, 2)

print("%d failed" % fails)
sys.exit(1 if fails else 0)
