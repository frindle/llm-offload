#!/usr/bin/env bash
# graphify_job.sh -- runs ON THE CPU RUNNER (never on the Mac). Shipped by ~/bin/graphify-lane via tools=.
#   graphify_job.sh setup   (network)    venv with the Mac's exact graphify pins ($JOB_TOOLS/requirements.txt)
#   graphify_job.sh run     (no network) exit 75 if the venv is missing; else restore the Mac mtimes, `graphify update <name>` from the checkout root,
#                                        copy the whitelisted graphify-out files + update.log to $CPU_RUNNER_ARTIFACTS
# Env: DISPATCH_GL_NAME=<project folder name>  DISPATCH_GL_REQ_SHA=<sha of requirements.txt>
set -u
# the runner passes only allow-listed env names (CPU_RUNNER_ENV_ALLOW: DISPATCH_*, ...)
GL_NAME=${DISPATCH_GL_NAME:-${GL_NAME:-}} GL_REQ_SHA=${DISPATCH_GL_REQ_SHA:-${GL_REQ_SHA:-}}
CACHE=/tmp/graphify-lane
VENV=$CACHE/venv-${GL_REQ_SHA:?}
export UV_CACHE_DIR=$CACHE/uv-cache UV_PYTHON_DOWNLOADS=never UV_NO_PROGRESS=1
export HOME=$CACHE/home XDG_CACHE_HOME=$CACHE/home/.cache XDG_CONFIG_HOME=$CACHE/home/.config
mkdir -p "$HOME" "$CACHE"
ART=${CPU_RUNNER_ARTIFACTS:-/tmp/graphify-art-$$}
mkdir -p "$ART"

setup() {
  exec 9>"$CACHE/.lock"; flock 9          # two setups may run at once
  if [ -f "$VENV/.ok" ]; then echo "[setup] venv ready ($VENV)"; return 0; fi
  rm -rf "$VENV"
  uv venv -q --python 3.13 "$VENV" || { echo "[setup] uv venv failed"; exit 11; }
  uv pip install -q --python "$VENV/bin/python" -r "$JOB_TOOLS/requirements.txt" \
    || { echo "[setup] pip install failed"; exit 12; }
  "$VENV/bin/python" -c "import graphify, importlib.metadata as m; print('[setup] graphifyy', m.version('graphifyy'))" \
    || { echo "[setup] import check failed"; exit 13; }
  touch "$VENV/.ok"
  ls "$CACHE" | sed 's/^/[setup] cache: /'
}

run() {
  local name=${GL_NAME:?}
  [ -d "$name" ] || { echo "[run] no $name/ in checkout"; exit 21; }
  # no venv yet: exit 75 so the Mac resubmits once WITH setup (a setup job counts against the runner's lane limit)
  [ -f "$VENV/.ok" ] || { echo "[run] GL_NEED_SETUP venv missing ($VENV)"; exit 75; }
  # the archive gives every file the same mtime; put back the Mac's so manifest.json stays valid there
  "$VENV/bin/python" -I - "$JOB_TOOLS/mtimes.json" "$name" <<'PY' || exit 23
import json, os, sys
m = json.load(open(sys.argv[1])); base = sys.argv[2]; n = 0
for rel, ns in m.items():
    p = os.path.join(base, rel)
    if os.path.isfile(p) and not os.path.islink(p):
        os.utime(p, ns=(ns, ns)); n += 1
print("[run] mtimes restored:", n, "of", len(m))
PY
  export GRAPHIFY_BACKEND=none GRAPHIFY_NO_TIPS=1 GRAPHIFY_NO_BACKUP=1
  unset GRAPHIFY_OUT
  t0=$(date +%s)
  "$VENV/bin/python" -m graphify update "$name" >"$ART/update.log" 2>&1
  rc=$?
  echo "$rc $(( $(date +%s) - t0 ))" >"$ART/update.rc"
  tail -5 "$ART/update.log" | sed 's/^/[run] /'
  mkdir -p "$ART/out"
  for f in graph.json manifest.json GRAPH_REPORT.md graph.html .graphify_labels.json .graphify_labels.json.sig \
           .graphify_labels.pending.json .graphify_analysis.json .graphify_semantic_marker cost.json; do
    [ -f "$name/graphify-out/$f" ] && [ ! -L "$name/graphify-out/$f" ] && cp "$name/graphify-out/$f" "$ART/out/$f"
  done
  ls -la "$ART/out" | sed 's/^/[run] /'
  exit "$rc"
}

case ${1:-} in setup) setup;; run) run;; *) echo "usage: $0 setup|run"; exit 2;; esac
