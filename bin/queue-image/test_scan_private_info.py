#!/usr/bin/env python3
"""Tests for scan-private-info.py. Fake secrets are built at run time so this file itself scans clean."""
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("scan", HERE / "scan-private-info.py")
scan = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scan)

fails = 0


def check(name, got, want):
    global fails
    if got != want:
        fails += 1
        print(f"FAIL {name}: got {got!r} want {want!r}")
    else:
        print(f"PASS {name}")


def kinds(line, private=()):
    return sorted({k for k, _ in scan.scan_line(line, list(private))})


J = ".".join
check("rfc1918 10/8", kinds("host " + J(["10", "1", "2", "3"])), ["private ip"])
check("rfc1918 192.168", kinds("x " + J(["192", "168", "0", "9"])), ["private ip"])
check("rfc1918 172.16", kinds("x " + J(["172", "20", "0", "9"])), ["private ip"])
check("tailscale 100.64/10", kinds("x " + J(["100", "100", "1", "1"])), ["tailscale/CGNAT ip"])
check("100.63 is public", kinds("x " + J(["100", "63", "1", "1"])), [])
check("loopback ok", kinds("http://127.0.0.1:8000"), [])
check("0.0.0.0 ok", kinds("bind " + J(["0", "0", "0", "0"])), [])
check("rfc5737 ok", kinds("x " + J(["192", "0", "2", "10"])), [])
check("public ip ok", kinds("x 8.8.8.8"), [])
check("mac colon", kinds("mac " + ":".join(["aa", "bb", "cc", "dd", "ee", "01"])), ["mac address"])
check("mac dash", kinds("mac " + "-".join(["AA", "BB", "CC", "DD", "EE", "01"])), ["mac address"])
check("home path", kinds("/" + "Users/" + "alice/bin"), ["home path"])
check("linux home path", kinds("/" + "home/" + "alice/bin"), ["home path"])
check("placeholder home ok", kinds("/Users/example/bin /Users/.... /home/user/x"), [])
check("email", kinds("mail bob" + "@" + "mailhost.net"), ["email"])
check("noreply email ok", kinds("1+x" + "@" + "users.noreply.github.com"), [])
check("reserved-TLD email ok (RFC 2606)", kinds("t" + "@" + "example.invalid t" + "@" + "a.test"), [])
check("look-alike TLD still a hit", kinds("bob" + "@" + "invalid.net"), ["email"])
check("decorator not email", kinds("@pytest.mark.skip and \\n" + "@m.patch"), [])
check("sk token", kinds("key=" + "sk-" + "a1b2c3d4e5f6g7h8i9j0"), ["sk- token"])
check("ghp token", kinds("t " + "ghp_" + "A" * 20), ["ghp_ token"])
check("bearer token", kinds("Authorization: " + "Bearer " + "abcdef0123456789abcdef"), ["Bearer token"])
check("bearer word ok", kinds('startswith("Bearer ")'), [])
check("long hex", kinds("h " + "ab12" * 10), ["long hex"])
check("short hex ok", kinds("commit 0123abc"), [])
check("private pattern", kinds("hello Zorp", [scan.re.compile("zorp", scan.re.I)]), ["private pattern"])

# allowlist
rules = [("a.py", "FAKE", "test")]
text = "x " + J(["10", "0", "0", "1"]) + " FAKE\ny " + J(["10", "0", "0", "2"]) + "\n"
hits = scan.scan_text("a.py", text, [], rules)
check("allow by regex covers only matching line", [h[1] for h in hits], [2])
check("allow glob must match path", len(scan.scan_text("b.py", text, [], rules)), 2)
h = scan.line_hash("y " + J(["10", "0", "0", "2"]))
check("allow by hash", scan.scan_text("a.py", text, [], [("*.py", "sha1:" + h, "r")]) != [], True)
check("allow by hash covers that line", [x[1] for x in scan.scan_text("a.py", text, [], [("*.py", "sha1:" + h, "r")])], [1])

with tempfile.TemporaryDirectory() as d:
    d = Path(d)
    (d / "allow.txt").write_text("a.txt : FAKE : why\n")
    (d / "bad.txt").write_text("nope\n")
    (d / "pat.txt").write_text("# c\nzorp\n")
    (d / "a.txt").write_text("ip " + J(["10", "0", "0", "1"]) + " FAKE\n")
    (d / "clean.txt").write_text("nothing here\n")
    (d / "priv.txt").write_text("Zorp lives here\n")
    (d / "noreason.txt").write_text("a.txt : FAKE\n")
    run = lambda *a: subprocess.run([sys.executable, str(HERE / "scan-private-info.py"), "--base", str(d), *a],
                                    capture_output=True, text=True)
    check("exit 0 when clean", run("--allow", str(d / "allow.txt"), "--patterns", str(d / "pat.txt"), "clean.txt", "a.txt").returncode, 0)
    r = run("--allow", str(d / "allow.txt"), "--patterns", str(d / "pat.txt"), "priv.txt")
    check("exit 1 on private pattern", r.returncode, 1)
    check("hit names file:line", r.stdout.startswith("priv.txt:1:"), True)
    check("exit 1 on unreadable file", run("--allow", str(d / "allow.txt"), "missing.txt").returncode, 1)
    check("allow entry without reason is refused", run("--allow", str(d / "noreason.txt"), "a.txt").returncode not in (0, 1), True)
    (d / "list").write_text("clean.txt  # why\n\npriv.txt\n")
    check("--list reads a manifest", run("--allow", str(d / "allow.txt"), "--patterns", str(d / "pat.txt"), "--list", str(d / "list")).returncode, 1)
    g = lambda *a: subprocess.run(["git", "-c", "core.hooksPath=/dev/null", "-C", str(d), *a], capture_output=True, text=True, check=True)
    g("init", "-q")
    (d / "f.txt").write_text("one\n")
    g("add", "f.txt")
    g("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qm", "a")
    (d / "f.txt").write_text("one\ntwo " + J(["10", "9", "9", "9"]) + "\n")
    g("-c", "user.name=t", "-c", "user.email=t@example.com", "commit", "-qam", "b")
    r = subprocess.run([sys.executable, str(HERE / "scan-private-info.py"), "--allow", "/dev/null", "--patterns", "/dev/null", "--diff", "HEAD~1..HEAD"],
                       capture_output=True, text=True, cwd=d)
    check("--diff flags only added lines", (r.returncode, r.stdout.startswith("f.txt:2:")), (1, True))

print("FAILED" if fails else "ALL PASS")
sys.exit(1 if fails else 0)
