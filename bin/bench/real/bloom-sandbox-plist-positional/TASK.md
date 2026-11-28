# TASK: bloom-sandbox-plist-positional

## Confirmed defect (observed, not suspected)

`sandbox_plist_status()` finds the Sandbox status file from the LaunchAgent plist's `ProgramArguments`, but only through a `--config <path>` or `--config=<path>` pair. The real installer plist is `/bin/sh -c '... --args agent --config "$config"' <exe> <app> <log> <agent.json>`: the config path appears only as a trailing positional argument, so the pair match never fires and resolution works only by accident of a fallback directory.

## Entry point

`lib/bloom_power_lib.py` `sandbox_plist_status`.

## Required change

Keep the `--config` pair and `--config=` forms working first. When neither is present, use an argument whose file name is `agent.json` (the last such argument wins), and return `status.json` in that file's directory. Non-string arguments in `ProgramArguments` are ignored. It still returns None when nothing matches or the plist cannot be read.

## Scope

Only edit `lib/bloom_power_lib.py`. Do not edit the tests.
