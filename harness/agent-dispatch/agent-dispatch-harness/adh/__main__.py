"""adh — command line entry point.

    bin/adh doctor      check config, host telemetry, backend, fixtures
    bin/adh probe       ask the backend each model's native context window
    bin/adh gate        show the readiness threshold for each model, now
    bin/adh preflight   run every fixture's guard against a pristine tree
    bin/adh plan        print the dispatch order this config produces
    bin/adh run         run the round
    bin/adh score       behavioural scorers over an existing results.csv
    bin/adh calibrate   build the claim-vs-verify worksheet
    bin/adh falsify     check a round against its pre-registered criteria
"""
from __future__ import annotations

import sys

from . import calibrate as calibrate_mod
from . import config as config_mod
from . import falsify as falsify_mod
from . import gate as gate_mod
from . import hosttel
from . import runner
from . import score as score_mod
from . import task as task_mod
from .worker import probe_native_ctx


def _split_config(argv: list[str]) -> tuple[str | None, list[str]]:
    if "--config" in argv:
        i = argv.index("--config")
        return argv[i + 1], argv[:i] + argv[i + 2:]
    return None, argv


def cmd_doctor(cfg) -> int:
    print(f"config:    {cfg.path}")
    s = hosttel.probe()
    print(f"telemetry: supported={s.supported} platform={s.platform} "
          f"total={s.total_mb}MB available={s.available_mb}MB swap={s.swap_used_mb}MB")
    if not s.supported:
        print("           -> host gating cannot run on this platform; see README "
              "Limitations. Runs will be recorded host_ready=no rather than "
              "silently assumed ready.")
    b = cfg.section("backend")
    print(f"backend:   {b.get('name')} {b.get('host')} style={b.get('api_style')} "
          f"owns_server={b.get('owns_server')}")
    ok = True
    for m in cfg.models:
        try:
            ctx = m.num_ctx(cfg.target_ctx)
            need = gate_mod.threshold_mb(cfg, m, ctx, total_mb=s.total_mb)
            cls = ("STRUCTURAL" if s.available_mb > 0 and need > s.available_mb
                   else "reachable now")
            print(f"model:     {m.name} ctx={ctx}/{m.native_ctx} "
                  f"resident={m.resident_mb}MB gate={need}MB ({cls}) wall={m.wall_s}s")
        except Exception as e:                                       # noqa: BLE001
            print(f"model:     {m.name} PROBLEM: {e}")
            ok = False
    for d in cfg.task_dirs:
        try:
            t = task_mod.load(d)
            print(f"task:      {t.name} [{t.work_class}/{t.language}] "
                  f"restore={len(t.restore_paths)} "
                  f"api_surface={'yes' if t.api_surface else 'no'}")
        except Exception as e:                                       # noqa: BLE001
            print(f"task:      {d} PROBLEM: {e}")
            ok = False
    return 0 if ok else 1


def cmd_probe(cfg) -> int:
    host = cfg.get("backend", "host", "")
    for m in cfg.models:
        native = probe_native_ctx(host, m.name)
        flag = ""
        if native and m.native_ctx and native != m.native_ctx:
            flag = (f"  <-- config says {m.native_ctx}; the config is what the "
                    f"harness uses, so fix it")
        print(f"{m.name:<32} native_ctx={native or 'unknown'}{flag}")
    return 0


def cmd_gate(cfg) -> int:
    s = hosttel.probe()
    print(f"available now: {s.available_mb}MB of {s.total_mb}MB")
    for m in cfg.models:
        ctx = m.num_ctx(cfg.target_ctx)
        need = gate_mod.threshold_mb(cfg, m, ctx, total_mb=s.total_mb)
        verdict = "would dispatch" if s.available_mb >= need else "would WAIT"
        print(f"  {m.name:<30} ctx={ctx:<7} need={need:>7}MB  {verdict}")
    return 0


def cmd_plan(cfg) -> int:
    cells = runner.plan(cfg)
    for i, c in enumerate(cells, 1):
        print(f"{i:>3}. {c.model.name:<30} {c.task.name:<32} {c.arm:<12} rep={c.rep}")
    print(f"\n{len(cells)} runs "
          f"(seed {cfg.get('round', 'shuffle_seed', 0)} — deterministic)")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] in ("-h", "--help", "help"):
        print(__doc__)
        return 0
    cmd, rest = argv[0], argv[1:]
    cfg_path, rest = _split_config(rest)

    # These three read a results file directly and need no config.
    if cmd == "score":
        return score_mod.main(rest)
    if cmd == "falsify":
        return falsify_mod.main(rest)
    if cmd == "calibrate":
        return calibrate_mod.main(rest)
    if cmd == "hosttel":
        return hosttel.main(rest)

    try:
        cfg = config_mod.load(cfg_path)
    except config_mod.ConfigError as e:
        print(f"config error: {e}", file=sys.stderr)
        return 2

    if cmd == "doctor":
        return cmd_doctor(cfg)
    if cmd == "probe":
        return cmd_probe(cfg)
    if cmd == "gate":
        return cmd_gate(cfg)
    if cmd == "preflight":
        return 0 if runner.preflight_all(cfg) else 1
    if cmd == "plan":
        return cmd_plan(cfg)
    if cmd == "run":
        limit = None
        if "--limit" in rest:
            limit = int(rest[rest.index("--limit") + 1])
        return runner.run_round(cfg, limit=limit)

    print(f"unknown command: {cmd}\n{__doc__}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
