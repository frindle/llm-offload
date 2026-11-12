#!/usr/bin/env python3
"""Mechanical constraint checker for a bulk-codemod fixture.

THIS IS THE ORACLE FOR CELL F (instrument construction). It is also the thing
that caught the v10 fixture failing its own approved spec, so it is not a
hypothetical: run against `bulk-codemod-v10` at `ea5c773` it returns FAIL on
file spread; at `b2d9b21` it returns PASS on all ten.

WHY A MODEL CAN BE SCORED ON FIXTURE-BUILDING AT ALL
-----------------------------------------------------
Most "prep work" has no ground truth -- design a round, write the results-read --
and grading it needs judgement, which means an LLM judge, which is the failure
mode that got the v9 quality arm descoped. Fixture construction is the exception:
every property that makes a fixture valid is decidable by a script. Either every
literal matches exactly once or it does not. Either the transform is idempotent
or it is not. There is nothing to adjudicate.

WHAT IT DOES NOT CHECK
----------------------
Whether the fixture is INTERESTING -- whether its targets exercise a failure mode
worth measuring. That is a judgement call and it stays with a human. A fixture can
pass all ten checks and still be a bad test, so a PASS here is a floor, never an
endorsement.
"""
import argparse
import importlib.util
import re
import sys
from collections import defaultdict
from pathlib import Path

SPEC = dict(
    sites_min=45, sites_max=50,
    files_min=12, files_max=14,
    inert_min=6, inert_max=8,
    concentration_max=0.30,
    meta_share_min=1/3,
    meta_files_min=4,
    meta_lang_files_min=2,
)


def load_codemod(fixture: Path):
    spec = importlib.util.spec_from_file_location("cm", fixture / "codemod.py")
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
    return cm


def check(fixture: Path, spec=SPEC, second_lang_ext=".ts"):
    cm = load_codemod(fixture)
    per = defaultdict(int)
    meta = defaultdict(int)
    dead = []
    not_idem = []

    for rel, old, new, kind in cm.REPLACEMENTS:
        f = fixture / rel
        n = f.read_text(encoding="utf-8").count(old) if f.is_file() else 0
        if n == 0:
            dead.append((rel, old))
        if old in new:
            not_idem.append((rel, old))
        per[rel] += n
        if kind == "meta":
            meta[rel] += n

    total = sum(per.values())
    mtot = sum(meta.values())
    srcs = [p for p in sorted(fixture.rglob("*"))
            if p.is_file() and p.suffix in (".py", second_lang_ext)
            and "__pycache__" not in p.parts and p.name != "codemod.py"]
    carrying = len(per)
    inert = len(srcs) - carrying
    top = max(per.values()) if per else 0
    meta_files = len(meta)
    meta_lang = len([f for f in meta if f.endswith(second_lang_ext)])
    src_text = (fixture / "codemod.py").read_text(encoding="utf-8")
    non_ascii = [c for c in src_text if ord(c) > 127]

    checks = [
        (f"total sites {spec['sites_min']}-{spec['sites_max']}",
         spec["sites_min"] <= total <= spec["sites_max"], str(total)),
        (f"files carrying {spec['files_min']}-{spec['files_max']}",
         spec["files_min"] <= carrying <= spec["files_max"], str(carrying)),
        (f"files inert {spec['inert_min']}-{spec['inert_max']}",
         spec["inert_min"] <= inert <= spec["inert_max"], str(inert)),
        (f"top file <= {int(spec['concentration_max']*100)}% of sites",
         total > 0 and top <= spec["concentration_max"] * total,
         f"{top}/{total} = {100*top/total:.1f}%" if total else "n/a"),
        ("meta share >= 1/3", total > 0 and mtot >= total * spec["meta_share_min"],
         f"{mtot}/{total} = {100*mtot/total:.1f}%" if total else "n/a"),
        (f"meta in >= {spec['meta_files_min']} files",
         meta_files >= spec["meta_files_min"], str(meta_files)),
        (f"meta in >= {spec['meta_lang_files_min']} {second_lang_ext} files",
         meta_lang >= spec["meta_lang_files_min"], str(meta_lang)),
        ("no dead targets", not dead,
         "all match" if not dead else f"{len(dead)} DEAD: {dead[:2]}"),
        ("idempotent by construction", not not_idem,
         "old not in new" if not not_idem else f"{len(not_idem)} VIOLATIONS"),
        ("codemod.py pure ASCII", not non_ascii,
         "0 non-ASCII" if not non_ascii else f"{len(non_ascii)} non-ASCII chars"),
    ]
    return checks, dict(total=total, carrying=carrying, inert=inert,
                        meta=mtot, per=dict(per))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("fixture")
    ap.add_argument("--second-lang-ext", default=".ts")
    a = ap.parse_args()
    checks, _ = check(Path(a.fixture), second_lang_ext=a.second_lang_ext)
    width = max(len(c[0]) for c in checks)
    ok = True
    print(f"{'CONSTRAINT':{width}s}  {'':4s} value")
    print("-" * (width + 30))
    for name, passed, val in checks:
        ok &= passed
        print(f"{name:{width}s}  {'PASS' if passed else 'FAIL':4s} {val}")
    print("-" * (width + 30))
    score = sum(1 for _, p, _ in checks if p)
    print(f"{score}/{len(checks)} constraints met -- "
          + ("FIXTURE VALID" if ok else "FIXTURE INVALID"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
