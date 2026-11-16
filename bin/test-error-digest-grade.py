#!/usr/bin/env python3
"""Tests for error_digest_grade.py (terminal grading of the container error digests), 2026-11-15.

Synthetic digests in a temp dir, scripted answers; no queue, no network.
  python3 test-error-digest-grade.py      prints ALL PASS / N FAILED (rc 0 / 1)
"""
import json
import shutil
import sys
import tempfile
from pathlib import Path

BIN = Path(__file__).resolve().parent
sys.path.insert(0, str(BIN))
import error_digest_grade as G  # noqa: E402

FAILS, N = [], [0]


def chk(name, cond, detail=""):
    N[0] += 1
    if not cond:
        FAILS.append(name)
        print("FAIL", name, detail)


def sig(i, level="error", new=True, count=5):
    return {"id": "id%03d" % i, "text": "kind %d went wrong at <time>" % i, "level": level, "count": count,
            "first_ns": 0, "last_ns": 0, "variants": 1, "new": new}


def write_day(d, day, containers, lines=None, baseline=False, md=""):
    rec = {"day": day, "baseline": baseline, "containers": {c: {"sigs": ss} for c, ss in containers.items()},
           "model": {"status": "done", "lines": lines or {}}}
    (d / "digests" / (day + ".json")).write_text(json.dumps(rec))
    (d / "digests" / (day + ".md")).write_text(md)


def setup():
    d = Path(tempfile.mkdtemp(prefix="edg-"))
    (d / "digests").mkdir()
    write_day(d, "2026-11-01", {"alpha": [sig(900)]}, baseline=True)
    alpha = [sig(i, count=100 - i) for i in range(1, 12)] + [sig(50, level="warn", count=999)]
    write_day(d, "2026-11-02", {"alpha": alpha, "beta": [sig(20), sig(21, new=False)], "(one-off containers)": [sig(30)]},
              lines={"id002": "Alpha could not reach its database."},
              md="- error, 98×: `kind 2 went wrong at <time>`\n")
    write_day(d, "2026-11-03", {"alpha": [sig(1)], "beta": [sig(22)]})   # id001 again: first day stays 11-02
    return d


def feeder(answers):
    it = iter(answers)

    def inp(prompt=""):
        try:
            return next(it)
        except StopIteration:
            raise EOFError
    return inp


def run(d, answers):
    out = []
    rc = G.grade(["--dir", str(d)], inp=feeder(answers), out=out.append)
    return rc, "\n".join(map(str, out))


def grades(d):
    return json.loads((d / "grades.json").read_text())


def t_load_kinds():
    d = setup()
    k = G.load_kinds(d)
    chk("baseline day skipped", "alpha|id900" not in k)
    chk("not-new kinds skipped", "beta|id021" not in k)
    chk("all new kinds found", len(k) == 12 + 2 + 1, len(k))
    chk("first day it was new wins", k["alpha|id001"]["day"] == "2026-11-02")
    chk("model note attached", k["alpha|id002"]["note"] == "Alpha could not reach its database.")
    chk("no note -> None", k["alpha|id003"]["note"] is None)
    shutil.rmtree(d)


def t_parse():
    chk("numbers", G.parse_answer("1 4", 10) == ("grade", {1, 4}, set()))
    chk("commas", G.parse_answer("1,4", 10) == ("grade", {1, 4}, set()))
    chk("enter = none", G.parse_answer("", 10) == ("grade", set(), set()))
    chk("wrong note", G.parse_answer("w3", 10) == ("grade", set(), {3}))
    chk("range", G.parse_answer("2-4 w1", 10) == ("grade", {2, 3, 4}, {1}))
    chk("skip", G.parse_answer(" S ", 10)[0] == "s")
    chk("quit", G.parse_answer("q", 10)[0] == "q")
    chk("out of range", G.parse_answer("11", 10)[0] == "bad")
    chk("zero", G.parse_answer("0", 3)[0] == "bad")
    chk("junk", G.parse_answer("yes", 10)[0] == "bad")
    chk("bad range", G.parse_answer("4-2", 10)[0] == "bad")
    chk("w with range is bad", G.parse_answer("w1-3", 10)[0] == "bad")
    chk("x", G.parse_answer("X", 10)[0] == "x")


def t_flow():
    d = setup()
    # alpha page 1 (10 kinds): want 1 and 3 (warn 999 sorts after errors), note 2 wrong; then quit on page 2
    rc, out = run(d, ["1 3 w2", "q"])
    chk("quit rc 0", rc == 0, out)
    g = grades(d)
    chk("page 1 saved before quit", len(g["kinds"]) == 10, len(g["kinds"]))
    order = sorted(G.load_kinds(d).values(), key=lambda k: (k["container"] != "alpha", k["level"] != "error", -k["count"]))
    first = order[0]
    chk("errors first, biggest first", first["id"] == "id001", first["id"])
    chk("picked = want", g["kinds"]["alpha|id001"]["want"] is True and g["kinds"]["alpha|id003"]["want"] is True)
    chk("not picked = not wanted", g["kinds"]["alpha|id004"]["want"] is False)
    chk("note marked wrong", g["kinds"]["alpha|id002"]["note"] == "wrong")
    chk("no note -> None", g["kinds"]["alpha|id003"]["note"] is None)
    chk("id stored", g["kinds"]["alpha|id001"]["id"] == "id001")
    chk("no tmp left", not (d / "grades.json.tmp").exists())
    # rerun: alpha page 2 (1 error + 1 warn), bad answer then fixed, skip beta, one-off none, then a missed note
    rc, out = run(d, ["7", "", "s", "", "the nextcloud cron error was missing"])
    chk("bad answer re-asked", "not a number between 1 and 2" in out, out[-400:])
    g = grades(d)
    chk("alpha page 2 graded", "alpha|id011" in g["kinds"] and "alpha|id050" in g["kinds"])
    chk("re-asked answer used", g["kinds"]["alpha|id011"]["want"] is False and g["kinds"]["alpha|id050"]["want"] is False)
    chk("skipped container left ungraded", not any(k.startswith("beta|") for k in g["kinds"]))
    chk("one-off graded last", "(one-off containers)|id030" in g["kinds"])
    chk("missed note kept", g["missed"] and g["missed"][0]["note"] == "the nextcloud cron error was missing")
    chk("report printed", "Graded 13 new error kinds" in out, out[-600:])
    # third run: only beta left; EOF counts as quit
    rc, out = run(d, [])
    chk("EOF saves and stops", rc == 0 and "Saved." in out, out)
    rc, out = run(d, ["1"])
    g = grades(d)
    chk("beta graded", g["kinds"]["beta|id020"]["want"] is True and g["kinds"]["beta|id022"]["want"] is False)
    rc, out = run(d, [])
    chk("nothing left", "Nothing new to grade" in out, out)
    shutil.rmtree(d)


def t_report():
    d = setup()
    run(d, ["1 2", "", "", "", ""])
    out = []
    G.report(["--dir", str(d)], out=out.append)
    text = "\n".join(out)
    chk("counts", "Graded 15 new error kinds: 2 worth hearing about, 13 not needed." in text, text)
    chk("notes", "'Probably' notes: 1 shown, 0 marked wrong." in text, text)
    # kind 2 is in the 11-02 .md, kind 1 is not -> 1 of 2 not listed by name
    chk("named check", "did not list by name: 1 of 2." in text, text)
    chk("quiet containers", "candidates for the noise list" not in text or "alpha" not in text.split("noise list")[-1], text)
    shutil.rmtree(d)


def t_rest_none():
    d = setup()
    # alpha has 12: "x" on page 1 marks all 12 not wanted; note on id002 not read -> "unread"; then beta "1", one-off q
    rc, out = run(d, ["x", "1", "q"])
    g = grades(d)
    a = {k: v for k, v in g["kinds"].items() if k.startswith("alpha|")}
    chk("x grades the whole container", len(a) == 12, len(a))
    chk("x = not wanted", not any(v["want"] for v in a.values()))
    chk("x on a shown page counts the note as read", a["alpha|id002"]["note"] == "not marked", a["alpha|id002"])
    chk("bulk flag only beyond the page", a["alpha|id050"]["bulk"] is True and a["alpha|id001"]["bulk"] is False)
    chk("next container asked", g["kinds"]["beta|id020"]["want"] is True)
    shutil.rmtree(d)


def t_unchecked():
    d = setup()
    (d / "digests" / "2026-11-02.md").unlink()
    run(d, ["1", "", "", "", ""])
    out = []
    G.report(["--dir", str(d)], out=out.append)
    chk("unchecked counted", "1 could not be checked" in "\n".join(out), out)
    shutil.rmtree(d)


def t_no_dir():
    d = Path(tempfile.mkdtemp(prefix="edg-"))
    out = []
    rc = G.grade(["--dir", str(d)], inp=feeder([]), out=out.append)
    chk("no digests -> rc 1", rc == 1 and "no digests" in out[0])
    shutil.rmtree(d)


for t in (t_load_kinds, t_parse, t_flow, t_report, t_rest_none, t_unchecked, t_no_dir):
    t()
print("ALL PASS (%d checks)" % N[0] if not FAILS else "%d FAILED of %d" % (len(FAILS), N[0]))
sys.exit(1 if FAILS else 0)
