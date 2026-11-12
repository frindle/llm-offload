#!/usr/bin/env python3
"""Runner-level tests of mode "auto" (description -> diagnose -> derived two-phase spec -> model test -> model fix) with a stub
MINI_BIN: no network, no GPU, no queue.   python3 ~/bin/test_mini_auto.py"""
import importlib.machinery
import importlib.util
import json
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = Path(os.environ.get("MINI_RUNNER_UNDER_TEST") or HERE / "mini-swe-runner.py")  # a candidate copy can be tested before it goes live
_sp = importlib.util.spec_from_file_location("mini_swe_runner_auto", RUNNER,  # explicit loader: a candidate copy has no .py suffix
                                             loader=importlib.machinery.SourceFileLoader("mini_swe_runner_auto", str(RUNNER)))
R = importlib.util.module_from_spec(_sp)
_sp.loader.exec_module(R)

# The stub model: the stage comes from the prompt (phase A says PHASE 1 OF 2, the diagnose contract says cat > DIAGNOSIS.md, the
# rest is phase B). "@test" in plan A means the derived test file, read from the run dir's spec.auto.json.
STUB = r'''#!/usr/bin/env python3
import json, os, sys, time
a = sys.argv; task = a[a.index("-t") + 1]; traj = a[a.index("-o") + 1]
cwd = next(x.split("=", 1)[1] for x in a if x.startswith("environment.cwd="))
plan = json.load(open(os.environ["STUB_PLAN"]))
stage = "A" if "PHASE 1 OF 2" in task else ("D" if "cat > DIAGNOSIS.md" in task else "B")
open(plan["log"], "a").write(f"=== {stage}\n{task}\n")
v = plan.get(stage) or {}
if isinstance(v, list):  # one entry per attempt of this stage; the last one repeats
    cf = plan["log"] + "." + stage
    n = int(open(cf).read() or 0) if os.path.exists(cf) else 0
    open(cf, "w").write(str(n + 1))
    v = v[min(n, len(v) - 1)]
for rel, txt in v.items():
    if rel == "@test":
        rel = json.load(open(os.path.join(os.path.dirname(cwd), "spec.auto.json")))["test_file"]
    p = os.path.join(cwd, rel)
    os.makedirs(os.path.dirname(p) or cwd, exist_ok=True)
    open(p, "w").write(txt)
now = int(time.time())
json.dump({"messages": [{"role": "assistant", "content": "done",
                         "extra": {"response": {"created": now, "usage": {"prompt_tokens": 100, "completion_tokens": 10}}}}]}, open(traj, "w"))
print("echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
'''

PRICE = """'use strict';
function clamp(n, lo, hi) {
  return Math.min(hi, Math.max(lo, n));
}

function bulkTotal(qty, unit) {
  let total = qty * unit;
  if (qty > 10) {
    total = total * 0.95;
  }
  return Math.round(total * 100) / 100;
}

function label(qty) {
  return qty === 1 ? 'item' : 'items';
}

module.exports = { clamp, bulkTotal, label };
"""
CART = """'use strict';
const { bulkTotal, label } = require('./price');

function summary(qty, unit) {
  return `${qty} ${label(qty)}: ${bulkTotal(qty, unit)}`;
}

module.exports = { summary };
"""
TOKEN = """'use strict';
function refreshToken(t) {
  return t + 1;
}

module.exports = { refreshToken };
"""
OLD_TEST = """const test = require('node:test');
const assert = require('node:assert');
const { bulkTotal, label } = require('./price');
test('small order pays full price', () => { assert.strictEqual(bulkTotal(2, 3), 6); });
test('label', () => { assert.strictEqual(label(1), 'item'); });
"""
NEW_TEST = """const test = require('node:test');
const assert = require('node:assert');
const { bulkTotal } = require('./price');
test('ten items get the bulk discount', () => { assert.strictEqual(bulkTotal(10, 2), 19); });
test('ten items at 4 get the bulk discount', () => { assert.strictEqual(bulkTotal(10, 4), 38); });
"""
FIXED = PRICE.replace("if (qty > 10) {", "if (qty >= 10) {")
# two targets in one file (bench-dupes: one fault copied in a radarr and a sonarr twin)
TWO_TEST = NEW_TEST.replace("const { bulkTotal }", "const { bulkTotal, label }").replace(
    "test('ten items at 4 get the bulk discount', () => { assert.strictEqual(bulkTotal(10, 4), 38); });",
    "test('ten items are labelled bulk', () => { assert.strictEqual(label(10), 'items (bulk)'); });")
TWO_FIXED = FIXED.replace("return qty === 1 ? 'item' : 'items';", "return qty === 1 ? 'item' : (qty >= 10 ? 'items (bulk)' : 'items');")
DESC = ("Orders of exactly 10 items don't get the 5% bulk discount. bulkTotal(10, 2) returns 20, but the bulk rule is "
        "10 or more items, so it should return 19.\n")


def cite(rel, text, a, b):
    lines = text.splitlines()[a - 1:b]
    return f"```js {rel}:{a}-{b}\n" + "\n".join(lines) + "\n```\n"


GOOD_DIAG = "\n".join([
    "# Diagnosis", "", "## Code path", "", "The cart summary prices an order through bulkTotal.", "",
    cite("lib/cart.js", CART, 4, 6), cite("lib/price.js", PRICE, 6, 12), cite("lib/price.js", PRICE, 8, 10),
    "The comparison at lib/price.js:8 is strict, so a quantity of 10 skips the discount.", "",
    "## Root cause", "", "bulkTotal compares qty with a strict greater-than, so an order of exactly 10 items is priced without the 5% bulk discount.", "",
    "## Second cause", "", "None: summary in lib/cart.js only formats what bulkTotal returns.", "",
    "## Minimal fix", "", "- File: `lib/price.js`", "- Function: `bulkTotal`", "",
    "Change the comparison to qty >= 10 so the bulk rule starts at ten items.", "",
    "## Regression test", "", "Call bulkTotal(10, 2): it returns 20 now and must return 19; bulkTotal(10, 4) returns 40 now and must return 38.", "",
    "## Not verified", "", "Whether other callers rely on the old threshold; only lib/cart.js calls bulkTotal in this repo.", ""])
TOKEN_DIAG = "\n".join([
    "# Diagnosis", "", "## Code path", "", "The refresh goes through refreshToken.", "",
    cite("lib/token.js", TOKEN, 2, 4), cite("lib/token.js", TOKEN, 3, 3), cite("lib/cart.js", CART, 4, 6),
    "The value is computed at lib/token.js:3 and returned unchanged.", "",
    "## Root cause", "", "refreshToken adds one to the old value instead of issuing a new one, so the refreshed value is predictable.", "",
    "## Second cause", "", "None found in the callers.", "",
    "## Minimal fix", "", "- File: `lib/token.js`", "- Function: `refreshToken`", "", "Return a new value instead of the old one plus one.", "",
    "## Regression test", "", "Call refreshToken(1) twice and assert the two results differ from 2; today both are 2.", "",
    "## Not verified", "", "Whether any caller depends on the incrementing value; none in this repo.", ""])


PAGE = """<!doctype html>
<html><body>
<p id="total"></p>
<script>
function renderTotal(qty, unit) {
  let total = qty * unit;
  if (qty > 10) {
    total = total * 0.95;
  }
  document.getElementById('total').textContent = String(total);
}
renderTotal(1, 1);
</script>
</body></html>
"""
PAGE_DIAG = "\n".join([
    "# Diagnosis", "", "## Code path", "", "The page renders the total through renderTotal.", "",
    cite("web/page.html", PAGE, 5, 11).replace("```js", "```html"), cite("web/page.html", PAGE, 7, 9).replace("```js", "```html"),
    cite("web/page.html", PAGE, 12, 12).replace("```js", "```html"),
    "The comparison at web/page.html:7 is strict, so a quantity of 10 skips the discount.", "",
    "## Root cause", "", "renderTotal compares qty with a strict greater-than, so an order of exactly 10 items shows no 5% bulk discount.", "",
    "## Second cause", "", "None: the page only displays what renderTotal computes.", "",
    "## Minimal fix", "", "- File: `web/page.html`", "- Function: `renderTotal`", "",
    "Change the comparison to qty >= 10 so the bulk rule starts at ten items.", "",
    "## Regression test", "", "Run the page script with renderTotal(10, 2): the total shows 20 now and must show 19.", "",
    "## Not verified", "", "Whether other pages share the threshold; only this page defines renderTotal.", ""])

# a fix that lives in a module-level value (bench-cfgkeys 2cdee16a8b71: a missing DEFAULTS key made set_setting reject a setting)
RULES = """'use strict';
const KNOWN = { small: 1, medium: 5 };

function sizeOf(name) {
  if (!(name in KNOWN)) {
    throw new Error('unknown size ' + name);
  }
  return KNOWN[name];
}

module.exports = { KNOWN, sizeOf };
"""
RULES_FIXED = RULES.replace("{ small: 1, medium: 5 }", "{ small: 1, medium: 5, large: 10 }")
RULES_TEST = """const test = require('node:test');
const assert = require('node:assert');
const { sizeOf } = require('./rules');
const size = (n) => { try { return sizeOf(n); } catch (e) { return 'threw: ' + e.message; } };
test('large is a supported size', () => { assert.strictEqual(size('large'), 10); });
test('large is twice medium', () => { assert.strictEqual(size('large'), 2 * size('medium')); });
"""
RULES_DESC = "sizeOf('large') throws 'unknown size large', but large is a supported size worth 10, so sizeOf('large') should return 10.\n"
RULES_DIAG = "\n".join([
    "# Diagnosis", "", "## Code path", "", "Sizes are looked up through sizeOf, which checks the KNOWN table first.", "",
    cite("lib/rules.js", RULES, 4, 9), cite("lib/rules.js", RULES, 2, 2), cite("lib/rules.js", RULES, 5, 7),
    "The check at lib/rules.js:5 rejects any name missing from KNOWN, and KNOWN at lib/rules.js:2 has no large entry.", "",
    "## Root cause", "", "KNOWN has no large entry, so sizeOf throws unknown size for large instead of returning 10.", "",
    "## Second cause", "", "None: sizeOf only reads KNOWN, and nothing else in this repo lists the sizes.", "",
    "## Minimal fix", "", "- File: `lib/rules.js`", "- Function: `sizeOf`", "- Constant: `KNOWN`", "",
    "Add large: 10 to KNOWN; sizeOf itself needs no change.", "",
    "## Regression test", "", "Call sizeOf('large'): it throws unknown size large now and must return 10.", "",
    "## Not verified", "", "Whether another module keeps its own list of sizes; none does in this repo.", ""])


def mkrepo(root, name="shop"):
    repo = Path(root) / name
    (repo / "lib").mkdir(parents=True)
    (repo / "package.json").write_text(json.dumps({"name": "shop", "version": "1.0.0", "scripts": {"test": "node --test lib/"}}))
    for rel, txt in (("lib/price.js", PRICE), ("lib/cart.js", CART), ("lib/token.js", TOKEN), ("lib/price.test.js", OLD_TEST)):
        (repo / rel).write_text(txt)
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
    return repo


# a check of the structured path's own gates: auto() turns the direct fallback on for every job whose spec does not say false,
# and it runs after a phase A or phase B that ends the job
NOFB = {"direct_fallback": False}


def run_case(root, name, repo, plan, spec_extra=None, desc=DESC, check_only=False, reuse=False, env_extra=None):
    run = Path(root) / name
    if not reuse:
        (run / "work").mkdir(parents=True)
        (run / "TASK.md").write_text(desc)
    spec = {"mode": "auto", "baseline_from": str(repo), "task": str(run / "TASK.md"), "timeout": 1800, "diag_attempts": 1,
            "test_attempts": 1, "fix_attempts": 1, **(spec_extra or {})}
    (run / "spec.json").write_text(json.dumps(spec))
    (run / "plan.json").write_text(json.dumps({**plan, "log": str(run / "stub.log")}))
    local = Path(root) / "local.json"
    local.write_text(json.dumps({"base_url": "http://127.0.0.1:9/v1", "api_key": "k-SECRET"}))
    stub = Path(root) / "stub-mini"
    stub.write_text(STUB)
    stub.chmod(0o755)
    cmd = [sys.executable, str(RUNNER), "--cwd", str(run / "work"), "--task-file", str(run / "spec.json")] + (["--check-only"] if check_only else [])
    env = {**os.environ, "STUB_PLAN": str(run / "plan.json"), "MINI_BIN": str(stub), "DARKBLOOM_LOCAL_JSON": str(local), "MINI_GRAPH_LOOKUP": "off",
           **(env_extra or {})}
    env.pop("MINI_SPEC_LIB", None)
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=600, env=env)
    res = [ln for ln in r.stdout.splitlines() if ln.startswith("RESULT ")]
    log = (run / "stub.log").read_text() if (run / "stub.log").exists() else ""
    stages = [ln[4:] for ln in log.splitlines() if ln.startswith("=== ")]
    return r, res, run, stages, log


FAILS = []


def need(cond, msg, r=None):
    if not cond:
        FAILS.append(msg)
        print("FAIL " + msg)
        if r is not None:
            print(r.stdout[-3500:], r.stderr[-1200:])
        return False
    print("ok   " + msg)
    return True


def units():
    f = R.auto_forbidden
    for n in ("lib/author.js", "authorName", "tokenize", "Tokenizer", "taxonomy.py", "gateway.js", "src/price.js", "bloomctl", "plexrepo"):
        need(not f([("x", n)]), f"forbid: {n} allowed")
    for n in ("checkAuth", "refreshToken", "lib/token.js", "calcTax", "mini-swe-runner.py", "gate.py", "resell-tracker", "bfmrJoin",
              "salePrice", "keychain_get", "db/migrations/1.sql", "ollama-queue.py", "bloomgauge-control"):
        need(bool(f([("x", n)])), f"forbid: {n} refused")
    need(not f([], "the tokenizer splits tokens wrong") and bool(f([], "the API token is logged")) and bool(f([], "BFMR dedup is off")),
         "forbid: description words")
    files = ["lib/price.js", "src/lib/price.js", "lib/cart.js"]
    need(R.auto_resolve("lib/cart.js", files) == "lib/cart.js" and R.auto_resolve("`./lib/cart.js:4-6`", files) == "lib/cart.js",
         "resolve: exact path, quotes, ./ and :N-M stripped")
    need(R.auto_resolve("cart.js", files) == "lib/cart.js" and R.auto_resolve("price.js", files) is None, "resolve: unique suffix only")
    md = "## Root cause\n\nx\n\n```\n## not a title\n```\n## Minimal fix\n\n- File: `a.js`\n- Function: `Foo.bar()`\n## Regression test idea\n\ny\n"
    secs = R.md_h2(md)
    need("not a title" not in secs and "## not a title" in secs["root cause"], "md_h2: fenced ## is body text")
    need(R.auto_target(md) == ("a.js", ["bar"], []) and R.md_sec(secs, "regression test") == "y", "auto_target + md_sec prefix match")
    md2 = md.replace("- Function: `Foo.bar()`", "- Function: `bar` (and `Foo.baz`)\n- Function: `qux` (the loop in it)")
    need(R.auto_target(md2) == ("a.js", ["bar", "qux"], ["baz"]), f"auto_target: two Function lines + an (and ...) tail ({R.auto_target(md2)})")
    need(R._fn_list("a") == "`a`" and R._fn_list(["a", "b", "c"]) == "`a`, `b` and `c`", "_fn_list")
    mc = "## Minimal fix\n- File: `a.js`\n- Function: `bar`\n- Constant: `TABLE` and `cfg.OTHER`\n"
    need(R.auto_consts(mc) == ["TABLE", "OTHER"] and R.auto_consts(md) == [], f"auto_consts ({R.auto_consts(mc)})")
    need(R._binds_top("const TABLE = {};\n", "TABLE") and R._binds_top("export let TABLE: Rec = {}\n", "TABLE")
         and R._binds_top("TABLE: dict = {}\n", "TABLE") and R._binds_top("TABLE = {\n", "TABLE")
         and not R._binds_top("  TABLE = 1\n", "TABLE") and not R._binds_top("TABLE == 1\n", "TABLE")
         and not R._binds_top("TABLES = 1\n", "TABLE"), "_binds_top: top-level assignments only")
    docs_and_survivors()
    missing_probe()
    auto_upd_units()


def missing_probe():
    """said_missing: only a SUBMITTED final message counts. setform 2fed9f58b143: an import probe printed 'MISSING: bloom_ledger',
    the run then hit the step cap, and phase A stopped after one attempt as if the agent had reported MISSING."""
    d = Path(tempfile.mkdtemp())

    def traj(name, status):
        p = d / name
        p.write_text(json.dumps({"messages": [{"role": "user", "content": "x"}, {"role": "exit", "content": "", "extra": {"exit_status": status}}]}))
        return p
    prompt = "Submit with echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT; when a file is missing, a final message that begins MISSING: names it.\n"
    probe = prompt + "$ python3 probe.py\nOK: bloom_lan\nMISSING: bloom_ledger: ModuleNotFoundError\n"
    need(not R.said_missing(probe, traj("lim.json", "LimitsExceeded")), "said_missing: step cap + a tool output 'MISSING:' -> not MISSING")
    need(not R.said_missing(probe, traj("time.json", "TimeExceeded")), "said_missing: time cap -> not MISSING")
    need(not R.said_missing(probe, d / "absent.json"), "said_missing: no trajectory written (killed) -> not MISSING")
    sub = probe + "$ echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT; echo 'MISSING: settings_form_page'\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nMISSING: settings_form_page\n"
    need(R.said_missing(sub, traj("sub.json", "Submitted")), "said_missing: a submitted 'MISSING:' message -> MISSING")
    done = probe + "$ echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\nCOMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT\n"
    need(not R.said_missing(done, traj("ok.json", "Submitted")), "said_missing: submitted, probe output before the submit -> not MISSING")
    need(R.said_missing(probe), "said_missing: no trajectory given -> the log-only rule (unchanged)")


PYMOD = '''"""Drop single-episode Sonarr torrents once they have seeded.

Other labels are kept.
"""
import os


def classify(label):
    """Sonarr labels are episodes."""
    if not label.startswith('sonarr'):
        return 'other'
    return 'episode'


def one(): """same line"""; return 1


def other(x):
    return x
'''


def docs_and_survivors():
    """2026-11-03 bench-libseed escape: a prefix match where the Problem said other labels are unchanged, and a module docstring the fix
    left stale. Docstring lines are in scope for fn_scope / fix_hunks / reach; the packet names mutation survivors; prompts ask exact."""
    import types
    doc = R._py_doc_lines(PYMOD)
    need(doc == frozenset({1, 2, 3, 4, 9}), f"_py_doc_lines: module (multi-line) + function docstring, not a same-line one ({sorted(doc)})")
    need(R._py_doc_lines("def f(:\n") == frozenset() and R._py_doc_lines("x = 1\n") == frozenset(), "_py_doc_lines: bad parse / no docstring")
    need(R._py_doc_lines('def f():\n    """a"""; return 1\n') == frozenset(), "_py_doc_lines: a docstring sharing a line with code is code")
    new = PYMOD.replace("seeded.\n", "seeded, and library-seed torrents too.\n")
    a, b = PYMOD.splitlines(True), new.splitlines(True)
    need(R._code_hunks(a, b) != [] and R._code_hunks(a, b, R._py_doc_lines(PYMOD), R._py_doc_lines(new)) == [],
         "_code_hunks: a docstring-only hunk is not a code hunk once the doc lines are given")
    code = new.replace("return x\n", "return x + 1\n")
    need(len(R._code_hunks(a, code.splitlines(True), R._py_doc_lines(PYMOD), R._py_doc_lines(code))) == 1, "_code_hunks: the code hunk still counts")
    with tempfile.TemporaryDirectory() as d:
        Path(d, "m.py").write_text(new)
        reach = {"mapped": {"m.py": True}, "lines": {"m.py": set()}, "exec": {}}
        cov, ex, miss, unm = R.diff_reach(d, reach, ["m.py"], {"m.py": PYMOD})
        need(ex == 0 and miss == [], f"diff_reach: a changed docstring line is not an uncovered executable line ({ex}, {miss})")
        Path(d, "m.py").write_text(code)
        cov, ex, miss, unm = R.diff_reach(d, reach, ["m.py"], {"m.py": PYMOD})
        need(ex == 1 and len(miss) == 1, f"diff_reach: the code line still counts ({ex}, {miss})")
        # fn_scope (enforce): the module docstring may change with the target; code outside the target may not
        sp = {"target_fn": ["classify"], "fn_scope": "enforce"}
        fixed = new.replace("if not label.startswith('sonarr'):", "if label not in ('library-seed',) and not label.startswith('sonarr'):")
        Path(d, "m.py").write_text(fixed)
        ok, det = R.fn_scope_check(d, sp, ["m.py"], {"m.py": PYMOD})
        need(ok and "outside 0" in det, f"fn_scope: a module docstring edit is in scope ({det[:160]})")
        Path(d, "m.py").write_text(fixed.replace("return x\n", "return x + 1\n"))
        ok, det = R.fn_scope_check(d, sp, ["m.py"], {"m.py": PYMOD})
        need(not ok and "other" in det, f"fn_scope: code outside the target is still refused ({det[:160]})")
        # the mutation gate names its survivors; the auto packet shows them
        Path(d, "m.py").write_text(fixed)
        out = ("MUTATION killed=1 survived=2 timeout=0 total=3 score=0.333\n"
               "survived m.py:11 breadth: label.startswith('sonarr') -> ((label) == 'sonarr')\n"
               "survived m.py:11 cmp: a | b -> c\n")
        Path(d, "out.txt").write_text(out)
        spec = {"target_fn": "classify", "test_cmd": "true", "mutation_gate": {"cmd": f"cat {Path(d, 'out.txt')}", "format": "line"}}
        ok, det, score = R.run_mutation_gate(d, spec, ["m.py"], 120)
        need("; survivors: m.py:11 breadth: label.startswith('sonarr') -> ((label) == 'sonarr') ;; m.py:11 cmp: a / b -> c" in det
             and abs((score or 0) - 1 / 3) < 0.01, f"mutation gate detail names the survivors, no | ({det[-200:]})")
        many = "MUTATION killed=0 survived=10 timeout=0 total=10\n" + "".join(f"survived m.py:{i} neg: x -> y\n" for i in range(10))
        Path(d, "out.txt").write_text(many)
        ok, det, score = R.run_mutation_gate(d, spec, ["m.py"], 120)
        need(det.count(" ;; ") == R.MUT_SURV_CAP and det.endswith(" ;; (+2 more)"), f"survivors capped at MUT_SURV_CAP ({det[-80:]})")
        run = Path(d, "run")
        (run / "work").mkdir(parents=True)
        Path(run, "work", "m.py").write_text(fixed)
        Path(run, "work", "test_m.py").write_text("def test_x():\n    assert True\n")
        Path(run, "REVIEW.md").write_text("| gate | | detail |\n|---|---|---|\n| fn_scope | ok | x |\n| mutation | ok | custom on m.py:9-13: "
                                          "score=0.50; survivors: m.py:11 breadth: a -> b ;; m.py:12 neg: c -> d |\n| suite | ok | y |\n")
        need(R._auto_survivors(run) == ["m.py:11 breadth: a -> b", "m.py:12 neg: c -> d"], f"_auto_survivors ({R._auto_survivors(run)})")
        need(R._auto_survivors(Path(d, "nope")) == [], "_auto_survivors: no packet")
        fspec = {"test_file": "test_m.py", "allowed": ["m.py"]}
        R.auto_review(run, run / "work", "t", "Add library-seed. Other labels are unchanged.", "PASS mode=auto", ("m.py", ["classify"]),
                      fspec=fspec, base_text=PYMOD)
        ra = (run / "REVIEW-AUTO.md").read_text()
        need("## Mutation survivors" in ra and "- `m.py:11 breadth: a -> b`" in ra and "breadth" in ra.split("## Mutation survivors")[1],
             "REVIEW-AUTO.md: survivors section")
        need("near-misses" in ra and "prefix or substring match is a reject" in ra and "4. Does every docstring" in ra, "REVIEW-AUTO.md: asks 3 and 4")
        Path(run, "REVIEW.md").write_text("| mutation | ok | custom on m.py:9-13: score=1.00 (8/8 killed) |\n")
        R.auto_review(run, run / "work", "t", "x", "PASS mode=auto", ("m.py", ["classify"]), fspec=fspec, base_text=PYMOD)
        ra = (run / "REVIEW-AUTO.md").read_text()
        need("## Mutation survivors" not in ra and "## Breadth retry" not in ra, "REVIEW-AUTO.md: no survivors and no breadth row, no sections")
        need(R._auto_breadth(run) == "" and R._auto_breadth(Path(d, "nope")) == "", "_auto_breadth: no row / no packet")
        # the breadth retry's row (spec breadth_retry): the auto packet quotes it and says the model's claims are its own reading
        Path(run, "REVIEW.md").write_text("| mutation | ok | custom on m.py:9-13: score=1.00 (8/8 killed) |\n| breadth | ok | asked: m.py:11 "
                                          "label.startswith('sonarr'); fix changed (the diff is the result); model: m.py:11 narrowed: \"exact labels\" |\n"
                                          "| suite | ok | y |\n")
        br = R._auto_breadth(run)
        need(br.startswith("asked: m.py:11 label.startswith('sonarr'); fix changed") and br.endswith('narrowed: "exact labels"'), f"_auto_breadth ({br})")
        R.auto_review(run, run / "work", "t", "x", "PASS mode=auto", ("m.py", ["classify"]), fspec=fspec, base_text=PYMOD)
        ra = (run / "REVIEW-AUTO.md").read_text()
        need("## Breadth retry" in ra and f"- {br}" in ra and "still a reject" in ra, "REVIEW-AUTO.md: breadth retry section")
    # only mini_mutate's line output names breadth survivors: Python gets the retry, Stryker (JS) does not; breadth_retry: false opts out
    py = R.auto_mutation_cfg("/w", "m.py", {})
    need(py and py["format"] == "line" and R.auto_breadth_retry(py, {}) and not R.auto_breadth_retry(py, {"breadth_retry": False})
         and not R.auto_breadth_retry(R.auto_mutation_cfg("/w", "m.js", {}), {}) and not R.auto_breadth_retry(None, {}), "auto_breadth_retry")
    b = {"tests": [("classify", "test_m_auto.py", "python3 -m pytest -q test_m_auto.py")], "texts": {"m.py": PYMOD}}
    ms = types.SimpleNamespace(exports=lambda f, t: [], env_reads=lambda t: [])
    t = R.auto_fix_task("t", "Add library-seed. Other labels are unchanged.", {}, "m.py", ["classify"], b, ms)
    need("matched exactly" in t and "never as a prefix or substring" in t and "module docstring too" in t and "'my-x'" in t
         and "one guard feeds near-misses" in t, "TASK.fix.md: exact match, docstrings, near-miss guard")


# canvas r2 (2026-11-05): the fix the issue asked for broke an existing test that asserted the TypeError the issue removes
UPD_SRC = '''def label(x):
    if x is None:
        raise TypeError("x must not be None")
    return str(x)


def other(y):
    return y
'''
UPD_FIXED = UPD_SRC.replace('        raise TypeError("x must not be None")\n', "        return ''\n")
UPD_OLD_T = '''import pytest
from pkg.util import label


def test_none():
    with pytest.raises(TypeError):
        label(None)


def test_one():
    assert label(1) == '1'
'''
UPD_NEW_T = UPD_OLD_T.replace("    with pytest.raises(TypeError):\n        label(None)\n", "    assert label(None) == ''\n")
UPD_OTHER_T = "from pkg import util\n\nlabel_count = 0\n\n\ndef test_o():\n    assert util.other(1) == 1\n"  # imports it, never calls label
UPD_CALLS = "from pkg.util import label\n\n\ndef test_x():\n    assert label(1) == '1'\n"
UPD_DESC = "label(None) raises TypeError, but None means no label, so label(None) should return an empty string.\n"

# doc_pass (2026-11-06, canvas r3): the fix leaves label's docstring saying it raises TypeError for None
DOC_SRC = '''def label(x):
    """Return x as a label.

    :raises TypeError: when x is None
    """
    if x is None:
        raise TypeError("x must not be None")
    return str(x)


def other(y):
    return y
'''
DOC_FIXED = DOC_SRC.replace('        raise TypeError("x must not be None")\n', "        return ''\n")
DOC_OLD_T = "from pkg.util import label\n\n\ndef test_one():\n    assert label(1) == '1'\n"
DOC_A_TEST = '''# Skeleton generated by mini-swe-runner: fill it in (keep the imports), do not create another test file.
import os
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent / '..'))

def _load_pkg_util():
    import pkg.util
    return pkg.util

def _label_none():
    try:
        return _load_pkg_util().label(None)
    except TypeError as e:
        return repr(e)

def test_none_is_empty():
    assert _label_none() == ''

def test_none_then_text():
    assert _label_none() + 'x' == 'x'

TESTS = [test_none_is_empty, test_none_then_text]  # explicit order: list every test function (never globals()/dir())
GUARDS = []  # the tests in TESTS that check behaviour the fix must keep: they PASS now and after the fix; every other test FAILS now

if __name__ == "__main__":
    _order = TESTS[::-1] if os.environ.get("TEST_ORDER") == "reverse" else TESTS
    _failed = 0
    for _t in _order:
        try:
            _t()
            print(f"RESULT {_t.__name__} PASS", flush=True)
        except Exception as _e:
            _failed += 1
            print(f"RESULT {_t.__name__} FAIL {type(_e).__name__}: {_e}", flush=True)
    print(f"{len(_order)} tests, {_failed} failed")
    sys.exit(1 if _failed else 0)
'''
# MINI_SINGLE_BIN stub for the doc pass: logs each request, answers with the corrected docstring
DOC_SS_STUB = r'''import json, os, sys
req = json.load(sys.stdin)
open(os.environ["DOC_STUB_LOG"], "a").write(json.dumps(req) + "\n")
doc = "Your one job is to decide whether that docstring still tells the truth" in req["messages"][0]["content"]
print("@@SS@@ " + json.dumps({"content": "```text\nReturn x as a label.\n\n:returns: '' when x is None\n```", "finish_reason": "stop",
                              "usage": {"prompt_tokens": 1000, "completion_tokens": 20}, "secs": 1} if doc else {"error": "not a doc pass prompt"}))
'''
WHOLE = "python3 -m pytest -q -p no:cacheprovider"


def mkpyrepo(root, name="pyshop", src=UPD_SRC, test=UPD_OLD_T):
    """A python repo for the derive cases: the target, an existing test that asserts the behaviour the fix changes, one that only
    imports the module, and an untracked .venv/bin/python wrapper (mini-spec derives with the repo's .venv python when there is
    one; the queue PATH's python3 may have no pytest)."""
    repo = Path(root) / name
    for rel, txt in (("pkg/__init__.py", ""), ("pkg/util.py", src), ("tests/test_util.py", test),
                     ("tests/test_other.py", UPD_OTHER_T), (".gitignore", ".venv/\n")):
        (repo / rel).parent.mkdir(parents=True, exist_ok=True)
        (repo / rel).write_text(txt)
    for c in (["init", "-q"], ["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "base"]):
        subprocess.run(["git", *c], cwd=repo, check=True, capture_output=True)
    vb = repo / ".venv" / "bin"
    vb.mkdir(parents=True)
    (vb / "python").write_text(f'#!/bin/sh\nexec {shlex.quote(sys.executable)} "$@"\n')
    (vb / "python").chmod(0o755)
    return repo


def auto_upd_units():
    """auto's update_tests: which existing pytest files a fix may update (auto_update_tests), the narrowed suite (auto_narrow_suite),
    and what TASK.fix.md and REVIEW-AUTO.md say about them."""
    import types
    ms = R._mini_spec_lib()
    tree = {"pkg/__init__.py": "", "pkg/util.py": UPD_SRC, "tests/test_util.py": UPD_OLD_T,
            "tests/test_util_auto.py": UPD_CALLS,      # the derived test file (b["tests"]): never an update_tests file
            "pkg/tests/test_pkgside.py": UPD_CALLS,    # shares a directory name with the target
            "tests/test_misc.py": UPD_CALLS, "tests/test_other.py": UPD_OTHER_T,
            "tests/deep/x/test_far.py": "import pkg.util\n\n\ndef test_f():\n    assert pkg.util.label(3) == '3'\n",
            "tests/test_nouse.py": "def test_n():\n    assert 1\n",
            "tests/helper.py": UPD_CALLS,              # not a file pytest collects
            "tests/test_untracked.py": UPD_CALLS}      # on disk, not tracked
    with tempfile.TemporaryDirectory() as d:
        wt = Path(d, "wt")
        for rel, txt in tree.items():
            (wt / rel).parent.mkdir(parents=True, exist_ok=True)
            (wt / rel).write_text(txt)
        files, f = sorted(t for t in tree if t != "tests/test_untracked.py"), "pkg/util.py"
        dtext = ("## Root cause\n\n`label` raises at `pkg/util.py:3` (lines 2-3) instead of returning ''; `pkg/util.py:8` is in other.\n\n"
                 "## Second cause\n\nnone\n")
        dl = R.auto_diag_lines(dtext, f, ["label"], str(wt))
        need(dl == [2, 3] and R.auto_diag_lines(dtext, f, ["util.label"], str(wt)) == [2, 3],
             f"auto_diag_lines: the fault sections' lines inside the target's body, not another function's ({dl})")
        need(R.auto_diag_lines(dtext, "pkg/nope.py", ["label"], str(wt)) == [] and R.auto_diag_lines("", f, ["label"], str(wt)) == [],
             "auto_diag_lines: a missing file or an empty diagnosis gives no lines")
        rel = R._auto_related(ms, str(wt), files, f)
        need(sorted(rel) == ["pkg/tests/test_pkgside.py", "tests/deep/x/test_far.py", "tests/test_misc.py", "tests/test_other.py",
                             "tests/test_util.py", "tests/test_util_auto.py"] and rel["tests/test_util.py"] == UPD_OLD_T,
             f"_auto_related: the tracked pytest files that import the module, with their text ({sorted(rel)})")
        rows = [{"test": "a", "tier": "MEDIUM"}, {"test": "b", "tier": "WEAK"}, {"test": "c", "tier": "STRONG"}, {"tier": "STRONG"},
                {"test": "a", "tier": "STRONG"}]
        need(R._impact_tests({"impact": {"rows": rows}}) == ["c", "a"] and R._impact_tests({"impact": None}) == [] and R._impact_tests({}) == [],
             "_impact_tests: STRONG then MEDIUM, no WEAK, each once")
        b0 = {"suite": WHOLE, "tests": [("label", "tests/test_util_auto.py", "python3 -m pytest -q tests/test_util_auto.py")], "drop": [],
              "impact": {"rows": []}}
        upd = lambda b, fns=("label",), ff=f, related=rel: R.auto_update_tests(ms, str(wt), files, ff, list(fns), b, related)
        u = upd(b0)
        need(u == ["tests/test_util.py", "pkg/tests/test_pkgside.py", "tests/test_misc.py"],
             f"auto_update_tests: nearest first (module name, then a shared directory), the test file skipped, at most 3 ({u})")
        b2 = {**b0, "impact": {"rows": [{"test": "tests/deep/x/test_far.py", "tier": "MEDIUM"}, {"test": "tests/test_untracked.py", "tier": "STRONG"},
                                        {"test": "tests/test_nouse.py", "tier": "STRONG"}]}}
        u = upd(b2)
        need(u == ["tests/deep/x/test_far.py", "tests/test_util.py", "pkg/tests/test_pkgside.py"],
             f"auto_update_tests: impact tests first; an untracked one and one that never names the target are left out ({u})")
        u = upd({**b0, "tests": [("label", "tests/test_util.py", "x")], "drop": ["pkg/tests/test_pkgside.py"]})
        need(u == ["tests/test_util_auto.py", "tests/test_misc.py", "tests/deep/x/test_far.py"], f"auto_update_tests: test_file and drop skipped ({u})")
        u = upd({**b0, "suite": "python3 -m pytest -q tests/test_misc.py tests/test_other.py"})
        need(u == ["tests/test_misc.py"], f"auto_update_tests: a listed suite allows only the files it runs ({u})")
        need(upd({**b0, "suite": "python3 -m pytest -q tests/test_other.py"}) == [], "auto_update_tests: label_count is not the name label")
        need(upd(b0, fns=("other",)) == ["tests/test_other.py"], "auto_update_tests: any target name counts (fns + constants)")
        need(upd(b0, ff="lib/a.js") == [] and upd({**b0, "suite": None}) == [] and upd({**b0, "suite": ""}) == [],
             "auto_update_tests: [] for a non-python target or no suite")
        nar = lambda b, first=None, related=rel, ff=f: R.auto_narrow_suite(ms, str(wt), files, ff, b, first=first, related=related)
        need(nar({**b0, "suite": "python3 -m pytest -q tests/test_misc.py"}) is None and nar(b0, ff="lib/a.js") is None
             and nar(b0, related={}) is None, "auto_narrow_suite: None for a listed suite, a non-python target, nothing to list")
        n = nar(b2, first=["tests/test_misc.py", "tests/test_gone.py"])
        want = (f"{WHOLE} tests/test_misc.py tests/test_nouse.py tests/deep/x/test_far.py tests/test_util.py pkg/tests/test_pkgside.py "
                "tests/test_other.py")
        need(n == want, f"auto_narrow_suite: update_tests, then impact tests, then the importers nearest first; untracked and the "
             f"test file left out ({n})")
        need(nar(b2, first=["tests/test_misc.py"], related=None) == nar(b2, first=["tests/test_misc.py"]), "auto_narrow_suite: related found when not given")
        old = R.AUTO_NARROW_MAX
        try:
            R.AUTO_NARROW_MAX = 2
            n2, n3 = nar(b2, first=["tests/test_misc.py"]), nar(b0, first=["tests/test_misc.py"])
        finally:
            R.AUTO_NARROW_MAX = old
        need(n2 is None and n3 == f"{WHOLE} tests/test_misc.py tests/test_util.py",
             f"auto_narrow_suite: more must-run tests than the cap keeps the whole suite; otherwise cut at the cap ({n2}, {n3})")
        # TASK.fix.md: the one exception to "do not edit them", the file listed, the scope line
        bft = {"tests": [("label", "tests/test_util_auto.py", "python3 -m pytest -q tests/test_util_auto.py")], "texts": {f: UPD_SRC},
               "impact": {"rows": [{"test": "tests/test_misc.py", "tier": "STRONG", "via": "imports pkg.util"}]}}
        sns = types.SimpleNamespace(exports=lambda f, t: [], env_reads=lambda t: [])
        t = R.auto_fix_task("t", UPD_DESC, {}, f, ["label"], bft, sns, upd=["tests/test_util.py"])
        need("with one exception: when a test function in `tests/test_util.py` asserts exactly the behaviour the Problem changes" in t
             and "- `tests/test_util.py` (calls the target)" in t and "- `tests/test_misc.py` (imports pkg.util)" in t
             and "and an existing test function the Problem's change makes wrong, as above" in t and "do not edit them." not in t,
             "TASK.fix.md: update_tests is the one exception, listed with the impact tests, in the scope line")
        t = R.auto_fix_task("t", UPD_DESC, {}, f, ["label"], bft, sns)
        need("do not edit them." in t and "one exception" not in t and "(calls the target)" not in t and "as above" not in t,
             "TASK.fix.md: no update_tests -> existing tests are not edited")
        t = R.auto_fix_task("t", UPD_DESC, {}, f, ["label"], bft, sns, upd=["tests/test_misc.py"])
        need(t.count("`tests/test_misc.py`") == 2 and "(calls the target)" not in t, "TASK.fix.md: an update_tests file that is an impact test is listed once")
        t = R.auto_fix_task("t", UPD_DESC, {}, f, ["label"], {**bft, "impact": None}, sns)
        need("## Existing tests near this code" not in t, "TASK.fix.md: no impact tests, no update_tests -> no section")
        # REVIEW-AUTO.md: the edit as a diff against the baseline, and ask 5, only when an update_tests file changed
        run = Path(d, "run")
        cwd = run / "work"
        for p, txt in ((f, UPD_FIXED), ("tests/test_util.py", UPD_NEW_T), ("tests/test_util_auto.py", UPD_CALLS)):
            (cwd / p).parent.mkdir(parents=True, exist_ok=True)
            (cwd / p).write_text(txt)
        fspec = {"test_file": "tests/test_util_auto.py", "allowed": [f], "update_tests": ["tests/test_util.py"], "baseline_from": str(wt)}
        R.auto_review(run, cwd, "t", UPD_DESC, "PASS mode=auto", (f, ["label"]), fspec=fspec, base_text=UPD_SRC)
        ra = (run / "REVIEW-AUTO.md").read_text()
        need("## Existing tests the fix updated" in ra and "+++ b/tests/test_util.py" in ra and "-    with pytest.raises(TypeError):" in ra
             and "+    assert label(None) == ''" in ra and "5. If the fix updated an existing test" in ra, "REVIEW-AUTO.md: the test edit as a diff, ask 5")
        (cwd / "tests/test_util.py").write_text(UPD_OLD_T)
        R.auto_review(run, cwd, "t", UPD_DESC, "PASS mode=auto", (f, ["label"]), fspec=fspec, base_text=UPD_SRC)
        ra = (run / "REVIEW-AUTO.md").read_text()
        need("## Existing tests the fix updated" not in ra and "5. If the fix" not in ra and "4. Does every docstring" in ra,
             "REVIEW-AUTO.md: an update_tests file left as it was adds no section and no ask 5")
        need("## Docstring check" not in ra, "REVIEW-AUTO.md: no doc pass row, no docstring section")
        row = "`label` (pkg/util.py): model corrected it (1 line(s), in the diff); the test re-run is green (2 tests)"
        (run / "REVIEW.md").write_text(f"## Phase B gates\n\n| gate | | detail |\n|---|---|---|\n| test_green | ok | x |\n| docstring | ok | {row} |\n")
        R.auto_review(run, cwd, "t", UPD_DESC, "PASS mode=auto", (f, ["label"]), fspec=fspec, base_text=UPD_SRC)
        ra = (run / "REVIEW-AUTO.md").read_text()
        need(f"## Docstring check (one local question per docstring the fix left as it was, before this review)\n\n- {row}\n" in ra
             and "check it is true and says no more than the code does" in ra and ra.index("## Docstring check") < ra.index("## Review asks"),
             "REVIEW-AUTO.md: the doc pass row from phase B's packet, before the asks")
        (run / "REVIEW.md").unlink()


def doc_pass_case(root):
    """doc_pass end to end (python target, stub models): the green fix left label's docstring saying it raises TypeError; the
    doc pass asks once (MINI_SINGLE_BIN stub), applies the correction, re-runs the test, and REVIEW.md / REVIEW-AUTO.md say so.
    doc_pass: false asks nothing."""
    repo = mkpyrepo(root, "pydoc", src=DOC_SRC, test=DOC_OLD_T)
    stub = Path(root) / "doc-ss-stub.py"
    stub.write_text(DOC_SS_STUB)
    plan = {"A": {"@test": DOC_A_TEST}, "B": {"pkg/util.py": DOC_FIXED}}
    hint = {"files": ["pkg/util.py"], "fn": "label", "diagnose": "skip", "auto_mutation": False}
    for name, extra in (("doc-pass", {}), ("doc-pass-off", {"doc_pass": False})):
        slog = Path(root) / f"{name}.ss.log"
        r, res, run, st, _ = run_case(root, name, repo, plan, {**hint, **extra}, desc=UPD_DESC,
                                      env_extra={"MINI_SINGLE_BIN": f"{shlex.quote(sys.executable)} {shlex.quote(str(stub))}", "DOC_STUB_LOG": str(slog)})
        reqs = [json.loads(x) for x in slog.read_text().splitlines()] if slog.is_file() else []
        rv = (run / "REVIEW.md").read_text() if (run / "REVIEW.md").is_file() else ""
        ra = (run / "REVIEW-AUTO.md").read_text() if (run / "REVIEW-AUTO.md").is_file() else ""
        src = (run / "work/pkg/util.py").read_text() if (run / "work/pkg/util.py").is_file() else ""
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=auto") and st == ["A", "B"], f"{name}: PASS mode=auto ({res}, {st})", r)
        if extra:
            need(not reqs and "| docstring |" not in rv and "## Docstring check" not in ra and ":raises TypeError: when x is None" in src,
                 f"{name}: no doc pass call, no row, the docstring as it was ({len(reqs)} calls)", r)
            continue
        need(len(reqs) == 1 and "## The problem the fix solves\n\nlabel(None) raises TypeError" in reqs[0]["messages"][0]["content"]
             and "k-SECRET" not in slog.read_text(), f"{name}: one doc pass call with the Problem, no key ({len(reqs)} calls)", r)
        need("| docstring | ok | `label` (pkg/util.py): model corrected it (1 line(s), in the diff); the test re-run is green (2 tests) |" in rv,
             f"{name}: REVIEW.md docstring row", r)
        head = DOC_SRC.split("    if x is None")[0].replace(":raises TypeError: when x is None", ":returns: '' when x is None")
        need(src.startswith(head) and "raise" not in src, f"{name}: the work file has the corrected docstring, laid out as before", r)
        need("## Docstring check" in ra and "+    :returns: '' when x is None" in ra, f"{name}: REVIEW-AUTO.md has the check and the corrected line in the fix diff", r)
        us = [x for x in r.stdout.splitlines() if "[worker] USAGE SUMMARY" in x]
        calls = [int(x.rsplit("calls=", 1)[1]) for x in us]
        need(len(us) >= 2 and calls[-1] == calls[-2] + 1 and "prompt_tokens=" in us[-1], f"{name}: a second usage summary counts the doc pass call ({calls})", r)


def main():
    units()
    with tempfile.TemporaryDirectory() as root:
        repo = mkrepo(root)
        plan = {"D": {"DIAGNOSIS.md": GOOD_DIAG}, "A": {"@test": NEW_TEST}, "B": {"lib/price.js": FIXED}}

        # 1. the whole lane: one diagnose run, one test run, one fix run -> PASS mode=auto, compact review packet
        r, res, run, st, log = run_case(root, "pass", repo, plan, {"files": ["price.js"]})
        need(r.returncode == 0 and len(res) == 1 and res[0].startswith("RESULT PASS mode=auto") and "target=lib/price.js:bulkTotal" in res[0],
             f"description -> PASS mode=auto ({res})", r)
        need(st == ["D", "A", "B"], f"stages D, A, B once each ({st})")
        d_prompt = log.split("=== A")[0]
        need("Reporter's hints" in d_prompt and "lib/price.js" in d_prompt and "bulk discount" in d_prompt, "diagnose prompt: problem + resolved hint")
        fs = json.loads((run / "spec.auto.json").read_text())
        need(fs["mode"] == "two-phase" and fs["allowed"] == ["lib/price.js"] and fs["target_fn"] == "bulkTotal" and fs["fn_scope"] == "enforce"
             and fs["test_file"].startswith("lib/") and fs["test_file"].endswith(".test.js") and fs["test_file"] != "lib/price.test.js"
             and "price.test.js" in (fs.get("suite_cmd") or ""), f"derived spec ({ {k: fs.get(k) for k in ('test_file', 'test_cmd', 'suite_cmd')} })")
        need((fs.get("mutation_gate") or {}).get("tool") == "stryker" and "fail_below" not in fs["mutation_gate"] and "breadth_retry" not in fs,
             "JS: advisory stryker mutation gate, no breadth retry")
        tfix = (run / "TASK.fix.md").read_text()
        need("TODO" not in tfix and "## Problem" in tfix and "### Root cause" in tfix and "`bulkTotal`" in tfix and "Every case must fail at baseline" in tfix
             and "`function bulkTotal(`" in tfix and "`clamp`" in tfix, "TASK.fix.md: problem, diagnosis, must keep, other fns, no TODO markers")
        ra = (run / "REVIEW-AUTO.md").read_text() if (run / "REVIEW-AUTO.md").exists() else ""
        need("RESULT PASS mode=auto" in ra and "ten items get the bulk discount" in ra and "+  if (qty >= 10) {" in ra and "Review asks" in ra,
             "REVIEW-AUTO.md: result, test text, fix diff, asks")
        need((run / "REVIEW-D.md").is_file() and (run / "REVIEW.md").is_file() and (run / "DIAGNOSIS.md").is_file(), "stage packets kept")
        need("k-SECRET" not in r.stdout + r.stderr + ra, "key never echoed")
        need((repo / "lib/price.js").read_text() == PRICE, "the baseline repo is untouched")

        # 2. a re-launch of the same run (the queue's PAUSED path) reuses the accepted diagnosis
        r, res, run, st, log = run_case(root, "pass", repo, plan, {"files": ["price.js"]}, reuse=True)
        need(res and res[0].startswith("RESULT PASS mode=auto") and st == ["D", "A", "B", "A", "B"] and "reusing the diagnosis" in r.stdout,
             f"re-launch skips the diagnose stage ({st})", r)

        # 3. diagnose=skip with one file + fn hint: no diagnose run
        r, res, run, st, _ = run_case(root, "skip", repo, plan, {"files": ["lib/price.js"], "fn": "bulkTotal", "diagnose": "skip"})
        need(res and res[0].startswith("RESULT PASS mode=auto") and st == ["A", "B"], f"diagnose=skip -> A, B only ({res}, {st})", r)
        r, res, run, st, _ = run_case(root, "skip-bad", repo, plan, {"diagnose": "skip"})
        need(r.returncode == 1 and res and "FAIL bad-spec mode=auto diagnose=skip needs" in res[0] and "class=SPEC" in res[0] and st == [],
             f"diagnose=skip without hints -> bad-spec ({res})", r)

        # 4. refusals before any model call: description words, repo name, hint names
        r, res, run, st, _ = run_case(root, "ref-desc", repo, plan, desc="The login page keeps the password field filled after logout.\n")
        need(res and "phase=refused must-not-offload" in res[0] and "class=SPEC" in res[0] and st == [], f"description refused ({res})", r)
        rrepo = mkrepo(root, "resell-mini")
        r, res, run, st, _ = run_case(root, "ref-repo", rrepo, plan)
        need(res and "phase=refused" in res[0] and "resell" in res[0] and st == [], f"repo name refused ({res})", r)
        r, res, run, st, _ = run_case(root, "ref-hint", repo, plan, {"files": ["lib/token.js"]})
        need(res and "phase=refused" in res[0] and "token" in res[0] and st == [], f"file hint refused ({res})", r)

        # 5. a diagnosis that lands in a must-not-offload function is refused after the diagnose stage (no test, no fix run)
        r, res, run, st, _ = run_case(root, "ref-diag", repo, {**plan, "D": {"DIAGNOSIS.md": TOKEN_DIAG}})
        need(res and "phase=refused" in res[0] and "refreshToken" in res[0] and st == ["D"], f"diagnosed target refused ({res}, {st})", r)

        # 6. no acceptable diagnosis -> FAIL phase=diagnose, nothing else runs
        r, res, run, st, _ = run_case(root, "diag-fail", repo, {**plan, "D": {"DIAGNOSIS.md": "Let me think.\n"}})
        need(r.returncode == 1 and res and res[0].startswith("RESULT FAIL mode=auto phase=diagnose") and "class=" in res[0] and st == ["D"]
             and (run / "REVIEW-AUTO.md").is_file(), f"bad diagnosis -> FAIL phase=diagnose ({res}, {st})", r)

        # 7. the model's fix is wrong -> FAIL phase=fix with the target, packet still written. The checks that end at a phase A
        # or phase B gate (7, 7b two-fn-reach, 7d guards-red, 7f ground-off) run with the fallback off (NOFB); 7g turns it on.
        r, res, run, st, _ = run_case(root, "fix-fail", repo, {**plan, "B": {"lib/price.js": PRICE.replace("0.95", "0.9")}}, NOFB)
        need(r.returncode == 1 and res and res[0].startswith("RESULT FAIL mode=auto") and "target=lib/price.js:bulkTotal" in res[0]
             and st == ["D", "A", "B"], f"wrong fix -> FAIL mode=auto ({res})", r)

        # 7b. a diagnosis that names two functions of one file: both are targets, the test must reach both, the fix may change both
        two = {"D": {"DIAGNOSIS.md": GOOD_DIAG.replace("- Function: `bulkTotal`", "- Function: `bulkTotal`\n- Function: `label`")},
               "A": {"@test": TWO_TEST}, "B": {"lib/price.js": TWO_FIXED}}
        r, res, run, st, _ = run_case(root, "two-fn", repo, two, {"files": ["price.js"]})
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=auto") and "target=lib/price.js:bulkTotal,label" in res[0]
             and st == ["D", "A", "B"], f"two Function lines -> PASS, target names both ({res}, {st})", r)
        fs = json.loads((run / "spec.auto.json").read_text()) if (run / "spec.auto.json").is_file() else {}
        need(fs.get("target_fn") == ["bulkTotal", "label"] and fs.get("fn_scope") == "enforce", f"spec.auto.json: target_fn is the list ({fs.get('target_fn')})")
        tfix = (run / "TASK.fix.md").read_text() if (run / "TASK.fix.md").is_file() else ""
        need("`bulkTotal` and `label`" in tfix and "fix every one of them" in tfix and "`clamp`" in tfix, "TASK.fix.md: both targets, fix every one")
        ra = (run / "REVIEW-AUTO.md").read_text() if (run / "REVIEW-AUTO.md").exists() else ""
        need("`bulkTotal` and `label`" in ra, "REVIEW-AUTO.md names both targets")
        r, res, run, st, _ = run_case(root, "two-fn-reach", repo, {**two, "A": {"@test": NEW_TEST}}, NOFB)
        need(r.returncode == 1 and res and "FAIL" in res[0] and "never reaches `label`" in r.stdout and st == ["D", "A"],
             f"a test that never calls the 2nd target fails phase A ({res}, {st})", r)
        r, res, run, st, _ = run_case(root, "two-fn-scope", repo, {**two, "B": {"lib/price.js": TWO_FIXED.replace("Math.max(lo, n)", "Math.max(lo, n | 0)")}})
        need(res and "needs-review" in res[0] and "fn_scope=FAIL" in res[0] and "`clamp` lines 3" in r.stdout and st == ["D", "A", "B"],
             f"an edit outside both targets is flagged by fn_scope (soft: needs-review) ({res}, {st})", r)

        # 7c. an accepted diagnosis whose target derive cannot take (a function in an HTML page's inline script): FAIL phase=derive,
        # and the packet carries the diagnosis's fix and test so Claude can fix it directly from there
        prepo = mkrepo(root, "page")
        (prepo / "web").mkdir()
        (prepo / "web" / "page.html").write_text(PAGE)
        for c in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "page"]):
            subprocess.run(["git", *c], cwd=prepo, check=True, capture_output=True)
        r, res, run, st, _ = run_case(root, "derive-html", prepo, {**plan, "D": {"DIAGNOSIS.md": PAGE_DIAG}})
        ra = (run / "REVIEW-AUTO.md").read_text() if (run / "REVIEW-AUTO.md").exists() else ""
        need(r.returncode == 1 and res and res[0].startswith("RESULT FAIL mode=auto phase=derive") and "must all be python or all JS/TS" in res[0]
             and "class=SPEC" in res[0] and st == ["D"], f"HTML inline-script target -> FAIL phase=derive class=SPEC after the diagnosis ({res}, {st})", r)
        need("## Minimal fix (model's diagnosis)" in ra and "qty >= 10" in ra and "## Regression test (model's diagnosis)" in ra
             and "renderTotal(10, 2)" in ra and "## Next" in ra and "Fix it directly" in ra and "## Review asks" not in ra,
             "derive-failure packet: the diagnosis's fix and test, and the fix-it-directly step instead of review asks")
        r, res, run, st, _ = run_case(root, "diag-fail-packet", repo, {**plan, "D": {"DIAGNOSIS.md": "Let me think.\n"}})
        ra = (run / "REVIEW-AUTO.md").read_text() if (run / "REVIEW-AUTO.md").exists() else ""
        need("## Next" not in ra and "(model's diagnosis)" not in ra, "a rejected diagnosis adds no diagnosis sections to the packet")

        # 7d. GUARDS: a test of behaviour the fix keeps is declared in GUARDS and must pass now and after the fix. A guard whose
        # expected value is wrong (it fails on the current code) goes back to the model before any fix; the corrected one passes.
        g_bad = NEW_TEST + ("test('eleven items keep the discount', () => { assert.strictEqual(bulkTotal(11, 2), 22); });\n"
                            "const GUARDS = ['eleven items keep the discount'];\n")
        g_ok = g_bad.replace("bulkTotal(11, 2), 22)", "bulkTotal(11, 2), 20.9)")
        r, res, run, st, log = run_case(root, "guards", repo, {**plan, "A": [{"@test": g_bad}, {"@test": g_ok}]},
                                        {"files": ["price.js"], "test_attempts": 2})
        a2 = log.split("=== A")[2] if log.count("=== A") >= 2 else ""
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=auto") and st == ["D", "A", "A", "B"],
             f"a guard declared in GUARDS -> PASS mode=auto after one send-back ({res}, {st})", r)
        need("eleven items keep the discount is in GUARDS" in a2 and "FAILS on the current code" in a2 and "PASSES at baseline" not in a2,
             "the 2nd phase A prompt names the guard that fails on the current code (not 'passes at baseline')")
        ra = (run / "REVIEW-AUTO.md").read_text() if (run / "REVIEW-AUTO.md").exists() else ""
        need("const GUARDS = ['eleven items keep the discount']" in ra, "REVIEW-AUTO.md shows the GUARDS line with the test")
        r, res, run, st, log = run_case(root, "guards-red", repo, {**plan, "A": {"@test": g_ok.replace("const GUARDS = ['eleven items keep the discount'];\n",
                                                                                                    "const GUARDS = ['ten items get the bulk discount', 'eleven items keep the discount'];\n")}},
                                        {"files": ["price.js"], **NOFB})
        need(r.returncode == 1 and res and "FAIL" in res[0] and "only 1 test(s) outside GUARDS" in r.stdout and st == ["D", "A"],
             f"a bug test moved into GUARDS still fails phase A: guards do not count as reds ({res}, {st})", r)

        # 7e. a fix in a module-level value: the diagnosis names it on a Constant: line; derive puts it in the spec's new_names so
        # fn_scope counts its statement, and TASK.fix.md points the fix at it. Without the line the same fix is flagged by fn_scope.
        rrepo2 = mkrepo(root, "sizes")
        (rrepo2 / "lib" / "rules.js").write_text(RULES)
        for c in (["add", "-A"], ["-c", "user.email=t@t", "-c", "user.name=t", "commit", "-qm", "rules"]):
            subprocess.run(["git", *c], cwd=rrepo2, check=True, capture_output=True)
        cplan = {"D": {"DIAGNOSIS.md": RULES_DIAG}, "A": {"@test": RULES_TEST}, "B": {"lib/rules.js": RULES_FIXED}}
        r, res, run, st, _ = run_case(root, "const", rrepo2, cplan, desc=RULES_DESC)
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=auto") and "target=lib/rules.js:sizeOf+KNOWN" in res[0]
             and st == ["D", "A", "B"], f"Constant: line -> PASS, the target names the value ({res}, {st})", r)
        fs = json.loads((run / "spec.auto.json").read_text()) if (run / "spec.auto.json").is_file() else {}
        need(fs.get("new_names") == ["KNOWN"] and fs.get("target_fn") == "sizeOf", f"spec.auto.json: new_names ({fs.get('new_names')}, {fs.get('target_fn')})")
        tfix = (run / "TASK.fix.md").read_text() if (run / "TASK.fix.md").is_file() else ""
        need("Module-level value: `KNOWN` (line 2)" in tfix and "change `KNOWN`" in tfix and "which `sizeOf` reads" in tfix,
             "TASK.fix.md points the fix at the value and its reader")
        need("constants KNOWN" in r.stdout, "the derive line names the value")
        r, res, run, st, _ = run_case(root, "const-noline", rrepo2, {**cplan, "D": {"DIAGNOSIS.md": RULES_DIAG.replace("- Constant: `KNOWN`\n", "")}},
                                      desc=RULES_DESC)
        need(res and "needs-review" in res[0] and "fn_scope=FAIL" in res[0] and st == ["D", "A", "B"],
             f"the same fix without the Constant: line is flagged by fn_scope (soft: needs-review) ({res}, {st})", r)
        r, res, run, st, _ = run_case(root, "const-fn-only", rrepo2, {**cplan, "D": {"DIAGNOSIS.md": RULES_DIAG.replace(
            "- Function: `sizeOf`\n- Constant: `KNOWN`", "- Function: `KNOWN`")}}, desc=RULES_DESC)
        need(r.returncode == 1 and res and "phase=diagnose" in res[0] and "module-level value" in r.stdout and st == ["D"],
             f"a Function: line naming the value is rejected by the diagnose gate with the Constant hint ({res}, {st})", r)

        # 7f. grounding (bench-notset): an expected value found in neither the Problem nor the code nor the test's own inputs goes
        # back to the model before any fix; the grounded test passes. On phase A's last attempt it is only noted for the review.
        invented = NEW_TEST.replace("const { bulkTotal }", "const { bulkTotal, label }") + (
            "test('ten items are labelled bulk', () => { assert.strictEqual(label(10), 'bulk_tier'); });\n")
        r, res, run, st, log = run_case(root, "ground", repo, {**plan, "A": [{"@test": invented}, {"@test": NEW_TEST}]},
                                        {"files": ["price.js"], "test_attempts": 2})
        a2 = log.split("=== A")[2] if log.count("=== A") >= 2 else ""
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS mode=auto") and st == ["D", "A", "A", "B"],
             f"an invented expected value -> one send-back, then PASS ({res}, {st})", r)
        need("found in neither TASK.md's Problem" in a2 and "`bulk_tier`" in a2, "the 2nd phase A prompt names the invented value")
        two_g = {"D": two["D"], "A": {"@test": invented.replace("test('ten items at 4 get the bulk discount', () => { assert.strictEqual(bulkTotal(10, 4), 38); });\n", "")},
                 "B": {"lib/price.js": FIXED.replace("return qty === 1 ? 'item' : 'items';", "return qty === 1 ? 'item' : (qty >= 10 ? 'bulk_tier' : 'items');")}}
        r, res, run, st, log = run_case(root, "ground-soft", repo, two_g, {"files": ["price.js"], "test_attempts": 1})
        ra = (run / "REVIEW-AUTO.md").read_text() if (run / "REVIEW-AUTO.md").exists() else ""
        rv = (run / "REVIEW.md").read_text() if (run / "REVIEW.md").exists() else ""
        need(res and "mode=auto" in res[0] and st == ["D", "A", "B"] and "found in neither TASK.md's Problem" not in log,
             f"on the last phase A attempt the check does not reject ({res}, {st})", r)
        need("## Expected values found in neither the Problem nor the baseline code" in ra and "`bulk_tier`" in ra,
             "REVIEW-AUTO.md lists the invented value for the review")
        need("## Test expectations found in neither TASK.md nor the code" in rv and "`bulk_tier`" in rv, "REVIEW.md notes it too")
        r, res, run, st, log = run_case(root, "ground-off", repo, {**plan, "A": [{"@test": invented}, {"@test": NEW_TEST}]},
                                        {"files": ["price.js"], "test_attempts": 2, "ground": "off", **NOFB})
        need(st == ["D", "A", "B"] and "found in neither TASK.md's Problem" not in log, f"spec ground=off skips the check ({res}, {st})", r)

        # 7g. the direct fallback (on by default) after two of those endings. It writes its own fix (the stub's B) and passes only
        # its own gates: the same wrong fix stays a FAIL with the fallback's verdict on the line; after phase A's reject, its
        # passing fix is a needs-review pass (it skips phase A's test gate) and the packet opens with the fallback section.
        r, res, run, st, _ = run_case(root, "fix-fail-fb", repo, {**plan, "B": {"lib/price.js": PRICE.replace("0.95", "0.9")}})
        need(r.returncode == 1 and res and res[0].startswith("RESULT FAIL mode=auto phase=fix") and " fallback=direct FAIL (test_green) " in res[0]
             and "target=lib/price.js:bulkTotal" in res[0] and st == ["D", "A", "B", "B"],
             f"wrong fix, fallback on -> still FAIL, with the fallback's FAIL on the line ({res}, {st})", r)
        r, res, run, st, _ = run_case(root, "two-fn-reach-fb", repo, {**two, "A": {"@test": NEW_TEST}})
        need(r.returncode == 0 and res and res[0].startswith("RESULT PASS needs-review soft=fallback mode=auto ") and " fallback=direct after=test " in res[0]
             and "target=lib/price.js:bulkTotal,label" in res[0] and "never reaches `label`" in r.stdout and st == ["D", "A", "B"],
             f"phase A's reject, fallback on -> the fallback's fix is a needs-review pass ({res}, {st})", r)
        rv = (run / "REVIEW.md").read_text() if (run / "REVIEW.md").exists() else ""
        need(rv.startswith("# REVIEW packet: PASS needs-review soft=fallback ") and "## Direct-fix fallback (read first)" in rv,
             "the fallback pass's REVIEW.md says so first")

        # 8. check-only: no model; with one file + fn hint it also derives and checks the two-phase spec
        r, res, run, st, _ = run_case(root, "co", repo, plan, check_only=True)
        need(r.returncode == 0 and st == [] and "diagnose prompt follows" in r.stdout, f"check-only, no hints: diagnose prompt only (rc={r.returncode})", r)
        r, res, run, st, _ = run_case(root, "co-hint", repo, plan, {"files": ["lib/price.js"], "fn": "bulkTotal"}, check_only=True)
        need(r.returncode == 0 and st == [] and "phase A prompt follows" in r.stdout and (run / "spec.auto.json").is_file(),
             f"check-only with hints derives the spec (rc={r.returncode})", r)
        r, res, run, st, _ = run_case(root, "co-ref", repo, plan, desc="Rotate the api key every day.\n", check_only=True)
        need(r.returncode == 1 and res and "phase=refused" in res[0], f"check-only refuses too ({res})", r)

        # 9. a python target that an existing test calls: the derived spec lets the fix update that test (update_tests_strict: gate
        # update_flip checks each edit); a test that only imports the module is not one. auto_update_tests=false turns it off.
        if subprocess.run([sys.executable, "-c", "import pytest"], capture_output=True).returncode != 0:
            print("skip python derive cases: pytest is not importable with " + sys.executable)
        else:
            pyrepo = mkpyrepo(root)
            hint = {"files": ["pkg/util.py"], "fn": "label"}
            r, res, run, st, _ = run_case(root, "co-py", pyrepo, {}, hint, desc=UPD_DESC, check_only=True)
            fs = json.loads((run / "spec.auto.json").read_text()) if (run / "spec.auto.json").is_file() else {}
            need(r.returncode == 0 and fs.get("update_tests") == ["tests/test_util.py"] and fs.get("update_tests_strict") is True
                 and fs.get("test_file") not in (None, "tests/test_util.py", "tests/test_other.py") and "suite_narrow" not in fs
                 and "the fix may update existing tests in tests/test_util.py (gate update_flip checks each edit)" in r.stdout,
                 f"python derive: update_tests = the existing test that calls the target "
                 f"({ {k: fs.get(k) for k in ('update_tests', 'test_file', 'suite_cmd')} })", r)
            need(fs.get("doc_pass") is True, f"python derive: doc_pass on ({fs.get('doc_pass')})")
            tfix = (run / "TASK.fix.md").read_text() if (run / "TASK.fix.md").is_file() else ""
            need("with one exception: when a test function in `tests/test_util.py`" in tfix, "python derive: TASK.fix.md names the exception")
            r, res, run, st, _ = run_case(root, "co-py-off", pyrepo, {}, {**hint, "auto_update_tests": False}, desc=UPD_DESC, check_only=True)
            fs = json.loads((run / "spec.auto.json").read_text()) if (run / "spec.auto.json").is_file() else {}
            need(r.returncode == 0 and fs and "update_tests" not in fs and "may update existing tests" not in r.stdout,
                 "auto_update_tests=false: no update_tests in the derived spec", r)
            r, res, run, st, _ = run_case(root, "co-py-nodoc", pyrepo, {}, {**hint, "doc_pass": False}, desc=UPD_DESC, check_only=True)
            fs = json.loads((run / "spec.auto.json").read_text()) if (run / "spec.auto.json").is_file() else {}
            need(r.returncode == 0 and fs and "doc_pass" not in fs, "doc_pass=false: no doc_pass in the derived spec", r)
            doc_pass_case(root)
    print("ALL PASS" if not FAILS else f"{len(FAILS)} FAILED: {FAILS}")
    return 1 if FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
