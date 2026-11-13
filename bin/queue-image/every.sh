#!/usr/bin/env bash
# every.sh: stand-in for launchd StartInterval / StartCalendarInterval inside the container.
#
#   every.sh SECONDS cmd [args...]   run cmd now, then again SECONDS after each start
#   every.sh at HH:MM cmd [args...]  run cmd every day at HH:MM (container TZ); not at start
#
# Like launchd, runs never overlap: if a run lasts longer than SECONDS, the next one starts
# right after it ends. A failing cmd is logged and the loop goes on. TERM/INT stop the child
# and the loop (supervisord sends TERM).
set -u

# Standby: never run the job (the container holds a copy of another machine's state). Sleep until stopped.
case "${QUEUE_STANDBY:-0}" in 1|true|yes|on)
    trap 'exit 0' TERM INT
    while :; do sleep 3600 & wait $!; done ;;
esac

usage() { echo "usage: every.sh SECONDS cmd [args...] | every.sh at HH:MM cmd [args...]" >&2; exit 64; }
[ $# -ge 2 ] || usage

mode=interval
if [ "$1" = "at" ]; then
    [ $# -ge 3 ] || usage
    mode=daily; at=$2; shift 2
    case "$at" in [0-2][0-9]:[0-5][0-9]) ;; *) usage ;; esac
else
    secs=$1; shift
    case "$secs" in ''|*[!0-9]*) usage ;; esac
    [ "$secs" -ge 1 ] || usage
fi

child=
stop=0
on_term() {
    stop=1
    [ -n "$child" ] && kill -TERM "$child" 2>/dev/null
}
trap on_term TERM INT

# sleep that a signal can interrupt
nap() {
    [ "$1" -gt 0 ] || return 0
    sleep "$1" &
    child=$!
    wait "$child" 2>/dev/null
    child=
}

run_once() {
    "$@" &
    child=$!
    wait "$child"
    rc=$?
    child=
    [ "$rc" -eq 0 ] || echo "[every] $(date '+%F %T') exit $rc: $1" >&2
}

while [ "$stop" -eq 0 ]; do
    if [ "$mode" = daily ]; then
        now=$(date +%s)
        next=$(date -d "today $at" +%s)
        [ "$next" -gt "$now" ] || next=$(date -d "tomorrow $at" +%s)
        nap $((next - now))
        [ "$stop" -eq 0 ] || break
        run_once "$@"
        nap 61   # step past the minute so one HH:MM never fires twice
    else
        start=$(date +%s)
        run_once "$@"
        [ "$stop" -eq 0 ] || break
        nap $((secs - ($(date +%s) - start)))
    fi
done
exit 0
