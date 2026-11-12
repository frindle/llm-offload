#!/bin/bash
# darkbloom-boot-start.sh: at login, start Darkbloom's provider once the model drive is mounted.
#
# Why (2026-11-09): the provider's launch agent (io.darkbloom.provider: RunAtLoad, KeepAlive off) starts at login.
# After the 13:49 restart it started at 13:52:20, before the encrypted NVMe-Models drive had mounted, found no models
# ("Failed to read cache directory ... No models selected.") and quit with exit 1. Nothing retried it: Darkbloom's
# own watchdog (io.darkbloom.watchdog, restarts after 300 s down) had been left disabled in launchd since the
# 12:50 update.
#
# What it does, once per login (its own launch agent com.example.darkbloom-boot-start, RunAtLoad, no KeepAlive):
#   1. waits for the model cache directory (provider.toml's model_cache_directory) to be readable, up to WAIT_S;
#   2. gives the provider GRACE_S to come up by itself;
#   3. kickstarts io.darkbloom.provider ONCE, and only when all of these hold:
#        - the Mac booted under MAX_UPTIME_S ago (a later reload of this agent never starts Darkbloom by surprise),
#        - the provider's launch agent is loaded and enabled (a `darkbloom stop` disables it: that choice is kept),
#        - it is not running,
#        - its last exit was not 0 (it failed; a clean stop is left alone).
#   It also logs when Darkbloom's watchdog is disabled. It never changes that, and never runs a darkbloom command.
# DRY_RUN=1 prints what it would do and changes nothing. Always exits 0 (a one-shot login job must not fail loud).
set -u
DOM=gui/$(id -u)
LABEL=io.darkbloom.provider
WATCHDOG=io.darkbloom.watchdog
TOML=${DARKBLOOM_TOML:-$HOME/.config/darkbloom/provider.toml}
WAIT_S=${WAIT_S:-900}
GRACE_S=${GRACE_S:-30}
POLL_S=${POLL_S:-5}
MAX_UPTIME_S=${MAX_UPTIME_S:-1800}
PORT_WAIT_S=${PORT_WAIT_S:-120}
PORT=${DARKBLOOM_PORT:-8000}
DRY_RUN=${DRY_RUN:-0}

log() { printf '%s darkbloom-boot-start: %s\n' "$(date '+%F %T')" "$*"; }

cache_dir() {
  local d=""
  [ -r "$TOML" ] && d=$(sed -n "s/^[[:space:]]*model_cache_directory[[:space:]]*=[[:space:]]*['\"]\([^'\"]*\)['\"].*/\1/p" "$TOML" | head -1)
  printf '%s\n' "${d:-/Volumes/NVMe-Models/hf-cache}"
}

# word from `launchctl print-disabled`: disabled|enabled (macOS 27) or true|false (older; true = disabled)
disabled_word() { launchctl print-disabled "$DOM" 2>/dev/null | sed -n "s/^[[:space:]]*\"$1\"[[:space:]]*=>[[:space:]]*\([A-Za-z]*\).*/\1/p" | head -1; }
is_disabled() { case "$(disabled_word "$1")" in disabled|true) return 0 ;; *) return 1 ;; esac; }

# top-level "state = ..." and "last exit code = ..." of the job (one tab of indent: nested blocks have more)
job_field() { printf '%s\n' "$1" | sed -n "s/^	$2 = \(.*\)$/\1/p" | head -1; }

boot=$(sysctl -n kern.boottime 2>/dev/null | sed -n 's/.*[{ ]sec = \([0-9][0-9]*\),.*/\1/p')  # "{ sec = N, usec = M } ...": not usec
now=$(date +%s)
if [ -z "$boot" ]; then log "cannot read the boot time: nothing done"; exit 0; fi
up=$((now - boot))
if [ "$up" -gt "$MAX_UPTIME_S" ]; then
  log "booted ${up}s ago (over ${MAX_UPTIME_S}s): not a boot start, nothing done"; exit 0
fi

if is_disabled "$WATCHDOG"; then
  log "note: Darkbloom's watchdog ($WATCHDOG) is disabled in launchd, so nothing restarts the provider if it stops; enable it with: launchctl enable $DOM/$WATCHDOG && launchctl bootstrap $DOM ~/Library/LaunchAgents/$WATCHDOG.plist"
fi

CACHE=$(cache_dir)
waited=0
until [ -d "$CACHE" ] && ls "$CACHE" >/dev/null 2>&1; do
  if [ "$waited" -ge "$WAIT_S" ]; then
    log "model cache $CACHE still not readable after ${WAIT_S}s (drive not mounted or locked): provider not started"; exit 0
  fi
  sleep "$POLL_S"; waited=$((waited + POLL_S))
done
[ "$waited" -gt 0 ] && log "model cache $CACHE readable after ${waited}s"
sleep "$GRACE_S"

info=$(launchctl print "$DOM/$LABEL" 2>/dev/null)
if [ -z "$info" ]; then log "$LABEL is not loaded in $DOM: nothing done"; exit 0; fi
state=$(job_field "$info" state)
last=$(job_field "$info" "last exit code")
if [ "$state" = running ]; then log "$LABEL is running: nothing to do"; exit 0; fi
if is_disabled "$LABEL"; then log "$LABEL is disabled (stopped on purpose): not started"; exit 0; fi
if [ "$last" = 0 ]; then log "$LABEL last exited 0 (a clean stop): not started"; exit 0; fi

if [ "$DRY_RUN" = 1 ]; then
  log "DRY_RUN: would kickstart $LABEL (state=${state:-?}, last exit=${last:-?})"; exit 0
fi
log "starting $LABEL (state=${state:-?}, last exit=${last:-?}, model cache readable)"
launchctl kickstart "$DOM/$LABEL"
rc=$?
if [ "$rc" -ne 0 ]; then log "kickstart failed rc=$rc"; exit 0; fi
w=0
while [ "$w" -lt "$PORT_WAIT_S" ]; do
  code=$(curl -s -m 3 -o /dev/null -w '%{http_code}' "http://127.0.0.1:$PORT/v1/models" 2>/dev/null)
  if [ -n "$code" ] && [ "$code" != 000 ]; then log "provider answers on :$PORT (http $code) after ${w}s"; exit 0; fi
  sleep "$POLL_S"; w=$((w + POLL_S))
done
log "provider started but :$PORT did not answer within ${PORT_WAIT_S}s (a model load can take longer; BloomGauge and the watchdog take it from here)"
exit 0
