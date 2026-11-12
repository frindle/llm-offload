#!/bin/bash
# Install the daily log-rotate LaunchAgent (user-level, no sudo).
#   log-rotate-install.sh --yes        copy scripts to ~/bin, install + load the agent (04:17 daily, --apply)
#   log-rotate-install.sh --uninstall  unload + remove the agent (scripts, config and logs are kept)
set -u
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UID_=$(id -u); LA="$HOME/Library/LaunchAgents"; L=com.example.log-rotate
BINS="log-rotate.py log_rotate_lib.py log_retention_config.py log-rotate-install.sh"
[ "$(id -u)" = 0 ] && { echo "refusing to run as root: user agent"; exit 1; }
if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$UID_/$L" 2>/dev/null; rm -f "$LA/$L.plist"; echo "removed $L"; exit 0
fi
if [ "${1:-}" != "--yes" ]; then
  echo "Will copy $BINS to ~/bin and load $L (daily 04:17, 'log-rotate.py --apply'). Dry-run first: ~/bin/log-rotate.py"
  echo "Re-run with --yes to apply, --uninstall to remove."; exit 1
fi
mkdir -p ~/bin "$LA"
for f in $BINS; do
  if [ "$REPO/bin/$f" -nt "$HOME/bin/$f" ] || [ ! -e "$HOME/bin/$f" ]; then cp -p "$REPO/bin/$f" "$HOME/bin/$f"; fi
done
chmod +x ~/bin/log-rotate.py ~/bin/log-rotate-install.sh
cp "$REPO/LaunchAgents/$L.plist" "$LA/$L.plist"
launchctl bootout "gui/$UID_/$L" 2>/dev/null
launchctl bootstrap "gui/$UID_" "$LA/$L.plist" && echo "loaded $L"
