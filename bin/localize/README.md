# localize: sweet-search and ColGREP as bug localizers

Query = the GitHub issue text (`desc.txt`, up to 8000 chars, same form for every task and arm). Both tools run ONLY on
the dispatch-cpu-runner (`submit_cpu_job(..., fallback=False)`); the Mac only parses results.

- `loc_job.sh`   runs on the runner: `setup` (network; installs + warms), `run` / `diag` (offline; index + search).
- `loc_submit.py` Mac side: submits (<=3 jobs at once), extracts artifacts with `tarfile` filter="data", normalizes.
  `python3 loc_submit.py --diag pyrato` first (dumps `--help`, env, process list), then `--tasks a,b` or no args = all.
  Refuses to submit unless `unraid-cpu-1` was seen <30 s ago (no polling loop).
- `loc_score.py` Mac side, parse-only (`ast`, regex): file hit@k/recall/MRR, function hit@k, gate test.
  `python3 -I loc_score.py --selftest`, then `python3 -I loc_score.py [--arms sweet-search,colgrep]`.
- Cache on the runner: `/tmp/loc-cache/` (npm prefix `npm/`, `colgrep/colgrep`, HOME=`home/` holding model caches,
  flock `/.lock`, marker `.warm-<versions>`). Per-job indices go to `/tmp/loc-idx.*` and are removed.
- Pins: `sweet-search@2.10.5`; ColGREP `v1.8.5` asset `colgrep-x86_64-unknown-linux-gnu.tar.xz`, sha256-checked.
- Outputs: `~/offload-research/localize/out/<arm>/<task>.json`, `runs.jsonl`, raw artifacts in `raw/<task>/`.

## Results

- 2026-11-12, arm A (ColGREP v1.8.5, issue text as query, 23 bench tasks): file hit@1 26%, hit@3 43%, hit@5 52%,
  hit@10 70%, MRR 0.39; function hit@5 9%, hit@10 22%. Below the 60% file hit@3 kill line, so arm A stops here.
  As a "patch missed a file" gate its false-alarm rate on passing patches is 63% at k=1, so it is no use there either.
- Arm B (sweet-search) started the same day, one runner job at a time (~20-25 min per task on the Unraid CPU).
- Arm B dropped the same day, before scoring: too slow on the Unraid CPU. Default mode: one big repo was 22% through
  indexing after 50 min, a small one took 24 min. Light mode (`sweetfast`, no late-interaction index): bibtex 447 s,
  conan1 still indexing after 21 min (bar: about 10 min per task), so it was stopped and its runner job cancelled.
  SweRank (CC-BY-NC-4.0) stays out unless the owner OKs it. No localizer arm is live.
