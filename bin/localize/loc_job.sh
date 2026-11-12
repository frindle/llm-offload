#!/usr/bin/env bash
# loc_job.sh -- runs ON THE CPU RUNNER (never on the Mac). Shipped by loc_submit.py via tools={"loc_job.sh":...,"query.txt":...}.
#   loc_job.sh setup      (network)   install sweet-search + ColGREP into /tmp/loc-cache and warm their models
#   loc_job.sh run        (no network) index + search the checkout with both tools; raw output -> $CPU_RUNNER_ARTIFACTS
#   loc_job.sh diag       (no network) like run, plus --help dumps, for learning the CLIs
# Env knobs: DISPATCH_LOC_ARMS="sweet colgrep"  DISPATCH_LOC_TASK=<id>  DISPATCH_LOC_TOPK=50
set -u
CACHE=/tmp/loc-cache
SS_VER=2.10.5
CG_VER=v1.8.5
CG_ASSET=colgrep-x86_64-unknown-linux-gnu.tar.xz
export HOME=$CACHE/home
export XDG_CACHE_HOME=$HOME/.cache XDG_CONFIG_HOME=$HOME/.config XDG_DATA_HOME=$HOME/.local/share
export NPM_CONFIG_PREFIX=$CACHE/npm NPM_CONFIG_UPDATE_NOTIFIER=false NPM_CONFIG_FUND=false
export PATH=$CACHE/npm/bin:$CACHE/colgrep:$PATH
export SWEET_SEARCH_TELEMETRY=0 DO_NOT_TRACK=1
export HF_HOME=$XDG_CACHE_HOME/huggingface
# the runner passes only allow-listed env names (CPU_RUNNER_ENV_ALLOW: DISPATCH_*, ...); LOC_* is the old spelling
LOC_ARMS=${DISPATCH_LOC_ARMS:-${LOC_ARMS:-sweet colgrep}} LOC_TOPK=${DISPATCH_LOC_TOPK:-${LOC_TOPK:-50}}
ART=${CPU_RUNNER_ARTIFACTS:-/tmp/loc-art-$$}
mkdir -p "$ART" "$HOME" "$CACHE"

now() { date +%s.%N; }
elapsed() { python3 -c "print(round($2-$1,2))"; }

setup() {
  exec 9>"$CACHE/.lock"; flock 9          # several setups can run at once
  echo "[setup] node $(node -v) npm $(npm -v) uid $(id -u) cache=$CACHE"
  # --- sweet-search (pinned) ---
  SSDIR=$CACHE/npm/lib/node_modules/sweet-search
  if ! grep -q "\"version\": \"$SS_VER\"" "$SSDIR/package.json" 2>/dev/null || [ ! -f "$SSDIR/node_modules/onnxruntime-node/package.json" ] || ! command -v sweet-search >/dev/null; then
    ONNXRUNTIME_NODE_INSTALL=skip npm install -g "sweet-search@$SS_VER" || { echo "[setup] npm install failed"; exit 11; }
  fi
  echo "[setup] sweet-search pkg version: $(grep '"version"' "$SSDIR/package.json" | head -1)"
  # --- ColGREP (pinned, sha256-checked) ---
  if ! [ -x "$CACHE/colgrep/colgrep" ] || ! "$CACHE/colgrep/colgrep" --version 2>&1 | grep -q "${CG_VER#v}"; then
    d=$(mktemp -d "$CACHE/cgdl.XXXXXX")
    base=https://github.com/lightonai/next-plaid/releases/download/$CG_VER
    curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$d/$CG_ASSET" "$base/$CG_ASSET" && curl -fsSL --retry 5 --retry-all-errors --retry-delay 3 -o "$d/$CG_ASSET.sha256" "$base/$CG_ASSET.sha256" \
      || { echo "[setup] colgrep download failed"; exit 12; }
    (cd "$d" && want=$(awk '{print $1}' "$CG_ASSET.sha256") && got=$(sha256sum "$CG_ASSET" | awk '{print $1}') \
       && echo "[setup] colgrep sha256 want=$want got=$got" && [ "$want" = "$got" ]) || { echo "[setup] colgrep sha256 MISMATCH"; exit 13; }
    mkdir -p "$d/x" && tar -xJf "$d/$CG_ASSET" -C "$d/x" --strip-components=1 && mkdir -p "$CACHE/colgrep" \
      && cp "$d/x/colgrep" "$CACHE/colgrep/colgrep.new" && chmod +x "$CACHE/colgrep/colgrep.new" && mv "$CACHE/colgrep/colgrep.new" "$CACHE/colgrep/colgrep" \
      || { echo "[setup] colgrep extract failed"; ls -R "$d/x" | head; exit 14; }
    rm -rf "$d"
  fi
  colgrep --version 2>&1 | head -2 | sed 's/^/[setup] colgrep --version: /'
  # --- warm models on a tiny dummy repo (network allowed here) ---
  if [ ! -f "$CACHE/.warm2-$SS_VER-$CG_VER" ]; then
    w=$(mktemp -d "$CACHE/warm.XXXXXX"); cd "$w" && git init -q . 2>/dev/null
    cat > m.py <<'PY'
def parse_config(path):
    """Read a configuration file and return a dict."""
    return {}

class Server:
    def handle_request(self, req):
        return req
PY
    echo "[setup] warm colgrep"; colgrep init -y . 2>&1 | tail -15; colgrep --json -k 3 "parse configuration file" 2>&1 | head -c 600
    echo "[setup] warm sweet-search"; (sweet-search init --codex 2>&1 | tail -15; echo "[setup] files under HOME newer than colgrep bin:"; find "$HOME" -maxdepth 2 -not -path "*/.cache/*" -not -path "*/.local/*" -newer "$CACHE/colgrep/colgrep" | head -20; sweet-search index 2>&1 | tail -8; sweet-search "parse configuration file" 2>&1 | head -c 600; sweet-search daemon stop 2>&1 | tail -2)
    cd /; rm -rf "$w"
    touch "$CACHE/.warm2-$SS_VER-$CG_VER"
  fi
  du -sh "$CACHE"/* "$HOME/.cache"/* 2>/dev/null | sed 's/^/[setup] du /'
  echo "[setup] done"
}

# run one arm: $1 = arm name; stdout/stderr/time files in $ART/<arm>.*
run_arm() {
  local arm=$1 q; q=$(cat "$JOB_TOOLS/query.txt")
  local top=${LOC_TOPK:-50}
  local wd; wd=$(pwd)
  # per-job index storage so runs never see each other's indices
  local idx; idx=$(mktemp -d /tmp/loc-idx.XXXXXX)
  export XDG_DATA_HOME=$idx
  local t0 t1 t2
  case $arm in
    colgrep)
      t0=$(now); timeout 1500 colgrep init -y . >"$ART/colgrep.index.out" 2>&1; echo $? >"$ART/colgrep.index.rc"; t1=$(now)
      timeout 600 colgrep --json -k "$top" "$q" >"$ART/colgrep.search.json" 2>"$ART/colgrep.search.err"; echo $? >"$ART/colgrep.search.rc"; t2=$(now)
      ;;
    sweet)
      t0=$(now); timeout 3000 sweet-search index >"$ART/sweet.index.out" 2>&1; echo $? >"$ART/sweet.index.rc"; t1=$(now)
      timeout 600 sweet-search "$q" --json --no-banner -k "$top" >"$ART/sweet.search.out" 2>"$ART/sweet.search.err"; echo $? >"$ART/sweet.search.rc"; t2=$(now)
      timeout 60 sweet-search daemon stop >"$ART/sweet.daemon-stop.out" 2>&1
      ;;
  esac
  printf '{"arm":"%s","index_s":%s,"search_s":%s}\n' "$arm" "$(elapsed "$t0" "$t1")" "$(elapsed "$t1" "$t2")" >"$ART/$arm.times.json"
  rm -rf "$idx"
}

run() {
  git status --porcelain | head -3 | sed 's/^/[run] checkout dirty: /'
  for arm in ${LOC_ARMS:-sweet colgrep}; do echo "[run] arm $arm"; run_arm "$arm"; done
  # sweet-search/colgrep leave .sweet-search/ and so on inside the checkout; artifacts only come from $ART
  ls -la "$ART" | head -40
}

diag() {
  { echo "== sweet-search --help"; sweet-search --help; echo "== sweet-search search --help"; sweet-search search --help
    echo "== sweet-search index --help"; sweet-search index --help; echo "== sweet-search daemon --help"; sweet-search daemon --help; } >"$ART/sweet.help.txt" 2>&1
  { echo "== colgrep --help"; colgrep --help; echo "== colgrep init --help"; colgrep init --help; } >"$ART/colgrep.help.txt" 2>&1
  { echo "uid $(id -u) nproc $(nproc)"; node -v; python3 -V; df -h /tmp | tail -1; du -sh "$CACHE"/* "$HOME/.cache"/* "$HOME/.config"/* 2>/dev/null
    echo "net check (should fail in run):"; curl -sS -m 5 -o /dev/null -w '%{http_code}\n' https://huggingface.co 2>&1 | tail -1; } >"$ART/env.txt" 2>&1
  run
  { find "$HOME" -maxdepth 4 -newer "$CACHE/colgrep/colgrep" 2>/dev/null | head -50; ps -eo pid,user,rss,args | grep -i -E 'sweet|colgrep' | grep -v grep; } >"$ART/after.txt" 2>&1
}

case ${1:-} in setup) setup;; run) run;; diag) diag;; *) echo "usage: $0 setup|run|diag"; exit 2;; esac
