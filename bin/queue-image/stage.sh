#!/usr/bin/env bash
# stage.sh: build the image's code context. Copies the files listed in MANIFEST from a ~/bin
# checkout into build/bin/ (the Dockerfile COPYs that). Symlinks are followed, so
# ollama-queue-api.py (a link into the dashboard repo) arrives as a real file.
#
#   queue-image/stage.sh [BIN_DIR]        BIN_DIR defaults to ~/bin
#
# MANIFEST: one path per line, relative to BIN_DIR; blank lines and lines starting with # are skipped.
# Override the list with QUEUE_MANIFEST=/path/to/file. The script stops at the first problem.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
src=${1:-$HOME/bin}
manifest=${QUEUE_MANIFEST:-$here/MANIFEST}
out=$here/build/bin

if [ ! -f "$manifest" ]; then
    echo "stage.sh: no manifest at $manifest" >&2
    echo "  MANIFEST lists the ~/bin files the image needs. It is written on the qd-config branch;" >&2
    echo "  merge it, or point QUEUE_MANIFEST at a list." >&2
    exit 2
fi
[ -d "$src" ] || { echo "stage.sh: not a directory: $src" >&2; exit 2; }

rm -rf "$out"
mkdir -p "$out"
n=0
missing=0
while IFS= read -r line || [ -n "$line" ]; do
    line=${line%%#*}
    line=$(printf '%s' "$line" | sed -e 's/^[[:space:]]*//' -e 's/[[:space:]]*$//')
    [ -n "$line" ] || continue
    case "$line" in /*|*..*) echo "stage.sh: bad path in manifest: $line" >&2; exit 2 ;; esac
    if [ ! -e "$src/$line" ]; then
        echo "stage.sh: listed but missing: $line" >&2
        missing=$((missing + 1))
        continue
    fi
    mkdir -p "$out/$(dirname "$line")"
    cp -L -p "$src/$line" "$out/$line"
    n=$((n + 1))
done < "$manifest"

if [ "$missing" -gt 0 ]; then
    echo "stage.sh: $missing listed file(s) missing from $src; build/bin is incomplete" >&2
    exit 1
fi
[ "$n" -gt 0 ] || { echo "stage.sh: the manifest lists no files" >&2; exit 1; }
echo "stage.sh: staged $n file(s) into $out"
