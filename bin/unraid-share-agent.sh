#!/bin/bash
# Single agent for the Unraid `data` SMB share on this Mac. Replaces three
# separate LaunchAgents: smb-keepalive-data, remount-data-share, and
# mount-ollama-models.
#
#   1. healthy mount  -> light touch, keeps the SMB session from going idle
#   2. mount down     -> remount once unraid-host is reachable again
#   3. mount present  -> point OLLAMA_MODELS at the share, but see the guard below
#
# WHY IT LIVES IN ~/bin: the old smb-keepalive-data.sh sat under ~/Desktop, which
# macOS TCC protects, so launchd could never execute it -- it had been failing with
# exit=126 on every run since it was created, which is why share drops went
# unnoticed until something broke.
MOUNT_POINT="/Volumes/data"
SMB_HOST="unraid-host.local"
SMB_USER="user"
MODELS_SUBDIR="ollama-models"

if mount | grep -q "on $MOUNT_POINT "; then
  stat "$MOUNT_POINT" >/dev/null 2>&1
else
  # Only act once the host is actually reachable, so an outage doesn't mean a
  # Keychain prompt every cycle.
  nc -z -G3 "$SMB_HOST" 445 2>/dev/null || exit 0
  # /Volumes/data is pre-created and owned by the user (one-time `sudo mkdir` +
  # `chown`), which is what makes this work from a user LaunchAgent -- a user
  # process cannot mkdir in /Volumes itself, and `open smb://` does not attach
  # reliably from launchd.
  #
  # ...and macOS DELETES that directory when a mount goes away, which makes the
  # line above a single point of failure this agent cannot repair itself. Real
  # incident 2026-08-23 09:08 -> 2026-08-24 22:00: the share dropped, the
  # mountpoint was reaped, and all 36h of 60s cycles logged the same generic
  # "mount attempt failed" that a genuine server-side failure produces. The two
  # need completely different fixes, so say which one this is.
  # com.example.mountpoint-keeper (root LaunchDaemon) restores the directory;
  # if it is still missing here, that daemon is not loaded.
  if [ ! -d "$MOUNT_POINT" ]; then
    echo "$(date '+%F %T') MOUNTPOINT MISSING: $MOUNT_POINT does not exist and this agent cannot create it (needs root). Check: sudo launchctl list | grep mountpoint-keeper. Manual fix: sudo mkdir -p $MOUNT_POINT && sudo chown user:wheel $MOUNT_POINT"
    exit 1
  fi
  PASS=$(security find-internet-password -a "$SMB_USER" -s "$SMB_HOST" -w 2>/dev/null)
  [ -n "$PASS" ] || { echo "$(date '+%F %T') no keychain credential"; exit 1; }
  # Keep stderr instead of discarding it: "Permission denied", "Connection
  # refused" and "File exists" are three different problems with three different
  # fixes, and the old code threw all three away. Scrub the password out of the
  # text first -- this log is not treated as a secret.
  if ERR=$(mount_smbfs "//${SMB_USER}:${PASS}@${SMB_HOST}/data" "$MOUNT_POINT" 2>&1 >/dev/null); then
    echo "$(date '+%F %T') remounted $MOUNT_POINT"
  else
    ERR=${ERR//"$PASS"/[redacted]}
    echo "$(date '+%F %T') mount attempt failed: ${ERR:-no error text}"; exit 1
  fi
fi

# --- OLLAMA_MODELS, with a hard safety guard -------------------------------
# Real incident 2026-08-22: restarting Ollama to pick up a changed OLLAMA_MODELS
# killed an in-flight dispatch and cost six models in a bake-off. That was a
# login-only script; running the same logic every 60s makes hitting a bad moment
# far more likely. So: never restart while Ollama is actually working.
# ── CHANGED 2026-08-23: SERVE FROM LOCAL, KEEP UNRAID AS THE STORE ──────────
# This pointed OLLAMA_MODELS at the SHARE, so Ollama read every model straight
# off SMB. Measured that day: SMB 299 MB/s vs local SSD 2927 MB/s, and far worse
# under memory pressure -- qwen3-coder-next (51.7GB) could not finish loading
# inside a 900s warmup budget and killed a positive-control run outright. The
# worker's own note states it: Ollama's model-load path over SMB hangs
# indefinitely, while a plain file COPY from the same share does not.
#
# The share still has to be mounted -- it is the model STORE, and
# bakeoff-v8-stage.py copies from it and evicts when done -- but Ollama serves
# from the local cache. The mount check above is therefore still load-bearing:
# no share means nothing to stage from.
#
# Guard kept: $WANT must exist, or we would repeat the 2026-08-22 incident where
# pointing at a missing directory stopped Ollama starting at all.
# ── CHANGED 2026-09-01: THE LOCAL STORE MOVED TO NVMe ──────────────────────
# This hardcoded $HOME/.ollama/models, correct until the 4TB NVMe landed and the
# real store became /Volumes/NVMe-Models/ollama-models (222G). After the move
# this agent -- which ticks every 60s -- kept resetting OLLAMA_MODELS back to
# ~/.ollama/models, now EMPTY, and restarting Ollama, which regenerates the
# LaunchAgent plist from that value and makes it durable. Ollama served ZERO
# models and every dispatch died in ensure_model_cached with "not found in SMB
# source". It killed a full 10-arm bake-off on 2026-09-01: every arm failed
# identically, which reads exactly like model incapacity and is nothing of the
# kind. Note mount-ollama-models.sh carried the SAME hardcoded path and was
# fixed in the same change -- BOTH agents must agree or they fight each other.
WANT="/Volumes/NVMe-Models/ollama-models"
# Guard hardened: the old check was `mkdir -p "$WANT"; [ -d "$WANT" ]`, which can
# never fail -- it CREATES the directory it then tests for, so an unmounted NVMe
# silently became an empty store. Require the store to actually hold manifests.
[ -d "$WANT/manifests" ] || exit 0
[ "$(launchctl getenv OLLAMA_MODELS 2>/dev/null)" = "$WANT" ] && exit 0

BUSY=0
# Studio no longer runs Ollama (dispatch is on Darkbloom): "busy" now means a queue
# worker/runner is active, not an Ollama model resident on :11434.
pgrep -f "ollama-worker\.py|code-review-agent\.py|studio-research\.py|bakeoff-" >/dev/null 2>&1 && BUSY=1
if [ "$BUSY" = "1" ]; then
  echo "$(date '+%F %T') OLLAMA_MODELS needs updating but Ollama is busy -- deferring"
  exit 0
fi
launchctl setenv OLLAMA_MODELS "$WANT"
/opt/homebrew/bin/brew services restart ollama 2>/dev/null
echo "$(date '+%F %T') set OLLAMA_MODELS=$WANT and restarted ollama (was idle)"

# --- sleep-setting drift check -------------------------------------------------
# Idle sleep kills long inference jobs: when the Mac sleeps, Ollama/Clamshell/SSH
# all go unreachable, and a multi-hour bake-off or vision round dies silently.
# `pmset sleep 0` persists across reboots, but macOS UPDATES and the Energy Saver
# pane can both quietly reset it. Warn if it ever comes back non-zero rather than
# discovering it via another lost overnight run.
SLEEP_VAL=$(pmset -g custom 2>/dev/null | sed -n '/AC Power/,/Battery/p' | awk '/^ *sleep/{print $2; exit}')
if [ -n "$SLEEP_VAL" ] && [ "$SLEEP_VAL" != "0" ]; then
  echo "$(date '+%F %T') WARNING: idle sleep is back on (sleep=$SLEEP_VAL) -- long jobs will die. Fix: sudo pmset -a sleep 0"
fi
