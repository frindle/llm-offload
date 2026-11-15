#!/usr/bin/env python3
"""Check that data files the image's code names by path are in MANIFEST.

  python3 check-data-files.py --base DIR [--list MANIFEST]

stage.sh copies only what MANIFEST lists. A code file that opens another file under ~/bin by a literal path
(`HOME / "bin/configs/mini-local.yaml"`, `"model_profiles.yaml"`) fails at run time in the container if that file
was left out, and nothing earlier notices: the 11-14 back-test found mini-swe-runner's four configs/ overlays
missing, so every Darkbloom mini job would have stopped at its first step.

For each listed code file (.py, .sh, or no extension), every quoted string that names a file under DIR (after
dropping a leading "~/bin/" or "bin/"; absolute paths are skipped) must be listed in MANIFEST or in NOT_SHIPPED below. Prints each miss
as `MISSING <path>  (named in <file>)` and exits 1 if there is one, else 0.
"""
import re
import sys
from pathlib import Path

# Named by listed code but deliberately not in the image. Each needs a reason.
NOT_SHIPPED = {
    "configs/graphify-lane.json": "graphify-lane's own config; graphify-lane stays a Mac job (MANIFEST.md)",
    "CLAUDE.md": "land_packet.py looks for it in the project repo being landed, not in ~/bin",
    "README.md": "a project repo's file in ollama-queue.py's self-tests, not a ~/bin path",
    "bloom_control.py": "mini_ctx.py's heartbeat asks it for Darkbloom's phase and shows '?' without it; it controls "
                        "Darkbloom on the Mac, so it cannot run in the container",
    "dispatch-worktree-reap": "reaps the Mac's dispatch worktrees under ~/Desktop/GitHub Projects; the container has "
                              "none, and dispatch-escalation-watcher skips the daily reap when the tool is absent",
}

LITERAL = re.compile(r"""["']((?:~/)?[A-Za-z0-9_][A-Za-z0-9_./-]*)["']""")  # absolute paths are not ~/bin files
CODE_EXT = {".py", ".sh", ""}


def listed(manifest):
    out = []
    for ln in manifest.read_text().splitlines():
        ln = ln.split("#", 1)[0].strip()
        if ln:
            out.append(ln)
    return out


def norm(s):
    for p in ("~/bin/", "bin/"):
        if s.startswith(p):
            return s[len(p):]
    return s


def misses(base, files):
    have = set(files)
    found = {}
    for rel in files:
        p = base / rel
        if Path(rel).suffix not in CODE_EXT or not p.is_file():
            continue
        try:
            text = p.read_text(errors="replace")
        except OSError:
            continue
        for m in LITERAL.finditer(text):
            s = norm(m.group(1))
            if not s or s in have or s in NOT_SHIPPED or ".." in s.split("/"):
                continue
            t = base / s
            if t.is_file() and not t.is_symlink():
                found.setdefault(s, rel)
    return found


def main(argv):
    base = Path(argv[argv.index("--base") + 1]) if "--base" in argv else None
    if base is None or not base.is_dir():
        print("usage: check-data-files.py --base DIR [--list MANIFEST]", file=sys.stderr)
        return 2
    man = Path(argv[argv.index("--list") + 1]) if "--list" in argv else Path(__file__).resolve().parent / "MANIFEST"
    found = misses(base, listed(man))
    for s, rel in sorted(found.items()):
        print("MISSING %s  (named in %s)" % (s, rel))
    print("check-data-files: %d missing" % len(found))
    return 1 if found else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
