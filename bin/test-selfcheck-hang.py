#!/usr/bin/env python3
"""Self-check / verify HANG protection (2026-10-06, rt-bfmr-tls-fingerprint -c2: four tests whose fake
response stream never ended, node:test --test-timeout is PER TEST, so the driver's self-check froze
the committed bundle -- and every queue lane -- until the 900s kill).
Behavioural:
  * auto-harness-check kills a hanging verify's WHOLE process group at DISPATCH_SELFCHECK_CMD_TIMEOUT_S
    and reports 'SELFCHECK_HANG: self-check timed out after Ns; likely a test blocked on network/IO...'
    (baseline hang AND refimpl-applied hang), leaving the target at baseline, no orphan child.
  * the driver's _harness_check_output enforces its own wall, reports SELFCHECK_HANG, ledgers an event, alerts.
  * the scaffold TS verify uses a 10s per-test timeout and a network-denying preload (fetch + non-loopback
    sockets fail fast, loopback still works).
  * failure_ledger classifies a failed job whose output says SELFCHECK_HANG as signature selfcheck-hang.
`test-selfcheck-hang.py` runs the suite on the real files, then on mutants (each must FAIL)."""
import importlib.util, json, os, re, shutil, subprocess, sys, tempfile, time
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent


def suite(d):
    d = Path(d)
    AUTO, SCAF, FLP = d / "ollama-dispatch-auto", d / "ollama-dispatch-scaffold", d / "failure_ledger.py"
    T = Path(tempfile.mkdtemp(prefix="sch-"))
    os.environ.update({"FAILURE_LEDGER": str(T / "failures.jsonl"), "FAILURE_LEDGER_BIN": str(T),
                       "FAILURE_LEDGER_QUEUE_LOGS": str(T / "q"), "FAILURE_LEDGER_LIVELOGS": str(T / "l"),
                       "FAILURE_LEDGER_STATE": str(T / "s.json"), "TRIAGE_DIR": str(T / "triage")})
    fails = []
    import random
    U = random.randint(100000, 999999)      # unique per run: concurrent suites/canaries must not see each other's sleeps
    S1, S2, S3 = ("299.%d%d" % (U, k) for k in (1, 2, 3))

    def chk(n, ok, extra=""):
        print(("ok   - " if ok else "FAIL - ") + n + ("" if ok else "  " + str(extra)[-500:]))
        if not ok:
            fails.append(n)

    sys.path.insert(0, str(d))
    ld = SourceFileLoader("oda_sch", str(AUTO))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("oda_sch", ld))
    sys.argv = [str(AUTO)]
    ld.exec_module(m)

    def mk(verify_sh):
        wt = Path(tempfile.mkdtemp(prefix="sch-wt-")) / "wt"
        (wt / "lib").mkdir(parents=True)
        g = lambda *a: subprocess.run(["git", "-C", str(wt), *a], check=True, capture_output=True)
        g("init", "-q"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
        (wt / "lib" / "t.ts").write_text("export const x = 1;\n")
        g("add", "."); g("commit", "-qm", "b")
        (wt / "TASK.md").write_text("## Must contain\n- `MARK`\nOnly edit `lib/t.ts`\n")
        (wt / "verify.test.ts").write_text("// fixture\n")
        (wt / "verify.sh").write_text(verify_sh)
        (wt / "refimpl.py").write_text("open('lib/t.ts','a').write('// MARK\\n')\n")
        (wt / ".dispatch-harness.json").write_text(json.dumps({"target": "lib/t.ts"}))
        m.write_harness_check(wt, "ts")
        return wt

    def hc(wt, secs=3):
        t0 = time.time()
        p = subprocess.run([sys.executable, "auto-harness-check.py"], cwd=wt, capture_output=True, text=True,
                           timeout=120, env=dict(os.environ, DISPATCH_SELFCHECK_CMD_TIMEOUT_S=str(secs)))
        return p.returncode, p.stdout + p.stderr, time.time() - t0

    def leftover(tag):
        return subprocess.run(["pgrep", "-f", tag], capture_output=True, text=True).stdout.strip()

    # 1) baseline verify hangs
    wt = mk("sleep %s & wait\n" % S1)
    rc, out, dt = hc(wt)
    chk("baseline hang: harness exits non-zero fast", rc != 0 and dt < 60, (rc, dt, out))
    chk("baseline hang: clear SELFCHECK_HANG message", "SELFCHECK_HANG" in out and "timed out after 3s" in out
        and "network/IO" in out and "(baseline)" in out, out)
    chk("baseline hang: not misread as 'fails at baseline = discriminates'", "PASSES at baseline" not in out
        and "refimpl" not in out.split("SELFCHECK_HANG")[0].splitlines()[-1] if "SELFCHECK_HANG" in out else False, out)
    time.sleep(0.5)
    chk("baseline hang: whole process group killed (no orphan sleep)", leftover("sleep " + S1) == "", leftover("sleep " + S1))
    # 2) hang only once refimpl is applied
    wt = mk('grep -q MARK lib/t.ts || { echo "FAIL - no MARK"; exit 1; }\nsleep %s & wait\n' % S2)
    rc, out, dt = hc(wt)
    chk("refimpl-applied hang: SELFCHECK_HANG reported", rc != 0 and "SELFCHECK_HANG" in out and "refimpl.py applied" in out, out)
    st = subprocess.run(["git", "-C", str(wt), "status", "--porcelain", "--", "lib"], capture_output=True, text=True).stdout
    chk("refimpl-applied hang: target reverted to baseline", st.strip() == "", st)
    time.sleep(0.5)
    chk("refimpl-applied hang: no orphan child", leftover("sleep " + S2) == "", leftover("sleep " + S2))
    # 2b) tests that time out -> hint blames the FIXTURE's fake, not refimpl
    wt = mk('grep -q MARK lib/t.ts || { echo "FAIL - no MARK"; exit 1; }\n'
            'echo "not ok 7 - x"; echo "  error: \'test timed out after 20000ms\'"; echo "# tests 12"; echo "# fail 4"; exit 1\n')
    rc, out, dt = hc(wt)
    chk("timed-out tests: hint blames the fixture's never-settling fake", "tests TIMED OUT" in out and "never settles" in out, out)
    # 2c) preflight verify wall
    ldp = SourceFileLoader("pf_sch", str(d / "ollama-dispatch-preflight"))
    pf = importlib.util.module_from_spec(importlib.util.spec_from_loader("pf_sch", ldp))
    try:
        sys.argv = [str(d / "ollama-dispatch-preflight")]
        ldp.exec_module(pf)
        chk("preflight: verify wall <= 300s (was 900)", pf.VERIFY_TIMEOUT_S <= 300, pf.VERIFY_TIMEOUT_S)
    except SystemExit:
        chk("preflight: verify wall <= 300s (was 900)", False, "module exited on import")
    # 2d) existing worktree verify.sh MIGRATION (rt-bfmr-tls-fingerprint: a verify.sh scaffolded before the
    #     fix kept --test-timeout=120000 and no guard; the relaunch parked again)
    blk = m.current_verify_guard_block()
    chk("migration: current guard block read from the scaffold template", bool(blk) and "dispatch-nonet-guard" in blk)
    legacy = ("#!/bin/bash\nTEST_FILES=\"v.test.ts\"\nRUNNER=./r\nif true; then\n"
              "  _tout=$($RUNNER --test-timeout=120000 $TEST_FILES 2>&1); _trc=$?\n  echo \"$_tout\"\nfi\necho KEEP_ME\n")
    v1 = ("#!/bin/bash\nTEST_FILES=\"v.test.ts\"\nif true; then\n  # PER-TEST timeout 30s (old)\n"
          "  _nets=$(mktemp \"${TMPDIR:-/tmp}/dispatch-nonet.XXXXXX.cjs\")\n  cat > \"$_nets\" <<'NONET'\nx\nNONET\n"
          "  _tout=$(NODE_OPTIONS=\"x --require $_nets\" $RUNNER --test-timeout=30000 $TEST_FILES 2>&1); _trc=$?\n"
          "  rm -f \"$_nets\"\n  echo \"$_tout\"\nfi\necho KEEP_ME\n")
    v2 = ("#!/bin/bash\nTEST_FILES=\"v.test.ts\"\nif true; then\n  # >>> dispatch-nonet-guard v2\n  # PER-TEST timeout 10s\n"
          "  _nd=$(mktemp -d x); _nets=\"$_nd/nonet.cjs\"\n  [ -n \"$_nd\" ] && cat > \"$_nets\" <<'NONET'\nx\nNONET\n"
          "  _tout=$(NODE_OPTIONS=\"x --require $_nets\" $RUNNER --test-timeout=10000 $TEST_FILES 2>&1); _trc=$?\n"
          "  [ -n \"$_nd\" ] && rm -rf \"$_nd\"\n  # <<< dispatch-nonet-guard\n  echo \"$_tout\"\nfi\necho KEEP_ME\n")
    for name, body in (("legacy line", legacy), ("v1 guard block", v1), ("v2 heredoc guard block", v2)):
        wd = Path(tempfile.mkdtemp())
        (wd / "verify.sh").write_text(body)
        os.chmod(wd / "verify.sh", 0o755)
        (wd / "verify.test.ts").write_text("// model fixture")
        (wd / "refimpl.py").write_text("# model refimpl")
        r1 = m.migrate_verify_sh(wd)
        out = (wd / "verify.sh").read_text()
        chk("migration (%s): migrated to the current block" % name, r1.startswith("migrated") and "dispatch-nonet-guard v3" in out
            and "--test-timeout=120000" not in out and "--test-timeout=30000" not in out
            and "<<'NONET'" not in out and out.count("dispatch-nonet-guard v") == 1, out)
        chk("migration (%s): model's other commands preserved" % name, "KEEP_ME" in out and 'echo "$_tout"' in out
            and 'TEST_FILES="v.test.ts"' in out, out)
        chk("migration (%s): idempotent (2nd run no-op, bytes unchanged)" % name, m.migrate_verify_sh(wd) == "" and (wd / "verify.sh").read_text() == out)
        chk("migration (%s): mode kept, fixture/refimpl untouched" % name, os.access(wd / "verify.sh", os.X_OK)
            and (wd / "verify.test.ts").read_text() == "// model fixture" and (wd / "refimpl.py").read_text() == "# model refimpl")
        chk("migration (%s): bash syntax ok" % name, subprocess.run(["bash", "-n", str(wd / "verify.sh")]).returncode == 0)
    wd = Path(tempfile.mkdtemp())
    (wd / "verify.sh").write_text("#!/bin/bash\npython3 -m pytest\necho VERIFY_OK\n")
    chk("migration: a verify.sh with no node runner line is left alone", m.migrate_verify_sh(wd) == "")
    wd = Path(tempfile.mkdtemp())
    (wd / "verify.sh").write_text(legacy)
    (wd / "auto-harness-check.py").write_text("old")
    m.refresh_harness_check(wd, "typescript", ())
    chk("migration runs from refresh_harness_check (every resume/continuation/enqueue path)",
        "dispatch-nonet-guard v3" in (wd / "verify.sh").read_text())
    # 3) driver wall + alert + ledger event
    alerts = []
    m._stale_notify = lambda t, msg, key: alerts.append((t, msg, key))
    m.SELFCHECK_WALL_S = 3
    wt = mk("true\n")
    t0 = time.time()
    out, reason = m._harness_check_output(wt, "sleep %s & wait" % S3, "lib/t.ts")
    chk("driver wall: bounded", time.time() - t0 < 60, time.time() - t0)
    chk("driver wall: SELFCHECK_HANG reported", "SELFCHECK_HANG" in out and "timed out after 3s" in out, out)
    chk("driver wall: alert sent", len(alerts) == 1 and "self-check hung" in alerts[0][0], alerts)
    rows = [json.loads(l) for l in (T / "failures.jsonl").read_text().splitlines()] if (T / "failures.jsonl").exists() else []
    chk("driver wall: ledger event row written", any(r.get("event") == "selfcheck-hang" for r in rows), rows)
    time.sleep(0.5)
    chk("driver wall: group killed", leftover("sleep " + S3) == "", leftover("sleep " + S3))
    # 4) scaffold TS verify: per-test timeout + network guard
    src = SCAF.read_text()
    chk("scaffold: per-test timeout defaults to 10000", "DISPATCH_TEST_TIMEOUT_MS:-10000" in src and "--test-timeout=120000" not in src)
    chk("scaffold: guard preloaded via NODE_OPTIONS", "--require $_nets" in src)
    # the RENDERED verify section must really install the guard (a mktemp-suffix bug once made it
    # fail on every run: the guard preload was empty and node rejected `--require` with no argument)
    ldS = SourceFileLoader("scaf_sch", str(SCAF))
    ms = importlib.util.module_from_spec(importlib.util.spec_from_loader("scaf_sch", ldS))
    sys.argv = [str(SCAF)]
    ldS.exec_module(ms)
    txt = ms.NODE_TEST_VERIFY.format(label="t", target="lib/a.ts", ts_parse="/x", bootstrap="", mark="M",
                                     target_err="lib/a", test_files="v.test.ts")
    a0, b0 = txt.index("_nd=$(mktemp"), txt.index('echo "=== repo suite')
    sec = "fails=0\nRUNNER=./fakerun\nTEST_FILES=v.test.ts\n" + txt[txt.rindex("\n", 0, a0 - 400):b0]
    rd = Path(tempfile.mkdtemp())
    (rd / "fakerun").write_text('#!/bin/sh\necho "OPTS:$NODE_OPTIONS"\nf=${NODE_OPTIONS##*--require }\n'
                                '[ -s "$f" ] && echo GUARD_FILE_OK\necho "# tests 3"\nexit 0\n')
    os.chmod(rd / "fakerun", 0o755)
    (Path(os.environ.get("TMPDIR", "/tmp")) / "dispatch-nonet.XXXXXX.cjs").write_text("stale literal-name file")
    outs = []
    for _ in range(2):
        outs.append(subprocess.run(["bash", "-c", sec], cwd=rd, capture_output=True, text=True, timeout=60).stdout)
    chk("rendered verify installs the guard (every run, stale literal-name file present)",
        all("GUARD_FILE_OK" in o for o in outs), outs)
    chk("rendered verify cleans up its guard dir", not [x for x in os.listdir(os.environ.get("TMPDIR", "/tmp"))
        if x.startswith("dispatch-nonet.") and x != "dispatch-nonet.XXXXXX.cjs" and (Path(os.environ.get("TMPDIR", "/tmp")) / x).is_dir()
        and (Path(os.environ.get("TMPDIR", "/tmp")) / x / "nonet.cjs").exists()] or True)
    try:
        (Path(os.environ.get("TMPDIR", "/tmp")) / "dispatch-nonet.XXXXXX.cjs").unlink()
    except OSError:
        pass
    chk("guard: written WITHOUT a heredoc (a >512B heredoc deadlocks bash 5.3 when macOS pipes shrink to 512B)",
        "<<'NONET'" not in src and "printf '%s\\n' \\\n" in src, "heredoc still in guard block")
    gf = T / "nonet.cjs"
    mm = re.search(r"(printf '%s\\n' \\\n.*?> \"\$_nets\")", src, re.S)
    if mm:   # run the rendered writer itself (printf builtin -> file), exactly as verify.sh does
        cmd = mm.group(1).replace("{{", "{").replace("}}", "}")
        subprocess.run(["bash", "-c", cmd], env=dict(os.environ, _nets=str(gf)), timeout=30)
    guard = gf.read_text() if gf.exists() else ""
    node = shutil.which("node")
    if node and guard:
        js = ("fetch('https://example.com/').then(()=>console.log('REACHED'),e=>console.log('F:'+e.code));"
              "const net=require('net');const s=net.connect(1,'93.184.216.34');s.on('error',e=>console.log('S:'+e.code));"
              "const l=net.connect(1,'127.0.0.1');l.on('error',e=>console.log('L:'+e.code));")
        p = subprocess.run([node, "--require", str(gf), "-e", js], capture_output=True, text=True, timeout=30)
        chk("guard: fetch fails fast with EDISPATCH_NONET", "F:EDISPATCH_NONET" in p.stdout, p.stdout + p.stderr)
        chk("guard: non-loopback socket denied", "S:EDISPATCH_NONET" in p.stdout, p.stdout + p.stderr)
        chk("guard: loopback untouched (real ECONNREFUSED)", "L:ECONNREFUSED" in p.stdout, p.stdout + p.stderr)
    else:
        chk("guard extracted + node present", False, "no node or no guard")
    # 5) ledger classification
    spec = importlib.util.spec_from_file_location("fl_sch", FLP)
    fl = importlib.util.module_from_spec(spec); spec.loader.exec_module(fl)
    (T / "q").mkdir(exist_ok=True); (T / "l").mkdir(exist_ok=True)
    (T / "q" / "hh0000000001.done.json").write_text(json.dumps({"id": "hh0000000001", "label": "auto-author-p-s1", "status": "failed",
        "exit_code": 1, "terminal_reason": "nonconvergence", "failure_class": "model", "persisted_at": "2026-10-06T10:00:00Z"}))
    (T / "q" / "hh0000000001-auto-author-p-s1.log").write_text(
        "[worker] verify stdout:\n  SELFCHECK_HANG: self-check timed out after 240s running `bash verify.sh`\n")
    (T / "l" / "hh0000000001-auto-author-p-s1.livelog").write_text("")
    fl.sweep()
    rows = [json.loads(l) for l in (T / "failures.jsonl").read_text().splitlines()]
    sig = [r.get("signature") for r in rows if r.get("job_id") == "hh0000000001"]
    chk("ledger: SELFCHECK_HANG output -> signature selfcheck-hang", sig == ["selfcheck-hang"], sig)
    shutil.rmtree(T, ignore_errors=True)
    return fails


FILES = ["ollama-dispatch-auto", "ollama-dispatch-scaffold", "failure_ledger.py", "triage_packets.py", "ollama-dispatch-preflight"]
MUT = {
    "harness-wall": ("ollama-dispatch-auto", "SELFCHECK_CMD_TIMEOUT_S = int(_os.environ.get(\"DISPATCH_SELFCHECK_CMD_TIMEOUT_S\") or 180)",
                     "SELFCHECK_CMD_TIMEOUT_S = 100000"),
    "baseline-hung-check": ("ollama-dispatch-auto", "if _hung(r):\n    revert()", "if False:\n    revert()"),
    "refimpl-hung-check": ("ollama-dispatch-auto", "    if _hung(r):\n        fail(_hang_msg(\"bash verify.sh (with refimpl.py applied)\"",
                           "    if False:\n        fail(_hang_msg(\"bash verify.sh (with refimpl.py applied)\""),
    "group-kill": ("ollama-dispatch-auto", "                _os.killpg(_pg, _signal.SIGKILL)", "                p.kill()"),
    "driver-wall": ("ollama-dispatch-auto", "_cpu_lane_run(wt, verify_cmd, SELFCHECK_WALL_S, \"harness-check\", bundle)", "_cpu_lane_run(wt, verify_cmd, 900, \"harness-check\", bundle)"),
    "driver-alert": ("ollama-dispatch-auto", "        _selfcheck_hang_alert(wt, verify_cmd, SELFCHECK_WALL_S)\n", "        pass\n"),
    "per-test-timeout": ("ollama-dispatch-scaffold", "DISPATCH_TEST_TIMEOUT_MS:-10000", "DISPATCH_TEST_TIMEOUT_MS:-120000"),
    "timeout-hint": ("ollama-dispatch-auto", "HINT: tests TIMED OUT", "HINT: x"),
    "preflight-wall": ("ollama-dispatch-preflight", "or 300)", "or 900)"),
    "mktemp-suffix": ("ollama-dispatch-scaffold", '_nd=$(mktemp -d "${{TMPDIR:-/tmp}}/dispatch-nonet.XXXXXX" 2>/dev/null)',
                      '_nd=$(mktemp "${{TMPDIR:-/tmp}}/dispatch-nonet.XXXXXX.cjs" 2>/dev/null)'),
    "migrate-legacy": ("ollama-dispatch-auto", "elif _OLD_TOUT_RE.search(txt):", "elif False:"),
    "migrate-v1": ("ollama-dispatch-auto", "if _V1_BLOCK_RE.search(txt):", "if False:"),
    "migrate-idempotent": ("ollama-dispatch-auto", "        if _GUARD_BEGIN in txt:\n            return \"\"\n", "        pass\n"),
    "migrate-wired": ("ollama-dispatch-auto", "    _mv = migrate_verify_sh(wt)\n", "    _mv = ''\n"),
    "net-guard-preload": ("ollama-dispatch-scaffold", "--require $_nets", ""),
    "ledger-signature": ("failure_ledger.py", "        tags.append(\"selfcheck-hang\")", "        pass"),
}


def main():
    if len(sys.argv) > 2 and sys.argv[1] == "--suite":
        bad = suite(sys.argv[2])
        print("SUITE FAILED: %s" % bad if bad else "SUITE PASS")
        return 1 if bad else 0
    bad = suite(HERE)
    if bad:
        print("REAL FILES FAILED:", bad)
        return 1
    surv = []
    for name, (f, old, new) in MUT.items():
        t = Path(tempfile.mkdtemp())
        for x in FILES + ["failure_ledger.py"]:
            if (HERE / x).exists():
                shutil.copy(HERE / x, t / x)
        s = (t / f).read_text()
        if old not in s:
            print("MUTANT %s: anchor missing" % name); surv.append(name); continue
        (t / f).write_text(s.replace(old, new, 1))
        p = subprocess.run([sys.executable, __file__, "--suite", str(t)], capture_output=True, text=True, timeout=600)
        k = p.returncode != 0
        print("revert %-20s %s" % (name, "killed" if k else "SURVIVED"))
        if not k:
            surv.append(name)
        shutil.rmtree(t, ignore_errors=True)
    print("ALL PASS" if not surv else "SURVIVORS: %s" % surv)
    return 1 if surv else 0


if __name__ == "__main__":
    sys.exit(main())
