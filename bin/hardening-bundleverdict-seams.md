# Bundle verdicts (verdicts live with their bundle): canary seam spec (2026-10-14)
pipeline-canary.py was NOT edited (Wave 2 owns its live baselines). Register this seam there.
No kill switch needed: the feature is a read-only view (`/api/bundle-verdicts`) over the annotated
/api/runs rows; /api/runs, /api/runs/tree, /api/runs/<id>/clear and /api/handoff are unchanged.

## seam `bundleverdicts`
- test: `python3 "$HOME/Desktop/GitHub Projects/ollama-queue-dashboard/tests/test-bundle-verdicts.py"`, marker `ALL PASS`
- file: `ollama-queue-api.py` (symlink ~/bin/ollama-queue-api.py -> ollama-queue-dashboard/src/); API_SRC env points at a mutated copy
- live probe (needs the API restarted onto this build): `curl -s localhost:7684/api/bundle-verdicts` -> every /api/runs id
  appears in `.rows`; `.unattached` + attached bundles partition them; each `.bundles[k].worst` equals the worst raw_verdict of its ids
- proofs (file: old -> new), all in `ollama-queue-api.py`:
  - **sign-off rows become clearable inline**
    - old: `'info["clearable"] = not (r.get("awaiting_signoff") or r.get("auto_run_live"))'`
    - new: `'info["clearable"] = True'`
  - **roll-up no longer worst-first**
    - old: `'if rk > b["worst_rank"]:'`
    - new: `'if rk < b["worst_rank"] or b["worst_rank"] < 0:'`
  - **unattached rows dropped**
    - old: `'            unattached.append(r.get("id"))\n'`
    - new: `'            pass\n'`
  - **needs_eyes count not tallied**
    - old: `'            b[g] += 1'`
    - new: `'            pass'`
