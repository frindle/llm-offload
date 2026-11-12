#!/usr/bin/env python3
"""test-dispatch-diagnostics.py -- both-ways checks for the diagnostics executor.

Every refusal is asserted with a request that WOULD have worked if the guard
were absent (the file exists, the git option is valid, the container is real),
so a check cannot pass on an executor that simply errors on everything; and
every acceptance is asserted on content the request could only have produced
by actually running (a line the fixture wrote, a commit subject).
"""
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("dd", HERE / "dispatch-diagnostics.py")
dd = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dd)

EXPECTED_CHECKS = 50
_n = 0
_fail = []


def check(cond, label):
    global _n
    _n += 1
    print(("ok   " if cond else "FAIL ") + label)
    if not cond:
        _fail.append(label)


def git(cwd, *a):
    return subprocess.run(["git", "-C", str(cwd), *a], capture_output=True, text=True, check=True).stdout


def build(root: Path):
    root.mkdir(parents=True)
    git(root, "init", "-q", "-b", "main")
    git(root, "config", "user.email", "t@t")
    git(root, "config", "user.name", "t")
    (root / "src").mkdir()
    (root / "src" / "app.py").write_text("\n".join(f"line{i} = {i}" for i in range(1, 501)) + "\nNEEDLE_TRACKED = 1\n")
    (root / ".env").write_text("API_KEY=supersecretvalue123\n")
    (root / "config.json").write_text('{"token": "abcdef123456", "name": "ok"}\n')
    (root / "verify.sh").write_text("echo VERIFY_RAN; exit 3\n")
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "fixture: initial commit SUBJECT_ONE")
    (root / "src" / "untracked.py").write_text("NEEDLE_UNTRACKED = 2\n")
    outside = root.parent / "outside.txt"
    outside.write_text("OUTSIDE_SECRET\n")
    os.symlink(outside, root / "link_out.txt")
    return root


def run(reqs, cwd, **ctx):
    return dd.run_requests(reqs, cwd, **ctx)


def one(req, cwd, **ctx):
    return run([req], cwd, **ctx)["results"][0]


with tempfile.TemporaryDirectory() as td:
    repo = build(Path(td) / "repo")

    # ---- git: allow reads, refuse writes/escapes --------------------------
    r = one({"kind": "git", "args": ["log", "--oneline", "-n", "5"]}, repo)
    check(r["ok"] and "SUBJECT_ONE" in r["output"], "git log returns the fixture commit subject")
    r = one({"kind": "git", "args": ["status", "--short"]}, repo)
    check(r["ok"] and "untracked.py" in r["output"], "git status sees the untracked file")
    r = one({"kind": "git", "args": ["commit", "-am", "x"]}, repo)
    check(not r["ok"] and "not an allowlisted" in r["refused"], "git commit refused (not a read subcommand)")
    r = one({"kind": "git", "args": ["checkout", "--", "src/app.py"]}, repo)
    check(not r["ok"], "git checkout refused")
    r = one({"kind": "git", "args": ["log", "--output=/tmp/x", "-n", "1"]}, repo)
    check(not r["ok"] and "--output=" in r["refused"], "git log --output= refused (writes a file)")
    r = one({"kind": "git", "args": ["diff", "--no-index", "src/app.py", "src/app.py"]}, repo)
    check(not r["ok"], "git diff --no-index refused (reads outside the tree)")
    r = one({"kind": "git", "args": ["log", "-c", "core.pager=touch pwned", "-n", "1"]}, repo)
    check(not r["ok"] and "-c" in r["refused"], "git -c refused (config injection)")
    r = one({"kind": "git", "args": ["show", "HEAD:../outside.txt"]}, repo)
    check(not r["ok"], "git show with .. refused")
    r = one({"kind": "git", "args": ["stash", "push"]}, repo)
    check(not r["ok"] and "stash list" in r["refused"], "git stash push refused, only list/show")
    r = one({"kind": "git", "args": ["stash", "list"]}, repo)
    check(r["ok"], "git stash list allowed")
    r = one({"kind": "git", "args": ["log", "--all", "--oneline", "-n", "1"]}, repo)
    check(r["ok"] and "SUBJECT_ONE" in r["output"], "git log --all allowed (plain read)")
    check(sorted(git(repo, "status", "--short").strip().splitlines()) == ["?? link_out.txt", "?? src/untracked.py"],
          "tree untouched after the refused write attempts (no commit, no checkout, no stash)")

    # ---- file: windows, caps, escapes, secrets ----------------------------
    r = one({"kind": "file", "path": "src/app.py", "start": 10, "end": 12}, repo)
    check(r["ok"] and "   10: line10 = 10" in r["output"] and "line13" not in r["output"], "file window is exact")
    r = one({"kind": "file", "path": "src/app.py", "start": 1, "end": 5000}, repo)
    check(r["ok"] and "line400" in r["output"] and "line401" not in r["output"], f"file window capped at {dd.FILE_MAX_LINES} lines")
    r = one({"kind": "file", "path": "../outside.txt"}, repo)
    check(not r["ok"] and "escapes" in r["refused"], "file ../ refused")
    r = one({"kind": "file", "path": "/etc/hosts"}, repo)
    check(not r["ok"], "file absolute path refused")
    r = one({"kind": "file", "path": "link_out.txt"}, repo)
    check(not r["ok"] and "OUTSIDE_SECRET" not in r["output"], "file symlink pointing outside refused")
    r = one({"kind": "file", "path": ".env"}, repo)
    check(not r["ok"] and "secret" in r["refused"], ".env refused by name")
    r = one({"kind": "file", "path": ".git/config"}, repo)
    check(not r["ok"] and ".git" in r["refused"], ".git/ internals refused")
    r = one({"kind": "file", "path": "config.json"}, repo)
    check(r["ok"] and "abcdef123456" not in r["output"] and '"name": "ok"' in r["output"],
          "readable file has token value redacted, rest intact")

    # ---- ls -----------------------------------------------------------
    r = one({"kind": "ls", "path": ".", "depth": 2}, repo)
    check(r["ok"] and "src/app.py" in r["output"] and ".git" not in r["output"], "ls lists nested files, hides .git")
    r = one({"kind": "ls", "path": "../"}, repo)
    check(not r["ok"], "ls ../ refused")

    # ---- grep ---------------------------------------------------------
    r = one({"kind": "grep", "pattern": "NEEDLE_"}, repo)
    check(r["ok"] and "NEEDLE_TRACKED" in r["output"] and "NEEDLE_UNTRACKED" in r["output"], "grep finds tracked + untracked")
    r = one({"kind": "grep", "pattern": "NEEDLE_", "glob": "src/untracked.py"}, repo)
    check(r["ok"] and "NEEDLE_TRACKED" not in r["output"] and "NEEDLE_UNTRACKED" in r["output"], "grep glob narrows")
    r = one({"kind": "grep", "pattern": "line[0-9]+ = 1", "regex": True, "max_matches": 3}, repo)
    check(r["ok"] and "more matches not shown" in r["output"], "grep regex + max_matches cap")
    r = one({"kind": "grep", "pattern": "supersecret"}, repo)
    check(r["ok"] and "supersecretvalue123" not in r["output"] and "no matches" in r["output"],
          "grep does not surface secret-named file content (.env is tracked; git grep WOULD match it)")
    r = one({"kind": "grep", "pattern": "x", "glob": "../outside.txt"}, repo)
    check(not r["ok"], "grep glob ../ refused")

    # ---- verify ---------------------------------------------------------
    r = one({"kind": "verify"}, repo, verify="bash verify.sh")
    check(r["ok"] and "VERIFY_RAN" in r["output"] and "[exit 3]" in r["output"], "verify runs the job verify and reports its exit")
    r = one({"kind": "verify"}, repo)
    check(not r["ok"] and "no --verify" in r["refused"], "verify refused when the job has none")

    # ---- docker_logs (catalog-bound, transport stubbed) ------------------
    cat = Path(td) / "catalog.json"
    cat.write_text(json.dumps({"docker": {"reselling-app-1": {"host": "unraid-host", "transport": "unraid-graphql"}}}))
    seen = {}

    def fake_fetch(src, tail, since):
        seen.update(src=src, tail=tail, since=since)
        return [f"2026-09-03T00:00:{i:02d}Z {'ERROR boom' if i % 5 == 0 else 'info ok'} apikey=sk-abcdefghijklmnop" for i in range(60)]

    r = one({"kind": "docker_logs", "container": "reselling-app-1", "tail": 50, "since": "30m", "filter": "ERROR"},
            repo, catalog=str(cat), docker_fetch=fake_fetch)
    check(r["ok"] and seen["tail"] == 50 and seen["since"] is not None, "docker_logs passes tail/since to the transport")
    check(r["ok"] and r["output"].count("ERROR boom") == 12 and "info ok" not in r["output"], "docker_logs filter applied")
    check(r["ok"] and "sk-abcdefghijklmnop" not in r["output"], "docker_logs output redacted")
    r = one({"kind": "docker_logs", "container": "plex", "tail": 10}, repo, catalog=str(cat), docker_fetch=fake_fetch)
    check(not r["ok"] and "not in the diagnostics catalog" in r["refused"] and "reselling-app-1" in r["refused"],
          "uncatalogued container refused, known names listed")
    r = one({"kind": "docker_logs", "container": "reselling-app-1", "tail": 99999}, repo, catalog=str(cat), docker_fetch=fake_fetch)
    check(r["ok"] and seen["tail"] == dd.DOCKER_MAX_TAIL, f"docker_logs tail clamped to {dd.DOCKER_MAX_TAIL}")
    r = one({"kind": "docker_logs", "container": "reselling-app-1", "since": "30d"}, repo, catalog=str(cat), docker_fetch=fake_fetch)
    check(not r["ok"] and "cap" in r["refused"], "docker_logs since beyond the 7-day cap refused")
    r = one({"kind": "docker_logs", "container": "reselling-app-1", "since": "; rm -rf /"}, repo, catalog=str(cat), docker_fetch=fake_fetch)
    check(not r["ok"] and "duration" in r["refused"], "docker_logs since must be a duration")

    # ---- driver bounds --------------------------------------------------
    r = one({"kind": "shell", "command": "ls"}, repo)
    check(not r["ok"] and "unknown kind" in r["refused"], "unknown kind refused with the allowed list")
    res = run([{"kind": "git", "args": ["status"]}] * 9, repo)
    check(len(res["results"]) == dd.MAX_REQUESTS_PER_CALL and res["budget"]["requests_dropped"] == 4,
          "requests per call capped, drop count reported")
    b = dd.Budget(max_calls=2)
    run([], repo, budget=b); run([], repo, budget=b)
    res = run([{"kind": "git", "args": ["status"]}], repo, budget=b)
    check(res.get("refused") and "budget exhausted" in res["refused"] and res["results"] == [],
          "per-run call budget refuses the call after max_calls")
    big = repo / "big.txt"
    big.write_text("Z" * 50000 + "\n")
    r = one({"kind": "file", "path": "big.txt", "start": 1, "end": 1}, repo)
    check(r["ok"] and r["truncated"] and len(r["output"]) <= dd.PER_REQUEST_CHARS + 60 and "elided" in r["output"],
          "per-request output cap applied with an elision marker")
    res = run([{"kind": "file", "path": "big.txt"}] * 4, repo)
    check(sum(len(x["output"]) for x in res["results"]) <= dd.PER_CALL_CHARS + 240, "per-call output cap applied")

    # ---- CLI --------------------------------------------------------------
    req = Path(td) / "req.json"
    req.write_text(json.dumps([{"kind": "git", "args": ["log", "--oneline", "-n", "1"]}]))
    p = subprocess.run([sys.executable, str(HERE / "dispatch-diagnostics.py"), str(req), "--cwd", str(repo)],
                       capture_output=True, text=True)
    check(p.returncode == 0 and "SUBJECT_ONE" in p.stdout, "CLI runs the same executor")
    check(dd.TOOL_SPEC["function"]["name"] == "request_diagnostics" and "requests" in dd.TOOL_SPEC["function"]["parameters"]["required"],
          "TOOL_SPEC exposes request_diagnostics with a required `requests` list")

    # ---- redaction: secrets scrubbed, CODE left intact ---------------------
    # REGRESSION (bg-crypto s3, 2026-09-19): `token`/`secret`/`password` are
    # ordinary identifiers, so source that ASSIGNS one read as a secret
    # assignment and had its right-hand side scrubbed. A model asked whether
    # decrypt_field was already implemented and was shown
    # `token = ***"utf-8"))` -- the answer to its own question, deleted. It
    # burned two ~11min authoring runs looping on the corrupted evidence.
    fernet_src = (
        'def decrypt_field(token: str, key: bytes) -> str:\n'
        '    token = Fernet(key).decrypt(token.encode("utf-8"))\n'
        '    return token.decode("utf-8")\n'
    )
    red = dd.redact(fernet_src)
    # The kill assertion: revert _redact_assignment to the old
    # `m.group(1) + "***"` and this line is exactly what comes back.
    check('token = ***"utf-8"))' not in red,
          "redact() does not scrub a Fernet call assigned to a variable named `token`")
    check("Fernet(key).decrypt(" in red and 'token.decode("utf-8")' in red,
          "redact() leaves the decrypt_field body readable end to end")

    # The other half: real secrets must STILL be scrubbed. A relaxation that
    # quietly stops redacting is the dangerous failure here, so pin both the
    # bare and the quoted form, including a quoted value holding the very
    # punctuation the code-detection keys on.
    check(dd.redact("API_KEY=sk_live_abcdef123456") == "API_KEY=***",
          "redact() still scrubs a bare .env-style secret assignment")
    check(dd.redact('api_key: "abcdef123456"') == 'api_key: "***"',
          "redact() still scrubs a quoted secret value")
    check(dd.redact('password = "p@ss(word)"') == 'password = "***"',
          "redact() still scrubs a QUOTED value containing code punctuation")
    check(dd.redact("token=ghp_aaaaaaaaaaaaaaaaaaaaaa") == "token=***",
          "redact() still scrubs a bare github token assignment")
    # Bare-token sweep is independent of the assignment rule: an inline JWT in
    # a code expression is still caught even though the line is left unscrubbed.
    check("***" in dd.redact('h = {"Authorization": "Bearer eyJabcdefghijklmnopqrst.abcdefghijk.abcdefghijk"}'),
          "redact() still scrubs a bare JWT appearing inside code")

print(f"\n{_n} checks, {len(_fail)} failed")
if _n != EXPECTED_CHECKS:
    print(f"FAIL: expected {EXPECTED_CHECKS} checks, ran {_n}")
    sys.exit(2)
sys.exit(1 if _fail else 0)
