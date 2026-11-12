#!/usr/bin/env python3
"""Post-hoc scorers for the v8 round — everything derivable from what the round
already archived, costing zero run-hours.

These exist because the CSV records OUTCOMES and the transcripts record BEHAVIOUR,
and the routing decision needs behaviour. `files_changed=0` is the clearest case:
it is written identically by a model that explored for 25 iterations and never
wrote anything, and by a model that correctly recognised the feature already
existed. Those are opposite results. Only the transcript separates them.

Scorers, and the decision each one informs:

  time_to_first_mutation  Which failure is this -- all looking and no doing, or
                          all doing and no looking? Opposite fixes.
  self_verify_count       Did the model run the build/test ITSELF before claiming
                          done? The preregistration makes this a POSITIVE
                          requirement for `unsupervised`, because "was not caught
                          lying three times" is thin evidence for trust, while
                          "checks its own work" is a mechanism.
  churn_ratio             Repeated identical tool calls. Distinguishes an
                          iter_cap row that would benefit from more budget from
                          one that was spinning and never would.
  diff_magnitude          A 40-line surgical change and a 2000-line bulldozer
                          both read as files_changed=6. This quantifies the
                          round's own "follow existing patterns" instruction.

NOT scored here: claim-vs-verify calibration. That needs a human (or a separate
judged pass) to read the model's final summary against ground truth, and the
class list is fixed in v8-PREREGISTRATION.md §2. Deliberately not automated with
an LLM judge -- that would add a second unvalidated instrument to a project whose
recurring failure mode is unvalidated instruments.
"""
import argparse
import csv
import glob
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# Import the worker's own embedded-tool-call parser rather than maintaining a
# second one here. Two parsers for one wire format is how the original scorer
# bug happened: this file only ever read structured `tool_calls`, so it scored
# deepseek-r1:32b and qwen2.5-coder:14b as "never mutated / never self-verified"
# across 100% of their rows while the worker was happily executing their calls.
# The worker lives outside this directory and is not importable as a package,
# so load it by path. Deliberately soft-failing: if it is absent, scoring falls
# back to the local parser rather than refusing to run.
#
# Points at ollama-worker-v7.py, NOT the -v8 copy: bakeoff-v8-lib.sh:69 sets
# WORKER=ollama-worker-v7.py, so v7 is the parser that ACTUALLY executed every
# call in this round. Scoring with the same code makes the scorer agree with
# ground truth by construction. The two copies are byte-identical in
# extract_manual_tool_calls today, but pinning to the one that ran removes the
# latent drift the post-run review flagged (2026-08-24).
_WORKER_PATH = Path.home() / "bin" / "ollama-worker-v7.py"
_worker_extract = None
try:
    import importlib.util as _ilu
    _spec = _ilu.spec_from_file_location("ollama_worker_v8", _WORKER_PATH)
    if _spec and _spec.loader:
        _mod = _ilu.module_from_spec(_spec)
        _spec.loader.exec_module(_mod)
        _worker_extract = getattr(_mod, "extract_manual_tool_calls", None)
except Exception as _e:                                             # noqa: BLE001
    print(f"[score] worker parser unavailable ({_e}); using local fallback",
          file=sys.stderr)

BASE = Path("/Users/user/Desktop/GitHub Projects")
OUTDIR = BASE / "model-buildoff-2026-08-22"

MUTATORS = ("write_file", "edit_file")
# A "self-verification" is the model running something that can tell it whether
# its own work is correct, before claiming done.
#
# `tsc --noEmit` and `swift test` added 2026-08-24 (Fable's review). Their
# absence was not cosmetic: §2 of the preregistration makes self-verify a
# POSITIVE per-passing-run requirement for an `unsupervised` verdict, so a
# verifier missing from this list silently costs a model credit it earned.
# Two qwen3.8 photo rows (base r2, repomap r2) ran tsc + eslint and scored 0
# for it; counting tsc moves qwen3.8 from 10/15 to 12/15 self-verified.
#
# `tsc --noEmit` is listed both bare and npx-prefixed because both forms appear
# in the transcripts and the match below is a plain substring test.
VERIFIERS = ("npm run build", "swift build", "swift run", "pytest",
             "python test_", "python3 test_", "npm test", "go test",
             "tsc --noEmit", "npx tsc --noEmit", "swift test",
             "/plex-bakeoff-venv/bin/python test_")


def load_transcript(path):
    try:
        return json.loads(Path(path).read_text())
    except Exception:                                               # noqa: BLE001
        return None


# Some models in this round (deepseek-r1:32b, qwen2.5-coder:14b) don't use
# Ollama's structured tool_calls field at all -- their harness has them emit
# the call as JSON embedded directly in the message content instead (raw, or
# ```json-fenced```), which the harness itself parses separately to actually
# execute the tool. The original version of this scorer only ever looked at
# tool_calls, so it silently scored 100% of these two models' rows as "never
# mutated" / "never self-verified" regardless of what they actually did --
# confirmed against files_changed and the harness's own [tool result for
# edit_file]: OK messages in the same transcripts. Found post-hoc while
# answering "does v8 need anything fixed before v9", 2026-08-24.
_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)
# Anchors the start of an embedded tool-call object -- some messages are pure
# JSON, some are ```json-fenced```, and some (seen in deepseek-r1:32b's actual
# mutating calls specifically) are prose followed by the JSON at the end with
# no fence at all. raw_decode from this anchor handles all three: it parses
# just the balanced JSON object starting here and ignores anything after.
_NAME_ANCHOR_RE = re.compile(r'\{\s*"name"\s*:')
_decoder = json.JSONDecoder()


def _parse_embedded_calls(text):
    """Find every {"name": ..., "arguments": ...} object in text, in order.
    Some runs batch several calls (plus fabricated "[After X response]"
    narration) into a single message instead of alternating turn-by-turn --
    the anchor regex + repeated raw_decode walks past each one regardless."""
    if not text:
        return
    pos = 0
    while True:
        anchor = _NAME_ANCHOR_RE.search(text, pos)
        if not anchor:
            return
        try:
            obj, end = _decoder.raw_decode(text, anchor.start())
        except (json.JSONDecodeError, ValueError):
            pos = anchor.start() + 1  # skip past a false-positive anchor and keep looking
            continue
        if isinstance(obj, dict) and "name" in obj:
            yield obj
        pos = end


def extract_calls(m):
    """Yield (name, args_str) for every tool call in one assistant message,
    from tool_calls if present, else from JSON embedded in content."""
    for tc in (m.get("tool_calls") or []):
        fn = ((tc.get("function") or {}).get("name") or "")
        args = (tc.get("function") or {}).get("arguments")
        args_s = json.dumps(args, sort_keys=True) if not isinstance(args, str) else args
        yield fn, args_s

    if m.get("tool_calls"):
        return  # structured calls present -- content is prose, don't double-count

    content = m.get("content") or ""
    if not content.strip():
        return

    # Prefer the WORKER'S OWN parser (ollama-worker-v8.py). It is the code that
    # actually executed these calls during the run, so using it here makes the
    # scorer agree with ground truth by construction instead of by coincidence.
    # It also handles cases this file's local parser does not: Python-style
    # triple-quoted values (deepseek-r1:32b emits these -- 2/69 rows were being
    # dropped), {"arguments": ..., "name": ...} key order, and string-aware
    # brace matching so an unbalanced brace inside generated code doesn't
    # truncate the call. Fall back to the local parser only if the worker
    # cannot be imported, so scoring still runs on a machine without it.
    if _worker_extract is not None:
        found = list(_worker_extract(content))
    else:
        found = list(_parse_embedded_calls(content))
    if not found:
        fence = _FENCE_RE.search(content)
        if fence:
            inner = fence.group(1)
            found = list(_worker_extract(inner)) if _worker_extract is not None \
                else list(_parse_embedded_calls(inner))
    for obj in found:
        args = obj.get("arguments")
        args_s = json.dumps(args, sort_keys=True) if not isinstance(args, str) else (args or "")
        yield obj["name"], args_s


_GREP_RE = re.compile(r'\bgrep\b|\brg\b|\bag\b')


def _read_pattern(read_events, grep_before_first_read):
    """Classify how a run approached reading a large source file (v9 R1
    diagnostic, added 2026-08-24 per Fable's Q4). read_events is the ordered
    list of read_file arg-dicts. Distinguishes the behaviour the read_file cap
    is meant to induce (grep-to-symbol / windowed paging) from the behaviour
    that ceilinged the context in v8 (a full read, now impossible, or blind
    sequential paging through thousands of lines). This is what explains any
    residual config_ceiling after the cap lands, and says whether the cap or
    the grep hint did the work."""
    if not read_events:
        return "no_read"
    if grep_before_first_read:
        return "grep_first"
    paged = sum(1 for a in read_events if _int_arg(a, "offset") > 0)
    if paged >= 1:
        return "paged"
    # Repeated reads of the same path with no offset = re-reading the same
    # window, i.e. not making progress through the file.
    return "windowed_top" if len(read_events) == 1 else "reread_top"


def _int_arg(args, key):
    try:
        return int((args or {}).get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0


def score_transcript(tr):
    """Return the behavioural metrics for one run."""
    msgs = (tr or {}).get("messages") or []
    first_mutation = None
    self_verify = 0
    calls = []
    iteration = 0
    read_events = []          # ordered read_file arg-dicts, for read-pattern
    seen_read = False
    grep_before_first_read = False

    for m in msgs:
        if m.get("role") == "assistant":
            iteration += 1
            for fn, args_s in extract_calls(m):
                calls.append(f"{fn}:{args_s}")
                if fn in MUTATORS and first_mutation is None:
                    first_mutation = iteration
                # The model running a build/test of its own accord.
                if fn == "run_bash" and any(v in (args_s or "") for v in VERIFIERS):
                    self_verify += 1
                # Read-pattern tracking (R1 diagnostic).
                if fn == "run_bash" and not seen_read and _GREP_RE.search(args_s or ""):
                    grep_before_first_read = True
                if fn == "read_file":
                    seen_read = True
                    try:
                        read_events.append(json.loads(args_s) if args_s else {})
                    except Exception:                                  # noqa: BLE001
                        read_events.append({})

    churn = 0.0
    if calls:
        seen = defaultdict(int)
        for c in calls:
            seen[c] += 1
        repeats = sum(n - 1 for n in seen.values() if n > 1)
        churn = repeats / len(calls)

    return {
        "time_to_first_mutation": first_mutation if first_mutation is not None else -1,
        "self_verify_count": self_verify,
        "churn_ratio": round(churn, 3),
        "tool_calls": len(calls),
        "read_pattern": _read_pattern(read_events, grep_before_first_read),
    }


def diff_magnitude(stem):
    """Lines added+removed in the archived diff, ignoring the porcelain tail."""
    p = OUTDIR / f"{stem}.diff"
    if not p.is_file():
        return -1
    n = 0
    for line in p.read_text(errors="ignore").splitlines():
        if line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            n += 1
    return n


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--csv", default=str(OUTDIR / "results-v8.csv"))
    ap.add_argument("--out", default=str(OUTDIR / "results-v8-behaviour.csv"))
    args = ap.parse_args()

    src = Path(args.csv)
    if not src.is_file():
        print(f"no results yet at {src}", file=sys.stderr)
        return 1

    rows = list(csv.DictReader(src.open()))
    if not rows:
        print("results file is empty", file=sys.stderr)
        return 1

    out_rows = []
    for r in rows:
        tr_path = r.get("transcript", "none")
        tr = load_transcript(tr_path) if tr_path and tr_path != "none" else None
        m = score_transcript(tr) if tr else {
            "time_to_first_mutation": -1, "self_verify_count": -1,
            "churn_ratio": -1, "tool_calls": -1}
        # ANCHOR THE GLOB TO THE MODEL SLUG. The first version globbed
        # "*-{task}-*-{arm}-r{rep}.diff", which matches EVERY model's diff for
        # that cell and then took the first alphabetically -- so diff_lines was
        # wrong for five of every six rows. The archived stem is slug-based, and
        # the slug is the model name with ':' replaced by '-'.
        slug = r["model"].replace(":", "-")
        pat = str(OUTDIR / f"{slug}-{r['task']}-*-{r['arm']}-r{r['rep']}.diff")
        cands = sorted(glob.glob(pat))
        dm = diff_magnitude(Path(cands[0]).stem) if cands else -1
        out_rows.append({
            "model": r["model"], "task": r["task"], "arm": r["arm"], "rep": r["rep"],
            "stop_reason": r.get("stop_reason", ""),
            "files_changed": r.get("files_changed", ""),
            "iterations": r.get("iterations", ""),
            **m,
            "diff_lines": dm,
        })

    fields = ["model", "task", "arm", "rep", "stop_reason", "files_changed",
              "iterations", "time_to_first_mutation", "self_verify_count",
              "churn_ratio", "tool_calls", "read_pattern", "diff_lines"]
    with Path(args.out).open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows(out_rows)
    print(f"wrote {args.out}  ({len(out_rows)} rows)")

    # ---- the one summary that matters for the routing table ----------------
    print()
    print(f"{'model':<28}{'runs':>6}{'self-verified':>15}{'never mutated':>15}{'med churn':>11}")
    print("-" * 75)
    by = defaultdict(list)
    for r in out_rows:
        by[r["model"]].append(r)
    for mdl in sorted(by):
        rs = by[mdl]
        sv = sum(1 for r in rs if (r["self_verify_count"] or 0) > 0)
        nm = sum(1 for r in rs if r["time_to_first_mutation"] == -1)
        ch = sorted(r["churn_ratio"] for r in rs if r["churn_ratio"] >= 0)
        med = ch[len(ch) // 2] if ch else -1
        print(f"{mdl:<28}{len(rs):>6}{f'{sv}/{len(rs)}':>15}{f'{nm}/{len(rs)}':>15}{med:>11}")
    print()
    print("self-verified is a POSITIVE requirement for `unsupervised` in the")
    print("preregistration -- a model that checks its own work has a mechanism for")
    print("catching its own errors; one that merely was not caught lying does not.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
