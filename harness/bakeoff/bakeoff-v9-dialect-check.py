#!/usr/bin/env python3
"""Flag v9 rows voided by the tool-call dialect gap (Fable ruling, 2026-08-24).

NOT a scorer and NOT a worker change. The instrument is frozen for v9; this is a
read-only analysis pass over rows already written, run at the results-read layer
where the voiding decision belongs.

THE SIGNATURE IT LOOKS FOR
--------------------------
A row is instrument-void when the model emitted a tool call the parser could not
receive: dialect text present in the transcript and no file changed. Earlier
calls in the same run may well have executed -- deepseek ran 4 read_file plus a
list_files and still had its edit discarded -- so execution count is context,
never a clearance. The originating case is deepseek-r1:32b emitting otherwise-valid
tool-call JSON whose string values carry Python raw-string prefixes (r"..."),
which is not valid JSON -- both extract_manual_tool_calls and
extract_qwen_xml_tool_calls return 0 calls on it, while a byte-identical control
without the prefixes parses to 1 call.

WHY THE BLAST RADIUS IS THE MODEL, NOT THE CELL (Fable's extension)
-------------------------------------------------------------------
MANUAL=yes is set per model in the roster, so every row that model produces --
debug, photo, clamshell, R3 -- rides the same parser. Checking only the debug
cell would under-count. deepseek-r1:32b is the only MANUAL=yes model on the v9
roster today, but the check is written against the signature rather than the
name so a future manual-tools model is caught automatically.

A row where calls demonstrably executed STANDS. Absence of file changes alone is
not the signature -- a model can legitimately fail without ever emitting a call.
"""
import csv
import re
import sys
from pathlib import Path

BASE = Path("/Users/user/Desktop/GitHub Projects/bakeoff/model-buildoff-2026-08-22")

# Python raw-string prefix on a JSON string value: the captured dialect. THIS is
# the discriminator -- it is the thing the parser provably cannot receive.
RAWSTR = re.compile(r':\s*[rR]["\']')

# The worker's real tool-execution line, verified against a live transcript:
#   [worker] tool read_file({"path": "README.md"}) -> # plex-automation
# An earlier version of this file matched r"\[worker\] tool:" -- with a colon,
# which the worker never emits -- so EXECUTED was always False.
EXECUTED = re.compile(r"\[worker\] tool \w+\(")

# NOT a void signal on its own. "no tool calls in response -- treating as final
# answer, stopping" is the NORMAL way a run ends when the model gives its final
# answer; it appears in most transcripts including successful ones. Recorded for
# context only. Triggering on it flagged a native-tool-call model (qwen3-14b-
# agentic) as instrument-void on the first run of this script, which is exactly
# the instrument-blaming-the-model error this whole check exists to prevent.
FINAL_ANSWER = "no tool calls in response"


def log_for(row, run_tag):
    """Exact filename, never a fuzzy glob.

    bakeoff-v8-lib.sh builds it as:
        STEM = $SLUG-$TASK_NAME-${RUN_TAG}-${BACKEND}-${ARM}-r${REP}
    Reconstructing that exactly matters: a glob without the run tag matched a v9
    log for a v8 row on the first run of this script, which would have
    cross-attributed a v9 parser failure onto v8's record -- the precise kind of
    round-conflation Fable's ruling warns against.
    """
    slug = row["model"].replace(":", "-").replace("/", "-")
    p = BASE / (
        f"{slug}-{row['task']}-{run_tag}-{row['backend']}"
        f"-{row['arm']}-r{row['rep']}.log"
    )
    return p if p.is_file() else None


def main():
    run_tag = sys.argv[1] if len(sys.argv) > 1 else "v9"
    csv_path = BASE / f"results-{run_tag}.csv"
    if not csv_path.exists():
        print(f"no {csv_path}")
        return 1
    rows = list(csv.DictReader(open(csv_path)))
    void, stands, nolog = [], [], []
    for r in rows:
        lg = log_for(r, run_tag)
        if not lg:
            nolog.append(r)
            continue
        txt = lg.read_text(errors="replace")
        dialect = bool(RAWSTR.search(txt))
        changed = r.get("files_changed") not in ("0", "", "na")
        # Void = the model emitted a call in a dialect the parser cannot receive
        # AND no file change resulted. Note reads may well have executed before
        # the dropped call -- deepseek ran 4 read_file plus a list_files and
        # still had its edit discarded -- so "executed" is context, never a
        # clearance. The dialect text plus an unchanged tree is the signature.
        n_exec = len(EXECUTED.findall(txt))
        if dialect and not changed:
            void.append((r, lg.name, n_exec, FINAL_ANSWER in txt))
        else:
            stands.append(r)

    print(f"{csv_path.name}: {len(rows)} rows\n")
    if void:
        print(f"INSTRUMENT-VOID ({len(void)}) -- exclude from model-merit scoring, keep raw rows:")
        for r, name, n_exec, saw_final in void:
            why = f"raw-string-dialect; {n_exec} tool call(s) did execute before the drop"
            print(f"  {r['model']:24s} {r['task']:28s} r{r['rep']}  [{why}]")
            print(f"      {name}")
    else:
        print("INSTRUMENT-VOID (0)")
    print(f"\nSTANDS ({len(stands)})")
    if nolog:
        print(f"\nNO LOG MATCHED ({len(nolog)}) -- check manually, do not assume valid:")
        for r in nolog:
            print(f"  {r['model']} {r['task']} r{r['rep']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
