#!/usr/bin/env bash
# throttle.sh SECONDS cmd [args...]: launchd ThrottleInterval for a KeepAlive program.
# Waits until SECONDS have passed since the last start of this same cmd, then execs it, so
# signals from supervisord reach the program itself. A crash loop restarts at most once per SECONDS.
set -u

# Standby: never run the job (the container holds a copy of another machine's state). Sleep until stopped.
case "${QUEUE_STANDBY:-0}" in 1|true|yes|on)
    trap 'exit 0' TERM INT
    while :; do sleep 3600 & wait $!; done ;;
esac
[ $# -ge 2 ] || { echo "usage: throttle.sh SECONDS cmd [args...]" >&2; exit 64; }
secs=$1; shift
case "$secs" in ''|*[!0-9]*) echo "throttle.sh: SECONDS must be a number" >&2; exit 64 ;; esac
key=$(printf '%s' "$*" | cksum | cut -d' ' -f1)
stamp="${TMPDIR:-/tmp}/throttle-$key"
now=$(date +%s)
last=0
[ -r "$stamp" ] && last=$(cat "$stamp" 2>/dev/null || echo 0)
case "$last" in ''|*[!0-9]*) last=0 ;; esac
wait=$((last + secs - now))
[ "$wait" -le 0 ] || sleep "$wait"
date +%s > "$stamp" 2>/dev/null || true
exec "$@"
