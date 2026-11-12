#!/usr/bin/env python3
"""Vault Claude/API-Docs/<Service>.md -> <repo>/<Service>/api.md, sanitized for the PUBLIC frindle/api-docs repo.
Redacts private patterns, prints a per-file redaction count (no values), then runs a GATE that exits 1 if
anything sensitive is left. Nothing is written to the repo if the gate fails.
usage: sanitize_apidocs.py <repo_dir> [--dry]
"""
import os, re, sys, json, urllib.request

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))
import vault_conf  # noqa: E402  (env VAULT_URL / VAULT_TOKEN_FILE -> ~/.config/vault/env -> defaults)

VAULT = vault_conf.vault_url() + "/vault/Claude/API-Docs/"
TOK = vault_conf.vault_token()
if not TOK:
    sys.exit("sanitize_apidocs: no vault token (VAULT_TOKEN_FILE / ~/.config/vault/env)")
H = {"Authorization": "Bearer " + TOK}

RULES = [  # (name, regex, replacement)
    ("lan-ip", re.compile(r"\b(?:10\.\d{1,3}|192\.168|172\.(?:1[6-9]|2\d|3[01]))\.\d{1,3}\.\d{1,3}\b"), "<lan-ip>"),
    ("mac", re.compile(r"\b[0-9A-Fa-f]{2}(?:[:-][0-9A-Fa-f]{2}){5}\b"), "<mac>"),
    ("vin", re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b(?<=\d)(?=.*)"), None),  # handled in code (needs a digit and a letter)
    ("uuid", re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"), "<uuid>"),
    ("personal-domain", re.compile(r"[\w.-]*user\.com", re.I), "<personal-domain>"),
    ("mdns-host", re.compile(r"\bhomeassistant\.local\b", re.I), "<ha-host>.local"),
    ("alias-email", re.compile(r"[\w.+-]+@[\w.-]*simplelogin\.com\b", re.I), "<alias-email>"),
    ("user-email", re.compile(r"[\w.+-]+@(?:pm\.me|proton\.me|protonmail\.com)\b", re.I), "<email>"),
    ("bearer", re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]{24,}"), "Bearer <token>"),
    ("long-secret", re.compile(r"(?<![A-Za-z0-9/_-])(?=[A-Za-z0-9_-]*\d)(?=[A-Za-z0-9_-]*[A-Za-z])[A-Za-z0-9_-]{40,}(?![A-Za-z0-9/_-])"), "<redacted>"),
    ("site-name", re.compile(r"Halton Place", re.I), "<site-name>"),
    ("energy-site-id", re.compile(r"\b\d{14,18}\b"), "<site-id>"),
    ("hw-serial", re.compile(r"\b(?:B7S|E4A)[A-Z0-9]{8,}\b"), "<serial>"),
    ("ipv4-cgnat-tailscale", re.compile(r"\b100\.(?:6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b"), "<lan-ip>"),
]
VIN_RE = re.compile(r"\b[A-HJ-NPR-Z0-9]{17}\b")
GATE_WORDS = re.compile(r"user|owner\.lastname|pm\.me|homeassistant\.local|/Users/user", re.I)


def sanitize(text):
    counts = {}
    for name, rx, rep in RULES:
        if rep is None:
            continue
        text, n = rx.subn(rep, text)
        if n:
            counts[name] = n

    def vin(m):
        s = m.group(0)
        return "<vin>" if re.search(r"\d", s) and re.search(r"[A-Z]", s) and not s.isdigit() else s
    text, n = VIN_RE.subn(vin, text)
    counts["vin"] = sum(1 for m in VIN_RE.finditer(text) if False) or counts.get("vin", 0)
    return text, counts


def gate(text):
    bad = []
    for name, rx, rep in RULES:
        if rep is None:
            continue
        m = rx.search(text)
        if m and not (rep and m.group(0) == rep):
            bad.append(name)
    if GATE_WORDS.search(text):
        bad.append("gate-word")
    for m in VIN_RE.finditer(text):
        s = m.group(0)
        if re.search(r"\d", s) and re.search(r"[A-Z]", s) and s != "<vin>":
            bad.append("vin")
            break
    return bad


def main():
    repo = sys.argv[1]
    dry = "--dry" in sys.argv
    listing = json.load(urllib.request.urlopen(urllib.request.Request(VAULT, headers=H), timeout=120))["files"]
    out, failed = {}, []
    for f in listing:
        if not f.endswith(".md") or f == "README.md":
            continue
        svc = f[:-3]
        raw = urllib.request.urlopen(urllib.request.Request(VAULT + f, headers=H), timeout=120).read().decode()
        txt, counts = sanitize(raw)
        bad = gate(txt)
        print("%-14s %6d chars  redactions=%s%s" % (svc, len(txt), json.dumps(counts), "  GATE FAIL: " + ",".join(bad) if bad else ""))
        if bad:
            failed.append(svc)
        out[svc] = txt
    if failed:
        print("GATE FAIL: %s. Nothing written." % failed)
        sys.exit(1)
    if dry:
        print("dry run, gate passed")
        return
    for svc, txt in out.items():
        os.makedirs(os.path.join(repo, svc), exist_ok=True)
        open(os.path.join(repo, svc, "api.md"), "w").write(txt)
    print("wrote %d services, gate passed" % len(out))


if __name__ == "__main__":
    main()
