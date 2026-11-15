#!/usr/bin/env python3
"""Scan files (or a git diff range) for private info. Exit 1 on any hit.

Generic patterns are built in: RFC1918 and Tailscale (100.64/10) IPs, MAC
addresses, /Users/<name> and /home/<name> paths, emails other than noreply,
tokens (sk-, ghp_, Bearer, long hex/base64). Operator-specific patterns (names,
handles, domains, hostnames) are read from a file outside git:
$QUEUE_PRIVATE_PATTERNS or ~/.config/ollama-queue/private-patterns.txt
(one regex per line, '#' comments, matched case-insensitively).

Allowlist: queue-image/scan-allow.txt (override with --allow). One entry per line:
  path-glob : regex : reason        (regex matched against the offending line)
  path-glob : sha1:<12 hex of the stripped line> : reason
The reason is required.

Usage: scan-private-info.py [--allow FILE] [--patterns FILE] [--list MANIFEST]
                            [--diff RANGE] [FILE ...]
"""
import fnmatch
import hashlib
import ipaddress
import os
import re
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
IPV4 = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d{1,3}){3})(?![\d])")
MAC = re.compile(r"(?<![0-9A-Fa-f:-])(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}(?![0-9A-Fa-f:-])")
HOME_PATH = re.compile(r"(?:/Users|/home)/([A-Za-z][A-Za-z0-9_.-]*)")
EMAIL = re.compile(r"(?<![\\A-Za-z0-9._%+-])[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}(?![A-Za-z0-9-])")
TOKENS = [
    ("sk- token", re.compile(r"\bsk-[A-Za-z0-9_-]{16,}")),
    ("ghp_ token", re.compile(r"\bghp_[A-Za-z0-9]{16,}")),
    ("github token", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("Bearer token", re.compile(r"Bearer\s+[A-Za-z0-9._~+/=-]{16,}")),
    ("long hex", re.compile(r"(?<![0-9A-Fa-f])[0-9a-f]{40,}(?![0-9A-Fa-f])")),
    ("long base64", re.compile(r"(?<![A-Za-z0-9+/])(?=[A-Za-z0-9+/]*[A-Z])(?=[A-Za-z0-9+/]*[a-z])(?=[A-Za-z0-9+/]*\d)[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])")),
]
# Home-dir names that are placeholders, not people.
OK_USERS = {"user", "username", "you", "me", "name", "example", "x", "<user>", "claude", "runner", "app", "home", "nobody", "root", "Shared", "$USER", "USER"}
OK_EMAIL_DOMAINS = {"users.noreply.github.com", "example.com", "example.org", "noreply.github.com", "anthropic.com"}
OK_EMAIL_LOCAL = {"noreply"}
# RFC 2606 / RFC 6761 reserved names: they can never be anyone's real address (test fixtures use them)
OK_EMAIL_TLDS = {"invalid", "test", "example", "localhost"}


def ip_hit(m):
    try:
        ip = ipaddress.IPv4Address(m)
    except ValueError:
        return None
    if ip in ipaddress.IPv4Network("100.64.0.0/10"):
        return "tailscale/CGNAT ip"
    if ip.is_private and not ip.is_loopback and not ip.is_link_local and not ip.is_unspecified:
        if ip in ipaddress.IPv4Network("192.0.2.0/24") or ip in ipaddress.IPv4Network("198.51.100.0/24") or ip in ipaddress.IPv4Network("203.0.113.0/24"):
            return None
        return "private ip"
    return None


def load_patterns(path):
    out = []
    if path and Path(path).is_file():
        for ln in Path(path).read_text().splitlines():
            ln = ln.strip()
            if ln and not ln.startswith("#"):
                out.append(re.compile(ln, re.I))
    return out


def load_allow(path):
    rules = []
    if path and Path(path).is_file():
        for n, ln in enumerate(Path(path).read_text().splitlines(), 1):
            if not ln.strip() or ln.lstrip().startswith("#"):
                continue
            parts = [p.strip() for p in ln.split(" : ", 2)]
            if len(parts) != 3 or not parts[2]:
                print(f"{path}:{n}: allow entry needs 'glob : regex-or-sha1:... : reason'", file=sys.stderr)
                raise SystemExit(2)
            rules.append(tuple(parts))
    return rules


def line_hash(line):
    return hashlib.sha1(line.strip().encode()).hexdigest()[:12]


def allowed(rules, rel, line):
    for glob, what, _ in rules:
        if not fnmatch.fnmatch(rel, glob):
            continue
        if what.startswith("sha1:"):
            if what[5:] == line_hash(line):
                return True
        elif re.search(what, line):
            return True
    return False


def scan_line(line, private):
    hits = []
    for m in IPV4.finditer(line):
        why = ip_hit(m.group(1))
        if why:
            hits.append((why, m.group(1)))
    for m in MAC.finditer(line):
        hits.append(("mac address", m.group(0)))
    for m in HOME_PATH.finditer(line):
        if m.group(1) not in OK_USERS:
            hits.append(("home path", m.group(0)))
    for m in EMAIL.finditer(line):
        loc, _, dom = m.group(0).partition("@")
        if (dom.lower() in OK_EMAIL_DOMAINS or dom.lower().rsplit(".", 1)[-1] in OK_EMAIL_TLDS
                or loc in OK_EMAIL_LOCAL):
            continue
        hits.append(("email", m.group(0)))
    for name, rx in TOKENS:
        for m in rx.finditer(line):
            hits.append((name, m.group(0)[:12] + "..."))
    for rx in private:
        for m in rx.finditer(line):
            hits.append(("private pattern", m.group(0)))
    return hits


def scan_text(rel, text, private, rules):
    out = []
    for n, line in enumerate(text.splitlines(), 1):
        for why, what in scan_line(line, private):
            if not allowed(rules, rel, line):
                out.append((rel, n, why, what, line_hash(line)))
    return out


def diff_added(rng):
    """Yield (path, lineno, text) for lines added in a git diff range."""
    p = subprocess.run(["git", "diff", "-U0", rng], capture_output=True, text=True, check=True)
    path, ln = None, 0
    for row in p.stdout.splitlines():
        if row.startswith("+++ b/"):
            path = row[6:]
        elif row.startswith("@@"):
            m = re.search(r"\+(\d+)", row)
            ln = int(m.group(1)) if m else 0
        elif row.startswith("+") and not row.startswith("+++"):
            yield path, ln, row[1:]
            ln += 1


def main(argv):
    allow = HERE / "scan-allow.txt"
    pat = os.environ.get("QUEUE_PRIVATE_PATTERNS") or str(Path.home() / ".config/ollama-queue/private-patterns.txt")
    files, base = [], Path.cwd()
    diff = None
    it = iter(argv)
    for a in it:
        if a == "--allow":
            allow = Path(next(it))
        elif a == "--patterns":
            pat = next(it)
        elif a == "--diff":
            diff = next(it)
        elif a == "--base":
            base = Path(next(it))
        elif a == "--list":
            lst = Path(next(it))
            files += [l.split("#")[0].strip() for l in lst.read_text().splitlines() if l.split("#")[0].strip()]
        elif a in ("-h", "--help"):
            print(__doc__)
            return 0
        else:
            files.append(a)
    private = load_patterns(pat)
    if not private:
        print(f"note: no private patterns at {pat}; generic patterns only", file=sys.stderr)
    rules = load_allow(allow)
    hits = []
    if diff:
        by = {}
        for path, ln, text in diff_added(diff):
            for why, what in scan_line(text, private):
                if not allowed(rules, path, text):
                    hits.append((path, ln, why, what, line_hash(text)))
    for f in files:
        p = Path(f)
        if not p.is_absolute():
            p = base / f
        rel = str(f)
        try:
            text = p.read_text(errors="replace")
        except OSError as e:
            print(f"cannot read {f}: {e}", file=sys.stderr)
            hits.append((rel, 0, "unreadable", str(e), ""))
            continue
        hits += scan_text(rel, text, private, rules)
    for rel, n, why, what, h in hits:
        print(f"{rel}:{n}: {why}: {what}  [sha1:{h}]")
    print(f"{len(hits)} hit(s)", file=sys.stderr)
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
