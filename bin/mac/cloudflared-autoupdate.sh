#!/bin/sh
# Weekly cloudflared update for the Studio's tunnel (root LaunchDaemon
# com.example.cloudflared-autoupdate). Upgrades Homebrew's cloudflared as the
# brew owner, restarts the tunnel daemon only when the version changed, and
# rolls back to the previous build if the tunnel is not ready 60 s later.
# Installed root-owned to /usr/local/libexec/cloudflared-autoupdate; this copy
# is the source.
set -u
OWNER=owner
BREW=/opt/homebrew/bin/brew
BIN=/opt/homebrew/bin/cloudflared
CELLAR=/opt/homebrew/Cellar/cloudflared
LOG=/var/log/cloudflared-autoupdate.log
SVC=system/com.cloudflare.cloudflared

say() { echo "$(date -u +%FT%TZ) $*" >> "$LOG"; }
as_owner() { /usr/bin/sudo -u "$OWNER" -H env HOMEBREW_NO_INSTALL_CLEANUP=1 HOMEBREW_NO_ENV_HINTS=1 "$@"; }
ready() {
  # cloudflared serves /ready on the first free port of 20241-20245.
  for p in 20241 20242 20243 20244 20245; do
    /usr/bin/curl -s -m 3 "http://127.0.0.1:$p/ready" | /usr/bin/grep -q '"status":200' && return 0
  done
  return 1
}
wait_ready() { i=0; while [ $i -lt 12 ]; do ready && return 0; sleep 5; i=$((i+1)); done; return 1; }

before=$("$BIN" --version 2>/dev/null | /usr/bin/awk '{print $3}')
as_owner "$BREW" update --quiet >/dev/null 2>&1
as_owner "$BREW" upgrade cloudflared >/dev/null 2>&1
after=$("$BIN" --version 2>/dev/null | /usr/bin/awk '{print $3}')

if [ -z "$after" ] || [ "$before" = "$after" ]; then
  say "$before unchanged"
  exit 0
fi

/bin/launchctl kickstart -k "$SVC"
if wait_ready; then
  say "$before -> $after restarted, tunnel ready"
  as_owner "$BREW" cleanup cloudflared >/dev/null 2>&1
  exit 0
fi

# New build never got ready: point the brew link back at the previous build.
if [ -x "$CELLAR/$before/bin/cloudflared" ]; then
  as_owner /bin/ln -sfn "../Cellar/cloudflared/$before/bin/cloudflared" "$BIN"
  /bin/launchctl kickstart -k "$SVC"
  if wait_ready; then say "$before -> $after NOT ready, ROLLED BACK to $before (ready)"
  else say "$before -> $after NOT ready, rolled back to $before, STILL NOT READY"; fi
else
  say "$before -> $after NOT ready, previous build missing, no rollback"
fi
exit 1
