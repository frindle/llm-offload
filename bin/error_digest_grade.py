#!/usr/bin/env python3
"""error_digest_grade: grade the container error digests in the terminal, a page at a time (2026-11-15).

The operator grades the digests 11-16..11-29: which NEW error kinds are worth hearing about, and which of the
model's "Probably:" notes are wrong. The pass mark for the digest: it names every kind the operator wants, invents none.

  error_digest_grade.py [--dir D]            grade every new kind not graded yet (pages of 10, per container)
  error_digest_grade.py report [--dir D]     the score so far

Per page the operator types the numbers worth hearing about ("1 4"), Enter for none, "w3" when note 3 is wrong, "x" for
none of the rest of this container, "s" to skip the container for now, "q" to save and stop. Everything on an answered page is graded (not picked = not needed).
Grades go to <dir>/grades.json after every page (atomic), so stopping anywhere loses nothing; a rerun picks up the
kinds still ungraded, including new days. Reads only the digest JSON files; changes no container, no noise list.
Dir: --dir, else $ERROR_DIGEST_DIR, else ~/.ollama-dispatch/error-digest (the queue container's digest folder).
"""
import datetime as dt
import json
import os
import shutil
import sys
from pathlib import Path

PAGE = 10


def digest_dir(argv):
    if "--dir" in argv:
        return Path(argv[argv.index("--dir") + 1]).expanduser()
    return Path(os.environ.get("ERROR_DIGEST_DIR") or Path.home() / ".ollama-dispatch" / "error-digest")


def load_kinds(ddir):
    """-> {key: kind} for every NEW kind in every digest, first day it was new. key = "container|sig id"."""
    kinds = {}
    for f in sorted((ddir / "digests").glob("*.json")):
        try:
            rec = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if rec.get("baseline"):
            continue
        meaning = ((rec.get("model") or {}).get("lines") or {})
        for c, info in (rec.get("containers") or {}).items():
            for s in info.get("sigs") or []:
                if not s.get("new"):
                    continue
                key = "%s|%s" % (c, s["id"])
                if key in kinds:
                    continue
                kinds[key] = {"container": c, "id": s["id"], "day": rec.get("day", f.stem), "level": s.get("level"),
                              "count": s.get("count", 0), "text": s.get("text", ""), "note": meaning.get(s["id"])}
    return kinds


def load_grades(ddir):
    try:
        g = json.loads((ddir / "grades.json").read_text())
    except (OSError, ValueError):
        g = {}
    g.setdefault("kinds", {})
    g.setdefault("missed", [])
    return g


def save_grades(ddir, g):
    p = ddir / "grades.json"
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(g, indent=1, sort_keys=True))
    os.replace(tmp, p)


def parse_answer(ans, n):
    """'1 4 w3' -> ('grade', {1, 4}, {3}); '' -> ('grade', set(), set()); 's'/'q' -> (cmd, ...); bad -> ('bad', msg)."""
    a = ans.strip().lower()
    if a in ("s", "q", "x"):
        return a, set(), set()
    want, wrong = set(), set()
    for tok in a.replace(",", " ").split():
        w = tok.startswith("w")
        t = tok[1:] if w else tok
        if "-" in t and not w:
            lo, _, hi = t.partition("-")
            if not (lo.isdigit() and hi.isdigit()) or not 1 <= int(lo) <= int(hi) <= n:
                return "bad", "%r is not a range between 1 and %d" % (tok, n), None
            want.update(range(int(lo), int(hi) + 1))
            continue
        if not t.isdigit() or not 1 <= int(t) <= n:
            return "bad", "%r is not a number between 1 and %d" % (tok, n), None
        (wrong if w else want).add(int(t))
    return "grade", want, wrong


def _cut(s, width):
    s = " ".join(str(s).split())
    return s if len(s) <= width else s[:width - 1] + "…"


def show_page(c, page, start, total, width, out=print):
    out("")
    out("== %s   (%d-%d of %d new kinds still to grade here)" % (c, start + 1, start + len(page), total))
    for i, k in enumerate(page, 1):
        head = "%2d. %-5s ×%-6s %s  " % (i, k["level"] or "?", f"{k['count']:,}", k["day"][5:])
        out(head + _cut(k["text"], max(30, width - len(head))))
        if k.get("note"):
            out("    Probably: " + _cut(k["note"], max(30, width - 14)))


HELP = ("Which would you want to hear about?  Type the numbers (1 4 7, or 2-5), or Enter for none of these.\n"
        "Also: w3 = the 'Probably' note on 3 is wrong | x = none of the rest of this container |\n"
        "      s = skip this container for now | q = save and stop")


def grade(argv, inp=input, out=print):
    ddir = digest_dir(argv)
    if not (ddir / "digests").is_dir():
        out("error-digest-grade: no digests in %s" % ddir)
        return 1
    kinds = load_kinds(ddir)
    g = load_grades(ddir)
    todo = [k for key, k in kinds.items() if key not in g["kinds"]]
    if not todo:
        out("Nothing new to grade (%d kinds graded). Score: error_digest_grade.py report" % len(g["kinds"]))
        return 0
    by_c = {}
    for k in todo:
        by_c.setdefault(k["container"], []).append(k)
    order = sorted(by_c, key=lambda c: (c.startswith("("), c.lower()))
    width = shutil.get_terminal_size((120, 40)).columns - 1
    out("%d new error kinds to grade, in %d containers. Your answers save after every page." % (len(todo), len(order)))
    out(HELP)
    for c in order:
        ks = sorted(by_c[c], key=lambda k: (k["level"] != "error", -k["count"], k["day"]))
        start = 0
        while start < len(ks):
            page = ks[start:start + PAGE]
            show_page(c, page, start, len(ks), width, out)
            while True:
                try:
                    ans = inp("> ")
                except EOFError:
                    ans = "q"
                res = parse_answer(ans, len(page))
                if res[0] != "bad":
                    break
                out(res[1] + ".  (numbers, Enter = none, w3, x, s, q)")
            if res[0] == "q":
                out("Saved. %d graded so far; run the same command again to go on." % len(g["kinds"]))
                return 0
            if res[0] == "s":
                break
            _, want, wrong = res
            now = dt.datetime.now().astimezone().isoformat(timespec="seconds")
            batch = ks[start:] if res[0] == "x" else page
            for i, k in enumerate(batch, 1):
                note = None
                if k.get("note"):
                    note = "unread" if (res[0] == "x" and i > len(page)) else ("wrong" if i in wrong else "not marked")
                g["kinds"]["%s|%s" % (k["container"], k["id"])] = {
                    "container": k["container"], "id": k["id"], "day": k["day"], "level": k["level"],
                    "want": res[0] == "grade" and i in want, "note": note, "graded": now, "bulk": i > len(page)}
            save_grades(ddir, g)
            start += len(batch)
    try:
        note = inp("\nDid a digest miss an error you cared about? Type it (Enter to finish): ").strip()
    except EOFError:
        note = ""
    if note:
        g["missed"].append({"note": note, "added": dt.datetime.now().astimezone().isoformat(timespec="seconds")})
        save_grades(ddir, g)
    out("Done: %d kinds graded." % len(g["kinds"]))
    report(argv, out=out)
    return 0


def report(argv, out=print):
    ddir = digest_dir(argv)
    g = load_grades(ddir)
    ks = list(g["kinds"].values())
    if not ks:
        out("No grades yet.")
        return 0
    want = [k for k in ks if k.get("want")]
    noted = [k for k in ks if k.get("note") in ("wrong", "not marked")]
    wrong = [k for k in noted if k["note"] == "wrong"]
    out("Graded %d new error kinds: %d worth hearing about, %d not needed." % (len(ks), len(want), len(ks) - len(want)))
    out("'Probably' notes: %d shown, %d marked wrong." % (len(noted), len(wrong)))
    shown_missing, unchecked = [], 0
    for k in want:
        md = ddir / "digests" / (k["day"] + ".md")
        try:
            text = md.read_text()
        except OSError:
            unchecked += 1
            continue
        kinds = load_kinds_cache(ddir)
        full = kinds.get("%s|%s" % (k["container"], k.get("id", ""))) if k.get("id") else None
        if full and " ".join(full["text"].split())[:60].replace("`", "'") not in " ".join(text.split()).replace("`", "'"):
            shown_missing.append(k)
    out("Wanted kinds the daily digest did not list by name: %d of %d.%s" % (
        len(shown_missing), len(want), " (%d could not be checked: no .md for that day)" % unchecked if unchecked else ""))
    per = {}
    for k in ks:
        p = per.setdefault(k["container"], [0, 0])
        p[0] += 1
        p[1] += bool(k.get("want"))
    quiet = sorted(c for c, (n, w) in per.items() if w == 0 and n >= 3)
    if quiet:
        out("Containers where you wanted none of 3+ kinds (candidates for the noise list, nothing changed): "
            + ", ".join(quiet))
    if g.get("missed"):
        out("Misses you noted: %d" % len(g["missed"]))
    return 0


_KC = {}


def load_kinds_cache(ddir):
    if ddir not in _KC:
        _KC[ddir] = load_kinds(ddir)
    return _KC[ddir]


def main(argv=None):
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] in ("-h", "--help"):
        print(__doc__)
        return 0
    if argv and argv[0] == "report":
        return report(argv[1:])
    return grade(argv)


if __name__ == "__main__":
    sys.exit(main())
