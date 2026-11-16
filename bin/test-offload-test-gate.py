#!/usr/bin/env python3
"""Tests for offload-test-gate (baseline compare, hidden check, flow with a fake ssh) and offload-repo-sync (scrub, scans,
runtime check, gate hook).
No network: ssh is a fake command on PATH."""
import importlib.machinery, importlib.util, json, os, re, shutil, subprocess, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ldr = importlib.machinery.SourceFileLoader("gate", str(HERE / "offload-test-gate"))
G = importlib.util.module_from_spec(importlib.util.spec_from_loader("gate", ldr)); ldr.exec_module(G)
ldr = importlib.machinery.SourceFileLoader("sync", str(HERE / "offload-repo-sync"))
S = importlib.util.module_from_spec(importlib.util.spec_from_loader("sync", ldr)); ldr.exec_module(S)
f = []
def ok(c, m):
    if not c: f.append(m); print("FAIL", m)

# --- compare logic
base = {"a": "pass", "b": "pass", "c": "fail", "d": "fail", "gone": "pass"}
c = G.compare(base, {"a": 0, "b": 1, "c": 1, "d": 0, "new_ok": 0, "new_bad": 2})
ok(c["new_fail"] == ["b", "new_bad"], "new failures: a passing test that fails + an unknown failing test")
ok(c["still_fail"] == ["c"], "already failing stays non-blocking")
ok(c["fixed"] == ["d"] and c["new_pass"] == ["new_ok"] and c["removed"] == ["gone"], "fixed / new / removed")
ok(c["passing"] == 3 and c["failing"] == 3, "counts")
ok(not G.compare({"a": "pass"}, {"a": 0})["new_fail"], "all green")
ok(G.updated_baseline(base, {"a": 0, "d": 0, "c": 7}) == {"a": "pass", "c": "fail", "d": "pass"}, "baseline update moves fixed to pass")
ok(G.hidden_count("0\n") == 0 and G.hidden_count(" 3 ") == 3, "hidden count parse")
ok(G.hidden_count("") is None and G.hidden_count("ls: error") is None, "hidden count garbage -> None")
r = G.parse_results('noise\nGATE-RESULT {"t.py": [1, ["x", "y"]], "u.py": [0, []]}\n')
ok(r == {"t.py": (1, ["x", "y"]), "u.py": (0, [])}, "parse results")
try:
    G.parse_results("nothing"); ok(False, "missing result line must raise")
except G.GateError:
    ok(True, "")

# --- end to end with a fake ssh
def gitrepo(d, files):
    d.mkdir(parents=True, exist_ok=True)
    g = lambda *a: subprocess.run(["git", "-C", str(d), "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                  check=True, capture_output=True)
    g("init", "-q")
    for n, t in files.items(): (d / n).write_text(t)
    g("add", "-A"); g("commit", "-q", "-m", "x")
    return d

FAKE = r'''#!/usr/bin/env python3
import os, sys
a = [x for x in sys.argv[1:] if x not in ("-o", "BatchMode=yes", "ConnectTimeout=15")]
cmd = a[-1]
open(os.environ["FAKE_LOG"], "a").write(cmd.split()[0] + " " + " ".join(cmd.split()[1:3]) + "\n")
if "hidden" in cmd:
    print(os.environ.get("FAKE_HIDDEN", "0"))
elif cmd.startswith("python3"):
    sys.stdin.read(); sys.stdout.write(open(os.environ["FAKE_RESULT"]).read())
elif "tar -x" in cmd:
    sys.stdin.buffer.read()
if os.environ.get("FAKE_FAIL_MATCH") and os.environ["FAKE_FAIL_MATCH"] in cmd:
    sys.exit(1)
sys.exit(int(os.environ.get("FAKE_RC_" + cmd.split()[0], "0")))
'''
with tempfile.TemporaryDirectory() as t:
    t = Path(t)
    (t / "pb").mkdir(); (t / "pb/ssh").write_text(FAKE); (t / "pb/ssh").chmod(0o755)
    bin_, dash = gitrepo(t / "bin", {"a.py": "1\n"}), gitrepo(t / "dash", {"b.py": "2\n"})
    env = {**os.environ, "PATH": "%s:%s" % (t / "pb", os.environ["PATH"]), "FAKE_LOG": str(t / "ssh.log"),
           "FAKE_RESULT": str(t / "res.txt"), "OFFLOAD_GATE_BIN": str(bin_), "OFFLOAD_GATE_DASH": str(dash),
           "OFFLOAD_GATE_BASELINE": str(t / "base.json"), "OFFLOAD_GATE_CARDS": str(t / "cards"),
           "OFFLOAD_GATE_SHIM": str(t / "shim"), "OFFLOAD_GATE_LOCK": str(t / "gate.lock")}
    def gate(*args, **ev):
        r = subprocess.run([sys.executable, str(HERE / "offload-test-gate"), *args], env={**env, **ev}, capture_output=True, text=True)
        return r.returncode, r.stdout, r.stderr
    def results(d): (t / "res.txt").write_text("GATE-RESULT " + json.dumps({n: [rc, ["line %s" % n]] for n, rc in d.items()}) + "\n")
    base_p = t / "base.json"

    results({"t1.py": 0, "t2.py": 1})
    rc, out, err = gate()
    ok(rc == 2 and "--take-baseline" in err, "no baseline -> exit 2 with hint")
    rc, out, err = gate("--take-baseline")
    b = json.loads(base_p.read_text())
    ok(rc == 0 and b["tests"] == {"t1.py": "pass", "t2.py": "fail"}, "take-baseline writes pass/fail per test: %s" % err)
    rc, out, err = gate()
    ok(rc == 0 and "1 passing, 1 failing" in out, "same results -> exit 0 (%s)" % out)
    results({"t1.py": 1, "t2.py": 1})
    rc, out, err = gate()
    ok(rc == 1 and "NEW FAILURE: t1.py" in out and "| line t1.py" in out and "already failing" in out, "new failure blocks, shows output, lists old")
    ok(json.loads(base_p.read_text())["tests"]["t1.py"] == "pass", "baseline unchanged on failure")
    results({"t1.py": 0, "t2.py": 0, "t3.py": 0})
    rc, out, err = gate("--json")
    ok(rc == 0 and json.loads(out.strip().splitlines()[-1])["fixed"] == ["t2.py"], "--json summary")
    ok(json.loads(base_p.read_text())["tests"] == {"t1.py": "pass", "t2.py": "pass", "t3.py": "pass"}, "fixed test moved to passing")
    # hidden data stops everything: no tar, no run
    (t / "ssh.log").write_text("")
    rc, out, err = gate(FAKE_HIDDEN="2")
    ok(rc == 3 and "hidden data on sandbox" in out, "hidden != 0 -> exit 3")
    ok("tar" not in (t / "ssh.log").read_text() and "python3" not in (t / "ssh.log").read_text(), "nothing shipped or run when hidden")
    rc, out, err = gate(FAKE_RC_ls="255")
    ok(rc == 4, "ssh failure on the hidden check -> exit 4")
    # untracked fixture dirs ship as a third tar (data the tests read; git ignores them)
    (t / "ssh.log").write_text(""); gate()
    ok("tar -x -C" not in (t / "ssh.log").read_text(), "no fixture dirs -> no fixture tar")
    (bin_ / "test-fixtures-x").mkdir(); (bin_ / "test-fixtures-x" / "case.json").write_text("{}\n")
    (t / "ssh.log").write_text(""); rc, out, err = gate()
    ok(rc == 0 and (t / "ssh.log").read_text().count("tar -x -C") == 1, "fixture dir shipped once: %s" % err)
    ok(G.ship_fixtures.__doc__ and "fixtures-" in G.FIXTURE_PREFIXES, "fixture prefixes")
    rc, out, err = gate(FAKE_RC_tar="1")
    ok(rc == 4 and "shipping fixtures failed" in err, "fixture ship failure -> exit 4 (%s)" % err)
    import shutil; shutil.rmtree(bin_ / "test-fixtures-x")
    # vendored model cards ship to <WORK>/references when present (MODEL_CARDS_ROOT for the profile tests)
    (t / "ssh.log").write_text("")
    rc, out, err = gate()
    ok("references" not in (t / "ssh.log").read_text(), "no cards dir -> no cards tar")
    (t / "cards" / "models").mkdir(parents=True); (t / "cards" / "models" / "m.md").write_text("card\n")
    (t / "ssh.log").write_text("")
    rc, out, err = gate()
    ok(rc == 0 and (t / "ssh.log").read_text().count("mkdir -p work/test-gate/references") == 1, "cards shipped once: %s" % err)
    rc, out, err = gate(FAKE_FAIL_MATCH="references")
    ok(rc == 4 and "shipping models failed" in err, "cards ship failure -> exit 4 (%s)" % err)
    shutil.rmtree(t / "cards")
    (t / "shim").mkdir(); (t / "shim" / "vault_shim.py").write_text("pass\n"); (t / "ssh.log").write_text("")
    rc, out, err = gate()
    ok(rc == 0 and (t / "ssh.log").read_text().count("mkdir -p work/test-gate/vault-shim") == 1, "shim shipped: %s" % err)
    shutil.rmtree(t / "shim")
    # cleanup unless --keep
    (t / "ssh.log").write_text(""); gate()
    ok(((t / "ssh.log").read_text().count("rm -rf")) >= 2, "sandbox dir removed")
    (t / "ssh.log").write_text(""); gate("--keep")
    ok(((t / "ssh.log").read_text().count("rm -rf")) == 1, "--keep leaves the sandbox dir (only the pre-run wipe)")
    # one run at a time even against another baseline file: the runs share the sandbox dir
    import fcntl, time
    shutil.copy(base_p, t / "base-copy.json")
    held = open(t / "gate.lock", "a"); fcntl.flock(held, fcntl.LOCK_EX)
    p = subprocess.Popen([sys.executable, str(HERE / "offload-test-gate")], env={**env, "OFFLOAD_GATE_BASELINE": str(t / "base-copy.json")},
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    time.sleep(2)
    waiting = p.poll() is None
    fcntl.flock(held, fcntl.LOCK_UN); held.close()
    out, err = p.communicate(timeout=60)
    ok(waiting and p.returncode == 0 and "another run is in progress" in err,
       "a run against a copied baseline waits for the running gate (%r %r %r)" % (waiting, p.returncode, err[-200:]))

    # --- offload-repo-sync. Private-looking values are built from parts, so this file passes the scrub unchanged.
    ip = lambda *p: ".".join(map(str, p))
    LAN, MACADDR, MAIL = ip(10, 0, 12, 5), ":".join(["02", "70", "0a", "00", "0c", "35"]), "ops" + "@" + "corp-mail.com"
    SECRET = "gh" + "p_" + "A1b2" * 9
    priv, pats = t / "priv", t / "patterns.txt"
    priv.mkdir(); (priv / "scrub-rules.txt").write_text("(?i)zorblax\towner\n"); pats.write_text("zorblax\nquillon\n")
    (priv / "public-words.txt").write_text("quillon\n")
    home = t / "home"
    gitrepo(home / "bin", {"x.py": "print(1)\n", "zorblax-notes.txt": "Zorblax at %s and %s, mac %s\n" % (LAN, LAN, MACADDR)})
    repo = gitrepo(t / "repo", {"MANIFEST.txt": "[bin-exclude]\n"})
    fake_gate = t / "fakegate"; fake_gate.write_text('#!/bin/sh\necho "$@" >> "$GATE_MARK"\nexit ${GATE_RC:-0}\n'); fake_gate.chmod(0o755)
    skips = t / "skips.log"; mark = t / "mark"
    senv = {k: v for k, v in os.environ.items() if not any(w in k.upper() for w in ("KEY", "TOKEN", "SECRET", "PASSW", "AUTH"))}
    senv.update(HOME=str(home), CLAUDE_OFFLOAD_REPO=str(repo), OFFLOAD_TEST_GATE=str(fake_gate), OFFLOAD_GATE_SKIPS_LOG=str(skips),
                GATE_MARK=str(mark), OFFLOAD_PRIVATE_DIR=str(priv), OFFLOAD_PRIVATE_PATTERNS=str(pats))
    def sync(*args, **ev):
        r = subprocess.run([sys.executable, str(HERE / "offload-repo-sync"), *args], env={**senv, **ev}, capture_output=True, text=True)
        return r.returncode, r.stdout + r.stderr
    def commits(): return subprocess.run(["git", "-C", str(repo), "rev-list", "--count", "HEAD"], capture_output=True, text=True).stdout.strip()
    def gitc(d, *a): subprocess.run(["git", "-C", str(d), "-c", "core.hooksPath=/dev/null", "-c", "user.name=t", "-c", "user.email=t@t", *a],
                                    check=True, capture_output=True)
    def bincommit(add=None, rm=()):
        for n, txt in (add or {}).items(): (home / "bin" / n).write_text(txt)
        for n in rm: gitc(home / "bin", "rm", "-q", n)
        gitc(home / "bin", "add", "-A"); gitc(home / "bin", "commit", "-qm", "c")
    n0 = commits()
    rc, out = sync("--dry-run")
    ok(rc == 0 and not mark.exists() and not skips.exists(), "--dry-run does not run the gate (%s)" % out)
    ok(commits() == n0 and not (repo / "bin").exists(), "--dry-run writes nothing")
    rc, out = sync("--no-push")
    ok(rc == 0 and not mark.exists() and commits() == str(int(n0) + 1), "--no-push does not run the gate, still commits (%s)" % out)
    # the scrub: names in contents and file names, addresses to documentation ranges (same input, same output)
    ok(not (repo / "bin/zorblax-notes.txt").exists() and (repo / "bin/owner-notes.txt").is_file(), "file name scrubbed")
    txt = (repo / "bin/owner-notes.txt").read_text() if (repo / "bin/owner-notes.txt").is_file() else ""
    addrs = [w.rstrip(",") for w in txt.split() if w.count(".") == 3]
    ok(txt.startswith("owner at ") and "orblax" not in txt and LAN not in txt and MACADDR not in txt, "contents scrubbed: %r" % txt)
    ok(len(addrs) == 2 and addrs[0] == addrs[1] and addrs[0].startswith(S.DOC_BLOCKS), "LAN address -> one documentation address: %r" % addrs)
    ok("00:00:5e:ef:10:" in txt, "MAC -> documentation MAC")
    ok((priv / "salt").is_file() and (priv / "salt").stat().st_mode & 0o077 == 0, "salt created, owner-only")
    ok(json.loads((repo / ".sync-state.json").read_text())["bin_head"], "sync state records the ~/bin commit")
    # leftovers stop the sync and never print the value
    n1 = commits()
    bincommit({"contact.txt": "write to %s\n" % MAIL})
    rc, out = sync("--no-push")
    ok(rc == 2 and "private-info scan" in out and "bin/contact.txt:1 email" in out and MAIL not in out and commits() == n1,
       "an email left after the scrub stops the sync (%s)" % out)
    (repo / ".private-allow").write_text("bin/contact.txt\tcorp-mail\\.com\ta fake address for the test\n")
    rc, out = sync("--no-push")
    ok(rc == 0 and commits() == str(int(n1) + 1), ".private-allow lets a known fake through (%s)" % out)
    n1 = commits()
    bincommit({"leak.py": "T = '%s'\n" % SECRET})
    rc, out = sync("--no-push")
    ok(rc == 2 and "secret guard" in out and "github-token" in out and SECRET not in out and commits() == n1, "a token stops the sync (%s)" % out)
    bincommit(rm=["leak.py"])
    (repo / "NOTES.md").write_text("ask Zorblax\n")
    rc, out = sync("--no-push")
    ok(rc == 2 and "(repo) NOTES.md:1 private pattern" in out and commits() == n1, "the repo's own files are scanned, not scrubbed (%s)" % out)
    (repo / "NOTES.md").write_text("ask quillon\n")
    rc, out = sync("--no-push")
    ok(rc == 0 and (repo / "NOTES.md").read_text() == "ask quillon\n", "public words pass; repo files are never rewritten (%s)" % out)
    # [runtime] files must be clean before the scrub
    rt = gitrepo(t / "rt", {"run.sh": "curl http://%s:7684/health\n" % LAN})
    (repo / "MANIFEST.txt").write_text("[bin-exclude]\n[git-sources]\nrt|%s\n[runtime]\nrt/*\n" % rt)
    n1 = commits()
    rc, out = sync("--no-push")
    ok(rc == 2 and "runtime check" in out and "rt/run.sh:1 private ip" in out and commits() == n1, "runtime file with a LAN address stops the sync (%s)" % out)
    (rt / "run.sh").write_text("curl http://${QUEUE_HOST:?}:7684/health\n"); gitc(rt, "commit", "-qam", "fix")
    rc, out = sync("--no-push")
    st = json.loads((repo / ".sync-state.json").read_text())
    ok(rc == 0 and (repo / "rt/run.sh").is_file() and st.get("rt_head"), "clean runtime source syncs; its commit is recorded (%s)" % out)
    # a file that leaves the source leaves the repo
    bincommit(rm=["contact.txt"])
    rc, out = sync("--no-push")
    ok(rc == 0 and not (repo / "bin/contact.txt").exists() and (repo / "bin/x.py").exists(), "removed source file leaves the repo (%s)" % out)
    rc, out = sync("--no-push", OFFLOAD_PRIVATE_DIR=str(t / "none"))
    ok(rc == 2 and "no scrub rules" in out, "no scrub rules -> refuses (%s)" % out)
    # gate wiring
    bincommit({"x.py": "print(2)\n"})
    n1 = commits()
    rc, out = sync(GATE_RC="1")
    ok(rc == 1 and "test gate exit 1" in out and commits() == n1, "failing gate stops the sync before any commit (%s)" % out)
    ok(mark.read_text().startswith("--commit "), "gate gets the commit")
    rc, out = sync("--no-gate", GATE_RC="1")
    line = skips.read_text().strip().split("\t") if skips.exists() else []
    ok(rc == 0 and len(line) == 3 and line[2] == "--no-gate" and len(line[1]) == 12, "--no-gate skips, logs time/commit/reason (%s)" % out)
    ok(commits() == str(int(n1) + 1), "--no-gate sync committed")
    bincommit({"x.py": "print(3)\n"})
    mark.unlink()
    rc, out = sync()
    ok(rc == 0 and mark.exists() and "no remote" in out, "passing gate lets the sync go on (%s)" % out)
    # push: an unpushed commit goes out on the next gated run even with no new changes; never a forced push
    bare = t / "remote.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True)
    gitc(repo, "remote", "add", "origin", str(bare))
    rc, out = sync()
    rhead = lambda d: subprocess.run(["git", "-C", str(d), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    ok(rc == 0 and "no changes" in out and "pushed" in out and rhead(bare) == rhead(repo), "no changes still pushes (%s)" % out)
    other = t / "other"
    subprocess.run(["git", "clone", "-q", str(bare), str(other)], check=True, capture_output=True)
    (other / "README.md").write_text("edited on GitHub\n"); gitc(other, "add", "-A"); gitc(other, "commit", "-qm", "web edit"); gitc(other, "push", "-q")
    bincommit({"x.py": "print(4)\n"})
    rc, out = sync()
    ok("push FAILED" in out and rhead(bare) == rhead(other), "a diverged remote is never overwritten (%s)" % out)

# --- the sandbox runner itself (G.RUNNER, run locally on a fake sandbox HOME): child `python3` is the gate's python
with tempfile.TemporaryDirectory() as td:
    rh = Path(td); vb = rh / "venv314" / "bin"; vb.mkdir(parents=True)
    for n in ("python", "python3"):
        os.symlink(sys.executable, vb / n)
    tb = rh / G.WORK / "bin"; tb.mkdir(parents=True)
    (tb / "test-which-python.py").write_text(
        "import os, shutil, sys\n"
        "w = shutil.which('python3')\n"
        "sys.exit(0 if w and os.path.dirname(w).endswith('venv314/bin') else 3)\n")
    (tb / "test-git-commit.py").write_text(
        "import subprocess, sys, tempfile\n"
        "d = tempfile.mkdtemp()\n"
        "g = lambda *a: subprocess.run(['git', '-C', d] + list(a), capture_output=True).returncode\n"
        "ok = g('init', '-q') == 0 and g('commit', '-q', '--allow-empty', '-m', 'x') == 0\n"
        "a = subprocess.run(['git', '-C', d, 'log', '-1', '--format=%an'], capture_output=True, text=True).stdout\n"
        "sys.exit(0 if ok and a.strip() == 'gate' else 3)\n")
    rr = subprocess.run([sys.executable, "-"], input=G.RUNNER.encode(), capture_output=True, timeout=120,
                        env={k: v for k, v in dict(os.environ, HOME=str(rh), PATH="/usr/bin:/bin").items()
                             if not k.startswith("GIT_")})
    got = G.parse_results(rr.stdout.decode()) if b"GATE-RESULT" in rr.stdout else {}
    ok(got.get("test-which-python.py", (None,))[0] == 0,
       "runner: a test's child python3 resolves to the gate's venv python (%r %r)" % (got, rr.stderr[-300:]))
    ok(got.get("test-git-commit.py", (None,))[0] == 0,
       "runner: a test can git commit in a scratch repo (identity in the throwaway HOME) (%r)" % (got,))

# --- the runner installs ts-mutator's lockfile deps once (stand-in npm), links them in, and leaves the tree alone
# when the install fails
with tempfile.TemporaryDirectory() as td:
    rh = Path(td); vb = rh / "venv314" / "bin"; vb.mkdir(parents=True)
    for n in ("python", "python3"):
        os.symlink(sys.executable, vb / n)
    tb = rh / G.WORK / "bin"; tm = tb / "ts-mutator"; (tm / "node_modules" / "typescript").mkdir(parents=True)
    (tm / "node_modules" / "typescript" / "README.md").write_text("shipped readme only\n")
    (tm / "package.json").write_text('{"dependencies": {"typescript": "5.7.3"}}\n')
    (tm / "package-lock.json").write_text('{"lockfileVersion": 3}\n')
    (tb / "test-ts-dep.py").write_text(
        "import os, sys\n"
        "p = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'ts-mutator', 'node_modules', 'typescript', 'package.json')\n"
        "sys.exit(0 if os.path.isfile(p) else 3)\n")
    gt = rh / "gate-tools" / "bin"; gt.mkdir(parents=True); calls = rh / "npm-calls"
    npm = gt / "npm"

    def fake_npm(fail):
        npm.write_text("#!/bin/sh\necho x >> %s\n%s\nmkdir -p node_modules/typescript\n"
                       "echo '{}' > node_modules/typescript/package.json\n" % (calls, "exit 1" if fail else ""))
        npm.chmod(0o755)

    def run_runner():
        rr = subprocess.run([sys.executable, "-"], input=G.RUNNER.encode(), capture_output=True, timeout=120,
                            env=dict(os.environ, HOME=str(rh), PATH="/usr/bin:/bin"))
        got = G.parse_results(rr.stdout.decode()) if b"GATE-RESULT" in rr.stdout else {}
        return got.get("test-ts-dep.py", (None,))[0], rr.stderr.decode("utf-8", "replace")

    def ncalls():
        return len(calls.read_text().split()) if calls.exists() else 0

    fake_npm(True)
    rc, err = run_runner()
    ok(rc == 3 and ncalls() == 1 and not (tm / "node_modules").is_symlink()
       and (tm / "node_modules" / "typescript" / "README.md").is_file() and "npm ci failed" in err,
       "runner: a failed ts-mutator install leaves the shipped tree as is (%r %d %r)" % (rc, ncalls(), err[-200:]))
    fake_npm(False)
    rc, err = run_runner()
    ok(rc == 0 and ncalls() == 2 and (tm / "node_modules").is_symlink()
       and "ts-mutator-cache" in os.readlink(tm / "node_modules"),
       "runner: ts-mutator deps installed from the lockfile and linked in (%r %d %r)" % (rc, ncalls(), err[-200:]))
    nmd = tm / "node_modules"
    if nmd.is_symlink(): os.unlink(nmd)
    else: shutil.rmtree(nmd)
    nmd.mkdir()
    rc, err = run_runner()
    ok(rc == 0 and ncalls() == 2 and (tm / "node_modules").is_symlink(),
       "runner: the cached install is reused, npm not run again (%r %d)" % (rc, ncalls()))
    ok(not [p for p in (rh / "gate-tools" / "ts-mutator-cache").iterdir() if p.name.startswith("install-")],
       "runner: no half-finished install dirs left in the cache")

# --- offload-repo-sync units
cfg = {"rules": [(re.compile("(?i)zorblax"), "owner")], "patterns": [re.compile("zorblax|quillon", re.I)],
       "public": {"quillon"}, "salt": b"k1"}
ip = lambda *p: ".".join(map(str, p))
LAN = ip(10, 0, 12, 5)
a = S.scrub_text(LAN, cfg)
ok(a.startswith(S.DOC_BLOCKS) and a == S.scrub_text(LAN, cfg), "address scrub is stable")
ok(S.scrub_text(ip(10, 0, 0, 0) + "/8", cfg) == ip(10, 0, 0, 0) + "/8", "RFC range names stay")
ok(S.scrub_text(ip(8, 8, 8, 8), cfg) == ip(8, 8, 8, 8), "public address stays")
ok(S.scrub_text(LAN + "/24", cfg).endswith("/24"), "prefix kept")
m = S.scrub_text("-".join(["02", "70", "0a", "00", "0c", "35"]), cfg)
ok(m.startswith("00-00-5e-ef-10-"), "MAC keeps its separator: %s" % m)
ok(S.scan_line(LAN, cfg) == ["private ip"] and S.scan_line(a, cfg) == [], "scan: LAN hit, documentation address clean")
ok("home path" in S.scan_line("/Users/" + "zorblax/bin", cfg), "scan: real home path")
ok(S.scan_line("/Users/<you>/bin /Users/$USER/x /home/someone/y /Users/p", cfg) == [], "scan: placeholder home paths")
ok(S.scan_line("ops" + "@" + "corp-mail.com", cfg) == ["email"], "scan: email")
ok(S.scan_line("a" + "@" + "example.invalid noreply" + "@" + "anthropic.com", cfg) == [], "scan: fake emails")
ok(S.scan_line("Zorblax", cfg) == ["private pattern"] and S.scan_line("quillon", cfg) == [], "scan: private pattern, public word")

print("FAILED %d" % len(f) if f else "ok")
sys.exit(1 if f else 0)
