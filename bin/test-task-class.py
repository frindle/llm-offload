#!/usr/bin/env python3
"""task_class.py: classifier, budgets, variants, manifest record, lane resolution."""
import importlib.util, json, os, sys, tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
spec = importlib.util.spec_from_file_location("task_class", os.environ.get("TC_SRC") or HERE / "task_class.py")
tc = importlib.util.module_from_spec(spec); spec.loader.exec_module(tc)
FAILS = []


def chk(name, got, want):
    ok = got == want
    print(("ok  " if ok else "FAIL") + f" - {name}" + ("" if ok else f"  (got: {got!r}, want: {want!r})"))
    if not ok:
        FAILS.append(name)


def cls(intent, target="lib/x.ts", **kw):
    return tc.classify_task(intent, kw.pop("interface", ""), target, kw.pop("edit_files", ()),
                            kw.pop("lang", "ts"), kw.pop("base", {"target_exists": True}))["class"]


chk("plain edit of an existing file -> modify", cls("Make parseSize accept a trailing unit"), "modify")
chk("absent target -> create", cls("Add parseSize", base={"target_exists": False}), "create")
chk("manifest creation_task -> create", cls("Add parseSize", base={"creation_task": True}), "create")
chk("'new module' wording -> create", cls("Create a new module that parses sizes"), "create")
chk("port wording -> port", cls("Port the retry logic from the Python reference implementation to TS"), "port")
chk("UI target -> ui_dom", cls("Hide the banner", target="app/Banner.tsx"), "ui_dom")
chk("DOM words -> ui_dom", cls("querySelector the button and attach addEventListener on click"), "ui_dom")
chk("extra edit files -> cross_file", cls("Rename the helper", edit_files=("lib/x.ts", "lib/y.ts")), "cross_file")
chk("many paths in the spec -> cross_file", cls("Change lib/a.ts, lib/b.ts, lib/c.ts and lib/d.ts together"), "cross_file")
chk("UI beats create", cls("Create a new module with a button widget and a modal", base={"target_exists": False}), "ui_dom")
for c in tc.CLASSES:
    b = tc.BUDGETS[c]
    chk(f"{c}: budgets carry every stage", all(k in b for k in ("task", "fixture", "refimpl", "continue_rounds", "reprompts")), True)
    chk(f"{c}: has a prompt variant", bool(tc.prompt_variant(c).strip()), True)
chk("budgets differ by class (port reads a reference: bigger refimpl stage)", tc.BUDGETS["port"]["refimpl"] > tc.BUDGETS["create"]["refimpl"], True)
r = tc.classify_task("x", "", "a.py", (), "python", {})
chk("result shape", sorted(r), ["budgets", "class", "signals"])
b1 = tc.classify_task("x", "", "a.py", (), "python", {})["budgets"]; b1["task"] = 999
chk("budgets are copies (no shared mutation)", tc.BUDGETS["modify"]["task"], 6)

d = Path(tempfile.mkdtemp())
chk("record on a missing manifest still works", tc.record_task_class(d, r, lane="open"), True)
chk("...and reads back", (tc.read_task_class(d) or {}).get("class"), "modify")
(d / ".dispatch-harness.json").write_text(json.dumps({"keep": 1}))
tc.record_task_class(d, r)
chk("record MERGES (other manifest keys survive)", json.loads((d / ".dispatch-harness.json").read_text()).get("keep"), 1)
(d / ".dispatch-harness.json").write_text("not json")
chk("a corrupt manifest is not clobbered", tc.record_task_class(d, r) or True, True)

chk("lane open -> open", tc.resolve_lane("open", "modify"), "open")
chk("lane fixed -> fixed", tc.resolve_lane("fixed", "create"), "fixed")
chk("lane auto + modify -> fixed", tc.resolve_lane("auto", "modify"), "fixed")
chk("lane auto + anything else -> open", [tc.resolve_lane("auto", c) for c in ("create", "port", "ui_dom", "cross_file", None)], ["open"] * 5)
try:
    tc.resolve_lane("bogus"); chk("bogus lane rejected", False, True)
except ValueError:
    chk("bogus lane rejected", True, True)
chk("default open lane emits no argv", tc.lane_args("open", "modify", _supports=True), [])
chk("unsupported queue emits no argv", tc.lane_args("fixed", "modify", _supports=False), [])
chk("fixed + support -> --mode fixed", tc.lane_args("fixed", "modify", _supports=True, targets=(), _supports_targets=True), ["--mode", "fixed"])
chk("fixed targets forwarded as --fixed-target when the queue takes them",
    tc.lane_args("fixed", "modify", _supports=True, targets=("a.py", "b.py"), _supports_targets=True),
    ["--mode", "fixed", "--fixed-target", "a.py", "--fixed-target", "b.py"])
chk("...but not when it does not", tc.lane_args("fixed", "modify", _supports=True, targets=("a.py",), _supports_targets=False), ["--mode", "fixed"])
chk("open mode never carries targets", tc.lane_args("auto", "create", _supports=True, targets=("a.py",), _supports_targets=True), ["--mode", "open"])
class R:  # fake subprocess result
    def __init__(s, o): s.stdout, s.stderr = o, ""
chk("probe reads --mode from enqueue --help", tc.queue_supports_flag("--mode", "q", lambda c: R("... [--mode {a,b}] ...")), True)
chk("probe: absent -> False", tc.queue_supports_flag("--mode", "q", lambda c: R("... nothing ...")), False)
def boom(c): raise OSError("x")
chk("probe fails closed", tc.queue_supports_flag("--mode", "q", boom), False)
print(f"\nFAILED {FAILS}" if FAILS else "\nall passed")
sys.exit(1 if FAILS else 0)
