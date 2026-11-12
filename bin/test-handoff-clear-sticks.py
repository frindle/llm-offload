#!/usr/bin/env python3
"""A handoff clear must STICK (2026-10-09): handoff-autoclear cleared the same 217 ids five
times over 16h because acted.json lost marks (any unreadable read became {} and was saved
over the real file; nothing healed marks erased by an outside writer). Pinned, against a
temp HANDOFF_DIR (never live state):
  * marks erased from acted.json by an outside writer come back on the next write
  * a corrupt acted.json with no last-good copy is REFUSED, not overwritten
  * a corrupt acted.json WITH a last-good copy is healed from it
  * --unact still removes a mark for good (the one legitimate removal)
  * --json overlays last-good (a lost mark cannot resurface a row) and writes nothing
  * handoff-autoclear plan() skips already-acted ids, apply() dedupes ids and records
    resurfaced ids loudly
HANDOFF=<path> / AUTOCLEAR=<path> point at a file under test (revert-check)."""
import importlib.util, json, os, subprocess, sys, tempfile
from importlib.machinery import SourceFileLoader
from pathlib import Path

HERE = Path(__file__).resolve().parent
EMIT = Path(os.environ.get("HANDOFF") or HERE / "handoff-emit.py")
AC = Path(os.environ.get("AUTOCLEAR") or HERE / "handoff-autoclear.py")
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def run(home, ho, *args):
    env = dict(os.environ, HOME=str(home), HANDOFF_DIR=str(ho))
    env.pop("HANDOFF_ACTED_NO_RESTORE", None)
    r = subprocess.run([sys.executable, str(EMIT), *args], capture_output=True, text=True, env=env, timeout=120)
    return r.returncode, r.stdout + r.stderr


def acted(ho):
    return json.loads((ho / "acted.json").read_text())


def fresh():
    home = Path(tempfile.mkdtemp(prefix="hcs-"))
    (home / "bin" / "ollama-queue-logs").mkdir(parents=True)
    ho = home / "ho"
    ho.mkdir()
    return home, ho


def load(path, name):
    ld = SourceFileLoader(name, str(path))
    m = importlib.util.module_from_spec(importlib.util.spec_from_loader(name, ld))
    sys.argv = [str(path)]
    ld.exec_module(m)
    return m


def main():
    # 1. outside writer erases marks -> next write restores them
    home, ho = fresh()
    for i in "abc":
        run(home, ho, "--acted", "id" + i, "--reason", "t1")
    chk("seeded 3 marks", sorted(acted(ho)), ["ida", "idb", "idc"])
    (ho / "acted.json").write_text("{}")                      # the outside wipe
    run(home, ho, "--acted", "idd", "--reason", "t2")
    chk("erased marks restored by the next write", sorted(acted(ho)), ["ida", "idb", "idc", "idd"])
    chk("restoration is logged for the post-mortem", (ho / "acted.restore.log").exists(), True)

    # 2. unact is the one legitimate removal and it sticks
    run(home, ho, "--unact", "ida")
    chk("--unact removes the mark for good", sorted(acted(ho)), ["idb", "idc", "idd"])
    run(home, ho, "--acted", "ide", "--reason", "t3")
    chk("...and a later write does not resurrect it", sorted(acted(ho)), ["idb", "idc", "idd", "ide"])

    # 3. corrupt file + last-good -> healed; corrupt + no last-good -> refused, untouched
    (ho / "acted.json").write_text("{not json")
    run(home, ho, "--acted", "idf", "--reason", "t4")
    chk("corrupt acted.json healed from last-good", sorted(acted(ho)), ["idb", "idc", "idd", "ide", "idf"])
    home2, ho2 = fresh()
    (ho2 / "acted.json").write_text("{not json")
    rc, out = run(home2, ho2, "--acted", "idz", "--reason", "t5")
    chk("corrupt + no last-good: refused (non-zero)", rc != 0, True)
    chk("...and the file is left untouched", (ho2 / "acted.json").read_text(), "{not json")

    # 4. --json overlays last-good and does not write
    home3, ho3 = fresh()
    run(home3, ho3, "--acted", "idq", "--reason", "t6")
    (ho3 / "acted.json").write_text("{}")
    rc, out = run(home3, ho3, "--json")
    chk("--json sees the overlaid mark", "idq" in json.loads(out)["acted"], True)
    chk("--json wrote nothing", (ho3 / "acted.json").read_text(), "{}")

    # 5. handoff-autoclear: idempotent plan/apply + resurfaced detector
    ac = load(AC, "ac_t")
    row = {"id": "r1", "label": "x", "status": "done", "gate": "FAIL", "files": 0, "cwd": None}
    data = {"complete": [row, dict(row, id="r2")], "acted": {"r1": {"ts": "t"}}}
    sc = {None: [{"id": "later", "status": "done", "exit_code": 0, "_ts": 9e9, "cwd": None}], "__ts__": {"r1": 1, "r2": 1}}
    p = ac.plan(data, [], sc, {}, use_llm=False)
    chk("plan skips an id that is already acted", [r["id"] for r, _ in p["clear"]], ["r2"])
    p2 = ac.plan({"complete": [row, row], "acted": {}}, [], sc, {}, use_llm=False)
    calls, logs = [], []
    led = Path(tempfile.mkdtemp()) / "led.json"
    ac.log = lambda rec, path=None: logs.append(rec)
    ac.apply(p2, clearer=lambda ids, why: (calls.append(list(ids)) or (0, "")), ledger_path=led)
    chk("apply dedupes a repeated id", calls, [["r1"]])
    chk("first clear is not 'resurfaced'", [l for l in logs if l.get("action") == "resurfaced"], [])
    logs.clear()
    ac.apply(p2, clearer=lambda ids, why: (0, ""), ledger_path=led)
    rs = [l for l in logs if l.get("action") == "resurfaced"]
    chk("a re-clear of an already-cleared id is logged 'resurfaced'", [l["ids"] for l in rs], [["r1"]])
    print("\nALL PASS" if not FAILS else f"\n{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
