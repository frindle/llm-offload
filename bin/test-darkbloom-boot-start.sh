#!/bin/bash
# Tests for darkbloom-boot-start with launchctl, sysctl, curl and sleep stubbed on PATH (runs anywhere: no launchd,
# no Darkbloom, nothing started). Usage: bash test-darkbloom-boot-start.sh [path/to/darkbloom-boot-start.sh]
set -u
SCRIPT=${1:-$(cd "$(dirname "$0")" && pwd)/darkbloom-boot-start.sh}
pass=0; fail=0
T=$(mktemp -d); trap 'rm -rf "$T"' EXIT
mkdir -p "$T/stub"

cat >"$T/stub/launchctl" <<'EOF'
#!/bin/bash
# state files in $STUB: state, last, prov_word, wd_word, loaded (0/1), kick_rc
case "$1" in
  print-disabled)
    printf 'disabled services = {\n\t\t"io.darkbloom.provider" => %s\n\t\t"io.darkbloom.watchdog" => %s\n\t}\n' \
      "$(cat "$STUB/prov_word")" "$(cat "$STUB/wd_word")" ;;
  print)
    [ "$(cat "$STUB/loaded")" = 1 ] || { echo 'Bad request.' >&2; exit 113; }
    printf 'gui/501/io.darkbloom.provider = {\n\tactive count = 0\n\tstate = %s\n\tprogram = /x/darkbloom\n' "$(cat "$STUB/state")"
    printf '\trequirements = {\n\t\tstate = active\n\t}\n'
    l=$(cat "$STUB/last"); [ -n "$l" ] && printf '\tlast exit code = %s\n' "$l"
    printf '}\n' ;;
  kickstart) echo "$2" >>"$STUB/kicks"; exit "$(cat "$STUB/kick_rc")" ;;
  *) echo "unexpected launchctl $*" >>"$STUB/unexpected" ;;
esac
EOF
cat >"$T/stub/sysctl" <<'EOF'
#!/bin/bash
echo "{ sec = $(( $(date +%s) - $(cat "$STUB/uptime") )), usec = 1 } Mon Nov  9 13:49:24 2026"
EOF
cat >"$T/stub/curl" <<'EOF'
#!/bin/bash
printf '%s' "$(cat "$STUB/http")"
EOF
cat >"$T/stub/sleep" <<'EOF'
#!/bin/bash
n=$(( $(cat "$STUB/sleeps" 2>/dev/null || echo 0) + 1 )); echo "$n" >"$STUB/sleeps"
m=$(cat "$STUB/mount_after"); [ "$m" -ge 0 ] && [ "$n" -ge "$m" ] && mkdir -p "$STUB/cache"
exit 0
EOF
chmod +x "$T/stub/"*

# setup NAME key=value ...: a fresh stub state with defaults (the 2026-11-09 boot), then the overrides
setup() {
  S="$T/$1"; shift; mkdir -p "$S"
  echo 'not running' >"$S/state"; echo 1 >"$S/last"; echo enabled >"$S/prov_word"; echo enabled >"$S/wd_word"
  echo 1 >"$S/loaded"; echo 0 >"$S/kick_rc"; echo 180 >"$S/uptime"; echo 401 >"$S/http"; echo -1 >"$S/mount_after"
  mkdir -p "$S/cache"; touch "$S/cache/models--x"
  printf "model_cache_directory = '%s'\nauto_restart = true\n" "$S/cache" >"$S/provider.toml"
  for kv in "$@"; do k=${kv%%=*}; v=${kv#*=}
    if [ "$k" = nocache ]; then rm -rf "$S/cache"; else printf '%s\n' "$v" >"$S/$k"; fi
  done
}
run() {  # run [ENV=val ...]: runs the script against $S, output in $OUT
  OUT=$(env STUB="$S" PATH="$T/stub:$PATH" DARKBLOOM_TOML="$S/provider.toml" WAIT_S=60 GRACE_S=0 POLL_S=5 \
        PORT_WAIT_S=10 "$@" bash "$SCRIPT" 2>&1); RC=$?
}
kicks() { [ -f "$S/kicks" ] && wc -l <"$S/kicks" | tr -d ' ' || echo 0; }
check() { if eval "$2"; then pass=$((pass+1)); else fail=$((fail+1)); echo "FAIL: $1"; echo "  output: $OUT"; fi; }

setup boot_failed; run
check "failed boot start (exit 1, not running, cache readable): one kickstart" '[ "$(kicks)" = 1 ] && grep -qE "^gui/[0-9]+/io\.darkbloom\.provider$" "$S/kicks"'
check "  ...and it reports the port answering" 'printf "%s" "$OUT" | grep -q "answers on :8000 (http 401)"'
check "  ...exit 0" '[ "$RC" = 0 ]'

setup running state=running; run
check "provider already running: no kickstart" '[ "$(kicks)" = 0 ] && printf "%s" "$OUT" | grep -q "is running"'

setup disabled prov_word=disabled; run
check "provider disabled (a darkbloom stop): no kickstart" '[ "$(kicks)" = 0 ] && printf "%s" "$OUT" | grep -q "disabled (stopped on purpose)"'

setup disabled_old prov_word=true; run
check "provider disabled, older print-disabled wording (true): no kickstart" '[ "$(kicks)" = 0 ]'

setup clean last=0; run
check "last exit 0 (clean stop): no kickstart" '[ "$(kicks)" = 0 ] && printf "%s" "$OUT" | grep -q "clean stop"'

setup never last=; run
check "never exited (no last exit line), not running: kickstart" '[ "$(kicks)" = 1 ]'

setup late uptime=7200; run
check "booted 2 h ago (agent reloaded later): no kickstart, nothing waited" '[ "$(kicks)" = 0 ] && printf "%s" "$OUT" | grep -q "not a boot start" && [ ! -f "$S/sleeps" ]'

setup unloaded loaded=0; run
check "provider job not loaded: no kickstart" '[ "$(kicks)" = 0 ] && printf "%s" "$OUT" | grep -q "not loaded"'

setup nodrive nocache=1; run
check "drive never mounts: gives up after WAIT_S, no kickstart" '[ "$(kicks)" = 0 ] && printf "%s" "$OUT" | grep -q "still not readable after 60s" && [ "$RC" = 0 ]'

setup latemount nocache=1 mount_after=3; run
check "drive mounts after 3 polls: then one kickstart" '[ "$(kicks)" = 1 ] && printf "%s" "$OUT" | grep -q "readable after 15s"'

setup dry; run DRY_RUN=1
check "DRY_RUN: says it would, no kickstart" '[ "$(kicks)" = 0 ] && printf "%s" "$OUT" | grep -q "DRY_RUN: would kickstart"'

setup wdoff wd_word=disabled; run
check "watchdog disabled: logged as a note, provider still started, watchdog left alone" \
  '[ "$(kicks)" = 1 ] && printf "%s" "$OUT" | grep -q "watchdog (io.darkbloom.watchdog) is disabled" && [ ! -f "$S/unexpected" ]'

setup kickfail kick_rc=5; run
check "kickstart fails: logged, exit 0" 'printf "%s" "$OUT" | grep -q "kickstart failed rc=5" && [ "$RC" = 0 ]'

setup noport http=000; run
check "port never answers: says the watchdog and BloomGauge take over, exit 0" '[ "$(kicks)" = 1 ] && printf "%s" "$OUT" | grep -q "did not answer within 10s" && [ "$RC" = 0 ]'

setup notoml; rm -f "$S/provider.toml"; run DARKBLOOM_TOML="$S/none.toml"
check "no provider.toml: falls back to /Volumes/NVMe-Models/hf-cache (absent here): no kickstart" \
  '[ "$(kicks)" = 0 ] && printf "%s" "$OUT" | grep -q "/Volumes/NVMe-Models/hf-cache still not readable"'

setup nested; run
check "nested 'state = active' lines don't read as the job state" '[ "$(kicks)" = 1 ]'

check "no launchctl call other than print, print-disabled, kickstart in any case" '! ls "$T"/*/unexpected >/dev/null 2>&1'

echo "darkbloom-boot-start: $pass passed, $fail failed"
[ "$fail" = 0 ]
