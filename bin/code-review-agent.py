#!/usr/bin/env python3
"""code-review-agent.py -- first-pass code review and code-only diagnosis on a
local model, with the harness enforcing what a prompt can only request.

WHY THIS SHAPE
--------------
The vault's review-bench history is the design spec, and it is a record of what
does not work:

  * v4/v5, qwen3.5:9b, blind whole-file review: **0/6 recall** across three
    seeded bug classes, and **23-45 findings on a CLEAN file**. Prompt design was
    declared exhausted at 9b. Precision, not recall, was called the bigger
    production blocker.
  * v5's headline "3/3 caught" was a SCORING ARTEFACT -- a regex matched a
    generic complaint that never mentioned the seeded change. Hence the standing
    rule: an answer key must require the seeded token itself.
  * The v7 diff-aware probe changed everything: blind whole-file 0-1/12 became
    7/8 seeded rows caught once the model saw a DIFF plus an intent claim.
    Diff-scoping is a precondition, not an optimisation.

So three things are moved out of the prompt and into the harness, because asking
a model to be disciplined does not make it disciplined:

  1. DIFF-SCOPED, INCLUDING REMOVALS. Reviewers fixate on added lines. The
     hardest seeded bug in the bench (`umask 077` deleted, `chmod 600` added
     after the write) is invisible unless you ask what the REMOVED line was
     protecting. The harness presents removals explicitly and asks that question
     directly.
  2. EVERY FINDING MUST QUOTE A REAL LINE. The quote is checked as a literal
     substring of the diff. A finding about code that is not there dies before a
     human sees it -- the same anti-fabrication check that worked in the research
     orchestrator.
  3. EVERY FINDING MUST SUPPLY A CONCRETE FAILURE SCENARIO, and a separate
     adversarial pass then tries to knock it down. This is the precision lever:
     a false positive can usually be asserted but not demonstrated. "This looks
     risky" cannot produce inputs that make it fail; a real off-by-one can.

MODES
-----
  review    -- a diff, an intent claim, and context -> ranked findings
  diagnose  -- a symptom plus the code -> ranked candidate root causes, each
               with the evidence that would confirm or refute it
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
import sys as _sys, os as _os
_sys.path.insert(0, _os.path.dirname(_os.path.abspath(__file__)))
import darkbloom_chat as _dbk  # DARKBLOOM: OpenAI-protocol adapter for the local Darkbloom lane
from datetime import datetime, timezone
from pathlib import Path

DEFAULT_HOST = _dbk.default_host()  # DARKBLOOM: local lane (Studio Ollama retired); F1: sticky, never the legacy fallback while Darkbloom is configured


def now() -> str:
    return datetime.now(timezone.utc).strftime("%H:%M:%S")


def log(msg: str) -> None:
    print(f"[review {now()}] {msg}", flush=True)


_WS = re.compile(r"\s+")


def normalise(s: str) -> str:
    return _WS.sub(" ", s or "").strip()


# ------------------------------------------------------------------- model

PROMPT_CHARS_PER_TOKEN = 3.5   # measured 3.84-4.07 on the live tiers; lower = safer
CTX_SAFETY_TOKENS = 256        # chat template + role tokens + estimate error
MIN_NUM_PREDICT = 512          # never starve the answer below this


def clamp_num_predict(num_predict: int, num_ctx: int, system: str, user: str,
                      schema=None) -> int:
    """num_predict, capped so prompt + output fits num_ctx (Ollama path only).
    The prompt size is ESTIMATED before the call (chars / 3.5 overestimates the
    measured 3.8-4.1 chars/token), plus the JSON schema Ollama injects for format.
    Floors at MIN_NUM_PREDICT: a prompt that already overflows is a separate bug,
    and an answer of zero tokens would hide it as an empty review."""
    if not num_ctx or num_ctx <= 0:
        return num_predict
    chars = len(system or "") + len(user or "")
    if schema is not None:
        chars += len(json.dumps(schema))
    est = int(chars / PROMPT_CHARS_PER_TOKEN) + 1
    room = int(num_ctx) - est - CTX_SAFETY_TOKENS
    return max(min(int(num_predict), room), min(int(num_predict), MIN_NUM_PREDICT))


class Model:
    def __init__(self, host: str, model: str, num_ctx: int, timeout: int = 1200,
                 think: bool = False):
        self.host = host.rstrip("/")
        self.model = model
        self.num_ctx = num_ctx
        self.timeout = timeout
        self.think = think
        self.think_supported = True
        self.calls = 0
        self.total_s = 0.0
        self.truncated = 0
        # Per-call token accounting, persisted to run.json as `call_usage`, so a
        # prompt that overflows num_ctx is visible instead of silently truncated.
        self.usage: list[dict] = []

    def _note_usage(self, prompt_tokens, completion_tokens, num_predict, finish):
        p, c = int(prompt_tokens or 0), int(completion_tokens or 0)
        self.usage.append({"prompt_tokens": p, "completion_tokens": c,
                           "num_predict": num_predict, "num_ctx": self.num_ctx,
                           "finish": finish,
                           "headroom": self.num_ctx - p - c,
                           "fits_with_full_output": p + num_predict <= self.num_ctx})

    def chat(self, system: str, user: str, schema: dict | None = None,
             temperature: float = 0.0, num_predict: int = 8000) -> str:
        # num_predict defaults to 8000, not a token or two. The vault records
        # that reasoning models on this bench returned EMPTY responses until the
        # budget was raised past 8000 -- the thinking trace consumed all of it,
        # and the initial "NONE" results were that, not model failures. Same
        # root cause as the research harness's empty-content bug.
        # DARKBLOOM: the local lane speaks OpenAI /v1, not Ollama /api/chat.
        _mis = (not _dbk.is_darkbloom(self.host)) and _dbk.legacy_ollama_misroute(self.host)
        if _mis:   # F1: fail loudly, never send a review down the wrong protocol
            raise RuntimeError(_mis)
        if _dbk.is_darkbloom(self.host):
            _t0 = time.time()
            _p0 = _dbk.STATS.get("prompt_tokens", 0)
            _c0 = _dbk.STATS.get("completion_tokens", 0)
            # CONTRACT (2026-10-05, regate 05cdd18d74e0/2fe073f6041c/954d2aeba164/
            # 50e6b9e487b0): an unparseable/schema-violating answer is a PARSE
            # FAILURE for the stage to handle (chat_json -> None), exactly as on the
            # Ollama path. _dbk.chat raises RuntimeError("darkbloom: schema rungs
            # exhausted ...") for that case instead, and nothing caught it: ONE bad
            # sample (mis-escaped code inside a JSON `quote`) killed the whole run
            # with exit 1 and no report/run.json -> queue "worker produced no
            # iterations". Transport/HTTP errors still raise (a real outage).
            try:
                content, _fin = _dbk.chat(self.host, self.model, system, user, schema=schema,
                                          schema_start=getattr(self, "schema_start", None),
                                          reasoning_effort=getattr(self, "reasoning_effort", None),
                                          # None => the review profile from the model card
                                          # (model_profiles.yaml); the stage's own 0.0 / num_predict
                                          # were hardcoded defaults, not explicit operator choices.
                                          temperature=None, max_tokens=None,
                                          think=self.think, timeout=self.timeout, role="review")
            except RuntimeError as _e:
                if not str(_e).startswith("darkbloom: schema rungs exhausted"):
                    raise
                self.calls += 1
                self.total_s += time.time() - _t0
                self.schema_exhausted = getattr(self, "schema_exhausted", 0) + 1
                log(f"  [model] unusable structured output (counted as a parse failure): "
                    f"{str(_e)[:160]}")
                return ""
            self._note_usage(_dbk.STATS.get("prompt_tokens", 0) - _p0,
                             _dbk.STATS.get("completion_tokens", 0) - _c0, num_predict, _fin)
            self.calls += 1
            self.total_s += time.time() - _t0
            if _fin == "length":
                self.truncated += 1
                log(f"  [model] hit the {num_predict}-token cap -- output truncated")
            return content
        # OLLAMA: clamp num_predict to what is LEFT of the window after the prompt.
        # At the pregate tier (qwen3:14b, num_ctx 6144) a ~3k-token prompt plus the
        # 8000 (review/removal) or 4000 (verify) allowance can never fit; a long
        # answer then makes Ollama context-shift, silently dropping the START of the
        # prompt (system rules + diff header) mid-generation. Clamping makes it stop
        # with done_reason=length instead -- counted and logged as truncation below.
        _asked = num_predict
        num_predict = clamp_num_predict(num_predict, self.num_ctx, system, user, schema)
        if num_predict < _asked:
            log(f"  [model] num_predict {_asked} -> {num_predict} to fit num_ctx {self.num_ctx}")
        body = {
            "model": self.model,
            "messages": [{"role": "system", "content": system},
                         {"role": "user", "content": user}],
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": self.num_ctx,
                        "num_predict": num_predict},
            "keep_alive": "30m",
        }
        if self.think_supported:
            body["think"] = self.think
        if schema is not None:
            body["format"] = schema
        req = urllib.request.Request(
            f"{self.host}/api/chat", data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json"})
        t0 = time.time()
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                data = json.loads(resp.read())
        except urllib.error.HTTPError as e:
            detail = e.read().decode(errors="replace")[:300]
            if self.think_supported and "think" in detail.lower():
                log(f"  [model] {self.model} rejects the think parameter -- dropping it")
                self.think_supported = False
                return self.chat(system, user, schema, temperature, num_predict)
            raise RuntimeError(f"ollama HTTP {e.code}: {detail}") from None
        self.calls += 1
        self.total_s += time.time() - t0
        self._note_usage(data.get("prompt_eval_count"), data.get("eval_count"),
                         num_predict, data.get("done_reason"))
        if data.get("done_reason") == "length":
            self.truncated += 1
            log(f"  [model] hit the {num_predict}-token cap -- output truncated")
        msg = data.get("message", {})
        content = msg.get("content", "")
        if not content and msg.get("thinking"):
            log(f"  [model] EMPTY content with {len(msg['thinking'])} chars of "
                f"thinking -- reasoning ate the whole budget")
        return content

    def chat_json(self, system: str, user: str, schema: dict,
                  temperature: float = 0.0, num_predict: int = 8000) -> dict | None:
        raw = self.chat(system, user, schema, temperature, num_predict)
        try:
            return json.loads(raw)
        except Exception:
            m = re.search(r"\{.*\}", raw, re.S)
            if m:
                try:
                    return json.loads(m.group(0))
                except Exception:
                    pass
        return None


# ------------------------------------------------------------------- diffs

class Hunk:
    def __init__(self, path: str, header: str, lines: list[str]):
        self.path = path
        self.header = header
        self.lines = lines

    @property
    def added(self) -> list[str]:
        return [l[1:] for l in self.lines if l.startswith("+") and not l.startswith("+++")]

    @property
    def removed(self) -> list[str]:
        return [l[1:] for l in self.lines if l.startswith("-") and not l.startswith("---")]

    def text(self) -> str:
        return "\n".join(self.lines)


# Generated / vendored files: reviewing them is noise by construction -- their
# churn is a build product, not an authored change. Added 2026-08-31 after the
# FIRST false positive on real churn: dropped_members() fired on
# tsconfig.tsbuildinfo, reporting two content hashes "silently dropped from a
# literal collection". The 0.00 FP rate that preceded this was measured on 9
# synthetic decoys, all hand-written source -- the fixture set contained no
# generated file, so the gap was invisible to the bench.
_GENERATED = re.compile(r"""(?xi)
    (^|/)(node_modules|vendor|dist|build|out|\.next|coverage|__snapshots__)/
  | (^|/)[^/]*\.(tsbuildinfo|min\.js|min\.css|map|lock)$
  | (^|/)(package-lock\.json|yarn\.lock|pnpm-lock\.yaml|poetry\.lock
        |Cargo\.lock|composer\.lock|go\.sum|Gemfile\.lock)$
  | (^|/)[^/]*\.(pb|generated|g)\.(go|py|ts|js|cs|java)$
""")

def is_generated(path: str) -> bool:
    return bool(_GENERATED.search((path or "").strip()))


def drop_generated(hunks):
    """(kept, dropped_paths). Never returns an empty list when the diff had
    ONLY generated files -- an all-generated diff is reported as such by the
    caller rather than silently reviewed or silently skipped."""
    kept, dropped = [], []
    for h in hunks:
        if is_generated(getattr(h, "path", "")):
            dropped.append(h.path)
        else:
            kept.append(h)
    return kept, sorted(set(dropped))


def parse_diff(diff: str) -> list[Hunk]:
    hunks: list[Hunk] = []
    path, header, buf = "", "", []
    for line in diff.splitlines():
        if line.startswith("+++ "):
            # A `+++` line starts a NEW FILE. The previous file's LAST hunk is
            # still sitting in `buf` (it is only flushed when the next `@@`
            # arrives), so flush it HERE under the OLD path before switching --
            # otherwise that hunk gets re-attributed to the file that follows.
            # This off-by-one is how a 596-hash tsconfig.tsbuildinfo hunk was
            # relabelled onto verify_impl.mjs, where dropped_members() then
            # manufactured 23 phantom "silently dropped from a literal" defects.
            if buf:
                hunks.append(Hunk(path, header, buf))
            header, buf = "", []
            # `diff -u` appends a tab and a timestamp to the filename; git does
            # not. Split on tab first so the path is not "file.py\t2026-08-30...".
            path = line[4:].split("\t")[0].strip()
            if path.startswith("b/"):
                path = path[2:]
            continue
        if line.startswith("--- ") or line.startswith("diff "):
            continue
        if line.startswith("@@"):
            if buf:
                hunks.append(Hunk(path, header, buf))
            header, buf = line, []
            continue
        if header:
            buf.append(line)
    if buf:
        hunks.append(Hunk(path, header, buf))
    return hunks


def diff_corpus(diff: str) -> str:
    """Every code line the diff touches, normalised, for quote verification.

    Context lines count: a finding may legitimately quote unchanged code that
    the change interacts with. What must NOT verify is a quote of code that
    appears nowhere in the diff at all.
    """
    out = []
    for line in diff.splitlines():
        if line.startswith(("+++", "---", "@@", "diff ")):
            continue
        out.append(squash_quotes(normalise(line[1:] if line[:1] in "+- " else line)))
    # Joined with spaces, not newlines, and normalised as one blob -- the quote
    # is whitespace-normalised too, so both sides must collapse line breaks the
    # same way. They did not: a model quoting two adjacent deleted lines as
    # "if not tracking_number:\n    raise ValueError(...)" normalised to a
    # single spaced string, while the corpus still had a newline between them,
    # so a perfectly real multi-line quote failed grounding. That is how
    # bug-guard was scored as "found nothing" when the removal pass had
    # correctly found it twice.
    return " ".join(out)


_LINENO = re.compile(r"^\s*\d+\s*\|\s?", re.M)


_DOUBLED_QUOTE = re.compile(r'("{2,}|\'{2,})')


def squash_quotes(text: str) -> str:
    """Collapse runs of repeated quote characters to a single one.

    A JSON-escaping artefact, not fabrication: models emit
    `chmod 600 ""$DOC_FILE""` where the source says `chmod 600 "$DOC_FILE"`.
    Applied to BOTH the quote and the corpus so the comparison is symmetric.
    Found only because rejected quotes are now recorded -- the same five
    candidates had been discarded silently twice, first as "the model produced
    nothing", then as "the gutter bug", and the gutter was only half of it.
    """
    return _DOUBLED_QUOTE.sub(lambda m: m.group(0)[0], text)


def strip_line_numbers(text: str) -> str:
    """Remove the `  377| ` gutter the harness itself adds.

    Diagnosis shows a narrowed excerpt WITH line numbers so a cause can cite a
    findable location, but grounding compares against the raw file. The model
    quotes what it was shown, prefix and all, so every quote failed grounding --
    on the Unraid umask fixture the model produced FIVE candidates and all five
    were discarded, which read as "no candidates at all" in the score table.
    The harness added the prefix; the harness removes it.
    """
    return _LINENO.sub("", text)


_IDENT_TOK_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _ident_like(t: str) -> bool:
    """An identifier, not an English word: >=6 chars and an inner capital, an
    underscore or a digit (OrderEgiftLink, order_egift, link2)."""
    return (len(t) >= 6 and (any(c.isupper() for c in t[1:]) or "_" in t
                             or any(c.isdigit() for c in t)))


def _near_ident(h: str, w: str) -> bool:
    """Same near-miss rule as gate-on-complete._near_miss_feedback and the
    escalation watcher's _near, so all three agree on what a misspelling is."""
    hl, wl = h.lower(), w.lower()
    return (hl != wl and abs(len(h) - len(w)) <= 2 and hl[:3] == wl[:3]
            and wl not in hl and hl not in wl
            and difflib.SequenceMatcher(None, hl, wl).ratio() >= 0.8)


def claim_identifier_drift(text: str, corpus: str, intent: str = "") -> list:
    """PURE. [(written, real)] for each BACKTICKED identifier in a finding's claim /
    failure scenario that occurs in neither the diff corpus nor the task intent but
    is a near-miss of an identifier that IS in the corpus.

    WHY (2026-10-06 gate audit; rt-egift-link-s1 s0): a local reviewer read
    `OrderEgiftLink` in the code and wrote `OrderEgmtLink` in its finding -- the
    quote was grounded (quote_is_real passed on the real line) but the CLAIM was
    about a name that exists nowhere. The literal-drift audit the escalation
    watcher runs on escalation reviews never covered gate findings. A finding
    whose argument rests on a misspelt identifier is transcription drift, not a
    defect: it is dropped like an ungrounded quote and counted
    (stats['identifier_drift_rejected']). A spec identifier legitimately absent
    from the code (named in the intent) is NOT drift."""
    have = set(_IDENT_TOK_RE.findall(corpus or ""))
    spec = set(_IDENT_TOK_RE.findall(intent or ""))
    # A name that exists in the changed files' FULL base/new source (outside the
    # hunks) is real, not drift. REF_SOURCES is filled by main() when a repo is known.
    try:
        for _b, _n in (REF_SOURCES or {}).values():
            spec |= set(_IDENT_TOK_RE.findall((_b or "") + "\n" + (_n or "")))
    except Exception:
        pass
    out = []
    for span in re.findall(r"`([^`\n]{1,120})`", text or ""):
        for t in _IDENT_TOK_RE.findall(span):
            if not _ident_like(t) or t in have or t in spec:
                continue
            near = next((h for h in sorted(have) if _ident_like(h) and _near_ident(t, h)), None)
            if near and (t, near) not in out:
                out.append((t, near))
    return out


def quote_is_real(quote: str, corpus: str) -> bool:
    """Is this quote actually present in the diff?

    The load-bearing anti-fabrication check. A model that invents a plausible
    line of code -- confirmed live on this exact model family, which invented a
    JSON-object case for a field the schema types as String? -- fails here and
    the finding is dropped before a human ever reads it.

    Whitespace-normalised substring, then a 25-character prefix for quotes the
    model truncated. Short quotes are rejected outright: a 12-character
    fragment can match by accident and proves nothing about whether the model
    was looking at the real code.
    """
    q = normalise(squash_quotes(strip_line_numbers(quote)))
    # 8 chars and >=2 tokens, not 12. The threshold has to admit the real short
    # lines that matter -- `umask 077` is 9 characters and is the entire subject
    # of the hardest bug in the bench; a 12-char floor silently discarded every
    # correct finding about it. Requiring two tokens still rejects a bare
    # keyword, which is the thing a length floor was actually guarding against.
    # Two tokens, OR one long enough to be distinctive. The bare 2-token rule
    # rejected legitimate single-token lines -- `BACKUP_EXISTS=false` is 19
    # characters and unmistakably real code, but has no space in it. The point
    # was always to reject a bare keyword ("the", "return"), and a length floor
    # does that without discarding assignments.
    if len(q) < 8 or (len(q.split()) < 2 and len(q) < 15):
        return False
    if q in corpus:
        return True
    return len(q) >= 25 and q[:25] in corpus


# A model finding whose thrust is "the removed line was the SOLE/ONLY mechanism
# for X" or "the deleted line was the ... that did X". This is a TOTALITY claim
# about a deletion: it is only true if nothing on the added side still does X.
# It is NOT the same as "the new logic may be wrong" -- that questions the
# replacement and is a legitimate class we must not touch. So the pattern is
# deliberately narrow: an exclusivity marker (sole/only) tied to a mechanism
# noun, or the literal "the deleted line was the ...", or "silently/only
# removed". Measured against the cce40aa3eeba gate findings ("The deleted line
# was the sole mechanism that added an order ID to `creditedOrderIds`") -- both
# match -- and against the sibling review finding that merely doubts the new
# impl ("this logic is removed and replaced with a different implementation that
# may not correctly track") -- which must NOT match.
_SOLE_MECHANISM_CLAIM = re.compile(r"""(?ix)
    \b(sole|only)\b [^.\n]{0,50}? \b(mechanism|means|place|way|logic|guard|
        check|thing|reason|path|code|line|point|handler)\b
  | \bthe\ deleted\ line\ was\ the\b
  | \b(was|were)\ the\ (sole|only)\b
  | \bsilently\ (removed|deleted|dropped)\b
""")


def _line_similar(a: str, b: str) -> float:
    """Sequence ratio OR token overlap, whichever is higher -- module-level
    twin of the helper inside stage_removals(), so the post-grounding filter can
    reuse the same notion of "a removed line and an added line are two spellings
    of the same code."
    """
    import difflib
    seq = difflib.SequenceMatcher(None, a, b).ratio()
    ta = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", a))
    tb = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", b))
    jac = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
    return max(seq, jac)


# A removed line that reappears verbatim, or has a structural twin, on the added
# side of the SAME hunk was REPLACED, not deleted -- so any "sole mechanism /
# now gone" claim about it is false. Measured on the cce40aa3eeba remove-and-
# replace pairs (`creditedOrderIds.add(x.id)` -> `creditTrackingForOrder(x.id,
# ...)`): similarity 0.55-0.58. A genuine deletion (`umask 077` removed, only an
# unrelated `chmod 600` added) scores 0.20. 0.5 sits in that gap, closer to the
# genuine-deletion floor so we stay conservative -- a real deletion is never
# read as a replacement. The verbatim-anywhere check is retained from the older
# moved-not-deleted filter so a line MOVED to a distant hunk is still covered.
_REPLACEMENT_SIM = 0.5


def removed_line_was_replaced(quote: str, hunks: list) -> bool:
    """Does the quoted REMOVED line have a replacement on the added side?

    True when the quote grounds to a `-` line that either (a) reappears verbatim
    anywhere on the added side of the diff, or (b) has a same-hunk `+` twin whose
    similarity clears _REPLACEMENT_SIM. False for a genuine deletion (removed,
    nothing similar re-added), which stays catchable.
    """
    q = normalise(squash_quotes(strip_line_numbers(quote)))
    if len(q) < 8:
        return False
    all_added = [normalise(a) for h in hunks for a in h.added if a.strip()]
    all_added_set = {a for a in all_added if a}
    for h in hunks:
        removed = [normalise(r) for r in h.removed if r.strip()]
        # Which removed line(s) in THIS hunk does the quote refer to?
        homes = [r for r in removed
                 if r and (q in r or r in q or (len(q) >= 25 and q[:25] in r))]
        if not homes:
            continue
        adds = [normalise(a) for a in h.added if a.strip()]
        for r in homes:
            # (a) same removed line re-added verbatim anywhere -> moved, not gone.
            if r in all_added_set:
                return True
            # (b) a same-hunk added line is a structural twin -> replaced.
            if any(_line_similar(r, a) >= _REPLACEMENT_SIM for a in adds):
                return True
    return False


def overturns_sole_mechanism_claim(finding: dict, hunks: list) -> bool:
    """A model finding asserts a removed line was the SOLE/ONLY mechanism for
    something (or was `the deleted line was the ...`) -- but a replacement for
    that line is present on the added side of the same hunk. The premise is
    therefore false: the mechanism was renamed/rewritten, not deleted.

    Harness findings (source `harness-*`) are deterministic facts and are never
    subject to this; only model findings (review / removal-pass) are.
    """
    if str(finding.get("source", "")).startswith("harness-"):
        return False
    claim = (finding.get("claim") or "")
    if not _SOLE_MECHANISM_CLAIM.search(claim):
        return False
    return removed_line_was_replaced(finding.get("quote") or "", hunks)


_CMD_WORD = re.compile(r"^[A-Za-z_][A-Za-z0-9_.-]*$")
# Control-flow and declaration keywords are not commands. Without this the rule
# read `if placed >= cutoff:` vs `if cutoff <= placed:` as the binary `if`
# invoked with two different subcommands, and fired on a benign reordering --
# a regression the original fixtures caught the moment the rule was widened.
_NOT_A_COMMAND = {
    "if", "elif", "else", "fi", "then", "while", "until", "for", "do", "done",
    "case", "esac", "function", "return", "break", "continue", "def", "class",
    "import", "from", "with", "try", "except", "finally", "raise", "assert",
    "lambda", "yield", "pass", "and", "or", "not", "in", "is", "local",
    "export", "declare", "readonly", "let", "const", "var", "func", "type",
}


def rank_sections(code: str, symptom: str, context: str, size: int,
                  k: int) -> tuple[str, bool]:
    """Narrow a large file to the sections a symptom actually implicates.

    Handing the model a whole file works while the file is small. Measured:
    diagnosis found the true cause at rank #1 on two ~25-line Python fixtures
    and returned ZERO candidates on a 452-line bash script -- nothing was
    filtered by the harness, the model simply produced nothing. A reader given
    450 lines and one vague symptom has no idea where to look, and neither does
    a model.

    Scoring is lexical and needs no model call: rare words from the symptom and
    context weigh more than common ones, so a section that merely repeats
    "the script" cannot win. Line numbers are preserved in the output, because
    a diagnosis that cites a line the reader cannot find is not much use.
    """
    lines = code.splitlines()
    if len(code) <= size:
        return code, False
    # Windows of `span` lines advancing by `step`, with a SMALL overlap so a
    # cause spanning a seam is not split. The first version used span = 2*step
    # with stride step -- every line appeared twice -- and with line-number
    # prefixes the "narrowed" excerpt came out at 24,934 characters from an
    # 18,506-character file. Narrowing that enlarges the input is worse than
    # not narrowing at all, and the totals below are asserted, not assumed.
    span = max(30, size // 90)
    step = max(15, span - 8)
    windows = []
    for i in range(0, len(lines), step):
        chunk = lines[i:i + span]
        if chunk:
            windows.append((i + 1, "\n".join(chunk)))
    terms = set(re.findall(r"[a-z0-9_][a-z0-9_.]{3,}",
                           (symptom + " " + context).lower()))
    if not terms:
        return code[:size], True
    df = {t: sum(1 for _, w in windows if t in w.lower()) or 1 for t in terms}
    scored = []
    for idx, (start, w) in enumerate(windows):
        low = w.lower()
        score = sum((low.count(t) ** 0.5) * (len(windows) / df[t]) for t in terms)
        scored.append((score, idx, start, w))
    scored.sort(reverse=True)
    # Trim by SCORE first, then emit in file order. Emitting in file order and
    # breaking on the first over-budget section spent the budget on earlier,
    # lower-ranked windows and dropped the rank-1 window entirely -- on the
    # umask fixture the ranking put the section containing BOTH `chmod` and
    # `$DOC_FILE` first, and the emitter then threw it away. The ranking was
    # never the problem; the truncation order was.
    chosen = scored[:k]
    while chosen:
        total = sum(len(w) + 40 for _, _, _, w in chosen)
        if total <= size:
            break
        chosen.pop()                    # drop the lowest-scoring survivor
    if not chosen:
        chosen = scored[:1]

    out, seen = [], set()
    for _, _, start, w in sorted(chosen, key=lambda x: x[2]):
        keep = [(start + n, l) for n, l in enumerate(w.splitlines())
                if (start + n) not in seen]
        if not keep:
            continue
        seen.update(n for n, _ in keep)
        body = "\n".join(f"{n:5d}| {l}" for n, l in keep)
        out.append(f"--- lines {keep[0][0]}-{keep[-1][0]} ---\n{body}")
    joined = "\n\n".join(out)
    # Never hand back more than the original; if we would, send the file whole.
    if len(joined) >= len(code):
        return code, False
    return joined, True


# Guard/cleanup vocabulary: constructs whose whole purpose is to prevent
# something. Deliberately generic -- permissions, locking, validation, early
# exit, cleanup -- not a list of the bench's fixtures.
_PROTECTIVE = re.compile(
    r"\b(umask|chmod|chown|chgrp|setfacl|lock|mutex|acquire|semaphore|"
    r"assert|raise|abort|validate|verify|sanitiz|escape|"
    r"return\s+(False|None|0|-1)|if\s+not\b|if\s*!|guard|check|"
    r"close|rollback|release|cleanup|finally|defer)\b"
    # A length/bounds comparison is a guard even with no guard-ish keyword:
    # `if len(name) < _MIN_CONTAINMENT_LEN or len(tracked) < ...`.
    r"|\blen\s*\([^)]*\)\s*[<>]=?", re.I)


def deletes_a_protection(finding: dict, require_deleted: bool = True) -> bool:
    """Does this finding concern the outright deletion of a guard?

    Equivalence for a deleted protection is an ORDERING question, not a value
    question, and weak verifiers get it wrong in a specific, repeatable way:
    they point at a replacement that exists but runs LATER than the thing it
    protects. Measured twice -- qwen3-coder at v1 ("the script already ensures
    secure permissions through explicit chmod 600", ignoring that the chmod runs
    after the write) and qwen3:14b again here, on the same fixture.

    So the harness stops letting a verifier close this class on its own. A
    deleted guard floors at UNSURE and reaches a human.
    """
    if not _PROTECTIVE.search(finding.get("quote", "")):
        return False
    if not require_deleted:
        return True
    # The line must be OUTRIGHT DELETED, not rewritten. Without this the floor
    # would fire on benign refactors of the same guard -- `ok-relink` rewrites
    # the length check as `min(len(a), len(b)) < CONST`, and flooring a
    # correctly-rejected finding there would manufacture a false positive on a
    # clean file, which is the one thing this tool must not do.
    # Read +/- lines straight out of the stored text. parse_diff() cannot be
    # used here: `hunk_text` is the REVIEW PROMPT's rendering -- "FILE: x",
    # "HUNK: @@ ...", markdown fences -- so it contains no bare `@@` header,
    # parse_diff returned zero hunks, and the floor silently never fired. That
    # is the eighth time a format mismatch made a working rule invisible.
    raw = finding.get("hunk_text", "")
    removed_lines, added_lines = [], []
    for line in raw.splitlines():
        if line.startswith(("+++", "---", "@@", "```", "FILE:", "HUNK:", "diff ")):
            continue
        if line.startswith("-"):
            removed_lines.append(line[1:])
        elif line.startswith("+"):
            added_lines.append(line[1:])
    q = normalise(finding.get("quote", ""))
    for _ in (0,):
        added_blob = " ".join(added_lines)
        added_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{5,}", added_blob))
        for r in (x.strip() for x in removed_lines if x.strip()):
            if normalise(r) not in q and q not in normalise(r):
                continue
            # A guard whose own distinctive identifiers still appear in the added
            # code was MOVED, not deleted. Textual similarity is the wrong test
            # here: `ok-relink` rewrites
            #   if len(name) < _MIN_CONTAINMENT_LEN or len(tracked) < ...
            # as
            #   too_short = min(len(name), len(tracked)) < _MIN_CONTAINMENT_LEN
            # which scores far below any sane similarity threshold while
            # preserving the guard exactly. `_MIN_CONTAINMENT_LEN` surviving is
            # the signal; without this the floor would fire on a clean file.
            # Language keywords are not identifiers. A quote spanning
            #     if not _within_window(o["placed_at"], now):
            #         continue
            # yields `continue` as a "distinctive token"; it never reappears in
            # the rewritten form, so the guard read as deleted when in fact
            # `_within_window` and `placed_at` both survived inside the new
            # helper. That produced a FALSE POSITIVE ON A CLEAN FILE -- the one
            # outcome this tool must never have.
            # The removed LINE must itself be protective. A quote spanning
            #     if not _within_window(o["placed_at"], now):
            #         continue
            # also substring-matches the bare `continue`, which has no
            # identifiers at all -- and "no identifiers survived" was being read
            # as "the guard was deleted". That produced a FALSE POSITIVE ON A
            # CLEAN FILE, the one outcome this tool must never have. A
            # continuation line is not a guard; only the line carrying the
            # protective construct is.
            if not _PROTECTIVE.search(r):
                continue
            removed_tokens = {t for t in re.findall(r"[A-Za-z_][A-Za-z0-9_]{5,}", r)
                              if t.lower() not in _NOT_A_COMMAND}
            if removed_tokens and removed_tokens <= added_tokens:
                continue                      # the guard survived, renamed/moved
            return True
    return False


def probe_is_unsound(probe: str) -> str | None:
    """Reject a probe that reasons from something it made up.

    Measured on qwen3:14b: "Testing with name='a' and tracked='b', both shorter
    than _MIN_CONTAINMENT_LEN (assumed to be 3)". The constant is 20 and is
    visible in the diff. A probe built on an invented value is not evidence, and
    a verdict resting on it must not be trusted -- the whole point of requiring
    a probe was to force checking rather than asserting.
    """
    m = re.search(r"\b(assum\w*|guess\w*|presum\w*|suppose)\b", probe, re.I)
    if m:
        return m.group(0)
    # A probe that ADMITS it could not see the code its conclusion rests on is
    # reasoning from a guess too (live 2026-10-02, s5 96a36dee6870: "We don't see
    # its implementation ... Without seeing the old helper, we cannot confirm
    # this" -- then REAL_DEFECT, failing the gate on a false VS_GATEWAY claim).
    m = _BLIND_SPOT.search(probe)
    return m.group(0) if m else None


_BLIND_SPOT = re.compile(
    r"(can(?:no|')t (?:see|confirm|verify|tell)|without (?:seeing|the source|its source|"
    r"the (?:implementation|definition|body))|(?:do(?:es)? not|don't|doesn't) (?:see|show|have) "
    r"(?:its|the|their) (?:implementation|definition|body|source)|not (?:shown|visible) "
    r"in (?:the|this) (?:diff|hunk))", re.I)


def referenced_definitions(hunk_text: str, base_src: str, new_src: str,
                           budget: int = 6000) -> str:
    """Definitions of functions the hunk CALLS (or removed calls to) that are
    defined OUTSIDE the hunk, from the base and new versions of the file.

    The verifier sees one diff hunk. A claim like "the inline call omits the
    REQUIRED VS_GATEWAY argument that the removed helper passed" (s5, 2026-10-02)
    or "pickActiveWorkOrder throws on null" (s4) is a claim about code it was
    never shown, and both were wrong. Showing the referenced bodies turns the
    guess into a check. Pure: no I/O; empty string when nothing resolves."""
    called = []
    for name in re.findall(r"\b([A-Za-z_][A-Za-z0-9_]{2,})\s*(?:<[^()]*?>)?\s*\(", hunk_text):
        if name in called or name in _NOT_CALLS:
            continue
        called.append(name)
    out, used = [], 0
    for name in called:
        for label, src in (("base (before this change)", base_src),
                           ("after this change", new_src)):
            body = _definition_of(name, src)
            if not body or body in hunk_text:
                continue
            block = f"--- {name} [{label}] ---\n{body}\n"
            if used + len(block) > budget:
                return "".join(out)
            out.append(block)
            used += len(block)
            break           # one version per name is enough; base wins (it was removed)
    return "".join(out)


_NOT_CALLS = {"if", "for", "while", "switch", "catch", "return", "function", "await",
              "typeof", "new", "String", "Number", "Boolean", "Array", "Object", "JSON",
              "Promise", "Error", "Date", "Math", "print", "len", "str", "int", "isinstance"}


def _definition_of(name: str, src: str, max_lines: int = 60) -> str:
    """The text of `name`'s definition in src (TS/JS function or Python def), by
    brace/indent matching. '' when not found."""
    if not src:
        return ""
    lines = src.splitlines()
    pat = re.compile(r"^\s*(?:export\s+)?(?:async\s+)?(?:function\s+" + re.escape(name)
                     + r"\b|(?:const|let)\s+" + re.escape(name) + r"\s*=|def\s+"
                     + re.escape(name) + r"\s*\()")
    for i, l in enumerate(lines):
        if not pat.search(l):
            continue
        if l.lstrip().startswith("def "):
            ind = len(l) - len(l.lstrip())
            j = i + 1
            while j < len(lines) and (not lines[j].strip()
                                      or len(lines[j]) - len(lines[j].lstrip()) > ind):
                j += 1
            return "\n".join(lines[i:min(j, i + max_lines)])
        depth, seen = 0, False
        for j in range(i, min(len(lines), i + max_lines)):
            depth += lines[j].count("{") - lines[j].count("}")
            seen = seen or "{" in lines[j]
            if seen and depth <= 0:
                return "\n".join(lines[i:j + 1])
        return "\n".join(lines[i:i + max_lines])
    return ""


# path -> (base_src, new_src). Filled by main() when the task file names a repo.
REF_SOURCES: dict = {}


def ref_budget_for(num_ctx: int) -> int:
    """Chars of REFERENCED DEFINITIONS the verifier prompt may carry at this
    context window. 0 at <= 8192 (the Unraid pre-gate runs qwen3:14b at 6144 and
    its prompt + 4000/8000-token output budget already fills that window), then
    1 char per 4 tokens of headroom above 8192 (6144 chars at 32768 ~= 1.7k
    tokens), capped at 24000. gate-on-complete computes the same number from the
    tier's num_ctx and passes it as task-file `ref_budget`; main() takes the MIN
    of that and this function of the ACTUAL --num-ctx, so a clamped context (the
    worker's UNRAID_CONFIRMED_SAFE_CTX) can only shrink the block."""
    n = int(num_ctx or 0)
    if n <= 8192:
        return 0
    return min(24000, (n - 8192) // 4)


REF_BUDGET = 0      # set by main(); 0 = hunk-only (the pre-patch behaviour)


def effective_ref_budget(num_ctx: int, spec_budget=None) -> int:
    """The tier's requested budget, never more than the ACTUAL context allows."""
    b = ref_budget_for(num_ctx)
    if spec_budget is not None:
        try:
            b = max(0, min(b, int(spec_budget)))
        except (TypeError, ValueError):
            pass
    return b


def load_ref_sources(repo: str, base: str | None, paths) -> None:
    """Fill REF_SOURCES from the job's worktree. Never fatal: a missing repo or
    ref just means the verifier runs hunk-only, as before."""
    root = Path(repo).expanduser()
    for p in paths:
        new_src = ""
        try:
            new_src = (root / p).read_text(errors="replace")
        except Exception:
            pass
        base_src = ""
        if base:
            try:
                r = subprocess.run(["git", "-C", str(root), "show", f"{base}:{p}"],
                                   capture_output=True, text=True, timeout=20,
                                   env={**_os.environ, "GIT_OPTIONAL_LOCKS": "0"})
                base_src = r.stdout if r.returncode == 0 else ""
            except Exception:
                pass
        if base_src or new_src:
            REF_SOURCES[p] = (base_src, new_src)


_LITERALS = re.compile(r"""(['"])([^'"]{1,80})\1""")
_COLLECTION = re.compile(r"[\(\[\{]([^\(\)\[\]\{\}]{2,400})[\)\]\}]")


def _collections(lines: list[str]) -> list[set]:
    """Every literal collection on these lines, as sets of member strings."""
    out = []
    for l in lines:
        for m in _COLLECTION.finditer(l):
            members = {v for _, v in _LITERALS.findall(m.group(1)) if v.strip()}
            if len(members) >= 1:
                out.append(members)
    return out


def dropped_members(hunk: Hunk) -> list[tuple[str, list[str]]]:
    """Members that vanish from a literal collection -- computed, not asked.

    The motivating case: a 53-line refactor extracts an inline tuple into a
    named constant and silently drops one value --
    `("cancelled", "refunded")` becomes `("cancelled",)`. The review model
    flagged the right line but only said the guard "is now removed"; it never
    noticed that `cancelled` survived and `refunded` did not. A flag costs a
    human the same read either way; naming the missing value is the difference
    between triage and a diagnosis.

    Set membership is decidable, so the harness decides it rather than asking.
    Same principle as comparison_verdict(): compute what is computable, and
    stay silent otherwise.

    Only fires when the member is absent from EVERY added collection in the
    hunk, so extracting a list into a constant, reordering it, or renaming the
    variable that holds it are all invisible here -- which is what makes the
    benign twins stay clean.
    """
    added = _collections(hunk.added)
    if not added:
        return []
    added_all = set().union(*added)
    # A member that still appears verbatim as a literal ANYWHERE on the added
    # side has not been dropped -- even when _COLLECTION could not parse the
    # collection that now holds it. A nested ternary introduces brackets that
    # break the flat bracket match, so the re-added member never enters
    # added_all and would read as gone. Grounding the "dropped" claim only in
    # added_all (literals inside *parseable* collections) let the VehicleCard
    # diff FAIL-gate a correct change: 'TAP DIAL TO SET' was present verbatim
    # on the added line yet reported dropped. Subtract every added-side literal
    # so a token that reappears cannot be claimed removed; a token truly gone
    # from the added side is still absent here and stays catchable.
    added_literals = {v for l in hunk.added
                      for _, v in _LITERALS.findall(l) if v.strip()}
    present = added_all | added_literals
    out = []
    for removed_set in _collections(hunk.removed):
        gone = sorted(removed_set - present)
        # A collection replaced wholesale is a rewrite, not a dropped member;
        # requiring that something SURVIVED keeps this to the silent-drop case.
        if gone and (removed_set & added_all):
            out.append((", ".join(sorted(removed_set & added_all)), gone))
    return out


def _cmd_path(line: str) -> list[str]:
    """The leading non-flag words of a command: ['aws','s3','sync'].

    Stops at the first flag, redirect, pipe or quoted argument, so what remains
    is the binary plus its subcommand path. Comparing THAT is the whole point:
    an earlier version compared only the first two tokens and therefore read
    `aws s3 sync` and `aws s3 cp` as the same command (`aws` + `s3`), missing a
    real semantic change. It happened to work on `sc_auth list` only because
    that command's subcommand is its second word -- the rule was fitted to the
    shape of the one fixture it was written for, which an unseen fixture
    exposed immediately.
    """
    body = line.strip().lstrip("$(").strip()
    first = body.split()[0] if body.split() else ""
    if first.lower() in _NOT_A_COMMAND or body.rstrip().endswith(":"):
        return []
    words: list[str] = []
    for w in body.split():
        if w.startswith(("-", "|", ">", "<", "&", "\"", "'", "$")):
            break
        if not _CMD_WORD.match(w):
            break
        words.append(w)
        if len(words) >= 4:
            break
    return words


def subcommand_swaps(hunk: Hunk) -> list[tuple[str, str, str]]:
    """Same binary invoked with a DIFFERENT subcommand path.

    Whether `sc_auth list` and `sc_auth identities` -- or `aws s3 sync` and
    `aws s3 cp` -- do the same thing is not decidable from a diff; it depends on
    documentation nobody in this pipeline has. Every model tested missed the
    seeded swap, and the v7 probe recorded qwen3-coder calling it "functionally
    equivalent". That is a knowledge limit, not a reasoning failure.

    So the harness stops trying to answer it and surfaces it instead. Being
    unable to verify something is a reportable state, not a reason to stay
    silent.

    Narrow by construction: the binary must be identical and the subcommand path
    must differ. Replacing one command with another (`echo` -> `printf`) is not
    this. Adding a flag to the same subcommand (`aws s3 sync --delete` ->
    `... --delete --only-show-errors`) is not this either.
    """
    # SCOPE + PAIRING (2026-10-06, gate audit): the rule ran on EVERY file type and
    # paired a removed line with the FIRST added line sharing its first word. 42
    # medium code findings in Oct alone were this rule reading non-commands as
    # commands -- SQL (`CREATE TABLE` -> `CREATE UNIQUE INDEX`, two different
    # statements of a rename diff), Prisma (`model OrderEgmtLink` -> `model
    # OrderEgiftLink`, an identifier fix), Dockerfile (`RUN useradd` -> `RUN apt-get`)
    # and TASK.md prose (`the leftovers` -> `the step-1 ...`). Each one pushed a
    # verdict to concerns and burned a re-gate. Now: shell-command contexts only,
    # and the added line must be the removed line's own counterpart (similar), not
    # merely the first same-first-word line in the hunk.
    if not _shell_context(hunk.path):
        return []
    out = []
    for r in hunk.removed:
        rr = _shell_line(r, hunk.path)
        rp = _cmd_path(rr)
        if len(rp) < 2:
            continue
        best = None
        for a in hunk.added:
            aa = _shell_line(a, hunk.path)
            ap = _cmd_path(aa)
            if len(ap) < 2 or ap[0] != rp[0] or ap == rp:
                continue
            sim = difflib.SequenceMatcher(None, rr.strip(), aa.strip()).ratio()
            if sim >= _SUBCMD_PAIR_MIN_SIM and (best is None or sim > best[0]):
                best = (sim, ap)
        if best:
            ap = best[1]
            out.append((rp[0], " ".join(rp[1:]), " ".join(ap[1:])))
    return out


_SUBCMD_PAIR_MIN_SIM = 0.5
_SHELL_SUFFIXES = {".sh", ".bash", ".zsh", ".ksh", ".command", ".mk"}
_SHELL_NAMES = {"makefile", "gnumakefile", "justfile", "dockerfile", "containerfile"}


def _shell_context(path: str) -> bool:
    """Files whose lines are shell commands: shell scripts, Makefile/Justfile
    recipes, Dockerfile RUN lines, and extension-less scripts (~/bin style).
    Everything else (SQL, Prisma, Python/TS source, Markdown, JSON, YAML) is not
    -- a first-word match there is a keyword or an identifier, not a binary."""
    name = Path(path or "").name
    low = name.lower()
    if low in _SHELL_NAMES or low.startswith("dockerfile."):
        return True
    suf = Path(low).suffix
    if suf in _SHELL_SUFFIXES:
        return True
    return suf == "" and bool(name)


def _shell_line(line: str, path: str) -> str:
    """The shell command a line carries: a Dockerfile `RUN x` is `x`."""
    low = Path(path or "").name.lower()
    s = line.strip()
    if low == "dockerfile" or low.startswith("dockerfile.") or low == "containerfile":
        m = re.match(r"(?i)^RUN\s+(.*)$", s)
        return m.group(1) if m else ""
    return line



# ------------------------------------------------- deterministic comparison check

_BASH_CMP = re.compile(r"([A-Za-z_$][\w{}:\-$\"]*)\s+(-lt|-le|-gt|-ge|-eq|-ne)\s+(-?\d+)")
_PY_CMP = re.compile(r"([A-Za-z_][\w.]*)\s*(<=|>=|==|!=|<|>)\s*([A-Za-z_][\w.]*|-?\d+)")
_BASH_OPS = {"-lt": "<", "-le": "<=", "-gt": ">", "-ge": ">=", "-eq": "==", "-ne": "!="}
_MIRROR = {"<": ">", ">": "<", "<=": ">=", ">=": "<=", "==": "==", "!=": "!="}


def _parse_cmp(line: str):
    m = _BASH_CMP.search(line)
    if m:
        return m.group(1), _BASH_OPS[m.group(2)], m.group(3)
    m = _PY_CMP.search(line)
    if m:
        return m.group(1), m.group(2), m.group(3)
    return None


_GUARD_OPEN = re.compile(r"""(?x)
    ^\s*(\}\s*)?(else\s+)?if\s*[\(:]      # if (...) / else if / python if x:
  | ^\s*(unless|elif)\b
""")
_EARLY_EXIT = re.compile(
    r"^\s*(return\b|break\b|continue\b|throw\b|raise\b|exit\b|sys\.exit\b|os\.exit\b)")
_INLINE_GUARDED_EXIT = re.compile(
    r"^\s*(if|unless)\b.*\b(return|break|continue|throw|raise|exit)\b")


def _guard_cond(line: str) -> str:
    """The CONDITION inside a guard, normalised -- for deciding 'is this the same
    guard' across a move.

    Exact-string matching was too strict and let the false positive through: the
    refactor moved `if (twelveVoltRaw !== '') console.log(`[rivian] ...`)` and
    changed `[rivian]` to `[${src}]` inside the log string, so the lines were not
    byte-identical even though the GUARD was untouched. Same trap as asserting a
    spec literal as an exact string. Compare what actually identifies the guard --
    its condition -- and ignore the body and whitespace.
    """
    m = re.search(r"\bif\s*\(", line)
    if not m:
        m2 = re.search(r"\bif\s+(.+?):", line)
        return re.sub(r"\s+", "", m2.group(1)) if m2 else ""
    i, depth = m.end(), 1
    for j in range(i, len(line)):
        if line[j] == "(":
            depth += 1
        elif line[j] == ")":
            depth -= 1
            if depth == 0:
                return re.sub(r"\s+", "", line[i:j])
    return ""


def guard_removed_early_exit(hunk: Hunk, all_added: set | None = None):
    """A guard was deleted and its early exit became UNCONDITIONAL.

    Decidable, so the harness decides it. Motivating case (shadow-gate case 1,
    a real ev-dashboard regression):

        -      if (ageMs < interval) {
        -        return { ...cache.state, _telemetryDegraded: true };
        -      }
        +      const degraded = await readTelemetryDegraded();
        +      return { ...cache.state, _telemetryDegraded: degraded };

    The fall-through path -- the branch that went on to fetch fresh data -- is
    now dead code. Every caller returns cached state forever.

    Why this rule exists rather than a prompt: the review model DID flag the
    right line on 4/4 runs, but explained it as a flag-correctness problem
    instead of a control-flow one. The verifier correctly judged the rationale
    wrong and discarded the LOCATION along with it (3/4 runs). Reasons are
    arguable; `an if disappeared and a return did not` is not. Emitted with
    skip_verify so a wrong rationale can never again bury a real regression.
    """
    removed = [r for r in hunk.removed if r.strip()]
    added = [a for a in hunk.added if a.strip()]
    if not removed or not added:
        return None
    # the guard, and the exit it used to protect, both disappeared
    if not any(_GUARD_OPEN.search(r) for r in removed):
        return None
    gone_exit = next((r for r in removed if _EARLY_EXIT.search(r)), None)
    if not gone_exit:
        return None
    # a bare early exit survives, and nothing re-guards it
    kept_exit = next((a for a in added if _EARLY_EXIT.search(a)), None)
    if not kept_exit:
        return None
    if any(_GUARD_OPEN.search(a) or _INLINE_GUARDED_EXIT.search(a) for a in added):
        return None
    cond = next(r for r in removed if _GUARD_OPEN.search(r))
    # A guard that reappears ANYWHERE ELSE in the diff was MOVED, not deleted.
    # Found on real churn 2026-08-31 and it was this rule's first false positive
    # on a real diff: an 858-line refactor extracted a function, removing
    # `if (twelveVoltRaw !== '') console.log(...)` at one line and re-adding it
    # ~200 lines earlier. Hunk-local logic sees a deletion and cannot see the
    # reappearance -- the same "moved, not deleted" case as the ok-guardmoved
    # decoy, one scope up. No purely hunk-scoped check can avoid this.
    if all_added is not None and _guard_cond(cond) and any(
            _guard_cond(a) == _guard_cond(cond) for a in all_added if _GUARD_OPEN.search(a)):
        return None
    return (cond.strip(), kept_exit.strip())


_CMP_RE = re.compile(r"(<=|>=|==|!=|<|>|-lt|-le|-gt|-ge|-eq|-ne)")


def comparison_verdict(hunk: Hunk):
    """Decide equivalence of a changed comparison by COMPUTING it.

    Exists because model-produced probes are wrong often enough to be dangerous
    once the harness acts on them. Measured: qwen3.5:9b, reviewing `-lt 2` ->
    `-lt 1`, wrote "PAIRED_COUNT=1: old -lt 2 -> true; new -lt 1 -> true. Same."
    `1 -lt 1` is FALSE. It got the arithmetic wrong, concluded the forms were
    equivalent, and the harness trusted it and deleted a correct finding.
    Delegating adjudication to model-produced evidence inherits the model's
    errors -- so where the harness CAN check, it must.

    Boundary comparisons are the most common defect class in review, and they
    are trivially decidable: evaluate both forms over a small integer range.
    Returns True (behaviour differs), False (equivalent), or None (not a simple
    comparison change -- say nothing rather than guess).
    """
    olds = [_parse_cmp(l) for l in hunk.removed]
    news = [_parse_cmp(l) for l in hunk.added]
    olds = [o for o in olds if o]
    news = [n for n in news if n]
    if len(olds) != 1 or len(news) != 1:
        return None
    (lv, lop, lr), (rv, rop, rr) = olds[0], news[0]

    # Operand swap with a mirrored operator is the same test: `a >= b` and
    # `b <= a`. Sequence-based similarity cannot see this, and a model reading
    # it as "the guard was changed" is how ok-window became a false positive.
    if lv == rr and lr == rv and _MIRROR.get(lop) == rop:
        return False

    if lv != rv:
        return None                     # different subjects; not comparable here

    # Same operands, different operator: decidable without knowing the values.
    # `placed >= cutoff` -> `placed > cutoff` disagree exactly at equality, and
    # that boundary case is the whole defect. Requiring numeric operands missed
    # this -- which is the single most common shape of a real off-by-one.
    if lr == rr:
        return lop != rop

    try:
        a, b = int(lr), int(rr)
    except ValueError:
        return None                     # symbolic and different; cannot decide
    lo, hi = min(a, b) - 2, max(a, b) + 2
    for x in range(lo, hi + 1):
        if eval(f"{x} {lop} {a}") != eval(f"{x} {rop} {b}"):   # noqa: S307
            return True                 # a value where they disagree
    return False


# ----------------------------------------------------------------- schemas

REVIEW_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array", "maxItems": 6,
            "items": {"type": "object", "properties": {
                "quote": {"type": "string"},
                "severity": {"type": "string",
                             "enum": ["high", "medium", "low"]},
                "claim": {"type": "string"},
                "failure_scenario": {"type": "string"},
            }, "required": ["quote", "severity", "claim", "failure_scenario"]}},
    },
    "required": ["findings"],
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string",
                    "enum": ["REAL_DEFECT", "NOT_A_DEFECT", "UNSURE"]},
        "reason": {"type": "string"},
        "concrete_trigger": {"type": "string"},
        "probe": {"type": "string"},
        "probe_shows_difference": {"type": "boolean"},
    },
    "required": ["verdict", "reason", "probe", "probe_shows_difference"],
}

DIAGNOSE_SCHEMA = {
    "type": "object",
    "properties": {
        "causes": {
            "type": "array", "maxItems": 5,
            "items": {"type": "object", "properties": {
                "quote": {"type": "string"},
                "hypothesis": {"type": "string"},
                "mechanism": {"type": "string"},
                "confirming_evidence": {"type": "string"},
                "refuting_evidence": {"type": "string"},
            }, "required": ["quote", "hypothesis", "mechanism",
                            "confirming_evidence", "refuting_evidence"]}},
    },
    "required": ["causes"],
}


# ----------------------------------------------------------------- prompts

REMOVAL_SYS = """You are auditing DELETIONS in a code change. Added code is \
someone else's problem; you look only at what is now missing.

You are given the lines this change deleted, plus the surrounding hunk. For each \
deleted line, answer one question: **what was that line doing, and is anything \
still doing it?**

Deleted lines that matter, and what to look for:
  - a guard, validation or early return -> what invalid input now gets through, \
and what does it corrupt when it does?
  - a umask, chmod, lock, or permission setting -> what is now exposed, and for \
how long?
  - a bounds or null check -> what value now reaches code that cannot handle it?
  - a cleanup, close, rollback or release -> what leaks or stays locked?

If the deletion is genuinely safe -- the work is done elsewhere, or the code was \
dead -- say so by returning an empty list. Dead code and redundant assignments \
get deleted all the time and are not findings.

Same rules as any finding: quote a real deleted line VERBATIM, and give a \
CONCRETE failure scenario with specific inputs or state and the specific wrong \
result. "Reduces safety" is not a failure scenario.

Judge the deletion against what the surrounding code still does. Do not assume \
a replacement exists that you cannot see in the hunk -- and if a replacement \
does exist but runs LATER than the thing it replaced, the gap between them is \
itself the defect."""


REVIEW_SYS = """You are a first-pass code reviewer. You are reviewing ONE hunk \
of a diff. Your findings go to a senior engineer who will act on them, so a \
confident wrong finding costs more than a missed one.

Report a finding ONLY when this change introduces a defect: it makes the code \
do something incorrect, unsafe, or different from what the stated intent says. \
Pre-existing issues in surrounding code are NOT findings. Style, naming, \
formatting and "could be cleaner" are NOT findings.

Every finding needs three things, and a finding missing any of them is worthless:

1. "quote" -- a line COPIED VERBATIM from the diff, character for character, \
that the finding is about. It is checked automatically against the real diff; \
an invented or paraphrased quote gets the finding discarded.

2. "claim" -- one sentence naming the defect precisely. Name the actual \
identifier, value or command involved, not a category.

3. "failure_scenario" -- CONCRETE inputs or state, and the specific wrong \
result. "User has exactly 1 paired key: guard does not fire, user is not warned, \
loses the key, is locked out." That is a failure scenario. "Could cause issues" \
and "may lead to security problems" are NOT -- they are the absence of one. If \
you cannot write down the specific circumstance in which this misbehaves, you do \
not have a finding: leave it out.

**Look hardest at what was REMOVED.** A deletion is invisible if you only read \
the added lines, and it is where the worst defects hide. For every removed line, \
ask: what was that line protecting against, and is anything still protecting \
against it? A guard, a umask, a lock, a bounds check or an early return that \
disappeared is a defect even when the added lines look reasonable on their own.

Also check, specifically: does a changed comparison or threshold still hold at \
its boundary? Does a swapped command or function actually do the same thing? \
Does a reordered operation open a window where state is briefly wrong?

**If this hunk is a TEST or FIXTURE, the defect to hunt is a test that cannot \
fail on broken code.** Mutation testing already proves a test NOTICES a changed \
line; it cannot prove the test checks the PROPERTY, and these three shapes pass \
it while proving nothing. Each is a finding:

  - PROXY / SHAPE-MATCHING: the test asserts on the query, filter, options or \
props object the code BUILDS -- `assert c.where == {...}`, comparing a \
JSON.stringify of a filter -- instead of driving real inputs through the real \
symbol and checking what came back. This re-states the implementation: it goes \
red whenever the line changes and green for any wrong filter that keeps the \
same shape. The failure_scenario to write is a WRONG implementation this test \
would still pass.
  - VACUOUS TRUTH: an assertion that is trivially true for the case as written \
-- a universal (`every`, `all()`, `NOT EXISTS`) over a collection that can be \
EMPTY, a loop body that never runs, an `assert` on a filtered list nobody \
checked is non-empty. `every` over an empty relation is TRUE, so the input that \
separates the right predicate from the wrong one is exactly the one missing.
  - STUB TESTS THE HARNESS: the assertions only exercise mocks, fakes or data \
defined inside the test file, and never reach the real symbol. Green forever, \
including against an empty target file.

For a test hunk, ask the one question that matters: **name a broken \
implementation that would still make this test pass.** If you can name one, that \
is the finding and the failure_scenario. If you cannot, the test is fine.

Returning an empty findings list is a correct and common answer. Most hunks in \
most diffs are fine. Do not invent something to say."""

VERIFY_SYS = """You are checking one finding from a code review, and you are \
adversarial. You did not write it and you owe it nothing. Reviewers produce far \
more plausible-sounding findings than real defects, and your job is to catch that.

You get the diff hunk and one finding. Decide:

  REAL_DEFECT  -- the defect is really there, AND you can state the concrete \
circumstance that triggers it. Put that in "concrete_trigger": specific values \
or state, and the specific wrong outcome.
  NOT_A_DEFECT -- the code is actually correct, the two forms are equivalent, \
the concern is about pre-existing code rather than this change, the "problem" is \
style, or the failure scenario does not actually follow from the code shown.
  UNSURE       -- deciding needs information not present here.

**Fill in "probe" FIRST, before deciding.** Use the identifiers, values and \
commands from THIS hunk -- naming anything that does not appear in the code above \
means you are not probing this change and your probe will be discarded.

If the change alters a computed value or condition: pick two or three specific \
inputs, and write out what the OLD and the NEW code each produce at those inputs. \
Evaluate them; do not summarise. If old and new agree at every value you probed, \
the verdict is NOT_A_DEFECT however different the two forms look.

If the change is NOT about a computed value -- an ordering change, a removed \
guard, a permission or lifetime change -- then a value table cannot settle it. \
Say so in "probe", describe the sequence of operations instead, and set \
"probe_shows_difference" to true if the new ordering leaves a gap the old one \
did not.

Then set "probe_shows_difference": true only if the old and new code produced \
DIFFERENT results at some value you probed. If every probed value gave the same \
result, it is false.

This is not paperwork. Stating that two forms differ is easy and often wrong; \
evaluating them is what catches it. Your verdict must follow your probe: if the \
probe shows no difference anywhere, the verdict is NOT_A_DEFECT.

Test the finding against the code, not against how reasonable it sounds. \
Specifically:
  - **ORDER MATTERS.** If a protection now happens AFTER the thing it protects, \
the exposure between them is real, and pointing at the later protection does not \
refute it. A `chmod` after a file is written does not undo the window in which \
the file existed with looser permissions. Check WHEN each step happens, not \
merely whether it is present.
  - If it claims something is unprotected, check whether a guard elsewhere in \
the hunk already handles it.
  - If the failure scenario is vague, hedged, or just restates the claim, that \
is grounds for NOT_A_DEFECT. A real defect has a trigger you can write down.

**A finding about a TEST that cannot fail is settled differently, and you must \
not discard it for lacking a value table.** The claim there is not "this code \
computes the wrong number" -- it is "this test would pass on a broken \
implementation". Probe it the matching way: write down a SPECIFIC wrong \
implementation (a filter with the opposite polarity, an unguarded `every` over \
an empty relation, the feature simply not implemented) and walk this test \
against it. If the test still passes against that wrong implementation, the \
finding is REAL_DEFECT and "probe_shows_difference" is true -- the difference \
you probed is between a correct and a broken target, not between old and new \
code. If every wrong implementation you can name makes the test go red, it is \
NOT_A_DEFECT. A test asserting on the query object the code built, alongside \
assertions that drive the real symbol and check its output, is NOT a defect: \
shape assertions are only a defect when they are all there is.

NOT_A_DEFECT is the most useful verdict you can return and you should expect to \
return it often."""

DIAGNOSE_SYS = """You are diagnosing a reported problem from code alone. You \
cannot run anything, so do not propose "add logging and see" -- reason from what \
the code must do.

Give at most 5 candidate root causes, best first. For each:

  "quote"  -- a line COPIED VERBATIM from the code shown, checked automatically \
against the real source; an invented quote discards the candidate.
  "hypothesis" -- one sentence: what is actually wrong.
  "mechanism"  -- the causal chain from that line to the reported symptom. If \
you cannot connect them step by step, it is not a candidate.
  "confirming_evidence" -- what someone should look at to confirm this, stated \
so they could go and check it.
  "refuting_evidence"   -- what would prove this hypothesis WRONG.

That last field is not optional and not a formality. A hypothesis that nothing \
could refute is not a diagnosis, and listing it wastes the reader's time. If \
the code shown genuinely cannot explain the symptom, say so with an empty list \
rather than offering the most plausible-looking line as a guess."""


# ------------------------------------------------------------------ stages

def group_hunks(hunks: list[Hunk], max_chars: int) -> list[list[Hunk]]:
    """Batch hunks so related changes are reviewed together.

    Reviewing every hunk in isolation loses cross-hunk defects, and the bench
    contains exactly one: `umask 077` is deleted in one hunk and `chmod 600` is
    added in another. Seen separately, hunk one is a bare deletion and hunk two
    looks like someone being careful about permissions; only together are they a
    TOCTOU window. Since this is how a human reviews a small change -- reading
    the whole thing -- the harness batches up to max_chars and only splits a
    diff genuinely too large to hold at once.

    Batches by FILE, not by a sequential character fill. Measured 2026-08-31 on a
    real 858-line refactor (ev-dashboard bc0132e): the naive fill split
    `lib/rivian.ts` -- the file containing the entire refactor -- across THREE
    batches, so a function extraction's removal and its re-addition were never
    in the same prompt. That is precisely why the move read as a deletion. A
    file's hunks are the most related things in any diff; splitting them while
    unrelated files sit together is the worst available packing.

    Small files are still packed together to keep the call count down; a file
    larger than the budget is split, but only within itself and contiguously.
    """
    by_path: dict[str, list[Hunk]] = {}
    for h in hunks:
        by_path.setdefault(h.path, []).append(h)

    groups: list[list[Hunk]] = []
    cur: list[Hunk] = []
    size = 0
    for path, hs in by_path.items():
        total = sum(len(h.text()) for h in hs)
        if total > max_chars:                 # too big to hold at once
            if cur:
                groups.append(cur)
                cur, size = [], 0
            chunk, csize = [], 0
            for h in hs:                      # split within the file only
                t = len(h.text())
                if chunk and csize + t > max_chars:
                    groups.append(chunk)
                    chunk, csize = [], 0
                chunk.append(h)
                csize += t
            if chunk:
                groups.append(chunk)
            continue
        if cur and size + total > max_chars:  # start a new batch, keep file whole
            groups.append(cur)
            cur, size = [], 0
        cur.extend(hs)
        size += total
    if cur:
        groups.append(cur)
    return groups


def stage_review(model: Model, hunks: list[Hunk], intent: str, context: str,
                 corpus: str, stats: dict, batch_chars: int = 9000) -> list[dict]:
    # batch_chars is a fixed 9000 -- about 7% of a 32k-token context. Whether
    # raising it helps (a file reviewed whole catches cross-hunk defects) or
    # hurts (dilution) is an empirical question, so it is a CLI flag and is
    # being measured rather than guessed. See --batch-chars.
    findings: list[dict] = []
    groups = group_hunks(hunks, batch_chars)
    if len(groups) < len(hunks):
        log(f"  reviewing {len(hunks)} hunk(s) in {len(groups)} batch(es) so "
            f"related changes are seen together")
    for i, group in enumerate(groups, 1):
        removed = [r for h in group for r in h.removed]
        body = "\n\n".join(f"FILE: {h.path}\nHUNK: {h.header}\n"
                            f"```diff\n{h.text()}\n```" for h in group)
        removal_block = ""
        if removed:
            # Surfaced separately and named, because a removal read inline is a
            # line with a minus in front of it and is trivially skimmed past.
            removal_block = ("\nLINES REMOVED BY THIS CHANGE -- for each one, ask "
                             "what it was protecting against and whether anything "
                             "still protects against it:\n"
                             + "\n".join(f"  - {r.strip()}" for r in removed if r.strip()))
        user = (f"STATED INTENT OF THIS CHANGE: {intent}\n\n"
                f"CONTEXT: {context}\n\n"
                f"CHANGE {i} of {len(groups)}:\n\n{body}\n{removal_block}\n\n"
                f"Report only defects this change introduces.")
        out = model.chat_json(REVIEW_SYS, user, REVIEW_SCHEMA)
        if out is None and len(group) > 1:
            # A multi-hunk batch is the longest answer the review asks for, so the
            # likeliest to come back truncated or mis-escaped. Re-ask one hunk at a
            # time before giving up on any of it; findings are merged.
            log(f"  change {i}: unusable answer for {len(group)} hunks -- retrying per hunk")
            merged, failed = [], 0
            for h in group:
                _ub = (f"FILE: {h.path}\nHUNK: {h.header}\n```diff\n{h.text()}\n```")
                _rm = [r for r in h.removed if r.strip()]
                _u = (f"STATED INTENT OF THIS CHANGE: {intent}\n\n"
                      f"CONTEXT: {context}\n\n"
                      f"CHANGE {i} of {len(groups)} (one hunk of it):\n\n{_ub}\n"
                      + (("\nLINES REMOVED BY THIS CHANGE -- for each one, ask what it "
                          "was protecting against and whether anything still protects "
                          "against it:\n" + "\n".join(f"  - {r.strip()}" for r in _rm))
                         if _rm else "")
                      + "\n\nReport only defects this change introduces.")
                _o = model.chat_json(REVIEW_SYS, _u, REVIEW_SCHEMA)
                if _o is None:
                    failed += 1
                    continue
                merged += list(_o.get("findings") or [])
            stats["review_split_retry"] = stats.get("review_split_retry", 0) + 1
            if failed == len(group):
                out = None
            else:
                # A hunk that still failed is UNREVIEWED: counted, so the verdict
                # can never read it as clean (_verdict incomplete=...).
                stats["review_parse_fail"] += failed
                out = {"findings": merged}
        if out is None:
            stats["review_parse_fail"] += 1
            log(f"  hunk {i}: PARSE FAILURE")
            continue
        kept = 0
        for f in (out.get("findings") or []):
            q = (f.get("quote") or "").strip()
            fs = (f.get("failure_scenario") or "").strip()
            claim = (f.get("claim") or "").strip()
            if not q or not claim:
                continue
            if not quote_is_real(q, corpus):
                stats["quote_rejected"] += 1
                continue
            _drift = claim_identifier_drift(claim + "\n" + fs, corpus, intent)
            if _drift:
                stats["identifier_drift_rejected"] = stats.get("identifier_drift_rejected", 0) + 1
                stats.setdefault("identifier_drift", []).extend(
                    f"{a}~{b}" for a, b in _drift[:3])
                continue
            if len(fs) < 25:
                # The requirement is the point, not paperwork: a finding whose
                # failure scenario is a fragment did not have one.
                stats["no_failure_scenario"] += 1
                continue
            findings.append({"hunk": i, "path": group[0].path, "quote": q,
                             "severity": f.get("severity", "medium"),
                             "claim": claim, "failure_scenario": fs,
                             "hunk_text": body})
            kept += 1
        log(f"  change {i} ({group[0].path}): {kept} finding(s) kept")
    return findings


def stage_removals(model: Model, hunks: list[Hunk], intent: str, context: str,
                   corpus: str, stats: dict) -> list[dict]:
    """A second, dedicated pass over deleted lines only.

    Two of the five seeded bugs in the bench are pure removals (`umask 077`
    deleted; a `raise ValueError` guard deleted) and the general review pass
    found neither reliably -- bug-guard raised nothing at all. That matches the
    vault's history, where deletions are the class that gets missed: a reviewer
    reading a diff sees added lines as the change and treats removed lines as
    background.

    Asking the same model the same question again would not help. Asking it a
    DIFFERENT question -- "what was this line doing, and is anything still doing
    it?" -- with the added lines out of view, removes the distraction that
    causes the miss.
    """
    # Only lines DELETED OUTRIGHT, never lines that were merely rewritten.
    # Every modification appears in a diff as a -/+ pair, so treating all minus
    # lines as deletions asks "what protection is gone?" about code that is
    # still right there under a new spelling -- and the model duly finds loss.
    # Measured: that produced false positives on ok-window (`placed >= cutoff`
    # rewritten as `cutoff <= placed`, reported as a deleted guard clause) and
    # was on track to do the same for every benign rewrite in the bench.
    import difflib

    def _tokens(x: str) -> set:
        return set(re.findall(r"[A-Za-z_][A-Za-z0-9_]*|\d+", x))

    def _similar(a: str, b: str) -> float:
        """Sequence ratio OR token overlap, whichever is higher.

        Sequence ratio alone is order-sensitive and misses a pure reordering:
        `if placed >= cutoff:` rewritten as `if cutoff <= placed:` scored below
        the threshold and was reported as a deleted guard clause. The same
        identifiers on both sides means the code is still there, however it has
        been rearranged, so token overlap catches what sequence order cannot.
        """
        seq = difflib.SequenceMatcher(None, a, b).ratio()
        ta, tb = _tokens(a), _tokens(b)
        jac = len(ta & tb) / len(ta | tb) if (ta or tb) else 0.0
        return max(seq, jac)

    def truly_deleted(h: Hunk) -> list[str]:
        adds = [a.strip() for a in h.added if a.strip()]
        out = []
        for r in (x.strip() for x in h.removed if x.strip()):
            twin = max((_similar(r, a) for a in adds), default=0.0)
            if twin < 0.6:          # no similar replacement -> genuinely gone
                out.append(r)
        return out

    todo = [(h, truly_deleted(h)) for h in hunks]
    todo = [(h, d) for h, d in todo if d]
    if not todo:
        log("  removal pass: no outright deletions (every removed line was "
            "rewritten) -- skipped")
        return []
    findings: list[dict] = []
    for h, deleted in todo:
        user = (f"STATED INTENT: {intent}\n\nCONTEXT: {context}\n\n"
                f"FILE: {h.path}\n\nDELETED LINES:\n"
                + "\n".join(f"  - {d}" for d in deleted)
                + f"\n\nSURROUNDING HUNK for reference:\n```diff\n{h.text()}\n```\n\n"
                f"What did these deletions remove, and is anything still doing it?")
        out = model.chat_json(REMOVAL_SYS, user, REVIEW_SCHEMA)
        if out is None:
            stats["review_parse_fail"] += 1
            continue
        for f in (out.get("findings") or []):
            q = (f.get("quote") or "").strip()
            fs = (f.get("failure_scenario") or "").strip()
            claim = (f.get("claim") or "").strip()
            if not q or not claim:
                continue
            if not quote_is_real(q, corpus):
                stats["quote_rejected"] += 1
                stats.setdefault("rejected_quotes", []).append(q[:160])
                continue
            _drift = claim_identifier_drift(claim + "\n" + fs, corpus, intent)
            if _drift:
                stats["identifier_drift_rejected"] = stats.get("identifier_drift_rejected", 0) + 1
                stats.setdefault("identifier_drift", []).extend(
                    f"{a}~{b}" for a, b in _drift[:3])
                continue
            if len(fs) < 25:
                stats["no_failure_scenario"] += 1
                stats.setdefault("thin_scenarios", []).append(claim[:120])
                continue
            findings.append({"hunk": 0, "path": h.path, "quote": q,
                             "severity": f.get("severity", "medium"),
                             "claim": claim, "failure_scenario": fs,
                             "hunk_text": h.text(), "hunk_header": h.header, "source": "removal-pass"})
    log(f"  removal pass: {len(findings)} finding(s) from "
        f"{len(todo)} hunk(s) with outright deletions")
    return findings


def dedupe(findings: list[dict], stats: dict | None = None) -> list[dict]:
    """Drop near-duplicates, keeping the most informative one.

    Order matters and the first version got it wrong. A harness-computed finding
    and a model flag often quote the SAME line -- on `bug-refactor` the removal
    pass said "this guard ... is now removed" while `dropped_members()` said
    "'refunded' was silently dropped from a collection that still contains
    cancelled". Both quote `if o.get("status") in ("cancelled", "refunded"):`,
    so they collide, and appending the model's first meant dedupe discarded the
    precise diagnosis in favour of the vague flag. The rule fired correctly and
    was eaten on the way out.

    Harness findings are deterministic facts and strictly more specific, so they
    are considered first and win the collision. What gets dropped is recorded,
    because a silent filter is how four earlier bugs hid.
    
    Extended to merge near-duplicates when:
    - same path 
    - same hunk_text
    - neither is from harness (harness findings must never be merged)
    """
    # Group findings by path and hunk_text for potential merging
    groups = {}
    for f in findings:
        key = (f["path"], f.get("hunk_text", ""))
        if key not in groups:
            groups[key] = []
        groups[key].append(f)
    
    out: list[dict] = []
    
    # Process each group of potentially near-duplicate findings
    for group in groups.values():
        # If there's only one finding or it contains a harness finding, keep all
        if len(group) == 1 or any(str(f.get("source", "")).startswith("harness-") for f in group):
            out.extend(group)
            continue
            
        # Sort by severity (high to low), then by original order 
        def sort_key(f):
            sev = SEV.get(f.get("severity"), 1)  # high=0, medium=1, low=2
            source = str(f.get("source", ""))
            is_harness = source.startswith("harness-")
            return (sev, not is_harness)  # lower severity number first, but harness findings last
        
        sorted_group = sorted(group, key=sort_key)
        
        # Keep the highest severity finding
        kept = sorted_group[0]
        
        # Add all findings to output except the one we're keeping
        for f in group:
            if f is not kept:
                if stats is not None:
                    stats.setdefault("deduped", []).append(
                        f"{f.get('source', 'model')}: {f['claim'][:80]}")
        out.append(kept)
    
    return out


def stage_verify(model: Model, findings: list[dict], intent: str,
                 stats: dict) -> list[dict]:
    for f in findings:
        if f.get("skip_verify"):
            continue        # harness-generated; there is nothing for a model to check
        _b, _n = REF_SOURCES.get(f.get("path") or "", ("", ""))
        _defs = (referenced_definitions(f.get("hunk_text", ""), _b, _n, budget=REF_BUDGET)
                 if (_b or _n) and REF_BUDGET > 0 else "")
        if _defs:
            stats["ref_defs_shown"] = stats.get("ref_defs_shown", 0) + 1
        user = (f"STATED INTENT: {intent}\n\n"
                f"DIFF HUNK:\n```diff\n{f['hunk_text']}\n```\n\n"
                + (f"REFERENCED DEFINITIONS (outside the hunk, read from the repo -- "
                   f"use these instead of guessing what a called or removed helper "
                   f"does):\n```\n{_defs}```\n\n" if _defs else "")
                + 
                f"THE FINDING TO CHECK\n"
                f"  quoted line: {f['quote']}\n"
                f"  claim: {f['claim']}\n"
                f"  claimed failure: {f['failure_scenario']}\n\n"
                f"Is this a real defect introduced by this change?")
        out = model.chat_json(VERIFY_SYS, user, VERDICT_SCHEMA, num_predict=4000)
        if out is None:
            f["verdict"], f["verdict_reason"] = "UNSURE", "verifier output unparseable"
            stats["verify_parse_fail"] += 1
            continue
        v = out.get("verdict", "UNSURE")
        # A first-pass triage tool must not suppress a high-severity flag on its
        # own say-so. Measured: on bug-umask the REVIEW pass found the real
        # TOCTOU correctly ("removal of umask 077 ... files created with default
        # permissions"), and the verifier killed it with reasoning that ignored
        # ORDERING -- "the script already ensures secure permissions through
        # explicit chmod 600" -- which is exactly the window the finding was
        # about. One over-confident verifier sentence turned a correct HIGH
        # finding into silence. Downgrading rather than dropping keeps the human
        # in the loop on precisely the findings where being wrong costs most,
        # and it costs only a line in an "uncertain" section.
        # Rescue a HIGH finding only when the verifier ASSERTED rather than
        # CHECKED. If it actually evaluated the old and new code at concrete
        # values and still says no, that is real work and it is trusted. If the
        # probe is empty, the rejection is an opinion, and an opinion must not
        # silently delete a high-severity finding. Rescuing every high-severity
        # rejection (the first version) converted correctly-rejected false
        # positives back into surfaced ones -- ok-colon went from clean to a
        # false positive that way.
        # The harness adjudicates the probe rather than trusting the verdict.
        # Measured on ok-le1: the verifier computed the probe CORRECTLY --
        # "=0: old true, new true. =1: old true, new true. =2: old false, new
        # false" -- and then returned REAL_DEFECT anyway, with reasoning that
        # contradicted its own arithmetic. Producing the right evidence and
        # drawing the opposite conclusion is not something a better prompt
        # fixes; the consistency check belongs in code.
        # A computed answer beats a claimed one. Where the harness can decide
        # the comparison itself, its verdict replaces the model's probe.
        computed = None
        for h in parse_diff("\n".join(f.get("hunk_text", "").splitlines())):
            computed = comparison_verdict(h)
            if computed is not None:
                break
        if computed is not None and out.get("probe_shows_difference") != computed:
            log(f"  [verify] harness computed the comparison directly: "
                f"differs={computed} (model's probe said "
                f"{out.get('probe_shows_difference')}) -- using the computation")
            out["probe_shows_difference"] = computed
            out["probe"] = (f"[harness-computed] the two comparisons "
                            f"{'differ' if computed else 'are equivalent'} when "
                            f"evaluated over a range of integer inputs. "
                            + str(out.get("probe", ""))[:200])
            stats["harness_computed"] = stats.get("harness_computed", 0) + 1
        probe_txt = (out.get("probe") or "").strip()
        # The probe must be about the code under review. Measured on bug-umask:
        # the model returned the worked example from this very prompt verbatim
        # ("PAIRED_COUNT=0: old -lt 2 -> true...") for a hunk about `umask 077`
        # and `$DOC_FILE`, and the harness then trusted it and overrode a
        # CORRECT high-severity finding into silence. A prompt example is a
        # template a model will copy; the defence is to check the probe mentions
        # identifiers that actually occur in this hunk.
        hunk_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}",
                                     f.get("hunk_text", "")))
        probe_tokens = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", probe_txt))
        on_topic = bool(hunk_tokens & probe_tokens)
        if probe_txt and not on_topic:
            log(f"  [verify] probe mentions nothing from this hunk -- ignoring it "
                f"(likely copied from the instructions)")
            stats["probe_offtopic"] = stats.get("probe_offtopic", 0) + 1
        unsound = probe_is_unsound(probe_txt)
        if unsound:
            log(f"  [verify] probe reasons from an assumption ({unsound!r}) rather "
                f"than the code -- not treating it as evidence")
            stats["probe_unsound"] = stats.get("probe_unsound", 0) + 1
        probed = len(probe_txt) >= 20 and on_topic and not unsound
        # Consistency is enforced in BOTH directions. The first version only
        # caught a verifier confirming a finding its probe had refuted
        # (ok-le1). The mirror image then bit on bug-guard: the probe stated
        # "OLD raises ValueError, NEW assigns None and continues" and set
        # probe_shows_difference=true -- its own evidence supporting the
        # finding -- and it returned NOT_A_DEFECT anyway. A verifier that
        # contradicts its own probe is unreliable in whichever direction it
        # does it, and only the harness can notice.
        #
        # The promotion is to UNSURE, deliberately not to REAL_DEFECT: a probed
        # difference means behaviour changed, which is what a change is FOR. It
        # is grounds for a human to look, not proof of a defect.
        if (probed and out.get("probe_shows_difference") is True
                and v == "NOT_A_DEFECT"):
            log(f"  [verify] NOT_A_DEFECT contradicts its own probe (which found "
                f"a behavioural difference) -- promoting to UNSURE")
            v = "UNSURE"
            out["reason"] = ("harness override: the verifier rejected this while "
                             "its own probe showed the old and new code behaving "
                             "differently. " + str(out.get("reason", ""))[:200])
            stats["probe_promotions"] = stats.get("probe_promotions", 0) + 1
        elif (probed and out.get("probe_shows_difference") is False
                and v == "REAL_DEFECT"):
            log(f"  [verify] overriding REAL_DEFECT -> NOT_A_DEFECT: the probe "
                f"found no behavioural difference at any value tested")
            v = "NOT_A_DEFECT"
            out["reason"] = ("harness override: the verifier's own probe showed "
                             "identical results for old and new code at every "
                             "value it tested. " + str(out.get("reason", ""))[:200])
            stats["probe_overrides"] = stats.get("probe_overrides", 0) + 1
        # A deleted guard is never closed by the verifier alone, regardless of
        # how confident its reasoning sounds.
        if v == "NOT_A_DEFECT" and deletes_a_protection(f):
            log(f"  [verify] rejection concerns a DELETED protection -- flooring "
                f"at UNSURE so a human sees it")
            f["verdict"] = "UNSURE"
            f["downgraded_from"] = "NOT_A_DEFECT"
            f["verdict_reason"] = ("harness floor: the finding is about a removed "
                                   "guard, where equivalence depends on ORDERING "
                                   "that verifiers repeatedly get wrong. "
                                   + str(out.get("reason", ""))[:200])
            stats["protection_floor"] = stats.get("protection_floor", 0) + 1
            v = "UNSURE"
        elif v == "REAL_DEFECT" and unsound:
            # Mirror of the rescue below (2026-10-02, s5 96a36dee6870): the probe
            # admitted it was reasoning from an assumption about code it could not
            # see, and confirmed anyway. An unsound probe is "not evidence" in BOTH
            # directions -- it may not establish a defect any more than it may
            # dismiss one. UNSURE keeps it in front of a human without a hard FAIL.
            log(f"  [verify] REAL_DEFECT rests on an unsound probe ({unsound!r}) -- "
                f"demoting to UNSURE")
            f["verdict"] = "UNSURE"
            f["downgraded_from"] = "REAL_DEFECT"
            stats["unsound_real_demoted"] = stats.get("unsound_real_demoted", 0) + 1
            v = "UNSURE"
        elif v == "NOT_A_DEFECT" and f.get("severity") == "high" and not probed:
            f["verdict"] = "UNSURE"
            f["downgraded_from"] = "NOT_A_DEFECT"
            v = "UNSURE"
        else:
            f["verdict"] = v
        f["verdict_reason"] = out.get("reason", "")
        f["concrete_trigger"] = out.get("concrete_trigger", "")
        # Persist the entire verifier payload rather than hand-picking fields.
        # Hand-picking has now silently dropped a required field three separate
        # times in one session (`probe`, then `probe_shows_difference`), each
        # time making a real behaviour unmeasurable and the next diagnosis wrong.
        for k, val in out.items():
            if k not in ("verdict", "reason"):
                f[k] = val
        stats[f"verdict_{v.lower()}"] = stats.get(f"verdict_{v.lower()}", 0) + 1
        if f.get("downgraded_from"):
            stats["high_severity_rescued"] = stats.get("high_severity_rescued", 0) + 1
    return findings


def stage_diagnose(model: Model, symptom: str, code: str, context: str,
                   stats: dict, section_chars: int = 6000,
                   sections: int = 4) -> list[dict]:
    # Quote grounding still uses the FULL file, not the excerpt -- a quote from
    # a section we did not show would be fabrication, but a quote the model
    # recalls correctly from anywhere in the real file is not.
    corpus = squash_quotes(" ".join(normalise(l) for l in code.splitlines()))
    shown, narrowed = rank_sections(code, symptom, context, section_chars, sections)
    if narrowed:
        log(f"  file is {len(code):,} chars -- narrowed to the {sections} "
            f"sections most relevant to the symptom ({len(shown):,} chars)")
        stats["narrowed"] = 1
    user = (f"REPORTED SYMPTOM: {symptom}\n\n"
            f"CONTEXT: {context}\n\n"
            + (f"CODE (the sections most relevant to this symptom, with line "
               f"numbers; the file is larger than what is shown):\n```\n{shown}\n```"
               if narrowed else f"CODE:\n```\n{shown}\n```")
            + f"\n\nWhat is causing the reported symptom?")
    out = model.chat_json(DIAGNOSE_SYS, user, DIAGNOSE_SCHEMA)
    if out is None:
        stats["diagnose_parse_fail"] += 1
        return []
    causes = []
    for c in (out.get("causes") or []):
        q = (c.get("quote") or "").strip()
        if not quote_is_real(q, corpus):
            stats["quote_rejected"] += 1
            stats.setdefault("rejected_quotes", []).append(q[:160])
            continue
        if len((c.get("refuting_evidence") or "").strip()) < 15:
            # An unfalsifiable hypothesis is not a diagnosis.
            stats["unfalsifiable"] += 1
            continue
        causes.append(c)
    return causes


# -------------------------------------------------------------------- main

BATCH_CHARS_FRACTION = 0.35   # of the context window, in chars (num_ctx * 4)
SEV = {"high": 0, "medium": 1, "low": 2}


def _anchor(f: dict) -> str:
    """`path:line` for a finding, derived from its hunk header.

    Added 2026-08-31: the worker-owner session said the report is "usable for
    triage, thin for a gate" and asked for file:line anchors and a top-line
    verdict. "hunk 0" is not an address a human can jump to.
    """
    ht = f.get("hunk_text") or ""
    quote = (f.get("quote") or "").strip()
    # The @@ header is a separate attribute on Hunk, NOT part of text(); reading
    # only hunk_text silently produced anchors with no line number at all.
    m = re.search(r"@@\s*-(\d+)(?:,\d+)?\s+\+(\d+)",
                  (f.get("hunk_header") or "") + "\n" + ht)
    if not m or not quote:
        return f"{f.get('path','?')}"
    new_ln = int(m.group(2))
    old_ln = int(m.group(1))
    # Iterate ALL lines: the @@ header is a separate attribute, so slicing [1:]
    # here skipped a real content line and shifted every anchor by one. Caught
    # only by checking a produced anchor against the actual file -- a plausible
    # line number is not a correct one.
    for line in ht.splitlines():
        # hunk_text on a REVIEW finding is the prompt rendering ("FILE: x", "HUNK: @@ ..",
        # a ```diff fence). Those wrapper lines are not file lines: counting them as
        # context shifted every anchor by 3 (lib/fmt.ts:5 on a 3-line file), and the
        # gate's ungrounded-line drop then discarded a real finding. A later HUNK:
        # header in a batch re-bases the counters.
        if line.startswith("```") or line.startswith("FILE:"):
            continue
        if line.startswith("HUNK:"):
            _hm = re.search(r"@@\s*-(\d+)(?:,\d+)?\s+\+(\d+)", line)
            if _hm:
                old_ln, new_ln = int(_hm.group(1)), int(_hm.group(2))
            continue
        if line.startswith("+"):
            if line[1:].strip() == quote:
                return f"{f.get('path','?')}:{new_ln}"
            new_ln += 1
        elif line.startswith("-"):
            if line[1:].strip() == quote:
                return f"{f.get('path','?')}:{old_ln} (removed)"
            old_ln += 1
        else:
            new_ln += 1
            old_ln += 1
    return f"{f.get('path','?')}"


def _verdict(real: list, unsure: list, truncated: bool = False,
             unreviewed: int = 0) -> tuple[str, str]:
    """Gate verdict. A gate must say pass/fail, not just narrate.

    `unreviewed` = review/removal calls whose answer was unusable (parse failure).
    That part of the diff was never looked at, so -- like truncation -- nothing
    found is UNPROVEN, not PASS (2026-10-05: before this a parse-failed batch
    simply dropped out and the run could still read PASS).

    A genuinely established defect (`real`) still FAILs even if the model was
    truncated elsewhere -- a real defect found before the cap is still a real
    defect. But when NOTHING was established and the model hit its token cap, the
    review did not finish: that is UNPROVEN (inconclusive), never a clean PASS.
    A truncated run silently read as "nothing survived" is how a review that
    never looked can launder a change through the gate.
    """
    if any(f.get("severity") == "high" for f in real):
        return ("FAIL", "a high-severity defect survived adversarial verification")
    if real:
        return ("FAIL", "a defect survived adversarial verification")
    if truncated:
        return ("UNPROVEN",
                "the model hit its token cap and the review did not complete -- "
                "inconclusive, not a pass"
                + (f"; {len(unsure)} finding(s) also left unsettled" if unsure else ""))
    if unreviewed:
        return ("UNPROVEN",
                f"{unreviewed} review call(s) returned unusable output, so part of the "
                f"change was never reviewed -- inconclusive, not a pass"
                + (f"; {len(unsure)} finding(s) also left unsettled" if unsure else ""))
    if unsure:
        return ("PASS WITH CAVEATS",
                f"{len(unsure)} finding(s) the harness could not settle either way")
    return ("PASS", "nothing survived verification")


def build_report(mode: str, findings: list[dict], causes: list[dict],
                 stats: dict, model: Model, elapsed: float, target: str) -> str:
    out = [f"# {mode.title()}: {target}", ""]
    if mode == "review":
        real = [f for f in findings if f.get("verdict") == "REAL_DEFECT"]
        unsure = [f for f in findings if f.get("verdict") == "UNSURE"]
        dropped = [f for f in findings if f.get("verdict") == "NOT_A_DEFECT"]
        v, why = _verdict(real, unsure, truncated=bool(getattr(model, "truncated", 0)),
                          unreviewed=int(stats.get("review_parse_fail") or 0))
        out += [f"## VERDICT: {v}", f"_{why}._", ""]
        if real or unsure:
            out += ["| # | severity | where | what |", "|---|---|---|---|"]
            ranked = sorted(real + unsure,
                            key=lambda x: (SEV.get(x.get("severity"), 1),
                                           0 if x.get("verdict") == "REAL_DEFECT" else 1))
            for i, f in enumerate(ranked, 1):
                claim = (f.get("claim") or "").replace("|", "\\|")[:100]
                out += [f"| {i} | {f.get('severity','medium').upper()} | "
                        f"`{_anchor(f)}` | {claim} |"]
            out += [""]
        if not real and not unsure:
            out += ["**No defects found.**", "",
                    f"{len(dropped)} candidate finding(s) were raised during review "
                    f"and every one was rejected by the verification pass as not a "
                    f"real defect. On a clean change this is the expected result."
                    if dropped else
                    "The review pass raised nothing. On a clean change this is the "
                    "expected result.", ""]
        for label, group in (("Defects", real), ("Uncertain — needs a human", unsure)):
            if not group:
                continue
            out += [f"## {label}", ""]
            for f in sorted(group, key=lambda x: SEV.get(x.get("severity"), 1)):
                out += [f"### [{f.get('severity','medium').upper()}] {f['claim']}",
                        f"`{_anchor(f)}`", "",
                        f"```\n{f['quote']}\n```",
                        f"**How it fails:** {f['failure_scenario']}"]
                if f.get("concrete_trigger"):
                    out += [f"**Verified trigger:** {f['concrete_trigger']}"]
                out += [""]
        if dropped:
            out += ["## Rejected by verification", "",
                    "Raised during review, then knocked down when checked against "
                    "the code by a pass that did not write them. Listed for audit, "
                    "not for action.", ""]
            for f in dropped:
                out += [f"- ~~{f['claim']}~~ — {f.get('verdict_reason','')[:220]}"]
            out += [""]
    else:
        if not causes:
            out += ["**No supportable root cause found in the code provided.**", "",
                    "The code shown does not explain the symptom. That is a result, "
                    "not a failure: it points at code that was not supplied, at the "
                    "environment, or at the symptom being misreported.", ""]
        for i, c in enumerate(causes, 1):
            out += [f"## {i}. {c['hypothesis']}", "",
                    f"```\n{c['quote']}\n```",
                    f"**Mechanism:** {c['mechanism']}",
                    f"**Confirm by:** {c['confirming_evidence']}",
                    f"**Ruled out if:** {c['refuting_evidence']}", ""]

    out += ["## How this was produced", "",
            f"- Model `{model.model}`, num_ctx {model.num_ctx:,}, "
            f"think={'on' if model.think else 'off'} — {model.calls} calls, "
            f"{model.total_s:.0f}s of model time"
            + (f", {model.truncated} truncated" if model.truncated else ""),
            f"- Wall clock {elapsed:.0f}s",
            f"- Filtered before you saw them: {stats['quote_rejected']} finding(s) "
            f"quoting code that is not in the diff, "
            f"{stats['no_failure_scenario']} with no concrete failure scenario"
            + (f", {stats.get('verdict_not_a_defect',0)} rejected by verification"
               if stats.get('verdict_not_a_defect') else ""),
            "",
            "*Every quote above was checked as a literal substring of the real "
            "input before the finding was allowed to appear, and every finding "
            "had to supply a concrete failure scenario and survive an adversarial "
            "re-check.*"]
    return "\n".join(out)


def resolve_diff_path(p) -> Path | None:
    """The diff to review, or None when it is gone. A dashboard/run-status CLEAR
    moves a finished job's <id>.diff into LOG_DIR/archive/ (ollama-queue-api
    _archive_run) -- and a queued 'secondop-<id>' review can run AFTER that
    (2026-10-02, secondop-d4e286641d56 21763ad589cc: parent cleared 11s after the
    second opinion was enqueued -> FileNotFoundError traceback, harness-class
    failure). Look in archive/ before giving up."""
    p = Path(p).expanduser()
    if p.is_file():
        return p
    alt = p.parent / "archive" / p.name
    return alt if alt.is_file() else None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["review", "diagnose", "refute"], default="review")
    ap.add_argument("--diff", help="path to a unified diff (review mode)")
    ap.add_argument("--code", help="path to the source file (diagnose mode)")
    ap.add_argument("--symptom", default="", help="reported problem (diagnose mode)")
    ap.add_argument("--intent", default="No stated intent.",
                    help="what the change claims to do; a real lift per the "
                         "diff-aware probe, so pass the commit message")
    ap.add_argument("--context", default="")
    ap.add_argument("--model", default="qwen3-coder-30b-ctx64k:latest")
    ap.add_argument("--host", default=DEFAULT_HOST)
    ap.add_argument("--batch-chars", type=int, default=0,
                    help="max chars of diff per review call; 0 = derive from --num-ctx "
                         "(measured, not guessed -- see BATCH_CHARS_FRACTION)")
    ap.add_argument("--num-ctx", type=int, default=32768)
    ap.add_argument("--think", action="store_true")
    ap.add_argument("--no-verify", action="store_true")
    ap.add_argument("--out", help="output directory")
    ap.add_argument("--cwd", help="output directory (queue --runner contract)")
    ap.add_argument("--task-file", help="JSON task file (queue --runner contract)")
    args = ap.parse_args()

    # The queue's --runner contract passes exactly --model/--host/--num-ctx/
    # --cwd/--task-file, so everything else has to arrive inside the task file.
    _schema_start = _effort = None
    if args.task_file:
        try:
            spec = json.loads(Path(args.task_file).read_text())
        except Exception as e:
            print(f"ERROR: bad --task-file: {e}", file=sys.stderr)
            return 2
        for k in ("mode", "diff", "code", "symptom", "intent", "context"):
            if spec.get(k):
                setattr(args, k, spec[k])
        # Opt-in thinking review (2026-11-07, review-test arm q36t): the queue can't pass --think, so a task file
        # may ask for it. Only a literal true counts, and no producer writes this key, so other jobs run as before.
        if spec.get("review_think") is True:
            args.think = True
        # Review-test arms (2026-11-09, fair Gemma re-test): start Darkbloom's schema ladder at a looser rung, and/or
        # send reasoning_effort. Only these exact values count; no producer writes these keys.
        if spec.get("review_schema_start") in ("relaxed", "none"):
            _schema_start = spec["review_schema_start"]
        if spec.get("review_reasoning_effort") in ("low", "medium", "high"):
            _effort = spec["review_reasoning_effort"]
        _repo, _base = spec.get("repo"), spec.get("base")
        _spec_budget = spec.get("ref_budget")
    outdir = Path(args.out or args.cwd or ".").expanduser().resolve()
    outdir.mkdir(parents=True, exist_ok=True)

    t0 = time.time()
    model = Model(args.host, args.model, args.num_ctx, think=args.think)
    model.schema_start, model.reasoning_effort = _schema_start, _effort
    stats = {"quote_rejected": 0, "no_failure_scenario": 0, "review_parse_fail": 0,
             "verify_parse_fail": 0, "diagnose_parse_fail": 0, "unfalsifiable": 0}
    findings: list[dict] = []
    causes: list[dict] = []

    if args.mode == "refute":
        # GATE FINDING CHECK (gate_finding_check.py, 2026-10-04): turn each reviewer
        # finding into a proposed REPRODUCER. This side only PROPOSES code; it never
        # executes it and never decides -- gate-on-complete runs the reproducers in a
        # throwaway copy of the gated tree and classifies the outcome itself.
        import gate_finding_check as _gfc
        return _gfc.runner_main(spec if args.task_file else {}, model, outdir, log=log)

    if args.mode == "review":
        if not args.diff:
            print("ERROR: --mode review needs --diff", file=sys.stderr)
            return 2
        _dp = resolve_diff_path(args.diff)
        if _dp is None:
            # Nothing to review is a decided, CLEAN outcome -- not a crash, and not
            # a verdict about the code. No report.md (a PASS here would be fake
            # corroboration); skipped.json tells the gate's merge why.
            _why = f"diff unavailable: {args.diff} (and no archive/ copy)"
            print(f"SKIPPED: {_why}", file=sys.stderr)
            (outdir / "skipped.json").write_text(json.dumps({"skipped": True, "reason": _why}))
            return 0
        if _dp != Path(args.diff).expanduser():
            log(f"  diff was archived (job cleared) -- reviewing {_dp}")
        diff = _dp.read_text()
        hunks = parse_diff(diff)
        if not hunks:
            print("ERROR: no hunks parsed from the diff", file=sys.stderr)
            return 3
        if args.task_file and _repo:
            global REF_BUDGET
            REF_BUDGET = effective_ref_budget(args.num_ctx, _spec_budget)
            if REF_BUDGET > 0:
                load_ref_sources(_repo, _base, {h.path for h in hunks})
            log(f"  referenced-definitions budget {REF_BUDGET} chars at num_ctx {args.num_ctx}")
        hunks, _gen = drop_generated(hunks)
        if _gen:
            log(f"  skipping {len(_gen)} generated/vendored file(s): {', '.join(_gen)}")
        if not hunks:
            # GATE REVIEW HOLE (2026-10-02). Returning without a report.md left the
            # gate record at verdict=pass-pending-review / review="failed (no report
            # produced)" -- never terminal -- so every slice whose job changed only
            # generated files (e.g. tsconfig.tsbuildinfo on an auto-author job) waited
            # out the slicer's 20-min lost-event grace, and the escalation pass logged
            # "no report at <id>-review/report.md" (c1b8126a2db3, 5d8e1e3e46e1, ...).
            # Nothing to review IS a decided outcome: say so in a real report.
            print("no reviewable changes: every changed file is generated or vendored",
                  file=sys.stderr)
            (outdir / "report.md").write_text(
                f"# Review: {args.diff}\n\n## VERDICT: PASS\n"
                f"_nothing to review: every changed file is generated or vendored "
                f"({', '.join(_gen) or 'none'}); the gate's other checks still apply._\n")
            return 0
        # Ground quotes against the KEPT hunks only, not the raw diff. A quote is
        # allowed to appear iff it is in content the review actually looked at;
        # building the corpus from `diff` let a quote of a dropped generated or
        # scaffold file ground as "real", which is exactly the class of content a
        # review must never speak to.
        corpus = diff_corpus("\n".join(h.header + "\n" + h.text() for h in hunks))
        log(f"review: {len(hunks)} hunk(s), model {args.model}")
        # Derive the review batch from the context window rather than a fixed
        # constant. The old hardcoded 9000 was ~7% of a 32k-token window, and on
        # a real 858-line refactor it forced lib/rivian.ts (28KB, the file
        # holding the WHOLE refactor) to be split across three batches -- which
        # is exactly why a cross-hunk function extraction read as a deletion.
        #
        # A/B'd over all 21 fixtures at 9000 vs 45000: IDENTICAL, 10/10
        # detection and 0 FP on both. Read that honestly -- the bench measured
        # NO HARM, not benefit: every fixture is small and single-file, so the
        # batch size never binds there. The upside is only visible on large
        # multi-file diffs, which this bench does not contain. Adopted because
        # it is free on everything measurable and principled on what is not.
        _bc = args.batch_chars or max(9000, int(args.num_ctx * 4 * BATCH_CHARS_FRACTION))
        findings = stage_review(model, hunks, args.intent, args.context, corpus, stats,
                                batch_chars=_bc)
        findings += stage_removals(model, hunks, args.intent, args.context,
                                   corpus, stats)
        _all_added = {a.strip() for hh in hunks for a in hh.added if a.strip()}
        # MOVED-NOT-DELETED filter for the model's removal pass.
        # Measured on a real 858-line refactor (ev-dashboard bc0132e): all THREE
        # surfaced findings were false positives from one mechanism -- code
        # removed in one hunk and re-added verbatim ~200 lines earlier in
        # another. Hunk-local analysis cannot see the reappearance, so a pure
        # function extraction reads as a deletion of validation logic. Whether a
        # removed line still exists in the diff is decidable, so the harness
        # decides it instead of asking the model to notice.
        _kept = []
        for _f in findings:
            _q = (_f.get("quote") or "").strip()
            if (_f.get("source") == "removal-pass" and _q and _q in _all_added):
                stats["moved_not_deleted"] = stats.get("moved_not_deleted", 0) + 1
                stats.setdefault("moved_examples", []).append(_q[:70])
                continue
            # SOLE-MECHANISM-BUT-REPLACED filter for ANY model finding (review
            # or removal-pass). Measured on cce40aa3eeba (resell BG-credited):
            # the model raised two HIGH findings -- "The deleted line was the
            # sole mechanism that added an order ID to `creditedOrderIds`" and
            # "... that marked an order as BG-credited" -- each anchored to a `-`
            # line whose crediting was REPLACED on the very next `+` line
            # (`creditedOrderIds.add(x.id)` -> `creditTrackingForOrder(x.id,
            # ...)`). The removed line is not re-added verbatim, so the older
            # moved-not-deleted check (verbatim, removal-pass only) missed it,
            # and a review-source finding was never checked at all. The premise
            # -- "sole mechanism, now gone" -- is decidably false when a
            # structural replacement sits in the same hunk, so the harness
            # decides it instead of trusting the claim. A GENUINE deletion (no
            # similar replacement) is untouched here AND still caught by the
            # harness deletion passes (dropped_members / guard_removed).
            if overturns_sole_mechanism_claim(_f, hunks):
                stats["sole_mechanism_replaced"] = stats.get("sole_mechanism_replaced", 0) + 1
                stats.setdefault("sole_mechanism_examples", []).append(_q[:70])
                log(f"  suppressed 'sole/deleted mechanism' finding -- the removed "
                    f"line is replaced on the added side: {_q[:70]}")
                continue
            _kept.append(_f)
        findings = _kept
        for h in hunks:
            for kept, gone in dropped_members(h):
                findings.append({
                    "hunk": 0, "path": h.path,
                    "quote": next((r.strip() for r in h.removed
                                   if all(g in r for g in gone)), gone[0]),
                    "severity": "high", "source": "harness-dropped-member",
                    "claim": (f"{', '.join(repr(g) for g in gone)} "
                              f"{'was' if len(gone) == 1 else 'were'} silently "
                              f"dropped from a literal collection that still "
                              f"contains {kept}."),
                    "failure_scenario": (
                        f"Every code path that relied on {gone[0]!r} being in that "
                        f"collection now takes the other branch. Because {kept} "
                        f"survived, the collection still looks correct at a glance "
                        f"and any test covering only {kept.split(',')[0].strip()} "
                        f"still passes."),
                    "hunk_text": h.text(), "hunk_header": h.header, "verdict": "REAL_DEFECT",
                    "concrete_trigger": (f"any input where the value is {gone[0]!r}"),
                    "verdict_reason": ("computed by the harness: set membership "
                                       "before vs after"),
                    "skip_verify": True})
                stats["dropped_members"] = stats.get("dropped_members", 0) + 1
            # comparison_verdict() has always KNOWN the answer for changed
            # boundary comparisons -- but until 2026-08-31 it was only consulted
            # inside stage_verify(), to adjudicate a finding the model had
            # already raised. When the model said nothing, the harness's own
            # correct verdict was computed nowhere and used never. Measured on
            # bug-window (`placed >= cutoff` -> `placed > cutoff`): the rule
            # returns True on 3/3 runs while the full agent reported CLEAN,
            # because no model finding existed for it to adjudicate. Emitting it
            # directly: 2 seeded bugs caught, 0 false positives across 10 decoys.
            if comparison_verdict(h) is True:
                _old = next((r.strip() for r in h.removed if _CMP_RE.search(r)), "")
                _new = next((a.strip() for a in h.added if _CMP_RE.search(a)), "")
                findings.append({
                    "hunk": 0, "path": h.path, "quote": _old or _new,
                    "severity": "high", "source": "harness-comparison",
                    "claim": (f"The comparison changed from `{_old}` to `{_new}`, and "
                              f"the two are NOT equivalent -- they disagree for at "
                              f"least one value."),
                    "failure_scenario": (
                        f"Evaluating both forms over an integer range finds a value "
                        f"where the old condition and the new one differ, so every "
                        f"input at that boundary now takes the other branch. Boundary "
                        f"cases are exactly what tests written around the old "
                        f"behaviour tend not to cover."),
                    "hunk_text": h.text(), "hunk_header": h.header, "verdict": "REAL_DEFECT",
                    "concrete_trigger": "a value at the boundary between the two forms",
                    "verdict_reason": ("computed by the harness: both comparisons "
                                       "evaluated over an integer range"),
                    "skip_verify": True})
                stats["comparison_emitted"] = stats.get("comparison_emitted", 0) + 1
            _gr = guard_removed_early_exit(h, _all_added)
            if _gr:
                _cond, _exit = _gr
                findings.append({
                    "hunk": 0, "path": h.path, "quote": _cond,
                    "severity": "medium", "source": "harness-guard-removed",
                    "claim": (f"The guard `{_cond}` was deleted, making `{_exit}` "
                              f"unconditional. The fall-through path it protected is "
                              f"now unreachable."),
                    "failure_scenario": (
                        f"Previously, when `{_cond}` was false, control fell through to the "
                        f"code after this block; now it never does. Whatever that "
                        f"fall-through reached no longer runs for any input, and any local "
                        f"used only by the deleted condition is now dead. "
                        f"CHECK THE FALL-THROUGH TARGET to judge severity: if it merely "
                        f"reached another equivalent return this is dead code, but if it "
                        f"reached a refresh, retry, fetch, or a different return shape, "
                        f"that behaviour is lost. The harness can prove the guard is gone; "
                        f"it cannot prove from this hunk alone what the lost path did."),
                    "hunk_text": h.text(), "hunk_header": h.header, "verdict": "REAL_DEFECT",
                    "concrete_trigger": f"any input for which `{_cond}` was previously false",
                    "verdict_reason": ("computed by the harness: guard present before, "
                                       "absent after, early exit retained"),
                    "skip_verify": True})
                stats["guard_removed"] = stats.get("guard_removed", 0) + 1
            for binary, old_sub, new_sub in subcommand_swaps(h):
                findings.append({
                    "hunk": 0, "path": h.path,
                    "quote": next((r.strip() for r in h.removed
                                   if f"{binary} {old_sub}" in r), f"{binary} {old_sub}"),
                    "severity": "medium", "source": "harness-subcommand-swap",
                    "claim": (f"`{binary} {old_sub}` was changed to "
                              f"`{binary} {new_sub}` -- a different subcommand of "
                              f"the same external command."),
                    "failure_scenario": (
                        f"Whether `{binary} {new_sub}` returns the same thing as "
                        f"`{binary} {old_sub}` cannot be determined from this diff; "
                        f"it depends on {binary}'s own behaviour. If the two differ, "
                        f"every caller of this code silently gets different data "
                        f"while the change is described as behaviour-preserving. "
                        f"Confirm against {binary}'s documentation."),
                    "hunk_text": h.text(), "hunk_header": h.header, "verdict": "UNSURE",
                    "verdict_reason": ("flagged by the harness: external-command "
                                       "equivalence is not verifiable from a diff"),
                    "skip_verify": True})
                stats["subcommand_swaps"] = stats.get("subcommand_swaps", 0) + 1
        before = len(findings)
        findings = dedupe(findings, stats)
        log(f"  {len(findings)} finding(s) survived grounding"
            + (f" ({before - len(findings)} duplicate(s) merged)"
               if before != len(findings) else ""))
        if findings and not args.no_verify:
            log("verification pass")
            findings = stage_verify(model, findings, args.intent, stats)
            log(f"  REAL_DEFECT {stats.get('verdict_real_defect',0)}  "
                f"NOT_A_DEFECT {stats.get('verdict_not_a_defect',0)}  "
                f"UNSURE {stats.get('verdict_unsure',0)}")
        target = hunks[0].path if hunks else args.diff
    else:
        if not args.code:
            print("ERROR: --mode diagnose needs --code", file=sys.stderr)
            return 2
        code = Path(args.code).read_text()
        log(f"diagnose: {args.symptom[:70]!r}")
        causes = stage_diagnose(model, args.symptom, code, args.context, stats)
        log(f"  {len(causes)} supportable cause(s)")
        target = args.code

    elapsed = time.time() - t0
    report = build_report(args.mode, findings, causes, stats, model, elapsed, target)
    (outdir / "report.md").write_text(report)
    (outdir / "run.json").write_text(json.dumps({
        "mode": args.mode, "model": args.model, "num_ctx": args.num_ctx,
        "target": target, "intent": args.intent,
        "findings": [{k: v for k, v in f.items() if k != "hunk_text"}
                     for f in findings],
        "causes": causes, "stats": stats, "model_calls": model.calls,
        "elapsed_s": round(elapsed, 1),
        "darkbloom_stats": _dbk.stats(),
        "call_usage": getattr(model, "usage", []),
        "ref_budget": REF_BUDGET,
        "schema_start": model.schema_start, "reasoning_effort": model.reasoning_effort,
    }, indent=2))
    log(f"done in {elapsed:.0f}s -> {outdir/'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
