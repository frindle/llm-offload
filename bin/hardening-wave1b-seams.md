# F1 seam spec (darkbloom_chat sticky local.json + review-path guard) -- for the pipeline-canary owner to register

Test file: ~/bin/test-darkbloom-sticky-review.py (self-contained, prints F1_STICKY_OK; 37 checks).
Revert-check: `python3 ~/bin/test-darkbloom-sticky-review.py --revert-check` (loads pre-fix darkbloom_chat.py +
code-review-agent.py from machine-config git HEAD, requires the test to FAIL; prints REVERT_CHECK_OK).
Env overrides for a mutant: DBCHAT=<path to darkbloom_chat.py> REVIEW=<path to code-review-agent.py>.

Suggested canary seam "dbchat-sticky-review" (dict-proof format {"mut","old","new","why"}), files darkbloom_chat.py:
 1. mut: _record() transient branch returns {} (drop sticky) -- old `return dict(_GOOD[0])` -> new `return {}`
    why: empty local.json mid-restart loses base/key, review flips to Ollama /api/chat.
 2. mut: is_darkbloom() drops the `record_transient() and h == DEFAULT_BASE` clause (fresh process in restart window).
 3. mut: default_host() falls back to LEGACY_OLLAMA_HOSTS[0] unconditionally (the old `or "http://localhost:11434"`).
 4. mut: legacy_ollama_misroute() returns None always (silent wrong-protocol send).
 5. mut: _raw_record() treats a MISSING file as transient (would mask an explicit off).
Revert (code-review-agent.py): `_dbk.default_host()` -> `_dbk.base_url() or "http://localhost:11434"`; remove the
`_mis = ... raise RuntimeError(_mis)` lines in Model.chat.
Kill switches: DARKBLOOM_STICKY_S=0 (cache off), DARKBLOOM_REVIEW_GUARD=0 (misroute guard off).
