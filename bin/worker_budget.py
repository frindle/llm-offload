#!/usr/bin/env python3
"""worker_budget.py -- switchable AGENT-BUDGET POLICIES for ollama-worker.py (2026-10-09).

WHY. Local analysis of ~2,400 dispatch-metrics rows / ~3,400 transcripts: flat-author jobs hit
the iteration cap 23-30% of the time, 160 jobs converged on exactly their LAST iteration, ~80% of
capped author jobs wrote a file in their last 3 iterations, and the first write lands at median
iteration 8 (capped) vs 6 (converged). Outside work disagrees on whether budget SIZE or budget
STRUCTURE is the lever, so none of this is a default: it is an A/B switch (see
~/bin/budget-policy-ab.md).

THE SWITCH. `ollama-worker.py --budget-policy name[,name...]`, else a `Budget-policy: a,b` line in
the TASK text (per-job passthrough with no queue change), else env WORKER_BUDGET_POLICY. Default
EMPTY = the worker runs byte-identical to before (every hook is gated on `bp is not None`).
`all` expands to every policy. Unknown names are reported (CLI = hard error, task/env = ignored
with a log line).

POLICIES
  progress_extend    at the cap, grant ONE extension (+8; +12 for refine tasks) if a write/edit landed
                     in the last 3 iterations AND the last verify output differs from the previous
                     one; refuse when the last 3 verify tails are identical, when no verify has been
                     seen, or past the hard ceiling (1.5x the original cap).
  budget_visible     one-line remaining-budget notice EVERY turn (iteration i of N, extension state).
  read_window        read_file shows >= 100 lines per page by default (and as a floor).
  mask_stale_reads   read_file results older than the last K=3 tool turns become a one-line stub
                     when the file has not been edited since (prompt copy only; transcript intact).
  prefetch_excerpt   inject the target file's head + outline (or a named line range) into the first
                     prompt. Specs: --prefetch-excerpt PATH[:A-B], or `Prefetch: PATH[:A-B]` lines
                     in the task / TASK.md / AUTO-TASK.md; falls back to the task's declared targets.
  checkpoint_revert  remember the best verify state (fewest failures); after 3 consecutive edit
                     rounds that each make the verify worse, restore the best-so-far files.
  read_streak_nudge  after 5 consecutive read-only turns with no write, tell the model to write a
                     first draft now (no tool_choice forcing).

This module is PURE apart from checkpoint file restore and prefetch file reads; it has no import of
the worker. The worker owns the wiring (all gated), the tests live in test-worker-budget-policy.py.
"""
import json
import os
import re
from pathlib import Path

POLICIES = ("progress_extend", "budget_visible", "read_window", "mask_stale_reads",
            "prefetch_excerpt", "checkpoint_revert", "read_streak_nudge")
ENV_VAR = "WORKER_BUDGET_POLICY"

EXTEND_STEP = 8
EXTEND_STEP_REFINE = 12
EXTEND_CEILING_FACTOR = 1.5
WRITE_RECENCY = 3          # a write/edit must have landed in the last N iterations
STALL_WINDOW = 3           # N identical verify tails in a row = stalled, refuse
READ_WINDOW_LINES = 100
MASK_KEEP_TURNS = 3
STREAK_NUDGE_AT = 5
STREAK_NUDGE_MAX = 2
REVERT_AFTER = 3
PREFETCH_MAX_CHARS = 6000
PREFETCH_HEAD_LINES = 40
PREFETCH_OUTLINE_MAX = 40
VERIFY_TAIL_CHARS = 1500

_WRITE_TOOLS = ("write_file", "edit_file", "str_replace")


# --------------------------------------------------------------------------- switch parsing
def parse_policy_list(raw):
    """-> (frozenset(known names), [unknown names]). Accepts 'a,b', 'a b', a list, 'all', 'none'."""
    if raw is None:
        return frozenset(), []
    if isinstance(raw, (list, tuple, set, frozenset)):
        parts = [str(x) for x in raw]
    else:
        parts = [str(raw)]
    names, unknown = set(), []
    for chunk in parts:
        for tok in re.split(r"[,\s]+", chunk.strip()):
            t = tok.strip().lower().replace("-", "_")
            if not t or t in ("none", "off", "default"):
                continue
            if t == "all":
                names.update(POLICIES)
            elif t in POLICIES:
                names.add(t)
            else:
                unknown.append(tok)
    return frozenset(names), unknown


_MARKER_RE = re.compile(r"^[ \t>*#-]*budget[- _]?policy[ \t]*:[ \t]*(.+?)[ \t]*$", re.I | re.M)


def task_policy_marker(task_text):
    """`Budget-policy: a,b` line(s) in the task text -> raw string or None."""
    found = _MARKER_RE.findall(task_text or "")
    return ",".join(found) if found else None


def resolve_policies(cli=None, env=None, task_text=None):
    """Precedence: explicit CLI value (even 'none') > task marker > env.
    -> (frozenset, unknown_names, source)."""
    if cli is not None:
        names, unk = parse_policy_list(cli)
        return names, unk, "cli"
    mk = task_policy_marker(task_text)
    if mk:
        names, unk = parse_policy_list(mk)
        return names, unk, "task"
    ev = os.environ.get(ENV_VAR) if env is None else env
    if ev:
        names, unk = parse_policy_list(ev)
        return names, unk, "env"
    return frozenset(), [], "default"


# --------------------------------------------------------------------------- verify helpers
_TIMING_RES = (
    re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|µs|us|ns|s|sec|secs|seconds|m)\b", re.I),
    re.compile(r"\b(?:duration|elapsed|time)[_a-z ]*[:=]\s*[\d.]+", re.I),
    re.compile(r"\d{4}-\d\d-\d\dT[\d:.]+Z?"),
    re.compile(r"\b\d{1,2}:\d\d:\d\d\b"),
)
_FAIL_COUNT_RES = (
    re.compile(r"(\d+)\s+(?:failed|failing|failures?)\b", re.I),
    re.compile(r"#\s*fail\s+(\d+)", re.I),
    re.compile(r"\bfailures?\s*[:=]\s*(\d+)", re.I),
    re.compile(r"(\d+)\s+errors?\b", re.I),
)


def verify_text_from_bash(result):
    """run_bash result string (JSON with exit_code/stdout/stderr, or an ERROR string) -> (text, ok)."""
    s = str(result)
    try:
        d = json.loads(s)
        if isinstance(d, dict) and "exit_code" in d:
            text = (d.get("stdout") or "") + (("\n" + d["stderr"]) if d.get("stderr") else "")
            return text, d.get("exit_code") == 0
    except Exception:
        pass
    return s, False


def normalize_verify(text, tail=VERIFY_TAIL_CHARS):
    """Tail of a verify output with timings/timestamps removed, so a re-run that only changed its
    durations compares IDENTICAL (no new information = no progress)."""
    t = (text or "")[-tail:]
    for r in _TIMING_RES:
        t = r.sub("#", t)
    return "\n".join(ln.rstrip() for ln in t.strip().splitlines())


def verify_score(text, ok, sig_fn=None):
    """Failure count of one verify output: 0 when it passed, else max(#recognized diagnostic lines,
    largest 'N failed' style count); None when unscorable (never guess)."""
    if ok:
        return 0
    n = 0
    if sig_fn is not None:
        try:
            n = len(sig_fn(text))
        except Exception:
            n = 0
    for r in _FAIL_COUNT_RES:
        for m in r.finditer(text or ""):
            try:
                n = max(n, int(m.group(1)))
            except Exception:
                pass
    return n if n > 0 else None


# --------------------------------------------------------------------------- read helpers
_READ_HDR = re.compile(r"\[read_file [^\]]*?lines (\d+)-(\d+) of (\d+)")


def read_stub(path, result):
    """One-line stub for an old read_file result."""
    s = str(result)
    m = _READ_HDR.search(s[:600])
    if m:
        a, b, tot = m.group(1), m.group(2), m.group(3)
        return f"[read {path} lines {a}-{b} of {tot}; omitted -- re-read if you need it]"
    n = len(s.splitlines())
    return f"[read {path} {n} lines 1-{n}; omitted -- re-read if you need it]"


# --------------------------------------------------------------------------- prefetch
_SPEC_RE = re.compile(r"^[ \t>*#-]*prefetch(?:[- _]?excerpt)?[ \t]*:[ \t]*(.+?)[ \t]*$", re.I | re.M)
_OUTLINE_RE = re.compile(
    r"^\s*(?:export\s+)?(?:default\s+)?(?:async\s+)?(?:def |class |function\b|const\s+\w+\s*=\s*(?:async\s*)?\(|"
    r"describe\(|it\(|test\(|interface |type \w+ =|enum |struct |func |fn |impl |pub )", re.M)


def prefetch_specs_from_text(text):
    out = []
    for raw in _SPEC_RE.findall(text or ""):
        for tok in re.split(r"[,\s]+", raw):
            tok = tok.strip().strip("`")
            if tok:
                out.append(tok)
    return out


def _split_spec(spec):
    m = re.match(r"^(.*?):(\d+)-(\d+)$", spec)
    if m:
        return m.group(1), int(m.group(2)), int(m.group(3))
    return spec, None, None


def build_prefetch(cwd, specs, max_chars=PREFETCH_MAX_CHARS):
    """-> prompt addendum ('' when nothing to show). Only files under cwd that exist; head + outline
    for a bare path, exact lines for PATH:A-B. Hard-capped at max_chars."""
    cwd = Path(cwd).resolve()
    chunks, seen = [], set()
    for spec in specs or []:
        rel, a, b = _split_spec(spec)
        if (rel, a, b) in seen:
            continue
        seen.add((rel, a, b))
        try:
            p = (cwd / rel).resolve()
            p.relative_to(cwd)
            if not p.is_file():
                continue
            lines = p.read_text(errors="replace").splitlines()
        except Exception:
            continue
        total = len(lines)
        if a is not None:
            a, b = max(1, a), min(total, max(a, b))
            body = "\n".join(lines[a - 1:b])
            chunks.append(f"### {rel} lines {a}-{b} of {total}\n{body}")
        else:
            head = "\n".join(lines[:PREFETCH_HEAD_LINES])
            outline = []
            for n, ln in enumerate(lines, 1):
                if n > PREFETCH_HEAD_LINES and _OUTLINE_RE.match(ln):
                    outline.append(f"{n}: {ln.strip()[:100]}")
                    if len(outline) >= PREFETCH_OUTLINE_MAX:
                        break
            sec = f"### {rel} ({total} lines) -- first {min(total, PREFETCH_HEAD_LINES)} lines\n{head}"
            if outline:
                sec += "\n--- outline (line: declaration) ---\n" + "\n".join(outline)
            chunks.append(sec)
    if not chunks:
        return ""
    text = "\n\n".join(chunks)
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "\n...[excerpt truncated]"
    return ("\n\n--- PREFETCHED EXCERPT (supplied by the harness so you can start writing sooner; "
            "use read_file for anything beyond it) ---\n" + text + "\n--- end excerpt ---")


# --------------------------------------------------------------------------- the state machine
class BudgetState:
    """Per-run state for the enabled policies. All methods are cheap and exception-safe at the call
    sites (the worker wraps them); none of them raise on odd input."""

    def __init__(self, policies, base_cap, refine=False, baseline_score=None, sig_fn=None):
        self.policies = frozenset(policies)
        self.base_cap = int(base_cap)
        self.refine = bool(refine)
        self.sig_fn = sig_fn
        # progress_extend
        self.write_iters = []
        self.verify_tails = []          # [(iteration, normalized tail)]
        self.granted = 0
        self.refused = None
        self.events = []
        # read tracking (mask_stale_reads / read_streak_nudge)
        self.reads = []                 # {path, it, idx, orig, edited_after, masked}
        self.tool_turns = []            # iterations that made at least one tool call
        self.masked_total = 0
        self.edit_seq = {}              # path -> iteration of last edit
        # turn flags
        self._turn = None
        self._turn_write = False
        self._turn_nonread = False
        self._turn_calls = 0
        self.read_streak = 0
        self.streak_nudges = 0
        # checkpoint_revert
        self.originals = {}             # path -> content or None (absent) before the first edit
        self.touched = []
        self.best = None                # {"score","it","files"}
        self.prev_score = baseline_score
        self.worse_streak = 0
        self.edits_since_verify = 0
        self.reverts = 0
        self.baseline = baseline_score
        self.cwd = None
        self.cur_score = None
        if baseline_score is not None:
            self.best = {"score": baseline_score, "it": 0, "files": None}   # None = originals

    def on(self, name):
        return name in self.policies

    # ---- per-turn
    def begin_turn(self, i):
        self._turn = i
        self._turn_write = False
        self._turn_nonread = False
        self._turn_calls = 0

    def before_tool(self, name, args, cwd):
        """Capture a file's pre-edit content (checkpoint_revert) BEFORE the tool runs."""
        if not self.on("checkpoint_revert") or name not in _WRITE_TOOLS or not isinstance(args, dict):
            return
        rel = str(args.get("path") or "")
        if not rel or rel in self.originals:
            return
        try:
            p = (Path(cwd) / rel)
            self.originals[rel] = p.read_text() if p.is_file() else None
            self.touched.append(rel)
        except Exception:
            self.originals.setdefault(rel, None)

    def observe_tool(self, i, name, args, result, own_verify=False, readonly=False, msg_index=None):
        res = str(result)
        ok_write = name in _WRITE_TOOLS and res.startswith("OK: ")
        self._turn_calls += 1
        if ok_write:
            self._turn_write = True
            self.write_iters.append(i)
            self.edits_since_verify += 1
            rel = str(args.get("path")) if isinstance(args, dict) else ""
            self.edit_seq[rel] = i
        elif not readonly:
            self._turn_nonread = True
        if name == "read_file" and isinstance(args, dict) and not res.startswith(("ERROR", "REFUSED")):
            self.reads.append({"path": str(args.get("path")), "it": i, "idx": msg_index,
                               "orig": res, "masked": False})
        if own_verify:
            text, ok = verify_text_from_bash(res)
            self.observe_verify(i, text, ok)

    def observe_verify(self, i, text, ok):
        self.verify_tails.append((i, normalize_verify(text)))
        score = verify_score(text, ok, self.sig_fn)
        if not self.on("checkpoint_revert"):
            return
        if score is None:
            return
        if self.edits_since_verify > 0 and self.prev_score is not None:
            if score > self.prev_score:
                self.worse_streak += 1
            else:
                self.worse_streak = 0
        elif self.edits_since_verify > 0:
            self.worse_streak = 0
        self.edits_since_verify = 0
        self.prev_score = score
        self.cur_score = score
        if self.best is None or score < self.best["score"]:
            self.best = {"score": score, "it": i, "files": self._snapshot_files()}

    def _snapshot_files(self):
        self_cwd = self.cwd
        snap = {}
        for rel in self.touched:
            try:
                p = Path(self_cwd) / rel
                snap[rel] = p.read_text() if p.is_file() else None
            except Exception:
                snap[rel] = None
        return snap

    def set_cwd(self, cwd):
        self.cwd = Path(cwd)

    # ---- progress_extend
    def _ceiling(self):
        return int(self.base_cap * EXTEND_CEILING_FACTOR)

    def extension_available(self, total_iters):
        return (self.on("progress_extend") and self.granted == 0 and self.refused is None
                and self._ceiling() > total_iters)

    def decide_extension(self, i, total_iters):
        """-> (extra:int, reason:str). Called when the loop is at its cap."""
        if self.granted:
            return 0, "already granted once"
        step = EXTEND_STEP_REFINE if self.refine else EXTEND_STEP
        room = self._ceiling() - total_iters
        if room <= 0:
            return 0, f"at hard ceiling {self._ceiling()} (1.5x cap {self.base_cap})"
        if not any(w > i - WRITE_RECENCY for w in self.write_iters):
            return 0, f"no write/edit in the last {WRITE_RECENCY} iterations"
        if not self.verify_tails:
            return 0, "no verify output seen (no progress signal)"
        tails = [t for _, t in self.verify_tails]
        if len(tails) >= STALL_WINDOW and len(set(tails[-STALL_WINDOW:])) == 1:
            return 0, f"last {STALL_WINDOW} verify outputs are identical (stalled)"
        if len(tails) >= 2 and tails[-1] == tails[-2]:
            return 0, "last verify output equals the previous one (no change)"
        return min(step, room), "write landed in last 3 iterations and verify output changed"

    def try_extend(self, i, total_iters):
        """At the cap: decide ONCE. -> (extra, note). Idempotent: after a grant or a refusal it
        returns (0, None) so the loop-condition fallback and end_turn can both call it."""
        if not self.on("progress_extend") or i < total_iters or self.granted or self.refused:
            return 0, None
        extra, why = self.decide_extension(i, total_iters)
        if extra:
            self.granted = extra
            self.events.append({"kind": "extension_granted", "iteration": i, "extra": extra,
                                "new_cap": total_iters + extra, "base_cap": self.base_cap, "reason": why})
            return extra, (f"[Budget extension granted: +{extra} iterations (cap {total_iters} -> "
                           f"{total_iters + extra}), because {why}. This is the only extension; "
                           f"finish and call task_complete.]")
        self.refused = why
        self.events.append({"kind": "extension_refused", "iteration": i, "reason": why})
        return 0, None

    # ---- end of turn: returns dict(extend, budget_line, notes)
    def end_turn(self, i, total_iters, cwd=None):
        out = {"extend": 0, "budget_line": None, "notes": []}
        had_tools = self._turn_calls > 0
        if had_tools:
            self.tool_turns.append(i)
        # read-streak
        if self.on("read_streak_nudge") and had_tools:
            if self._turn_write:
                self.read_streak = 0
            elif self._turn_nonread:
                self.read_streak = 0
            else:
                self.read_streak += 1
                if (self.read_streak % STREAK_NUDGE_AT == 0
                        and self.streak_nudges < STREAK_NUDGE_MAX):
                    self.streak_nudges += 1
                    out["notes"].append(
                        f"[Harness: your last {self.read_streak} turns only READ files and wrote "
                        f"nothing. Write a first draft now -- call write_file/edit_file with your "
                        f"best current attempt, even if incomplete. The verify output after it will "
                        f"show what to fix; more reading will not.]")
                    self.events.append({"kind": "read_streak_nudge", "iteration": i})
        # checkpoint revert
        if self.on("checkpoint_revert") and self.worse_streak >= REVERT_AFTER and self.best is not None \
                and self.prev_score is not None and self.prev_score > self.best["score"]:
            restored = self.restore_best(cwd or getattr(self, "cwd", None))
            if restored:
                self.reverts += 1
                bs = self.best
                self.events.append({"kind": "checkpoint_revert", "iteration": i, "best_score": bs["score"],
                                    "from_score": self.prev_score, "files": restored})
                out["notes"].append(
                    f"[Checkpoint revert: your last {REVERT_AFTER} edit rounds each made the verify WORSE "
                    f"({bs['score']} failure(s) at iteration {bs['it']} -> {self.prev_score} now). "
                    f"The harness restored {', '.join(restored)} to the best state seen. Do not "
                    f"repeat those edits; make a different, smaller change from here.]")
                self.prev_score = bs["score"]
                self.worse_streak = 0
                out["reverted"] = restored
        # progress_extend at the cap
        extra, note = self.try_extend(i, total_iters)
        if extra:
            out["extend"] = extra
            out["notes"].insert(0, note)
            total_iters += extra
        if self.on("budget_visible"):
            rem = max(0, total_iters - i)
            if self.on("progress_extend"):
                if self.granted:
                    ext = "extension used"
                elif self.extension_available(total_iters):
                    ext = (f"extension available (+{EXTEND_STEP_REFINE if self.refine else EXTEND_STEP} "
                           f"if edits land and verify output changes)")
                else:
                    ext = "no extension available"
            else:
                ext = "no extension available"
            out["budget_line"] = f"[Budget: iteration {i} of {total_iters}, {rem} left; {ext}.]"
        return out

    # ---- mask_stale_reads
    def mask_view(self, messages):
        """Copy of `messages` with stale read_file results replaced by stubs; the input is untouched."""
        if not self.on("mask_stale_reads") or not self.reads:
            return messages
        keep = set(self.tool_turns[-MASK_KEEP_TURNS:])
        cur = self._turn
        if cur is not None:
            keep.add(cur)
        view = None
        n = 0
        for r in self.reads:
            if r["it"] in keep:
                continue
            if self.edit_seq.get(r["path"], 0) >= r["it"]:
                continue        # edited since (or in that same turn): leave it alone
            idx = r.get("idx")
            if idx is None:
                continue
            cand = (idx, idx - 1, idx + 1, idx + 2, idx - 2)
            for j in cand:
                if j < 0 or j >= len(messages):
                    continue
                c = messages[j].get("content")
                if c == r["orig"] or c == f"[tool result for read_file]: {r['orig']}":
                    if view is None:
                        view = list(messages)
                    stub = read_stub(r["path"], r["orig"])
                    if c != r["orig"]:
                        stub = f"[tool result for read_file]: {stub}"
                    view[j] = dict(messages[j], content=stub)
                    n += 1
                    break
        self.masked_total = max(self.masked_total, n)
        return view if view is not None else messages

    # ---- checkpoint restore
    def restore_best(self, cwd):
        if cwd is None or self.best is None:
            return []
        files = self.best["files"]
        if files is None:
            files = self.originals
        done = []
        for rel in self.touched:
            if rel not in files:
                continue
            want = files[rel]
            try:
                p = Path(cwd) / rel
                if want is None:
                    if p.exists():
                        p.unlink()
                        done.append(rel)
                else:
                    if not p.is_file() or p.read_text() != want:
                        p.write_text(want)
                        done.append(rel)
            except Exception:
                pass
        return done

    # ---- metrics
    def metrics(self):
        m = {"budget_policy": sorted(self.policies), "budget_base_cap": self.base_cap}
        if self.on("progress_extend"):
            m["budget_extension_granted"] = self.granted
            if self.refused:
                m["budget_extension_refused"] = self.refused
        if self.on("mask_stale_reads"):
            m["budget_reads_masked"] = self.masked_total
        if self.on("checkpoint_revert"):
            m["budget_reverts"] = self.reverts
        if self.on("read_streak_nudge"):
            m["budget_streak_nudges"] = self.streak_nudges
        if self.events:
            m["budget_events"] = self.events[-20:]
        return m
