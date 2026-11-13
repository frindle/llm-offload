#!/usr/bin/env python3
"""gate_finding_check.py -- make a gate reviewer's finding FALSIFIABLE.

WHY (unattended-readiness item 4, operator/Main 2026-10-04)
-------------------------------------------------------
The authoritative re-gate reviewer (studio-27b-regate) flags correct code, and on a
GREEN verify that parks the slice as needs_opus. Main overrode every one by hand:
chat-fixes s4b (a0be91760fb5, a330fff2d326 -- "build_request mutates the input
list"; it builds new dicts), s6c (ef2868d21945 -- "'queued' silently dropped"), and
the "concerns / PASS WITH CAVEATS" parks whose only finding is the reviewer's own
UNCERTAIN caveat (c63772e0b710, b1b085738769).

A reviewer finding is a CLAIM about behaviour. This module turns it into a check:
  1. a second model call (runner side, `runner_main`) is asked for a REPRODUCER --
     a `def reproduce():` that drives the real code with the claim's trigger and
     asserts the CORRECT behaviour. The model only PROPOSES; it never decides.
  2. the GATE executes it (`check_row`) in a THROWAWAY COPY of the exact tree the
     reviewer saw (launch-baseline archive + the gated diff), and again on the
     baseline tree, and classifies the outcome DETERMINISTICALLY:
       head fails (assert / exception raised from the code under test), baseline
         does not fail the same way          -> CONFIRMED  (parks, reproducer attached)
       head fails, baseline fails identically -> pre-existing (advisory: this change
                                               did not introduce it)
       head passes                           -> UNCONFIRMED (advisory caveat; lands)
       no usable reproducer                  -> UNCONFIRMED (advisory caveat; lands)
       timeout / harness / env error         -> ERROR (fail-safe: park exactly as today)

PURE parts (`plan`, `classify_row`, `overall`, `apply_outcome`, prompt build/parse)
are what --self-test and the unit tests drive; `materialize`/`run_reproducer` are
the only side-effecting pieces and are bounded (size cap, timeout, scratch HOME).

LANGUAGES (2026-10-14, cardBonuses-v2 964c4a833b82): Python AND TypeScript/JavaScript. A TS finding used
to be "cannot be reproduced mechanically" -> parked as needs_opus even when it was false (the 27b regate
reviewer claimed `1e15 / 1` under-counts; `1e15 / 1 === 1e15` in IEEE 754 and the cited trigger returns the
right answer). A JS reproducer is an ES module (`export function reproduce()`, node:assert) run by
`node --experimental-strip-types` against the same throwaway trees; "touched the cited file" is read from
V8 coverage (a non-top-level function of the cited file ran), the analogue of the Python settrace check.

LIMITS (stated, not hidden): the verifier can only CONFIRM what a reproducer shows.
A real defect whose reproducer is vacuous (asserts too little) or cannot be written
(timing races, UI, non-Python code) comes back UNCONFIRMED and LANDS as an advisory
caveat -- that is the false-negative risk this design accepts in exchange for not
parking correct code. It is bounded by: a green verify, a relevance-PROVEN fixture
(plan() refuses otherwise), the baseline differential, and the caveat being recorded.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

# QUEUE LABEL. The daemon (ollama-queue._fire_gate_on_complete) only re-invokes the
# completion hook for a --runner job whose label starts gate-/regate-/secondop-; any
# other --runner job is "ungateable" and its completion would never reach the merge.
# gate-/regate- rows are QUEUE BARRIERS (hold fresh authoring, preempt) and their
# label is parsed as gate-<parent id> all over the queue; secondop- is the
# non-barrier child kind, and bundle_view/the dashboard map ^secondop-<hex> to its
# parent. So the check rides that prefix with a distinct SUFFIX -- no daemon change
# (and no daemon restart) needed. gate-on-complete.main() routes it BEFORE the plain
# secondop- branch.
FC_LABEL_RE = re.compile(r"^secondop-([0-9a-f]{6,})-fcheck$")
FC_AUTHORITY = "studio-27b-regate"
FC_PENDING_VERDICT = "pending-finding-check"


def fc_label(parent: str) -> str:
    return f"secondop-{parent}-fcheck"


def fc_parent(label: str):
    m = FC_LABEL_RE.match(str(label or ""))
    return m.group(1) if m else None
FC_MAX_ROWS = int(os.environ.get("GATE_FC_MAX_ROWS", "4"))
FC_REPRO_TIMEOUT_S = int(os.environ.get("GATE_FC_REPRO_TIMEOUT_S", "60"))
FC_SURVIVOR_NEAR = int(os.environ.get("GATE_FC_SURVIVOR_NEAR", "5"))
FC_TREE_MAX_BYTES = int(os.environ.get("GATE_FC_TREE_MAX_BYTES", str(300 * 1024 * 1024)))
FC_CODE_CHARS = 60000
FC_DIFF_CHARS = 16000
_IGNORE_DIRS = {".git", "node_modules", ".venv", "venv", "__pycache__", ".mypy_cache",
                ".pytest_cache", ".next", ".turbo", ".cache"}
# Exceptions raised while IMPORTING the code that mean "this interpreter cannot load
# the project" (missing third-party dep), not "the code is wrong". Treated as an
# environment ERROR (fail-safe park), never as a confirmation.
_ENV_EXC = {"ModuleNotFoundError", "ImportError"}


# ----------------------------------------------------------------- eligibility

def review_rows(payload: dict) -> list:
    """[(index, issue)] for the reviewer's code findings in payload['issues']."""
    return [(n, i) for n, i in enumerate(payload.get("issues") or [])
            if isinstance(i, dict) and i.get("source") == "review"
            and i.get("category") == "code"]


def _file_matches(f: str, df: str) -> bool:
    f, df = str(f or "").strip().lstrip("./"), str(df or "").strip().lstrip("./")
    return bool(f) and bool(df) and (f == df or df.endswith("/" + f) or f.endswith("/" + df))


JS_EXTS = (".ts", ".mts", ".js", ".mjs", ".cjs")      # .tsx/.jsx need a transform: not checkable


def lang_of(path) -> str:
    """'py' | 'js' | '' for a cited file. PURE."""
    p = str(path or "").lower()
    if p.endswith(".py"):
        return "py"
    if p.endswith(JS_EXTS):
        return "js"
    return ""


def plan(payload: dict, diff_files) -> dict:
    """PURE. What to do with a TERMINAL authoritative verdict's reviewer findings.

    {"action": "check"|"advisory"|"none", "reason": str, "rows": [issue index ...]}
      check    -- every finding is a reviewer code claim about a .py/.ts/.js file in the diff,
                  on a green, relevance-proven verify: run the reproducer pass.
      advisory -- 'concerns' made ONLY of the reviewer's own UNCERTAIN caveats
                  (not high) on a green, relevance-proven verify, that cannot be
                  checked mechanically (non-Python): record, do not park.
      none     -- anything else: today's behaviour, unchanged (fail-closed).
    """
    def none(why):
        return {"action": "none", "reason": why, "rows": []}
    v = str(payload.get("verdict") or "")
    if v not in ("fail", "concerns"):
        return none(f"verdict={v or '?'}: nothing to check")
    if payload.get("gate_authority") != FC_AUTHORITY:
        return none(f"authority={payload.get('gate_authority')!r}: only a terminal "
                    f"authoritative re-gate verdict is checked")
    if payload.get("untrusted"):
        return none("untrusted (dirty) launch baseline")
    if str(payload.get("review_verdict") or "").upper().startswith("UNPROVEN"):
        return none("the review itself is UNPROVEN (truncated)")
    if payload.get("verify_exit_reported") != 0:
        return none(f"the job's verify is not green "
                    f"(verify_exit_reported={payload.get('verify_exit_reported')!r})")
    if payload.get("job_exit_code") not in (0, None):
        return none(f"job exit {payload.get('job_exit_code')!r}")
    if payload.get("verify_failed_at_baseline") is False:
        return none("verify was already green at baseline (proves nothing about the diff)")
    issues = [i for i in (payload.get("issues") or []) if isinstance(i, dict)]
    rows = review_rows(payload)
    if not issues or not rows:
        return none("no reviewer code finding")
    other = sorted({str(i.get("source")) for i in issues
                    if not (i.get("source") == "review" and i.get("category") == "code")})
    if other:
        return none(f"non-reviewer findings present ({','.join(other)}): not this check's call")
    if len(rows) > FC_MAX_ROWS:
        return none(f"{len(rows)} reviewer findings > GATE_FC_MAX_ROWS={FC_MAX_ROWS}")
    vr = payload.get("verify_relevance") or {}
    if str(vr.get("verdict") or "") != "relevant":
        return none(f"verify relevance is {vr.get('verdict') or 'unmeasured'!r}, not "
                    f"'relevant': the green verify is not proven to pin the property")
    for _n, r in rows:
        ln = int(r.get("line") or 0)
        if ln <= 0:
            continue
        for s in vr.get("survivors") or []:
            if (_file_matches(r.get("file"), s.get("file"))
                    and abs(int(s.get("line") or 0) - ln) <= FC_SURVIVOR_NEAR):
                return none(f"a surviving relevance mutant sits at {s.get('file')}:"
                            f"{s.get('line')}, within {FC_SURVIVOR_NEAR} lines of the "
                            f"finding at {r.get('file')}:{ln} -- the fixture does not pin it")
    dfiles = list(diff_files or [])
    checkable = all(lang_of(r.get("file"))
                    and any(_file_matches(r.get("file"), d) for d in dfiles)
                    for _n, r in rows)
    if checkable:
        return {"action": "check", "rows": [n for n, _ in rows],
                "reason": f"{len(rows)} reviewer finding(s) on a green, relevance-proven "
                          f"verify -> reproducer check"}
    if v == "concerns" and all(r.get("uncertain") and r.get("severity") != "high"
                               for _n, r in rows):
        return {"action": "advisory", "rows": [n for n, _ in rows],
                "reason": "concerns made only of the reviewer's own UNCERTAIN caveats on a "
                          "green, relevance-proven verify; not mechanically checkable "
                          "(non-Python / not in the diff) -> advisory, not a park"}
    return none("finding cites a file that is not Python/TypeScript/JavaScript, or one outside "
                "the diff: cannot be reproduced mechanically")


# ------------------------------------------------------------- report details

def finding_detail(report_txt: str, what: str) -> str:
    """The reviewer's FULL finding block (title + how-it-fails + trigger) whose title
    starts with the (180-char-truncated) `what`. '' when not found."""
    key = re.sub(r"\s+", " ", str(what or "")).strip()[:120]
    if not key:
        return ""
    blocks = re.split(r"(?m)^### ", report_txt or "")
    for b in blocks[1:]:
        title = re.sub(r"^\[[A-Z]+\]\s*", "", b.split("\n", 1)[0]).strip()
        if re.sub(r"\s+", " ", title).startswith(key[:100]):
            body = re.split(r"(?m)^## ", b, maxsplit=1)[0]
            return ("### " + body).strip()[:4000]
    return ""


# ------------------------------------------------------------ prompt (runner)

SYSTEM_PROMPT = """You turn a code-review CLAIM into an executable Python reproducer.
You do NOT judge the claim. A harness runs your code against the real repository and
decides from what actually happens. A wrong claim is common; a test that passes on
correct code is a perfectly good outcome.

Write a Python module that defines `def reproduce():` (no arguments). Rules:
1. The working directory and sys.path[0] are the repository root. Import the module
   under test by its module path (e.g. `import dashboard_chat`, `from pkg import mod`).
2. reproduce() must call the REAL code the claim is about, with the input/trigger the
   claim describes, and then `assert` the CORRECT behaviour -- what the code does if
   the claim is FALSE. If the claim is TRUE, reproduce() must fail: your assert fails,
   or the code under test raises.
3. Never catch exceptions raised by the code under test. Never assert that the claim
   is wrong in prose; never print a verdict.
4. Isolation: create any files under tempfile.mkdtemp(); point environment variables
   the code reads at temp paths; never touch the network, the real home directory,
   or real services. Patch subprocess, sockets, HTTP, threads and time.sleep with
   unittest.mock when the code would invoke them, and assert on the mock's calls if
   the claim is about what gets invoked. Standard library only.
5. Deterministic, under 80 lines, finishes in well under 20 seconds.
6. If the claim is not observable by running code (style, intent, a hypothetical
   about code that is not shown, a race that needs real concurrency), return null.

Reply with JSON only: {"reproducer": "<python source>" or null, "explanation": "<one or two sentences: what the reproducer exercises and what it asserts>"}"""

JS_SYSTEM_PROMPT = """You turn a code-review CLAIM into an executable TypeScript/JavaScript reproducer.
You do NOT judge the claim. A harness runs your code against the real repository and
decides from what actually happens. A wrong claim is common; a test that passes on
correct code is a perfectly good outcome.

Write an ES module that exports `export function reproduce(): void` (no arguments, synchronous).
Rules:
1. The module is placed at the repository root. Import the module under test by a relative path
   WITH its extension (e.g. `import { f } from './lib/thing.ts';`). Import
   `import assert from 'node:assert/strict';`.
2. reproduce() must call the REAL code the claim is about, with the input/trigger the
   claim describes, and then `assert` the CORRECT behaviour -- what the code does if
   the claim is FALSE. If the claim is TRUE, reproduce() must fail: your assert fails,
   or the code under test throws.
3. Never catch exceptions raised by the code under test. Never assert that the claim is
   wrong in prose; never print a verdict. Do not re-implement the code under test.
4. Only syntax that Node's type stripping supports (no enums, namespaces, parameter
   properties). Node standard library only. No network, no real home directory; use
   os.tmpdir() for any file.
5. Deterministic, under 80 lines, finishes in well under 20 seconds.
6. If the claim is not observable by running code (style, intent, a hypothetical about
   code that is not shown, a race that needs real concurrency), return null.

Reply with JSON only: {"reproducer": "<typescript source>" or null, "explanation": "<one or two sentences: what the reproducer exercises and what it asserts>"}"""

REPLY_SCHEMA = {"type": "object",
                "properties": {"reproducer": {"type": ["string", "null"]},
                               "explanation": {"type": "string"}},
                "required": ["reproducer", "explanation"]}


def _window(text: str, line: int, cap: int) -> str:
    if len(text) <= cap:
        return text
    lines = text.splitlines(keepends=True)
    if line <= 0:
        return text[:cap]
    lo = max(0, line - 1)
    out, n, a, b = [], 0, lo, lo
    while (a > 0 or b < len(lines)) and n < cap:
        if b < len(lines):
            out.append((b, lines[b])); n += len(lines[b]); b += 1
        if a > 0 and n < cap:
            a -= 1; out.append((a, lines[a])); n += len(lines[a])
    out.sort()
    return "".join(f"{i + 1:>5}| {t}" for i, t in out)


def build_user_prompt(row: dict, code_text: str, diff_text: str, intent: str) -> str:
    f, ln = row.get("file") or "?", int(row.get("line") or 0)
    fence = "typescript" if lang_of(f) == "js" else "python"
    claim = row.get("detail") or row.get("what") or ""
    return (f"CLAIM (from an automated reviewer; may be wrong):\n{claim}\n\n"
            f"Cited location: {f}:{ln if ln else '(unknown line)'}\n\n"
            f"What the change was meant to do:\n{(intent or '(not stated)')[:2500]}\n\n"
            f"The change under review (unified diff):\n```diff\n{diff_text[:FC_DIFF_CHARS]}\n```\n\n"
            f"Current contents of {f} (after the change):\n```{fence}\n"
            f"{_window(code_text, ln, FC_CODE_CHARS)}\n```\n\n"
            f"Write the reproducer for THIS claim (JSON only).")


def _node() -> str | None:
    for c in (os.environ.get("NODE"), "/opt/homebrew/bin/node", "/usr/local/bin/node"):
        if c and os.access(c, os.X_OK):
            return c
    return shutil.which("node")


def parse_reply(obj, lang: str = "py") -> tuple:
    """(source|None, explanation, problem|None). PURE except the JS syntax check (node --check)."""
    if not isinstance(obj, dict):
        return None, "", "model reply was not a JSON object"
    src, expl = obj.get("reproducer"), str(obj.get("explanation") or "")[:600]
    if src is None or (isinstance(src, str) and not src.strip()):
        return None, expl, "model declined (no reproducer)"
    if not isinstance(src, str):
        return None, expl, "reproducer is not a string"
    src = re.sub(r"^\s*```(?:python|typescript|ts|javascript|js)?\s*\n|\n```\s*$", "", src.strip()) + "\n"
    if lang == "js":
        if not re.search(r"(?m)^export\s+function\s+reproduce\s*\(\s*\)", src):
            return None, expl, "no top-level `export function reproduce()`"
        node = _node()
        if node:
            d = tempfile.mkdtemp(prefix="gate-fc-syn-")
            try:
                fp = Path(d) / "r.mts"
                fp.write_text(src)
                # `node --check` does NOT parse .mts (exits 0 on garbage); stripTypeScriptTypes does.
                c = subprocess.run([node, "--no-warnings", "--input-type=module", "-e",
                                    "import {stripTypeScriptTypes as s} from 'node:module';"
                                    "import fs from 'node:fs'; s(fs.readFileSync(process.argv[1],'utf8'))",
                                    str(fp)], capture_output=True, text=True, timeout=30)
                if c.returncode != 0 and "SyntaxError" in (c.stderr or ""):
                    m = re.search(r"SyntaxError[^\n]*", c.stderr)
                    return None, expl, "reproducer does not compile: " + (m.group(0) if m else "syntax error")[:160]
            except Exception:
                pass
            finally:
                shutil.rmtree(d, ignore_errors=True)
        return src, expl, None
    if not re.search(r"(?m)^def reproduce\s*\(\s*\)\s*:", src):
        return None, expl, "no top-level `def reproduce():`"
    try:
        compile(src, "<reproducer>", "exec")
    except SyntaxError as e:
        return None, expl, f"reproducer does not compile: {e.msg} (line {e.lineno})"
    return src, expl, None


def runner_main(spec: dict, model, outdir: Path, log=print) -> int:
    """code-review-agent.py --mode refute: one model call per finding (+1 retry when
    the reply does not compile). Writes outdir/refute.json. Never executes anything."""
    head = Path(spec.get("head_tree") or "")
    try:
        diff_text = Path(spec.get("diff") or "").read_text(errors="replace")
    except Exception:
        diff_text = ""
    rows_out = []
    for row in spec.get("rows") or []:
        rec = {"idx": row.get("idx"), "file": row.get("file"), "line": row.get("line"),
               "reproducer": None, "explanation": "", "problem": None}
        try:
            code_text = (head / str(row.get("file") or "")).read_text(errors="replace")
        except Exception as e:
            rec["problem"] = f"cannot read cited file: {e}"
            rows_out.append(rec)
            continue
        user = build_user_prompt(row, code_text, diff_text, spec.get("intent") or "")
        lang = lang_of(row.get("file")) or "py"
        sysmsg = JS_SYSTEM_PROMPT if lang == "js" else SYSTEM_PROMPT
        problem = None
        for attempt in (1, 2):
            try:
                obj = model.chat_json(sysmsg, user if attempt == 1 else
                                      user + f"\n\nYour previous reply was unusable: "
                                             f"{problem}. Reply again, JSON only.",
                                      REPLY_SCHEMA, num_predict=6000)
            except Exception as e:
                obj, problem = None, f"model call failed: {type(e).__name__}: {e}"[:300]
                break
            src, expl, problem = parse_reply(obj, lang)
            rec["explanation"] = expl
            if src is not None or (problem or "").startswith("model declined"):
                rec["reproducer"] = src
                break
        rec["problem"] = problem if rec["reproducer"] is None else None
        rec["attempts"] = attempt
        log(f"  refute row {row.get('idx')} {row.get('file')}:{row.get('line')}: "
            f"{'reproducer' if rec['reproducer'] else 'none'}"
            f"{' (' + rec['problem'] + ')' if rec['problem'] else ''}")
        rows_out.append(rec)
    outdir.mkdir(parents=True, exist_ok=True)
    (outdir / "refute.json").write_text(json.dumps(
        {"rows": rows_out, "model": getattr(model, "model", None),
         "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}, indent=1))
    return 0


# -------------------------------------------------------- trees + execution

def _tree_bytes(root: Path) -> int:
    n = 0
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d not in _IGNORE_DIRS]
        for fn in fns:
            try:
                n += os.lstat(os.path.join(dp, fn)).st_size
            except OSError:
                pass
    return n


def materialize(job_cwd: str, base_commit: str, diff_path, dest: Path) -> tuple:
    """Build dest/base (git archive of the launch baseline) and dest/head (base + the
    gated diff) -- EXACTLY the code the reviewer saw, independent of whatever the
    worktree holds now. (ok, why). Never raises."""
    try:
        if not job_cwd or not base_commit:
            return False, "no job cwd / launch-baseline commit recorded"
        if not Path(job_cwd).is_dir():
            return False, f"job worktree gone: {job_cwd}"
        base, head = dest / "base", dest / "head"
        for p in (base, head):
            if p.exists():
                shutil.rmtree(p)
        base.mkdir(parents=True)
        arc = subprocess.run(["git", "-C", str(job_cwd), "archive", "--format=tar",
                              str(base_commit)], capture_output=True, timeout=300)
        if arc.returncode != 0:
            return False, f"git archive {base_commit[:12]} failed: {arc.stderr[-200:]!r}"
        if len(arc.stdout) > FC_TREE_MAX_BYTES:
            return False, f"tree {len(arc.stdout)} bytes > GATE_FC_TREE_MAX_BYTES"
        x = subprocess.run(["tar", "-x", "-C", str(base)], input=arc.stdout,
                           capture_output=True, timeout=300)
        if x.returncode != 0:
            return False, f"tar extract failed: {x.stderr[-200:]!r}"
        shutil.copytree(base, head, symlinks=True)
        ap = subprocess.run(["git", "apply", "--whitespace=nowarn", str(diff_path)],
                            cwd=str(head), capture_output=True, text=True, timeout=120)
        if ap.returncode != 0:
            return False, f"the gated diff does not apply to the baseline: {ap.stderr[-200:]}"
        return True, "ok"
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"[:300]


_HARNESS = r'''
import json, os, sys, traceback
REPRO, OUT, CITED = sys.argv[1], sys.argv[2], sys.argv[3]
ROOT = os.path.realpath(os.getcwd())
sys.path.insert(0, ROOT)
REPRO_R = os.path.realpath(REPRO)
CITED_R = os.path.realpath(os.path.join(ROOT, CITED))
_seen = {}
_touched = [False]
def _real(fn):
    r = _seen.get(fn)
    if r is None:
        try:
            r = os.path.realpath(fn)
        except Exception:
            r = fn
        _seen[fn] = r
    return r
def _tr(frame, event, arg):
    if not _touched[0] and _real(frame.f_code.co_filename) == CITED_R:
        _touched[0] = True
    return None
def _classify(e, phase):
    tb = traceback.extract_tb(e.__traceback__)
    inside = [f for f in tb if _real(f.filename).startswith(ROOT + os.sep)
              or _real(f.filename) == REPRO_R]
    last = inside[-1] if inside else None
    lastf = _real(last.filename) if last else None
    where = (os.path.relpath(lastf, ROOT) + ":" + str(last.lineno)) if last else "?"
    name = type(e).__name__
    if phase == "setup" and name in %(env)r:
        kind = "env"
    elif lastf is None or lastf == REPRO_R:
        kind = "fail" if (isinstance(e, AssertionError) and phase == "run") else "invalid"
    else:
        kind = "fail"
    return {"kind": kind, "phase": phase, "exc": name, "msg": str(e)[:300],
            "where": where, "in_product": bool(lastf and lastf != REPRO_R),
            "trace": "".join(traceback.format_exception(type(e), e, e.__traceback__))[-1500:]}
res = None
try:
    code = compile(open(REPRO).read(), REPRO, "exec")
except SyntaxError as e:
    res = {"kind": "invalid", "phase": "compile", "exc": "SyntaxError", "msg": str(e)[:300]}
if res is None:
    ns = {"__name__": "gate_reproducer", "__file__": REPRO}
    sys.settrace(_tr)
    try:
        exec(code, ns)
    except BaseException as e:
        res = _classify(e, "setup")
    if res is None:
        fn = ns.get("reproduce")
        if not callable(fn):
            res = {"kind": "invalid", "phase": "setup", "msg": "no reproduce() defined"}
        else:
            # Only code run BY reproduce() counts as exercising the cited file --
            # merely importing the module (setup) executes its top level too.
            _touched[0] = False
            try:
                fn()
                res = {"kind": "pass", "phase": "run"}
            except BaseException as e:
                res = _classify(e, "run")
    sys.settrace(None)
res["touched_cited"] = _touched[0]
if res["kind"] == "fail" and not _touched[0]:
    res["kind"] = "invalid"
    res["note"] = "failed without executing any code in the cited file"
if res["kind"] == "pass" and not _touched[0]:
    res["note"] = "passed without executing any code in the cited file (vacuous)"
with open(OUT, "w") as fh:
    json.dump(res, fh)
sys.stdout.flush()
os._exit(0)
''' % {"env": sorted(_ENV_EXC)}

_JS_HARNESS = r'''
import { pathToFileURL } from 'node:url';
import fs from 'node:fs';
const [, , REPRO, OUT, CITED] = process.argv;
const ROOT = fs.realpathSync(process.cwd());
const ENVEXC = ['ERR_MODULE_NOT_FOUND', 'ERR_PACKAGE_PATH_NOT_EXPORTED', 'ERR_UNKNOWN_FILE_EXTENSION'];
function frames(e) {
  const st = String((e && e.stack) || '').split('\n').filter(l => /^\s+at /.test(l));
  return st.map(l => { const m = l.match(/\(?(?:file:\/\/)?(\/[^():]+?):(\d+):\d+\)?\s*$/); return m ? { f: m[1], l: +m[2] } : null; }).filter(Boolean);
}
function classify(e, phase) {
  const fr = frames(e).filter(x => x.f.startsWith(ROOT + '/') && !x.f.includes('/node_modules/'));
  const last = fr[0] || null;                       // innermost in-tree frame (V8 stacks are innermost-first)
  const inRepro = !!(last && last.f === REPRO);
  const name = (e && e.name) || 'Error';
  let kind;
  if (phase === 'setup' && ENVEXC.includes(e && e.code)) kind = 'env';
  else if (name === 'AssertionError' || (e && e.code === 'ERR_ASSERTION')) kind = phase === 'run' ? 'fail' : 'invalid';
  else if (phase === 'setup' || !last || inRepro) kind = 'invalid';   // the reproducer itself is broken
  else kind = 'fail';                                // the code under test threw
  return { kind, phase, exc: name, msg: String((e && e.message) || e).slice(0, 300),
           where: last ? last.f.slice(ROOT.length + 1) + ':' + last.l : '?',
           in_product: !!(last && !inRepro), trace: String((e && e.stack) || '').slice(-1500) };
}
let res;
let mod = null;
try { mod = await import(pathToFileURL(REPRO).href); } catch (e) { res = classify(e, 'setup'); }
if (!res) {
  if (typeof mod.reproduce !== 'function') res = { kind: 'invalid', phase: 'setup', msg: 'no reproduce() exported' };
  else {
    try { const r = mod.reproduce(); if (r && typeof r.then === 'function') await r; res = { kind: 'pass', phase: 'run' }; }
    catch (e) { res = classify(e, 'run'); }
  }
}
fs.writeFileSync(OUT, JSON.stringify(res));
process.exit(0);
'''


def _js_touched(covdir: Path, cited: str, root: Path) -> bool:
    """V8 coverage: did a NON-top-level function of the cited file run? (the top-level range
    also runs on import, which is setup, not the claim's trigger.) PURE file read."""
    want = os.path.realpath(str(root / cited))
    for f in Path(covdir).glob("coverage-*.json"):
        try:
            d = json.loads(f.read_text())
        except Exception:
            continue
        for sc in d.get("result") or []:
            u = str(sc.get("url") or "")
            if not u.startswith("file://"):
                continue
            try:
                from urllib.parse import unquote
                if os.path.realpath(unquote(u[7:])) != want:
                    continue
            except Exception:
                continue
            fns = sc.get("functions") or []
            for fn in fns[1:]:
                rs = fn.get("ranges") or []
                if rs and rs[0].get("count", 0) > 0:
                    return True
    return False


def _run_js(work: Path, scratch: Path, source: str, cited_file: str, timeout: int, env: dict) -> dict:
    node = _node()
    if not node:
        return {"kind": "error", "msg": "node not found"}
    repro = work / "gate_reproducer.mts"
    repro.write_text(source)
    harness = scratch / "gate_fc_harness.mjs"
    harness.write_text(_JS_HARNESS)
    out = scratch / "result.json"
    cov = scratch / "cov"
    cov.mkdir()
    e = dict(env)
    e["PATH"] = os.path.dirname(node) + ":" + env.get("PATH", "")
    e["NODE_V8_COVERAGE"] = str(cov)
    try:
        p = subprocess.run([node, "--experimental-strip-types", "--no-warnings", str(harness),
                            str(os.path.realpath(repro)), str(out), str(cited_file)],
                           cwd=str(work), env=e, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        return {"kind": "timeout", "msg": f"reproducer exceeded {timeout}s"}
    if not out.is_file():
        return {"kind": "error", "msg": "harness wrote no result (reproducer exited the process?)",
                "stderr": (p.stderr or "")[-600:]}
    try:
        res = json.loads(out.read_text())
    except Exception as ex:
        return {"kind": "error", "msg": f"unreadable result: {ex}"}
    touched = _js_touched(cov, cited_file, work)
    res["touched_cited"] = touched
    if res.get("kind") == "fail" and not touched:
        res["kind"] = "invalid"
        res["note"] = "failed without executing any code in the cited file"
    if res.get("kind") == "pass" and not touched:
        res["note"] = "passed without executing any code in the cited file (vacuous)"
    return res


def run_reproducer(tree: Path, source: str, cited_file: str,
                   timeout: int = None, python: str = None, deps_from: str = None) -> dict:
    """Execute `source` against a THROWAWAY copy of `tree`. Returns the harness
    result {"kind": pass|fail|invalid|env|timeout|error, ...}. Never raises."""
    timeout = timeout or FC_REPRO_TIMEOUT_S
    scratch = Path(tempfile.mkdtemp(prefix="gate-fc-"))
    try:
        if _tree_bytes(tree) > FC_TREE_MAX_BYTES:
            return {"kind": "error", "msg": "tree too large to copy"}
        work = scratch / "tree"
        shutil.copytree(tree, work, symlinks=True,
                        ignore=shutil.ignore_patterns(*_IGNORE_DIRS))
        (scratch / "home").mkdir()
        (scratch / "tmp").mkdir()
        if lang_of(cited_file) == "js":
            # env parity: git archive carries no node_modules; link the job worktree's so a
            # cited module's package imports resolve exactly as they did for its verify.
            nm = Path(deps_from or "") / "node_modules" if deps_from else None
            if nm and nm.is_dir() and not (work / "node_modules").exists():
                try:
                    os.symlink(str(nm), str(work / "node_modules"))
                except OSError:
                    pass
            jenv = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
                    "HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"),
                    "LANG": "en_US.UTF-8"}
            return _run_js(work, scratch, source, cited_file, timeout, jenv)
        repro = scratch / "gate_reproducer.py"
        repro.write_text(source)
        harness = scratch / "gate_fc_harness.py"
        harness.write_text(_HARNESS)
        out = scratch / "result.json"
        # HOME is a scratch dir (an expanduser("~/...") in the code under test must
        # never touch the real home); PYTHONUSERBASE keeps the REAL user site-packages
        # importable, since that is where the verify's own interpreter found its deps.
        import site
        env = {"PATH": "/usr/bin:/bin:/usr/sbin:/sbin:/opt/homebrew/bin",
               "HOME": str(scratch / "home"), "TMPDIR": str(scratch / "tmp"),
               "LANG": "en_US.UTF-8", "PYTHONDONTWRITEBYTECODE": "1",
               "PYTHONHASHSEED": "0", "PYTHONUSERBASE": site.getuserbase() or ""}
        try:
            p = subprocess.run([python or sys.executable, "-B", str(harness),
                                str(repro), str(out), str(cited_file)],
                               cwd=str(work), env=env, capture_output=True, text=True,
                               timeout=timeout)
        except subprocess.TimeoutExpired:
            return {"kind": "timeout", "msg": f"reproducer exceeded {timeout}s"}
        if not out.is_file():
            return {"kind": "error", "msg": "harness wrote no result (reproducer exited "
                                            "the interpreter?)",
                    "stderr": (p.stderr or "")[-600:]}
        try:
            return json.loads(out.read_text())
        except Exception as e:
            return {"kind": "error", "msg": f"unreadable result: {e}"}
    except Exception as e:
        return {"kind": "error", "msg": f"{type(e).__name__}: {e}"[:300]}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def _sig(r: dict) -> tuple:
    return (r.get("exc"), r.get("where"), (r.get("msg") or "")[:120])


def classify_row(head: dict | None, base: dict | None, problem: str | None = None) -> dict:
    """PURE. One finding's outcome from its head (and, if head failed, baseline) runs.
    {"outcome": confirmed|pre-existing|unconfirmed|no-reproducer|error, "why": str}"""
    if head is None:
        return {"outcome": "no-reproducer",
                "why": problem or "no reproducer could be produced within the budget"}
    k = head.get("kind")
    if k in ("timeout", "error", "env"):
        return {"outcome": "error", "why": f"head run {k}: {head.get('msg') or head.get('exc')}"}
    if k == "pass":
        return {"outcome": "unconfirmed",
                "why": "the reproducer PASSES against the gated code"
                       + (f" ({head['note']})" if head.get("note") else "")}
    if k == "invalid":
        return {"outcome": "no-reproducer",
                "why": f"the reproducer itself is broken ({head.get('exc') or ''} "
                       f"{head.get('msg') or head.get('note') or ''})".strip()}
    if k != "fail":
        return {"outcome": "error", "why": f"unknown head result kind {k!r}"}
    hs = f"{head.get('exc')} at {head.get('where')}: {(head.get('msg') or '')[:160]}"
    if base and base.get("kind") == "fail" and _sig(base) == _sig(head):
        return {"outcome": "pre-existing",
                "why": f"fails identically on the launch baseline ({hs}) -- not introduced "
                       f"by this change"}
    return {"outcome": "confirmed", "why": f"the reproducer FAILS against the gated code: {hs}"
            + (f"; baseline: {base.get('kind')}" if base else "")}


# A row the runner could not even attempt is a VERIFIER failure (park), not "no
# reproducer within budget" (land): the model was unreachable or the tree unreadable.
_RUNNER_ERROR_PREFIXES = ("model call failed", "cannot read cited file")


def check_rows(issues: list, plan_rows: list, refute: dict, fcdir: Path,
               python: str = None, runner=None, deps_from: str = None) -> dict:
    """Execute the runner's proposed reproducers. {issue index: classify_row(...) +
    "reproducer" path}. Every planned row gets an outcome; anything the runner did
    not return, or returned for a different finding, is an ERROR (fail-safe park).
    `runner` is injectable (defaults to run_reproducer). Never raises."""
    runner = runner or run_reproducer
    head, base = Path(fcdir) / "head", Path(fcdir) / "base"
    got = {}
    for r in (refute or {}).get("rows") or []:
        if isinstance(r, dict) and isinstance(r.get("idx"), int):
            got[r["idx"]] = r
    out = {}
    for n in plan_rows:
        try:
            issue = issues[n]
            r = got.get(n)
            if r is None:
                out[n] = {"outcome": "error", "why": "the verifier returned no result for this finding"}
                continue
            if str(r.get("file") or "") != str(issue.get("file") or "") \
                    or int(r.get("line") or 0) != int(issue.get("line") or 0):
                out[n] = {"outcome": "error", "why": "verifier result does not match the finding "
                                                     f"({r.get('file')}:{r.get('line')})"}
                continue
            src = r.get("reproducer")
            if not src:
                prob = str(r.get("problem") or "")
                if prob.startswith(_RUNNER_ERROR_PREFIXES):
                    out[n] = {"outcome": "error", "why": f"verifier failed: {prob[:200]}"}
                else:
                    out[n] = classify_row(None, None, prob or None)
                continue
            if not head.is_dir():
                out[n] = {"outcome": "error", "why": "materialized head tree is missing"}
                continue
            rp = Path(fcdir) / (f"row{n}.mts" if lang_of(issue.get("file")) == "js" else f"row{n}.py")
            rp.write_text(src)
            _kw = {"python": python}
            if deps_from:
                _kw["deps_from"] = deps_from
            h = runner(head, src, issue.get("file"), **_kw)
            b = None
            if h.get("kind") == "fail":
                b = (runner(base, src, issue.get("file"), **_kw) if base.is_dir()
                     else {"kind": "error", "msg": "no baseline tree"})
            c = classify_row(h, b)
            c["reproducer"] = str(rp)
            c["head"] = {k: v for k, v in h.items() if k != "trace"}
            c["head_trace"] = (h.get("trace") or "")[-800:]
            if b is not None:
                c["base"] = {k: v for k, v in b.items() if k != "trace"}
            out[n] = c
        except Exception as e:
            out[n] = {"outcome": "error", "why": f"{type(e).__name__}: {e}"[:300]}
    return out


def overall(outcomes: list) -> str:
    """PURE. confirmed > error > unconfirmed. An empty list is an error (fail-safe)."""
    kinds = [o.get("outcome") for o in outcomes]
    if not kinds:
        return "error"
    if "confirmed" in kinds:
        return "confirmed"
    if "error" in kinds:
        return "error"
    return "unconfirmed"


def recompute_verdict(payload: dict) -> None:
    """Same rule as gate-on-complete.merge_review: a code HIGH fails, any remaining
    issue (or an UNPROVEN review) is concerns, else pass. Refreshes counts."""
    issues = [i for i in payload.get("issues") or [] if isinstance(i, dict)]
    code_high = [i for i in issues if i.get("category") == "code" and i.get("severity") == "high"]
    payload["counts"] = {
        "code_high": len(code_high),
        "code": sum(1 for i in issues if i.get("category") == "code"),
        "input": sum(1 for i in issues if i.get("category") == "input"),
        "total": len(issues)}
    unproven = str(payload.get("review_verdict", "")).upper().startswith("UNPROVEN")
    payload["verdict"] = "fail" if code_high else "concerns" if (issues or unproven) else "pass"


def apply_outcome(payload: dict, results: dict) -> str:
    """PURE (mutates payload). results = {issue index: {"outcome","why","reproducer"?}}.
    CONFIRMED rows stay (an uncertain-capped row is restored to the reviewer's own
    severity -- it is now a demonstrated defect); every other row moves to
    payload['advisory_caveats'] and out of the verdict. Returns the overall outcome;
    on 'error' NOTHING is changed (fail-safe: today's park)."""
    ov = overall(list(results.values()))
    if ov == "error":
        return ov
    keep, adv = [], payload.setdefault("advisory_caveats", [])
    for n, i in enumerate(payload.get("issues") or []):
        r = results.get(n)
        if r is None:
            keep.append(i)
            continue
        if r["outcome"] == "confirmed":
            i = dict(i)
            if i.get("uncertain") and i.get("reviewer_severity"):
                i["severity"] = i["reviewer_severity"]
            i["uncertain"] = False
            i["confirmed_by_reproducer"] = r.get("reproducer") or True
            i["finding_check"] = r["why"][:300]
            keep.append(i)
        else:
            a = dict(i)
            a["finding_check"] = r["outcome"]
            a["finding_check_why"] = r["why"][:300]
            if r.get("reproducer"):
                a["reproducer"] = r["reproducer"]
            adv.append(a)
    payload["issues"] = keep
    recompute_verdict(payload)
    return ov


def _self_test() -> bool:
    ok = True

    def check(name, got, want):
        nonlocal ok
        if got != want:
            ok = False
        print(("ok   " if got == want else "FAIL ") + f"- {name}"
              + ("" if got == want else f"  (got {got!r}, want {want!r})"))
    base = {"verdict": "fail", "gate_authority": FC_AUTHORITY, "verify_exit_reported": 0,
            "job_exit_code": 0, "verify_failed_at_baseline": True, "review_verdict": "FAIL",
            "verify_relevance": {"verdict": "relevant", "survivors": []},
            "issues": [{"severity": "high", "category": "code", "source": "review",
                        "file": "m.py", "line": 10, "what": "x"}]}
    check("eligible -> check", plan(base, ["m.py"])["action"], "check")
    check("verify red -> none", plan({**base, "verify_exit_reported": 1}, ["m.py"])["action"], "none")
    check("relevance low -> none", plan({**base, "verify_relevance": {"verdict": "low"}},
                                        ["m.py"])["action"], "none")
    check("pregate authority -> none", plan({**base, "gate_authority": "x"}, ["m.py"])["action"], "none")
    check("classify pass -> unconfirmed", classify_row({"kind": "pass"}, None)["outcome"], "unconfirmed")
    check("classify fail/base pass -> confirmed",
          classify_row({"kind": "fail", "exc": "E"}, {"kind": "pass"})["outcome"], "confirmed")
    check("classify timeout -> error", classify_row({"kind": "timeout"}, None)["outcome"], "error")
    check("overall empty -> error", overall([]), "error")
    check("label round-trips", fc_parent(fc_label("a330fff2d326")), "a330fff2d326")
    check("plain secondop is not a finding check", fc_parent("secondop-a330fff2d326"), None)
    p = json.loads(json.dumps(base))
    check("apply unconfirmed -> pass", (apply_outcome(p, {0: {"outcome": "unconfirmed", "why": "w"}}),
                                        p["verdict"]), ("unconfirmed", "pass"))
    print("ALL PASS" if ok else "SELF-TEST FAILED")
    return ok


if __name__ == "__main__":
    if "--self-test" in sys.argv:
        sys.exit(0 if _self_test() else 1)
    print(__doc__)
