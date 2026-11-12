# doc-summary

Scenario-focused PDF summaries on the local queue (Darkbloom models only).

    doc-summary FILE.pdf --scenario "who I am / what I must decide" [--model M] [--ctx N]

Prints an opaque job id and the private result path. Results: summary.pdf and summary.md
in `~/Documents/doc-summaries/private/<id>/` (dir 700, files 600; outside ~/bin and any git repo).
The input copy, scenario, extracted text and outputs live only there.

Pipeline: docsum_extract (pypdf) -> docsum_chunk -> docsum_core (single pass or map-reduce)
-> docsum_pdf (stdlib PDF writer). Runner: docsum_run.py (in ollama-queue ALLOWED_RUNNERS).

Privacy: queue rows, logs, done.json carry only the id, label `docsum-<random8>`, counts,
durations and codes. Failures print codes only: E_USAGE E_NO_FILE E_NOT_PDF E_NO_SCENARIO
E_MODEL_NOT_SERVED E_BAD_CTX E_JOB_EXISTS (CLI); E_TASK E_EXTRACT E_SCANNED_NO_OCR
E_MODEL_CALL E_EMPTY_SUMMARY E_SUMMARIZE (runner); status.json in the job dir holds the code.

Limits: no OCR (scanned PDFs fail E_SCANNED_NO_OCR); PDF output is plain text (model markdown
shows literally); oversize-note merge is a single grouping pass.

## Web UI (LAN only)

`docsum_server.py` (stdlib `http.server`, default port **7690**) serves one inline page: PDF picker + drag/drop, a scenario
box, an optional model dropdown (the Darkbloom-served models, same check as the CLI), and a job list (opaque id, status
queued/running/done/failed with an `E_*` code only, pages, elapsed, Download PDF, Download MD, Delete). Uploads go through
`docsum_cli.prepare_job` (dir 700, files 600) and the same enqueue command as `doc-summary` (label `docsum-<id>`).
Status is read from `<id>/status.json` plus the queue row. Delete wipes the job dir (and resolves a failed queue row).

Privacy: documents stay local. The server refuses any client that is not a private/loopback address, any request with
`CF-Connecting-IP`, `Cf-Access-Jwt-Assertion`, `CF-Ray` or a non-private `X-Forwarded-For` hop, and any `Host` that is not an
IP literal / localhost / single-label / `.local` name (DNS rebinding); POSTs must also be same-origin. There is no Cloudflare
Access path: do not put this behind a tunnel. The log holds only timestamp, route class, opaque job id, status code and byte
counts. The original file name sits in the job's `meta.json` and appears only in your browser; downloads are named
`summary-<id>.pdf` / `.md`. Limits: PDF <= 100 MB, scenario <= 20000 chars. `dispatch-escalation-watcher.py` ignores
`docsum-*` jobs so no headless `claude -p` review can ever read their data.

Pairing token (second layer, because the network guard alone cannot tell a LAN device from one riding a WARP/tunnel
private route): on first start the server writes a random 256-bit token to `~/Documents/doc-summaries/private/.ui-token`
(file 600, dir 700) and never logs or prints it. Every route except `/pair` returns 401 until the browser is paired. Once per
browser, in your OWN terminal (Claude must never read this file):

    cat ~/Documents/doc-summaries/private/.ui-token
    # then open, once:  http://<LAN host>:7690/pair?t=<token>

That sets an HttpOnly, SameSite=Strict cookie (30 days; an HMAC derived from the token, not the token) and redirects to `/`.
More than 5 wrong `/pair` attempts a minute from one address get 429. Delete `.ui-token` and restart to rotate (all browsers
must re-pair). Uploads are read under a 120 s overall deadline and at most 2 run at once. The watcher/handoff exclusions match
exactly `docsum-<8 hex>` (the labels `doc-summary` generates); builder labels like `docsum-ui-jobs` are ordinary work.

Run once by hand (foreground, Ctrl-C to stop): `python3 ~/bin/docsum_server.py` (`--port N`, `--host H`).

Install the always-on service (NOT done yet; needs the owner's approval):

    cp ~/bin/launchd/com.example.docsum-ui.plist ~/Library/LaunchAgents/
    launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/com.example.docsum-ui.plist

Remove: `launchctl bootout gui/$(id -u)/com.example.docsum-ui && rm ~/Library/LaunchAgents/com.example.docsum-ui.plist`.
URL: `http://<this-mac-LAN-IP>:7690/` or `http://<hostname>.local:7690/` from a device on the home network
(`http://127.0.0.1:7690/` on the Mac itself). Log: `~/bin/ollama-queue-logs/docsum-ui.log`.
Tests: `python3 -m pytest -q test_docsum_ui_*.py`.
