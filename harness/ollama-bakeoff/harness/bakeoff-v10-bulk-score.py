#!/usr/bin/env python3
"""Scorer for v10 cell E (bulk/mechanical codemod). Byte-diff against the oracle.

THE ANSWER-KEY LEAK THIS AVOIDS
-------------------------------
The fixture repo contains `codemod.py`, which IS the answer key, and
`CHECKSUMS.sha256`, which fingerprints the pristine state. Handing a model a
worktree of that repo would let it run the oracle and score 49/49 having done
none of the work -- and the CSV would look like a triumph. `stage` therefore
copies ONLY `py/` and `ts/` into a fresh repo. Nothing else crosses.

WHAT IS SCORED
--------------
Per SITE, not per file, because a model that gets 12 of 13 sites in a file is
not equivalent to one that missed the file entirely. For every (rel, old, new)
entry the oracle declares:

    expected  = occurrences of `old` in the PRISTINE file
    got_new   = occurrences of `new` in the MODEL file
    got_old   = occurrences of `old` remaining in the MODEL file

    correct   = min(got_new, expected)          # applied as specified
    missed    = got_old                         # left untouched

`correct` is capped at `expected` so a model cannot inflate its score by
inserting extra copies of the replacement text.

Metacharacter and identifier sites are reported SEPARATELY and never pooled --
pooling would hide the over-escaping effect the cell exists to detect. A model
that over-escaped every regex would still post ~53% on a pooled number.

COLLATERAL is counted but does NOT reduce the site score (pre-registered): a
file the oracle never touches that the model modified anyway. Reported in its
own column so the temptation to fold it into one headline number is foreclosed.

TWO-RUN CALIBRATION (`--calibrate`) is the guard against the failure mode that
actually threatens this cell: an ORACLE BUG SILENTLY PASSING EVERYONE. A
known-good tree must score perfect; a known-bad tree with exactly one wrong
replacement must be caught AND the site named. If either fails, cell E does not
dispatch.
"""
import argparse
import csv
import importlib.util
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

FIXTURE = Path("/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-fixtures/bulk-codemod-v10")
SRC_DIRS = ("py", "ts")


def load_oracle():
    spec = importlib.util.spec_from_file_location("cm", FIXTURE / "codemod.py")
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
    return cm


def copy_sources(dst: Path):
    """Only py/ and ts/. codemod.py and CHECKSUMS.sha256 must never be copied."""
    dst.mkdir(parents=True, exist_ok=True)
    for d in SRC_DIRS:
        shutil.copytree(FIXTURE / d, dst / d, dirs_exist_ok=True)
    for forbidden in ("codemod.py", "CHECKSUMS.sha256"):
        leaked = dst / forbidden
        if leaked.exists():
            raise SystemExit(f"FATAL: {forbidden} leaked into {dst}")


def stage(slug_dir: Path):
    """Fresh working copy for one model, with the answer key absent."""
    if slug_dir.exists():
        shutil.rmtree(slug_dir)
    copy_sources(slug_dir)
    subprocess.run(["git", "init", "-q"], cwd=slug_dir, check=True)
    subprocess.run(["git", "add", "-A"], cwd=slug_dir, check=True)
    subprocess.run(["git", "-c", "user.email=b@b", "-c", "user.name=bakeoff",
                    "commit", "-qm", "pristine"], cwd=slug_dir, check=True)
    return slug_dir


def build_oracle_tree(dst: Path):
    copy_sources(dst)
    cm = load_oracle()
    cm.apply(dst, report=False)
    return dst


def score_tree(model_dir: Path):
    cm = load_oracle()
    pristine = Path(tempfile.mkdtemp()) / "p"
    copy_sources(pristine)

    per_kind = {"ident": [0, 0], "meta": [0, 0]}      # [correct, expected]
    missed_sites = 0
    detail = []
    target_files = set()

    for rel, old, new, kind in cm.REPLACEMENTS:
        target_files.add(rel)
        expected = (pristine / rel).read_text(encoding="utf-8").count(old)
        mf = model_dir / rel
        if not mf.is_file():
            detail.append((rel, kind, expected, 0, expected, "FILE MISSING"))
            per_kind[kind][1] += expected
            missed_sites += expected
            continue
        txt = mf.read_text(encoding="utf-8", errors="replace")
        got_new = txt.count(new)
        got_old = txt.count(old)
        correct = min(got_new, expected)
        per_kind[kind][0] += correct
        per_kind[kind][1] += expected
        missed_sites += got_old
        note = "" if correct == expected and got_old == 0 else "PARTIAL/MISSED"
        detail.append((rel, kind, expected, correct, got_old, note))

    # collateral: a file the oracle never touches, modified anyway
    collateral = []
    for d in SRC_DIRS:
        for f in sorted((pristine / d).rglob("*")):
            if not f.is_file():
                continue
            rel = f.relative_to(pristine).as_posix()
            if rel in target_files:
                continue
            mf = model_dir / rel
            if not mf.is_file():
                collateral.append((rel, "DELETED"))
            elif mf.read_bytes() != f.read_bytes():
                collateral.append((rel, "MODIFIED"))

    shutil.rmtree(pristine.parent, ignore_errors=True)
    tot_c = per_kind["ident"][0] + per_kind["meta"][0]
    tot_e = per_kind["ident"][1] + per_kind["meta"][1]
    return {
        "sites_correct": tot_c, "sites_total": tot_e,
        "ident_correct": per_kind["ident"][0], "ident_total": per_kind["ident"][1],
        "meta_correct": per_kind["meta"][0], "meta_total": per_kind["meta"][1],
        "sites_missed": missed_sites,
        "collateral_files": len(collateral),
        "collateral_detail": collateral,
        "detail": detail,
    }


def calibrate():
    """Known-good must be perfect; known-bad must be caught and the site named."""
    ok = True
    tmp = Path(tempfile.mkdtemp())

    good = build_oracle_tree(tmp / "good")
    g = score_tree(good)
    good_ok = (g["sites_correct"] == g["sites_total"]
               and g["sites_missed"] == 0 and g["collateral_files"] == 0)
    print(f"[known-good] {g['sites_correct']}/{g['sites_total']} sites, "
          f"missed={g['sites_missed']}, collateral={g['collateral_files']} -> "
          f"{'PASS' if good_ok else '*** FAIL ***'}")
    ok &= good_ok

    # known-bad: revert exactly ONE replacement, so the scorer must find it.
    cm = load_oracle()
    bad = build_oracle_tree(tmp / "bad")
    rel, old, new, kind = cm.REPLACEMENTS[0]
    p = bad / rel
    t = p.read_text(encoding="utf-8")
    n_before = t.count(new)
    p.write_text(t.replace(new, old), encoding="utf-8")
    b = score_tree(bad)
    caught = b["sites_correct"] < b["sites_total"]
    named = [d for d in b["detail"] if d[0] == rel and d[5]]
    bad_ok = caught and bool(named)
    print(f"[known-bad ] reverted {n_before} site(s) of {old!r} in {rel}")
    print(f"[known-bad ] {b['sites_correct']}/{b['sites_total']} sites, "
          f"missed={b['sites_missed']} -> {'PASS' if bad_ok else '*** FAIL ***'}"
          f"{'  named: ' + rel if named else '  *** SITE NOT NAMED ***'}")
    ok &= bad_ok

    shutil.rmtree(tmp, ignore_errors=True)
    print("\nCALIBRATION PASSED -- cell E may dispatch" if ok
          else "\n*** CALIBRATION FAILED -- cell E MUST NOT DISPATCH ***")
    return 0 if ok else 1


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--calibrate", action="store_true")
    ap.add_argument("--stage", metavar="DIR")
    ap.add_argument("--score", metavar="DIR")
    ap.add_argument("--verbose", action="store_true")
    a = ap.parse_args()

    if a.calibrate:
        return calibrate()
    if a.stage:
        d = stage(Path(a.stage))
        print(f"staged {d} (answer key absent)")
        return 0
    if a.score:
        s = score_tree(Path(a.score))
        print(f"sites {s['sites_correct']}/{s['sites_total']}  "
              f"ident {s['ident_correct']}/{s['ident_total']}  "
              f"meta {s['meta_correct']}/{s['meta_total']}  "
              f"missed {s['sites_missed']}  collateral {s['collateral_files']}")
        if a.verbose:
            for rel, kind, exp, cor, left, note in s["detail"]:
                if note:
                    print(f"   {rel:28s} {kind:5s} expected={exp} correct={cor} left={left} {note}")
            for rel, how in s["collateral_detail"]:
                print(f"   COLLATERAL {rel} {how}")
        return 0
    ap.print_help()
    return 1


if __name__ == "__main__":
    sys.exit(main())
