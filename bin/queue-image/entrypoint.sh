#!/usr/bin/env bash
# Container start: check the volumes, write config and secret files from env, then exec supervisord.
# State no longer lives next to the code, so nothing is copied into ~/bin. No secret is ever echoed,
# and `set -x` must never be turned on here.
set -euo pipefail

HOME=${HOME:-/home/queue}
export HOME
export QUEUE_STATE_DIR=${QUEUE_STATE_DIR:-$HOME/state}

log()  { printf '[entrypoint] %s\n' "$*"; }
fail() { printf '[entrypoint] FATAL: %s\n' "$*" >&2; exit 1; }

# ---- volumes -----------------------------------------------------------------------------------
# Real filesystems only: flock (the queue state lock, the daemon lock) and sqlite WAL (the CPU lane)
# do not work on NFS, SMB or FUSE. On Unraid that rules out /mnt/user (shfs); use the cache pool.
DIRS=(
    "$QUEUE_STATE_DIR"
    "$QUEUE_STATE_DIR/ollama-queue-logs"
    "$HOME/.ollama-dispatch"
    "$HOME/dispatch-worktrees"
    "$HOME/Library/Logs"
)
BAD_FS=' nfs nfs4 cifs smb smb2 smbfs fuse fuseblk fuse.shfs fuse.sshfs 9p virtiofs afs ncpfs '

check_dir() {
    local d=$1 fs t
    mkdir -p "$d" 2>/dev/null || fail "cannot create $d (is the volume mounted, and owned by the container user?)"
    t=$(mktemp "$d/.write-test.XXXXXX" 2>/dev/null) || fail "$d is not writable by uid $(id -u)"
    rm -f "$t"
    fs=$(stat -f -c %T "$d" 2>/dev/null || echo unknown)
    case "$BAD_FS" in *" $fs "*) fail "$d is on '$fs', which does not support flock/sqlite WAL reliably. Mount appdata from the cache pool (for example /mnt/cache/appdata/...), not /mnt/user." ;; esac
}

# flock must really exclude: two separate opens of one file, second non-blocking lock must fail.
check_flock() {
    local d=$1
    python3 - "$d" <<'PY' || fail "flock does not work in $1"
import fcntl, os, sys
p = os.path.join(sys.argv[1], ".flock-test")
a = open(p, "w"); b = open(p, "w")
fcntl.flock(a, fcntl.LOCK_EX | fcntl.LOCK_NB)
try:
    fcntl.flock(b, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    os.unlink(p); sys.exit(0)
sys.exit(1)
PY
}

for d in "${DIRS[@]}"; do check_dir "$d"; done
check_flock "$QUEUE_STATE_DIR"
check_flock "$HOME/.ollama-dispatch"

# git worktrees need symlinks and real permissions on the worktree volume
ln -s probe "$HOME/dispatch-worktrees/.link-test" 2>/dev/null || fail "$HOME/dispatch-worktrees does not allow symlinks"
rm -f "$HOME/dispatch-worktrees/.link-test"

# /config holds config.toml; it may be read-only
if [ -d /config ]; then
    [ -r /config ] || fail "/config is not readable"
    [ -f "${QUEUE_CONFIG:-/config/config.toml}" ] || log "WARNING: no config file at ${QUEUE_CONFIG:-/config/config.toml}; running on public defaults"
else
    log "WARNING: /config is not mounted; running on public defaults"
fi

# ---- git identity and GitHub auth ----------------------------------------------------------------
[ -n "${GIT_AUTHOR_NAME:-}" ]  || fail "GIT_AUTHOR_NAME is not set"
[ -n "${GIT_AUTHOR_EMAIL:-}" ] || fail "GIT_AUTHOR_EMAIL is not set"
git config --global user.name  "$GIT_AUTHOR_NAME"
git config --global user.email "$GIT_AUTHOR_EMAIL"
git config --global --add safe.directory '*'

write_secret() {  # write_secret PATH VALUE: 0600 file, dir 0700, value never printed. umask is restored: workers inherit it.
    local path=$1 val=$2 tmp old
    old=$(umask); umask 077
    mkdir -p "$(dirname "$path")"; chmod 700 "$(dirname "$path")"
    tmp=$(mktemp "$path.XXXXXX")
    printf '%s' "$val" > "$tmp"
    chmod 600 "$tmp"
    mv -f "$tmp" "$path"
    umask "$old"
}

if [ -n "${GITHUB_TOKEN:-}" ]; then
    git config --global credential.helper "store --file=$HOME/.config/queue/git-credentials"
    write_secret "$HOME/.config/queue/git-credentials" "https://x-access-token:${GITHUB_TOKEN}@github.com
"
    log "GitHub credential written"
else
    log "WARNING: GITHUB_TOKEN is not set; pushes and private clones will fail"
fi

# ---- secret files the code reads from ~/.config ------------------------------------------------
# HOME is not a volume, so these are rewritten on every start.
[ -z "${CPU_RUNNER_TOKEN:-}" ]   || { write_secret "$HOME/.config/dispatch-cpu-runner/token" "$CPU_RUNNER_TOKEN"; log "wrote dispatch-cpu-runner token file"; }
[ -z "${OBSIDIAN_TOKEN:-}" ]     || { write_secret "$HOME/.config/ollama-worker/obsidian-token" "$OBSIDIAN_TOKEN"; log "wrote obsidian token file"; }
[ -z "${QUEUE_AGENT_TOKEN:-}" ]  || { write_secret "$HOME/.config/ollama-queue/agent-token" "$QUEUE_AGENT_TOKEN"; log "wrote queue-agent token file"; }
# Other secrets stay env-only: DARKBLOOM_API_KEY, QUEUE_API_TOKEN, worker search keys.

# ---- required settings, by name only -------------------------------------------------------------
for v in DARKBLOOM_BASE_URL DARKBLOOM_API_KEY QUEUE_AGENT_URL QUEUE_AGENT_TOKEN CPU_RUNNER_TOKEN QUEUE_API_TOKEN; do
    [ -n "${!v:-}" ] || log "WARNING: $v is not set"
done

# ---- standby: the container runs on a copy of the Mac's state and must not act on it ------------------
# QUEUE_STANDBY=1 (the default): only the API starts (read routes, import, back-test); the queue daemon,
# CPU-lane prefetch, watchers and reaper stay stopped, and nothing notifies or writes to the Mac.
# Set QUEUE_STANDBY=0 and restart the container to go live.
case "${QUEUE_STANDBY:-1}" in
    0|false|no|off) export QUEUE_STANDBY=0 QUEUE_BG_AUTOSTART=true ;;
    *) export QUEUE_STANDBY=1 QUEUE_BG_AUTOSTART=false OLLAMA_QUEUE_NO_NOTIFY=1
       log "STANDBY: background programs stay stopped; write routes answer 503" ;;
esac

log "starting: $*"
exec "$@"
