#!/usr/bin/env python3
"""test-queue-bundle8.py -- queue-move bundle 8 (L1 test-draft jobs remote, L2 remote validate job, L3 studio guard).
Each check goes red when its change is reverted. Fake data only; no network, no queue.
Run (sandbox only): python3 tests/test-queue-bundle8.py [--bin PIPELINE_BIN]  (--bin = qd-submit files with qd-dash src/*.py overlaid)"""
import argparse
import importlib.machinery
import importlib.util
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--bin", default=os.path.join(os.environ["HOME"], "bin"))
BIN = Path(ap.parse_args().bin)
FAILS = []


def ok(name, cond, extra=""):
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else "  -- %s" % (extra,)))
    if not cond:
        FAILS.append(name)


T = Path(tempfile.mkdtemp(prefix="b8-"))
os.environ["HOME"] = str(T / "home")
(T / "home").mkdir()
for k in [k for k in os.environ if k.startswith("QUEUE_")]:
    del os.environ[k]
sys.path.insert(0, str(BIN))
# docsum_proxy lives in this repo's src/; append so a --bin overlay still wins (the publish gate runs without one)
sys.path.append(str(Path(__file__).resolve().parent.parent / "src"))
import queue_remote as QR            # noqa: E402
import queue_upload as QU            # noqa: E402

CMD = 'python3 "$HOME/bin/mini-testdraft" check'


def refused(cmd):
    try:
        QR.parse_draft_cmd(cmd)
        return False
    except QR.RemoteError:
        return True


# ---- L1: parse / refuse
good = QR.parse_draft_cmd("python3 /x/bin/mini-testdraft check /w/spec.json --ref /w/ref --baseline /w/base --draft {draft}")
ok("L1 parse: a plain accept command parses", good["spec"] == "/w/spec.json" and good["ref"] == "/w/ref" and good["baseline"] == "/w/base", good)
S = "python3 /x/mini-testdraft check s.json --draft {draft}"
for tag, cmd in (("--allow-unscored", S + " --allow-unscored"), ("--shadow", S + " --shadow"), ("--keep", S + " --keep"),
                 ("--min-mutation 0", S + " --min-mutation 0"), ("--min-mutation 0.5", S + " --min-mutation 0.5"),
                 ("unknown flag", S + " --frobnicate 1"), ("other script", "python3 /x/evil.py check s.json --draft {draft}"),
                 ("no --draft", "python3 /x/mini-testdraft check s.json"), ("shell word", "sh -c 'x'"),
                 ("two --draft", S + " --draft {draft}")):
    ok("L1 refused: " + tag, refused(cmd), cmd)
ok("L1 kept: --min-mutation 0.9 and --max-mutants 50", not refused(S + " --min-mutation 0.9 --max-mutants 50"))

# ---- L1: package rewrite
W = T / "work"
base, ref = W / "base", W / "ref"
for d in (base, ref):
    (d / "pkg").mkdir(parents=True)
    (d / "pkg" / "m.py").write_text("x = 1\n")
(base / "tests").mkdir()
(base / "tests" / "test_m.py").write_text("def test_a():\n    assert True\n")
(W / "task.md").write_text("fix the thing\n")
orig = {"baseline_from": str(base), "test_file": "tests/test_m.py", "task": str(W / "task.md"), "test_cmd": "python -m pytest tests"}
(W / "orig.json").write_text(json.dumps(orig))
dspec = {"baseline_from": str(base), "ref": str(ref), "phase_a_only": True, "task": str(W / "task.md"),
         "draft_accept_cmd": "python3 %s/mini-testdraft check %s --ref %s --baseline %s --draft {draft} --max-mutants 40"
                             % (BIN, W / "orig.json", ref, base)}
(W / "draft.json").write_text(json.dumps(dspec))


def pack(spec_file, name):
    dest = T / (name + ".tar")
    man = QR.build_package(spec_file, {"model": "m", "label": "l"}, "mini-swe-runner.py", dest)
    with tarfile.open(dest) as tf:
        names = tf.getnames()
        spec = json.loads(tf.extractfile("spec.json").read())
    return man, spec, names


man, pspec, names = pack(W / "draft.json", "draft")
cmd = pspec["draft_accept_cmd"]
ok("L1 package: cmd runs the queue host's mini-testdraft", cmd.startswith(CMD + " "), cmd)
ok("L1 package: no local path left in the cmd", str(W) not in cmd and str(BIN) not in cmd and "/__qdraft_spec__/spec.json" in cmd, cmd)
ok("L1 package: --max-mutants kept, floor flags absent", "--max-mutants 40" in cmd and "--allow-unscored" not in cmd, cmd)
subs = [c for c in man.get("cmd_refs", []) if c["path"] == ["draft_accept_cmd"]]
ok("L1 package: three cmd_refs for the placeholders", sorted(c["from"] for c in subs) ==
   sorted(QR.DRAFT_PH.values()), subs)
spec_sub = [c for c in subs if c["from"] == QR.DRAFT_PH["spec"]]
ok("L1 package: the spec placeholder points at the shipped spec.json", spec_sub and spec_sub[0]["rel"] == "spec.json", subs)
ok("L1 package: original spec shipped as its own tree", any(n.endswith("overlay/spec.json") for n in names), names)
shipped = [n for n in names if n.endswith("overlay/spec.json")][0]
with tarfile.open(T / "draft.tar") as tf:
    inner = json.loads(tf.extractfile(shipped).read())
ok("L1 package: shipped spec has no ref / accept cmd and a relative task", "ref" not in inner and "draft_accept_cmd" not in inner
   and not str(inner.get("task", "/")).startswith("/"), inner)
ok("L1 package: the task file is shipped beside it", any(os.path.basename(n).startswith("task-") for n in names), names)

# weakened gate stays refused at pack time
for tag, extra in (("--allow-unscored", " --allow-unscored"), ("--min-mutation 0", " --min-mutation 0")):
    bad = dict(dspec, draft_accept_cmd=dspec["draft_accept_cmd"] + extra)
    (W / "bad.json").write_text(json.dumps(bad))
    try:
        pack(W / "bad.json", "bad")
        ok("L1 package refuses " + tag, False)
    except QR.RemoteError:
        ok("L1 package refuses " + tag, True)
try:
    QR.check_paths(json.loads((W / "bad.json").read_text()), W)
    ok("L1 check_paths refuses a weakened draft gate", False)
except QR.RemoteError:
    ok("L1 check_paths refuses a weakened draft gate", True)

# a non-draft spec is packed as before: spec.json byte-identical, no draft subs
plain = {"baseline_from": str(base), "test_file": "tests/test_m.py", "task": "inline task text that is long enough to be a prompt"}
(W / "plain.json").write_text(json.dumps(plain))
pman, pplain, _n = pack(W / "plain.json", "plain")
ok("L1 non-draft spec: unchanged", pplain == plain and not [c for c in pman.get("cmd_refs", []) if c["path"] == ["draft_accept_cmd"]], pplain)

# mini-testdraft --baseline
src = (BIN / "mini-testdraft").read_text()
ok("L1 mini-testdraft check takes --baseline", '"--baseline"' in src and "baseline_from" in src)
ok("L1 mini-testdraft is in the image MANIFEST", re.search(r"^mini-testdraft\s*$", (BIN / "queue-image" / "MANIFEST").read_text(), re.M) is not None)

# mini-finish: remote draft jobs no longer refused; check fetches the draft files
mf = (BIN / "mini-finish").read_text()
ok("L1 mini-finish no longer refuses a remote test-draft job", "test-draft job remote" not in mf)
mfr = (BIN / "mini_finish_remote.py").read_text()
ok("L1 mini_finish_remote fetches DRAFT-CHECK files", "def fetch_draft_files" in mfr and "DRAFT-CHECK" in mfr)

# ---- L2: validate job
ok("L2 allowlist: --validate-spec accepted, takes no value", QU.RUNNER_FLAGS.get("--validate-spec") is False, QU.RUNNER_FLAGS)
ok("L2 allowlist: unknown flags still absent", "--validate-everything" not in QU.RUNNER_FLAGS and "--allow-unscored" not in QU.RUNNER_FLAGS)
import mini_validate_remote as MVR  # noqa: E402
spec = {"baseline_from": str(base), "test_file": "tests/test_m.py", "task": "inline task text that is long enough to be a prompt",
        "red_ok": False}
sp = W / "vspec.json"
sp.write_text(json.dumps(spec))
mod = MVR._load_validate()
runner_sha = mod.sha_file(mod.RUNNER)[:12]
remote_text = "## Fingerprint\n\n- spec: deadbeef\n- test s1 tests/test_m.py: 00\n\n- runner: /queue/bin/mini-swe-runner.py sha=%s\n- test cap: %d\n\nRESULT: PASS\n" % (runner_sha, mod.FIX_JOB_MAX_TESTS)
changes = MVR.apply_fills(spec, {"red_ok": True, "fn_scope": "m.f", "unrelated": 1})
ok("L2 apply_fills copies fill keys only", spec.get("red_ok") is True and spec.get("fn_scope") == "m.f" and "unrelated" not in spec and len(changes) == 2, spec)
sp.write_text(json.dumps(spec))
MVR.write_validation(sp, remote_text, "abcdef012345")
vtxt = (W / "VALIDATION.md").read_text()
ok("L2 write_validation: marker line after the runner line", "- validated on the queue: job abcdef012345" in vtxt and MVR.marked_job(W / "VALIDATION.md") == "abcdef012345", vtxt)
ok("L2 write_validation: fingerprint recomputed locally", "deadbeef" not in vtxt and mod.sha_file(sp) in vtxt, vtxt)
p = subprocess.run([sys.executable, str(BIN / "mini-validate"), "--check-fresh", str(sp)], capture_output=True, text=True, env=dict(os.environ))
ok("L2 mini-validate --check-fresh passes on the written VALIDATION.md", p.returncode == 0, p.stdout[-300:] + p.stderr[-300:])
sp.write_text(json.dumps(dict(spec, task="changed " * 10)))
p = subprocess.run([sys.executable, str(BIN / "mini-validate"), "--check-fresh", str(sp)], capture_output=True, text=True, env=dict(os.environ))
ok("L2 a spec edit after the remote validate is stale (fail closed)", p.returncode != 0, p.stdout[-200:])
try:
    MVR.write_validation(sp, "no fingerprint here\nRESULT: PASS\n", "abcdef012345")
    ok("L2 write_validation refuses a VALIDATION.md without a fingerprint block", False)
except QR.RemoteError:
    ok("L2 write_validation refuses a VALIDATION.md without a fingerprint block", True)
(W / "VALIDATION.md").write_text("- runner: x sha=1\n")
ok("L2 an unmarked VALIDATION.md is not a queue validation", MVR.marked_job(W / "VALIDATION.md") is None)
s1 = MVR.snapshot(sp)
ok("L2 snapshot is stable when nothing changes", MVR.snapshot(sp) == s1)
_tf = Path(base) / "tests" / "test_m.py"
_old = _tf.read_text()
_tf.write_text(_old + "\n# edited while the validate job ran\n")
ok("L2 snapshot changes when a frozen test changes during the wait", MVR.snapshot(sp) != s1)
_tf.write_text(_old)
sp.write_text(sp.read_text() + " ")
ok("L2 snapshot changes when the spec text changes during the wait", MVR.snapshot(sp) != s1)
ok("L2 remote_validate refuses a PASS when the snapshot changed",
   "before = snapshot(spec_path)" in (BIN / "mini_validate_remote.py").read_text()
   and "snapshot(spec_path) != before" in (BIN / "mini_validate_remote.py").read_text())

mf_cont = mf[mf.index("def continuation"):]
ok("L2 continuation: remote branch runs the validate job", "remote_validate" in mf_cont and "remote()" in mf_cont[:3000], "")
ms = (BIN / "mini-submit").read_text()
ok("L2 mini-submit: C5/C6 covered by the queue's validate job when remote", "c5_remote" in ms and "covered by the queue's validate job" in ms)
rn = (BIN / "mini-swe-runner.py").read_text()
ok("L2 runner has --validate-spec running mini-validate, C5, C6", "--validate-spec" in rn and "def validate_spec_job" in rn
   and "validate-c5" in rn and "validate-c6" in rn)
# the validate flag runs only inside the queue's runner: the client must not run it locally
ok("L2 local mode: mini-submit gates the remote path on _remote_queue()", "_remote_queue()" in ms)

# ---- L3
import docsum_proxy as DP            # noqa: E402
ok("L3 docsum default upstream is unchanged", DP.current_upstream() == ("127.0.0.1", 7690), DP.current_upstream())
os.environ["QUEUE_DOCSUM_UPSTREAM"] = "docsum.example.test:7799"
ok("L3 docsum upstream from env", DP.current_upstream() == ("docsum.example.test", 7799), DP.current_upstream())
ok("L3 upstream_headers rewrites Host to the configured upstream",
   DP.upstream_headers({"Host": "q.example.test"}, DP.current_upstream())["Host"] == "docsum.example.test:7799")
os.environ["QUEUE_DOCSUM_UPSTREAM"] = "not a host"
ok("L3 a malformed upstream falls back to the default", DP.current_upstream() == ("127.0.0.1", 7690), DP.current_upstream())
del os.environ["QUEUE_DOCSUM_UPSTREAM"]

print("%d failed" % len(FAILS) if FAILS else "all passed")
sys.exit(1 if FAILS else 0)
