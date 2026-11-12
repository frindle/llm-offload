# Onboarding a model

`METHODOLOGY.md` explains *why* a round is built the way it is. This is the
operational counterpart: the steps to get a new model measured, in order, with
the gates that stop you.

It exists because on 2026-08-25 three models were added to a live programme and
the run **aborted three separate times** before scoring anything. Every abort
was the instrument correctly refusing to guess. None of the steps were written
down anywhere; they were rediscovered by tripping over them. That is the cost
this file removes.

The gates are a feature. An instrument that guesses a context window, or
silently scores a model against a worktree that does not exist, produces numbers
that look fine and mean nothing. Let it refuse.

---

## 0. Before anything: is this model worth a round?

A round is 4-6 hours of GPU. Ask what dispatch decision the result would change.
"It would be interesting to know" is not an answer. New arrivals from the
monthly model check are audited into the existing table — same cells, matched n
— not made the subject of a new programme.

---

## 1. Stage the model LOCALLY

**Never load a model over SMB.** Local NVMe measures ~2927 MB/s against ~299
MB/s over the share — roughly 10x — and a round run over the share measures the
filesystem, not the model.

```bash
# Unraid share -> local. Skips blobs already present with a matching size.
machine-config/bin/copy-ollama-model-from-unraid.py pull <model>

# Confirm it landed locally and that Ollama is pointed at the local store
curl -s http://localhost:11434/api/tags | grep <model>
ps eww $(pgrep -f 'ollama serve' | head -1) | tr ' ' '\n' | grep OLLAMA_MODELS
```

Unraid is the library; the local store is the working set. Evicting a model
locally is fine **provided it exists on Unraid** — never delete the last copy.

---

## 2. Smoke parse-check — MANDATORY, before any scored run

The single highest-value five minutes in this process. A model whose tool calls
do not parse produces rows that are instrument artifacts, not measurements —
this is exactly what voided the devstral block.

```bash
curl -s http://localhost:11434/api/chat -d '{
  "model":"<model>","stream":false,
  "messages":[{"role":"user","content":"Read the file arr-webhook.py. Use the read_file tool."}],
  "tools":[{"type":"function","function":{"name":"read_file","description":"Read a file",
    "parameters":{"type":"object","properties":{"path":{"type":"string"}},"required":["path"]}}}],
  "options":{"temperature":0.2,"num_ctx":8192}}' | python3 -c "
import json,sys
m=json.load(sys.stdin).get('message',{})
c=m.get('content') or ''
print('native tool_calls:', 'YES' if m.get('tool_calls') else 'NO')
print('content len:', len(c))
print('markers leaked:', [k for k in ('<|channel|>','<|start|>','<tool_call>','\`\`\`json') if k in c] or 'NONE')"
```

Pass = native `tool_calls` present, content length 0, no markers leaked.

**If it fails, that is a finding, not a blocker.** Set `MANUAL=yes` in the roster
and record the dialect. Known dialects: harmony (gpt-oss), ornith newline-strip,
deepseek raw-string. `qwen2.5-coder:14b` emits **zero** native calls — every one
of its calls is recovered by the fallback parser, which makes that parser
load-bearing infrastructure rather than a safety net.

Re-run this after any Ollama version change. Harmony was confirmed on 0.32.15
and assumed to hold on 0.32.14 for weeks before anyone checked; it did, but that
was luck, not evidence.

---

## 3. `native_ctx_for()` — read it from the server, never the model card

**GATE 1:** `ABORT: no native context recorded for '<model>' -- add it to native_ctx_for()`

```bash
curl -s http://localhost:11434/api/show -d '{"model":"<model>"}' | python3 -c "
import json,sys
mi=json.load(sys.stdin).get('model_info',{})
print([v for k,v in mi.items() if k.endswith('context_length')], mi.get('general.architecture'))"
```

Add the entry to `native_ctx_for()` in the lib. Purely additive — never touch an
incumbent's value, or you invalidate completed rows.

Use the **exact tag being dispatched**, not the modelfile you think is loaded.
`ctx_for()` resolves `min(65536, native)`, so most models run at 65536 regardless
— that uniformity is what makes rows comparable.

Why this matters: `qwen3-coder:30b` hit `config_ceiling` 3/3 with a 262144-token
native window. It is now known that two peers passed the same cell at the same
65536, so the window was not the problem — but a wrong entry here is
indistinguishable from a model failing, and costs a whole round to find out.

---

## 4. Worktrees — one per model, per cell

**GATE 2:** `ABORT (worktree missing): <model> / <task>`

```bash
cd <source-repo>          # e.g. plex-automation-debugcell, clamshell
git worktree add --detach <build-dir>/<cell>/<slug> <BASE_SHA>
```

Use the **same base SHA as every incumbent** — check one first. Then confirm the
planted defect is present in the new worktree; a fixture that starts green
silently turns the whole cell into a no-op.

---

## 5. Roster entry

Format: `MODEL|SLUG|TIMEOUT_S|MANUAL|NUDGE`

- **SLUG** — filename-safe, used for logs, diffs and worktree paths.
- **TIMEOUT** — size against incumbents by class, do not invent. 14B dense ~1800,
  ~30B MoE ~3600, 27B q8 ~4200, 80B ~5400.
- **MANUAL** — `yes` only if step 2 failed.

---

## 6. Cloning a driver — CHECK FOR HARDCODED PATHS

**GATE 3, and the dangerous one, because it does not abort.** Retagging
`RUN_TAG` does **not** catch paths written out in full. On 2026-08-25 a cloned
two-turn driver carried `R3_CSV="$OUTDIR/results-v9-r3.csv"` and would have
appended new rows into a **completed, frozen dataset**, silently corrupting a
results-read that had already been filed.

```bash
grep -nE 'results-v[0-9]|"\$OUTDIR/[^"]*v[0-9]' <new-driver>.sh
```

The lib derives `RESULTS_CSV="$OUTDIR/results-${RUN_TAG}.csv"`, so anything
going through the lib is safe. Anything hardcoded in a driver is not.

---

## 7. Run, in this order

Debug cell n=3, then clamshell n=2 + the two-turn follow-up. Matching n to the
incumbents is what makes the rows comparable; a first-look audition at lower n
produces a row you cannot put in the same table.

Budget **4-6 hours**, not 4-5 — the two-turn follow-ups make the lower figure a
floor rather than an estimate.

---

## 8. Score — and pass every results dir at once

`gc`-style scorers and `bakeoff-v9-calibration.py` build their truth set from
**all** directories passed in, deliberately, so the truth does not depend on the
model under test. Passing one at a time makes each engine its own ground truth
and produces numbers that are wrong in a direction that flatters whoever you
scored first.

Then run the claim-vs-verify calibration. **Pass rate is not the routing answer.**
A model can score 2/3 and still be disqualified: `qwen3-14b-agentic` claimed a
fix after four consecutive rejected `edit_file` calls, with the error text in its
own context each time.

---

## 9. Clean up between rounds

```bash
./bakeoff-tidy.sh            # dry run
./bakeoff-tidy.sh --apply
```

Refuses while any driver or worker is alive, and will not move a file another
driver references. The working root doubles as the live directory; hand-tidying
it mid-round breaks the round hours in, silently.

---

## The gates, as a checklist

| # | Gate | Symptom |
|---|---|---|
| 1 | model not staged locally | slow round, or a filesystem measurement |
| 2 | tool calls do not parse | rows are instrument artifacts |
| 3 | `native_ctx_for()` missing | `ABORT: no native context recorded` |
| 4 | worktree missing | `ABORT (worktree missing)` |
| 5 | hardcoded results path | **no error — corrupts a frozen dataset** |
| 6 | scored one dir at a time | each model becomes its own ground truth |

Only #5 and #6 fail silently. Those are the two worth checking twice.
