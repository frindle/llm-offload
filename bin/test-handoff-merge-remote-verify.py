#!/usr/bin/env python3
"""handoff-emit --merged must verify a landing the LOCAL clone has not fetched
via the read-only GitHub compare API, not refuse it (2026-10-02).

Live: bfmr-replace-tracking landed e692a48 from a separate clone; the Desktop
resell-tracker repo had not fetched it, so `--merged ... --commit e692a48` REFUSED
("could not verify") and the only way through was --allow-unverified-merge --
i.e. an unattended run could not record a real merge without a human override.
Pinned: unknown-locally + GitHub says behind/identical -> True; GitHub says
ahead/diverged -> False (a branch commit is NOT a landing); no GitHub origin ->
None (unchanged behaviour); locally-landed -> True without calling gh.
--revert-check mutates the fix. HANDOFF_SRC env overrides the file under test."""
import importlib.util, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
SRC = Path(os.environ.get("HANDOFF_SRC") or HERE / "handoff-emit.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def main():
    ld = SourceFileLoader("ho_rm", str(SRC))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader("ho_rm", ld))
    sys.argv = [str(SRC)]
    ld.exec_module(m)
    tmp = Path(tempfile.mkdtemp(prefix="horm-"))
    repo = tmp / "resell-tracker"
    repo.mkdir()
    g = lambda *a: subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True,
                                  text=True).stdout.strip()
    g("init", "-q", "-b", "main"); g("config", "user.email", "t@t"); g("config", "user.name", "t")
    (repo / "f").write_text("1\n"); g("add", "f"); g("commit", "-qm", "base")
    local_sha = g("rev-parse", "HEAD")
    g("remote", "add", "origin", "https://github.com/frindle/resell-tracker.git")
    bind = tmp / "bin"
    bind.mkdir()
    calls = tmp / "gh.calls"
    status_file = tmp / "gh.status"
    (bind / "gh").write_text(f'#!/bin/bash\necho "$@" >> "{calls}"\ncat "{status_file}"\n')
    os.chmod(bind / "gh", 0o755)
    os.environ["PATH"] = f"{bind}:{os.environ['PATH']}"

    def ask(sha, gh_status):
        status_file.write_text(gh_status)
        calls.write_text("")
        return m._commit_in_default_branch(str(repo), sha), calls.read_text()

    got, c = ask("e692a48", "behind\n")
    chk("unfetched landing: GitHub says behind -> landed (True)", got, True)
    chk("...asked GitHub for the right repo + commit", "repos/frindle/resell-tracker/compare/main...e692a48" in c, True)
    chk("unfetched: GitHub identical -> True", ask("e692a48", "identical\n")[0], True)
    chk("a branch commit (GitHub ahead) is NOT a landing -> False", ask("abc1234", "ahead\n")[0], False)
    chk("GitHub cannot resolve the SHA -> None (caller refuses, as before)", ask("abc1234", "")[0], None)
    got, c = ask(local_sha, "ahead\n")
    chk("locally landed -> True without calling gh", (got, c), (True, ""))
    g("remote", "set-url", "origin", "https://gitlab.com/x/y.git")
    chk("non-GitHub origin -> None (unchanged)", ask("e692a48", "behind\n")[0], None)
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


MUTANTS = [
    ("no remote fallback", "        remote = _commit_in_default_branch_remote(base, commit)\n",
     "        remote = None\n"),
    ("ahead counted as landed", '        if st in ("behind", "identical"):', '        if st in ("behind", "identical", "ahead"):'),
    ("compare direction reversed", 'f"repos/{slug}/compare/{br}...{commit}"', 'f"repos/{slug}/compare/{commit}...{br}"'),
]


def revert_check():
    src, bad = SRC.read_text(), 0
    for name, old, new in MUTANTS:
        assert src.count(old) == 1, f"anchor missing: {name}"
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False, dir=str(HERE)) as f:
            f.write(src.replace(old, new))
        r = subprocess.run([sys.executable, __file__], env={**os.environ, "HANDOFF_SRC": f.name},
                           capture_output=True, text=True, timeout=120)
        os.unlink(f.name)
        red = r.returncode != 0
        print(("bites" if red else "INERT") + f": {name} -> suite {'RED' if red else 'green'}")
        bad += 0 if red else 1
    print("REVERT-CHECK OK" if not bad else f"REVERT-CHECK FAILED ({bad} inert)")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(revert_check() if "--revert-check" in sys.argv else main())
