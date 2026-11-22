#!/bin/bash
# Run ON UNRAID: move the ollama-queue container to the newest image CI published, keeping all of its settings.
#   ./update.sh           refresh this script, pull the image, recreate the container only if a newer image came down
#   ./update.sh --check   pull and report, change nothing
# Works for both installs: the Unraid template (Docker tab) and docker compose (docker-compose.yml + .env).
# New images come from CI (queue-pre branch -> tag standby, main -> latest); nothing is built here.
set -euo pipefail
SELF="$(cd "$(dirname "$0")" && pwd)/$(basename "$0")"
NAME=${QUEUE_CONTAINER:-ollama-queue}
SRC=https://raw.githubusercontent.com/frindle/llm-offload/main/bin/queue-image/update.sh
CHECK=0
for a in "$@"; do case "$a" in --check) CHECK=1;; --no-self-update) ;; *) echo "unknown option: $a"; exit 2;; esac; done

# 1. Keep this script current (same idea as git pull in the other update.sh scripts).
case " $* " in *" --no-self-update "*) ;; *)
  if tmp=$(mktemp) && curl -fsSL -m 20 "$SRC" -o "$tmp" 2>/dev/null && head -1 "$tmp" | grep -q '^#!/bin/bash' \
     && ! cmp -s "$tmp" "$SELF"; then
    echo "=== this script changed upstream; using the new copy ==="
    cat "$tmp" > "$SELF"; rm -f "$tmp"; exec "$SELF" --no-self-update "$@"
  fi
  rm -f "${tmp:-}";;
esac

docker inspect "$NAME" >/dev/null 2>&1 || { echo "no container named $NAME"; exit 1; }
IMG=$(docker inspect -f '{{.Config.Image}}' "$NAME")
OLD=$(docker inspect -f '{{.Image}}' "$NAME")
# A compose install follows its .env (QUEUE_IMAGE): pull what compose would run, not the tag the container was
# made from, or a tag changed in .env never takes effect.
CDIR=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$NAME")
if [ -n "$CDIR" ] && [ -f "$CDIR/docker-compose.yml" ]; then
  CIMG=$(cd "$CDIR" && { docker compose config --images 2>/dev/null || docker-compose config --images 2>/dev/null; } | grep -m1 'llm-offload-queue' || true)
  [ -n "$CIMG" ] && [ "$CIMG" != "$IMG" ] && { echo "=== compose .env names $CIMG (container was made from $IMG) ==="; IMG=$CIMG; }
fi
case "$IMG" in
  *:pre-*|*@sha256:*) echo "note: $NAME is pinned to one build ($IMG). Set the tag to :standby in its template to follow new builds.";;
esac
rev() { docker image inspect -f '{{index .Config.Labels "org.opencontainers.image.revision"}} built {{.Created}}' "$1" 2>/dev/null | cut -c1-60; }
echo "=== running: $(rev "$OLD") ==="

# 2. Pull.
echo "=== pull $IMG ==="
docker pull -q "$IMG" >/dev/null
NEW=$(docker image inspect -f '{{.Id}}' "$IMG")
if [ "$NEW" = "$OLD" ]; then
  echo "=== already on the newest image ==="
  docker ps -a --filter "name=^${NAME}\$" --format '{{.Names}}  {{.Status}}'
  exit 0
fi
echo "=== newer image: $(rev "$NEW") ==="
[ "$CHECK" = 1 ] && { echo "--check: not recreating"; exit 0; }

# 3. Recreate with the same settings.
DIR=$(docker inspect -f '{{index .Config.Labels "com.docker.compose.project.working_dir"}}' "$NAME")
if [ -n "$DIR" ] && [ -f "$DIR/docker-compose.yml" ]; then
  echo "=== recreate (compose) ==="
  if docker compose version >/dev/null 2>&1; then (cd "$DIR" && docker compose up -d)
  else (cd "$DIR" && docker-compose up -d); fi
else
  U=/usr/local/emhttp/plugins/dynamix.docker.manager/scripts/update_container
  if [ -x "$U" ]; then
    echo "=== recreate (Unraid template) ==="
    "$U" "$NAME"
  else
    echo "The new image is pulled. Finish in the Unraid Docker tab: $NAME > Apply update."
    exit 0
  fi
fi

# 4. Wait for the health check (up to 3 min).
echo "=== waiting for healthy ==="
for _ in $(seq 1 36); do
  s=$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}{{.State.Status}}{{end}}' "$NAME" 2>/dev/null || echo gone)
  case "$s" in healthy|running) break;; unhealthy|exited|dead|gone) break;; esac
  sleep 5
done
echo "=== done: $(rev "$(docker inspect -f '{{.Image}}' "$NAME")") ==="
docker ps -a --filter "name=^${NAME}\$" --format '{{.Names}}  {{.Status}}'
case "$s" in healthy|running) ;; *) echo "NOT healthy ($s): docker logs --tail 40 $NAME"; exit 1;; esac
