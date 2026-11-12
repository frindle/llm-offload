"""The round driver: one row of the results CSV per run.

ORDER OF OPERATIONS, AND WHY
----------------------------
1. Preflight every fixture against a pristine tree, ONCE, before anything
   dispatches. A fixture that no longer discriminates has to be caught before a
   round is spent on it, not after.
2. Measure clean-host available memory, ONCE, before anything dispatches. That
   figure classifies each model's gate as REACHABLE or STRUCTURAL, and the
   classification is part of the pre-registered falsification test — decided
   afterwards it would just be a way of excusing whatever happened.
3. Per run: stage a fresh tree, restart and gate the host, dispatch, archive the
   transcript and diff, restore ground-truth files, verify, write the row.

The dispatch order is a deterministic shuffle seeded from the config, so a
reviewer can regenerate the exact order that produced a CSV. An unseeded shuffle
makes position unrecoverable after the fact, which is the same class of mistake
as not recording context per row.
"""
from __future__ import annotations

import csv
import random
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import gate, hosttel, task as task_mod
from .config import Config, ModelSpec
from .worker import Worker, write_transcript

FIELDS = [
    "model", "backend", "task", "work_class", "language", "arm", "rep",
    "verify_passed", "duration_s", "warmup_s", "loop_s", "timed_out",
    "files_changed", "iterations", "num_ctx", "native_ctx", "stop_reason",
    "host_ready", "gate_need_mb", "gate_class",
    "prompt_tokens", "output_tokens", "decode_s", "out_tps",
    "swap_before_mb", "swap_after_mb", "avail_before_mb", "avail_after_mb",
    "load_before", "transcript", "diff",
]


@dataclass
class Cell:
    model: ModelSpec
    task: task_mod.Task
    arm: str
    rep: int


def build_prompt(t: task_mod.Task, arm: str, tree: Path, cfg: Config) -> str:
    """Arms are PROMPT treatments on an otherwise identical cell.

    Keeping the treatment in the prompt and everything else fixed is what makes
    base-vs-arm readable at all: same model, same task, same context, same
    controlled host, one difference.
    """
    if arm == "repomap":
        n = int(cfg.get("arms", "repomap_max_files", 400))
        return task_mod.repo_map(tree, n) + "\n\n---\n\n" + t.prompt
    if arm == "apisurface":
        surface = t.api_surface
        if not surface:
            # Never silently degrade an arm into the base arm — that produces
            # rows labelled `apisurface` that received no treatment, and the
            # arm's conclusion is then drawn from data that does not exist.
            raise task_mod.TaskError(
                f"task {t.name!r} has no api_surface.txt but was scheduled in "
                f"the apisurface arm")
        return ("Reference: the exact API surface you will need, excerpted "
                "verbatim from the library.\n\n" + surface
                + "\n\n---\n\n" + t.prompt)
    return t.prompt


def plan(cfg: Config) -> list[Cell]:
    tasks = [task_mod.load(d) for d in cfg.task_dirs]
    reps = int(cfg.get("round", "reps", 3))
    cells: list[Cell] = []
    for rep in range(1, reps + 1):
        row = [Cell(m, t, arm, rep)
               for m in cfg.models for t in tasks for arm in cfg.arms
               if arm != "apisurface" or t.api_surface]
        random.Random(int(cfg.get("round", "shuffle_seed", 0)) + rep).shuffle(row)
        cells.extend(row)
    return cells


def preflight_all(cfg: Config, log=print) -> bool:
    ok = True
    work = cfg.dir("work_dir")
    for d in cfg.task_dirs:
        t = task_mod.load(d)
        tree = work / f"preflight-{t.name}"
        sha = task_mod.stage(t, tree)
        good, detail = task_mod.preflight(t, tree)
        log(f"[preflight] {t.name}: {'OK' if good else 'BROKEN'} — {detail.splitlines()[0]}")
        if not good:
            log(detail)
            ok = False
        del sha
    return ok


def results_path(cfg: Config) -> Path:
    p = cfg.dir("results_dir") / "results.csv"
    if not p.exists():
        with p.open("w", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FIELDS).writeheader()
    return p


def run_cell(cfg: Config, cell: Cell, clean_avail_mb: int, log=print) -> dict:
    m, t = cell.model, cell.task
    num_ctx = m.num_ctx(cfg.target_ctx)
    stem = f"{m.slug}-{t.name}-{cell.arm}-r{cell.rep}"
    tree = cfg.dir("work_dir") / stem

    log(f"[run] {stem} ctx={num_ctx}/{m.native_ctx}")
    baseline = task_mod.stage(t, tree)

    g = gate.prepare(cfg, m, num_ctx, log=log)
    gate_class = gate.classify(cfg, m, num_ctx, clean_avail_mb) if clean_avail_mb > 0 else "UNKNOWN"
    before = hosttel.probe()

    b = cfg.section("backend")
    w = cfg.section("worker")
    worker = Worker(
        host=b.get("host", ""), model=m.name, num_ctx=num_ctx,
        temperature=float(w.get("temperature", 0.2)),
        manual_tools=m.manual_tools, api_style=b.get("api_style", "ollama"),
        max_iterations=int(w.get("max_iterations", 25)),
        read_max_chars=int(w.get("read_file_max_chars", 60000)),
        bash_timeout_s=int(w.get("bash_timeout_s", 180)),
        repeat_nudge_after=int(w.get("repeat_call_nudge_after", 2)),
        web=cfg.section("web"), log=log)

    started = time.time()
    outcome = worker.run(tree, build_prompt(t, cell.arm, tree, cfg), wall_s=m.wall_s)
    duration = round(time.time() - started, 1)
    after = hosttel.probe()

    # config_ceiling vs native_ceiling. One is a knob we chose and can turn next
    # round; the other is the model's own limit and there is nothing to turn.
    stop = outcome.stop_reason
    if stop == "context_ceiling":
        stop = "native_ceiling" if num_ctx >= m.native_ctx else "config_ceiling"

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    tr = write_transcript(cfg.dir("transcripts_dir") / f"{stem}-{ts}.json",
                          model=m.name, task=t.name, cwd=tree, outcome=outcome)
    diff = task_mod.archive_diff(tree, baseline,
                                 cfg.dir("results_dir") / f"{stem}.diff")
    files_changed = task_mod.changed_files(tree, baseline)

    # verify_passed is TERNARY. "not_run" is not a failure — a run killed at the
    # wall or stopped at a ceiling never reached the point where verify could
    # say anything, and recording that as `no` would blame the model for the
    # instrument. This distinction is what `unverifiable_claim` in the
    # calibration taxonomy is built on.
    if stop in ("timeout", "load_failed", "backend_error"):
        vpass, vlog = "not_run", ""
    else:
        try:
            ok, vlog = task_mod.verify(t, tree, baseline,
                                       int(w.get("verify_timeout_s", 300)))
            vpass = "yes" if ok else "no"
        except Exception as e:                                       # noqa: BLE001
            vpass, vlog = "not_run", f"verify raised: {e}"
    if vlog:
        (cfg.dir("results_dir") / f"{stem}.verify.log").write_text(vlog)

    return {
        "model": m.name, "backend": b.get("name", "local"), "task": t.name,
        "work_class": t.work_class, "language": t.language,
        "arm": cell.arm, "rep": cell.rep,
        "verify_passed": vpass, "duration_s": duration,
        "warmup_s": outcome.warmup_s, "loop_s": outcome.loop_s,
        "timed_out": str(outcome.timed_out).lower(),
        "files_changed": files_changed, "iterations": outcome.iterations,
        "num_ctx": num_ctx, "native_ctx": m.native_ctx, "stop_reason": stop,
        "host_ready": g.host_ready, "gate_need_mb": g.needed_mb,
        "gate_class": gate_class,
        "prompt_tokens": outcome.prompt_tokens,
        "output_tokens": outcome.output_tokens,
        "decode_s": outcome.decode_s, "out_tps": outcome.out_tps,
        "swap_before_mb": before.swap_used_mb, "swap_after_mb": after.swap_used_mb,
        "avail_before_mb": before.available_mb, "avail_after_mb": after.available_mb,
        "load_before": before.load1,
        "transcript": tr.name, "diff": diff.name,
    }


def run_round(cfg: Config, log=print, limit: int | None = None) -> int:
    if not preflight_all(cfg, log=log):
        log("[round] ABORT: at least one fixture failed its preflight guard.")
        return 2

    # Clean-host available memory, measured once, before anything runs. This
    # fixes which models' gates are REACHABLE and which are STRUCTURAL, and the
    # pre-registered falsification test depends on that split being decided in
    # advance.
    gate.restart_server(cfg, log=log)
    time.sleep(5)
    clean = hosttel.probe().available_mb
    log(f"[round] clean-host available memory: {clean}MB")
    for m in cfg.models:
        c = gate.classify(cfg, m, m.num_ctx(cfg.target_ctx), clean) if clean > 0 else "UNKNOWN"
        log(f"[round]   {m.name}: need={gate.threshold_mb(cfg, m, m.num_ctx(cfg.target_ctx))}MB -> {c}")

    cells = plan(cfg)
    if limit:
        cells = cells[:limit]
    out = results_path(cfg)
    log(f"[round] {len(cells)} runs -> {out}")

    for n, cell in enumerate(cells, 1):
        log(f"[round] ({n}/{len(cells)})")
        try:
            row = run_cell(cfg, cell, clean, log=log)
        except Exception as e:                                       # noqa: BLE001
            log(f"[round] run raised, recording as instrument failure: {e}")
            continue
        with out.open("a", newline="") as fh:
            csv.DictWriter(fh, fieldnames=FIELDS).writerow(row)
    return 0
