#!/usr/bin/env python3
"""Emit the cell-E task text, GENERATED FROM THE ORACLE'S OWN TABLE.

WHY GENERATED AND NOT HAND-WRITTEN: the prose spec and the oracle must state the
same rules -- currently 29 replacement entries covering 49 sites across 12 of
the 20 files. A hand-written spec can drift from the table by one character --
and every model would then be graded against a transform it was never asked to
perform, which is indistinguishable from every model failing the same site. This
is the same class of defect as the dead-target trap inside codemod.py, so it is
closed the same way: one source of truth, read at generation time.

The spec deliberately quotes each `old` and `new` string LITERALLY, exactly as
they appear in the files. That is the point of the cell: the model must locate a
literal that contains regex metacharacters and reproduce it without escaping
what is already literal. Describing the change in words instead ("widen the
character class") would remove the trap and measure something else.
"""
import importlib.util
from pathlib import Path

FIXTURE = Path("/Users/user/Desktop/GitHub Projects/bakeoff/bakeoff-fixtures/bulk-codemod-v10")


def load_oracle():
    spec = importlib.util.spec_from_file_location("cm", FIXTURE / "codemod.py")
    cm = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cm)
    return cm


def build_task() -> str:
    cm = load_oracle()
    by_file = {}
    for rel, old, new, kind in cm.REPLACEMENTS:
        by_file.setdefault(rel, []).append((old, new))

    lines = [
        "Apply a mechanical codemod to this repository. It contains Python under `py/` "
        "and TypeScript under `ts/`.",
        "",
        "Below is the complete list of replacements. For each one, replace EVERY "
        "occurrence of the OLD text with the NEW text, in the file named. Some files "
        "contain several occurrences of the same OLD text; all of them must be replaced. "
        "Files not listed below must be left byte-for-byte unchanged.",
        "",
        "The OLD and NEW values are LITERAL TEXT, quoted exactly as they appear in the "
        "files. Several contain regular-expression syntax such as bracket classes, "
        "backslash escapes, anchors and quantifier braces. Treat them as literal "
        "characters to find and replace -- do not add or remove escaping, and do not "
        "reinterpret them as patterns to match against.",
        "",
    ]
    n = 0
    for rel in sorted(by_file):
        lines.append(f"### {rel}")
        for old, new in by_file[rel]:
            n += 1
            lines.append(f"{n}. replace")
            lines.append(f"     OLD: {old}")
            lines.append(f"     NEW: {new}")
        lines.append("")

    lines += [
        "When you are finished, every listed OLD string should be absent from its file "
        "and the corresponding NEW string present in its place.",
        "",
        "You may run `python3 -m py_compile py/*.py` to confirm the Python files still "
        "parse. That check does not tell you whether the replacements are correct or "
        "complete -- it only catches syntax damage.",
    ]
    return "\n".join(lines)


if __name__ == "__main__":
    print(build_task())
