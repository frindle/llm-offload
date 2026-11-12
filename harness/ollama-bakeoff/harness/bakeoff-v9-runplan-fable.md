# Bake-off v9 + auditions — Fable's consolidated GO + run scripts (2026-08-24)

**VERDICT: GO**, conditional on the pre-run gate table. Nothing dispatches until each gate is checked, in order.

| # | Gate | Status |
|---|---|---|
| 1 | Debug-cell scrub complete (S1 runs clean: scrubbed repo built, leak-grep passes, lib re-pointed, worktrees rebranched, inverted preflight FAILS on pristine, old branch bundle-archived) | **blocks R1** |
| 2 | Pre-push guard on plex-automation | DONE (tested, blocks pushes containing 3f5e4ff) |
| 3 | stage.py changes (hash-verify, namespace-aware eviction, mount check) + `--keep` full-roster rule | TODO before staging |
| 4 | v9 pre-dispatch checklist items 1-9 re-confirmed after scrub (esp. #5 fresh worktrees, #6 inverted preflight, #8 parity checksum) + post-run out-of-worktree leak-grep below | before R1 |
| 5 | OCR sweep finished, GPU free | external (~ready) |

**Gate-4 annotation**

> **ANNOTATION (pre-R1, 2026-08-24, Fable-ruled — appended, original text unmodified):** The host-readiness gate (`bakeoff-v8-lib.sh` ~240-252) requires `need=55705MB` available against a threshold of 85% of 64GB physical = 55706MB — unsatisfiable by construction once any model is resident. Observed behaviour across 69 v8 runs is bimodal: clears in ~5s (49/49) or burns the full 240s timeout (18/18). The `host_ready>20%` falsifier is therefore uninformative as a host-degradation signal this round. For v9 it is computed exactly as pre-registered (same formula, same denominator, no exclusions) but demoted from model-falsifier to instrument flag: a trip triggers attribution review against the gate bug and does not by itself falsify a model row. The gate itself is FROZEN for v9 for v8-comparability and will be corrected (wait cap ~15s, threshold ~70%) only inside the single post-v9 probe-block/freeze window, before auditions.

Instrument verified live: read-cap 600 + paging + grep hint; Qwen-XML fallback; scorer→v7; read-pattern scorer;
`test-worker-parsers.py` 17/17 pass. Predicted worker changes, confined to the ONE probe-block window post-v9:
ornith newline-strip in `extract_qwen_xml_tool_calls`; possible harmony extractor for gpt-oss (only if Ollama's
server-side harmony parse leaks raw — probe decides); fix the stale "2000-line" docstring. Then FREEZE + re-run parity.

Run order end-to-end: OCR done → **S1 scrub** → v9 checklist → v9 R1/R2/R3 (NO worker edits) → v9 results-read →
[NVMe block boundary if the Fri drive is in] → **S4** (stage→probe/freeze→ornith-9b→qwen3.6→checkpoint→ornith-35b→
gpt-oss:20b→devstral last→score). Budget: v9 as designed + **50-72h** auditions. Gate 1 blocks R1; everything after
R1 is blocked only by its own phase gate.

OCR non-LLM track (Tesseract/PaddleOCR/EasyOCR): NOT in this harness — own small comparison script inside
`giftcard-ocr`, scored against the same labeled set, disagreement-rate metric. Deferred.

### Gate 4 — post-run out-of-worktree leak-grep (Fable, 2026-08-24)

The in-script leak-grep proves the answer key is unreachable *inside* the worktree. This one
checks the transcripts for a model that reached *outside* it. Run in the v9 results-read,
before scoring any debug row:

```bash
grep -lE '\.\./\.\.|/plex-automation/\.git|plex-automation-debugcell|bakeoff-v9-scrub|bakeoff-v9-runplan|session-passdown|bakeoff-private-archive|bakeoff-v8-lib\.sh|Tighten release-group|alphabetic names|digit-bearing group cases' "$OUTDIR"/*plex-release-group-debug*-r*.log
```

Any hit = row **quarantined pending manual transcript read**, not auto-void —
`plex-automation-debugcell` also appears if a model merely cats the worktree's `.git` file,
which is benign. Do NOT grep for `A-Za-z0-9`: that string is the correct fix and appears in
every honest solution.

---

## S1 — bakeoff-v9-scrub-debugcell.sh (run before R1; idempotent)

### Baseline provenance

All v8 debug rows were measured against plex-automation `bc08ae2` (branch
`bakeoff-v8-debug-baseline`). That branch's history leaked the planted defect (commit
`3f5e4ff`) to any model running git archaeology, so for v9 the identical TREE
(`git rev-parse bc08ae2^{tree}` = `42182bf77ba2241720071f808c763827f0fe3d05`) was
re-imported as a single-commit, no-remote repo (`plex-automation-debugcell`, commit
`33d4735` / `33d4735ac575b590bddf7770da6f474105b99ab6`, message "Import project").
Tree-hash equality is asserted by the scrub script. v8 and v9 debug rows are
byte-identical in task content; the baseline SHA change is provenance-only and implies
no change in difficulty or scoring. The old branch is archived at
`bakeoff-private-archive/bakeoff-v8-debug-baseline.bundle` and retired from the public
repo.

**S1 RUN 2026-08-24 21:47 — COMPLETE.** `bc08ae2` → `33d4735`; 6 worktrees re-provisioned
from the clean repo; tree hashes verified equal; defect unreachable from every worktree;
no dangling objects. Fable APPROVE-WITH-CHANGES (Edits A+B) applied verbatim before the run.

```bash
#!/bin/bash
# v9 R1 blocker fix: history-free debug-cell baseline. Fable-ruled 2026-08-24.
set -euo pipefail
SRC="/Users/user/Desktop/GitHub Projects/plex-automation"
CLEAN="/Users/user/Desktop/GitHub Projects/plex-automation-debugcell"
LIB="/Users/user/Desktop/GitHub Projects/bakeoff-v8-lib.sh"
ARCHIVE="/Users/user/Desktop/GitHub Projects/bakeoff-private-archive"
PYBIN="/Users/user/bin/plex-bakeoff-venv/bin/python"
OLD_SHA="bc08ae2"; DEFECT_SHA="3f5e4ff"
mkdir -p "$ARCHIVE"
git -C "$SRC" bundle create "$ARCHIVE/bakeoff-v8-debug-baseline.bundle" bakeoff-v8-debug-baseline
rm -rf "$CLEAN"; mkdir -p "$CLEAN"
git -C "$SRC" archive "$OLD_SHA" | tar -x -C "$CLEAN"
git -C "$CLEAN" init -q; git -C "$CLEAN" add -A
git -C "$CLEAN" -c user.name=frindle -c user.email=69526085+frindle@users.noreply.github.com commit -qm "Import project"
NEW_SHA=$(git -C "$CLEAN" rev-parse --short HEAD)
[ -z "$(git -C "$CLEAN" remote)" ] || { echo "FATAL: clean repo has a remote"; exit 1; }
if git -C "$CLEAN" log --all -p | grep -qE 'alphabetic names|digit-bearing group cases|Tighten release-group'; then
  echo "FATAL: leak-grep found diagnostic text"; exit 1; fi
[ "$(git -C "$CLEAN" rev-list --count HEAD)" = 1 ] || { echo "FATAL: expected 1 commit"; exit 1; }
if (cd "$CLEAN" && "$PYBIN" test_release_group_numeric.py >/dev/null 2>&1); then
  echo "FATAL: repro PASSES on pristine tree"; exit 1; fi
grep -q "BASELINE_plex_automation=\"$OLD_SHA\"" "$LIB" && \
  sed -i '' "s/BASELINE_plex_automation=\"$OLD_SHA\"/BASELINE_plex_automation=\"$NEW_SHA\"/" "$LIB"
grep -q "BASELINE_plex_automation=\"$NEW_SHA\"" "$LIB" || { echo "FATAL: lib re-point failed"; exit 1; }
echo "NOTE: provision debug-cell worktrees FROM $CLEAN. Then v9 checklist 5-7."
echo "SCRUB OK: baseline=$NEW_SHA  bundle=$ARCHIVE/bakeoff-v8-debug-baseline.bundle"
```
After it verifies through one full preflight: `git -C "$SRC" branch -D bakeoff-v8-debug-baseline` (bundle keeps it
reconstructable). The lib's `_one_task_v8` does `reset --hard $BASE_SHA` in the worktree, so the worktree MUST
come from the clean repo. **VERIFY before running:** the exact repro test filename (`test_release_group_numeric.py`
is Fable's assumption) and the lib var name/line (`BASELINE_plex_automation`, ~line 83).

## S2 — bakeoff-v95-stage-pin.sh (stage + hash-verify + digest pin; between-runs only)
```bash
#!/bin/bash
set -euo pipefail
STAGE="/Users/user/Desktop/GitHub Projects/bakeoff-v8-stage.py"
LOCAL="$HOME/.ollama/models"; PINS="/Users/user/Desktop/GitHub Projects/bakeoff-v95-digest-pins.json"
KEEP="${KEEP_ROSTER:?set KEEP_ROSTER=comma-separated models never to evict}"
stage_and_pin() {
  local M="$1"; python3 "$STAGE" stage "$M" --keep "$KEEP"
  local name="${M%%:*}" tag="${M##*:}"; local MF="$LOCAL/manifests/registry.ollama.ai/library/$name/$tag"
  [ -f "$MF" ] || { echo "FATAL: no manifest for $M"; exit 1; }
  python3 - "$MF" "$LOCAL" <<'EOF'
import json,hashlib,sys,pathlib
mf,root=pathlib.Path(sys.argv[1]),pathlib.Path(sys.argv[2]); m=json.loads(mf.read_text())
digs=[m["config"]["digest"]]+[l["digest"] for l in m.get("layers",[])]
for d in digs:
    b=root/"blobs"/d.replace(":","-"); h=hashlib.sha256()
    with open(b,"rb") as f:
        for chunk in iter(lambda: f.read(1<<24), b""): h.update(chunk)
    assert "sha256:"+h.hexdigest()==d, f"HASH MISMATCH {b}"
print(f"  verified {len(digs)} blobs")
EOF
  local MSHA; MSHA=$(shasum -a 256 "$MF" | cut -d' ' -f1)
  python3 - "$PINS" "$M" "$MSHA" "$MF" <<'EOF'
import json,sys,pathlib
p,model,msha,mf=sys.argv[1:5]
pins=json.loads(pathlib.Path(p).read_text()) if pathlib.Path(p).exists() else {}
entry={"manifest_sha256":msha,"blobs":[l["digest"] for l in json.loads(pathlib.Path(mf).read_text()).get("layers",[])]}
if model in pins and pins[model]!=entry: sys.exit(f"PIN CONFLICT for {model}")
pins[model]=entry; pathlib.Path(p).write_text(json.dumps(pins,indent=1)); print(f"  pinned {model}")
EOF
}
for m in "$@"; do stage_and_pin "$m"; done
# assert_pin(): before EVERY scored run, re-shasum the manifest and assert == pinned; drift => abort.
```

## S3 — bakeoff-v95-probe.sh (multi-turn probe block; unscored; opens the ONLY worker-change window)
```bash
#!/bin/bash
set -euo pipefail
WORKER="$HOME/bin/ollama-worker-v7.py"
OUT="/Users/user/Desktop/GitHub Projects/bakeoff-v95-probes"; mkdir -p "$OUT"
probe_one() {  # $1=model  $2..=extra worker args (e.g. --api openai for llama-server)
  local M="$1"; shift; local SLUG; SLUG=$(echo "$M" | tr ':/' '--')
  local D="$OUT/$SLUG-scratch"; rm -rf "$D"; mkdir -p "$D"
  printf '{ "retries": 3, "verbose": false }\n' > "$D/config.json"
  # TASK: "Read config.json, change retries to 5, then run python3 -c to confirm it is 5."
  # >>> SPEC LINE: invoke the worker EXACTLY as bakeoff-v8-lib.sh's dispatch call site does
  #     (same argv, task via same mechanism, num_ctx via ctx_for, temp per roster), cwd=$D,
  #     extra args "$@", log to $OUT/$SLUG.log. COPY THE CALL SITE VERBATIM -- do not reconstruct. <<<
  local LOG="$OUT/$SLUG.log"
  python3 -c "import json;assert json.load(open('$D/config.json'))['retries']==5" \
    && echo "PROBE PASS(mutation): $M" || { echo "PROBE FAIL: $M"; return 1; }
  grep -cE '<function='               "$LOG" | xargs -I{} echo "  qwen-xml-text: {}"
  grep -cE 'to=functions\.|<\|channel\|>' "$LOG" | xargs -I{} echo "  harmony-text: {}"
}
probe_one "ornith-1.5:9b"
probe_one "qwen3.6:latest"
probe_one "gpt-oss:20b"
probe_one "ornith-1.5:35b"
probe_one "devstral:24b" || probe_one "devstral:24b" --api openai
# Added 2026-08-24 (Fable-ruled): unratified v7 roster drop, artifact-confounded by the
# namespace bug fixed in stage.py the same day. Runs LAST, same frozen worker, same pass
# criterion as every other candidate -- its history is handled by the staging precondition
# (hash-verified blobs via namespace-aware stage.py), not by moving the bar.
probe_one "MFDoom/deepseek-r1-tool-calling:14b"
```
PASS = file mutated AND model's own verify ran (format-independent proof of >=3 executed calls). FAIL with dialect
text present = instrument gap → fix parser → extend test-worker-parsers.py → parity re-assert → re-probe → FREEZE.

## S4 — bakeoff-v95-audition.sh (master driver, resume-safe)
```bash
#!/bin/bash
set -euo pipefail
source "/Users/user/Desktop/GitHub Projects/bakeoff-v8-lib.sh"
_orig_native_ctx_for=$(declare -f native_ctx_for)
native_ctx_for() { case "$1" in
  ornith-1.5:9b|ornith-1.5:35b|qwen3.6:latest) echo 262144 ;;
  gpt-oss:20b|devstral:24b)                    echo 131072 ;;
  *) eval "${_orig_native_ctx_for/native_ctx_for/_nc}"; _nc "$1" ;; esac; }
AUDITION=( "ornith-1.5:9b" "qwen3.6:latest" "ornith-1.5:35b" "gpt-oss:20b" "devstral:24b" )
export KEEP_ROSTER=$(IFS=,; echo "${AUDITION[*]}")
P="/Users/user/Desktop/GitHub Projects"
phase(){ local n="$1"; shift; [ "${!n:-}" = yes ] && { echo "SKIP $n"; return 0; }; echo "== $n =="; "$@"; }
phase SKIP_STAGE  bash "$P/bakeoff-v95-stage-pin.sh" "${AUDITION[@]}"
phase SKIP_PROBE  bash "$P/bakeoff-v95-probe.sh"
phase SKIP_PARITY python3 "$P/test-worker-parsers.py"
for M in "${AUDITION[@]}"; do
  V="SKIP_$(echo "$M" | tr ':.-' '___')"; [ "${!V:-}" = yes ] && { echo "SKIP $M"; continue; }
  # >>> SPEC BLOCK: dispatch $M through the SAME cell entry points the v9 driver uses -- run_cells
  #     path in bakeoff-v8-lib.sh (photo, clamshell, RUN_DEBUG=yes debug vs the SCRUBBED baseline,
  #     then the R3 two-turn cell), reps 1..3, calibration first to size the wall, ornith checkpoint
  #     between 9b and 35b, devstral EXTRA args per probe outcome. Copy the v9 driver's per-model
  #     loop body verbatim with MODEL=$M -- identical cells by construction. <<<
done
phase SKIP_SCORE  python3 "$P/bakeoff-v8-score.py"
```

**The two SPEC blocks stay spec on purpose:** identical-cells-by-construction is the comparability argument, achieved
by reusing the v9 driver's LITERAL call sites, not re-deriving argv from memory (a one-flag drift = a third harness =
the bug-10 mistake). The paste is mechanical; review it as one diff against the v9 driver's call site.

> **CORRECTION PENDING (2026-08-24 22:50, appended — do NOT dispatch R1 against the annotation above).**
> The annotation's central claim — that the gate demands 85% of physical and is "unsatisfiable by
> construction once any model is resident" — is FALSE. `readiness_threshold_mb()` is per-model:
> `NEED = resident*1.2 + KV`, where `KV = ctx*150/1024`, and 85% of physical is only a documented
> upper CAP whose stated purpose is to prevent exactly the unreachable-threshold failure the
> annotation alleges. The `need=55705MB` figure appears for ONE model, `qwen3-coder-next:q4_K_M`,
> whose honest gate (71640MB) exceeds the cap. Gates for the rest are 15600-45600MB.
> v8 data: the 18 `host_ready=no` rows are `qwen3-coder-next` 9/9 and `qwen3.8:27b-q8_0` 8/15, plus
> `deepseek-r1:32b` 1/15. The other four models are 0/45. The gate works for every model that fits
> the host. The §5 "structural" carve-out was CORRECT as originally written and must not be
> re-attributed. Superseding text pending Fable ruling.
> **SUPERSEDING ANNOTATION (pre-R1, 2026-08-24, Fable-ruled — retracts the annotation of earlier today):** The prior annotation claimed the readiness threshold was "85% of physical, unsatisfiable by construction." That was false. `readiness_threshold_mb()` computes a per-model NEED (`resident*1.2 + ctx*150/1024`) and the 85% figure (55705MB) is a documented safety CAP whose stated purpose is to prevent exactly the failure alleged. Per-model gates on this host range 15600–45600MB against a B3-measured clean-host availability of 42068MB; `need=55705` occurs only for `qwen3-coder-next:q4_K_M`, whose honest gate (71640MB) exceeds the cap. The v8 `host_ready=no` rows (18/69) concentrate in the two models whose gates exceed clean-host availability (qwen3-coder-next 9/9, qwen3.8:27b-q8_0 8/15); of the remaining four, the other three pass 0/30 and deepseek-r1:32b records 1/15. **The gate is not broken and will not be modified — not for v9, and no threshold/wait change is carried into the post-v9 probe window.** The `host_ready>20%` falsifier is RESTORED to full pre-registered force for all non-structural cells. The only change surviving from the retracted ruling is classification: structural cells (see structural-classification entry, this date) are excluded from falsifier #1's denominator. A possible future optimization — short-circuiting the 240s wait for cells pre-classified structural (~72 min/program) — is deferred to the v9/v10 boundary and requires separate sign-off.
> **Structural classification for v9 (pre-dispatch, 2026-08-24, Fable-ruled, B3 clean-host measured available = 42068MB/65536MB):** `qwen3-coder-next:q4_K_M` — structural (honest gate 71640MB; capped to 55705). `qwen3.8:27b-q8_0` — structural-marginal (gate 45600MB > 42068MB measured; v8 record 8/15 host_ready=no shows the margin is availability-dependent). Both are excluded from falsifier #1 (`host_ready>20%`) for v9; their rows still dispatch after the gate wait and carry the flag, per v8 practice. All other models: falsifier #1 applies at full pre-registered force. This classification is fixed now and will not be revisited after results exist.
### FALSIFIER TRIP — recorded mid-R1

> **FALSIFIER TRIP (mid-R1, 2026-08-24, recorded before round completion, Fable-ruled):** `dialect-text-with-executed=0` tripped on `deepseek-r1:32b / plex-release-group-debug / base / r1`. The model emitted the exactly correct fix for the planted defect (`[A-Za-z]` → `[A-Za-z0-9]`) as a tool call whose JSON string values carried Python raw-string prefixes (`r"..."`); both worker parsers (`extract_manual_tool_calls`, `extract_qwen_xml_tool_calls`) yield 0 calls on the captured text and 1 call on a byte-identical control with prefixes removed. Ruling: R1 continues (defect isolated to the sole MANUAL=yes model; instrument frozen); all deepseek-r1:32b v9 rows are checked for the same signature and any that trip are instrument-void, excluded from model-merit scoring, raw rows preserved; voided rows re-run as R1b after the parser fix lands in the single post-v9 probe-window (test case from the captured string, parity, re-freeze, new digest logged); R1b is never merged into R1 without its worker-version caveat. v8 note: this model's v8 debug rows (3/3 files=0) share the CSV signature with a different cause (context ceiling, since fixed) — attribution between rounds must not be conflated.
