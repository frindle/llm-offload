# Run-status / handoff SHARED HANDLED LEDGER: canary seam spec (2026-10-14)
pipeline-canary.py was NOT edited (Wave 2 owns its live-baselines). Register this seam there.
Kill switch: `RUNSTATUS_SHARED_LEDGER=off` (on the com.example.ollama-queue-api LaunchAgent env) restores
the old two-ledger behaviour exactly (no acted fold, no acted mark on clear, no beyond-cap sign-off rows).

## seam `runstatusledger`
- test: `python3 "$HOME/Desktop/GitHub Projects/ollama-queue-dashboard/tests/test-runstatus-shared-ledger.py"`, marker `ALL PASS`
- revert-check: same file with `--revert-check`, marker `REVERT-CHECK PASSED` (7/7 mutants bite, 2026-10-14)
- file: `ollama-queue-api.py` (symlink ~/bin/ollama-queue-api.py -> ollama-queue-dashboard/src/)
- API_SRC env points the test at a mutated copy (same convention as test-runstatus-retention-api.py)
- proofs (file: old -> new), all in `ollama-queue-api.py`:
  - **handoff acted mark no longer folds the run-status row**
    - old: `'if _am is not None and (not r.get("awaiting_signoff")'`
    - new: `'if False and (not r.get("awaiting_signoff")'`
  - **acted mark folds a row still owing sign-off**
    - old: `'if _am is not None and (not r.get("awaiting_signoff")'`
    - new: `'if _am is not None and (True'`
  - **run-status clear writes no handoff acted mark**
    - old: `'ledger = _mark_shared_acted(job_id, label,'`
    - new: `'ledger = "off" or _mark_shared_acted(job_id, label,'`
  - **run-status clear overwrites an existing (first) acted mark**
    - old: `'            if job_id in acted:\n                return "already"\n'`
    - new: `'            if False:\n                return "already"\n'`
  - **sign-off rows past the 100-row cap vanish again**
    - old: `'    rows += _signoff_rows_beyond_cap(rows, acted)\n'`
    - new: `'\n'`
  - **fixture LOG_DIR guard dropped (tests would write the real acted.json)**
    - old: `'return Path(q.LOG_DIR).resolve() == Path(ho.LOGS).resolve()'`
    - new: `'return True'`
  - **kill switch ignored**
    - old: `'if SHARED_LEDGER_MODE in ("off", "0", "false", "no"):'`
    - new: `'if False:'`
