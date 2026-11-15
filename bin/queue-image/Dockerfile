# Queue image: the job queue (daemon, API, CPU lane, watchers) from ~/bin, run by supervisord.
# Build context is this directory (queue-image/). Run stage.sh first: it fills build/bin/ from MANIFEST.
# No docker build is run from the Mac; CI (image.yml) builds it.

# Node comes from the official image so the version is exact. Node 26 is what the Mac runs (v26.7)
# and what the target repos' verify.sh expects. Node 25+ no longer ships corepack, so pnpm is
# installed with npm below.
FROM node:26-bookworm-slim AS node

# Python 3.14 is the Mac plists' interpreter (/opt/homebrew/bin/python3 = 3.14.7).
FROM python:3.14-slim-bookworm

ARG PUID=99
ARG PGID=100

# System packages, and why:
#   git, openssh-client   workers clone, branch, commit and push (GitHub is the source of truth)
#   curl                  the container healthcheck
#   ca-certificates       https to GitHub, Darkbloom, Ollama
#   tini                  PID 1: reaps the many short child processes the workers start
#   supervisor            replaces the 9 launchd jobs (supervisord.conf)
#   procps, lsof          verify-orphan-reap and cpu-prefetch call `ps -axo` and `lsof -d cwd`
#   build-essential, pkg-config   native npm modules (better-sqlite3 in resell-tracker) and pip wheels with no build
#   openssl               prisma engines (resell-tracker, prisma ^7) look for libssl at install time
#   tzdata                log-rotate runs at 04:17 local time; TZ comes from env
#   bash                  already present; every.sh, throttle.sh, entrypoint.sh need bash 4+
RUN apt-get update && apt-get install -y --no-install-recommends \
        git openssh-client curl ca-certificates tini supervisor procps lsof \
        build-essential pkg-config openssl tzdata \
    && rm -rf /var/lib/apt/lists/*

# Node 26 + npm, copied from the official image.
COPY --from=node /usr/local/bin/node /usr/local/bin/node
COPY --from=node /usr/local/lib/node_modules /usr/local/lib/node_modules
RUN ln -sf ../lib/node_modules/npm/bin/npm-cli.js /usr/local/bin/npm \
    && ln -sf ../lib/node_modules/npm/bin/npx-cli.js /usr/local/bin/npx \
    # pnpm: bloomgauge. tsx: the verify scripts' fallback when a repo has not vendored it (^4.23.13 pinned in the target repos).
    && npm install -g pnpm tsx@^4.23.13 \
    && npm cache clean --force

# Python packages: the Mac's own set at the Mac's versions (requirements-mac.txt), since jobs' checks fall back to the
# system python3 and their verdicts must match the Mac's. It includes what the queue code imports that is not stdlib:
#   pyyaml, jinja2   mini-swe-runner.py
#   pypdf, pdfplumber, trafilatura   ollama-worker.py (each import is optional there; docsum and web fetch use them)
# uv: the CPU lane's setup phase (lane_remote.py) builds venvs with it (a separate binary on the Mac).
COPY requirements-mac.txt /tmp/requirements-mac.txt
RUN pip install --no-cache-dir -r /tmp/requirements-mac.txt uv \
    && rm /tmp/requirements-mac.txt \
    && python3 -c "import yaml, jinja2, pypdf, pdfplumber, trafilatura, flask, fastapi, httpx, pytest"

# mini-swe-agent and litellm in their own venv, at the Mac's version (mini_swe_agent 2.4.6, litellm 1.105.1 as
# resolved there). MINI_BIN points at it, so it does not sit under the volume-mounted ~/.ollama-dispatch.
# ruff: the lint gate that lane_remote installs next to mini.
RUN python3 -m venv /opt/mini-venv \
    && /opt/mini-venv/bin/pip install --no-cache-dir "mini-swe-agent==2.4.6" ruff \
    && /opt/mini-venv/bin/python -c "import litellm, yaml, jinja2, minisweagent"

# Repo toolchains the Mac's checks call by absolute path, in venvs of their own (uv fetches the interpreter; the dir is
# readable by the queue user). The back-test's path_map points the Mac paths at them (queue-backtest B3):
#   /opt/repo-toolchain/difvenv   Python 3.12 + the web packages of /Volumes/NVMe-Models/image-models/.difvenv (fastapi
#                                 0.141.1, starlette 1.6.0, uvicorn 0.52.4, python-multipart 0.0.32). pet-portrait-studio's verify.sh.
# Python 3.13 under UV_PYTHON_INSTALL_DIR: the graph job script (graphify_job.sh setup, `uv venv --python 3.13`) builds
# its own venv from the Mac's pins, as on the Unraid runner. The back-test's B5 test runner finds it with
# `uv python find 3.13`; uv never downloads one at run time.
ENV UV_PYTHON_INSTALL_DIR=/opt/uv-python
COPY requirements-difvenv.txt /tmp/
RUN uv venv /opt/repo-toolchain/difvenv --python 3.12 \
    && uv pip install --python /opt/repo-toolchain/difvenv/bin/python --no-cache -r /tmp/requirements-difvenv.txt \
    && /opt/repo-toolchain/difvenv/bin/python -c "import fastapi, starlette, uvicorn, multipart, httpx, PIL; from fastapi.testclient import TestClient" \
    && uv python install 3.13 \
    && UV_PYTHON_DOWNLOADS=never uv python find 3.13 \
    && chmod -R a+rX /opt/uv-python /opt/repo-toolchain \
    && rm /tmp/requirements-difvenv.txt

# The unprivileged user. Unraid's default owner of appdata is 99:100 (nobody:users). GID 100 already
# exists in Debian (users), so reuse a group when the GID is taken.
RUN (getent group "${PGID}" >/dev/null || groupadd -g "${PGID}" queue) \
    && useradd -m -d /home/queue -s /bin/bash -u "${PUID}" -g "${PGID}" queue

# Code: staged by stage.sh from the files in MANIFEST. Owned by queue, so a script that writes next to
# itself does not crash (state is meant to live in QUEUE_STATE_DIR, not here).
COPY --chown=queue build/bin/ /opt/queue/bin/
# ts-mutator's typescript dependency, from its lockfile (dev dependencies left out). verify-relevance.py runs these
# sidecars from /opt/queue/bin/ts-mutator, and the TS verify scripts parse with ts-parse.mjs there.
RUN cd /opt/queue/bin/ts-mutator \
    && npm ci --omit=dev --no-audit --no-fund \
    && npm cache clean --force \
    && chown -R queue:"${PGID}" /opt/queue/bin/ts-mutator \
    && printf 'const x: number = 1;\n' > /tmp/probe.ts \
    && node /opt/queue/bin/ts-mutator/ts-parse.mjs /tmp/probe.ts \
    && rm /tmp/probe.ts
COPY every.sh throttle.sh /usr/local/bin/
# The container checks the workflow runs in the built image (parity-ci.sh), with the scanner they use.
COPY parity-ci.sh parity_ci.py scan-private-info.py scan-allow.txt every.sh /opt/queue/ci/
COPY supervisord.conf /etc/queue/supervisord.conf
COPY entrypoint.sh /usr/local/bin/queue-entrypoint.sh
RUN chmod 0755 /usr/local/bin/every.sh /usr/local/bin/throttle.sh /usr/local/bin/queue-entrypoint.sh \
    && ln -s /opt/queue/bin /home/queue/bin \
    && chown -h queue:"${PGID}" /home/queue/bin \
    && mkdir -p /config && chown queue:"${PGID}" /config

ENV HOME=/home/queue \
    USER=queue \
    PATH=/home/queue/.local/bin:/usr/local/bin:/usr/bin:/bin \
    QUEUE_IN_CONTAINER=1 \
    QUEUE_STANDBY=1 \
    QUEUE_BG_AUTOSTART=false \
    QUEUE_CONFIG=/config/config.toml \
    QUEUE_STATE_DIR=/home/queue/state \
    MINI_BIN=/opt/mini-venv/bin/mini \
    PYTHONUNBUFFERED=1 \
    PYTHONPYCACHEPREFIX=/tmp/pycache

USER queue
WORKDIR /home/queue
EXPOSE 7684

# /  is a static page: cheap, and it needs the API thread pool, not the queue state.
HEALTHCHECK --interval=30s --timeout=10s --start-period=90s --retries=3 \
    CMD curl -fsS -o /dev/null http://127.0.0.1:7684/ || exit 1

# SIGTERM from `docker stop` reaches supervisord, which gives the daemon 60 s (stopwaitsecs).
# Set the stop timeout above that (compose: stop_grace_period 90s).
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/queue-entrypoint.sh"]
CMD ["supervisord", "-c", "/etc/queue/supervisord.conf"]
